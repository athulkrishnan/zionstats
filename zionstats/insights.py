"""Derived metrics + alert engine: the researcher layer on top of raw NVML."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .nvml_layer import Sample

_PCIE_GBPS = {1: 0.25, 2: 0.5, 3: 1.0, 4: 2.0, 5: 4.0, 6: 8.0}  # per lane, GB/s


@dataclass
class Alert:
    ts: float
    level: str  # "warn" | "crit" | "info"
    msg: str


@dataclass
class DeviceInsight:
    verdict: str = "ACTIVE"
    verdict_color: str = "accent"
    clock_deficit_mhz: int = 0
    thermal_headroom: int | None = None
    thermal_rate_c_per_min: float = 0.0
    pcie_theoretical_gbps: float | None = None
    pcie_gbps: float = 0.0
    pcie_sat: float = 0.0
    session_wh: float = 0.0
    avg_w: float | None = None
    peak_w: float | None = None
    idle_leak_w: float | None = None
    mem_frac: float = 0.0


class Insights:
    """One global engine; per-device accumulators keyed by GPU index."""

    def __init__(self, on_alert=None) -> None:
        self.alerts: deque[Alert] = deque(maxlen=200)
        self._on_alert = on_alert
        self._st: dict[int, dict] = {}

    def reset(self) -> None:
        self._st.clear()
        self.alerts.clear()

    # ------------------------------------------------------------ helpers

    def _alert(self, level: str, msg: str) -> None:
        a = Alert(time.time(), level, msg)
        self.alerts.append(a)
        if self._on_alert:
            self._on_alert(a)

    def _state(self, idx: int) -> dict:
        return self._st.setdefault(
            idx,
            {"energy_wh": 0.0, "last_ts": None, "last_energy_mj": None,
             "power_sum": 0.0, "power_n": 0, "peak_w": None,
             "idle_w_sum": 0.0, "idle_n": 0,
             "throttled": set(), "temp_warned": False, "mem_warned": False,
             "dbe": 0, "temp_hist": deque(maxlen=32)},
        )

    # ------------------------------------------------------------ update

    def update(self, s: Sample, temp_hist: deque | None) -> DeviceInsight:
        st = self._state(s.index)
        ins = DeviceInsight()
        now = s.ts

        # ---- energy (NVML mJ counter preferred, integration fallback)
        if s.energy_mj is not None and st["last_energy_mj"] is not None:
            d = s.energy_mj - st["last_energy_mj"]
            if 0 <= d < 3.6e9:
                st["energy_wh"] += d / 3.6e6
        elif s.power_w is not None and st["last_ts"] is not None:
            dt = max(0.0, min(10.0, now - st["last_ts"]))
            st["energy_wh"] += s.power_w * dt / 3600.0
        if s.energy_mj is not None:
            st["last_energy_mj"] = s.energy_mj
        st["last_ts"] = now
        ins.session_wh = st["energy_wh"]

        # ---- power stats
        if s.power_w is not None:
            st["power_sum"] += s.power_w
            st["power_n"] += 1
            if st["peak_w"] is None or s.power_w > st["peak_w"]:
                st["peak_w"] = s.power_w
            if s.util_gpu is not None and s.util_gpu < 5:
                st["idle_w_sum"] += s.power_w
                st["idle_n"] += 1
            ins.avg_w = st["power_sum"] / st["power_n"]
            ins.peak_w = st["peak_w"]
            ins.idle_leak_w = st["idle_w_sum"] / st["idle_n"] if st["idle_n"] else None

        # ---- clocks
        if s.clocks_sm_mhz is not None and s.clocks_max_sm_mhz is not None:
            if s.util_gpu is not None and s.util_gpu > 50:
                ins.clock_deficit_mhz = max(0, s.clocks_max_sm_mhz - s.clocks_sm_mhz)

        # ---- thermal
        if s.temp_gpu is not None and s.temp_slowdown is not None:
            ins.thermal_headroom = s.temp_slowdown - s.temp_gpu
        if temp_hist and len(temp_hist) >= 4:
            xs = list(temp_hist)
            span = (xs[-1][0] - xs[0][0]) or 1.0
            ins.thermal_rate_c_per_min = (xs[-1][1] - xs[0][1]) / span * 60.0

        # ---- pcie
        gen = s.pcie_gen_max or 4
        width = s.pcie_width_max or 16
        per_lane = _PCIE_GBPS.get(gen, 2.0)
        ins.pcie_theoretical_gbps = per_lane * width
        rx = (s.pcie_rx_kbps or 0) / 1e6  # KB/s -> GB/s
        tx = (s.pcie_tx_kbps or 0) / 1e6
        ins.pcie_gbps = rx + tx
        theo = ins.pcie_theoretical_gbps or 1.0
        ins.pcie_sat = max(0.0, min(1.0, ins.pcie_gbps / theo))

        # ---- memory
        if s.mem_total:
            ins.mem_frac = s.mem_used / s.mem_total

        # ---- verdict (priority order matters)
        throttled = set(s.throttle)
        if s.util_gpu is not None and s.util_gpu < 5:
            ins.verdict, ins.verdict_color = "IDLE", "dim"
        elif "HW-Slowdown" in throttled or "HW-Thermal" in throttled or "HW-Brake" in throttled:
            ins.verdict, ins.verdict_color = "THERMALLY-LIMITED", "crit"
        elif "SW-Thermal" in throttled:
            ins.verdict, ins.verdict_color = "THERMALLY-LIMITED", "warn"
        elif "SW-Power-Cap" in throttled:
            ins.verdict, ins.verdict_color = "POWER-LIMITED", "warn"
        elif (
            s.util_gpu is not None and s.util_mem is not None
            and s.util_mem > 80 and s.util_gpu < s.util_mem - 15
        ):
            ins.verdict, ins.verdict_color = "MEMORY-BOUND", "accent2"
        elif s.util_gpu is not None and s.util_gpu >= 60:
            ins.verdict, ins.verdict_color = "COMPUTE-BOUND", "ok"
        else:
            ins.verdict, ins.verdict_color = "ACTIVE", "accent"

        # ---- alert edges
        hw_names = {"HW-Slowdown", "HW-Thermal", "HW-Brake"}
        hw = throttled & hw_names
        sw = throttled & {"SW-Power-Cap", "SW-Thermal", "Sync-Boost"}
        new_hw, cleared_hw = hw - st["throttled"], (st["throttled"] & hw_names) - throttled
        new_sw = sw - st["throttled"]
        if new_hw:
            self._alert("crit", f"GPU{i0(s)}: hardware throttle engaged ({', '.join(sorted(hw))})")
        if new_sw:
            self._alert("warn", f"GPU{i0(s)}: {', '.join(sorted(new_sw))} active")
        if cleared_hw:
            self._alert("info", f"GPU{i0(s)}: hardware throttle cleared ({', '.join(sorted(cleared_hw))})")
        st["throttled"] = throttled

        if (
            ins.thermal_headroom is not None and ins.thermal_headroom <= 5
            and not st["temp_warned"]
        ):
            st["temp_warned"] = True
            self._alert("warn", f"GPU{i0(s)}: within 5 C of thermal slowdown limit")
        if ins.thermal_headroom is not None and ins.thermal_headroom > 15:
            st["temp_warned"] = False

        if ins.mem_frac > 0.92 and not st["mem_warned"]:
            st["mem_warned"] = True
            self._alert("warn", f"GPU{i0(s)}: memory above 92% ({ins.mem_frac:.0%})")
        if ins.mem_frac < 0.85:
            st["mem_warned"] = False

        if s.ecc_dbe is not None:
            if s.ecc_dbe > st["dbe"]:
                self._alert("crit", f"GPU{i0(s)}: +{s.ecc_dbe - st['dbe']} uncorrected ECC errors")
            st["dbe"] = s.ecc_dbe

        return ins


def i0(s: Sample) -> int:
    return s.index


class AutoScale:
    """Rolling y-max with exponential decay for graph tiles (btop scales its
    network graphs the same way): vmax follows peaks up instantly and bleeds
    back down slowly, so idle-after-load graphs stay readable but honest.
    The legend prints the live vmax so the axis is never silently rescaled."""

    def __init__(self, decay: float = 0.997, floor_v: float = 1.0,
                 ceil_v: float | None = None) -> None:
        self.decay = decay
        self.floor_v = max(1e-9, floor_v)
        self.ceil_v = ceil_v
        self.vmax = floor_v

    def update(self, v: float | None) -> float:
        if v is None:
            return self.vmax
        target = max(float(v), self.vmax * self.decay, self.floor_v)
        if self.ceil_v is not None:
            target = min(target, self.ceil_v)
        self.vmax = target
        return self.vmax
