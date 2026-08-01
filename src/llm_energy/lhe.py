"""LHE (Les Houches Event) file comparison.

Used to confirm that task runs coordinated by different LLMs produced
identical physics output. Only <event> block content is compared — the LHE
header (<init> banner, MG5 banner) contains timestamps, hostnames, and paths
that legitimately differ between runs.

Two levels of comparison:
  1. exact: SHA-256 over whitespace-normalized event text (same MG5 version,
     same seed, same arch => identical expected);
  2. numeric: token-by-token float comparison with relative tolerance, for
     cross-architecture runs where the last ulp may differ.
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


def _open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def iter_events(path: Path) -> Iterator[list[str]]:
    """Yield each <event> block as a list of normalized lines (whitespace
    collapsed, comments inside the block dropped)."""
    in_event = False
    lines: list[str] = []
    with _open_text(path) as fh:
        for raw in fh:
            stripped = raw.strip()
            if stripped.startswith("<event"):
                in_event = True
                lines = []
                continue
            if stripped.startswith("</event>"):
                in_event = False
                yield lines
                continue
            if in_event:
                if stripped.startswith("#") or stripped.startswith("<"):
                    continue  # generator comments / nested tags (e.g. <mgrwt>)
                if stripped:
                    lines.append(" ".join(stripped.split()))


def event_summary(path: Path) -> tuple[int, str]:
    """(n_events, sha256 of normalized event content)."""
    h = hashlib.sha256()
    n = 0
    for ev in iter_events(path):
        for line in ev:
            h.update(line.encode())
            h.update(b"\n")
        h.update(b"--event--\n")
        n += 1
    return n, h.hexdigest()


def read_init(path: Path) -> dict | None:
    """Beam configuration from the LHE `<init>` block.

    The first line after `<init>` is, per the Les Houches accord:
        IDBMUP(1) IDBMUP(2) EBMUP(1) EBMUP(2) PDFGUP(1..2) PDFSUP(1..2)
        IDWTUP NPRUP
    Read from the machine-readable block rather than the MG5 banner, which is
    free text and varies by version.
    """
    with _open_text(path) as fh:
        for raw in fh:
            if raw.strip().startswith("<init"):
                break
        else:
            return None
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith(("#", "<")):
                continue
            f = line.split()
            if len(f) < 4:
                return None
            try:
                return {"beam_pdg": (int(float(f[0])), int(float(f[1]))),
                        "beam_energy_gev": (float(f[2]), float(f[3]))}
            except ValueError:
                return None
    return None


def read_generator_version(path: Path) -> str | None:
    """Generator version from the LHE header, if it recorded one.

    Two runs with the same seed but different generator versions produce
    different events, so a version difference has to be distinguishable from
    a mistake when hashes disagree. Only the header is scanned — the events
    themselves can be millions of lines.
    """
    import re

    want_next = False
    with _open_text(path) as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("<event"):
                break
            if want_next and line and not line.startswith("<"):
                return line
            if line.startswith("<MGVersion>"):
                inline = line[len("<MGVersion>"):].replace("</MGVersion>", "").strip()
                if inline:
                    return inline
                want_next = True
                continue
            m = re.search(r"MadGraph5_aMC@NLO\s+v?\.?\s*([\d][\w.]*)", line)
            if m:
                return m.group(1)
    return None


def final_state_pdgs(event_lines: list[str]) -> tuple[int, ...] | None:
    """Sorted PDG ids of the outgoing particles in one normalized event block.

    Event record: a header line `NUP IDPRUP XWGTUP SCALUP AQEDUP AQCDUP`, then
    NUP particle lines whose second field ISTUP is +1 for a final-state
    particle. Reading the events themselves checks the physics that was
    actually generated, independent of what the banner claims.
    """
    if not event_lines:
        return None
    try:
        nup = int(float(event_lines[0].split()[0]))
    except (ValueError, IndexError):
        return None
    out: list[int] = []
    for line in event_lines[1:1 + nup]:
        f = line.split()
        if len(f) < 2:
            return None
        try:
            if int(float(f[1])) == 1:
                out.append(int(float(f[0])))
        except ValueError:
            return None
    return tuple(sorted(out))


@dataclass
class LheComparison:
    files: list[str]
    n_events: list[int]
    hashes: list[str]
    identical: bool
    numerically_equal: bool | None = None   # set when exact fails and rtol given
    rtol: float | None = None
    first_difference: str | None = None
    notes: list[str] = field(default_factory=list)


def _numeric_equal(a: list[str], b: list[str], rtol: float) -> str | None:
    """None if event blocks match within rtol; else description of first diff."""
    if len(a) != len(b):
        return f"line count {len(a)} vs {len(b)}"
    for i, (la, lb) in enumerate(zip(a, b)):
        ta, tb = la.split(), lb.split()
        if len(ta) != len(tb):
            return f"line {i}: token count {len(ta)} vs {len(tb)}"
        for j, (xa, xb) in enumerate(zip(ta, tb)):
            if xa == xb:
                continue
            try:
                fa, fb = float(xa), float(xb)
            except ValueError:
                return f"line {i} token {j}: {xa!r} vs {xb!r}"
            denom = max(abs(fa), abs(fb), 1e-300)
            if abs(fa - fb) / denom > rtol:
                return f"line {i} token {j}: {fa} vs {fb} (rel diff {abs(fa-fb)/denom:.2e})"
    return None


def compare_lhe(paths: list[Path], rtol: float | None = 1e-9) -> LheComparison:
    if len(paths) < 2:
        raise ValueError("need at least two LHE files to compare")
    summaries = [event_summary(p) for p in paths]
    n_events = [s[0] for s in summaries]
    hashes = [s[1] for s in summaries]
    comp = LheComparison(
        files=[str(p) for p in paths],
        n_events=n_events,
        hashes=hashes,
        identical=len(set(hashes)) == 1,
    )
    if comp.identical:
        return comp

    if len(set(n_events)) != 1:
        comp.first_difference = f"event counts differ: {n_events}"
        comp.numerically_equal = False if rtol is not None else None
        comp.rtol = rtol
        return comp

    if rtol is not None:
        comp.rtol = rtol
        ref, ref_path = None, paths[0]
        comp.numerically_equal = True
        for other in paths[1:]:
            for idx, (ev_a, ev_b) in enumerate(zip(iter_events(ref_path),
                                                   iter_events(other))):
                diff = _numeric_equal(ev_a, ev_b, rtol)
                if diff is not None:
                    comp.numerically_equal = False
                    comp.first_difference = (
                        f"{ref_path.name} vs {Path(other).name}, event {idx}: {diff}")
                    break
            if not comp.numerically_equal:
                break
        if comp.numerically_equal:
            comp.notes.append(
                f"exact hashes differ but all values agree within rtol={rtol} "
                "(expected for cross-architecture runs)")
    return comp
