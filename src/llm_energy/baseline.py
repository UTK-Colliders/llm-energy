"""Idle-power baseline measurement."""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from llm_energy import machine_info
from llm_energy.docker_util import docker_available
from llm_energy.schemas import BaselineResult, now_iso


def measure_baseline(backend, duration_s: float, interval_ms: int,
                     out_dir: Path) -> BaselineResult:
    """Sample idle power for duration_s. The Docker VM should be running but
    idle (its idle draw belongs in the baseline that gets subtracted)."""
    ts = now_iso().replace(":", "-")
    raw_path = out_dir / f"baseline-trace-{ts}.txt"
    backend.start(interval_ms, raw_path)
    time.sleep(duration_s)
    trace = backend.stop()

    watts = [s.combined_mw / 1000.0 for s in trace.samples]
    return BaselineResult(
        duration_s=trace.duration_s(),
        mean_w=trace.mean_watts(),
        std_w=statistics.stdev(watts) if len(watts) > 1 else 0.0,
        joules=trace.integrate_joules(),
        n_samples=len(trace.samples),
        min_w=min(watts) if watts else 0.0,
        max_w=max(watts) if watts else 0.0,
        backend=trace.backend,
        docker_running=docker_available(),
        machine=machine_info.collect(),
        raw_trace_file=str(raw_path) if raw_path.exists() else None,
    )


def find_latest_baseline(results_dir: Path, machine) -> Path | None:
    """Newest baseline JSON from the same machine (chip + hostname match)."""
    from llm_energy.schemas import load_baseline

    candidates = sorted(results_dir.glob("baseline-*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            b = load_baseline(p)
        except (ValueError, KeyError, TypeError):
            continue
        if b.machine.chip == machine.chip and b.machine.hostname == machine.hostname:
            return p
    return None
