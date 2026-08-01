# llm-energy

How much energy does an LLM consume *coordinating* a HEP workflow task,
compared to the energy of *running* the task itself?

This tool measures both sides for tasks in a HEP research workflow. First
benchmark: MadGraph5_aMC@NLO generating 10,000 `p p > t t~` events at
√s = 13.6 TeV (LO, unweighted LHE output) in a Docker container.

A measured run is one command. It takes an idle baseline, starts a *fresh*
Claude Code session on the task brief, measures everything that session does,
and reports:

```sh
scripts/measure-run.sh
```

Three energies come out of it:

- **E_task** is *measured* on a dedicated Mac: Apple Silicon package power
  (`sudo powermetrics`) integrated over the container's run, minus an idle
  baseline.
- **E_coord,local** is *measured* the same way over the whole agent session,
  minus the nested task run — this machine's cost of the agent working.
- **E_LLM** is *estimated* from the Claude Code session that coordinated the
  task: token counts parsed from the session transcript, converted to Joules
  via literature-derived coefficients (Epoch AI 2025, Google's Gemini
  disclosure, Mistral's lifecycle analysis, per-token GPU measurements),
  reported as a low/central/high band including PUE.

Full assumptions, citations, and caveats: [docs/methodology.md](docs/methodology.md).

## Setup

Copy-paste, start to finish. Five steps on a fresh machine; step 3 is
macOS-only, step 5 is only needed for the MadGraph task.

| Dependency | Why it's needed | Required for |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python project manager — also provisions Python ≥ 3.10 itself | everything |
| Docker | runs the measured task container | `run-task` |
| [Claude Code](https://claude.com/claude-code) | the coordination being measured; writes the session transcripts this tool reads | `analyze-session` |
| password-less `sudo powermetrics` | reads Apple Silicon package power without an interactive prompt mid-run | measuring on macOS |

You do **not** need to install Python separately — `uv sync` downloads a
suitable interpreter if the system one is too old.

### 1. Install the dependencies

**macOS** — the measurement machine:

```sh
# Homebrew — skip if `brew --version` already works
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install uv
brew install --cask docker-desktop     # ships the `docker` CLI too
brew install --cask claude-code

open -a Docker                         # start it, and leave it running
```

<details>
<summary>Using colima instead of Docker Desktop</summary>

```sh
brew install uv colima docker          # here `docker` is the CLI only
brew install --cask claude-code
colima start --cpu 4 --memory 8 --disk 60
```

MadGraph's Fortran build needs more than colima's 2 CPU / 2 GiB default, hence
the explicit sizing. Either VM works for measurement, as long as the idle
baseline is taken with that same VM running (step 6.1).
</details>

**Linux** — development, CI, or RAPL-based measurement:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"        # log out and back in for this to apply
curl -fsSL https://claude.ai/install.sh | bash
```

### 2. Get the code

```sh
git clone https://github.com/UTK-Colliders/llm-energy
cd llm-energy
uv sync
```

`uv sync` creates `.venv/`, installs the runtime and dev dependencies, and puts
the `llm-energy` CLI on `uv run`. For chart output (`--chart`), install the
extra instead: `uv sync --extra plots` — a later plain `uv sync` removes
matplotlib again, so keep the flag once you start using it.

### 3. Password-less powermetrics (macOS only)

`powermetrics` requires root, and runs are unattended — a sudo password prompt
part-way through invalidates the trace. Install a rule scoped to that one
binary:

```sh
printf '%s ALL=(root) NOPASSWD: /usr/bin/powermetrics\n' "$(whoami)" \
  | sudo tee /etc/sudoers.d/powermetrics >/dev/null
sudo chmod 440 /etc/sudoers.d/powermetrics
sudo visudo -c -f /etc/sudoers.d/powermetrics    # must print "parsed OK"
```

Confirm it works without a prompt:

```sh
sudo -n powermetrics --samplers cpu_power -n 1 -i 200 >/dev/null && echo ok
```

Rationale and the no-sudoers alternative:
[scripts/sudoers-powermetrics.md](scripts/sudoers-powermetrics.md).

### 4. Verify the install

```sh
uv run llm-energy doctor
uv run pytest
```

`doctor` on a ready Mac, before the image is built:

```
llm-energy doctor (platform: Darwin arm64)
  ok  power backend 'powermetrics' — parsed sample: combined=1421 mW (cpu=1180.0, gpu=190.0, ane=0.0), source=combined-line
  ok  docker daemon reachable
  ok  task 'madgraph-ttbar-lhe' parses — default image: llm-energy/mg5amc:3.5.16
  --  image llm-energy/mg5amc:3.5.16 not built/pulled yet (run-task will handle it)
  ok  coefficients parse — default.yaml, pue=1.2, sha256=c37725a39568
```

It exits nonzero if any check fails, so it can gate a script. See
[Troubleshooting](#troubleshooting) for what each failure means.

### 5. Pre-build the MadGraph image

```sh
docker build -t llm-energy/mg5amc:3.5.16 tasks/madgraph-ttbar-lhe
```

Takes 10–20 min and a few GB of disk. `run-task` builds it automatically if
missing, outside the measured window — but building it now keeps the baseline
and the measured run close together in time, and surfaces build failures before
you have quiesced the machine.

### Try it without a Mac or Docker

The parsers, coefficients, and reporting run anywhere. To exercise them:

```sh
uv run pytest
uv run llm-energy analyze-session --transcript tests/fixtures/session_basic.jsonl
uv run llm-energy baseline --duration 10 --backend tdp-model
```

The `analyze-session` call prints a real energy band from the fixture
transcript:

```
  claude-fable-5: in=110 out=250 cache-create=1,000 cache-read=5,000 (2 requests)
E_LLM = 159 / 841 / 3306 J (low/central/high, PUE 1.2) -> results/session-...json
```

`tdp-model` is a labeled CPU-utilization × TDP *estimate*, not a measurement —
fine for checking the plumbing, not for results.

## Run a measurement end to end

On the measurement Mac, with setup complete:

```sh
scripts/measure-run.sh
```

That is the whole thing. It runs `doctor`, makes sure the task image is built
(*before* the measurement starts, so a 10–20 min build is never charged to the
agent), takes a 2-minute idle baseline, starts a fresh headless Claude Code
session pointed at the task brief, measures package power for as long as that
session runs, then pairs the artifacts and writes a report. Useful flags:

| Flag | Effect |
|---|---|
| `--model NAME` | pass a model to `claude` — one flag per cross-model trial |
| `--interactive` | supervise the session instead of running it headless |
| `--skip-baseline` | reuse the newest baseline for this machine |
| `--task NAME` | a different task under `tasks/` |

### What the agent is told

The session is started with nothing but *"read
`tasks/<task>/BRIEF.md` and do the job it describes."* Two files define its
behaviour, and both are part of the experiment:

- [`CLAUDE.md`](CLAUDE.md) — loaded automatically. It tells a session whether
  it is the measured coordinating agent or a harness developer, and bars the
  agent from running the operator's instruments (`baseline`,
  `analyze-session`, `report`, …). Without that boundary the session spends
  its tokens measuring itself.
- [`tasks/madgraph-ttbar-lhe/BRIEF.md`](tasks/madgraph-ttbar-lhe/BRIEF.md) —
  the job: the required physics, how to run it, how to verify the output, and
  what to do if it fails.

### Doing it by hand

The script is four commands in a trench coat, if you'd rather drive them:

```sh
# 1. Idle baseline (~2 min). Quiesce the machine; leave Docker running idle.
uv run llm-energy baseline --duration 120

# 2. Run the agent under power measurement. Everything it does counts.
uv run llm-energy measure-session -- \
  claude -p "Read tasks/madgraph-ttbar-lhe/BRIEF.md and do the job it describes."

# 3. Pair the artifacts. The task result carries the id of the session that
#    invoked it, so this is exact rather than newest-file guesswork.
POWER=$(ls -t results/power-session-*.json | head -1)
TASK=$(uv run llm-energy find-task-result "$POWER")
uv run llm-energy analyze-session --for-task "$TASK" --out-file results/session-energy.json

# 4. Report all three energies.
uv run llm-energy report "$TASK" results/session-energy.json \
                         --session-power "$POWER" --md results/report.md
```

If a coordination episode spanned several sessions (resumed or compacted),
merge them explicitly:

```sh
uv run llm-energy list-sessions --cwd "$PWD"
uv run llm-energy analyze-session --session-id a1b2c3d4 --session-id e5f6a7b8
```

Each run writes timestamped JSON to `results/`, plus a
`results/run-madgraph-ttbar-lhe-<ts>/` directory holding the raw power trace,
the container log, and the MadGraph output tree
(`proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz`).

### How the pieces are linked

Claude Code exports `CLAUDE_CODE_SESSION_ID` into every subprocess it spawns,
and its value is the transcript's filename. So when the agent invokes
`run-task`, the harness records *which conversation asked for it* in the task
result. `analyze-session --for-task` then analyses exactly that session.

This matters more than it sounds. `--latest` sorts every transcript on the
machine by mtime: run it from a second Claude session and you measure that
session instead, silently. Prefer `--for-task` whenever a task result exists.

## Open tasks: measuring the cost of solving, not of executing

There are two kinds of task here, and they measure different things.

| | `madgraph-ttbar-lhe` (pinned) | `madgraph-ttbar-open` |
|---|---|---|
| The agent is given | the run card, the command, the steps | the required physics, nothing else |
| The agent works out | nothing | how to get MadGraph, what card to write, how to run and check it |
| Runs in | this repo | a scratch workspace outside it, containing only the brief |
| Measures | the floor: executing a known solution | solving the problem |
| Correctness is | guaranteed by construction | **an outcome**, graded per run |

The pinned task exists as a control. Its brief hands over
`cards/ttbar_lhe.mg5`, which is the complete answer — including the
non-obvious double `done` that MG5 3.5.x's prompt flow needs — so a session on
it costs about what reading a runbook costs, and barely discriminates between
models. Subtract it from an open run and what's left is the cost of figuring
the problem out.

```sh
scripts/measure-run.sh --task madgraph-ttbar-open
```

**The workspace is outside the repo on purpose.** Inside it, an agent can find
that worked card, and a good one *would* — at which point the open task
silently degrades back into the pinned one. The script copies the brief into
`~/llm-energy-workspaces/<task>-<ts>/` (override with
`LLM_ENERGY_WORKSPACE_ROOT`) and runs the session there, with nothing else in
scope. No image is pre-built either: sourcing a generator is part of the job.

### How it is still measured

The harness launches nothing, so it watches instead. `measure-session` records
the Docker daemon's event stream for the session's duration and pairs
container start/stop events into windows, whoever started them:

| Quantity | How |
|---|---|
| Session energy | measured, whole window |
| **E_compute** | measured, inside the union of observed container windows |
| **E_coord,local** | measured, the remainder — the agent thinking and reading |
| E_LLM | estimated from tokens, as before |

Overlapping containers are merged so their energy is counted once, and one
still running at session end is reported rather than attributed. If the agent
runs a generator directly on the host instead of in a container, that work
lands in the coordination bucket — the report says so.

### Grading the result

With the method unpinned, the output can be wrong, so a run's energy only
means something next to a verdict:

```sh
uv run llm-energy verify-deliverable ~/llm-energy-workspaces/madgraph-ttbar-open-<ts>
```

```
  ok    beam energy — 6800 / 6800 GeV, wanted 6800 each
  ok    beam particles — PDG 2212 / 2212, wanted 2212 / 2212
  FAIL  event count — 500 events, wanted 10000
  FAIL  final state — found an event with final state (-5, 5), wanted (-6, 6)
deliverable does NOT meet the specification
```

Checks read the LHE payload — the `<init>` block and the event records — not
the generator's banner. A sample produced by an unexpected route still passes;
a convincing banner over the wrong physics still fails. The grading key lives
in `tasks/madgraph-ttbar-open/spec.yaml` and is never shown to the agent.

A failed run is recorded, not discarded: a model that burns 300k tokens and
produces nothing is a result.

## Separating compilation from execution

`madgraph-ttbar-lhe` measures one number for two different kinds of work: MG5's
`launch` compiles the generated Fortran and *then* generates events.
`madgraph-ttbar-split` runs the same physics — same process, beams, event
count, and seed — as two separately measured phases:

```sh
uv run llm-energy run-task madgraph-ttbar-split
```

```
  codegen-compile: 3100.0 J (77.5%) over 380.0 s, mean 8.16 W, 142 build artifacts
  event-generation: 900.0 J (22.5%) over 220.0 s, mean 4.09 W, 0 build artifacts
gross 4000.0 J, net 3800.0 J over 600.0 s (mean 6.67 W) -> results/task-...json
```

Each phase runs in its own container over a shared run directory, all under one
power trace, with each phase's energy integrated over its own window. Task
totals are the sum over phases, so `report` and `compare` work unchanged and
gain a per-phase table.

**The split is verified, not assumed.** Every phase counts the compiler output
(`.o`, `.a`, `.so`, `.mod`) written inside its own window. A clean split shows
a large count in `codegen-compile` and **zero** in `event-generation`; if
artifacts appear in both, the run is flagged, because compilation leaking into
the generation window would move energy between the two buckets:

```
warning: compiler output was written in 2 phases (codegen-compile,
event-generation) — the compile/run split did not hold
```

Whether the split task produces byte-identical events to the single-phase one
is an empirical question — check it with `verify-events` rather than assuming
it. `madgraph-ttbar-lhe` is untouched, so existing results stay comparable.

### Adding phases to your own task

Any `task.yaml` can use `phases:` instead of `command:`:

```yaml
phases:
  - name: codegen-compile
    description: matrix-element code generation and Fortran compilation
    command: ["bash", "-euc", "mg5_aMC /cards/codegen.mg5 && cd ... && make"]
  - name: event-generation
    command: ["mg5_aMC", "/cards/launch.mg5"]
```

Phases share the run directory and run in order; a failed phase skips the rest.
`command:` remains valid and is reported as a single phase named `run`.

## Comparing LLM models

One run per model — same brief, same pinned physics, different coordinator:

```sh
scripts/measure-run.sh --model claude-fable-5 --label fable --skip-baseline
scripts/measure-run.sh --model claude-haiku-4-5-20251001 --label haiku --skip-baseline
```

Take a fresh baseline for the first run and reuse it for the rest, so all
trials are netted against the same idle figure. Each run prints its artifact
paths at the end; feed them to `compare`, which lines the trials up and
verdicts whether they produced physically identical events (the run card pins
`iseed`, so identical events are the expected outcome on the same image):

```sh
uv run llm-energy compare \
  --trial fable  results/task-...-a.json results/session-aa...json \
  --trial haiku  results/task-...-b.json results/session-bb...json \
  --trial-power fable results/power-session-a.json \
  --trial-power haiku results/power-session-b.json \
  --md results/comparison.md

# or directly on LHE files:
uv run llm-energy verify-events \
  results/run-madgraph-ttbar-lhe-A/proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz \
  results/run-madgraph-ttbar-lhe-B/proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz
```

Event identity compares only `<event>` physics content (headers with
timestamps/hostnames are ignored) and falls back to numeric comparison with
`--rtol` when exact hashes differ (e.g. across CPU architectures).

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Preflight: power backend, sudo, docker, task config, coefficients |
| `baseline` | Measure idle package power (subtracted from every other measurement) |
| `run-task TASK` | Run a task container under energy measurement |
| `measure-session [-- CMD]` | Measure package power for a whole agent session (default `claude`) |
| `list-sessions` | List Claude Code sessions on this machine |
| `analyze-session` | Token counts → energy band (`--for-task` pairs exactly; merge with repeated `--session-id`) |
| `find-task-result` | The task run a `measure-session` result coordinated |
| `report` | Task run vs. coordination session, plus `--session-power` for local cost |
| `compare` | Multiple model trials side by side + event identity |
| `verify-events` | Check LHE files for identical physics events |

Results land in `results/` as timestamped JSON, with raw power traces and
container logs kept per run for auditability:

| File | What it holds |
|---|---|
| `baseline-<ts>.json` | idle package power for this machine |
| `task-<task>-<ts>.json` | E_task, plus the id of the session that invoked it |
| `power-session-<ts>.json` | E over the whole agent session, plus its session ids |
| `session-<id>-<ts>.json` | tokens and the E_LLM band |
| `run-<task>-<ts>/` | raw power trace, container log, MadGraph output tree |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `FAIL power backend 'powermetrics' — sudo requires a password` | no sudoers rule | setup step 3, or `sudo -v` immediately before a short run |
| `FAIL power backend 'powermetrics' — powermetrics binary not found` | not macOS | pass `--backend rapl` (bare metal, root) or `--backend tdp-model` (estimate) |
| `FAIL docker daemon reachable` | Docker not started | `open -a Docker`, or `colima start` |
| `image ... EMULATED, energy will be distorted` | amd64 image on Apple Silicon | drop `--image-variant scailfin`; the default `native` variant builds for the host |
| `no baseline for this machine in results/` | no baseline recorded on this host (matched by chip + hostname) | run `llm-energy baseline` first, or pass `--baseline none` |
| `no sessions found (is ~/.claude/projects present?)` | Claude Code hasn't run locally, or transcripts live elsewhere | coordinate in the local CLI (web sessions leave no local transcript); set `CLAUDE_CONFIG_DIR` if your config dir is non-standard |
| `has no coordinating_session_id` | `run-task` ran outside a Claude Code session, or the result predates the stamp | pass `--session-id` explicitly |
| `no task result ... was coordinated by session(s) ...` | the agent never got as far as `run-task` | read the session output; the brief tells it to run the task |
| `no Claude Code session started inside the measured window` | the agent was resumed rather than started fresh | `measure-session` must wrap a *new* session; drop `--resume`/`--continue` |
| `the task run does not lie inside the measured session window` | task and session came from different runs | re-pair with `find-task-result`, don't hand-pick files |
| `no usable energy figure for this session` | the sampler died mid-session (usually sudo credentials expiring) | install the sudoers rule; the session's tokens are still recoverable — the result keeps its session ids |
| `powermetrics exited early (rc=...)` | cached sudo credentials expired mid-run | install the sudoers rule — the default 5-min sudo timeout is shorter than a MadGraph run |
| RAPL: `energy_uj not readable (needs root)` | kernels ≥ 5.10 restrict RAPL counters | run as root, or use `--backend tdp-model` |
| `task exited with code N` | container failed; no partial result is written | read `results/run-<task>-<ts>/container.log` |

## Notes

- The primary MadGraph image is built natively for the host architecture; the
  amd64-only community image (`scailfin/madgraph5-amc-nlo`) is available as
  `--image-variant scailfin` but runs emulated on Apple Silicon and is flagged
  as such — don't use it for headline numbers.
- On Linux, a RAPL backend (bare metal, root) or a clearly-labeled
  CPU-utilization × TDP estimate backend keeps the pipeline testable; the Mac
  path is primary. Override the assumed TDP with `LLM_ENERGY_TDP_W`.
- Dev: `uv run pytest` — all parsers are pure functions tested from fixtures.
