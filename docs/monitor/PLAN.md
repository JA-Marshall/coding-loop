# Monitor: the operator's view of a running batch

Status: DRAFT, 2026-09-26. Nothing here is built. The approved static mockup is
[mockup.html](mockup.html): open it in a browser, it needs no server.

## Why

The loop runs overnight with nobody watching. What the operator wants when they
wake up, or glance at their phone at 2 am, is the answer to five questions:

1. Is it still alive, and where is it?
2. How much of each budget is left?
3. Which packets are merged, which one is in flight, which are still queued?
4. If it stopped, why, and where is the evidence?
5. What did each packet cost in calls, tokens, corrections and wall time?

Today the answers are spread across `state.json`, `report.md`, a dozen per-call
files and `systemctl status`. The monitor is one page that reads those files and
puts the answers in one place. It changes nothing about how the loop runs.

The Muse Orchestrator console solved a different problem: deciding whether to
believe a worker. It gave the operator four visual registers (running, waiting
for you, machine evidence, worker claim) and used them identically everywhere.
That idea carries over unchanged. What does not carry over is the control room.
The coding loop has no decisions to make while it runs; the only control is
STOP. So this is a monitor, not a console, and it should look like one:
quieter, denser, and honest about what it does not know.

## What already exists to read

Everything is in the batch evidence directory (`--directory`), which is
outside the checkout by construction.

| File | Written by | Holds |
| --- | --- | --- |
| `state.json` | `Batch.checkpoint` | `phase` (PREPARE, RUN, COMMIT, PUSH, PR, CI, MERGE, COMPLETE, STOPPED), `index`, `calls`, `tokens`, `deadline`, `url`, `pr`, `completed[]`, `reason`, `stopped_phase`, `usage_incomplete` |
| `report.md` | `Batch.checkpoint` | Same, rendered for humans. The monitor does not parse it. |
| `STOP` | the operator | Its existence stops the queue at the next phase boundary. |
| `awake.json` | keep-awake helper | `state` of the Windows wake lock. |
| `<packet-id>/runner/state.json` | `Runner.checkpoint` | `phase` (IMPLEMENT, APPLYING, CHECKS, REVIEW, LOCAL_REVIEWED, STOPPED), `calls`, `corrections`, `candidate`, `checks.results[]` with `exit_code` and `log`, `review`, `advisory`, `usage[]`, `feedback`, `deadline`, `inflight`, `reason` |
| `<packet-id>/runner/packet.json` | `Runner` | The frozen packet: title, objective, owned files, checks, `max_calls`, `max_corrections`, worker model. |
| `<packet-id>/runner/prompt-N.txt`, `schema-N.json`, `output-N.json`, `result-N.json`, `model-N.log` | `Runner` | One set per model call. `result-N.json` is the parsed worker or reviewer answer. |
| `<packet-id>/runner/check-C-<id>.log` | `Runner` | Check output for correction round C. |
| `<packet-id>/runner/candidate-N.diff`, `candidate.diff` | `Runner` | The diff that was checked and reviewed. |

Two facts the page must respect:

- **Time is only in mtimes.** No checkpoint carries a timestamp. Phase
  durations come from file modification times until phase 2 adds an event log.
- **Usage can be unknown.** Muse reports no tokens; `usage_incomplete` is set
  when the batch cannot account. Unknown is rendered as the word, in the
  attention colour, never as `0`.

## Shape

```
scripts/monitor/
  serve.py       stdlib http.server on 127.0.0.1; serves index.html and /api/state
  collect.py     pure function: evidence directory -> one JSON document
  index.html     one file, inline CSS and JS, no build step, no CDN
scripts/tests/
  test_monitor_collect.py    collect() against examples/smoke-run and synthetic trees
  test_monitor_serve.py      the two routes, the 404, the bind address
```

Why this and not the Muse Next.js console:

- The loop's whole pitch is that ordinary Python does everything but the model
  calls. A monitor that needs `npm install` would be the odd one out.
- The monitor must live outside `scripts/coordination/`. `Batch.runtime_hash`
  covers every `.py`, `.json`, `.md` and `.ps1` in that directory, and a change
  there stops the running batch. `scripts/monitor/` is not hashed.
- One HTML file with inline assets is the same discipline as the Proof
  Hardware site, and it means the page works from `file://` for a saved
  snapshot.

`collect.py` is the whole model. It takes a directory and returns a document
like this, and it is the only thing the tests need to exercise properly:

