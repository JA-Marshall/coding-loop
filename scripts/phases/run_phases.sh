#!/usr/bin/env bash
# Coordinator for a phased build: one fresh headless Claude session per phase prompt, in order,
# then an independent review of the phase's pull request. Blocking findings go back to a fresh
# correction session on the same branch, at most MAX_CORRECTIONS times. Nothing is merged here:
# the owner merges after reading the review, so the loop stops after each phase that leaves a
# PR open. A phase counts as done when its row in STATUS_FILE on BASE_BRANCH says "done".
#
# Usage: ./run_phases.sh run           every phase in PHASES not yet done (default when no command)
#        ./run_phases.sh status        one line per phase, plus the lock and STOP file
#        ./run_phases.sh stop          ask a running loop to stop at the next phase or review boundary
#        ./run_phases.sh review N      re-review phase N's open PR (no worker session)
#        ./run_phases.sh correct N     one correction round from phase N's latest review
#        PHASES="13" ./run_phases.sh run
#
# Configuration lives in the block below; every value can be overridden by an environment
# variable, and $LOG/phases.env (if present) is sourced first. Nothing project-specific
# appears below the configuration block. Evidence is never overwritten: each session writes
# phase-NN-attempt-K.* and phase-NN.* points at the latest. Every step also appends one JSON
# line to $LOG/events.jsonl for tools such as the monitor.

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "run_phases.sh: run it (./run_phases.sh ...), do not source it. Sourcing would launch a session in your shell." >&2
  return 1 2>/dev/null || exit 1
fi
set -u

# ---------------------------------------------------------------- configuration
LOG="${LOG:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
[ -f "$LOG/phases.env" ] && . "$LOG/phases.env"
REPO="${REPO:-/mnt/c/Users/james/Documents/proof-hardware}"
PROMPTS="${PROMPTS:-$REPO/docs/orchestration-prompts/website}"
GH_REPO="${GH_REPO:-JA-Marshall/proof-hardware-website}"
BASE_BRANCH="${BASE_BRANCH:-staging}"
BRANCH_PREFIX="${BRANCH_PREFIX:-phase-}"
STATUS_FILE="${STATUS_FILE:-docs/orchestration-prompts/website/STATUS.md}"
SITE="${SITE:-a static Astro website (proofhardware.co.uk) that will sell server RAM through a separate checkout API}"
PHASES="${PHASES:-01 02 03 04 05 06 07 08 13}"
WORKER_MODEL="${WORKER_MODEL:-claude-sonnet-5}"          # unless the phase file says **Model:** Opus
WORKER_MODEL_OPUS="${WORKER_MODEL_OPUS:-claude-opus-5-5}"
REVIEW_MODEL="${REVIEW_MODEL:-claude-opus-5-5}"
ADVISORY_MODEL="${ADVISORY_MODEL:-gpt-5.6-sol}"
ADVISORY_GATES="${ADVISORY_GATES:-0}"                    # 1: an advisory BLOCKING verdict also blocks
MAX_CORRECTIONS="${MAX_CORRECTIONS:-2}"
PHASE_TIMEOUT="${PHASE_TIMEOUT:-10800}"
REVIEW_TIMEOUT="${REVIEW_TIMEOUT:-1800}"
CORRECT_TIMEOUT="${CORRECT_TIMEOUT:-5400}"
PROMPT_DIR="${PROMPT_DIR:-$LOG/prompts}"                 # review.md and correct.md templates
# The only MCP servers a phase or correction session loads. Without this they would also load
# the account's claude.ai connectors and plugins, which unattended sessions never use and
# report as needing sign-in. The reviewer loads none at all.
[ -n "${WORKER_MCP:-}" ] || WORKER_MCP='{"mcpServers":{"playwright":{"type":"stdio","command":"npx","args":["-y","@playwright/mcp@latest","--browser","chromium"]}}}'
# ---------------------------------------------------------------- end of configuration

log() { echo "$(date -Is) $*" >> "$LOG/coordinator.log"; }

event() {
  # event <name> key=value ... : one JSON line, appended, never rewritten. Values are strings
  # except exit/round/pr/attempt/bytes, which become integers when they look like one.
  python3 - "$@" >> "$LOG/events.jsonl" <<'PY'
import json, sys, time
name, pairs = sys.argv[1], sys.argv[2:]
record = {"at": round(time.time(), 3), "event": name}
for pair in pairs:
    key, _, value = pair.partition("=")
    if key in {"exit", "round", "pr", "attempt", "bytes"} and value.lstrip("-").isdigit():
        record[key] = int(value)
    elif value != "":
        record[key] = value
print(json.dumps(record, sort_keys=True))
PY
}

