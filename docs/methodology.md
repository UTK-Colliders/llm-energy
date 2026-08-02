# Methodology

## Question and scope

For common HEP research workflow tasks, how much energy does an LLM consume
*coordinating* the task, compared to the energy consumed by *running* the task
itself? The first benchmark task is MadGraph5_aMC@NLO generating 10,000
`p p > t t~ j j` events at √s = 13.6 TeV at leading order, producing unweighted
LHE files, in a Docker container on a dedicated Mac.

Three quantities are kept in deliberately separate pipelines that only meet at
the report stage:

- **E_task** — *measured*: Apple Silicon SoC package power integrated over the
  container's wall-time window, optionally net of an idle baseline.
- **E_coord,local** — *measured*: the same package power integrated over the
  whole coordination session, minus the nested task run. This is what the
  agent costs *this* machine while it works.
- **E_LLM** — *estimated*: token counts from the interactive Claude Code
  session that coordinated the task, converted to Joules through
  literature-derived coefficients, reported as a low/central/high envelope
  including a PUE factor.

This is knowingly an apples-to-oranges comparison in dimensions (local package
energy vs. estimated remote datacenter energy); the report states this and the
envelope is constructed to bound the answer rather than pin it.

### Relation to prior work

To our knowledge (literature search 2026-08-01) no published study makes this
direct comparison, in HEP or elsewhere. The two halves exist separately:

- LLM agents driving MadGraph pipelines, without energy accounting:
  MadAgents (arXiv:2601.21015), HEPTAPOD (arXiv:2512.15867), Collider-Bench
  (arXiv:2605.13950), SMEFT-Pheno-Agent (arXiv:2607.22331), ArgoLOOM
  (arXiv:2510.02426).
- HEP task-energy measurement, without an LLM angle: "Watts per event"
  HIJING++ benchmarking (arXiv:2607.05018), whose energy-per-event unit we
  adopt; the ATLAS computing environmental impact study (Eur. Phys. J. C 85,
  1397 (2025)); WLCG/HEPScore23 power-aware benchmarking.
- Nearest conceptual neighbor: "Energy per Successful Goal" (arXiv:2605.22883)
  measures agentic-vs-linear LLM overhead but not LLM-vs-external-task energy.

## Task energy (E_task)

### Instrument

`powermetrics --samplers cpu_power` on macOS, run via `sudo -n`, sampling at
1 Hz (configurable), writing to a raw text file that is retained alongside the
result JSON as an audit artifact. We parse the plain-text output rather than
`--format plist`: the text lines have been stable across macOS versions and
are trivially fixture-testable.

Per sample block we read the actual elapsed window from the header
(`(1003.42ms elapsed)`) and the `Combined Power (CPU + GPU + ANE)` line
(falling back to summing the `CPU Power`/`GPU Power`/`ANE Power` lines if the
combined line is absent; the result records which path was used).

### What package power includes and excludes

powermetrics reports SoC package power: CPU clusters, GPU, and the Apple
Neural Engine. It **excludes** part of DRAM, SSD, display, networking, and
power-supply conversion losses. E_task is therefore a lower bound on
wall-plug energy. On Apple Silicon the package dominates compute-induced
draw, and the idle-baseline subtraction removes most of the constant terms,
but the report always labels the quantity as package energy.

### Integration and alignment

Energy is the rectangle-rule sum `E = Σ P_i × Δt_i` over sample windows,
which is exact given the samples since windows tile the trace. Sampling
starts ~2 intervals before the container launches and stops ~1 interval
after it exits; the integral is then restricted to the container's wall-time
window (matched via monotonic timestamps). Residual skew between the
powermetrics clock and the harness clock is at most about one interval and is
reported as `alignment_uncertainty_j ≈ mean_W × interval`.

### Baseline

`llm-energy baseline` measures idle package power (default 30 s) with the
machine quiesced **and the Docker VM running but idle** — Docker Desktop's VM
has nonzero idle draw that belongs in the subtracted baseline, not in the
task. The tool warns if Docker is not running during a baseline. Net energy
is `E_net = E_gross − P̄_baseline × t_wall`. Both gross and net are reported;
the baseline's standard deviation is recorded so noisy baselines are visible —
which matters more at the 30 s default than it did at 120 s, since a shorter
window averages over fewer samples. Lengthen it with `--duration` if the
recorded std is a large fraction of the mean.
`run-task --baseline latest` only accepts baselines recorded on the same
machine (chip + hostname match).

