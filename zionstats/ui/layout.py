"""Responsive layout + panel rendering (header, GPU cards, detail, processes)."""

from __future__ import annotations

import time

from .cells import Screen
from .themes import Theme
from .widgets import Table, chip, graph, hbar, keyhints, panel_frame, sparkline

# ----------------------------------------------------------------- helpers


def fmt_temp(c: float | int | None, fahrenheit: bool) -> str:
    if c is None:
        return "n/a"
    if fahrenheit:
        return f"{c * 9 / 5 + 32:.0f}F"
    return f"{c:.0f}C"


def fmt_gbps(v: float) -> str:
    return f"{v:.1f}GB/s" if v < 10 else f"{v:.0f}GB/s"


def loadavg() -> str:
    try:
        with open("/proc/loadavg") as f:
            p = f.read().split()
        return f"{p[0]} {p[1]} {p[2]}"
    except OSError:
        return "-"


def uptime_str() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return "-"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def plan_layout(W: int, H: int, n_gpu: int, mode: str, detail: bool,
                climate: bool = False, procs: bool = True) -> dict:
    """Returns rects for content panels.  Header (2 rows) + alerts (1) +
    keyhints (1) are fixed chrome; everything else is content.  `procs`
    (btop-style box toggle) hands the process table's rows to the cards."""
    content_y = 2
    chrome_bottom = 1  # keyhints row
    content_h = H - content_y - chrome_bottom
    if W < 110:
        auto = "compact"
    elif n_gpu == 1:
        auto = "hero"
    else:
        auto = "grid"
    mode = auto if mode == "auto" else mode
    # hero shows one card; with multiple GPUs a forced hero would silently
    # drop every card after the first -- render the grid instead
    if mode == "hero" and n_gpu > 1:
        mode = "grid"
    rects: dict = {"mode": mode, "content_y": content_y, "content_h": content_h}

    # optional climate strip (v0.2) -- 10 rows carved out of the content area
    climate_h = 10 if (climate and content_h >= 26) else 0
    if climate_h:
        content_h -= climate_h + 1

    if mode == "compact":
        # a full-height card in a tall pane is a void -- cap it and give the
        # process table the rest
        card_h = min(content_h, 13) if procs else content_h
        rects["cards"] = [(0, content_y, W, card_h)]
        rest = content_h - card_h - 1
        rects["proc"] = (0, content_y + card_h + 1, W, rest) if (procs and rest >= 6) else None
        rects["detail"] = None
        rects["climate"] = (0, content_y + content_h + 1, W, climate_h) if climate_h else None
        return rects

    cols = 2 if W >= 150 and n_gpu > 1 else 1
    rows_g = (n_gpu + cols - 1) // cols
    cards_need = rows_g * 9 + 2
    if content_h >= 16 and procs:
        top_cap = max(18, min(content_h - 7, cards_need))
        proc_h = content_h - top_cap
        top_h = top_cap
    else:
        proc_h = 0
        top_h = content_h
    if proc_h == 0:
        rects["proc"] = None

    if mode == "hero":
        if detail and W >= 150:
            cw = min(90, int(W * 0.58))
            rects["cards"] = [(0, content_y, cw, top_h)]
            rects["detail"] = (cw + 1, content_y, W - cw - 1, top_h)
        else:
            rects["cards"] = [(0, content_y, W, top_h)]
            rects["detail"] = (0, content_y, W, top_h) if detail else None
    else:  # grid
        cols = 2 if W >= 150 and n_gpu > 1 else 1
        rows = (n_gpu + cols - 1) // cols
        ch = max(7, min(13, top_h // rows - (rows - 1)))
        # only emit card rows that fit above the process area -- a card row
        # drawn past the content height would collide with the bottom chrome
        rows_fit = max(1, (top_h + 1) // (ch + 1))
        cw = W // cols - 1
        cards = []
        for i in range(n_gpu):
            r, c = divmod(i, cols)
            if r >= rows_fit:
                break
            cards.append((c * (cw + 1), content_y + r * (ch + 1), cw, ch))
        rects["cards"] = cards
        dtop = content_y + rows * (ch + 1)
        if detail and top_h - rows * (ch + 1) >= 6:
            rects["detail"] = (0, dtop, W, content_y + top_h - dtop)
        else:
            rects["detail"] = None
    if proc_h:
        rects["proc"] = (0, content_y + top_h, W, proc_h)
    rects["climate"] = (0, content_y + content_h + 1, W, climate_h) if climate_h else None
    return rects


# ----------------------------------------------------------------- header


def draw_header(scr: Screen, W: int, env: dict, agg: dict, app) -> None:
    th: Theme = app.theme
    bg = th.q("bg")
    y = 0
    scr.puts(1, y, "zionstats", th.q("accent"), bg, bold=True)
    x = 11
    scr.puts(x, y, f"v{app.version}", th.q("dim"), bg)
    x += len(f"v{app.version}") + 1
    scr.puts(x, y, env.get("hostname", ""), th.q("fg"), bg, bold=True)
    x += len(env.get("hostname", "")) + 1
    prov = f"[{env.get('provider', 'nvml')}]"
    scr.puts(x, y, prov, th.q("key_hint"), bg)
    x += len(prov) + 2
    if getattr(app, "controller", None) is not None:
        st = app.controller.state
        if st.mode == "dry_run":
            btxt, bcol = f"DRY:WOULD-BE-{'ON' if st.would_on else 'OFF'}", "warn"
        elif st.on:
            btxt, bcol = "BLOWER ON", "ok"
        else:
            btxt, bcol = "BLOWER OFF", "dim"
        scr.puts(x, y, btxt, th.q(bcol), bg, bold=(bcol != "dim"))
        x += len(btxt) + 2
    if app.paused:
        scr.puts(x, y, "PAUSED", th.q("warn"), bg, bold=True)
        x += 8
    if app.recording:
        scr.puts(x, y, f"REC {app.recording}", th.q("crit"), bg, bold=True)
        x += len(f"REC {app.recording}") + 2
    if app.replaying:
        scr.puts(x, y, f"REPLAY {app.replaying}", th.q("accent2"), bg, bold=True)
        x += len(f"REPLAY {app.replaying}") + 2
    scr.puts(
        W - 10 - len(f"{app.interval:.1f}s"), y,
        f"{app.interval:.1f}s", th.q("dim"), bg,
    )
    scr.puts(W - 9, y, time.strftime("%H:%M:%S"), th.q("header_fg"), bg, bold=True)

    y = 1
    parts = []
    if getattr(app, "weather_now", None) is not None:
        wmo = app.weather_now
        parts.append((f"out {fmt_temp(wmo.temp_c, app.fahrenheit)} {wmo.rh:.0f}%RH", None))
    if agg.get("power_w") is not None:
        cap = agg.get("power_limit_w")
        parts.append((f"ΣPWR {agg['power_w']:.0f}W" + (f"/{cap:.0f}W" if cap else ""), None))
    if agg.get("mem_used") is not None:
        parts.append((f"ΣMEM {agg['mem_used_gib']:.1f}/{agg['mem_total_gib']:.1f}GiB ({agg['mem_frac']:.0%})", None))
    parts.append((f"load {loadavg()}", None))
    parts.append((f"up {uptime_str()}", None))
    drv = env.get("driver", "")
    cuda = env.get("cuda_max", "")
    if drv:
        parts.append((f"drv {drv}" + (f" · CUDA<={cuda}" if cuda else ""), None))
    x = 1
    for text, _ in parts:
        if x + len(text) + 2 > W:
            break
        scr.puts(x, y, text, th.q("dim"), bg)
        x += len(text) + 2
    right = f"GPU {app.selected} · {th.name}"
    scr.puts(W - 1 - len(right), y, right, th.q("key_label"), bg)


# ----------------------------------------------------------------- GPU card


def draw_gpu_card(
    scr: Screen,
    app,
    x: int,
    y: int,
    w: int,
    h: int,
    idx: int,
    selected: bool,
) -> None:
    th: Theme = app.theme
    bg = th.q("bg")
    s = app.samples[idx]
    ins = app.insights.get(idx)
    hist = app.hist.get(idx, {})
    app._add_zone(x, y, w, h, "gpu", idx)   # click to select this GPU
    name = s.name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
    right = f"{s.pstate or '-'} · {s.arch}" + (f" · MIGx{s.mig_instances}" if s.mig_instances else "")
    panel_frame(scr, x, y, w, h, f"GPU {idx} ─ {name}", th, selected, right)

    if h < 6 or w < 24:
        return
    r = y + 1
    # identity + energy line
    e = f"E {ins.session_wh:.1f}Wh" if ins else ""
    pk = f"· peak {ins.peak_w:.0f}W" if ins and ins.peak_w else ""
    lk = f"· idle {ins.idle_leak_w:.0f}W" if ins and ins.idle_leak_w else ""
    left = f"{s.compute_cap} · {s.compute_mode or ''}" + (f" · {e} {pk} {lk}" if e else "")
    scr.puts(x + 2, r, left, th.q("dim"), bg, maxw=w - 4)
    r += 1

    ch = h - 2 - (r - y - 1)
    meters_rows = 2
    gh = max(2, min(8, ch - meters_rows - 1))
    show_labels = gh >= 2 and ch >= 6
    if show_labels:
        half = (w - 4) // 2
        scr.puts(x + 2, r, "GPU UTIL %", th.q("table_head"), bg)
        scr.puts(x + 4 + half, r, "MEM UTIL %", th.q("table_head"), bg)
        r += 1
        gh -= 1
    half = (w - 4) // 2
    gw = half - 2 if w >= 70 else w - 6
    u = list(hist.get("util_gpu", []))
    m = list(hist.get("util_mem", []))
    if show_labels or gh:
        graph(scr, getattr(app, "graph_style", "block"), x + 2, r, gw, gh, u, 100.0, th, "grad", floor=0.06)
        if w >= 70:
            gs = getattr(app, "graph_style", "block")
            graph(scr, gs, x + 4 + half, r, gw, gh, m, 100.0, th, "grad_mem", floor=0.06)
        r += gh
    # meters row 1: power + temp (fixed split, no collision)
    mw = max(10, (w - 6) // 2 - 16)
    if s.power_w is not None and s.power_limit_w:
        frac = max(0.0, min(1.5, s.power_w / s.power_limit_w))
        scr.puts(x + 2, r, "PWR", th.q("table_head"), bg)
        hbar(scr, x + 6, r, mw, min(frac, 1.0), th, "grad_power")
        pc = "warn" if frac > 0.92 else "fg"
        scr.puts(x + 6 + mw + 1, r, f"{s.power_w:.0f}/{s.power_limit_w:.0f}W", th.q(pc), bg)
    temp_max = (s.temp_shutdown or s.temp_slowdown or 100)
    if s.temp_gpu is not None:
        tx = x + 6 + mw + 14
        tw = max(8, w - (tx - x) - 16)
        tfrac = max(0.0, min(1.0, (s.temp_gpu - 25) / max(1, temp_max - 25)))
        ticks = []
        if s.temp_slowdown:
            ticks.append((max(0.0, min(1.0, (s.temp_slowdown - 25) / max(1, temp_max - 25))), th.q("warn")))
        scr.puts(tx - 4, r, "TMP", th.q("table_head"), bg)
        hbar(scr, tx, r, tw, tfrac, th, "grad_temp", ticks=ticks)
        tc = "crit" if (ins and ins.thermal_headroom is not None and ins.thermal_headroom <= 5) else "fg"
        rate = ""
        if ins and ins.thermal_rate_c_per_min > 2:
            rate = "▲"
        elif ins and ins.thermal_rate_c_per_min < -2:
            rate = "▼"
        scr.puts(
            tx + tw + 1, r,
            f"{fmt_temp(s.temp_gpu, app.fahrenheit)}/{fmt_temp(temp_max, app.fahrenheit)}{rate}",
            th.q(tc), bg, maxw=w - (tx + tw + 1 - x) - 2,
        )
    r += 1
    if h - 2 - (r - y - 1) < 2:
        return
    # clocks + pcie row
    sm = s.clocks_sm_mhz
    smx = s.clocks_max_sm_mhz
    deficit = f" (-{ins.clock_deficit_mhz})" if ins and ins.clock_deficit_mhz else ""
    sm_txt = f"SM {sm or '?'}/{smx or '?'}MHz{deficit}"
    memc = f"MEM {s.clocks_mem_mhz or '?'}MHz"
    gen = f"Gen{s.pcie_gen_cur or '?' }"
    width = f"x{s.pcie_width_cur or '?'}"
    sat = f" ({ins.pcie_sat:.0%})" if ins else ""
    tput = ""
    if s.pcie_rx_kbps is not None:
        tput = f" ↓{(s.pcie_rx_kbps or 0)/1e6:.1f} ↑{(s.pcie_tx_kbps or 0)/1e6:.1f}GB/s{sat}"
    fan = f" · FAN {s.fan_pct}%" if s.fan_pct is not None else ""
    line = f"{sm_txt} · {memc} · PCIe {gen} {width}{tput}{fan}"
    scr.puts(x + 2, r, line, th.q("fg"), bg, maxw=w - 4)
    r += 1
    # chips row
    cx = x + 2
    if ins:
        cx += chip(scr, cx, r, ins.verdict, ins.verdict_color, th) + 1
    for reason in s.throttle:
        color = "crit" if "HW" in reason or reason == "SW-Thermal" else "warn"
        cx += chip(scr, cx, r, reason, color, th) + 1
    if ins and ins.clock_deficit_mhz > 50:
        cx += chip(scr, cx, r, f"DEFICIT {ins.clock_deficit_mhz}MHz", "warn", th) + 1
    if s.ecc_dbe:
        cx += chip(scr, cx, r, f"ECC-DBE {s.ecc_dbe}", "crit", th) + 1
    if ins and ins.pcie_sat > 0.7:
        cx += chip(scr, cx, r, "PCIe-SAT", "accent2", th) + 1


# ----------------------------------------------------------------- detail


def _fmt_num(v, unit=""):
    if unit == "C":
        return f"{v:.0f}C"
    if unit == "W":
        return f"{v:.0f}W"
    if unit == "MHz":
        return f"{v:.0f}"
    if unit == "%":
        return f"{v:.0f}%"
    return f"{v:.1f}"


def _tile(scr, app, x: int, y: int, w: int, h: int, title: str,
          data, vmax: float, grad: str, fmt, hlines=None) -> None:
    """A Grafana-style panel: rounded frame, title + now/lo/hi legend in the
    border, gradient braille time-series inside."""
    th: Theme = app.theme
    series = list(data)[-((w - 2) * 2):] or [0.0]
    now_v, lo, hi = series[-1], min(series), max(series)
    for legend in (
        f"now {fmt(now_v)} · lo {fmt(lo)} · hi {fmt(hi)}",
        f"now {fmt(now_v)} · hi {fmt(hi)}",
        f"now {fmt(now_v)}",
    ):
        if len(legend) + len(title) + 8 <= w:
            break
    panel_frame(scr, x, y, w, h, title, th, False, right=legend)
    if h >= 3:
        graph(scr, getattr(app, "graph_style", "block"), x + 1, y + 1, w - 2,
              h - 2, series, vmax or 1.0, th, grad, hlines=hlines, floor=0.06)


def _as_legend(vmax: float, fixed_max: float) -> str:
    """Suffix for the tile title when the y-axis is auto-scaled, so the
    rescaled axis is visible rather than silently lying about the range."""
    if vmax > 0 and fixed_max > 0 and abs(vmax - fixed_max) / fixed_max > 0.08:
        return f" ·max {vmax:.0f}"
    return ""


def draw_detail(scr: Screen, app, x: int, y: int, w: int, h: int, idx: int) -> None:
    """Grafana-style detail: a grid of live time-series tiles with
    threshold lines; falls back to instrument meters when tight."""
    th: Theme = app.theme
    bg = th.q("bg")
    s = app.samples[idx]
    ins = app.insights.get(idx)
    hist = app.hist.get(idx, {})
    panel_frame(scr, x, y, w, h, f"GPU {idx} detail", th, True,
                right=f"{s.pstate or '-'} · {s.arch}" if h >= 3 else "")
    if w < 40 or h < 5:
        return
    if w < 72 or h < 12:
        _draw_detail_meters(scr, app, x, y, w, h, idx)
        return

    inner_x, inner_w = x + 2, w - 4
    tmax = s.temp_shutdown or s.temp_slowdown or 100.0
    limit = s.power_limit_w or max(1.0, ins.peak_w if ins and ins.peak_w else 1.0)
    theo = (ins.pcie_theoretical_gbps if ins else 0) or 1.0
    smax = s.clocks_max_sm_mhz or max(1.0, max(list(hist.get("sm", [])) or [1.0]))
    memfrac = list(hist.get("memfrac", []))
    if not memfrac and s.mem_total:
        memfrac = [100.0 * s.mem_used / s.mem_total]

    # thresholds as graph fractions
    t_th = [((s.temp_slowdown - 25.0) / max(1.0, tmax - 25.0), 0)] if s.temp_slowdown else []
    t_th += [((s.temp_shutdown - 25.0) / max(1.0, tmax - 25.0), 0)] if s.temp_shutdown else []
    # temp series normalized to the 25..tmax window so the graph has headroom
    temp_raw = [v for _, v in hist.get("temp", [])] or ([s.temp_gpu] if s.temp_gpu is not None else [0])
    temp_series = [max(0.0, min(1.0, (v - 25.0) / max(1.0, tmax - 25.0))) for v in temp_raw]

    rate = ins.thermal_rate_c_per_min if ins else 0.0
    arrow = "▲" if rate > 2 else ("▼" if rate < -2 else "")

    # auto-scale (btop-style rolling max, decayed): power + pcie tiles follow
    # peaks and bleed down slowly; the legend prints the live axis max
    autoscale = getattr(app, "autoscale", {}) or {}
    p_as = autoscale.get((idx, "power"))
    pc_as = autoscale.get((idx, "pcie"))
    p_vmax = p_as.vmax if (getattr(app, "auto_scale", False) and p_as and p_as.vmax > 1.0) \
        else limit
    pc_vmax = pc_as.vmax if (getattr(app, "auto_scale", False) and pc_as and pc_as.vmax > 0.5) \
        else theo

    tiles = [
        # temp series is normalized to the 25..tmax window; fmt denormalizes
        ("TEMPERATURE" + arrow, temp_series, 1.0, "grad_temp",
         lambda v: _fmt_num(25.0 + v * max(1.0, tmax - 25.0), "C"), t_th),
        ("POWER W" + _as_legend(p_vmax, limit), list(hist.get("power", [])) or [s.power_w or 0],
         p_vmax, "grad_power", lambda v: _fmt_num(v, "W"), [(0.92, 0)]),
        ("GPU UTIL %", list(hist.get("util_gpu", [])) or [0], 100.0,
         "grad", lambda v: _fmt_num(v, "%"), []),
        ("VRAM %", memfrac or [0], 100.0, "grad_mem",
         lambda v: _fmt_num(v, "%"), [(90.0, 0)]),
        ("SM CLOCK", list(hist.get("sm", [])) or [s.clocks_sm_mhz or 0], smax,
         "grad", lambda v: _fmt_num(v, "MHz"), []),
        ("PCIE GB/s" + _as_legend(pc_vmax, theo), list(hist.get("pcie", [])) or [0],
         pc_vmax, "grad_power", lambda v: f"{v:.1f}", [(0.7, 0)]),
    ]

    tile_h = 5 if h >= 24 else 4
    rows_fit = min(3, (h - 5) // tile_h)
    colw = (inner_w - 3) // 2
    for r in range(rows_fit):
        for c in range(2):
            k = r * 2 + c
            if k >= len(tiles):
                break
            title, series, vmax, grad, fmt, thl = tiles[k]
            _tile(
                scr, app, inner_x + c * (colw + 3), y + 1 + r * tile_h, colw, tile_h,
                title, series, vmax, grad, fmt,
                hlines=[(f, th.q("warn")) for f, _ in thl] or None,
            )

    # bottom line: throttle state + power session stats + modes
    by = y + 1 + rows_fit * tile_h
    if by < y + h - 1 and ins:
        active = set(s.throttle)
        cx = inner_x
        if active:
            hot = any("HW" in a or a == "SW-Thermal" for a in active)
            cx += chip(scr, cx, by, " ".join(sorted(active)),
                       "crit" if hot else "warn", th) + 1
        else:
            scr.puts(cx, by, "throttles: all clear", th.q("dim"), bg)
            cx += 21
        stats = []
        if s.power_w is not None and ins.avg_w is not None:
            stats.append(f"avg {ins.avg_w:.0f}W · pk {ins.peak_w:.0f}W · {ins.session_wh:.1f}Wh")
        if ins.idle_leak_w is not None:
            stats.append(f"idle-leak {ins.idle_leak_w:.0f}W")
        if s.ecc_sbe is not None:
            stats.append(f"ECC {s.ecc_sbe}/{s.ecc_dbe}")
        line = " · ".join(stats) + f" · {s.compute_mode or '?'}"
        scr.puts(cx + 1, by, line, th.q("dim"), bg, maxw=w - (cx - x) - 4)


def _draw_detail_meters(scr: Screen, app, x: int, y: int, w: int, h: int, idx: int) -> None:
    """Compact fallback: one gradient meter row per subsystem."""
    th: Theme = app.theme
    bg = th.q("bg")
    s = app.samples[idx]
    ins = app.insights.get(idx)
    bw = max(10, min(26, w - 34))
    rows = []
    tmax = s.temp_shutdown or s.temp_slowdown or 100

    def meter(label, frac, grad, value, vcolor="fg", ticks=None):
        rows.append((label, max(0.0, min(1.0, frac)), grad, ticks or [], value, vcolor))

    if s.temp_gpu is not None:
        ticks = []
        if s.temp_slowdown:
            ticks.append(((s.temp_slowdown - 25) / max(1, tmax - 25), th.q("warn")))
        if s.temp_shutdown:
            ticks.append(((s.temp_shutdown - 25) / max(1, tmax - 25), th.q("crit")))
        rate = ins.thermal_rate_c_per_min if ins else 0.0
        arrow = "▲" if rate > 2 else ("▼" if rate < -2 else "·")
        tcol = "crit" if (ins and ins.thermal_headroom is not None and ins.thermal_headroom <= 5) else "fg"
        meter("TEMP", (s.temp_gpu - 25) / max(1, tmax - 25), "grad_temp",
              f"{fmt_temp(s.temp_gpu, app.fahrenheit)}{arrow}", tcol, ticks)
        if ins and ins.thermal_headroom is not None:
            hr = ins.thermal_headroom
            hcol = "crit" if hr <= 5 else ("warn" if hr <= 12 else "ok")
            meter("HEAD", 1.0 - max(0.0, min(1.0, hr / 30.0)), "grad_temp",
                  f"{fmt_temp(hr, app.fahrenheit)} to limit", hcol)
    if s.clocks_sm_mhz is not None and s.clocks_max_sm_mhz:
        deficit = ins.clock_deficit_mhz if ins else 0
        meter("SM", s.clocks_sm_mhz / max(1, s.clocks_max_sm_mhz), "grad",
              f"{s.clocks_sm_mhz}/{s.clocks_max_sm_mhz}MHz" + (f" (-{deficit})" if deficit else ""),
              "warn" if deficit > 200 else "fg")
    if s.power_w is not None and s.power_limit_w:
        frac = min(1.0, s.power_w / s.power_limit_w)
        extra = f" · avg {ins.avg_w:.0f} · pk {ins.peak_w:.0f}W" if ins and ins.avg_w is not None else ""
        meter("PWR", frac, "grad_power", f"{s.power_w:.0f}/{s.power_limit_w:.0f}W{extra}",
              "warn" if frac > 0.92 else "fg", [(0.92, th.q("warn"))])
    if s.mem_total:
        mfrac = s.mem_used / s.mem_total
        res = f" +{s.mem_reserved >> 20}MiB res" if s.mem_reserved else ""
        meter("VRAM", mfrac, "grad_mem",
              f"{s.mem_used >> 20}/{s.mem_total >> 20}MiB ({mfrac:.0%}){res}",
              "warn" if mfrac > 0.9 else "fg")
    if ins and ins.pcie_theoretical_gbps:
        meter("PCIE", ins.pcie_sat, "grad",
              f"↓{(s.pcie_rx_kbps or 0)/1e6:.1f} ↑{(s.pcie_tx_kbps or 0)/1e6:.1f} "
              f"of {fmt_gbps(ins.pcie_theoretical_gbps)}",
              "warn" if ins.pcie_sat > 0.7 else "fg", [(0.7, th.q("warn"))])
    active = set(s.throttle)
    if active:
        hot = any("HW" in a or a == "SW-Thermal" for a in active)
        rows.append(("THROT", None, None, [], " ".join(sorted(active)),
                     "crit" if hot else "warn"))
    lab_w, val_x = 7, bw + 9
    for i, (label, frac, grad, ticks, value, vcol) in enumerate(rows[: h - 2]):
        ry = y + 1 + i
        scr.puts(x + 2, ry, label.ljust(lab_w), th.q("table_head"), bg)
        if frac is not None:
            hbar(scr, x + 2 + lab_w, ry, bw, frac, th, grad, ticks=ticks or None)
        scr.puts(x + val_x, ry, value, th.q(vcol), bg, bold=(vcol in ("warn", "crit")),
                 maxw=w - (val_x + 2))


# ----------------------------------------------------------------- process


PROC_COLS = [
    ("PID", 8, ">"),
    ("USER", 13, "<"),
    ("T", 3, "^"),
    ("GPU", 3, ">"),
    ("MEM", 9, ">"),
    ("SM%", 5, ">"),
    ("ENC%", 5, ">"),
    ("CPU%", 6, ">"),
    ("COMMAND", 0, "<"),
]


def draw_processes(scr: Screen, app, x: int, y: int, w: int, h: int) -> None:
    th: Theme = app.theme
    bg = th.q("bg")
    sort = app.sort
    filt = "me" if app.filter_me else "all"
    tree = bool(getattr(app, "tree_mode", False))
    tagged: set = getattr(app, "tagged", set()) or set()
    cursor_pid = getattr(app, "proc_cursor_pid", None)
    regex = getattr(app, "_filter_re_src", "") or ""
    filt += f" · /{regex}/i" if regex else ""
    view = "tree" if tree else "flat"
    panel_frame(scr, x, y, w, h,
                f"Processes ─ sort:{sort} · filter:{filt} · view:{view}", th, False,
                right=(f"{len(app.proc_rows)} procs" if app.proc_rows else ""))
    if h < 5:
        return
    # per-user rollup line
    if app.user_rollup:
        ru = "   ".join(
            f"Σ {u}: {n}p · {mem:.1f}GiB" for u, n, mem in app.user_rollup[:3]
        )
        scr.puts(x + 2, y + 1, ru, th.q("accent"), bg, maxw=w - 4)
    cols = [("TAG", 3, "^")] + [c for c in PROC_COLS]
    if w < 120:
        cols = [c for c in cols if c[0] not in ("ENC%", "CPU%")]
    if w < 95:
        cols = [c for c in cols if c[0] not in ("MEM",)]
    cols[-1] = ("COMMAND", max(8, w - 4 - sum(c[1] + 1 for c in cols[:-1])), "<")
    table = Table(cols)
    col_names = [c[0] for c in cols]
    rows: list[list[str]] = []
    colors: list[list[int | None]] = []
    dim_rows: set[int] = set()
    selectable: dict[int, int] = {}   # pid -> row index (non-ancestor rows only)

    def emit(values: dict[str, str], color: list[int | None],
             dim: bool, indent: int = 0, pid: int | None = None) -> None:
        # columns and cells are filtered independently elsewhere; map by
        # column NAME so narrow layouts can never shift a value into the
        # wrong column (v0.3 rendered ENC% under COMMAND below 120 cols)
        if indent:
            values["COMMAND"] = ("│ " * min(indent, 6)) + ("└ " if indent else "") \
                + values["COMMAND"]
        if pid is not None:
            selectable[pid] = len(rows)
        rows.append([values[n] for n in col_names])
        colors.append(color)
        if dim:
            dim_rows.add(len(rows) - 1)

    if tree:
        for trow in getattr(app, "_tree_rows", []) or []:
            indent = min(trow.depth, 6)
            if trow.is_ancestor:
                emit({"TAG": " ", "PID": str(trow.pid), "USER": "?", "T": "─",
                      "GPU": str(trow.gpu_index), "MEM": "-", "SM%": "-",
                      "ENC%": "-", "CPU%": "-", "COMMAND": trow.label},
                     [None] * len(cols), True, indent)
            else:
                p = trow.proc
                emit(_proc_values(p, tagged),
                     _proc_colors(th, cols, p), False, indent, pid=p.pid)
    else:
        for p in app.proc_rows:
            emit(_proc_values(p, tagged), _proc_colors(th, cols, p), False, pid=p.pid)

    head_rows = 3 if app.user_rollup else 2
    n_visible = max(1, h - head_rows - 2)
    # window follows the cursor (pid-keyed selection survives re-sorts)
    top = int(getattr(app, "proc_top", 0) or 0)
    cursor_idx: int | None = None
    if cursor_pid is not None:
        cursor_idx = selectable.get(cursor_pid)
    if cursor_idx is not None:
        if cursor_idx < top:
            top = cursor_idx
        elif cursor_idx >= top + n_visible:
            top = cursor_idx - n_visible + 1
    max_top = max(0, len(rows) - n_visible)
    top = max(0, min(top, max_top))
    app.proc_top = top
    table.draw(scr, x + 2, y + head_rows, w - 4, rows, th, n_visible, top=top,
               color_rows=colors, cursor=cursor_idx, dim_rows=dim_rows)
    # clickable rows (visible window only)
    rows_y0 = y + head_rows + 2
    for pid, ridx in selectable.items():
        if top <= ridx < top + n_visible:
            app._add_zone(x + 2, rows_y0 + (ridx - top), w - 4, 1, "procrow", pid)


def _proc_values(p, tagged: set) -> dict[str, str]:
    return {
        "TAG": "▪" if p.pid in tagged else " ",
        "PID": str(p.pid),
        "USER": p.user[:13],
        "T": p.type,
        "GPU": str(p.gpu_index),
        "MEM": f"{p.mem_used / 2**30:.2f}Gi" if p.mem_used >= 2**20 else f"{p.mem_used >> 10}Ki",
        "SM%": f"{p.sm_pct:.0f}" if p.sm_pct is not None else "-",
        "ENC%": f"{p.enc_pct:.0f}" if p.enc_pct is not None else "-",
        "CPU%": f"{p.cpu_pct:.0f}" if p.cpu_pct is not None else "-",
        "COMMAND": p.command,
    }


def _proc_colors(th: Theme, cols: list, p) -> list[int | None]:
    pc: list[int | None] = [None] * len(cols)
    t_idx = next((i for i, c in enumerate(cols) if c[0] == "T"), None)
    if t_idx is not None:
        pc[t_idx] = th.q("ok") if "C" in p.type else th.q("accent")
    sm_col = next((i for i, c in enumerate(cols) if c[0] == "SM%"), None)
    if sm_col is not None and p.sm_pct is not None and p.sm_pct > 90:
        pc[sm_col] = th.q("warn")
    return pc


# ----------------------------------------------------------------- climate


def draw_climate_panel(scr: Screen, app, x: int, y: int, w: int, h: int) -> None:
    th: Theme = app.theme
    bg = th.q("bg")
    wmo = app.weather_now
    if wmo is None:
        panel_frame(scr, x, y, w, h, "Climate", th, False)
        scr.puts(x + 2, y + 1, "waiting for first observation...", th.q("dim"), bg)
        return
    hot = app._hottest_sample()
    tgpu = (hot.temp_gpu if hot else None)
    mode = getattr(getattr(app, "controller", None), "cfg", None)
    mode = mode.mode if mode else "off"
    cstate = getattr(getattr(app, "controller", None), "state", None)
    panel_frame(
        scr, x, y, w, h,
        f"Climate ─ {wmo.label or f'{wmo.lat:.2f},{wmo.lon:.2f}'} · {wmo.condition()}",
        th, False,
        right=(f"age {wmo.age_s()/60:.0f}m" + (" STALE" if wmo.stale else "")) if wmo else "",
    )
    if w < 40 or h < 6:
        return
    def t(c):
        return fmt_temp(c, app.fahrenheit)
    r = y + 1
    eff = wmo.effectiveness(tgpu) if tgpu is not None else 0.0
    scr.puts(
        x + 2, r,
        f"out {t(wmo.temp_c)}  feels {t(wmo.apparent_c)}  RH {wmo.rh:.0f}%  "
        f"dew {t(wmo.dew_c)}  wet-bulb {t(wmo.wet_bulb_c())}  wind {wmo.wind_kmh:.0f}km/h",
        th.q("fg"), bg, maxw=w - 4,
    )
    r += 1
    # blower line
    if cstate is not None:
        if cstate.mode == "dry_run":
            txt = f"DRY-RUN: blower would be {'ON' if cstate.would_on else 'OFF'}"
            col = "warn" if cstate.would_on else "dim"
        elif cstate.on:
            mins = cstate.duty_seconds / 60.0
            txt = f"BLOWER ON ({cstate.mode}" + (f", {mins:.0f}min)" if mins >= 1 else ")")
            col = "ok"
        else:
            txt = f"blower off ({cstate.mode})"
            col = "dim"
        reason = f" -- {cstate.reason}" if cstate.reason else ""
        scr.puts(x + 2, r, txt, th.q(col), bg, bold=(col in ("ok", "warn")))
        scr.puts(x + 2 + len(txt), r, reason[: max(0, w - 4 - len(txt))], th.q("dim"), bg)
    else:
        scr.puts(x + 2, r, "cooling disabled in settings", th.q("dim"), bg)
    r += 1
    # cooling-effectiveness bar for the hottest GPU
    if h < 8:
        return
    if tgpu is not None:
        bw = max(12, min(40, w - 34))
        scr.puts(x + 2, r, "BLOWER VALUE", th.q("table_head"), bg)
        hbar(scr, x + 15, r, bw, eff, th, "grad")
        scr.puts(
            x + 15 + bw + 1, r,
            f"{eff:.0%}  (dT {tgpu - wmo.temp_c:+.0f}C vs {t(wmo.temp_c)})",
            th.q("ok" if eff > 0.6 else ("warn" if eff > 0.3 else "crit")), bg,
        )
        r += 1
    # 24h temperature sparkline
    if h < 10 or not wmo.hourly:
        return
    scr.puts(x + 2, r, "24H OUTLOOK", th.q("table_head"), bg)
    r += 1
    data = [t for _, t in wmo.hourly]
    vmax = max(data) if data else 1.0
    sparkline(scr, x + 2, r, min(w - 4, len(data)), data, max(vmax, 1.0), th, "grad_temp")
    scr.puts(x + 2 + min(w - 4, len(data)) + 1, r,
             f"{t(min(data))}-{t(max(data))}", th.q("dim"), bg)
    r += 1
    for level, msg in wmo.advisories(tgpu, app.fahrenheit)[:1]:
        scr.puts(x + 2, r, "! " + msg, th.q({"warn": "warn", "crit": "crit"}.get(level, "accent")), bg,
                 maxw=w - 4)
        break


# ----------------------------------------------------------------- alerts


def draw_alerts(scr: Screen, app, y: int, W: int) -> None:
    th: Theme = app.theme
    bg = th.q("bg")
    recent = list(app.alerts_engine.alerts)[-3:]
    x = 1
    for a in recent:
        text = time.strftime("%H:%M:%S", time.localtime(a.ts)) + " " + a.msg
        color = {"crit": "crit", "warn": "warn", "info": "accent"}.get(a.level, "fg")
        if x + len(text) + 4 > W:
            break
        scr.puts(x, y, " ! ", th.q("bg"), th.q(color), bold=True)
        scr.puts(x + 3, y, text, th.q(color), bg)
        x += len(text) + 5


def draw_keyhints(scr: Screen, app, y: int, W: int) -> None:
    th: Theme = app.theme
    # contextual hint sets: the bar cannot fit all bindings, so show the
    # process keys when a process is focused and trim the rest
    hints = [
        ("j/k", "Procs"), ("enter", "Proc info"), ("space", "Tag"),
        ("v", "Tree"), ("/", "Regex"), ("T", "Term"), ("K", "Kill"),
        ("O", "Options"), ("x", "Panels"), ("P", "Presets"),
        ("q", "Quit"), ("t", "Theme"), ("m", "Next"),
        ("d", "Detail"), ("w", "Weather"), ("o", "Blower"), ("c", "Cool mode"),
        ("a", "Blower auto"),
        ("p", "Pause"), ("r", "Reset"),
        ("s", "Sort"), ("f", "Filter"), ("F", "DegF"),
        ("b", "BG"), ("l", "Layout"), ("+/-", "Rate"), ("?", "Help"),
    ]
    # clickable hints: single-character keys only ("j/k" and "+/-" are labels)
    keyhints(scr, y, hints, th, W,
             on_zone=lambda x, yy, ww, hh, k: app._add_zone(x, yy, ww, hh, "key", k)
             if len(k) == 1 else None)


# ------------------------------------------------------- bars + graphs

def draw_bars_mode(scr: Screen, app, x: int, y: int, w: int, h: int, idx: int) -> None:
    """btop-style horizontal bars for all GPU metrics -- compact, data-rich,
    at-a-glance.  Every metric gets a gradient-filled bar with its value
    printed at the end.  Session stats and throttle info at the bottom."""
    th: Theme = app.theme
    bg = th.q("bg")
    s = app.samples.get(idx)
    ins = app.insights.get(idx)
    if s is None:
        return

    panel_frame(scr, x, y, w, h, f" BARS ─ GPU {idx} ", th, False)

    def bar(label, frac, text, r, color_key="grad", tick_marks=None):
        """Gradient horizontal bar with label and value."""
        lw = 14
        scr.puts(x + 3, r, label.rjust(lw), th.q("table_head"), bg)
        bar_w = max(10, w - lw - 16)
        bx = x + 3 + lw
        frac = max(0.0, min(1.0, frac))
        filled = int(frac * bar_w)
        color = th.q(color_key) if color_key else th.q("fg")
        for i in range(bar_w):
            if i < filled:
                scr.put(bx + i, r, "━", color, bg)
            else:
                scr.put(bx + i, r, "─", th.q("dim"), bg)
        scr.puts(bx + bar_w + 1, r, text, th.q("fg"), bg)

    r = y + 2
    def line(text, color="fg", bold=False):
        nonlocal r
        scr.puts(x + 3, r, text, th.q(color) if not bold else th.q(color) | 0, bg,
                 bold=bold)
        r += 1

    # -- bars
    bar("GPU Util", (s.util_gpu or 0) / 100.0,
        f"{s.util_gpu or 0:.0f}%", r, "grad")
    r += 1
    bar("Mem Util", (s.util_mem or 0) / 100.0,
        f"{s.util_mem or 0:.0f}%", r, "grad_mem")
    r += 1
    vram_frac = s.mem_used / s.mem_total if s.mem_total else 0
    vram_str = f"{s.mem_used / 2**30:.1f}/{s.mem_total / 2**30:.1f} GiB"
    bar("VRAM", vram_frac,
        vram_str, r, "grad_mem")
    r += 1
    if s.power_limit_w:
        bar("Power", (s.power_w or 0) / s.power_limit_w,
        f"{s.power_w or 0:.0f}/{s.power_limit_w:.0f}W", r, "grad_power")
    r += 1
    t_style = "crit" if (s.temp_gpu or 0) >= 83 else ("warn" if (s.temp_gpu or 0) >= 70 else "ok")
    bar("Temp", (s.temp_gpu or 0) / 100.0,
        fmt_temp(s.temp_gpu, app.fahrenheit), r, t_style)
    r += 1
    sm_max = s.clocks_max_sm_mhz or 1
    bar("SM Clock", (s.clocks_sm_mhz or 0) / max(1,
        sm_max),
        f"{s.clocks_sm_mhz or 0} MHz", r, "grad")
    r += 1
    if s.fan_pct is not None:
        bar("Fan", s.fan_pct / 100.0,
        f"{s.fan_pct:.0f}%", r, "grad")
    r += 1
    r += 1  # spacing

    # -- session info
    if ins:
        line(f"  Session: {ins.session_wh:.2f} Wh · Avg {ins.avg_w or 0:.0f}W · "
             f"Peak {ins.peak_w or 0:.0f}W · Throttle: {', '.join(s.throttle) or 'none'}")
        line(f"  PCIe: Gen{s.pcie_gen_cur or '?'}/x{s.pcie_width_cur or '?'} · "
             f"Energy: {ins.session_wh:.2f} Wh")
        r += 1

    # -- throttle chips
    if s.throttle:
        for reason in s.throttle:
            color = "crit" if "HW" in reason else ("warn" if reason == "SW-Thermal" else "dim")
            line(f"  ⚠ {reason}", color)


def draw_graphs_mode(scr: Screen, app, x: int, y: int, w: int, h: int, idx: int) -> None:
    """Full-height multi-row braille graphs -- oscilloscope-style, maximum
    detail.  Each metric gets its own labeled graph row with gradient
    coloring and threshold lines."""
    th: Theme = app.theme
    bg = th.q("bg")
    s = app.samples.get(idx)
    if s is None:
        return
    hist = app.hist.get(idx, {})
    ins = app.insights.get(idx)

    panel_frame(scr, x, y, w, h, f" GRAPHS ─ GPU {idx} ", th, False)

    r = y + 2
    graph_h = 4  # braille rows per graph
    label_x = x + 3
    graph_x = x + 3
    graph_w = w - 6

    def graph_row(label, data, vmax, color_key, unit, hlines=None):
        nonlocal r
        if r + graph_h > y + h - 1:
            return
        scr.puts(label_x, r, label, th.q("table_head"), bg, bold=True)
        r += 1
        # braille graph
        graph(scr, getattr(app, "graph_style", "braille"), graph_x, r,
              graph_w, graph_h, data, vmax, th, color_key, floor=0.03,
              hlines=hlines)
        # value label at the right edge
        if data:
            cur = data[-1]
            scr.puts(graph_x + graph_w - len(unit) - 12, r - 1,
                     f"now {cur:.0f}{unit}", th.q("fg"), bg)
        r += graph_h + 1  # graph rows + separator

    # -- GPU Utilization
    util_data = list(hist.get("util_gpu", []))
    graph_row("GPU UTILIZATION", util_data, 100.0, "grad", "%",
              hlines=[(0.92, th.q("warn"))] if s.throttle_bits else [])
    r += 1

    # -- Memory Utilization
    mem_data = list(hist.get("util_mem", []))
    graph_row("MEMORY BANDWIDTH", mem_data, 100.0, "grad_mem", "%")

    # -- Power
    pw_data = list(hist.get("power", []))
    pw_max = s.power_limit_w or max(pw_data) if pw_data else s.power_limit_w or 100
    graph_row("POWER DRAW", pw_data, pw_max or 100.0, "grad_power", "W",
              hlines=[(0.92, th.q("warn"))] if s.power_limit_w else [])

    # -- Temperature
    t_data = [v for _, v in hist.get("temp", [])]
    t_max = s.temp_shutdown or 100
    graph_row("TEMPERATURE", t_data, t_max, "grad_temp", "°C",
              hlines=[(s.temp_slowdown / t_max, th.q("crit"))] if s.temp_slowdown else [])

    # -- verdict strip at the bottom
    if ins and r < y + h - 1:
        vc = ins.verdict
        vcol = {"COMPUTE-BOUND": "ok", "MEMORY-BOUND": "accent2",
                "POWER-LIMITED": "warn", "THERMALLY-LIMITED": "crit",
                "IDLE": "dim"}.get(vc, "accent")
        scr.puts(label_x, y + h - 1, f"  {vc}", th.q(vcol), bg, bold=True)
        if s.throttle:
            scr.puts(label_x + len(vc) + 4, y + h - 1,
                     " · ".join(s.throttle), th.q("warn"), bg)
