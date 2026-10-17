"""nvml_layer helpers: throttle decoding, field-value union extraction,
arch table, /proc CPU delta.  All pure -- no NVML, no GPU."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.nvml_layer import (
    _arch_name,
    _field_value,
    _last_cpu,
    decode_throttle,
)


def test_decode_throttle_individual_bits():
    nv = object()  # decode_throttle takes nv but only uses the static table
    assert decode_throttle(nv, 0x0) == []
    assert decode_throttle(nv, 0x1) == ["GPU-Idle"]
    assert decode_throttle(nv, 0x4) == ["SW-Power-Cap"]
    assert decode_throttle(nv, 0x8) == ["HW-Slowdown"]
    assert decode_throttle(nv, 0x40) == ["HW-Thermal"]
    assert decode_throttle(nv, 0x80) == ["HW-Brake"]
    assert decode_throttle(nv, 0x100) == ["Display-Clocks"]
    print("  individual bits")


def test_decode_throttle_combos():
    nv = object()
    got = decode_throttle(nv, 0x1 | 0x4 | 0x8)  # idle + SW power cap + HW slowdown
    assert got == ["GPU-Idle", "SW-Power-Cap", "HW-Slowdown"], got
    assert "HW-Slowdown" in decode_throttle(nv, 0xFF)
    assert len(decode_throttle(nv, 0x1FF)) == 9
    print("  combos")


def test_decode_throttle_unknown_bits_visible():
    """The mask is open-ended: a set bit with no label must surface as
    Bit-0xN instead of hiding."""
    got = decode_throttle(object(), 0x400)
    assert got == ["Bit-0x400"], got
    got = decode_throttle(object(), 0x1 | 0x400 | 0x8000)
    assert got == ["GPU-Idle", "Bit-0x400", "Bit-0x8000"], got
    print("  unknown bits decode visibly")


def test_decode_throttle_binding_resolved_extras():
    """v615 reasons decode only when the installed binding exports their
    constants; the numeric value comes from the binding, not from memory."""

    class FakeBindingNew:
        NVML_CLOCKS_EVENT_REASON_BOARD_LIMIT = 0x200

    assert decode_throttle(FakeBindingNew(), 0x200) == ["Board-Limit"]
    # a binding without the constant leaves the bit unknown-but-visible
    assert decode_throttle(object(), 0x200) == ["Bit-0x200"]
    # duplicate constant under an alternate spelling contributes nothing new
    class FakeBindingBoth:
        NVML_CLOCKS_EVENT_REASON_BOARD_LIMIT = 0x200
        NVML_CLOCKS_EVENT_REASON_BOARD_LIMIT_EPD = 0x200

    assert decode_throttle(FakeBindingBoth(), 0x200) == ["Board-Limit"]
    print("  binding-resolved extra reasons")


def test_sample_new_fields_default():
    from zionstats.nvml_layer import Sample

    s = Sample(ts=1.0, index=0)
    assert s.vendor == "nvidia"
    assert s.temp_gpu_max is None
    assert s.fan_rpm is None
    assert s.pcie_replay is None
    assert s.throttle_counters is None
    print("  sample defaults")


def test_field_value_value_types():
    # valueType -> member: 1 dVal, 2 uiVal, 3 ulVal, 4 ullVal, 5 lVal
    for vt, attr, val in ((1, "dVal", 42.5), (2, "uiVal", 7), (3, "ulVal", 9),
                          (4, "ullVal", 123), (5, "lVal", -5)):
        fv = SimpleNamespace(nvmlReturn=0, valueType=vt, **{attr: val})
        fv.value = SimpleNamespace(**{attr: val})
        assert _field_value(fv) == float(val), f"vt={vt}"
    print("  value types")


def test_field_value_failure_and_garbage():
    assert _field_value(SimpleNamespace(nvmlReturn=1, valueType=1, dVal=1.0)) is None
    assert _field_value(SimpleNamespace(nvmlReturn=0, valueType=0)) is None  # unknown vt
    assert _field_value(SimpleNamespace()) is None  # bare object
    # union fallback when top-level member is absent
    fv = SimpleNamespace(nvmlReturn=0, valueType=1, value=SimpleNamespace(dVal=3.25))
    assert _field_value(fv) == 3.25
    # non-numeric member
    fv2 = SimpleNamespace(nvmlReturn=0, valueType=2, uiVal="x")
    assert _field_value(fv2) is None
    print("  failure/garbage handling")


def test_arch_name():
    assert _arch_name(8, 9) == "Ada"
    assert _arch_name(9, 0) == "Hopper"
    assert _arch_name(12, 0) == "Blackwell"
    assert _arch_name(7, 5) == "Turing"
    assert _arch_name(3, 5) == "Kepler"
    assert _arch_name(99, 0) == "CC 99.0"
    assert _arch_name(0, 0) == "?"
    print("  arch table")


def test_pid_cpu_delta(monkey_patch_home=None):
    """_pid_cpu_delta parses /proc/<pid>/stat fields 14/15 (utime+stime).

    Drives two deltas synthetically by interposing on builtins.open."""
    import builtins
    import io

    import zionstats.nvml_layer as nl

    _last_cpu.clear()
    stat_line = "184722 (python) S 1 1 1 0 -1 4194560 0 0 0 0 150 50 0 0 20 5 0 0 1234 1"
    real_open = builtins.open
    holder = {"line": stat_line}

    def fake_open(path, *a, **kw):
        if str(path) == "/proc/184722/stat":
            return io.BytesIO(holder["line"].encode())
        return real_open(path, *a, **kw)

    builtins.open = fake_open
    try:
        first = nl._pid_cpu_delta(184722, 100.0)
        assert first is None, "first observation returns None (no baseline)"
        # advance cpu totals by (150+50)=200 -> (250+50)=300 ticks, 2 s wall
        holder["line"] = stat_line.replace(" 150 50 ", " 250 50 ")
        second = nl._pid_cpu_delta(184722, 102.0)
        # 100 ticks / 100 ticks-per-sec / 2 s = 50 %
        assert second is not None and abs(second - 50.0) < 1e-6, second
    finally:
        builtins.open = real_open
        _last_cpu.clear()
    print("  /proc stat cpu delta")


def test_pid_cpu_delta_garbled_stat():
    import builtins
    import io

    import zionstats.nvml_layer as nl

    _last_cpu.clear()
    real_open = builtins.open
    builtins.open = lambda p, *a, **kw: (
        io.BytesIO(b"garbage") if str(p).endswith("/stat") else real_open(p, *a, **kw))
    try:
        assert nl._pid_cpu_delta(999999, 100.0) is None
    finally:
        builtins.open = real_open
        _last_cpu.clear()
    print("  garbled stat tolerated")


if __name__ == "__main__":
    test_decode_throttle_individual_bits()
    test_decode_throttle_combos()
    test_decode_throttle_unknown_bits_visible()
    test_decode_throttle_binding_resolved_extras()
    test_sample_new_fields_default()
    test_field_value_value_types()
    test_field_value_failure_and_garbage()
    test_arch_name()
    test_pid_cpu_delta()
    test_pid_cpu_delta_garbled_stat()
    print("nvml_layer helpers: ALL PASS")
