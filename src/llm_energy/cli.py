"""llm-energy CLI."""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from llm_energy import __version__

console = Console()

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = REPO_ROOT / "tasks"
DEFAULT_RESULTS = REPO_ROOT / "results"
DEFAULT_COEFFS = REPO_ROOT / "coefficients" / "default.yaml"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_or_die(loader, path: Path, what: str):
    """Load a result file, turning malformed input into a clean CLI error.

    `results/` is full of superficially similar JSON, so pointing a command at
    the wrong file is a routine mistake and deserves a message, not a
    traceback.
    """
    import json

    try:
        return loader(path)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"{path} is not valid JSON: {e}")
    except (KeyError, TypeError, ValueError) as e:
        raise click.ClickException(f"{path} is not a {what} result: {e}")


def _resolve_baseline(baseline_arg: str, out: Path):
    """(BaselineResult | None, ref) from 'latest' | a path | 'none'."""
    from llm_energy import machine_info
    from llm_energy.baseline import find_latest_baseline
    from llm_energy.schemas import load_baseline

    if baseline_arg == "latest":
        p = find_latest_baseline(out, machine_info.collect(include_docker=False))
        if p is None:
            raise click.ClickException(
                "no baseline for this machine in results/ — run `llm-energy "
                "baseline` first, or pass --baseline none")
        return load_baseline(p), str(p)
    if baseline_arg not in ("none", ""):
        p = Path(baseline_arg)
        return load_baseline(p), str(p)
    return None, None


@click.group()
@click.version_option(version=__version__)
def main():
    """Compare LLM coordination energy vs. HEP task energy."""


# --- doctor -----------------------------------------------------------------

@main.command()
@click.option("--task", "task_name", default="madgraph-ttbar-lhe", show_default=True)
@click.option("--task-dir", type=click.Path(path_type=Path), default=DEFAULT_TASKS,
              show_default=True)
@click.option("--coefficients", type=click.Path(path_type=Path), default=DEFAULT_COEFFS,
              show_default=True)
