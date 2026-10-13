"""Double-buffered cell renderer on stdlib curses.

Every frame is drawn into an off-screen cell grid (char + fg + bg + bold)
and flushed as a diff against the previous frame, so unchanged regions are
never rewritten -- no flicker, minimal I/O.  Colors are xterm-256 indices
allocated from an LRU pool of curses color pairs.
"""

from __future__ import annotations

import curses
import os

DEFAULT_BG = -1  # use_default_colors()

# nearest base-16 color for each xterm-256 index (computed once)
def _build_256_to_16() -> dict:
    def lum(c):
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    cube = [0, 95, 135, 175, 215, 255]
    pal = ([(cube[r], cube[g], cube[b]) for r in range(6) for g in range(6) for b in range(6)]
           + [(v, v, v) for v in range(8, 248, 10)])
    a16 = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
           (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
           (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
           (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
    out = {}
    for i, (r, g, b) in enumerate(pal):
        best, best_d = 7, 1 << 30
        for j, (pr, pg, pb) in enumerate(a16):
            d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
            # prefer the bright variant for high-luminance colors
            if d + (2 if lum((r, g, b)) > 128 and j < 8 else 0) < best_d:
                best, best_d = j, d
        out[16 + i] = best
    return out


_256_TO_16 = _build_256_to_16()


class Screen:
    def __init__(self, stdscr, paint_bg: bool = True) -> None:
        self.stdscr = stdscr
        self.paint_bg = paint_bg
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            self.paint_bg = False
        self.ncolors = getattr(curses, "COLORS", 8)
        self.max_pairs = max(8, min(getattr(curses, "COLOR_PAIRS", 64), 256) - 1)
        # terminals with <256 colors cannot take quantized indices: map every
        # 256-palette value to the nearest base-16 color instead (the silent
        # black-on-black failure this prevents cost us one bug report)
        self.degrade = self.ncolors < 256
        # NO_COLOR (https://no-color.org): drop all color, keep bold only
        self.no_color = bool(os.environ.get("NO_COLOR"))

        self._pairs: dict[tuple[int, int], int] = {}
        self._lru: list[int] = []
        self._next = 1

        self.rows, self.cols = stdscr.getmaxyx()
        self._alloc()
        self._prev: list[list[tuple[str, int, int, bool]]] = []

    # ------------------------------------------------------------ lifecycle

    def _alloc(self) -> None:
        w = max(1, self.cols)
        h = max(1, self.rows)
        blank = (" ", 0, DEFAULT_BG if not self.paint_bg else 0, False)
        self._cur: list[list[tuple[str, int, int, bool]]] = [
            [blank] * w for _ in range(h)
        ]
        self._prev = []
        self._blank_rows: dict[int, list] = {}

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self._alloc()
        self.stdscr.erase()

    def set_paint_bg(self, on: bool) -> None:
        if on != self.paint_bg:
            self.paint_bg = on
            self._alloc()
            self.stdscr.erase()

    # ------------------------------------------------------------ color pool

    def _attr(self, fg: int, bg: int, bold: bool) -> int:
        if self.no_color:
            return curses.A_BOLD if bold else 0
        if self.degrade:
            fg = _256_TO_16.get(fg, 7 if fg else 0)
            if bg != DEFAULT_BG:
                bg = _256_TO_16.get(bg, 0)
            if self.ncolors <= 8:
                # classic 8-color brightening: high colors become bold + base
                bold = bold or fg >= 8
                fg %= 8
                bg %= 8
        key = (fg, bg)
        pid = self._pairs.get(key)
        if pid is None:
            if len(self._pairs) >= self.max_pairs:
                pid = self._lru.pop(0)
                old = next(k for k, v in self._pairs.items() if v == pid)
                del self._pairs[old]
            else:
                pid = self._next
                self._next += 1
            try:
                curses.init_pair(pid, fg, bg)
            except curses.error:
                curses.init_pair(pid, 0, bg)
            self._pairs[key] = pid
        else:
            try:
                self._lru.remove(pid)
            except ValueError:
                pass
        self._lru.append(pid)
        attr = curses.color_pair(pid)
        return attr | curses.A_BOLD if bold else attr

    # ------------------------------------------------------------ drawing

    def begin_frame(self, bg_index: int | None = None) -> None:
        fill_bg = bg_index if (self.paint_bg and bg_index is not None) else DEFAULT_BG
        row = self._blank_rows.get(fill_bg)
        if row is None:
            row = [(" ", 0, fill_bg, False)] * self.cols
            self._blank_rows[fill_bg] = row
        for r in self._cur:
            r[:] = row

    def put(
        self,
        x: int,
        y: int,
        ch: str,
        fg: int = 0,
        bg: int = DEFAULT_BG,
        bold: bool = False,
    ) -> None:
        if 0 <= x < self.cols and 0 <= y < self.rows:
            self._cur[y][x] = (ch, fg, bg, bold)

    def puts(
        self,
        x: int,
        y: int,
        text: str,
        fg: int = 0,
        bg: int = DEFAULT_BG,
        bold: bool = False,
        maxw: int | None = None,
    ) -> None:
        if maxw is not None:
            text = text[:maxw]
        for i, ch in enumerate(text):
            if ch == "\n" or ord(ch) < 32:
                continue
            self.put(x + i, y, ch, fg, bg, bold)

    def flush(self) -> None:
        self._err_count = getattr(self, "_err_count", 0)
        self._err_last = getattr(self, "_err_last", "")

        stdscr = self.stdscr
        prev = self._prev
        for y, crow in enumerate(self._cur):
            prow = prev[y] if y < len(prev) else None
            x = 0
            n = len(crow)
            while x < n:
                cell = crow[x]
                old = prow[x] if prow is not None and x < len(prow) else None
                if cell == old:
                    x += 1
                    continue
                fg, bg, bold = cell[1], cell[2], cell[3]
                run_end = x
                while run_end < n and crow[run_end] == cell:
                    run_end += 1
                text = "".join(c[0] for c in crow[x:run_end])
                # writing the bottom-right corner cell always returns ERR and
                # would drop the whole run -- keep the corner out of addstr
                corner = y == self.rows - 1 and run_end >= self.cols
                if corner:
                    run_end = self.cols - 1
                    text = text[: run_end - x]
                try:
                    stdscr.addstr(y, x, text, self._attr(fg, bg, bold))
                except curses.error as e:
                    self._err_count += 1
                    self._err_last = f"y={y} x={x} rows={self.rows} cols={self.cols} {e!r}"
                if corner and x + len(text) == self.cols - 1:
                    try:
                        stdscr.addch(y, self.cols - 1, crow[self.cols - 1][0],
                                     self._attr(fg, bg, bold))
                    except curses.error:
                        pass
                    x = run_end + 1
                    continue
                x = run_end
        self._prev = [row[:] for row in self._cur]
        if self._err_count and os.environ.get("ZION_DEBUG_FLUSH"):
            with open("/tmp/zs_flush_err.log", "a") as _f:
                _f.write(f"errors={self._err_count} last={self._err_last}\n")
        try:
            stdscr.refresh()
        except curses.error:
            pass
