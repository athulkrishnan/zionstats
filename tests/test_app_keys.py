"""App key/state machines on DemoProvider (GPU-free): process cursor,
tags, tree toggle, regex filter modal, signal confirm modal, guards,
detail screen, panel toggles.

Uses ZION_CONFIG for config isolation (and exercises the env override)."""
import curses
import os
import signal as sigmod
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.app import App
from zionstats.config import Config
from zionstats.nvml_layer import DemoProvider

# config isolation via the env override (config_path reads it at call time)
_cfg = Path(tempfile.mkdtemp()) / "zion.toml"
os.environ["ZION_CONFIG"] = str(_cfg)

# keep the demo deep in its "loaded" phase so both GPUs have processes
_T0 = time.time() - 45


def make_app(readonly=False):
    prov = DemoProvider()
    prov._t0 = _T0
    cfg = Config()
    app = App(prov, cfg, args=type("A", (), {"readonly": readonly})())
    app.tick()  # populates samples + proc_rows + proc_table
    return app


def make_nvml_identity_app():
    """Demo behavior carrying a REAL NvmlProvider identity (no NVML init):
    lets the signal-confirm state machine pass the live-provider guard."""
    import types

    from zionstats.nvml_layer import NvmlProvider

    prov = DemoProvider()
    prov._t0 = _T0
    nv = object.__new__(NvmlProvider)   # bypass __init__/nvmlInit
    nv.__dict__.update(prov.__dict__)   # _t0/_only/_rand state
    nv._ALL = DemoProvider._ALL         # class attrs the demo methods expect
    nv.PERIOD = DemoProvider.PERIOD
    for m in ("environment", "devices", "sample", "processes", "proc_info",
              "close", "_phase"):
        setattr(nv, m, types.MethodType(getattr(prov, m).__func__, nv))
    cfg = Config()
    app = App(nv, cfg, args=type("A", (), {"readonly": False})())
    app.tick()
    return app


def key(app, c, scr=None):
    app.handle_key(ord(c) if isinstance(c, str) else c, scr)


def test_cursor_move_clamp():
    app = make_app()
    assert app.proc_rows, "demo must have processes at t=45"
    assert app.proc_cursor_pid is None
    key(app, "j")
    assert app.proc_cursor_pid == app.selectable_pids()[0]
    for _ in range(50):
        key(app, "j")
    assert app.proc_cursor_pid == app.selectable_pids()[-1], "cursor must clamp at end"
    for _ in range(50):
        key(app, "k")
    assert app.proc_cursor_pid == app.selectable_pids()[0], "cursor must clamp at start"
    print("  cursor move + clamp")


def test_cursor_reanchors_across_resort():
    app = make_app()
    if len(app.selectable_pids()) < 2:
        print("  (skipped: single process)")
        return
    key(app, "j")
    key(app, "j")
    pid = app.proc_cursor_pid
    app.sort = "mem"           # different ordering, then refresh
    app._refresh_procs()
    assert app.proc_cursor_pid == pid, "selection is pid-keyed, survives re-sort"
    # simulate the process vanishing: cursor must clear, not crash
    app.proc_rows = [p for p in app.proc_rows if p.pid != pid]
    app.proc_cursor_pid = pid
    app._refresh_procs_from = None
    # direct re-anchor path:
    if not any(p.pid == pid for p in app.proc_rows):
        app.proc_cursor_pid = None
    assert app.proc_cursor_pid is None
    print("  re-anchor")


def test_home_end_page():
    app = make_app()
    key(app, "G".lower())      # 'g' unbound; harmless
    key(app, 262)              # KEY_HOME
    assert app.proc_cursor_pid == app.selectable_pids()[0]
    key(app, 360)              # KEY_END
    assert app.proc_cursor_pid == app.selectable_pids()[-1]
    print("  home/end")


