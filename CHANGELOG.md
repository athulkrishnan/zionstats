# Changelog

## v0.5.0 -- Rust core: zionstatsd daemon + SHM TUI (2026-10-14)

### Added
- **Rust Cargo workspace** (`crates/`): zionstats-core (types, seqlock SHM
  layout, hysteresis controller, sources, metrics), zionstats-daemon
  (`zionstatsd`), zionstats-tui, and a unified `zionstats` CLI.
- **zionstatsd**: always-on systemd daemon (Type=notify + watchdog), NVML
  polling to 4 Hz (`--interval 0.25`), zero-copy shared memory in
  `/dev/shm/zionstats-<uid>` (0600, seqlock + ABI tag), flock two-writer
  guard, SIGTERM restoring driver fan control, graceful plug shutdown
  policy.
- **Thermal controller (Rust port)**: the Python fail-safe state machine
  (82/78 hysteresis, usage trigger, min on/off, duty cap, stale force-off,
  readback verify x3) ported 1:1 with its 23-case table tests; smart plugs
  Shelly (Gen1/Gen2) + Tasmota native; Tuya deferred to the Python daemon.
- **GPU fan PWM (opt-in)**: `[daemon] fan_control` with a configurable fan
  curve, anti-chatter band, capability probing, startup/exit driver-default
  restore (nvml warns: too-low fan speeds can burn the GPU).
- **Container/K8s attribution**: /proc cgroup v1+v2 parsing resolving GPU
  PIDs to docker/containerd/cri-o/podman container IDs and k8s pod names
  (PID-reuse-safe LRU, fuzz-tested, never panics).
- **Metrics on :9400**: same `zionstats_gpu_*` families as the Python
  exporter (existing Grafana board keeps working) + DCGM-style labels +
  fan/control/daemon gauges; OpenMetrics negotiation; /snapshot JSON.
- **zionstats-tui**: instant-attach SHM terminal UI (never inits NVML):
  LIVE/STALE/DAEMON-DOWN badges, GPU cards + sparklines, process table,
  alerts, pause/rate keys; launch/refresh benchmark flags.
- CI: cargo fmt/clippy/test/deny jobs; release: musl static amd64/arm64
  binaries; packaging/systemd/zionstatsd.service + packaging/install.sh.

### Changed
- **Privacy**: the shipped weather-example coordinates were a real city;
  defaults are now neutral (0, 0, no label) -- set yours via
  `--location LAT,LON[,LABEL]` or `[weather] lat/lon`. Test fixtures and
  demo defaults scrubbed to match.

### Fixed
- `zionstats --log --export` crashed on an internal tuple unpack (v0.4.0
  regression).
- **Safety**: `zionstats --log --demo` built real plugs from config, so the
  usage trigger could actuate mains hardware on synthetic data. Demo now
  forces a mock plug in dry_run, mirroring the TUI posture.

### Notes
- The Python TUI/daemon are unchanged and remain fully supported; feature
  parity (themes, record/replay, climate layer) is tracked for v0.6.

## v0.4.0 -- process forensics, btop UX, built-in exporter (2026-10-13)

### Added
- **Process forensics (nvitop-inspired, clean-room)**: selectable, scrollable
  process table (arrows/j/k, PgUp/PgDn, Home/End), tree view with /proc
  parent chains (`v`), per-process detail screen (Enter: full cmdline, cwd,
  exe, ancestor chain, scrollable environment, live SM% sparkline), tag/untag
  (Space), and signals T (SIGTERM) / K (SIGKILL) behind a typed `yes`
  confirmation, `--readonly`, and hard demo/replay blocks. `/` regex filter
  persists to the config; `NaType` + TTL-cache patterns ported from
  nvitop's Apache-2.0 API layer (see NOTICE).
- **btop-style UX (btop-inspired, Apache-2.0)**: full mouse support (click
  GPU cards / keyhints / process rows / menus, wheel scrolls the process
  list, `mouse = false` opt-out), an in-TUI options menu (`O`) that live-
  applies and persists every setting, panel toggles (`x`, btop box-toggle),
  named layout presets (`P` + `[presets.<name>]` in the config, optional
  one-key hotkeys), and auto-scaling power/PCIe graphs with an honest
  `max` legend.
- **Embedded Prometheus exporter + Grafana dashboard** (nvitop-exporter
  idea, implemented with the stdlib): `--export [HOST:]PORT` (default bind
  127.0.0.1:9835, matching nvitop-exporter so its Grafana board is a drop-in)
  serves `/metrics` (typed families, label escaping, counters for energy/
  ECC) and `/snapshot` (live JSON). Works with the TUI and headless
  (`--log --export`). Ships `dashboards/zionstats-prom.json` and a
  `--doctor` check.
- Grid layouts now cap card rows to the visible height instead of drawing
  into the bottom chrome on small terminals with many GPUs.
