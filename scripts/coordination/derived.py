"""Run a packet derived from a merged pull request, judged by that pull request's own tests.

A derived packet poses a merged change as a task: reproduce the product change from the
base commit. Its tests, as they stood at the merge commit, are the hidden checks. They
are kept outside the checkout and placed over it only while a check command runs, and
every model call reads an isolated source snapshot with no Git history, so neither the
tests nor the merged change can be read by the worker or the reviewer.

    python3 -m scripts.coordination.derived prepare PACKET --source CLONE --directory DIR
    scripts/coordination/derived_env.sh REPO [CHECKOUT]     # prints the interpreter to validate with
    python3 -m scripts.coordination.derived validate --directory DIR --python /env/bin/python
    python3 -m scripts.coordination.derived run --directory DIR --worker-model M --worker-reasoning R \\
        --auth-home ~/.codex --live

prepare  clones CLONE at the packet's base commit into DIR/checkout and saves the hidden
         files from the merge commit under DIR/hidden. No model, no check.
validate proves the packet can be judged, with no model call: the checks must fail at the
         base commit and pass once the merged change to the owned files is applied. The
         interpreter given replaces a leading "python" in each check command and must
         already hold the target repository's test dependencies.
run      makes one attempt in a fresh checkout under DIR/attempts/NAME: a worker, the
         primary reviewer, and no advisory review. It refuses without --live, because it
         makes real model calls, and without a passing validation. The worker edits a copy
         with its own tools in a Docker container (native.py), with the validated interpreter
         mounted read-only for the visible tests; --read-only-worker restores the older call
         that returns a diff inside a JSON answer.

This path has no plan authority: it is for public repositories that have no plan
lifecycle, and it refuses a clone that has one.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .isolated import IsolatedAdapter
from .native import NativeAdapter
from .runner import REVIEW_MODEL, Runner, RunnerError, git, hidden_overlay, save_json, validate_packet

PLAN = "derived-from-merged-pull-request"
DEFAULT_LIMITS = {"max_calls": 6, "max_corrections": 2, "call_timeout": 1800, "total_timeout": 7200}
LOOP_FIELDS = ("id", "base_sha", "branch", "objective", "acceptance", "owned_files", "checks")


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RunnerError(f"Cannot read {path}") from exc


def derived_packet(path):
    packet = load(path)
    hidden = packet.get("hidden_checks") if isinstance(packet, dict) else None
    if (not isinstance(packet, dict) or any(key not in packet for key in LOOP_FIELDS)
            or not isinstance(hidden, dict) or not isinstance(hidden.get("ref"), str)
            or not isinstance(hidden.get("files"), list) or not hidden["files"]
            or not all(isinstance(name, str) and name for name in hidden["files"])):
        raise RunnerError("Not a derived packet: the loop fields and hidden_checks (ref, files) are required")
    return packet


def tracked_symlinks(root, commit):
    """Paths the commit stores as symbolic links."""
    entries = git(root, "ls-tree", "-r", "-z", commit).split(b"\0")
    return sorted(os.fsdecode(entry.split(b"\t", 1)[1]) for entry in entries if entry.startswith(b"120000 "))


def clone_at_base(source, destination, packet):
    """A checkout of its own at the base commit, on the packet's branch, with the public repository as origin.

    The loop refuses a candidate that holds a symbolic link. Links the base commit tracks are therefore
    left out of the working tree (a sparse checkout): HEAD is still the base commit, the tree is clean,
    and a link the worker adds is refused as before. Returns the paths left out.
    """
    git(source, "cat-file", "-e", packet["base_sha"] + "^{commit}")
    subprocess.run(["git", "clone", "-q", "--no-checkout", "--local", str(source), str(destination)],
                   check=True, capture_output=True, timeout=600)
    git(destination, "checkout", "-q", "-b", packet["branch"], packet["base_sha"])
    links = tracked_symlinks(destination, packet["base_sha"])
    if links:
        git(destination, "sparse-checkout", "set", "--no-cone", "/*",
            *("!/" + re.sub(r"([*?\[\\])", r"\\\1", name) for name in links))
    if isinstance(packet.get("repo"), str) and packet["repo"]:
        git(destination, "remote", "set-url", "origin", packet["repo"])
    return links


def prepare(args):
    packet = derived_packet(args.packet)
    source, directory = args.source.resolve(), args.directory.resolve()
    if (source / "docs" / "plans" / "ACTIVE.md").exists():
        raise RunnerError("This repository has a plan lifecycle; run its packets through runner --execute")
    if directory.exists():
        raise RunnerError("Choose a new directory; existing evidence is never reset")
    directory.mkdir(mode=0o700, parents=True)
    ref = packet["hidden_checks"]["ref"]
    for name in packet["hidden_checks"]["files"]:
        if name in packet["owned_files"]:
            raise RunnerError("A hidden check file is also an owned file: " + name)
        target = directory / "hidden" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git(source, "show", f"{ref}:{name}"))
    links = clone_at_base(source, directory / "checkout", packet)
    save_json(directory / "derived.json", {"packet": packet, "source": str(source), "omitted_symlinks": links})
    print(f"Prepared {packet['id']} in {directory}: checkout at {packet['base_sha'][:12]}, "
          f"{len(packet['hidden_checks']['files'])} hidden check file(s)"
          + (f", {len(links)} tracked symbolic link(s) left out of the working tree" if links else "")
          + ". Validate it next.")
    return 0


BLACK_CASE = re.compile(r"tests/data/[a-z_]+/([A-Za-z0-9_]+)\.py")


def runnable_argv(packet, argv):
    """The command that actually runs a packet's test targets.

    Black keeps its formatting tests as data files under tests/data/, which are not tests themselves:
    pytest collects nothing from them, or fails importing them. tests/test_format.py runs each one, so
    a command whose targets are all such files is pointed there, selected by case name.
    """
    targets = argv[4:] if argv[:4] == ["python", "-m", "pytest", "-q"] else []
    repo = packet.get("repo")
    if (isinstance(repo, str) and repo.rstrip("/").endswith("/black") and targets
            and all(BLACK_CASE.fullmatch(target) for target in targets)):
        return argv[:4] + ["tests/test_format.py", "-k", " or ".join(BLACK_CASE.fullmatch(t).group(1) for t in targets)]
    return argv


def bound_checks(packet, python):
    """The packet's checks with a leading "python" replaced by the interpreter that holds the test dependencies."""
    return [dict(check, argv=[python if index == 0 and value in ("python", "python3") else value
                              for index, value in enumerate(runnable_argv(packet, check["argv"]))])
            for check in packet["checks"]]


