import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.climate import Weather
from zionstats.cooling import CoolingConfig, CoolingController
from zionstats.nvml_layer import Sample
from zionstats.plugs import MockPlug


def make(sample_ts=1000.0, temp=85.0, util=40.0, trigger="temp"):
    plug = MockPlug("blower")
    cfg = CoolingConfig(mode="auto", verify_delay_s=0.0, trigger=trigger)
    clock = {"t": sample_ts + 1.0}

    def now():
        return clock["t"]

    def sleep(s):
        clock["t"] += s

    ctl = CoolingController([plug], cfg, clock=now, sleep=sleep)
    weather = Weather(ts=clock["t"], lat=0, lon=0, temp_c=30.0, rh=70.0,
                      dew_c=25.0, code=1)
    sample = Sample(ts=sample_ts, index=0, temp_gpu=temp, util_gpu=util)
    return plug, ctl, clock, weather, sample


def test_off_mode_never_actuates():
    plug, ctl, clock, weather, sample = make()
    ctl.cfg.mode = "off"
    ctl.evaluate(sample, weather)
    assert plug.commands == [] and not plug.state


def test_trigger_and_hysteresis():
    plug, ctl, clock, weather, sample = make(temp=85.0)
    ctl.evaluate(sample, weather)                       # 85 >= 82, delta 55 -> ON
    assert plug.state is True and ctl.state.on
    # stay on while above resume
    clock["t"] += 200
    ctl.evaluate(replace(sample, ts=clock["t"], temp_gpu=80.0), weather)
    assert plug.state is True
    # below resume -> off
    clock["t"] += 200
    ctl.evaluate(replace(sample, ts=clock["t"], temp_gpu=77.0), weather)
    assert plug.state is False and ctl.state.on is False


def test_min_delta_blocks_useless_blowing():
    plug, ctl, clock, weather, sample = make(temp=84.0)
    weather.temp_c = 82.0                               # delta 2C < 8C
    ctl.evaluate(sample, weather)
    assert plug.state is False and ctl.state.on is False
    assert "delta" in ctl.state.reason


def test_min_off_time_holds():
    plug, ctl, clock, weather, sample = make(temp=85.0)
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 10
    ctl.evaluate(replace(sample, temp_gpu=70.0), weather)   # wants off, min_on=120s
    assert plug.state is True and "[min-on hold]" in ctl.state.reason
    clock["t"] += 200
    ctl.evaluate(replace(sample, ts=clock["t"], temp_gpu=70.0), weather)   # off allowed now
    assert plug.state is False


def test_stale_sample_failsafe():
    plug, ctl, clock, weather, sample = make(sample_ts=1000.0, temp=85.0)
    ctl.evaluate(sample, weather)                       # ON
    assert plug.state is True
    clock["t"] += 60                                    # sample now 61s old (>15s)
    ctl.evaluate(replace(sample, temp_gpu=85.0), weather)
    assert plug.state is False
    assert ctl.state.inhibited == "GPU samples stale"
    kinds = [e.kind for e in ctl.events]
    assert "inhibit" in kinds


def test_stale_weather_inhibits_auto():
    plug, ctl, clock, weather, sample = make(temp=85.0)
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 60 * 60                               # weather 1h old
    ctl.evaluate(replace(sample, ts=clock["t"], temp_gpu=85.0), weather)
    assert plug.state is False and ctl.state.inhibited == "weather data stale"


def test_duty_cap():
    plug, ctl, clock, weather, sample = make(temp=85.0)
    ctl.cfg.max_duty_min = 0.05                         # 3s cap
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 5
    ctl.evaluate(replace(sample, temp_gpu=85.0), weather)
    assert plug.state is False
    assert any(e.kind == "duty" for e in ctl.events)


def test_dry_run_never_actuates_but_decides():
    plug, ctl, clock, weather, sample = make(temp=85.0)
    ctl.cfg.mode = "dry_run"
    ctl.evaluate(sample, weather)
    assert plug.commands == [] and plug.state is False
    assert ctl.state.would_on is True
    assert any(e.kind == "would-on" for e in ctl.events)
    # no duplicate spam on the same decision
    clock["t"] += 5
    ctl.evaluate(replace(sample, temp_gpu=85.0), weather)
    assert sum(1 for e in ctl.events if e.kind == "would-on") == 1


