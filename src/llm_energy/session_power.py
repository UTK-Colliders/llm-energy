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
from llm_energy.docker_events import DockerEventRecorder, merge_windows
from llm_energy.schemas import (BaselineResult, ObservedContainer, PowerTrace,
                                SessionPowerResult, now_iso)
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
    events = DockerEventRecorder(run_dir / "docker-events.jsonl")
    events_ok = events.start()
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
        events.stop()
        raise

    container_windows = events.stop()
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

    # Attribute energy to the containers the agent ran. Wall-clock event times
    # map onto the trace through the session's own start, which was captured
    # in both clocks.
    def to_trace_rel(when) -> float:
        return (when - t_start_wall).total_seconds() + (t0 - backend_start_mono)

    observed: list[ObservedContainer] = []
    container_j = container_wall = None
    if power_ok and events_ok and events.healthy:
        outside_window = [w for w in container_windows
                          if w.ended_at is not None
                          and (w.ended_at < t_start_wall or w.started_at > t_end_wall)]
        if outside_window:
            # Event timestamps come from the daemon, which on macOS lives in a
            # VM with its own clock. Windows landing outside the session cannot
            # be mapped onto the trace, and quietly contributing zero would
            # read as "that container used no energy".
            notes.append(
                f"{len(outside_window)} container window(s) fall outside the "
                "measured session, so the daemon clock disagrees with this "
                "machine's — the in/out-of-container split is unreliable")
        for w in container_windows:
            if w.ended_at is None:
                notes.append(f"container {w.container_id} ({w.image}) was still "
                             "running at session end; its energy is not attributed")
                continue
            e = trace.integrate_joules(t_start=to_trace_rel(w.started_at),
                                       t_end=to_trace_rel(w.ended_at))
            observed.append(ObservedContainer(
                container_id=w.container_id, image=w.image,
                started_at=w.started_at.isoformat(),
                ended_at=w.ended_at.isoformat(), wall_time_s=w.wall_time_s,
                gross_joules=e,
                mean_power_w=e / w.wall_time_s if w.wall_time_s > 0 else 0.0))
        # union, so overlapping containers are counted once and the figure
        # stays subtractable from the session total
        merged = merge_windows(container_windows)
        container_j = sum(trace.integrate_joules(t_start=to_trace_rel(a),
                                                 t_end=to_trace_rel(b))
                          for a, b in merged)
        container_wall = sum((b - a).total_seconds() for a, b in merged)
    elif power_ok:
        notes.append(
            "the docker event stream did not run"
            + ("" if events_ok else " (docker not available)")
            + ", so energy could not be split into in-container and "
            "out-of-container parts — the session total is unaffected")

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
        containers=observed,
        container_joules=container_j,
        container_wall_s=container_wall,
        notes=notes,
        machine=machine_info.collect(),
    )