### Docker on macOS

Containers on macOS run inside a lightweight Linux VM. Two consequences:

1. **VM overhead is part of E_task.** The VM's marginal activity during the
   run is genuinely caused by the task; its idle draw is removed by the
   baseline.
2. **Architecture matters.** The community `scailfin/madgraph5-amc-nlo`
   images are linux/amd64 only and would run under Rosetta 2 emulation on
   Apple Silicon, distorting energy. The primary image is therefore built
   natively for the host architecture from `tasks/madgraph-ttbar2j-lhe/
   Dockerfile` (MG5_aMC v3.5.16, python:3.11-slim base). Any run whose image
   architecture differs from the host is flagged `emulated: true` and loudly
   warned about.

Container CPU time is sampled from the container cgroup
(`cpu.stat usage_usec`, v1 fallback `cpuacct.usage`) via `docker exec` during
the run — it must be captured before the `--rm` container vanishes — plus
periodic `docker stats` snapshots as diagnostics.

### MadGraph specifics

- Process: `generate p p > t t~ j j` at LO — ttbar with two additional hard
  partons, a substantially larger calculation than plain ttbar in both code
  generation and unweighting, which is why the task timeout is 8 hours.
  Grading checks the topology (one top, one antitop, exactly two partons of
  any light flavour) rather than an exact final state, since the jet flavours
  differ event by event. The reconstructed-mass window is widened to +/- 20
  GeV because two extra hard jets worsen the wrong-pairing combinatorics.
- `nevents = 10000`,
  `ebeam1 = ebeam2 = 6800` GeV; output gzipped unweighted LHE.
- `iseed = 42` is pinned so repeat runs (and runs coordinated by different
  LLMs on the same image) must produce identical events.
- The image warms up MG5's one-time setup at build time (a trivial
  `e+ e- > mu+ mu-` run), so measured runs exclude first-use tool setup. The
  ttbar process's own code generation and Fortran compilation stay **inside**
  the measured run: they are part of performing the task. The
  `madgraph-ttbar2j-split` task measures them separately — see below.

## Splitting compilation from execution

`madgraph-ttbar2j-lhe` bills one number for work of two very different kinds:
`launch` compiles the generated Fortran and *then* generates events. The
`madgraph-ttbar2j-split` task runs the same physics — same process, beams,
event count, and seed — as two separately measured phases:

| Phase | Contents |
|---|---|
| `codegen-compile` | `generate` + `output` write the process directory as Fortran, then `make` builds it |
| `event-generation` | `launch` on the pre-built directory |

### Mechanics

Each phase runs in its own container over a shared run directory, so phase 2
consumes phase 1's build. All phases are sampled by **one** power trace, with
each phase's energy integrated over its own container window — the same
carve-out used for the single-phase task, so phases and tasks stay directly
comparable. Task totals are the sum over phases by construction.

Forcing the compile into phase 1 requires driving `make` explicitly, because
`launch` would otherwise do it. The compile phase ends with an `ls` of the
expected `madevent` executables so a wrong make target fails loudly there
rather than silently pushing compilation into phase 2.

### Verifying the split rather than assuming it

A phase split is only meaningful if the compiling actually finished in the
compile phase. Every phase therefore counts the compiler output (`.o`, `.a`,
`.so`, `.mod`) written inside its own window, recorded as
`build_artifacts_written`. A clean split shows a large count in
`codegen-compile` and **zero** in `event-generation`; artifacts in more than
one phase are reported as a warning on the run, because the phase energies
are then not a clean separation.

### What the split does not claim

Event identity between `madgraph-ttbar2j-split` and `madgraph-ttbar2j-lhe` is an
empirical question, not an assumption: the same seed drives the same
generator, but the two reach madevent by different routes. Check it with
`llm-energy verify-events` rather than relying on it. The single-phase task is
unchanged, so results already collected under it remain comparable.
- MG5 version is pinned; the scailfin variant ships MG5 3.5.1, so event
  identity across image variants is *not* expected — only within a variant.

## What the agent is asked to do

The question is what an LLM spends *coordinating* a task, so what counts as
coordination decides what the study measures. Two task kinds bracket it.