def doctor(task_name: str, task_dir: Path, coefficients: Path):
    """Check that this machine can measure and run tasks."""
    from llm_energy import docker_util
    from llm_energy.power import get_backend
    from llm_energy.session.energy import load_coefficients

    failures = 0

    def check(ok: bool, label: str, detail: str = ""):
        nonlocal failures
        mark = "[green]ok[/green]" if ok else "[red]FAIL[/red]"
        console.print(f"  {mark}  {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures += 1

    console.print(f"[bold]llm-energy doctor[/bold] (platform: {platform.system()} "
                  f"{platform.machine()})")

    backend = get_backend()
    probe = backend.probe()
    check(probe.ok, f"power backend '{probe.backend}'", "; ".join(probe.messages))
    if not probe.ok and platform.system() == "Darwin":
        console.print("    [dim]sudoers rule (see scripts/sudoers-powermetrics.md):\n"
                      "    <you> ALL=(root) NOPASSWD: /usr/bin/powermetrics[/dim]")

    docker_ok = docker_util.docker_available()
    check(docker_ok, "docker daemon reachable",
          "" if docker_ok else "start Docker Desktop / colima")

    try:
        from llm_energy.config import find_task
        task = find_task(task_name, task_dir)
        check(True, f"task '{task_name}' parses",
              f"default image: {task.image().tag}")
        if docker_ok:
            img = task.image()
            if docker_util.image_exists(img.tag):
                emu = docker_util.is_emulated(img.tag)
                check(not emu, f"image {img.tag} architecture",
                      "native" if not emu else
                      f"{docker_util.image_arch(img.tag)} on {docker_util.host_arch()} "
                      "host — EMULATED, energy will be distorted")
            else:
                console.print(f"  [yellow]--[/yellow]  image {img.tag} not built/pulled "
                              f"yet (run-task will handle it)")
    except (FileNotFoundError, ValueError) as e:
        check(False, f"task '{task_name}'", str(e))

    try:
        coeffs = load_coefficients(coefficients)
        check(True, "coefficients parse",
              f"{coefficients.name}, pue={coeffs.pue}, sha256={coeffs.sha256[:12]}")
    except (OSError, ValueError) as e:
        check(False, "coefficients parse", str(e))

    raise SystemExit(1 if failures else 0)


# --- baseline ---------------------------------------------------------------

@main.command()
@click.option("--duration", default=120.0, show_default=True, help="seconds")
@click.option("--interval-ms", default=1000, show_default=True)
@click.option("--backend", "backend_name", default=None,
              type=click.Choice(["powermetrics", "rapl", "tdp-model"]))
@click.option("--out", type=click.Path(path_type=Path), default=DEFAULT_RESULTS,
              show_default=True)
def baseline(duration: float, interval_ms: int, backend_name: str | None, out: Path):
    """Measure idle power. Quiesce the machine; leave the Docker VM running."""
    from llm_energy.baseline import measure_baseline
    from llm_energy.power import get_backend
    from llm_energy.schemas import write_result

    out.mkdir(parents=True, exist_ok=True)
    backend = get_backend(backend_name)
    if not backend.probe().ok:
        raise click.ClickException(f"backend {backend.name} not usable; run doctor")
    console.print(f"Measuring idle power for {duration:.0f} s with {backend.name} — "
                  "close other applications and step away.")
    result = measure_baseline(backend, duration, interval_ms, out)
    if not result.docker_running:
        console.print("[yellow]warning: docker is not running — the task-time "
                      "baseline should include the idle Docker VM[/yellow]")
    path = write_result(result, out / f"baseline-{_ts()}.json")
    console.print(f"mean {result.mean_w:.2f} W (std {result.std_w:.2f}, "
                  f"n={result.n_samples}) -> {path}")


# --- run-task ---------------------------------------------------------------

@main.command("run-task")
@click.argument("task_name")
@click.option("--task-dir", type=click.Path(path_type=Path), default=DEFAULT_TASKS,
              show_default=True)
@click.option("--baseline", "baseline_arg", default="latest", show_default=True,
              help="baseline JSON path, 'latest', or 'none'")
@click.option("--image-variant", default=None, help="image variant from task.yaml")
@click.option("--interval-ms", default=1000, show_default=True)
@click.option("--backend", "backend_name", default=None,
              type=click.Choice(["powermetrics", "rapl", "tdp-model"]))
@click.option("--out", type=click.Path(path_type=Path), default=DEFAULT_RESULTS,
              show_default=True)
def run_task_cmd(task_name: str, task_dir: Path, baseline_arg: str,
                 image_variant: str | None, interval_ms: int,
                 backend_name: str | None, out: Path):
    """Run a task under energy measurement."""
    from llm_energy.config import find_task
    from llm_energy.power import get_backend
    from llm_energy.schemas import write_result
    from llm_energy.task_runner import TaskRunError, run_task

    out.mkdir(parents=True, exist_ok=True)
    task = find_task(task_name, task_dir)
    base, base_ref = _resolve_baseline(baseline_arg, out)

    backend = get_backend(backend_name)
    if not backend.probe().ok:
        raise click.ClickException(f"backend {backend.name} not usable; run doctor")
    if base is not None and base.backend != backend.name:
        console.print(f"[yellow]warning: baseline was measured with "
                      f"{base.backend}, task with {backend.name}[/yellow]")

    console.print(f"Running task [bold]{task_name}[/bold] "
                  f"(image variant: {image_variant or task.default_image}, "
                  f"backend: {backend.name})")
    try:
        result = run_task(task, backend, out, baseline=base, baseline_ref=base_ref,
                          image_variant=image_variant, interval_ms=interval_ms)
    except TaskRunError as e:
        raise click.ClickException(str(e))

    path = write_result(result, out / f"task-{task_name}-{_ts()}.json")
    if len(result.phases) > 1:
        from llm_energy.report import compilation_leak, phase_energy_j

        total = sum(phase_energy_j(p) for p in result.phases)
        for p in result.phases:
            e = phase_energy_j(p)
            share = f"{100.0 * e / total:.1f}%" if total > 0 else "n/a"
            console.print(f"  {p.name}: {e:.1f} J ({share}) over "
                          f"{p.wall_time_s:.1f} s, mean {p.mean_power_w:.2f} W, "
                          f"{p.build_artifacts_written} build artifacts")
        leak = compilation_leak(result)
        if leak:
            console.print(f"[yellow]warning: {leak}[/yellow]")
    net = f", net {result.net_joules:.1f} J" if result.net_joules is not None else ""
    console.print(f"gross {result.gross_joules:.1f} J{net} over "
                  f"{result.wall_time_s:.1f} s "
                  f"(mean {result.mean_power_w:.2f} W) -> {path}")
    if result.coordinating_session_id:
        console.print(f"[dim]coordinating session: "
                      f"{result.coordinating_session_id[:8]} — pair with "
                      f"`analyze-session --for-task {path}`[/dim]")
    else:
        console.print("[yellow]note: no CLAUDE_CODE_SESSION_ID in the "
                      "environment, so this run is not linked to a "
                      "coordination session[/yellow]")
    if result.emulated:
        console.print("[yellow]warning: image ran under emulation — energy is "
                      "not representative of native execution[/yellow]")


# --- measure-session ---------------------------------------------------------

@main.command("measure-session")
@click.argument("command", nargs=-1)
@click.option("--baseline", "baseline_arg", default="latest", show_default=True,
              help="baseline JSON path, 'latest', or 'none'")
@click.option("--interval-ms", default=1000, show_default=True)
@click.option("--backend", "backend_name", default=None,
              type=click.Choice(["powermetrics", "rapl", "tdp-model"]))
@click.option("--cwd", type=click.Path(path_type=Path), default=None,
              help="working directory for the session [default: current]")
@click.option("--out", type=click.Path(path_type=Path), default=DEFAULT_RESULTS,
              show_default=True)
def measure_session_cmd(command: tuple[str, ...], baseline_arg: str,
                        interval_ms: int, backend_name: str | None,
                        cwd: Path | None, out: Path):
    """Measure package power for a whole coordination session.

    Runs COMMAND (default: `claude`) under power measurement and records the
    energy this machine spent while the agent worked — thinking, reading
    files, running commands — with the measured task run nested inside it.
    Sessions started during the window are linked automatically.

    Example:
      llm-energy measure-session -- claude "do the madgraph-ttbar-lhe task"
    """
    from llm_energy.power import get_backend
    from llm_energy.schemas import write_result
    from llm_energy.session_power import measure_session

    out.mkdir(parents=True, exist_ok=True)
    cmd = list(command) or ["claude"]
    cwd = cwd or Path.cwd()
    base, base_ref = _resolve_baseline(baseline_arg, out)

    backend = get_backend(backend_name)
    if not backend.probe().ok:
        raise click.ClickException(f"backend {backend.name} not usable; run doctor")

    console.print(f"Measuring [bold]{' '.join(cmd)}[/bold] with {backend.name}. "
                  "Everything this machine does until it exits counts as "
                  "coordination.")
    result = measure_session(cmd, backend, out, baseline=base,
                             baseline_ref=base_ref, interval_ms=interval_ms,
                             cwd=cwd)

    # prefix deliberately not "session-": that glob belongs to analyze-session's
    # energy results, and scripts pick those up with `results/session-*.json`
    path = write_result(result, out / f"power-session-{_ts()}.json")
    if result.power_ok:
        net = (f", net {result.net_joules:.1f} J"
               if result.net_joules is not None else "")
        console.print(f"session gross {result.gross_joules:.1f} J{net} over "
                      f"{result.wall_time_s:.0f} s "
                      f"(mean {result.mean_power_w:.2f} W) -> {path}")
    else:
        # never print 0.0 J as though it were a reading
        console.print(f"[red]no usable energy figure for this session[/red] "
                      f"({result.wall_time_s:.0f} s elapsed) -> {path}")
    if result.session_ids:
        console.print("linked session(s): " +
                      ", ".join(s[:8] for s in result.session_ids))
    for n in result.notes:
        console.print(f"[yellow]note: {n}[/yellow]")
    if result.exit_code != 0:
        console.print(f"[yellow]command exited {result.exit_code}[/yellow]")


# --- sessions ---------------------------------------------------------------

@main.command("list-sessions")
@click.option("--cwd", type=click.Path(path_type=Path), default=None,
              help="filter to sessions run in this directory")
@click.option("--since", default=None, help="ISO date, e.g. 2026-07-01")
@click.option("--all-projects", is_flag=True)
def list_sessions(cwd: Path | None, since: str | None, all_projects: bool):
    """List Claude Code sessions found on this machine."""
    from rich.table import Table

    from llm_energy.session.locate import find_sessions

    since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) if since else None
    infos = find_sessions(cwd=cwd, since=since_dt, all_projects=all_projects or cwd is None)
    if not infos:
        console.print("no sessions found (is ~/.claude/projects present?)")
        return
    table = Table()
    for col in ("session id", "modified (UTC)", "size", "cwd"):
        table.add_column(col)
    for i in infos[:50]:
        table.add_row(i.session_id[:8], i.mtime.strftime("%Y-%m-%d %H:%M"),
                      f"{i.size_bytes // 1024} kB", i.cwd or "?")
    console.print(table)


