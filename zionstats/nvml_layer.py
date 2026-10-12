"""Data layer: DeviceProvider protocol + local NVML provider + demo provider.

Sampling uses the batched NVML FieldValues API where the driver exposes the
fields, falling back to individual getters per metric (and to None when the
hardware doesn't support a metric at all -- nothing here may raise at tick
time, or the TUI dies mid-session).
"""

from __future__ import annotations

import math
import os
import random
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

NOT_SUPPORTED = object()

# ----------------------------------------------------------------- types


@dataclass
class Sample:
    ts: float
    index: int
    uuid: str = ""
    name: str = "?"
    arch: str = "?"
    compute_cap: str = ""
    pstate: str = ""
    util_gpu: float | None = None
    util_mem: float | None = None
    util_enc: float | None = None
    util_dec: float | None = None
    mem_used: int = 0
    mem_total: int = 0
    mem_reserved: int | None = None
    bar1_used: int | None = None
    bar1_total: int | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    power_default_w: float | None = None
    power_min_w: float | None = None
    power_max_w: float | None = None
    clocks_sm_mhz: int | None = None
    clocks_mem_mhz: int | None = None
    clocks_video_mhz: int | None = None
    clocks_max_sm_mhz: int | None = None
    energy_mj: int | None = None
    throttle_bits: int = 0
    throttle: list[str] = field(default_factory=list)
    temp_gpu: int | None = None
    temp_mem: int | None = None
    temp_slowdown: int | None = None
    temp_shutdown: int | None = None
    fan_pct: int | None = None
    pcie_gen_cur: int | None = None
    pcie_gen_max: int | None = None
    pcie_width_cur: int | None = None
    pcie_width_max: int | None = None
    pcie_rx_kbps: int | None = None
    pcie_tx_kbps: int | None = None
    ecc_sbe: int | None = None
    ecc_dbe: int | None = None
    mig_mode: str | None = None
    mig_instances: int = 0
    display_active: bool | None = None
    persistence: bool | None = None
    compute_mode: str | None = None

    def merged_over(self, base: "Sample") -> "Sample":
        """Fill None fields of self from base (used by replay/demo merging)."""
        return replace(self, **{
            k: getattr(self, k) if getattr(self, k) is not None else getattr(base, k)
            for k in self.__dataclass_fields__
        })


@dataclass
class GpuProcess:
    pid: int
    gpu_index: int
    type: str = "C"
    mem_used: int = 0
    sm_pct: float | None = None
    mem_pct: float | None = None
    enc_pct: float | None = None
    dec_pct: float | None = None
    cpu_pct: float | None = None
    user: str = "?"
    command: str = "?"


class ProviderError(RuntimeError):
    pass


# ------------------------------------------------------------- nvml provider


