"""Built-in Prometheus/OpenMetrics exporter + live JSON snapshot endpoint.

Ships inside the TUI (idea credited to nvitop-exporter, which needs a
separate package + process; the default port 9835 matches it so a Grafana
board built for nvitop-exporter works unchanged).  Implemented with the
Python standard library only.

Threading model: the HTTP thread NEVER touches pynvml, curses, or the
Config -- the TUI thread publishes a finished Payload after each tick and
the server serves the last-complete frame.  publish() is a single
attribute assignment (atomic under the GIL); stop() must be called from
the main thread (shutdown() from a handler thread deadlocks).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def parse_export_spec(spec: str) -> tuple[str, int]:
    """'9835' | '127.0.0.1:9835' -> (host, port).  Raises ValueError."""
    s = str(spec).strip()
    if ":" in s:
        host, _, port_s = s.rpartition(":")
        if not host or not port_s:
            raise ValueError(f"bad --export spec: {spec!r}")
        host = host.strip("[]")
    else:
        host, port_s = "127.0.0.1", s
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"bad --export port: {spec!r}") from None
    if not (1 <= port <= 65535):
        raise ValueError(f"--export port out of range: {port}")
    return host, port


@dataclass
class Payload:
    env: dict
    samples: list
    procs: list
    insights: dict
    expose_processes: bool = True
    version: str = ""
    paused: bool = False
    # (plug name, live watts | None, session Wh | None) per metered plug
    plugs: list = dataclasses_field(default_factory=list)


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(v) -> str | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf: omit
        return None
    return repr(float(f))


def render_metrics(p: Payload) -> str:
    """Pure: Payload -> Prometheus text format.  Golden-string tested.
    Families are grouped so each # HELP / # TYPE appears exactly once."""
    families: dict[str, tuple[str, str, list[str]]] = {}

    def series(name: str, help_text: str, mtype: str, labels: str, value) -> None:
        v = _num(value)
        if v is None:
            return  # absent metric omits the series entirely (never NaN)
        fam = families.setdefault(name, (help_text, mtype, []))
        fam[2].append(f"zionstats_{name}{{{labels}}} {v}")

    gpu_name = {}
    for d in getattr(p, "_devices", None) or []:
        gpu_name[d.get("index")] = d.get("name", "?")
    from .nvml_layer import Sample  # noqa: F401  (typing only)

    for s in p.samples:
        labels = (
            f'gpu="{s.index}",name="{_esc(gpu_name.get(s.index) or s.name or "?")}",'
            f'uuid="{_esc(s.uuid or "")}",vendor="{_esc(getattr(s, "vendor", "") or "nvidia")}"'
        )
        series("gpu_utilization_percent",
                "GPU utilization (%)", "gauge", labels, s.util_gpu)
        series("gpu_memory_utilization_percent",
                "Memory-bandwidth utilization (%)", "gauge", labels, s.util_mem)
        series("gpu_memory_used_bytes", "VRAM used (bytes)",
                "gauge", labels, s.mem_used)
        series("gpu_memory_total_bytes", "VRAM total (bytes)",
                "gauge", labels, s.mem_total)
        series("gpu_power_watts", "Instantaneous power draw (W)",
                "gauge", labels, s.power_w)
        series("gpu_power_limit_watts", "Enforced power limit (W)",
                "gauge", labels, s.power_limit_w)
        series("gpu_temperature_celsius", "GPU temperature (C)",
                "gauge", labels + ',sensor="gpu"', s.temp_gpu)
        series("gpu_temperature_celsius", "Memory temperature (C)",
                "gauge", labels + ',sensor="memory"', s.temp_mem)
        series("gpu_temperature_celsius", "Hottest-part temperature (C)",
                "gauge", labels + ',sensor="hottest"', s.temp_gpu_max)
        series("gpu_clock_sm_mhz", "SM clock (MHz)", "gauge",
                labels, s.clocks_sm_mhz)
        series("gpu_clock_mem_mhz", "Memory clock (MHz)", "gauge",
                labels, s.clocks_mem_mhz)
        series("gpu_fan_percent", "Fan speed (%)", "gauge", labels, s.fan_pct)
        series("gpu_fan_rpm", "Fan speed (RPM)", "gauge", labels, s.fan_rpm)
        series("gpu_pcie_link_gen", "PCIe link generation", "gauge",
                labels, s.pcie_gen_cur)
        series("gpu_pcie_link_width", "PCIe link width (lanes)",
                "gauge", labels, s.pcie_width_cur)
        series("gpu_pcie_rx_bytes_per_second",
                "PCIe read throughput (bytes/s; NVML reports KB/s)",
                "gauge", labels, s.pcie_rx_kbps * 1000 if s.pcie_rx_kbps is not None else None)
        series("gpu_pcie_tx_bytes_per_second",
                "PCIe write throughput (bytes/s; NVML reports KB/s)",
                "gauge", labels, s.pcie_tx_kbps * 1000 if s.pcie_tx_kbps is not None else None)
        series("gpu_pcie_replay_counter_total",
                "PCIe replay counter since driver load (counter, not gauge)",
                "counter", labels, s.pcie_replay)
        series("gpu_throttle_bitmask",
                "NVML clocksThrottleReasons bitmask (nvml.h ABI)", "gauge",
                labels, s.throttle_bits)
        series("gpu_energy_consumption_joules_total",
                "Cumulative energy since driver load (J; resets on driver reload)",
                "counter", labels, s.energy_mj / 1000.0 if s.energy_mj is not None else None)
        series("gpu_ecc_errors_total", "Volatile ECC errors",
                "counter", labels + ',type="corrected"', s.ecc_sbe)
        series("gpu_ecc_errors_total", "Volatile ECC errors",
                "counter", labels + ',type="uncorrected"', s.ecc_dbe)
        ins = p.insights.get(s.index)
        if ins is not None:
            series("gpu_session_energy_watt_hour",
                    "Session energy (Wh; since zionstats start or last reset)",
                    "gauge", labels, ins.session_wh)
            series("gpu_power_avg_watts", "Session average power (W)",
                    "gauge", labels, ins.avg_w)
            series("gpu_power_peak_watts", "Session peak power (W)",
                    "gauge", labels, ins.peak_w)
            series("gpu_pcie_saturation_ratio",
                    "Measured PCIe throughput / theoretical link (0..1)",
                    "gauge", labels, ins.pcie_sat)

    if p.expose_processes:
        for pr in p.procs:
            labels = (f'gpu="{pr.gpu_index}",pid="{pr.pid}",'
                      f'user="{_esc(pr.user or "?")}",type="{pr.type}",'
                      f'command="{_esc((pr.command or "?")[:80])}"')
            series("process_memory_bytes",
                    "Per-process GPU memory (bytes)", "gauge", labels, pr.mem_used)
            for kind, v in (("sm", pr.sm_pct), ("mem", pr.mem_pct),
                            ("enc", pr.enc_pct), ("dec", pr.dec_pct)):
                series("process_utilization_percent",
                        "Per-process GPU utilization (%)", "gauge",
                        labels + f',kind="{kind}"', v)

    for plug_name, watts, wh in getattr(p, "plugs", None) or []:
        plabels = f'plug="{_esc(plug_name)}"'
        series("plug_power_watts", "Plug wall power (W)", "gauge", plabels, watts)
        series("plug_energy_watt_hour",
                "Plug session energy (Wh; since zionstats start)",
                "gauge", plabels, wh)

    out: list[str] = []
    for name, (help_text, mtype, rows) in families.items():
        out.append(f"# HELP zionstats_{name} {help_text}")
        out.append(f"# TYPE zionstats_{name} {mtype}")
        out.extend(rows)
    out.append(f'zionstats_build_info{{version="{_esc(p.version or "?")}"}} 1')
    if p.paused:
        out.append("zionstats_paused 1")
    return "\n".join(out) + "\n"


