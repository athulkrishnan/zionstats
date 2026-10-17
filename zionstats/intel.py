"""Intel provider (i915/xe sysfs + DRM fdinfo).  EXPERIMENTAL.

Per-engine aggregation is best-effort across driver generations: i915
exposes gt/gt0/rps_cur_freq (MHz); the xe driver uses a tile0 layout and
its frequency units here follow the fixture pin pending hardware
verification.  fdinfo client keys (drm-cycles-*/drm-memory-*) are shared
with the AMD provider via drmfd.

Deliberate honest gaps: NO power draw (client GPUs have no
GPU-attributable power on this path; RAPL is socket-level and would be
a lie), no fan, no energy counter.  util_gpu is derived from fdinfo
cycles deltas, so it reads None until a second scan sees GPU clients.
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

VENDOR_ID = "0x8086"
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


def parse_cur_freq_mhz(text: str | None) -> int | None:
    """Bare-int frequency files; the kernel exposes MHz for both the i915
    rps_cur_freq and the pinned xe freq0_cur_freq layouts."""
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def parse_temp(text: str | None) -> int | None:
    """hwmon tempN_input is MILLIcelsius."""
    if text is None:
        return None
    try:
        return round(int(text.strip()) / 1000.0)
    except ValueError:
        return None


def discover_cards(sysfs_root: str) -> list[tuple[str, str]]:
    """(device dir, driver) for Intel (0x8086) cards, driver from uevent."""
    out: list[tuple[str, str]] = []
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
        if vendor is None or vendor.strip().lower() != VENDOR_ID:
            continue
        driver = ""
        for line in (_read(os.path.join(dev, "uevent")) or "").splitlines():
            if line.startswith("DRIVER="):
                driver = line.split("=", 1)[1].strip()
        out.append((dev, driver))
    return out


def _is_render_engine(name: str) -> bool:
    low = name.lower()
    return low.startswith("rcs") or low.startswith("ccs")


class IntelProvider:
    """Duck-typed twin of NvmlProvider over i915/xe sysfs + fdinfo."""

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
            raise ProviderError(f"no Intel GPUs found on {sysfs_root}")
        self._cards = cards
        self._proc_cache = TTLCache(maxsize=256, ttl=30.0)
        self._client_ring: dict[str, dict[str, int]] = {}
        # card-level engine sums: util_gpu is the fdinfo delta between scans
        self._card_ring: dict[int, dict[str, int]] = {}
        self._card_ring_prev: dict[int, dict[str, int]] = {}

    def environment(self) -> dict:
        uname = os.uname()
        driver = ""
        for mod in ("xe", "i915"):
            v = (_read(f"/sys/module/{mod}/version") or "").strip()
            if v:
                driver = f"{mod} {v}"
                break
        return {
            "hostname": socket.gethostname(),
            "kernel": f"{uname.sysname} {uname.release}",
            "driver": driver,
            "nvml": "",
            "cuda_max": "",
            "provider": "intel",
        }

    def _card_name(self, dev: str, driver: str, index: int) -> str:
        product = (_read(os.path.join(dev, "product_name")) or "").strip()
        if product:
            return product
        return driver or f"Intel GPU {index}"

    def _card_uuid(self, dev: str, index: int) -> str:
        busid = os.path.basename(os.path.realpath(dev))
        return f"INTEL-{busid or index}"

    def devices(self) -> list[dict]:
        return [{"index": i, "name": self._card_name(dev, drv, i),
                 "uuid": self._card_uuid(dev, i)}
                for i, (dev, drv) in enumerate(self._cards)]

    def _hwmon_dir(self, dev: str) -> str:
        hwmon_root = os.path.join(dev, "hwmon")
        try:
            entries = sorted(os.listdir(hwmon_root))
        except OSError:
            return ""
        return os.path.join(hwmon_root, entries[0]) if entries else ""

    def _clock_path(self, dev: str, driver: str) -> str:
        if driver == "xe":
            return os.path.join(dev, "tile0", "gt0", "freq0_cur_freq")
        return os.path.join(dev, "gt", "gt0", "rps_cur_freq")

    def sample(self) -> list[Sample]:
        now = time.time()
        return [self._sample_card(i, dev, drv, now)
                for i, (dev, drv) in enumerate(self._cards)]

    def _sample_card(self, i: int, dev: str, driver: str, now: float) -> Sample:
        s = Sample(ts=now, index=i, vendor="intel")
        s.uuid = self._card_uuid(dev, i)
        s.name = self._card_name(dev, driver, i)
        total = _read_int(os.path.join(dev, "mem_info_vram_total"))
        s.mem_total = total if total is not None else 0
        hw = self._hwmon_dir(dev)
        if hw:
            s.temp_gpu = parse_temp(_read(os.path.join(hw, "temp1_input")))
        s.clocks_sm_mhz = parse_cur_freq_mhz(_read(self._clock_path(dev, driver)))
        # per-client sums from the last processes() scan (card-level view)
        cur = self._card_ring.get(i)
        if cur:
            vram = sum(v for k, v in cur.items()
                       if k.startswith("drm-memory-") and "vram" in k)
            s.mem_used = vram
            prev = self._card_ring_prev.get(i)
            if prev is not None:
                s.util_gpu = engine_utilization(prev, cur, _is_render_engine)
        # power_w stays None by design (no GPU-attributable sysfs power;
        # RAPL would be socket-level, not the GPU)
        return s

    def processes(self) -> list[GpuProcess]:
        wanted = ("i915", "xe")
        clients = [c for c in scan_clients(self._proc_root) if c.driver in wanted]
        if len(clients) > MAX_CLIENTS:
            clients = sorted(
                clients,
                key=lambda c: sum(v for k, v in c.fields.items()
                                  if k.startswith("drm-memory-") and "vram" in k),
                reverse=True)[:MAX_CLIENTS]
        pci_to_idx = {
            os.path.basename(os.path.realpath(dev)): i
            for i, (dev, _drv) in enumerate(self._cards)
        }
        per_pid: dict[int, dict] = {}
        card_sums: dict[int, dict[str, int]] = {}
        for c in clients:
            prev = self._client_ring.get(c.client_id)
            self._client_ring[c.client_id] = dict(c.fields)
            idx = pci_to_idx.get(c.pdev, 0)
            agg = per_pid.setdefault(
                c.pid, {"mem": 0, "sm": [], "idx": idx})
            agg["mem"] += sum(v for k, v in c.fields.items()
                              if k.startswith("drm-memory-") and "vram" in k)
            if prev is not None:
                u = engine_utilization(prev, c.fields, _is_render_engine)
                if u is not None:
                    agg["sm"].append(u)
            sums = card_sums.setdefault(idx, {})
            for k, v in c.fields.items():
                sums[k] = sums.get(k, 0) + v
        # feed the card-level ring consumed by sample()
        self._card_ring_prev = dict(self._card_ring)
        self._card_ring = card_sums
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
