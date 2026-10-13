"""Overlays: theme picker (live preview), help window, process detail,
signal confirmation, regex filter input."""

from __future__ import annotations

from .cells import Screen
from .themes import REGISTRY, THEME_ORDER, Theme
from .widgets import fmt_bytes, panel_frame, sparkline

HELP_LINES = [
    ("q", "quit"),
    ("up/down j/k", "move process cursor"),
    ("pgup/pgdn home/end", "page / jump in process list"),
    ("enter", "process detail (cmdline, env, cwd, live graphs)"),
    ("space", "tag / untag process"),
    ("v", "tree <-> flat process view"),
    ("/", "regex filter (user, command, pid)"),
    ("T / K", "SIGTERM / SIGKILL selected process (type yes)"),
    ("t", "theme picker (live preview, Enter keeps, Esc reverts)"),
    ("m", "cycle theme instantly"),
    ("1-9 / <- ->", "select GPU"),
    ("d", "toggle detail panel"),
    ("p", "pause / resume sampling"),
    ("r", "reset session stats (energy, peaks, alerts)"),
    ("s", "cycle process sort (pid/user/mem/sm/cpu)"),
    ("f", "filter processes: all <-> my user"),
    ("F", "toggle Fahrenheit"),
    ("b", "toggle background painting"),
    ("l", "cycle layout auto -> hero -> grid -> compact"),
    ("w", "weather panel (climate + blower state)"),
    ("o", "manual blower toggle (switches to manual mode)"),
    ("c", "cycle cooling mode off -> dry_run -> auto -> manual"),
    ("a", "blower AUTO toggle -- on at GPU load, off 10 min after idle+cool"),
    ("+ / -", "sample interval"),
    ("?", "this help"),
]


