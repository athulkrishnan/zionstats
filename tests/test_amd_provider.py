"""AMD provider: sysfs parsers (unit conversions, garbage tolerance),
vendor discovery, fdinfo per-process deltas, demo card, and the Insights
power-integration fallback that covers the missing hw energy counter."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.amd import (
    AmdProvider,
    discover_cards,
    parse_clocks,
    parse_fan,
    parse_gpu_busy_percent,
    parse_power,
    parse_pstate,
    parse_temp,
    parse_vram,
)
from zionstats.insights import Insights
from zionstats.nvml_layer import DemoProvider, ProviderError, Sample

FIX_DRM = Path(__file__).resolve().parent / "fixtures" / "amd" / "drm"
FIX_PROC = Path(__file__).resolve().parent / "fixtures" / "amd" / "proc"


def test_parse_gpu_busy_percent():
    assert parse_gpu_busy_percent("37\n") == 37.0
    assert parse_gpu_busy_percent("0") == 0.0
    assert parse_gpu_busy_percent(" 100 ") == 100.0
    assert parse_gpu_busy_percent(None) is None
    assert parse_gpu_busy_percent("banana") is None
    assert parse_gpu_busy_percent("140\n") is None, "out of range -> None"
    print("  gpu_busy_percent")


def test_parse_vram_units():
    assert parse_vram("1234567890\n", "17163091968\n") == (1234567890, 17163091968)
    assert parse_vram(None, "100\n") == (None, 100)
    assert parse_vram("x\n", "y\n") == (None, None)
    print("  vram bytes")


def test_parse_power_micro_watts():
    p = parse_power("84123456\n", "254000000\n", "0\n", "299000000\n")
    assert abs(p["power_w"] - 84.123456) < 1e-9
    assert p["power_limit_w"] == 254.0
    assert p["power_min_w"] == 0.0
    assert p["power_max_w"] == 299.0
    assert parse_power(None, None, None, None) == {
        "power_w": None, "power_limit_w": None,
        "power_min_w": None, "power_max_w": None}
    assert parse_power("garbage\n", "", "-3\n", "z\n")["power_w"] is None
    print("  power uW -> W")


def test_parse_temp_milli_celsius():
    assert parse_temp("55500\n") in (55, 56), "55.5 C rounds to nearest int"
    assert parse_temp("78500\n") in (78, 79)
    assert parse_temp("0\n") == 0
    assert parse_temp(None) is None
    assert parse_temp("hot\n") is None
    print("  hwmon mC -> C")


def test_parse_fan_pwm():
    assert parse_fan("153\n", "255\n") == 60
    assert parse_fan("0\n", "255\n") == 0
    assert parse_fan("255\n", None) == 100, "missing pwm1_max defaults to 255"
    assert parse_fan(None, "255\n") is None
    assert parse_fan("x\n", "255\n") is None
    print("  pwm 0-255 -> percent")


def test_parse_clocks_hz_and_dpm_fallback():
    sm, mem = parse_clocks("1800000000\n", "2500000000\n", None)
    assert sm == 1800 and mem == 2500, "Hz -> MHz"
    # freq1_input missing -> pp_dpm_sclk '*' entry
    sm, mem = parse_clocks(None, "2500000000\n", "0: 500M\n1: 2300M\n2: 2400M *\n")
    assert sm == 2400 and mem == 2500
    # both missing -> (None, None)
    assert parse_clocks(None, None, None) == (None, None)
    assert parse_clocks("x\n", "y\n", "no marker\n") == (None, None)
    print("  clocks Hz -> MHz + dpm fallback")


def test_parse_pstate():
    assert parse_pstate("auto\n", None) == "auto"
    assert parse_pstate(None, "0: 500M *\n") == "0: 500M *"
    assert parse_pstate(None, None) is None
    assert parse_pstate("\n", "") is None
    print("  pstate honest mapping")


def test_discovery_and_provider_fixture():
    cards = discover_cards(str(FIX_DRM))
    assert len(cards) == 1, "nvidia-vendor card1 must be skipped"
    assert cards[0].endswith("card0/device")
    prov = AmdProvider(sysfs_root=str(FIX_DRM), proc_root=str(FIX_PROC))
    devs = prov.devices()
    assert len(devs) == 1
    assert devs[0]["name"] == "AMD Radeon RX 7900 XTX"
    assert devs[0]["uuid"].startswith("AMD-0xdeadbeef")
    s = prov.sample()[0]
    assert s.vendor == "amd"
    assert s.util_gpu == 37.0
    assert s.mem_used == 1234567890 and s.mem_total == 17163091968
    assert abs(s.power_w - 84.123456) < 1e-9
    assert s.power_limit_w == 254.0 and s.power_max_w == 299.0
    assert s.temp_gpu == 55 or s.temp_gpu == 56
    assert s.temp_gpu_max == 78 or s.temp_gpu_max == 79, "junction -> hottest"
    assert s.temp_mem in (62, 63)
    assert s.fan_pct == 60
    assert s.clocks_sm_mhz == 2400, "no freq1_input -> dpm table fallback"
    assert s.clocks_mem_mhz == 2500
    assert s.pstate == "auto"
    assert s.util_mem is None, "documented gap: no bandwidth utilization"
    assert s.energy_mj is None, "no hw counter on this path"
    prov.close()
    print("  provider over fixture tree")


def test_provider_empty_discovery_fails_clean():
    with tempfile.TemporaryDirectory() as d:
        try:
            AmdProvider(sysfs_root=d)
            raise AssertionError("empty root must raise")
        except ProviderError as e:
            assert str(d) in str(e), "message names the sysfs root"
    print("  empty discovery -> clean ProviderError")


def test_fdinfo_delta_over_two_scans():
    """First sighting yields None utilization; second scan computes the
    cycles/total-cycles delta; memory sums across a pid's clients."""
    with tempfile.TemporaryDirectory() as d:
        proc = Path(d) / "proc"
        shutil.copytree(FIX_PROC, proc, symlinks=True)
        prov = AmdProvider(sysfs_root=str(FIX_DRM), proc_root=str(proc))
        first = {p.pid: p for p in prov.processes()}
        assert set(first) == {4242}, "i915 pid 5000 must be filtered out"
        assert first[4242].mem_used == 4096 + 2048
        assert first[4242].sm_pct is None, "first sighting: no baseline"
        # advance both clients' cycle counters (fd 3: +1000/+10000 cycles,
        # fd 4: +300/+1000) and vram on fd 4
        f3 = proc / "4242" / "fdinfo" / "3"
        f3.write_text(f3.read_text()
                      .replace("drm-cycles-gfx:\t1000", "drm-cycles-gfx:\t2000")
                      .replace("drm-total-cycles-gfx:\t10000",
                               "drm-total-cycles-gfx:\t20000"))
        f4 = proc / "4242" / "fdinfo" / "4"
        f4.write_text(f4.read_text()
                      .replace("drm-cycles-gfx:\t100", "drm-cycles-gfx:\t400")
                      .replace("drm-total-cycles-gfx:\t1000",
                               "drm-total-cycles-gfx:\t2000")
                      .replace("drm-memory-vram:\t2048", "drm-memory-vram:\t8192"))
        second = {p.pid: p for p in prov.processes()}
        # fd3: 1000/10000 = 10 %, fd4: 300/1000 = 30 % -> max 30 %
        assert abs(second[4242].sm_pct - 30.0) < 1e-9
        assert second[4242].mem_used == 4096 + 8192
        prov.close()
    print("  fdinfo deltas over two scans")


