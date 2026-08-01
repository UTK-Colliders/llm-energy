# Task brief: madgraph-ttbar-lhe

You are the coordinating agent for a measured run. Everything you do from here
until you finish is the experiment: your tokens are counted, and this machine's
power draw is being recorded.

Work efficiently, but do the job properly — an efficient run that produces the
wrong events is a failed measurement, not a cheap one.

## The job

Generate a leading-order MadGraph5_aMC@NLO event sample and confirm it is
correct:

| Property | Required value |
|---|---|
| Process | `p p > t t~` |
| Order | LO |
| Beam energy | 6800 GeV per beam (√s = 13.6 TeV) |
| Events | 10,000, unweighted |
| Random seed | 42 |
| Output | LHE, gzipped |

The task definition in this directory already encodes all of it —
`task.yaml` for the container and `cards/ttbar_lhe.mg5` for the physics. Treat
those as the specification, not as something to improve.

## Steps

1. Get oriented: read `task.yaml` and `cards/ttbar_lhe.mg5` and check they
   match the table above. If they don't, stop and tell the operator — do not
   edit them.
2. Run the task:

   ```sh
   uv run llm-energy run-task madgraph-ttbar-lhe
   ```

   This takes a while. It prints gross/net Joules, the wall time, and the path
   to the result JSON. If the image is missing it builds first, outside the
   measured window.
3. Verify the output. The run directory printed in the result contains
   `proc_pp_ttbar/Events/run_01/unweighted_events.lhe.gz`. Confirm:
   - the command exited 0;
   - the result JSON's `lhe_file_nevents` is 10000;
   - the beam energies and seed in the LHE header match the table.
4. Report back to the operator in a few lines: the event count, the wall time,
   the energy figure, the path to the result JSON, and anything that looked
   wrong.

Then stop. You are done.

## If the run fails

Read `container.log` in the run directory and diagnose it. Fixing a broken
*environment* is in scope — a missing image, a stopped Docker daemon, a full
disk. Changing the *physics* is not. If the job cannot be completed without
altering the run card, the task definition, or the seed, stop and explain why.

Report the failure honestly. A failed run that gets reported as a success
poisons the dataset it lands in.

## Out of scope

Do not run `baseline`, `analyze-session`, `measure-session`, `report`, or
`compare` — those belong to the operator, and running them here charges the
measurement for the cost of measuring itself. Do not commit anything.

---

*Note for whoever maintains this brief: the job is deliberately fully
specified, because the seed is pinned so that runs coordinated by different
models yield byte-identical events — that identity check is what makes a
cross-model comparison meaningful. If you instead want to measure heavier
coordination, write a second brief that states only the physics goal and lets
the agent derive the setup; expect event identity to stop holding, and drop
that check from the comparison.*
