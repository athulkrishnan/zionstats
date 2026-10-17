"""Shared DRM fdinfo scanner (per-process GPU stats for AMD + Intel).

Since kernel 5.14 the amdgpu/i915/xe drivers expose per-open-client GPU
usage in /proc/<pid>/fdinfo/<fd> as drm-* keys; only fds whose symlink
target is a /dev/dri render or card node carry them, which bounds the
scan to real GPU clients.  Pure stdlib, nothing raises at scan time,
and a client's first sighting yields no utilization (no delta baseline)
-- the same best-effort contract as the /proc CPU sampler in
nvml_layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class DrmClient:
    pid: int
    fd: int
    client_id: str
    driver: str
    pdev: str = ""  # PCI bus id when the kernel exposes drm-pdev
    fields: dict[str, int] = field(default_factory=dict)


def _is_drm_node(target: str) -> bool:
    return target.startswith("/dev/dri/renderD") or target.startswith("/dev/dri/card")


def parse_fdinfo(text: str) -> dict[str, str]:
    """drm-* keys -> raw string values.  Everything else is ignored."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key = key.strip()
        if key.startswith("drm-"):
            out[key] = val.strip()
    return out


def scan_clients(proc_root: str = "/proc") -> list[DrmClient]:
    """One DrmClient per (pid, drm fd).  Unreadable pids/fds are skipped."""
    clients: list[DrmClient] = []
    try:
        pids = os.listdir(proc_root)
    except OSError:
        return clients
    for pid in pids:
        if not pid.isdigit():
            continue
        fd_dir = os.path.join(proc_root, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if not _is_drm_node(target):
                continue
            try:
                with open(os.path.join(proc_root, pid, "fdinfo", fd),
                          encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            kv = parse_fdinfo(text)
            fields = {k: int(v) for k, v in kv.items()
                      if k not in ("drm-client-id", "drm-driver", "drm-pdev")
                      and v.lstrip("-").isdigit()}
            clients.append(DrmClient(
                pid=int(pid), fd=int(fd),
                client_id=kv.get("drm-client-id", f"{pid}:{fd}"),
                driver=kv.get("drm-driver", ""),
                pdev=kv.get("drm-pdev", ""),
                fields=fields,
            ))
    return clients


def engine_utilization(prev: dict[str, int], cur: dict[str, int],
                       engine_match=None) -> float | None:
    """drm-cycles-<engine> delta over drm-total-cycles-<engine> delta,
    summed across engines (optionally restricted to engines whose name
    matches).  None without a two-scan baseline or a zero total."""
    cyc = tot = 0
    for k, v in cur.items():
        if not k.startswith("drm-cycles-"):
            continue
        eng = k[len("drm-cycles-"):]
        if engine_match is not None and not engine_match(eng):
            continue
        tkey = "drm-total-cycles-" + eng
        if k in prev and tkey in prev and tkey in cur:
            cyc += max(0, v - prev[k])
            tot += max(0, cur[tkey] - prev[tkey])
    if tot <= 0:
        return None
    return max(0.0, min(100.0, cyc / tot * 100.0))