```json
{
  "batch": {"id": "ebay-01", "phase": "RUN", "alive": {"state_age_s": 41, "unit": "active"},
            "budgets": {"clock": {"used": 9120, "limit": 28800},
                        "calls": {"used": 7, "limit": 24},
                        "tokens": {"used": 812340, "limit": 4000000, "incomplete": false}},
            "stop_requested": false, "reason": null, "stopped_phase": null,
            "evidence_dir": "/home/james/.local/share/storehouse-runner/ebay-01"},
  "packets": [
    {"id": "packet-12", "title": "…", "status": "merged", "pr": {"number": 112, "url": "…"},
     "calls": 3, "corrections": 0, "tokens": 190211, "started": 1790400000, "finished": 1790402300},
    {"id": "packet-13", "title": "…", "status": "running", "phase": "CHECKS", "correction": 1,
     "calls": [{"n": 1, "role": "worker", "at": 1790402400, "tokens": 71000, "summary": "…"},
               {"n": 2, "role": "reviewer", "at": 1790402900, "tokens": 40000, "blocking": 1}],
     "checks": [{"id": "django", "exit_code": 1, "log": "…/check-0-django.log", "tail": "…"}],
     "review": {"blocking": [...], "notes": [...]}, "advisory": null,
     "diff": {"files": 4, "added": 120, "removed": 18}},
    {"id": "packet-14", "title": "…", "status": "queued"}
  ]
}
```

`serve.py` adds nothing clever: it binds `127.0.0.1` only, serves the page at
`/`, serves `collect()` at `/api/state`, and serves at most the last 8,000
characters of a named check or model log at `/api/log?path=…`, refusing any
path outside the evidence directory. The page polls `/api/state` every three
seconds and re-renders only when the JSON changes. No websockets, no SSE; the
loop writes a checkpoint a few times an hour, and a poll of a directory of
small files costs nothing.

## The page

One screen, no navigation. Reading order top to bottom, the five questions in
order.

**1. Header strip: alive and where.** Batch id, the outer phase as a word, and
a liveness dot. The dot is the running register only while the latest
checkpoint is younger than the current phase's expected duration and the
systemd unit reports active; otherwise it goes to attention with "No checkpoint
for 47 min" so a hung CI wait is visible without reading anything else. If
`STOP` exists, the strip says "Stop requested. Finishing the current phase."
in the attention register.

**2. Budget row: four meters.** Wall clock, model calls, reported tokens, and
the current packet's calls against its own cap. Each is a thin bar with the
used and limit figures beside it, and the deadline as a real clock time. The
bar turns to attention at 80 percent and failed at 100. When
`usage_incomplete` is true, the tokens meter shows "unknown" and a one-line
reason instead of a bar.

**2b. Tape: the night so far.** A single SVG row per packet on an hour axis
from the batch start to the deadline. Merged packets are verified bars with
their PR number, the running packet is a running bar ending at a "now" line,
queued packets are absent, and every model call is a tick under its packet.
The future is shaded and labelled with what is left: time, calls, packets.
The deadline is a dashed attention line at the right edge. A caption states
where the times come from (mtimes in P1, `events.jsonl` from P2), because a
timeline that looks exact and is not would be worse than none.

**3. Packet rail: the queue.** One row per manifest task, in queue order. Left
edge carries the register: verified for merged (with the PR number as a link),
running for the packet in flight, neutral for queued, failed for stopped. Each
row shows calls, corrections, tokens and elapsed time in a fixed numeric column
so the eye can compare packets. The running row expands into the detail panel;
merged rows expand on click to the same panel, read-only.

**4. Detail panel: one packet.** Two stage rails, both always showing every
stage so the end is visibly not the end:

```
IMPLEMENT ▸ APPLYING ▸ CHECKS ▸ REVIEW ▸ LOCAL_REVIEWED
COMMIT ▸ PUSH ▸ PR ▸ CI ▸ MERGE
```

Below them, the call list: one line per model call with its number, role,
model, tokens and the reviewer's verdict where it is one. Then two side-by-side
cards at equal width, exactly as Muse did it:

- **Checks the supervisor ran**: solid card, one line per check with exit code,
  correction round and an expandable log tail fetched on demand. Machine
  evidence, hard edges.
- **What the worker says it did**: dashed colourless card with the summary
  from the latest `result-N.json`, tagged "Unverified claim". The supervisor
  never acts on this text, and the card says so.

Then the review: blocking findings in the failed register, non-blocking notes
in neutral, and the advisory review as its own block when the packet had one.
Last, the candidate: file count, added and removed lines, and the path to
`candidate.diff` with a copy button. The diff itself is not rendered; the
answer to "show me all of it" is always the file on disk.

**5. Stop card.** Appears only when the batch phase is STOPPED. Failed
register, the `reason` verbatim, the phase it stopped in, and the evidence
directory path with a copy button. This is the card the operator reads first in
the morning, so it sits at the top when it exists, above the header strip.

### Design