class NvmlProvider:
    """Live provider over pynvml.  Every metric is best-effort."""

    def __init__(self, only: list[int] | None = None) -> None:
        import pynvml  # nvidia-ml-py

        self.nv = pynvml
        try:
            pynvml.nvmlInit()
        except pynvml.NVMLError as e:
            raise ProviderError(f"NVML init failed: {e}") from e
        self._only = set(only) if only else None
        self._handles: list[tuple[int, object]] = []
        n = pynvml.nvmlDeviceGetCount()
        for i in range(n):
            if self._only and i not in self._only:
                continue
            self._handles.append((i, pynvml.nvmlDeviceGetHandleByIndex(i)))
        self._last_seen_util_ts: dict[int, int] = {}
        self._energy0: dict[int, int | None] = {}
        self._fast_ids = self._resolve_fast_fields()

    # -- fast-path field resolution -------------------------------------

    # Stable NVML field IDs (nvml.h ABI).  pynvml doesn't export the
    # NVML_FI_DEV_* names, so we carry the numbers and prefer any attr
    # the installed binding does expose.
    _FIELD_IDS: tuple[tuple[str, int], ...] = (
        ("NVML_FI_DEV_GPU_UTIL", 100),
        ("NVML_FI_DEV_MEM_COPY_UTIL", 101),
        ("NVML_FI_DEV_ENC_UTIL", 102),
        ("NVML_FI_DEV_DEC_UTIL", 103),
        ("NVML_FI_DEV_POWER_INSTANT", 186),
        ("NVML_FI_DEV_POWER_AVERAGE", 185),
        ("NVML_FI_DEV_SM_CLOCK", 20),
        ("NVML_FI_DEV_MEM_CLOCK", 21),
        ("NVML_FI_DEV_TOTAL_ENERGY_CONSUMPTION", 83),
    )

    def _resolve_fast_fields(self) -> dict[str, int]:
        out = {}
        for name, default in self._FIELD_IDS:
            fid = getattr(self.nv, name, default)
            out[name] = int(fid)
        return out

    def environment(self) -> dict:
        nv = self.nv
        cuda_max = ""
        try:
            v = nv.nvmlSystemGetCudaDriverVersion()
            cuda_max = f"{v // 1000}.{(v % 1000) // 10}"
        except Exception:
            pass
        uname = os.uname()
        return {
            "hostname": socket.gethostname(),
            "kernel": f"{uname.sysname} {uname.release}",
            "driver": _b2s(nv.nvmlSystemGetDriverVersion),
            "nvml": _b2s(nv.nvmlSystemGetNVMLVersion),
            "cuda_max": cuda_max,
            "provider": "nvml",
        }

    def devices(self) -> list[dict]:
        out = []
        for i, h in self._handles:
            name = _b2s(lambda: self.nv.nvmlDeviceGetName(h))
            uuid = _b2s(lambda: self.nv.nvmlDeviceGetUUID(h))
            out.append({"index": i, "name": name, "uuid": uuid})
        return out

    def sample(self) -> list[Sample]:
        now = time.time()
        out = []
        for i, h in self._handles:
            s = Sample(ts=now, index=i)
            s.uuid = _b2s(lambda: self.nv.nvmlDeviceGetUUID(h)) or ""
            s.name = _b2s(lambda: self.nv.nvmlDeviceGetName(h)) or "?"
            major, minor = _g(lambda: self.nv.nvmlDeviceGetCudaComputeCapability(h), (0, 0))
            s.compute_cap = f"{major}.{minor}"
            s.arch = _arch_name(major, minor)
            pstate = _g(lambda: self.nv.nvmlDeviceGetPerformanceState(h), None)
            if pstate is not None:
                s.pstate = f"P{pstate}"
            self._fast_or_slow(s, h)
            u = _g(lambda: self.nv.nvmlDeviceGetUtilizationRates(h), None)
            if u:
                s.util_gpu, s.util_mem = float(u.gpu), float(u.memory)
            enc = _g(lambda: self.nv.nvmlDeviceGetEncoderUtilization(h), None)
            if enc:
                s.util_enc = float(enc[0])
            dec = _g(lambda: self.nv.nvmlDeviceGetDecoderUtilization(h), None)
            if dec:
                s.util_dec = float(dec[0])
            m = _g(lambda: self.nv.nvmlDeviceGetMemoryInfo(h), None)
            if m:
                s.mem_used, s.mem_total = int(m.used), int(m.total)
                s.mem_reserved = getattr(m, "reserved", None)
            b1 = _g(lambda: self.nv.nvmlDeviceGetBAR1MemoryInfo(h), None)
            if b1:
                s.bar1_used, s.bar1_total = int(b1.bar1Used), int(b1.bar1Total)
            if s.power_limit_w is None:
                s.power_limit_w = _mw(lambda: self.nv.nvmlDeviceGetPowerManagementLimit(h))
            s.power_default_w = _mw(lambda: self.nv.nvmlDeviceGetPowerManagementDefaultLimit(h))
            lohi = _g(lambda: self.nv.nvmlDeviceGetPowerManagementLimitConstraints(h), None)
            if lohi:
                s.power_min_w, s.power_max_w = lohi[0] / 1000.0, lohi[1] / 1000.0
            s.clocks_sm_mhz = _g(lambda: self.nv.nvmlDeviceGetClockInfo(h, self.nv.NVML_CLOCK_SM), None)
            s.clocks_mem_mhz = _g(lambda: self.nv.nvmlDeviceGetClockInfo(h, self.nv.NVML_CLOCK_MEM), None)
            s.clocks_video_mhz = _g(lambda: self.nv.nvmlDeviceGetClockInfo(h, self.nv.NVML_CLOCK_VIDEO), None)
            s.clocks_max_sm_mhz = _g(lambda: self.nv.nvmlDeviceGetMaxClockInfo(h, self.nv.NVML_CLOCK_SM), None)
            bits = _g(lambda: self.nv.nvmlDeviceGetCurrentClocksThrottleReasons(h), None)
            if bits is not None:
                s.throttle_bits = int(bits)
                s.throttle = decode_throttle(self.nv, int(bits))
            s.temp_gpu = _g(lambda: self.nv.nvmlDeviceGetTemperature(h, self.nv.NVML_TEMPERATURE_GPU), None)
            s.temp_mem = _g(
                lambda: self.nv.nvmlDeviceGetTemperature(h, getattr(self.nv, "NVML_TEMPERATURE_MEMORY", -1)),
                None,
            )
            s.temp_slowdown = _g(
                lambda: self.nv.nvmlDeviceGetTemperatureThreshold(h, self.nv.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN),
                None,
            )
            s.temp_shutdown = _g(
                lambda: self.nv.nvmlDeviceGetTemperatureThreshold(h, self.nv.NVML_TEMPERATURE_THRESHOLD_SHUTDOWN),
                None,
            )
            s.fan_pct = _g(lambda: self.nv.nvmlDeviceGetFanSpeed(h), None)
            s.pcie_gen_max = _g(lambda: self.nv.nvmlDeviceGetMaxPcieLinkGeneration(h), None)
            s.pcie_gen_cur = _g(lambda: self.nv.nvmlDeviceGetPcieLinkGeneration(h), s.pcie_gen_max)
            s.pcie_width_max = _g(lambda: self.nv.nvmlDeviceGetMaxPcieLinkWidth(h), None)
            s.pcie_width_cur = _g(lambda: self.nv.nvmlDeviceGetPcieLinkWidth(h), s.pcie_width_max)
            s.pcie_rx_kbps = _g(
                lambda: self.nv.nvmlDeviceGetPcieThroughput(h, self.nv.NVML_PCIE_UTIL_RX_BYTES), None
            )
            s.pcie_tx_kbps = _g(
                lambda: self.nv.nvmlDeviceGetPcieThroughput(h, self.nv.NVML_PCIE_UTIL_TX_BYTES), None
            )
            s.ecc_sbe = _g(
                lambda: self.nv.nvmlDeviceGetTotalEccErrors(
                    h, self.nv.NVML_MEMORY_ERROR_TYPE_CORRECTED, self.nv.NVML_VOLATILE_ECC
                ),
                None,
            )
            s.ecc_dbe = _g(
                lambda: self.nv.nvmlDeviceGetTotalEccErrors(
                    h, self.nv.NVML_MEMORY_ERROR_TYPE_UNCORRECTED, self.nv.NVML_VOLATILE_ECC
                ),
                None,
            )
            mig = _g(lambda: self.nv.nvmlDeviceGetMigMode(h), None)
            if mig:
                s.mig_mode = "ON" if mig[0] else "OFF"
                if mig[0]:
                    s.mig_instances = _g(lambda: len(self.nv.nvmlDeviceGetMigDevices(h)), 0) or 0
            s.display_active = bool(_g(lambda: self.nv.nvmlDeviceGetDisplayActive(h), False))
            s.persistence = bool(_g(lambda: self.nv.nvmlDeviceGetPersistenceMode(h), 0))
            cm = _g(lambda: self.nv.nvmlDeviceGetComputeMode(h), None)
            if cm is not None:
                s.compute_mode = {
                    getattr(self.nv, "NVML_COMPUTEMODE_DEFAULT", 0): "Default",
                    getattr(self.nv, "NVML_COMPUTEMODE_EXCLUSIVE_PROCESS", 3): "Excl.Proc",
                    getattr(self.nv, "NVML_COMPUTEMODE_PROHIBITED", 2): "Prohibited",
                }.get(cm, str(cm))
            out.append(s)
        return out

    def _fast_or_slow(self, s: Sample, h: object) -> None:
        """One batched FieldValues call for the hot metrics; slow getters fill gaps."""
        if self._fast_ids:
            try:
                vals = self.nv.nvmlDeviceGetFieldValues(h, list(self._fast_ids.values()))
                got: dict[str, float] = {}
                for (name, _fid), fv in zip(self._fast_ids.items(), vals):
                    if getattr(fv, "nvmlReturn", -1) == 0:
                        v = _field_value(fv)
                        if v is not None:
                            got[name] = v
                if "NVML_FI_DEV_GPU_UTIL" in got:
                    s.util_gpu = got["NVML_FI_DEV_GPU_UTIL"]
                if "NVML_FI_DEV_MEM_COPY_UTIL" in got:
                    s.util_mem = got["NVML_FI_DEV_MEM_COPY_UTIL"]
                if "NVML_FI_DEV_ENC_UTIL" in got:
                    s.util_enc = got["NVML_FI_DEV_ENC_UTIL"]
                if "NVML_FI_DEV_DEC_UTIL" in got:
                    s.util_dec = got["NVML_FI_DEV_DEC_UTIL"]
                pw = got.get("NVML_FI_DEV_POWER_INSTANT",
                             got.get("NVML_FI_DEV_POWER_AVERAGE"))
                # some driver/field combos return denormal garbage -- gate it
                if pw is not None and 0.05 <= pw <= 5_000_000:
                    s.power_w = pw / 1000.0
                if "NVML_FI_DEV_SM_CLOCK" in got:
                    s.clocks_sm_mhz = int(got["NVML_FI_DEV_SM_CLOCK"])
                if "NVML_FI_DEV_MEM_CLOCK" in got:
                    s.clocks_mem_mhz = int(got["NVML_FI_DEV_MEM_CLOCK"])
                if "NVML_FI_DEV_TOTAL_ENERGY_CONSUMPTION" in got:
                    s.energy_mj = int(got["NVML_FI_DEV_TOTAL_ENERGY_CONSUMPTION"])
            except Exception:
                pass
        if s.power_w is None:
            s.power_w = _mw(lambda: self.nv.nvmlDeviceGetPowerUsage(h))
        if s.power_limit_w is None:
            s.power_limit_w = _mw(lambda: self.nv.nvmlDeviceGetEnforcedPowerLimit(h))

    # -- processes -------------------------------------------------------

    def processes(self) -> list[GpuProcess]:
        out: list[GpuProcess] = []
        for i, h in self._handles:
            per_pid = self._pid_utils(h, i)
            for kind, fn in (
                ("C", lambda: self.nv.nvmlDeviceGetComputeRunningProcesses(h)),
                ("G", lambda: self.nv.nvmlDeviceGetGraphicsRunningProcesses(h)),
            ):
                try:
                    infos = fn()
                except Exception:
                    continue
                for info in infos or []:
                    pid = int(info.pid)
                    p = GpuProcess(pid=pid, gpu_index=i, mem_used=int(getattr(info, "usedMemory", 0)))
                    pu = per_pid.get(pid)
                    if pu:
                        p.sm_pct, p.mem_pct, p.enc_pct, p.dec_pct = pu
                    for other in out:
                        if other.pid == pid and other.type != kind:
                            other.type = "C+G"
                            other.mem_used = max(other.mem_used, p.mem_used)
                            break
                    else:
                        p.type = kind
                        out.append(p)
        enrich_from_proc(out)
        return out

    def _pid_utils(self, h: object, idx: int) -> dict[int, tuple]:
        fn = getattr(self.nv, "nvmlDeviceGetProcessUtilization", None)
        if fn is None:
            return {}
        try:
            last = self._last_seen_util_ts.get(idx, 0)
            samples = fn(h, last)
            if samples:
                self._last_seen_util_ts[idx] = max(int(getattr(x, "timeStamp", 0)) for x in samples)
            return {
                int(x.pid): (
                    float(getattr(x, "smUtil", 0)),
                    float(getattr(x, "memUtil", 0)),
                    float(getattr(x, "encUtil", 0)),
                    float(getattr(x, "decUtil", 0)),
                )
                for x in samples or []
            }
        except Exception:
            return {}

    def close(self) -> None:
        try:
            self.nv.nvmlShutdown()
        except Exception:
            pass


