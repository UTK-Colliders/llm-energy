"""Check an LLM-produced event sample against the task specification.

When the agent is told only *what* to produce and works out *how* itself, the
result can be wrong — wrong process, wrong beam energy, too few events, no
file at all. Correctness stops being a guarantee and becomes an outcome to
measure, so a run's energy is only meaningful next to a verdict on whether it
delivered.

Everything here reads the LHE payload — the `<init>` block and the event
records — rather than the MG5 banner, so an agent that produced the right
events by an unexpected route still passes, and one that wrote a convincing
banner over the wrong physics still fails.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from llm_energy.lhe import (event_summary, final_state_pdgs, iter_events,
                            read_generator_version, read_init)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class DeliverableReport:
    path: str
    exists: bool
    checks: list[Check] = field(default_factory=list)
    n_events: int = 0
    events_sha256: str = ""
    generator_version: str | None = None

    @property
    def ok(self) -> bool:
        return self.exists and all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


# MadGraph's default `j` is g and the four light flavours; b appears once the
# agent chooses a 5-flavour scheme. Both are defensible, so both pass.
DEFAULT_JET_PDGS = (21, 1, 2, 3, 4, 5, -1, -2, -3, -4, -5)


def check_event_topology(pdgs: tuple[int, ...],
                         required: tuple[int, ...],
                         n_jets: int,
                         jet_pdgs: tuple[int, ...]) -> str | None:
    """None if the final state is the right topology, else what is wrong.

    An exact-set match cannot express `t t~ + 2 jets`: the jet flavours differ
    event by event (gg, gu, ud~, ...), so what must hold is one top, one
    antitop, and exactly n_jets more partons of any allowed flavour.
    """
    remaining = list(pdgs)
    for want in required:
        if want not in remaining:
            return f"no {want} in final state {tuple(sorted(pdgs))}"
        remaining.remove(want)
    if len(remaining) != n_jets:
        return (f"{len(remaining)} extra particles, wanted {n_jets} "
                f"(final state {tuple(sorted(pdgs))})")
    stray = [p for p in remaining if p not in jet_pdgs]
    if stray:
        return f"non-parton {tuple(sorted(stray))} among the extra particles"
    return None


def verify_lhe(path: Path,
               nevents: int,
               beam_energy_gev: float,
               final_state: tuple[int, ...] = (-6, 6),
               n_extra_jets: int = 0,
               jet_pdgs: tuple[int, ...] = DEFAULT_JET_PDGS,
               beam_pdg: tuple[int, int] = (2212, 2212),
               energy_rtol: float = 1e-6,
               max_events_checked: int | None = None) -> DeliverableReport:
    """Verify an LHE file against the physics the task asked for."""
    report = DeliverableReport(path=str(path), exists=path.exists())
    if not report.exists:
        return report

    def check(name, ok, detail):
        report.checks.append(Check(name=name, ok=bool(ok), detail=detail))

    init = read_init(path)
    if init is None:
        check("beams", False, "no readable <init> block")
    else:
        got_e = init["beam_energy_gev"]
        ok_e = all(abs(e - beam_energy_gev) <= energy_rtol * beam_energy_gev
                   for e in got_e)
        check("beam energy", ok_e,
              f"{got_e[0]:g} / {got_e[1]:g} GeV, wanted {beam_energy_gev:g} each")
        got_b = init["beam_pdg"]
        check("beam particles", tuple(got_b) == tuple(beam_pdg),
              f"PDG {got_b[0]} / {got_b[1]}, wanted {beam_pdg[0]} / {beam_pdg[1]}")

    wanted_fs = tuple(sorted(final_state))
    n = 0
    bad_fs: str | None = None
    unparsed = 0
    for ev in iter_events(path):
        n += 1
        if max_events_checked is None or n <= max_events_checked:
            got = final_state_pdgs(ev)
            if got is None:
                unparsed += 1
            else:
                problem = check_event_topology(got, wanted_fs, n_extra_jets,
                                               tuple(jet_pdgs))
                if problem and bad_fs is None:
                    bad_fs = f"event {n}: {problem}"
    report.n_events = n

    wanted_desc = " + ".join(
        [str(wanted_fs)] + ([f"{n_extra_jets} jets"] if n_extra_jets else []))
    check("event count", n == nevents, f"{n} events, wanted {nevents}")
    if unparsed:
        check("event records parse", False, f"{unparsed} unparsable event(s)")
    if n == 0:
        # "every event is t t~ + 2 jets" is vacuously true of no events, and
        # reads as a pass. An empty file has not been checked, it has nothing
        # to check.
        check("final state", False, "no events to check")
    else:
        check("final state", bad_fs is None,
              f"every event is {wanted_desc}" if bad_fs is None
              else f"wanted {wanted_desc}; {bad_fs}")

    if n:
        report.n_events, report.events_sha256 = event_summary(path)
    report.generator_version = read_generator_version(path)
    return report


@dataclass
class MassPeakReport:
    path: str
    exists: bool
    checks: list[Check] = field(default_factory=list)
    entries: int = 0
    peak_gev: float | None = None
    fwhm_gev: float | None = None
    prominence: float | None = None
    histogram_sha256: str = ""

    @property
    def ok(self) -> bool:
        return self.exists and all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


# A casually-worded brief does not dictate key names, so accept the spellings
# a physicist would reach for, and fall back to structure: in a histogram one
# array has exactly one more entry than the other.
EDGE_KEYS = ("bin_edges_gev", "bin_edges", "edges", "bins", "x")
COUNT_KEYS = ("counts", "values", "y", "n", "entries")


def _numeric_list(v):
    if not isinstance(v, list) or not v:
        return None
    try:
        return [float(x) for x in v]
    except (TypeError, ValueError):
        return None


def extract_histogram(data: dict):
    """(edges, counts) from a histogram JSON, however it was spelled."""
    if not isinstance(data, dict):
        return None, None
    arrays = {k: a for k, a in ((k, _numeric_list(v)) for k, v in data.items())
              if a is not None}
    for ek in EDGE_KEYS:
        for ck in COUNT_KEYS:
            if ek in arrays and ck in arrays and len(arrays[ek]) == len(arrays[ck]) + 1:
                return arrays[ek], arrays[ck]
    # structural fallback: any two arrays in the N+1 / N relationship
    for ek, e in arrays.items():
        for ck, c in arrays.items():
            if ek != ck and len(e) == len(c) + 1:
                return e, c
    return None, None


def histogram_fingerprint(edges: list[float], counts: list[float]) -> str:
    """SHA-256 over the histogram's values, not its JSON text.

    Formatting choices — trailing zeros, integer vs float, key order — are not
    physics, so two identical histograms written differently must fingerprint
    the same. Values are rendered at a fixed precision to make that so.
    """
    import hashlib

    payload = (";".join(f"{e:.9g}" for e in edges) + "|"
               + ";".join(f"{c:.9g}" for c in counts))
    return hashlib.sha256(payload.encode()).hexdigest()


def _peak_stats(edges: list[float], counts: list[float]):
    """(index, centre, FWHM, prominence) of the tallest bin.

    Prominence is the peak height over the median bin — a flat or monotonic
    distribution scores ~1 however tall its maximum bin happens to be, so this
    separates a peak from the top of a slope.
    """
    peak_i = max(range(len(counts)), key=lambda i: counts[i])
    centre = (edges[peak_i] + edges[peak_i + 1]) / 2.0
    peak = counts[peak_i]

    ordered = sorted(counts)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2.0)
    prominence = peak / median if median > 0 else float("inf")

    # width at half maximum, walking out from the peak until the counts drop
    half = peak / 2.0
    lo = peak_i
    while lo > 0 and counts[lo] > half:
        lo -= 1
    hi = peak_i
    while hi < len(counts) - 1 and counts[hi] > half:
        hi += 1
    fwhm = edges[hi + 1] - edges[lo] if hi > lo else None
    return peak_i, centre, fwhm, prominence


def verify_mass_peak(path: Path,
                     expect_gev: float,
                     tolerance_gev: float,
                     min_entries: int = 200,
                     min_prominence: float = 1.5) -> MassPeakReport:
    """Check a reconstructed invariant-mass histogram for a top peak.

    Reads the numbers the brief asks for alongside the figure, because the
    figure cannot be checked: a plot with the axes relabelled looks the same
    to a grader.
    """
    report = MassPeakReport(path=str(path), exists=path.exists())
    if not report.exists:
        return report

    def check(name, ok, detail):
        report.checks.append(Check(name=name, ok=bool(ok), detail=detail))

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        check("histogram parses", False, str(e))
        return report
    if not isinstance(data, dict):
        check("histogram parses", False, "not a JSON object")
        return report

    edges, counts = extract_histogram(data)
    if edges is None:
        check("histogram parses", False,
              "no pair of arrays looks like bin edges and counts (one array "
              "must have exactly one more entry than the other)")
        return report
    try:
        edges = [float(x) for x in edges]
        counts = [float(x) for x in counts]
    except (TypeError, ValueError):
        check("histogram parses", False, "non-numeric bin edges or counts")
        return report
    if len(edges) != len(counts) + 1 or not counts:
        check("histogram parses", False,
              f"{len(edges)} edges for {len(counts)} bins — needs one more edge "
              "than counts")
        return report
    if any(b <= a for a, b in zip(edges, edges[1:])):
        check("histogram parses", False, "bin edges are not increasing")
        return report
    check("histogram parses", True, f"{len(counts)} bins, "
                                    f"{edges[0]:g}-{edges[-1]:g} GeV")

    total = sum(counts)
    report.entries = int(total)
    check("entries", total >= min_entries,
          f"{total:.0f} entries, wanted at least {min_entries}")

    report.histogram_sha256 = histogram_fingerprint(edges, counts)
    peak_i, centre, fwhm, prominence = _peak_stats(edges, counts)
    report.peak_gev, report.fwhm_gev, report.prominence = centre, fwhm, prominence

    # A maximum in the first or last bin is the end of a slope, not a peak —
    # the distribution was probably never cut around the mass region.
    interior = 0 < peak_i < len(counts) - 1
    check("peak is interior", interior,
          f"tallest bin at {centre:.1f} GeV"
          + ("" if interior else " — that is the edge of the range, so the "
                                 "histogram shows a tail, not a peak"))
    check("peak position", abs(centre - expect_gev) <= tolerance_gev,
          f"{centre:.1f} GeV, wanted {expect_gev:g} ± {tolerance_gev:g}"
          + (f" (FWHM ~{fwhm:.0f} GeV)" if fwhm else ""))
    check("peak prominence", prominence >= min_prominence,
          f"{prominence:.1f}x the median bin, wanted at least {min_prominence:g}x"
          if math.isfinite(prominence)
          else "stands over an empty median bin — most of the histogram is "
               "empty, so the peak is prominent but the range is very sparse")
    return report


@dataclass
class OpenSpec:
    """The operator's grading key for an open task. Never shown to the agent."""
    name: str
    deliverables: dict          # role -> path relative to the workspace
    requirements: dict

    @property
    def events_path(self) -> str:
        return self.deliverables["events"]

    @property
    def hepmc_path(self) -> str | None:
        return self.deliverables.get("hepmc")

    @property
    def histogram_path(self) -> str | None:
        return self.deliverables.get("mass_histogram")

    @property
    def plot_path(self) -> str | None:
        return self.deliverables.get("mass_plot")

    def verify(self, path: Path) -> DeliverableReport:
        r = self.requirements
        return verify_lhe(
            path,
            nevents=int(r["nevents"]),
            beam_energy_gev=float(r["beam_energy_gev"]),
            final_state=tuple(r.get("final_state_pdg", (-6, 6))),
            n_extra_jets=int(r.get("extra_jets", 0)),
            jet_pdgs=tuple(r.get("jet_pdg", DEFAULT_JET_PDGS)),
            beam_pdg=tuple(r.get("beam_pdg", (2212, 2212))),
        )

    def verify_hepmc(self, path: Path) -> DeliverableReport:
        r = self.requirements
        return verify_hepmc(
            path,
            nevents=int(r.get("hepmc_nevents", r["nevents"])),
            require_pdgs=tuple(r.get("hepmc_require_pdg", ())),
            min_particles_per_event=int(r.get("min_particles_per_event", 50)),
        )

    def verify_peak(self, path: Path) -> MassPeakReport:
        r = self.requirements
        return verify_mass_peak(
            path,
            expect_gev=float(r.get("top_mass_gev", 172.5)),
            tolerance_gev=float(r.get("mass_tolerance_gev", 15.0)),
            min_entries=int(r.get("min_histogram_entries", 200)),
            min_prominence=float(r.get("min_peak_prominence", 1.5)),
        )


