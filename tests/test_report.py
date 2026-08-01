from pathlib import Path

import pytest

from llm_energy.report import (Trial, check_event_identity, local_coordination,
                               render_comparison_markdown, render_markdown)
from llm_energy.schemas import (EnergyBand, MachineInfo, ModelUsage,
                                SessionEnergyResult, SessionPowerResult,
                                SessionUsage, TaskRunResult)

# a task run nested inside the session window used by make_session_power
TASK_START, TASK_END = "2026-08-01T10:05:00+00:00", "2026-08-01T10:15:00+00:00"


def make_task_result(events_hash="a" * 64, nevents=10000, lhe_path="",
                     net=500.0, backend="powermetrics", gross=9000.0,
                     started_at=TASK_START, ended_at=TASK_END) -> TaskRunResult:
    outputs = {}
    if events_hash:
        outputs = {"lhe_file": lhe_path, "lhe_file_nevents": nevents,
                   "lhe_file_events_sha256": events_hash}
    return TaskRunResult(
        task_name="madgraph-ttbar-lhe", image="llm-energy/mg5amc:test",
        image_arch="arm64", emulated=False, wall_time_s=600.0,
        gross_joules=gross, baseline_ref="b.json", baseline_mean_w=4.0,
        net_joules=net, mean_power_w=15.0, alignment_uncertainty_j=15.0,
        backend=backend, container_cpu_seconds=2300.0,
        outputs=outputs, machine=MachineInfo(chip="Apple M2"),
        started_at=started_at, ended_at=ended_at,
    )


def make_session_power(net=3000.0, gross=12000.0, backend="powermetrics",
                       started_at="2026-08-01T10:00:00+00:00",
                       ended_at="2026-08-01T10:30:00+00:00") -> SessionPowerResult:
    return SessionPowerResult(
        command=["claude", "-p", "do the job"], wall_time_s=1800.0,
        gross_joules=gross, baseline_ref="b.json",
        baseline_mean_w=4.0 if net is not None else None,
        net_joules=net, mean_power_w=6.7, alignment_uncertainty_j=6.7,
        backend=backend, exit_code=0, started_at=started_at, ended_at=ended_at,
        session_ids=["abc123"], machine=MachineInfo(chip="Apple M2"),
    )


def make_session_result(central=2000.0) -> SessionEnergyResult:
    usage = SessionUsage(
        session_ids=["abc123"], wall_time_s=900.0, assistant_turns=12,
        user_turns=3,
        per_model=[ModelUsage(model="claude-fable-5", input_tokens=1000,
                              output_tokens=5000, cache_creation_tokens=40000,
                              cache_read_tokens=200000, requests=12)])
    return SessionEnergyResult(
        usage=usage, coefficients_file="default.yaml", coefficients_sha256="x",
        total_band=EnergyBand(low_j=central / 4, central_j=central,
                              high_j=central * 4),
        per_model_bands={"claude-fable-5": EnergyBand(central / 4, central,
                                                      central * 4)},
        pue=1.2)


def test_ratio_uses_net_energy():
    t = Trial("run", make_task_result(net=500.0), make_session_result(2000.0))
    lo, mid, hi = t.ratio_band()
    assert mid == pytest.approx(2000.0 / 500.0)
    assert lo < mid < hi


def test_single_report_markdown_contents():
    md = render_markdown(Trial("run", make_task_result(), make_session_result()))
    assert "madgraph-ttbar-lhe" in md
    assert "E_LLM / E_task" in md
    assert "Caveats" in md
    assert "envelope" in md or "not a statistical" in md
    assert "package power" in md.lower() or "SoC" in md


def test_event_identity_identical_via_hashes():
    trials = [Trial("a", make_task_result(events_hash="h1" * 32), make_session_result()),
              Trial("b", make_task_result(events_hash="h1" * 32), make_session_result())]
    identity = check_event_identity(trials)
    assert identity.checked and identity.identical


def test_event_identity_differing_hashes_no_files():
    trials = [Trial("a", make_task_result(events_hash="h1" * 32, lhe_path="/gone/a.lhe"),
                    make_session_result()),
              Trial("b", make_task_result(events_hash="h2" * 32, lhe_path="/gone/b.lhe"),
                    make_session_result())]
    identity = check_event_identity(trials)
    assert identity.checked and identity.identical is False