def _box(scr: Screen, W: int, H: int, w: int, h: int, title: str, theme: Theme) -> tuple[int, int]:
    x = max(0, (W - w) // 2)
    y = max(0, (H - h) // 2)
    # dim backdrop
    bg = theme.q("bg")
    for yy in range(y, min(H, y + h)):
        for xx in range(x, min(W, x + w)):
            scr.put(xx, yy, " ", theme.q("fg"), bg)
    panel_frame(scr, x, y, w, h, title, theme, selected=True)
    return x, y


def draw_theme_picker(scr: Screen, W: int, H: int, cursor: int, theme: Theme,
                      preview_name: str, original_name: str) -> None:
    n = len(THEME_ORDER)
    w = 56
    h = min(n + 8, H - 4)
    x, y = _box(scr, W, H, w, h, "Theme", theme)
    visible = h - 7
    top = max(0, min(cursor - visible // 2, n - visible))
    scr.puts(x + 2, y + 1, "enter keep · esc revert · up/down or j/k move",
             theme.q("dim"), theme.q("bg"))
    for i in range(top, min(n, top + visible)):
        name = THEME_ORDER[i]
        t = REGISTRY[name]
        ry = y + 3 + (i - top)
        marker = ">" if i == cursor else " "
        fg = theme.q("border_sel") if i == cursor else theme.q("fg")
        scr.puts(x + 2, ry, marker, fg, theme.q("bg"), bold=True)
        label = name[:24]
        scr.puts(x + 4, ry, label, fg, theme.q("bg"),
                 bold=(name == preview_name), maxw=24)
        # swatch strip: 5 gradient steps + semantic trio
        sw = t.grad_q("grad", 5)
        for si, c in enumerate(sw):
            scr.put(x + 30 + si, ry, "█", c, theme.q("bg"))
        scr.put(x + 36, ry, "█", t.q("ok"), theme.q("bg"))
        scr.put(x + 37, ry, "█", t.q("warn"), theme.q("bg"))
        scr.put(x + 38, ry, "█", t.q("crit"), theme.q("bg"))
        scr.put(x + 40, ry, "█", t.q("bg"), t.q("bg"))
        tags = []
        if name == preview_name:
            tags.append("preview")
        if name == original_name:
            tags.append("saved")
        if tags:
            scr.puts(x + 42, ry, " ".join(tags), theme.q("key_hint"), theme.q("bg"), maxw=w - 44)
    foot = f"{cursor + 1}/{n}  {preview_name}"
    scr.puts(x + 2, y + h - 2, foot, theme.q("key_label"), theme.q("bg"))


def draw_help(scr: Screen, W: int, H: int, theme: Theme) -> None:
    w = 74
    h = len(HELP_LINES) + 4
    x, y = _box(scr, W, H, w, h, "Keys", theme)
    for i, (k, d) in enumerate(HELP_LINES):
        scr.puts(x + 2, y + 2 + i, k.ljust(12), theme.q("key_hint"), theme.q("bg"), bold=True)
        scr.puts(x + 15, y + 2 + i, d, theme.q("fg"), theme.q("bg"), maxw=w - 17)
    scr.puts(x + 2, y + h - 2, "esc/? close", theme.q("dim"), theme.q("bg"))


# ------------------------------------------------------------------ v0.4


def draw_process_detail(scr: Screen, W: int, H: int, app) -> None:
    """Full-canvas per-process screen: cmdline, cwd/exe, parent chain, env
    vars (scrollable), live SM% sparkline.  UI concept: nvitop (GPL-3.0),
    original implementation."""
    theme = app.theme
    bg = theme.q("bg")
    # the frozen-at-open row keeps the screen stable if the list re-sorts
    p = _frozen_proc(app)
    w = min(96, W - 4)
    h = min(H - 2, 30)
    x, y = _box(scr, W, H, w, h, f"Process {app.proc_detail_pid}", theme)
    if p is None:
        scr.puts(x + 2, y + 1, "process has exited", theme.q("dim"), bg)
        return
    r = y + 1
    user = getattr(p, "user", "?")
    scr.puts(x + 2, r, f"{user} · type {p.type} · GPU {p.gpu_index} · "
                       f"mem {fmt_bytes(p.mem_used)}", theme.q("fg"), bg)
    r += 1
    info = app.proc_detail_info
    cmdline = getattr(info, "cmdline", "") or p.command
    exe = getattr(info, "exe", "") or "-"
    cwd = getattr(info, "cwd", "") or "-"
    for line in _wrap(cmdline, w - 6)[:2]:
        scr.puts(x + 2, r, line, theme.q("accent"), bg, bold=True, maxw=w - 4)
        r += 1
    scr.puts(x + 2, r, f"exe {exe[: w - 10]}", theme.q("dim"), bg, maxw=w - 4)
    r += 1
    scr.puts(x + 2, r, f"cwd {cwd[: w - 10]}", theme.q("dim"), bg, maxw=w - 4)
    r += 1
    chain = _parent_chain(app)
    if chain:
        scr.puts(x + 2, r, "parents: " + " -> ".join(chain), theme.q("dim"), bg, maxw=w - 4)
        r += 1
    # live SM sparkline from the per-pid history ring
    hist = app.proc_hist.get(p.pid, {}).get("sm")
    if hist:
        scr.puts(x + 2, r, "SM%", theme.q("table_head"), bg, bold=True)
        spark_w = max(10, w - 12)
        sparkline(scr, x + 6, r, spark_w, list(hist), 100.0, theme)
        cur = list(hist)[-1]
        scr.puts(x + 6 + spark_w + 1, r, f"{cur:.0f}%", theme.q("fg"), bg)
        r += 1
    # env vars, scrollable
    scr.puts(x + 2, r, f"ENV ({len(app.proc_detail_env)})  j/k scroll",
             theme.q("table_head"), bg, bold=True)
    r += 1
    vis = max(1, y + h - r - 1)
    env = app.proc_detail_env
    if not env:
        scr.puts(x + 2, r, "(unreadable or empty)", theme.q("dim"), bg)
    else:
        scroll = max(0, min(app.proc_detail_scroll, max(0, len(env) - vis)))
        app.proc_detail_scroll = scroll
        for i in range(scroll, min(len(env), scroll + vis)):
            k, v = env[i]
            scr.puts(x + 2, r + (i - scroll), f"{k}={v}", theme.q("fg"), bg, maxw=w - 4)
    scr.puts(x + 1, y + h - 1, " esc close ", theme.q("key_hint"), bg)


def _frozen_proc(app):
    for p in app.proc_rows:
        if p.pid == app.proc_detail_pid:
            return p
    return None


def _parent_chain(app) -> list[str]:
    info = app.proc_detail_info
    if info is None:
        return []
    from ..procs import ancestors_of

    names = []
    table = app.proc_table or {}
    for pid in ancestors_of(info.pid, table):
        pi = table.get(pid)
        names.append(f"{pid} {(pi.cmdline[:32] if pi and pi.cmdline else '')}".strip())
    return names[:4]


def _wrap(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out or [""]


def draw_confirm(scr: Screen, W: int, H: int, confirm: dict, theme: Theme) -> None:
    """Typed-confirmation modal before a signal is sent (nvitop concept)."""
    w = min(80, W - 6)
    h = 7
    x, y = _box(scr, W, H, w, h, f"Send {confirm['label']}?", theme)
    scr.puts(x + 2, y + 1, f"pid {confirm['pid']}", theme.q("warn"), theme.q("bg"), bold=True)
    scr.puts(x + 2, y + 2, confirm["cmdline"], theme.q("fg"), theme.q("bg"), maxw=w - 4)
    scr.puts(x + 2, y + 3, 'type "yes" + enter (esc cancels):', theme.q("dim"), theme.q("bg"))
    scr.puts(x + 2, y + 4, "> " + confirm["typed"] + "_",
             theme.q("key_hint"), theme.q("bg"), bold=True)


def draw_regex_input(scr: Screen, W: int, H: int, value: str, theme: Theme) -> None:
    w = min(70, W - 6)
    h = 5
    x, y = _box(scr, W, H, w, h, "Regex filter", theme)
    scr.puts(x + 2, y + 1, "matches user, command, pid (case-insensitive)",
             theme.q("dim"), theme.q("bg"))
    scr.puts(x + 2, y + 2, "/" + value + "_", theme.q("fg"), theme.q("bg"), bold=True)
    scr.puts(x + 2, y + 3, "enter apply · empty clears · esc cancel",
             theme.q("key_label"), theme.q("bg"))


# ------------------------------------------------------------- btop UX pack


def _menu(scr: Screen, W: int, H: int, title: str, theme: Theme,
          rows: list[tuple[str, str]], cursor: int, foot: str,
          app=None) -> tuple[int, int, int, int]:
    """Generic btop-style menu box: two-column rows (label, value).
    Returns the content geometry for zone registration."""
    n = len(rows)
    w = min(58, W - 6)
    h = min(n + 6, H - 4)
    x, y = _box(scr, W, H, w, h, title, theme)
    scr.puts(x + 2, y + 1, "up/down or j/k · enter/<- -> cycle · esc close",
             theme.q("dim"), theme.q("bg"))
    visible = h - 5
    top = max(0, min(cursor - visible // 2, max(0, n - visible)))
    for i in range(top, min(n, top + visible)):
        label, value = rows[i]
        ry = y + 3 + (i - top)
        sel = i == cursor
        fg = theme.q("border_sel") if sel else theme.q("fg")
        scr.puts(x + 2, ry, ">" if sel else " ", fg, theme.q("bg"), bold=True)
        scr.puts(x + 4, ry, label, fg, theme.q("bg"), maxw=w - 22)
        scr.puts(x + w - 16, ry, value, theme.q("key_hint") if sel else theme.q("dim"),
                 theme.q("bg"), bold=sel, maxw=14)
        if app is not None:
            app._add_zone(x + 2, ry, w - 4, 1, "menurow", i)
    scr.puts(x + 2, y + h - 2, foot, theme.q("key_label"), theme.q("bg"))
    return x, y, w, h


def draw_options(scr: Screen, W: int, H: int, app) -> None:
    rows = [(r["label"], r["value"]) for r in app._options_rows()]
    _menu(scr, W, H, "Options", app.theme, rows, app.options_ui["cursor"],
          "settings persist to the config file", app=app)


def draw_panels(scr: Screen, W: int, H: int, app) -> None:
    rows = []
    for n in ("detail", "climate", "alerts", "procs"):
        on = getattr(app, f"show_{n}")
        rows.append((n, "on " if on else "off"))
    _menu(scr, W, H, "Panels", app.theme, rows, app.panels_ui["cursor"],
          "enter/space toggles · state is saved", app=app)


def draw_presets(scr: Screen, W: int, H: int, app) -> None:
    rows = []
    for name, pr in app.cfg.presets.items():
        bits = []
        for k in ("layout", "theme", "graph_style", "interval"):
            if k in pr:
                bits.append(f"{pr[k]}")
        if pr.get("key"):
            bits.append(f"key {pr['key']}")
        rows.append((name, " · ".join(bits)[:14]))
    _menu(scr, W, H, "Presets", app.theme, rows, app.presets_ui["cursor"],
          "enter applies · defined as [presets.<name>]", app=app)
