# Storehouse plan workflow

This file is the canonical lifecycle and template contract for Storehouse
implementation plans. Repository guardrails in [AGENTS.md](AGENTS.md) always
take precedence. The stable entry points are:

1. `/plan` creates one focused `DRAFT` in `docs/plans/tasks/` and registers its
   status-free link in [the inbox](docs/plans/INBOX.md). Planning does not
   authorize code changes.
2. `Make it READY` resolves every decision and placeholder, selects the first
   phase, records authority and impact, and points
   [ACTIVE.md](docs/plans/ACTIVE.md) at the plan.
3. `/goal Execute docs/plans/ACTIVE.md` grants implementation authority for
   that READY plan. The primary agent changes it to `IN_PROGRESS`, executes it,
   and keeps the phase and evidence current.

Run `python scripts/validate_plans.py` after every lifecycle edit.

## Unified selling shortcut

For the unified selling project only, the owner's explicit request **"Continue
selling"** or **"Continue the selling project"** authorizes the coordinator to
prepare, ready and execute one bounded current/next selling plan. It is the
project-specific shorthand for the three entry points above; the DRAFT, READY
and execution gates still run and are validated in order. It is not authority
for all 18 packets. A request for status, planning or orchestration setup alone
does not start product implementation.

Start from [the project entry point](docs/projects/unified-selling.md). Resume
an active selling plan or its existing PR/correction first. If none is active,
select the first dependency-satisfied packet without a merged implementation;
verify GitHub facts rather than interpreting COMPLETE as merged. Resolve genuine
product/authority choices before READY. Do not replace an unrelated active plan.

The coordinator owns worker dispatch, waiting, review, correction and the PR.
After passing READY with SEQUENTIAL_WORKER, the same explicit shortcut grants
execution authority for that one plan: enter IN_PROGRESS/GOAL_GRANTED before
dispatch. On resuming a BLOCKED plan, record the resolved blocker before making
its phase ACTIVE. No new authorization is needed for routine checks or fixes
inside the granted scope. This is foreground coordination, not a scheduler or
permission to create separate user-owned tasks.

Stop at a reviewed PR awaiting the owner, a genuine blocker or a scope decision.
The shortcut does not grant owner merge, release approval, real charges, live
marketplace writes, customer messages or worktree creation. Existing grants
remain effective; do not repeatedly ask for an already-authorized action.

## Explicit automatic batch authority

An explicit owner request for an automatic run may grant prepare/ready/execute and
merge authority for a named, frozen queue. This is separate from the one-packet
"Continue selling" shortcut. The 2026-09-26 eBay grant and exact contracts are in
[ebay-auto-run-authority.md](docs/workflows/ebay-auto-run-authority.md).

Only the primary coordinator's deterministic supervisor advances that queue.
Queued contracts are not active plans. After each predecessor merges, generate and
validate DRAFT, READY and IN_PROGRESS in order for the next frozen contract; retain
one active plan and frozen SEQUENTIAL_WORKER boundaries. The supervisor owns
integration, exact-candidate review evidence and final-head CI gates. OWNER_GRANTED
permits the named PR merge only. Record COMPLETE and clear ACTIVE in the final
candidate; GitHub remains authoritative for merge and CI. This grants no deployment
or live marketplace actions and does not relax failure checks or protected paths.

## Status lifecycle

| Status | Meaning | Required phase state |
| --- | --- | --- |
| `DRAFT` | Scope is being formed; `TBD` is allowed. | No active phase; phases are `PENDING`. |
| `READY` | Scope, acceptance, impacts, authority, and agent ownership are executable. | `active_phase` names one `PENDING` phase. |
| `IN_PROGRESS` | The goal command has authorized implementation. | Exactly one named phase is `ACTIVE`. |
| `BLOCKED` | Work cannot safely continue without an external decision or state change. | Exactly one named phase is `BLOCKED`. |
| `COMPLETE` | Repository-owned work is evidenced in the final PR candidate; GitHub owns required-check, review, and owner-merge facts. | All phases are `COMPLETE` or `SKIPPED`; no active phase. |
| `SUPERSEDED` | The plan is retained as history but must not be executed. | No active phase; replacement evidence is required. |

Only one task may be `READY`, `IN_PROGRESS`, or `BLOCKED`; it must be the
target in `docs/plans/ACTIVE.md`. The plan control block is the only repository
source of overall lifecycle status. Phase states describe resumable work;
`INBOX.md` is only an index and `ACTIVE.md` is only the executable pointer.

`COMPLETE` means the repository-owned outcome, verification, and safety review
are recorded in the final implementation PR candidate. Set it, clear
`ACTIVE.md`, and preserve the status-free inbox link in that same final commit.
The required check then validates the final head, and the owner reviews and
merges it through protected `main`. GitHub metadata is authoritative for check,
review, PR, and merge facts; do not copy successful run IDs or a future merge
claim into another evidence commit. If final-head CI fails, reopen the affected
phase in the next corrective commit. A `COMPLETE` candidate reaches `main` only
through the still-mandatory green required check and owner merge.

## Agent strategy

`PRIMARY` is the default. One primary agent owns planning, implementation,
integration, verification, and reporting.

