"""Config: flat v1 keys + additive v0.2 sections ([weather], [cooling]).

Old config files keep working: missing sections fall back to defaults, and
the defaults are everything-off for actuation (v0.1 behavior preserved).
"""

from __future__ import annotations

import os

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # py3.9/3.10
    import tomli as tomllib

from pathlib import Path
from typing import Any

_KNOWN_THEMES: set[str] = set()  # filled lazily to avoid a UI import at config load

DEFAULTS = {
    "theme": "predawn",
    "interval": 1.0,
    "layout": "auto",
    "fahrenheit": False,
    "paint_bg": True,
    "sort": "pid",
    "filter_me": False,
    "filter_regex": "",
    "graph_style": "btop",
    "readonly": False,
    "auto_scale": True,
    "mouse": True,
    "panels_detail": True,
    "panels_climate": False,
    "panels_alerts": True,
    "panels_procs": True,
}

WEATHER_DEFAULTS = {
    "enabled": True,
    "lat": 0.0,             # set your location: --location LAT,LON[,LABEL] or here
    "lon": 0.0,
    "label": "",
    "refresh_min": 20.0,
}

EXPORT_DEFAULTS = {
    "enabled": False,        # opt-in; --export enables for the session
    "host": "127.0.0.1",     # do NOT bind 0.0.0.0 on untrusted LANs
    "port": 9835,            # matches nvitop-exporter (Grafana drop-in)
    "expose_processes": True,
}

COOLING_DEFAULTS = {
    "enabled": False,        # opt-in only; also flip mode to dry_run/auto
    "mode": "off",           # off | dry_run | auto | manual
    "trigger_temp": 82.0,
    "resume_temp": 78.0,
    "min_delta_c": 8.0,
    "min_on_s": 120.0,
    "min_off_s": 180.0,
    "max_duty_min": 90.0,
    "trigger": "temp",       # temp | usage (usage: ON at load, OFF after idle_off_s)
    "usage_util": 5.0,       # % GPU util that counts as "in use"
    "idle_off_s": 600.0,     # continuous idle seconds before OFF (usage trigger)
    "plugs": [],             # [[cooling.plugs]] entries, see README
}