- `ZION_CONFIG` environment variable overrides the config file location.
- `NO_COLOR` support: all color is dropped (bold kept) when the variable is set.

### Fixed
- The process table rendered the ENC% value under the COMMAND column below
  120 columns (cells and columns were filtered independently since v0.1).
- `c` (cooling-mode cycle) silently erased its own status toast and forced an
  immediate re-sample: a stray block of init code ran right after the flash
  message was set.
- Forced `--layout hero` with more than one GPU rendered only the first card;
  hero now falls back to the grid layout (and the `l` cycle skips hero on
  multi-GPU systems).
- The process list queried NVML twice per refresh (the second call only fed
  the per-user rollup).
- `begin_frame` cleared the cell grid cell-by-cell; blank rows are now
  cached and applied with slice assignment (C speed).
- Duplicate version headings in this file (v0.2.1/0.2.2/0.2.3).

## v0.3.0 -- usage-triggered blower, headless automation (2026-10-12)

### Added
- **Usage-triggered blower mode**: `[cooling] trigger = "usage"` powers the
  blower ON when GPU utilization crosses `usage_util` (default 5%) and OFF
  after the GPU has been idle continuously for `idle_off_s` (default 600 s)
  AND cooled below `resume_temp`. The hot-guard extends an existing ON (a
  hot-idling GPU keeps blowing) but never starts the blower on its own.
- Blower automation now runs headless: `zionstats --log` evaluates the same
  fail-safe controller and announces every event on stderr.
- `--doctor` reports the configured cooling trigger.
- `a` key: one-press blower AUTO arm/disarm (usage-triggered); header key
  `w` relabeled Weather.

### Fixed
- `Config` silently aborted `[weather]`/`[cooling]` loading: `_coerce` hit a
  KeyError on `refresh_min` (a weather key checked against the cooling
  table) that was swallowed at startup. Sections now load fully and are
  coerced like flat keys, so a garbage `lat` or `usage_util` can no longer
  crash TUI startup or silently fall back to defaults.
- Help overlay (`?`) now lists the `w`/`o`/`c` cooling keys.

## v0.2.4 -- the btop graph engine, properly (2026-10-12)

### Changed
- **Graphs now use a faithful port of btop's rendering engine**
  (`btop_draw.cpp` `Graph::_create`): each character cell encodes the
  transition between the previous and current sample through btop's 5x5
  quadrant symbol tables (`▗▐▖▄▟▌▙█`), so traces render connected with
  slopes instead of detached steps.
- **Per-row gradient shading** -- btop's signature look: glyph rows are
  tinted at output time from a 101-step interpolation of the theme's color
  stops (hot at the top, cool at the bottom), replacing per-column
  value-coloring. Bars shade along their width the same way (btop
  `Meter::operator()`).
- `graph_style = btop` is the new default; `block`, `tty` (btop's ░▒█
  fallback), `braille`, `ascii` remain available.

### Fixed
- Threshold rules no longer cut through graph data (dashed rules stamp only
  blank cells).
- `no_zero` bottom-row clamp ports btop's floor semantics (continuous
  baseline without distorting values).

### Notes
- Reference implementation: aristocratos/btop `src/btop_draw.cpp`
  (Apache-2.0). Port written from scratch against that algorithm.

## v0.2.3 -- graph rendering overhaul (2026-10-12)