stop_requested() { [ -e "$LOG/STOP" ]; }

check_stop() {
  # Called at every phase and review boundary. The file is left in place for the operator to remove.
  if stop_requested; then
    log "STOP: operator requested stop ($LOG/STOP exists) before $1"
    event stop reason="operator requested stop" where="$1"
    exit 4
  fi
}

status_of() { git -C "$REPO" show "$2:$STATUS_FILE" 2>/dev/null \
  | awk -F'|' -v p="$1 " 'index($2, p)==2 {gsub(/ /,"",$3); print $3}'; }

model_for() {
  case "$(grep -m1 -i '^\- \*\*Model:\*\*' "$1")" in
    *Opus*) echo "$WORKER_MODEL_OPUS" ;;
    *) echo "$WORKER_MODEL" ;;
  esac
}

next_attempt() {
  # next_attempt <prefix> : the smallest K such that <prefix>-attempt-K.* does not exist yet.
  local k=1
  while ls "$1-attempt-$k".* >/dev/null 2>&1; do k=$((k + 1)); done
  echo "$k"
}

point_latest() {
  # point_latest <link> <target> : phase-NN.json -> phase-NN-attempt-K.json, relative, replaced atomically.
  ln -sfn "$(basename "$2")" "$1"
}

render() {
  # render <template> KEY=VALUE ... : substitute {{KEY}} placeholders; unknown placeholders are left as is.
  python3 - "$@" <<'PY'
import sys
path, pairs = sys.argv[1], sys.argv[2:]
text = open(path, encoding="utf-8").read()
for pair in pairs:
    key, _, value = pair.partition("=")
    text = text.replace("{{" + key + "}}", value)
sys.stdout.write(text)
PY
}

find_pr() {
  gh pr list -R "$GH_REPO" --base "$BASE_BRANCH" --state open --json number,headRefName \
    --jq ".[] | select(.headRefName | startswith(\"$BRANCH_PREFIX$1-\")) | .number" | head -1
}

owner_decisions() {
  # owner_decisions <n> : the owner's decisions for phase n, if $LOG/phase-NN-owner.md exists. They go into the
  # build, every review and every correction, and they are final: they settle what the prompt or a reviewer left open.
  local f="$LOG/phase-$1-owner.md"
  [ -s "$f" ] || return 0
  echo; echo "===== OWNER DECISIONS FOR PHASE $1 (final: they override the phase prompt, and a reviewer must not flag what they settle) ====="
  cat "$f"
}

verdict_in() { grep -q '^VERDICT: CLEAN' "$1" && echo CLEAN || { grep -q '^VERDICT: BLOCKING' "$1" && echo BLOCKING || echo UNKNOWN; }; }