@main.command("analyze-session")
@click.option("--session-id", "session_ids", multiple=True,
              help="session id (prefix ok); repeat to merge several sessions")
@click.option("--latest", is_flag=True, help="use the most recent session")
@click.option("--for-task", "for_task", default=None,
              type=click.Path(path_type=Path, exists=True),
              help="analyze the session that coordinated this task result JSON")
@click.option("--for-session-power", "for_session_power", default=None,
              type=click.Path(path_type=Path, exists=True),
              help="analyze the session(s) linked to a measure-session result")
@click.option("--cwd", type=click.Path(path_type=Path), default=None)
@click.option("--coefficients", type=click.Path(path_type=Path), default=DEFAULT_COEFFS,
              show_default=True)
@click.option("--transcript", "transcripts", multiple=True,
              type=click.Path(path_type=Path),
              help="explicit transcript JSONL path(s), bypassing lookup")
@click.option("--out", type=click.Path(path_type=Path), default=DEFAULT_RESULTS,
              show_default=True)
@click.option("--out-file", "out_file", type=click.Path(path_type=Path),
              default=None,
              help="write the result to exactly this path (for scripts)")
def analyze_session(session_ids: tuple[str, ...], latest: bool,
                    for_task: Path | None, for_session_power: Path | None,
                    cwd: Path | None, coefficients: Path,
                    transcripts: tuple[Path, ...], out: Path,
                    out_file: Path | None):
    """Sum a session's tokens and estimate its energy band."""
    from llm_energy.schemas import (load_session_power, load_task_result,
                                    write_result)
    from llm_energy.session.energy import energy_band, load_coefficients
    from llm_energy.session.locate import find_session_by_id, find_sessions
    from llm_energy.session.parse import merge_usages, parse_session

    def resolve(sid: str, origin: str) -> tuple[Path, str]:
        info = find_session_by_id(sid)
        if info is None:
            raise click.ClickException(
                f"{origin} recorded session '{sid}', but no transcript for it "
                "was found — a different machine, or ~/.claude/projects pruned?")
        return info.path, info.session_id

    files: list[tuple[Path, str]] = [(p, p.stem) for p in transcripts]

    if for_task:
        task = _load_or_die(load_task_result, for_task, "task-run")
        if not task.coordinating_session_id:
            raise click.ClickException(
                f"{for_task} has no coordinating_session_id — it was produced "
                "outside a Claude Code session, or by an older version of this "
                "tool. Pass --session-id explicitly.")
        files.append(resolve(task.coordinating_session_id, str(for_task)))

    if for_session_power:
        sp = _load_or_die(load_session_power, for_session_power,
                          "session-power")
        if not sp.session_ids:
            raise click.ClickException(
                f"{for_session_power} has no linked sessions — no transcript "
                "started inside the measured window. Pass --session-id "
                "explicitly.")
        files.extend(resolve(s, str(for_session_power)) for s in sp.session_ids)

    for sid in session_ids:
        info = find_session_by_id(sid)
        if info is None:
            raise click.ClickException(f"session '{sid}' not found")
        files.append((info.path, info.session_id))
    if latest and not files:
        infos = find_sessions(cwd=cwd, all_projects=cwd is None)
        if not infos:
            raise click.ClickException("no sessions found")
        files.append((infos[0].path, infos[0].session_id))
    if not files:
        raise click.ClickException(
            "give --for-task, --for-session-power, --session-id, --latest, "
            "or --transcript")

    # the same transcript can arrive from several selectors; counting it twice
    # would inflate the band
    seen: set[Path] = set()
    unique: list[tuple[Path, str]] = []
    for p, sid in files:
        if p in seen:
            continue
        seen.add(p)
        unique.append((p, sid))
    files = unique

    usages = [parse_session(p.read_text().splitlines(), session_id=sid)
              for p, sid in files]
    usage = merge_usages(usages) if len(usages) > 1 else usages[0]
    coeffs = load_coefficients(coefficients)
    result = energy_band(usage, coeffs)

    short = (usage.session_ids[0][:8] if usage.session_ids else "manual")
    path = write_result(result, out_file or out / f"session-{short}-{_ts()}.json")
    b = result.total_band
    for m in usage.per_model:
        console.print(f"  {m.model}: in={m.input_tokens:,} out={m.output_tokens:,} "
                      f"cache-create={m.cache_creation_tokens:,} "
                      f"cache-read={m.cache_read_tokens:,} ({m.requests} requests)")
    console.print(f"E_LLM = {b.low_j:.0f} / {b.central_j:.0f} / {b.high_j:.0f} J "
                  f"(low/central/high, PUE {result.pue}) -> {path}")
    for n in result.notes:
        console.print(f"[dim]note: {n}[/dim]")


