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
#   --allow-all-tools    let the headless agent use tools without prompting
#                        (it will install software and run containers as you)
#   --baseline-seconds N idle baseline duration (default: 120)
#   --skip-baseline      reuse the newest baseline for this machine
#   --backend NAME       powermetrics | rapl | tdp-model (default: auto)
#
set -euo pipefail

TASK=madgraph-ttbar-lhe
MODEL=""
LABEL=""
INTERACTIVE=0
ALLOW_TOOLS=0
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
    --allow-all-tools)   ALLOW_TOOLS=1; shift ;;
    --baseline-seconds)  BASELINE_SECONDS=${2:?--baseline-seconds needs a value}; shift 2 ;;
    --skip-baseline)     SKIP_BASELINE=1; shift ;;
    --backend)           BACKEND=${2:?--backend needs a value}; shift 2 ;;
    -h|--help)           sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                   die "unknown option: $1 (try --help)" ;;
  esac
done

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

BRIEF="tasks/$TASK/BRIEF.md"
[ -f "$BRIEF" ] || die "no brief at $BRIEF — the agent needs one to follow"
command -v claude >/dev/null || die "the 'claude' CLI is not on PATH"

# An "open" task states the goal and nothing else: the agent works out the
# method itself. That only measures anything if it cannot read the answer, so
# an open run happens in a scratch workspace *outside* this repo — where
# tasks/madgraph-ttbar-lhe/cards/ttbar_lhe.mg5 is a complete worked solution
# that any competent agent would rightly find and use.
OPEN=0
[ -f "tasks/$TASK/spec.yaml" ] && OPEN=1

BACKEND_ARGS=()
[ -n "$BACKEND" ] && BACKEND_ARGS=(--backend "$BACKEND")
LABEL=${LABEL:-${MODEL:-run}}

# macOS ships bash 3.2, where "${arr[@]}" on an EMPTY array trips `set -u`
# with "unbound variable". Every BACKEND_ARGS use below goes through the
# ${arr[@]+...} guard so the script survives its own primary platform.

# 1. Preflight ---------------------------------------------------------------
step "Preflight"
uv run llm-energy doctor || die "doctor failed — fix the above before measuring"

# 2. Image ------------------------------------------------------------------
# Must happen BEFORE the session starts. run-task would otherwise build the
# image inside the measured window, charging a 10-20 minute build to the
# agent's coordination energy.
#
# An open task has no harness-provided image: obtaining a generator is part of
# what the agent has to work out, and whatever it pulls or builds is measured
# along with everything else it does.
if [ "$OPEN" -eq 1 ]; then
  step "Open task — no image is provided; the agent sources its own"
else
step "Ensuring the task image exists"
# Goes through the harness's own ensure_image so build- and pull-type variants
# are both handled exactly as run-task would handle them. The task name is
# passed as argv, not interpolated into the source.
uv run python - "$TASK" <<'PY'
import sys
from pathlib import Path
from llm_energy.config import find_task
from llm_energy.docker_util import ensure_image, image_exists

image = find_task(sys.argv[1], Path("tasks")).image()
if image_exists(image.tag):
    print(f"{image.tag} already present")
else:
    print(f"preparing {image.tag} (excluded from the measurement)")
    ensure_image(image)
PY
fi

# 3. Baseline ----------------------------------------------------------------
if [ "$SKIP_BASELINE" -eq 1 ]; then
  step "Skipping baseline (reusing the newest one for this machine)"
  # A pinned task's run-task defaults to --baseline latest; without a matching
  # baseline it fails mid-session, wasting the whole measured run. Check now,
  # before any tokens are spent.
  uv run python - <<'PY' || die "no baseline recorded for this machine — drop --skip-baseline"
import sys
from pathlib import Path
from llm_energy import machine_info
from llm_energy.baseline import find_latest_baseline

found = find_latest_baseline(Path("results"),
                             machine_info.collect(include_docker=False))
print(f"reusing {found}" if found else "", end="")
sys.exit(0 if found else 1)
PY
  echo
else
  step "Idle baseline (${BASELINE_SECONDS}s) — leave the machine alone"
  uv run llm-energy baseline --duration "$BASELINE_SECONDS" \
    "${BACKEND_ARGS[@]+"${BACKEND_ARGS[@]}"}"
fi

