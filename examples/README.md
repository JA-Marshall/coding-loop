# Examples

## smoke-run/

A real recorded run of `runner.py` against a two-file synthetic fixture, produced by `python -m scripts.coordination.smoke --live --exercise-recovery`. The smoke harness deliberately corrupts the first worker patch so you can see the correction path.

What happened, in order:

| Call | Role | Result |
| --- | --- | --- |
| 1 | worker (gpt-5.6-sol) | returned a correct patch; the harness replaced one context line with `INTENTIONALLY_INVALID_SMOKE_CONTEXT` (`fault-1.json`) so the patch failed to apply |
| 2 | worker | received the parser error as feedback and returned a clean patch; check `clamp-acceptance` passed |
| 3 | reviewer | full-diff review, no findings |

Files:

- `packet.json` is the packet the smoke harness generated. Note the absolute checkout, the 40-character `base_sha`, the single owned file and the one check command.
- `fixture/` is the two-file repo the worker was pointed at.
- `run/state.json` is the checkpoint after the final phase, `LOCAL_REVIEWED`.
- `run/report.md` is the human-readable summary the runner rewrites at every checkpoint.
- `run/result-N.json` is the parsed JSON the model returned on call N; `run/output-N.json` is the raw CLI output; `run/schema-N.json` is the schema it was asked to match.
- `run/candidate.diff` is the reviewed diff.
- `run/check-1-clamp-acceptance.log` is the check output on correction round 1.

Absolute paths in these files are from the machine that produced them and are left as recorded.

## packet.json

A starter single-packet file for `runner.py`. Replace `checkout`, `base_sha`, `owned_files` and `checks`. Every field is validated by `validate_packet` in `runner.py`; unknown fields are rejected.

## manifest.json

A starter batch manifest of the shape `configure.py` produces. In practice you generate this rather than write it, but the fields are shown so the budgets are visible in one place.

## claude-hooks-settings.json

A Claude Code `settings.json` fragment that wires `scripts/coordination/hooks.py` into a worker process. The hook denies every tool call, injects a one-line reminder at session start, and lets the session stop normally. The runner does not install this automatically; the isolation in `isolated.py` is the real boundary and hooks are only feedback.
