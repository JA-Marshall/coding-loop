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
AUTO_MERGE="${AUTO_MERGE:-0}"                            # 1: every phase but the last merges itself once reviewed clean and CI is green
CI_TIMEOUT="${CI_TIMEOUT:-1800}"
CI_START_GRACE="${CI_START_GRACE:-180}"               # seconds to wait for CI to appear before deciding a repo has none
ARBITER_MODEL="${ARBITER_MODEL:-}"                       # e.g. claude-fable-5-1: after MAX_CORRECTIONS, fixes it itself or hands it to the owner; empty = stop for the owner
ARBITER_EFFORT="${ARBITER_EFFORT:-xhigh}"
ARBITER_TIMEOUT="${ARBITER_TIMEOUT:-5400}"
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

notify() {
  # notify <text> : one message to the Discord webhook the monitor uses (NOTIFY_WEBHOOK and MONITOR_URL, from the
  # environment or ~/.config/coding-loop/notify.env). A failed post is logged and never stops the run.
  python3 - "$1" "$(basename "$LOG")" <<'NOTIFY_PY' 2>> "$LOG/notify.err" || true
import json, os, sys, urllib.request
from pathlib import Path
text, batch = sys.argv[1], sys.argv[2]
settings = {k: os.environ.get(k) for k in ("NOTIFY_WEBHOOK", "MONITOR_URL")}
try:
    for line in Path("~/.config/coding-loop/notify.env").expanduser().read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() in settings and not settings[key.strip()]:
            settings[key.strip()] = value.strip().strip('"').strip("'")
except OSError:
    pass
if not settings["NOTIFY_WEBHOOK"]:
    sys.exit(0)
if settings["MONITOR_URL"]:
    text += "\n" + settings["MONITOR_URL"].rstrip("/") + "/?batch=" + batch
request = urllib.request.Request(settings["NOTIFY_WEBHOOK"], data=json.dumps({"content": text[:1990]}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "coding-loop-runner"})
try:
    urllib.request.urlopen(request, timeout=10)
except Exception as exc:  # the webhook address is never printed
    print("notify failed:", type(exc).__name__, file=sys.stderr)
NOTIFY_PY
}