review_phase() {
  # review_phase <n> <phase file> <round> : Claude (primary) and, when installed, GPT (advisory).
  # Findings are posted as one PR comment; nothing is merged. Returns 1 when the verdict blocks.
  local n=$1 file=$2 round=${3:-0}
  local pr branch base attempt
  pr=$(find_pr "$n")
  if [ -z "$pr" ]; then log "REVIEW phase $n round $round: no open PR found"; event review phase="$n" round="$round" note="no open PR found"; return 0; fi
  branch=$(gh pr view "$pr" -R "$GH_REPO" --json headRefName --jq .headRefName)
  base="$LOG/phase-$n-review-r$round"
  attempt=$(next_attempt "$base")
  local stem="$base-attempt-$attempt"
  { render "$PROMPT_DIR/review.md" SITE="$SITE" PR="$pr" BRANCH="$branch" BASE="$BASE_BRANCH" PHASE="$n" ROUND="$round"
    echo; echo "===== PULL REQUEST #$pr ($branch -> $BASE_BRANCH) DIFF ====="; gh pr diff "$pr" -R "$GH_REPO"
    echo; echo "===== PHASE PROMPT ====="; cat "$file"; owner_decisions "$n"; } > "$stem-input.md"
  local bytes; bytes=$(wc -c < "$stem-input.md")
  log "REVIEW phase $n round $round: PR #$pr, $bytes bytes"
  event review phase="$n" round="$round" pr="$pr" bytes="$bytes" attempt="$attempt" branch="$branch"
  ( cd "$REPO" && timeout "$REVIEW_TIMEOUT" claude -p --restricted --strict-mcp-config --model "$REVIEW_MODEL" --effort high --permission-mode dontAsk \
      --tools Read,Grep,Glob --no-session-persistence --disable-slash-commands \
      < "$stem-input.md" > "$stem-claude.md" 2> "$stem-claude.err" )
  local claude_code=$?
  local advisory_note="GPT advisory review: codex not installed." advisory=UNKNOWN
  if command -v codex >/dev/null; then
    ( cd "$REPO" && timeout "$REVIEW_TIMEOUT" codex exec --sandbox read-only --model "$ADVISORY_MODEL" -c 'model_reasoning_effort="high"' \
        --ephemeral -o "$stem-gpt.md" - < "$stem-input.md" > "$stem-gpt.err" 2>&1 )
    if [ -s "$stem-gpt.md" ]; then advisory_note="$(cat "$stem-gpt.md")"; advisory=$(verdict_in "$stem-gpt.md")
    else advisory_note="GPT advisory review produced no output; see $(basename "$stem-gpt.err")."; fi
  fi
  local verdict=UNKNOWN
  [ $claude_code -eq 0 ] && verdict=$(verdict_in "$stem-claude.md")
  local gating=$verdict
  [ "$ADVISORY_GATES" = "1" ] && [ "$advisory" = "BLOCKING" ] && gating=BLOCKING
  { echo "## Independent review of phase $n, round $round (posted by run_phases.sh, not by the phase session)"
    echo; echo "### Claude review ($REVIEW_MODEL, high) — verdict: $verdict"; echo
    if [ $claude_code -eq 0 ] && [ -s "$stem-claude.md" ]; then cat "$stem-claude.md"; else echo "Claude review failed (exit $claude_code); see $(basename "$stem-claude.err")."; fi
    echo; echo "### GPT advisory review ($ADVISORY_MODEL, high) — verdict: $advisory$([ "$ADVISORY_GATES" = "1" ] && echo ", gating" || echo ", advisory only")"; echo; echo "$advisory_note"
    echo; echo "Merge is the owner's decision. A blocking finding goes back to a correction session (at most $MAX_CORRECTIONS rounds)."; } > "$stem.md"
  for suffix in -input.md -claude.md -claude.err -gpt.md -gpt.err .md; do
    [ -e "$stem$suffix" ] && point_latest "$LOG/phase-$n-review$([ "$round" != "0" ] && echo "-r$round")${suffix#-}" "$stem$suffix"
  done
  # Latest of any round, under the plain names the monitor and the operator look at first.
  for suffix in input.md claude.md claude.err gpt.md gpt.err; do [ -e "$stem-$suffix" ] && point_latest "$LOG/phase-$n-review-$suffix" "$stem-$suffix"; done
  point_latest "$LOG/phase-$n-review.md" "$stem.md"
  if gh pr comment "$pr" -R "$GH_REPO" --body-file "$stem.md" >/dev/null 2>&1; then
    log "REVIEW phase $n round $round: verdict $verdict (advisory $advisory), posted to PR #$pr"
    event verdict phase="$n" round="$round" pr="$pr" verdict="$verdict" advisory="$advisory" gating="$gating" posted=yes attempt="$attempt"
  else
    log "REVIEW phase $n round $round: verdict $verdict (advisory $advisory); could not post the comment; see $(basename "$stem.md")"
    event verdict phase="$n" round="$round" pr="$pr" verdict="$verdict" advisory="$advisory" gating="$gating" posted=no attempt="$attempt"
  fi
  [ "$gating" = "BLOCKING" ] && return 1
  return 0
}