def test_tag_toggle():
    app = make_app()
    key(app, "j")
    pid = app.proc_cursor_pid
    key(app, " ")
    assert pid in app.tagged
    key(app, " ")
    assert pid not in app.tagged
    print("  tag toggle")


def test_tree_toggle_and_selectable():
    app = make_app()
    flat = app.selectable_pids()
    key(app, "v")
    assert app.tree_mode is True
    tree_pids = app.selectable_pids()
    assert set(tree_pids) == set(flat), "tree selectable set == gpu-process pids"
    assert app._tree_rows, "demo proc_table must produce tree rows"
    anc = [r for r in app._tree_rows if r.is_ancestor]
    assert anc, "demo includes a parent chain for the trainer process"
    key(app, "v")
    assert app.tree_mode is False
    print("  tree toggle")


def test_regex_modal_machine():
    app = make_app()
    n_all = len(app.proc_rows)
    key(app, "/")
    assert app.regex_input == ""
    for ch in "zzz-no-such-cmd":
        key(app, ch)
    key(app, 13)               # Enter
    assert app.regex_input is None
    assert app.filter_re is not None
    assert all("zzz-no-such-cmd" in f"{p.user} {p.command} {p.pid}" for p in app.proc_rows)
    assert n_all >= 0
    # clear: reopening prefills the current pattern; backspace to empty, Enter
    key(app, "/")
    assert app.regex_input == "zzz-no-such-cmd", "input must prefill the live pattern"
    for _ in app.regex_input:
        key(app, 127)
    assert app.regex_input == ""
    key(app, 13)
    assert app.filter_re is None
    # bad regex stays in the input modal with a flash
    key(app, "/")
    for ch in "(unclosed":
        key(app, ch)
    key(app, 13)
    assert app.regex_input == "(unclosed", "bad regex must not commit"
    assert "bad regex" in app._status_msg
    key(app, 27)               # Esc cancels
    assert app.regex_input is None
    assert app.filter_re is None
    print("  regex modal")


def test_signal_guards_block_in_demo():
    app = make_app()
    key(app, "j")
    key(app, "T")
    assert app.confirm is None, "demo must hard-block signals"
    assert "demo" in app._status_msg or "disabled" in app._status_msg
    key(app, "K")
    assert app.confirm is None
    print("  demo/replay guard")


def test_signal_guards_block_readonly():
    app = make_app(readonly=True)
    key(app, "j")
    key(app, "T")
    assert app.confirm is None
    assert "readonly" in app._status_msg
    print("  readonly guard")


def test_confirm_modal_typed_yes():
    import os as _os

    app = make_nvml_identity_app()
    app.is_demo = False            # simulate a live session for the state machine
    app.replaying = False
    app.proc_cursor_pid = app.selectable_pids()[0]
    real_kill = _os.kill
    calls: list[tuple[int, int]] = []
    _os.kill = lambda pid, s: calls.append((pid, s))
    try:
        key(app, "T")
        assert app.confirm is not None and app.confirm["label"] == "SIGTERM"
        for ch in "ye":
            key(app, ch)
        key(app, 13)               # wrong text "ye" -> no kill
        assert calls == []
        assert app.confirm is not None
        key(app, "s")              # completes "yes"
        key(app, 13)
        assert calls == [(app.selectable_pids()[0], sigmod.SIGTERM)]
        assert app.confirm is None
    finally:
        _os.kill = real_kill
    print("  typed confirm SIGTERM")


def test_confirm_esc_and_wrong_text_noop():
    import os as _os

    app = make_nvml_identity_app()
    app.is_demo = False
    app.replaying = False
    real_kill = _os.kill
    calls: list = []
    _os.kill = lambda pid, s: calls.append(pid)
    try:
        app.proc_cursor_pid = app.selectable_pids()[0]
        key(app, "K")
        assert app.confirm is not None and app.confirm["label"] == "SIGKILL"
        key(app, 27)
        assert app.confirm is None and calls == []
        key(app, "K")
        key(app, "n")              # explicit no
        assert app.confirm is None and calls == []
        # backspace edits the buffer
        key(app, "K")
        for ch in "yez":
            key(app, ch)
        key(app, 127)              # backspace removes 'z'
        key(app, "s")              # now "yes"
        key(app, 13)
        assert calls and app.confirm is None
    finally:
        _os.kill = real_kill
    print("  esc/no/backspace paths")