def test_manual_immediate_and_failsafe():
    plug, ctl, clock, weather, sample = make(temp=40.0)
    ctl.cfg.mode = "manual"
    ctl.set_manual(True)
    assert plug.state is True                           # immediate, no hysteresis
    ctl.set_manual(False)
    assert plug.state is False
    ctl.set_manual(True)
    clock["t"] += 60
    ctl.evaluate(replace(sample, temp_gpu=40.0), weather)   # stale sample -> force off
    assert plug.state is False


def test_retry_then_fail_alert():
    plug, ctl, clock, weather, sample = make(temp=85.0)
    plug.fail_next = 3                                  # all 3 attempts fail
    ctl.evaluate(sample, weather)
    assert plug.state is False
    assert any(e.kind == "fail" for e in ctl.events)
    assert len(plug.commands) == 3                      # retried exactly 3x
    # recovers on a later healthy cycle
    plug.fail_next = 0
    clock["t"] += 200
    ctl.evaluate(replace(sample, ts=clock["t"], temp_gpu=85.0), weather)
    assert plug.state is True


def test_readback_mismatch_retries():
    plug, ctl, clock, weather, sample = make(temp=85.0)

    class LyingPlug(MockPlug):
        def status(self):
            return False                                # claims off after on()

    lp = LyingPlug("liar", fail_next=2)                 # 2 failures -> 3rd ok
    ctl.plugs = [lp]
    ctl.evaluate(sample, weather)
    assert lp.state is True                             # 3rd attempt verified
    assert len(lp.commands) == 3


def test_ambient_fallback_without_weather():
    plug, ctl, clock, weather, sample = make(temp=85.0)
    ctl.cfg.mode = "auto"
    ctl.evaluate(replace(sample, temp_gpu=85.0), None)
    assert ctl.state.on is True                         # fallback ambient 30C, delta 55
    st2 = ctl.evaluate(replace(sample, ts=clock["t"], temp_gpu=33.0), None)  # delta 3C
    assert st2.would_on is False or "delta" in st2.reason


# ---- usage trigger -------------------------------------------------------


def test_usage_turns_on_at_threshold():
    plug, ctl, clock, weather, sample = make(temp=45.0, util=40.0, trigger="usage")
    st = ctl.evaluate(sample, weather)
    assert plug.state is True and ctl.state.on
    assert "usage" in st.reason


def test_usage_below_threshold_stays_off():
    plug, ctl, clock, weather, sample = make(temp=45.0, util=2.0, trigger="usage")
    st = ctl.evaluate(sample, weather)
    assert plug.commands == [] and ctl.state.on is False
    assert "idle" in st.reason


def test_usage_off_requires_idle_and_cool():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=40.0, trigger="usage")
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 300
    st = ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=50.0), weather)
    assert plug.state is True and "off in" in st.reason  # idle 0s < 600s
    clock["t"] += 601
    st = ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=50.0), weather)
    assert plug.state is False and ctl.state.on is False  # idle 601s, cool 50C


def test_usage_hot_idle_holds_past_idle_off():
    plug, ctl, clock, weather, sample = make(temp=79.0, util=40.0, trigger="usage")
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 5
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=79.0), weather)
    clock["t"] += 700
    st = ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=79.0), weather)
    assert plug.state is True, "hot idle must hold past idle_off_s"
    assert "hot" in st.reason and "guard" in st.reason
    clock["t"] += 1
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=77.0), weather)
    assert plug.state is False                          # cooled below guard -> off


def test_usage_hot_idle_never_starts_blower():
    plug, ctl, clock, weather, sample = make(temp=80.0, util=0.0, trigger="usage")
    st = ctl.evaluate(sample, weather)
    assert plug.state is False and ctl.state.on is False
    assert "idle" in st.reason                          # guard extends, never initiates


def test_usage_util_none_holds():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=40.0, trigger="usage")
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 5
    st = ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=None), weather)
    assert plug.state is True and "holding" in st.reason
    clock["t"] += 1000                                  # never toggles on unknown
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=None), weather)
    assert plug.state is True
    # OFF direction: unknown util must not switch on either
    plug, ctl, clock, weather, sample = make(temp=50.0, util=0.0, trigger="usage")
    assert ctl.evaluate(sample, weather).would_on is False
    clock["t"] += 5
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=None), weather)
    assert plug.state is False


