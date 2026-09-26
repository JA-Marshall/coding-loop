You are correcting phase {{PHASE}} of {{SITE}} after an independent review. Work only in this repository,
on the existing branch `{{BRANCH}}` (already checked out; pull request #{{PR}} into {{BASE}}). Fix ONLY the BLOCKING findings
below, add or adjust tests that prove each fix, run `npm run verify` until it passes, commit with a clear message,
and push the branch. Do not merge, do not rebase or force-push, do not widen scope, and do not weaken acceptance
checks to make a finding go away. If a finding is wrong, say so in the commit message and leave the code as it is.
Append one hand-off line under the phase {{PHASE}} row in {{STATUS_FILE}} describing round {{ROUND}}.