A **pinned** task (`madgraph-ttbar2j-lhe`) hands the agent a working run card and
the command to run. Its session cost is the cost of executing a known
solution: reading a runbook, invoking one command, checking a number. That is
a floor, not the quantity of interest, and it barely separates one model from
another.

An **open** task (`madgraph-ttbar2j-open`) is a dozen lines written the way a
colleague would ask, and leaves the method to the agent. It is a three-step physics job: generate the
hard process in MadGraph, shower and hadronise it in Pythia 8, then
reconstruct the top quark and show its invariant mass peak. The agent must
work out that it needs both generators, how to obtain them, what cards to
write, which decay channel to target, how to build jets, how to resolve the
combinatorics, and how to plot the result. The difference between the two
tasks isolates the cost of solving.

The pinned task currently stops at the LHE file, so it is a control for step 1
only. Extending it to match would require Pythia 8 in the image and a tested
reconstruction script.

The brief's *register* is part of the design. A detailed specification
measures an agent's ability to follow a specification; a short informal
request measures what the study is actually about, which is an agent doing
research from the kind of instruction a physicist would really send. The
grader therefore infers what it needs — searching for artefacts by shape
rather than by dictated filename — so that the brief can stay casual without
the measurement becoming unverifiable.

### Isolation

An open run happens in a scratch workspace outside this repository, containing
only the brief. This is a measurement requirement, not tidiness:
`tasks/madgraph-ttbar2j-lhe/cards/ttbar2j_lhe.mg5` is a complete worked solution,
and an agent working in the repository could find it — correctly and
helpfully — collapsing the open task back into the pinned one. Isolation also
puts the harness's own commands out of reach, so an open session cannot
measure itself.

No image is provided either: obtaining a generator is part of the job.

### Measuring what the harness did not launch

With the method unpinned, the harness no longer starts the containers, so it
cannot time them directly. It records the Docker daemon's event stream for the
session and pairs container start/stop events into windows, regardless of who
started them. Energy is then attributed as:

- **E_compute** — integrated over the *union* of observed container windows,
  so concurrent containers are counted once and the figure stays subtractable;
- **E_coord,local** — the rest of the session window.

Limits, stated in every open report: work run outside a container lands in the
coordination bucket; a container still running when the session ends is
reported rather than attributed; and if the Docker event stream is
unavailable, the session total is still valid but the split is not made.

### Correctness as an outcome

When the agent chooses the method, the output can be wrong, so an open run's
energy is only meaningful beside a verdict on whether it delivered.
`verify-deliverable` checks the LHE payload — the `<init>` block's beam PDG ids
and energies, the event count, and the final-state PDG ids of every event —
rather than the MG5 banner, which is free text the agent could have produced
by any route. A sample generated unconventionally still passes; a plausible
banner over the wrong physics still fails.

### Seeding, and what exact comparison can mean

Every stochastic stage of the open task is pinned: the hard process (MadGraph,
seed 42), the shower (Pythia 8, seed 42), and any randomness the agent
introduces in reconstruction. The brief additionally requires that a rerun of
the agent's own pipeline reproduce byte-identical output, which is the only
check that catches an unseeded stage the brief did not anticipate — a
clock-seeded shower, or workers merged in completion order.

Seeding buys different things at different stages, and the comparison tool
keeps them apart:

- **Event samples must be identical** across agents. Same generator, same
  version, same seed leaves nothing free. A difference is a defect.
- **Histograms are not expected to be identical** across agents. The
  reconstruction method is the agent's to choose, so identical events
  legitimately yield different histograms; the spread of peak positions across
  methods is a result, not an error. Only a rerun of the *same* pipeline
  should reproduce the histogram exactly.

Generator versions confound this: the same seed in different MG5 versions
gives different events. The version is therefore read from the LHE banner and
reported alongside the hashes, and an unrecorded version is distinguished from
a mismatched one — unknown is not evidence of difference, and treating it as
such would blame a seed for a version's doing.

### Grading a reconstructed mass peak

The figure asked for in step 3 cannot be graded: relabel its axes and it looks
the same to any checker. The brief therefore also requires the histogram as
numbers (`bin_edges_gev`, `counts`), and the peak is judged from those.

Four criteria, all reported with their measured values:

