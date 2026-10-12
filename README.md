# zionstats

[![CI](https://github.com/athulkrishnan/zionstats/actions/workflows/ci.yml/badge.svg)](https://github.com/athulkrishnan/zionstats/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Graph engine](https://img.shields.io/badge/graphs-btop--style%20quadrant%20gradients-8bb04f.svg)](#why-graphs-look-like-btop)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

A btop-grade NVIDIA GPU observatory. Dense, colorful, insight-first.

**v0.3.0 — Climate-Aware Cooling + Usage-Triggered Blower**: a keyless global
weather layer (works anywhere on Earth; the shipped example is Singapore —
hot, humid, tropical) and optional smart-plug blower-fan automation that
switches on the moment GPU load starts and off after the rig idles cool.
Off by default, dry-run first, fail-safe always.

zionstats watches your NVIDIA GPUs the way btop watches your CPU: full-canvas
TUI, gradient braille graphs, block bars, rounded panels, 16 built-in themes,
and a researcher-grade metric layer on top of NVML -- throttle reasons, clock
deficit, PCIe saturation, session energy, per-process GPU utilization, and a
live verdict chip (`COMPUTE-BOUND`, `POWER-LIMITED`, `THERMALLY-LIMITED`,
`MEMORY-BOUND`, `IDLE`).

One dependency (`nvidia-ml-py`). Stdlib curses renderer with a diff-flush
cell buffer, so it stays crisp over SSH, in tmux splits, and in iTerm2.

```
gpu-rig   driver 580.159.03  CUDA<=13.0  [nvml]                          1.0s 19:06:50
 ΣPWR 80W/635W  ΣMEM 19.1/96.0GiB (20%)  load 0.15 0.18 0.18  ...        GPU 0 · predawn
╭─ GPU 0 ─ RTX 4070 Ti SUPER ─────────────────────P8 · Ada─╮
│ 8.9 · Default · E 0.2Wh · peak 37W · idle 35W            │
│ GPU UTIL %              MEM UTIL %                       │
│   ⣀⣠⣤⣶⣿ (gradient braille graphs)                        │
│ PWR ▕██████▂──────▏ 36/285W  TMP ▕████▄──┃────▏ 36C/98C  │
│ SM 253/2850MHz · MEM 10951MHz · PCIe Gen4 x16 ↓0.0 ↑0.0  │
│  IDLE                                                    │
╰──────────────────────────────────────────────────────────╯
```

## Highlights

- **Climate-aware** (v0.3.0) -- keyless global weather, dew-point/wet-bulb
  physics, cooling-effectiveness index, and optional fail-safe smart-plug
  blower automation (see below).
- **Verdict chip per GPU** -- the app tells you what your workload is:
  compute-bound, memory-bound, power-limited, thermally-limited, or idle,
  derived from utilization split + NVML throttle-reason bitmask.
- **Full NVML 13 coverage** -- GPU/mem/encoder/decoder utilization, VRAM +
  reserved + BAR1, power draw/limits/range, SM/mem/video clocks vs max,
  decoded throttle reasons (SW-Power-Cap, HW-Slowdown, SW-Thermal, ...),
  temps + slowdown/shutdown thresholds + dT/dt trend, fans, PCIe
  gen/width/throughput with saturation %, ECC SBE/DBE, MIG, modes.
- **Session energy** -- integrates the NVML millijoule counter into Wh,
  plus average/peak power and idle-leak watts (draw at <5% util).
- **Per-process GPU utilization** -- pmon-class per-PID SM/ENC% via the
  NVML process-utilization API, merged with /proc for user + command +
  CPU%, with a per-user rollup row for shared boxes.
- **Alerts** -- edge-triggered strip: hardware throttle engaged/cleared,
  within 5C of slowdown, VRAM > 92%, uncorrected ECC.
- **16 themes, live preview picker** -- press `t`: arrow through swatches,
  the whole UI re-skins live; Enter keeps it, Esc reverts. Themes: predawn
  (default), solarized-dark, gruvbox, nord, dracula, catppuccin-mocha,
  tokyo-night, synthwave-84, cyberpunk, everforest, rose-pine, one-dark,
  matrix, amber-crt, btop-classic, paperwhite.
- **Record / replay / snapshot / log** -- capture a gzipped ndjson trace
  and replay it later in the full TUI; one-shot JSON for scripts; headless
  jsonl logging for regression tests; `--doctor` environment check.
- **Batched sampling** -- one NVML FieldValues call per GPU per tick for
  the hot metrics (energy counter, power, clocks, utils), individual
  getters as fallback; graceful per-metric degradation on GeForce
  (no ECC/MIG? those fields just hide).
- **Layouts that fit your terminal** -- hero (single GPU, big graphs +
  detail), grid (2-8 GPUs), compact (<110 cols, split-pane friendly);
  auto-switched, or forced with `l`. Verified at 90x25 through 300x80.

## Building from source

Everything builds with the standard toolchain — no cross-compilation needed
because the wheel is pure Python (works on any OS/arch), and binaries are
built natively per-platform by CI.

```bash
# 1) universal wheel + sdist  ->  dist/
pip install build && python -m build

# 2) self-contained binary (build on the OS/arch you target)
pip install .[dev] pyinstaller
pyinstaller --onefile --name zionstats --clean -y run.py
#    -> dist/zionstats (linux/macOS) or dist\zionstats.exe (windows)
```

**Releases are automated**: pushing a tag (`git tag v0.3.0 && git push --tags`)
triggers GitHub Actions to build the wheel, sdist, and native binaries for
`linux_amd64`, `linux_arm64`, `macOS_amd64`, `macOS_arm64`, `windows_amd64`,
sha256 them into `checksums.txt`, and attach everything to the GitHub Release
(see `.github/workflows/release.yml`). Local builds and CI builds use the
exact same entry point (`run.py`).

Optional extras: `pip install .[plugs]` (tinytuya + python-kasa for smart
plugs), `pip install .[dev]` (build + pyinstaller).

## Climate + blower automation (optional)

The weather layer is on by default (one keyless request per 20 min, cached to
disk, offline-tolerant — set `[weather] enabled = false` for zero network).
It shows ambient temperature, humidity, dew point, wet-bulb, a 24 h outlook,
and the honest part: a **BLOWER VALUE** index — how much a fan blowing outside
air at your GPU is actually worth right now (`(Tgpu − Tamb)` headroom, derated
by humidity). It also warns when a surface sits below the dew point
(condensation — cold AC room, muggy monsoon night: keep the blower off).

The cooling automation is **off until you turn it on**:

```toml
[cooling]
enabled = true
mode = "dry_run"        # off | dry_run | auto | manual  -- start with dry_run
trigger = "usage"       # usage | temp
usage_util = 5.0        # (usage) GPU util % that counts as "in use"
idle_off_s = 600        # (usage) OFF after this many idle seconds AND below resume_temp
trigger_temp = 82.0     # (temp) blower ON at/above this GPU temp ...
resume_temp = 78.0      # ... and OFF again at/below this (hysteresis / hot-guard)
min_delta_c = 8.0       # (temp) only bother if GPU is >= 8C over ambient
min_on_s = 120          # no flapping
min_off_s = 180
max_duty_min = 90       # forced cooldown after 90 continuous minutes

[[cooling.plugs]]
backend = "tuya"        # tuya | kasa | shelly | tasmota | ha | mock
name = "blower"
id = "..."; address = "192.168.1.50"; key = "..."; version = 3.3
```

**Two triggers.** `trigger = "usage"` turns the blower ON the moment GPU
utilization crosses `usage_util`, and OFF once the GPU has stayed idle for
`idle_off_s` **and** cooled below `resume_temp` — a hot-idling GPU after a
load spike keeps its blower until it is actually cool (the hot-guard only
extends an ON, it never starts one). `trigger = "temp"` is the original
temperature-hysteresis behavior: ON at `trigger_temp` with `min_delta_c` over
ambient, OFF at `resume_temp`.

In `dry_run` zionstats decides, logs and shows `DRY-RUN: blower would be ON`
without touching anything — watch it for a day, then flip `mode = "auto"`.
Keys: `w` climate panel · `o` manual blower toggle · `c` cycle mode.

**Headless.** The automation does not need the TUI: `zionstats --log
gpu.jsonl` runs the same fail-safe controller (systemd-unit friendly) and
prints every blower event to stderr (`zionstats blower [on] usage 40% >= 5%`).

**Failsafes** (auto mode): GPU samples stale > 15 s force the blower OFF in
every actuating mode; stale weather (> 45 min) force-off applies to the
`temp` trigger only (usage mode never reads weather); minimum on/off times
prevent relay chatter; 90 min continuous duty forces a cooldown; every
command is verified by readback and retried twice before an alert fires.

**Plug setup** — the plugs stay paired with Alexa/Google exactly as they are
(voice still works); zionstats drives them locally on your LAN, so automation
works with the internet down and no cloud sees your data:

| backend | needs | notes |
|---|---|---|
| `tuya` | `pip install tinytuya` + one-time `python -m tinytuya wizard` for the local key | most Alexa-compatible plugs are Tuya |
| `kasa` | `pip install python-kasa` | TP-Link plugs |
| `shelly` | nothing | Gen1/Gen2 REST, config `gen = 1|2` |
| `tasmota` | nothing | stock HTTP endpoint |
| `ha` | Home Assistant long-lived token | the hub route: Alexa → HA → anything |

Verify hardware while awake: `zionstats --plug-test "blower on"` → `blower: ON`.
`zionstats --doctor` reports weather reachability, plug reachability, and
flags loudly when AUTO is armed.

**Tested hardware**: Zebronics ZEB-SP110 (10A, Tuya firmware v3.4) — verified
end-to-end: local ON/OFF with readback, `--plug-test`, and a full
usage-triggered automation cycle in both dry-run and auto. Any Tuya-based
plug that pairs with a Tuya app should behave the same way.

> Safety: this feature switches mains devices. zionstats ships it disabled,
> recommends dry-run first, force-offs on stale telemetry, and never touches
> real plugs in `--demo`.

## Install

```bash
pip install zionstats                                  # from PyPI
pip install git+https://github.com/athulkrishnan/zionstats.git
pip install .                                          # from a checkout
./bin/zionstats                                        # without installing
```

Or grab a self-contained binary from
[Releases](https://github.com/athulkrishnan/zionstats/releases) (bundled
Python, no interpreter needed) — `linux_amd64`, `linux_arm64`,
`macOS_amd64`, `macOS_arm64`, `windows_amd64`, each with a `checksums.txt`.
Other platforms: `pip install` from source runs anywhere Python 3.9+ does.

| Platform | Live NVML metrics | Notes |
|---|---|---|
| Linux | yes (primary target) | NVIDIA driver 5xx+ |
| Windows | yes | `windows-curses` auto-installed with pip; binaries bundle it |
| macOS | demo mode | NVIDIA dropped modern macOS drivers; TUI + `--demo` work |

Requires an NVIDIA GPU with driver 5xx+ for live metrics; `--demo` works
anywhere.

## Usage

```bash
zionstats                     # the TUI
zionstats --demo              # synthetic 2-GPU trace (theme dev, no GPU needed)
zionstats --theme gruvbox     # pick a theme
zionstats --only 0            # watch specific GPUs
zionstats -1                  # one-shot text summary
zionstats --snapshot          # one-shot JSON
zionstats --record trace.ndjson.gz --duration 60
zionstats --replay trace.ndjson.gz     # replay in the full TUI
zionstats --log run.jsonl     # headless logging until Ctrl-C
zionstats --doctor            # environment health check
zionstats --location 51.5,-0.13,London    # weather anywhere on Earth
zionstats --plug-test "blower on"         # verify a plug safely
zionstats --list-themes
```

## Keys

```
q quit        t theme picker   m cycle theme   1-9/<-> select GPU
d detail      w weather        o blower        c cool mode
a blower auto p pause          r reset stats   s sort procs
f filter me   F Fahrenheit     b paint bg      l layout
+/- rate      ? help
```

`a` is the one-press automation switch: AUTO watches GPU usage and runs the
blower from load start until 10 minutes after idle (extended while the GPU
is still hot); pressing it again disarms to `off`.

## Why "the researcher layer"

Raw utilization is half the story. A GPU pinned at 100% while drawing its
full power envelope is *power-limited* -- you are leaving clocks on the
table and your training throughput is throttled by the cap, not by your
batch size. zionstats surfaces that distinction the moment it happens:
the verdict chip flips, `SW-Power-Cap` lights up, the power bar saturates,
the clock-deficit counter shows exactly how many MHz below max you are
running, and an alert fires once -- not spam -- until the state clears.

## Security notes

- The config file (`~/.config/zionstats/config.toml`) can hold plug secrets
  (Tuya local keys, Home Assistant tokens). It is written with `0600`
  permissions -- keep it that way if you edit by hand.
- The weather layer is the **only** outbound network call (keyless HTTPS to
  Open-Meteo, cached, and fully disableable with `[weather] enabled = false`
  or `--no-weather`). Plug control is LAN-only; nothing is sent to any cloud.
- No telemetry, no accounts, no eval/subprocess anywhere in the codebase.
- Cooling automation ships **disabled** and `dry_run` is the recommended
  first mode; `--demo` structurally cannot touch real plugs.

## Contributing

Issues and PRs welcome. Development loop:

```bash
pip install -e .
python tests/test_climate.py && python tests/test_cooling.py \
  && python tests/test_themes_and_config.py
zionstats --demo          # everything interesting, no GPU needed
```

Please keep new backends local-first (no cloud calls), and keep every
feature working with `--demo` so screenshots and CI stay hardware-free.

## FAQ

**Does it work with multiple GPUs?** Yes -- hero/grid layouts scale to 8+
cards; cards tile and the verdict engine runs per GPU.

**Does it need a GPU?** No. `zionstats --demo` runs a synthetic two-GPU
scenario (including a scripted thermal-throttle arc and a mock blower) for
theme development, screenshots, and CI.

**Graphs look like dots / broken?** Graphs default to the btop rendering
engine — quadrant transition glyphs (`▗▟█`) with per-row gradient shading,
ported from btop's `btop_draw.cpp` — solid in every monospace font. Other
styles via `graph_style = block | tty | braille | ascii` (braille needs a
DejaVu/JetBrains-class font; ascii is bulletproof). Terminals exposing fewer
than 256 colors automatically degrade the palette to base 16 (`zionstats
--doctor` reports what your terminal detected).

**Why not just talk to Alexa directly?** Amazon offers no consumer local
API for Alexa, and cloud round-trips are both fragile and a privacy cost.
Your plugs stay paired with Alexa for voice; zionstats drives them over the
LAN so automation works offline.

**Is the blower feature safe?** It switches mains devices, so it ships off,
recommends dry-run first, force-offs on stale telemetry, duty caps, and
readback verification. See the cooling section above.

**My Tuya plug won't pair.** These plugs are 2.4 GHz-only — if your router
merges bands, split the SSIDs (or use the app's AP-mode fallback: fast-blinking
LED, connect the phone to the plug's hotspot).

**`tinytuya wizard` can't find my devices / the app won't link.** Brand apps
(Zeb Home, and most other whitelabel Tuya apps) cannot be linked to your own
iot.tuya.com project — only Tuya Smart and Smart Life can. Factory-reset the
plug and pair it with **Tuya Smart** or **Smart Life** (pick your real country
at registration — devices live in that data center), create the cloud project
with the *same* data center, then run `python3 -m tinytuya wizard -region in`
(`-region` options: `cn, us, us-e, eu, eu-w, in, sg`). The cloud is used once,
to read the local key; runtime stays 100% LAN-local.

**`--plug-test` prints UNKNOWN.** Some firmwares don't expose a readable
switch DP; status readback returns "no readback support" and plug-test exits 1
on `status`. Automation still works — the controller's write + verify loop
treats an unreachable readback as success and retries on disagreement.

**I re-paired my plug and zionstats lost control.** The Tuya local key
rotates on every pairing. Re-run the wizard and update `key` in the plug's
config entry.

**Can it run without the TUI (e.g. while gaming)?** Yes — `zionstats --log
gpu.jsonl` runs the same fail-safe blower controller headless and prints every
event to stderr. A minimal systemd user unit:

```ini
[Unit]
Description=zionstats blower automation
After=network-online.target

[Service]
ExecStart=%h/.local/bin/zionstats --log %h/gpu.jsonl
Restart=on-failure

[Install]
WantedBy=default.target
```

## Status & disclaimer

zionstats was hacked together in under two days, hackathon-style. It works
well on the author's rig — and nowhere else is guaranteed. Expect bugs. The
software is provided **as-is, without warranty of any kind**; the author is
**not liable** for any damages, data loss, or burnt-down houses (the binding
legal text is Apache-2.0 §7 and §8 in [LICENSE](LICENSE) — this section is
just the human version). The blower feature switches **mains-voltage
devices**: test with `--plug-test` while awake, stay in `dry_run` until you
trust it, and never automate a load that is unsafe to switch. zionstats is
not affiliated with NVIDIA, Zebronics, Tuya, or any plug vendor; all product
names and trademarks belong to their respective owners.

## Credits and prior art

zionstats is an original implementation (no nvitop code), standing on the
shoulders of:

- **[nvitop](https://github.com/XuehaiPan/nvitop)** -- the interactive
  NVIDIA-GPU process viewer that inspired this project
- **[btop](https://github.com/aristocratos/btop)** -- the design language:
  braille graphs, block bars, rounded panels, dense theming
- **[all-smi](https://github.com/lablup/all-smi)** -- record/replay, JSON
  snapshots, doctor, provider abstraction
- **[gputop](https://github.com/rib/gputop)** -- headless machine-readable
  logging heritage

## License

Apache-2.0 -- see [LICENSE](LICENSE). The Apache license was chosen for
its explicit patent grant, which matters for a tool that interfaces with
hardware control paths.
