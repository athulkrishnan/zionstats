"""Exporter: pure metric rendering (golden strings), spec parsing, and a
best-effort live loopback test (skipped where bind is not allowed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.exporter import (
    Payload,
    parse_export_spec,
    render_metrics,
    render_snapshot,
)
from zionstats.nvml_layer import DemoProvider, GpuProcess, Sample


def mk_sample(**kw) -> Sample:
    base = dict(ts=1000.0, index=0, uuid="GPU-test", name='Weird "GPU" \\name',
                util_gpu=42.0, util_mem=30.0, mem_used=2 << 30, mem_total=16 << 30,
                power_w=120.0, power_limit_w=300.0, temp_gpu=55, temp_mem=50,
                clocks_sm_mhz=1800, clocks_mem_mhz=9000, fan_pct=40,
                pcie_gen_cur=4, pcie_width_cur=16, pcie_rx_kbps=1000, pcie_tx_kbps=500,
                throttle_bits=4, energy_mj=3_600_000, ecc_sbe=1, ecc_dbe=0)
    base.update(kw)
    return Sample(**base)


def test_golden_new_field_series():
    """v0.6 tops-ups: hottest-part temp (new sensor label), fan RPM, PCIe
    replay counter.  Absent fields must omit the series (never NaN)."""
    s = mk_sample(temp_gpu_max=88, fan_rpm=2150, pcie_replay=7)
    text = render_metrics(Payload(env={}, samples=[s], procs=[], insights={}))
    assert ('zionstats_gpu_temperature_celsius{gpu="0",name="Weird \\"GPU\\" \\\\name",'
            'uuid="GPU-test",sensor="hottest"} 88.0') in text
    assert ('zionstats_gpu_fan_rpm{gpu="0",name="Weird \\"GPU\\" \\\\name",'
            'uuid="GPU-test"} 2150.0') in text
    assert ('zionstats_gpu_pcie_replay_counter_total{gpu="0",name="Weird \\"GPU\\" \\\\name",'
            'uuid="GPU-test"} 7.0') in text
    # absent -> series omitted entirely
    s2 = mk_sample()
    text2 = render_metrics(Payload(env={}, samples=[s2], procs=[], insights={}))
    assert 'sensor="hottest"' not in text2
    assert "gpu_fan_rpm" not in text2
    assert "gpu_pcie_replay_counter_total" not in text2
    print("  golden new-field series (hottest/rpm/replay)")


def test_golden_full_sample():
    p = Payload(env={}, samples=[mk_sample()], procs=[],
                insights={}, version="0.4.0")
    text = render_metrics(p)
    lines = text.splitlines()
    assert lines[0] == "# HELP zionstats_gpu_utilization_percent GPU utilization (%)"
    assert lines[1] == "# TYPE zionstats_gpu_utilization_percent gauge"
    assert ('zionstats_gpu_utilization_percent{gpu="0",name="Weird \\"GPU\\" '
            '\\\\name",uuid="GPU-test"} 42.0') in lines
    assert 'zionstats_gpu_power_watts{gpu="0",name="Weird \\"GPU\\" \\\\name",uuid="GPU-test"} 120.0' in lines
    assert 'sensor="gpu"} 55.0' in text
    assert 'sensor="memory"} 50.0' in text
    assert 'zionstats_gpu_pcie_rx_bytes_per_second{gpu="0"' in text
    assert "} 1000000.0" in text, "KB/s must scale to bytes/s"
    assert 'zionstats_gpu_energy_consumption_joules_total{gpu="0"' in text
    assert "} 3600.0" in text, "mJ must scale to J"
    assert 'type="corrected"} 1.0' in text and 'type="uncorrected"} 0.0' in text
    assert 'zionstats_build_info{version="0.4.0"} 1' in text
    # every family header appears exactly once (no duplicate HELP/TYPE)
    helps = [ln for ln in lines if ln.startswith("# HELP")]
    assert len(helps) == len(set(helps)), "duplicate family headers are invalid"
    print("  golden full sample")


def test_none_fields_omit_series_never_nan():
    s = mk_sample(util_gpu=None, power_w=None, energy_mj=None,
                  clocks_sm_mhz=None, temp_gpu=None)
    text = render_metrics(Payload(env={}, samples=[s], procs=[], insights={}))
    assert "gpu_utilization_percent" not in text
    assert "gpu_power_watts" not in text
    assert "nan" not in text.lower(), "NaN must never be emitted"
    assert "gpu_memory_used_bytes" in text, "present fields still render"
    print("  None fields omitted, no NaN")


def test_insights_and_process_series():
    from zionstats.insights import DeviceInsight

    ins = DeviceInsight(session_wh=1.5, avg_w=100.0, peak_w=250.0, pcie_sat=0.25)
    proc = GpuProcess(184722, 0, "C", 13 << 30, 97.0, 84.0, 0.0, 0.0,
                      12.0, "demo", 'python "train".py\nx')
    text = render_metrics(Payload(env={}, samples=[mk_sample()], procs=[proc],
                                  insights={0: ins}, version="0.4.0"))
    assert 'zionstats_gpu_session_energy_watt_hour{gpu="0"' in text
    assert "} 1.5" in text
    assert 'zionstats_process_memory_bytes{gpu="0",pid="184722",user="demo"' in text
    assert 'command="python \\"train\\".py\\nx"' in text, "label escaping"
    assert 'kind="sm"} 97.0' in text and 'kind="enc"} 0.0' in text
    # gate off
    text2 = render_metrics(Payload(env={}, samples=[mk_sample()], procs=[proc],
                                   insights={0: ins}, expose_processes=False))
    assert "zionstats_process_" not in text2
    print("  insights + process series + gate")


def test_empty_payload():
    text = render_metrics(Payload(env={}, samples=[], procs=[], insights={},
                                  version="9.9.9"))
    assert text == 'zionstats_build_info{version="9.9.9"} 1\n'
    print("  empty payload")


def test_parse_export_spec():
    assert parse_export_spec("9835") == ("127.0.0.1", 9835)
    assert parse_export_spec("127.0.0.1:9835") == ("127.0.0.1", 9835)
    assert parse_export_spec("0.0.0.0:9") == ("0.0.0.0", 9)
    for bad in ("", ":", "host:", ":123", "1.2.3.4:0", "1.2.3.4:99999",
                "1.2.3.4:http"):
        try:
            parse_export_spec(bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, f"{bad!r} must raise"
    print("  spec parsing")


def test_render_snapshot_shape():
    dp = DemoProvider()
    p = Payload(env=dp.environment(), samples=dp.sample(), procs=dp.processes(),
                insights={})
    import json

    f = json.loads(render_snapshot(p))
    assert set(f) >= {"ts", "env", "devices", "processes"}
    print("  snapshot shape")


def test_live_loopback():
    """Real socket round-trip on an OS-picked port; skipped when bind is
    denied (sandboxed CI runners)."""
    from zionstats.exporter import Exporter

    ex = Exporter("127.0.0.1", 0, version="t", expose_processes=True)
    if not ex.start():
        print("  (skipped: bind denied)")
        return
    try:
        dp = DemoProvider()
        ex.publish(dp.environment(), dp.sample(), dp.processes(), {})
        import urllib.request

        port0 = ex._httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port0}/metrics", timeout=3) as r:
            body = r.read().decode()
            assert r.headers["Content-Type"].startswith("text/plain")
        assert "zionstats_gpu_utilization_percent" in body
        port = ex._httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot", timeout=3) as r:
            snap = r.read()
        assert b'"devices"' in snap
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/bogus", timeout=3)
            code = 200
        except Exception as e:
            code = getattr(e, "code", 0)
        assert code == 404
    finally:
        ex.stop()
    print("  live loopback /metrics /snapshot 404")


def test_live_loopback_token_auth():
    """F3: with [export] token set, /metrics and /snapshot require
    Authorization: Bearer; wrong/missing header -> 401."""
    from zionstats.exporter import Exporter

    ex = Exporter("127.0.0.1", 0, version="t", expose_processes=True,
                  token="s3cret")
    if not ex.start():
        print("  (skipped: bind denied)")
        return
    try:
        dp = DemoProvider()
        ex.publish(dp.environment(), dp.sample(), dp.processes(), {})
        import urllib.error
        import urllib.request

        port = ex._httpd.server_address[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 401, "missing Authorization header must be 401"
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/metrics",
                headers={"Authorization": "Bearer wrong"})
            urllib.request.urlopen(req, timeout=3)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 401, "wrong token must be 401"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/metrics",
            headers={"Authorization": "Bearer s3cret"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read().decode()
            assert r.status == 200
        assert "zionstats_gpu_utilization_percent" in body
        # snapshot is gated like /metrics
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot", timeout=3)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 401, "snapshot must be gated too"
    finally:
        ex.stop()
    print("  live loopback bearer auth 401/200")


if __name__ == "__main__":
    test_golden_full_sample()
    test_golden_new_field_series()
    test_none_fields_omit_series_never_nan()
    test_insights_and_process_series()
    test_empty_payload()
    test_parse_export_spec()
    test_render_snapshot_shape()
    test_live_loopback()
    test_live_loopback_token_auth()
    print("exporter: ALL PASS")
