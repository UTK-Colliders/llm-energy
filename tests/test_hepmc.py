"""Reading HepMC2 and HepMC3 ASCII records.

Graded on payload, like the LHE side: a file that declares itself HepMC3 in
the header and holds nothing still fails, and a parton-level record rewritten
in HepMC still fails the check that says the shower ran.
"""

import gzip
from pathlib import Path

from llm_energy import hepmc
from llm_energy.deliverable import verify_hepmc


def write_hepmc(path, n=10, particles=200, version=3, pdgs=(6, -6), gz=False):
    marker = "HepMC::Asciiv3" if version == 3 else "HepMC::IO_GenEvent"
    out = [f"HepMC::Version {'3.02.05' if version == 3 else '2.06.09'}",
           f"{marker}-START_EVENT_LISTING"]
    for i in range(n):
        out.append(f"E {i} 2 {particles}")
        out.append("U GEV MM")
        ids = list(pdgs) + [211] * max(0, particles - len(pdgs))
        for j, pdg in enumerate(ids, start=1):
            out.append(f"P {j} -1 {pdg} 0 0 100 100 0.14 1" if version == 3
                       else f"P {j} {pdg} 0 0 100 100 0.14 1 0 0 0 0")
    out.append(f"{marker}-END_EVENT_LISTING")
    text = "\n".join(out) + "\n"
    if gz:
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    return path


# --- parsing ------------------------------------------------------------------

def test_reads_hepmc3(tmp_path):
    p = write_hepmc(tmp_path / "s.hepmc", n=4, version=3)
    header = hepmc.read_header(p)
    assert header.version_major == 3 and header.version == "3.02.05"
    assert sum(1 for _ in hepmc.iter_events(p)) == 4


def test_reads_hepmc2(tmp_path):
    p = write_hepmc(tmp_path / "s.hepmc", n=4, version=2)
    assert hepmc.read_header(p).version_major == 2
    assert sum(1 for _ in hepmc.iter_events(p)) == 4


def test_reads_through_gzip(tmp_path):
    p = write_hepmc(tmp_path / "s.hepmc.gz", n=3, gz=True)
    assert sum(1 for _ in hepmc.iter_events(p)) == 3


def test_the_pdg_column_differs_between_the_two_formats(tmp_path):
    """HepMC2 puts the PDG id one field earlier than HepMC3 does.

    Read a v2 file with v3 offsets and the tops come out as momenta.
    """
    for version in (2, 3):
        p = write_hepmc(tmp_path / f"v{version}.hepmc", n=1, particles=4,
                        version=version)
        event = next(hepmc.iter_events(p))
        pdgs = hepmc.particle_pdgs(event, version)
        assert 6 in pdgs and -6 in pdgs, f"HepMC{version} particle line misread"


def test_a_file_that_is_not_hepmc_has_no_header(tmp_path):
    (tmp_path / "notes.txt").write_text("I ran Pythia, honest\n" * 100)
    assert hepmc.read_header(tmp_path / "notes.txt") is None
    assert list(hepmc.iter_events(tmp_path / "notes.txt")) == []


def test_lines_outside_the_listing_are_not_events(tmp_path):
    p = tmp_path / "s.hepmc"
    p.write_text("HepMC::Version 3.02.05\n"
                 "E 999 0 0\n"                        # before the listing
                 "HepMC::Asciiv3-START_EVENT_LISTING\n"
                 "E 0 2 1\nP 1 -1 6 0 0 1 1 0 1\n"
                 "HepMC::Asciiv3-END_EVENT_LISTING\n"
                 "E 998 0 0\n")                       # after it
    assert sum(1 for _ in hepmc.iter_events(p)) == 1


# --- grading ------------------------------------------------------------------

def test_a_good_shower_passes(tmp_path):
    r = verify_hepmc(write_hepmc(tmp_path / "s.hepmc", n=100), nevents=100,
                     require_pdgs=(6, -6))
    assert r.ok, [c.detail for c in r.failures]
    assert r.n_events == 100
    assert r.generator_version == "HepMC3 3.02.05"


def test_a_missing_file_is_not_ok(tmp_path):
    r = verify_hepmc(tmp_path / "nope.hepmc", nevents=10)
    assert not r.exists and not r.ok


def test_an_unreadable_file_fails_on_format(tmp_path):
    (tmp_path / "s.hepmc").write_text("this is not HepMC\n")
    r = verify_hepmc(tmp_path / "s.hepmc", nevents=10)
    assert not r.ok
    assert [c.name for c in r.failures] == ["format"]


def test_an_empty_record_does_not_pass_vacuously(tmp_path):
    """No events means nothing was checked, not that everything checked out."""
    r = verify_hepmc(write_hepmc(tmp_path / "s.hepmc", n=0), nevents=100,
                     require_pdgs=(6, -6))
    assert not r.ok
    failed = {c.name: c.detail for c in r.failures}
    assert failed["showered"] == "no events to check"
    assert failed["hard process"] == "no events to check"


def test_a_converted_lhe_is_not_a_shower(tmp_path):
    """The check the whole shower requirement rests on.

    A parton-level record rewritten as HepMC has the right header, the right
    event count and the right tops. Multiplicity is what gives it away.
    """
    r = verify_hepmc(write_hepmc(tmp_path / "s.hepmc", n=100, particles=10),
                     nevents=100, require_pdgs=(6, -6),
                     min_particles_per_event=50)
    assert not r.ok
    fail = [c for c in r.failures if c.name == "showered"]
    assert fail and "parton-level" in fail[0].detail


def test_events_without_the_hard_process_fail(tmp_path):
    """Showered minimum-bias would pass every check but this one."""
    r = verify_hepmc(write_hepmc(tmp_path / "s.hepmc", n=10, pdgs=(211, -211)),
                     nevents=10, require_pdgs=(6, -6))
    assert not r.ok
    fail = [c for c in r.failures if c.name == "hard process"]
    assert fail and "missing PDG 6" in fail[0].detail


# --- finding it in a workspace ------------------------------------------------

def test_search_prefers_the_record_with_events(tmp_path):
    write_hepmc(tmp_path / "empty.hepmc", n=0)
    real = write_hepmc(tmp_path / "run.hepmc", n=50)
    assert hepmc.find_hepmc(tmp_path)[0] == real


def test_search_skips_the_generator_scratch_tree(tmp_path):
    scratch = tmp_path / "ttbar2j" / "SubProcesses" / "P1_gg_ttxqq"
    scratch.mkdir(parents=True)
    write_hepmc(scratch / "s.hepmc", n=500)
    real = write_hepmc(tmp_path / "s.hepmc", n=10)
    assert hepmc.find_hepmc(tmp_path)[0] == real


def test_search_sniffs_files_with_no_recognisable_suffix(tmp_path):
    """Pythia's default output is often `.dat` or has no suffix at all."""
    p = write_hepmc(tmp_path / "pythia_out.dat", n=7)
    assert hepmc.find_hepmc(tmp_path) == [Path(p)]
