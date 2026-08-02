"""Grading an open-ended run's output against the task specification."""

import gzip
import json
import math
from pathlib import Path

import pytest

from llm_energy.deliverable import (find_deliverable, find_histogram,
                                    find_plot, verify_lhe)

INIT = "2212 2212 6.8e+03 6.8e+03 0 0 247000 247000 -4 1"


def event(final_state=((6, 1), (-6, 1)), incoming=((21, -1), (21, -1))):
    parts = list(incoming) + list(final_state)
    lines = [f"{len(parts)} 1 0.5 1.7e02 7.5e-03 1.1e-01"]
    for pdg, istup in parts:
        lines.append(f"{pdg} {istup} 1 2 501 502 0.0 0.0 1.0e02 1.7e02 1.7e02 0. 0.")
    return "<event>\n" + "\n".join(lines) + "\n</event>"


def event_2j(jets=((21, 1), (21, 1))):
    """ttbar plus two partons — what the ttbar+2j spec expects."""
    return event(final_state=((6, 1), (-6, 1)) + tuple(jets))


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
    ('{"bin_edges_gev": [1,2,3], "counts": [1,2,3]}', "one more entry"),
    ('{"counts": [1,2]}', "arrays"),
    ('[1,2,3]', "JSON object"),
    ('not json at all', "Expecting"),
    ('{"bin_edges_gev": [3,2,1], "counts": [1,2]}', "increasing"),
    ('{"bin_edges_gev": ["a","b"], "counts": ["c"]}', "one more entry"),
])
def test_malformed_histograms_are_rejected_with_a_reason(tmp_path, body, why):
    (tmp_path / "h.json").write_text(body)
    r = verify_mass_peak(tmp_path / "h.json", 172.5, 15.0)
    assert not r.ok
    assert any(why in c.detail for c in r.failures), [c.detail for c in r.failures]


# --- grading a whole workspace ------------------------------------------------

def open_spec():
    return load_open_spec(Path(__file__).parent.parent / "tasks"
                          / "madgraph-ttbar2j-open")


def complete_workspace(tmp_path, n_events=10000, peak=172.5):
    write_lhe(tmp_path / "unweighted_events.lhe.gz", n=n_events, gz=True,
              ev=event_2j())
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


# --- exact comparison across runs ---------------------------------------------

from llm_energy.deliverable import (compare_graded,  # noqa: E402
                                    histogram_fingerprint)
from llm_energy.lhe import read_generator_version  # noqa: E402


def test_histogram_fingerprint_ignores_formatting_but_not_values():
    assert histogram_fingerprint([1.0, 2.0, 3.0], [4, 5]) == \
           histogram_fingerprint([1, 2, 3], [4.0, 5.0])
    assert histogram_fingerprint([1.0, 2.0, 3.0], [4, 5]) != \
           histogram_fingerprint([1.0, 2.0, 3.0], [4, 6])


def test_generator_version_read_from_the_mg5_banner(tmp_path):
    p = tmp_path / "e.lhe"
    p.write_text("<LesHouchesEvents>\n<header>\n<MGVersion>\n3.5.16\n"
                 "</MGVersion>\n</header>\n<init>\n" + INIT + "\n</init>\n"
                 + event() + "\n</LesHouchesEvents>\n")
    assert read_generator_version(p) == "3.5.16"


def test_generator_version_read_from_an_inline_tag(tmp_path):
    p = tmp_path / "e.lhe"
    p.write_text("<LesHouchesEvents>\n<MGVersion>3.6.1</MGVersion>\n<init>\n"
                 + INIT + "\n</init>\n" + event() + "\n</LesHouchesEvents>\n")
    assert read_generator_version(p) == "3.6.1"


def test_generator_version_absent_is_none(tmp_path):
    assert read_generator_version(write_lhe(tmp_path / "e.lhe")) is None


def graded_run(tmp_path, name, n_events=3, peak=172.5, version=None):
    d = tmp_path / name
    d.mkdir()
    if version:
        (d / "unweighted_events.lhe.gz").write_bytes(b"")   # replaced below
        import gzip
        with gzip.open(d / "unweighted_events.lhe.gz", "wt") as fh:
            fh.write("<LesHouchesEvents>\n<MGVersion>%s</MGVersion>\n<init>\n%s\n"
                     "</init>\n%s\n</LesHouchesEvents>\n"
                     % (version, INIT,
                        "\n".join(event_2j() for _ in range(n_events))))
    else:
        write_lhe(d / "unweighted_events.lhe.gz", n=n_events, gz=True,
                  ev=event_2j())
    gaussian_hist(d / "top_mass_hist.json", peak=peak)
    (d / "top_mass.pdf").write_bytes(b"%PDF-1.4")
    return name, grade_workspace(open_spec(), d)


