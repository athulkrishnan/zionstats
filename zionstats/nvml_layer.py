"""Data layer: DeviceProvider protocol + local NVML provider + demo provider.

Sampling uses the batched NVML FieldValues API where the driver exposes the
fields, falling back to individual getters per metric (and to None when the
hardware doesn't support a metric at all -- nothing here may raise at tick
time, or the TUI dies mid-session).
"""

from __future__ import annotations

import os
import random
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from . import procs
from .procs import ProcInfo
from .util import TTLCache

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
    vendor: str = "nvidia"  # nvidia | amd | intel | demo
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
    # historical clocks-event-reason tallies (driver 575+); None when the
    # installed binding does not expose the counters API
    throttle_counters: dict[str, int] | None = None
    temp_gpu: int | None = None
    # hottest-part sensor (NVML v615 TEMPERATURE_GPU_MAX); None when absent
    temp_gpu_max: int | None = None
    temp_mem: int | None = None
    temp_slowdown: int | None = None
    temp_shutdown: int | None = None
    fan_pct: int | None = None
    fan_rpm: int | None = None
    pcie_gen_cur: int | None = None
    pcie_gen_max: int | None = None
    pcie_width_cur: int | None = None
    pcie_width_max: int | None = None
    pcie_rx_kbps: int | None = None
    pcie_tx_kbps: int | None = None
    # cumulative PCIe replay counter (seconds-since-driver-load semantics
    # untouched: counter, not gauge, downstream); None when unsupported
    pcie_replay: int | None = None
    ecc_sbe: int | None = None
    ecc_dbe: int | None = None
    mig_mode: str | None = None
    mig_instances: int = 0
    display_active: bool | None = None
    persistence: bool | None = None
    compute_mode: str | None = None

    def merged_over(self, base: Sample) -> Sample:
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
        self._proc_cache = TTLCache(maxsize=256, ttl=30.0)
        self._energy0: dict[int, int | None] = {}
        self._fast_ids = self._resolve_fast_fields()
        # capability probes: absent binding constants/functions mean the
        # feature is absent on this box (never synthesized)
        self._replay_fid: int | None = None
        fid = getattr(self.nv, "NVML_FI_DEV_PCIE_REPLAY_COUNTER", None)
        if fid is not None:
            self._replay_fid = int(fid)
        self._fan_rpm_fn = (
            getattr(self.nv, "nvmlDeviceGetFanSpeedRPM", None)
            or getattr(self.nv, "nvmlDeviceGetFanSpeedRpm", None)
        )
        self._temp_gpu_max_const = getattr(self.nv, "NVML_TEMPERATURE_GPU_MAX", None)
        self._throttle_counters_fn = None
        for name in ("nvmlDeviceGetClkEventReasonCounters",
                     "nvmlDeviceGetClockEventReasonCounters",
                     "nvmlDeviceGetClkEventsReasonCounters"):
            fn = getattr(self.nv, name, None)
            if fn is not None:
                self._throttle_counters_fn = fn
                break

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
            out.append({"index": i, "name": self._dev_name(h), "uuid": self._dev_uuid(h)})
        return out

    def _dev_name(self, h: object) -> str:
        return _b2s(lambda: self.nv.nvmlDeviceGetName(h))

    def _dev_uuid(self, h: object) -> str:
        return _b2s(lambda: self.nv.nvmlDeviceGetUUID(h))

    def sample(self) -> list[Sample]:
        now = time.time()
        return [self._sample_one(i, h, now) for i, h in self._handles]

    def _sample_one(self, i: int, h: object, now: float) -> Sample:
        s = Sample(ts=now, index=i)
        s.uuid = self._dev_uuid(h) or ""
        s.name = self._dev_name(h) or "?"
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
        if self._throttle_counters_fn is not None:
            s.throttle_counters = self._throttle_counters(h)
        s.temp_gpu = _g(lambda: self.nv.nvmlDeviceGetTemperature(h, self.nv.NVML_TEMPERATURE_GPU), None)
        if self._temp_gpu_max_const is not None:
            s.temp_gpu_max = _g(
                lambda: self.nv.nvmlDeviceGetTemperature(h, self._temp_gpu_max_const), None
            )
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
        fan_rpm_fn = self._fan_rpm_fn
        if fan_rpm_fn is not None:
            rpm = _g(lambda: fan_rpm_fn(h, 0), None)
            if rpm is None:
                rpm = _g(lambda: fan_rpm_fn(h), None)  # older 1-arg binding
            s.fan_rpm = int(rpm) if rpm is not None else None
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
        return s

    def _fast_or_slow(self, s: Sample, h: object) -> None:
        """One batched FieldValues call for the hot metrics; slow getters fill gaps."""
        if self._fast_ids:
            try:
                ids = dict(self._fast_ids)
                if self._replay_fid is not None:
                    ids["NVML_FI_DEV_PCIE_REPLAY_COUNTER"] = self._replay_fid
                vals = self.nv.nvmlDeviceGetFieldValues(h, list(ids.values()))
                got: dict[str, float] = {}
                for (name, _fid), fv in zip(ids.items(), vals):
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
                if "NVML_FI_DEV_PCIE_REPLAY_COUNTER" in got:
                    s.pcie_replay = int(got["NVML_FI_DEV_PCIE_REPLAY_COUNTER"])
            except Exception:
                pass
        if s.power_w is None:
            s.power_w = _mw(lambda: self.nv.nvmlDeviceGetPowerUsage(h))
        if s.power_limit_w is None:
            s.power_limit_w = _mw(lambda: self.nv.nvmlDeviceGetEnforcedPowerLimit(h))
        if s.pcie_replay is None:
            fn = getattr(self.nv, "nvmlDeviceGetPcieReplayCounter", None)
            if fn is not None:
                v = _g(lambda: fn(h), None)
                if v is not None:
                    s.pcie_replay = int(v)

    def _throttle_counters(self, h: object) -> dict[str, int] | None:
        """Historical clocks-event-reason tallies (driver 575+).  Only the
        installed binding's own struct/dict shape is trusted; anything else
        leaves the feature None."""
        fn = self._throttle_counters_fn
        if fn is None:
            return None
        res = _g(lambda: fn(h), None)
        if res is None:
            return None
        if isinstance(res, dict):
            return {str(k): int(v) for k, v in res.items() if isinstance(v, int)}
        fields = getattr(res, "_fields_", None)  # ctypes struct from the binding
        if fields:
            out = {}
            for fname, _ftype in fields:
                v = getattr(res, fname, None)
                if isinstance(v, int):
                    out[str(fname)] = v
            if out:
                return out
        return None

    # -- processes -------------------------------------------------------

    def processes(self) -> list[GpuProcess]:
        out: list[GpuProcess] = []
        for i, h in self._handles:
            per_pid = self._pid_utils(h, i)
            for kind in ("C", "G"):
                try:
                    if kind == "C":
                        infos = self.nv.nvmlDeviceGetComputeRunningProcesses(h)
                    else:
                        infos = self.nv.nvmlDeviceGetGraphicsRunningProcesses(h)
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

    def proc_info(self, pids: list[int], with_env: bool = False) -> dict[int, ProcInfo]:
        """/proc table for the process tree + detail screen.  Best-effort;
        missing processes are simply absent from the result."""
        out: dict[int, ProcInfo] = {}
        for pid in pids:
            info = procs.read_proc_info(pid, self._proc_cache)
            if info is None:
                continue
            if with_env:
                info.environ = procs.read_proc_env(pid)
                info.cwd = procs.read_proc_cwd(pid)
            out[pid] = info
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
try:
    _CLK_TCK: float = os.sysconf("SC_CLK_TCK")  # Linux only; /proc parsing is guarded below
