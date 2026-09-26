"""Sequential approved batch: prepare -> local runner -> commit -> PR -> CI -> merge.

The manifest is operator-owned trusted input, not model output. Resume reconciles
local transactions and remote facts. Ambiguous worker operations stop for inspection.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from .runner import (Runner, RunnerError, authorize_live, canonical, checkout_lock,
                     digest, fingerprint, git, relative_file, review_notes, save_json, validate_packet)
from .isolated import IsolatedAdapter
from .github import GitHub
from scripts.validate_plans import SELLING_WORKER_MODELS, validate_repository

ACTIVE = "docs/plans/ACTIVE.md"
INBOX = "docs/plans/INBOX.md"
ACTIVE_TEXT = "# Active Storehouse plan\n\n<!-- storehouse-active\ntarget: {}\n-->\n"


def head(root):
    return git(root, "rev-parse", "HEAD").decode().strip()


HIGH_RISK_IMPACTS = ("schema", "data", "valuation", "marketplace_write")


def high_risk(task):
    """Stock, money, schema or marketplace-write packets get the advisory second review."""
    return any(task["impacts"][key].startswith("PRESENT") for key in HIGH_RISK_IMPACTS)


def clean(root):
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise RunnerError("Unrecorded checkout changes; preserve and inspect")


def usage_count(usage):
    total = 0
    for call in usage:
        reported = call.get("reported")
        if not reported:
            raise RunnerError("Unknown token usage; batch cannot advance")
        for turn in reported:
            if not isinstance(turn, dict) or any(type(turn.get(k)) is not int or turn[k] < 0
                                                for k in ("input_tokens", "output_tokens")):
                raise RunnerError("Invalid token usage")
            total += turn["input_tokens"] + turn["output_tokens"]
    return total


def validate_manifest(manifest):
    if set(manifest) != {"id", "checkout", "repository", "auth_home", "tasks", "total_timeout",
                        "max_calls", "max_reported_tokens", "ci_timeout", "authority"}:
        raise RunnerError("Invalid batch manifest fields")
    if not re.fullmatch(r"[a-z0-9-]{1,40}", manifest["id"]):
        raise RunnerError("Invalid batch ID")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["repository"]):
        raise RunnerError("Invalid GitHub repository")
    for k, high in (("total_timeout", 28800), ("max_calls", 24),
                    ("max_reported_tokens", 4000000), ("ci_timeout", 3600)):
        if type(manifest[k]) is not int or not 1 <= manifest[k] <= high:
            raise RunnerError("Invalid global budget: " + k)
    for k in ("checkout", "auth_home"):
        if not Path(manifest[k]).is_absolute():
            raise RunnerError("Absolute operator paths required")
    if not isinstance(manifest["tasks"], list) or not 1 <= len(manifest["tasks"]) <= 4:
        raise RunnerError("Batch requires one to four frozen tasks")
    seen = set()
    for task in manifest["tasks"]:
        if set(task) != {"id", "title", "objective", "acceptance", "owned_files", "checks",
                         "worker_model", "worker_reasoning", "impacts", "depends_on"}:
            raise RunnerError("Invalid frozen task fields")
        packet = dict(task)
        for k in ("title", "impacts", "depends_on"):
            del packet[k]
        packet.update(checkout=manifest["checkout"], base_sha="0" * 40,
                      branch="codex/" + task["id"], plan="docs/plans/tasks/" + task["id"] + ".md")
        validate_packet(packet)
        if not task["id"].startswith("selling-") or task["id"] in seen:
            raise RunnerError("Unique selling task IDs required")
        if not set(task["depends_on"]) <= seen:
            raise RunnerError("Dependencies must precede their task")
        if SELLING_WORKER_MODELS.get(task["worker_model"]) != task["worker_reasoning"]:
            raise RunnerError("Unapproved worker model")
        if set(task["impacts"]) != {"schema", "data", "valuation", "reporting", "marketplace_write", "production"}:
            raise RunnerError("All plan impacts required")
        if any(not re.match(r"^(NONE|PRESENT) - .+", value) for value in task["impacts"].values()):
            raise RunnerError("Explain every plan impact")
        seen.add(task["id"])
    if not isinstance(manifest["authority"], str) or not manifest["authority"].startswith("docs/workflows/"):
        raise RunnerError("Repository batch authority required")
    return manifest


def reviewer_notes_section(notes):
    """Non-blocking reviewer findings for the owner; they never gate the candidate."""
    if not notes:
        return ""
    lines = ["", "", "## Reviewer notes (non-blocking)", ""]
    for note in notes:
        lines.append(f"- [{note['severity']}] {note['file']}: {note['summary']}")
    return "\n".join(lines)


def plan_text(task, run_dir, status):
    complete = status == "COMPLETE"
    phase = "COMPLETE" if complete else "ACTIVE" if status == "IN_PROGRESS" else "PENDING"
    control = {"id": task["id"], "status": status,
               "active_phase": "NONE" if status in {"DRAFT", "COMPLETE"} else "P1",
               "agent_strategy": "SEQUENTIAL_WORKER",
               "implementation_authority": {"DRAFT": "PLAN_ONLY", "READY": "GOAL_REQUIRED",
                   "IN_PROGRESS": "GOAL_GRANTED", "COMPLETE": "CLOSED"}[status],
               "merge_authority": "OWNER_GRANTED", "release_authority": "OWNER_REQUIRED"}
    control.update({k + "_impact": v for k, v in task["impacts"].items()})
    control["tbd"] = "NONE"
    owned = ", ".join(task["owned_files"])
    evidence = ("Exact candidate checks and complete-file acceptance review passed. "
                "Private evidence: " + str(run_dir)) if complete else "PENDING"
    return ("# " + task["title"] + "\n\n<!-- storehouse-plan\n" +
            "\n".join(k + ": " + v for k, v in control.items()) + "\n-->\n\n" +
            "## Objective\n\n" + task["objective"] + "\n\n## Scope\n\n" +
            "Frozen files: " + owned + ". No production, real marketplace requests, website or unrelated changes.\n\n" +
            "## Acceptance criteria\n\n" + "\n".join("- " + x for x in task["acceptance"]) +
            "\n\n## Impact and authority detail\n\n" +
            "\n".join("- " + k + ": " + v for k, v in task["impacts"].items()) +
            "\n\nOnly additive migrations; no backfill/destructive schema changes. Before deployment require backup, "
            "migration rehearsal and explicit rollback assessment; application rollback does not reverse schema. "
            "Writes remain off by default. Missing real Sandbox qualification remains a release blocker. "
            "Owner granted implementation/merge for the named eBay batch; no release grant.\n\n" +
            "## Agent strategy\n\n### Worker: implementation\n\n- Project: unified-selling\n- Model: " + task["worker_model"] +
            "\n- Reasoning: " + task["worker_reasoning"] + "\n- Max workers: 1\n- Owns: " + owned +
            "\n- Deliverable: Frozen objective, all acceptance examples and focused regression coverage.\n" +
            "- Coordinator owns: docs/plans/tasks/" + task["id"] + ".md, docs/plans/ACTIVE.md, docs/plans/INBOX.md\n" +
            "- Review owner: PRIMARY\n\nHandoff: " + str(run_dir) + "\n\n" +
            "## Phases\n\n### P1 - Implement and verify the frozen contract\n\n- State: " + phase +
            "\n- Depends on: NONE\n- Files: " + owned +
            "\n- Acceptance: Every acceptance example passes fixed checks and complete-diff review.\n\n" +
            "## Verification\n\n- Focused: " + "; ".join(c["id"] for c in task["checks"]) +
            "\n- Baseline: required repository CI on exact final head before merge.\n" +
            "- Additional: synthetic stock interleavings, unknown outcomes and disabled write gates.\n" +
            "- Browser smoke: changed UI must have request/template regression coverage and recorded operator flows.\n\n" +
            "## Failure and stopping rules\n\nTwo corrections and six calls per task (seven with an advisory review) within global batch budgets. "
            "Stop and preserve work on scope, authority, usage or evidence failure.\n\n" +
            "## Completion evidence\n\n- Outcome: " + evidence + "\n- Verification: " + evidence +
            "\n- Review: " + evidence + "\n- Pull request: " + ("GITHUB_AUTHORITATIVE" if complete else "PENDING") +
            "\n- Merge: " + ("OWNER_GATE - owner-granted batch; exact-head CI mandatory" if complete else "PENDING") +
            "\n- Production: NOT DEPLOYED\n\n## Progress log\n\n" +
            "- Batch supervisor validated DRAFT, READY and IN_PROGRESS in order before dispatch. "
            "Runtime evidence and original plan are preserved under " + str(run_dir.parent) + ".\n")


def transaction(root, journal, files, message, allowed_existing=()):
    """Replayable exact-file write/stage/commit transaction, including commit response loss."""
    if journal.exists():
        record = json.loads(journal.read_text())
        if record["files"] != files or record["message"] != message:
            raise RunnerError("Local transaction contract changed")
    else:
        staged = git(root, "diff", "--cached", "--name-only").decode().splitlines()
        changed = git(root, "diff", "--name-only").decode().splitlines()
        changed += git(root, "ls-files", "--others", "--exclude-standard").decode().splitlines()
        if staged or not set(changed) <= set(allowed_existing):
            raise RunnerError("Unexpected candidate files before commit")
        record = {"base": head(root), "files": files, "message": message,
                  "before": {p: digest((root / p).read_bytes()) if (root / p).exists() else None for p in files},
                  "owned_hashes": {p: digest((root / p).read_bytes()) if (root / p).exists() else None
                                   for p in allowed_existing}, "tree": None}
        save_json(journal, record)
    current = head(root)
    if current != record["base"]:
        if (not record["tree"] or git(root, "show", "-s", "--format=%T", current).decode().strip() != record["tree"]
                or git(root, "show", "-s", "--format=%P", current).decode().strip() != record["base"]
                or git(root, "show", "-s", "--format=%B", current).decode().strip() != message):
            raise RunnerError("HEAD changed outside recorded commit transaction")
        clean(root)
        return current
    for name, expected in record["owned_hashes"].items():
        actual = digest((root / name).read_bytes()) if (root / name).exists() else None
        if actual != expected:
            raise RunnerError("Reviewed code changed before commit")
    for name, content in files.items():
        target = root / name
        actual = digest(target.read_bytes()) if target.exists() else None
        if actual not in {record["before"][name], digest(content.encode())}:
            raise RunnerError("Control file changed outside commit transaction")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    errors = validate_repository(root)
    if errors:
        raise RunnerError("Plan lifecycle validation failed: " + "; ".join(errors))
    names = sorted(set(files) | set(allowed_existing))
    existing_or_tracked = [p for p in names if (root / p).exists() or git(root, "ls-files", "--", p).strip()]
    git(root, "add", "--", *existing_or_tracked)
    staged = set(git(root, "diff", "--cached", "--name-only").decode().splitlines())
    if not staged or not staged <= set(names):
        raise RunnerError("Unexpected staged files")
    tree = git(root, "write-tree").decode().strip()
    if record["tree"] and tree != record["tree"]:
        raise RunnerError("Index changed during commit transaction")
    record["tree"] = tree
    save_json(journal, record)
    git(root, "commit", "-m", message)
    clean(root)
    return head(root)


class BudgetAdapter:
    def __init__(self, batch, adapter):
        self.batch, self.adapter = batch, adapter

    def __call__(self, runner, role, feedback):
        self.batch.check_budget(current=runner.state)
        return self.adapter(runner, role, feedback)


class Batch:
    def __init__(self, manifest, directory, github=None, adapter=None):
        self.manifest = validate_manifest(manifest)
        self.root = Path(manifest["checkout"]).resolve()
        self.directory = Path(directory).resolve()
        if self.directory == self.root or self.root in self.directory.parents or self.directory in self.root.parents:
            raise RunnerError("Batch evidence must be outside checkout")
        self.github = github or GitHub(manifest["repository"])
        self.adapter = adapter or IsolatedAdapter(manifest["auth_home"])
        self.state = {}

    def runtime_hash(self):
        names = sorted(Path(__file__).parent.glob("*"))
        data = b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in names
                        if p.is_file() and p.suffix in {".py", ".json", ".md", ".ps1"})
        return digest(data)

    def checkpoint(self, **updates):
        self.state.update(updates)
        save_json(self.directory / "state.json", self.state)
        lines = ["# Overnight run " + self.manifest["id"], "", "Status: " + self.state["phase"],
                 "", "Reason: " + self.state.get("reason", "In progress"), "",
                 "Reported tokens: " + str(self.state.get("tokens", 0)),
                 "Model calls: " + str(self.state.get("calls", 0)), "", "## Tasks", ""]
        for item in self.state.get("completed", []):
            lines.append(f"- {item['id']}: merged PR {item['url']}; reviewed head {item['head']}; merge {item['merge']}.")
        lines += ["", "Current index: " + str(self.state["index"]),
                  "Current PR: " + self.state.get("url", "Not opened"),
                  "Remaining tasks: " + ", ".join(t["id"] for t in self.manifest["tasks"][self.state["index"]:]),
                  "Usage incomplete: " + str(self.state.get("usage_incomplete", False)),
                  "Candidate and check/review details remain in task evidence directories.",
                  "No production deployment or live eBay calls are authorized."]
        (self.directory / "report.md").write_text("\n".join(lines) + "\n")

    def check_budget(self, current=None):
        if time.time() >= self.state["deadline"]:
            raise RunnerError("Batch wall-clock deadline exhausted")
        calls, tokens = self.state.get("calls", 0), self.state.get("tokens", 0)
        if current is not None:
            calls += current["calls"] - 1  # Runner increments immediately before dispatch.
            tokens += usage_count(current.get("usage", []))
        if calls >= self.manifest["max_calls"] or tokens >= self.manifest["max_reported_tokens"]:
            raise RunnerError("Global model budget exhausted")

    def assert_authority(self):
        authority = self.root / self.manifest["authority"]
        text = authority.read_text()
        if "OWNER_GRANTED" not in text or any(task["id"] not in text for task in self.manifest["tasks"]):
            raise RunnerError("Named batch is not authorized in repository policy")
        from .configure import approved_tasks
        try:
            approved = approved_tasks(self.root, [task["id"] for task in self.manifest["tasks"]])
        except ValueError as exc:
            raise RunnerError(str(exc))
        if approved != self.manifest["tasks"]:
            raise RunnerError("Task contracts differ from the approved repository queue")
        remote = git(self.root, "remote", "get-url", "origin").decode().strip()
        expected = self.manifest["repository"]
        if remote not in {"https://github.com/" + expected + ".git", "git@github.com:" + expected + ".git"}:
            raise RunnerError("Wrong Git remote")

    def prepare(self, task, item):
        branch = "codex/" + self.manifest["id"] + "-" + str(self.state["index"] + 1)
        plan = "docs/plans/tasks/" + task["id"] + ".md"
        runner_dir = item / "runner"
        if not (item / "prepare.json").exists():
            clean(self.root)
            if "target: NONE" not in (self.root / ACTIVE).read_text():
                raise RunnerError("Another canonical plan is active")
            if (self.root / plan).exists():
                raise RunnerError("Task plan already exists; reconcile rather than duplicate")
            git(self.root, "fetch", "origin", "main")
            base = git(self.root, "rev-parse", "origin/main").decode().strip()
            if self.state["completed"]:
                merge = self.state["completed"][-1]["merge"]
                git(self.root, "merge-base", "--is-ancestor", merge, base)
            save_json(item / "prepare.json", {"base": base, "branch": branch,
                "inbox": (self.root / INBOX).read_text() + "\n- [" + task["title"] + "](tasks/" + task["id"] + ".md)\n"})
        prep = json.loads((item / "prepare.json").read_text())
        if not (item / "prepare-commit.json").exists():
            changed = set(git(self.root, "diff", "--name-only").decode().splitlines())
            changed.update(git(self.root, "ls-files", "--others", "--exclude-standard").decode().splitlines())
            if not changed <= {plan, ACTIVE, INBOX} or git(self.root, "diff", "--cached", "--name-only").strip():
                raise RunnerError("Unexpected files during preparation recovery")
            expected_controls = {
                plan: {plan_text(task, runner_dir, s) for s in ("DRAFT", "READY", "IN_PROGRESS")},
                ACTIVE: {ACTIVE_TEXT.format("NONE"), ACTIVE_TEXT.format(plan)},
                INBOX: {prep["inbox"]},
            }
            for name in changed:
                if not (self.root / name).exists() or (self.root / name).read_text() not in expected_controls[name]:
                    raise RunnerError("Preparation controls changed unexpectedly")
            # A crash after switch is safe: only this frozen branch at this base is accepted.
            current_branch = git(self.root, "branch", "--show-current").decode().strip()
            if current_branch != branch:
                git(self.root, "switch", "-c", branch, prep["base"])
            if head(self.root) != prep["base"]:
                raise RunnerError("Preparation branch has unexpected commits")
            for status in ("DRAFT", "READY", "IN_PROGRESS"):
                text = plan_text(task, runner_dir, status)
                save_json(item / (status.lower() + ".json"), {"plan": text})
                (self.root / plan).write_text(text)
                (self.root / INBOX).write_text(prep["inbox"])
                (self.root / ACTIVE).write_text(ACTIVE_TEXT.format("NONE" if status == "DRAFT" else plan))
                errors = validate_repository(self.root)
                if errors:
                    raise RunnerError("Generated plan failed " + status + ": " + "; ".join(errors))
            # These deterministic control writes form the transaction's input.
        files = {plan: plan_text(task, runner_dir, "IN_PROGRESS"), INBOX: prep["inbox"],
                 ACTIVE: ACTIVE_TEXT.format(plan)}
        sha = transaction(self.root, item / "prepare-commit.json", files,
                          "Authorize " + task["id"] + " in " + self.manifest["id"], allowed_existing=files)
        packet = {k: task[k] for k in ("id", "objective", "acceptance", "owned_files", "checks", "worker_model", "worker_reasoning")}
        packet.update(checkout=str(self.root), base_sha=sha, branch=branch, plan=plan,
                      max_calls=7 if high_risk(task) else 6, max_corrections=2, call_timeout=2700,
                      total_timeout=max(1, min(28800, int(self.state["deadline"] - time.time()))), luna_triage=False,
                      advisory_review=high_risk(task))
        save_json(item / "packet.json", packet)
        self.checkpoint(phase="RUN", packet=packet)

    def run(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with checkout_lock(self.root) as lock_fd:
            state_path = self.directory / "state.json"
            manifest_hash = digest(canonical(self.manifest).encode())
            if state_path.exists():
                self.state = json.loads(state_path.read_text())
                if self.state["manifest_hash"] != manifest_hash:
                    raise RunnerError("Manifest changed during run")
                if self.state.get("runtime_hash") != self.runtime_hash():
                    raise RunnerError("Supervisor code changed; inspect before resuming")
                if self.state["phase"] in {"COMPLETE", "STOPPED"}:
                    return self.state
            else:
                self.assert_authority()
                clean(self.root)
                self.state = {"manifest_hash": manifest_hash, "runtime_hash": self.runtime_hash(), "phase": "PREPARE", "index": 0,
                              "calls": 0, "tokens": 0, "completed": [],
                              "deadline": time.time() + self.manifest["total_timeout"]}
                save_json(self.directory / "manifest.json", self.manifest)
                self.checkpoint()
            try:
                while self.state["phase"] != "COMPLETE":
                    if (self.directory / "STOP").exists():
                        raise RunnerError("Operator requested stop")
                    if time.time() >= self.state["deadline"]:
                        raise RunnerError("Batch wall-clock deadline exhausted")
                    if self.state["runtime_hash"] != self.runtime_hash():
                        raise RunnerError("Supervisor controls changed during run")
                    self.assert_authority()
                    task = self.manifest["tasks"][self.state["index"]]
                    item = self.directory / task["id"]
                    item.mkdir(mode=0o700, exist_ok=True)
                    phase = self.state["phase"]
                    if phase != "PREPARE" and git(self.root, "branch", "--show-current").decode().strip() != self.state["packet"]["branch"]:
                        raise RunnerError("Checkout branch changed during batch")
                    if phase == "PREPARE":
                        self.check_budget()
                        self.prepare(task, item)
                    elif phase == "RUN":
                        packet = self.state["packet"]
                        authorize_live(packet, item / "runner")
                        runner = Runner(packet, item / "runner", BudgetAdapter(self, self.adapter))
                        result = runner.run(resume=(item / "runner/state.json").exists(), inherited_lock=lock_fd)
                        if result["phase"] != "LOCAL_REVIEWED":
                            raise RunnerError("Packet stopped: " + result.get("reason", "inspect evidence"))
                        runner.assert_candidate()
                        if result["checks"]["candidate"] != result["candidate"] or result["review"]["candidate"] != result["candidate"]:
                            raise RunnerError("Stale handback evidence")
                        advisory = result.get("advisory")
                        if packet.get("advisory_review") and (not advisory or advisory["candidate"] != result["candidate"]):
                            raise RunnerError("High-risk handback lacks a current advisory review")
                        self.checkpoint(phase="COMMIT", candidate=result["candidate"],
                            review_notes=review_notes(result["review"]) + (review_notes(advisory) if advisory else []),
                            calls=self.state["calls"] + result["calls"], tokens=self.state["tokens"] + usage_count(result["usage"]))
                    elif phase == "COMMIT":
                        packet = self.state["packet"]
                        if not (item / "final-commit.json").exists() and fingerprint(self.root) != self.state["candidate"]:
                            raise RunnerError("Candidate changed after review")
                        files = {packet["plan"]: plan_text(task, item / "runner", "COMPLETE"), ACTIVE: ACTIVE_TEXT.format("NONE")}
                        sha = transaction(self.root, item / "final-commit.json", files,
                                          "Implement " + task["id"], allowed_existing=task["owned_files"])
                        self.checkpoint(phase="PUSH", head=sha)
                    elif phase == "PUSH":
                        clean(self.root)
                        if head(self.root) != self.state["head"]:
                            raise RunnerError("HEAD changed before push")
                        # No force push. A response loss safely repeats the same exact ref.
                        git(self.root, "push", "origin", self.state["head"] + ":refs/heads/" + self.state["packet"]["branch"])
                        self.checkpoint(phase="PR")
                    elif phase == "PR":
                        pull = self.github.ensure_pull(self.state["packet"]["branch"], task["title"],
                            "## Outcome\n\n" + task["objective"] + "\n\n## Verification\n\n"
                            "Prescribed local checks and complete-file acceptance review passed. "
                            "Required repository CI must pass on the exact head before owner-authorized automatic merge.\n\n"
                            "## Scope\n\nPlan: " + self.state["packet"]["plan"] + ". No deployment or live eBay calls."
                            + reviewer_notes_section(self.state.get("review_notes", [])))
                        self.checkpoint(phase="CI", pr=pull["number"], url=pull["html_url"],
                                        ci_deadline=min(self.state["deadline"], time.time() + self.manifest["ci_timeout"]))
                    elif phase in {"CI", "MERGE"}:
                        status, pull = self.github.gate(self.state["pr"], self.state["head"], self.state["packet"]["branch"])
                        if status == "MERGED":
                            done = self.state["completed"] + [{"id": task["id"], "url": self.state["url"],
                                "head": self.state["head"], "merge": pull["merge_commit_sha"]}]
                            index = self.state["index"] + 1
                            self.checkpoint(completed=done, index=index,
                                            phase="COMPLETE" if index == len(self.manifest["tasks"]) else "PREPARE")
                        elif time.time() >= self.state["ci_deadline"]:
                            raise RunnerError("CI/merge deadline exhausted")
                        elif status == "READY":
                            self.checkpoint(phase="MERGE")
                            self.github.merge(self.state["pr"], self.state["head"], self.state["packet"]["branch"])
                        else:
                            time.sleep(min(30, max(1, self.state["ci_deadline"] - time.time())))
                    else:
                        raise RunnerError("Unknown batch phase")
            except (RunnerError, OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
                reason = str(exc) if isinstance(exc, RunnerError) else type(exc).__name__ + "; inspect private checkpoint"
                partial = {}
                packet_state = self.directory / self.manifest["tasks"][min(self.state["index"], len(self.manifest["tasks"])-1)]["id"] / "runner/state.json"
                if self.state["phase"] == "RUN" and packet_state.exists():
                    live = json.loads(packet_state.read_text())
                    partial["calls"] = self.state["calls"] + live["calls"]
                    try:
                        partial["tokens"] = self.state["tokens"] + usage_count(live.get("usage", []))
                        partial["usage_incomplete"] = len(live.get("usage", [])) != live["calls"]
                    except RunnerError:
                        partial["usage_incomplete"] = True
                self.checkpoint(phase="STOPPED", stopped_phase=self.state["phase"], reason=reason, **partial)
            return self.state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = validate_manifest(json.loads(args.manifest.read_text()))
        batch = Batch(manifest, args.directory)
        batch.assert_authority()
        if not args.execute:
            print("Batch manifest and named authority valid; no work started.")
            return 0
        from .service import preflight
        preflight(manifest, args.directory.resolve())
        result = batch.run()
        print(result["phase"] + ": " + str(args.directory / "report.md"))
        return 0 if result["phase"] == "COMPLETE" else 1
    except (RunnerError, OSError, ValueError, KeyError) as exc:
        print(str(exc) if isinstance(exc, RunnerError) else "Invalid batch input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
