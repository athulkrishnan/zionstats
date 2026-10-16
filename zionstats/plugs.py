"""Smart-plug backends for blower-fan control.

Design stance: the plugs stay paired with your voice assistant (Alexa etc.)
exactly as before -- zionstats drives them **locally on the LAN**, so cooling
automation keeps working when the internet is down and no cloud account sees
your telemetry.  Heavy SDKs are lazy-imported only when their backend is
actually configured.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any


class PlugError(RuntimeError):
    pass


class Plug:
    name = "plug"

    def on(self) -> None: ...
    def off(self) -> None: ...
    def status(self) -> bool | None:  # None = unknown/not reachable
        return None
    def close(self) -> None: ...


def _http_json(url: str, timeout: float = 4.0, method: str = "GET",
               body: dict | None = None, headers: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
    except Exception as e:
        raise PlugError(f"{url.split('/')[2]}: {e}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# ---------------------------------------------------------------- mock


class MockPlug(Plug):
    """For --demo and tests: records commands, simulates a real device,
    can be scripted to fail (fail_next) for retry-path testing."""

    def __init__(self, name: str = "blower (mock)", fail_next: int = 0) -> None:
        self.name = name
        self.state = False
        self.commands: list[str] = []
        self.fail_next = fail_next

    def _maybe_fail(self, cmd: str) -> None:
        self.commands.append(cmd)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise PlugError("mock plug scripted failure")

    def on(self) -> None:
        self._maybe_fail("on")
        self.state = True

    def off(self) -> None:
        self._maybe_fail("off")
        self.state = False

    def status(self) -> bool | None:
        return self.state


# ---------------------------------------------------------------- tuya


class TuyaPlug(Plug):
    """Tuya-based plugs (the vast majority of Alexa-compatible plugs).
    Local LAN control via tinytuya; needs dev_id + local_key (one-time
    `python -m tinytuya wizard`)."""

    def __init__(self, name: str, dev_id: str, address: str, local_key: str,
                 version: float = 3.3) -> None:
        try:
            import tinytuya  # optional dependency
        except ImportError as e:
            raise PlugError("backend 'tuya' needs: pip install tinytuya") from e
        self.name = name
        self._dev = tinytuya.OutletDevice(dev_id, address or "Auto", local_key,
                                          version=float(version),
                                          connection_timeout=2,
                                          connection_retry_limit=1,
                                          connection_retry_delay=0)

    def _cmd(self, fn) -> None:
        try:
            result = fn()
        except Exception as e:
            raise PlugError(f"tuya {self.name}: {e}") from e
        # tinytuya returns error dicts instead of raising on network
        # failure -- surface them so the verify/retry loop is real
        if isinstance(result, dict) and result.get("Error"):
            raise PlugError(str(result["Error"]))

    def on(self) -> None:
        self._cmd(self._dev.turn_on)

    def off(self) -> None:
        self._cmd(self._dev.turn_off)

    def status(self) -> bool | None:
        try:
            data = self._dev.status()
        except Exception:
            return None  # unreachable -- caller decides policy
        if not isinstance(data, dict):
            return None
        if data.get("Error"):
            return None  # tinytuya error dict -> unreachable
        dps = data.get("dps", {})
        for k in ("1", 1, "20"):
            if k in dps:
                return bool(dps[k])
        return None  # no readback DP on this firmware


# ---------------------------------------------------------------- kasa


class KasaPlug(Plug):
    """TP-Link Kasa plugs via python-kasa (local protocol)."""

    def __init__(self, name: str, host: str) -> None:
        try:
            import kasa  # optional dependency  # noqa: F401 (availability probe)
        except ImportError as e:
            raise PlugError("backend 'kasa' needs: pip install python-kasa") from e
        self.name = name
        self._host = host
        self._dev = None

    def _connect(self):
        if self._dev is None:
            import asyncio

            from kasa import Device

            async def _c():
                dev = await Device.connect(host=self._host)
                await dev.update()
                return dev

            self._dev = asyncio.run(_c())
        return self._dev

    def _run(self, coro_fn) -> None:
        import asyncio

        try:
            asyncio.run(coro_fn())
        except Exception as e:
            raise PlugError(f"kasa {self.name}: {e}") from e

    def on(self) -> None:
        dev = self._connect()
        self._run(dev.turn_on)

    def off(self) -> None:
        dev = self._connect()
        self._run(dev.turn_off)

    def status(self) -> bool | None:
        try:
            dev = self._connect()
            return bool(dev.is_on)
        except Exception:
            return None


# ---------------------------------------------------------------- shelly


class ShellyPlug(Plug):
    """Shelly plugs/relays: Gen1 REST + Gen2 RPC, plain HTTP, no SDK."""

    def __init__(self, name: str, host: str, gen: int = 1, channel: int = 0) -> None:
        self.name = name
        self.host = host.rstrip("/")
        self.gen = int(gen)
        self.channel = int(channel)

    def on(self) -> None:
        self._set(True)

    def off(self) -> None:
        self._set(False)

    def _set(self, val: bool) -> None:
        if self.gen == 2:
            _http_json(
                f"{self.host}/rpc/Switch.Set?id={self.channel}&on={'true' if val else 'false'}"
            )
        else:
            _http_json(f"{self.host}/relay/{self.channel}?turn={'on' if val else 'off'}")

    def status(self) -> bool | None:
        try:
            if self.gen == 2:
                d = _http_json(f"{self.host}/rpc/Switch.GetStatus?id={self.channel}")
            else:
                d = _http_json(f"{self.host}/relay/{self.channel}")
            return bool(d.get("output")) if isinstance(d, dict) else None
        except PlugError:
            return None


# ---------------------------------------------------------------- tasmota


class TasmotaPlug(Plug):
    """Tasmota-flashed plugs: stock HTTP command endpoint."""

    def __init__(self, name: str, host: str) -> None:
        self.name = name
        self.host = host.rstrip("/")

    def on(self) -> None:
        _http_json(f"{self.host}/cm?cmnd=Power%20ON")

    def off(self) -> None:
        _http_json(f"{self.host}/cm?cmnd=Power%20OFF")

    def status(self) -> bool | None:
        try:
            d = _http_json(f"{self.host}/cm?cmnd=Power")
            if isinstance(d, dict):
                return str(d.get("Power", "")).upper() == "ON"
        except PlugError:
            pass
        return None


# ---------------------------------------------------------------- ha


class HAPlug(Plug):
    """Home Assistant REST entity (the hub route: Alexa -> HA -> anything).
    Needs a long-lived access token."""

    def __init__(self, name: str, base_url: str, token: str, entity_id: str) -> None:
        self.name = name
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"}
        self.entity = entity_id

    def _service(self, service: str) -> None:
        _http_json(f"{self.base}/api/services/switch/{service}", method="POST",
                   body={"entity_id": self.entity}, headers=self.headers)

    def on(self) -> None:
        self._service("turn_on")

    def off(self) -> None:
        self._service("turn_off")

    def status(self) -> bool | None:
        try:
            d = _http_json(f"{self.base}/api/states/{self.entity}", headers=self.headers)
            if isinstance(d, dict):
                return d.get("state") == "on"
        except PlugError:
            pass
        return None


# ---------------------------------------------------------------- factory

_BACKENDS = {
    "mock": lambda c: MockPlug(c.get("name", "mock")),
    "tuya": lambda c: TuyaPlug(c.get("name", "tuya"), c["id"], c.get("address", "Auto"),
                               c["key"], float(c.get("version", 3.3))),
    "kasa": lambda c: KasaPlug(c.get("name", "kasa"), c["host"]),
    "shelly": lambda c: ShellyPlug(c.get("name", "shelly"), c["host"],
                                   int(c.get("gen", 1)), int(c.get("channel", 0))),
    "tasmota": lambda c: TasmotaPlug(c.get("name", "tasmota"), c["host"]),
    "ha": lambda c: HAPlug(c.get("name", "ha"), c["base_url"], c["token"], c["entity_id"]),
}


def build_plug(cfg: dict) -> Plug:
    backend = str(cfg.get("backend", "")).lower()
    factory = _BACKENDS.get(backend)
    if factory is None:
        raise PlugError(f"unknown plug backend {backend!r} (known: {', '.join(_BACKENDS)})")
    try:
        return factory(cfg)
    except KeyError as e:
        raise PlugError(f"plug {cfg.get('name', '?')}: missing config field {e}") from e
    except RuntimeError as e:
        raise PlugError(f"plug {cfg.get('name', '?')}: {e}") from e


def build_plugs(cfgs: list[dict]) -> tuple[list[Plug], list[str]]:
    plugs, errors = [], []
    for c in cfgs or []:
        try:
            plugs.append(build_plug(c))
        except PlugError as e:
            errors.append(str(e))
    return plugs, errors
