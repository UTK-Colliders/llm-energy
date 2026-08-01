"""Energy budget: where a run's energy went, and in what proportion.

The pipeline produces energy figures for several portions of the same job —
compilation, event generation, the agent working locally, the inference behind
it — recorded in different artifacts and on two different footings. This
assembles them into one budget so their relative sizes are readable at a
glance.

The footing distinction is load-bearing and is never averaged away. Energy
measured on this machine's SoC and energy estimated for remote inference are
kept in separate totals: shares are computed within the measured group, where
the terms are commensurable, and the estimated band is expressed as a *ratio*
to that group rather than folded into it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from llm_energy.report import local_coordination, phase_energy_j
from llm_energy.schemas import (EnergyBand, SessionEnergyResult,
                                SessionPowerResult, TaskRunResult)

MEASURED = "measured"
ESTIMATED = "estimated"


@dataclass
class Component:
    """One portion of the job's energy."""
    name: str
    basis: str                       # MEASURED | ESTIMATED
    joules: float = 0.0              # the point value, or the band's central
    band: EnergyBand | None = None   # set for estimated components
    detail: str = ""


@dataclass
class Budget:
    label: str
    components: list[Component] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def measured(self) -> list[Component]:
        return [c for c in self.components if c.basis == MEASURED]

    @property
    def estimated(self) -> list[Component]:
        return [c for c in self.components if c.basis == ESTIMATED]

    @property
    def measured_total_j(self) -> float:
        return sum(c.joules for c in self.measured)

    def share(self, c: Component) -> float | None:
        """Fraction of the measured total, for measured components only.

        None for estimated ones: a share of a total it is not part of would
        invite exactly the comparison this module is built to prevent.
        """
        total = self.measured_total_j
        if c.basis != MEASURED or total <= 0:
            return None
        return c.joules / total

    def ratio_to_measured(self, c: Component) -> tuple[float, float, float] | None:
        """An estimated component as a multiple of the measured total."""
        total = self.measured_total_j
        if c.band is None or total <= 0:
            return None
        return (c.band.low_j / total, c.band.central_j / total,
                c.band.high_j / total)


def build_budget(label: str,
                 task: TaskRunResult | None = None,
                 session_power: SessionPowerResult | None = None,
                 session: SessionEnergyResult | None = None) -> Budget:
    """Assemble whatever artifacts are available into one budget.

    Handles both shapes the harness produces: a pinned task, whose container
    the harness launched and whose phases it timed; and an open task, where it
    only observed the containers the agent chose to start.
    """
    budget = Budget(label=label)
    usable_power = session_power is not None and session_power.power_ok
    if session_power is not None and not session_power.power_ok:
        budget.notes.append(
            "session power sampling failed, so the local terms are omitted")

    if task is not None:
        # A pinned task: the harness ran it, so its phases are known exactly.
        if len(task.phases) > 1:
            for p in task.phases:
                budget.components.append(Component(
                    name=p.name, basis=MEASURED, joules=phase_energy_j(p),
                    detail=p.description or f"{p.wall_time_s:.0f} s"))
        else:
            e = task.net_joules if task.net_joules is not None else task.gross_joules
            budget.components.append(Component(
                name="task", basis=MEASURED, joules=e,
                detail=f"{task.wall_time_s:.0f} s"))
        if usable_power:
            lc = local_coordination(session_power, task)
            if lc is not None:
                budget.components.append(Component(
                    name="coordination (local)", basis=MEASURED,
                    joules=lc.joules, detail=lc.basis))
                budget.notes.extend(lc.notes)

    elif usable_power:
        # An open task: nothing was launched by the harness, so compute is
        # whatever ran inside containers it observed.
        if session_power.container_joules is None:
            budget.components.append(Component(
                name="session (local)", basis=MEASURED,
                joules=session_power.gross_joules,
                detail="not split: no container observation"))
            budget.notes.append(
                "compute and coordination could not be separated for this run")
        else:
            compute = session_power.container_net_joules()
            outside = session_power.outside_container_joules()
            note = session_power.energy_basis()
            budget.components.append(Component(
                name="compute (containers)", basis=MEASURED,
                joules=compute if compute is not None else 0.0,
                detail=f"{len(session_power.containers)} container(s), "
                       f"{session_power.container_wall_s:.0f} s, {note}"))
            budget.components.append(Component(
                name="coordination (local)", basis=MEASURED,
                joules=outside if outside is not None else 0.0,
                detail=f"outside any container, {note}"))

    if session is not None:
        b = session.total_band
        tokens = sum(m.total_tokens() for m in session.usage.per_model)
        budget.components.append(Component(
            name="LLM inference", basis=ESTIMATED, joules=b.central_j, band=b,
            detail=f"{tokens:,} tokens, PUE {session.pue}"))

    if not budget.components:
        budget.notes.append("no usable artifacts — nothing to break down")
    return budget


def _wh(j: float) -> float:
    return j / 3600.0


def budget_rows(budget: Budget) -> list[list[str]]:
    """Table rows: component, basis, energy, share of measured, detail."""
    rows = []
    for c in budget.measured:
        share = budget.share(c)
        rows.append([c.name, "measured",
                     f"{c.joules:.0f} J ({_wh(c.joules):.3f} Wh)",
                     f"{100 * share:.1f}%" if share is not None else "—",
                     c.detail])
    total = budget.measured_total_j
    if budget.measured:
        rows.append(["total measured", "", f"{total:.0f} J ({_wh(total):.3f} Wh)",
                     "100%", ""])
    for c in budget.estimated:
        ratio = budget.ratio_to_measured(c)
        b = c.band
        rows.append([
            c.name, "estimated",
            f"{b.low_j:.0f} / {b.central_j:.0f} / {b.high_j:.0f} J"
            if b else f"{c.joules:.0f} J",
            f"{ratio[0]:.1f}x / {ratio[1]:.1f}x / {ratio[2]:.1f}x of measured"
            if ratio else "—",
            c.detail])
    return rows


