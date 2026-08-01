"""Orchestrate a measured task run: power trace around a docker container."""

from __future__ import annotations

import time
from pathlib import Path

from llm_energy import docker_util, machine_info
from llm_energy.config import TaskSpec
from llm_energy.schemas import BaselineResult, TaskRunResult, now_iso
from llm_energy.session.locate import current_session_id


class TaskRunError(RuntimeError):
    pass


def run_task(task: TaskSpec,
             backend,
             out_dir: Path,
             baseline: BaselineResult | None = None,
             baseline_ref: str | None = None,
             image_variant: str | None = None,
             interval_ms: int = 1000,
             sleep_fn=time.sleep) -> TaskRunResult:
    """Measure the energy of one task run.

    Sequence: start power sampling, pad ~2 intervals, run the container to
    completion, pad ~1 interval, stop sampling, then integrate only the
    container's wall-time window out of the trace.
    """
    image = task.image(image_variant)
    if not docker_util.docker_available():
        raise TaskRunError("docker daemon is not reachable")
    docker_util.ensure_image(image)
    emulated = docker_util.is_emulated(image.tag)

    ts = now_iso().replace(":", "-")
    run_dir = out_dir / f"run-{task.name}-{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_path = run_dir / "power-trace.txt"

    # If the coordinating agent invoked this, record which conversation it was
    # so the run can be paired with its token cost without mtime guessing.
    coordinating_session_id = current_session_id()
    started_at = now_iso()

    interval_s = interval_ms / 1000.0
    backend_start_mono = time.monotonic()
    backend.start(interval_ms, raw_path)
    try:
        sleep_fn(2 * interval_s)
        run = docker_util.run_container(task, image, run_dir)
        sleep_fn(1 * interval_s)
    except BaseException:
        # Don't leave the sampler running: on macOS that is a root
        # powermetrics process writing to disk indefinitely.
        try:
            backend.stop()
        except Exception:
            pass
        raise
    trace = backend.stop()

    # Trace-relative window of the container run. t_rel 0 ≈ backend start;
    # residual skew is ≤ ~1 sample interval and reported as uncertainty.
    w_start = run.t0_monotonic - backend_start_mono
    w_end = run.t1_monotonic - backend_start_mono
    gross_j = trace.integrate_joules(t_start=w_start, t_end=w_end)
    mean_w = gross_j / run.wall_time_s if run.wall_time_s > 0 else 0.0

    net_j = None
    baseline_mean_w = None
    if baseline is not None:
        baseline_mean_w = baseline.mean_w
        net_j = gross_j - baseline.mean_w * run.wall_time_s

    outputs: dict = {"container_log": str(run_dir / "container.log"),
                     "stdout_tail": run.stdout_tail if run.exit_code != 0 else ""}
    missing = []
    for exp in task.expected_outputs:
        matches = sorted(run_dir.glob(exp.glob))
        if matches:
            f = matches[0]
            outputs[exp.record_as] = str(f)
            outputs[f"{exp.record_as}_size_bytes"] = f.stat().st_size
            if f.name.endswith((".lhe", ".lhe.gz")):
                # physics-content fingerprint so runs coordinated by different
                # LLMs can be checked for identical events (llm-energy verify-events)
                from llm_energy.lhe import event_summary
                n_ev, ev_hash = event_summary(f)
                outputs[f"{exp.record_as}_nevents"] = n_ev
                outputs[f"{exp.record_as}_events_sha256"] = ev_hash
        else:
            missing.append(exp.glob)
    if run.exit_code == 0 and missing:
        outputs["missing_expected"] = missing

    result = TaskRunResult(
        task_name=task.name,
        image=image.tag,
        image_arch=docker_util.image_arch(image.tag),
        emulated=emulated,
        wall_time_s=run.wall_time_s,
        gross_joules=gross_j,
        baseline_ref=baseline_ref,
        baseline_mean_w=baseline_mean_w,
        net_joules=net_j,
        mean_power_w=mean_w,
        alignment_uncertainty_j=mean_w * interval_s,
        backend=trace.backend,
        container_cpu_seconds=run.cpu_seconds,
        docker_stats_samples=run.stats_samples,
        exit_code=run.exit_code,
        outputs=outputs,
        power_trace_file=str(raw_path) if raw_path.exists() else None,
        task_metadata=task.metadata,
        machine=machine_info.collect(),
        coordinating_session_id=coordinating_session_id,
        started_at=started_at,
        ended_at=now_iso(),
    )
    if run.exit_code != 0:
        raise TaskRunError(
            f"task exited with code {run.exit_code}; see {run_dir}/container.log. "
            f"(partial result not written — energy for a failed task is not "
            f"comparable)")
    return result
