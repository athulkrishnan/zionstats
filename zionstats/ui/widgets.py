"""btop-style widgets: rounded panels, gradient braille graphs, block bars,
chips, tables, keyhint bars.  All draw straight into the cell Screen."""

from __future__ import annotations

from collections.abc import Sequence

from .cells import Screen
from .themes import Theme

# ------------------------------------------------------------------ braille

_BRAILLE_BASE = 0x2800
# bit for (col 0/1, row 0..3): col0 rows are dots 1,2,3,7 ; col1 rows dots 4,5,6,8
_COL_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
_BLOCKS = " ▁▂▃▄▅▆▇█"


def graph_braille(
    scr: Screen,
    x: int,
    y: int,
    w: int,
    h: int,
    data: Sequence[float],
    vmax: float,
    theme: Theme,
    which: str = "grad",
    fg_dim: int | None = None,
    hlines: list[tuple[float, int]] | None = None,
    floor: float = 0.0,
) -> None:
    """Multi-row braille area graph, column-colored by intensity gradient.

    `data` oldest-first; the most recent w*2 values are shown (2 per cell).
    `hlines` stamps a solid horizontal rule at each (fraction, color) --
    single braille dot-rows read as broken texture at small font sizes.
    `floor` lifts every column (0..1) so flat/idle series render as a
    continuous trace line instead of vanishing into invisible single dots.
    """
    if w <= 0 or h <= 0:
        return
    rows = h * 4
    n = w * 2
    vals = list(data)[-n:]
    if len(vals) < n:
        vals = [0.0] * (n - len(vals)) + vals
    if vmax <= 0:
        vmax = 1.0

    grid: list[list[list[int]]] = [[[0, 0] for _ in range(w)] for _ in range(h)]
    col_level = [0.0] * n
    floor = max(0.0, min(0.25, floor))
    for ci, v in enumerate(vals):
        frac = 0.0 if vmax <= 0 else max(0.0, min(1.0, v / vmax))
        frac = floor + (1.0 - floor) * frac
        col_level[ci] = frac
        filled = max(1, round(frac * rows))
        gx, cx = divmod(ci, 2)
        for r_global in range(rows - filled, rows):
            gy, r = divmod(r_global, 4)
            grid[gy][gx][cx] |= _COL_BITS[cx][r]

    # render data dots
    for gy in range(h):
        for gx in range(w):
            bits = grid[gy][gx][0] | grid[gy][gx][1]
            if bits == 0:
                scr.put(x + gx, y + gy, " ", theme.q("dim"), theme.q("bg"))
                continue
            i1 = min(gx * 2 + 1, n - 1)
            peak = max(col_level[gx * 2], col_level[i1])
            color = theme.level_color(which, peak) if fg_dim is None else fg_dim
            scr.put(x + gx, y + gy, chr(_BRAILLE_BASE + bits), color, theme.q("bg"))

    # solid threshold rules stamped on top, Grafana-style
    for hf, hc in hlines or []:
        r_global = rows - max(1, min(rows, round(max(0.0, min(1.0, hf)) * rows)))
        gy, _r = divmod(r_global, 4)
        for gx in range(w):
            scr.put(x + gx, y + gy, "─", hc or theme.q("warn"), theme.q("bg"))


def graph(
    scr: Screen,
    style: str,
    x: int,
    y: int,
    w: int,
    h: int,
    data,
    vmax: float,
    theme: Theme,
    which: str = "grad",
    hlines=None,
    floor: float = 0.0,
) -> None:
    """Dispatcher: one call site per graph, style chosen at startup."""
    if style == "btop":
        graph_btop(scr, x, y, w, h, data, vmax, theme, which, hlines=hlines)
    elif style == "tty":
        graph_tty(scr, x, y, w, h, data, vmax, theme, which, hlines=hlines)
    elif style == "braille":
        graph_braille(scr, x, y, w, h, data, vmax, theme, which, hlines=hlines, floor=floor)
    elif style == "ascii":
        graph_ascii(scr, x, y, w, h, data, vmax, theme, which, hlines=hlines, floor=floor)
    else:
        graph_blocks(scr, x, y, w, h, data, vmax, theme, which, hlines=hlines, floor=floor)



