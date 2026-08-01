# llm-energy

How much energy does an LLM consume *coordinating* a HEP workflow task,
compared to the energy of *running* the task itself?

This tool measures both sides for tasks in a HEP research workflow. First
benchmark: MadGraph5_aMC@NLO generating 10,000 `p p > t t~` events at
√s = 13.6 TeV (LO, unweighted LHE output) in a Docker container.

- **E_task** is *measured* on a dedicated Mac: Apple Silicon package power
  (`sudo powermetrics`) integrated over the container's run, minus an idle
  baseline.
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
# 1. Idle baseline (~2 min). Quiesce the machine; leave Docker running idle.
uv run llm-energy baseline --duration 120

# 2. Do the task interactively in Claude Code — this session IS the
#    coordination being measured. Start it in this repo:
claude
#    ...and ask it to generate the events. The session ends with the LLM
#    invoking the measured run itself:
#        uv run llm-energy run-task madgraph-ttbar-lhe

# 3. Convert the coordination session's tokens to an energy band:
uv run llm-energy analyze-session --latest

# 4. Compare the two sides:
TASK=$(ls -t results/task-madgraph-ttbar-lhe-*.json | head -1)
SESSION=$(ls -t results/session-*.json | head -1)
uv run llm-energy report "$TASK" "$SESSION" --md results/report.md
```

Step 3 picks the newest transcript. If the coordination spanned several
sessions (resumed or compacted), list them and merge explicitly:

```sh
uv run llm-energy list-sessions --cwd "$PWD"
uv run llm-energy analyze-session --session-id a1b2c3d4 --session-id e5f6a7b8
```

Each run writes a timestamped JSON to `results/`, plus a
`results/run-madgraph-ttbar-lhe-<ts>/` directory holding the raw power trace,
the container log, and the MadGraph output tree
(`proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz`).

## Comparing LLM models

Repeat the workflow once per model (e.g. switch the model in Claude Code),
then line the trials up — including a verdict on whether the models produced
physically identical events (the run card pins `iseed`, so identical events
are the expected outcome on the same image):

```sh
uv run llm-energy compare \
  --trial fable  results/task-...-a.json results/session-aa...json \
  --trial haiku  results/task-...-b.json results/session-bb...json \
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
| `baseline` | Measure idle package power (subtracted from task runs) |
| `run-task TASK` | Run a task container under energy measurement |
| `list-sessions` | List Claude Code sessions on this machine |
| `analyze-session` | Token counts → energy band for a session (merge with repeated `--session-id`) |
| `report` | One task run vs. one coordination session |
| `compare` | Multiple model trials side by side + event identity |
| `verify-events` | Check LHE files for identical physics events |

Results land in `results/` as timestamped JSON, with raw power traces and
container logs kept per run for auditability.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `FAIL power backend 'powermetrics' — sudo requires a password` | no sudoers rule | setup step 3, or `sudo -v` immediately before a short run |
| `FAIL power backend 'powermetrics' — powermetrics binary not found` | not macOS | pass `--backend rapl` (bare metal, root) or `--backend tdp-model` (estimate) |
| `FAIL docker daemon reachable` | Docker not started | `open -a Docker`, or `colima start` |
| `image ... EMULATED, energy will be distorted` | amd64 image on Apple Silicon | drop `--image-variant scailfin`; the default `native` variant builds for the host |
| `no baseline for this machine in results/` | no baseline recorded on this host (matched by chip + hostname) | run `llm-energy baseline` first, or pass `--baseline none` |
| `no sessions found (is ~/.claude/projects present?)` | Claude Code hasn't run locally, or transcripts live elsewhere | coordinate in the local CLI (web sessions leave no local transcript); set `CLAUDE_CONFIG_DIR` if your config dir is non-standard |
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
