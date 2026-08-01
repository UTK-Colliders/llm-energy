"""Orchestrate a measured task run: power trace around a docker container."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from llm_energy import docker_util, machine_info
from llm_energy.build_artifacts import (count_build_artifacts,
                                        unattributed_artifacts)
from llm_energy.config import TaskSpec
from llm_energy.schemas import (BaselineResult, PhaseResult, TaskRunResult,
                                now_iso)
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
    # One sampler spanning every phase; each phase's window is carved out of
    # the same trace, so phases stay directly comparable and the sampler is
    # started once no matter how many phases there are.
    backend.start(interval_ms, raw_path)
    runs: list[tuple] = []
    try:
        sleep_fn(2 * interval_s)
        for phase in task.phases:
            p_start_wall = datetime.now(timezone.utc)
            run = docker_util.run_container(task, image, run_dir,
                                            command=phase.command,
                                            timeout_s=phase.timeout_s)
            p_end_wall = datetime.now(timezone.utc)
            runs.append((phase, run, p_start_wall, p_end_wall))
            if run.exit_code != 0:
                break       # later phases depend on this one; don't run them
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

    def window_energy(r) -> tuple[float, float, float | None]:
        """(gross, mean W, net) over a container run's slice of the trace.

        t_rel 0 ≈ backend start; residual skew is ≤ ~1 sample interval and is
        reported as alignment uncertainty.
        """
        gross = trace.integrate_joules(t_start=r.t0_monotonic - backend_start_mono,
                                       t_end=r.t1_monotonic - backend_start_mono)
        mean = gross / r.wall_time_s if r.wall_time_s > 0 else 0.0
        net = (gross - baseline.mean_w * r.wall_time_s
               if baseline is not None else None)
        return gross, mean, net

    phase_results: list[PhaseResult] = []
    for phase, r, p_start, p_end in runs:
        p_gross, p_mean, p_net = window_energy(r)
        build = count_build_artifacts(run_dir, p_start, p_end)
        phase_results.append(PhaseResult(
            name=phase.name, command=list(phase.command),
            wall_time_s=r.wall_time_s, gross_joules=p_gross, net_joules=p_net,
            mean_power_w=p_mean, alignment_uncertainty_j=p_mean * interval_s,
            exit_code=r.exit_code, container_cpu_seconds=r.cpu_seconds,
            started_at=p_start.isoformat(), ended_at=p_end.isoformat(),
            build_artifacts_written=build.count,
            build_artifact_examples=build.examples,
            description=phase.description))

    # Totals are the sum over phases, so a single-phase task is unchanged and
    # a multi-phase one stays consistent with its own breakdown.
    run = runs[-1][1]
    wall_time_s = sum(p.wall_time_s for p in phase_results)
    gross_j = sum(p.gross_joules for p in phase_results)
    mean_w = gross_j / wall_time_s if wall_time_s > 0 else 0.0
    cpu_seconds = [p.container_cpu_seconds for p in phase_results
                   if p.container_cpu_seconds is not None]
    # each phase window carries its own ~1-interval skew; they add
    alignment_j = sum(p.alignment_uncertainty_j for p in phase_results)
    stats_samples = [s for _, r, _, _ in runs for s in r.stats_samples]

    net_j = None
    baseline_mean_w = None
    if baseline is not None:
        baseline_mean_w = baseline.mean_w
        net_j = gross_j - baseline.mean_w * wall_time_s

    outputs: dict = {"container_log": str(run_dir / "container.log"),
                     "stdout_tail": run.stdout_tail if run.exit_code != 0 else ""}
    orphaned = unattributed_artifacts(
        run_dir, sum(p.build_artifacts_written for p in phase_results))
    if orphaned:
        outputs["unattributed_build_artifacts"] = orphaned
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
        wall_time_s=wall_time_s,
        gross_joules=gross_j,
        baseline_ref=baseline_ref,
        baseline_mean_w=baseline_mean_w,
        net_joules=net_j,
        mean_power_w=mean_w,
        alignment_uncertainty_j=alignment_j,
        backend=trace.backend,
        container_cpu_seconds=sum(cpu_seconds) if cpu_seconds else None,
        docker_stats_samples=stats_samples,
        exit_code=run.exit_code,
        outputs=outputs,
        power_trace_file=str(raw_path) if raw_path.exists() else None,
        task_metadata=task.metadata,
        machine=machine_info.collect(),
        coordinating_session_id=coordinating_session_id,
        started_at=started_at,
        ended_at=now_iso(),
        phases=phase_results,
    )
    if run.exit_code != 0:
        failed = phase_results[-1].name
        raise TaskRunError(
            f"task phase '{failed}' exited with code {run.exit_code}; see "
            f"{run_dir}/container.log. (partial result not written — energy "
            f"for a failed task is not comparable)")
    return result
