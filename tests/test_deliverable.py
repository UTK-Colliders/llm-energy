"""Grading an open-ended run's output against the task specification."""

import gzip
import json
import math
from pathlib import Path

import pytest

from llm_energy.deliverable import find_deliverable, verify_lhe

INIT = "2212 2212 6.8e+03 6.8e+03 0 0 247000 247000 -4 1"


def event(final_state=((6, 1), (-6, 1)), incoming=((21, -1), (21, -1))):
    parts = list(incoming) + list(final_state)
    lines = [f"{len(parts)} 1 0.5 1.7e02 7.5e-03 1.1e-01"]
    for pdg, istup in parts:
        lines.append(f"{pdg} {istup} 1 2 501 502 0.0 0.0 1.0e02 1.7e02 1.7e02 0. 0.")
    return "<event>\n" + "\n".join(lines) + "\n</event>"


def write_lhe(path, n=3, init=INIT, ev=None, gz=False):
    body = ("<LesHouchesEvents version=\"3.0\">\n<init>\n" + init + "\n</init>\n"
            + "\n".join((ev or event()) for _ in range(n))
            + "\n</LesHouchesEvents>\n")
    if gz:
        with gzip.open(path, "wt") as fh:
            fh.write(body)
    else:
        path.write_text(body)
    return path


def test_a_correct_sample_passes(tmp_path):
    r = verify_lhe(write_lhe(tmp_path / "e.lhe", n=3), nevents=3,
                   beam_energy_gev=6800.0)
    assert r.ok, [c.detail for c in r.failures]
    assert r.n_events == 3
    assert len(r.events_sha256) == 64


def test_works_through_gzip(tmp_path):
    r = verify_lhe(write_lhe(tmp_path / "e.lhe.gz", n=2, gz=True), nevents=2,
                   beam_energy_gev=6800.0)
    assert r.ok, [c.detail for c in r.failures]


def test_missing_file_is_not_ok(tmp_path):
    r = verify_lhe(tmp_path / "nope.lhe", nevents=3, beam_energy_gev=6800.0)
    assert not r.exists and not r.ok


def test_wrong_event_count_fails(tmp_path):
    r = verify_lhe(write_lhe(tmp_path / "e.lhe", n=2), nevents=10000,
                   beam_energy_gev=6800.0)
    assert not r.ok
    assert any(c.name == "event count" for c in r.failures)


def test_wrong_beam_energy_fails(tmp_path):
    init = "2212 2212 6.5e+03 6.5e+03 0 0 247000 247000 -4 1"
    r = verify_lhe(write_lhe(tmp_path / "e.lhe", init=init), nevents=3,
                   beam_energy_gev=6800.0)
    assert not r.ok
    assert any(c.name == "beam energy" for c in r.failures)


def test_wrong_process_fails_even_with_the_right_count(tmp_path):
    """A b b~ sample with the right beams and count is still the wrong physics."""
    wrong = event(final_state=((5, 1), (-5, 1)))
    r = verify_lhe(write_lhe(tmp_path / "e.lhe", n=3, ev=wrong), nevents=3,
                   beam_energy_gev=6800.0)
    assert not r.ok
    fs = [c for c in r.failures if c.name == "final state"]
    assert fs and "(-5, 5)" in fs[0].detail


def test_one_bad_event_among_good_ones_is_caught(tmp_path):
    body = ("<LesHouchesEvents>\n<init>\n" + INIT + "\n</init>\n"
            + event() + "\n" + event(final_state=((5, 1), (-5, 1))) + "\n"
            + event() + "\n</LesHouchesEvents>\n")
    (tmp_path / "e.lhe").write_text(body)
    r = verify_lhe(tmp_path / "e.lhe", nevents=3, beam_energy_gev=6800.0)
    assert not r.ok
    assert any(c.name == "final state" for c in r.failures)


def test_non_proton_beams_fail(tmp_path):
    init = "11 -11 6.8e+03 6.8e+03 0 0 247000 247000 -4 1"
    r = verify_lhe(write_lhe(tmp_path / "e.lhe", init=init), nevents=3,
                   beam_energy_gev=6800.0)
    assert any(c.name == "beam particles" for c in r.failures)


