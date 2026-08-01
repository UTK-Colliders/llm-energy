"""Grading an open-ended run's output against the task specification."""

import gzip

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
