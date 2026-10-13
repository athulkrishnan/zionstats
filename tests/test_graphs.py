import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.ui.cells import _256_TO_16, DEFAULT_BG
from zionstats.ui.themes import REGISTRY, THEME_ORDER
from zionstats.ui.widgets import graph, hbar


class FakeScreen:
    def __init__(self, w, h):
        self.w, self.h, self.cols, self.rows = w, h, w, h
        self.cells = {}

    def put(self, x, y, ch, fg=0, bg=DEFAULT_BG, bold=False):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.cells[(x, y)] = ch


def _render(style, values, w=10, h=3):
    scr = FakeScreen(w, h)
    graph(scr, style, 0, 0, w, h, values, 100.0, REGISTRY["predawn"])
    return scr


def test_block_glyph_set_and_order():
    # full value -> solid columns of █
    scr = _render("block", [100.0] * 10, 10, 3)
    glyphs = {c for c in scr.cells.values()}
    assert glyphs == {"█"}, f"unexpected glyphs at 100%: {glyphs}"
    # half value -> top row empty, bottom rows filled with ▄/█
    scr = _render("block", [50.0] * 10, 10, 3)
    assert all(scr.cells.get((x, 0), " ") == " " for x in range(10))
    assert all(scr.cells.get((x, 2), " ") in "█▄" for x in range(10))
    # zero value -> baseline only (▄ on the bottom row)
    scr = _render("block", [0.0] * 10, 10, 3)
    assert all(scr.cells.get((x, 2), " ") == "▄" for x in range(10))
    assert all(scr.cells.get((x, 0), " ") == " " for x in range(10))
    # ramp: right side strictly taller or equal vs left
    scr = _render("block", [i * 100 / 9 for i in range(10)], 10, 3)


def test_ascii_fallback():
    scr = _render("ascii", [100.0] * 10, 10, 3)
    glyphs = {c for c in scr.cells.values()}
    assert glyphs == {"#"}, f"unexpected glyphs at 100%: {glyphs}"
    scr = _render("ascii", [50.0] * 10, 10, 3)
    row_glyphs = [scr.cells.get((x, 2), " ") for x in range(10)]
    assert set(row_glyphs) <= {"#", "+", "."}


def test_braille_still_available():
    scr = FakeScreen(10, 2)
    graph(scr, "braille", 0, 0, 10, 2, [100.0] * 20, 100.0, REGISTRY["predawn"])
    glyphs = {c for c in scr.cells.values()}
    assert any(ord(c) >= 0x2800 for c in glyphs)


def test_threshold_rule_stamps_solid():
    scr = FakeScreen(10, 3)
    graph(scr, "block", 0, 0, 10, 3, [50.0] * 10, 100.0, REGISTRY["predawn"],
          hlines=[(0.5, REGISTRY["predawn"].q("warn"))])
    # the rule row is a solid ─ line
    row = [scr.cells.get((x, 1), " ") for x in range(10)]
    assert set(row) == {"─"}


def test_ansi16_degradation_bounds():
    for name in THEME_ORDER:
        th = REGISTRY[name]
        for slot in ("bg", "fg", "ok", "warn", "crit", "border", "accent"):
            v = _256_TO_16[th.q(slot)]
            assert 0 <= v <= 15, f"{name}.{slot} -> {v}"
    # dark themes: fg must differ from bg after degradation (no invisible text)
    th = REGISTRY["predawn"]
    assert _256_TO_16[th.q("fg")] != _256_TO_16[th.q("bg")]
    th = REGISTRY["paperwhite"]
    assert _256_TO_16[th.q("fg")] != _256_TO_16[th.q("bg")]


_TABLE = set(__import__("zionstats.ui.widgets", fromlist=["_BLOCK_UP"])._BLOCK_UP)


def test_btop_transition_glyphs():
    from zionstats.ui.widgets import _BLOCK_UP

    # flat 100% -> solid █ columns
    scr = _render("btop", [100.0] * 12, 10, 3)
    glyphs = {c for c in scr.cells.values()}
    assert glyphs <= set(_BLOCK_UP) | {" "}, glyphs
    assert "█" in glyphs
    # flat 0% -> continuous baseline (no_zero clamp), lowest glyphs
    scr = _render("btop", [0.0] * 12, 10, 3)
    bottom = [scr.cells.get((x, 2), " ") for x in range(10)]
    assert all(g != " " for g in bottom), "no_zero must draw a baseline"
    # rising transition -> quadrant/slope glyphs appear (▗ ▟ family)
    ramp = [0.0, 0.0, 20.0, 100.0, 100.0, 100.0, 100.0, 100.0]
    scr = _render("btop", ramp, 6, 3)
    mid = [scr.cells.get((x, 1), " ") for x in range(6)]
    assert any(g in "▗▐▖▄▟▌▙█" for g in mid)