except (AttributeError, OSError, ValueError):
    _CLK_TCK = 100.0


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
    """Decode a clocksThrottleReasons mask into labels.

    The v12-era bits (0x001..0x100) are stable nvml.h ABI and always decoded.
    Newer driver bits (v615: Board-Limit, Reliability, ...) are appended only
    when the installed binding exports their NVML_CLOCKS_EVENT_REASON_*
    constants -- values always resolved from the binding, never from memory.
    The mask is open-ended (NVIDIA reserves the right to add bits), so any
    set bit with no label decodes as Bit-0xN and stays visible in the UI.
    """
    table = [
        (0x001, "GPU-Idle"),
        (0x002, "App-Clocks"),
        (0x004, "SW-Power-Cap"),
        (0x008, "HW-Slowdown"),
        (0x010, "Sync-Boost"),
        (0x020, "SW-Thermal"),
        (0x040, "HW-Thermal"),
        (0x080, "HW-Brake"),
        (0x100, "Display-Clocks"),
    ]
    seen = {bit for bit, _ in table}
    for const, label in _EXTRA_REASON_CONSTANTS:
        v = getattr(nv, const, None)
        if isinstance(v, int) and v and v not in seen:
            table.append((v, label))
            seen.add(v)
    out = [label for bit, label in table if bits & bit]
    for shift in range(max(1, bits.bit_length())):
        bit = 1 << shift
        if bits & bit and bit not in seen:
            out.append(f"Bit-0x{bit:x}")
    return out


