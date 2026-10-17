"""Plug power metering: per-backend watt parsers, MockPlug scripting, and
the session-Wh integration used by the App readback cadence."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zionstats.plugs as plugs_mod
from zionstats.app import _integrate_plug_energy
from zionstats.plugs import MockPlug, Plug, ShellyPlug, TasmotaPlug, _kasa_watts


def _fake_http(payload):
    return lambda url, *a, **kw: payload


def test_plug_protocol_default():
    assert Plug().power() is None, "base protocol: no meter"
    print("  protocol default None")


def test_mockplug_scriptable():
    assert MockPlug().power() is None
    assert MockPlug(power_w=87.5).power() == 87.5
    print("  MockPlug power_w")


def test_shelly_gen1_and_gen2():
    s1 = ShellyPlug("g1", "http://shelly-1", gen=1, channel=0)
    orig = plugs_mod._http_json
    plugs_mod._http_json = _fake_http({"ison": True, "output": True, "power": 42.5})
    try:
        assert s1.power() == 42.5
    finally:
        plugs_mod._http_json = orig
    s2 = ShellyPlug("g2", "http://shelly-2", gen=2, channel=0)
    plugs_mod._http_json = _fake_http({"output": True, "power": 61.25})
    try:
        assert s2.power() == 61.25
    finally:
        plugs_mod._http_json = orig
    # no meter (e.g. Shelly PLUG S style relay without power key)
    plugs_mod._http_json = _fake_http({"output": True})
    try:
        assert ShellyPlug("g1nm", "http://x", gen=1).power() is None
    finally:
        plugs_mod._http_json = orig
    print("  Shelly Gen1/Gen2 watts")


def test_tasmota_status8_both_casings():
    t = TasmotaPlug("t", "http://tasmota")
    orig = plugs_mod._http_json
    upper = {"StatusSNS": {"Time": "x", "Energy": {"Power": 13.7}}}
    lower = {"StatusSNS": {"Time": "x", "energy": {"Power": 13.7}}}
    for payload in (upper, lower):
        plugs_mod._http_json = _fake_http(payload)
        try:
            assert t.power() == 13.7
        finally:
            plugs_mod._http_json = orig
    plugs_mod._http_json = _fake_http({"StatusSNS": {}})
    try:
        assert t.power() is None
    finally:
        plugs_mod._http_json = orig
    print("  Tasmota Status 8 (Energy/energy casings)")


def test_kasa_watts_shapes():
    assert _kasa_watts({"power_mw": 12345}) == 12.345
    assert _kasa_watts({"power": 15.5}) == 15.5
    assert _kasa_watts({}) is None
    assert _kasa_watts("garbage") is None
    print("  Kasa emeter shapes (power_mw / legacy power)")


def test_kasa_no_emeter():
    """A plug without a meter (HS103-class) returns None, not an error.
    (Built via __new__: the real ctor probe-imports python-kasa.)"""
    k = plugs_mod.KasaPlug.__new__(plugs_mod.KasaPlug)
    k.name = "k"
    k._host = "http://kasa"
    k._dev = None

    class NoMeterDev:
        emeter = None
        is_on = True

    k._connect = lambda: NoMeterDev()
    assert k.power() is None
    print("  Kasa no-emeter -> None")


def test_ha_power_attributes():
    h = plugs_mod.HAPlug("ha", "http://ha", "tok", "switch.blower")
    orig = plugs_mod._http_json
    for attrs in ({"power": 8.25}, {"current_power_w": 8.25}):
        plugs_mod._http_json = _fake_http(
            {"state": "on", "attributes": attrs})
        try:
            assert h.power() == 8.25
        finally:
            plugs_mod._http_json = orig
    plugs_mod._http_json = _fake_http({"state": "on", "attributes": {}})
    try:
        assert h.power() is None
    finally:
        plugs_mod._http_json = orig
    print("  HA power/current_power_w attributes")


def test_tuya_defensive_none():
    """Firmware without a recognizable metering DP stays None (never a
    guessed scale)."""

    class FakeTuyaDev:
        def status(self):
            return {"dps": {"1": True, "19": 1234}}  # 19 is energy, not watts

    t = plugs_mod.TuyaPlug.__new__(plugs_mod.TuyaPlug)
    t.name = "t"
    t._dev = FakeTuyaDev()
    assert t.power() is None
    t._dev = type("D2", (), {"status": lambda s: {"Error": "timeout"}})()
    assert t.power() is None
    print("  Tuya defensive None")


def test_integration_math():
    """Known watts over known dt -> exact Wh (rectangular, same fallback
    math as Insights); None readings integrate nothing but update 'last'."""
    energy, last = {}, {}
    _integrate_plug_energy(energy, last, None, 100.0, {"blower": 120.0})
    assert energy == {} and last == {"blower": 120.0}, "baseline only"
    _integrate_plug_energy(energy, last, 100.0, 160.0, {"blower": 120.0})
    assert abs(energy["blower"] - 120.0 * 60.0 / 3600.0) < 1e-9, "2.0 Wh"
    _integrate_plug_energy(energy, last, 160.0, 220.0, {"ghost": None})
    assert energy.get("ghost") is None and last.get("ghost") is None
    # dt clamped at 120 s (a stalled loop cannot inject phantom Wh)
    _integrate_plug_energy(energy, last, 0.0, 1000.0, {"blower": 100.0})
    assert abs(energy["blower"] - (2.0 + 100.0 * 120.0 / 3600.0)) < 1e-9
    print("  session Wh integration")


if __name__ == "__main__":
    test_plug_protocol_default()
    test_mockplug_scriptable()
    test_shelly_gen1_and_gen2()
    test_tasmota_status8_both_casings()
    test_kasa_watts_shapes()
    test_kasa_no_emeter()
    test_ha_power_attributes()
    test_tuya_defensive_none()
    test_integration_math()
    print("plug power: ALL PASS")
