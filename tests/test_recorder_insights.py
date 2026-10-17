"""Recorder (ndjson/.gz, replay) and Insights (energy, verdicts, alerts)."""
import sys
import tempfile
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.insights import Insights
from zionstats.nvml_layer import DemoProvider, GpuProcess, Sample
from zionstats.recorder import Recorder, ReplayProvider, frame_to_dict


def mk_sample(**kw) -> Sample:
    base = dict(ts=1000.0, index=0, util_gpu=90.0, util_mem=40.0, mem_used=1 << 30,
                mem_total=16 << 30, power_w=100.0, power_limit_w=300.0,
                clocks_sm_mhz=2000, clocks_max_sm_mhz=2500, temp_gpu=60.0,
                temp_slowdown=90, pcie_gen_max=4, pcie_width_max=16)
    base.update(kw)
    return Sample(**base)


# ----------------------------------------------------------------- recorder

def test_recorder_ndjson_roundtrip():
    d = Path(tempfile.mkdtemp())
    p = d / "trace.ndjson"
    env = {"hostname": "h", "provider": "demo"}
    s = mk_sample()
    proc = GpuProcess(1, 0, "C", 100)
    rec = Recorder(p)
    rec.write(env, [s], [proc])
    rec.write(env, [s], [])
    rec.close()
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    import json
    f = json.loads(lines[0])
    assert f["env"] == env
    assert f["devices"][0]["index"] == 0
    assert f["processes"][0]["pid"] == 1
    print("  ndjson roundtrip")


def test_recorder_gzip_roundtrip():
    import gzip as gz
    import json
    d = Path(tempfile.mkdtemp())
    p = d / "trace.ndjson.gz"
    rec = Recorder(p)
    rec.write({"provider": "demo"}, [mk_sample()], [])
    rec.close()
    with gz.open(p, "rt", encoding="utf-8") as fh:
        f = json.loads(fh.readline())
    assert f["devices"][0]["index"] == 0
    print("  gzip roundtrip")


def test_replay_provider_exhaustion():
    d = Path(tempfile.mkdtemp())
    p = d / "t.ndjson"
    rec = Recorder(p)
    for i in range(3):
        rec.write({"provider": "demo"}, [mk_sample(ts=100.0 + i, util_gpu=10.0 * i)], [])
    rec.close()
    rp = ReplayProvider(p)
    assert rp.environment()["provider"] == "replay"
    assert [d_["index"] for d_ in rp.devices()] == [0]
    seen = [rp.sample()[0].util_gpu for _ in range(5)]
    assert seen == [0.0, 10.0, 20.0, 20.0, 20.0], "must hold last frame after exhaustion"
    assert rp.exhausted is True
    rp.close()
    print("  replay exhaustion")


def test_frame_to_dict_shape():
    f = frame_to_dict({"a": 1}, [mk_sample()], [GpuProcess(7, 0)])
    assert set(f) == {"ts", "env", "devices", "processes"}
    assert f["processes"][0]["pid"] == 7
    print("  frame shape")


# ----------------------------------------------------------------- insights

def test_energy_from_mj_counter_delta():
    eng = Insights()
    s1 = mk_sample(energy_mj=1000)
    s2 = mk_sample(ts=1001.0, energy_mj=1_000 + 3600_000)  # +3600 kJ = 1 Wh
    eng.update(s1, None)
    ins = eng.update(s2, None)
    assert abs(ins.session_wh - 1.0) < 1e-6
    print("  energy mJ delta")


def test_energy_falls_back_to_power_integration():
    eng = Insights()
    eng.update(mk_sample(ts=1000.0, power_w=360.0, energy_mj=None), None)
    ins = eng.update(mk_sample(ts=1001.0, power_w=360.0, energy_mj=None), None)  # dt 1s
    assert abs(ins.session_wh - 0.1) < 1e-6  # 360 W * 1 s = 0.1 Wh
    print("  energy power-integration fallback")


def test_energy_ignores_counter_reset():
    eng = Insights()
    eng.update(mk_sample(energy_mj=5_000_000), None)
    ins = eng.update(mk_sample(ts=1001.0, energy_mj=10), None)  # driver reset -> 0 delta
    assert ins.session_wh == 0.0
    print("  energy counter-reset guard")


def test_power_stats_and_idle_leak():
    eng = Insights()
    eng.update(mk_sample(util_gpu=0.0, power_w=20.0), None)
    eng.update(mk_sample(ts=1001.0, util_gpu=95.0, power_w=300.0), None)
    ins = eng.update(mk_sample(ts=1002.0, util_gpu=90.0, power_w=280.0), None)
    assert ins.avg_w == (20.0 + 300.0 + 280.0) / 3
    assert ins.peak_w == 300.0
    assert ins.idle_leak_w == 20.0
    print("  power stats")


def test_clock_deficit_only_under_load():
    eng = Insights()
    ins = eng.update(mk_sample(util_gpu=10.0, clocks_sm_mhz=300, clocks_max_sm_mhz=2500), None)
    assert ins.clock_deficit_mhz == 0
    ins = eng.update(mk_sample(util_gpu=90.0, clocks_sm_mhz=300, clocks_max_sm_mhz=2500), None)
    assert ins.clock_deficit_mhz == 2200
    print("  clock deficit gating")


