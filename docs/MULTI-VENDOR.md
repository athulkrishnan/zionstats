# Multi-vendor support matrix (v0.6.0)

zionstats reads three GPU families through one provider interface. The
rule across all of them: a metric the hardware or driver does not expose
is `None` -- the UI hides it and the exporter omits the series. Nothing
is synthesized, and no number in this table is claimed without either a
fixture test behind it or a real hardware run (hardware rows below that
came from fixture tests only are marked as such).

Select a backend with `--provider auto|nvidia|amd|intel` (or the
`provider` config key; default `auto` = NVML first, then a drm vendor
scan). `--demo` runs all four synthetic GPUs and needs no hardware.

## Coverage

| Metric | NVIDIA (NVML) | AMD (amdgpu sysfs) | Intel (i915/xe, experimental) |
|---|---|---|---|
| GPU utilization | yes | yes (`gpu_busy_percent`) | fdinfo cycle deltas only |
| Memory bandwidth util | yes | no | no |
| VRAM used / total | yes | yes (`mem_info_vram_*`) | per-client sums / xe only |
| Edge temperature | yes | yes (hwmon `temp1`) | yes (hwmon `temp1`) |
| Junction / hottest part | driver >= v615 (`TEMPERATURE_GPU_MAX`) | yes (hwmon `temp2`) | no |
| Memory temperature | yes (`TEMPERATURE_MEMORY`) | yes (hwmon `temp3`, when present) | no |
| Power draw + limits | yes | yes (`power1_*`, microwatts) | no (by design, see gaps) |
| SM / memory clocks | yes | yes (`freq1/2_input`, `pp_dpm_*` fallback) | i915 `rps_cur_freq`; xe `freq0_cur_freq` |
| Fan percent | yes | yes (`pwm1`) | no |
| Fan RPM | driver >= 565 | no (sysfs has no rpm) | no |
| Energy counter | yes (mJ counter) | no (session Wh via power integration) | no |
| PCIe gen/width/throughput | yes | no | no |
| ECC | yes | no | no |
| Per-process GPU util | yes (pmon) | yes (fdinfo cycles) | yes (fdinfo cycles) |
| Per-process VRAM | yes | yes (fdinfo `drm-memory-vram`) | yes (fdinfo memory keys) |
| Session Wh | yes | yes (power integration) | no |
| Verdict chips + alerts | full | degraded (no PCIe/ECC inputs) | degraded (no power input) |

## Requirements

- NVIDIA: driver 5xx+ with `nvidia-ml-py` installed. Every v0.6 tops-up
  (hottest-part temp, fan RPM, replay counter, event-reason counters) is
  capability-probed against the installed binding at runtime; an older
  driver simply reports those as absent (check `zionstats --doctor`).
- AMD: amdgpu sysfs needs no extra packages. Per-process stats need
  kernel >= 5.14, which introduced the per-client DRM fdinfo
  (`drm-cycles-*` / `drm-total-cycles-*` / `drm-memory-*` keys); nvtop
  documents the same requirement for its AMD process stats. Reference
  implementations for the sysfs quantities: amdgpu_top and nvtop's AMD
  path.
- Intel: i915 or xe. Both drivers expose the same fdinfo client keys;
  clock paths differ (`gt/gt0/rps_cur_freq` on i915,
  `tile0/gt0/freq0_cur_freq` on xe -- xe units are fixture-pinned in
  this release pending hardware verification). Marked experimental:
  per-engine aggregation is best-effort across driver generations.

## Known gaps (kept None, never invented)

| Gap | Why |
|---|---|
| Intel power draw | client GPUs expose no GPU-attributable power on this path; RAPL is socket-level and would be a lie |
| AMD memory-bandwidth util | amdgpu sysfs has no equivalent of NVML's memory utilization |
| Intel VRAM total on older kernels | `mem_info_vram_total` appears on newer xe kernels; the VRAM-fraction UI degrades to 0 gracefully |
| RTX 50 memory-junction temp | no documented NVML sensor (nor a 12V-2x6 connector temp) on consumer Blackwell |
| AMD PCIe / ECC telemetry | not exposed on the plain sysfs surface used here |

## Exporter changes

Every `zionstats_gpu_*` series now carries a `vendor` label (additive;
existing Grafana boards keep working). New optional series:
`zionstats_gpu_temperature_celsius{sensor="hottest"}`,
`zionstats_gpu_fan_rpm`, `zionstats_gpu_pcie_replay_counter_total`, and
the plug-metering families `zionstats_plug_power_watts` /
`zionstats_plug_energy_watt_hour`. Absent hardware omits the series
entirely.

## Ports note

This repo's exporter defaults to `127.0.0.1:9835` (nvitop-exporter
compatible, so its Grafana boards are drop-ins). The Rust sibling
(zionstatsd) serves the same families on `:9400`, where dcgm-exporter
also lives by convention -- do not run both on the same host without
changing one of them.

## Hardware verification status

The parsers above are pinned by fixture trees under `tests/fixtures/`
(unit conversions, garbage tolerance, delta math). Rows marked from
fixture tests have not been validated against live AMD/Intel hardware
in this release; per the project's documentation rule, real-hardware
confirmations may only be filled in from actual runs. NVIDIA rows are
exercised on the reference box via `zionstats --doctor`.