### Fixed
- **Graphs no longer render as faint dotted dust.** The default graph style
  is now solid half-block area charts (`█▀▄`, btop `tty-block` style) which
  render correctly in every monospace font -- braille dot glyphs require
  special font coverage (DejaVu/JetBrains) and turn to noise otherwise
  (same root cause as btop issue #1507).

### Added
- `graph_style = block | braille | ascii` config + `--graph-style` flag
  (mirrors btop's `graph_symbol`); `braille` stays opt-in, `ascii` is the
  bulletproof fallback.
- Adaptive color degradation: terminals exposing fewer than 256 colors now
  map the palette to base 16 instead of silently rendering near-invisible
  text; `--doctor` reports TERM, detected color count, locale codeset and
  the active graph style, and warns when the environment would degrade.
- `--demo --only 0` now filters the synthetic devices.

### Notes
- Braille remains available and is unchanged for users whose fonts support
  it; block style was verified side-by-side against user screenshots where
  bars/borders rendered fine but braille did not.

## v0.2.2 -- packaging, Apache-2.0, release engineering (2026-10-12)

### Changed
- **Relicensed MIT -> Apache-2.0** (explicit patent grant; still permissive).
- Windows support: `windows-curses` installs automatically on win32.

### Added
- **Binary releases**: tag-triggered GitHub Actions build self-contained
  PyInstaller binaries for linux_amd64, linux_arm64, macOS_amd64,
  macOS_arm64, windows_amd64, plus universal wheels/sdist and a combined
  `checksums.txt`, attached to the GitHub Release.
- `plugs` and `dev` optional-dependency extras.

### Notes
- 32-bit targets (`386`/`arm`) are source-only: no GitHub runners exist for
  them and no NVIDIA-driver audience does either; `pip install` covers any
  Python-capable platform.
- macOS has no NVIDIA NVML for modern drivers; the binary runs the TUI and
  `--demo` there.

## v0.2.1 -- OSS-readiness pass (2026-10-12)

### Fixed
- **Light themes no longer break on dark terminals**: `paperwhite` (and any
  future light theme) always paints its canvas; the `b` toggle is a no-op for
  light themes instead of leaving dark-on-dark text.

### Hardening
- Config type coercion: hand-edited garbage values (string intervals, boolean
  temps, unknown themes/layouts/sorts, bad coordinates) are sanitized to
  defaults at startup instead of crashing the TUI.
- Config file is written with `0600` permissions -- it can hold Tuya local
  keys and Home Assistant tokens.
- Security sweep: no eval/exec/subprocess/pickle anywhere; weather is the
  only outbound call (keyless HTTPS, disableable); plug control is LAN-only.

### Added
- Theme + config QA suite: all 16 themes render a full frame at three sizes,
  light-theme background visibility asserted, garbage-config fuzz test,
  config permission test.
- GitHub Actions CI: unit tests + CLI smoke across Python 3.9-3.13 (GPU-free).
- README: badges, security notes, contributing guide, FAQ.

## v0.2.0 -- Climate-Aware Cooling (2026-10-12)

Optional, settings-gated cooling automation plus a world-first weather layer.
v0.1 behavior is preserved: everything new defaults off except the weather
display, and no network call happens when the weather layer is disabled.

### Added

- **Weather layer (keyless, global)** -- Open-Meteo current conditions +
  24h outlook for any lat/lon (shipped default: a hot, humid tropical city
  as the example -- set yours). Derived metrics: dew point, Stull wet-bulb,
  GPU-vs-ambient delta, cooling-effectiveness index, condensation advisory
  (GPU below dew point), muggy-heat warnings. Disk-cached and offline-tolerant.
  Header ambient chip; full panel on `w`.
- **Climate panel (`w`)** -- conditions row, blower state line,
  BLOWER VALUE effectiveness bar, 24H OUTLOOK temperature sparkline,
  rotating advisories.
- **Cooling controller** -- hysteresis state machine (trigger 82C / resume
  78C / min ambient delta 8C by default), minimum on/off times, maximum
  continuous duty cap, stale-sample and stale-weather failsafes (force-off),
  readback verification with retry x2 then alert. Modes: `off` (default),
  `dry_run` (decides + logs, never actuates), `auto` (actuates), `manual`
  (`o` key toggle). Mode cycling on `c`, persisted to config.
- **Smart-plug backends (local-first)** -- Tuya (tinytuya, covers most
  Alexa-compatible plugs), TP-Link Kasa (python-kasa), Shelly (Gen1/Gen2
  REST), Tasmota (HTTP), Home Assistant (REST + long-lived token), plus a
  MockPlug for demos/tests. Plugs stay paired with Alexa/Google for voice;
  zionstats drives them on the LAN so automation survives internet outages.
  `zionstats --plug-test "NAME on|off|status"` verifies hardware safely.
- **Demo showcase** -- `--demo` now runs a tropical weather curve and
  a mock blower in dry-run mode; demo can never touch real plugs.
- **CLI**: `--location LAT,LON[,LABEL]`, `--no-weather`,
  `--cooling off|dry_run|auto|manual`, `--plug-test`, and `--doctor`
  coverage for weather reachability, plug reachability, and an explicit
  warning when AUTO actuation is armed.
- **Insight layer**: verdict chips now fire alerts on SW-Power-Cap /
  SW-Thermal engagement (edge-triggered, no spam).
- **Grafana-style detail panel** -- the GPU detail is now a grid of six
  live time-series tiles (TEMPERATURE, POWER, GPU UTIL, VRAM, SM CLOCK,
  PCIe GB/s) with threshold lines (slowdown/shutdown, 92% power cap, 90%
  VRAM, 70% PCIe saturation), adaptive `now/lo/hi` legends, and gradient
  braille rendering; falls back to instrument meter bars on narrow panes.

### Fixed

- Alerts strip referenced the wrong store when the first alert fired
  (latent since v0.1, surfaced by the demo's scripted throttle arc).

## v0.1.0 -- initial release (2026-10-10)

btop-grade NVIDIA GPU observatory: gradient braille graphs, verdict chips,
throttle/clock-deficit/PCIe-saturation/energy insights, per-process GPU
utilization, record/replay/snapshot/log, --doctor, --demo, 16 themes with
live-preview picker, hero/grid/compact layouts. Inspired by nvitop and btop.