# (binding constant name, UI label) pairs probed at decode time; several
# spellings per label cover nvidia-ml-py naming drift.  A constant the
# installed binding does not export contributes nothing.
_EXTRA_REASON_CONSTANTS = (
    ("NVML_CLOCKS_EVENT_REASON_BOARD_LIMIT", "Board-Limit"),
    ("NVML_CLOCKS_EVENT_REASON_BOARD_LIMIT_EPD", "Board-Limit"),
    ("NVML_CLOCKS_EVENT_REASON_RELIABILITY", "Reliability"),
    ("NVML_CLOCKS_EVENT_REASON_RELIABILITY_OVERHEATING", "Reliability"),
)


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
    """Four synthetic GPUs running a scripted 120 s load arc: a thermal-
    throttle sequence (0), a power-capped memory-bound arc (1), an AMD
    card with a junction-temp arc (2), and an Intel iGPU with the
    provider's honest gaps (no power, no fan) (3)."""

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
        {"index": 2, "name": "AMD Radeon RX 7900 XTX", "uuid": "AMD-0000:0b:00.0"},
        {"index": 3, "name": "Intel Iris Xe Graphics", "uuid": "INTEL-0000:00:02.0"},
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
        def j(a: float = 1.0) -> float:
            return a * (self._rand.random() - 0.5)

        out = []
        want = self._only or {0, 1, 2, 3}

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
            util_gpu=load, util_mem=min(100.0, max(0.0, load * 0.82 + j(6))),
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

        # -- GPU 2: AMD RDNA3 -- edge/junction split with a junction peak
        if 2 in want:
            load2 = min(100.0, max(5.0, 60 + 38 * (1 if t > 30 and t < 95 else 0.2) + j(4)))
            edge2 = 55 + min(15.0, max(0.0, (t - 8) * 0.9)) - max(0.0, (t - 95) * 1.4) + j(0.7)
            edge2 = min(70.0, max(52.0, edge2))
            # hotspot runs far above edge under load (GDDR6X/CDNA-class gap);
            # peaks near 108 C mid-arc -- the paste/pad-failure signal
            junc2 = edge2 + 18 + 20 * min(1.0, max(0.0, (t - 20) / 40)) - max(0.0, (t - 100) * 1.6)
            junc2 = min(108.0, max(edge2 + 8.0, junc2))
            junc_hot = junc2 >= 100.0
            out.append(Sample(
                ts=now, index=2, vendor="amd",
                uuid="AMD-0000:0b:00.0",
                name="AMD Radeon RX 7900 XTX", arch="RDNA3", compute_cap="",
                pstate="auto",
                util_gpu=load2, util_mem=None,
                mem_used=int((6 if t < 20 else 20.5) * 2**30 + j(0.6 * 2**30)),
                mem_total=24 * 2**30,
                power_w=min(452.0, max(28.0, 30 + load2 * 4.2)) if t > 15 else 29.0 + j(2),
                power_limit_w=455.0, power_default_w=455.0,
                power_min_w=100.0, power_max_w=500.0,
                clocks_sm_mhz=int(2310 if load2 > 40 else 480 + j(60)),
                clocks_mem_mhz=2500,
                clocks_max_sm_mhz=2500,
                energy_mj=None,  # no hw counter: session Wh via integration
                throttle_bits=0,
                throttle=(["SW-Thermal", "Junction-Hot"] if junc_hot else []),
                temp_gpu=int(edge2), temp_gpu_max=int(junc2), temp_mem=84,
                temp_slowdown=90, temp_shutdown=100,
                fan_pct=int(min(100, max(20, (edge2 - 30) * 2.2))),
                fan_rpm=None,  # amdgpu sysfs has no rpm readout
                pcie_gen_cur=4, pcie_gen_max=4, pcie_width_cur=16, pcie_width_max=16,
                pcie_rx_kbps=int((load2 - 10) * 21000 + j(5000)) if load2 > 10 else 130,
                pcie_tx_kbps=int((load2 - 10) * 4200 + j(1500)) if load2 > 10 else 95,
                ecc_sbe=None, ecc_dbe=None, mig_mode=None, mig_instances=0,
                display_active=None, persistence=None, compute_mode=None,
            ))

        # -- GPU 3: Intel iGPU -- honest gaps mirror the provider:
        # no power (no GPU-attributable sysfs path), no fan, no enc/dec
        if 3 in want:
            load3 = min(100.0, max(2.0, 25 + 55 * (1 if 35 < t < 90 else 0.1) + j(5)))
            out.append(Sample(
                ts=now, index=3, vendor="intel",
                uuid="INTEL-0000:00:02.0",
                name="Intel Iris Xe Graphics", arch="Xe-LP", compute_cap="",
                pstate="",
                util_gpu=load3, util_mem=None,
                mem_used=int((0.4 if t < 35 else 7.5) * 2**30 + j(0.2 * 2**30)),
                mem_total=16 * 2**30,
                power_w=None,  # provider never reports it -- demo matches
                power_limit_w=None, power_default_w=None,
                power_min_w=None, power_max_w=None,
                clocks_sm_mhz=int(2100 if load3 > 50 else 400 + j(80)),
                clocks_mem_mhz=None, clocks_max_sm_mhz=2100,
                energy_mj=None,
                throttle_bits=0, throttle=[],
                temp_gpu=int(45 + min(18.0, max(0.0, (t - 30) * 0.5)) - max(0.0, (t - 92) * 1.2) + j(0.6)),
                temp_gpu_max=None, temp_mem=None,
                temp_slowdown=85, temp_shutdown=100,
                fan_pct=None, fan_rpm=None,
                pcie_gen_cur=None, pcie_gen_max=None,
                pcie_width_cur=None, pcie_width_max=None,
                pcie_rx_kbps=None, pcie_tx_kbps=None,
                ecc_sbe=None, ecc_dbe=None, mig_mode=None, mig_instances=0,
                display_active=None, persistence=None, compute_mode=None,
            ))
        return out

    def processes(self) -> list[GpuProcess]:
        t = self._phase()
        def j(a: float = 1.0) -> float:
            return a * (self._rand.random() - 0.5)

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
        if t > 20:
            # AMD fdinfo client: per-process mem/util from drm-* deltas
            # (util_mem absent -- amdgpu has no bandwidth utilization)
            procs.append(GpuProcess(220001, 2, "C", int(1.4 * 2**30), 97.0, None,
                                    None, None, 41.0, "demo",
                                    "blender --background --cycles"))
        if t > 35:
            # Intel iGPU client: sm from rcs0 fdinfo deltas, mem_pct absent
            procs.append(GpuProcess(230101, 3, "G", int(0.9 * 2**30), 43.0, None,
                                    None, None, 9.5, "demo",
                                    "chrome --type=gpu-process --use-gl"))
        if not active0:
            procs = [p for p in procs if p.gpu_index != 0 or p.type == "G"]
        if self._only:
            procs = [p for p in procs if p.gpu_index in self._only]
        return procs

    def proc_info(self, pids: list[int], with_env: bool = False) -> dict[int, ProcInfo]:
        table = _demo_proc_info()
        out: dict[int, ProcInfo] = {}
        for pid in pids:
            info = table.get(pid)
            if info is not None:
                out[pid] = info
        return out

    def close(self) -> None:
        pass


