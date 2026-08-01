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

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from llm_energy.lhe import event_summary, final_state_pdgs, iter_events, read_init


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

    @property
    def ok(self) -> bool:
        return self.exists and all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def verify_lhe(path: Path,
               nevents: int,
               beam_energy_gev: float,
               final_state: tuple[int, ...] = (-6, 6),
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
    bad_fs: tuple | None = None
    unparsed = 0
    for ev in iter_events(path):
        n += 1
        if max_events_checked is None or n <= max_events_checked:
            got = final_state_pdgs(ev)
            if got is None:
                unparsed += 1
            elif got != wanted_fs and bad_fs is None:
                bad_fs = got
    report.n_events = n

    check("event count", n == nevents, f"{n} events, wanted {nevents}")
    if unparsed:
        check("event records parse", False, f"{unparsed} unparsable event(s)")
    check("final state", bad_fs is None,
          f"all events {wanted_fs}" if bad_fs is None
          else f"found an event with final state {bad_fs}, wanted {wanted_fs}")

    if n:
        report.n_events, report.events_sha256 = event_summary(path)
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

    @property
    def ok(self) -> bool:
        return self.exists and all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


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

    edges, counts = data.get("bin_edges_gev"), data.get("counts")
    if not isinstance(edges, list) or not isinstance(counts, list):
        check("histogram parses", False,
              "needs 'bin_edges_gev' and 'counts' arrays")
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
            beam_pdg=tuple(r.get("beam_pdg", (2212, 2212))),
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


def find_deliverable(root: Path, patterns: tuple[str, ...] =
                     ("**/*.lhe.gz", "**/*.lhe")) -> list[Path]:
    """Candidate LHE files under a workspace, largest first.

    The agent chose where to put things, so the operator should not have to go
    looking. Largest first because a real 10k-event sample outweighs any
    test-run leftovers beside it.
    """
    seen: list[Path] = []
    for pat in patterns:
        for p in root.glob(pat):
            if p.is_file() and p not in seen:
                seen.append(p)
    return sorted(seen, key=lambda p: p.stat().st_size, reverse=True)


@dataclass
class GradedWorkspace:
    """Every artefact an open run was asked for, graded together."""
    events: DeliverableReport | None = None
    peak: MassPeakReport | None = None
    plot_missing: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.events is not None and self.events.ok
                and (self.peak is None or self.peak.ok)
                and not self.plot_missing)


def grade_workspace(spec: "OpenSpec", workspace: Path,
                    events_override: Path | None = None) -> GradedWorkspace:
    """Grade a whole open run: events, mass peak, and the figure's presence.

    Each artefact is looked for where the brief said to put it. The events
    fall back to a search, because a sample in the wrong place is a naming
    slip rather than wrong physics; the histogram does not, since guessing
    which JSON in a workspace is the histogram would invent a result.
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

    if spec.histogram_path:
        hist = workspace / spec.histogram_path
        graded.peak = spec.verify_peak(hist)
        if not hist.exists():
            graded.notes.append(
                f"no histogram at {spec.histogram_path} — the top mass peak "
                "could not be checked, only the plot's existence")

    if spec.plot_path:
        graded.plot_missing = not (workspace / spec.plot_path).exists()

    return graded