@main.command("find-task-result")
@click.argument("session_power", type=click.Path(path_type=Path, exists=True))
@click.option("--out", type=click.Path(path_type=Path), default=DEFAULT_RESULTS,
              show_default=True, help="results directory to search")
@click.option("--all", "show_all", is_flag=True,
              help="print every match rather than failing on more than one")
def find_task_result(session_power: Path, out: Path, show_all: bool):
    """Print the task result coordinated by a measure-session run.

    Matches on the session id stamped into the task result, so it is exact
    even when results/ holds many runs.
    """
    from llm_energy.pairing import find_task_results_for_sessions
    from llm_energy.schemas import load_session_power

    sp = _load_or_die(load_session_power, session_power, "session-power")
    if not sp.session_ids:
        raise click.ClickException(
            f"{session_power} links no sessions — nothing to match against")
    matches = find_task_results_for_sessions(out, sp.session_ids)
    if not matches:
        raise click.ClickException(
            f"no task result in {out} was coordinated by session(s) "
            f"{', '.join(s[:8] for s in sp.session_ids)} — did the agent "
            "actually run `llm-energy run-task`?")
    if len(matches) > 1 and not show_all:
        raise click.ClickException(
            f"{len(matches)} task results match this session: "
            + ", ".join(str(m) for m in matches)
            + " — pass --all, or pick one explicitly")
    for m in matches:
        click.echo(str(m))