# ------------------------------------------------------------------ /proc


def enrich_from_proc(procs: list[GpuProcess]) -> None:
    """Attach username + command + cpu%% (vs previous call) from /proc."""
    now = time.time()
    for p in procs:
        stat = f"/proc/{p.pid}"
        try:
            with open(f"{stat}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
            p.command = cmd or f"[{_proc_name(stat)}]"
        except OSError:
            continue
        try:
            p.user = _uid_to_name(os.stat(stat).st_uid)
        except OSError:
            pass
        cpu = _pid_cpu_delta(p.pid, now)
        if cpu is not None:
            p.cpu_pct = cpu


_last_cpu: dict[int, tuple[float, float]] = {}
_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _pid_cpu_delta(pid: int, now: float) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            parts = f.read().decode(errors="replace").rsplit(") ", 1)[-1].split()
        utime, stime = float(parts[11]), float(parts[12])
    except (OSError, IndexError, ValueError):
        return None
    total = utime + stime
    prev = _last_cpu.get(pid)
    _last_cpu[pid] = (total, now)
    if prev is None:
        return None
    dt = now - prev[1]
    if dt <= 0:
        return None
    return max(0.0, (total - prev[0]) / _CLK_TCK / dt * 100.0)


def _proc_name(stat: str) -> str:
    try:
        with open(f"{stat}/comm") as f:
            return f.read().strip()
    except OSError:
        return "pid"


def _uid_to_name(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


# ----------------------------------------------------------------- helpers


def _g(fn: Callable, default=None):
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _mw(fn: Callable) -> float | None:
    v = _g(fn, None)
    return None if v is None else v / 1000.0


def _b2s(fn: Callable) -> str:
    v = _g(fn, b"")
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    return str(v or "")


_VALUE_TYPE_ATTR = {1: "dVal", 2: "uiVal", 3: "ulVal", 4: "ullVal", 5: "lVal"}


def _field_value(fv) -> float | None:
    """Extract from an nvmlFieldValue: `.value` is the C union — pick the
    member that matches valueType (dVal/uiVal/ulVal/ullVal)."""
    try:
        if int(getattr(fv, "nvmlReturn", 1)) != 0:
            return None
        vt = int(getattr(fv, "valueType", 0) or 0)
    except Exception:
        return None
    attr = _VALUE_TYPE_ATTR.get(vt)
    if not attr:
        return None
    v = getattr(fv, attr, None)
    if isinstance(v, (int, float)):
        return float(v)
    union = getattr(fv, "value", None)
    if union is not None:
        try:
            return float(getattr(union, attr))
        except AttributeError:
            return None
    return None


def decode_throttle(nv, bits: int) -> list[str]:
    # nvml.h clocksThrottleReasons bits (stable ABI)
    table = (
        (0x001, "GPU-Idle"),
        (0x002, "App-Clocks"),
        (0x004, "SW-Power-Cap"),
        (0x008, "HW-Slowdown"),
        (0x010, "Sync-Boost"),
        (0x020, "SW-Thermal"),
        (0x040, "HW-Thermal"),
        (0x080, "HW-Brake"),
        (0x100, "Display-Clocks"),
    )
    return [label for bit, label in table if bits & bit]


def _arch_name(major: int, minor: int) -> str:
    return {
        (1, 0): "Fermi", (2, 0): "Fermi", (3, 0): "Kepler", (3, 2): "Kepler",
        (3, 5): "Kepler", (3, 7): "Kepler", (5, 0): "Maxwell", (5, 2): "Maxwell",
        (5, 3): "Maxwell", (6, 0): "Pascal", (6, 1): "Pascal", (6, 2): "Pascal",
        (7, 0): "Volta", (7, 2): "Volta", (7, 5): "Turing",
        (8, 0): "Ampere", (8, 6): "Ampere", (8, 7): "Ampere", (8, 9): "Ada",
        (9, 0): "Hopper", (10, 0): "Blackwell", (12, 0): "Blackwell",
    }.get((major, minor), f"CC {major}.{minor}" if major else "?")


# ------------------------------------------------------------------ demo


class DemoProvider:
    """Two synthetic GPUs running a scripted 120 s load arc incl. a
    thermal-throttle sequence on GPU 0 and a power-cap arc on GPU 1."""

    PERIOD = 120.0

    def __init__(self, only=None) -> None:
        self._only = set(only) if only else None
        self._t0 = time.time()
        self._rand = random.Random(1847)

    def environment(self) -> dict:
        return {
            "hostname": "zion-demo",
            "kernel": "Linux 6.9.0 (demo)",
            "driver": "580.00 (demo)",
            "nvml": "13.590 (demo)",
            "cuda_max": "13.0",
            "provider": "demo",
        }

    _ALL = [
        {"index": 0, "name": "NVIDIA GeForce RTX 4070 Ti SUPER", "uuid": "GPU-demo-4070tis"},
        {"index": 1, "name": "NVIDIA H100 PCIe", "uuid": "GPU-demo-h100"},
    ]

    def devices(self) -> list[dict]:
        if self._only:
            return [d for d in self._ALL if d["index"] in self._only]
        return list(self._ALL)

    def _phase(self) -> float:
        return (time.time() - self._t0) % self.PERIOD

    def sample(self) -> list[Sample]:
        t = self._phase()
        now = time.time()
        j = lambda a=1.0: a * (self._rand.random() - 0.5)
        out = []
        want = self._only or {0, 1}

        # -- GPU 0: idle -> burst -> sustained -> thermal throttle -> cooldown
        if t < 15:
            load = 3 + j(2)
        elif t < 30:
            load = 20 + (t - 15) * 6 + j(4)
        elif t < 70:
            load = 96 + j(4)
        elif t < 95:
            load = 99 + j(2)
        else:
            load = max(4, 99 - (t - 95) * 5) + j(3)
        load = min(100.0, max(0.0, load))
        temp = 36 + min(52.0, max(0.0, (t - 12) * 1.25)) - max(0.0, (t - 95) * 2.0) + j(0.8)
        temp = min(89.0, max(33.0, temp))
        throttling = t >= 55 and t < 100
        sm_clock = (2850 if load > 30 else 240 + j(40)) - (620 if throttling else 0) + j(30)
        out.append(Sample(
            ts=now, index=0, uuid="GPU-demo-4070tis",
            name="NVIDIA GeForce RTX 4070 Ti SUPER", arch="Ada", compute_cap="8.9",
            pstate="P0" if load > 8 else "P8",
            util_gpu=load, util_mem=min(100.0, load * 0.82 + j(6)),
            util_enc=max(0.0, 40 - abs(t - 42) * 2) if 20 < t < 65 else 0.0,
            util_dec=0.0,
            mem_used=int(1.1e9 + (13.2e9 if t > 18 else 0) * min(1.0, max(0.0, (t - 15) / 3)) + j(0.4e9)),
            mem_total=16 * 1024**3, mem_reserved=268 << 20,
            bar1_used=256 << 20, bar1_total=8 << 30,
            power_w=max(14.0, 28 + load * 2.62 - (60 if throttling else 0) + j(6)),
            power_limit_w=285.0, power_default_w=285.0, power_min_w=170.0, power_max_w=320.0,
            clocks_sm_mhz=int(sm_clock), clocks_mem_mhz=10951, clocks_video_mhz=2250,
            clocks_max_sm_mhz=2850,
            energy_mj=int(now * 137_000),
            throttle_bits=(0x20 if throttling else (0x1 if load < 3 else 0x0)),
            throttle=(["SW-Thermal"] if throttling else (["GPU-Idle"] if load < 3 else [])),
            temp_gpu=int(temp), temp_mem=54, temp_slowdown=90, temp_shutdown=98,
            fan_pct=int(min(100, max(22, (temp - 30) * 1.7))),
            pcie_gen_cur=4, pcie_gen_max=4, pcie_width_cur=16, pcie_width_max=16,
            pcie_rx_kbps=int(max(0, (load - 20) * 18000 + j(4000))),
            pcie_tx_kbps=int(max(0, (load - 20) * 4000 + j(1500))),
            ecc_sbe=0, ecc_dbe=0, mig_mode="OFF", mig_instances=0,
            display_active=True, persistence=True, compute_mode="Default",
        ))
        if 0 not in want:
            out.pop()

        # -- GPU 1: power-capped memory-bound training arc
        if t < 25:
            l1, m1 = 8 + j(3), 12 + j(4)
        else:
            l1 = 88 + j(5)
            m1 = min(99.0, 40 + (t - 25) * 2.2) if t < 80 else 99.0 + j(1)
        out.append(Sample(
            ts=now, index=1, uuid="GPU-demo-h100",
            name="NVIDIA H100 PCIe", arch="Hopper", compute_cap="9.0",
            pstate="P0" if l1 > 8 else "P2",
            util_gpu=l1, util_mem=m1,
            util_enc=0.0, util_dec=max(0.0, 30 - abs(t - 60)) if 40 < t < 90 else 0.0,
            mem_used=int((18 if t < 25 else 74) * 2**30 + j(0.8 * 2**30)),
            mem_total=80 * 2**30, mem_reserved=1024 << 20,
            bar1_used=512 << 20, bar1_total=128 << 30,
            power_w=min(348.0, max(42.0, 45 + l1 * 3.3)) if t > 20 else 43.0 + j(2),
            power_limit_w=350.0, power_default_w=350.0, power_min_w=200.0, power_max_w=400.0,
            clocks_sm_mhz=int(1980 if l1 > 30 else 345 + j(20)),
            clocks_mem_mhz=2619, clocks_video_mhz=1700, clocks_max_sm_mhz=1980,
            energy_mj=int(now * 210_000),
            throttle_bits=(0x4 if l1 > 60 else (0x1 if l1 < 3 else 0)),
            throttle=(["SW-Power-Cap"] if l1 > 60 else (["GPU-Idle"] if l1 < 3 else [])),
            temp_gpu=int(44 + min(31.0, max(0.0, (t - 20) * 0.45))), temp_mem=52,
            temp_slowdown=90, temp_shutdown=95,
            fan_pct=int(38 + min(40.0, max(0.0, (t - 20) * 0.6))),
            pcie_gen_cur=5, pcie_gen_max=5, pcie_width_cur=16, pcie_width_max=16,
            pcie_rx_kbps=int((l1 - 10) * 26000 + j(6000)) if l1 > 10 else 120,
            pcie_tx_kbps=int((l1 - 10) * 5000 + j(2000)) if l1 > 10 else 90,
            ecc_sbe=2, ecc_dbe=0, mig_mode="OFF", mig_instances=0,
            display_active=False, persistence=True, compute_mode="Default",
        ))
        if 1 not in want:
            out.pop()
        return out

    def processes(self) -> list[GpuProcess]:
        t = self._phase()
        j = lambda a=1.0: a * (self._rand.random() - 0.5)
        active0 = t > 15
        procs = [
            GpuProcess(184722, 0, "C", int(13.4 * 2**30 + j(0.2e9)), 97.0, 84.0, 0.0, 0.0,
                       312.0, "demo", "python train.py --batch 256 --amp"),
            GpuProcess(184701, 0, "G", 1180 * 2**20, 2.0, 3.0, 0.0, 0.0, 4.1,
                       "demo", "chrome --type=gpu-process"),
        ]
        if t > 25:
            procs.append(GpuProcess(192004, 1, "C", int(73.5 * 2**30), 88.0, 95.0, 0.0, 12.0,
                                    26.0, "mlteam", "python -m vllm.entrypoints serve"))
            procs.append(GpuProcess(192101, 1, "C", int(1.8 * 2**30), 6.0, 2.0, 0.0, 0.0, 1.2,
                                    "mlteam", "python watchdog.py"))
        if not active0:
            procs = [p for p in procs if p.gpu_index != 0 or p.type == "G"]
        if self._only:
            procs = [p for p in procs if p.gpu_index in self._only]
        return procs

    def close(self) -> None:
        pass
