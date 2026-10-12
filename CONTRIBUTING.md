# Contributing

Issues and PRs welcome. Keep it lean — that is the point of the project.

## Development loop

```bash
pip install -e .
python tests/test_climate.py && python tests/test_cooling.py \
  && python tests/test_themes_and_config.py && python tests/test_graphs.py
zionstats --demo          # everything interesting, no GPU needed
```

CI runs the test files directly with `python` on a 3.9-3.13 matrix (plus a
`--demo --snapshot` smoke), so tests must stay runnable without pytest and
without an NVIDIA GPU.

## Ground rules

- **Runtime stays stdlib-first**: `nvidia-ml-py` is the only hard dependency;
  plug SDKs (tinytuya, python-kasa) are lazy-imported optional extras. No
  requests/httpx/aiohttp by design.
- **New plug backends must be local-first** — no cloud calls. If it needs a
  vendor cloud to function, it does not belong here.
- **Cooling changes must keep the fail-safe posture**: dry-run decides but
  never actuates, every actuation is verified by readback and retried before
  alerting, stale telemetry force-offs, duty cap, minimum on/off times, and
  `--demo` can never touch real plugs. New trigger logic belongs beside the
  existing `_decide` / `_decide_usage` in `zionstats/cooling.py` and needs
  table tests in `tests/test_cooling.py` (injectable clock — see the
  existing usage-trigger tests).
- **No PII in the repo**: no real device IDs, local keys, coordinates, or
  hostnames in code, docs, tests, or fixtures. Use the placeholders the
  tests already ship with.
- Keep every feature working with `--demo` so screenshots and CI stay
  hardware-free.
