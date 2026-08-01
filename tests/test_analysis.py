"""The energy budget: which portions appear, and how shares are computed."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from llm_energy.analysis import (ESTIMATED, MEASURED, build_budget,  # noqa: E402
                                 render_budget_chart, render_budget_markdown)
from llm_energy.schemas import ObservedContainer  # noqa: E402
from test_report import (make_phase, make_session_power,  # noqa: E402
                         make_session_result, make_task_result)


def split_task():
    t = make_task_result(net=4000.0)
    t.phases = [make_phase("codegen-compile", 3100.0, 3100.0, 142, wall=380.0),
                make_phase("event-generation", 900.0, 900.0, 0, wall=220.0)]
    return t


def named(budget):
    return [c.name for c in budget.components]


def by_name(budget, name):
    return next(c for c in budget.components if c.name == name)


# --- pinned tasks -------------------------------------------------------------

def test_phases_become_separate_portions():
    b = build_budget("x", task=split_task(),
                     session_power=make_session_power(net=10900.0))
    assert named(b) == ["codegen-compile", "event-generation",
                        "coordination (local)"]


def test_shares_are_of_the_measured_total_and_sum_to_one():
    b = build_budget("x", task=split_task(),
                     session_power=make_session_power(net=10900.0))
    assert b.measured_total_j == pytest.approx(10900.0)
    shares = [b.share(c) for c in b.measured]
    assert shares[0] == pytest.approx(3100 / 10900)
    assert shares[1] == pytest.approx(900 / 10900)
    assert sum(shares) == pytest.approx(1.0)


def test_coordination_is_the_session_minus_the_task():
    b = build_budget("x", task=split_task(),
                     session_power=make_session_power(net=10900.0))
    # session 10900 net, task 4000 net
    assert by_name(b, "coordination (local)").joules == pytest.approx(6900.0)


def test_single_phase_task_is_one_portion():
    b = build_budget("x", task=make_task_result(net=500.0))
    assert named(b) == ["task"]
    assert by_name(b, "task").joules == pytest.approx(500.0)


def test_task_alone_needs_no_session_artifacts():
    b = build_budget("x", task=split_task())
    assert named(b) == ["codegen-compile", "event-generation"]
    assert b.measured_total_j == pytest.approx(4000.0)


# --- open tasks ---------------------------------------------------------------

def open_power(container_j=19900.0, gross=52000.0, n=2):
    sp = make_session_power(net=None, gross=gross)
    sp.baseline_mean_w = None
    sp.container_joules = container_j
    sp.container_wall_s = 1380.0
    sp.containers = [ObservedContainer(f"c{i}", "mg5:1", "", "", 100.0, 1.0, 1.0)
                     for i in range(n)]
    return sp


def test_open_run_splits_compute_from_coordination():
    b = build_budget("open", session_power=open_power())
    assert named(b) == ["compute (containers)", "coordination (local)"]
    assert by_name(b, "compute (containers)").joules == pytest.approx(19900.0)
    assert by_name(b, "coordination (local)").joules == pytest.approx(32100.0)
    assert b.measured_total_j == pytest.approx(52000.0)


def test_open_run_without_container_observation_is_not_split():
    sp = open_power()
    sp.container_joules = None
    b = build_budget("open", session_power=sp)
    assert named(b) == ["session (local)"]
    assert any("could not be separated" in n for n in b.notes)


# --- the measured / estimated boundary ---------------------------------------

def test_inference_is_estimated_and_carries_its_band():
    b = build_budget("x", task=split_task(), session=make_session_result(2000.0))
    llm = by_name(b, "LLM inference")
    assert llm.basis == ESTIMATED
    assert llm.band.low_j == pytest.approx(500.0)
    assert llm.band.high_j == pytest.approx(8000.0)


def test_estimated_energy_is_excluded_from_the_measured_total():
    """Local package energy and estimated remote energy must not be summed."""
    b = build_budget("x", task=split_task(), session=make_session_result(180000.0))
    assert b.measured_total_j == pytest.approx(4000.0)   # not 184000
    assert all(c.basis == MEASURED for c in b.measured)


def test_estimated_component_has_no_share_only_a_ratio():
    b = build_budget("x", task=split_task(), session=make_session_result(180000.0))
    llm = by_name(b, "LLM inference")
    assert b.share(llm) is None
    lo, mid, hi = b.ratio_to_measured(llm)
    assert mid == pytest.approx(180000.0 / 4000.0)
    assert lo < mid < hi


def test_failed_power_sampling_drops_the_local_terms():
    sp = make_session_power(net=10900.0)
    sp.power_ok = False
    b = build_budget("x", task=split_task(), session_power=sp)
    assert "coordination (local)" not in named(b)
    assert any("power sampling failed" in n for n in b.notes)


def test_no_artifacts_produces_an_empty_budget_with_a_note():
    b = build_budget("x")
    assert b.components == []
    assert any("nothing to break down" in n for n in b.notes)


def test_measured_total_of_zero_does_not_divide_by_zero():
    t = make_task_result(net=0.0)
    b = build_budget("x", task=t, session=make_session_result(100.0))
    assert b.share(b.measured[0]) is None
    assert b.ratio_to_measured(by_name(b, "LLM inference")) is None


# --- rendering ----------------------------------------------------------------

def test_markdown_keeps_the_two_bases_visibly_apart():
    md = render_budget_markdown(build_budget(
        "x", task=split_task(), session_power=make_session_power(net=10900.0),
        session=make_session_result(180000.0)))
    assert "measured" in md and "estimated" in md
    assert "do not sum" in md
    assert "28.4%" in md
    assert "of measured" in md


# matplotlib lives in the [plots] extra, so a plain `uv sync` has no charting
needs_mpl = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("matplotlib") is None,
    reason="charting needs the [plots] extra")


@needs_mpl
def test_chart_writes_a_single_panel_pdf(tmp_path):
    b = build_budget("x", task=split_task(),
                     session_power=make_session_power(net=10900.0),
                     session=make_session_result(180000.0))
    out = render_budget_chart(b, tmp_path / "budget")
    assert out.suffix == ".pdf"
    assert out.read_bytes().startswith(b"%PDF")


@needs_mpl
def test_chart_extension_is_forced_to_pdf(tmp_path):
    b = build_budget("x", task=split_task())
    assert render_budget_chart(b, tmp_path / "fig.png").name == "fig.pdf"


@needs_mpl
def test_chart_refuses_an_empty_budget(tmp_path):
    with pytest.raises(ValueError, match="nothing to plot"):
        render_budget_chart(build_budget("x"), tmp_path / "f")
