"""plan_layout: responsive rect planning across the documented size range,
the hero->grid multi-GPU fallback, and the climate carve-out."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.ui.layout import plan_layout

SIZES = ((90, 25), (100, 26), (120, 34), (170, 44), (220, 60), (300, 80))


def test_size_matrix_never_crashes_and_fits():
    for W, H in SIZES:
        for n in (1, 2, 4, 8):
            for mode in ("auto", "hero", "grid", "compact"):
                for detail in (True, False):
                    r = plan_layout(W, H, n, mode, detail)
                    assert r["cards"], (W, H, n, mode)
                    for (x, y, w, h) in r["cards"]:
                        assert w > 0 and h > 0, (W, H, n, mode)
                        assert x + w <= W and y + h <= H - 1, \
                            f"card {x},{y},{w},{h} overflows {W}x{H}"
                    if r["proc"]:
                        px, py, pw, ph = r["proc"]
                        assert ph >= 0 and py + ph <= H - 1
    print(f"  {len(SIZES)}x4x2x4 layout combos fit")


def test_hero_multiple_gpus_falls_back_to_grid():
    """Regression: forced hero with >1 GPU used to render only the first
    card (single rect); it must fall back to grid and emit n rects."""
    r = plan_layout(170, 44, 3, "hero", True)
    assert r["mode"] == "grid"
    assert len(r["cards"]) == 3, r["cards"]
    r = plan_layout(170, 44, 2, "hero", False)
    assert r["mode"] == "grid" and len(r["cards"]) == 2
    # single GPU keeps hero
    r = plan_layout(170, 44, 1, "hero", True)
    assert r["mode"] == "hero" and len(r["cards"]) == 1
    # auto picks grid for multi-GPU anyway
    r = plan_layout(170, 44, 2, "auto", True)
    assert r["mode"] == "grid"
    print("  hero->grid fallback")


def test_auto_mode_picks_by_width():
    assert plan_layout(90, 25, 1, "auto", True)["mode"] == "compact"
    assert plan_layout(120, 34, 1, "auto", True)["mode"] == "hero"
    assert plan_layout(120, 34, 2, "auto", True)["mode"] == "grid"
    print("  auto mode selection")


def test_climate_carveout():
    W, H = 170, 44
    without = plan_layout(W, H, 1, "hero", True, climate=False)
    with_c = plan_layout(W, H, 1, "hero", True, climate=True)
    assert without["climate"] is None
    assert with_c["climate"] is not None
    cw = with_c["climate"]
    assert cw[1] + cw[3] <= H - 1, "climate strip must stay inside the frame"
    # the climate strip must not push the process area off-frame
    pw = with_c["proc"]
    assert pw is not None and pw[1] + pw[3] <= cw[1], (pw, cw)
    # narrow terminals skip the climate strip entirely
    assert plan_layout(90, 25, 1, "auto", True, climate=True)["climate"] is None
    print("  climate carve-out")


def test_small_terminal_floors():
    """60x12 is the documented minimum; cards must still be planned."""
    r = plan_layout(60, 12, 1, "auto", True)
    assert r["cards"] and r["cards"][0][3] >= 3
    print("  minimum terminal floor")


if __name__ == "__main__":
    test_size_matrix_never_crashes_and_fits()
    test_hero_multiple_gpus_falls_back_to_grid()
    test_auto_mode_picks_by_width()
    test_climate_carveout()
    test_small_terminal_floors()
    print("layout planning: ALL PASS")