@main.command("verify-deliverable")
@click.argument("workspace", type=click.Path(path_type=Path, exists=True))
@click.option("--task", "task_name", default="madgraph-ttbar-open",
              show_default=True, help="open task whose spec.yaml grades this")
@click.option("--task-dir", type=click.Path(path_type=Path), default=DEFAULT_TASKS,
              show_default=True)
@click.option("--file", "explicit", type=click.Path(path_type=Path), default=None,
              help="grade this file instead of searching the workspace")
def verify_deliverable(workspace: Path, task_name: str, task_dir: Path,
                       explicit: Path | None):
    """Check an open task's output against what the brief demanded.

    Reads the LHE payload, not the generator banner: a sample produced by an
    unexpected route still passes, and a convincing banner over the wrong
    physics still fails. Exits nonzero if the deliverable is wrong or missing.
    """
    from llm_energy.deliverable import find_deliverable, load_open_spec

    from llm_energy.deliverable import grade_workspace

    spec = load_open_spec(task_dir / task_name)
    if explicit is not None and not explicit.exists():
        # An explicit --file is a request to grade that file; falling back to
        # whatever else is lying around would answer a different question.
        raise click.ClickException(f"{explicit} does not exist")

    graded = grade_workspace(spec, workspace, events_override=explicit)
    for note in graded.notes:
        console.print(f"[yellow]note: {note}[/yellow]")
    if graded.events is None:
        raise click.ClickException(
            f"no LHE file anywhere under {workspace} — the run produced no "
            "deliverable")

    def show(title, report):
        console.print(f"[bold]{title}[/bold] {report.path}")
        for c in report.checks:
            mark = "[green]ok[/green]" if c.ok else "[red]FAIL[/red]"
            console.print(f"  {mark}  {c.name} — {c.detail}")

    show("events:", graded.events)
    if graded.events.events_sha256:
        console.print(f"  [dim]event content sha256: "
                      f"{graded.events.events_sha256[:16]}[/dim]")
    if graded.peak is not None:
        show("mass peak:", graded.peak)
    if graded.plot_missing:
        console.print(f"[red]FAIL[/red]  plot — nothing at "
                      f"{spec.plot_path}")

    if graded.ok:
        console.print("[green]deliverables meet the specification[/green]")
    else:
        console.print("[red]deliverables do NOT meet the specification[/red]")
        raise SystemExit(1)


