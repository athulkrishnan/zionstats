"""util: NaType sentinel (ported from nvitop/api) and the TTL cache."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.util import NaType, TTLCache


def test_natype_renders_as_na():
    n = NaType  # public singleton instance (nvitop API compatibility)
    assert repr(n) == "N/A"
    assert str(n) == "N/A"
    assert f"{n}" == "N/A"
    assert math.isnan(float(n))
    print("  NaType render")


def test_natype_comparisons_all_false():
    n = NaType
    assert not (n == 0)
    assert not (n < 100)
    assert not (n > 100)
    assert not (n <= 100)
    assert not (n >= 100)
    assert n != 0
    assert not (0 == n)
    print("  NaType comparisons")


def test_natype_arithmetic_stays_nan():
    n = NaType
    assert math.isnan(n + 1)
    assert math.isnan(n * 2)
    assert math.isnan(abs(n))
    print("  NaType arithmetic")


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_ttlcache_hit_and_expiry():
    clk = FakeClock()
    c = TTLCache(maxsize=8, ttl=30.0, clock=clk)
    c["a"] = 1
    assert c.get("a") == 1
    assert "a" in c
    clk.t += 31.0
    assert c.get("a") is None, "entry must expire after ttl"
    assert "a" not in c
    print("  ttl expiry")


def test_ttlcache_setitem_refreshes_ttl():
    clk = FakeClock()
    c = TTLCache(ttl=30.0, clock=clk)
    c["k"] = 1
    clk.t += 20.0
    c["k"] = 2                      # re-set refreshes the stamp
    clk.t += 20.0                   # 40 s since first set, 20 since re-set
    assert c.get("k") == 2
    print("  re-set refreshes ttl")


def test_ttlcache_maxsize_eviction():
    clk = FakeClock()
    c = TTLCache(maxsize=3, ttl=100.0, clock=clk)
    for i in range(5):
        c[f"k{i}"] = i
    assert len(c) <= 3
    assert c.get("k4") == 4
    assert c.get("k0") is None, "oldest must be evicted first"
    print("  maxsize eviction")


def test_ttlcache_getitem_raises_and_expire():
    clk = FakeClock()
    c = TTLCache(ttl=10.0, clock=clk)
    try:
        c["nope"]
        raised = False
    except KeyError:
        raised = True
    assert raised
    c["x"] = 1
    c["y"] = 2
    clk.t += 11.0
    assert c.expire() == 2
    assert len(c) == 0
    print("  getitem/expire")


def test_ttlcache_default_clock_monotonic():
    import time as _t
    c = TTLCache(ttl=0.05)
    c["z"] = 1
    assert c.get("z") == 1
    _t.sleep(0.06)
    assert c.get("z") is None
    print("  default monotonic clock")


if __name__ == "__main__":
    test_natype_renders_as_na()
    test_natype_comparisons_all_false()
    test_natype_arithmetic_stays_nan()
    test_ttlcache_hit_and_expiry()
    test_ttlcache_setitem_refreshes_ttl()
    test_ttlcache_maxsize_eviction()
    test_ttlcache_getitem_raises_and_expire()
    test_ttlcache_default_clock_monotonic()
    print("util: ALL PASS")
