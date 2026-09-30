"""Read a batch evidence directory into one JSON document for the monitor.

``collect(directory)`` is the whole model. It opens files for reading only and
never creates, touches or writes anything under the directory. Every time in
the document comes from a file modification time, so the UI labels them as
approximate; no checkpoint carries a timestamp until the event log ships.

Two layouts are understood:

- a batch evidence directory (``state.json`` with ``manifest_hash``,
  ``manifest.json`` and one ``<task-id>/runner/`` tree per packet), and
- a single runner directory (``state.json`` with ``packet_hash``), or a
  wrapper holding ``packet.json`` and ``run/`` as ``examples/smoke-run`` does.

The module lives outside ``scripts/coordination/`` on purpose: that directory
is hashed by the running batch and any change there stops it.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

BATCH_PHASES = ["PREPARE", "RUN", "COMMIT", "PUSH", "PR", "CI", "MERGE", "COMPLETE", "STOPPED"]
PACKET_STAGES = ["IMPLEMENT", "APPLYING", "CHECKS", "REVIEW", "LOCAL_REVIEWED"]
BATCH_STAGES = ["COMMIT", "PUSH", "PR", "CI", "MERGE"]
TERMINAL_BATCH = {"COMPLETE", "STOPPED"}
TERMINAL_PACKET = {"LOCAL_REVIEWED", "STOPPED"}
TIME_SOURCE = "mtime"
# Bytes read from the end of a check log for the inline tail; the page fetches
# more on demand through /api/log.
TAIL_BYTES = 600
TAIL_LINES = 6

# The reviewer and advisory models are pinned in runner.py. Read them from
# there when the checkout is importable so the monitor never drifts; fall back
# to the shipped values when collect() runs from a bare copy.
try:  # pragma: no cover - exercised only when the package layout differs
    from scripts.coordination.runner import ADVISORY_MODEL, REVIEW_MODEL
except Exception:  # noqa: BLE001
    REVIEW_MODEL = ("claude-opus-5-5", "high")
    ADVISORY_MODEL = ("gpt-5.6-sol", "high")


# ---------------------------------------------------------------- helpers

def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def load_json(path, directory, errors):
    """Parse one JSON file; on failure record the file and message, return None."""
    try:
        # utf-8-sig: the Windows awake helper writes a byte-order mark.
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        errors.append({"file": relative(path, directory), "message": exc.strerror or str(exc)})
    except ValueError as exc:
        errors.append({"file": relative(path, directory), "message": str(exc)})
    return None


def relative(path, directory):
    try:
        return str(Path(path).relative_to(directory))
    except ValueError:
        return str(path)


def pr_number(url):
    match = re.search(r"/pull/(\d+)", url or "")
    return int(match.group(1)) if match else None


def call_tokens(entry):
    """input + output tokens for one usage entry, or None when unaccounted."""
    reported = entry.get("reported") if isinstance(entry, dict) else None
    if not reported:
        return None
    total = 0
    for turn in reported:
        if not isinstance(turn, dict):
            return None
        i, o = turn.get("input_tokens"), turn.get("output_tokens")
        if type(i) is not int or type(o) is not int or i < 0 or o < 0:
            return None
        total += i + o
    return total


def usage_breakdown(entry):
    """{input, cached, output} raw token counts for one usage entry, or None when unaccounted."""
    reported = entry.get("reported") if isinstance(entry, dict) else None
    if not reported:
        return None
    total = {"input": 0, "cached": 0, "output": 0}
    for turn in reported:
        if not isinstance(turn, dict) or type(turn.get("input_tokens")) is not int or type(turn.get("output_tokens")) is not int:
            return None
        raw = turn.get("raw_input_tokens")
        total["input"] += raw if type(raw) is int else turn["input_tokens"]
        cached = turn.get("cached_input_tokens")
        total["cached"] += cached if type(cached) is int else 0
        total["output"] += turn["output_tokens"]
    return total


def read_tail(path, limit_bytes=TAIL_BYTES, limit_lines=TAIL_LINES):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(size - limit_bytes, 0))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if size > limit_bytes and lines:
        lines = lines[1:]  # drop the partial first line
    return "\n".join(lines[-limit_lines:])


DIGEST_BYTES = 1 << 20
UNITTEST_HEAD = re.compile(r"^(FAIL|ERROR): (\S+)(?: \((.*)\))?\s*$")
EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Warning|Timeout|Exit))\b:?\s*(.*)$")
PYTEST_FAILED = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")


def log_digest(text):
    """The decision-relevant part of a check log, deterministically.

    unittest/Django: each FAIL/ERROR block becomes one finding with the test
    name and the last exception line; the "Ran N tests" and FAILED/OK lines
    become the summary. pytest short summaries are read the same way. Identical
    causes are grouped so three tests failing on one misconfiguration read as
    one line. Returns None when the log has none of these shapes.
    """
    lines = text.splitlines()
    findings, summary, ran, verdict = [], None, None, None
    i = 0
    while i < len(lines):
        head = UNITTEST_HEAD.match(lines[i].strip())
        if head:
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("=====") and not (lines[i].startswith("-----") and len(block) > 1 and block[-1].strip() == ""):
                block.append(lines[i])
                i += 1
            exc = None
            for line in block:
                m = EXC_LINE.match(line.strip())
                if m:
                    exc = (m.group(1) + (": " + m.group(2) if m.group(2) else "")).strip()
            findings.append({"kind": head.group(1), "test": head.group(2), "where": head.group(3) or "",
                             "message": exc or next((l.strip() for l in reversed(block) if l.strip()), "")})
            continue
        m = PYTEST_FAILED.match(lines[i].strip())
        if m and "::" in m.group(2):
            findings.append({"kind": "FAIL" if m.group(1) == "FAILED" else "ERROR", "test": m.group(2).split("::")[-1],
                             "where": m.group(2), "message": (m.group(3) or "").strip()})
        stripped = lines[i].strip()
        if stripped.startswith("Ran ") and " test" in stripped:
            ran = stripped
        elif stripped.startswith(("FAILED (", "OK")) and (len(stripped) < 80):
            verdict = stripped
        elif re.match(r"^=+ .*(passed|failed|error).* in [\d.]+s.* =+$", stripped):
            ran = stripped.strip("= ")
        i += 1
    if ran or verdict:
        summary = " · ".join(x for x in (ran, verdict) if x)
    if not findings and not summary:
        return None
    groups = []
    for f in findings:
        for g in groups:
            if g["message"] == f["message"] and g["kind"] == f["kind"]:
                g["tests"].append(f["test"])
                break
        else:
            groups.append({"kind": f["kind"], "message": f["message"], "tests": [f["test"]]})
    return {"findings": findings, "groups": groups, "summary": summary,
            "failures": sum(1 for f in findings if f["kind"] == "FAIL"),
            "errors": sum(1 for f in findings if f["kind"] == "ERROR")}


def digest_file(path):
    try:
        with open(path, "rb") as handle:
            text = handle.read(DIGEST_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    return log_digest(text)


def diff_stats(path):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    files = added = removed = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"files": files, "added": added, "removed": removed, "bytes": len(text)}


def earliest_mtime(directory):
    times = []
    try:
        for entry in os.scandir(directory):
            if entry.is_file(follow_symlinks=False):
                times.append(entry.stat().st_mtime)
    except OSError:
        return None
    return min(times) if times else None


def latest_mtime(directory):
    times = []
    try:
        for root, _dirs, names in os.walk(directory):
            for name in names:
                stamp = mtime(Path(root) / name)
                if stamp is not None:
                    times.append(stamp)
    except OSError:
        return None
    return max(times) if times else None


def resolve_log(recorded, run_dir, directory):
    """Map a recorded log path onto a file the server may serve.

    Checkpoints store absolute paths from the machine that wrote them. Prefer
    the recorded path when it is inside the evidence directory; otherwise look
    for the same file name in the runner directory (the examples fixture was
    recorded elsewhere). Anything outside the directory is never served.
    """
    candidates = []
    if recorded:
        candidates.append(Path(recorded))
        candidates.append(Path(run_dir) / Path(recorded).name)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(directory):
            return str(resolved), True
    return recorded, False


def model_for(role, packet):
    if role == "worker" and packet:
        return {"model": packet.get("worker_model"), "reasoning": packet.get("worker_reasoning")}
    if role == "reviewer":
        return {"model": (packet or {}).get("reviewer_model", REVIEW_MODEL[0]),
                "reasoning": (packet or {}).get("reviewer_reasoning", REVIEW_MODEL[1])}
    if role == "advisory":
        return {"model": ADVISORY_MODEL[0], "reasoning": ADVISORY_MODEL[1]}
    return {"model": None, "reasoning": None}


def review_document(review):
    if not isinstance(review, dict):
        return None
    findings = [f for f in review.get("findings", []) if isinstance(f, dict)]
    return {"candidate": review.get("candidate"),
            "covered_files": review.get("covered_files", []),
            "blocking": [f for f in findings if f.get("severity") == "blocking"],
            "notes": [f for f in findings if f.get("severity") != "blocking"]}


# ---------------------------------------------------------------- packet

def read_runner(run_dir, packet, directory, errors, now):
    """Everything the detail panel needs about one runner directory."""
    run_dir = Path(run_dir)
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return None
    state = load_json(state_path, directory, errors)
    if not isinstance(state, dict):
        return {"phase": None, "unreadable": True, "checkpoint_at": mtime(state_path)}
    if packet is None:
        packet = load_json(run_dir / "packet.json", directory, errors) \
            if (run_dir / "packet.json").is_file() else None
    packet = packet if isinstance(packet, dict) else {}
    calls = state.get("calls", 0) if type(state.get("calls")) is int else 0
    roles = {}
    for entry in state.get("prompts", []) or []:
        if isinstance(entry, dict) and type(entry.get("call")) is int:
            roles[entry["call"]] = entry.get("role")
    usage = {}
    for entry in state.get("usage", []) or []:
        if isinstance(entry, dict) and type(entry.get("call")) is int:
            usage[entry["call"]] = entry
            roles.setdefault(entry["call"], entry.get("role"))

    inflight_label = str((state.get("inflight") or {}).get("label", "")) if isinstance(state.get("inflight"), dict) else ""
    checkpoint_at = mtime(state_path)
    call_rows, claim, tokens_known, tokens = [], None, True, 0
    for n in range(1, calls + 1):
        role = roles.get(n)
        row = {"n": n, "role": role, **model_for(role, packet)}
        row["inflight"] = inflight_label == f"model-{n}"
        started = mtime(run_dir / f"prompt-{n}.txt") or mtime(run_dir / f"schema-{n}.json")
        finished = None
        for name in (f"result-{n}.json", f"output-{n}.json", f"model-{n}.log"):
            finished = mtime(run_dir / name)
            if finished is not None:
                break
        row["started_at"] = started
        row["at"] = finished or started
        row["duration_s"] = (finished - started) if (started is not None and finished is not None and finished >= started) else None
        if row["inflight"] and checkpoint_at is not None:
            row["duration_s"] = now - checkpoint_at  # so far
        if n in usage:
            row["tokens"] = call_tokens(usage[n])
            row["usage"] = usage_breakdown(usage[n])
        else:
            row["tokens"] = None
            row["usage"] = None
        result_path = run_dir / f"result-{n}.json"
        row["result"] = result_path.is_file()
        # The runner counts a call before dispatching it. A counted call with no
        # result, no log and no usage in a stopped packet never happened: the
        # budget check refused it. It must not read as lost accounting.
        row["never_dispatched"] = bool(state.get("phase") == "STOPPED" and not row["result"] and not row["inflight"]
                                       and n not in usage and not (run_dir / f"model-{n}.log").is_file())
        if row["tokens"] is not None:
            tokens += row["tokens"]
        elif not (row["inflight"] or row["never_dispatched"]):
            tokens_known = False
        if row["result"]:
            result = load_json(result_path, directory, errors)
            if isinstance(result, dict):
                if "summary" in result:
                    row["summary"] = str(result["summary"]).strip()
                    claim = {"call": n, "summary": row["summary"], "at": row["at"]}
                if "findings" in result:
                    doc = review_document(result)
                    row["blocking"] = len(doc["blocking"])
                    row["notes"] = len(doc["notes"])
        log_path = run_dir / f"model-{n}.log"
        row["log"] = str(log_path.resolve()) if log_path.is_file() else None
        call_rows.append(row)

    worker_ends = [c["at"] for c in call_rows if c.get("role") == "worker" and c.get("at") is not None]
    round_start = max(worker_ends) if worker_ends else None
    checks = read_checks(state, packet, run_dir, directory, now, mtime(state_path), round_start)
    checks_started = round_start if state.get("phase") == "CHECKS" else None
    # What became of each worker patch: the latest one is the candidate under test,
    # earlier ones were superseded by a correction.
    workers = [c for c in call_rows if c.get("role") == "worker"]
    for i, c in enumerate(workers):
        if i + 1 < len(workers):
            c["outcome"] = "superseded by call " + str(workers[i + 1]["n"])
        elif state.get("phase") == "APPLYING":
            c["outcome"] = "applying"
        elif state.get("phase") == "CHECKS":
            c["outcome"] = "applied · checks running"
        elif state.get("phase") in {"REVIEW", "LOCAL_REVIEWED"}:
            c["outcome"] = "applied · checks passed"
        elif state.get("phase") == "STOPPED":
            c["outcome"] = "stopped"
        else:
            c["outcome"] = None
    diff = None
    diff_path = run_dir / "candidate.diff"
    if diff_path.is_file():
        diff = diff_stats(diff_path) or {}
        diff["path"] = str(diff_path.resolve())
        diff["at"] = mtime(diff_path)
    candidate = state.get("candidate") if isinstance(state.get("candidate"), str) else None
    inflight = state.get("inflight") if isinstance(state.get("inflight"), dict) else None
    if inflight:
        inflight = dict(inflight)
        inflight["since"] = mtime(run_dir / (str(inflight.get("label", "")) + ".log"))
    feedback = state.get("feedback") if isinstance(state.get("feedback"), str) else ""
    return {
        "phase": state.get("phase"),
        "calls": calls,
        "max_calls": packet.get("max_calls"),
        "corrections": state.get("corrections", 0),
        "max_corrections": packet.get("max_corrections"),
        "tokens": tokens if tokens_known else None,
        "tokens_incomplete": not tokens_known,
        "calls_settled": sum(1 for c in call_rows if not (c["inflight"] or c["never_dispatched"])),
        "candidate": candidate,
        "candidate_short": candidate[:8] if candidate else None,
        "reason": state.get("reason"),
        "feedback": " ".join(feedback.split())[:600],
        "feedback_summary": feedback_summary(feedback),
        "inflight": inflight,
        "deadline": state.get("deadline"),
        "checkpoint_at": mtime(state_path),
        "started": mtime(run_dir / "packet.json") or earliest_mtime(run_dir),
        "call_list": call_rows,
        "claim": claim,
        "checks": checks,
        "checks_started": checks_started,
        "review": review_document(state.get("review")),
        "advisory": review_document(state.get("advisory")),
        "diff": diff,
        "run_dir": str(run_dir.resolve()),
        "worker": model_for("worker", packet),
        "owned_files": packet.get("owned_files", []),
        "advisory_review": bool(packet.get("advisory_review")),
    }


def feedback_summary(feedback):
    """One plain sentence for the last correction's cause, from the runner's feedback text."""
    text = " ".join(str(feedback or "").split())
    if not text:
        return None
    if text.startswith("Prescribed checks failed:"):
        failed = re.findall(r'"exit_code":\s*([1-9]\d*),\s*"id":\s*"([^"]+)"', text)
        if failed:
            return "failed checks: " + ", ".join(i for _c, i in failed) + " (log excerpts were sent to the worker)"
        return "failed checks (log excerpts were sent to the worker)"
    if text.startswith("Reviewer findings") or '"severity"' in text:
        return "blocking review findings were sent to the worker"
    return text[:200] + ("…" if len(text) > 200 else "")


def read_checks(state, packet, run_dir, directory, now, checkpoint_at=None, round_start=None):
    """Current check results plus logs from earlier correction rounds.

    The runner records exit codes only when the whole round has run, so a log
    for this round without a recorded result means the check finished and its
    exit code arrives with the next checkpoint.
    """
    commands = {c.get("id"): c for c in packet.get("checks", []) if isinstance(c, dict)}
    rows, seen = [], set()
    round_now = state.get("corrections") if type(state.get("corrections")) is int else 0
    current = state.get("checks") if isinstance(state.get("checks"), dict) else {}
    for result in current.get("results", []) or []:
        if not isinstance(result, dict):
            continue
        recorded = result.get("log")
        path, available = resolve_log(recorded, run_dir, directory)
        name = Path(recorded).name if recorded else ""
        match = re.fullmatch(r"check-(\d+)-(.+)\.log", name)
        correction = int(match.group(1)) if match else round_now
        row = {"id": result.get("id"), "exit_code": result.get("exit_code"),
               "correction": correction,
               "log": path, "log_available": available, "current": correction == round_now,
               "tail": read_tail(path) if available else None,
               "digest": digest_file(path) if available else None,
               "at": mtime(path) if available else None,
               "command": " ".join(commands.get(result.get("id"), {}).get("argv", [])) or None}
        rows.append(row)
        seen.add(name)
    inflight = state.get("inflight") if isinstance(state.get("inflight"), dict) else None
    label = str(inflight.get("label", "")) if inflight else ""
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        names = []
    for name in names:
        match = re.fullmatch(r"check-(\d+)-(.+)\.log", name)
        if not match or name in seen or name == label + ".log":
            continue  # the in-flight log is listed as running below
        path = str((run_dir / name).resolve())
        this_round = int(match.group(1)) == round_now
        rows.append({"id": match.group(2), "exit_code": None, "correction": int(match.group(1)),
                     "log": path, "log_available": True, "current": this_round,
                     "finished": this_round and state.get("phase") == "CHECKS",
                     "tail": read_tail(path), "digest": digest_file(path), "at": mtime(path),
                     "command": " ".join(commands.get(match.group(2), {}).get("argv", [])) or None})
    match = re.fullmatch(r"check-(\d+)-(.+)", label)
    if match and state.get("phase") == "CHECKS":
        log_path = run_dir / (label + ".log")
        exists = log_path.is_file()
        since = checkpoint_at  # the runner checkpoints inflight={label, pid} as the command starts
        rows.append({"id": match.group(2), "exit_code": None, "correction": int(match.group(1)),
                     "log": str(log_path.resolve()) if exists else None,
                     "log_available": exists, "current": True, "running": True,
                     "since": since, "elapsed_s": (now - since) if since else None, "tail": None,
                     "at": None, "command": " ".join(commands.get(match.group(2), {}).get("argv", [])) or None})
    for check_id, spec in commands.items():
        if state.get("phase") == "CHECKS" and not any(r["id"] == check_id and r["current"] for r in rows):
            rows.append({"id": check_id, "exit_code": None, "correction": round_now,
                         "log": None, "log_available": False, "current": True, "pending": True,
                         "tail": None, "at": None, "command": " ".join(spec.get("argv", [])) or None})
    order = {check_id: i for i, check_id in enumerate(commands)}
    for round_no in {r["correction"] for r in rows if r["correction"] is not None}:
        # Checks run one after another in packet order, so each finished log's time
        # minus the previous one's is that check's duration. The first check starts
        # when the round starts (the last worker result was applied just before).
        ran = sorted((r for r in rows if r["correction"] == round_no and r.get("at") is not None and not r.get("running")),
                     key=lambda r: (order.get(r["id"], 99), r["at"]))
        previous = round_start if round_no == round_now else None
        for r in ran:
            r["duration_s"] = (r["at"] - previous) if (previous is not None and r["at"] >= previous) else None
            previous = r["at"]
    rows.sort(key=lambda r: (-(r["correction"] if r["correction"] is not None else -1),
                             not r["current"], order.get(r["id"], 99), str(r["id"])))
    return rows


# ---------------------------------------------------------------- batch

def sibling_runs(directory):
    """Other evidence directories beside this one under the same batch id folder, newest first."""
    parent = directory.parent
    found = []
    try:
        for entry in parent.iterdir():
            if entry.is_dir() and entry != directory and (entry / "state.json").is_file():
                found.append(entry)
    except OSError:
        return []
    return sorted(found, key=lambda e: -(mtime(e / "state.json") or 0))


def expected_duration(batch_phase, manifest, runner):
    """How long the current phase may plausibly sit between checkpoints."""
    if batch_phase == "RUN" and runner:
        phase = runner.get("phase")
        if phase in {"IMPLEMENT", "REVIEW"}:
            return 2700
        if phase == "CHECKS":
            return 3600
        return 600
    if batch_phase in {"CI", "MERGE"}:
        return manifest.get("ci_timeout", 3600) if manifest else 3600
    return 900


def liveness(phase, checkpoint_at, expected_s, now, terminal, abandoned=False, last_activity=None):
    age = (now - checkpoint_at) if checkpoint_at is not None else None
    if phase in terminal:
        status = "terminal"
    elif abandoned:
        status = "abandoned"
    elif age is None:
        status = "unknown"
    elif age <= expected_s:
        status = "running"
    else:
        status = "stale"
    return {"state_age_s": age, "expected_s": expected_s, "status": status, "unit": None,
            "checkpoint_at": checkpoint_at, "last_activity": last_activity}


def collect_batch(directory, state, errors, now):
    manifest_path = directory / "manifest.json"
    manifest = load_json(manifest_path, directory, errors) if manifest_path.is_file() else None
    if not isinstance(manifest, dict):
        if not manifest_path.is_file():
            errors.append({"file": "manifest.json", "message": "file is missing; packet titles and limits unknown"})
        manifest = {}
    tasks = [t for t in manifest.get("tasks", []) if isinstance(t, dict)]
    phase = state.get("phase")
    index = state.get("index", 0) if type(state.get("index")) is int else 0
    completed = {c.get("id"): c for c in state.get("completed", []) if isinstance(c, dict)}
    current_task = state.get("packet") if isinstance(state.get("packet"), dict) else None
    if not tasks and current_task:
        tasks = [current_task]

    packets = []
    for i, task in enumerate(tasks):
        task_id = task.get("id")
        item = directory / str(task_id)
        borrowed = None
        if not (item / "runner").is_dir():
            # A resumed batch (run-continuation-01 beside run/) keeps earlier packets'
            # evidence in the sibling run directory. Read it from there, read-only.
            for sibling in sibling_runs(directory):
                if (sibling / str(task_id) / "runner" / "state.json").is_file():
                    item, borrowed = sibling / str(task_id), str(sibling)
                    break
        run_dir = item / "runner"
        if task_id in completed:
            status = "merged"
        elif i == index and phase == "STOPPED":
            status = "stopped"
        elif i == index and phase not in TERMINAL_BATCH:
            status = "running"
        else:
            status = "queued"
        packet_spec = None
        for candidate in (run_dir / "packet.json", item / "packet.json"):
            if candidate.is_file():
                packet_spec = load_json(candidate, directory, errors)
                break
        if not isinstance(packet_spec, dict) and current_task and current_task.get("id") == task_id:
            packet_spec = current_task
        row = {"id": task_id, "title": task.get("title") or task_id, "status": status, "evidence_from": borrowed,
               "advisory_review": bool(task.get("advisory_review")),
               "worker": model_for("worker", packet_spec if isinstance(packet_spec, dict) else task),
               "owned_files": task.get("owned_files", []), "batch_phase": None, "pr": None,
               "runner": None, "phase": None, "calls": None, "corrections": None, "tokens": None,
               "started": None, "finished": None, "elapsed_s": None}
        if status == "queued":
            packets.append(row)
            continue
        runner = read_runner(run_dir, packet_spec if isinstance(packet_spec, dict) else None,
                             directory, errors, now) if run_dir.is_dir() else None
        if runner and status == "stopped" and runner.get("checks"):
            # Nothing will run after a stop; a "pending" pill would promise otherwise.
            runner["checks"] = [c for c in runner["checks"] if not c.get("pending")]
        row["runner"] = runner
        if runner:
            row["phase"] = runner.get("phase")
            row["calls"] = runner.get("calls")
            row["corrections"] = runner.get("corrections")
            row["tokens"] = runner.get("tokens")
        if i == index and phase not in TERMINAL_BATCH:
            row["batch_phase"] = phase
            if phase not in {"PREPARE", "RUN"} or not runner:
                row["phase"] = row["phase"] or phase
        if status == "stopped":
            row["batch_phase"] = state.get("stopped_phase")
        if status == "merged":
            row["batch_phase"] = "MERGE"
            row["pr"] = {"number": pr_number(completed[task_id].get("url")), "url": completed[task_id].get("url"),
                         "head": completed[task_id].get("head"), "merge": completed[task_id].get("merge")}
        elif i == index and state.get("url"):
            row["pr"] = {"number": state.get("pr") or pr_number(state.get("url")), "url": state.get("url")}
        row["started"] = mtime(item / "prepare.json") or (runner or {}).get("started") or earliest_mtime(item)
        if status == "merged":
            following = directory / str(tasks[i + 1].get("id")) / "prepare.json" if i + 1 < len(tasks) else None
            row["finished"] = mtime(following) if following else None
            if row["finished"] is None:
                row["finished"] = mtime(directory / "state.json") if phase in TERMINAL_BATCH or i + 1 == index \
                    else latest_mtime(item)
        elif status == "stopped":
            row["finished"] = mtime(directory / "state.json")
        if row["started"] is not None:
            end = row["finished"] if row["finished"] is not None else now
            row["elapsed_s"] = max(end - row["started"], 0)
        packets.append(row)

    live = None
    if phase == "RUN" and index < len(packets):
        live = packets[index].get("runner")
    calls_used = state.get("calls", 0) if type(state.get("calls")) is int else 0
    tokens_used = state.get("tokens", 0) if type(state.get("tokens")) is int else None
    incomplete = bool(state.get("usage_incomplete"))
    reason_tokens = None
    if live:
        calls_used += live.get("calls") or 0
        if live.get("tokens") is None:
            incomplete = True
            reason_tokens = "the running packet has calls without reported usage"
        elif tokens_used is not None:
            tokens_used += live["tokens"]
    if state.get("usage_incomplete"):
        reason_tokens = "the batch could not account for every call"
        stopped_runner = packets[index].get("runner") if (phase == "STOPPED" and index < len(packets)) else None
        if stopped_runner and not stopped_runner.get("tokens_incomplete") and any(c.get("never_dispatched") for c in stopped_runner.get("call_list", [])):
            # The supervisor counted a call it then refused; every call that ran has usage.
            incomplete, reason_tokens = False, None
    if incomplete:
        tokens_used = None
    deadline = state.get("deadline") if isinstance(state.get("deadline"), (int, float)) else None
    total = manifest.get("total_timeout")
    started = (deadline - total) if (deadline is not None and type(total) is int) else earliest_mtime(directory)
    checkpoint_at = mtime(directory / "state.json")
    # During RUN the runner checkpoints far more often than the batch; the newest write is the pulse.
    if live and live.get("checkpoint_at") is not None and (checkpoint_at is None or live["checkpoint_at"] > checkpoint_at):
        checkpoint_at = live["checkpoint_at"]
    last_activity = latest_mtime(directory)
    # No terminal checkpoint, yet the deadline has passed: the process died or was
    # superseded. Nothing here is running, so every clock stops at the last write.
    abandoned = phase not in TERMINAL_BATCH and deadline is not None and now > deadline
    if phase in TERMINAL_BATCH and checkpoint_at is not None:
        clock_end = checkpoint_at
    elif abandoned and last_activity is not None:
        clock_end = last_activity
    else:
        clock_end = now
    if abandoned:
        for row in packets:
            if row["status"] == "running" and row["started"] is not None:
                row["finished"] = latest_mtime(directory / str(row["id"])) or clock_end
                row["elapsed_s"] = max(row["finished"] - row["started"], 0)
    current = live or (packets[index].get("runner") if index < len(packets) else None)
    packet_budget = None
    if current:
        packet_budget = {"calls": {"used": current.get("calls"), "limit": current.get("max_calls")},
                         "corrections": {"used": current.get("corrections"), "limit": current.get("max_corrections")},
                         "id": packets[index]["id"]}
    awake = None
    if (directory / "awake.json").is_file():
        awake_doc = load_json(directory / "awake.json", directory, errors)
        awake = awake_doc.get("state") if isinstance(awake_doc, dict) else None
    batch = {
        "id": manifest.get("id") or str(directory.name),
        "mode": "batch",
        "repository": manifest.get("repository"),
        "phase": phase,
        "index": index,
        "total": len(tasks),
        "alive": liveness(phase, checkpoint_at, expected_duration(phase, manifest, live), now, TERMINAL_BATCH,
                          abandoned=abandoned, last_activity=last_activity),
        "abandoned": abandoned,
        "unit": "storehouse-" + str(manifest.get("id")) if manifest.get("id") else None,
        "awake": awake,
        "budgets": {
            "clock": {"used": max(clock_end - started, 0) if started is not None else None, "limit": total,
                      "started": started, "deadline": deadline,
                      "ended": clock_end if (phase in TERMINAL_BATCH or abandoned) else None},
            "calls": {"used": calls_used, "limit": manifest.get("max_calls")},
            "tokens": {"used": tokens_used, "limit": manifest.get("max_reported_tokens"),
                       "incomplete": incomplete, "reason": reason_tokens},
            "packet": packet_budget,
        },
        "stop_requested": (directory / "STOP").exists(),
        "reason": state.get("reason"),
        "stopped_phase": state.get("stopped_phase"),
        "evidence_dir": str(directory),
        "pr": {"number": state.get("pr"), "url": state.get("url")} if state.get("url") else None,
    }
    return batch, packets


def collect_runner(directory, run_dir, packet_spec, errors, now):
    """A single runner directory, as ``examples/smoke-run`` and ``runner.py`` produce."""
    runner = read_runner(run_dir, packet_spec, directory, errors, now)
    packet_spec = packet_spec if isinstance(packet_spec, dict) else {}
    packet_id = packet_spec.get("id") or directory.name
    phase = runner.get("phase") if runner else None
    status = "stopped" if phase == "STOPPED" else "running"
    if phase == "LOCAL_REVIEWED":
        status = "reviewed"
    row = {"id": packet_id, "title": packet_spec.get("objective", packet_id).split(".")[0][:120] or packet_id,
           "status": status, "advisory_review": bool(packet_spec.get("advisory_review")),
           "worker": model_for("worker", packet_spec), "owned_files": packet_spec.get("owned_files", []),
           "batch_phase": None, "pr": None, "runner": runner, "phase": phase,
           "calls": runner.get("calls") if runner else None,
           "corrections": runner.get("corrections") if runner else None,
           "tokens": runner.get("tokens") if runner else None,
           "started": runner.get("started") if runner else None,
           "finished": runner.get("checkpoint_at") if runner and phase in TERMINAL_PACKET else None,
           "elapsed_s": None}
    if row["started"] is not None:
        end = row["finished"] if row["finished"] is not None else now
        row["elapsed_s"] = max(end - row["started"], 0)
    deadline = runner.get("deadline") if runner else None
    total = packet_spec.get("total_timeout")
    started = (deadline - total) if (isinstance(deadline, (int, float)) and type(total) is int) else row["started"]
    checkpoint_at = runner.get("checkpoint_at") if runner else None
    terminal = phase in TERMINAL_PACKET
    last_activity = latest_mtime(run_dir)
    abandoned = not terminal and isinstance(deadline, (int, float)) and now > deadline
    if terminal and checkpoint_at is not None:
        clock_end = checkpoint_at
    elif abandoned and last_activity is not None:
        clock_end = last_activity
    else:
        clock_end = now
    if abandoned and row["started"] is not None:
        row["finished"] = clock_end
        row["elapsed_s"] = max(clock_end - row["started"], 0)
    tokens_incomplete = bool(runner and runner.get("tokens_incomplete"))
    batch = {
        "id": packet_id,
        "mode": "runner",
        "repository": None,
        "phase": phase,
        "index": 0,
        "total": 1,
        "alive": liveness(phase, checkpoint_at, expected_duration("RUN", None, runner), now, TERMINAL_PACKET,
                          abandoned=abandoned, last_activity=last_activity),
        "abandoned": abandoned,
        "unit": None,
        "awake": None,
        "budgets": {
            "clock": {"used": max(clock_end - started, 0) if started is not None else None, "limit": total,
                      "started": started, "deadline": deadline, "ended": clock_end if (terminal or abandoned) else None},
            "calls": {"used": runner.get("calls") if runner else None, "limit": packet_spec.get("max_calls")},
            "tokens": {"used": None if tokens_incomplete else (runner or {}).get("tokens"), "limit": None,
                       "incomplete": tokens_incomplete,
                       "reason": "a call has no reported usage" if tokens_incomplete else None},
            "packet": {"calls": {"used": runner.get("calls") if runner else None, "limit": packet_spec.get("max_calls")},
                       "corrections": {"used": runner.get("corrections") if runner else None,
                                       "limit": packet_spec.get("max_corrections")},
                       "id": packet_id},
        },
        "stop_requested": (directory / "STOP").exists(),
        "reason": runner.get("reason") if runner else None,
        "stopped_phase": phase if phase == "STOPPED" else None,
        "evidence_dir": str(directory),
        "pr": None,
    }
    return batch, [row]


# ---------------------------------------------------------------- phase runs
# A second orchestrator: run_phases.sh drives one headless Claude session per
# phase prompt, writing phase-NN.json (the session's JSON result), phase-NN.err
# and one coordinator.log line per START/END/STOP. Different evidence, same
# operator questions.

LOG_LINE = re.compile(r"^(\S+) (.*)$")
START_LINE = re.compile(r"^START phase (\d+) \((.*?)\)(?: model=(\S+))?$")
END_LINE = re.compile(r"^END phase (\d+) exit=(\d+) staging-status=(\S+) :: (.*)$")
DONE_LINE = re.compile(r"^phase (\d+) already done$")
REVIEW_LINE = re.compile(r"^REVIEW(?:-ONLY)? phase (\d+)(?: round (\d+))?:? ?(.*)$")
CORRECT_LINE = re.compile(r"^CORRECT phase (\d+) round (\d+) on (\S+) \(PR #(\d+)\) model=(\S+)$")
CORRECTED_LINE = re.compile(r"^CORRECTED phase (\d+) round (\d+) exit=(\d+)$")
CI_LINE = re.compile(r"^CI phase (\d+) round (\d+): (waiting|passed|failed|no checks|still pending)")
INPUT_LINE = re.compile(r"^INPUT (build|review|fix|arbiter) phase (\d+) round (\d+) \((\S+)\): (.*)$")
MERGED_LINE = re.compile(r"^MERGED phase (\d+) PR #(\d+) at (\S+)$")
ARBITER_LINE = re.compile(r"^ARBITER phase (\d+) round (\d+)(?: on (\S+) \(PR #(\d+)\) model=(\S+).*|: (fixed and pushed|handed to the owner)[^:]*: ?(.*))$")
REVIEW_TEXT_LIMIT = 20000


def is_phase_run(directory):
    directory = Path(directory)
    return (directory / "coordinator.log").is_file() and any(directory.glob("phase-*.json"))


def parse_stamp(text):
    from datetime import datetime
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def phase_name(prompt_path, number):
    stem = Path(prompt_path).stem if prompt_path else ""
    stem = re.sub(r"^phase-\d+-?", "", stem).replace("-", " ").strip()
    return stem or ("phase " + number)


def phase_result(path, directory, errors):
    """The last JSON line of a phase's output: cost, usage, model and the session's final text."""
    try:
        lines = [l for l in path.read_text(errors="replace").splitlines() if l.strip()]
    except OSError as exc:
        errors.append({"file": relative(path, directory), "message": exc.strerror or str(exc)})
        return None
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except ValueError as exc:
        errors.append({"file": relative(path, directory), "message": str(exc)})
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    ints = lambda k: usage.get(k) if type(usage.get(k)) is int else 0  # noqa: E731
    models = list((data.get("modelUsage") or {}).keys()) if isinstance(data.get("modelUsage"), dict) else []
    tokens = None
    if usage:
        tokens = {"input": ints("input_tokens") + ints("cache_creation_input_tokens") + ints("cache_read_input_tokens"),
                  "cached": ints("cache_read_input_tokens"), "output": ints("output_tokens"),
                  "thinking": (usage.get("output_tokens_details") or {}).get("thinking_tokens") if isinstance(usage.get("output_tokens_details"), dict) else None}
    return {"subtype": data.get("subtype"), "is_error": bool(data.get("is_error")),
            "result": str(data.get("result") or "").strip(), "cost_usd": data.get("total_cost_usd"),
            "duration_api_s": (data.get("duration_api_ms") or 0) / 1000 if isinstance(data.get("duration_api_ms"), (int, float)) else None,
            "turns": data.get("num_turns"), "session": data.get("session_id"), "model": models[0] if models else None,
            "context_window": (data["modelUsage"][models[0]] or {}).get("contextWindow") if models and isinstance(data["modelUsage"][models[0]], dict) else None,
            "tokens": tokens, "stop_reason": data.get("stop_reason"), "terminal_reason": data.get("terminal_reason")}


# The session result only has usage summed over every turn. How full the context got
# is in Claude Code's own transcript: each assistant turn records the input it was
# sent, so the largest input + cache read + cache write is the peak. Transcripts are
# read incrementally and cached, because a running session's file grows every poll.

CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")).expanduser()
_CONTEXT_CACHE = {}
_TRANSCRIPT_CACHE = {}
SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def transcript_context(path):
    """{"peak", "last", "turns"} for one transcript's main thread, or None."""
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    entry = _CONTEXT_CACHE.get(key)
    if entry is None or entry["inode"] != st.st_ino or st.st_size < entry["offset"]:
        entry = {"inode": st.st_ino, "offset": 0, "peak": 0, "last": 0, "ids": set()}
    if st.st_size > entry["offset"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(entry["offset"])
                chunk = fh.read()
        except OSError:
            return None
        end = chunk.rfind(b"\n") + 1  # only whole lines; a half-written one waits for the next poll
        for raw in chunk[:end].splitlines():
            if b'"usage"' not in raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            message = record.get("message")
            if record.get("type") != "assistant" or record.get("isSidechain") or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            size = sum(usage[k] for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                       if type(usage.get(k)) is int)
            entry["peak"], entry["last"] = max(entry["peak"], size), size
            entry["ids"].add(message.get("id") or record.get("uuid"))
        entry["offset"] += end
    _CONTEXT_CACHE[key] = entry
    if not entry["ids"]:
        return None
    return {"peak": entry["peak"], "last": entry["last"], "turns": len(entry["ids"])}


def session_transcript(session):
    """A finished session's transcript, found by its id under any project folder."""
    if not isinstance(session, str) or not SESSION_ID.match(session):
        return None
    if session not in _TRANSCRIPT_CACHE or not _TRANSCRIPT_CACHE[session].is_file():
        hits = sorted(CLAUDE_PROJECTS.glob("*/" + session + ".jsonl"))
        if not hits:
            return None
        _TRANSCRIPT_CACHE[session] = hits[0]
    return _TRANSCRIPT_CACHE[session]


def first_user_text(path, max_lines=40):
    try:
        with open(path, "rb") as fh:
            for _ in range(max_lines):
                raw = fh.readline()
                if not raw:
                    return None
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                message = record.get("message")
                if record.get("type") == "user" and isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
    except OSError:
        return None
    return None


def live_transcript(prompt_path, started):
    """A running phase session's transcript: in the folder Claude Code keeps for the prompt's
    repository, written since the phase started, and opening with the phase prompt."""
    if not prompt_path or started is None:
        return None
    key = (prompt_path, started)
    if key in _TRANSCRIPT_CACHE:
        return _TRANSCRIPT_CACHE[key]
    prompt = Path(prompt_path)
    repo = next((d for d in prompt.parents if (d / ".git").exists()), None)
    try:
        heading = prompt.read_text(errors="replace").split("\n", 1)[0].strip()
    except OSError:
        return None
    if repo is None or not heading:
        return None
    # Claude Code names a project's folder after its path with every other character a hyphen.
    folder = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    try:
        candidates = sorted((p for p in folder.glob("*.jsonl") if p.stat().st_mtime >= started - 5),
                            key=lambda p: -p.stat().st_mtime)
    except OSError:
        return None
    for path in candidates:
        text = first_user_text(path)
        if text is not None and text.split("\n", 1)[0].strip() == heading:
            _TRANSCRIPT_CACHE[key] = path
            return path
    return None


# Proof of life. run_phases.sh opens and flocks $LOG/lock for as long as it runs, and every
# session it starts inherits that descriptor. So the live processes holding the lock file
# open are exactly the runner and what it is doing. This only looks; it never takes the
# lock, which could make a runner starting at that instant refuse to run.

_HOLDERS_CACHE = {}


def _proc_start(pid):
    try:
        fields = Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()
        with open("/proc/stat") as fh:
            boot = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
        return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _describe(argv):
    line = " ".join(argv)
    if "run_phases.sh" in line:
        rest = line.split("run_phases.sh", 1)[1].split()
        return "runner", "run_phases.sh " + (" ".join(rest) or "run")
    if argv and os.path.basename(argv[0]) == "claude" and "-p" in argv and "fable" in line:
        return "session", "Fable arbiter"
    if argv and os.path.basename(argv[0]) == "claude" and "-p" in argv:
        return "session", "Claude review" if "Read,Grep,Glob" in line else "Claude session"
    if "codex" in line and " exec" in line and os.path.basename(argv[0]) != "timeout":
        return "session", "GPT review"
    return None, None


def lock_holders(directory, now=None, proc=Path("/proc")):
    """{"alive", "runner", "work"} from the processes holding directory/lock open."""
    lock = Path(directory) / "lock"
    try:
        target = lock.stat()
    except OSError:
        return {"alive": False, "runner": None, "work": []}
    key = (str(lock), int(now or time.time()) // 2)
    if key in _HOLDERS_CACHE:
        return _HOLDERS_CACHE[key]
    runner, work, alive = None, [], False
    try:
        pids = [d.name for d in proc.iterdir() if d.name.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        try:
            fds = list((proc / pid / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                st = os.stat(fd)
            except OSError:
                continue
            if st.st_ino == target.st_ino and st.st_dev == target.st_dev:
                break
        else:
            continue
        alive = True
        try:
            argv = [a for a in (proc / pid / "cmdline").read_bytes().decode(errors="replace").split("\0") if a]
        except OSError:
            continue
        kind, label = _describe(argv)
        entry = {"pid": int(pid), "what": label, "since": _proc_start(pid)}
        if kind == "runner" and (runner is None or (entry["since"] or 0) < (runner["since"] or 0)):
            runner = entry  # the outermost bash; its subshells hold the lock too
        elif kind == "session" and label not in {w["what"] for w in work}:
            work.append(entry)
    result = {"alive": alive, "runner": runner, "work": work}
    _HOLDERS_CACHE.clear()
    _HOLDERS_CACHE[key] = result
    return result


def session_writes(repo, holders):
    """When a live session last wrote its transcript: the model producing output, turn by turn."""
    if not repo or not holders["work"]:
        return []
    since = min((w["since"] for w in holders["work"] if w["since"]), default=None)
    folder = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    try:
        return [m for m in (mtime(p) for p in folder.glob("*.jsonl")) if m is not None and (since is None or m >= since)]
    except OSError:
        return []


# One meaning per colour on the page: green is done and good, blue is working, amber is
# waiting for the owner (nothing is wrong), red is broken. A phase run that stops so the
# owner can merge, or because the owner asked it to, is waiting, not failed.
WAITING_REASONS = ("merge its PR", "merge it, then", "operator requested stop", "working tree not clean")


def tone_for(phase, reason, mode):
    """'good', 'yours' or 'broken' for a finished or stopped run; None while it works."""
    if phase in ("COMPLETE", "LOCAL_REVIEWED"):
        return "good"
    if phase == "STOPPED":
        if mode == "phases" and any(s in (reason or "") for s in WAITING_REASONS):
            return "yours"
        return "broken"
    return None


def collect_phases(directory, errors, now):
    directory = Path(directory)
    log_path = directory / "coordinator.log"
    try:
        log_lines = log_path.read_text(errors="replace").splitlines()
    except OSError as exc:
        errors.append({"file": "coordinator.log", "message": exc.strerror or str(exc)})
        log_lines = []
    planned = []
    script = directory / "run_phases.sh"
    repo = prompts = None
    if script.is_file():
        try:
            text = script.read_text(errors="replace")
        except OSError:
            text = ""
        m = re.search(r"^for n in ((?:\d+\s*)+); do", text, re.MULTILINE)
        if m:
            planned = m.group(1).split()
        m = re.search(r'^PHASES="\$\{PHASES:-([\d\s]+)\}"', text, re.MULTILINE)
        if m:
            planned = m.group(1).split()
        # phases.env (sourced by the script) wins; otherwise the script's own REPO="${REPO:-default}".
        try:
            env = (directory / "phases.env").read_text(errors="replace")
        except OSError:
            env = ""
        def setting(name):
            for source in (env, text):
                m = re.search(r"^" + name + r"=(\S+)", source, re.MULTILINE)
                if m:
                    value = m.group(1).strip('"').strip("'")
                    d = re.fullmatch(r"\$\{" + name + r":-(.*)\}", value)
                    return d.group(1) if d else value
            return None
        repo = setting("REPO")
        prompts = setting("PROMPTS")
        prompts = prompts.replace("$REPO", repo or "") if prompts else None
    phases, order = {}, []
    stop, all_done, last_stamp = None, None, None

    def phase(number):
        if number not in phases:
            phases[number] = {"n": number, "name": "phase " + number, "prompt": None, "started": None, "finished": None,
                              "exit_code": None, "staging_status": None, "already_done": False, "attempts": 0,
                              "summary": None, "result": None, "stderr": None, "stderr_bytes": 0, "status": "queued",
                              "model": None, "review": None, "corrections": [],
                              # Every stretch of work for the timeline: build, review and fix, in order.
                              "segments": []}
            order.append(number)
        return phases[number]

    for number in planned:
        phase(number)
    for raw in log_lines:
        m = LOG_LINE.match(raw.strip())
        if not m:
            continue
        stamp, rest = parse_stamp(m.group(1)), m.group(2)
        last_stamp = stamp or last_stamp
        m2 = START_LINE.match(rest)
        if m2:
            stop, all_done = None, None  # the coordinator was rerun after a stop or after finishing an earlier list
            ph = phase(m2.group(1))
            ph.update(started=stamp, finished=None, exit_code=None, prompt=m2.group(2), name=phase_name(m2.group(2), m2.group(1)),
                      attempts=ph["attempts"] + 1, status="running", model=m2.group(3) or ph["model"], review=None)
            ph["segments"].append({"kind": "build", "start": stamp, "end": None})
            continue
        m2 = REVIEW_LINE.match(rest)
        if m2:
            ph = phase(m2.group(1))
            note = m2.group(3)
            review = ph["review"] or {"pr": None, "posted": False, "notes": [], "started": None, "at": None, "round": None}
            if rest.startswith("REVIEW-ONLY"):
                stop = None
                review["review_only"] = True
            if m2.group(2) is not None:
                review["round"] = int(m2.group(2))
            m3 = re.search(r"PR #(\d+)", note)
            if m3:
                review["pr"] = int(m3.group(1))
            if note.startswith("PR #"):
                review["started"], review["posted"] = stamp, False
                ph["segments"].append({"kind": "review", "start": stamp, "end": None})
            elif "verdict" in note or "no open PR" in note:
                for seg in reversed(ph["segments"]):
                    if seg["kind"] == "review" and seg["end"] is None:
                        seg["end"] = stamp
                        break
            if "posted" in note:
                review["posted"] = True
            if note:
                review["notes"].append(note)
            review["at"] = stamp
            ph["review"] = review
            continue
        m2 = END_LINE.match(rest)
        if m2:
            ph = phase(m2.group(1))
            ph.update(finished=stamp, exit_code=int(m2.group(2)), staging_status=m2.group(3), summary=m2.group(4).strip())
            for seg in reversed(ph["segments"]):
                if seg["kind"] == "build" and seg["end"] is None:
                    seg["end"] = stamp
                    break
            ph["status"] = "merged" if m2.group(3) == "done" and ph["exit_code"] == 0 else "stopped"
            continue
        m2 = DONE_LINE.match(rest)
        if m2:
            stop = None
            ph = phase(m2.group(1))
            ph.update(already_done=True, status="merged")  # done on staging, whatever the earlier pass said
            continue
        m2 = CORRECT_LINE.match(rest)
        if m2:
            stop = None  # a correction started by hand after a stop reopens the run
            ph = phase(m2.group(1))
            ph["corrections"].append({"round": int(m2.group(2)), "branch": m2.group(3), "pr": int(m2.group(4)),
                                      "model": m2.group(5), "started": stamp, "finished": None, "exit_code": None})
            ph["segments"].append({"kind": "fix", "start": stamp, "end": None})
            continue
        m2 = CI_LINE.match(rest)
        if m2:  # the repository's checks, run before any review
            ph = phase(m2.group(1))
            if m2.group(3) == "waiting":
                stop = None
                ph["segments"].append({"kind": "tests", "start": stamp, "end": None})
            else:
                for seg in reversed(ph["segments"]):
                    if seg["kind"] == "tests" and seg["end"] is None:
                        seg.update(end=stamp, outcome=m2.group(3))
                        break
            continue
        m2 = INPUT_LINE.match(rest)
        if m2:  # the receipt for what the session just started was given
            ph = phase(m2.group(2))
            item = {"file": str(directory / m2.group(4)), "receipt": m2.group(5), "at": stamp}
            kind = m2.group(1)
            if kind == "fix" and ph["corrections"]:
                ph["corrections"][-1]["input"] = item
            elif kind == "review" and ph["review"]:
                ph["review"]["input"] = item
            elif kind == "arbiter" and ph.get("arbiter"):
                ph["arbiter"]["input"] = item
            elif kind == "build":
                ph["input"] = item
            continue
        m2 = MERGED_LINE.match(rest)
        if m2:  # the runner merged it itself
            ph = phase(m2.group(1))
            ph.update(status="merged", staging_status="done", auto_merged={"pr": int(m2.group(2)), "head": m2.group(3), "at": stamp})
            continue
        m2 = ARBITER_LINE.match(rest)
        if m2:
            stop = None
            ph = phase(m2.group(1))
            if m2.group(3):  # the arbiter started: it fixes the branch itself or hands it to the owner
                ph["segments"].append({"kind": "arbiter", "start": stamp, "end": None})
                ph["arbiter"] = {"started": stamp, "finished": None, "outcome": None, "summary": None, "model": m2.group(5)}
            else:
                for seg in reversed(ph["segments"]):
                    if seg["kind"] == "arbiter" and seg["end"] is None:
                        seg["end"] = stamp
                        break
                ph["arbiter"] = (ph.get("arbiter") or {}) | {"finished": stamp, "summary": m2.group(7),
                                                            "outcome": "fixed" if m2.group(6).startswith("fixed") else "handed over"}
            continue
        m2 = CORRECTED_LINE.match(rest)
        if m2:
            ph = phase(m2.group(1))
            for c in ph["corrections"]:
                if c["round"] == int(m2.group(2)) and c["finished"] is None:
                    c.update(finished=stamp, exit_code=int(m2.group(3)))
            for seg in reversed(ph["segments"]):
                if seg["kind"] == "fix" and seg["end"] is None:
                    seg["end"] = stamp
                    break
            continue
        if rest.startswith("STOP"):
            stop = {"at": stamp, "reason": rest[4:].lstrip(": ").strip() or "stopped"}
            continue
        if rest.startswith("ALL "):
            all_done = {"at": stamp, "text": rest}
    events_path = directory / "events.jsonl"
    if events_path.is_file():
        # The refactored runner appends one JSON line per step. The text log stays the
        # source of the timeline; events add what the text does not carry.
        try:
            for raw in events_path.read_text(errors="replace").splitlines():
                if not raw.strip():
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or "phase" not in ev:
                    continue
                ph = phases.get(str(ev["phase"]).zfill(2)) or phases.get(str(ev["phase"]))
                if ph is None:
                    continue
                kind = ev.get("event")
                if kind == "start" and ev.get("attempt") is not None:
                    ph["attempt"] = ev["attempt"]
                elif kind == "verdict" and ph["review"] is not None:
                    ph["review"]["advisory"] = ev.get("advisory")
                    ph["review"]["gating"] = ev.get("gating")
                    ph["review"]["verdict"] = ev.get("verdict")
                    if ev.get("attempt") is not None:
                        ph["review"]["attempt"] = ev["attempt"]
        except OSError as exc:
            errors.append({"file": "events.jsonl", "message": exc.strerror or str(exc)})
    for number in order:
        ph = phases[number]
        owner = directory / f"phase-{number}-owner.md"  # the owner's final decisions for this phase
        try:
            ph["owner"] = {"text": owner.read_text(errors="replace")[:20000], "at": mtime(owner)} if owner.is_file() else None
        except OSError:
            ph["owner"] = None
        for label, suffix in (("claude", "-review-claude.md"), ("gpt", "-review-gpt.md"), ("combined", "-review.md")):
            path = directory / f"phase-{number}{suffix}"
            if path.is_file():
                review = ph["review"] or {"pr": None, "posted": False, "notes": [], "started": None, "at": None, "round": None}
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    text = ""
                stamp = mtime(path)
                started = review.get("started")
                review[label] = {"path": str(path.resolve()), "text": text[:REVIEW_TEXT_LIMIT].strip(),
                                 "truncated": len(text) > REVIEW_TEXT_LIMIT, "bytes": len(text), "at": stamp,
                                 # Written before this round began: it belongs to an earlier round.
                                 "stale": bool(started is not None and stamp is not None and stamp < started - 1)}
                ph["review"] = review
        if ph["review"]:
            for label in ("claude", "gpt"):
                err = directory / f"phase-{number}-review-{label}.err"
                if err.is_file():
                    try:
                        ph["review"][label + "_stderr"] = {"path": str(err.resolve()), "bytes": err.stat().st_size}
                    except OSError:
                        pass
        out = directory / f"phase-{number}.json"
        if out.is_file():
            ph["result"] = phase_result(out, directory, errors)
            ph["output"] = str(out.resolve())
            if ph["result"] and ph["result"].get("is_error") and ph["status"] != "stopped":
                ph["status"] = "stopped"
        result = ph.get("result") or {}
        if result.get("session"):
            context = transcript_context(session_transcript(result["session"]))
        elif ph["status"] == "running":
            context = transcript_context(live_transcript(ph.get("prompt"), ph.get("started")))
        else:
            context = None
        ph["context"] = (context | {"window": result.get("context_window"), "live": not result.get("session")}) if context else None
        err = directory / f"phase-{number}.err"
        if err.is_file():
            ph["stderr"] = str(err.resolve())
            try:
                ph["stderr_bytes"] = err.stat().st_size
            except OSError:
                ph["stderr_bytes"] = 0
        if ph["status"] == "running" and ph["finished"] is None and ph["started"] is not None:
            ph["elapsed_s"] = now - ph["started"]
        elif ph["started"] is not None and ph["finished"] is not None:
            ph["elapsed_s"] = max(ph["finished"] - ph["started"], 0)
        else:
            ph["elapsed_s"] = None
    rows = [phases[n] for n in order]
    running = [r for r in rows if r["status"] == "running"]
    reviewing = [r for r in rows if r["review"] and r["review"].get("started") and not r["review"].get("posted")
                 and not any("could not post" in n or "no open PR" in n for n in r["review"]["notes"])]
    holders = lock_holders(directory, now)
    open_corrections = any(c["finished"] is None for r in rows for c in r["corrections"]) \
        or any(seg["end"] is None and seg["kind"] == "tests" for r in rows for seg in r["segments"]) \
        or any((r.get("arbiter") or {}).get("started") and not r["arbiter"].get("finished") for r in rows)
    died = False
    if not stop and not all_done and (running or reviewing or open_corrections) and not holders["alive"]:
        # The log says work is in progress but no process holds the lock: the runner was
        # killed, crashed or the machine restarted. Nothing is running; say so.
        died = True
        stop = {"at": mtime(log_path) or now, "reason": "the runner died while it was working (killed, crashed or "
                "the machine restarted); nothing is running. Press Run or Review to carry on"}
        reviewing = []
    # A phase left "running" after a later START or STOP never ended; mark it.
    if stop:
        for r in rows:  # nothing is still going once the loop has stopped or died
            for seg in r["segments"]:
                if seg["end"] is None:
                    seg["end"] = stop["at"]
        for r in running:
            r["status"] = "stopped"
            r["finished"] = r["finished"] or stop["at"]
        running = []
    if all_done:
        phase_word = "COMPLETE"
    elif stop:
        phase_word = "STOPPED"
    elif running or open_corrections or (reviewing and not stop):
        phase_word = "RUN"
    else:
        phase_word = "IDLE"
    starts = [r["started"] for r in rows if r["started"] is not None]
    ends = [r["finished"] for r in rows if r["finished"] is not None]
    started = min(starts) if starts else None
    ended = (all_done or stop or {}).get("at") or (max(ends) if ends else None)
    checkpoint_at = mtime(log_path)
    if phase_word == "RUN":
        # A phase session may run up to three hours (the script's timeout); silence past that is trouble.
        live = liveness("RUN", checkpoint_at, 10800, now, {"COMPLETE", "STOPPED"})
        live["reviewing"] = [r["n"] for r in reviewing]
    else:
        live = liveness(phase_word, checkpoint_at, 7200, now, {"COMPLETE", "STOPPED", "IDLE"})
    tokens_total = sum(r["result"]["tokens"]["input"] + r["result"]["tokens"]["output"]
                       for r in rows if r.get("result") and r["result"].get("tokens"))
    cost_total = sum(r["result"]["cost_usd"] for r in rows if r.get("result") and isinstance(r["result"].get("cost_usd"), (int, float)))
    done = sum(1 for r in rows if r["status"] == "merged")
    batch = {
        "id": directory.name, "mode": "phases", "repository": repo, "prompts": prompts,
        "phase": phase_word, "index": done, "total": len(rows),
        "alive": live, "abandoned": False, "unit": None, "awake": None,
        "budgets": {
            "clock": {"used": ((ended if phase_word != "RUN" else now) - started) if started is not None and (ended or phase_word == "RUN") else None,
                      "limit": None, "started": started, "deadline": None, "ended": ended if phase_word != "RUN" else None},
            "calls": {"used": sum(r["attempts"] for r in rows), "limit": None},
            "tokens": {"used": tokens_total if rows else None, "limit": None, "incomplete": False, "reason": None},
            "packet": None, "cost_usd": cost_total,
        },
        "tone": tone_for(phase_word, stop["reason"] if stop else None, "phases"),
        "proof": {"alive": holders["alive"], "died": died, "runner": holders["runner"], "work": holders["work"],
                  "last_output": max([s for s in [checkpoint_at] + [mtime(p) for p in directory.glob("phase-*")]
                                      + session_writes(repo, holders) if s is not None], default=None)},
        "stop_requested": (directory / "STOP").exists(), "reason": stop["reason"] if stop else None,
        "stopped_phase": ("phase " + running[0]["n"]) if (stop and running) else None,
        "evidence_dir": str(directory), "pr": None,
        "stopped_at": stop["at"] if stop else None,
        "events": events_path.is_file(),
    }
    reviewing_ids = {r["n"] for r in reviewing}
    last_stopped = next((r["n"] for r in reversed(rows) if r["status"] == "stopped"), None)

    def row_tone(r):
        """A phase that ended without merging is usually a healthy PR waiting for review or merge."""
        if r["status"] != "stopped":
            return None
        if not stop and (r["n"] in reviewing_ids or any(c["finished"] is None for c in r["corrections"])
                         or any(seg["end"] is None and seg["kind"] == "tests" for seg in r["segments"])
                         or ((r.get("arbiter") or {}).get("started") and not r["arbiter"].get("finished"))):
            return "working"
        if batch["tone"] == "broken" and r["n"] == last_stopped:
            return "broken"
        res = r.get("result") or {}
        if r["exit_code"] not in (0, None) or res.get("is_error"):
            return "broken"
        return "yours"

    packets = []
    for r in rows:
        res = r.get("result") or {}
        pr = None
        m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)", (res.get("result") or "") + " " + (r.get("summary") or ""))
        if m:
            pr = {"number": int(m.group(1)), "url": m.group(0)}
        packets.append({"id": "phase-" + r["n"], "title": r["name"], "status": r["status"], "advisory_review": bool(r.get("review")),
                        "worker": {"model": res.get("model") or r.get("model"), "reasoning": None}, "owned_files": [], "batch_phase": None,
                        "tone": row_tone(r),
                        "pr": pr, "runner": None, "phase": r["staging_status"], "calls": r["attempts"] or None,
                        "corrections": len(r["corrections"]) if (r["corrections"] or r["status"] != "queued") else None,
                        "tokens": (res["tokens"]["input"] + res["tokens"]["output"]) if res.get("tokens") else None,
                        "started": r["started"], "finished": r["finished"], "elapsed_s": r["elapsed_s"], "evidence_from": None,
                        "phase_run": r})
    return batch, packets


# ---------------------------------------------------------------- what the operator must do

def needs_you(batch, packets):
    """The one thing waiting on a human, or None. Muse's 'waiting for you' register."""
    reason = batch.get("reason") or ""
    phase = batch.get("phase")
    if batch.get("mode") == "phases":
        if (batch.get("proof") or {}).get("died"):
            return {"kind": "inspect", "text": "The runner died while it was working: no process is running for this run any more, although the log says a phase or review was in progress. Press Run (or Review for the open PR) to carry on."}
        if batch.get("stop_requested") and phase in {"STOPPED", "COMPLETE", "IDLE"}:
            return {"kind": "clear_stop", "text": "A STOP file is present. Remove it (or press clear STOP) before the next run."}
        stopped = [p for p in packets if p["status"] == "stopped"]
        last = stopped[-1] if stopped else None
        pr = (last or {}).get("pr")
        rv = ((last or {}).get("phase_run") or {}).get("review") or {}
        if phase == "STOPPED" and ("merge its PR" in reason or "merge it, then" in reason):
            number = last["id"].replace("phase-", "") if last else "?"
            if "last phase" in reason or "the last of the task" in reason:
                text = "Phase " + number + ", the last of this task, is reviewed clean. Merge its PR to finish the task."
            elif "not merged automatically" in reason:
                why = reason.split("not merged automatically:", 1)[1].rsplit("; merge it", 1)[0].strip()
                text = "Phase " + number + " is reviewed clean but did not merge itself: " + why + ". Sort that out and merge it, then press Run."
            else:
                text = "Phase " + number + " is reviewed clean and its PR waits for your merge. Merge it, then press Run."
            if rv.get("advisory") == "BLOCKING":
                text += " The GPT advisory review flagged a blocking finding; read it before merging."
            return {"kind": "merge", "text": text, "pr": pr, "phase": last["id"] if last else None}
        if phase == "STOPPED" and "arbiter handed it to the owner" in reason:
            arb = ((last or {}).get("phase_run") or {}).get("arbiter") or {}
            return {"kind": "decide", "text": "Phase " + (last["id"].replace("phase-", "") if last else "?") + " was still blocking after the automatic fixes, and the arbiter chose not to force a fix: " + (arb.get("summary") or "see its reasoning in the evidence") + " Read its reasoning, write your decisions in the phase's box, then press Save and fix.", "pr": pr, "phase": last["id"] if last else None}
        if phase == "STOPPED" and "still blocking" in reason:
            return {"kind": "decide", "text": "Phase " + (last["id"].replace("phase-", "") if last else "?") + " is still blocking after the allowed correction rounds. Read the review and decide: fix it yourself, or press Correct for one more round.", "pr": pr, "phase": last["id"] if last else None}
        if phase == "STOPPED" and "working tree not clean" in reason:
            return {"kind": "fix", "text": "The checkout has uncommitted changes, so no phase can start. Commit or stash them, then press Run."}
        if phase == "STOPPED" and "operator requested stop" in reason:
            return {"kind": "clear_stop", "text": "You stopped the loop. Remove the STOP file (or press clear STOP), then press Run to continue."}
        if phase == "STOPPED":
            return {"kind": "inspect", "text": "The loop stopped: " + reason + ". Read the evidence, then press Run or Review."}
        return None
    if phase == "STOPPED":
        hint = "Inspect the packet's evidence, then restart the batch from the shell (service.py start) once the cause is fixed."
        if "Operator requested stop" in reason:
            hint = "You stopped it. Remove the STOP file and restart the batch from the shell when ready."
        return {"kind": "inspect", "text": "The batch stopped: " + reason + ". " + hint}
    if batch.get("abandoned"):
        return {"kind": "inspect", "text": "The deadline passed with no final checkpoint. Nothing is running. Inspect the evidence, then restart or archive the run."}
    return None


# ---------------------------------------------------------------- entry

def collect(directory, now=None):
    """Return the monitor document for one evidence directory. Reads only."""
    now = time.time() if now is None else now
    directory = Path(directory).expanduser()
    try:
        directory = directory.resolve()
    except OSError:
        pass
    document = {"generated": now, "time_source": TIME_SOURCE, "directory": str(directory),
                "empty": None, "errors": [], "batch": None, "packets": []}
    errors = document["errors"]
    if not directory.is_dir():
        document["empty"] = "missing"
        return document
    state_path = directory / "state.json"
    if is_phase_run(directory):
        batch, packets = collect_phases(directory, errors, now)
    elif state_path.is_file():
        state = load_json(state_path, directory, errors)
        if not isinstance(state, dict):
            return document
        if "packet_hash" in state and "manifest_hash" not in state:
            packet_path = directory / "packet.json"
            packet_spec = load_json(packet_path, directory, errors) if packet_path.is_file() else None
            batch, packets = collect_runner(directory, directory, packet_spec, errors, now)
        else:
            batch, packets = collect_batch(directory, state, errors, now)
    elif (directory / "run" / "state.json").is_file():
        inner = load_json(directory / "run" / "state.json", directory, errors)
        if isinstance(inner, dict) and "manifest_hash" in inner:
            # batches/<id>/run holds a whole batch; read it as such.
            return collect(directory / "run", now)
        packet_spec = None
        for candidate in (directory / "packet.json", directory / "run" / "packet.json"):
            if candidate.is_file():
                packet_spec = load_json(candidate, directory, errors)
                break
        batch, packets = collect_runner(directory, directory / "run", packet_spec, errors, now)
    else:
        document["empty"] = "preflight"
        return document
    batch["needs_you"] = needs_you(batch, packets)
    document["batch"] = batch
    document["packets"] = packets
    return document


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Print the monitor document for an evidence directory.")
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args(argv)
    json.dump(collect(args.directory), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
