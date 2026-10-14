"""Main TUI application: sampling loop, key handling, panel orchestration."""

from __future__ import annotations

import curses
import time
from collections import deque
from typing import Any

from . import __version__
from . import procs as procs_mod
from .config import Config
from .insights import AutoScale as InsightsAutoScale
from .insights import Insights
from .nvml_layer import DemoProvider, NvmlProvider, Sample
from .recorder import Recorder, ReplayProvider
from .ui import layout, overlays
from .ui.cells import Screen
from .ui.themes import REGISTRY, THEME_ORDER, get_theme, next_theme

METRICS = ("util_gpu", "util_mem", "power", "sm", "pcie", "memfrac")
SORTS = ("pid", "user", "mem", "sm", "cpu")

# characters the TUI already binds; preset hotkeys may not shadow these
_BOUND_CHARS = frozenset(
    "qjktmdprsFblwocav /TKOxP+-=?" + "".join(str(i) for i in range(1, 10))
)


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
        self._screen: Screen | None = None
        self.sort = cfg.sort if cfg.sort in SORTS else "pid"
        self.graph_style = getattr(cfg, "graph_style", "block")
        self.filter_me = bool(cfg.filter_me)
        self.replaying = isinstance(provider, ReplayProvider)
        self.is_demo = bool(getattr(provider, "environment", None)) and \
            provider.environment().get("provider") == "demo"

        self.env = provider.environment()
        self.devices = provider.devices()
        self.indices = [d["index"] for d in self.devices]
        self.selected = self.indices[0] if self.indices else 0
        # panel visibility (btop-style box toggles, persisted)
        self.show_detail = bool(getattr(cfg, "panels_detail", True))
        self.show_climate = bool(getattr(cfg, "panels_climate", False))
        self.show_alerts = bool(getattr(cfg, "panels_alerts", True))
        self.show_procs = bool(getattr(cfg, "panels_procs", True))
        self.paused = False
        self.running = True

        self.hist: dict[int, dict] = {}
        self.samples: dict[int, Sample] = {}
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

        # ---- v0.4: process forensics (tree/tags/detail/signals: nvitop
        # UI concepts, clean-room implementation; see NOTICE)
        self.readonly = bool(getattr(args, "readonly", False)) or bool(
            getattr(cfg, "readonly", False))
        self.proc_cursor_pid: int | None = None   # pid-keyed: rows re-sort live
        self.proc_top: int = 0
        self.tree_mode: bool = False
        self.tagged: set[int] = set()
        self.filter_re = None                     # compiled regex | None
        self._filter_re_src: str = str(getattr(cfg, "filter_regex", "") or "")
        if self._filter_re_src:
            import re as _re

            try:
                self.filter_re = _re.compile(self._filter_re_src, _re.IGNORECASE)
            except _re.error:
                self._filter_re_src = ""
        self.regex_input: str | None = None       # non-None = input modal open
        self.confirm: dict | None = None          # signal confirmation modal
        self.proc_detail_pid: int | None = None
        self.proc_detail_scroll = 0
        self.proc_detail_env: list[tuple[str, str]] = []
        self.proc_detail_info = None              # ProcInfo | None (frozen at open)
        self.proc_hist: dict[int, dict[str, deque]] = {}
        self.proc_table: dict = {}                # pid -> ProcInfo (tree/detail)
        self._tree_rows: list = []                # TreeRow cache for selection

        # ---- v0.4: btop-style menus, presets, mouse, auto-scale
        self.options_ui: dict = {}                # {"cursor": int}
        self.panels_ui: dict = {}
        self.presets_ui: dict = {}
        self.mouse_enabled = bool(getattr(cfg, "mouse", True))
        self._zones: list[tuple[int, int, int, int, str, Any]] = []
        self.autoscale: dict[tuple, Any] = {}
        self.auto_scale = bool(getattr(cfg, "auto_scale", True))
        self.preset_keys: dict[str, str] = {}     # hotkey char -> preset name
        for name, pr in getattr(cfg, "presets", {}).items():
            key = pr.get("key")
            if isinstance(key, str) and len(key) == 1 and \
                    key not in _BOUND_CHARS:
                self.preset_keys[key] = name

        # ---- v0.4: embedded metrics exporter (nvitop-exporter idea, stdlib)
        self.exporter = None
        exporter, err = make_exporter(args, cfg)
        if exporter is not None:
            self.exporter = exporter
            self._flash(f"exporting {exporter.url}")
        elif err:
            self.alerts_engine._alert("warn", err)

        # ---- v0.2: weather layer (duck-typed DemoWeather | WeatherProvider)
        self.weather: Any = None
        self.weather_now: Any = None
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

                self.weather = DemoWeather(wcfg.get("lat", 0.0), wcfg.get("lon", 0.0),
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
        self.controller: Any = None
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
            if self.auto_scale:
                gbps = ((s.pcie_rx_kbps or 0) + (s.pcie_tx_kbps or 0)) / 1e6
                self.autoscale.setdefault((s.index, "power"), InsightsAutoScale()) \
                    .update(s.power_w)
                self.autoscale.setdefault((s.index, "pcie"), InsightsAutoScale()) \
                    .update(gbps)
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
        if self.exporter is not None:
            self.exporter.publish(
                self.env, [self.samples[i] for i in self.indices if i in self.samples],
                self.proc_rows, self.insights, paused=self.paused,
            )
        self._last_sample = time.time()

    def _refresh_procs(self) -> None:
        all_procs = self.provider.processes()
        procs = all_procs
        if self.filter_me:
            procs = [p for p in procs if p.user == self._username]
        if self.filter_re is not None:
            procs = [p for p in procs
                     if self.filter_re.search(f"{p.user} {p.command} {p.pid}")]
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

        # per-process metric history (feeds the detail-screen sparkline)
        live_pids: set[int] = set()
        for p in all_procs:
            live_pids.add(p.pid)
            h = self.proc_hist.setdefault(p.pid, {})
            for m, v in (("sm", p.sm_pct), ("mem", p.mem_pct),
                         ("enc", p.enc_pct), ("dec", p.dec_pct)):
                q = h.setdefault(m, deque(maxlen=240))
                q.append(v if v is not None else 0.0)
        for pid in [k for k in self.proc_hist if k not in live_pids]:
            del self.proc_hist[pid]
        self.tagged &= live_pids

        # /proc forensics table for tree + detail (missing pids silently absent)
        getter = getattr(self.provider, "proc_info", None)
        if getter is not None:
            try:
                self.proc_table = getter(sorted(live_pids))
            except Exception:
                self.proc_table = {}
            if self.tree_mode:
                # ancestors need their own records for labels; fetch to a
                # fixpoint (a fetched parent may reveal its own parent)
                from .procs import ancestors_of as _anc

                for _ in range(4):
                    need: set[int] = set()
                    for p in procs:
                        need.update(_anc(p.pid, self.proc_table))
                    missing = sorted(need - set(self.proc_table))
                    if not missing:
                        break
                    try:
                        self.proc_table.update(getter(missing))
                    except Exception:
                        break
        self._tree_rows = procs_mod.build_tree_rows(
            procs, self.proc_table,
            sort_key=sk, reverse=key in ("mem", "sm", "cpu"),
        ) if self.tree_mode else []

        # cursor re-anchor: the list re-sorts every refresh, so selection is
        # pid-keyed; clear when the process vanished
        if self.proc_cursor_pid is not None and \
                not any(p.pid == self.proc_cursor_pid for p in procs):
            self.proc_cursor_pid = None

        # per-user rollup from the UNFILTERED list (v0.1 semantics)
        roll: dict[str, list] = {}
        for p in all_procs:
            r = roll.setdefault(p.user, [0, 0])
            r[0] += 1
            r[1] += p.mem_used
        self.user_rollup = sorted(
            ((u, n, mem / 2**30) for u, (n, mem) in roll.items()),
            key=lambda t: -t[2],
        )

    # ---------------------------------------------------------- v0.4 procs

    def selectable_pids(self) -> list[int]:
        """PIDs the cursor can occupy, in display order (flat rows, or
        non-ancestor tree rows)."""
        if self.tree_mode:
            return [r.pid for r in self._tree_rows if not r.is_ancestor]
        return [p.pid for p in self.proc_rows]

    def _proc_step(self, n: int) -> None:
        pids = self.selectable_pids()
        if not pids:
            self.proc_cursor_pid = None
            return
        if self.proc_cursor_pid is None or self.proc_cursor_pid not in pids:
            self.proc_cursor_pid = pids[0 if n >= 0 else -1]
            return
        i = pids.index(self.proc_cursor_pid)
        self.proc_cursor_pid = pids[max(0, min(len(pids) - 1, i + n))]

    def _proc_page(self, pages: int) -> None:
        # page size comes from the renderer; 10 is a sane keyboard default
        self._proc_step(pages * 10)

    def _proc_home_end(self, end: bool) -> None:
        pids = self.selectable_pids()
        if pids:
            self.proc_cursor_pid = pids[-1] if end else pids[0]

    def _selected_proc(self):
        if self.proc_cursor_pid is None:
            return None
        for p in self.proc_rows:
            if p.pid == self.proc_cursor_pid:
                return p
        return None

    def _open_proc_detail(self) -> None:
        p = self._selected_proc()
        if p is None:
            self._flash("no process selected")
            return
        self.proc_detail_pid = p.pid
        self.proc_detail_scroll = 0
        self.proc_detail_env = []
        self.proc_detail_info = None
        getter = getattr(self.provider, "proc_info", None)
        if getter is not None:
            try:
                got = getter([p.pid], with_env=True)
                self.proc_detail_info = got.get(p.pid)
                self.proc_detail_env = list(getattr(self.proc_detail_info, "environ", []) or [])
            except Exception:
                pass

    def _request_signal(self, sig: int) -> None:
        p = self._selected_proc()
        if p is None:
            self._flash("no process selected")
            return
        if self.readonly:
            self._flash("readonly -- signals disabled (--readonly)")
            return
        if self.is_demo or self.replaying:
            self._flash("signals disabled in demo/replay")
            return
        if not isinstance(self.provider, NvmlProvider):
            self._flash("signals need a live NVML provider")
            return
        label = procs_mod.SIGNAL_NAME.get(int(sig), str(sig))
        self.confirm = {"pid": p.pid, "sig": int(sig), "label": label,
                        "cmdline": (p.command or "?")[:60], "typed": ""}

    def _confirm_step(self, key: int) -> None:
        c = self.confirm
        if c is None:
            return
        if key == 27 or key in (ord("q"), ord("n"), ord("N")):
            self.confirm = None
            self._flash("cancelled")
            return
        if key in (curses.KEY_ENTER, 10, 13):
            if c["typed"] != "yes":
                self._flash('type "yes" to confirm (Esc cancels)')
                return
            self.confirm = None
            try:
                procs_mod.signal_process(c["pid"], c["sig"])
            except ProcessLookupError:
                self._flash(f"pid {c['pid']} already gone")
                return
            except PermissionError:
                self.alerts_engine._alert("crit", f"permission denied: {c['label']} pid {c['pid']}")
                self._flash("permission denied")
                return
            self._flash(f"sent {c['label']} to {c['pid']}")
            self.alerts_engine._alert("warn",
                                      f"sent {c['label']} to pid {c['pid']} ({c['cmdline']})")
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            c["typed"] = c["typed"][:-1]
            return
        if 32 <= key < 127:
            if len(c["typed"]) < 8:
                c["typed"] += chr(key)

    def _regex_step(self, key: int) -> None:
        if self.regex_input is None:
            return
        if key == 27:
            self.regex_input = None
            self._flash("cancelled")
            return
        if key in (curses.KEY_ENTER, 10, 13):
            src = self.regex_input
            import re as _re

            if not src:
                self.regex_input = None
                self.filter_re = None
                self._filter_re_src = ""
                self.cfg.set("filter_regex", "")
                self._flash("regex filter cleared")
                self._refresh_procs()
                return
            try:
                compiled = _re.compile(src, _re.IGNORECASE)
            except _re.error as e:
                self._flash(f"bad regex: {e}")   # stay in the input modal
                return
            self.regex_input = None
            self.filter_re = compiled
            self._filter_re_src = src
            self.cfg.set("filter_regex", src)
            self._flash(f"filter: /{src}/i")
            self._refresh_procs()
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.regex_input = self.regex_input[:-1]
            return
        if 32 <= key < 127:
            if len(self.regex_input) < 120:
                self.regex_input += chr(key)

    def _toggle_tree(self) -> None:
        self.tree_mode = not self.tree_mode
        self._refresh_procs()
        self._flash("tree view" if self.tree_mode else "flat view")

    # --------------------------------------------------- v0.4 btop UX pack

    def _step_interval(self, mult: float) -> None:
        self.interval = min(10.0, max(0.2, self.interval * mult))
        self.cfg.set("interval", round(self.interval, 2))
        self._flash(f"interval {self.interval:.1f}s")

    def _send_key(self, ch: str) -> None:
        """Synthetic keypress straight to the normal-mode handler: modal
        menus must not intercept the very actions they trigger."""
        self._key_normal(ord(ch), self._screen)

    def _cycle_graph_style(self) -> None:
        order = ("btop", "block", "tty", "braille", "ascii")
        i = order.index(self.graph_style) if self.graph_style in order else 0
        self.graph_style = order[(i + 1) % len(order)]
        self.cfg.set("graph_style", self.graph_style)
        self._flash(f"graph style: {self.graph_style}")

    def _set_panel(self, name: str, on: bool) -> None:
        setattr(self, f"show_{name}", on)
        self.cfg.set(f"panels_{name}", on)

    def _options_rows(self) -> list[dict]:
        """Menu model for the btop-style options overlay.  Every row cycles
        through the SAME handler the direct keys use, so live-apply and
        persistence stay single-sourced."""
        return [
            {"label": "interval", "value": f"{self.interval:.1f}s",
             "cycle": lambda d: self._step_interval(1.5 if d > 0 else 1 / 1.5)},
            {"label": "theme", "value": self.theme.name,
             "cycle": lambda d: self._apply_theme(next_theme(self.theme.name), persist=True)},
            {"label": "graph style", "value": self.graph_style,
             "cycle": lambda d: self._cycle_graph_style()},
            {"label": "layout", "value": self.layout_mode,
             "cycle": lambda d: self._send_key("l")},
            {"label": "sort", "value": self.sort,
             "cycle": lambda d: self._send_key("s")},
            {"label": "filter (my user)", "value": "on " if self.filter_me else "off",
             "cycle": lambda d: self._send_key("f")},
            {"label": "fahrenheit", "value": "on " if self.fahrenheit else "off",
             "cycle": lambda d: self._send_key("F")},
            {"label": "paint background", "value": "on " if self.paint_bg_pref else "off",
             "cycle": lambda d: self._send_key("b")},
            {"label": "tree view", "value": "on " if self.tree_mode else "off",
             "cycle": lambda d: self._toggle_tree()},
            {"label": "auto-scale graphs", "value": "on " if self.auto_scale else "off",
             "cycle": lambda d: self._toggle_auto_scale()},
            {"label": "panels...", "value": "(x)",
             "cycle": lambda d: setattr(self, "panels_ui", {"cursor": 0})},
            {"label": "presets...", "value": f"{len(self.cfg.presets)}",
             "cycle": lambda d: self._send_key("P")},
            {"label": "mouse", "value": "on " if self.mouse_enabled else "off",
             "cycle": lambda d: None},
            {"label": "readonly", "value": "on " if self.readonly else "off",
             "cycle": lambda d: None},
        ]

    def _toggle_auto_scale(self) -> None:
        self.auto_scale = not self.auto_scale
        self.cfg.set("auto_scale", self.auto_scale)
        if not self.auto_scale:
            self.autoscale.clear()
        self._flash(f"auto-scale graphs {'on' if self.auto_scale else 'off'}")

    def _key_options(self, key: int) -> None:
        rows = self._options_rows()
        ui = self.options_ui
        n = len(rows)
        if key in (curses.KEY_UP, ord("k")):
            ui["cursor"] = (ui["cursor"] - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            ui["cursor"] = (ui["cursor"] + 1) % n
        elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")):
            rows[ui["cursor"]]["cycle"](1)
        elif key in (curses.KEY_LEFT, ord("h")):
            rows[ui["cursor"]]["cycle"](-1)
        elif key == 27 or key == ord("q"):
            self.options_ui = {}
            return
        else:
            return

    def _key_panels(self, key: int) -> None:
        names = ("detail", "climate", "alerts", "procs")
        ui = self.panels_ui
        n = len(names)
        if key in (curses.KEY_UP, ord("k")):
            ui["cursor"] = (ui["cursor"] - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            ui["cursor"] = (ui["cursor"] + 1) % n
        elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            name = names[ui["cursor"]]
            self._set_panel(name, not getattr(self, f"show_{name}"))
            if name == "detail":
                self.cfg.set("panels_detail", self.show_detail)
        elif key == 27 or key == ord("q"):
            self.panels_ui = {}
            return
        else:
            return

    def _key_presets(self, key: int) -> None:
        names = list(self.cfg.presets)
        ui = self.presets_ui
        n = max(1, len(names))
        if key in (curses.KEY_UP, ord("k")):
            ui["cursor"] = (ui["cursor"] - 1) % n
        elif key in (curses.KEY_DOWN, ord("j")):
            ui["cursor"] = (ui["cursor"] + 1) % n
        elif key in (curses.KEY_ENTER, 10, 13) and names:
            self._apply_preset(names[ui["cursor"] % len(names)])
            self.presets_ui = {}
            return
        elif key == 27 or key == ord("q"):
            self.presets_ui = {}
            return

    def _apply_preset(self, name: str) -> None:
        pr = self.cfg.presets.get(name)
        if not pr:
            self._flash(f"preset {name!r} vanished")
            return
        if "layout" in pr:
            self.layout_mode = pr["layout"]
            self.cfg.set("layout", self.layout_mode)
        if "theme" in pr:
            self._apply_theme(pr["theme"], persist=True)
        if "graph_style" in pr:
            self.graph_style = pr["graph_style"]
            self.cfg.set("graph_style", self.graph_style)
        if "interval" in pr:
            self.interval = float(pr["interval"])
            self.cfg.set("interval", round(self.interval, 2))
        for pan in pr.get("panels", []):
            self._set_panel(pan, True)
        self._flash(f"preset: {name}")

    # ------------------------------------------------------------ drawing

    def draw(self, scr: Screen) -> None:
        W, H = scr.cols, scr.rows
        bg = self.theme.q("bg")
        self._zones = []          # rebuilt every frame; overlays register last
        scr.begin_frame(bg)
        if W < 60 or H < 12:
            scr.puts(2, H // 2, f"terminal too small ({W}x{H}) -- need >= 60x12",
                     self.theme.q("warn"), bg, bold=True)
            scr.flush()
            return
        agg = self._aggregate()
        layout.draw_header(scr, W, self.env, agg, self)
        rects = layout.plan_layout(W, H, len(self.indices), self.layout_mode,
                                   self.show_detail, climate=self.show_climate,
                                   procs=self.show_procs)
        for i, idx in enumerate(self.indices):
            if i < len(rects["cards"]):
                x, y, w, h = rects["cards"][i]
                layout.draw_gpu_card(scr, self, x, y, w, h, idx, selected=(idx == self.selected))
        d = rects["detail"]
        if d and self.selected in self.samples:
            layout.draw_detail(scr, self, d[0], d[1], d[2], d[3], self.selected)
        if rects.get("climate") and self.weather is not None:
            layout.draw_climate_panel(scr, self, *rects["climate"])
        if rects["proc"] and self.show_procs:
            layout.draw_processes(scr, self, *rects["proc"])
        if self.show_alerts and list(self.alerts_engine.alerts):
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
        if self.options_ui:
            overlays.draw_options(scr, W, H, self)
        if self.panels_ui:
            overlays.draw_panels(scr, W, H, self)
        if self.presets_ui:
            overlays.draw_presets(scr, W, H, self)
        if self.proc_detail_pid is not None:
            overlays.draw_process_detail(scr, W, H, self)
        if self.confirm is not None:
            overlays.draw_confirm(scr, W, H, self.confirm, self.theme)
        if self.regex_input is not None:
            overlays.draw_regex_input(scr, W, H, self.regex_input, self.theme)
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

    def handle_key(self, key: int, scr: Screen | None = None) -> None:
        # modal dispatch chain (single choke point; first match wins)
        if self.confirm is not None:
            self._confirm_step(key)
            return
        if self.regex_input is not None:
            self._regex_step(key)
            return
        if self.proc_detail_pid is not None:
            if key == 27 or key in (ord("q"), curses.KEY_ENTER, 10, 13):
                self.proc_detail_pid = None
            elif key in (curses.KEY_UP, ord("k")):
                self.proc_detail_scroll = max(0, self.proc_detail_scroll - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.proc_detail_scroll += 1
            return
        if self.options_ui:
            self._key_options(key)
            return
        if self.panels_ui:
            self._key_panels(key)
            return
        if self.presets_ui:
            self._key_presets(key)
            return
        if self.theme_ui:
            self._key_theme(key)
            return
        if self.show_help:
            self.show_help = False
            return
        if key in (curses.KEY_UP,):
            self._proc_step(-1)
            return
        if key in (curses.KEY_DOWN,):
            self._proc_step(1)
            return
        if key == curses.KEY_PPAGE:
            self._proc_page(-1)
            return
        if key == curses.KEY_NPAGE:
            self._proc_page(1)
            return
        if key == curses.KEY_HOME:
            self._proc_home_end(False)
            return
        if key == curses.KEY_END:
            self._proc_home_end(True)
            return
        if key == curses.KEY_ENTER or key in (10, 13):
            self._open_proc_detail()
            return
        self._key_normal(key, scr)

    def _key_normal(self, key: int, scr: Screen | None) -> None:
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
            elif c == "k":
                self._proc_step(-1)
            elif c == "j":
                self._proc_step(1)
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
                    if scr is not None:
                        scr.set_paint_bg(self.paint_bg_pref)
            elif c == "l":
                order: tuple[str, ...] = ("auto", "hero", "grid", "compact")
                if len(self.indices) > 1:
                    order = ("auto", "grid", "compact")  # hero renders one card only
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
            elif c == "v":
                self._toggle_tree()
            elif c == "/":
                self.regex_input = self._filter_re_src
            elif c == " ":
                p = self._selected_proc()
                if p is not None and p.pid in self.tagged:
                    self.tagged.discard(p.pid)
                elif p is not None:
                    self.tagged.add(p.pid)
            elif c in ("T", "K"):
                import signal as _signal

                self._request_signal(_signal.SIGTERM if c == "T" else _signal.SIGKILL)
            elif c == "O":
                self.options_ui = {"cursor": 0}
            elif c == "x":
                self.panels_ui = {"cursor": 0}
            elif c == "P":
                if self.cfg.presets:
                    self.presets_ui = {"cursor": 0}
                else:
                    self._flash("no presets -- add [presets.<name>] to config")
            elif c in self.preset_keys:
                self._apply_preset(self.preset_keys[c])
            elif c in "+=":
                self._step_interval(1.5)
            elif c == "-":
                self._step_interval(1 / 1.5)
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

    # ------------------------------------------------------------ mouse

    def _add_zone(self, x: int, y: int, w: int, h: int, kind: str, arg) -> None:
        """Register a clickable rect (inclusive box).  Later zones win:
        overlays register after the panels beneath them."""
        self._zones.append((x, y, x + max(0, w) - 1, y + max(0, h) - 1, kind, arg))

    def handle_mouse(self, mx: int, my: int, bstate: int) -> None:
        """Dispatch a mouse event.  Takes plain ints (no curses.getmouse())
        so tests can drive it with synthetic button states."""
        import curses as _c

        b1 = _c.BUTTON1_PRESSED
        b1d = getattr(_c, "BUTTON1_DOUBLE_CLICKED", 0)
        up = getattr(_c, "BUTTON4_PRESSED", 0)
        down = getattr(_c, "BUTTON5_PRESSED", 0)
        if (up or down) and (bstate & (up | down)):
            # wheel scrolls the process list
            steps = 3
            self._proc_step(steps if bstate & down else -steps)
            return
        click = bool(bstate & b1) or bool(b1d and bstate & b1d)
        if not click:
            return
        for (x0, y0, x1, y1, kind, arg) in reversed(self._zones):
            if x0 <= mx <= x1 and y0 <= my <= y1:
                if kind == "key":
                    self.handle_key(ord(arg), self._screen)
                elif kind == "gpu":
                    self.selected = arg
                elif kind == "procrow":
                    self.proc_cursor_pid = arg
                    if b1d and bstate & b1d:
                        self._open_proc_detail()
                elif kind == "menurow":
                    self._menu_click(arg, bool(b1d and bstate & b1d))
                return

    def _menu_click(self, index: int, activate: bool) -> None:
        if self.options_ui:
            self.options_ui["cursor"] = index
            if activate:
                self._key_options(curses.KEY_ENTER)
        elif self.panels_ui:
            self.panels_ui["cursor"] = index
            if activate:
                self._key_panels(curses.KEY_ENTER)
        elif self.presets_ui:
            self.presets_ui["cursor"] = index
            if activate:
                self._key_presets(curses.KEY_ENTER)
        elif self.theme_ui:
            self.theme_ui["cursor"] = index
            self._apply_theme(list(REGISTRY)[index], persist=False)
            self.theme_ui["preview"] = list(REGISTRY)[index]
            if activate:
                self._key_theme(curses.KEY_ENTER)

    # ------------------------------------------------------------ main loop

    def run(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.nodelay(True)
        stdscr.timeout(80)
        if self.mouse_enabled:
            try:
                curses.mousemask(
                    curses.BUTTON1_PRESSED
                    | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
                    | getattr(curses, "BUTTON4_PRESSED", 0)
                    | getattr(curses, "BUTTON5_PRESSED", 0)
                )
                curses.mouseinterval(0)
            except curses.error:
                pass  # degrade to keyboard-only (rare exotic terminals)
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
                if key == curses.KEY_MOUSE:
                    try:
                        _mid, _mx, _my, _mz, _mb = curses.getmouse()
                    except curses.error:
                        continue
                    self.handle_mouse(_mx, _my, _mb)
                    continue
                self.handle_key(key, scr)
        if self.recorder is not None:
            self.recorder.close()
        if self.exporter is not None:
            self.exporter.stop()


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        import os

        return str(os.getuid())


def make_exporter(args, cfg: Config):
    """Build + start the metrics exporter from --export / [export] config.
    Returns (Exporter | None, error_message | None).  Bind failure is
    non-fatal by design (flash/alert, never kill the TUI)."""
    ex_cfg = getattr(cfg, "export", None) or {}
    cli_export = getattr(args, "export", None) if args is not None else None
    if not (cli_export or ex_cfg.get("enabled")):
        return None, None
    from .exporter import Exporter, parse_export_spec

    try:
        if cli_export:
            host, port = parse_export_spec(cli_export)
        else:
            host = str(ex_cfg.get("host", "127.0.0.1"))
            port = int(ex_cfg.get("port", 9835))
    except ValueError as e:
        return None, f"exporter: {e}"
    exporter = Exporter(host, port, version=__version__,
                        expose_processes=bool(ex_cfg.get("expose_processes", True)))
    if not exporter.start():
        return None, f"exporter bind failed on {host}:{port}"
    return exporter, None


def run_tui(provider, cfg: Config, **kw) -> None:
    app = App(provider, cfg, **kw)
    curses.wrapper(app.run)


def make_provider(args) -> Any:
    if args.demo:
        return DemoProvider(only=args.only)
    if args.replay:
        return ReplayProvider(args.replay)
    return NvmlProvider(only=args.only)