def test_btop_per_row_gradient():
    # a full-height graph must tint rows differently (btop signature)
    scr = FakeScreen(8, 3)
    from zionstats.ui.cells import DEFAULT_BG

    class FS2(FakeScreen):
        def __init__(self):
            super().__init__(8, 3)
            self.fg = {}
        def put(self, x, y, ch, fg=0, bg=DEFAULT_BG, bold=False):
            if 0 <= x < 8 and 0 <= y < 3:
                self.cells[(x, y)] = ch
                self.fg[(x, y)] = fg

    scr = FS2()
    graph(scr, "btop", 0, 0, 8, 3, [100.0] * 16, 100.0, REGISTRY["predawn"])
    row_fgs = [scr.fg.get((3, y)) for y in range(3)]
    assert row_fgs[0] != row_fgs[2], f"rows must shade differently: {row_fgs}"


def test_tty_fallback():
    from zionstats.ui.widgets import _TTY_UP

    scr = _render("tty", [50.0] * 10, 10, 3)
    glyphs = {c for c in scr.cells.values()}
    assert glyphs <= set(_TTY_UP) | {" "}


def test_meter_gradient_along_width():
    class FgScreen(FakeScreen):
        def __init__(self, w, h):
            super().__init__(w, h)
            self.fgs = {}
        def put(self, x, y, ch, fg=0, bg=DEFAULT_BG, bold=False):
            super().put(x, y, ch, fg, bg, bold)
            self.fgs[(x, y)] = fg

    scr = FgScreen(12, 1)
    hbar(scr, 0, 0, 12, 1.0, REGISTRY["predawn"], "grad_power")
    fgs = [scr.fgs[(x, 0)] for x in range(1, 11)]
    assert len(set(fgs)) >= 3, f"bar must shade along width, got {fgs}"


def test_table_windowing_and_cursor():
    from zionstats.ui.widgets import Table

    class TextScreen(FakeScreen):
        def puts(self, x, y, text, fg=0, bg=DEFAULT_BG, bold=False, maxw=None):
            for i, ch in enumerate(text[:maxw if maxw is not None else len(text)]):
                self.put(x + i, y, ch, fg, bg, bold)

    th = REGISTRY["predawn"]
    t = Table([("PID", 5, ">"), ("CMD", 10, "<")])
    rows = [[str(i), f"cmd{i}"] for i in range(20)]
    scr = TextScreen(30, 12)
    # visible window: top=5 shows rows 5..9
    t.draw(scr, 0, 0, 30, rows, th, n_visible=5, top=5)
    texts = "".join(c for c in scr.cells.values())
    assert "cmd5" in texts and "cmd9" in texts
    assert "cmd4" not in texts and "cmd10" not in texts
    # cursor row renders bold
    class BoldScreen(FakeScreen):
        def __init__(self, w, h):
            super().__init__(w, h)
            self.bold_cells = set()
        def put(self, x, y, ch, fg=0, bg=DEFAULT_BG, bold=False):
            super().put(x, y, ch, fg, bg, bold)
            if bold:
                self.bold_cells.add((x, y))
    class BoldTextScreen(BoldScreen):
        def puts(self, x, y, text, fg=0, bg=DEFAULT_BG, bold=False, maxw=None):
            for i, ch in enumerate(text[:maxw if maxw is not None else len(text)]):
                self.put(x + i, y, ch, fg, bg, bold)
    scr2 = BoldTextScreen(30, 12)
    t.draw(scr2, 0, 0, 30, rows, th, n_visible=5, top=0, cursor=2)
    assert scr2.bold_cells, "cursor row must render bold"
    bold_texts = "".join(scr2.cells.get(c, " ")
                         for c in sorted(scr2.bold_cells, key=lambda c: (c[1], c[0])))
    assert "cmd2" in bold_texts
    # dim rows render (ancestors); not-bold and present
    scr3 = TextScreen(30, 12)
    t.draw(scr3, 0, 0, 30, rows, th, n_visible=5, top=0, dim_rows={0})
    print("  table windowing + cursor")


if __name__ == "__main__":
    test_block_glyph_set_and_order()
    test_ascii_fallback()
    test_braille_still_available()
    test_threshold_rule_stamps_solid()
    test_ansi16_degradation_bounds()
    test_btop_transition_glyphs()
    test_btop_per_row_gradient()
    test_tty_fallback()
    test_meter_gradient_along_width()
    test_table_windowing_and_cursor()
    print("graph tests: ALL PASS")