# ------------------------------------------------------------------ btop engine
# Ported from aristocratos/btop src/btop_draw.cpp (Apache-2.0)
# Copyright (C) 2020-present Aristocratos.  https://github.com/aristocratos/btop
# Modifications for zionstats: coloring applied per row from a 101-step theme
# gradient (top row = hot end) instead of btop's computed graph colors;
# threshold rules stamped only on blank cells; py3.9 floor.
# Each cell encodes the transition between the previous sample and the current
# one: per row band both values quantize to sub-levels 0..4 and the glyph is
# table[last * 5 + current].

_BLOCK_UP = [
    " ", "▗", "▗", "▐", "▐",
    "▖", "▄", "▄", "▟", "▟",
    "▖", "▄", "▄", "▟", "▟",
    "▌", "▙", "▙", "█", "█",
    "▌", "▙", "▙", "█", "█",
]
_BLOCK_DOWN = [
    " ", "▝", "▝", "▐", "▐",
    "▘", "▀", "▀", "▜", "▜",
    "▘", "▀", "▀", "▜", "▜",
    "▌", "▛", "▛", "█", "█",
    "▌", "▛", "▛", "█", "█",
]
_TTY_UP = [
    " ", "░", "░", "▒", "▒",
    "░", "░", "▒", "▒", "█",
    "░", "▒", "▒", "▒", "█",
    "▒", "▒", "▒", "█", "█",
    "▒", "█", "█", "█", "█",
]
_TTY_DOWN = _TTY_UP


def _quant(value_pct: float, cur_high: int, cur_low: int, clamp_min: int) -> int:
    if value_pct >= cur_high:
        return 4
    if value_pct <= cur_low:
        return clamp_min
    return max(clamp_min, min(4, round((value_pct - cur_low) * 4 / (cur_high - cur_low) + 0.1)))