- **Registers** are Muse's, verbatim, because they already mean one thing
  each: running steel, attention amber (the only warm colour), verified green
  (only for what a machine confirmed), failed red, claim grey. Diff counts use
  the desaturated diff tokens so a green number is never mistaken for a pass.
- **Both themes.** Muse was dark only. A monitor open on a phone in daylight
  needs a light theme, so the tokens are defined on `:root` for light and
  redefined under `prefers-color-scheme: dark`, with the same oklch hues at
  adjusted lightness. Body has an explicit background in both.
- **Type.** System UI stack for text, system monospace for ids, hashes,
  commands, paths and every number. No web fonts: the page must not fetch
  anything. Tabular figures on all numeric columns.
- **Density.** One line per fact. Row height around 36 px, 13 px text, 12 px
  monospace. It is a status board, not a marketing page.
- **Motion.** One animation: the liveness dot's slow pulse while the batch is
  running, off under `prefers-reduced-motion`. Nothing else moves.
- **Phone.** Single column at 600 px and below, meters stack, the packet rail
  becomes cards, the two evidence cards stack with the checks card first. 16 px
  gutters, no horizontal scroll.
- **Empty and error states.** No evidence directory: a plain sentence and the
  command that starts a batch. Directory but no `state.json`: "Preflight only.
  Nothing launched." Unreadable JSON: the file name and the parser message, in
  the failed register, never a blank panel.

## Phases

### P1: read-only monitor

- Files: `scripts/monitor/collect.py`, `scripts/monitor/serve.py`,
  `scripts/monitor/index.html`, `scripts/tests/test_monitor_collect.py`,
  `scripts/tests/test_monitor_serve.py`.
- `collect()` reads only; it never writes to the evidence directory.
- Durations from mtimes, labelled as approximate in the UI. The tape ships in
  P1 using them; P2 only makes it exact.
- Acceptance: `collect()` on `examples/smoke-run` returns a document whose
  packet is `LOCAL_REVIEWED` with 3 calls and 1 correction and one passed
  check; a synthetic STOPPED tree produces the stop card fields; `serve.py`
  refuses to bind anything but loopback and refuses `/api/log` paths outside
  the directory; `python -m scripts.monitor.serve --directory <dir>` opens a
  page that renders all five sections at 390 px and 1280 px.
- Checks: `python -m pytest scripts/tests -q`, `python scripts/validate_plans.py`.

### P2: event log and real timings

- Files: `scripts/coordination/batch.py`, `scripts/coordination/runner.py`
  (one appended line in each `checkpoint`), `scripts/monitor/collect.py`,
  `scripts/monitor/index.html`, tests.
- Each checkpoint also appends `{"at": <epoch>, "scope": "batch"|"<packet-id>", ...updates}`
  to `events.jsonl` in the evidence directory. Appending, never rewriting, so a
  crash mid-write loses at most one line.
- The monitor prefers `events.jsonl` when present and falls back to mtimes.
  Phase durations become exact; the page gains a compact timeline per packet.
- This changes the runtime hash, so it ships before a batch, never during one.
- Acceptance: a resumed batch produces one contiguous event log; the timeline
  for the smoke run shows three calls with their gaps.

### P3: stop and status

- Files: `scripts/monitor/serve.py`, `scripts/monitor/index.html`, tests.
- `POST /api/stop` creates the `STOP` file, and nothing else. It is the only
  solid high-contrast button on the page, it asks once in plain words
  ("The batch finishes its current phase, then stops. Evidence is kept."), and
  it is disabled once the file exists.
- `serve.py` shells out to `systemctl --user is-active storehouse-<id>` for the
  liveness dot, with a three-second cache.
- Acceptance: the button writes exactly one file; a second press is a no-op;
  the page reflects "Stop requested" within one poll.

### P4: docs

- Files: `README.md`, `docs/diagrams/4-monitor.svg`.
- A fourth diagram in the same style as the three existing ones: the evidence
  directory on the left, `collect()` in the middle, the five sections on the
  right. Amber does not appear in it, because the monitor spends no tokens.
- A README section, "Watching it run", with the one command.

## Not in scope

- Any write to the checkout or the evidence directory other than `STOP`.
- Exposing the page beyond loopback. The evidence holds full prompts and diffs
  from a private repository. A phone on the same LAN can use an SSH tunnel.
- Rendering diffs or full logs in the page. Paths and tails only.
- Multiple batches. One directory, one page. A batch picker can come later if
  it is ever needed.
- Publishing a snapshot as an artifact. Possible later, but only with prompts
  and summaries stripped.

## Running it as packets

Each phase above is one packet for the loop itself, with the files listed as
`owned_files` and the two check commands. P1 and P4 are low risk. P2 touches
`batch.py` and `runner.py` and should carry `advisory_review: true`. P3 is
the only phase that writes anything, and its test must prove the write is
exactly one empty file at one path.
