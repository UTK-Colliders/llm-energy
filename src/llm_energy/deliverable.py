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
class OpenSpec:
    """The operator's grading key for an open task. Never shown to the agent."""
    name: str
    deliverable_path: str
    requirements: dict

    def verify(self, path: Path) -> DeliverableReport:
        r = self.requirements
        return verify_lhe(
            path,
            nevents=int(r["nevents"]),
            beam_energy_gev=float(r["beam_energy_gev"]),
            final_state=tuple(r.get("final_state_pdg", (-6, 6))),
            beam_pdg=tuple(r.get("beam_pdg", (2212, 2212))),
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
    return OpenSpec(name=data["name"],
                    deliverable_path=str(data["deliverable"]["path"]),
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