# 4. The measured session ----------------------------------------------------
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
CWD_ARGS=()
if [ "$OPEN" -eq 1 ]; then
  # Outside the repo on purpose. Inside it, the agent can read a worked
  # solution and the harness's own instruments, and the measurement collapses
  # back to "cost of running a known command".
  WORKSPACE="${LLM_ENERGY_WORKSPACE_ROOT:-$HOME/llm-energy-workspaces}/$TASK-$STAMP"
  mkdir -p "$WORKSPACE"
  cp "$BRIEF" "$WORKSPACE/BRIEF.md"
  CWD_ARGS=(--cwd "$WORKSPACE")
  PROMPT="Read BRIEF.md and do the job it describes."
  echo "workspace: $WORKSPACE (contains only BRIEF.md)"
else
  WORKSPACE=""
  PROMPT="Read $BRIEF and do the job it describes."
fi

CLAUDE_ARGS=()
[ -n "$MODEL" ] && CLAUDE_ARGS+=(--model "$MODEL")
if [ "$INTERACTIVE" -eq 1 ]; then
  CLAUDE_ARGS+=("$PROMPT")
else
  # A headless session has nobody to approve tool use, so every Bash and Write
  # call is denied: the agent talks for a few minutes and produces nothing,
  # which still costs a full measurement run. Refuse to start rather than
  # collect that.
  if [ "$ALLOW_TOOLS" -eq 0 ]; then
    die "a headless run cannot use tools, so it would produce nothing.
  Either --interactive (approve tool calls yourself), or --allow-all-tools
  (the agent runs commands, installs software and starts containers as you,
  unattended, in $WORKSPACE — only do this on a machine you are willing to
  hand over)."
  fi
  CLAUDE_ARGS+=(--dangerously-skip-permissions -p "$PROMPT")
fi

step "Measured coordination session${MODEL:+ (model: $MODEL)}"
uv run llm-energy measure-session "${BACKEND_ARGS[@]+"${BACKEND_ARGS[@]}"}" \
  "${CWD_ARGS[@]+"${CWD_ARGS[@]}"}" -- claude "${CLAUDE_ARGS[@]}"

# 5. Pair the artifacts ------------------------------------------------------
step "Pairing results"
# `|| die` on this assignment would be dead code: the pipeline's status is
# head's, which is 0 even when ls finds nothing. Test the value instead.
SESSION_POWER=$(ls -t results/power-session-*.json 2>/dev/null | head -1)
[ -n "$SESSION_POWER" ] || die "no session-power result was written"

SESSION_ENERGY="results/session-energy-$(basename "$SESSION_POWER" .json).json"
REPORT="results/report-$LABEL-$(basename "$SESSION_POWER" .json).md"

if [ "$OPEN" -eq 1 ]; then
  # Nothing was launched by the harness, so there is no task result to pair
  # against; the session-power result carries the sessions it observed.
  uv run llm-energy analyze-session --for-session-power "$SESSION_POWER" \
                                    --out-file "$SESSION_ENERGY"

  if ! grep -q '"containers": \[[^]]' "$SESSION_POWER" 2>/dev/null; then
    echo
    echo "note: the agent started no containers at all. If the deliverables are"
    echo "      also missing, it never got as far as running anything — check"
    echo "      the session output above before trusting these energy figures"
    echo "      as a measurement of doing the task."
  fi

  step "Deliverable"
  # Not fatal: a run that burned tokens and produced nothing usable is a
  # result, and the energy figures for it are still valid.
  if uv run llm-energy verify-deliverable "$WORKSPACE" --task "$TASK"; then
    DELIVERED="met the specification"
  else
    DELIVERED="DID NOT meet the specification"
  fi

  step "Report"
  uv run llm-energy report-open "$SESSION_POWER" "$SESSION_ENERGY" \
                                --workspace "$WORKSPACE" --task "$TASK" \
                                --md "$REPORT"
  cat <<EOF

Artifacts for this run:
  workspace      $WORKSPACE
  deliverable    $DELIVERED
  session tokens $SESSION_ENERGY
  session power  $SESSION_POWER
  report         $REPORT
EOF
else
  TASK_RESULT=$(uv run llm-energy find-task-result "$SESSION_POWER") \
    || die "could not find the task run for this session (see above)"

  uv run llm-energy analyze-session --for-task "$TASK_RESULT" \
                                    --out-file "$SESSION_ENERGY"

  step "Report"
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
fi
