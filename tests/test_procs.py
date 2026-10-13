"""procs: /proc parsing, ancestor chains, tree rows, signal delivery."""
import os
import signal as sigmod
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats import procs
from zionstats.nvml_layer import GpuProcess
from zionstats.procs import (
    ProcInfo,
    ancestors_of,
    build_tree_rows,
    parse_stat_ppid,
    signal_process,
)
from zionstats.util import TTLCache


def test_parse_stat_ppid():
    stat = b"184722 (python) S 184700 184722 1 0 -1 4194560 0 0 0 0 150 50 0 0 20 5 0 0 1234"
    assert parse_stat_ppid(stat) == 184700
    # comm containing spaces and parens
    stat2 = b"99 (my (weird) proc) R 42 99 1 0 0 0 0 0 0 0 0 0 0 0 20 5 0 0 5"
    assert parse_stat_ppid(stat2) == 42
    assert parse_stat_ppid(b"garbage") is None
    assert parse_stat_ppid(b"12 (x) Z") is None  # no fields after comm
    assert parse_stat_ppid(b"12 (x) Z not-a-num 1") is None  # non-numeric ppid
    print("  parse_stat_ppid")


def _table(*specs) -> dict[int, ProcInfo]:
    return {pid: ProcInfo(pid=pid, ppid=ppid, cmdline=cmd)
            for pid, ppid, cmd in specs}


def test_ancestors_chain():
    t = _table((100, 90, "grandparent"), (90, 80, "parent"), (80, 1, "init-child"))
    assert ancestors_of(100, t) == [90, 80]
    print("  ancestor chain")


def test_ancestors_missing_link_and_root():
    t = _table((100, 90, "child"))
    # the dangling parent pid is claimed (rendered as "[pid 90]"), but the
    # walk stops there because its own record is absent
    assert ancestors_of(100, t) == [90]
    t2 = _table((100, 1, "child-of-init"))
    assert ancestors_of(100, t2) == []
    t3 = _table((100, None, "orphan"))
    assert ancestors_of(100, t3) == []
    print("  missing links")


def test_ancestors_cycle_safe():
    # corrupted /proc: A -> B -> A
    t = _table((100, 200, "A"), (200, 100, "B"))
    chain = ancestors_of(100, t)
    assert chain == [200], "cycle must terminate immediately"
    # deeper cycle
    t2 = _table((1, 2, "a"), (2, 3, "b"), (3, 1, "c"))
    assert len(ancestors_of(1, t2, max_depth=16)) <= 2
    print("  cycle safety")


def test_ancestors_depth_cap():
    specs = [(i, i - 1, f"p{i}") for i in range(2, 40)]
    t = _table(*specs)
    t[40] = ProcInfo(pid=40, ppid=39, cmdline="leaf")
    assert len(ancestors_of(40, t, max_depth=16)) == 16
    print("  depth cap")


def _gpu(pid, gpu=0, mem=100, cmd="proc"):
    return GpuProcess(pid, gpu, "C", mem, 10.0, 10.0, 0.0, 0.0, 1.0, "u", cmd)


def test_build_tree_rows_structure():
    t = _table((800, 1, "sshd"), (810, 800, "bash run.sh"))
    procs = [_gpu(184722, gpu=0, cmd="python train.py")]
    t[184722] = ProcInfo(pid=184722, ppid=810, cmdline="python train.py")
    rows = build_tree_rows(procs, t)
    # distant ancestor first, parent just above the process row
    assert [r.pid for r in rows] == [800, 810, 184722]
    assert rows[0].is_ancestor is True
    assert rows[-1].is_ancestor is False
    assert [r.depth for r in rows] == [0, 1, 2]
    assert rows[-1].proc is procs[0]
    print("  tree structure")


