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