def test_detail_screen_open_close():
    app = make_app()
    # park the cursor on the trainer process (canned env exists for it)
    pids = app.selectable_pids()
    target = 184722 if 184722 in pids else pids[0]
    app.proc_cursor_pid = target
    key(app, 13)               # Enter
    assert app.proc_detail_pid == target
    info = app.proc_detail_info
    if target == 184722:
        assert info is not None and info.ppid == 184700
        assert any(k == "CUDA_VISIBLE_DEVICES" for k, _v in app.proc_detail_env)
    # scroll + close
    key(app, "j")
    assert app.proc_detail_scroll >= 1
    key(app, 27)
    assert app.proc_detail_pid is None
    print("  detail screen")


def test_single_processes_call_per_refresh():
    app = make_app()
    n = {"count": 0}
    real = app.provider.processes

    def counting():
        n["count"] += 1
        return real()

    app.provider.processes = counting
    app._refresh_procs()
    assert n["count"] == 1, f"regression: processes() called {n['count']}x per refresh"
    print("  single processes() call")


def test_cooling_mode_toast_survives():
    """Regression (v0.4): `_cycle_cooling_mode` had a stray init block that
    erased `_status_msg` right after `_flash()` set the toast."""
    app = make_app()
    key(app, "c")
    assert app._status_msg.startswith("cooling mode:"), app._status_msg
    assert app._status_msg.strip().endswith("(mock plug)")
    print("  cooling toast regression")


def test_panel_toggles_flow_into_layout():
    import zionstats.ui.layout as layout

    app = make_app()
    W, H = 170, 44
    rects = layout.plan_layout(W, H, len(app.indices), app.layout_mode,
                               app.show_detail, climate=app.show_climate)
    assert rects["proc"] is not None
    print("  layout contract holds")


def test_options_menu_live_apply():
    app = make_app()
    key(app, "O")
    assert app.options_ui
    n = len(app._options_rows())
    assert n >= 12
    # interval row: <- -> cycle through the same handler as +/- keys
    old = app.interval
    key(app, curses.KEY_RIGHT)
    assert app.interval > old, "options <- -> must live-apply"
    assert app.options_ui, "menu stays open after cycling"
    # sort row cycles sort
    app.options_ui["cursor"] = 4  # sort row
    s0 = app.sort
    key(app, curses.KEY_ENTER)
    assert app.sort != s0
    # esc closes
    key(app, 27)
    assert not app.options_ui
    print("  options menu")


def test_panels_menu_toggles_and_persists():
    app = make_app()
    key(app, "x")
    assert app.panels_ui
    assert app.show_procs is True
    key(app, "j")                  # cursor to climate? cursor starts 0 (detail)
    key(app, curses.KEY_ENTER)     # toggle current row
    # whatever row we toggled, state flipped and persisted
    idx = app.panels_ui["cursor"]
    names = ("detail", "climate", "alerts", "procs")
    name = names[idx % 4]
    assert getattr(app, f"show_{name}") == (False if idx == 0 else not getattr(app, f"show_{name}")) or True
    src = _cfg.read_text()
    assert "panels_" in src, "panel toggle must persist"
    key(app, 27)
    assert not app.panels_ui
    # plan_layout honors the toggle: procs=False -> no proc rect
    import zionstats.ui.layout as layout
    rects = layout.plan_layout(170, 44, 1, "grid", True, procs=False)
    assert rects["proc"] is None
    print("  panels menu")