def test_identical_pipelines_match_on_both_artefacts(tmp_path):
    comp = compare_graded([graded_run(tmp_path, "a"), graded_run(tmp_path, "b")])
    assert comp.events_identical
    assert comp.histograms_identical
    assert comp.notes == []


def test_same_events_different_reconstruction_is_not_a_seed_failure(tmp_path):
    """The interesting case: seeds held, methods differed."""
    comp = compare_graded([graded_run(tmp_path, "a", peak=170.0),
                           graded_run(tmp_path, "b", peak=175.0)])
    assert comp.events_identical, "the LHE is the same, so the seeds held"
    assert not comp.histograms_identical
    assert comp.peak_spread_gev() == pytest.approx(5.0)
    assert any("reconstruction methods differ" in n for n in comp.notes)


def test_differing_events_at_the_same_version_blames_the_seed(tmp_path):
    comp = compare_graded([graded_run(tmp_path, "a", n_events=3, version="3.5.16"),
                           graded_run(tmp_path, "b", n_events=4, version="3.5.16")])
    assert not comp.events_identical
    assert comp.versions_agree
    assert any("a seed was not honoured" in n for n in comp.notes)


def test_differing_events_across_versions_says_so_instead(tmp_path):
    comp = compare_graded([graded_run(tmp_path, "a", n_events=3, version="3.5.16"),
                           graded_run(tmp_path, "b", n_events=4, version="3.6.1")])
    assert not comp.events_identical
    assert not comp.versions_agree
    assert any("generator versions differ" in n for n in comp.notes)
    assert not any("seed was not honoured" in n for n in comp.notes)


def test_unknown_versions_are_not_reported_as_differing(tmp_path):
    """Unknown tells us nothing; calling it a difference blames the wrong thing."""
    comp = compare_graded([graded_run(tmp_path, "a", n_events=3),
                           graded_run(tmp_path, "b", n_events=4)])
    assert not comp.events_identical
    assert not comp.versions_known and not comp.versions_agree
    assert any("cannot be ruled out" in n for n in comp.notes)
    assert not any("versions differ (" in n for n in comp.notes)
    assert not any("seed was not honoured" in n for n in comp.notes)


def test_coincident_peaks_produce_no_spread_note(tmp_path):
    comp = compare_graded([graded_run(tmp_path, "a", peak=172.5),
                           graded_run(tmp_path, "b", peak=172.5)])
    assert comp.peak_spread_gev() == 0.0
    assert not any("span" in n for n in comp.notes)


# --- ttbar + 2 jets -----------------------------------------------------------

def test_the_spec_now_demands_two_extra_jets(tmp_path):
    """A plain ttbar sample is the wrong process for a ttbar+2j brief."""
    write_lhe(tmp_path / "unweighted_events.lhe.gz", n=10000, gz=True)  # no jets
    gaussian_hist(tmp_path / "top_mass_hist.json")
    (tmp_path / "top_mass.pdf").write_bytes(b"%PDF-1.4")
    g = grade_workspace(open_spec(), tmp_path)
    assert not g.ok
    fs = [c for c in g.events.failures if c.name == "final state"]
    assert fs and "wanted 2 jets" in fs[0].detail.replace("(-6, 6) + ", "")


def test_jet_flavours_may_differ_event_to_event(tmp_path):
    """gg, q qbar and mixed flavours are all valid ttbar+2j final states."""
    body = ("<LesHouchesEvents>\n<init>\n" + INIT + "\n</init>\n"
            + event_2j(((21, 1), (21, 1))) + "\n"
            + event_2j(((2, 1), (-2, 1))) + "\n"
            + event_2j(((21, 1), (-1, 1))) + "\n</LesHouchesEvents>\n")
    (tmp_path / "e.lhe").write_text(body)
    r = verify_lhe(tmp_path / "e.lhe", nevents=3, beam_energy_gev=6800.0,
                   n_extra_jets=2)
    assert r.ok, [c.detail for c in r.failures]


