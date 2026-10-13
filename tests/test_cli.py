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
    assert len(f["devices"]) == 2
    print("  --demo --snapshot end to end")


if __name__ == "__main__":
    test_defaults()
    test_only_parsing_in_main()
    test_mutually_exclusive_modes()
    test_version_flag()
    test_list_themes_runs_hw_free()
    test_demo_snapshot_json()
    print("cli: ALL PASS")