def config_path() -> Path:
    override = os.environ.get("ZION_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "zionstats" / "config.toml"


def _quote(v) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Config:
    def __init__(self) -> None:
        self.values: dict[str, Any] = dict(DEFAULTS)
        self.weather: dict[str, Any] = dict(WEATHER_DEFAULTS)
        self.cooling: dict[str, Any] = dict(COOLING_DEFAULTS)
        self.presets: dict[str, dict[str, Any]] = {}
        self.export: dict[str, Any] = dict(EXPORT_DEFAULTS)
        p = config_path()
        if p.exists():
            try:
                data = tomllib.loads(p.read_text())
                for k in DEFAULTS:
                    if k in data:
                        self.values[k] = data[k]
                for k in WEATHER_DEFAULTS:
                    if k in data.get("weather", {}):
                        self.weather[k] = data["weather"][k]
                sec = data.get("cooling", {})
                for k in COOLING_DEFAULTS:
                    if k in sec:
                        self.cooling[k] = sec[k]
                sec = data.get("export", {})
                for k in EXPORT_DEFAULTS:
                    if k in sec:
                        self.export[k] = sec[k]
                raw_presets = data.get("presets", {})
                if isinstance(raw_presets, dict):
                    self.presets = {str(k): dict(v) for k, v in raw_presets.items()
                                    if isinstance(v, dict)}
                # coerce last, so file-loaded section values are sanitized too
                self._coerce()
            except Exception:
                pass

    def _coerce(self) -> None:
        """Hand-edited TOML must not crash the TUI at startup."""
        try:
            self.values["interval"] = max(0.2, min(30.0, float(self.values["interval"])))
        except (TypeError, ValueError):
            self.values["interval"] = DEFAULTS["interval"]
        for key in ("theme", "layout", "sort"):
            if not isinstance(self.values[key], str):
                self.values[key] = DEFAULTS[key]
        if self.values["theme"] not in _KNOWN_THEMES:
            self.values["theme"] = DEFAULTS["theme"]
        if self.values["layout"] not in ("auto", "hero", "grid", "compact"):
            self.values["layout"] = "auto"
        if self.values["sort"] not in ("pid", "user", "mem", "sm", "cpu"):
            self.values["sort"] = "pid"
        try:
            wlat, wlon = float(self.weather["lat"]), float(self.weather["lon"])
            self.weather["lat"], self.weather["lon"] = wlat, wlon
        except (TypeError, ValueError, KeyError):
            self.weather["lat"] = WEATHER_DEFAULTS["lat"]
            self.weather["lon"] = WEATHER_DEFAULTS["lon"]
        try:
            self.weather["refresh_min"] = float(self.weather["refresh_min"])
        except (TypeError, ValueError, KeyError):
            self.weather["refresh_min"] = WEATHER_DEFAULTS["refresh_min"]
        for key in ("trigger_temp", "resume_temp", "min_delta_c",
                    "min_on_s", "min_off_s", "max_duty_min",
                    "usage_util", "idle_off_s"):
            try:
                self.cooling[key] = float(self.cooling[key])
            except (TypeError, ValueError):
                self.cooling[key] = COOLING_DEFAULTS[key]
        if self.values.get("graph_style") not in ("btop", "block", "tty", "braille", "ascii"):
            self.values["graph_style"] = DEFAULTS["graph_style"]
        self.values["readonly"] = bool(self.values.get("readonly", False))
        for k in ("auto_scale", "mouse", "panels_detail", "panels_climate",
                  "panels_alerts", "panels_procs"):
            self.values[k] = bool(self.values.get(k, DEFAULTS[k]))
        # a stored regex must still compile, or it would break every startup
        rx = self.values.get("filter_regex", "")
        if not isinstance(rx, str):
            rx = ""
        elif rx:
            import re

            try:
                re.compile(rx, re.IGNORECASE)
            except re.error:
                rx = ""
        self.values["filter_regex"] = rx
        if self.cooling.get("mode") not in ("off", "dry_run", "auto", "manual"):
            self.cooling["mode"] = COOLING_DEFAULTS["mode"]
        if self.cooling.get("trigger") not in ("temp", "usage"):
            self.cooling["trigger"] = COOLING_DEFAULTS["trigger"]
        if not isinstance(self.cooling.get("plugs", []), list):
            self.cooling["plugs"] = []
        for k in ("enabled", "expose_processes"):
            self.export[k] = bool(self.export.get(k, EXPORT_DEFAULTS[k]))
        if not isinstance(self.export.get("host"), str):
            self.export["host"] = EXPORT_DEFAULTS["host"]
        try:
            self.export["port"] = int(self.export.get("port", EXPORT_DEFAULTS["port"]))
        except (TypeError, ValueError):
            self.export["port"] = EXPORT_DEFAULTS["port"]
        if not (1 <= self.export["port"] <= 65535):
            self.export["port"] = EXPORT_DEFAULTS["port"]
        # presets: [presets.<name>] with layout/theme/graph_style/interval/
        # panels/key -- garbage fields fall back to the live defaults
        for name, pr in list(self.presets.items()):
            if not isinstance(pr, dict):
                del self.presets[name]
                continue
            if pr.get("layout") not in ("auto", "hero", "grid", "compact"):
                pr.pop("layout", None)
            if not isinstance(pr.get("theme"), str):
                pr.pop("theme", None)  # theme validity checked at apply time
            if pr.get("graph_style") not in ("btop", "block", "tty", "braille", "ascii"):
                pr.pop("graph_style", None)
            try:
                pr["interval"] = max(0.2, min(30.0, float(pr["interval"])))
            except (KeyError, TypeError, ValueError):
                pr.pop("interval", None)
            panels = pr.get("panels")
            if isinstance(panels, list):
                pr["panels"] = [p for p in panels
                                if p in ("detail", "climate", "alerts", "procs")]
            else:
                pr.pop("panels", None)
            pkey = pr.get("key")
            if not (isinstance(pkey, str) and len(pkey) == 1 and pkey.isprintable()
                    and not pkey.isspace()):
                pr.pop("key", None)

    def __getattr__(self, k: str):
        try:
            return self.values[k]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(k) from e

    def set(self, key: str, value) -> None:
        """Flat key ('theme') or dotted section key ('cooling.mode')."""
        if "." in key:
            sec, sub = key.split(".", 1)
            table = {"weather": self.weather, "cooling": self.cooling,
                     "export": self.export}.get(sec)
            if table is not None and sub in table:
                table[sub] = value
                self.save()
            return
        if key in DEFAULTS:
            self.values[key] = value
            self.save()

    def save(self) -> None:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# zionstats config", ""]
        for k, v in self.values.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {_quote(v)}")
            else:
                lines.append(f"{k} = {v}")
        lines += ["", "[weather]"]
        for k, v in self.weather.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {_quote(v)}")
            else:
                lines.append(f"{k} = {v}")
        lines += ["", "[export]"]
        for k, v in self.export.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {_quote(v)}")
            else:
                lines.append(f"{k} = {v}")
        lines += ["", "[cooling]"]
        plugs = self.cooling.get("plugs", [])
        for k, v in self.cooling.items():
            if k == "plugs":
                continue
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f"{k} = {_quote(v)}")
            else:
                lines.append(f"{k} = {v}")
        for plug in plugs:
            lines += ["", "[[cooling.plugs]]"]
            for k, v in plug.items():
                if isinstance(v, bool):
                    lines.append(f"{k} = {'true' if v else 'false'}")
                elif isinstance(v, (int, float)):
                    lines.append(f"{k} = {v}")
                else:
                    lines.append(f"{k} = {_quote(v)}")
        for name, pr in self.presets.items():
            safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_") or "preset"
            lines += ["", f"[presets.{safe}]"]
            for k, v in pr.items():
                if k == "panels" and isinstance(v, list):
                    lines.append("panels = [" + ", ".join(_quote(x) for x in v) + "]")
                elif isinstance(v, bool):
                    lines.append(f"{k} = {'true' if v else 'false'}")
                elif isinstance(v, (int, float)):
                    lines.append(f"{k} = {v}")
                else:
                    lines.append(f"{k} = {_quote(v)}")
        p.write_text("\n".join(lines) + "\n")
        try:
            os.chmod(p, 0o600)  # config can hold plug tokens / local keys
        except OSError:
            pass