def test_client_cap_256():
    """More than 256 GPU clients: keep the highest-VRAM 256."""
    import zionstats.amd as amd_mod

    prov = AmdProvider(sysfs_root=str(FIX_DRM), proc_root=str(FIX_PROC))
    fake = [type("C", (), {
        "pid": 900000 + i, "fd": 3, "client_id": str(900000 + i),
        "driver": "amdgpu", "pdev": "",
        "fields": {"drm-memory-vram": i}})()
        for i in range(300)]
    orig = amd_mod.scan_clients
    amd_mod.scan_clients = lambda root: fake
    try:
        out = prov.processes()
        assert len(out) == 256
        # highest-VRAM kept: the cap drops the vram=0..43 tail
        assert {p.pid for p in out} <= {900000 + i for i in range(44, 300)}
    finally:
        amd_mod.scan_clients = orig
    prov.close()
    print("  256-client cap")


def test_demo_third_card():
    prov = DemoProvider()
    devs = prov.devices()
    assert [d["index"] for d in devs] == [0, 1, 2]
    samples = {s.index: s for s in prov.sample()}
    assert set(samples) == {0, 1, 2}
    amd = samples[2]
    assert amd.vendor == "amd" and "7900" in amd.name
    assert amd.mem_total == 24 * 1024**3
    assert amd.temp_gpu_max is not None
    assert amd.temp_gpu_max >= amd.temp_gpu, "junction never below edge"
    assert amd.fan_rpm is None
    assert amd.energy_mj is None
    only = DemoProvider(only=[2])
    assert [s.index for s in only.sample()] == [2]
    assert [d["index"] for d in only.devices()] == [2]
    print("  demo AMD card + --only")


def test_insights_power_integration_fallback():
    """No energy_mj (AMD path): session Wh must come from power * dt."""
    eng = Insights()
    base = Sample(ts=0.0, index=2, vendor="amd", power_w=100.0)
    ins0 = eng.update(base, None)          # baseline only
    assert ins0.session_wh == 0.0
    ins1 = eng.update(Sample(ts=10.0, index=2, vendor="amd", power_w=100.0), None)
    assert abs(ins1.session_wh - 100.0 * 10.0 / 3600.0) < 1e-9
    print("  insights power-integration fallback")


if __name__ == "__main__":
    test_parse_gpu_busy_percent()
    test_parse_vram_units()
    test_parse_power_micro_watts()
    test_parse_temp_milli_celsius()
    test_parse_fan_pwm()
    test_parse_clocks_hz_and_dpm_fallback()
    test_parse_pstate()
    test_discovery_and_provider_fixture()
    test_provider_empty_discovery_fails_clean()
    test_fdinfo_delta_over_two_scans()
    test_client_cap_256()
    test_demo_third_card()
    test_insights_power_integration_fallback()
    print("amd provider: ALL PASS")
