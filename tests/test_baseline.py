"""Baseline capture quality.

Every later measurement subtracts the baseline, so a bad one does not fail —
it quietly shaves watts off real results, and if it is bad enough it drives
them negative. These check that a contaminated capture says so at the moment
it is taken, while re-recording still costs 30 seconds.
"""

from pathlib import Path

from llm_energy.baseline import baseline_warnings
from llm_energy.schemas import BaselineResult, MachineInfo, write_result


def a_baseline(mean_w=5.0, std_w=0.1, chip="test", hostname="host"):
    return BaselineResult(
        duration_s=30.0, mean_w=mean_w, std_w=std_w, joules=mean_w * 30,
        n_samples=30, min_w=mean_w - 2 * std_w, max_w=mean_w + 2 * std_w,
        backend="canned", docker_running=True,
        machine=MachineInfo(chip=chip, hostname=hostname))


def test_a_settled_machine_draws_no_warnings(tmp_path):
    assert baseline_warnings(a_baseline(), tmp_path) == []


def test_a_noisy_capture_is_flagged(tmp_path):
    # 5 W mean with 2 W of scatter is a machine doing something, not an idle one
    warnings = baseline_warnings(a_baseline(mean_w=5.0, std_w=2.0), tmp_path)
    assert any("not settled" in w for w in warnings)


def test_a_capture_well_above_the_last_one_is_flagged(tmp_path):
    write_result(a_baseline(mean_w=4.0), tmp_path / "baseline-old.json")
    warnings = baseline_warnings(a_baseline(mean_w=8.0), tmp_path)
    assert any("above the last one" in w for w in warnings)
    assert any("baseline-old.json" in w for w in warnings)


def test_drift_is_only_compared_against_the_same_machine(tmp_path):
    write_result(a_baseline(mean_w=4.0, chip="other"),
                 tmp_path / "baseline-old.json")
    assert baseline_warnings(a_baseline(mean_w=8.0), tmp_path) == []


def test_a_lower_baseline_than_last_time_is_fine(tmp_path):
    write_result(a_baseline(mean_w=9.0), tmp_path / "baseline-old.json")
    assert baseline_warnings(a_baseline(mean_w=4.0), tmp_path) == []


def test_an_unreadable_previous_baseline_does_not_crash_the_capture(tmp_path):
    Path(tmp_path / "baseline-broken.json").write_text("{not json")
    assert baseline_warnings(a_baseline(), tmp_path) == []


def test_a_container_running_during_the_capture_is_flagged(tmp_path):
    b = a_baseline()
    b.containers_running = ["42fd1e297226 (llm-energy/mg5amc:3.5.16)"]
    warnings = baseline_warnings(b, tmp_path)
    assert any("were running during this capture" in w for w in warnings)
    assert any("42fd1e297226" in w for w in warnings)


def test_a_persistently_leaked_container_is_invisible_to_the_drift_check(tmp_path):
    """The failure this exists for.

    A container leaked by run N is still up for run N+1's baseline, so that
    baseline is contaminated — and so is N+2's, by the same amount. Drift
    compares consecutive baselines and sees nothing wrong. Only the container
    check catches it.
    """
    write_result(a_baseline(mean_w=20.95), tmp_path / "baseline-old.json")
    contaminated = a_baseline(mean_w=21.76)
    assert baseline_warnings(contaminated, tmp_path) == [], "drift is blind here"

    contaminated.containers_running = ["42fd1e297226 (mg5amc)"]
    assert baseline_warnings(contaminated, tmp_path) != []
