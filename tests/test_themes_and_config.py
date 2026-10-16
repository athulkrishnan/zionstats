"""OSS QA: every theme must render a full frame without errors; hand-edited
config files must never crash startup."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats.climate import DemoWeather
from zionstats.insights import Insights
from zionstats.nvml_layer import DemoProvider
from zionstats.ui import layout
from zionstats.ui.cells import DEFAULT_BG, Screen
from zionstats.ui.themes import REGISTRY, THEME_ORDER, quantize256


class FakeScreen(Screen):
    """Screen without curses: records cells, exposes the same drawing API."""

    def __init__(self, w, h, paint_bg=True):
        self.w = self.cols = w
        self.h = self.rows = h
        self.paint_bg = paint_bg
        self.cells = {}

    def _alloc(self):
        pass

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols

    def set_paint_bg(self, on):
        self.paint_bg = on

    def begin_frame(self, bg_index=None):
        pass

    def put(self, x, y, ch, fg=0, bg=DEFAULT_BG, bold=False):
        if 0 <= x < self.cols and 0 <= y < self.rows:
            self.cells[(x, y)] = (ch, fg, bg)

    def puts(self, x, y, text, fg=0, bg=DEFAULT_BG, bold=False, maxw=None):
        for i, ch in enumerate(text[: maxw if maxw is not None else len(text)]):
            self.put(x + i, y, ch, fg, bg, bold)

    def flush(self):
        pass

    def _attr(self, fg, bg, bold):  # pragma: no cover
        return 0


class FakeApp:
    version = "0.2.0"
    paused = False
    recording = None
    replaying = None
    interval = 1.0
    selected = 0
    sort = "pid"
    filter_me = False
    fahrenheit = False
    show_help = False
    theme_ui = {}
    show_climate = True
    show_detail = True
    layout_mode = "auto"

    def __init__(self, theme_name):
        prov = DemoProvider()
        prov._t0 = __import__("time").time() - 45
        self.theme = REGISTRY[theme_name]
        self.env = prov.environment()
        self.devices = prov.devices()
        self.indices = [d["index"] for d in self.devices]
        samples = prov.sample()
        engine = Insights()
        self.samples = {s.index: s for s in samples}
        self.insights = {s.index: engine.update(s, None) for s in samples}
        self.alerts_engine = engine
        self.proc_rows = prov.processes()
        self.user_rollup = [("demo", 2, 13.4)]
        self.hist = {i: {"util_gpu": [10, 50, 90], "util_mem": [5, 40, 80],
                         "power": [30, 60, 120], "sm": [210, 1500, 2610],
                         "pcie": [0.1, 2.0, 5.0], "memfrac": [10, 50, 94],
                         "temp": [(0, 40), (1, 55), (2, 70)]}
                     for i in self.indices}
        self.weather = DemoWeather()
        self.weather_now = self.weather.refresh()
        self.controller = None
        self._username = "demo"
        self._status_msg = ""

    def _hottest_sample(self):
        return next(iter(self.samples.values()))

    def _add_zone(self, x, y, w, h, kind, arg):
        self.zones = getattr(self, "zones", [])
        self.zones.append((x, y, x + w - 1, y + h - 1, kind, arg))

    def _aggregate(self):
        return {"power_w": 90.0, "power_limit_w": 635.0, "mem_used": 2**30,
                "mem_used_gib": 1.0, "mem_total_gib": 96.0, "mem_frac": 0.2}


def test_all_themes_render_full_frame():
    for size in ((170, 44), (120, 34), (100, 26)):
        W, H = size
        for name in THEME_ORDER:
            app = FakeApp(name)
            scr = FakeScreen(W, H)
            scr.begin_frame(app.theme.q("bg"))
            layout.draw_header(scr, W, app.env, app._aggregate(), app)
            rects = layout.plan_layout(W, H, len(app.indices), "auto", True, climate=True)
            for i, idx in enumerate(app.indices):
                if i < len(rects["cards"]):
                    layout.draw_gpu_card(scr, app, *rects["cards"][i], idx,
                                         selected=(idx == app.selected))
            if rects.get("detail"):
                layout.draw_detail(scr, app, *rects["detail"], app.selected)
            if rects.get("climate"):
                layout.draw_climate_panel(scr, app, *rects["climate"])
            if rects.get("proc"):
                layout.draw_processes(scr, app, *rects["proc"])
            layout.draw_keyhints(scr, app, H - 1, W)
    print(f"  all {len(THEME_ORDER)} themes render at {size}")


def test_light_theme_visibility():
    """Light themes quantize their background to a BRIGHT 256 color."""
    for name in THEME_ORDER:
        th = REGISTRY[name]
        bg = quantize256(th.bg)
        if th.light:
            assert bg >= 230, f"{name}: light theme bg must quantize bright, got {bg}"


def test_bar_and_chip_colors_differ_from_bg():
    """Bars/chips must be visible: their colors differ from the theme bg."""
    for name in THEME_ORDER:
        th = REGISTRY[name]
        assert th.q("ok") != th.q("bg"), name
        assert th.q("crit") != th.q("bg"), name
        assert th.q("border_sel") != th.q("bg"), name


def test_config_garbage_does_not_crash(tmp_path=None):

    cfgdir = Path(tempfile.mkdtemp()) / ".config" / "zionstats"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.toml").write_text(
        'theme = 12345\ninterval = "banana"\nlayout = false\n'
        'paint_bg = "yes please"\nsort = 7\n[weather]\nlat = "north"\n'
        "[cooling]\ntrigger_temp = true\n"
    )
    real_home = Path.home
    try:
        import zionstats.config as cfgmod

        # home = cfgdir.parent.parent so config_path() == cfgdir/config.toml
        # (an older patch of cfgdir.parent made Config look one level too deep
        # and the file was never read at all)
        Path.home = staticmethod(lambda: cfgdir.parent.parent)  # type: ignore
        c = cfgmod.Config()
        assert c.interval == 1.0 and c.theme == "predawn" and c.layout == "auto"
        assert c.sort == "pid" and c.weather["lat"] == cfgmod.WEATHER_DEFAULTS["lat"]
        assert isinstance(c.cooling.get("trigger_temp"), float)  # bool -> float
        assert c.cooling.get("mode") == "off"
    finally:
        Path.home = real_home  # type: ignore
    print("  garbage config sanitized")


def test_config_file_permission():
    d = Path(tempfile.mkdtemp())
    real_home = Path.home
    try:
        import zionstats.config as cfgmod

        Path.home = staticmethod(lambda: d)  # type: ignore
        c = cfgmod.Config()
        c.set("theme", "nord")
        p = d / ".config" / "zionstats" / "config.toml"
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"config must be 0600 (holds plug secrets), got {oct(mode)}"
    finally:
        Path.home = real_home  # type: ignore


def test_config_mode_warning():
    """F2: group/other-readable config must produce a warning; 0600 and
    missing files must not."""
    import os

    import zionstats.config as cfgmod

    d = Path(tempfile.mkdtemp())
    p = d / "config.toml"
    p.write_text("interval = 1\n")
    os.chmod(p, 0o644)
    w = cfgmod.config_mode_warning(p)
    assert w is not None and "group/other-readable" in w and "chmod 600" in w
    os.chmod(p, 0o600)
    assert cfgmod.config_mode_warning(p) is None
    assert cfgmod.config_mode_warning(d / "missing.toml") is None
    print("  config mode warning (0644 fires, 0600 silent)")


def test_config_sections_load_from_file():
    """Regression: _coerce used to KeyError on refresh_min (a weather key
    checked against the cooling table), silently aborting [weather]/[cooling]
    loading -- hand-edited cooling settings fell back to defaults."""
    cfgdir = Path(tempfile.mkdtemp()) / ".config" / "zionstats"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.toml").write_text(
        '[weather]\nrefresh_min = "45"\nlat = "north"\n'
        "[cooling]\nenabled = true\nmode = \"dry_run\"\ntrigger = \"usage\"\n"
        "usage_util = 8\nidle_off_s = 300\n"
    )
    real_home = Path.home
    try:
        import zionstats.config as cfgmod

        # home = cfgdir.parent.parent so config_path() == cfgdir/config.toml
        Path.home = staticmethod(lambda: cfgdir.parent.parent)  # type: ignore
        c = cfgmod.Config()
        assert c.weather["refresh_min"] == 45.0
        assert c.weather["lat"] == cfgmod.WEATHER_DEFAULTS["lat"]  # garbage coerced
        assert c.cooling["enabled"] is True
        assert c.cooling["mode"] == "dry_run"
        assert c.cooling["trigger"] == "usage"
        assert c.cooling["usage_util"] == 8.0
        assert c.cooling["idle_off_s"] == 300.0
    finally:
        Path.home = real_home  # type: ignore
    print("  weather/cooling sections load from file")


def test_cooling_new_keys_coerced():
    cfgdir = Path(tempfile.mkdtemp()) / ".config" / "zionstats"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.toml").write_text(
        '[cooling]\ntrigger = "banana"\nusage_util = "x"\nidle_off_s = "600"\n'
    )
    real_home = Path.home
    try:
        import zionstats.config as cfgmod

        Path.home = staticmethod(lambda: cfgdir.parent.parent)  # type: ignore
        c = cfgmod.Config()
        assert c.cooling["trigger"] == "temp"
        assert c.cooling["usage_util"] == 5.0
        assert c.cooling["idle_off_s"] == 600.0
    finally:
        Path.home = real_home  # type: ignore
    print("  new cooling keys coerced")


def test_zion_config_env_override():
    """ZION_CONFIG must relocate both config loading and saving."""
    import os

    p = Path(tempfile.mkdtemp()) / "alt" / "zion.toml"
    p.parent.mkdir(parents=True)
    p.write_text('theme = "nord"\n')
    old = os.environ.get("ZION_CONFIG")
    os.environ["ZION_CONFIG"] = str(p)
    try:
        import zionstats.config as cfgmod

        assert cfgmod.config_path() == p
        from zionstats.ui.themes import THEME_ORDER as _TO

        cfgmod._KNOWN_THEMES.update(_TO)  # main() does this lazily
        c = cfgmod.Config()
        assert c.theme == "nord"
        c.set("interval", 2.5)          # save must write through the override too
        assert "interval = 2.5" in p.read_text()
    finally:
        if old is None:
            os.environ.pop("ZION_CONFIG", None)
        else:
            os.environ["ZION_CONFIG"] = old
    print("  ZION_CONFIG override")


if __name__ == "__main__":
    test_all_themes_render_full_frame()
    test_light_theme_visibility()
    test_bar_and_chip_colors_differ_from_bg()
    test_config_garbage_does_not_crash()
    test_config_file_permission()
    test_config_mode_warning()
    test_config_sections_load_from_file()
    test_cooling_new_keys_coerced()
    test_zion_config_env_override()
    print("theme + config QA: ALL PASS")
