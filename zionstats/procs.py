"""Process forensics: /proc enrichment, ancestor chains, tree rows, and
signal delivery for the process table.

UI concept (process tree, tagging, kill-confirmation) credited to nvitop
(GPL-3.0); this is a clean-room implementation -- no nvitop TUI/CLI code
was used.  The tree walks /proc PPID links directly.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .util import TTLCache

if TYPE_CHECKING:
    from .nvml_layer import GpuProcess

MAX_ENV_BYTES = 256 << 10  # cap /proc/<pid>/environ reads


@dataclass
class ProcInfo:
    pid: int
    ppid: int | None = None
    user: str = "?"
    cmdline: str = ""
    cwd: str = ""
    exe: str = ""
    environ: list[tuple[str, str]] = field(default_factory=list)


def parse_stat_ppid(stat_bytes: bytes) -> int | None:
    """PPID is field 4 of /proc/<pid>/stat; the comm field can contain
    spaces and parens, so split on the last ') ' first."""
    try:
        after = stat_bytes.decode(errors="replace").rsplit(") ", 1)[-1]
        return int(after.split()[1])
    except (IndexError, ValueError):
        return None


def read_proc_info(pid: int, cache: TTLCache | None = None) -> ProcInfo | None:
    """Read cmdline/environ/cwd/exe/ppid for a host PID.  Returns None when
    the process is gone or unreadable.  `cache` (optional) memoizes the
    stable parts (cmdline/exe/ppid) -- environ and cwd stay live."""
    key = ("procinfo", pid)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            # clone: caller may mutate environ lists
            return ProcInfo(pid=cached.pid, ppid=cached.ppid, user=cached.user,
                            cmdline=cached.cmdline, cwd="", exe=cached.exe,
                            environ=[])
    base = f"/proc/{pid}"
    try:
        with open(f"{base}/stat", "rb") as f:
            ppid = parse_stat_ppid(f.read())
        with open(f"{base}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
        try:
            exe = os.path.realpath(f"{base}/exe")
            if not os.path.exists(exe):
                exe = ""
        except OSError:
            exe = ""
    except OSError:
        return None
    try:
        user = _uid_to_name(os.stat(base).st_uid)
    except OSError:
        user = "?"
    info = ProcInfo(pid=pid, ppid=ppid, user=user, cmdline=cmdline, exe=exe)
    if cache is not None:
        cache[key] = info
    return info


def read_proc_env(pid: int, limit: int = MAX_ENV_BYTES) -> list[tuple[str, str]]:
    """Live /proc/<pid>/environ (NUL-separated).  Sorted by name; best-effort
    (kernel hides it from other users' processes)."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read(limit)
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        k, _, v = chunk.partition(b"=")
        if k:
            out.append((k.decode(errors="replace"), v.decode(errors="replace")))
    out.sort(key=lambda kv: kv[0])
    return out


def read_proc_cwd(pid: int) -> str:
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        return cwd if cwd.startswith("/") else ""
    except OSError:
        return ""


def ancestors_of(pid: int, table: dict[int, ProcInfo], max_depth: int = 16) -> list[int]:
    """PPID chain [parent, grandparent, ...] from a known ProcInfo table.
    Cycle-safe (visited set) and depth-capped; stops at pid <= 1 or when a
    link is missing from the table."""
    out: list[int] = []
    seen: set[int] = {pid}
    cur = pid
    for _ in range(max_depth):
        info = table.get(cur)
        if info is None:
            break
        ppid = info.ppid
        if ppid is None or ppid <= 1 or ppid in seen:
            break
        out.append(ppid)
        seen.add(ppid)
        cur = ppid
    return out


@dataclass
class TreeRow:
    pid: int
    gpu_index: int
    depth: int
    is_ancestor: bool
    is_gpu: bool
    proc: GpuProcess | None
    label: str
    key: str  # stable identity for selection: "p<pid>" or "a<pid>@<gpu>"


def build_tree_rows(procs: list[GpuProcess], table: dict[int, ProcInfo],
                    sort_key: Callable[[GpuProcess], Any] | None = None,
                    reverse: bool = False) -> list[TreeRow]:
    """Group GPU processes by device; splice dim ancestor rows above each
    process using its /proc PPID chain.  Ancestor rows are display-only
    (is_ancestor=True) and never selectable."""
    rows: list[TreeRow] = []
    by_gpu: dict[int, list[GpuProcess]] = {}
    for p in procs:
        by_gpu.setdefault(p.gpu_index, []).append(p)
    for gpu in sorted(by_gpu):
        plist = by_gpu[gpu]
        if sort_key is not None:
            plist = sorted(plist, key=sort_key, reverse=reverse)
        # ancestor chains deduplicated within this GPU group
        claimed: set[int] = set()
        for p in plist:
            chain = [a for a in ancestors_of(p.pid, table) if a not in claimed]
            # display order: most distant ancestor first, parent just above
            # the process row
            for i, a in enumerate(reversed(chain)):
                claimed.add(a)
                info = table.get(a)
                label = (info.cmdline if info and info.cmdline else f"[pid {a}]")
                rows.append(TreeRow(pid=a, gpu_index=gpu, depth=i, is_ancestor=True,
                                    is_gpu=False, proc=None, label=label, key=f"a{a}@{gpu}"))
            rows.append(TreeRow(pid=p.pid, gpu_index=gpu, depth=len(chain),
                                is_ancestor=False, is_gpu=True, proc=p,
                                label=p.command, key=f"p{p.pid}"))
    return rows


def signal_process(pid: int, sig: int) -> None:
    """Send a signal to a host PID.  Propagates ProcessLookupError
    (already gone) and PermissionError to the caller for friendly handling.
    Callers must gate this on --readonly / demo / replay BEFORE calling."""
    os.kill(pid, sig)


SIGNAL_NAME: dict[int, str] = {
    int(signal.SIGTERM): "SIGTERM",
    # Windows has no SIGKILL; TerminateProcess semantics come from os.kill
    int(getattr(signal, "SIGKILL", 9)): "SIGKILL",
}


def _uid_to_name(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)