@main.command("compare-deliverables")
@click.option("--run", "runs", multiple=True, required=True, nargs=2,
              metavar="LABEL WORKSPACE",
              help="repeat for each run being compared")
@click.option("--task", "task_name", default="madgraph-ttbar-open",
              show_default=True)
@click.option("--task-dir", type=click.Path(path_type=Path), default=DEFAULT_TASKS,
              show_default=True)
def compare_deliverables(runs: tuple[tuple[str, str], ...], task_name: str,
                         task_dir: Path):
    """Check whether open runs produced the same physics.

    With every stochastic stage seeded, the event samples must match exactly.
    The histograms need not: two agents that reconstruct the top differently
    get different histograms from identical events, which is the method
    varying rather than a reproducibility failure.
    """
    from rich.table import Table

    from llm_energy.deliverable import (compare_graded, grade_workspace,
                                        load_open_spec)

    if len(runs) < 2:
        raise click.ClickException("need at least two --run entries to compare")
    spec = load_open_spec(task_dir / task_name)
    graded = [(label, grade_workspace(spec, Path(ws))) for label, ws in runs]
    comp = compare_graded(graded)

    table = Table(title="open-run deliverables")
    table.add_column("Quantity")
    for label in comp.labels:
        table.add_column(label)
    table.add_row("generator version",
                  *[v or "unknown" for v in comp.generator_versions])
    table.add_row("event hash", *[h[:12] or "n/a" for h in comp.event_hashes])
    table.add_row("histogram hash",
                  *[h[:12] or "n/a" for h in comp.histogram_hashes])
    table.add_row("peak (GeV)",
                  *[f"{p:.1f}" if p is not None else "n/a" for p in comp.peaks_gev])
    console.print(table)

    if comp.events_identical:
        console.print("[green]events IDENTICAL[/green] — the pinned seeds held")
    else:
        console.print("[red]events DIFFER[/red] — at a fixed seed they should not")
    if comp.histograms_identical and comp.events_identical:
        console.print("[green]histograms identical[/green] — same reconstruction "
                      "as well as same events")
    elif comp.histograms_identical:
        # Different events cannot normally give the same histogram; the usual
        # cause is a histogram left over from an earlier run.
        console.print("[yellow]histograms identical despite differing events[/yellow]"
                      " — check the histogram was regenerated, not reused")
    else:
        console.print("[dim]histograms differ — expected when the "
                      "reconstruction methods differ[/dim]")
    for n in comp.notes:
        console.print(f"[yellow]note: {n}[/yellow]")
    if not comp.events_identical:
        raise SystemExit(1)


