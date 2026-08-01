"""Task runner arithmetic with a canned power backend and fake docker layer."""

from pathlib import Path

import pytest

import llm_energy.task_runner as tr
from llm_energy.config import ExpectedOutput, ImageSpec, TaskSpec
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


def make_task(tmp_path, expect=()):
    return TaskSpec(
        name="fake", description="", task_dir=tmp_path,
        images={"native": ImageSpec(variant="native", tag="fake:latest")},
        default_image="native", command=["true"], mounts=[], workdir=None,
        env={}, timeout_s=60,
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

    def fake_run_container(task, image, run_dir, stats_interval_s=2.0):
        return ContainerRun(exit_code=0, wall_time_s=4.0,
                            t0_monotonic=1002.0, t1_monotonic=1006.0,
                            cpu_seconds=3.5, stats_samples=[{"CPUPerc": "95%"}])

    monkeypatch.setattr(tr.docker_util, "run_container", fake_run_container)
    monkeypatch.setattr(tr.time, "monotonic", lambda: t["now"])
    return t


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
    def failing_run(task, image, run_dir, stats_interval_s=2.0):
        return ContainerRun(exit_code=2, wall_time_s=1.0,
                            t0_monotonic=1002.0, t1_monotonic=1003.0)
    monkeypatch.setattr(tr.docker_util, "run_container", failing_run)
    with pytest.raises(tr.TaskRunError, match="code 2"):
        tr.run_task(make_task(tmp_path), CannedBackend(10.0, 10), tmp_path,
                    interval_ms=1000, sleep_fn=lambda s: None)


def test_expected_output_recorded_with_lhe_fingerprint(patched, monkeypatch, tmp_path):
    exp = ExpectedOutput(glob="out/events.lhe", record_as="lhe_file")

    def run_and_produce(task, image, run_dir, stats_interval_s=2.0):
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


def test_missing_expected_output_flagged(patched, tmp_path):
    exp = ExpectedOutput(glob="out/never.lhe", record_as="lhe_file")
    res = tr.run_task(make_task(tmp_path, expect=[exp]), CannedBackend(10.0, 10),
                      tmp_path, interval_ms=1000, sleep_fn=lambda s: None)
    assert res.outputs["missing_expected"] == ["out/never.lhe"]
