"""One audited continuation after a stopped local packet is reviewed and merged.

The original terminal run is immutable. This command only seeds the remaining
frozen queue after GitHub proves exact-head CI and merge for packet one.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import tempfile
import time

from .batch import Batch, clean, usage_count, validate_manifest
from .github import GitHub
from .runner import RunnerError, canonical, checkout_lock, digest, git


def create_continuation(original, continuation, review_path, number, expected_head, expected_merge):
    original = Path(original).resolve()
    continuation = Path(continuation).resolve()
    review_path = Path(review_path).resolve()
    if continuation.parent != original.parent or original.parent not in review_path.parents:
        raise RunnerError("Continuation and review must stay in the original private batch directory")
    manifest = validate_manifest(json.loads((original.parent / "manifest.json").read_text()))
    batch = Batch(manifest, continuation)
    root = batch.root
    with checkout_lock(root):
        batch.assert_authority()
        if continuation.exists():
            raise RunnerError("Continuation directory already exists")
        if not (original / "STOP").exists():
            raise RunnerError("Original stop marker missing")
        old = json.loads((original / "state.json").read_text())
        if (old.get("phase"), old.get("stopped_phase"), old.get("index"), old.get("completed")) != (
            "STOPPED", "RUN", 0, [],
        ):
            raise RunnerError("Original run is not the expected terminal first-packet state")
        manifest_hash = digest(canonical(manifest).encode())
        if old.get("manifest_hash") != manifest_hash:
            raise RunnerError("Frozen manifest changed")
        item = original / manifest["tasks"][0]["id"]
        local = json.loads((item / "runner/state.json").read_text())
        if local.get("phase") != "STOPPED" or local.get("inflight") is not None:
            raise RunnerError("Original worker has not handed back")
        if json.loads((item / "packet.json").read_text()) != old.get("packet"):
            raise RunnerError("Frozen packet differs from original run")
        if local.get("calls") != old.get("calls") or len(local.get("usage", [])) != local["calls"]:
            raise RunnerError("Original model-call accounting is incomplete")
        counted_tokens = usage_count(local["usage"])
        if old.get("usage_incomplete") or old.get("tokens") != counted_tokens:
            raise RunnerError("Original reported-token accounting is incomplete")
        if old["calls"] >= manifest["max_calls"] or counted_tokens >= manifest["max_reported_tokens"]:
            raise RunnerError("Original model budget is exhausted")
        if time.time() >= old["deadline"]:
            raise RunnerError("Original wall-clock deadline is exhausted")
        clean(root)
        if git(root, "branch", "--show-current").decode().strip() != old["packet"]["branch"]:
            raise RunnerError("Checkout is not on the stopped packet branch")
        if git(root, "rev-parse", "HEAD").decode().strip() != expected_head:
            raise RunnerError("Checkout is not at the reviewed first-task head")
        review = json.loads(review_path.read_text())
        base = old["packet"]["base_sha"]
        changed = sorted(git(root, "diff", "--name-only", base, expected_head, "--").decode().splitlines())
        diff_hash = digest(git(root, "diff", "--binary", base, expected_head, "--"))
        expected_checks = {check["id"] for check in manifest["tasks"][0]["checks"]} | {"gate-regressions"}
        if (review.get("head") != expected_head or review.get("base") != base
                or review.get("diff_sha256") != diff_hash
                or review.get("reviewed_files") != changed
                or set(review.get("checks", {})) != expected_checks
                or any(value != "PASS" for value in review["checks"].values())):
            raise RunnerError("First-task review and local checks do not bind to exact final diff")
        gate = GitHub(manifest["repository"])
        status, pull = gate.gate(number, expected_head, old["packet"]["branch"])
        if status != "MERGED" or pull.get("merge_commit_sha") != expected_merge:
            raise RunnerError("Corrected exact-head CI gate has not verified the first merge")
        git(root, "fetch", "origin", "main")
        git(root, "merge-base", "--is-ancestor", expected_head, expected_merge)
        git(root, "merge-base", "--is-ancestor", expected_merge, "origin/main")
        if manifest["tasks"][1]["depends_on"] != [manifest["tasks"][0]["id"]]:
            raise RunnerError("Frozen next-task dependency changed")
        runtime_hash = batch.runtime_hash()
        completed = [{"id": manifest["tasks"][0]["id"], "url": pull["html_url"],
                      "head": expected_head, "merge": expected_merge}]
        state = {"manifest_hash": manifest_hash, "runtime_hash": runtime_hash,
                 "phase": "PREPARE", "index": 1, "calls": old["calls"],
                 "tokens": counted_tokens, "completed": completed, "deadline": old["deadline"],
                 "recovery": {"source": str(original), "old_runtime_hash": old["runtime_hash"],
                              "new_runtime_hash": runtime_hash, "review_sha256": digest(review_path.read_bytes()),
                              "first_pr": number, "first_head": expected_head,
                              "first_merge": expected_merge}}
        continuation.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="continuation-stage-", dir=continuation.parent) as stage:
            staged = Path(stage) / "run"
            staged.mkdir(mode=0o700)
            (staged / "manifest.json").write_text(canonical(manifest))
            staged_batch = Batch(manifest, staged)
            staged_batch.state = state
            staged_batch.checkpoint()
            os.rename(staged, continuation)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--continuation", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--merge", required=True)
    args = parser.parse_args(argv)
    try:
        state = create_continuation(args.original, args.continuation, args.review,
                                    args.pr, args.head, args.merge)
    except (RunnerError, OSError, ValueError, KeyError, IndexError) as exc:
        parser.exit(2, str(exc) + "\n")
    print("PREPARE: index", state["index"], "calls", state["calls"], "tokens", state["tokens"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
