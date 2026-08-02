# llm-energy

How much energy does an LLM consume *coordinating* a HEP workflow task,
compared to the energy of *running* the task itself?

This tool measures both sides for tasks in a HEP research workflow. First
benchmark: MadGraph5_aMC@NLO generating 10,000 `p p > t t~ j j` events at
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
  ok  task 'madgraph-ttbar2j-lhe' parses — default image: llm-energy/mg5amc:3.5.16
  --  image llm-energy/mg5amc:3.5.16 not built/pulled yet (run-task will handle it)
  ok  coefficients parse — default.yaml, pue=1.2, sha256=c37725a39568
```

It exits nonzero if any check fails, so it can gate a script. See
[Troubleshooting](#troubleshooting) for what each failure means.

### 5. Pre-build the MadGraph image

```sh
docker build -t llm-energy/mg5amc:3.5.16 tasks/madgraph-ttbar2j-lhe
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
agent), takes a 30-second idle baseline, starts a fresh headless Claude Code
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
- [`tasks/madgraph-ttbar2j-lhe/BRIEF.md`](tasks/madgraph-ttbar2j-lhe/BRIEF.md) —
  the job: the required physics, how to run it, how to verify the output, and
  what to do if it fails.

### Doing it by hand

The script is four commands in a trench coat, if you'd rather drive them:

```sh
# 1. Idle baseline (30 s). Quiesce the machine; leave Docker running idle.
uv run llm-energy baseline

# 2. Run the agent under power measurement. Everything it does counts.
uv run llm-energy measure-session -- \
  claude -p "Read tasks/madgraph-ttbar2j-lhe/BRIEF.md and do the job it describes."

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
`results/run-madgraph-ttbar2j-lhe-<ts>/` directory holding the raw power trace,
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

| | `madgraph-ttbar2j-lhe` (pinned) | `madgraph-ttbar2j-open` |
|---|---|---|
| The agent is given | the run card, the command, the steps | a dozen lines, the way a colleague would ask |
| The job | generate the events | generate → shower in Pythia 8 → reconstruct the top mass peak |
| The agent works out | nothing | how to get MadGraph and Pythia, what cards to write, how to reconstruct, how to plot |
| Runs in | this repo | a scratch workspace outside it, containing only the brief |
| Measures | the floor: executing a known solution | solving the problem |
| Correctness is | guaranteed by construction | **an outcome**, graded per run |

The open task is a three-step physics job, not a one-liner. The agent has to
work out the decay channel, the jet definition, and the combinatorics on its
own — that reasoning is the thing being measured.

The brief is deliberately short and informal, because a specification document
is not what anyone would actually send a collaborator, and a measurement of an
agent following a spec is not a measurement of an agent doing research. It
reads in full:

> Can you get me a ttbar+2 jets sample and show me the top mass peak?
>
> - 10k events, leading order, pp at 13.6 TeV, MadGraph
> - shower it through Pythia 8
> - seed 42 everywhere, so I can compare this against other people's runs
> - leave the LHE, the plot, and the histogram numbers as JSON (bin edges and
>   counts, in GeV) somewhere in this directory

Everything the grader needs is implied by that, not dictated by it. Because
the brief says *somewhere in this directory* rather than naming files, grading
searches by shape: the LHE by extension, the histogram by finding a JSON whose
arrays are in the N+1/N relationship a histogram has, the plot by being a
figure. Key names are read forgivingly — `bin_edges_gev`/`counts`,
`edges`/`values`, `x`/`y`, or anything else that fits structurally.

The pinned task exists as a control. Its brief hands over
`cards/ttbar2j_lhe.mg5`, which is the complete answer — including the
non-obvious double `done` that MG5 3.5.x's prompt flow needs — so a session on
it costs about what reading a runbook costs, and barely discriminates between
models. Subtract it from an open run and what's left is the cost of figuring
the problem out.

```sh
scripts/measure-run.sh --task madgraph-ttbar2j-open --allow-all-tools
```

A headless session has nobody to approve tool use, so every Bash and Write call
is denied and the agent talks for a few minutes and produces nothing — a full
measurement run spent on an empty result. `--allow-all-tools` passes
`--dangerously-skip-permissions`, which is what it sounds like: the agent runs
commands, installs software and starts containers as you, unattended. Do that
only on a machine you would hand over. `--interactive` is the alternative —
you approve each call, at the cost of your own attention being in the loop.
The script refuses a headless run without one of the two rather than collect a
run that cannot work.

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
uv run llm-energy verify-deliverable ~/llm-energy-workspaces/madgraph-ttbar2j-open-<ts>
```

