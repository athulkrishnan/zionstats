"""Main TUI application: sampling loop, key handling, panel orchestration."""

from __future__ import annotations

import curses
import time
from collections import deque

from . import __version__
from .config import Config
from .insights import Insights
from .nvml_layer import DemoProvider, NvmlProvider, ProviderError
from .recorder import Recorder, ReplayProvider
from .ui import layout, overlays
from .ui.cells import Screen
from .ui.themes import DEFAULT_THEME, REGISTRY, THEME_ORDER, get_theme, next_theme

METRICS = ("util_gpu", "util_mem", "power", "sm", "pcie", "memfrac")
SORTS = ("pid", "user", "mem", "sm", "cpu")


class App:
    def __init__(self, provider, cfg: Config, theme_name: str | None = None,
                 interval: float | None = None, layout_mode: str | None = None,
                 args=None) -> None:
        self.provider = provider
        self.cfg = cfg
        self.version = __version__
        self.theme = get_theme(theme_name or cfg.theme)
        self.interval = interval or float(cfg.interval)
        self.layout_mode = layout_mode or cfg.layout
        self.fahrenheit = bool(cfg.fahrenheit)
        self.paint_bg_pref = bool(cfg.paint_bg)
        self.paint_bg = self.paint_bg_pref
        self._screen = None
        self.sort = cfg.sort if cfg.sort in SORTS else "pid"
        self.graph_style = getattr(cfg, "graph_style", "block")
        self.filter_me = bool(cfg.filter_me)
        self.replaying = isinstance(provider, ReplayProvider)
        self.is_demo = bool(getattr(provider, "environment", None)) and \
            getattr(provider, "environment")().get("provider") == "demo"

        self.env = provider.environment()
        self.devices = provider.devices()
        self.indices = [d["index"] for d in self.devices]
        self.selected = self.indices[0] if self.indices else 0
        self.show_detail = True
        self.show_climate = False
        self.paused = False
        self.running = True

        self.hist: dict[int, dict] = {}
        self.samples: dict[int, object] = {}
        self.insights: dict[int, object] = {}
        self.alerts_engine = Insights()
        self.proc_rows: list = []
        self.user_rollup: list = []

        self.recorder: Recorder | None = None
        self.recording: str | None = None
        self.theme_ui: dict = {}
        self.show_help = False
        self._status_until = 0.0
        self._status_msg = ""
        self._username = _current_user()
        self._last_sample = 0.0
        self._proc_every = 2
        self._tick_n = 0

        # ---- v0.2: weather layer
        wcfg = dict(cfg.weather)
        if args is not None and getattr(args, "location", None):
            try:
                bits = str(args.location).split(",")
                wcfg["lat"] = float(bits[0])
                wcfg["lon"] = float(bits[1])
                if len(bits) > 2:
                    wcfg["label"] = bits[2]
            except (ValueError, IndexError):
                pass  # bad --location falls back to config
        if args is not None and getattr(args, "no_weather", False):
            wcfg["enabled"] = False
        self.weather = None        # provider (fetch/cache)
        self.weather_now = None    # latest Weather observation
        if wcfg.get("enabled"):
            if self.is_demo:
                from .climate import DemoWeather

                self.weather = DemoWeather(wcfg.get("lat", 11.15), wcfg.get("lon", 75.83),
                                           wcfg.get("label", "") + " (demo)")
            else:
                from .climate import WeatherProvider

                self.weather = WeatherProvider(
                    float(wcfg.get("lat", 0.0)), float(wcfg.get("lon", 0.0)),
                    wcfg.get("label", ""), float(wcfg.get("refresh_min", 20.0)),
                )
                self.weather.refresh()  # warm the cache; failures are tolerated
            self.weather_now = self.weather.last

        # ---- v0.2: cooling controller
        self.controller = None
        cmode = getattr(args, "cooling", None) if args is not None else None
        if self.is_demo:
            # demo showcase: mock plug, dry-run only -- never real mains
            from .cooling import CoolingConfig, CoolingController
            from .plugs import MockPlug

            cc = CoolingConfig(mode="dry_run")
            self.controller = CoolingController([MockPlug("blower (mock)")], cc,
                                                on_event=self._cooling_event)
            self.cooling_mode = "dry_run"
        elif cfg.cooling.get("enabled"):
            from .cooling import CoolingController, config_from_section
            from .plugs import build_plugs

            plugs, errors = build_plugs(cfg.cooling.get("plugs", []))
            for err in errors:
                self.alerts_engine._alert("warn", f"plug config: {err}")
            if cmode is None:
                cmode = cfg.cooling.get("mode", "off")
            cc = config_from_section(cfg.cooling, mode=cmode)
            self.controller = CoolingController(plugs, cc, on_event=self._cooling_event)
            self.cooling_mode = cc.mode
        else:
            self.cooling_mode = cmode or "off"

    @staticmethod
    def _do_resize(stdscr, scr, rows: int, cols: int) -> None:
        curses.resizeterm(rows, cols)
        scr.resize(rows, cols)
        try:
            curses.update_lines_cols()
        except Exception:
            pass

    # ------------------------------------------------------------ v0.2

    def _cooling_event(self, ev) -> None:
        level = {"fail": "crit", "duty": "warn", "inhibit": "warn",
                 "on": "info", "off": "info"}.get(ev.kind)
        if level and ev.kind != "would-on" and ev.kind != "would-off":
            self.alerts_engine._alert(level, f"blower: {ev.msg}")

    def _hottest_sample(self):
        hot = None
        for s in self.samples.values():
            if hot is None or (s.temp_gpu or 0) > (hot.temp_gpu or 0):
                hot = s
        return hot

    def _cycle_cooling_mode(self) -> None:
        order = ("off", "dry_run", "auto", "manual")
        i = order.index(getattr(self.controller.cfg, "mode", "off")) if self.controller else 0
        nxt = order[(i + 1) % len(order)]
        if self.controller is None:
            self._flash("cooling disabled in settings ([cooling] enabled = true)")
            return
        if nxt == "auto" and not self.controller.plugs:
            self._flash("no plugs configured -- see README plug setup")
            return
        self.controller.set_mode(nxt)
        self.cooling_mode = nxt
        self.cfg.set("cooling.mode", nxt)
        self._flash(f"cooling mode: {nxt}" + (" (mock plug)" if self.is_demo else ""))

        self._username = _current_user()
        self._last_sample = 0.0
        self._proc_every = 2
        self._tick_n = 0
        self._status_msg = ""

    # ------------------------------------------------------------ sampling

    def tick(self) -> None:
        self._tick_n += 1
        for s in self.provider.sample():
            h = self.hist.setdefault(s.index, {m: deque(maxlen=900) for m in METRICS})
            h["util_gpu"].append(s.util_gpu or 0.0)
            h["util_mem"].append(s.util_mem or 0.0)
            h["memfrac"].append(100.0 * s.mem_used / s.mem_total if s.mem_total else 0.0)
            h["power"].append(s.power_w or 0.0)
            h["sm"].append(s.clocks_sm_mhz or 0.0)
            gbps = ((s.pcie_rx_kbps or 0) + (s.pcie_tx_kbps or 0)) / 1e6
            h["pcie"].append(gbps)
            th = h.setdefault("temp", deque(maxlen=900))
            th.append((s.ts, s.temp_gpu or 0.0))
            self.samples[s.index] = s
            self.insights[s.index] = self.alerts_engine.update(s, h["temp"])
        if self._tick_n % self._proc_every == 0 or not self.proc_rows:
            self._refresh_procs()
        if self.weather is not None:
            self.weather_now = self.weather.refresh()
        if self.controller is not None:
            hot = self._hottest_sample()
            if hot is not None:
                self.controller.evaluate(hot, self.weather_now)
        if self.recorder is not None:
            self.recorder.write(
                self.env, [self.samples[i] for i in self.indices if i in self.samples],
                self.proc_rows,
            )
        self._last_sample = time.time()

    def _refresh_procs(self) -> None:
        procs = self.provider.processes()
        if self.filter_me:
            procs = [p for p in procs if p.user == self._username]
        key = self.sort
        def sk(p):
            v = {
                "pid": p.pid, "user": p.user, "mem": p.mem_used,
                "sm": p.sm_pct if p.sm_pct is not None else -1,
                "cpu": p.cpu_pct if p.cpu_pct is not None else -1,
            }[key]
            return (v if not isinstance(v, str) else v.lower())
        procs.sort(key=sk, reverse=key in ("mem", "sm", "cpu"))
        self.proc_rows = procs
        roll: dict[str, list] = {}
        for p in self.provider.processes():
            r = roll.setdefault(p.user, [0, 0])
            r[0] += 1
            r[1] += p.mem_used
        self.user_rollup = sorted(
            ((u, n, mem / 2**30) for u, (n, mem) in roll.items()),
            key=lambda t: -t[2],
        )

    # ------------------------------------------------------------ drawing

    def draw(self, scr: Screen) -> None:
        W, H = scr.cols, scr.rows
        bg = self.theme.q("bg")
        scr.begin_frame(bg)
        if W < 60 or H < 12:
            scr.puts(2, H // 2, f"terminal too small ({W}x{H}) -- need >= 60x12",
                     self.theme.q("warn"), bg, bold=True)
            scr.flush()
            return
        agg = self._aggregate()
        layout.draw_header(scr, W, self.env, agg, self)
        rects = layout.plan_layout(W, H, len(self.indices), self.layout_mode,
                                   self.show_detail, climate=self.show_climate)
        for i, idx in enumerate(self.indices):
            if i < len(rects["cards"]):
                x, y, w, h = rects["cards"][i]
                layout.draw_gpu_card(scr, self, x, y, w, h, idx, selected=(idx == self.selected))
        if rects["detail"] and self.selected in self.samples:
            layout.draw_detail(scr, self, *rects["detail"], self.selected)
        if rects.get("climate") and self.weather is not None:
            layout.draw_climate_panel(scr, self, *rects["climate"])
        if rects["proc"]:
            layout.draw_processes(scr, self, *rects["proc"])
        if list(self.alerts_engine.alerts):
            layout.draw_alerts(scr, self, H - 2, W)
        if self._status_msg:
            scr.puts(1, H - 2, self._status_msg[:W - 40], self.theme.q("accent2"), bg)
        layout.draw_keyhints(scr, self, H - 1, W)
        if self.theme_ui:
            overlays.draw_theme_picker(
                scr, W, H, self.theme_ui["cursor"], self.theme,
                self.theme_ui["preview"], self.theme_ui["original"],
            )
        elif self.show_help:
            overlays.draw_help(scr, W, H, self.theme)
        scr.flush()

    def _aggregate(self) -> dict:
        ss = [self.samples[i] for i in self.indices if i in self.samples]
        agg: dict = {}
        if ss:
            pw = [s.power_w for s in ss if s.power_w is not None]
            cap = [s.power_limit_w for s in ss if s.power_limit_w]
            agg["power_w"] = sum(pw) if pw else None
            agg["power_limit_w"] = sum(cap) if cap else None
            mu = sum(s.mem_used for s in ss)
            mt = sum(s.mem_total for s in ss)
            agg["mem_used"] = mu
            agg["mem_used_gib"] = mu / 2**30
            agg["mem_total_gib"] = mt / 2**30
            agg["mem_frac"] = mu / mt if mt else 0
        return agg

    # ------------------------------------------------------------ keys

    def handle_key(self, key: int, scr: Screen) -> None:
        if self.theme_ui:
            self._key_theme(key)
            return
        if self.show_help:
            self.show_help = False
            return
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_SLEFT, curses.KEY_SRIGHT):
            step = -1 if key in (curses.KEY_LEFT, curses.KEY_SLEFT) else 1
            pos = self.indices.index(self.selected) if self.selected in self.indices else 0
            self.selected = self.indices[(pos + step) % len(self.indices)]
        elif key == ord("\t"):
            pos = self.indices.index(self.selected) if self.selected in self.indices else 0
            self.selected = self.indices[(pos + 1) % len(self.indices)]
        elif ord("1") <= key <= ord("9"):
            idx = key - ord("1")
            if idx < len(self.indices):
                self.selected = self.indices[idx]
        else:
            c = chr(key) if 0 <= key < 256 else ""
            if c == "q":
                self.running = False
            elif c == "t":
                start = THEME_ORDER.index(self.theme.name) if self.theme.name in THEME_ORDER else 0
                self.theme_ui = {"cursor": start, "preview": self.theme.name,
                                 "original": self.theme.name}
            elif c == "m":
                self._apply_theme(next_theme(self.theme.name), persist=True)
            elif c == "d":
                self.show_detail = not self.show_detail
            elif c == "p":
                self.paused = not self.paused
                self._flash("paused" if self.paused else "resumed")
            elif c == "r":
                self.alerts_engine.reset()
                for h in self.hist.values():
                    for q in h.values():
                        q.clear()
                self.insights.clear()
                self._flash("session stats reset")
            elif c == "s":
                i = SORTS.index(self.sort)
                self.sort = SORTS[(i + 1) % len(SORTS)]
                self.cfg.set("sort", self.sort)
                self._refresh_procs()
            elif c == "f":
                self.filter_me = not self.filter_me
                self.cfg.set("filter_me", self.filter_me)
                self._refresh_procs()
            elif c == "F":
                self.fahrenheit = not self.fahrenheit
                self.cfg.set("fahrenheit", self.fahrenheit)
            elif c == "b":
                if self.theme.light:
                    self._flash("light themes always paint their background")
                else:
                    self.paint_bg_pref = not self.paint_bg_pref
                    self.cfg.set("paint_bg", self.paint_bg_pref)
                    scr.set_paint_bg(self.paint_bg_pref)
            elif c == "l":
                order = ("auto", "hero", "grid", "compact")
                i = order.index(self.layout_mode) if self.layout_mode in order else 0
                self.layout_mode = order[(i + 1) % len(order)]
                self.cfg.set("layout", self.layout_mode)
            elif c == "w":
                if self.weather is not None:
                    self.show_climate = not self.show_climate
                else:
                    self._flash("weather layer disabled in settings")
            elif c == "o":
                if self.controller is None:
                    self._flash("cooling disabled in settings ([cooling] enabled = true)")
                else:
                    if self.controller.cfg.mode != "manual":
                        self.controller.set_mode("manual")
                        self.cooling_mode = "manual"
                    self.controller.set_manual(not self.controller.state.on)
                    self._flash(f"blower {'ON' if self.controller.state.on else 'OFF'} (manual)")
            elif c == "c":
                self._cycle_cooling_mode()
            elif c == "a":
                # one-press arm/disarm: AUTO = usage-triggered blower
                if self.controller is None:
                    self._flash("cooling disabled in settings ([cooling] enabled = true)")
                elif not self.controller.plugs:
                    self._flash("no plugs configured -- see README plug setup")
                else:
                    nxt = "off" if self.controller.cfg.mode == "auto" else "auto"
                    self.controller.set_mode(nxt)
                    self.cooling_mode = nxt
                    self.cfg.set("cooling.mode", nxt)
                    self._flash("blower: AUTO -- on at GPU load, off after idle+cool"
                                if nxt == "auto" else "blower: off")
            elif c in "+=":
                self.interval = min(10.0, max(0.2, self.interval * 1.5))
                self.cfg.set("interval", round(self.interval, 2))
                self._flash(f"interval {self.interval:.1f}s")
            elif c == "-":
                self.interval = min(10.0, max(0.2, self.interval / 1.5))
                self.cfg.set("interval", round(self.interval, 2))
                self._flash(f"interval {self.interval:.1f}s")
            elif c == "?":
                self.show_help = True

    def _key_theme(self, key: int) -> None:
        ui = self.theme_ui
        n = len(REGISTRY)
        if key in (curses.KEY_UP, ord("k")):
            ui["cursor"] = (ui["cursor"] - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            ui["cursor"] = (ui["cursor"] + 1) % n
        elif key == curses.KEY_ENTER or key in (10, 13):
            self._apply_theme(ui["preview"], persist=True)
            self.theme_ui = {}
            self._flash(f"theme: {self.theme.name}")
            return
        elif key == 27 or key == ord("q"):
            self._apply_theme(ui["original"], persist=False)
            self.theme_ui = {}
            return
        else:
            return
        ui["preview"] = list(REGISTRY)[ui["cursor"]]
        self._apply_theme(ui["preview"], persist=False)

    def _apply_theme(self, name: str, persist: bool) -> None:
        self.theme = get_theme(name)
        # light themes must paint their canvas or they are unreadable on a
        # dark terminal; dark themes follow the user's preference
        if self._screen is not None:
            self._screen.set_paint_bg(self.theme.light or self.paint_bg_pref)
        if persist:
            self.cfg.set("theme", name)

    def _flash(self, msg: str) -> None:
        self._status_msg = msg
        self._status_until = time.time() + 3

    # ------------------------------------------------------------ main loop

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.nodelay(True)
        stdscr.timeout(80)
        scr = Screen(stdscr, paint_bg=self.theme.light or self.paint_bg_pref)
        self._screen = scr
        try:
            self.tick()
        except Exception as e:
            self.running = False
            stdscr.addstr(0, 0, f"sample error: {e}")
            stdscr.refresh()
            time.sleep(2)
            return
        while self.running:
            if self.replaying and getattr(self.provider, "exhausted", False):
                self._flash("end of trace -- q to quit")
            now = time.time()
            if not self.paused and not (
                self.replaying and getattr(self.provider, "exhausted", False)
            ) and now - self._last_sample >= self.interval:
                try:
                    self.tick()
                except Exception as e:
                    self._flash(f"sample error: {e}")
            if time.time() > getattr(self, "_status_until", 0):
                self._status_msg = ""
            try:
                rows, cols = stdscr.getmaxyx()
                if (rows, cols) != (scr.rows, scr.cols):
                    self._do_resize(stdscr, scr, rows, cols)
            except Exception:
                pass
            self.draw(scr)
            key = stdscr.getch()
            if key != -1:
                if key == curses.KEY_RESIZE:
                    # ncurses only resizes its windows when we tell it to;
                    # do it here too so a client resize is never missed
                    try:
                        rows, cols = stdscr.getmaxyx()
                        if (rows, cols) != (scr.rows, scr.cols):
                            self._do_resize(stdscr, scr, rows, cols)
                    except Exception:
                        pass
                    continue
                self.handle_key(key, scr)
        if self.recorder is not None:
            self.recorder.close()


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        import os

        return str(os.getuid())


def run_tui(provider, cfg: Config, **kw) -> None:
    app = App(provider, cfg, **kw)
    curses.wrapper(app.run)


def make_provider(args) -> object:
    if args.demo:
        return DemoProvider(only=args.only)
    if args.replay:
        return ReplayProvider(args.replay)
    return NvmlProvider(only=args.only)