# ------------------------------------------------------------------ replay extras


def _demo_proc_info() -> dict[int, ProcInfo]:
    """Canned /proc table for the demo, including a two-level parent chain:
    800 sshd -> 810 bash run.sh -> 184722 python train.py.  Makes the tree
    view and the per-process detail screen fully demoable without a GPU."""
    def info(pid, ppid, user, cmdline, cwd, exe, env):
        return ProcInfo(pid=pid, ppid=ppid, user=user, cmdline=cmdline, cwd=cwd,
                        exe=exe, environ=env)
    return {
        800: info(800, 1, "root", "sshd: demo [priv]", "", "/usr/sbin/sshd", []),
        810: info(810, 800, "demo", "-bash", "/home/demo", "/usr/bin/bash",
                  [("SHELL", "/bin/bash"), ("USER", "demo")]),
        184700: info(184700, 810, "demo", "bash run.sh", "/home/demo/train",
                     "/usr/bin/bash",
                     [("CUDA_VISIBLE_DEVICES", "0,1"), ("PATH", "/usr/local/bin:/usr/bin"),
                      ("USER", "demo")]),
        184722: info(184722, 184700, "demo", "python train.py --batch 256 --amp",
                     "/home/demo/train", "/usr/bin/python3.12",
                     [("CUDA_VISIBLE_DEVICES", "0,1"),
                      ("CUDA_DEVICE_ORDER", "PCI_BUS_ID"),
                      ("NVIDIA_VISIBLE_DEVICES", "all"),
                      ("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True"),
                      ("USER", "demo")]),
        184701: info(184701, 800, "demo", "chrome --type=gpu-process", "", "", []),
        192000: info(192000, 1, "mlteam", "bash entry.sh", "/srv/vllm", "/usr/bin/bash",
                     [("USER", "mlteam")]),
        192004: info(192004, 192000, "mlteam", "python -m vllm.entrypoints serve",
                     "/srv/vllm", "/usr/bin/python3.12",
                     [("CUDA_VISIBLE_DEVICES", "1"),
                      ("VLLM_ATTENTION_BACKEND", "FLASHINFER"),
                      ("USER", "mlteam")]),
        192101: info(192101, 192000, "mlteam", "python watchdog.py", "/srv/vllm",
                     "/usr/bin/python3.12", [("USER", "mlteam")]),
        220001: info(220001, 810, "demo", "blender --background --cycles",
                     "/home/demo/scene", "/usr/bin/blender", [("USER", "demo")]),
        230101: info(230101, 800, "demo", "chrome --type=gpu-process --use-gl",
                     "", "/usr/bin/chrome", []),
    }
