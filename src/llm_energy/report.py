"""Comparison reports: E_task (measured) vs E_LLM (estimated band).

Single-trial report: one task run + one coordination session.
Multi-trial comparison: several trials (e.g. different LLM models
coordinating the same task) side by side, including whether the output
events are physically identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from llm_energy.lhe import compare_lhe
from llm_energy.schemas import SessionEnergyResult, TaskRunResult

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


def _wh(j: float) -> float:
    return j / 3600.0


@dataclass
class Trial:
    label: str
    task: TaskRunResult
    session: SessionEnergyResult

    def models(self) -> str:
        return ", ".join(m.model for m in self.session.usage.per_model)

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
    return rows


def render_markdown(trial: Trial) -> str:
    lines = [f"# llm-energy report: {trial.task.task_name}", ""]
    lines.append(f"Machine: {trial.task.machine.chip or trial.task.machine.hostname} "
                 f"({trial.task.machine.platform}) — {trial.task.created_at}")
    lines += ["", "| Quantity | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in _trial_rows(trial)]
    if trial.task.outputs.get("lhe_file_nevents"):
        lines.append(f"| LHE events | {trial.task.outputs['lhe_file_nevents']} |")
    lines += ["", "## Caveats", ""]
    lines += [f"- {c}" for c in CAVEATS]
    for n in trial.session.notes:
        lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


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
    console.print("[dim]Caveats:[/dim]")
    for c in CAVEATS:
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
    """Log-scale bar chart: E_task and E_LLM (central, low-high errorbar)."""
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

    fig, ax = plt.subplots(figsize=(1.8 + 2.2 * len(trials), 4.5))
    w = 0.38
    ax.bar([i - w / 2 for i in x], task_e, w, label="E_task (measured)")
    ax.bar([i + w / 2 for i in x], llm_c, w, yerr=[llm_lo, llm_hi], capsize=5,
           label="E_LLM (estimated band)")
    ax.set_yscale("log")
    ax.set_ylabel("Energy (J)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.legend()
    ax.set_title("LLM coordination energy vs task energy")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