`SEQUENTIAL_WORKER` is limited to the unified selling project. One implementing
worker and the primary coordinator work serially; the primary reviews and owns
final integration. DRAFT creation and READY preparation use no subagents. Freeze
exactly one worker section before READY, using this form in Agent strategy:

```markdown
### Worker: implementation

- Project: unified-selling
- Model: gpt-5.6-terra
- Reasoning: medium
- Max workers: 1
- Owns: operations/selling_workspace.py, operations/test_selling_workspace.py
- Deliverable: The plan's bounded implementation and focused verification.
- Coordinator owns: docs/plans/tasks/selling-workspace-foundation.md, docs/plans/ACTIVE.md, docs/plans/INBOX.md, docs/projects/unified-selling.md
- Review owner: PRIMARY
```

The owned paths above are an example, not the complete Selling page contract.
Replace them with the exact files/directories required by the selected plan.
Use `gpt-5.6-terra`/`medium`, `gpt-5.6-sol`/`high` or `claude-opus-5-5`/`high`. A
Claude worker only implements; review and triage stay on Codex. Owned paths must be relative,
bounded and non-overlapping; the worker cannot own workflow/plan/CI controls.
The coordinator reads but does not edit worker-owned files until handback. Record
the worker handle and actual handoff in the canonical plan's progress log before
launching any replacement. A completed worker may receive bounded corrections;
never create another merely because the coordinator resumed or changed context.

The validator checks the declared scope and worker contract. The coordinator
must also enforce one running worker and the agreed file boundary at runtime;
a valid Markdown contract is not a sandbox or proof of a correct implementation.

`DISJOINT_LANES` is exceptional and may be used only after the plan passes the
READY gate. The plan must define exactly two independent lanes, list explicit
non-overlapping file ownership, and state a separately verifiable deliverable
for each lane. The primary agent remains responsible for integration. Never
create agents merely to rediscover, design, or review the same work.

## Plan control block

Every plan starts with this machine-readable block. Keep keys exact and use a
short explanation after each impact value when useful.

```markdown
<!-- storehouse-plan
id: short-hyphenated-id
status: DRAFT
active_phase: NONE
agent_strategy: PRIMARY
implementation_authority: PLAN_ONLY
merge_authority: OWNER_REQUIRED
release_authority: OWNER_REQUIRED
schema_impact: NONE - No schema change expected.
data_impact: NONE - No backfill or destructive data operation expected.
valuation_impact: NONE - No inventory value or ledger semantic change expected.
reporting_impact: NONE - No reporting view or dashboard change expected.
marketplace_write_impact: NONE - No eBay write path is in scope.
production_impact: NONE - Planning and implementation do not deploy production.
tbd: Describe unresolved choices, or NONE.
-->
```

Allowed impact prefixes are `NONE` and `PRESENT`. A `PRESENT` impact must be
explained in the plan, including migration/backup/rollback implications where
applicable. Authority values are:

- `implementation_authority`: `PLAN_ONLY`, `GOAL_REQUIRED`, `GOAL_GRANTED`, or
  `CLOSED`;
- `merge_authority`: `OWNER_REQUIRED` or `OWNER_GRANTED`;
- `release_authority`: always `OWNER_REQUIRED` in repository plans. A plan or
  goal cannot grant release or deployment authority.

## Canonical task template

Copy [docs/plans/templates/TASK.md](docs/plans/templates/TASK.md) to
`docs/plans/tasks/<short-purpose>.md`. Replace every instructional placeholder;
only a `DRAFT` may retain `TBD`. Keep phases small enough to verify and resume.
Do not delete earlier progress or evidence when updating a plan.

Each phase uses this exact form:

```markdown
### P1 - Short outcome

- State: PENDING
- Depends on: NONE
- Files: explicit paths or bounded areas
- Acceptance: observable stopping condition
```

Allowed phase states are `PENDING`, `ACTIVE`, `BLOCKED`, `COMPLETE`, and
`SKIPPED`. A skipped phase needs a reason in the progress log.

## READY gate

Before changing a draft to `READY`:

- replace every `TBD`, `TODO`, `???`, and ambiguous choice;
- define objective, non-goals, acceptance criteria, failure/permission paths,
  expected files, and focused/full verification;
- classify schema, data, valuation, reporting, marketplace-write, and
  production impacts;
- set `implementation_authority: GOAL_REQUIRED` and preserve owner-only merge
  and release boundaries unless the owner explicitly granted merge authority;
- use `PRIMARY`, the selling-only `SEQUENTIAL_WORKER` contract above, or exactly
  two genuinely independent disjoint lanes;
- set `active_phase` to the first `PENDING` phase;
- ensure the status-free inbox link exists and make `ACTIVE.md` point to this
  task; and
- run the focused validator tests and the repository validator.

## Completion evidence

A `COMPLETE` plan must preserve non-empty evidence for outcome, local/baseline
verification, diff/safety review, the authoritative GitHub PR record, the
external owner-merge gate, and production state. The PR and merge fields may
use stable markers such as `GITHUB_AUTHORITATIVE` and `OWNER_GATE`; they must
not claim a future result. Never use `COMPLETE` to hide failed or unrun
repository-owned verification. Required final-head CI and owner merge remain
external mandatory gates enforced after the final candidate commit.