def run_checks(root, checks, hidden, owned, log_prefix):
    results = []
    for check in checks:
        log = Path(f"{log_prefix}-{check['id']}.log")
        with hidden_overlay(root, hidden, owned), log.open("wb") as stream:
            try:
                code = subprocess.run(check["argv"], cwd=root, stdin=subprocess.DEVNULL, stdout=stream,
                                      stderr=subprocess.STDOUT, timeout=check["timeout"], env=Runner.environment()).returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
            except OSError:
                code = "could not start"
        results.append({"id": check["id"], "exit_code": code, "log": str(log)})
    return results


def validate(args):
    directory = args.directory.resolve()
    packet = load(directory / "derived.json")["packet"]
    root, hidden = directory / "checkout", directory / "hidden"
    python = str(args.python.absolute())
    if not os.access(python, os.X_OK):
        raise RunnerError("No executable interpreter at " + python)
    if git(root, "rev-parse", "HEAD").decode().strip() != packet["base_sha"] or git(root, "status", "--porcelain").strip():
        raise RunnerError("The validation checkout is not clean at the base commit")
    checks = bound_checks(packet, python)
    at_base = run_checks(root, checks, hidden, packet["owned_files"], directory / "validate-base")
    merged = git(root, "diff", "--no-ext-diff", "--no-textconv", "--binary", packet["base_sha"],
                 packet["hidden_checks"]["ref"], "--", *packet["owned_files"])
    git(root, "apply", "--whitespace=nowarn", data=merged)
    try:
        with_change = run_checks(root, checks, hidden, packet["owned_files"], directory / "validate-merged")
    finally:
        git(root, "apply", "-R", "--whitespace=nowarn", data=merged)
    if git(root, "status", "--porcelain").strip():
        raise RunnerError("A check left files behind in the checkout; its evidence cannot be trusted")
    fails_at_base = any(result["exit_code"] != 0 for result in at_base)
    passes_merged = all(result["exit_code"] == 0 for result in with_change)
    save_json(directory / "validation.json", {
        "valid": fails_at_base and passes_merged, "python": python, "at_base": at_base, "with_merged_change": with_change})
    print(f"{packet['id']}: checks at base {'fail' if fails_at_base else 'PASS (nothing to do)'}, "
          f"with the merged change {'pass' if passes_merged else 'FAIL'}"
          f" -> {'valid' if fails_at_base and passes_merged else 'not usable'}")
    return 0 if fails_at_base and passes_merged else 1


