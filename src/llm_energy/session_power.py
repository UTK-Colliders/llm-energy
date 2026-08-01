"""Measure package power across a whole coordination session.

`task_runner.run_task` measures the container's window only. This wraps the
interactive agent itself, so the *local* cost of coordination — the agent
thinking, reading files, running exploratory commands — is measured on the
same instrument, with the task run nested inside the same trace.

The wrapper cannot know the child's session id up front (Claude Code mints it
at startup), so sessions are attributed afterwards by transcript start time
falling inside the measured window.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from llm_energy import machine_info
from llm_energy.schemas import (BaselineResult, PowerTrace, SessionPowerResult,
                                now_iso)
from llm_energy.session.locate import find_sessions_started_in_window


def _default_run(command: list[str], cwd: Path | None) -> int:
    """Run the child with stdio inherited — the agent session is interactive."""
    return subprocess.run(command, cwd=str(cwd) if cwd else None).returncode


def measure_session(command: list[str],
                    backend,
                    out_dir: Path,
                    baseline: BaselineResult | None = None,
                    baseline_ref: str | None = None,
                    interval_ms: int = 1000,
                    cwd: Path | None = None,
                    sleep_fn=time.sleep,
                    run_fn=None) -> SessionPowerResult:
    """Run `command` to completion under power measurement.

    Mirrors run_task's sequence: start sampling, pad ~2 intervals, run the
    child, pad ~1 interval, stop, then integrate only the child's window out
    of the trace.
    """
    run_fn = run_fn or _default_run

    ts = now_iso().replace(":", "-")
    run_dir = out_dir / f"session-power-{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_path = run_dir / "power-trace.txt"

    interval_s = interval_ms / 1000.0
    backend_start_mono = time.monotonic()
    backend.start(interval_ms, raw_path)
    try:
        sleep_fn(2 * interval_s)
        t_start_wall = datetime.now(timezone.utc)
        t0 = time.monotonic()
        exit_code = run_fn(command, cwd)
        t1 = time.monotonic()
        t_end_wall = datetime.now(timezone.utc)
        sleep_fn(1 * interval_s)
    except BaseException:
        # No session happened, so there is nothing to salvage — but the
        # sampler must not be left running. On macOS that is a root
        # powermetrics process writing to disk forever.
        try:
            backend.stop()
        except Exception:
            pass
        raise

    notes: list[str] = []
    power_ok = True
    try:
        trace = backend.stop()
    except Exception as e:
        # The session already happened and cannot be replayed. Keep the result
        # so its session ids survive; mark the energy unusable.
        power_ok = False
        trace = PowerTrace(backend=getattr(backend, "name", "unknown"))
        notes.append(
            f"power sampling failed ({e}) — the energy figures for this "
            "session are unusable, but the linked session ids are still valid, "
            "so the token side can be recovered with analyze-session")

    wall_time_s = t1 - t0
    gross_j = trace.integrate_joules(t_start=t0 - backend_start_mono,
                                     t_end=t1 - backend_start_mono)
    mean_w = gross_j / wall_time_s if wall_time_s > 0 else 0.0

    net_j = None
    baseline_mean_w = None
    if baseline is not None and power_ok:
        baseline_mean_w = baseline.mean_w
        net_j = gross_j - baseline.mean_w * wall_time_s

    sessions = find_sessions_started_in_window(t_start_wall, t_end_wall, cwd=cwd)
    if not sessions and cwd is not None:
        # A transcript records its own cwd, which can differ from ours through
        # symlinks (/tmp vs /private/tmp on macOS). Widen rather than lose the
        # link, and say that the match is weaker.
        sessions = find_sessions_started_in_window(t_start_wall, t_end_wall)
        if sessions:
            notes.append(f"session matched on time window alone — the recorded "
                         f"cwd differs from {cwd}")
    if not sessions:
        notes.append(
            "no Claude Code session started inside the measured window — the "
            "power number is still valid, but nothing links it to a transcript "
            "(was the agent resumed rather than started fresh?)")
    elif len(sessions) > 1:
        notes.append(f"{len(sessions)} sessions started inside the window; all "
                     "are attributed to this measurement")

    return SessionPowerResult(
        command=list(command),
        wall_time_s=wall_time_s,
        gross_joules=gross_j,
        baseline_ref=baseline_ref,
        baseline_mean_w=baseline_mean_w,
        net_joules=net_j,
        mean_power_w=mean_w,
        alignment_uncertainty_j=mean_w * interval_s,
        backend=trace.backend,
        power_ok=power_ok,
        exit_code=exit_code,
        started_at=t_start_wall.isoformat(),
        ended_at=t_end_wall.isoformat(),
        session_ids=[s.session_id for s in sessions],
        cwd=str(cwd) if cwd else None,
        power_trace_file=str(raw_path) if raw_path.exists() else None,
        notes=notes,
        machine=machine_info.collect(),
    )
