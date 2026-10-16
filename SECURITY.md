# Security policy

## Scope

zionstats is a local desktop tool. Its security surface is deliberately tiny:

- **Network egress**: exactly one optional outbound call — keyless HTTPS to
  the Open-Meteo weather API, disk-cached, fully disableable
  (`[weather] enabled = false` or `--no-weather`). No telemetry, no accounts.
- **LAN device control** (optional, off by default): smart plugs are driven
  directly over your LAN (Tuya/Kasa/Shelly/Tasmota local protocols or a Home
  Assistant REST token). Nothing is sent to any cloud by zionstats.
  Note: a LAN man-in-the-middle can spoof plug verification readbacks
  seen by the control loop. On any actuator error or failed readback
  the daemon fails safe to driver-default fan control; do not treat
  plug state as a safety mechanism.
- **Mains actuation** (optional): the cooling controller switches real
  plugs. It ships disabled, supports a `dry_run` mode that never actuates,
  verifies every command by readback, and force-offs on stale telemetry.
- **Local metrics exporter** (optional, off by default, v0.4):
  `--export [HOST:]PORT` or `[export] enabled = true` serves two read-only
  HTTP endpoints from the monitor process itself (`/metrics` in Prometheus
  text format and `/snapshot` as JSON). Defaults: bind `127.0.0.1:9835`,
  no authentication by design.
  - What it exposes: GPU identifiers (name, UUID), utilization, power,
    temperatures, clocks, counters — and, when `[export] expose_processes`
    is `true` (the default), the **PID, username, and command line of every
    process using the GPU**. On shared/multi-user hosts that is sensitive
    information; set `expose_processes = false`.
  - Binding to a non-loopback address (`--export 0.0.0.0:9835`) exposes all
    of the above to your network. Do not do this on an untrusted LAN; there
    is no auth layer by default.
    Optionally set `[export] token` (config only, never CLI) to require
    a Bearer token on /metrics and /snapshot.

## Secrets handling

Plug credentials (Tuya local keys, HA tokens) live in
`~/.config/zionstats/config.toml`, written with `0600` permissions. The
tinytuya helper files (`tinytuya.json`, `devices.json`, `snapshot.json`,
`tuya-raw.json`) also contain keys — they are gitignored; keep them that way
and never commit them.

## Rust daemon (v0.5)

- **Shared memory**: `/dev/shm/zionstats-<uid>`, mode 0600, uid-scoped by
  construction. Content is telemetry only (no secrets); readers validate an
  ABI tag and refuse mismatched layouts.
- **Metrics listener** (default `127.0.0.1:9400`): same exposure rules as
  the Python exporter -- PIDs/users/cmdlines of GPU processes unless
  `expose_processes = false`; no auth by design, keep loopback on shared
  hosts.
- **Fan PWM** (`[daemon] fan_control`, default **off**): hands GPU fans to
  manual control. The daemon restores driver control at startup, on
  SIGTERM, and after watchdog restarts, and falls back to driver control on
  any actuator error. NVML's own documentation warns that setting fan speeds
  too low can damage the GPU -- keep `fan_curve` floors sane and treat the
  curve as safety-critical config.
- **Two-writer guard**: the daemon takes an advisory flock on its SHM file
  and refuses to start if another writer (including the Python blower
  daemon) is active.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab →
Report a vulnerability) rather than a public issue. Include the affected
code path and, if possible, a reproduction. Given the project's scope, the
most valuable reports involve: the cooling controller actuating against
user intent, credential exposure, or the weather layer doing more network
I/O than documented.