```
events:   .../unweighted_events.lhe.gz
  ok    beam energy — 6800 / 6800 GeV, wanted 6800 each
  ok    event count — 10000 events, wanted 10000
  ok    final state — all events (-6, 6)
mass peak: .../top_mass_hist.json
  ok    histogram parses — 30 bins, 100-250 GeV
  ok    entries — 45119 entries, wanted at least 200
  FAIL  peak is interior — tallest bin at 102.5 GeV — that is the edge of the
        range, so the histogram shows a tail, not a peak
  FAIL  peak position — 102.5 GeV, wanted 172.5 ± 15
deliverables do NOT meet the specification
```

Event checks read the LHE payload — the `<init>` block and the event records —
not the generator's banner. A sample produced by an unexpected route still
passes; a convincing banner over the wrong physics still fails.

The peak is graded from `top_mass_hist.json`, which the brief asks for
alongside the figure, because a plot cannot be checked automatically — relabel
its axes and it looks the same to a grader. Four things are checked: the
histogram parses, it has enough entries, the tallest bin is *interior* (a
maximum in the end bin is a falling spectrum, not a peak), it sits within
172.5 ± 15 GeV, and it rises above its own median bin. The window is wide on
purpose: this is a *reconstructed* mass, so jets, combinatorics and
out-of-cone losses shift and broaden it. A peak outside that window means the
reconstruction is wrong, not that the physics is.

The grading key lives in `tasks/madgraph-ttbar2j-open/spec.yaml` and is never
shown to the agent. A failed run is recorded, not discarded: a model that
burns 300k tokens and produces a W peak where a top peak belongs is a result.

## Separating compilation from execution

`madgraph-ttbar2j-lhe` measures one number for two different kinds of work: MG5's
`launch` compiles the generated Fortran and *then* generates events.
`madgraph-ttbar2j-split` runs the same physics — same process, beams, event
count, and seed — as two separately measured phases:

```sh
uv run llm-energy run-task madgraph-ttbar2j-split
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
it. `madgraph-ttbar2j-lhe` is untouched, so existing results stay comparable.

### Comparing runs exactly

Every stochastic stage in the open brief is pinned — MadGraph seed 42, Pythia
seed 42, and any randomness the agent introduces must be seeded too — so runs
can be compared event by event:

```sh
uv run llm-energy compare-deliverables \
  --run fable ~/llm-energy-workspaces/madgraph-ttbar2j-open-A \
  --run haiku ~/llm-energy-workspaces/madgraph-ttbar2j-open-B
```

```
 generator version   3.5.16         3.5.16
 event hash          94b2d3eaf82b   94b2d3eaf82b
 histogram hash      37a4180fbf1f   16a0685bc157
 peak (GeV)          171.0          176.0
events IDENTICAL — the pinned seeds held
histograms differ — expected when the reconstruction methods differ
```

The two artefacts carry different expectations, and conflating them would
mislead:

- **Events must match.** Same generator, same version, same seed. A difference
  means a seed was not honoured — or the versions differ, which the tool
  checks first, because an unrecorded or mismatched version explains different
  events on its own and blaming a seed would be wrong.
- **Histograms need not.** Two agents reconstructing the top differently reach
  different histograms from identical events. That is the method varying, not
  a reproducibility failure — and it is the interesting comparison: same
  physics in, how far apart do the answers land. Identical histograms *with*
  differing events is the one suspicious combination, and is flagged: it
  usually means a histogram was reused rather than regenerated.

Histograms are fingerprinted over their values, not their JSON text, so
formatting choices do not masquerade as physics differences.


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

## Where the energy went

`breakdown` assembles whichever artifacts a run produced into one budget, so
the portions can be read against each other:

```sh
uv run llm-energy breakdown --label madgraph-ttbar2j-split \
  --task results/task-....json \
  --session-power results/power-session-....json \
  --session results/session-....json \
  --md results/budget.md --chart results/budget.pdf
