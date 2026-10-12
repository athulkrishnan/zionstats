import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.climate import Weather, WeatherProvider, wet_bulb_c

FIXTURE = Path(__file__).parent / "fixtures" / "openmeteo_demo.json"


def test_wet_bulb_physics():
    # psychrometric-chart reference points (tolerance covers chart reading)
    assert 25.0 <= wet_bulb_c(30.0, 70.0) <= 26.5   # chart ~25.8
    assert 30.5 <= wet_bulb_c(35.0, 80.0) <= 32.5   # chart ~31.8
    assert 21.0 <= wet_bulb_c(40.0, 20.0) <= 23.5   # hot-dry, chart ~21.5-22.7
    # invariants: dew point <= wet bulb <= dry bulb
    import math

    for t in (10, 20, 29.3, 33, 40):
        for rh in (15, 50, 76, 95):
            tw = wet_bulb_c(t, rh)
            assert tw <= t + 1e-6, "wet bulb can never exceed dry bulb"
            a = math.log(rh / 100.0) + 17.62 * t / (243.12 + t)
            td = 243.12 * a / (17.62 - a)
            assert tw >= td - 1.0, "wet bulb can never sit below dew point"


def test_fixture_parse(tmp_path=None):
    data = json.loads(FIXTURE.read_text())
    cur = data["current"]
    w = Weather(
        ts=12345.0, lat=data["latitude"], lon=data["longitude"],
        temp_c=cur["temperature_2m"], rh=cur["relative_humidity_2m"],
        dew_c=cur["dew_point_2m"], apparent_c=cur["apparent_temperature"],
        precip_mm=cur["precipitation"], code=cur["weather_code"],
        wind_kmh=cur["wind_speed_10m"], is_day=bool(cur["is_day"]),
        hourly=[(0.0, t) for t in data["hourly"]["temperature_2m"][:24]],
    )
    assert w.temp_c == 29.3
    assert 0 < w.rh <= 100
    assert w.condition() == "Drizzle" or w.condition() != ""
    assert len(w.hourly) == 24
    assert w.dew_c <= w.wet_bulb_c() <= w.temp_c + 0.5
    eff = w.effectiveness(85.0)
    assert 0.0 <= eff <= 1.0 and eff > 0.5   # 85C GPU vs 29C ambient = useful


def test_effectiveness_and_advisories():
    hot_humid = Weather(ts=0, lat=0, lon=0, temp_c=31.0, rh=90.0, dew_c=29.0, code=61)
    cool_dry = Weather(ts=0, lat=0, lon=0, temp_c=12.0, rh=30.0, dew_c=2.0, code=1)
    assert hot_humid.effectiveness(85) < cool_dry.effectiveness(85)
    adv = hot_humid.advisories(83.0)
    assert any("wet-bulb" in m for _, m in adv)     # 29C+ wet bulb muggy warning
    cold_gpu = Weather(ts=0, lat=0, lon=0, temp_c=28.0, rh=90.0, dew_c=26.5, code=1)
    adv2 = cold_gpu.advisories(24.0)                 # idle card below dew point
    assert any("condensation" in m for _, m in adv2)


def test_cache_roundtrip(tmp_path):
    import time as _t

    cp = Path(tmp_path) / "weather.json"
    prov = WeatherProvider(1.3521, 103.8198, "Singapore", cache_path=cp)
    w = Weather(ts=_t.time(), lat=1.3521, lon=103.8198, label="Singapore",
                temp_c=29.3, rh=76.0, dew_c=25.0, apparent_c=33.0,
                precip_mm=0.1, code=51, wind_kmh=7.0, is_day=False,
                hourly=[(0.0, 29.0)])
    prov.last = w
    prov._write_cache(w)
    prov2 = WeatherProvider(1.3521, 103.8198, "Singapore", cache_path=cp)
    got = prov2._read_cache()
    assert got is not None and abs(got.temp_c - 29.3) < 1e-6
    assert got.rh == 76.0 and got.code == 51


def test_demo_weather():
    from zionstats.climate import DemoWeather

    dw = DemoWeather()
    w = dw.refresh(force=True)
    assert 24 <= w.temp_c <= 36
    assert 55 <= w.rh <= 98
    assert len(w.hourly) == 24
    assert w.condition() != ""


if __name__ == "__main__":
    test_wet_bulb_physics()
    test_fixture_parse()
    test_effectiveness_and_advisories()
    test_cache_roundtrip(__import__("tempfile").mkdtemp())
    test_demo_weather()
    print("climate tests: ALL PASS")