@main.command("breakdown")
@click.option("--label", default="run", show_default=True)
@click.option("--task", "task_result", default=None,
              type=click.Path(path_type=Path, exists=True),
              help="task result (its phases become separate portions)")
@click.option("--session-power", "session_power", default=None,
              type=click.Path(path_type=Path, exists=True),
              help="measure-session result, for the local coordination term")
@click.option("--session", "session_result", default=None,
              type=click.Path(path_type=Path, exists=True),
              help="analyze-session result, for the estimated inference term")
@click.option("--md", type=click.Path(path_type=Path), default=None)
@click.option("--chart", type=click.Path(path_type=Path), default=None,
              help="write a single-panel PDF figure (needs the [plots] extra)")
def breakdown_cmd(label: str, task_result: Path | None,
                  session_power: Path | None, session_result: Path | None,
                  md: Path | None, chart: Path | None):
    """Relative energy of each portion of a run.

    Takes whichever artifacts exist and assembles one budget: task phases (or
    observed containers), local coordination, and estimated inference. Shares
    are within the measured group; the estimated term is reported as a
    multiple of it, never folded in.
    """
    from llm_energy.analysis import (build_budget, render_budget_chart,
                                     render_budget_markdown,
                                     render_budget_terminal)
    from llm_energy.schemas import (load_session_power, load_session_result,
                                    load_task_result)

    if not any((task_result, session_power, session_result)):
        raise click.ClickException(
            "give at least one of --task, --session-power, --session")

    budget = build_budget(
        label,
        task=_load_or_die(load_task_result, task_result, "task-run")
        if task_result else None,
        session_power=_load_or_die(load_session_power, session_power,
                                   "session-power") if session_power else None,
        session=_load_or_die(load_session_result, session_result,
                             "session-energy") if session_result else None)

    render_budget_terminal(budget)
    if md:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_budget_markdown(budget))
        console.print(f"wrote {md}")
    if chart:
        console.print(f"wrote {render_budget_chart(budget, chart)}")


@main.command("report-open")
@click.argument("session_power", type=click.Path(path_type=Path, exists=True))
@click.argument("session_result", type=click.Path(path_type=Path, exists=True))
@click.option("--workspace", type=click.Path(path_type=Path), default=None,
              help="grade the deliverable in this workspace too")
@click.option("--task", "task_name", default="madgraph-ttbar-open",
              show_default=True)
@click.option("--task-dir", type=click.Path(path_type=Path), default=DEFAULT_TASKS,
              show_default=True)
@click.option("--md", type=click.Path(path_type=Path), default=None)
def report_open(session_power: Path, session_result: Path,
                workspace: Path | None, task_name: str, task_dir: Path,
                md: Path | None):
    """Report an open run: no harness-launched task, so E_task is the energy
    observed inside the containers the agent started."""
    from llm_energy.report import render_open_markdown, render_open_terminal
    from llm_energy.schemas import load_session_power, load_session_result

    sp = _load_or_die(load_session_power, session_power, "session-power")
    se = _load_or_die(load_session_result, session_result, "session-energy")

    graded = None
    if workspace:
        from llm_energy.deliverable import grade_workspace, load_open_spec
        graded = grade_workspace(load_open_spec(task_dir / task_name), workspace)

    render_open_terminal(sp, se, graded)
    if md:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_open_markdown(sp, se, graded))
        console.print(f"wrote {md}")


# --- report / compare / verify-events ----------------------------------------

@main.command("report")
@click.argument("task_result", type=click.Path(path_type=Path, exists=True))
@click.argument("session_result", type=click.Path(path_type=Path, exists=True))
@click.option("--session-power", "session_power", default=None,
              type=click.Path(path_type=Path, exists=True),
              help="measure-session result, adding the locally-measured cost "
                   "of coordination to the report")
@click.option("--md", type=click.Path(path_type=Path), default=None,
              help="write markdown report here")
@click.option("--chart", type=click.Path(path_type=Path), default=None,
              help="write bar chart PNG here (needs [plots] extra)")