```

| Portion | Basis | Energy | Share |
|---|---|---|---|
| codegen-compile | measured | 3100 J (0.861 Wh) | 28.4% |
| event-generation | measured | 900 J (0.250 Wh) | 8.3% |
| coordination (local) | measured | 6900 J (1.917 Wh) | 63.3% |
| **total measured** | | **10900 J (3.028 Wh)** | **100%** |
| LLM inference | estimated | 45000 / 180000 / 720000 J | 4.1× / 16.5× / 66.1× of measured |

It adapts to what it is given: a split task contributes one portion per phase,
a single-phase task one, an open run contributes compute-in-containers and
coordination, and the token result contributes the inference band. Any subset
of the three artifacts works.

**Shares are within the measured group only.** Local SoC package energy and
estimated remote datacenter energy are different quantities and are never
summed — the estimated term is reported as a *multiple* of the measured total,
never as a slice of it. `--chart` writes a single-panel PDF on a log axis
(the portions span orders of magnitude), drawn as a dot plot rather than bars
because a log axis has no zero for a bar to grow from, with the estimated term
marked differently so it cannot be misread as a measurement.

## Energy reference points

Joules mean nothing to most readers, so `references` converts a run into things
people have intuitions about. Twenty-nine of them across three scales:

```sh
uv run llm-energy references --joules 62928
uv run llm-energy references --scale industry --scale national --joules 2e6
```

**Individual** — the same order as a research run, so the ratio reads directly:

| | Energy | Kind |
|---|---|---|
| Charging a phone overnight | 0.07 MJ | consumed |
| Boiling a kettle (1 L) | 0.39 MJ | consumed |
| Under-inflated tyres, one commute | 1.85 MJ | wasted |
| 65" TV on 6 h | 2.16 MJ | consumed |
| AC 3 °F below recommended, one day | 6.48 MJ | wasted |
| Washing one load hot instead of cold | 7.20 MJ | wasted |
| House lights on 12 h (LED / incandescent) | 7.78 / 51.8 MJ | wasted |
| Tumble drying one load instead of hanging it | 10.8 MJ | wasted |
| A 10-minute hot shower | 11.1 MJ | consumed |
| Charging a phone nightly for a year | 26.3 MJ | consumed |
| Commute, hybrid / sedan / pickup (2 × 30 min) | 65 / 103 / 180 MJ | consumed |
| Driving 100 miles in an EV | 103 MJ | consumed |
| AC 3 °F below recommended, one season | 583 MJ | wasted |
| Under-inflated tyres, one year | 833 MJ | wasted |
| Transatlantic flight, one economy seat, return | 13.5 GJ | consumed |
| One US home's electricity for a year | 37.8 GJ | consumed |

**Industry** — six to nine orders above a run:

| | Energy |
|---|---|
| One datacentre rack for a day | 864 MJ |
| Smelting one tonne of steel | 20 GJ |
| A 1 MW university cluster for a day | 86.4 GJ |
| **The LHC running for an hour** | 720 GJ |
| Training one frontier-scale LLM (GPT-3 scale) | 4.6 TJ |
| A 1 MW datacentre for a year | 31.5 TJ |

**National** — annual electricity for a small country:

| | Energy |
|---|---|
| Malta, one day | 26.6 TJ |
| Malta, one year | 9.7 PJ |
| Estonia, one year | 30.6 PJ |
| Iceland, one year | 70.2 PJ |

### One run cannot be compared to a country

A run is kJ–MJ; Malta's year is PJ. *"1/9,700,000,000 of Malta"* is not a number
anyone can read, and quoting it would be theatre. The big references only mean
something against an **aggregate**, so state the rate and the population:

```sh
uv run llm-energy references --scale national --joules 2e6 \
  --runs-per-day 5 --actors 1000
```

```
At scale: 1,000 × 5 runs/day × 365 days = 3.65 GJ/year (1.0 MWh)
  Malta, one year of electricity    9.72 PJ    1/2,663
```

That is a claim worth making — a thousand researchers doing this five times a
day for a year is a readable fraction of a small country. One run is not.
Industry and national rows also carry a **runs to equal** column for the same
reason: *35.1 billion runs = Iceland for a year* is legible where the inverse
fraction is not.

### What the table keeps apart

- **Basis.** Vehicles and flights are chemical fuel energy; household,
  industrial and national figures are electricity at the meter. A kWh of each
  is not the same thing — US electricity costs roughly 2.6 kWh of primary
  energy to deliver. `--primary` puts both on one footing.
- **Consumed vs wasted.** The AC setpoint, soft tyres, a hot wash and the
  tumble dryer are *avoidable overhead*. "Costs as much as a commute" and
  "costs as much as the waste from a hot wash" are different claims.
- **Scale.** Every reference declares whether it is individual, industry or
  national, so a plot cannot silently put a kettle and a country on one axis
  as though the comparison meant the same thing.
- **Spread.** These are order-of-magnitude anchors. Assumptions live in
  [`references/everyday.yaml`](references/everyday.yaml) and are meant to be
  edited; the arithmetic is in `references.py` and tested.

Confirm the citations in that file before publishing — they are standard
published figures (EIA, EPA, DOE, FHWA, ENERGY STAR, Patterson et al. 2021 for
LLM training, national electricity statistics) but are quoted from general
knowledge, not fetched. The LHC and national numbers are rounded hard.

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
  results/run-madgraph-ttbar2j-lhe-A/proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz \
  results/run-madgraph-ttbar2j-lhe-B/proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz
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