def test_missing_init_block_is_reported(tmp_path):
    (tmp_path / "e.lhe").write_text(
        "<LesHouchesEvents>\n" + event() + "\n</LesHouchesEvents>\n")
    r = verify_lhe(tmp_path / "e.lhe", nevents=1, beam_energy_gev=6800.0)
    assert not r.ok
    assert any(c.name == "beams" for c in r.failures)


def test_deliverable_search_prefers_the_largest_sample(tmp_path):
    (tmp_path / "work").mkdir()
    small = write_lhe(tmp_path / "work" / "test_run.lhe", n=1)
    big = write_lhe(tmp_path / "work" / "final.lhe", n=50)
    found = find_deliverable(tmp_path)
    assert found[0] == big and small in found


def test_deliverable_search_finds_gzipped_output(tmp_path):
    p = write_lhe(tmp_path / "deep" / "run_01" / "unweighted_events.lhe.gz",
                  n=2, gz=True) if (tmp_path / "deep" / "run_01").mkdir(
                      parents=True) is None else None
    assert find_deliverable(tmp_path) == [p]


def test_explicit_file_that_is_missing_is_an_error_not_a_fallback(tmp_path):
    """Grading some other file would answer a different question."""
    from click.testing import CliRunner
    from llm_energy.cli import main

    write_lhe(tmp_path / "decoy.lhe", n=3)
    res = CliRunner().invoke(main, ["verify-deliverable", str(tmp_path),
                                    "--file", str(tmp_path / "typo.lhe")])
    assert res.exit_code != 0
    assert "does not exist" in res.output
    assert "decoy" not in res.output


# --- reconstructed top mass peak ---------------------------------------------

from llm_energy.deliverable import (grade_workspace, load_open_spec,  # noqa: E402
                                    verify_mass_peak)


def gaussian_hist(path, peak=172.5, sigma=18.0, n=5000, lo=100.0, hi=250.0,
                  nbins=30, background=0.0):
    """A plausible reconstructed-mass histogram: a peak over flat combinatorics."""
    width = (hi - lo) / nbins
    edges = [lo + i * width for i in range(nbins + 1)]
    counts = []
    for i in range(nbins):
        c = (edges[i] + edges[i + 1]) / 2
        counts.append(n * math.exp(-0.5 * ((c - peak) / sigma) ** 2) + background)
    path.write_text(json.dumps({"bin_edges_gev": edges,
                                "counts": [round(c) for c in counts]}))
    return path


def test_a_top_peak_in_the_right_place_passes(tmp_path):
    r = verify_mass_peak(gaussian_hist(tmp_path / "h.json"),
                         expect_gev=172.5, tolerance_gev=15.0)
    assert r.ok, [c.detail for c in r.failures]
    assert r.peak_gev == pytest.approx(172.5, abs=3.0)
    assert r.fwhm_gev and r.fwhm_gev > 0


def test_a_peak_in_the_wrong_place_fails(tmp_path):
    """A W peak instead of a top peak: right shape, wrong physics."""
    r = verify_mass_peak(gaussian_hist(tmp_path / "h.json", peak=80.4, lo=40.0,
                                       hi=140.0),
                         expect_gev=172.5, tolerance_gev=15.0)
    assert not r.ok
    fail = [c for c in r.failures if c.name == "peak position"]
    assert fail and "wanted 172.5" in fail[0].detail


def test_a_monotonic_falling_spectrum_is_not_a_peak(tmp_path):
    """A steeply falling background has a tall first bin and no peak at all."""
    edges = [100.0 + 5 * i for i in range(31)]
    counts = [int(5000 * math.exp(-i / 4)) for i in range(30)]
    (tmp_path / "h.json").write_text(
        json.dumps({"bin_edges_gev": edges, "counts": counts}))
    r = verify_mass_peak(tmp_path / "h.json", expect_gev=172.5,
                         tolerance_gev=1000.0)     # position can't be the failure
    assert not r.ok
    assert any(c.name == "peak is interior" for c in r.failures)


def test_a_flat_histogram_has_no_prominence(tmp_path):
    edges = [100.0 + 5 * i for i in range(31)]
    counts = [100] * 15 + [101] + [100] * 14      # a "peak" one count high
    (tmp_path / "h.json").write_text(
        json.dumps({"bin_edges_gev": edges, "counts": counts}))
    r = verify_mass_peak(tmp_path / "h.json", expect_gev=177.5,
                         tolerance_gev=15.0)
    assert not r.ok
    assert any(c.name == "peak prominence" for c in r.failures)