def load_open_spec(task_dir: Path) -> OpenSpec:
    import yaml

    path = task_dir / "spec.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no spec.yaml in {task_dir}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("kind") != "open":
        raise ValueError(f"{path}: not an open-task spec (needs kind: open)")
    for key in ("name", "deliverable", "requirements"):
        if key not in data:
            raise ValueError(f"{path}: missing required key '{key}'")
    deliverables = data["deliverable"]
    if not isinstance(deliverables, dict) or "events" not in deliverables:
        raise ValueError(f"{path}: 'deliverable' needs at least an 'events' path")
    return OpenSpec(name=data["name"],
                    deliverables={k: str(v) for k, v in deliverables.items()},
                    requirements=data["requirements"])


def find_histogram(root: Path) -> Path | None:
    """The JSON under `root` that parses as a histogram, largest first."""
    for p in sorted((q for q in root.glob("**/*.json")
                     if q.is_file() and not _is_scratch(q, root)),
                    key=lambda q: q.stat().st_size, reverse=True):
        try:
            edges, counts = extract_histogram(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
        if edges is not None:
            return p
    return None


# Directories a generator fills with its own working files. MadGraph leaves
# zero-event `events.lhe` scratch files under SubProcesses/*/Hel/ and diagram
# images under the same tree; grading those answers a question nobody asked.
SCRATCH_DIRS = frozenset({
    "SubProcesses", "Source", "lib", "Cards", "bin", "Hel", "MCatNLO",
    "HTML", "madevent", "internal", "Template", "__pycache__",
})


def _is_scratch(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return any(p in SCRATCH_DIRS for p in parts)


def find_deliverable(root: Path, patterns: tuple[str, ...] =
                     ("**/*.lhe.gz", "**/*.lhe")) -> list[Path]:
    """Candidate LHE files under a workspace, most events first.

    Ranked by how many events actually parse, not by file size. A generator's
    working tree is full of plausible-looking LHE files with nothing in them,
    and picking one of those produced a report that failed on "0 events" while
    passing the final-state check — the wrong file graded convincingly.
    Generator scratch directories are skipped outright; a file inside one is
    only considered if nothing else exists at all.
    """
    seen: list[Path] = []
    for pat in patterns:
        for p in root.glob(pat):
            if p.is_file() and p not in seen:
                seen.append(p)

    def rank(p: Path):
        try:
            n = sum(1 for _ in iter_events(p))
        except (OSError, EOFError):
            n = 0
        # real output first, then anything with events, then by size
        return (0 if _is_scratch(p, root) else 1, n, p.stat().st_size)

    return sorted(seen, key=rank, reverse=True)


def verify_hepmc(path: Path, nevents: int, require_pdgs: tuple[int, ...] = (),
                 min_particles_per_event: int = 50) -> DeliverableReport:
    """Grade a showered HepMC record on its contents.

    The particle-count floor is the check that matters. Writing an LHE out as
    HepMC is a format conversion, not a shower, and the two are indistinguishable
    from the header — but a parton-level ttbar+2j record holds under a dozen
    particles where a showered and hadronised one holds hundreds. Without it,
    skipping Pythia entirely passes every other check here.
    """
    from llm_energy import hepmc

    report = DeliverableReport(path=str(path), exists=path.exists())
    if not report.exists:
        return report

    def check(name, ok, detail):
        report.checks.append(Check(name=name, ok=bool(ok), detail=detail))

    header = hepmc.read_header(path)
    if header is None:
        check("format", False, "not a readable HepMC2 or HepMC3 ASCII file")
        return report
    report.generator_version = f"HepMC{header.version_major}" + (
        f" {header.version}" if header.version else "")
    check("format", True, f"HepMC{header.version_major}"
                          + (f" (v{header.version})" if header.version else ""))

    n = 0
    particle_counts: list[int] = []
    missing_pdgs = None
    # Over the particle lines only. Event headers carry counters and weights
    # that differ between writers without the physics differing.
    digest = hashlib.sha256()
    for event in hepmc.iter_events(path):
        n += 1
        particle_counts.append(hepmc.count_particles(event))
        for line in event:
            if line.startswith("P "):
                digest.update(" ".join(line.split()).encode())
                digest.update(b"\n")
        if require_pdgs and missing_pdgs is None:
            present = set(hepmc.particle_pdgs(event, header.version_major))
            absent = [p for p in require_pdgs if p not in present]
            if absent:
                missing_pdgs = (n, absent)
    report.n_events = n
    if n:
        report.events_sha256 = digest.hexdigest()
    check("event count", n == nevents, f"{n} events, wanted {nevents}")

    if n == 0:
        # Vacuously true of nothing, and it reads as a pass. An empty record
        # has not been checked; it has nothing to check.
        check("showered", False, "no events to check")
        if require_pdgs:
            check("hard process", False, "no events to check")
        return report

    median = sorted(particle_counts)[len(particle_counts) // 2]
    check("showered", median >= min_particles_per_event,
          f"median {median} particles per event"
          + ("" if median >= min_particles_per_event else
             f" — under {min_particles_per_event}, so this looks like the "
             "parton-level record converted to HepMC rather than showered"))

    if require_pdgs:
        wanted = ", ".join(str(p) for p in require_pdgs)
        check("hard process", missing_pdgs is None,
              f"every event contains {wanted}" if missing_pdgs is None
              else f"event {missing_pdgs[0]} is missing PDG "
                   f"{', '.join(str(p) for p in missing_pdgs[1])}")
    return report


def find_plot(root: Path) -> Path | None:
    """A figure the agent produced, ignoring the generator's own images."""
    best: Path | None = None
    for pat in ("**/*.pdf", "**/*.png", "**/*.svg", "**/*.jpg"):
        for p in root.glob(pat):
            if not p.is_file() or _is_scratch(p, root):
                continue
            if best is None or p.stat().st_size > best.stat().st_size:
                best = p
    return best


@dataclass
class GradedWorkspace:
    """Every artefact an open run was asked for, graded together."""
    events: DeliverableReport | None = None
    hepmc: DeliverableReport | None = None
    peak: MassPeakReport | None = None
    plot_missing: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.events is not None and self.events.ok
                and (self.hepmc is None or self.hepmc.ok)
                and (self.peak is None or self.peak.ok)
                and not self.plot_missing)


def grade_workspace(spec: "OpenSpec", workspace: Path,
                    events_override: Path | None = None) -> GradedWorkspace:
    """Grade a whole open run against whatever its spec asks for.

    Each artefact is looked for where the brief said to put it, then by shape
    if it is not there — a sample in the wrong place is a naming slip rather
    than wrong physics. Only the artefacts named in the spec's `deliverable`
    block are graded, so dropping one from the task drops it from the verdict.
    """
    graded = GradedWorkspace()

    events = events_override or (workspace / spec.events_path)
    if not events.exists() and events_override is None:
        found = find_deliverable(workspace)
        if found:
            events = found[0]
            graded.notes.append(
                f"nothing at {spec.events_path}; grading "
                f"{events.relative_to(workspace)} instead")
    graded.events = spec.verify(events) if events.exists() else None

    if spec.hepmc_path:
        from llm_energy.hepmc import find_hepmc

        shower = workspace / spec.hepmc_path
        if not shower.exists():
            found = find_hepmc(workspace)
            if found:
                shower = found[0]
                graded.notes.append(
                    f"nothing at {spec.hepmc_path}; grading "
                    f"{shower.relative_to(workspace)} instead")
        graded.hepmc = spec.verify_hepmc(shower)

    # The brief asks for files "somewhere in this directory" rather than at
    # fixed paths, so look for the shape of each artefact and fall back to a
    # search. A histogram under another name is a naming choice; a missing one
    # is a missing result.
    if spec.histogram_path:
        hist = workspace / spec.histogram_path
        if not hist.exists():
            found = find_histogram(workspace)
            if found is not None:
                hist = found
                graded.notes.append(
                    f"histogram found at {found.relative_to(workspace)} rather "
                    f"than {spec.histogram_path}")
        # When the search comes up empty this keeps the suggested path as the
        # report's `path`, which is a name the run never wrote. Callers must
        # check `.exists` before showing it as the file that was graded.
        graded.peak = spec.verify_peak(hist)

    if spec.plot_path:
        plot = workspace / spec.plot_path
        if not plot.exists():
            found = find_plot(workspace)
            if found is not None:
                graded.notes.append(
                    f"plot found at {found.relative_to(workspace)} rather than "
                    f"{spec.plot_path}")
            graded.plot_missing = found is None
        else:
            graded.plot_missing = False

    return graded


@dataclass
class DeliverableComparison:
    """Two or more open runs, compared artefact by artefact."""
    labels: list[str] = field(default_factory=list)
    event_hashes: list[str] = field(default_factory=list)
    hepmc_hashes: list[str] = field(default_factory=list)
    histogram_hashes: list[str] = field(default_factory=list)
    generator_versions: list[str | None] = field(default_factory=list)
    hepmc_versions: list[str | None] = field(default_factory=list)
    peaks_gev: list[float | None] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def events_identical(self) -> bool:
        hashes = [h for h in self.event_hashes if h]
        return len(hashes) == len(self.labels) and len(set(hashes)) == 1

    @property
    def showers_compared(self) -> bool:
        return any(h for h in self.hepmc_hashes)

    @property
    def showers_identical(self) -> bool:
        hashes = [h for h in self.hepmc_hashes if h]
        return len(hashes) == len(self.labels) and len(set(hashes)) == 1

    @property
    def histograms_identical(self) -> bool:
        hashes = [h for h in self.histogram_hashes if h]
        return len(hashes) == len(self.labels) and len(set(hashes)) == 1

    @property
    def versions_known(self) -> bool:
        return all(v for v in self.generator_versions)

    @property
    def versions_agree(self) -> bool:
        """True only when every version is known and they match.

        Unknown is not the same as differing: a sample whose banner carried no
        version tells us nothing about whether the versions matched, and
        reporting that as a difference would blame the wrong thing.
        """
        return (self.versions_known
                and len(set(self.generator_versions)) == 1)

    def peak_spread_gev(self) -> float | None:
        known = [p for p in self.peaks_gev if p is not None]
        return max(known) - min(known) if len(known) > 1 else None


def compare_graded(labelled: list[tuple[str, GradedWorkspace]]) -> DeliverableComparison:
    """Compare graded runs.

    Expectations differ by artefact, and conflating them would mislead:

    - **Events must match.** Same generator, same version, same pinned seed —
      a difference means a seed was not honoured or the versions differ.
    - **Showers need not, quite.** The brief pins the shower seed, so an
      identical pipeline reproduces the record exactly — but Pythia's version
      and tune are the agent's to choose, and either changes the output from
      the same seed and the same LHE. A difference is a difference in method,
      not evidence a seed was dropped, and it is only informative next to the
      event comparison: same events, different showers isolates the shower.
    - **Histograms need not.** Two agents that reconstruct the top differently
      land on different histograms from identical events; that is the method
      varying, not a reproducibility failure. Identical histograms mean the
      pipelines agree all the way down, which is what a rerun should show.
    """
    comp = DeliverableComparison()
    for label, g in labelled:
        comp.labels.append(label)
        comp.event_hashes.append(g.events.events_sha256 if g.events else "")
        comp.generator_versions.append(
            g.events.generator_version if g.events else None)
        comp.hepmc_hashes.append(g.hepmc.events_sha256 if g.hepmc else "")
        comp.hepmc_versions.append(g.hepmc.generator_version if g.hepmc else None)
        comp.histogram_hashes.append(g.peak.histogram_sha256 if g.peak else "")
        comp.peaks_gev.append(g.peak.peak_gev if g.peak else None)

    if not comp.events_identical:
        if not comp.versions_known:
            comp.notes.append(
                "generator versions were not recorded in every sample, so a "
                "version difference cannot be ruled out as the cause — check "
                "the agents' reports before blaming a seed")
        elif not comp.versions_agree:
            comp.notes.append(
                "generator versions differ ("
                + ", ".join(str(v) for v in comp.generator_versions)
                + "), which alone explains different events at a fixed seed")
        else:
            comp.notes.append(
                "same generator version but different events — a seed was not "
                "honoured somewhere in the hard-process step")
    if comp.showers_compared and not comp.showers_identical:
        if comp.events_identical:
            comp.notes.append(
                "identical events but different showers — the hard process "
                "reproduced and the shower did not, so the difference is in "
                "the Pythia version, tune or seed rather than in MadGraph"
                + ("" if len(set(comp.hepmc_versions)) == 1 else
                   " (the HepMC writers differ too: "
                   + ", ".join(str(v) for v in comp.hepmc_versions) + ")"))
        else:
            comp.notes.append(
                "showers differ, but so do the events they were made from — "
                "fix the hard-process difference before reading anything into "
                "this one")
    spread = comp.peak_spread_gev()
    if spread:
        comp.notes.append(
            f"peak positions span {spread:.1f} GeV across runs — expected when "
            "the reconstruction methods differ, since the events do not")
    return comp