- the histogram parses and its edges are monotonic;
- it has at least a few hundred entries, below which a "peak" is noise;
- the tallest bin is **interior** — a maximum in the first or last bin is a
  falling spectrum whose range never covered the mass region, which would
  otherwise pass a position check by accident;
- the peak sits within 172.5 ± 15 GeV, and rises at least 1.5× above the
  median bin.

The window is wide deliberately. This is a *reconstructed* mass, not a
generated one: jet clustering, wrong-pairing combinatorics and out-of-cone
losses shift the peak down and broaden it, by amounts that depend on choices
the agent is free to make. A peak outside the window indicates a broken
reconstruction rather than new physics, which is exactly the failure worth
catching. FWHM and prominence are recorded as information, not as pass/fail
thresholds beyond the minimum above.

Runs that fail are kept. A model that spends heavily and produces nothing
usable is a data point about that model, not an absent measurement.

## Local coordination energy (E_coord,local)

### Measurement

`llm-energy measure-session` wraps the agent process itself in the same
power measurement used for task runs: sampling starts ~2 intervals before the
child launches, stops ~1 interval after it exits, and the integral is
restricted to the child's wall-time window. The measured task run happens
*inside* that window, so

    E_coord,local = E_session − E_task

with both terms net of the idle baseline when both carry one, and gross
otherwise — the idle term must be removed from both windows or from neither.
The report flags three ways this subtraction can be invalid: the two runs
having used different power backends, the task window not lying inside the
session window, and a negative result.

This closes a gap the token-only pipeline leaves open. E_LLM covers estimated
*remote* inference; it says nothing about the laptop running hot for forty
minutes while the agent reads files and runs commands. E_coord,local is
measured on the same instrument as E_task, so those two are directly
comparable in a way E_LLM never is.

### The image build is excluded

Building the MG5 image takes 10–20 minutes. `run_task` builds before power
sampling starts, so it never lands in E_task — but under session-window
measurement an unbuilt image *would* land in E_coord,local, dwarfing it.
`scripts/measure-run.sh` therefore ensures the image exists before the session
begins.

### What is still excluded

Wall-plug power (the package excludes display, most DRAM, SSD, PSU losses),
the network path to the provider, and the researcher's own time. E_coord,local
is a lower bound on the local cost, in the same direction as E_task.

## LLM energy (E_LLM)

### Attributing a session to a run

Claude Code exports `CLAUDE_CODE_SESSION_ID` into every subprocess it spawns,
and its value is the transcript's filename stem. When the coordinating agent
invokes `run-task`, the harness records that id in the task result, so
`analyze-session --for-task` reads the exact conversation that drove the run.

This replaces selecting the newest transcript by mtime, which is wrong in a
way that does not announce itself: running the analysis from a second Claude
session measures *that* session instead. `measure-session` cannot use the env
var — it starts the agent, so the child's id does not exist yet — and instead
attributes sessions whose transcript's first record falls inside the measured
window.

The protocol the harness assumes: the coordinating session is **fresh**, does
only the job described in `tasks/<name>/BRIEF.md`, and runs none of the
measurement commands. `CLAUDE.md` states those constraints to the agent. A
session that measures itself inflates E_LLM with the cost of measurement,
which is not what the study is asking about.

### Token accounting

The coordination happens in an interactive Claude Code session. Claude Code
writes transcripts as JSONL under `~/.claude/projects/<encoded-cwd>/
<session-id>.jsonl`; each assistant record carries `message.usage` with
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, and
`cache_read_input_tokens`, plus the model ID.

Parsing details that materially affect the numbers:

- **Streaming duplicates.** One API response (one `message.id`) appears as
  several JSONL records with cumulative usage; naive summation overcounts by
  2–3×. We deduplicate by `message.id`, keeping the record with the largest
  `output_tokens` (verified against real transcripts: 24 records → 9 unique
  messages in one observed session).
- **Sidechains.** Subagent (sidechain) records are *included* in token totals
  — subagent inference costs energy — but counted separately as
  `sidechain_turns`.
- Synthetic/internal models (`<synthetic>`) are skipped with a note.
- Several sessions can be merged into one "coordination episode"
  (`analyze-session --session-id A --session-id B`), since real work often
  spans a resumed or compacted session.

What token counts do *not* capture: server-side batching efficiency,
speculative decoding, draft models, retrieval infrastructure, and idle
capacity of the provider fleet (partially covered by the Google-style
disclosures used for the coefficients). Training energy is excluded by scope.