def test_usage_stale_sample_force_off():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=40.0, trigger="usage")
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 60                                    # sample 61s old (>15s)
    ctl.evaluate(replace(sample, util_gpu=40.0), weather)
    assert plug.state is False
    assert ctl.state.inhibited == "GPU samples stale"
    assert any(e.kind == "inhibit" for e in ctl.events)


def test_usage_ignores_stale_weather():
    plug, ctl, clock, weather, sample = make(temp=60.0, util=40.0, trigger="usage")
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 60 * 60                               # weather now 1h stale
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=40.0, temp_gpu=60.0), weather)
    assert plug.state is True, "usage trigger must not care about weather"
    assert ctl.state.inhibited == ""
    assert not any(e.kind == "inhibit" for e in ctl.events)


def test_usage_dry_run_would_events():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=40.0, trigger="usage")
    ctl.cfg.mode = "dry_run"
    ctl.evaluate(sample, weather)
    assert plug.commands == [] and plug.state is False
    assert ctl.state.would_on is True
    assert sum(1 for e in ctl.events if e.kind == "would-on") == 1
    clock["t"] += 700
    st = ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=50.0), weather)
    assert st.would_on is False
    assert sum(1 for e in ctl.events if e.kind == "would-off") == 1  # no spam


def test_usage_manual_mode_wins():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=0.0, trigger="usage")
    ctl.cfg.mode = "manual"
    ctl.set_manual(True)
    assert plug.state is True                           # immediate, util is 0
    clock["t"] += 5
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0), weather)
    assert plug.state is True                           # usage decision can't override
    ctl.set_manual(False)
    assert plug.state is False


def test_usage_min_on_hold():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=40.0, trigger="usage")
    ctl.cfg.idle_off_s = 10.0
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 20
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=50.0), weather)
    clock["t"] += 11                                    # idle 11s >= 10s -> wants OFF,
    st = ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=50.0), weather)
    assert plug.state is True and "[min-on hold]" in st.reason  # but min_on=120s


def test_usage_duty_cap():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=90.0, trigger="usage")
    ctl.cfg.max_duty_min = 0.05                         # 3s cap
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 5
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=90.0), weather)
    assert plug.state is False                          # cap wins even at full load
    assert any(e.kind == "duty" for e in ctl.events)


def test_mode_change_resets_idle():
    plug, ctl, clock, weather, sample = make(temp=50.0, util=40.0, trigger="usage")
    ctl.evaluate(sample, weather)                       # ON
    clock["t"] += 300
    ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=50.0), weather)
    ctl.set_mode("manual")
    ctl.set_mode("auto")
    clock["t"] += 1
    st = ctl.evaluate(replace(sample, ts=clock["t"], util_gpu=0.0, temp_gpu=50.0), weather)
    assert "off in 10:00" in st.reason                  # idle clock restarted


if __name__ == "__main__":
    test_off_mode_never_actuates()
    test_trigger_and_hysteresis()
    test_min_delta_blocks_useless_blowing()
    test_min_off_time_holds()
    test_stale_sample_failsafe()
    test_stale_weather_inhibits_auto()
    test_duty_cap()
    test_dry_run_never_actuates_but_decides()
    test_manual_immediate_and_failsafe()
    test_retry_then_fail_alert()
    test_readback_mismatch_retries()
    test_ambient_fallback_without_weather()
    test_usage_turns_on_at_threshold()
    test_usage_below_threshold_stays_off()
    test_usage_off_requires_idle_and_cool()
    test_usage_hot_idle_holds_past_idle_off()
    test_usage_hot_idle_never_starts_blower()
    test_usage_util_none_holds()
    test_usage_stale_sample_force_off()
    test_usage_ignores_stale_weather()
    test_usage_dry_run_would_events()
    test_usage_manual_mode_wins()
    test_usage_min_on_hold()
    test_usage_duty_cap()
    test_mode_change_resets_idle()
    print("cooling tests: ALL PASS")
