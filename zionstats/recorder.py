"""Recorder: one-shot JSON snapshots, ndjson(.gz) trace record/replay,
and headless jsonl logging (gputop-style regression capture)."""

from __future__ import annotations

import gzip
import json
import sys
import time
from pathlib import Path

from .nvml_layer import GpuProcess, Sample


def frame_to_dict(env: dict, samples: list[Sample], processes: list[GpuProcess]) -> dict:
    return {
        "ts": samples[0].ts if samples else time.time(),
        "env": env,
        "devices": [{k: v for k, v in vars(s).items()} for s in samples],
        "processes": [{k: v for k, v in vars(p).items()} for p in processes],
    }


def snapshot_json(provider) -> str:
    env = provider.environment()
    samples = provider.sample()
    processes = provider.processes()
    return json.dumps(frame_to_dict(env, samples, processes), indent=2)


class Recorder:
    """Appends one JSON frame per tick.  .gz handled transparently."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh = None
        if str(path).endswith(".gz"):
            self._fh = gzip.open(self.path, "at", encoding="utf-8")
        else:
            self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, env: dict, samples: list[Sample], processes: list[GpuProcess]) -> None:
        assert self._fh is not None
        self._fh.write(json.dumps(frame_to_dict(env, samples, processes)) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class ReplayProvider:
    """Replays a recorded trace into the TUI through the same provider
    interface as live NVML.  `exhausted` flips True at end of trace."""

    def __init__(self, path: str | Path) -> None:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[operator]
            self._frames = [json.loads(line) for line in f if line.strip()]
        if not self._frames:
            raise ValueError(f"empty trace: {path}")
        self._i = 0
        self.exhausted = False

    def environment(self) -> dict:
        return dict(self._frames[0].get("env", {})) | {"provider": "replay"}

    def devices(self) -> list[dict]:
        return [
            {"index": d["index"], "name": d.get("name", "?"), "uuid": d.get("uuid", "")}
            for d in self._frames[0].get("devices", [])
        ]

    def _cur(self) -> dict:
        return self._frames[min(self._i, len(self._frames) - 1)]

    def sample(self) -> list[Sample]:
        if self._i >= len(self._frames):
            self.exhausted = True
            return self._to_samples(self._frames[-1])
        frame = self._frames[self._i]
        self._i += 1
        return self._to_samples(frame)

    @staticmethod
    def _to_samples(frame: dict) -> list[Sample]:
        out = []
        for d in frame.get("devices", []):
            known = {k for k in Sample.__dataclass_fields__}
            out.append(Sample(**{k: v for k, v in d.items() if k in known}))
        return out

    def processes(self) -> list[GpuProcess]:
        frame = self._cur()
        known = {k for k in GpuProcess.__dataclass_fields__}
        return [
            GpuProcess(**{k: v for k, v in p.items() if k in known})
            for p in frame.get("processes", [])
        ]

    def proc_info(self, pids: list[int], with_env: bool = False) -> dict:
        """Traces don't capture /proc forensics; the UI degrades gracefully."""
        return {}

    def close(self) -> None:
        pass


def record_for(provider, path: str, duration: float, interval: float) -> int:
    """Headless capture loop used by `zionstats --record FILE --duration N`."""
    rec = Recorder(path)
    env = provider.environment()
    n = 0
    deadline = time.time() + duration
    try:
        while time.time() < deadline:
            t0 = time.time()
            rec.write(env, provider.sample(), provider.processes())
            n += 1
            time.sleep(max(0.0, interval - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
    finally:
        rec.close()
        provider.close()
    return n


def log_loop(provider, path: str, interval: float, cfg=None, exporter=None) -> None:
    """Headless logging until SIGINT (gputop regression-capture heritage).
    `exporter` (optional) receives every frame so --log --export works."""
    rec = Recorder(path)
    env = provider.environment()
    is_demo = env.get("provider") == "demo"
    ctl = None
    if cfg is not None and cfg.cooling.get("enabled"):
        from .cooling import CoolingController, config_from_section
        from .plugs import MockPlug, Plug, build_plugs

        plugs: list[Plug]
        if is_demo:
            # --demo must never reach mains hardware: mock plug + dry_run,
            # same posture as the TUI's demo showcase
            plugs = [MockPlug("blower (mock)")]
            cc = config_from_section(cfg.cooling, mode="dry_run")
            print(
                "zionstats: demo provider -- cooling runs in dry_run with a "
                "mock plug (real plugs are never touched in demo)",
                file=sys.stderr,
            )
        else:
            plugs, errors = build_plugs(cfg.cooling.get("plugs", []))
            for err in errors:
                print(f"zionstats: plug config: {err}", file=sys.stderr)
            cc = config_from_section(cfg.cooling, mode=cfg.cooling.get("mode", "off"))

        def _announce(ev) -> None:
            print(f"zionstats blower [{ev.kind}] {ev.msg}", file=sys.stderr, flush=True)

        ctl = CoolingController(plugs, cc, on_event=_announce)
    print(f"zionstats: logging to {path} every {interval:.1f}s -- Ctrl-C to stop", file=sys.stderr)
    try:
        while True:
            t0 = time.time()
            samples = provider.sample()
            procs = provider.processes()
            rec.write(env, samples, procs)
            if ctl is not None:
                hot = None
                for s in samples:
                    if hot is None or (s.temp_gpu or 0) > (hot.temp_gpu or 0):
                        hot = s
                if hot is not None:
                    ctl.evaluate(hot, None)  # no weather headless; usage trigger ignores it
            if exporter is not None:
                exporter.publish(env, samples, procs, {})
            time.sleep(max(0.0, interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        rec.close()
        provider.close()
        if exporter is not None:
            exporter.stop()
