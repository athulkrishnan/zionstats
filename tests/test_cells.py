"""Screen diff-flush contract: a frame identical to the previous one must
issue zero writes; changed regions must issue exactly the changed runs."""
import atexit
import curses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.ui.cells import DEFAULT_BG, Screen

# The real flush()/_attr() path needs curses color APIs, which require an
# initialized terminal.  Stub them for this process (restored at exit so a
# pytest run sharing the interpreter is unaffected).
_orig_init_pair = curses.init_pair
_orig_color_pair = curses.color_pair
curses.init_pair = lambda i, f, b: None
curses.color_pair = lambda i: 0
atexit.register(setattr, curses, "init_pair", _orig_init_pair)
atexit.register(setattr, curses, "color_pair", _orig_color_pair)


class FakeStdscr:
    """Records addstr/addch calls; satisfies the Screen surface."""

    def __init__(self, rows=10, cols=40):
        self.rows, self.cols = rows, cols
        self.writes: list[tuple[int, int, str]] = []
        self.ch_writes: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return self.rows, self.cols

    def erase(self):
        pass

    def refresh(self):
        pass

    def addstr(self, y, x, text, attr=0):
        self.writes.append((y, x, text))

    def addch(self, y, x, ch, attr=0):
        self.ch_writes.append((y, x, ch))


# The real flush()/_attr() path needs curses color APIs, which require an
# initialized terminal.  Stub them for this process (restored at exit so a
# pytest run sharing the interpreter is unaffected).


def make_screen(rows=10, cols=40):
    std = FakeStdscr(rows, cols)
    scr = Screen.__new__(Screen)
    scr.stdscr = std
    scr.paint_bg = True
    scr.ncolors = 256
    scr.max_pairs = 256
    scr.degrade = False
    scr.no_color = False
    scr._pairs = {}
    scr._lru = []
    scr._next = 1
    scr.rows, scr.cols = rows, cols
    scr._alloc()
    scr._prev = []
    return scr, std


def test_identical_frame_zero_writes():
    scr, std = make_screen()
    scr.begin_frame()
    scr.puts(1, 1, "hello", 4, 0)
    scr.flush()
    n_first = len(std.writes)
    assert n_first > 0, "first flush must write"

    std.writes.clear()
    scr.begin_frame()
    scr.puts(1, 1, "hello", 4, 0)
    scr.flush()
    assert std.writes == [], f"identical frame must write nothing, got {std.writes}"
    print("  identical frame -> zero writes")


def test_changed_cells_written_as_runs():
    scr, std = make_screen()
    scr.begin_frame()
    scr.puts(0, 0, "AAAAAAAA", 4, 0)
    scr.puts(20, 0, "B", 5, 0)
    scr.flush()
    std.writes.clear()

    scr.begin_frame()
    scr.puts(0, 0, "AAAAAAAA", 4, 0)          # unchanged
    scr.puts(20, 0, "C", 5, 0)                # one cell changes
    scr.flush()
    assert len(std.writes) == 1, std.writes
    y, x, text = std.writes[0]
    assert (y, x, text) == (0, 20, "C")
    print("  diff runs")


def test_begin_frame_clears_previous_content():
    scr, std = make_screen()
    scr.begin_frame()
    scr.puts(2, 3, "GONE", 4, 0)
    scr.flush()
    std.writes.clear()
    scr.begin_frame()                          # no draws at all
    scr.flush()
    assert any(" " in w[2] for w in std.writes), "cleared cells must be rewritten as blanks"
    print("  clear writes blanks")


def test_bottom_right_corner_survives():
    scr, std = make_screen(rows=5, cols=10)
    scr.begin_frame()
    scr.puts(0, 0, "X" * 10, 4, 0)             # full row incl. corner cell
    scr.flush()
    # corner handled via addch (or dropped) but never a hard error
    assert any(x >= 8 for (_, x, _) in std.writes) or std.ch_writes
    print("  corner handling")


def test_color_pair_pool_recycles():
    scr, std = make_screen()
    if scr.degrade or scr.no_color:
        print("  (skipped: degraded/no-color environment)")
        return
    scr.begin_frame()
    for fg in range(16, 320):                  # exceed any pair pool
        scr.put(fg % scr.cols, fg % scr.rows, "x", fg, DEFAULT_BG)
    scr.flush()
    assert len(scr._pairs) <= scr.max_pairs
    print("  pair pool capped")


if __name__ == "__main__":
    test_identical_frame_zero_writes()
    test_changed_cells_written_as_runs()
    test_begin_frame_clears_previous_content()
    test_bottom_right_corner_survives()
    test_color_pair_pool_recycles()
    print("cells diff-flush: ALL PASS")