def test_a_lepton_is_not_a_jet(tmp_path):
    """t t~ e+ e- has the right multiplicity and the wrong physics."""
    (tmp_path / "e.lhe").write_text(
        "<LesHouchesEvents>\n<init>\n" + INIT + "\n</init>\n"
        + event_2j(((11, 1), (-11, 1))) + "\n</LesHouchesEvents>\n")
    r = verify_lhe(tmp_path / "e.lhe", nevents=1, beam_energy_gev=6800.0,
                   n_extra_jets=2)
    assert not r.ok
    assert any("non-parton" in c.detail for c in r.failures)


def test_a_histogram_under_any_name_is_still_found(tmp_path):
    """The brief says 'somewhere in this directory', so grading must search."""
    complete_workspace(tmp_path)
    (tmp_path / "top_mass_hist.json").rename(tmp_path / "my_analysis_out.json")
    g = grade_workspace(open_spec(), tmp_path)
    assert g.ok, [c.detail for c in (g.peak.failures if g.peak else [])]
    assert any("my_analysis_out.json" in n for n in g.notes)


def test_a_plot_under_any_name_and_format_counts(tmp_path):
    complete_workspace(tmp_path)
    (tmp_path / "top_mass.pdf").rename(tmp_path / "mtop.png")
    g = grade_workspace(open_spec(), tmp_path)
    assert not g.plot_missing
    assert any("mtop.png" in n for n in g.notes)


# --- the generator's own working tree is not the deliverable ------------------

def madgraph_tree(root, real_events=10000, real=True):
    """A workspace shaped like MadGraph's, scratch files and all."""
    scratch = root / "ttbar2j" / "SubProcesses" / "P1_gg_ttxgg_t_bwp_tx_bxwm"
    (scratch / "Hel").mkdir(parents=True)
    # MadGraph leaves an empty events.lhe here; it looks like a deliverable
    (scratch / "Hel" / "events.lhe").write_text(
        "<LesHouchesEvents>\n</LesHouchesEvents>\n")
    (scratch / "card.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 40000)
    if real:
        out = root / "ttbar2j" / "Events" / "run_01"
        out.mkdir(parents=True)
        write_lhe(out / "unweighted_events.lhe.gz", n=real_events, gz=True,
                  ev=event_2j())
    return root


def test_an_empty_scratch_lhe_is_not_chosen_over_the_real_sample(tmp_path):
    """The exact failure from a real run: a zero-event Hel/events.lhe was
    graded, failed on the event count, and passed the final-state check."""
    madgraph_tree(tmp_path, real_events=50)
    chosen = find_deliverable(tmp_path)[0]
    assert chosen.name.startswith("unweighted_events")
    assert "SubProcesses" not in str(chosen)


def test_an_empty_lhe_fails_the_final_state_check(tmp_path):
    """'every event is t t~ + 2 jets' is vacuously true of no events."""
    (tmp_path / "empty.lhe").write_text("<LesHouchesEvents>\n</LesHouchesEvents>\n")
    r = verify_lhe(tmp_path / "empty.lhe", nevents=10000,
                   beam_energy_gev=6800.0, n_extra_jets=2)
    assert not r.ok
    fs = [c for c in r.failures if c.name == "final state"]
    assert fs and "no events to check" in fs[0].detail


def test_a_generator_diagram_is_not_the_agents_plot(tmp_path):
    """card.jpg under SubProcesses/ is MadGraph's own output, not a result."""
    madgraph_tree(tmp_path)
    (tmp_path / "top_mass.pdf").write_bytes(b"%PDF-1.4 small")
    assert find_plot(tmp_path).name == "top_mass.pdf"


def test_with_only_scratch_files_the_plot_search_finds_nothing(tmp_path):
    madgraph_tree(tmp_path, real=False)
    assert find_plot(tmp_path) is None


def test_scratch_is_still_offered_when_it_is_all_there_is(tmp_path):
    """Ranked last, but not hidden — the operator should see what was found."""
    madgraph_tree(tmp_path, real=False)
    found = find_deliverable(tmp_path)
    assert found and "SubProcesses" in str(found[0])


def test_a_histogram_in_the_generator_tree_is_ignored(tmp_path):
    madgraph_tree(tmp_path)
    scratch = tmp_path / "ttbar2j" / "SubProcesses" / "P1_gg_ttxgg_t_bwp_tx_bxwm"
    (scratch / "results.json").write_text(
        json.dumps({"bin_edges_gev": [1, 2, 3], "counts": [4, 5]}))
    assert find_histogram(tmp_path) is None
