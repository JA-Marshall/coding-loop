You are correcting phase {{PHASE}} of {{SITE}} after an independent review. Work only in this repository,
on the existing branch `{{BRANCH}}` (already checked out; pull request #{{PR}} into {{BASE}}). Findings carry IDs and priorities (P0-1, P1-2, ...).
Fix every open P0 and P1 finding below, by ID, and leave P2 and P3 alone unless a fix is one line and risk-free; add or adjust tests that prove each fix, run `npm run verify` until it passes, commit with a message that lists the IDs you fixed (and any you judged wrong, with why),
and push the branch. Do not merge, do not rebase or force-push, do not widen scope, and do not weaken acceptance
checks to make a finding go away. If a finding is wrong, say so in the commit message and leave the code as it is.
Append one hand-off line under the phase {{PHASE}} row in {{STATUS_FILE}} describing round {{ROUND}}.