arbitrate_phase() {
  # arbitrate_phase <n> <phase file> <round> : the phase is still blocking after MAX_CORRECTIONS fixes. ARBITER_MODEL
  # reads every review, the diff, the prompt and the owner's decisions, and takes one of two options: fix it itself on
  # the branch (recording the rules it settled in phase-NN-owner.md), or change nothing and hand it to the owner.
  # Returns 0 only when it fixed and pushed.
  local n=$1 file=$2 round=$3 pr branch url attempt stem before after
  pr=$(find_pr "$n")
  [ -z "$pr" ] && return 1
  [ -f "$PROMPT_DIR/arbiter.md" ] || { log "ARBITER phase $n: no $PROMPT_DIR/arbiter.md template"; return 1; }
  branch=$(gh pr view "$pr" -R "$GH_REPO" --json headRefName --jq .headRefName)
  url="https://github.com/$GH_REPO/pull/$pr"
  git -C "$REPO" fetch -q origin && git -C "$REPO" switch -q "$branch" && git -C "$REPO" pull -q --ff-only origin "$branch" || return 1
  before=$(git -C "$REPO" rev-parse HEAD)
  attempt=$(next_attempt "$LOG/phase-$n-arbiter")
  stem="$LOG/phase-$n-arbiter-attempt-$attempt"
  { render "$PROMPT_DIR/arbiter.md" SITE="$SITE" PR="$pr" BRANCH="$branch" BASE="$BASE_BRANCH" PHASE="$n" FIXES="$MAX_CORRECTIONS" STATUS_FILE="$STATUS_FILE"
    echo; echo "===== EVERY REVIEW OF THIS PHASE, OLDEST FIRST ====="
    for f in $(ls -tr "$LOG"/phase-"$n"-review-r*-attempt-*-claude.md "$LOG"/phase-"$n"-review-r*-attempt-*-gpt.md 2>/dev/null); do
      echo; echo "----- $(basename "$f") -----"; cat "$f"
    done
    echo; echo "===== PULL REQUEST #$pr ($branch -> $BASE_BRANCH) DIFF ====="; gh pr diff "$pr" -R "$GH_REPO"
    echo; echo "===== PHASE PROMPT ====="; cat "$file"; owner_decisions "$n"; } > "$stem-input.md"
  log "ARBITER phase $n round $round on $branch (PR #$pr) model=$ARBITER_MODEL effort=$ARBITER_EFFORT"
  receipt arbiter "$n" "$round" "$stem-input.md"
  event arbiter phase="$n" round="$round" pr="$pr" branch="$branch" model="$ARBITER_MODEL" attempt="$attempt"
  notify "⚖️ **$(basename "$LOG")** phase $n is still blocking after $MAX_CORRECTIONS fixes. $ARBITER_MODEL ($ARBITER_EFFORT) is looking at it: it will either fix it or hand it to you. $url"
  ( cd "$REPO" && timeout "$ARBITER_TIMEOUT" claude -p --strict-mcp-config --mcp-config "$WORKER_MCP" --model "$ARBITER_MODEL" --effort "$ARBITER_EFFORT" \
      --permission-mode auto --output-format json < "$stem-input.md" > "$stem.json" 2> "$stem.err" )
  local code=$? outcome summary
  git -C "$REPO" fetch -q origin
  after=$(git -C "$REPO" rev-parse "origin/$branch" 2>/dev/null)
  outcome=$(python3 - "$stem.json" "$stem.md" "$stem-decisions.md" "$before" "$after" <<'ARBITER_PY'
import json, re, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read().strip().splitlines()
try:
    text = str(json.loads(raw[-1]).get("result") or "") if raw else ""
except ValueError:
    text = ""
open(sys.argv[2], "w", encoding="utf-8").write(text + "\n")
verdict = re.findall(r"^ARBITER:\s*(FIXED|ESCALATE)\s*$", text, re.M)
summary = (re.findall(r"^SUMMARY:\s*(.+)$", text, re.M) or [""])[-1].strip()
block = re.search(r"^BEGIN DECISIONS\s*$(.*?)^END DECISIONS\s*$", text, re.M | re.S)
decisions = block.group(1).strip() if block else ""
pushed = bool(sys.argv[5]) and sys.argv[4] != sys.argv[5]
fixed = bool(verdict) and verdict[-1] == "FIXED" and pushed
if fixed and decisions:
    open(sys.argv[3], "w", encoding="utf-8").write(decisions + "\n")
if verdict and verdict[-1] == "FIXED" and not pushed:
    summary = "It said FIXED but pushed no commit, so nothing changed. " + summary
print(("FIXED" if fixed else "ESCALATE") + "\t" + (summary or "no summary given").replace("\t", " "))
ARBITER_PY
)
  git -C "$REPO" switch -q "$BASE_BRANCH" 2>/dev/null
  summary=${outcome#*$'\t'}
  if [ $code -ne 0 ] || [ "${outcome%%$'\t'*}" != "FIXED" ]; then
    log "ARBITER phase $n round $round: handed to the owner (exit $code): $summary"
    event arbiter_done phase="$n" round="$round" outcome="escalated" exit="$code" summary="${summary:0:300}"
    notify "⚖️ **$(basename "$LOG")** phase $n: $ARBITER_MODEL did not force a fix and hands it to you. $summary
Its full reasoning: $(basename "$stem.md") in the run's evidence. $url"
    return 1
  fi
  if [ -s "$stem-decisions.md" ]; then
    local owner="$LOG/phase-$n-owner.md"
    { [ -s "$owner" ] && { cat "$owner"; echo; }
      echo "## Settled by $ARBITER_MODEL ($ARBITER_EFFORT) on $(date '+%Y-%m-%d %H:%M'), after $MAX_CORRECTIONS fixes"
      echo "It fixed the branch following these rules. Edit or delete this section if you disagree."
      echo; cat "$stem-decisions.md"; } > "$owner.tmp" && mv "$owner.tmp" "$owner"
  fi
  log "ARBITER phase $n round $round: fixed and pushed: $summary"
  event arbiter_done phase="$n" round="$round" outcome="fixed" exit="$code" summary="${summary:0:300}"
  notify "⚖️ **$(basename "$LOG")** phase $n: $ARBITER_MODEL fixed it and pushed. $summary
A review is starting now. The rules it settled are in the phase's decisions box on the monitor; edit them if you disagree.
\`\`\`
$(head -c 1000 "$stem-decisions.md" 2>/dev/null)
\`\`\`"
  return 0
}

ci_state() {
  # ci_state <pr> : pass, pending, none, or fail:<names> for the PR's head commit.
  gh pr view "$1" -R "$GH_REPO" --json statusCheckRollup | python3 -c '
import json, sys
checks = json.load(sys.stdin).get("statusCheckRollup") or []
bad, pending = [], False
for c in checks:
    name = c.get("name") or c.get("context") or "check"
    if c.get("__typename") == "StatusContext" or "state" in c:
        state = (c.get("state") or "").upper()
        if state in ("PENDING", "EXPECTED", ""): pending = True
        elif state != "SUCCESS": bad.append(name)
    else:
        if (c.get("status") or "").upper() != "COMPLETED": pending = True
        elif (c.get("conclusion") or "").upper() not in ("SUCCESS", "SKIPPED", "NEUTRAL"): bad.append(name)
print("fail:" + ", ".join(bad) if bad else "pending" if pending else "pass" if checks else "none")'
}

auto_merge() {
  # auto_merge <n> : merge phase n's PR when CI is green on exactly the commit the reviewers passed, then confirm the
  # phase's status row reads done on the base branch. Prints the reason and returns 1 when it does not merge.
  local n=$1 pr head reviewed url waited=0 ci
  pr=$(find_pr "$n")
  [ -z "$pr" ] && { echo "no open PR"; return 1; }
  url="https://github.com/$GH_REPO/pull/$pr"
  head=$(gh pr view "$pr" -R "$GH_REPO" --json headRefOid --jq .headRefOid)
  reviewed=$(cat "$LOG/phase-$n-reviewed-head" 2>/dev/null)
  if [ -z "$head" ] || [ "$head" != "$reviewed" ]; then
    echo "the PR's head ${head:0:7} is not the commit the reviewers passed (${reviewed:0:7})"; return 1
  fi
  while :; do
    ci=$(ci_state "$pr")
    case "$ci" in
      pass) break ;;
      fail:*) echo "CI failed on ${head:0:7}: ${ci#fail:}"; return 1 ;;
    esac
    [ "$waited" -ge "$CI_TIMEOUT" ] && { echo "CI still $ci on ${head:0:7} after $((CI_TIMEOUT / 60)) minutes"; return 1; }
    sleep 20; waited=$((waited + 20))
  done
  if ! gh pr merge "$pr" -R "$GH_REPO" --merge --match-head-commit "$head" > /dev/null 2> "$LOG/phase-$n-merge.err"; then
    echo "GitHub refused the merge: $(tail -1 "$LOG/phase-$n-merge.err")"; return 1
  fi
  log "MERGED phase $n PR #$pr at $head"
  event merged phase="$n" pr="$pr" head="$head"
  git -C "$REPO" fetch -q origin
  if [ "$(status_of "$n" "origin/$BASE_BRANCH")" != "done" ]; then
    echo "merged PR #$pr, but its row in $STATUS_FILE does not read done, so the runner would build it again; fix the row"; return 1
  fi
  local settled=""
  if ls "$LOG/phase-$n-arbiter-attempt-"*-decisions.md > /dev/null 2>&1; then
    settled="
The arbiter fixed this phase; the rules it settled (check them in the phase's decisions box):
\`\`\`
$(head -c 900 "$(ls -t "$LOG/phase-$n-arbiter-attempt-"*-decisions.md | head -1)")
\`\`\`"
  fi
  notify "✅ **$(basename "$LOG")** phase $n merged automatically (both reviews clean, CI green on ${head:0:7}). $url$settled"
  return 0
}

finish_phase() {
  # finish_phase <n> : phase n reviewed clean. With AUTO_MERGE=1 every phase but the last merges itself and the run
  # goes on; the last phase of the task, or one that cannot merge cleanly, stops for the owner.
  local n=$1 last reason
  last=$(echo $PHASES | awk '{print $NF}')
  if [ "$AUTO_MERGE" = 1 ] && [ "$n" != "$last" ]; then
    if reason=$(auto_merge "$n"); then return 0; fi
    log "STOP: phase $n reviewed clean but was not merged automatically: $reason; merge it, then rerun"
    event stop phase="$n" reason="reviewed clean; not merged automatically: $reason; merge it, then rerun"; exit 2
  fi
  if [ "$n" = "$last" ] && [ "$AUTO_MERGE" = 1 ]; then
    log "STOP: phase $n, the last of the task, reviewed clean; merge its PR to finish"
    event stop phase="$n" reason="last phase reviewed clean; merge its PR to finish"; exit 2
  fi
  log "STOP: phase $n reviewed clean; merge its PR, then rerun"
  event stop phase="$n" reason="reviewed clean; merge its PR, then rerun"; exit 2
}

receipt() {
  # receipt <what> <n> <round> <input file> [note] : one line saying exactly what a session was given: each section's
  # size, the finding IDs it carries and its verdict. Logged, evented and kept beside the input, so the owner can check
  # that a fixer received the right findings. <what> is build, review, fix or arbiter.
  local what=$1 n=$2 round=$3 file=$4 note=${5:-} line
  line=$(python3 - "$file" <<'RECEIPT_PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
def size(s):
    n = len(s.encode())
    return "%.1f KB" % (n / 1024) if n >= 1024 else "%d B" % n
def order(i):
    p, k = i[1:].split("-")
    return (int(p), int(k))
parts = re.split(r"^===== (.+?) =====$", text, flags=re.M)
out = ["instructions " + size(parts[0])]
for title, body in zip(parts[1::2], parts[2::2]):
    label = re.sub(r"\s*\(.*$", "", title).strip().lower()
    item = label + " " + size(body)
    ids = sorted(set(re.findall(r"\bP[0-3]-\d+\b", body)), key=order)
    if ids and "diff" not in label and "prompt" not in label:
        item += " [" + ",".join(ids) + "]"
    verdict = re.findall(r"^VERDICT:\s*(\w+)", body, re.M)
    if verdict:
        item += " " + verdict[-1]
    out.append(item)
print(" · ".join(out))
RECEIPT_PY
)
  [ -n "$note" ] && line="$line · $note"
  echo "$line" > "${file%-input.md}-receipt.txt"
  log "INPUT $what phase $n round $round ($(basename "$file")): $line"
  event input phase="$n" round="$round" what="$what" file="$(basename "$file")" receipt="$line"
}

ci_gate() {
  # ci_gate <n> <pr> <round> : the repository's own checks run before any model reviews the code, because they are far
  # cheaper. Returns 0 when CI passed, has no checks or is still pending after CI_TIMEOUT (the review goes ahead and
  # auto-merge checks again). On a failure it writes the failing log as a P1 finding for the next fix and returns 1,
  # so no review is spent on code that does not pass its tests.
  local n=$1 pr=$2 round=$3 waited=0 ci head attempt stem
  head=$(gh pr view "$pr" -R "$GH_REPO" --json headRefOid --jq .headRefOid)
  log "CI phase $n round $round: waiting for checks on ${head:0:7}"
  event ci phase="$n" round="$round" pr="$pr" head="$head" state="waiting"
  while :; do
    ci=$(ci_state "$pr")
    case "$ci" in
      pass) log "CI phase $n round $round: passed on ${head:0:7}"; event ci phase="$n" round="$round" state="passed"; return 0 ;;
      fail:*) break ;;
      none) [ "$waited" -ge "$CI_START_GRACE" ] && { log "CI phase $n round $round: no checks on ${head:0:7}; reviewing anyway"; return 0; } ;;
    esac
    if [ "$waited" -ge "$CI_TIMEOUT" ]; then
      log "CI phase $n round $round: still pending on ${head:0:7} after $((CI_TIMEOUT / 60)) minutes; reviewing anyway"
      event ci phase="$n" round="$round" state="pending"; return 0
    fi
    sleep 20; waited=$((waited + 20))
  done
  attempt=$(next_attempt "$LOG/phase-$n-ci-r$round")
  stem="$LOG/phase-$n-ci-r$round-attempt-$attempt"
  { echo "The repository's checks failed on ${head:0:7}, so no model reviewed this commit."
    echo; echo "### P1-1 · CI: ${ci#fail:}"
    echo "What fails: the checks above fail on commit $head."
    echo "Smallest fix: make them pass without weakening, skipping or deleting any test or check."
    echo; echo "Failing job output (last part):"; echo '```text'
    for run in $(gh pr view "$pr" -R "$GH_REPO" --json statusCheckRollup --jq '.statusCheckRollup[] | select((.conclusion // .state) as $c | ($c != "SUCCESS" and $c != "SKIPPED" and $c != "NEUTRAL" and $c != "PENDING")) | .detailsUrl' \
               | sed -n 's#.*/actions/runs/\([0-9]*\).*#\1#p' | sort -u); do
      gh run view "$run" -R "$GH_REPO" --log-failed 2>&1 | tail -c 6000
    done
    echo '```'; echo; echo "VERDICT: BLOCKING"; } > "$stem.md"
  FINDINGS_FILE="$stem.md"
  FINDINGS_KIND=ci
  log "CI phase $n round $round: failed on ${head:0:7}: ${ci#fail:}; skipping the review, the log goes to a fix"
  event ci phase="$n" round="$round" state="failed" checks="${ci#fail:}"
  return 1
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
  # The commit this review judges; auto-merge refuses any other head.
  gh pr view "$pr" -R "$GH_REPO" --json headRefOid --jq .headRefOid > "$LOG/phase-$n-reviewed-head"
  # Tests before reviewers: a failing commit goes straight back to a fix with the CI log as its finding.
  FINDINGS_FILE="$LOG/phase-$n-review-claude.md"; FINDINGS_KIND=review
  ci_gate "$n" "$pr" "$round" || return 1
  base="$LOG/phase-$n-review-r$round"
  attempt=$(next_attempt "$base")
  local stem="$base-attempt-$attempt"
  { render "$PROMPT_DIR/review.md" SITE="$SITE" PR="$pr" BRANCH="$branch" BASE="$BASE_BRANCH" PHASE="$n" ROUND="$round"
    # The last review's findings, so this one marks each ID FIXED or NOT FIXED instead of starting over.
    if [ -s "$LOG/phase-$n-review-claude.md" ]; then
      echo; echo "===== PREVIOUS REVIEW (Claude, the latest before this one) ====="; cat "$LOG/phase-$n-review-claude.md"
    fi
    if [ "$ADVISORY_GATES" = "1" ] && [ -s "$LOG/phase-$n-review-gpt.md" ]; then
      echo; echo "===== PREVIOUS REVIEW (GPT, the latest before this one) ====="; cat "$LOG/phase-$n-review-gpt.md"
    fi
    echo; echo "===== PULL REQUEST #$pr ($branch -> $BASE_BRANCH) DIFF ====="; gh pr diff "$pr" -R "$GH_REPO"
    echo; echo "===== PHASE PROMPT ====="; cat "$file"; owner_decisions "$n"; } > "$stem-input.md"
  local bytes; bytes=$(wc -c < "$stem-input.md")
  log "REVIEW phase $n round $round: PR #$pr, $bytes bytes"
  receipt review "$n" "$round" "$stem-input.md"
  event review phase="$n" round="$round" pr="$pr" bytes="$bytes" attempt="$attempt" branch="$branch"
  ( cd "$REPO" && timeout "$REVIEW_TIMEOUT" claude -p --restricted --strict-mcp-config --model "$REVIEW_MODEL" --effort high --permission-mode dontAsk \
      --tools Read,Grep,Glob --no-session-persistence --disable-slash-commands \
      < "$stem-input.md" > "$stem-claude.md" 2> "$stem-claude.err" )
  local claude_code=$?
  local advisory_note="GPT advisory review: turned off or codex not installed." advisory=UNKNOWN
  if [ -n "$ADVISORY_MODEL" ] && command -v codex >/dev/null; then  # ADVISORY_MODEL= (empty) turns the GPT review off
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
    if [ "${FINDINGS_KIND:-review}" = "ci" ]; then
      echo; echo "===== CI FAILURE (the checks ran before any review; round $((round - 1))) ====="; cat "$FINDINGS_FILE"
    else
      echo; echo "===== REVIEW FINDINGS (round $((round - 1))) ====="; cat "$LOG/phase-$n-review-claude.md"
    fi
    # A blocking advisory review travels too when it gates, so a correction can fix what only GPT found.
    if [ "${FINDINGS_KIND:-review}" = "review" ] && [ "$ADVISORY_GATES" = "1" ] && [ -s "$LOG/phase-$n-review-gpt.md" ] && [ "$(verdict_in "$LOG/phase-$n-review-gpt.md")" = "BLOCKING" ]; then
      echo; echo "===== ADVISORY REVIEW FINDINGS (GPT, round $((round - 1))) ====="; cat "$LOG/phase-$n-review-gpt.md"
    fi
    echo; echo "===== PHASE PROMPT (for the contract; do not redo it) ====="; cat "$file"; owner_decisions "$n"; } > "$stem-input.md"
  log "CORRECT phase $n round $round on $branch (PR #$pr) model=$model"
  local gpt_note=""
  if [ "${FINDINGS_KIND:-review}" = "review" ] && [ "$ADVISORY_GATES" != "1" ] && [ -s "$LOG/phase-$n-review-gpt.md" ] && [ "$(verdict_in "$LOG/phase-$n-review-gpt.md")" = "BLOCKING" ]; then
    gpt_note="GPT's blocking review left out (advisory only)"
  fi
  receipt fix "$n" "$round" "$stem-input.md" "$gpt_note"
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
  # review_until_clean <n> <file> <model> : review, correct, review... within MAX_CORRECTIONS. Then, once per phase,
  # ARBITER_MODEL either fixes it (and a review follows) or hands it to the owner. Exit codes end the loop.
  local n=$1 file=$2 model=$3 round=${REVIEW_ROUND_START:-0} arbitrated=0
  until review_phase "$n" "$file" "$round"; do
    round=$((round + 1))
    if [ "$round" -gt "$MAX_CORRECTIONS" ]; then
      if [ "$arbitrated" = 0 ] && [ -n "$ARBITER_MODEL" ] && ! ls "$LOG/phase-$n-arbiter-attempt-"*-input.md >/dev/null 2>&1; then
        check_stop "arbitration of phase $n"
        if arbitrate_phase "$n" "$file" "$round"; then arbitrated=1; continue; fi
        log "STOP: phase $n still blocking after $MAX_CORRECTIONS corrections; the arbiter handed it to the owner"
        event stop phase="$n" reason="still blocking after $MAX_CORRECTIONS corrections; the arbiter handed it to the owner"; exit 3
      fi
      log "STOP: phase $n still blocking after $MAX_CORRECTIONS corrections$([ "$arbitrated" = 1 ] && echo " and the arbiter's fix"); owner decides"
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
  receipt build "$n" 0 "$stem-input.md"
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
  if [ "$s" != "done" ]; then finish_phase "$n"; fi  # merges and returns, or stops for the owner
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
  finish_phase "$n"
  cmd_run  # merged: carry on with the phases still to do
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
  finish_phase "$n"
  cmd_run  # merged: carry on with the phases still to do
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