correct_phase() {
  # correct_phase <n> <phase file> <round> <model> : a fresh session on the phase's branch fixes only
  # the blocking findings of the latest review, verifies, commits and pushes. Never merges.
  local n=$1 file=$2 round=$3 model=$4
  local pr branch attempt
  pr=$(find_pr "$n")
  [ -z "$pr" ] && { log "CORRECT phase $n round $round: no open PR found"; return 1; }
  branch=$(gh pr view "$pr" -R "$GH_REPO" --json headRefName --jq .headRefName)
  git -C "$REPO" fetch -q origin && git -C "$REPO" switch -q "$branch" && git -C "$REPO" pull -q --ff-only origin "$branch"
  attempt=$(next_attempt "$LOG/phase-$n-correct-r$round")
  local stem="$LOG/phase-$n-correct-r$round-attempt-$attempt"
  { render "$PROMPT_DIR/correct.md" SITE="$SITE" PR="$pr" BRANCH="$branch" BASE="$BASE_BRANCH" PHASE="$n" ROUND="$round" STATUS_FILE="$STATUS_FILE"
    echo; echo "===== REVIEW FINDINGS (round $((round - 1))) ====="; cat "$LOG/phase-$n-review-claude.md"
    # A blocking advisory review travels too, so a correction can fix what only GPT found.
    if [ -s "$LOG/phase-$n-review-gpt.md" ] && [ "$(verdict_in "$LOG/phase-$n-review-gpt.md")" = "BLOCKING" ]; then
      echo; echo "===== ADVISORY REVIEW FINDINGS (GPT, round $((round - 1))) ====="; cat "$LOG/phase-$n-review-gpt.md"
    fi
    echo; echo "===== PHASE PROMPT (for the contract; do not redo it) ====="; cat "$file"; owner_decisions "$n"; } > "$stem-input.md"
  log "CORRECT phase $n round $round on $branch (PR #$pr) model=$model"
  event correct phase="$n" round="$round" pr="$pr" branch="$branch" model="$model" attempt="$attempt"
  ( cd "$REPO" && timeout "$CORRECT_TIMEOUT" claude -p --strict-mcp-config --mcp-config "$WORKER_MCP" --model "$model" --permission-mode auto --output-format json \
      < "$stem-input.md" > "$stem.json" 2> "$stem.err" )
  local code=$?
  point_latest "$LOG/phase-$n-correct-r$round.json" "$stem.json"
  point_latest "$LOG/phase-$n-correct-r$round.err" "$stem.err"
  log "CORRECTED phase $n round $round exit=$code"
  event corrected phase="$n" round="$round" pr="$pr" exit="$code" attempt="$attempt"
  git -C "$REPO" switch -q "$BASE_BRANCH" 2>/dev/null
  return $code
}

review_until_clean() {
  # review_until_clean <n> <file> <model> : review, correct, review... within MAX_CORRECTIONS. Exit codes end the loop.
  local n=$1 file=$2 model=$3 round=${REVIEW_ROUND_START:-0}
  until review_phase "$n" "$file" "$round"; do
    round=$((round + 1))
    if [ "$round" -gt "$MAX_CORRECTIONS" ]; then
      log "STOP: phase $n still blocking after $MAX_CORRECTIONS corrections; owner decides"
      event stop phase="$n" reason="still blocking after $MAX_CORRECTIONS corrections"; exit 3
    fi
    check_stop "correction round $round of phase $n"
    correct_phase "$n" "$file" "$round" "$model" || { log "STOP: correction round $round failed"; event stop phase="$n" round="$round" reason="correction round failed"; exit 3; }
  done
}

run_phase() {
  local n=$1 file model attempt s
  file=$(ls "$PROMPTS/$BRANCH_PREFIX$n"-*.md 2>/dev/null | head -1)
  [ -z "$file" ] && { log "STOP: no prompt file for phase $n under $PROMPTS"; event stop phase="$n" reason="no prompt file"; exit 1; }
  model=$(model_for "$file")
  # Start every phase from up-to-date base with a clean tree (untracked .claude/ is expected).
  git -C "$REPO" switch -q "$BASE_BRANCH" && git -C "$REPO" pull -q --ff-only
  local dirty; dirty=$(git -C "$REPO" status --porcelain | grep -v '^?? .claude/$')
  if [ -n "$dirty" ]; then log "STOP before phase $n: working tree not clean: $dirty"; event stop phase="$n" reason="working tree not clean"; exit 1; fi
  attempt=$(next_attempt "$LOG/phase-$n")
  local stem="$LOG/phase-$n-attempt-$attempt"
  log "START phase $n ($file) model=$model"
  event start phase="$n" prompt="$file" model="$model" attempt="$attempt"
  point_latest "$LOG/phase-$n.json" "$stem.json"; point_latest "$LOG/phase-$n.err" "$stem.err"
  { cat "$file"; owner_decisions "$n"; } > "$stem-input.md"
  ( cd "$REPO" && timeout "$PHASE_TIMEOUT" claude -p --strict-mcp-config --mcp-config "$WORKER_MCP" --model "$model" --permission-mode auto --output-format json \
      < "$stem-input.md" > "$stem.json" 2> "$stem.err" )
  local code=$?
  git -C "$REPO" fetch -q origin
  s=$(status_of "$n" "origin/$BASE_BRANCH")
  local result
  result=$(python3 -c "import json,sys; d=json.loads(open('$stem.json').read().splitlines()[-1]); print(d.get('subtype'), '|', (d.get('result') or '')[-600:].replace('\n',' '))" 2>/dev/null)
  log "END phase $n exit=$code staging-status=${s:-unknown} :: $result"
  event end phase="$n" exit="$code" status="${s:-unknown}" attempt="$attempt" summary="${result:0:600}"
  review_until_clean "$n" "$file" "$model"
  if [ "$s" != "done" ]; then
    log "STOP: phase $n reviewed clean but its PR is open; merge it, then rerun"
    event stop phase="$n" reason="reviewed clean; PR open; merge it, then rerun"; exit 2
  fi
}

