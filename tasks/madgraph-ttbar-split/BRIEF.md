# Task brief: madgraph-ttbar-split

You are the coordinating agent for a measured run. Everything you do from here
until you finish is the experiment: your tokens are counted, and this machine's
power draw is being recorded.

Work efficiently, but do the job properly — an efficient run that produces the
wrong events is a failed measurement, not a cheap one.

## The job

Same physics as `madgraph-ttbar-lhe`, run in two separately measured phases so
that compilation and event generation get their own energy figures:

| Property | Required value |
|---|---|
| Process | `p p > t t~` |
| Order | LO |
| Beam energy | 6800 GeV per beam (√s = 13.6 TeV) |
| Events | 10,000, unweighted |
| Random seed | 42 |
| Output | LHE, gzipped |

| Phase | What it covers |
|---|---|
| `codegen-compile` | MG5 writes the Fortran process directory, then `make` builds it |
| `event-generation` | madevent runs on the pre-built directory |

`task.yaml` and the two cards in this directory already encode all of it.
Treat them as the specification, not as something to improve.

## Steps

1. Get oriented: read `task.yaml` and both cards in `cards/`, and check they
   match the tables above. If they don't, stop and tell the operator — do not
   edit them.
2. Run the task:

   ```sh
   uv run llm-energy run-task madgraph-ttbar-split
   ```

   This takes a while. It prints a line per phase — energy, share, and how
   many build artifacts that phase wrote — then the totals and the result
   path. If the image is missing it builds first, outside the measured window.
3. Verify the output. The run directory printed in the result contains
   `proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz`. Confirm:
   - both phases exited 0;
   - the result JSON's `lhe_file_nevents` is 10000;
   - the beam energies and seed in the LHE header match the table;
   - **`event-generation` wrote zero build artifacts.** A non-zero count there
     means compilation leaked into the event-generation window and the two
     energy figures are not a clean split. Report it — do not try to fix the
     phase commands yourself.
4. Report back to the operator in a few lines: the per-phase energies and
   their shares, the event count, the total wall time, the path to the result
   JSON, and anything that looked wrong.

Then stop. You are done.

## If the run fails

Read `container.log` in the run directory — it has a `===== <command> =====`
banner per phase, so you can tell which one failed. Diagnose it. Fixing a
broken *environment* is in scope: a missing image, a stopped Docker daemon, a
full disk. Changing the *physics*, the phase split, or the make targets is
not. If the job cannot be completed without altering those, stop and explain
why.

A likely failure is the compile phase's `ls` check at the end: it means `make`
did not produce the `madevent` executables, so the build targets need
revisiting. That is the operator's call, not yours — report it.

Report the failure honestly. A failed run that gets reported as a success
poisons the dataset it lands in.

## Out of scope

Do not run `baseline`, `analyze-session`, `measure-session`, `report`, or
`compare` — those belong to the operator, and running them here charges the
measurement for the cost of measuring itself. Do not commit anything.