def test_too_few_entries_fails(tmp_path):
    r = verify_mass_peak(gaussian_hist(tmp_path / "h.json", n=10),
                         expect_gev=172.5, tolerance_gev=15.0, min_entries=200)
    assert not r.ok
    assert any(c.name == "entries" for c in r.failures)


def test_missing_histogram_is_not_ok(tmp_path):
    r = verify_mass_peak(tmp_path / "nope.json", 172.5, 15.0)
    assert not r.exists and not r.ok


@pytest.mark.parametrize("body,why", [
    ('{"bin_edges_gev": [1,2,3], "counts": [1,2,3]}', "one more edge"),
    ('{"counts": [1,2]}', "arrays"),
    ('[1,2,3]', "JSON object"),
    ('not json at all', "Expecting"),
    ('{"bin_edges_gev": [3,2,1], "counts": [1,2]}', "increasing"),
    ('{"bin_edges_gev": ["a","b"], "counts": ["c"]}', "non-numeric"),
])
def test_malformed_histograms_are_rejected_with_a_reason(tmp_path, body, why):
    (tmp_path / "h.json").write_text(body)
    r = verify_mass_peak(tmp_path / "h.json", 172.5, 15.0)
    assert not r.ok
    assert any(why in c.detail for c in r.failures), [c.detail for c in r.failures]


# --- grading a whole workspace ------------------------------------------------

def open_spec():
    return load_open_spec(Path(__file__).parent.parent / "tasks"
                          / "madgraph-ttbar-open")


def complete_workspace(tmp_path, n_events=10000, peak=172.5):
    write_lhe(tmp_path / "unweighted_events.lhe.gz", n=n_events, gz=True)
    gaussian_hist(tmp_path / "top_mass_hist.json", peak=peak)
    (tmp_path / "top_mass.pdf").write_bytes(b"%PDF-1.4 fake")
    return tmp_path


def test_a_complete_run_passes_every_deliverable(tmp_path):
    g = grade_workspace(open_spec(), complete_workspace(tmp_path, n_events=10000))
    assert g.ok, [c.detail for c in (g.events.failures + g.peak.failures)]


def test_good_events_but_a_wrong_peak_fails_overall(tmp_path):
    g = grade_workspace(open_spec(),
                        complete_workspace(tmp_path, n_events=10000, peak=90.0))
    assert g.events.ok
    assert not g.peak.ok
    assert not g.ok, "a correct sample with a bad reconstruction is not a pass"


def test_a_missing_plot_fails_even_when_the_physics_is_right(tmp_path):
    complete_workspace(tmp_path)
    (tmp_path / "top_mass.pdf").unlink()
    g = grade_workspace(open_spec(), tmp_path)
    assert g.events.ok and g.peak.ok
    assert g.plot_missing and not g.ok


def test_a_missing_histogram_is_reported_not_silently_skipped(tmp_path):
    complete_workspace(tmp_path)
    (tmp_path / "top_mass_hist.json").unlink()
    g = grade_workspace(open_spec(), tmp_path)
    assert not g.ok
    assert any("could not be checked" in n for n in g.notes)


def test_events_found_under_another_name_are_still_graded(tmp_path):
    complete_workspace(tmp_path)
    (tmp_path / "unweighted_events.lhe.gz").rename(tmp_path / "events.lhe.gz")
    g = grade_workspace(open_spec(), tmp_path)
    assert g.events is not None and g.events.ok
    assert any("nothing at unweighted_events" in n for n in g.notes)


def test_a_sparse_histogram_reports_prominence_readably(tmp_path):
    """An all-but-empty range makes prominence infinite; it must still read."""
    edges = [100.0 + 5 * i for i in range(31)]
    counts = [0] * 14 + [500] + [0] * 15
    (tmp_path / "h.json").write_text(
        json.dumps({"bin_edges_gev": edges, "counts": counts}))
    r = verify_mass_peak(tmp_path / "h.json", expect_gev=172.5, tolerance_gev=15.0)
    prom = [c for c in r.checks if c.name == "peak prominence"][0]
    assert prom.ok and "inf" not in prom.detail
    assert "sparse" in prom.detail
