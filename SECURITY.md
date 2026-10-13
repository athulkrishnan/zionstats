# Security policy

## Scope

zionstats is a local desktop tool. Its security surface is deliberately tiny:

- **Network egress**: exactly one optional outbound call — keyless HTTPS to
  the Open-Meteo weather API, disk-cached, fully disableable
  (`[weather] enabled = false` or `--no-weather`). No telemetry, no accounts.
- **LAN device control** (optional, off by default): smart plugs are driven
  directly over your LAN (Tuya/Kasa/Shelly/Tasmota local protocols or a Home
  Assistant REST token). Nothing is sent to any cloud by zionstats.
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
    is no auth layer.

## Secrets handling

Plug credentials (Tuya local keys, HA tokens) live in
`~/.config/zionstats/config.toml`, written with `0600` permissions. The
tinytuya helper files (`tinytuya.json`, `devices.json`, `snapshot.json`,
`tuya-raw.json`) also contain keys — they are gitignored; keep them that way
and never commit them.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab →
Report a vulnerability) rather than a public issue. Include the affected
code path and, if possible, a reproduction. Given the project's scope, the
most valuable reports involve: the cooling controller actuating against
user intent, credential exposure, or the weather layer doing more network
I/O than documented.
