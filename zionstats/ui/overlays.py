"""Overlays: theme picker (live preview), help window."""

from __future__ import annotations

from .cells import Screen
from .themes import REGISTRY, THEME_ORDER, Theme
from .widgets import panel_frame

HELP_LINES = [
    ("q", "quit"),
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
