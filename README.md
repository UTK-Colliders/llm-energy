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

## Install

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/), plus Docker
Desktop (or colima) on the measurement Mac.

```sh
git clone https://github.com/UTK-Colliders/llm-energy
cd llm-energy
uv sync
uv run llm-energy doctor        # checks powermetrics/sudo, docker, task, coefficients
```

`powermetrics` needs password-less sudo for unattended runs — see
[scripts/sudoers-powermetrics.md](scripts/sudoers-powermetrics.md).

## Workflow on the measurement Mac

```sh
# 1. Idle baseline (~2 min). Quiesce the machine; leave Docker running idle.
uv run llm-energy baseline --duration 120

# 2. Do the task interactively in Claude Code (this is the coordination
#    being measured), ending with the LLM invoking the measured run:
uv run llm-energy run-task madgraph-ttbar-lhe
# First run builds the native MG5 image (~10-20 min, unmeasured); the
# measured container run follows.

# 3. Convert the coordination session's tokens to an energy band:
uv run llm-energy analyze-session --latest

# 4. Compare:
uv run llm-energy report results/task-madgraph-ttbar-lhe-<ts>.json \
                         results/session-<id>-<ts>.json \
                         --md results/report.md
```

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
uv run llm-energy verify-events runA/unweighted_events.lhe.gz \
                                runB/unweighted_events.lhe.gz
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

## Notes

- The primary MadGraph image is built natively for the host architecture;
  the amd64-only community image (`scailfin/madgraph5-amc-nlo`) is available
  as `--image-variant scailfin` but runs emulated on Apple Silicon and is
  flagged as such — don't use it for headline numbers.
- On Linux, a RAPL backend (bare metal, root) or a clearly-labeled
  CPU-utilization × TDP estimate backend keeps the pipeline testable; the
  Mac path is primary.
- Dev: `uv run pytest` — all parsers are pure functions tested from fixtures.
