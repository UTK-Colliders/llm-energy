import math
from pathlib import Path

import pytest

from llm_energy.power.powermetrics import parse_powermetrics_text
from llm_energy.schemas import PowerSample, PowerTrace

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_full_fixture_samples_and_integration():
    trace = parse_powermetrics_text(load("powermetrics_apple_silicon.txt"))
    assert trace.backend == "powermetrics"
    assert trace.combined_source == "combined-line"
    assert len(trace.samples) == 3

    s0 = trace.samples[0]
    assert s0.combined_mw == 3264
    assert s0.cpu_mw == 3220
    assert s0.gpu_mw == 44
    assert s0.ane_mw == 0
    assert s0.elapsed_s == pytest.approx(1.00342)
    assert s0.t_rel_s == 0.0
    assert trace.samples[1].t_rel_s == pytest.approx(1.00342)

    # hand-computed: 3.264*1.00342 + 4.350*1.00010 + 4.161*0.99955
    expected = 3.264 * 1.00342 + 4.350 * 1.00010 + 4.161 * 0.99955
    assert trace.integrate_joules() == pytest.approx(expected, rel=1e-9)
    assert trace.duration_s() == pytest.approx(1.00342 + 1.00010 + 0.99955)
    assert trace.mean_watts() == pytest.approx(expected / trace.duration_s())


def test_truncated_final_block_is_dropped():
    trace = parse_powermetrics_text(load("powermetrics_truncated.txt"))
    assert len(trace.samples) == 1
    assert trace.samples[0].combined_mw == 2100
    assert trace.integrate_joules() == pytest.approx(2.1)


def test_missing_combined_line_falls_back_to_component_sum():
    trace = parse_powermetrics_text(load("powermetrics_no_combined.txt"))
    assert trace.combined_source == "component-sum"
    assert len(trace.samples) == 2
    assert trace.samples[0].combined_mw == 1500 + 250 + 50
    assert trace.samples[1].combined_mw == 1200
    # 1.8 W * 2 s + 1.2 W * 2 s
    assert trace.integrate_joules() == pytest.approx(1.8 * 2 + 1.2 * 2)


def test_empty_input():
    trace = parse_powermetrics_text("")
    assert trace.samples == []
    assert trace.combined_source == "empty"
    assert trace.integrate_joules() == 0.0
    assert trace.mean_watts() == 0.0


def test_integration_window_trimming():
    # three 1 s samples at 1000/2000/3000 mW; trim to the middle one
    samples = [
        PowerSample(t_rel_s=0.0, elapsed_s=1.0, combined_mw=1000),
        PowerSample(t_rel_s=1.0, elapsed_s=1.0, combined_mw=2000),
        PowerSample(t_rel_s=2.0, elapsed_s=1.0, combined_mw=3000),
    ]
    trace = PowerTrace(backend="test", samples=samples)
    assert trace.integrate_joules() == pytest.approx(6.0)
    assert trace.integrate_joules(t_start=1.0, t_end=2.0) == pytest.approx(2.0)
    # partial samples count for the fraction that overlaps: 0.1 s of the first
    # sample, then both of the others, and t_end clips to the trace's 3.0 s end
    assert trace.integrate_joules(t_start=0.9, t_end=3.1) == pytest.approx(5.1)


def test_a_window_shorter_than_a_sample_gets_its_share_not_the_whole_sample():
    # The failure this replaces: a 0.4 s container whose window happened to
    # straddle a sample midpoint was handed the entire 1 s sample, and the
    # report showed 20.7 J at 264 W over "0 s". Its neighbour, straddling no
    # midpoint, was handed nothing and reported a clean 0.0 J.
    trace = PowerTrace(backend="test", samples=[
        PowerSample(t_rel_s=0.0, elapsed_s=1.0, combined_mw=20_000),
        PowerSample(t_rel_s=1.0, elapsed_s=1.0, combined_mw=20_000),
    ])
    straddles_midpoint = trace.integrate_joules(t_start=0.3, t_end=0.7)
    straddles_boundary = trace.integrate_joules(t_start=0.8, t_end=1.2)
    assert straddles_midpoint == pytest.approx(8.0)   # 0.4 s x 20 W
    assert straddles_boundary == pytest.approx(8.0)   # same duration, same energy
    for e, wall in ((straddles_midpoint, 0.4), (straddles_boundary, 0.4)):
        assert e / wall == pytest.approx(20.0), "mean power must stay physical"