### Coefficients

`coefficients/default.yaml` maps tokens to Joules with low/central/high
values per token type, a per-request overhead, and a PUE factor applied to
models whose sources don't already include it. Anchors:

- **Epoch AI (Feb 2025)**, "How much energy does ChatGPT use?": ~0.3 Wh
  (1080 J) for a typical GPT-4o query with ~500-token response → ~2 J per
  output token; adopted as the central output-token coefficient.
- **Google (Aug 2025)** production disclosure: 0.24 Wh (864 J) median Gemini
  text prompt *including* datacenter overhead — consistent with the central
  value at typical response lengths.
- **arXiv:2607.26571** ("From Tokens to Watt-hours"): 0.0001–0.002 Wh
  (0.36–7.2 J) per output token measured across modern GPUs → our low/high
  rails.
- **Samsi et al., arXiv:2310.03003** ("From Words to Watts") and the
  **Mistral Large 2 lifecycle analysis (Jul 2025)** as corroboration.

Input (prefill) tokens are priced roughly an order of magnitude below output
tokens; cache reads roughly another order below input; cache creation is
priced like input processing. These splits are a modeling choice — providers
do not publish them — which is why results are only ever reported as a band.

- PUE: 1.2 central (Google fleet ~1.09; global average ~1.5–1.6, Uptime
  Institute 2024).
- The band is an **envelope** (low coefficients everywhere vs. high
  everywhere), not a statistical interval.
- Result JSONs record the SHA-256 of the coefficients file used, so numbers
  are reproducible as coefficients get revised.

## Model comparison and output verification

To compare different LLMs coordinating the same task, each trial (one
session + one task run) is labeled and fed to `llm-energy compare`, which
tabulates tokens, energy bands, task energy, and ratios per trial — and
verdicts whether the trials produced **identical physics output**:

- Task results automatically record an event fingerprint for LHE outputs:
  SHA-256 over whitespace-normalized `<event>` block content, ignoring
  headers (which contain timestamps, hostnames, and paths that legitimately
  differ).
- With `iseed` pinned and the same image, identical hashes are the expected
  outcome; a mismatch means a coordinating LLM changed the physics setup.
- If exact hashes differ, `compare`/`verify-events` re-compares numerically
  with a relative tolerance (default 1e-9) to distinguish "different last-ulp
  formatting" (e.g. cross-architecture runs) from genuinely different events.

## Reading the portions against each other

`llm-energy breakdown` assembles the artifacts a run produced into a single
budget: task phases (or, for an open run, the observed containers), the local
coordination term, and the estimated inference band.

Two rules govern it, both there to stop a number being read as something it
is not.

**Shares are computed only within the measured group.** Compilation, event
generation and local coordination are all SoC package energy on the same
instrument, so their proportions are meaningful. The inference term is
estimated remote datacenter energy including PUE; it is reported as a multiple
of the measured total, never as a share of it, and never added into that
total. A single pie over both would imply a common footing that does not
exist.

**The figure is a dot plot on a log axis.** The portions routinely differ by
two orders of magnitude, which forces a log scale; a bar on a log axis has no
zero to grow from, so its length would encode nothing while still reading as a
proportion. Position carries the value instead. The estimated term is drawn as
its low-central-high range with an open marker — a deliberately different mark
from the measured points.

## Comparability caveats (restated in every report)

1. E_task is SoC package energy; E_LLM is estimated total datacenter energy
   including PUE — the LLM side is charged for overheads the task side is
   not. This biases the comparison *against* the LLM, which is the
   conservative direction for the study's question. E_coord,local is the one
   term measured on the same footing as E_task.
2. The E_LLM band is a modeling envelope, not a confidence interval.
3. Embodied energy and training energy are excluded on both sides.
4. Runs under emulation are flagged and should not be used for headline
   numbers.
5. Networking, cooling of the local machine, and the researcher's own time
   and equipment are out of scope.

## Reproducibility

- Every result JSON: `schema_version`, `tool_version`, timestamp, machine
  metadata (chip, OS, docker info), and the raw power trace path.
- Coefficients file hash recorded in session results.
- MG5 version and seed pinned in the task definition; images tagged by
  version.
- Raw powermetrics traces and container logs are kept in per-run directories
  under `results/`.
