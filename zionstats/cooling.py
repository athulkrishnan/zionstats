"""Cooling controller: hysteresis state machine driving blower plugs.

Safety posture -- this module can switch mains devices, so it is built
fail-safe first:

- mode `off` (default): does nothing at all
- mode `dry_run`: computes and logs decisions, never actuates
- mode `auto`: actuates, but force-OFF on stale GPU samples or stale weather,
  enforces minimum on/off times and a maximum continuous-duty cap, verifies
  every command by readback (retry x2, then alert)
- mode `manual`: the user toggles via the TUI; the controller still enforces
  failsafes and the duty cap

All clock time is injectable so the state machine is table-testable.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .plugs import Plug, PlugError

MODES = ("off", "dry_run", "auto", "manual")


@dataclass
class CoolingConfig:
    mode: str = "off"
    trigger_temp: float = 82.0
    resume_temp: float = 78.0
    min_delta_c: float = 8.0
    min_on_s: float = 120.0
    min_off_s: float = 180.0
    max_duty_min: float = 90.0
    trigger: str = "temp"    # "temp" | "usage" (usage: ON at load, OFF after idle_off_s)
    usage_util: float = 5.0  # % GPU util that counts as "in use"
    idle_off_s: float = 600.0
    max_sample_age_s: float = 15.0
    max_weather_age_s: float = 45 * 60.0
    verify_delay_s: float = 0.4
    ambient_temp_fallback_c: float = 30.0  # used when weather is unavailable


@dataclass
class CoolingState:
    mode: str = "off"
    on: bool = False
    would_on: bool = False
    reason: str = ""
    last_change_ts: float = 0.0
    on_since: float | None = None
    duty_seconds: float = 0.0
    plug_ok: bool | None = None
    inhibited: str = ""
    idle_since: float | None = None   # start of the current continuous-idle stretch
    util_pct: float | None = None     # last seen GPU util, for UI/debug


@dataclass
class CoolingEvent:
    ts: float
    kind: str  # "on" | "off" | "would-on" | "would-off" | "fail" | "inhibit" | "duty"
    msg: str


def config_from_section(c: dict, mode: str | None = None) -> CoolingConfig:
    """Build a validated CoolingConfig from the [cooling] config dict.

    `mode` overrides c["mode"] when given (e.g. the CLI --cooling flag)."""
    trigger = str(c.get("trigger", "temp"))
    m = mode if mode is not None else c.get("mode", "off")
    return CoolingConfig(
        mode=m if m in MODES else "off",
        trigger=trigger if trigger in ("temp", "usage") else "temp",
        trigger_temp=float(c.get("trigger_temp", 82.0)),
        resume_temp=float(c.get("resume_temp", 78.0)),
        min_delta_c=float(c.get("min_delta_c", 8.0)),
        min_on_s=float(c.get("min_on_s", 120.0)),
        min_off_s=float(c.get("min_off_s", 180.0)),
        max_duty_min=float(c.get("max_duty_min", 90.0)),
        usage_util=float(c.get("usage_util", 5.0)),
        idle_off_s=float(c.get("idle_off_s", 600.0)),
    )


class CoolingController:
    def __init__(
        self,
        plugs: list[Plug],
        cfg: CoolingConfig,
        on_event=None,
        clock=time.time,
        sleep=time.sleep,
    ) -> None:
        self.plugs = plugs
        self.cfg = cfg
        self._on_event = on_event
        self._clock = clock
        self._sleep = sleep
        self.state = CoolingState(mode=cfg.mode)
        self.events: deque[CoolingEvent] = deque(maxlen=100)
        self._desired: bool | None = None
        self._inhibited_alerted = False
        self._fail_alerted = False
        self._last_would: bool | None = None

    # ------------------------------------------------------------------

    def _emit(self, kind: str, msg: str) -> None:
        ev = CoolingEvent(self._clock(), kind, msg)
        self.events.append(ev)
        if self._on_event:
            self._on_event(ev)

    def _read_gpu_temp(self, sample) -> float | None:
        return getattr(sample, "temp_gpu", None)

    def _read_gpu_util(self, sample) -> float | None:
        return getattr(sample, "util_gpu", None)

    @staticmethod
    def _mmss(sec: float) -> str:
        m, s = divmod(max(0, int(sec)), 60)
        return f"{m}:{s:02d}"

    def _ambient(self, weather) -> float:
        if weather is not None and weather.temp_c is not None:
            return float(weather.temp_c)
        return self.cfg.ambient_temp_fallback_c

    def _decide(self, tgpu: float, tamb: float) -> tuple[bool, str]:
        """Hysteresis decision independent of timing constraints."""
        if self.state.on:
            if tgpu <= self.cfg.resume_temp:
                return False, f"gpu {tgpu:.0f}C <= resume {self.cfg.resume_temp:.0f}C"
            return True, f"holding (resume at {self.cfg.resume_temp:.0f}C)"
        if tgpu >= self.cfg.trigger_temp:
            if (tgpu - tamb) >= self.cfg.min_delta_c:
                return True, f"gpu {tgpu:.0f}C >= trigger {self.cfg.trigger_temp:.0f}C, delta {tgpu - tamb:.0f}C"
            return False, f"gpu hot but delta {tgpu - tamb:.0f}C < {self.cfg.min_delta_c:.0f}C (blower won't help)"
        return False, f"gpu {tgpu:.0f}C < trigger {self.cfg.trigger_temp:.0f}C"

    def _decide_usage(self, util, tgpu, now) -> tuple[bool, str]:
        """Usage-trigger decision: ON at load, OFF after idle_off_s AND cool.

        The hot-guard (resume_temp) extends an existing ON -- it never
        initiates one, so an idle-but-warm GPU with no load cannot chatter.
        """
        th = self.cfg.usage_util
        if util is None:
            # can't confirm idle continuity -> hold state, break the idle timer
            self.state.idle_since = None
            return self.state.on, "GPU util unknown -- holding"
        self.state.util_pct = util
        if util >= th:
            self.state.idle_since = None
            return True, f"usage {util:.0f}% >= {th:.0f}%"
        if self.state.idle_since is None:
            self.state.idle_since = now
        idle = now - self.state.idle_since
        if not self.state.on:
            return False, f"idle {self._mmss(idle)} (on at usage >= {th:.0f}%)"
        if tgpu is not None and tgpu >= self.cfg.resume_temp:
            return True, (f"idle {self._mmss(idle)}, hot {tgpu:.0f}C "
                          f">= guard {self.cfg.resume_temp:.0f}C")
        remaining = self.cfg.idle_off_s - idle
        if remaining > 0:
            return True, f"idle {self._mmss(idle)}, off in {self._mmss(remaining)}"
        return False, f"idle {self._mmss(idle)} >= {self.cfg.idle_off_s / 60:.0f}min, cool"

    def _apply(self, desired: bool, reason: str) -> None:
        """Actuate with readback verification + retry; alert on failure."""
        now = self._clock()
        ok_all = True
        for plug in self.plugs:
            ok = False
            for attempt in range(3):
                try:
                    (plug.on if desired else plug.off)()
                    self._sleep(self.cfg.verify_delay_s)
                    st = plug.status()
                    if st is None or bool(st) == desired:
                        ok = True
                        break
                except PlugError:
                    continue
            if not ok:
                ok_all = False
        if ok_all:
            self.state.on = desired
            self.state.last_change_ts = now
            self.state.on_since = now if desired else None
            if not desired:
                self.state.on_since = None
            self.state.reason = reason
            self.state.plug_ok = True
            self._emit("on" if desired else "off", reason)
            self._fail_alerted = False
        else:
            self.state.plug_ok = False
            if not self._fail_alerted:
                self._emit("fail", f"blower plug did not confirm {'ON' if desired else 'OFF'} after retries")
                self._fail_alerted = True
        # duty clock
        if desired and self.state.on:
            self.state.on_since = self.state.on_since or now

    # ------------------------------------------------------------------

    def evaluate(self, sample, weather, now: float | None = None) -> CoolingState:
        now = now if now is not None else self._clock()
        st = self.state
        st.mode = self.cfg.mode
        if self.cfg.mode == "off":
            st.would_on = False
            st.reason = "cooling disabled in settings"
            st.inhibited = ""
            if st.on:
                self._apply(False, "cooling mode set to off")
            return st

        tgpu = self._read_gpu_temp(sample)
        if tgpu is None or sample is None:
            st.reason = "no GPU sample"
            return st

        # sample staleness failsafe
        sample_age = now - getattr(sample, "ts", now)
        weather_age = (now - weather.ts) if weather is not None else float("inf")
        stale_sample = sample_age > self.cfg.max_sample_age_s
        stale_weather = weather is None or weather_age > self.cfg.max_weather_age_s

        if self.cfg.trigger == "usage":
            want, why = self._decide_usage(self._read_gpu_util(sample), tgpu, now)
        else:
            want, why = self._decide(tgpu, self._ambient(weather))
        st.would_on = want
        eff = ""
        if self.cfg.trigger != "usage" and weather is not None:
            eff = f" · eff {weather.effectiveness(tgpu):.0%}"
        st.reason = why + eff

        # manual mode: user's toggle wins; controller only enforces failsafes
        if self.cfg.mode == "manual":
            want = self._desired if self._desired is not None else st.on

        # duty cap (always enforced when actually on)
        if st.on and st.on_since is not None:
            st.duty_seconds = now - st.on_since
            if st.duty_seconds > self.cfg.max_duty_min * 60.0:
                self._emit("duty", f"max continuous duty {self.cfg.max_duty_min:.0f}min reached -- forced cooldown")
                if self.cfg.mode == "auto":
                    self._apply(False, "duty cap reached")
                st.on = False
                st.on_since = None
                return st

        # failsafes (auto actuation only)
        inhibit = ""
        if stale_sample:
            inhibit = "GPU samples stale"
        elif stale_weather and self.cfg.trigger != "usage" and self.cfg.mode == "auto":
            inhibit = "weather data stale"
        # stale telemetry force-off applies in every actuating mode;
        # stale weather only blocks auto (manual = user took responsibility)
        if inhibit and st.on and (stale_sample or self.cfg.mode == "auto"):
            self._apply(False, f"failsafe: {inhibit}")
            st.inhibited = inhibit
            if not self._inhibited_alerted:
                self._emit("inhibit", f"blower force-off: {inhibit}")
                self._inhibited_alerted = True
            return st
        if not inhibit:
            st.inhibited = ""
            self._inhibited_alerted = False

        if self.cfg.mode == "dry_run":
            if want != self._last_would:
                kind = "would-on" if want else "would-off"
                self._emit(kind, f"dry-run: would turn blower {'ON' if want else 'OFF'} -- {why}")
                self._last_would = want
            st.would_on = want
            return st

        if self.cfg.mode == "manual":
            if want != st.on:
                self._apply(want, why or "manual toggle")
            return st

        # timing constraints for actuation
        if want != st.on:
            now_ts = now
            since = st.last_change_ts
            if st.on and now_ts - since < self.cfg.min_on_s:
                st.reason += " [min-on hold]"
                return st
            if not st.on and now_ts - since < self.cfg.min_off_s:
                st.reason += " [min-off hold]"
                return st
            self._apply(want, why)
        return st

    # ------------------------------------------------------------------

    def set_manual(self, desired: bool) -> None:
        """TUI `o` key: user override (only meaningful in manual mode)."""
        self._desired = desired
        if self.cfg.mode == "manual":
            self._apply(desired, "manual toggle")

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            return
        self.cfg.mode = mode
        self.state.mode = mode
        self.state.idle_since = None  # a mode excursion must not leave a stale idle clock
        if mode == "off":
            self._apply(False, "cooling mode set to off")
        elif mode == "manual":
            self._desired = self.state.on