def test_build_tree_rows_grouping_and_sort():
    t = _table((1, None, "init"))
    procs = [_gpu(300, gpu=1, mem=10), _gpu(100, gpu=0, mem=50), _gpu(200, gpu=0, mem=500)]
    rows = build_tree_rows(procs, t)
    gpus = [r.gpu_index for r in rows if r.is_gpu]
    assert gpus == sorted(gpus), "GPU groups must be ordered by index"
    pids = [r.pid for r in rows if r.is_gpu and r.gpu_index == 0]
    assert pids == [100, 200], "within a group, input order is kept without sort_key"
    # with a sort key (descending mem), order flips
    rows2 = build_tree_rows(procs, t, sort_key=lambda p: p.mem_used, reverse=True)
    pids2 = [r.pid for r in rows2 if r.is_gpu and r.gpu_index == 0]
    assert pids2 == [200, 100]
    print("  grouping + sort")


def test_build_tree_rows_shared_ancestor_claimed_once():
    t = _table((10, 1, "parent"))
    procs = [_gpu(11, gpu=0), _gpu(12, gpu=0)]
    t[11] = ProcInfo(pid=11, ppid=10, cmdline="a")
    t[12] = ProcInfo(pid=12, ppid=10, cmdline="b")
    rows = build_tree_rows(procs, t)
    anc_rows = [r for r in rows if r.is_ancestor]
    assert len(anc_rows) == 1, f"shared ancestor must render once, got {len(anc_rows)}"
    print("  shared ancestor dedup")


def test_signal_process_patches_clean():
    calls: list[tuple[int, int]] = []
    real_kill = os.kill
    os.kill = lambda pid, s: calls.append((pid, s))
    try:
        signal_process(4242, sigmod.SIGTERM)
    finally:
        os.kill = real_kill
    assert calls == [(4242, sigmod.SIGTERM)]
    print("  signal dispatch")


def test_signal_process_propagates_esrch_eperm():
    import signal as _s

    def esrch(pid, s):
        raise ProcessLookupError()

    def perm(pid, s):
        raise PermissionError()

    real_kill = os.kill
    try:
        os.kill = esrch
        try:
            signal_process(1, _s.SIGTERM)
            raised = False
        except ProcessLookupError:
            raised = True
        assert raised
        os.kill = perm
        try:
            signal_process(1, _s.SIGKILL)
            raised = False
        except PermissionError:
            raised = True
        assert raised
    finally:
        os.kill = real_kill
    print("  ESRCH/EPERM propagate")


def test_read_proc_info_against_self():
    """The only /proc entry guaranteed readable in CI is our own process."""
    me = os.getpid()
    cache = TTLCache(ttl=60.0)
    info = procs.read_proc_info(me, cache)
    assert info is not None
    assert info.pid == me
    assert info.ppid is not None and info.ppid > 0
    assert "python" in info.cmdline or info.cmdline
    assert isinstance(info.environ, list)
    # second read hits the cache (stable fields identical)
    info2 = procs.read_proc_info(me, cache)
    assert info2.cmdline == info.cmdline
    assert info2.environ == [], "environ stays out of the cached copy"
    gone = procs.read_proc_info(-1)
    assert gone is None
    print("  read_proc_info(self)")


def test_read_proc_env_and_cwd_self():
    me = os.getpid()
    env = procs.read_proc_env(me)
    assert isinstance(env, list)
    for k, v in env:
        assert "=" not in k and isinstance(v, str)
    cwd = procs.read_proc_cwd(me)
    assert cwd.startswith("/") or cwd == ""
    print("  env/cwd(self)")


if __name__ == "__main__":
    test_parse_stat_ppid()
    test_ancestors_chain()
    test_ancestors_missing_link_and_root()
    test_ancestors_cycle_safe()
    test_ancestors_depth_cap()
    test_build_tree_rows_structure()
    test_build_tree_rows_grouping_and_sort()
    test_build_tree_rows_shared_ancestor_claimed_once()
    test_signal_process_patches_clean()
    test_signal_process_propagates_esrch_eperm()
    test_read_proc_info_against_self()
    test_read_proc_env_and_cwd_self()
    print("procs: ALL PASS")
