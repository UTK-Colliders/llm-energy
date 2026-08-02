# llm-energy

Measures how much energy an LLM spends *coordinating* a HEP workflow task
against the energy of *running* it. See `README.md` for the operator workflow
and `docs/methodology.md` for the assumptions.

## Which job are you doing?

A session in this repo is one of two things. Work out which before acting.

**1. You are the measured coordinating agent.** The operator started you to
*do* a task — generating events, producing a physics result. Your token usage
and this machine's power draw are the experiment. Read
`tasks/<task-name>/BRIEF.md` and follow it.

The measurement is only valid if you stay inside the job:

- Do **not** run `baseline`, `analyze-session`, `measure-session`, `report`,
  or `compare`. Those are the operator's instruments. Running them makes the
  session partly a measurement of itself.
- Do **not** read `docs/methodology.md`, `coefficients/`, or the measurement
  sections of `README.md` unless the brief sends you there. Every file you
  read is tokens charged to the job.
- Do **not** edit the run card or any pinned physics parameter of a task whose
  brief hands you one. Seeds are fixed so different models' runs produce
  comparable events; changing them destroys the comparison.
- Do **not** commit anything. The operator handles version control.

**2. You are developing the harness** — changing code under `src/`, tests,
docs, or task definitions. Normal work; the rest of this file applies.

If it is ambiguous, ask. Guessing wrong either wastes a measurement run or
silently corrupts one.

### A note on open tasks

Some tasks state only the goal and leave the method to the agent — that is the
point of them, and the token cost of working it out *is* the measurement. An
open task runs in a scratch workspace outside this repo, containing nothing
but its brief, so a session reading this file is not on one. If you find
yourself in a bare directory with a `BRIEF.md` and no repo around it, the
brief is the whole of your instructions.

This also means: **do not add worked solutions for open tasks to this repo.**
`tasks/madgraph-ttbar2j-lhe/cards/ttbar2j_lhe.mg5` is a complete answer to the
MadGraph problem. Anything similar left where an open run could reach it turns
the experiment back into a reading-comprehension test.

## Layout

| Path | What's there |
|---|---|
| `src/llm_energy/power/` | power backends: `powermetrics` (macOS, primary), `rapl`, `tdp-model` |
| `src/llm_energy/session/` | transcript location, parsing, token → Joules |
| `src/llm_energy/session_power.py` | package power across a whole coordination session |
| `src/llm_energy/task_runner.py` | measured container run |
| `src/llm_energy/report.py` | single-trial report and multi-model comparison |
| `tasks/<name>/` | `task.yaml`, `BRIEF.md`, Dockerfile, cards |
| `coefficients/` | token → Joule coefficients, versioned and hashed into results |

## Conventions

- `uv run <cmd>`; `uv sync` after dependency changes. Never `pip install`.
- `uv run pytest` — parsers are pure functions tested from fixtures in
  `tests/fixtures/`. New parsing behaviour needs a fixture, not a live capture.
- Results are append-only JSON under `results/` (gitignored). Never edit a
  result by hand; regenerate it.
- Energy figures always carry their basis: gross vs. net of baseline, measured
  vs. estimated. Don't add a number to a report without labelling which it is.
- Estimates are reported as low/central/high bands, never a single figure.