def run(args):
    if not args.live:
        raise RunnerError("--live is required; an attempt makes real model calls")
    directory = args.directory.resolve()
    record = load(directory / "derived.json")
    packet, validation = record["packet"], load(directory / "validation.json")
    if validation.get("valid") is not True:
        raise RunnerError("The packet did not validate; an attempt could not be judged")
    reviewer = (args.reviewer_model or REVIEW_MODEL[0], args.reviewer_reasoning or REVIEW_MODEL[1])
    # Both roles are in the name, so two pairs on one packet cannot collide.
    name = args.name or f"{args.worker_model}-{args.worker_reasoning}--{reviewer[0]}-{reviewer[1]}".replace(".", "-").replace("/", "-")
    attempt = directory / "attempts" / name
    if attempt.exists():
        raise RunnerError("That attempt exists; choose another --name")
    attempt.mkdir(mode=0o700, parents=True)
    clone_at_base(Path(record["source"]), attempt / "checkout", packet)
    limits = {"max_calls": args.max_calls, "max_corrections": args.max_corrections,
              "call_timeout": args.call_timeout, "total_timeout": args.total_timeout}
    # The launcher, not the packet, decides how far a limit may be raised: the flag is its own ceiling.
    ceilings = {key: value for key, value in limits.items() if value != DEFAULT_LIMITS[key]}
    loop_packet = validate_packet(dict(
        {key: packet[key] for key in LOOP_FIELDS}, checkout=str(attempt / "checkout"),
        checks=bound_checks(packet, validation["python"]), hidden_overlay=str(directory / "hidden"),
        worker_model=args.worker_model, worker_reasoning=args.worker_reasoning, plan=PLAN,
        # One worker and the primary reviewer: no triage call and no second, advisory review.
        luna_triage=False, advisory_review=False, **limits,
        # Only the reviewer the launcher names is recorded; otherwise the loop's pinned one runs, as before.
        **{key: value for key, value in (("reviewer_model", args.reviewer_model),
                                         ("reviewer_reasoning", args.reviewer_reasoning)) if value}), ceilings)
    save_json(attempt / "packet.json", loop_packet)
    decision = json.loads(args.decision_file.read_text()) if args.decision_file else None
    adapter = (IsolatedAdapter(args.auth_home) if args.read_only_worker
               else NativeAdapter(args.auth_home, validation["python"]))
    result = Runner(loop_packet, attempt / "run", adapter, decision=decision,
                    attempt_log=args.attempt_log, ceilings=ceilings).run()
    print(json.dumps({"phase": result["phase"], "calls": result["calls"], "corrections": result["corrections"],
                      "reason": result.get("reason"), "report": str(attempt / "run" / "report.md")}, indent=2))
    return 0 if result["phase"] == "LOCAL_REVIEWED" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare", help="clone at the base commit and save the hidden check files")
    command.add_argument("packet", type=Path, help="derived packet JSON")
    command.add_argument("--source", type=Path, required=True, help="a local clone that holds the base and merge commits")
    command.add_argument("--directory", type=Path, required=True, help="new private directory for this packet")
    command.set_defaults(handler=prepare)
    command = commands.add_parser("validate", help="checks must fail at base and pass with the merged change; no model")
    command.add_argument("--directory", type=Path, required=True)
    command.add_argument("--python", type=Path, required=True, help="interpreter that holds the repository's test dependencies")
    command.set_defaults(handler=validate)
    command = commands.add_parser("run", help="one live attempt in a fresh checkout")
    command.add_argument("--directory", type=Path, required=True)
    command.add_argument("--worker-model", required=True)
    command.add_argument("--worker-reasoning", required=True)
    command.add_argument("--auth-home", type=Path, required=True, help="Codex home whose file login isolated calls copy")
    command.add_argument("--reviewer-model", help="primary reviewer model, e.g. claude-opus-5-5 or meta/<id> in Codex (default: the loop's pinned reviewer)")
    command.add_argument("--reviewer-reasoning", help="the reviewer's effort (default: the loop's pinned effort)")
    for flag, key in (("max-calls", "max_calls"), ("max-corrections", "max_corrections"),
                      ("call-timeout", "call_timeout"), ("total-timeout", "total_timeout")):
        command.add_argument("--" + flag, type=int, default=DEFAULT_LIMITS[key],
                             help=f"limit (default {DEFAULT_LIMITS[key]}); a value above the loop's ceiling raises it for this launch only")
    command.add_argument("--name", help="attempt name (default: worker model and effort, then reviewer model and effort)")
    command.add_argument("--live", action="store_true", help="explicitly authorize real model calls")
    command.add_argument("--read-only-worker", action="store_true",
                         help="run the worker read-only, returning a diff in its answer, instead of editing a copy in a container")
    command.add_argument("--attempt-log", type=Path, help="JSON-lines file the finished attempt is appended to (default: attempts.jsonl beside the run directory)")
    command.add_argument("--decision-file", type=Path, help="JSON object recording the routing decision; stored untouched, never acted on")
    command.set_defaults(handler=run)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (RunnerError, subprocess.SubprocessError, KeyError) as exc:
        print(str(exc) if isinstance(exc, RunnerError) else f"Failed: {type(exc).__name__}; inspect the directory", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
