# Phase runner

`run_phases.sh` drives a phased build the way the coding loop drives packets: one fresh headless
Claude session per phase prompt, in order, then an independent review of the phase's pull request,
with blocking findings sent back to a bounded number of correction sessions. Nothing is merged by
the loop; it stops after each phase that leaves a PR open, and the owner merges after reading the
review. A phase counts as done when its row in the status file on the base branch says `done`.

This directory is the canonical copy. The installed copy lives in the run's evidence directory
(for the Proof Hardware website, `~/.local/share/proof-hardware-phases/`) beside the logs it writes.

## Commands

```
./run_phases.sh status        one line per phase, plus the lock and the STOP file
./run_phases.sh run           every phase in PHASES not yet done
./run_phases.sh review 13     re-review phase 13's open PR; no worker session
./run_phases.sh correct 13    one correction session from phase 13's latest review
./run_phases.sh stop          write the STOP file; a running loop stops at its next boundary
```

The monitor (`scripts/monitor`) offers the same commands as buttons on the run's page.

## Configuration

Everything project-specific is in the configuration block at the top of the script. Every value
can be overridden by an environment variable, and `phases.env` in the evidence directory is
sourced first, so a new project needs a `phases.env`, not an edited script:

```
REPO=/path/to/checkout
PROMPTS=$REPO/docs/orchestration-prompts/website
GH_REPO=owner/repo
BASE_BRANCH=staging
STATUS_FILE=docs/orchestration-prompts/website/STATUS.md
SITE="a static Astro website that sells server RAM"
PHASES="01 02 03"
```

The reviewer and correction prompts are templates in `prompts/`, with `{{PR}}`, `{{BRANCH}}`,
`{{BASE}}`, `{{PHASE}}`, `{{ROUND}}`, `{{SITE}}` and `{{STATUS_FILE}}` placeholders.

## Evidence

Every session writes `phase-NN-attempt-K.json` and `.err`; `phase-NN.json` is a symlink to the
latest attempt, so a restart never erases an earlier session's record. Reviews and corrections
follow the same pattern with `-review-rR-attempt-K` and `-correct-rR-attempt-K`. Each step also
appends one JSON line to `events.jsonl` (`at`, `event`, `phase`, `round`, `pr`, `verdict`,
`advisory`, `exit`, `model`, `attempt`), which the monitor reads in addition to `coordinator.log`.

## Safety

The script refuses to be sourced, takes an exclusive lock so two loops cannot run at once, and
checks for a `STOP` file before every phase and every review round. The advisory (GPT) review is
recorded but does not gate unless `ADVISORY_GATES=1`.