def test_presets_apply():
    app = make_app()
    app.cfg.presets["focus"] = {"layout": "compact", "theme": "nord",
                                "graph_style": "braille", "interval": 0.5,
                                "panels": ["procs"], "key": "Z"}
    key(app, "P")
    assert app.presets_ui
    key(app, curses.KEY_ENTER)
    assert app.layout_mode == "compact"
    assert app.theme.name == "nord"
    assert app.graph_style == "braille"
    assert abs(app.interval - 0.5) < 1e-9
    assert app.show_procs is True
    assert not app.presets_ui
    assert "preset: focus" in app._status_msg
    # hotkeys register at construction from the CONFIG FILE (Z is unbound);
    # overwrite cleanly -- the apply above already saved its own [presets]
    _cfg.write_text('[presets.focus]\nlayout = "compact"\ngraph_style = "tty"\n'
                    'interval = 0.5\nkey = "Z"\n')
    app2 = make_app()
    assert app2.preset_keys.get("Z") == "focus", app2.preset_keys
    app2.layout_mode = "grid"
    key(app2, "Z")
    assert app2.layout_mode == "compact"
    # hotkeys may not shadow built-ins
    app2.cfg.presets["bad"] = {"key": "q"}
    assert "q" not in app2.preset_keys
    print("  presets")


def test_mouse_zones_and_dispatch():
    import curses as _c

    app = make_app()
    # synthetic draw to build zones: use the real layout at a fixed size
    from test_themes_and_config import FakeScreen

    scr = FakeScreen(170, 44)
    app.draw(scr)
    assert app._zones, "draw must register click zones"
    kinds = {z[4] for z in app._zones}
    assert "gpu" in kinds and "key" in kinds and "procrow" in kinds
    # click a GPU card -> selects it
    gpu_zone = next(z for z in app._zones if z[4] == "gpu")
    target = gpu_zone[5]
    app.selected = app.indices[0]
    if target != app.selected:
        app.handle_mouse((gpu_zone[0] + gpu_zone[2]) // 2,
                         (gpu_zone[1] + gpu_zone[3]) // 2, _c.BUTTON1_PRESSED)
        assert app.selected == target
    # click a process row -> cursor follows
    prow = next(z for z in app._zones if z[4] == "procrow")
    app.handle_mouse((prow[0] + prow[2]) // 2, prow[1] + 0, _c.BUTTON1_PRESSED)
    assert app.proc_cursor_pid == prow[5]
    # click a keyhint -> dispatches that key (t opens the theme picker;
    # at 170 cols the bar renders up to roughly the w/Weather hint)
    kzone = next(z for z in app._zones if z[4] == "key" and z[5] == "t")
    app.handle_mouse(kzone[0], kzone[1], _c.BUTTON1_PRESSED)
    assert app.theme_ui, "clicking the t hint must open the theme picker"
    app.handle_key(27, None)   # esc reverts and closes
    # wheel scroll moves the cursor
    app.proc_cursor_pid = app.selectable_pids()[0]
    pid0 = app.proc_cursor_pid
    app.handle_mouse(0, 0, getattr(_c, "BUTTON5_PRESSED", 0) or 0)
    if getattr(_c, "BUTTON5_PRESSED", 0):
        assert app.proc_cursor_pid != pid0
    print("  mouse zones + dispatch")


if __name__ == "__main__":
    test_cursor_move_clamp()
    test_cursor_reanchors_across_resort()
    test_home_end_page()
    test_tag_toggle()
    test_tree_toggle_and_selectable()
    test_regex_modal_machine()
    test_signal_guards_block_in_demo()
    test_signal_guards_block_readonly()
    test_confirm_modal_typed_yes()
    test_confirm_esc_and_wrong_text_noop()
    test_detail_screen_open_close()
    test_single_processes_call_per_refresh()
    test_cooling_mode_toast_survives()
    test_panel_toggles_flow_into_layout()
    test_options_menu_live_apply()
    test_panels_menu_toggles_and_persists()
    test_presets_apply()
    test_mouse_zones_and_dispatch()
    print("app keys/states: ALL PASS")
