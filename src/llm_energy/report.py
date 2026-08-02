"""Comparison reports: E_task (measured) vs E_LLM (estimated band).

Single-trial report: one task run + one coordination session.
Multi-trial comparison: several trials (e.g. different LLM models
coordinating the same task) side by side, including whether the output
events are physically identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from llm_energy.lhe import compare_lhe
from llm_energy.schemas import (EnergyBand, SessionEnergyResult,
                                SessionPowerResult, TaskRunResult)

CAVEATS = [
    "E_task is SoC package power (CPU+GPU+ANE) integrated over the run — not "
    "wall power; DRAM (partially), SSD, display, and PSU losses are excluded.",
    "E_LLM is an estimate from token counts and literature-derived "
    "coefficients including a PUE factor — measured local energy is being "
    "compared to estimated remote datacenter energy.",
    "The E_LLM band is a low/high envelope across published estimates, not a "
    "statistical confidence interval.",
    "Embodied energy (hardware manufacturing) and model training energy are "
    "excluded on both sides.",
]

LOCAL_COORD_CAVEAT = (
    "E_coord,local is this machine's package energy over the session window "
    "minus the nested task run — the local cost of the agent working. It is "
    "measured on the same instrument as E_task, so the two are directly "
    "comparable; E_LLM is not (it is estimated remote datacenter energy)."
)


def _wh(j: float) -> float:
    return j / 3600.0


def _iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class LocalCoordination:
    """Locally-measured energy of coordination: session window minus task."""
    joules: float
    basis: str                       # "net of idle baseline" | "gross"
    session_joules: float
    task_joules: float
    notes: list[str] = field(default_factory=list)


def local_coordination(sp: SessionPowerResult,
                       task: TaskRunResult) -> LocalCoordination | None:
    """Subtract the nested task run from the session-window measurement.

    Uses net-of-baseline energy when both sides carry a baseline (the idle
    term must be removed from both windows or not at all); otherwise gross.
    Returns None when the session's power sampling failed — those figures are
    not a small error, they are meaningless, so there is nothing to report.
    """
    if not sp.power_ok:
        return None
    notes: list[str] = []
    if sp.net_joules is not None and task.net_joules is not None:
        session_j, task_j, basis = sp.net_joules, task.net_joules, "net of idle baseline"
    else:
        session_j, task_j, basis = sp.gross_joules, task.gross_joules, "gross"
        if sp.net_joules is not None or task.net_joules is not None:
            notes.append("only one of the two runs had a baseline, so gross "
                         "energy is used for both")

    if sp.backend != task.backend:
        notes.append(f"session measured with {sp.backend}, task with "
                     f"{task.backend} — not directly comparable")

    t_start, t_end = _iso(sp.started_at), _iso(sp.ended_at)
    task_start, task_end = _iso(task.started_at), _iso(task.ended_at)
    if t_start and t_end and task_start and task_end:
        if not (t_start <= task_start and task_end <= t_end):
            notes.append("the task run does not lie inside the measured "
                         "session window — subtracting it is not meaningful")
    else:
        notes.append("task run window unknown (older result), so its nesting "
                     "inside the session window could not be verified")

    joules = session_j - task_j
    if joules < 0:
        notes.append("negative after subtraction — the task run appears to "
                     "account for more energy than the whole session window")
    return LocalCoordination(joules=joules, basis=basis, session_joules=session_j,
                             task_joules=task_j, notes=notes)


def phase_energy_j(p) -> float:
    """Net if a baseline was applied, else gross — matching Trial.task_energy_j."""
    return p.net_joules if p.net_joules is not None else p.gross_joules


def compilation_leak(task: TaskRunResult) -> str | None:
    """Warn when compiler output is spread across a phase split.

    A compile/run split is only meaningful if the compiling finished in the
    compile phase. Several phases writing build artifacts means energy that
    belongs to one bucket was billed to another.
    """
    orphaned = task.outputs.get("unattributed_build_artifacts", 0)
    if orphaned:
        # Would otherwise read as a clean split: unattributed compiler output
        # looks like "nothing compiled in this phase".
        return (f"{orphaned} build artifacts fall outside every phase window, "
                "so the split could not be verified — most likely the "
                "container clock has drifted from the host's")
    building = [p.name for p in task.phases if p.build_artifacts_written > 0]
    if len(building) < 2:
        return None
    return (f"compiler output was written in {len(building)} phases "
            f"({', '.join(building)}) — the compile/run split did not hold, so "
            "these phase energies are not a clean separation")


@dataclass
class Trial:
    label: str
    task: TaskRunResult
    session: SessionEnergyResult
    session_power: SessionPowerResult | None = None

    def multiphase(self) -> bool:
        return len(self.task.phases) > 1

    def models(self) -> str:
        return ", ".join(m.model for m in self.session.usage.per_model)

    def local_coordination(self) -> LocalCoordination | None:
        if self.session_power is None:
            return None
        return local_coordination(self.session_power, self.task)

    def power_failure_note(self) -> str | None:
        """Why the local terms are absent despite a session-power result."""
        sp = self.session_power
        if sp is None or sp.power_ok:
            return None
        return ("session power sampling failed, so E_coord,local is omitted; "
                + "; ".join(sp.notes))

    def total_coordination_band(self) -> EnergyBand | None:
        """E_LLM (estimated remote) + E_coord,local (measured here)."""
        lc = self.local_coordination()
        if lc is None:
            return None
        b = self.session.total_band
        return EnergyBand(b.low_j + lc.joules, b.central_j + lc.joules,
                          b.high_j + lc.joules)

    def total_tokens(self) -> int:
        return sum(m.total_tokens() for m in self.session.usage.per_model)

    def output_tokens(self) -> int:
        return sum(m.output_tokens for m in self.session.usage.per_model)

    def task_energy_j(self) -> float:
        """Net if a baseline was applied, else gross."""
        return self.task.net_joules if self.task.net_joules is not None \
            else self.task.gross_joules

    def ratio_band(self) -> tuple[float, float, float]:
        e = self.task_energy_j()
        b = self.session.total_band
        if e <= 0:
            return (float("nan"),) * 3
        return (b.low_j / e, b.central_j / e, b.high_j / e)

    def lhe_fingerprint(self) -> tuple[str, int, str] | None:
        """(path, nevents, sha256) of the first recorded LHE output."""
        out = self.task.outputs
        for key, val in out.items():
            if key.endswith("_events_sha256"):
                base = key[: -len("_events_sha256")]
                return (out.get(base, ""), out.get(f"{base}_nevents", -1), val)
        return None


@dataclass
class EventIdentity:
    checked: bool
    identical: bool | None = None
    numerically_equal: bool | None = None
    detail: str = ""


def check_event_identity(trials: list[Trial], rtol: float = 1e-9) -> EventIdentity:
    fps = [t.lhe_fingerprint() for t in trials]
    if any(fp is None for fp in fps) or len(fps) < 2:
        return EventIdentity(checked=False,
                             detail="not all trials recorded an LHE output")
    counts = {fp[1] for fp in fps}
    hashes = {fp[2] for fp in fps}
    if len(hashes) == 1 and len(counts) == 1:
        return EventIdentity(checked=True, identical=True,
                             detail=f"{fps[0][1]} events, identical content hash")
    paths = [Path(fp[0]) for fp in fps]
    if all(p.exists() for p in paths):
        comp = compare_lhe(paths, rtol=rtol)
        return EventIdentity(
            checked=True, identical=comp.identical,
            numerically_equal=comp.numerically_equal,
            detail=comp.first_difference or "; ".join(comp.notes))
    return EventIdentity(
        checked=True, identical=False,
        detail=f"content hashes differ ({len(hashes)} distinct); original LHE "
               "files unavailable for numeric re-comparison")


# --- markdown -------------------------------------------------------------

def _trial_rows(t: Trial) -> list[tuple[str, str]]:
    task, sess = t.task, t.session
    b = sess.total_band
    rows = [
        ("Task", f"{task.task_name} ({task.image}, {task.image_arch}"
                 f"{', EMULATED' if task.emulated else ''})"),
        ("Task wall time", f"{task.wall_time_s:.1f} s"),
        ("Task energy (gross)", f"{task.gross_joules:.1f} J ({_wh(task.gross_joules):.3f} Wh)"),
    ]
    if task.net_joules is not None:
        rows.append(("Task energy (net of idle)",
                     f"{task.net_joules:.1f} J ({_wh(task.net_joules):.3f} Wh), "
                     f"baseline {task.baseline_mean_w:.2f} W"))
    if task.container_cpu_seconds is not None:
        rows.append(("Container CPU time", f"{task.container_cpu_seconds:.1f} s"))
    rows += [
        ("Measurement backend", task.backend +
         (" (estimate only)" if task.backend == "tdp-model" else "")),
        ("LLM model(s)", t.models()),
        ("LLM tokens (in/out/cache-create/cache-read)",
         " / ".join(str(x) for x in [
             sum(m.input_tokens for m in sess.usage.per_model),
             sum(m.output_tokens for m in sess.usage.per_model),
             sum(m.cache_creation_tokens for m in sess.usage.per_model),
             sum(m.cache_read_tokens for m in sess.usage.per_model)])),
        ("LLM energy (low/central/high)",
         f"{b.low_j:.0f} / {b.central_j:.0f} / {b.high_j:.0f} J "
         f"({_wh(b.low_j):.3f} / {_wh(b.central_j):.3f} / {_wh(b.high_j):.3f} Wh)"),
        ("Session duration", f"{sess.usage.wall_time_s:.0f} s, "
                             f"{sess.usage.assistant_turns} assistant turns"),
    ]
    lo, mid, hi = t.ratio_band()
    rows.append(("E_LLM / E_task", f"{lo:.2f} / {mid:.2f} / {hi:.2f}"))

    lc = t.local_coordination()
    if lc is not None and t.session_power is not None:
        sp = t.session_power
        tb = t.total_coordination_band()
        rows += [
            ("Session wall time", f"{sp.wall_time_s:.0f} s "
                                  f"(mean {sp.mean_power_w:.2f} W)"),
            (f"Session energy ({lc.basis})",
             f"{lc.session_joules:.1f} J ({_wh(lc.session_joules):.3f} Wh)"),
            ("E_coord,local (session − task, measured)",
             f"{lc.joules:.1f} J ({_wh(lc.joules):.3f} Wh)"),
            ("E_coord,total = E_LLM + E_coord,local",
             f"{tb.low_j:.0f} / {tb.central_j:.0f} / {tb.high_j:.0f} J "
             f"({_wh(tb.low_j):.3f} / {_wh(tb.central_j):.3f} / "
             f"{_wh(tb.high_j):.3f} Wh)"),
        ]
        e = t.task_energy_j()
        if e > 0:
            rows.append(("E_coord,total / E_task",
                         f"{tb.low_j / e:.2f} / {tb.central_j / e:.2f} / "
                         f"{tb.high_j / e:.2f}"))
    return rows


def phase_table(task: TaskRunResult) -> list[list[str]]:
    """Rows of (phase, wall, energy, share, mean W, compiler output)."""
    total = sum(phase_energy_j(p) for p in task.phases)
    rows = []
    for p in task.phases:
        e = phase_energy_j(p)
        rows.append([
            p.name,
            f"{p.wall_time_s:.1f} s",
            f"{e:.1f} J ({_wh(e):.3f} Wh)",
            f"{100.0 * e / total:.1f}%" if total > 0 else "n/a",
            f"{p.mean_power_w:.2f} W",
            str(p.build_artifacts_written),
        ])
    return rows


PHASE_HEADERS = ["Phase", "Wall", "Energy", "Share", "Mean power",
                 "Build artifacts"]


def render_markdown(trial: Trial) -> str:
    lines = [f"# llm-energy report: {trial.task.task_name}", ""]
    lines.append(f"Machine: {trial.task.machine.chip or trial.task.machine.hostname} "
                 f"({trial.task.machine.platform}) — {trial.task.created_at}")
    lines += ["", "| Quantity | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in _trial_rows(trial)]
    if trial.task.outputs.get("lhe_file_nevents"):
        lines.append(f"| LHE events | {trial.task.outputs['lhe_file_nevents']} |")

    if trial.multiphase():
        lines += ["", "## Task phases", "",
                  "| " + " | ".join(PHASE_HEADERS) + " |",
                  "|" + "---|" * len(PHASE_HEADERS)]
        lines += ["| " + " | ".join(r) + " |" for r in phase_table(trial.task)]
        basis = ("net of idle baseline"
                 if trial.task.net_joules is not None else "gross")
        lines += ["", f"Phase energies are {basis} and sum to the task total. "
                      "*Build artifacts* counts compiler output (`.o`, `.a`, "
                      "`.so`, `.mod`) written during each phase — the check "
                      "that the split held."]
        leak = compilation_leak(trial.task)
        if leak:
            lines += ["", f"**Warning:** {leak}"]
    lines += ["", "## Caveats", ""]
    lines += [f"- {c}" for c in CAVEATS]
    lc = trial.local_coordination()
    if lc is not None:
        lines.append(f"- {LOCAL_COORD_CAVEAT}")
        lines += [f"- {n}" for n in lc.notes]
    fail = trial.power_failure_note()
    if fail:
        lines.append(f"- {fail}")
    for n in trial.session.notes:
        lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def _local_cell(t: Trial) -> str:
    lc = t.local_coordination()
    return f"{lc.joules:.1f}" if lc else "n/a"


def _total_central_cell(t: Trial) -> str:
    band = t.total_coordination_band()
    return f"{band.central_j:.0f}" if band else "n/a"


def render_comparison_markdown(trials: list[Trial], identity: EventIdentity,
                               rtol: float) -> str:
    lines = ["# llm-energy model comparison", ""]
    header = ["Quantity"] + [t.label for t in trials]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    def row(name, fn):
        lines.append("| " + " | ".join([name] + [fn(t) for t in trials]) + " |")

    row("LLM model(s)", lambda t: t.models())
    row("Total tokens", lambda t: f"{t.total_tokens():,}")
    row("Output tokens", lambda t: f"{t.output_tokens():,}")
    row("Assistant turns", lambda t: str(t.session.usage.assistant_turns))
    row("Session duration (s)", lambda t: f"{t.session.usage.wall_time_s:.0f}")
    row("E_LLM central (J)", lambda t: f"{t.session.total_band.central_j:.0f}")
    row("E_LLM band (J)", lambda t: f"{t.session.total_band.low_j:.0f}-"
                                    f"{t.session.total_band.high_j:.0f}")
    row("Task wall time (s)", lambda t: f"{t.task.wall_time_s:.1f}")
    row("E_task (J)", lambda t: f"{t.task_energy_j():.1f}")
    row("E_LLM/E_task (central)", lambda t: f"{t.ratio_band()[1]:.2f}")
    if any(t.session_power for t in trials):
        row("E_coord,local (J)", _local_cell)
        row("E_coord,total central (J)", _total_central_cell)
    row("LHE events", lambda t: str((t.lhe_fingerprint() or ("", "n/a", ""))[1]))
    row("Event hash (short)",
        lambda t: (t.lhe_fingerprint() or ("", 0, "n/a"))[2][:12])

    lines += ["", "## Output event identity", ""]
    if not identity.checked:
        lines.append(f"Not checked: {identity.detail}")
    elif identity.identical:
        lines.append(f"**IDENTICAL** — {identity.detail}")
    elif identity.numerically_equal:
        lines.append(f"**Numerically equal** within rtol={rtol} (hashes differ; "
                     f"expected across architectures). {identity.detail}")
    else:
        lines.append(f"**DIFFERENT** — {identity.detail}")
    lines += ["", "## Caveats", ""]
    lines += [f"- {c}" for c in CAVEATS]
    return "\n".join(lines) + "\n"


# --- terminal -------------------------------------------------------------

def render_terminal(trial: Trial) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title=f"llm-energy: {trial.task.task_name}")
    table.add_column("Quantity")
    table.add_column("Value")
    for k, v in _trial_rows(trial):
        table.add_row(k, v)
    console.print(table)

    if trial.multiphase():
        ptable = Table(title="Task phases")
        for col in PHASE_HEADERS:
            ptable.add_column(col)
        for r in phase_table(trial.task):
            ptable.add_row(*r)
        console.print(ptable)
        leak = compilation_leak(trial.task)
        if leak:
            console.print(f"[yellow]warning: {leak}[/yellow]")

    lc = trial.local_coordination()
    fail = trial.power_failure_note()
    if fail:
        console.print(f"[yellow]warning: {fail}[/yellow]")
    for n in (lc.notes if lc else []):
        console.print(f"[yellow]note: {n}[/yellow]")
    console.print("[dim]Caveats:[/dim]")
    for c in CAVEATS + ([LOCAL_COORD_CAVEAT] if lc else []):
        console.print(f"[dim] - {c}[/dim]")


def render_comparison_terminal(trials: list[Trial], identity: EventIdentity,
                               rtol: float) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="llm-energy model comparison")
    table.add_column("Quantity")
    for t in trials:
        table.add_column(t.label)

    def row(name, fn):
        table.add_row(name, *[fn(t) for t in trials])

    row("LLM model(s)", lambda t: t.models())
    row("Total tokens", lambda t: f"{t.total_tokens():,}")
    row("E_LLM central (J)", lambda t: f"{t.session.total_band.central_j:.0f}")
    row("E_LLM band (J)", lambda t: f"{t.session.total_band.low_j:.0f}-"
                                    f"{t.session.total_band.high_j:.0f}")
    row("Task wall (s)", lambda t: f"{t.task.wall_time_s:.1f}")
    row("E_task (J)", lambda t: f"{t.task_energy_j():.1f}")
    row("E_LLM/E_task", lambda t: f"{t.ratio_band()[1]:.2f}")
    if any(t.session_power for t in trials):
        row("E_coord,local (J)", _local_cell)
        row("E_coord,total (J)", _total_central_cell)
    row("Event hash", lambda t: (t.lhe_fingerprint() or ("", 0, "n/a"))[2][:12])
    console.print(table)

    if not identity.checked:
        console.print(f"[yellow]Event identity not checked: {identity.detail}[/yellow]")
    elif identity.identical:
        console.print(f"[green]Output events IDENTICAL[/green] — {identity.detail}")
    elif identity.numerically_equal:
        console.print(f"[green]Output events numerically equal[/green] "
                      f"(rtol={rtol}) — hashes differ, expected cross-arch")
    else:
        console.print(f"[red]Output events DIFFER[/red] — {identity.detail}")


# --- chart ----------------------------------------------------------------

def render_chart(trials: list[Trial], path: Path) -> None:
    """Log-scale bar chart: E_task, E_LLM (central, low-high errorbar), and
    E_coord,local where it was measured."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [t.label for t in trials]
    x = range(len(trials))
    task_e = [t.task_energy_j() for t in trials]
    llm_c = [t.session.total_band.central_j for t in trials]
    llm_lo = [t.session.total_band.central_j - t.session.total_band.low_j
              for t in trials]
    llm_hi = [t.session.total_band.high_j - t.session.total_band.central_j
              for t in trials]
    locals_ = [(t.local_coordination().joules if t.local_coordination() else 0.0)
               for t in trials]
    # a log axis cannot render a bar at or below zero
    show_local = any(v > 0 for v in locals_)

    fig, ax = plt.subplots(figsize=(1.8 + 2.2 * len(trials), 4.5))
    w = 0.26 if show_local else 0.38
    off = w if show_local else w / 2
    ax.bar([i - off for i in x], task_e, w, label="E_task (measured)")
    ax.bar([i + (0.0 if show_local else off) for i in x], llm_c, w,
           yerr=[llm_lo, llm_hi], capsize=5, label="E_LLM (estimated band)")
    if show_local:
        ax.bar([i + off for i in x], locals_, w,
               label="E_coord,local (measured)")
    ax.set_yscale("log")
    ax.set_ylabel("Energy (J)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.legend()
    ax.set_title("LLM coordination energy vs task energy")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# --- open tasks --------------------------------------------------------------
#
# For an open task the harness launches nothing, so there is no TaskRunResult.
# E_task is the energy observed inside the containers the agent started, and
# the coordination cost is what is left of the session.

OPEN_CAVEATS = [
    "E_compute is the energy inside containers the agent started, observed via "
    "the Docker event stream — the harness did not launch them, so the "
    "boundary is the container's lifetime, not a task definition.",
    "Work the agent ran outside a container counts as coordination, not "
    "compute. A generator invoked directly on the host would land in the wrong "
    "bucket.",
    "E_LLM is estimated from tokens and literature coefficients including PUE; "
    "the two measured terms are local package energy. Only the measured terms "
    "are comparable with each other.",
]


def open_rows(sp: SessionPowerResult, se: SessionEnergyResult,
              graded=None) -> list[tuple[str, str]]:
    b = se.total_band
    rows = [("Session wall time", f"{sp.wall_time_s:.0f} s "
                                  f"(mean {sp.mean_power_w:.2f} W)")]
    if not sp.power_ok:
        rows.append(("Session energy", "unusable — power sampling failed"))
    else:
        rows.append(("Session energy (measured, gross)",
                     f"{sp.gross_joules:.1f} J ({_wh(sp.gross_joules):.3f} Wh)"))
        if sp.net_joules is not None:
            if sp.baseline_usable():
                rows.append(("Session energy (net of idle)",
                             f"{sp.net_joules:.1f} J "
                             f"({_wh(sp.net_joules):.3f} Wh)"))
            else:
                # Printing "-162.9 J" here invites someone to use it. The
                # baseline is the broken part, not the session.
                rows.append(("Session energy (net of idle)",
                             f"unusable — idle baseline "
                             f"{sp.baseline_mean_w:.2f} W exceeds the "
                             f"session's {sp.mean_power_w:.2f} W"))
        # Both terms net of idle where a baseline exists: an agent session is
        # mostly network wait, so gross is dominated by draw the machine would
        # have had anyway and would overstate the agent's cost several-fold.
        basis = sp.energy_basis()
        compute = sp.container_net_joules()
        outside = sp.outside_container_joules()
        if compute is not None:
            rows.append((f"E_compute (in containers, {basis})",
                         f"{compute:.1f} J ({_wh(compute):.3f} Wh) over "
                         f"{sp.container_wall_s:.0f} s, "
                         f"{len(sp.containers)} container(s)"))
        if outside is not None:
            total = (compute or 0.0) + outside
            # A share is only meaningful when both parts are positive and sum
            # to the whole. "nan%" was the old way of saying they did not.
            share = (f", {100.0 * outside / total:.1f}% of the session"
                     if total > 0 and outside >= 0 else "")
            rows.append((f"E_coord,local (outside containers, {basis})",
                         f"{outside:.1f} J ({_wh(outside):.3f} Wh){share}"))
    rows += [
        ("LLM model(s)", ", ".join(m.model for m in se.usage.per_model)),
        ("LLM tokens (in/out/cache-create/cache-read)",
         " / ".join(str(x) for x in [
             sum(m.input_tokens for m in se.usage.per_model),
             sum(m.output_tokens for m in se.usage.per_model),
             sum(m.cache_creation_tokens for m in se.usage.per_model),
             sum(m.cache_read_tokens for m in se.usage.per_model)])),
        ("LLM turns", f"{se.usage.assistant_turns} assistant, "
                      f"{se.usage.sidechain_turns} sidechain"),
        ("E_LLM (low/central/high, estimated)",
         f"{b.low_j:.0f} / {b.central_j:.0f} / {b.high_j:.0f} J"),
    ]
    if graded is None:
        rows.append(("Deliverables", "not checked"))
        return rows

    if not graded.ok and any(c.still_running for c in sp.containers):
        # "the agent got it wrong" and "the agent ran out of session while the
        # generator was still going" are different results, and the second one
        # says nothing about the model. Do not let the verdict imply the first.
        rows.append(("Deliverables",
                     "NOT MET — but a container was still running at the end, "
                     "so the run was cut off rather than finished wrong"))
    else:
        rows.append(("Deliverables", "MET" if graded.ok else "NOT MET"))
    ev = graded.events
    if ev is None:
        rows.append(("  events", "missing"))
    else:
        rows.append(("  events", f"{ev.n_events} events, "
                                 + ("ok" if ev.ok else "failed")))
        for c in ev.failures:
            rows.append((f"    {c.name}", c.detail))
    peak = graded.peak
    if peak is not None:
        if not peak.exists:
            rows.append(("  top mass peak", "no histogram"))
        else:
            where = (f"{peak.peak_gev:.1f} GeV" if peak.peak_gev is not None
                     else "no peak found")
            width = f", FWHM ~{peak.fwhm_gev:.0f} GeV" if peak.fwhm_gev else ""
            rows.append(("  top mass peak",
                         f"{where}{width}, {peak.entries} entries — "
                         + ("ok" if peak.ok else "failed")))
            for c in peak.failures:
                rows.append((f"    {c.name}", c.detail))
    if graded.plot_missing:
        rows.append(("  plot", "missing"))
    return rows


def container_rows(sp: SessionPowerResult) -> list[list[str]]:
    return [[c.container_id, c.image or "?", f"{c.wall_time_s:.0f} s",
             f"{c.gross_joules:.1f} J", f"{c.mean_power_w:.2f} W"]
            for c in sp.containers]


CONTAINER_HEADERS = ["Container", "Image", "Wall", "Energy", "Mean power"]


def render_open_terminal(sp: SessionPowerResult, se: SessionEnergyResult,
                         graded=None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="llm-energy: open task")
    table.add_column("Quantity")
    table.add_column("Value")
    for k, v in open_rows(sp, se, graded):
        table.add_row(k, v)
    console.print(table)

    if sp.containers:
        ct = Table(title="Containers the agent ran")
        for col in CONTAINER_HEADERS:
            ct.add_column(col)
        for r in container_rows(sp):
            ct.add_row(*r)
        console.print(ct)

    for n in sp.notes:
        console.print(f"[yellow]note: {n}[/yellow]")
    console.print("[dim]Caveats:[/dim]")
    for c in OPEN_CAVEATS:
        console.print(f"[dim] - {c}[/dim]")


def render_open_markdown(sp: SessionPowerResult, se: SessionEnergyResult,
                         graded=None) -> str:
    lines = ["# llm-energy report: open task", ""]
    lines.append(f"Machine: {sp.machine.chip or sp.machine.hostname} "
                 f"({sp.machine.platform}) — {sp.created_at}")
    lines += ["", "| Quantity | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in open_rows(sp, se, graded)]
    if sp.containers:
        lines += ["", "## Containers the agent ran", "",
                  "| " + " | ".join(CONTAINER_HEADERS) + " |",
                  "|" + "---|" * len(CONTAINER_HEADERS)]
        lines += ["| " + " | ".join(r) + " |" for r in container_rows(sp)]
    lines += ["", "## Caveats", ""] + [f"- {c}" for c in OPEN_CAVEATS]
    lines += [f"- {n}" for n in sp.notes]
    return "\n".join(lines) + "\n"
