"""Intel provider (EXPERIMENTAL): i915/xe discovery + parsers, fdinfo
card/process utilization, deliberate power absence, demo 4th card."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.intel import IntelProvider, discover_cards, parse_cur_freq_mhz, parse_temp
from zionstats.nvml_layer import DemoProvider, ProviderError

FIX_DRM = Path(__file__).resolve().parent / "fixtures" / "intel" / "drm"
FIX_PROC = Path(__file__).resolve().parent / "fixtures" / "intel" / "proc"


def test_parse_freq_and_temp():
    assert parse_cur_freq_mhz("1250\n") == 1250
    assert parse_cur_freq_mhz(None) is None
    assert parse_cur_freq_mhz("fast\n") is None
    assert parse_temp("45000\n") in (45, 46), "mC -> C"
    assert parse_temp(None) is None
    print("  freq/temp parsers")


def test_discovery_drivers():
    cards = discover_cards(str(FIX_DRM))
    assert [d for _d, d in cards] == ["i915", "xe"]
    assert cards[0][0].endswith("card0/device")
    assert discover_cards("/nonexistent") == []
    print("  i915 + xe discovery")


def test_provider_i915_fixture():
    prov = IntelProvider(sysfs_root=str(FIX_DRM), proc_root=str(FIX_PROC))
    devs = prov.devices()
    assert len(devs) == 2
    assert devs[0]["name"] == "i915" and devs[0]["uuid"].startswith("INTEL-")
    assert devs[1]["name"] == "xe"
    env = prov.environment()
    assert env["provider"] == "intel"
    s0 = prov.sample()[0]
    assert s0.vendor == "intel"
    assert s0.clocks_sm_mhz == 1250, "i915 gt/gt0/rps_cur_freq (MHz)"
    assert s0.temp_gpu in (45, 46)
    assert s0.power_w is None, "no GPU-attributable power -- ever (no RAPL)"
    assert s0.fan_pct is None and s0.fan_rpm is None
    assert s0.util_gpu is None, "no fdinfo baseline yet"
    s1 = prov.sample()[1]
    assert s1.clocks_sm_mhz == 2350, "xe tile0/gt0/freq0_cur_freq"
    assert s1.mem_total == 28615671808, "xe exposes mem_info_vram_total"
    assert s0.mem_total == 0, "i915 card lacks the total file -> UI degrades"
    prov.close()
    print("  provider over fixture tree (both drivers)")


def test_provider_empty_fails_clean():
    with tempfile.TemporaryDirectory() as d:
        try:
            IntelProvider(sysfs_root=d)
            raise AssertionError("empty root must raise")
        except ProviderError as e:
            assert str(d) in str(e)
    print("  empty discovery -> clean ProviderError")


def test_fdinfo_card_and_process_deltas():
    with tempfile.TemporaryDirectory() as d:
        proc = Path(d) / "proc"
        shutil.copytree(FIX_PROC, proc, symlinks=True)
        prov = IntelProvider(sysfs_root=str(FIX_DRM), proc_root=str(proc))
        first = {p.pid: p for p in prov.processes()}
        assert set(first) == {4242}
        assert first[4242].mem_used == 1024
        assert first[4242].sm_pct is None
        assert prov.sample()[0].util_gpu is None
        # advance the rcs0 counters (500->2000 of 5000->20000 => 10 %)
        f3 = proc / "4242" / "fdinfo" / "3"
        f3.write_text(f3.read_text()
                      .replace("drm-cycles-rcs0:\t500", "drm-cycles-rcs0:\t2000")
                      .replace("drm-total-cycles-rcs0:\t5000",
                               "drm-total-cycles-rcs0:\t20000")
                      .replace("drm-memory-vram:\t1024", "drm-memory-vram:\t4096"))
        second = {p.pid: p for p in prov.processes()}
        assert abs(second[4242].sm_pct - 10.0) < 1e-9
        assert second[4242].mem_used == 4096
        # card-level util: summed rcs0 delta == same 10 %
        card_util = prov.sample()[0].util_gpu
        assert card_util is not None and abs(card_util - 10.0) < 1e-9
        assert prov.sample()[0].mem_used == 4096
        prov.close()
    print("  fdinfo deltas feed both process and card views")


def test_demo_four_cards():
    prov = DemoProvider()
    assert [d["index"] for d in prov.devices()] == [0, 1, 2, 3]
    samples = {s.index: s for s in prov.sample()}
    intel = samples[3]
    assert intel.vendor == "intel"
    assert intel.power_w is None, "demo mirrors the provider's honest gap"
    assert intel.fan_pct is None
    only = DemoProvider(only=[3])
    assert [s.index for s in only.sample()] == [3]
    print("  demo Intel card + --only")


if __name__ == "__main__":
    test_parse_freq_and_temp()
    test_discovery_drivers()
    test_provider_i915_fixture()
    test_provider_empty_fails_clean()
    test_fdinfo_card_and_process_deltas()
    test_demo_four_cards()
    print("intel provider: ALL PASS")