BUDGET_HEADERS = ["Portion", "Basis", "Energy", "Share", "Detail"]


def render_budget_markdown(budget: Budget) -> str:
    lines = [f"# Energy budget: {budget.label}", "",
             "| " + " | ".join(BUDGET_HEADERS) + " |",
             "|" + "---|" * len(BUDGET_HEADERS)]
    lines += ["| " + " | ".join(r) + " |" for r in budget_rows(budget)]
    lines += ["",
              "Shares are of the measured total only. The estimated term is "
              "given as a multiple of that total rather than a share of it: "
              "local SoC package energy and estimated remote datacenter energy "
              "are different quantities and do not sum.", ""]
    if budget.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in budget.notes]
    return "\n".join(lines) + "\n"


def render_budget_terminal(budget: Budget) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title=f"Energy budget: {budget.label}")
    for col in BUDGET_HEADERS:
        table.add_column(col)
    for r in budget_rows(budget):
        table.add_row(*r)
    console.print(table)
    console.print("[dim]Shares are of the measured total. The estimated term is "
                  "a multiple of it, not a share — the two are different "
                  "quantities and do not sum.[/dim]")
    for n in budget.notes:
        console.print(f"[yellow]note: {n}[/yellow]")


# --- figure ------------------------------------------------------------------

# shipped inside the package so an installed wheel styles figures identically
STYLE = Path(__file__).resolve().parent / "assets" / "tufte.mplstyle"


def render_budget_chart(budget: Budget, path: Path) -> Path:
    """One-panel PDF: each portion's energy on a log axis, directly labelled.

    A dot plot, not bars. The portions span two or more orders of magnitude,
    which forces a log axis — and a log axis has no zero, so a bar's length
    would encode nothing while still reading as a proportion. The marker
    position carries the value; a faint leader line only helps the eye travel
    from the label.

    Measured portions get a filled dot, the estimated one an open dot with its
    low-high range drawn through it: a different mark, so an estimate cannot
    be mistaken for a measurement at a glance.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import NullLocator

    if STYLE.exists():
        plt.style.use(str(STYLE))

    items = budget.measured + budget.estimated
    if not items:
        raise ValueError("nothing to plot")

    y = list(range(len(items)))[::-1]        # first component at the top
    fig, ax = plt.subplots(figsize=(5.4, 0.5 * len(items) + 1.5))
    ink, faint = "#333333", "#aaaaaa"

    values = [(c.band.high_j if c.band else c.joules) for c in items]
    lows = [(c.band.low_j if c.band else c.joules) for c in items]
    left = min(v for v in lows if v > 0) / 3 if any(v > 0 for v in lows) else 1.0

    for yi, c in zip(y, items):
        if c.basis == MEASURED:
            ax.plot([left, c.joules], [yi, yi], color=faint, linewidth=0.5,
                    zorder=1)
            ax.plot([c.joules], [yi], marker="o", markersize=5, color=ink,
                    zorder=3)
            share = budget.share(c)
            label = f"  {c.joules:,.0f} J" + (f"   {100 * share:.0f}%"
                                              if share is not None else "")
            ax.text(c.joules, yi, label, va="center", ha="left", fontsize=8.5,
                    color=ink)
        else:
            b = c.band
            lo, hi, mid = ((b.low_j, b.high_j, b.central_j) if b
                           else (c.joules, c.joules, c.joules))
            ax.plot([left, lo], [yi, yi], color=faint, linewidth=0.5, zorder=1)
            ax.plot([lo, hi], [yi, yi], color=ink, linewidth=1.0,
                    solid_capstyle="butt", zorder=2)
            for x in (lo, hi):
                ax.plot([x, x], [yi - 0.14, yi + 0.14], color=ink,
                        linewidth=1.0, zorder=2)
            ax.plot([mid], [yi], marker="o", markersize=5,
                    markerfacecolor="white", markeredgecolor=ink,
                    markeredgewidth=1.1, zorder=3)
            ratio = budget.ratio_to_measured(c)
            label = f"  {mid:,.0f} J" + (f"   {ratio[1]:.0f}x measured"
                                         if ratio else "")
            ax.text(hi, yi, label, va="center", ha="left", fontsize=8.5,
                    color=ink)
            ax.text(mid, yi - 0.34, "estimated: low-central-high", va="top",
                    ha="center", fontsize=7.5, color=faint)

    ax.set_yticks(y)
    ax.set_yticklabels([c.name for c in items])
    ax.set_xscale("log")
    ax.set_xlabel("Energy (J)")
    ax.tick_params(axis="y", length=0)
    ax.xaxis.set_minor_locator(NullLocator())
    ax.set_ylim(-0.8, len(items) - 0.3)
    ax.set_xlim(left=left, right=max(values) * 14)

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    # Tufte range frame: the spine spans exactly the data, and no tick is
    # drawn past it — the axis itself then reports the extent, and the label
    # headroom on the right does not read as empty data space.
    lo_data, hi_data = min(lows), max(values)
    ax.spines["bottom"].set_bounds(lo_data, hi_data)
    ax.set_xticks([t for t in ax.get_xticks() if lo_data <= t <= hi_data])

    path = Path(path).with_suffix(".pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="pdf", bbox_inches="tight")
    plt.close(fig)
    return path