def report_cmd(task_result: Path, session_result: Path,
               session_power: Path | None, md: Path | None, chart: Path | None):
    """Compare one task run against one coordination session."""
    from llm_energy.report import (Trial, render_chart, render_markdown,
                                   render_terminal)
    from llm_energy.schemas import (load_session_power, load_session_result,
                                    load_task_result)

    trial = Trial(
        label="run",
        task=_load_or_die(load_task_result, task_result, "task-run"),
        session=_load_or_die(load_session_result, session_result,
                             "session-energy"),
        session_power=(_load_or_die(load_session_power, session_power,
                                    "session-power")
                       if session_power else None))
    render_terminal(trial)
    if md:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_markdown(trial))
        console.print(f"wrote {md}")
    if chart:
        render_chart([trial], chart)
        console.print(f"wrote {chart}")


@main.command("compare")
@click.option("--trial", "trial_specs", multiple=True, required=True, nargs=3,
              metavar="LABEL TASK.json SESSION.json",
              help="repeat for each model/trial being compared")
@click.option("--trial-power", "trial_power_specs", multiple=True, nargs=2,
              metavar="LABEL POWER.json",
              help="attach a measure-session result to a trial, by label")
@click.option("--rtol", default=1e-9, show_default=True,
              help="relative tolerance for numeric event comparison")
@click.option("--md", type=click.Path(path_type=Path), default=None)
@click.option("--chart", type=click.Path(path_type=Path), default=None)
def compare_cmd(trial_specs: tuple[tuple[str, str, str], ...],
                trial_power_specs: tuple[tuple[str, str], ...], rtol: float,
                md: Path | None, chart: Path | None):
    """Compare trials coordinated by different LLM models, including whether
    their output events are identical.

    Example:
      llm-energy compare \\
        --trial fable results/task-...-1.json results/session-aa.json \\
        --trial haiku results/task-...-2.json results/session-bb.json
    """
    from llm_energy.report import (Trial, check_event_identity, render_chart,
                                   render_comparison_markdown,
                                   render_comparison_terminal)
    from llm_energy.schemas import (load_session_power, load_session_result,
                                    load_task_result)

    labels = [label for label, _, _ in trial_specs]
    powers = {}
    for label, p in trial_power_specs:
        if label not in labels:
            raise click.ClickException(
                f"--trial-power {label}: no --trial with that label "
                f"(have: {', '.join(labels)})")
        powers[label] = _load_or_die(load_session_power, Path(p),
                                     "session-power")

    trials = [Trial(label=label,
                    task=_load_or_die(load_task_result, Path(t), "task-run"),
                    session=_load_or_die(load_session_result, Path(s),
                                         "session-energy"),
                    session_power=powers.get(label))
              for label, t, s in trial_specs]
    if len(trials) < 2:
        raise click.ClickException("need at least two --trial entries to compare")
    identity = check_event_identity(trials, rtol=rtol)
    render_comparison_terminal(trials, identity, rtol)
    if md:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_comparison_markdown(trials, identity, rtol))
        console.print(f"wrote {md}")
    if chart:
        render_chart(trials, chart)
        console.print(f"wrote {chart}")


@main.command("verify-events")
@click.argument("lhe_files", nargs=-1, required=True,
                type=click.Path(path_type=Path, exists=True))
@click.option("--rtol", default=1e-9, show_default=True)
def verify_events(lhe_files: tuple[Path, ...], rtol: float):
    """Check whether LHE files contain identical physics events.

    Compares only <event> content (headers with timestamps/hosts are
    ignored); falls back to numeric comparison within --rtol when exact
    hashes differ.
    """
    from llm_energy.lhe import compare_lhe

    if len(lhe_files) < 2:
        raise click.ClickException("give at least two LHE files")
    comp = compare_lhe(list(lhe_files), rtol=rtol)
    for f, n, h in zip(comp.files, comp.n_events, comp.hashes):
        console.print(f"  {f}: {n} events, hash {h[:16]}")
    if comp.identical:
        console.print("[green]IDENTICAL[/green] event content")
    elif comp.numerically_equal:
        console.print(f"[green]numerically equal[/green] within rtol={rtol} "
                      "(exact hashes differ)")
    else:
        console.print(f"[red]DIFFERENT[/red]: {comp.first_difference}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
