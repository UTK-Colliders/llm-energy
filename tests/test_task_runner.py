"""Task runner arithmetic with a canned power backend and fake docker layer."""

from pathlib import Path

import pytest

import llm_energy.task_runner as tr
from llm_energy.config import ExpectedOutput, ImageSpec, PhaseSpec, TaskSpec
from llm_energy.docker_util import ContainerRun
from llm_energy.schemas import BaselineResult, MachineInfo, PowerSample, PowerTrace


class CannedBackend:
    """Emits 1 s samples at a fixed power; t_rel 0 == start() call."""

    def __init__(self, watts: float, n: int):
        self.trace = PowerTrace(backend="canned", samples=[
            PowerSample(t_rel_s=float(i), elapsed_s=1.0, combined_mw=watts * 1000)
            for i in range(n)
        ])

    def start(self, interval_ms, raw_path):
        Path(raw_path).write_text("canned")

    def stop(self):
        return self.trace


def make_task(tmp_path, expect=(), phases=None):
    return TaskSpec(
        name="fake", description="", task_dir=tmp_path,
        images={"native": ImageSpec(variant="native", tag="fake:latest")},
        default_image="native",
        phases=list(phases) if phases else [PhaseSpec(name="run", command=["true"])],
        mounts=[], workdir=None, env={}, timeout_s=60,
        expected_outputs=list(expect), metadata={"nevents": 1},
    )


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Fake docker: container 'runs' from t=2 s to t=6 s after backend start."""
    t = {"now": 1000.0}

    monkeypatch.setattr(tr.docker_util, "docker_available", lambda: True)
    monkeypatch.setattr(tr.docker_util, "ensure_image", lambda img: None)
    monkeypatch.setattr(tr.docker_util, "is_emulated", lambda tag: False)
    monkeypatch.setattr(tr.docker_util, "image_arch", lambda tag: "arm64")
    monkeypatch.setattr(tr.machine_info, "collect", lambda: MachineInfo(chip="test"))

    def fake_run_container(task, image, run_dir, stats_interval_s=2.0,
                           command=None, timeout_s=None):
        return ContainerRun(exit_code=0, wall_time_s=4.0,
                            t0_monotonic=1002.0, t1_monotonic=1006.0,
                            cpu_seconds=3.5, stats_samples=[{"CPUPerc": "95%"}])

    monkeypatch.setattr(tr.docker_util, "run_container", fake_run_container)
    monkeypatch.setattr(tr.time, "monotonic", lambda: t["now"])
    return t


def scripted_runs(monkeypatch, windows, exit_codes=None):
    """Fake successive containers occupying the given (t0, t1) monotonic windows."""
    exit_codes = exit_codes or [0] * len(windows)
    seen = []

    def fake(task, image, run_dir, stats_interval_s=2.0, command=None,
             timeout_s=None):
        i = len(seen)
        seen.append(command)
        t0, t1 = windows[i]
        return ContainerRun(exit_code=exit_codes[i], wall_time_s=t1 - t0,
                            t0_monotonic=t0, t1_monotonic=t1, cpu_seconds=1.0)

    monkeypatch.setattr(tr.docker_util, "run_container", fake)
    return seen


def test_energy_window_and_net(patched, tmp_path):
    # 10 W for 10 s; container occupies seconds [2, 6] -> gross = 40 J
    backend = CannedBackend(watts=10.0, n=10)
    baseline = BaselineResult(
        duration_s=60, mean_w=4.0, std_w=0.1, joules=240, n_samples=60,
        min_w=3.9, max_w=4.2, backend="canned", docker_running=True,
        machine=MachineInfo(chip="test"))

    res = tr.run_task(make_task(tmp_path), backend, tmp_path,
                      baseline=baseline, baseline_ref="baseline-x.json",
                      interval_ms=1000, sleep_fn=lambda s: None)

    assert res.gross_joules == pytest.approx(40.0)
    assert res.mean_power_w == pytest.approx(10.0)
    # net = 40 - 4 W * 4 s = 24
    assert res.net_joules == pytest.approx(24.0)
    assert res.alignment_uncertainty_j == pytest.approx(10.0)
    assert res.container_cpu_seconds == 3.5
    assert res.wall_time_s == 4.0
    assert not res.emulated
    assert res.exit_code == 0


def test_no_baseline_means_no_net(patched, tmp_path):
    res = tr.run_task(make_task(tmp_path), CannedBackend(10.0, 10), tmp_path,
                      interval_ms=1000, sleep_fn=lambda s: None)
    assert res.net_joules is None
    assert res.baseline_mean_w is None


def test_failed_container_raises(patched, monkeypatch, tmp_path):
    def failing_run(task, image, run_dir, stats_interval_s=2.0, command=None,
                    timeout_s=None):
        return ContainerRun(exit_code=2, wall_time_s=1.0,
                            t0_monotonic=1002.0, t1_monotonic=1003.0)
    monkeypatch.setattr(tr.docker_util, "run_container", failing_run)
    with pytest.raises(tr.TaskRunError, match="code 2"):
        tr.run_task(make_task(tmp_path), CannedBackend(10.0, 10), tmp_path,
                    interval_ms=1000, sleep_fn=lambda s: None)


def test_expected_output_recorded_with_lhe_fingerprint(patched, monkeypatch, tmp_path):
    exp = ExpectedOutput(glob="out/events.lhe", record_as="lhe_file")

    def run_and_produce(task, image, run_dir, stats_interval_s=2.0, command=None,
                        timeout_s=None):
        (run_dir / "out").mkdir(parents=True)
        (run_dir / "out" / "events.lhe").write_text(
            "<LesHouchesEvents>\n<event>\n1 2 3\n</event>\n</LesHouchesEvents>\n")
        return ContainerRun(exit_code=0, wall_time_s=4.0,
                            t0_monotonic=1002.0, t1_monotonic=1006.0)

    monkeypatch.setattr(tr.docker_util, "run_container", run_and_produce)
    res = tr.run_task(make_task(tmp_path, expect=[exp]), CannedBackend(10.0, 10),
                      tmp_path, interval_ms=1000, sleep_fn=lambda s: None)
    assert "lhe_file" in res.outputs
    assert res.outputs["lhe_file_nevents"] == 1
    assert len(res.outputs["lhe_file_events_sha256"]) == 64


def test_coordinating_session_is_stamped_from_the_environment(
        patched, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1234")
    res = tr.run_task(make_task(tmp_path), CannedBackend(10.0, 10), tmp_path,
                      interval_ms=1000, sleep_fn=lambda s: None)
    assert res.coordinating_session_id == "sess-1234"
    assert res.started_at and res.ended_at


def test_no_session_id_outside_a_claude_session(patched, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    res = tr.run_task(make_task(tmp_path), CannedBackend(10.0, 10), tmp_path,
                      interval_ms=1000, sleep_fn=lambda s: None)
    assert res.coordinating_session_id is None


def test_missing_expected_output_flagged(patched, tmp_path):
    exp = ExpectedOutput(glob="out/never.lhe", record_as="lhe_file")
    res = tr.run_task(make_task(tmp_path, expect=[exp]), CannedBackend(10.0, 10),
                      tmp_path, interval_ms=1000, sleep_fn=lambda s: None)
    assert res.outputs["missing_expected"] == ["out/never.lhe"]


# --- multi-phase -------------------------------------------------------------

COMPILE_RUN = [PhaseSpec(name="compile", command=["make"], description="build"),
               PhaseSpec(name="generate", command=["run"], description="events")]


def test_phases_are_measured_separately(patched, monkeypatch, tmp_path):
    # 10 W throughout; compile occupies [1,3] s of the trace, generate [4,9]
    seen = scripted_runs(monkeypatch, [(1001.0, 1003.0), (1004.0, 1009.0)])
    baseline = BaselineResult(
        duration_s=60, mean_w=4.0, std_w=0.1, joules=240, n_samples=60,
        min_w=3.9, max_w=4.2, backend="canned", docker_running=True,
        machine=MachineInfo(chip="test"))

    res = tr.run_task(make_task(tmp_path, phases=COMPILE_RUN),
                      CannedBackend(10.0, 12), tmp_path, baseline=baseline,
                      interval_ms=1000, sleep_fn=lambda s: None)

    assert [p.name for p in res.phases] == ["compile", "generate"]
    compile_p, generate_p = res.phases
    assert compile_p.gross_joules == pytest.approx(20.0)     # 10 W x 2 s
    assert generate_p.gross_joules == pytest.approx(50.0)    # 10 W x 5 s
    assert compile_p.net_joules == pytest.approx(20.0 - 4.0 * 2)
    assert generate_p.net_joules == pytest.approx(50.0 - 4.0 * 5)
    assert compile_p.description == "build"
    # each phase ran its own command
    assert seen == [["make"], ["run"]]


def test_totals_are_the_sum_over_phases(patched, monkeypatch, tmp_path):
    scripted_runs(monkeypatch, [(1001.0, 1003.0), (1004.0, 1009.0)])
    res = tr.run_task(make_task(tmp_path, phases=COMPILE_RUN),
                      CannedBackend(10.0, 12), tmp_path,
                      interval_ms=1000, sleep_fn=lambda s: None)

    assert res.gross_joules == pytest.approx(sum(p.gross_joules for p in res.phases))
    assert res.wall_time_s == pytest.approx(sum(p.wall_time_s for p in res.phases))
    assert res.container_cpu_seconds == pytest.approx(2.0)   # 1.0 per phase


def test_single_phase_task_still_reports_one_phase(patched, tmp_path):
    res = tr.run_task(make_task(tmp_path), CannedBackend(10.0, 10), tmp_path,
                      interval_ms=1000, sleep_fn=lambda s: None)
    assert [p.name for p in res.phases] == ["run"]
    assert res.phases[0].gross_joules == pytest.approx(res.gross_joules)


def test_a_failed_phase_skips_the_rest(patched, monkeypatch, tmp_path):
    seen = scripted_runs(monkeypatch, [(1001.0, 1003.0), (1004.0, 1009.0)],
                         exit_codes=[1, 0])
    with pytest.raises(tr.TaskRunError, match="phase 'compile' exited with code 1"):
        tr.run_task(make_task(tmp_path, phases=COMPILE_RUN),
                    CannedBackend(10.0, 12), tmp_path,
                    interval_ms=1000, sleep_fn=lambda s: None)
    assert seen == [["make"]], "the generate phase must not run after compile fails"


def test_compiler_output_is_attributed_to_the_phase_that_wrote_it(
        patched, monkeypatch, tmp_path):
    """The check that a compile/run split actually held."""
    def build_then_run(task, image, run_dir, stats_interval_s=2.0, command=None,
                       timeout_s=None):
        if command == ["make"]:
            (run_dir / "matrix.o").write_bytes(b"obj")
            (run_dir / "libmodel.a").write_bytes(b"ar")
            return ContainerRun(exit_code=0, wall_time_s=2.0,
                                t0_monotonic=1001.0, t1_monotonic=1003.0)
        (run_dir / "events.lhe").write_text("<event>1</event>")
        return ContainerRun(exit_code=0, wall_time_s=5.0,
                            t0_monotonic=1004.0, t1_monotonic=1009.0)

    monkeypatch.setattr(tr.docker_util, "run_container", build_then_run)

    res = tr.run_task(make_task(tmp_path, phases=COMPILE_RUN),
                      CannedBackend(10.0, 12), tmp_path,
                      interval_ms=1000, sleep_fn=lambda s: None)

    compile_p, generate_p = res.phases
    assert compile_p.build_artifacts_written == 2
    assert sorted(compile_p.build_artifact_examples) == ["libmodel.a", "matrix.o"]
    # nothing compiled during event generation: the split held
    assert generate_p.build_artifacts_written == 0