cmd_run() {
  for n in $PHASES; do
    check_stop "phase $n"
    git -C "$REPO" fetch -q origin
    if [ "$(status_of "$n" "origin/$BASE_BRANCH")" = "done" ]; then log "phase $n already done"; event already_done phase="$n"; continue; fi
    run_phase "$n"
  done
  log "ALL requested phases done ($PHASES)"
  event all_done phases="$PHASES"
}

cmd_review() {
  local n=${1:?phase number required}
  local file; file=$(ls "$PROMPTS/$BRANCH_PREFIX$n"-*.md 2>/dev/null | head -1)
  [ -z "$file" ] && { echo "no prompt file for phase $n under $PROMPTS" >&2; exit 1; }
  check_stop "review of phase $n"
  log "REVIEW-ONLY phase $n"
  event review_only phase="$n"
  review_until_clean "$n" "$file" "$(model_for "$file")"
  log "STOP: phase $n reviewed clean; merge its PR, then rerun without REVIEW_ONLY"
  event stop phase="$n" reason="reviewed clean; merge its PR, then rerun"
}

cmd_correct() {
  local n=${1:?phase number required}
  local file; file=$(ls "$PROMPTS/$BRANCH_PREFIX$n"-*.md 2>/dev/null | head -1)
  [ -z "$file" ] && { echo "no prompt file for phase $n under $PROMPTS" >&2; exit 1; }
  [ -s "$LOG/phase-$n-review-claude.md" ] || { echo "no review of phase $n to correct from" >&2; exit 1; }
  check_stop "correction of phase $n"
  local round; round=$(( $(ls "$LOG"/phase-"$n"-correct-r*-attempt-*.json 2>/dev/null | sed 's/.*-r\([0-9]*\)-attempt.*/\1/' | sort -n | tail -1) + 1 ))
  correct_phase "$n" "$file" "$round" "$(model_for "$file")" || { log "STOP: correction round $round failed"; event stop phase="$n" round="$round" reason="correction round failed"; exit 3; }
  # A correction is always followed by a review, which corrects again within MAX_CORRECTIONS.
  check_stop "review after correction of phase $n"
  REVIEW_ROUND_START=$round review_until_clean "$n" "$file" "$(model_for "$file")"
  log "STOP: phase $n reviewed clean; merge its PR, then rerun"
  event stop phase="$n" reason="reviewed clean; merge its PR, then rerun"
}

cmd_status() {
  git -C "$REPO" fetch -q origin 2>/dev/null
  echo "phases  base=$BASE_BRANCH  status file=$STATUS_FILE  log=$LOG"
  for n in $PHASES; do
    local s; s=$(status_of "$n" "origin/$BASE_BRANCH")
    printf '  %-4s %-12s %s\n' "$n" "${s:-unknown}" "$(ls "$PROMPTS/$BRANCH_PREFIX$n"-*.md 2>/dev/null | head -1 | xargs -r basename)"
  done
  if ( exec 9>"$LOG/lock"; flock -n 9 ); then echo "lock: free (nothing running)"; else echo "lock: held (a loop is running)"; fi
  if stop_requested; then echo "STOP: present at $LOG/STOP (remove it to allow a run)"; else echo "STOP: absent"; fi
  [ -f "$LOG/coordinator.log" ] && echo "last: $(tail -1 "$LOG/coordinator.log" | cut -c1-160)"
}

cmd_stop() {
  : > "$LOG/STOP"
  event stop_requested by=operator
  echo "STOP written to $LOG/STOP; a running loop stops at its next phase or review boundary. Remove the file before the next run."
}

command=${1:-run}
[ $# -gt 0 ] && shift
case "$command" in
  status) cmd_status; exit 0 ;;
  stop) cmd_stop; exit 0 ;;
  run|review|correct) ;;
  *) echo "usage: $0 [run|status|stop|review N|correct N]" >&2; exit 64 ;;
esac

exec 9>"$LOG/lock"
if ! flock -n 9; then
  echo "run_phases.sh: another loop holds $LOG/lock; use './run_phases.sh status' or './run_phases.sh stop'." >&2
  exit 5
fi
mkdir -p "$PROMPT_DIR"
for template in review.md correct.md; do
  [ -f "$PROMPT_DIR/$template" ] || { echo "missing prompt template $PROMPT_DIR/$template" >&2; exit 1; }
done
case "$command" in
  run) cmd_run ;;
  review) cmd_review "$@" ;;
  correct) cmd_correct "$@" ;;
esac