def test_event_identity_unchecked_when_missing():
    trials = [Trial("a", make_task_result(events_hash=""), make_session_result()),
              Trial("b", make_task_result(), make_session_result())]
    identity = check_event_identity(trials)
    assert not identity.checked


def test_local_coordination_subtracts_the_nested_task_run():
    lc = local_coordination(make_session_power(net=3000.0),
                            make_task_result(net=500.0))
    assert lc.joules == pytest.approx(2500.0)
    assert lc.basis == "net of idle baseline"
    assert lc.notes == []


def test_local_coordination_falls_back_to_gross_without_baselines():
    lc = local_coordination(make_session_power(net=None, gross=12000.0),
                            make_task_result(net=None, gross=9000.0))
    assert lc.joules == pytest.approx(3000.0)
    assert lc.basis == "gross"
    assert lc.notes == []


def test_mixed_baselines_use_gross_and_say_so():
    lc = local_coordination(make_session_power(net=3000.0, gross=12000.0),
                            make_task_result(net=None, gross=9000.0))
    assert lc.joules == pytest.approx(3000.0)
    assert lc.basis == "gross"
    assert any("only one of the two runs had a baseline" in n for n in lc.notes)


def test_task_outside_the_session_window_is_flagged():
    lc = local_coordination(
        make_session_power(),
        make_task_result(started_at="2026-08-01T09:00:00+00:00",
                         ended_at="2026-08-01T09:30:00+00:00"))
    assert any("does not lie inside the measured session window" in n
               for n in lc.notes)


def test_unknown_task_window_is_flagged():
    lc = local_coordination(make_session_power(),
                            make_task_result(started_at="", ended_at=""))
    assert any("could not be verified" in n for n in lc.notes)


def test_backend_mismatch_is_flagged():
    lc = local_coordination(make_session_power(backend="powermetrics"),
                            make_task_result(backend="tdp-model"))
    assert any("not directly comparable" in n for n in lc.notes)


def test_negative_local_coordination_is_flagged():
    lc = local_coordination(make_session_power(net=100.0),
                            make_task_result(net=500.0))
    assert lc.joules == pytest.approx(-400.0)
    assert any("negative after subtraction" in n for n in lc.notes)


def test_total_coordination_band_adds_measured_local_to_estimated_llm():
    t = Trial("run", make_task_result(net=500.0), make_session_result(2000.0),
              session_power=make_session_power(net=3000.0))
    band = t.total_coordination_band()
    assert band.central_j == pytest.approx(2000.0 + 2500.0)
    assert band.low_j == pytest.approx(500.0 + 2500.0)
    assert band.high_j == pytest.approx(8000.0 + 2500.0)


def test_trial_without_session_power_has_no_local_terms():
    t = Trial("run", make_task_result(), make_session_result())
    assert t.local_coordination() is None
    assert t.total_coordination_band() is None
    assert "E_coord,local" not in render_markdown(t)


def test_report_markdown_includes_local_coordination():
    t = Trial("run", make_task_result(net=500.0), make_session_result(2000.0),
              session_power=make_session_power(net=3000.0))
    md = render_markdown(t)
    assert "E_coord,local" in md
    assert "E_coord,total" in md
    assert "measured on the same instrument as E_task" in md


def test_report_markdown_surfaces_local_coordination_warnings():
    t = Trial("run", make_task_result(backend="tdp-model"),
              make_session_result(), session_power=make_session_power())
    assert "not directly comparable" in render_markdown(t)


def test_comparison_markdown():
    trials = [Trial("fable", make_task_result(), make_session_result(2000.0)),
              Trial("haiku", make_task_result(), make_session_result(800.0))]
    identity = check_event_identity(trials)
    md = render_comparison_markdown(trials, identity, rtol=1e-9)
    assert "| fable |" in md.replace("Quantity | fable", "| fable |") or "fable" in md
    assert "IDENTICAL" in md
    assert "E_LLM/E_task" in md
    # no trial carries session power, so the local rows stay out entirely
    assert "E_coord,local" not in md


def test_comparison_markdown_with_partial_session_power():
    trials = [Trial("fable", make_task_result(net=500.0),
                    make_session_result(2000.0),
                    session_power=make_session_power(net=3000.0)),
              Trial("haiku", make_task_result(net=500.0),
                    make_session_result(800.0))]
    md = render_comparison_markdown(trials, check_event_identity(trials), rtol=1e-9)
    assert "E_coord,local" in md
    assert "2500.0" in md          # fable's measured local cost
    assert "n/a" in md             # haiku has no session-power result
