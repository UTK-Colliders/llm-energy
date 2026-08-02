"""Session-window power measurement: arithmetic and session attribution."""

from pathlib import Path

import pytest

import llm_energy.session_power as sp
from llm_energy.schemas import (BaselineResult, MachineInfo, PowerSample,
                                PowerTrace)
from llm_energy.session.locate import SessionFileInfo


class CannedBackend:
    """Emits 1 s samples at a fixed power; t_rel 0 == start() call."""

    def __init__(self, watts: float, n: int):
        self.trace = PowerTrace(backend="canned", samples=[
            PowerSample(t_rel_s=float(i), elapsed_s=1.0, combined_mw=watts * 1000)
            for i in range(n)
        ])
        self.started_with = None

    def start(self, interval_ms, raw_path):
        self.started_with = interval_ms
        Path(raw_path).write_text("canned")

    def stop(self):
        return self.trace


def session_info(session_id="s1"):
    from datetime import datetime, timezone
    return SessionFileInfo(
        path=Path(f"/tmp/{session_id}.jsonl"), session_id=session_id,
        mtime=datetime(2026, 8, 1, tzinfo=timezone.utc), size_bytes=10)


@pytest.fixture
def patched(monkeypatch):
    """Child 'runs' from t=2 s to t=6 s after the backend starts."""
    clock = iter([1000.0, 1002.0, 1006.0])
    monkeypatch.setattr(sp.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(sp.machine_info, "collect", lambda: MachineInfo(chip="test"))
    monkeypatch.setattr(sp, "find_sessions_started_in_window",
                        lambda *a, **k: [session_info()])


def run(backend, tmp_path, baseline=None, run_fn=None, command=("claude",)):
    return sp.measure_session(
        list(command), backend, tmp_path, baseline=baseline,
        baseline_ref="b.json" if baseline else None, interval_ms=1000,
        cwd=tmp_path, sleep_fn=lambda s: None,
        run_fn=run_fn or (lambda cmd, cwd: 0))


def test_energy_window_and_net(patched, tmp_path):
    baseline = BaselineResult(
        duration_s=60, mean_w=4.0, std_w=0.1, joules=240, n_samples=60,
        min_w=3.9, max_w=4.2, backend="canned", docker_running=True,
        machine=MachineInfo(chip="test"))

    res = run(CannedBackend(10.0, 10), tmp_path, baseline=baseline)

    # 10 W over the child's [2, 6] s window
    assert res.gross_joules == pytest.approx(40.0)
    assert res.wall_time_s == pytest.approx(4.0)
    assert res.mean_power_w == pytest.approx(10.0)
    # net = 40 - 4 W * 4 s
    assert res.net_joules == pytest.approx(24.0)
    assert res.alignment_uncertainty_j == pytest.approx(10.0)
    assert res.backend == "canned"


def test_no_baseline_means_no_net(patched, tmp_path):
    res = run(CannedBackend(10.0, 10), tmp_path)
    assert res.net_joules is None
    assert res.baseline_mean_w is None


def test_child_exit_code_and_command_recorded(patched, tmp_path):
    res = run(CannedBackend(10.0, 10), tmp_path, run_fn=lambda cmd, cwd: 3,
              command=("claude", "-p", "do the job"))
    assert res.exit_code == 3
    assert res.command == ["claude", "-p", "do the job"]


def test_sessions_started_in_window_are_linked(patched, tmp_path):
    res = run(CannedBackend(10.0, 10), tmp_path)
    assert res.session_ids == ["s1"]
    assert res.notes == []
    assert res.started_at and res.ended_at


def test_no_linked_session_is_flagged(patched, monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "find_sessions_started_in_window", lambda *a, **k: [])
    res = run(CannedBackend(10.0, 10), tmp_path)
    assert res.session_ids == []
    assert any("no Claude Code session started" in n for n in res.notes)


def test_multiple_linked_sessions_are_flagged_and_kept(patched, monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "find_sessions_started_in_window",
                        lambda *a, **k: [session_info("s1"), session_info("s2")])
    res = run(CannedBackend(10.0, 10), tmp_path)
    assert res.session_ids == ["s1", "s2"]
    assert any("2 sessions started" in n for n in res.notes)


def test_raw_trace_is_kept(patched, tmp_path):
    res = run(CannedBackend(10.0, 10), tmp_path)
    assert res.power_trace_file and Path(res.power_trace_file).exists()


def test_sampler_is_stopped_when_the_child_cannot_launch(patched, tmp_path):
    """Otherwise a root `sudo powermetrics` is left writing to disk forever."""
    backend = CannedBackend(10.0, 10)
    stopped = []
    backend.stop = lambda: stopped.append(True) or backend.trace

    def cannot_launch(cmd, cwd):
        raise FileNotFoundError("claude")

    with pytest.raises(FileNotFoundError):
        run(backend, tmp_path, run_fn=cannot_launch)
    assert stopped, "backend.stop() must run even when the child never started"


def test_sampler_failure_keeps_the_session_link(patched, tmp_path):
    """A session cannot be replayed — losing its ids to a sampler crash is worse
    than losing the energy figure."""
    backend = CannedBackend(10.0, 10)

    def dying_stop():
        raise RuntimeError("powermetrics exited early (rc=1)")
    backend.stop = dying_stop

    res = run(backend, tmp_path)
    assert res.power_ok is False
    assert res.session_ids == ["s1"]              # the recoverable half survives
    assert res.net_joules is None
    assert any("power sampling failed" in n for n in res.notes)


def test_cwd_mismatch_falls_back_to_a_time_only_match(patched, monkeypatch,
                                                      tmp_path):
    """Transcripts record their own cwd, which differs under symlinks such as
    macOS's /tmp -> /private/tmp."""
    calls = []

    def by_cwd(t0, t1, cwd=None, **kw):
        calls.append(cwd)
        return [] if cwd is not None else [session_info("elsewhere")]

    monkeypatch.setattr(sp, "find_sessions_started_in_window", by_cwd)
    res = run(CannedBackend(10.0, 10), tmp_path)
    assert res.session_ids == ["elsewhere"]
    assert any("matched on time window alone" in n for n in res.notes)
    assert calls == [tmp_path, None]


# --- observing the agent's own containers -------------------------------------

class FakeEvents:
    """Stands in for the docker event stream with a scripted set of windows."""

    def __init__(self, windows, ok=True):
        self.windows, self.ok = windows, ok
        self.healthy = ok

    def __call__(self, out_path):
        return self

    def start(self):
        return self.ok

    def stop(self):
        return self.windows


def window(start_offset, end_offset, cid="abc", image="mg5:1"):
    """Offsets are seconds after the session's own start."""
    from datetime import timedelta
    from llm_energy.docker_events import ContainerWindow
    base = _SESSION_START[0]
    return ContainerWindow(
        container_id=cid, image=image,
        started_at=base + timedelta(seconds=start_offset),
        ended_at=None if end_offset is None else base + timedelta(seconds=end_offset))


_SESSION_START = [None]


@pytest.fixture
def with_events(patched, monkeypatch):
    """Pin the session's wall-clock start so container offsets are meaningful."""
    from datetime import datetime, timezone
    start = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    _SESSION_START[0] = start

    # start and end must differ, or clamping an unfinished container to the
    # session end would give it a zero-length window
    from datetime import timedelta
    stamps = iter([start, start + timedelta(seconds=4)])

    class FixedClock:
        def now(self, tz=None):
            return next(stamps)
    monkeypatch.setattr(sp, "datetime", FixedClock())
    return start


def install(monkeypatch, windows, ok=True):
    monkeypatch.setattr(sp, "DockerEventRecorder", FakeEvents(windows, ok=ok))


def test_container_energy_is_attributed_from_observed_windows(
        with_events, monkeypatch, tmp_path):
    # session occupies trace seconds [2,6]; one container runs [1,3] s into it,
    # i.e. trace seconds [3,5] -> 2 s at 10 W = 20 J
    install(monkeypatch, [window(1, 3)])
    res = run(CannedBackend(10.0, 12), tmp_path)

    assert res.container_joules == pytest.approx(20.0)
    assert res.container_wall_s == pytest.approx(2.0)
    assert len(res.containers) == 1
    assert res.containers[0].image == "mg5:1"
    assert res.containers[0].gross_joules == pytest.approx(20.0)


def test_outside_container_energy_is_the_remainder(with_events, monkeypatch,
                                                   tmp_path):
    install(monkeypatch, [window(1, 3)])
    res = run(CannedBackend(10.0, 12), tmp_path)
    # 40 J over the whole session, 20 J of it inside the container
    assert res.gross_joules == pytest.approx(40.0)
    assert res.outside_container_joules() == pytest.approx(20.0)


def test_overlapping_containers_are_not_double_counted(with_events, monkeypatch,
                                                       tmp_path):
    install(monkeypatch, [window(0, 3, cid="a"), window(1, 4, cid="b")])
    res = run(CannedBackend(10.0, 12), tmp_path)
    # union is [0,4] of the session = 4 s at 10 W, not 3+3
    assert res.container_joules == pytest.approx(40.0)
    assert len(res.containers) == 2


def test_container_still_running_is_attributed_up_to_the_session_end(
        with_events, monkeypatch, tmp_path):
    """Dropping it would bill its energy to coordination and report a clean
    zero for compute — the instrument failing in the shape of a measurement."""
    install(monkeypatch, [window(1, None)])
    res = run(CannedBackend(10.0, 12), tmp_path)

    # the container was up from 1 s into the session until the end at 4 s
    assert len(res.containers) == 1
    assert res.containers[0].still_running is True
    assert res.container_joules == pytest.approx(30.0)   # 3 s at 10 W
    assert res.outside_container_joules() == pytest.approx(10.0)
    assert any("cut off mid-flight" in n for n in res.notes)
    assert any("docker rm -f" in n for n in res.notes)


def test_an_unfinished_container_no_longer_reads_as_zero_compute(
        with_events, monkeypatch, tmp_path):
    install(monkeypatch, [window(1, None)])
    res = run(CannedBackend(10.0, 12), tmp_path)
    assert res.container_joules > 0
    # and it must not swallow the whole session either
    assert res.container_joules < res.gross_joules


def test_no_docker_means_no_split_but_a_valid_total(with_events, monkeypatch,
                                                    tmp_path):
    install(monkeypatch, [], ok=False)
    res = run(CannedBackend(10.0, 12), tmp_path)
    assert res.gross_joules == pytest.approx(40.0)
    assert res.container_joules is None
    assert res.outside_container_joules() is None
    assert any("docker not available" in n for n in res.notes)


def test_broken_event_stream_is_not_reported_as_zero_containers(
        with_events, monkeypatch, tmp_path):
    """`docker events` exits immediately when the daemon is unreachable. The
    empty log it leaves must not read as 'the agent ran no containers', which
    would bill every joule of compute to coordination."""
    class Unhealthy(FakeEvents):
        def __init__(self):
            super().__init__([], ok=True)
            self.healthy = False
    monkeypatch.setattr(sp, "DockerEventRecorder", Unhealthy())

    res = run(CannedBackend(10.0, 12), tmp_path)
    assert res.gross_joules == pytest.approx(40.0)     # total still valid
    assert res.container_joules is None                 # but no split claimed
    assert res.outside_container_joules() is None
    assert any("did not run" in n for n in res.notes)


def test_healthy_stream_with_no_containers_reports_a_real_zero(
        with_events, monkeypatch, tmp_path):
    install(monkeypatch, [])
    res = run(CannedBackend(10.0, 12), tmp_path)
    assert res.container_joules == pytest.approx(0.0)
    assert res.outside_container_joules() == pytest.approx(40.0)


def test_container_windows_outside_the_session_flag_clock_disagreement(
        with_events, monkeypatch, tmp_path):
    """Event times come from the daemon, which on macOS runs in its own VM."""
    install(monkeypatch, [window(-9000, -8000)])
    res = run(CannedBackend(10.0, 12), tmp_path)
    assert any("daemon clock disagrees" in n for n in res.notes)


# --- net-of-idle accounting ---------------------------------------------------

def real_session():
    """The first real open run: 395 s, an idle-dominated session, no containers.

    Gross 260.5 J over a 0.434 W baseline leaves 89.1 J of marginal cost — so
    two thirds of the gross figure is draw the Mac would have had anyway.
    """
    from llm_energy.schemas import SessionPowerResult
    return SessionPowerResult(
        command=["claude"], wall_time_s=395.0, gross_joules=260.5,
        baseline_ref="b.json", baseline_mean_w=0.434, net_joules=89.1,
        mean_power_w=0.66, alignment_uncertainty_j=0.66,
        backend="powermetrics", exit_code=0,
        container_joules=0.0, container_wall_s=0.0)


def test_coordination_is_net_of_idle_not_gross():
    sp = real_session()
    assert sp.outside_container_joules() == pytest.approx(89.1, abs=0.5)
    assert sp.outside_container_joules(net=False) == pytest.approx(260.5)


def test_with_no_containers_coordination_equals_the_session_net():
    sp = real_session()
    assert sp.outside_container_joules() == pytest.approx(sp.net_joules, abs=0.5)


def test_container_energy_is_also_netted():
    sp = real_session()
    sp.container_joules = 100.0
    sp.container_wall_s = 60.0
    # 100 J inside the containers, less 0.434 W of idle over their 60 s
    assert sp.container_net_joules() == pytest.approx(100.0 - 0.434 * 60)
    # and the two net terms still add up to the session net
    assert (sp.container_net_joules() + sp.outside_container_joules()
            == pytest.approx(sp.net_joules, abs=0.5))


def test_without_a_baseline_both_terms_fall_back_to_gross():
    sp = real_session()
    sp.baseline_mean_w = None
    sp.container_joules = 100.0
    assert sp.outside_container_joules() == pytest.approx(160.5)
    assert sp.container_net_joules() == pytest.approx(100.0)
    assert sp.energy_basis() == "gross"


# --- a baseline that is not an idle floor ------------------------------------

def contaminated_baseline_session():
    """The run that produced negative energy in every derived row.

    324 s at 20.48 W mean, measured against a 30 s baseline that came out at
    20.95 W — captured while the Mac was still busy from the preflight. The
    subtraction then reports -162.9 J for the session and -148.7 J of compute
    across five containers that demonstrably ran MadGraph.
    """
    from llm_energy.schemas import SessionPowerResult
    return SessionPowerResult(
        command=["claude"], wall_time_s=324.0, gross_joules=6626.7,
        baseline_ref="b.json", baseline_mean_w=20.95, net_joules=-162.9,
        mean_power_w=20.48, alignment_uncertainty_j=20.48,
        backend="powermetrics", exit_code=0,
        container_joules=2602.9, container_wall_s=131.0)


def test_a_baseline_above_the_session_mean_is_rejected():
    sp = contaminated_baseline_session()
    assert not sp.baseline_usable()
    assert sp.energy_basis() == "gross — idle baseline rejected"


def test_rejecting_the_baseline_falls_back_to_gross_not_to_negatives():
    sp = contaminated_baseline_session()
    compute = sp.container_net_joules()
    outside = sp.outside_container_joules()
    assert compute == pytest.approx(2602.9)
    assert outside == pytest.approx(6626.7 - 2602.9)
    assert compute > 0 and outside > 0, "energy spent is not negative"
    # and the parts still make up the gross whole they are now quoted against
    assert compute + outside == pytest.approx(sp.gross_joules)


def test_a_baseline_below_the_session_mean_is_still_subtracted():
    sp = contaminated_baseline_session()
    sp.baseline_mean_w = 8.0
    assert sp.baseline_usable()
    assert sp.container_net_joules() == pytest.approx(2602.9 - 8.0 * 131.0)
    assert sp.energy_basis() == "net of idle baseline"


def test_the_rejection_is_recorded_when_the_session_is_measured(patched, tmp_path):
    """The note has to travel with the JSON, not just the terminal."""
    # 1 W session against a 5 W "idle" baseline
    baseline = BaselineResult(
        duration_s=30.0, mean_w=5.0, std_w=0.1, joules=150.0, n_samples=30,
        min_w=4.9, max_w=5.1, backend="canned", docker_running=True,
        machine=MachineInfo(chip="test"))
    res = run(CannedBackend(watts=1.0, n=10), tmp_path, baseline=baseline)
    assert not res.baseline_usable()
    assert any("idle baseline" in n and "rejected" in n for n in res.notes)
    # the recorded net stays in the JSON as captured; it is the accessors and
    # the report that refuse to present it
    assert res.net_joules is not None and res.net_joules < 0