def render_snapshot(p: Payload) -> str:
    """Live JSON: the recorder frame shape, reusable by any JSON consumer."""
    from .recorder import frame_to_dict

    return json.dumps(frame_to_dict(p.env, list(p.samples), list(p.procs)))


class _MetricsServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the last-published frame."""

    daemon_threads = True

    def __init__(self, addr: tuple[str, int], payload: Payload) -> None:
        super().__init__(addr, _MetricsHandler)
        self.payload = payload


class _MetricsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "zionstats"
    token: str = ""  # set by Exporter.start()

    def _authorized(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if not self._authorized():
            self._send(401, b"unauthorized\n", "text/plain")
            return
        payload = getattr(self.server, "payload", None)
        path = self.path.split("?", 1)[0]
        if payload is None:  # never observed in practice: start() seeds a frame
            self._send(503, b"no frame published yet\n", "text/plain")
            return
        try:
            if path == "/metrics":
                body = render_metrics(payload).encode("utf-8")
                self._send(200, body, CONTENT_TYPE)
            elif path == "/snapshot":
                body = render_snapshot(payload).encode("utf-8")
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found\n", "text/plain")
        except Exception:  # never kill the thread; report and continue
            try:
                self._send(500, b"exporter error\n", "text/plain")
            except Exception:
                pass

    def log_message(self, fmt: str, *args) -> None:  # silence request spam
        pass


class Exporter:
    def __init__(self, host: str, port: int, version: str = "",
                 expose_processes: bool = True, token: str = "") -> None:
        self.host = host
        self.port = port
        self._expose_processes = expose_processes
        self._token = token
        self._version = version
        self._httpd: _MetricsServer | None = None
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/metrics"

    def start(self) -> bool:
        try:
            self._httpd = _MetricsServer(
                (self.host, self.port),
                Payload(env={}, samples=[], procs=[], insights={},
                        expose_processes=self._expose_processes,
                        version=self._version),
            )
            _MetricsHandler.token = self._token
        except OSError:
            return False
        self._thread = Thread(target=self._httpd.serve_forever,
                              name="zionstats-exporter", daemon=True)
        self._thread.start()
        return True

    def publish(self, env: dict, samples: list, procs: list,
                insights: dict, paused: bool = False,
                plugs: list | None = None) -> None:
        if self._httpd is None:
            return
        self._httpd.payload = Payload(
            env=env, samples=list(samples), procs=list(procs),
            insights=dict(insights), expose_processes=self._expose_processes,
            version=self._version, paused=paused,
            plugs=list(plugs or []),
        )

    def stop(self) -> None:
        """Main-thread only (shutdown() deadlocks from a handler thread)."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
