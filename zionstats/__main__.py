"""CLI entry: `python -m zionstats` / `zionstats`."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .config import Config
from .nvml_layer import NvmlProvider, ProviderError
from .recorder import log_loop, record_for, snapshot_json
from .ui.themes import THEME_ORDER


def _config_mod():
    from zionstats import config as _c

    return _c


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zionstats",
        description="A btop-grade NVIDIA GPU observatory "
        "(inspired by nvitop and btop).",
    )
    p.add_argument("--version", action="version", version=f"zionstats {__version__}")
    p.add_argument("-i", "--interval", type=float, help="sampling interval seconds (default 1)")
    p.add_argument("--theme", choices=THEME_ORDER, help="UI theme (default: config/predawn)")
    p.add_argument("--layout", choices=("auto", "hero", "grid", "compact"))
    p.add_argument("--only", type=str, help="comma-separated GPU indices, e.g. 0,1")
    p.add_argument("--provider", choices=("auto", "nvidia", "amd", "intel"),
                   help="GPU data source (default: auto -- NVML, then drm scan)")
    p.add_argument("--fahrenheit", action="store_true", help="temperatures in Fahrenheit")
    p.add_argument("--graph-style",
                   choices=("btop", "block", "tty", "braille", "ascii"),
                   help="graph glyph engine: btop (default: quadrant transitions "
                        "+ row gradients, ported from btop_draw.cpp), block "
                        "(half-blocks), tty (░▒█), braille (fine dots), ascii")
    p.add_argument("--no-bg", action="store_true", help="do not paint the background")
    p.add_argument("--readonly", action="store_true",
                   help="disable process signals (T/K) for this session")
    p.add_argument("--export", metavar="[HOST:]PORT",
                   help="serve Prometheus /metrics + /snapshot (default bind "
                        "127.0.0.1; enables [export] for this session)")
    p.add_argument("--demo", action="store_true", help="synthetic 4-GPU demo (no hardware needed)")
    p.add_argument("--location", metavar="LAT,LON[,LABEL]",
                   help="override weather location for this session")
    p.add_argument("--no-weather", action="store_true", help="disable the weather layer")
    p.add_argument("--cooling", choices=("off", "dry_run", "auto", "manual"),
                   help="override cooling mode for this session")
    p.add_argument("--plug-test", metavar="NAME.CMD", help="test a configured plug: NAME on|off|status")
    p.add_argument("--list-themes", action="store_true", help="list built-in themes and exit")
    p.add_argument("-1", "--once", action="store_true", help="print one text snapshot and exit")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--snapshot", action="store_true", help="one-shot JSON to stdout")
    modes.add_argument("--record", metavar="FILE", help="record ndjson(.gz) trace")
    modes.add_argument("--replay", metavar="FILE", help="replay a recorded trace in the TUI")
    modes.add_argument("--log", metavar="FILE", help="headless jsonl logging until Ctrl-C")
    modes.add_argument("--doctor", action="store_true", help="environment health check")
    p.add_argument("--duration", type=float, default=60.0, help="seconds for --record")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_themes:
        for name in THEME_ORDER:
            print(f"  {name}" + ("   (default)" if name == "predawn" else ""))
        return 0

    from .ui import themes as _themes

    config_module = _config_mod()
    config_module._KNOWN_THEMES.update(_themes.THEME_ORDER)
    cfg = Config()
    if args.only:
        try:
            args.only = [int(x) for x in str(args.only).split(",") if x.strip()]
        except ValueError:
            print("zionstats: --only expects e.g. 0,1", file=sys.stderr)
            return 2
    else:
        args.only = None

    if args.doctor:
        return doctor()

    if args.plug_test:
        return plug_test(args.plug_test, cfg)

    from .app import make_provider

    if args.snapshot or args.once or args.log or args.record:
        try:
            provider = make_provider(args, cfg)
        except ProviderError as e:
            print(f"zionstats: {e}", file=sys.stderr)
            return 1
        if args.snapshot:
            print(snapshot_json(provider))
            provider.close()
            return 0
        if args.once:
            print(text_once(provider, fahrenheit=args.fahrenheit))
            provider.close()
            return 0
        if args.log:
            exporter = None
            if args.export:
                from .app import make_exporter

                exporter, err = make_exporter(args, cfg)
                if err:
                    print(f"zionstats: {err}", file=sys.stderr)
            log_loop(provider, args.log, args.interval or cfg.interval, cfg=cfg,
                     exporter=exporter)
            return 0
        n = record_for(provider, args.record, args.duration, args.interval or cfg.interval)
        print(f"zionstats: wrote {n} frames to {args.record}")
        return 0

    # TUI
    try:
        provider = make_provider(args, cfg)
    except ProviderError as e:
        print(f"zionstats: {e}\n(hint: --demo runs without a GPU)", file=sys.stderr)
        return 1
    from .app import run_tui

    try:
        run_tui(
            provider, cfg,
            theme_name=args.theme,
            interval=args.interval,
            layout_mode=args.layout,
            args=args,
        )
    finally:
        provider.close()
    return 0


def plug_test(spec: str, cfg: Config) -> int:
    """--plug-test 'NAME on|off|status' -- verify a configured plug while awake."""
    from .plugs import build_plug

    parts = spec.rsplit(" ", 1)
    if len(parts) != 2 or parts[1] not in ("on", "off", "status"):
        print("usage: zionstats --plug-test 'NAME on|off|status'", file=sys.stderr)
        return 2
    name, cmd = parts[0].strip(), parts[1]
    entries = [p for p in cfg.cooling.get("plugs", []) if p.get("name") == name]
    if not entries:
        known = ", ".join(p.get("name", "?") for p in cfg.cooling.get("plugs", []))
        print(f"zionstats: no plug named {name!r} in config (known: {known or 'none'})",
              file=sys.stderr)
        return 2
    try:
        plug = build_plug(entries[0])
    except Exception as e:
        print(f"zionstats: {e}", file=sys.stderr)
        return 1
    try:
        if cmd == "on":
            plug.on()
        elif cmd == "off":
            plug.off()
        time.sleep(0.5)
        st = plug.status()
        state = {True: "ON", False: "OFF", None: "UNKNOWN (no readback support)"}[st]
        print(f"{name}: {state}")
        return 0 if cmd == "status" or st is (cmd == "on") else 1
    except Exception as e:
        print(f"zionstats: plug command failed: {e}", file=sys.stderr)
        return 1


def text_once(provider, fahrenheit: bool = False) -> str:
    env = provider.environment()
    lines = [f"{env.get('hostname', '')}  driver {env.get('driver', '?')}  "
             f"CUDA<={env.get('cuda_max', '?')}  [{env.get('provider', 'nvml')}]"]
    for s in provider.sample():
        t = f"{s.temp_gpu}C" if not fahrenheit and s.temp_gpu is not None else (
            f"{s.temp_gpu * 9 / 5 + 32:.0f}F" if s.temp_gpu is not None else "?")
        mem = s.mem_total >> 20
        used = s.mem_used >> 20
        thr = ("  [" + ",".join(s.throttle) + "]") if s.throttle else ""
        lines.append(
            f"[{s.index}] {s.name} ({s.arch} {s.compute_cap})  {t} {s.pstate or '-'}  "
            f"{(s.power_w or 0):.1f}/{(s.power_limit_w or 0):.0f}W  "
            f"{used}/{mem}MiB  {s.util_gpu or 0:.0f}%  "
            f"fan {(s.fan_pct if s.fan_pct is not None else 0)}%{thr}"
        )
    procs = provider.processes()
    for pr in procs:
        lines.append(
            f"    {pr.pid:>7}  {pr.user:<10} GPU{pr.gpu_index} {pr.type}  "
            f"{pr.mem_used / 2**20:>6.0f}MiB  sm {pr.sm_pct if pr.sm_pct is not None else '-'}%  "
            f"{pr.command[:60]}"
        )
    return "\n".join(lines)


def doctor() -> int:
    cfg = Config()
    checks: list[tuple[str, bool, str]] = []

    try:
        import pynvml  # noqa: F401

        checks.append(("python: nvidia-ml-py", True, "imported"))
    except ImportError as e:
        checks.append(("python: nvidia-ml-py", False, str(e)))
        _print_doctor(checks)
        return 1

    try:
        prov = NvmlProvider()
    except ProviderError as e:
        checks.append(("NVML init", False, str(e)))
        _print_doctor(checks)
        return 1

    env = prov.environment()
    checks.append(("NVML init", True, f"driver {env['driver']} · NVML {env['nvml']}"))
    checks.append(("CUDA ceiling (driver max)", True, env["cuda_max"] or "unknown"))
    devs = prov.devices()
    checks.append(("devices", bool(devs), f"{len(devs)} GPU(s): " +
                   ", ".join(f"[{d['index']}] {d['name']}" for d in devs)))
    for s in prov.sample():
        feat = []
        feat.append(("power", s.power_w is not None))
        feat.append(("throttle-reasons", bool(s.throttle) or s.throttle_bits == 0))
        feat.append(("hottest-temp", s.temp_gpu_max is not None))
        feat.append(("fan-rpm", s.fan_rpm is not None))
        feat.append(("pcie-replay", s.pcie_replay is not None))
        feat.append(("throttle-counters", s.throttle_counters is not None))
        feat.append(("enc/dec", s.util_enc is not None))
        feat.append(("pcie", s.pcie_rx_kbps is not None))
        feat.append(("ecc", s.ecc_sbe is not None))
        feat.append(("mig", s.mig_mode is not None))
        feat.append(("energy-counter", s.energy_mj is not None))
        missing = [n for n, ok in feat if not ok]
        checks.append((f"GPU{s.index} metrics", True,
                       "all supported" if not missing else "unsupported: " + ", ".join(missing)))
    procs = prov.processes()
    checks.append(("process table", True, f"{len(procs)} process(es) attributed"))
    prov.close()

    # ---- terminal capability (rendering) checks
    import curses
    import locale as _locale

    term = os.environ.get("TERM", "")
    colors = 0
    try:
        curses.setupterm(term)
        colors = curses.tigetnum("colors") or 8
    except Exception as e:
        checks.append(("terminal", False, f"TERM={term!r}: {e}"))
    else:
        if colors >= 256:
            checks.append(("terminal colors", True, f"TERM={term} · {colors} colors"))
        else:
            checks.append(("terminal colors", False,
                           f"TERM={term} exposes {colors} colors -- themes degrade to "
                           "base 16; prefer a *-256color TERM for the full palette"))
    codeset = _locale.nl_langinfo(_locale.CODESET) if hasattr(_locale, "nl_langinfo") else "unknown"
    gs = cfg.graph_style
    if codeset.upper().replace("-", "") == "UTF8":
        checks.append(("locale / graph glyphs", True,
                       f"{codeset} · graph_style={gs} (block renders in any font; "
                       "braille needs a DejaVu/JetBrains-class font)"))
    else:
        checks.append(("locale / graph glyphs", False,
                       f"{codeset} is not UTF-8 -- use graph_style = ascii"))

    # ---- weather layer (v0.2)
    from .ui import themes as _themes

    config_module = _config_mod()
    config_module._KNOWN_THEMES.update(_themes.THEME_ORDER)
    cfg = Config()
    if cfg.weather.get("enabled"):
        try:
            from .climate import WeatherProvider

            wp = WeatherProvider(cfg.weather["lat"], cfg.weather["lon"],
                                 cfg.weather.get("label", ""),
                                 cfg.weather.get("refresh_min", 20.0))
            w = wp.refresh()
            if w is not None:
                checks.append(("weather (Open-Meteo)", True,
                               f"{w.summary_line()}" + (" [cached/stale]" if w.stale else "")))
            else:
                checks.append(("weather (Open-Meteo)", False, wp.last_error or "no data"))
        except Exception as e:
            checks.append(("weather (Open-Meteo)", False, str(e)))
    else:
        checks.append(("weather (Open-Meteo)", True, "disabled in settings"))

    # ---- cooling / plugs (v0.2)
    if cfg.cooling.get("enabled"):
        from .plugs import build_plug

        mode = cfg.cooling.get("mode", "off")
        if mode == "auto":
            checks.append(("cooling mode", False,
                           "AUTO is enabled -- zionstats will switch mains plugs. "
                           "Verify with --plug-test before trusting it."))
        else:
            checks.append(("cooling mode", True, mode))
        trig = cfg.cooling.get("trigger", "temp")
        checks.append(("cooling trigger", True,
                       f"{trig}" + (f" · usage_util>={cfg.cooling.get('usage_util', 5.0):.0f}%"
                                    f" · idle_off={cfg.cooling.get('idle_off_s', 600.0):.0f}s"
                                    if trig == "usage" else "")))
        for pc in cfg.cooling.get("plugs", []):
            try:
                plug = build_plug(pc)
                st = plug.status()
                try:
                    pw = plug.power()
                except Exception:
                    pw = None
                checks.append((f"plug {pc.get('name', '?')} ({pc.get('backend')})",
                               st is not None,  # False = healthy readback, just OFF
                               {True: "reachable, ON", False: "reachable, OFF",
                                None: "not reachable / no readback"}[st]
                               + (f" · metering: {'yes' if pw is not None else 'no'}")))
            except Exception as e:
                checks.append((f"plug {pc.get('name', '?')} ({pc.get('backend')})",
                               False, str(e)[:100]))
    else:
        checks.append(("cooling / plugs", True, "disabled in settings (default)"))

    # ---- metrics exporter (v0.4)
    if cfg.export.get("enabled"):
        host = str(cfg.export.get("host", "127.0.0.1"))
        port = int(cfg.export.get("port", 9835))
        import socket as _socket

        sock = _socket.socket()
        sock.settimeout(0.5)
        try:
            sock.bind((host, 0) if host == "127.0.0.1" else (host, 0))
            free = True
        except OSError:
            free = False
        finally:
            sock.close()
        checks.append(("exporter config", True,
                       f"{host}:{port}/metrics" + (" (bind test: port family OK)"
                                                   if free else " (bind test failed)")))
        if host not in ("127.0.0.1", "localhost", "::1"):
            checks.append(("exporter exposure", False,
                           f"non-loopback bind ({host}) exposes PIDs/users/cmdlines "
                           "on the network -- see SECURITY.md"))
    else:
        checks.append(("metrics exporter", True,
                       "disabled (start with --export [HOST:]PORT or [export] enabled)"))

    # ---- config file permissions (F2)
    perm_warn = _config_mod().config_mode_warning()
    if perm_warn is not None:
        checks.append(("config permissions", False, perm_warn))
    else:
        try:
            p = _config_mod().config_path()
            checks.append(("config permissions", True,
                           f"{p} mode {oct(p.stat().st_mode & 0o777)}"))
        except OSError:
            pass

    ok = all(ok for _, ok, _ in checks)
    _print_doctor(checks)
    return 0 if ok else 1


def _print_doctor(checks) -> None:
    width = max(len(c[0]) for c in checks) + 2
    for name, ok, msg in checks:
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {name.ljust(width)} {msg}")
    print()


if __name__ == "__main__":
    sys.exit(main())
