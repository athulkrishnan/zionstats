"""AMD provider: amdgpu sysfs + DRM fdinfo per-process stats.  Zero deps.

Reads the standard amdgpu sysfs surface (gpu_busy_percent, mem_info_*,
power1_*, hwmon temp/freq/pwm, pp_dpm_*) and per-client DRM fdinfo
(kernel >= 5.14, shared scanner in drmfd).  Every metric is best-effort:
a missing or garbage file means None and the UI/exporter omit it.

Honest gaps, documented not hidden: no memory-bandwidth utilization
(util_mem stays None -- amdgpu sysfs does not expose it), no hardware
energy counter (session Wh comes from the Insights power-integration
fallback over power1_average), no PCIe link telemetry, no NVML-style
throttle bitmask.  Junction/hotspot hwmon temp maps to the shared
hottest-part field (temp_gpu_max).
"""

from __future__ import annotations

import os
import socket
import time

from . import procs
from .drmfd import engine_utilization, scan_clients
from .nvml_layer import GpuProcess, ProviderError, Sample, enrich_from_proc
from .procs import ProcInfo
from .util import TTLCache

VENDOR_ID = "0x1002"
MAX_CLIENTS = 256


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


# ------------------------------------------------------------- pure parsers


def parse_gpu_busy_percent(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        v = int(text.strip())
    except ValueError:
        return None
    return float(v) if 0 <= v <= 100 else None


def parse_vram(used_text: str | None, total_text: str | None) -> tuple[int | None, int | None]:
    def one(t: str | None) -> int | None:
        if t is None:
            return None
        try:
            return int(t.strip())
        except ValueError:
            return None
    return one(used_text), one(total_text)


def parse_power(avg_text: str | None, cap_text: str | None,
                min_text: str | None, max_text: str | None) -> dict[str, float | None]:
    """power1_* files are MICROwatts; convert honestly, garbage -> None."""
    def w(t: str | None) -> float | None:
        if t is None:
            return None
        try:
            return int(t.strip()) / 1_000_000.0
        except ValueError:
            return None
    return {"power_w": w(avg_text), "power_limit_w": w(cap_text),
            "power_min_w": w(min_text), "power_max_w": w(max_text)}


def parse_temp(text: str | None) -> int | None:
    """hwmon tempN_input is MILLIcelsius."""
    if text is None:
        return None
    try:
        return round(int(text.strip()) / 1000.0)
    except ValueError:
        return None


def parse_fan(pwm_text: str | None, max_text: str | None) -> int | None:
    """pwm1 (0-255) over pwm1_max (usually 255) -> percent."""
    if pwm_text is None:
        return None
    try:
        pwm = int(pwm_text.strip())
    except ValueError:
        return None
    pmax = 255
    if max_text is not None:
        try:
            pmax = int(max_text.strip()) or 255
        except ValueError:
            pass
    if pwm < 0 or pmax <= 0:
        return None
    return min(100, max(0, round(pwm / pmax * 100)))


def _dpm_marked_mhz(dpm_text: str | None) -> int | None:
    """Current entry of a pp_dpm_sclk-style table: the frequency on the
    line marked '*' (lines look like '2: 2400M *')."""
    if dpm_text is None:
        return None
    for line in dpm_text.splitlines():
        if "*" not in line:
            continue
        after = line.split(":", 1)[-1] if ":" in line else line
        for tok in after.split():
            if tok.endswith("M") and tok[:-1].isdigit():
                return int(tok[:-1])
    return None


def parse_clocks(freq1_text: str | None, freq2_text: str | None,
                 sclk_table_text: str | None) -> tuple[int | None, int | None]:
    """freq1/freq2 are HERTZ -> MHz; sclk falls back to the pp_dpm_sclk
    table's '*'-marked entry when freq1_input is missing."""
    def mhz(t: str | None) -> int | None:
        if t is None:
            return None
        try:
            return round(int(t.strip()) / 1_000_000.0)
        except ValueError:
            return None
    sm = mhz(freq1_text)
    if sm is None:
        sm = _dpm_marked_mhz(sclk_table_text)
    return sm, mhz(freq2_text)


def parse_pstate(level_text: str | None, dpm_text: str | None) -> str | None:
    """amdgpu has no P-state line; the forced performance level (or the
    marked dpm entry) is the honest equivalent.  Many cards expose
    neither -> None."""
    if level_text is not None and level_text.strip():
        return level_text.strip()
    if dpm_text is not None and dpm_text.strip():
        return dpm_text.strip()
    return None


# ---------------------------------------------------------------- discovery


def discover_cards(sysfs_root: str) -> list[str]:
    """Sorted card device dirs whose PCI vendor is AMD (0x1002)."""
    out: list[str] = []
    try:
        entries = os.listdir(sysfs_root)
    except OSError:
        return out
    def card_no(name: str) -> int:
        try:
            return int(name[4:])
        except ValueError:
            return 1 << 30
    for name in sorted((e for e in entries if e.startswith("card")), key=card_no):
        dev = os.path.join(sysfs_root, name, "device")
        if not os.path.isdir(dev):
            continue
        vendor = _read(os.path.join(dev, "vendor"))
        if vendor is not None and vendor.strip().lower() == VENDOR_ID:
            out.append(dev)
    return out


# ----------------------------------------------------------------- provider


class AmdProvider:
    """Duck-typed twin of NvmlProvider over amdgpu sysfs.  Takes an
    injectable sysfs/proc root so fixture trees can drive the tests."""

    def __init__(self, only: list[int] | None = None,
                 sysfs_root: str = "/sys/class/drm",
                 proc_root: str = "/proc") -> None:
        self._only = set(only) if only else None
        self._sysfs = sysfs_root
        self._proc_root = proc_root
        cards = discover_cards(sysfs_root)
        if self._only:
            cards = [c for i, c in enumerate(cards) if i in self._only]
        if not cards:
            raise ProviderError(f"no AMD GPUs found on {sysfs_root}")
        self._cards = cards
        self._proc_cache = TTLCache(maxsize=256, ttl=30.0)
        self._client_ring: dict[str, dict[str, int]] = {}

    # -- identity

    def environment(self) -> dict:
        uname = os.uname()
        driver = (_read("/sys/module/amdgpu/version") or "").strip()
        return {
            "hostname": socket.gethostname(),
            "kernel": f"{uname.sysname} {uname.release}",
            "driver": driver or "amdgpu",
            "nvml": "",
            "cuda_max": "",
            "provider": "amd",
        }

    def _card_name(self, dev: str, index: int) -> str:
        product = (_read(os.path.join(dev, "product_name")) or "").strip()
        if product:
            return product
        driver = ""
        for line in (_read(os.path.join(dev, "uevent")) or "").splitlines():
            if line.startswith("DRIVER="):
                driver = line.split("=", 1)[1].strip()
        return driver or f"AMD GPU {index}"

    def _card_uuid(self, dev: str, index: int) -> str:
        uid = (_read(os.path.join(dev, "unique_id")) or "").strip()
        if uid:
            return f"AMD-{uid}"
        busid = os.path.basename(os.path.realpath(dev))
        return f"AMD-{busid or index}"

    def devices(self) -> list[dict]:
        return [{"index": i, "name": self._card_name(dev, i),
                 "uuid": self._card_uuid(dev, i)}
                for i, dev in enumerate(self._cards)]

    # -- sampling

    def sample(self) -> list[Sample]:
        now = time.time()
        return [self._sample_card(i, dev, now) for i, dev in enumerate(self._cards)]

    def _hwmon_dir(self, dev: str) -> str:
        hwmon_root = os.path.join(dev, "hwmon")
        try:
            entries = sorted(os.listdir(hwmon_root))
        except OSError:
            return ""
        return os.path.join(hwmon_root, entries[0]) if entries else ""

    def _sample_card(self, i: int, dev: str, now: float) -> Sample:
        s = Sample(ts=now, index=i, vendor="amd")
        s.uuid = self._card_uuid(dev, i)
        s.name = self._card_name(dev, i)
        hw = self._hwmon_dir(dev)
        s.util_gpu = parse_gpu_busy_percent(
            _read(os.path.join(dev, "gpu_busy_percent")))
        used, total = parse_vram(
            _read(os.path.join(dev, "mem_info_vram_used")),
            _read(os.path.join(dev, "mem_info_vram_total")))
        s.mem_used = used if used is not None else 0
        s.mem_total = total if total is not None else 0
        for k, v in parse_power(
                _read(os.path.join(dev, "power1_average")),
                _read(os.path.join(dev, "power1_cap")),
                _read(os.path.join(dev, "power1_cap_min")),
                _read(os.path.join(dev, "power1_cap_max"))).items():
            setattr(s, k, v)
        if hw:
            s.temp_gpu = parse_temp(_read(os.path.join(hw, "temp1_input")))
            s.temp_gpu_max = parse_temp(_read(os.path.join(hw, "temp2_input")))
            s.temp_mem = parse_temp(_read(os.path.join(hw, "temp3_input")))
            s.fan_pct = parse_fan(_read(os.path.join(hw, "pwm1")),
                                  _read(os.path.join(hw, "pwm1_max")))
        sclk_table = _read(os.path.join(dev, "pp_dpm_sclk"))
        s.clocks_sm_mhz, s.clocks_mem_mhz = parse_clocks(
            _read(os.path.join(hw, "freq1_input")) if hw else None,
            _read(os.path.join(hw, "freq2_input")) if hw else None,
            sclk_table)
        s.pstate = parse_pstate(
            _read(os.path.join(dev, "power_dpm_force_performance_level")),
            sclk_table) or ""
        return s

    # -- processes (DRM fdinfo delta rings)

    def processes(self) -> list[GpuProcess]:
        clients = [c for c in scan_clients(self._proc_root) if c.driver == "amdgpu"]
        if len(clients) > MAX_CLIENTS:
            clients = sorted(
                clients,
                key=lambda c: c.fields.get("drm-memory-vram", 0),
                reverse=True)[:MAX_CLIENTS]
        pci_to_idx = {
            os.path.basename(os.path.realpath(dev)): i
            for i, dev in enumerate(self._cards)
        }
        per_pid: dict[int, dict] = {}
        for c in clients:
            prev = self._client_ring.get(c.client_id)
            self._client_ring[c.client_id] = dict(c.fields)
            idx = pci_to_idx.get(c.pdev, 0)
            agg = per_pid.setdefault(
                c.pid, {"mem": 0, "sm": [], "enc": [], "dec": [], "idx": idx})
            agg["mem"] += c.fields.get("drm-memory-vram", 0)
            if prev is None:
                continue  # first sighting: no delta baseline
            for key, match in (("sm", _is_gfx_engine), ("enc", _is_enc_engine),
                               ("dec", _is_dec_engine)):
                u = engine_utilization(prev, c.fields, match)
                if u is not None:
                    agg[key].append(u)
        live = {c.client_id for c in clients}
        if len(self._client_ring) > 4096:
            self._client_ring = {k: v for k, v in self._client_ring.items()
                                 if k in live}
        out: list[GpuProcess] = []
        for pid, agg in sorted(per_pid.items()):
            out.append(GpuProcess(
                pid=pid, gpu_index=agg["idx"], type="C",
                mem_used=agg["mem"],
                sm_pct=max(agg["sm"]) if agg["sm"] else None,
                enc_pct=max(agg["enc"]) if agg["enc"] else None,
                dec_pct=max(agg["dec"]) if agg["dec"] else None,
            ))
        enrich_from_proc(out)
        return out

    def proc_info(self, pids: list[int], with_env: bool = False) -> dict[int, ProcInfo]:
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

    def close(self) -> None:
        pass


def _is_gfx_engine(name: str) -> bool:
    return name.startswith("gfx")


def _is_enc_engine(name: str) -> bool:
    low = name.lower()
    return "enc" in low or "vce" in low


def _is_dec_engine(name: str) -> bool:
    low = name.lower()
    return "dec" in low or "uvd" in low
