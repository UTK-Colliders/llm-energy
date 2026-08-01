#!/usr/bin/env bash
#
# One measured run, start to finish:
#
#   preflight -> pre-build image -> idle baseline -> fresh agent session
#   (measured) -> pair artifacts -> report
#
# The agent session is what gets measured: its tokens become E_LLM, and this
# machine's package power over the session window becomes E_coord,local, with
# the task container's own energy nested inside.
#
# Usage:  scripts/measure-run.sh [options]
#   --task NAME          task under tasks/ (default: madgraph-ttbar-lhe)
#   --model NAME         pass --model to claude (for cross-model comparison)
#   --label NAME         label for the report (default: the model, else "run")
#   --interactive        supervise the session instead of running headless
#   --baseline-seconds N idle baseline duration (default: 120)
#   --skip-baseline      reuse the newest baseline for this machine
#   --backend NAME       powermetrics | rapl | tdp-model (default: auto)
#
set -euo pipefail

TASK=madgraph-ttbar-lhe
MODEL=""
LABEL=""
INTERACTIVE=0
BASELINE_SECONDS=120
SKIP_BASELINE=0
BACKEND=""

die() { printf 'measure-run: %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --task)              TASK=${2:?--task needs a value}; shift 2 ;;
    --model)             MODEL=${2:?--model needs a value}; shift 2 ;;
    --label)             LABEL=${2:?--label needs a value}; shift 2 ;;
    --interactive)       INTERACTIVE=1; shift ;;
    --baseline-seconds)  BASELINE_SECONDS=${2:?--baseline-seconds needs a value}; shift 2 ;;
    --skip-baseline)     SKIP_BASELINE=1; shift ;;
    --backend)           BACKEND=${2:?--backend needs a value}; shift 2 ;;
    -h|--help)           sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                   die "unknown option: $1 (try --help)" ;;
  esac
done

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

BRIEF="tasks/$TASK/BRIEF.md"
[ -f "$BRIEF" ] || die "no brief at $BRIEF — the agent needs one to follow"
command -v claude >/dev/null || die "the 'claude' CLI is not on PATH"

BACKEND_ARGS=()
[ -n "$BACKEND" ] && BACKEND_ARGS=(--backend "$BACKEND")
LABEL=${LABEL:-${MODEL:-run}}

# 1. Preflight ---------------------------------------------------------------
step "Preflight"
uv run llm-energy doctor || die "doctor failed — fix the above before measuring"

# 2. Image ------------------------------------------------------------------
# Must happen BEFORE the session starts. run-task would otherwise build the
# image inside the measured window, charging a 10-20 minute build to the
# agent's coordination energy.
step "Ensuring the task image exists"
IMAGE_TAG=$(uv run python -c "
from pathlib import Path
from llm_energy.config import find_task
print(find_task('$TASK', Path('tasks')).image().tag)
")
if docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
  echo "$IMAGE_TAG already built"
else
  echo "building $IMAGE_TAG (this is excluded from the measurement)"
  docker build -t "$IMAGE_TAG" "tasks/$TASK"
fi

# 3. Baseline ----------------------------------------------------------------
if [ "$SKIP_BASELINE" -eq 1 ]; then
  step "Skipping baseline (reusing the newest one for this machine)"
else
  step "Idle baseline (${BASELINE_SECONDS}s) — leave the machine alone"
  uv run llm-energy baseline --duration "$BASELINE_SECONDS" "${BACKEND_ARGS[@]}"
fi

# 4. The measured session ----------------------------------------------------
PROMPT="Read $BRIEF and do the job it describes."
CLAUDE_ARGS=()
[ -n "$MODEL" ] && CLAUDE_ARGS+=(--model "$MODEL")
if [ "$INTERACTIVE" -eq 1 ]; then
  CLAUDE_ARGS+=("$PROMPT")
else
  CLAUDE_ARGS+=(-p "$PROMPT")
fi

step "Measured coordination session${MODEL:+ (model: $MODEL)}"
uv run llm-energy measure-session "${BACKEND_ARGS[@]}" -- claude "${CLAUDE_ARGS[@]}"

# 5. Pair the artifacts ------------------------------------------------------
step "Pairing results"
SESSION_POWER=$(ls -t results/power-session-*.json 2>/dev/null | head -1) \
  || die "no session-power result was written"
[ -n "$SESSION_POWER" ] || die "no session-power result was written"

TASK_RESULT=$(uv run llm-energy find-task-result "$SESSION_POWER") \
  || die "could not find the task run for this session (see above)"

SESSION_ENERGY="results/session-energy-$(basename "$SESSION_POWER" .json).json"
uv run llm-energy analyze-session --for-task "$TASK_RESULT" \
                                  --out-file "$SESSION_ENERGY"

# 6. Report ------------------------------------------------------------------
step "Report"
REPORT="results/report-$LABEL-$(basename "$SESSION_POWER" .json).md"
uv run llm-energy report "$TASK_RESULT" "$SESSION_ENERGY" \
                         --session-power "$SESSION_POWER" \
                         --md "$REPORT"

cat <<EOF

Artifacts for this run:
  task run       $TASK_RESULT
  session tokens $SESSION_ENERGY
  session power  $SESSION_POWER
  report         $REPORT

Compare against another model's run with:
  uv run llm-energy compare --trial $LABEL $TASK_RESULT $SESSION_ENERGY \\
                            --trial <other> <task.json> <session.json>
EOF
