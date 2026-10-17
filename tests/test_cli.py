"""CLI surface: parser defaults, mutually exclusive modes, --only parsing,
and main() dispatch for the hardware-free paths."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zionstats import __version__
from zionstats.__main__ import build_parser, main


def test_defaults():
    a = build_parser().parse_args([])
    assert a.interval is None and a.theme is None and a.layout is None
    assert a.demo is False and a.snapshot is False and a.doctor is False
    assert a.duration == 60.0
    print("  parser defaults")


def test_only_parsing_in_main():
    """--only bad input exits with code 2 and does not touch hardware."""
    rc = main(["--demo", "--only", "zero", "--snapshot"])
    assert rc == 2
    print("  --only garbage rejected")


def test_mutually_exclusive_modes():
    try:
        build_parser().parse_args(["--demo", "--snapshot", "--record", "x.ndjson"])
        raised = False
    except SystemExit:
        raised = True
    assert raised, "--snapshot and --record must be mutually exclusive"
    print("  mutually exclusive modes")


def test_version_flag(capsys=None):
    try:
        build_parser().parse_args(["--version"])
        raised = False
    except SystemExit as e:
        raised = e.code == 0
    assert raised
    assert __version__
    print("  version flag")


def test_list_themes_runs_hw_free():
    rc = main(["--list-themes"])
    assert rc == 0
    print("  --list-themes")


def test_demo_snapshot_json():
    import contextlib
    import io
    import json
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["--demo", "--snapshot"])
    assert rc == 0
    f = json.loads(buf.getvalue())
    assert f["env"]["provider"] == "demo"
    assert len(f["devices"]) == 4
    print("  --demo --snapshot end to end")


def test_provider_flag_and_config_coercion():
    a = build_parser().parse_args(["--provider", "amd"])
    assert a.provider == "amd"
    a = build_parser().parse_args([])
    assert a.provider is None, "flag absent -> config decides"

    import zionstats.config as cfgmod

    cfg = cfgmod.Config()
    assert cfg.values["provider"] == "auto"
    # garbage-tolerant coercion via a hand-edited file
    import tempfile

    d = Path(tempfile.mkdtemp())
    (d / ".config" / "zionstats").mkdir(parents=True)
    (d / ".config" / "zionstats" / "config.toml").write_text(
        'provider = "opencl"\n')
    real_home = Path.home
    try:
        Path.home = staticmethod(lambda: d)  # type: ignore
        cfg = cfgmod.Config()
        assert cfg.values["provider"] == "auto", "garbage -> auto"
        (d / ".config" / "zionstats" / "config.toml").write_text(
            'provider = "amd"\n')
        cfg = cfgmod.Config()
        assert cfg.values["provider"] == "amd"
    finally:
        Path.home = real_home  # type: ignore
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)
    print("  --provider flag + config coercion")


def test_make_provider_resolution():
    """demo/replay never auto-switch; explicit amd with no devices fails
    cleanly; auto falls back to the vendor scan and re-raises the original
    NVML error when nothing fits.  Providers are monkeypatched so the test
    is deterministic on any host."""
    import types

    import zionstats.amd as amd_mod
    import zionstats.app as app_mod
    import zionstats.intel as intel_mod
    from zionstats.nvml_layer import DemoProvider, ProviderError

    def boom(msg):
        class Boom:
            def __init__(self, only=None):
                raise ProviderError(msg)
        return Boom

    args = types.SimpleNamespace(demo=False, replay=None, only=None,
                                 provider=None)
    assert isinstance(app_mod.make_provider(
        types.SimpleNamespace(demo=True, replay=None, only=None,
                              provider="amd")), DemoProvider), \
        "demo is never auto-switched"

    class FakeCfg:
        values = {"provider": "amd"}

    real_amd_provider = amd_mod.AmdProvider
    amd_mod.AmdProvider = boom("no AMD GPUs found on /sys/class/drm (fake)")
    try:
        try:
            app_mod.make_provider(args, FakeCfg())
            raised = None
        except ProviderError as e:
            raised = e
        assert raised is not None and "/sys/class/drm" in str(raised), \
            "explicit amd with no devices names the sysfs root"
    finally:
        amd_mod.AmdProvider = real_amd_provider

    # auto: NVML fails -> no amd cards -> no intel cards -> re-raise
    real_nvml = app_mod.NvmlProvider
    real_amd_discover = amd_mod.discover_cards
    real_intel_discover = intel_mod.discover_cards
    app_mod.NvmlProvider = boom("no NVML (fake)")
    amd_mod.discover_cards = lambda root: []
    intel_mod.discover_cards = lambda root: []
    try:
        try:
            app_mod.make_provider(args)
            raised = None
        except ProviderError as e:
            raised = e
        assert raised is not None and "no NVML (fake)" in str(raised), \
            "auto re-raises the original NVML error when nothing else fits"
    finally:
        app_mod.NvmlProvider = real_nvml
        amd_mod.discover_cards = real_amd_discover
        intel_mod.discover_cards = real_intel_discover
    print("  make_provider resolution order")


if __name__ == "__main__":
    test_defaults()
    test_only_parsing_in_main()
    test_mutually_exclusive_modes()
    test_version_flag()
    test_list_themes_runs_hw_free()
    test_demo_snapshot_json()
    test_provider_flag_and_config_coercion()
    test_make_provider_resolution()
    print("cli: ALL PASS")
