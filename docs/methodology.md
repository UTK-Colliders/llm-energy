# Methodology

## Question and scope

For common HEP research workflow tasks, how much energy does an LLM consume
*coordinating* the task, compared to the energy consumed by *running* the task
itself? The first benchmark task is MadGraph5_aMC@NLO generating 10,000
`p p > t t~` events at √s = 13.6 TeV at leading order, producing unweighted
LHE files, in a Docker container on a dedicated Mac.

Two quantities are kept in deliberately separate pipelines that only meet at
the report stage:

- **E_task** — *measured*: Apple Silicon SoC package power integrated over the
  container's wall-time window, optionally net of an idle baseline.
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

`llm-energy baseline` measures idle package power (default 120 s) with the
machine quiesced **and the Docker VM running but idle** — Docker Desktop's VM
has nonzero idle draw that belongs in the subtracted baseline, not in the
task. The tool warns if Docker is not running during a baseline. Net energy
is `E_net = E_gross − P̄_baseline × t_wall`. Both gross and net are reported;
the baseline's standard deviation is recorded so noisy baselines are visible.
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
   natively for the host architecture from `tasks/madgraph-ttbar-lhe/
   Dockerfile` (MG5_aMC v3.5.16, python:3.11-slim base). Any run whose image
   architecture differs from the host is flagged `emulated: true` and loudly
   warned about.

Container CPU time is sampled from the container cgroup
(`cpu.stat usage_usec`, v1 fallback `cpuacct.usage`) via `docker exec` during
the run — it must be captured before the `--rm` container vanishes — plus
periodic `docker stats` snapshots as diagnostics.

### MadGraph specifics

- Process: `generate p p > t t~` at LO; `nevents = 10000`,
  `ebeam1 = ebeam2 = 6800` GeV; output gzipped unweighted LHE.
- `iseed = 42` is pinned so repeat runs (and runs coordinated by different
  LLMs on the same image) must produce identical events.
- The image warms up MG5's one-time setup at build time (a trivial
  `e+ e- > mu+ mu-` run), so measured runs exclude first-use tool setup. The
  ttbar process's own code generation and Fortran compilation stay **inside**
  the measured run: they are part of performing the task. A variant with a
  pre-generated process directory would isolate pure event generation and is
  an easy follow-up if wanted.
- MG5 version is pinned; the scailfin variant ships MG5 3.5.1, so event
  identity across image variants is *not* expected — only within a variant.

## LLM energy (E_LLM)

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

## Comparability caveats (restated in every report)

1. E_task is SoC package energy; E_LLM is estimated total datacenter energy
   including PUE — the LLM side is charged for overheads the task side is
   not. This biases the comparison *against* the LLM, which is the
   conservative direction for the study's question.
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
