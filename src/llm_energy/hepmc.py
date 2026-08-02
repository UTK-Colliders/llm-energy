"""Read HepMC2 and HepMC3 ASCII event records.

The parallel of `lhe.py` for the showered output: enough parsing to answer
"is this really N showered ttbar+2j events" from the payload, without pulling
in HepMC's own bindings. Graded on content, not on the writer's banner — a
file that says HepMC3 at the top and holds nothing still fails.

Both ASCII flavours are line-oriented and start each event with an `E` line.
They differ in where the PDG id sits on a particle line, which is the only
reason the version matters here:

    HepMC2  P barcode pdg px py pz e m status ...
    HepMC3  P id vertex pdg px py pz e m status
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Where the PDG id sits on a `P` line, by flavour, counting the "P" itself.
_PDG_FIELD = {2: 2, 3: 3}

_START_MARKERS = {
    "HepMC::IO_GenEvent-START_EVENT_LISTING": 2,
    "HepMC::Asciiv3-START_EVENT_LISTING": 3,
    # HepMC2 files written by older IO backends
    "HepMC::IO_Ascii-START_EVENT_LISTING": 2,
    "HepMC::IO_ExtendedAscii-START_EVENT_LISTING": 2,
}
_END_MARKERS = tuple(m.replace("START", "END") for m in _START_MARKERS)


def _open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


@dataclass
class HepMCHeader:
    version_major: int          # 2 or 3
    version: str = ""           # e.g. "3.02.05", when the file states one


def read_header(path: Path) -> HepMCHeader | None:
    """Flavour and version, or None if this is not a HepMC ASCII file.

    Scans a bounded prefix: a file that never declares itself is not one,
    and reading a multi-gigabyte record to find that out is not worth it.
    """
    version = ""
    try:
        with _open_text(path) as fh:
            for _, raw in zip(range(50), fh):
                line = raw.strip()
                if line.startswith("HepMC::Version"):
                    parts = line.split()
                    if len(parts) > 1:
                        version = parts[1]
                    continue
                major = _START_MARKERS.get(line)
                if major is not None:
                    return HepMCHeader(version_major=major, version=version)
    except (OSError, EOFError, UnicodeDecodeError):
        return None
    return None


def iter_events(path: Path) -> Iterator[list[str]]:
    """Yield each event as its list of stripped lines, `E` line first.

    Only lines between the start and end listing markers count. Anything
    outside them is header or trailer, not an event.
    """
    header = read_header(path)
    if header is None:
        return
    listing = False
    current: list[str] | None = None
    try:
        with _open_text(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not listing:
                    listing = line in _START_MARKERS
                    continue
                if line.startswith(_END_MARKERS):
                    break
                if line.startswith("E "):
                    if current is not None:
                        yield current
                    current = [line]
                elif current is not None:
                    current.append(line)
    except (OSError, EOFError, UnicodeDecodeError):
        pass
    if current is not None:
        yield current


def particle_pdgs(event: list[str], version_major: int) -> list[int]:
    """PDG ids of every particle in one event record."""
    field = _PDG_FIELD.get(version_major, 3)
    pdgs = []
    for line in event:
        if not line.startswith("P "):
            continue
        parts = line.split()
        if len(parts) > field:
            try:
                pdgs.append(int(parts[field]))
            except ValueError:
                continue
    return pdgs


def count_particles(event: list[str]) -> int:
    return sum(1 for line in event if line.startswith("P "))


def find_hepmc(root: Path) -> list[Path]:
    """HepMC candidates under a workspace, most events first.

    Ranked by parsed event count for the same reason the LHE search is: a
    generator's working tree holds plausible-looking files with nothing in
    them, and picking one grades convincingly against the wrong thing.
    """
    from llm_energy.deliverable import _is_scratch

    seen: list[Path] = []
    for pat in ("**/*.hepmc", "**/*.hepmc3", "**/*.hepmc.gz", "**/*.hepmc3.gz",
                "**/*.hepmc2", "**/*.hepmc2.gz"):
        for p in root.glob(pat):
            if p.is_file() and p not in seen:
                seen.append(p)
    # Pythia's default output is often just `.dat` or has no suffix at all, so
    # fall back to sniffing anything small enough to be worth opening.
    if not seen:
        for p in root.glob("**/*"):
            if (p.is_file() and p.suffix.lower() in ("", ".dat", ".txt", ".hepmc")
                    and read_header(p) is not None):
                seen.append(p)

    def rank(p: Path):
        n = sum(1 for _ in iter_events(p))
        return (0 if _is_scratch(p, root) else 1, n, p.stat().st_size)

    return sorted(seen, key=rank, reverse=True)