def test_thermal_rate_sign():
    eng = Insights()
    h: deque = deque(maxlen=32)
    h.append((1000.0, 40.0))
    h.append((1020.0, 44.0))
    h.append((1040.0, 48.0))
    h.append((1060.0, 52.0))  # +12 C / 60 s = 12 C/min
    ins = eng.update(mk_sample(), h)
    assert abs(ins.thermal_rate_c_per_min - 12.0) < 1e-6
    ins = eng.update(mk_sample(), deque(maxlen=32))  # too short -> no rate
    assert ins.thermal_rate_c_per_min == 0.0
    print("  thermal rate")


def test_verdict_priority_table():
    cases = [
        (dict(util_gpu=3.0), "IDLE"),
        (dict(util_gpu=90.0, throttle=["HW-Thermal"]), "THERMALLY-LIMITED"),
        (dict(util_gpu=90.0, throttle=["HW-Slowdown"]), "THERMALLY-LIMITED"),
        (dict(util_gpu=90.0, throttle=["HW-Brake"]), "THERMALLY-LIMITED"),
        (dict(util_gpu=90.0, throttle=["SW-Thermal"]), "THERMALLY-LIMITED"),
        (dict(util_gpu=90.0, throttle=["SW-Power-Cap"]), "POWER-LIMITED"),
        (dict(util_gpu=60.0, util_mem=95.0), "MEMORY-BOUND"),
        (dict(util_gpu=85.0, util_mem=40.0), "COMPUTE-BOUND"),
        (dict(util_gpu=40.0, util_mem=30.0), "ACTIVE"),
    ]
    for kw, want in cases:
        ins = Insights().update(mk_sample(**kw), None)
        assert ins.verdict == want, f"{kw}: got {ins.verdict}, want {want}"
    # HW beats SW beats power-cap regardless of order
    ins = Insights().update(mk_sample(util_gpu=90.0, throttle=["SW-Thermal", "HW-Brake"]), None)
    assert ins.verdict == "THERMALLY-LIMITED"
    print("  verdict priority")


def test_alerts_edge_triggered():
    eng = Insights()
    eng.update(mk_sample(util_gpu=95.0, throttle=[]), None)
    n0 = len(eng.alerts)
    eng.update(mk_sample(ts=1001.0, util_gpu=95.0, throttle=["SW-Power-Cap"]), None)
    n1 = len(eng.alerts)
    assert n1 == n0 + 1, "first engagement must alert"
    eng.update(mk_sample(ts=1002.0, util_gpu=95.0, throttle=["SW-Power-Cap"]), None)
    assert len(eng.alerts) == n1, "sustained state must not re-alert"
    eng.update(mk_sample(ts=1003.0, util_gpu=95.0, throttle=[]), None)
    eng.update(mk_sample(ts=1004.0, util_gpu=95.0, throttle=["SW-Power-Cap"]), None)
    assert len(eng.alerts) == n1 + 1, "re-engage alerts again (SW clear is silent)"
    print("  alert edges")


def test_mem_alert_hysteresis():
    eng = Insights()
    eng.update(mk_sample(mem_used=int(0.93 * (16 << 30)), mem_total=16 << 30, util_gpu=50.0), None)
    fired = any("memory above" in a.msg for a in eng.alerts)
    assert fired, "93% must fire the memory alert"
    eng.update(mk_sample(ts=1001.0, mem_used=int(0.93 * (16 << 30)), util_gpu=50.0), None)
    n = len(eng.alerts)
    eng.update(mk_sample(ts=1002.0, mem_used=int(0.93 * (16 << 30)), util_gpu=50.0), None)
    assert len(eng.alerts) == n, "no re-fire while above threshold"
    print("  mem hysteresis")


def test_ecc_dbe_alert():
    eng = Insights()
    eng.update(mk_sample(ecc_dbe=0), None)
    eng.update(mk_sample(ts=1001.0, ecc_dbe=2), None)
    assert any("uncorrected ECC" in a.msg for a in eng.alerts)
    print("  ECC alert")


def test_demo_provider_smoke():
    """DemoProvider must satisfy the provider contract GPU-free."""
    p = DemoProvider()
    env = p.environment()
    assert env["provider"] == "demo"
    assert len(p.devices()) == 3
    samples = p.sample()
    assert all(isinstance(s, Sample) for s in samples)
    procs = p.processes()
    assert all(isinstance(pr, GpuProcess) for pr in procs)
    p.close()
    print("  demo provider contract")


if __name__ == "__main__":
    test_recorder_ndjson_roundtrip()
    test_recorder_gzip_roundtrip()
    test_replay_provider_exhaustion()
    test_frame_to_dict_shape()
    test_energy_from_mj_counter_delta()
    test_energy_falls_back_to_power_integration()
    test_energy_ignores_counter_reset()
    test_power_stats_and_idle_leak()
    test_clock_deficit_only_under_load()
    test_thermal_rate_sign()
    test_verdict_priority_table()
    test_alerts_edge_triggered()
    test_mem_alert_hysteresis()
    test_ecc_dbe_alert()
    test_demo_provider_smoke()
    print("recorder + insights: ALL PASS")
