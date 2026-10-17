"""DRM fdinfo scanner + delta math (shared by the AMD and Intel
providers).  Pure, fixture-driven, no /proc on the test machine."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.drmfd import engine_utilization, parse_fdinfo, scan_clients

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "amd" / "proc"


def test_parse_fdinfo_keeps_drm_keys_only():
    text = ("pos:\t480000\nflags:\t0100002\nmnt_id:\t26\n"
            "drm-driver:\tamdgpu\n"
            "drm-client-id:\t42\n"
            "drm-cycles-gfx:\t1000\n"
            "drm-total-cycles-gfx:\t10000\n"
            "drm-memory-vram:\t4096\n")
    kv = parse_fdinfo(text)
    assert set(kv) == {"drm-driver", "drm-client-id", "drm-cycles-gfx",
                       "drm-total-cycles-gfx", "drm-memory-vram"}
    assert kv["drm-driver"] == "amdgpu"
    print("  fdinfo key filter")


def test_scan_clients_fixture_tree():
    clients = scan_clients(str(FIXTURES))
    by_client = {c.client_id: c for c in clients}
    # pid 4242: two drm fds + one non-drm fd (skipped)
    assert set(by_client) >= {"42", "43"}, by_client
    c42 = by_client["42"]
    assert c42.pid == 4242 and c42.driver == "amdgpu"
    assert c42.pdev == "0000:03:00.0"
    assert c42.fields["drm-cycles-gfx"] == 1000
    assert c42.fields["drm-total-cycles-gfx"] == 10000
    assert c42.fields["drm-memory-vram"] == 4096
    c43 = by_client["43"]
    assert c43.pid == 4242 and c43.fields["drm-memory-vram"] == 2048
    # pid 5000 is a drm client but a different driver: still scanned
    assert any(c.pid == 5000 and c.driver == "i915" for c in clients)
    # the /etc/hosts fd must not appear anywhere
    assert all(c.fd != 5 for c in clients if c.pid == 4242)
    print("  fixture scan (drm fds only, per-client fields)")


def test_scan_clients_empty_and_bad_roots():
    assert scan_clients("/nonexistent/proc") == []
    with tempfile.TemporaryDirectory() as d:
        assert scan_clients(d) == []
    print("  empty/bad roots tolerated")


def test_engine_utilization_delta_math():
    prev = {"drm-cycles-gfx": 1000, "drm-total-cycles-gfx": 10000}
    cur = {"drm-cycles-gfx": 2000, "drm-total-cycles-gfx": 20000}
    # delta 1000 / delta 10000 = 10 %
    assert abs(engine_utilization(prev, cur) - 10.0) < 1e-9
    print("  cycles/total-cycles delta")


def test_engine_utilization_first_sighting_and_garbage():
    cur = {"drm-cycles-gfx": 2000, "drm-total-cycles-gfx": 20000}
    assert engine_utilization({}, cur) is None, "no baseline -> None"
    # counter went backwards (client replaced): clamp to 0, never negative
    prev = {"drm-cycles-gfx": 5000, "drm-total-cycles-gfx": 10000}
    assert engine_utilization(prev, cur) == 0.0
    # zero total window -> None (idle ring, no reference ticks)
    assert engine_utilization({"drm-cycles-gfx": 5, "drm-total-cycles-gfx": 5},
                              {"drm-cycles-gfx": 5, "drm-total-cycles-gfx": 5}) is None
    print("  first sighting / clamp / zero-total")


def test_engine_utilization_engine_filter():
    prev = {"drm-cycles-gfx": 0, "drm-total-cycles-gfx": 1000,
            "drm-cycles-enc": 0, "drm-total-cycles-enc": 1000}
    cur = {"drm-cycles-gfx": 100, "drm-total-cycles-gfx": 2000,
           "drm-cycles-enc": 800, "drm-total-cycles-enc": 2000}
    gfx = engine_utilization(prev, cur, lambda e: e.startswith("gfx"))
    enc = engine_utilization(prev, cur, lambda e: "enc" in e)
    assert abs(gfx - 10.0) < 1e-9, gfx
    assert abs(enc - 80.0) < 1e-9, enc
    # a stalled engine (total stopped moving) contributes nothing, not 100%
    stalled = engine_utilization(
        {"drm-cycles-enc": 0, "drm-total-cycles-enc": 1000},
        {"drm-cycles-enc": 800, "drm-total-cycles-enc": 1000},
        lambda e: "enc" in e)
    assert stalled is None
    print("  per-engine filter")


if __name__ == "__main__":
    test_parse_fdinfo_keeps_drm_keys_only()
    test_scan_clients_fixture_tree()
    test_scan_clients_empty_and_bad_roots()
    test_engine_utilization_delta_math()
    test_engine_utilization_first_sighting_and_garbage()
    test_engine_utilization_engine_filter()
    print("drmfd: ALL PASS")
