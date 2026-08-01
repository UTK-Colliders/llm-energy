from pathlib import Path

import pytest

from llm_energy.report import (Trial, check_event_identity,
                               render_comparison_markdown, render_markdown)
from llm_energy.schemas import (EnergyBand, MachineInfo, ModelUsage,
                                SessionEnergyResult, SessionUsage,
                                TaskRunResult)


def make_task_result(events_hash="a" * 64, nevents=10000, lhe_path="",
                     net=500.0) -> TaskRunResult:
    outputs = {}
    if events_hash:
        outputs = {"lhe_file": lhe_path, "lhe_file_nevents": nevents,
                   "lhe_file_events_sha256": events_hash}
    return TaskRunResult(
        task_name="madgraph-ttbar-lhe", image="llm-energy/mg5amc:test",
        image_arch="arm64", emulated=False, wall_time_s=600.0,
        gross_joules=9000.0, baseline_ref="b.json", baseline_mean_w=4.0,
        net_joules=net, mean_power_w=15.0, alignment_uncertainty_j=15.0,
        backend="powermetrics", container_cpu_seconds=2300.0,
        outputs=outputs, machine=MachineInfo(chip="Apple M2"),
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


def test_comparison_markdown():
    trials = [Trial("fable", make_task_result(), make_session_result(2000.0)),
              Trial("haiku", make_task_result(), make_session_result(800.0))]
    identity = check_event_identity(trials)
    md = render_comparison_markdown(trials, identity, rtol=1e-9)
    assert "| fable |" in md.replace("Quantity | fable", "| fable |") or "fable" in md
    assert "IDENTICAL" in md
    assert "E_LLM/E_task" in md