def _graph_transition(
    scr: Screen, x: int, y: int, w: int, h: int, data, vmax: float,
    theme: Theme, which: str, table: list[str], no_zero: bool = True,
    hlines=None,
) -> None:
    if w <= 0 or h <= 0:
        return
    n = w + 1                       # cells need value pairs -> w+1 samples
    vals = list(data)[-n:]
    if len(vals) < n:
        vals = [0.0] * (n - len(vals)) + vals
    vmax = vmax if vmax > 0 else 1.0
    pct = [max(0.0, min(100.0, v / vmax * 100.0)) for v in vals]

    grad = theme.grad_q(which, 101)
    bg = theme.q("bg")
    dim = theme.q("dim")
    blanks = set()
    for gy in range(h):
        row_color = grad[100 - (gy * 100 // max(1, h))]
        cur_high = 100 if h == 1 else round(100.0 * (h - gy) / h)
        cur_low = 0 if h == 1 else round(100.0 * (h - (gy + 1)) / h)
        clamp_min = 1 if (no_zero and gy == h - 1) else 0
        for gx in range(w):
            q_last = _quant(pct[gx], cur_high, cur_low, clamp_min)
            q_cur = _quant(pct[gx + 1], cur_high, cur_low, clamp_min)
            ch = table[q_last * 5 + q_cur]
            if ch == " ":
                blanks.add((gx, gy))
                scr.put(x + gx, y + gy, " ", dim, bg)
            else:
                scr.put(x + gx, y + gy, ch, row_color, bg)
    # threshold rules: dashed, only over blank cells (never cut through data)
    for hf, hc in hlines or []:
        r = h - max(1, min(h, round(max(0.0, min(1.0, hf)) * h)))
        for gx in range(w):
            if (gx, r) in blanks:
                scr.put(x + gx, y + r, "╌", hc or theme.q("warn"), bg)


def graph_btop(scr, x, y, w, h, data, vmax, theme, which="grad", hlines=None, floor=True):
    _graph_transition(scr, x, y, w, h, data, vmax, theme, which, _BLOCK_UP, no_zero=floor, hlines=hlines)


def graph_tty(scr, x, y, w, h, data, vmax, theme, which="grad", hlines=None, floor=True):
    _graph_transition(scr, x, y, w, h, data, vmax, theme, which, _TTY_UP, no_zero=floor, hlines=hlines)


def graph_blocks(
    scr: Screen,
    x: int,
    y: int,
    w: int,
    h: int,
    data,
    vmax: float,
    theme: Theme,
    which: str = "grad",
    hlines=None,
    floor: float = 0.0,
) -> None:
    """Solid half-block area graph (btop tty-block style): each cell stacks
    bottom + top sub-rows as ▄ / ▀ / █.  Renders solidly in every font."""
    if w <= 0 or h <= 0:
        return
    sub = h * 2
    vals = list(data)[-w:]
    if len(vals) < w:
        vals = [0.0] * (w - len(vals)) + vals
    vmax = vmax if vmax > 0 else 1.0
    floor = max(0.0, min(0.25, floor))
    bg = theme.q("bg")
    for gx, v in enumerate(vals):
        f = 0.0 if vmax <= 0 else max(0.0, min(1.0, v / vmax))
        f = floor + (1.0 - floor) * f
        filled = max(1, round(f * sub))
        color = theme.level_color(which, f)
        for gy in range(h):
            top_i, bot_i = gy * 2, gy * 2 + 1   # sub-row indices from the TOP
            top_on = top_i >= sub - filled      # filled sub-rows stack from the BOTTOM
            bot_on = bot_i >= sub - filled
            ch = "█" if (top_on and bot_on) else ("▀" if top_on else ("▄" if bot_on else " "))
            if ch == " ":
                scr.put(x + gx, y + gy, " ", theme.q("dim"), bg)
            else:
                scr.put(x + gx, y + gy, ch, color, bg)
    for hf, hc in hlines or []:
        r = sub - max(1, min(sub, round(max(0.0, min(1.0, hf)) * sub)))
        gy = r // 2
        for gx in range(w):
            scr.put(x + gx, y + gy, "─", hc or theme.q("warn"), bg)


def graph_ascii(
    scr: Screen,
    x: int,
    y: int,
    w: int,
    h: int,
    data,
    vmax: float,
    theme: Theme,
    which: str = "grad",
    hlines=None,
    floor: float = 0.0,
) -> None:
    """ASCII fallback: '#' area, '+' current level, '.' baseline."""
    if w <= 0 or h <= 0:
        return
    vals = list(data)[-w:]
    if len(vals) < w:
        vals = [0.0] * (w - len(vals)) + vals
    vmax = vmax if vmax > 0 else 1.0
    floor = max(0.0, min(0.25, floor))
    bg = theme.q("bg")
    dim = theme.q("dim")
    for gx, v in enumerate(vals):
        f = 0.0 if vmax <= 0 else max(0.0, min(1.0, v / vmax))
        f = floor + (1.0 - floor) * f
        level = max(1, round(f * h))
        color = theme.level_color(which, f)
        for gy in range(h):
            depth = h - 1 - gy          # 0 at bottom
            if depth > level:
                scr.put(x + gx, y + gy, " ", dim, bg)
            elif depth == level:
                scr.put(x + gx, y + gy, "+", color, bg, bold=True)
            else:
                scr.put(x + gx, y + gy, "#", color, bg)
    for hf, hc in hlines or []:
        r = h - max(1, min(h, round(max(0.0, min(1.0, hf)) * h)))
        for gx in range(w):
            scr.put(x + gx, y + r, "-", hc or theme.q("warn"), bg)


def sparkline(
    scr: Screen,
    x: int,
    y: int,
    w: int,
    data: Sequence[float],
    vmax: float,
    theme: Theme,
    which: str = "grad",
) -> None:
    """Single-row block sparkline, colored by the latest value."""
    vals = list(data)[-w:]
    if len(vals) < w:
        vals = [0.0] * (w - len(vals)) + vals
    if vmax <= 0:
        vmax = 1.0
    for i, v in enumerate(vals):
        frac = max(0.0, min(1.0, v / vmax))
        bi = int(frac * (len(_BLOCKS) - 1) + 0.5)
        color = theme.level_color(which, frac)
        scr.put(x + i, y, _BLOCKS[bi], color, theme.q("bg"))


# ------------------------------------------------------------------ bars


def hbar(
    scr: Screen,
    x: int,
    y: int,
    w: int,
    frac: float,
    theme: Theme,
    which: str | None = "grad",
    color: int | None = None,
    ticks: list[tuple[float, int]] | None = None,
) -> None:
    """btop-style bar:  bookend | fill with fractional block | bookend.

    `ticks` are (fraction, color256) markers drawn over the fill.
    """
    if w < 3:
        return
    frac = max(0.0, min(1.0, frac))
    inner = w - 2
    scr.put(x, y, "▕", theme.q("dim"), theme.q("bg"))
    scr.put(x + w - 1, y, "▏", theme.q("dim"), theme.q("bg"))
    fill = frac * inner
    full = int(fill)
    grad = theme.grad_q(which or "grad", 101)  # btop Meter: gradient along width
    for i in range(full):
        c = color if color is not None else grad[min(100, int((i + 1) * 100 / max(1, inner)))]
        scr.put(x + 1 + i, y, "█", c, theme.q("bg"))

    if full < inner and fill > full + 0.01:
        bi = int((fill - full) * (len(_BLOCKS) - 1) + 0.5)
        tip_c = color if color is not None else grad[min(100, int(fill * 100 / max(1, inner)) + 1)]
        scr.put(x + 1 + full, y, _BLOCKS[bi], tip_c, theme.q("bg"))
    for i in range(full + 1 if fill > full + 0.01 else full, inner):
        scr.put(x + 1 + i, y, "─", theme.q("bar_track"), theme.q("bg"))
    for tf, tc in ticks or []:
        ti = int(tf * inner)
        if 0 <= ti < inner:
            scr.put(x + 1 + ti, y, "┃", tc, theme.q("bg"))


# ------------------------------------------------------------------ panel


def panel_frame(
    scr: Screen,
    x: int,
    y: int,
    w: int,
    h: int,
    title: str = "",
    theme: Theme = None,  # type: ignore[assignment]
    selected: bool = False,
    right: str = "",
) -> None:
    if w < 4 or h < 2:
        return
    bcol = theme.q("border_sel") if selected else theme.q("border")
    bg = theme.q("bg")
    scr.put(x, y, "╭", bcol, bg)
    scr.put(x + w - 1, y, "╮", bcol, bg)
    scr.put(x, y + h - 1, "╰", bcol, bg)
    scr.put(x + w - 1, y + h - 1, "╯", bcol, bg)
    for i in range(1, w - 1):
        scr.put(x + i, y, "─", bcol, bg)
        scr.put(x + i, y + h - 1, "─", bcol, bg)
    for j in range(1, h - 1):
        scr.put(x, y + j, "│", bcol, bg)
        scr.put(x + w - 1, y + j, "│", bcol, bg)
    if title:
        t = f" {title} "
        tcol = theme.q("border_sel" if selected else "title")
        scr.puts(x + 2, y, t, tcol, bg, bold=True, maxw=w - 4)
        if len(t) + 2 < w - 2:
            scr.put(x + 2 + len(t), y, "─", bcol, bg)
    if right:
        scr.puts(x + w - 2 - len(right), y, right, theme.q("dim"), bg, maxw=w - 4)


# ------------------------------------------------------------------ chips


def chip(scr: Screen, x: int, y: int, text: str, color: str, theme: Theme) -> int:
    fg = theme.q("bg")
    bg = theme.q(color)
    s = f" {text} "
    scr.puts(x, y, s, fg, bg, bold=True)
    return len(s)


# ------------------------------------------------------------------ table


class Table:
    """Column spec: (title, width, align) where align in '<' '>' '^'."""

    def __init__(self, cols: list[tuple[str, int, str]]) -> None:
        self.cols = cols

    def draw(
        self,
        scr: Screen,
        x: int,
        y: int,
        w: int,
        rows: list[list[str]],
        theme: Theme,
        n_visible: int,
        top: int = 0,
        color_rows: list[list[int | None]] | None = None,
        cursor: int | None = None,
        dim_rows: set[int] | None = None,
    ) -> None:
        bg = theme.q("bg")
        cx = x
        head_colors = theme.q("table_head")
        for (title, width, align), _ci in zip(self.cols, range(len(self.cols))):
            cell = _align(title, width, align)
            scr.puts(cx, y, cell, head_colors, bg, bold=True, maxw=w - (cx - x))
            cx += width + 1
            if cx - x > w:
                break
        scr.puts(x, y + 1, "─" * min(w, sum(cw[1] + 1 for cw in self.cols)), theme.q("border"), bg)
        dim = dim_rows or set()
        for ri, row in enumerate(rows[top : top + n_visible]):
            ry = y + 2 + ri
            absi = ri + top
            selected = cursor is not None and absi == cursor
            alt = absi % 2 == 1
            if selected:
                row_bg = theme.q("table_alt")
            else:
                row_bg = theme.q("table_alt") if alt else bg
            cx = x
            colors = color_rows[absi] if color_rows else None
            for ci, (val, (_title, width, align)) in enumerate(zip(row, self.cols)):
                if cx - x >= w:
                    break
                if selected:
                    fg = theme.q("border_sel")
                else:
                    fg = theme.q("dim") if absi in dim else theme.q("fg")
                    if colors and ci < len(colors) and colors[ci] is not None:
                        fg = colors[ci] or fg
                scr.puts(cx, ry, _align(val, width, align), fg, row_bg,
                         bold=selected, maxw=w - (cx - x))
                cx += width + 1


def _align(s: str, width: int, align: str) -> str:
    s = s[:width]
    pad = width - len(s)
    if align == ">":
        return " " * pad + s
    if align == "^":
        left = pad // 2
        return " " * left + s + " " * (pad - left)
    return s + " " * pad


# ------------------------------------------------------------------ hints


def keyhints(scr: Screen, y: int, hints: list[tuple[str, str]], theme: Theme, w: int,
             on_zone=None) -> None:
    """Two-tone hint bar: key in key_hint color, label in key_label color.
    `on_zone(x, y, w, h, key)` is called per rendered hint (mouse support)."""
    cx = 1
    keyc = theme.q("key_hint")
    labelc = theme.q("key_label")
    bg = theme.q("bg")
    for k, label in hints:
        if cx + len(k) + len(label) + 3 > w:
            break
        if on_zone is not None:
            on_zone(cx, y, len(k) + 1 + len(label), 1, k)
        scr.puts(cx, y, k, keyc, bg, bold=True)
        cx += len(k)
        scr.puts(cx, y, " ", labelc, bg)
        cx += 1
        scr.puts(cx, y, label, labelc, bg)
        cx += len(label)
        scr.puts(cx, y, " │ ", theme.q("border"), bg)
        cx += 3


def fmt_bytes(v: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(v) < 1024 or unit == "TiB":
            return f"{v:.0f} {unit}" if unit in ("B",) else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TiB"
