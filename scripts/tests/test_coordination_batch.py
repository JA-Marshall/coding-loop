"""Batch integration with real Git/subprocesses and simulated model/GitHub boundaries."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.coordination.batch import (Batch, ACTIVE_TEXT, head, plan_text, transaction,
                                         usage_count, validate_manifest)
from scripts.coordination.github import GitHub
from scripts.coordination.runner import RunnerError, git, save_json
from scripts.coordination.isolated import snapshot


class FakeAdapter:
    def __call__(self, runner, role, feedback):
        runner.checkpoint(usage=runner.state.get("usage", []) + [{"reported": [{"input_tokens": 20, "output_tokens": 10}]}])
        name = runner.packet["owned_files"][0]
        if role == "worker":
            return {"patch": f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-old\n+new\n", "summary": "fixture"}
        return {"candidate": runner.state["candidate"], "covered_files": [name],
                "acceptance": runner.packet["acceptance"], "findings": []}


class NotesAdapter(FakeAdapter):
    """Reviewer that files one non-blocking note; the candidate must still complete."""
    def __call__(self, runner, role, feedback):
        result = super().__call__(runner, role, feedback)
        if role == "reviewer":
            result["findings"] = [{"file": runner.packet["owned_files"][0], "severity": "should_fix",
                                   "summary": "Consider a guard", "failure_scenario": "none"}]
        return result


class FakeGitHub:
    def __init__(self, root, bare):
        self.root, self.bare = root, bare
        self.pulls = {}
        self.merges = []
        self.fail_ci = False

    def ensure_pull(self, branch, title, body):
        if branch not in self.pulls:
            self.pulls[branch] = {"number": len(self.pulls) + 1, "html_url": "https://example.invalid/pull/" + branch,
                                  "head": {"sha": head(self.root)}, "merged": False, "body": body}
        return self.pulls[branch]

    def gate(self, number, sha, branch):
        pull = self.pulls[branch]
        assert pull["head"]["sha"] == sha
        if self.fail_ci:
            raise RunnerError("Required CI failed")
        return ("MERGED" if pull["merged"] else "READY"), pull

    def merge(self, number, sha, branch):
        self.merges.append(sha)
        git(self.bare, "update-ref", "refs/heads/main", sha)
        self.pulls[branch].update(merged=True, merge_commit_sha=sha)
        return self.pulls[branch]


def task(number=1):
    name = f"sample{number}.txt"
    return {"id": f"selling-batch-{number}", "title": "Fixture " + str(number), "objective": "Replace old with new",
            "acceptance": [name + " contains new"], "owned_files": [name],
            "checks": [{"id": "content", "argv": [sys.executable, "-c",
                f"from pathlib import Path; assert Path('{name}').read_text() == 'new\\n'"], "timeout": 10}],
            "worker_model": "gpt-5.6-terra", "worker_reasoning": "medium",
            "impacts": {k: "NONE - Synthetic fixture only." for k in
                        ("schema", "data", "valuation", "reporting", "marketplace_write", "production")},
            "depends_on": [] if number == 1 else [f"selling-batch-{number-1}"]}


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux runner")
class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "repo"
        self.bare = self.directory / "remote.git"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Fixture")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        (self.root / "docs/plans/tasks").mkdir(parents=True)
        (self.root / "docs/plans/ACTIVE.md").write_text(ACTIVE_TEXT.format("NONE"))
        (self.root / "docs/plans/INBOX.md").write_text("# Inbox\n")
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n.env\n")
        for n in (1, 2):
            (self.root / f"sample{n}.txt").write_text("old\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "base")
        subprocess.run(["git", "clone", "--bare", str(self.root), str(self.bare)], check=True, capture_output=True)
        git(self.root, "remote", "add", "origin", str(self.bare))
        self.manifest = {"id": "fixture", "checkout": str(self.root), "repository": "example/repo",
                         "auth_home": str(self.directory / "auth"), "tasks": [task(1), task(2)],
                         "total_timeout": 60, "max_calls": 24, "max_reported_tokens": 1000,
                         "ci_timeout": 30, "authority": "docs/workflows/batch.md"}
        self.github = FakeGitHub(self.root, self.bare)
        self.batch = Batch(self.manifest, self.directory / "run", self.github, FakeAdapter())
        self.authority = patch.object(self.batch, "assert_authority")
        self.authority.start()
        self.addCleanup(self.authority.stop)

    def test_two_tasks_real_git_checks_review_commit_push_merge(self):
        result = self.batch.run()
        self.assertEqual(result["phase"], "COMPLETE", result)
        self.assertEqual(len(self.github.merges), 2)
        self.assertEqual(result["calls"], 4)
        self.assertEqual(result["tokens"], 120)
        self.assertEqual((self.root / "sample2.txt").read_text(), "new\n")
        self.assertFalse(git(self.root, "status", "--porcelain").strip())
        self.assertEqual(self.batch.run(), result)
        self.assertEqual(len(self.github.merges), 2)

    def test_non_blocking_reviewer_notes_reach_the_pull_request(self):
        self.authority.stop()
        self.batch = Batch(self.manifest, self.directory / "run", self.github, NotesAdapter())
        self.authority = patch.object(self.batch, "assert_authority")
        self.authority.start()
        result = self.batch.run()
        self.assertEqual(result["phase"], "COMPLETE", result)
        bodies = [pull["body"] for pull in self.github.pulls.values()]
        self.assertTrue(all("## Reviewer notes (non-blocking)" in body and "Consider a guard" in body for body in bodies), bodies)
        self.assertEqual(len(self.github.merges), 2)

    def test_failed_ci_preserves_pr_and_stops_dependent_task(self):
        self.github.fail_ci = True
        result = self.batch.run()
        self.assertEqual(result["phase"], "STOPPED", result)
        self.assertEqual(result["stopped_phase"], "CI", result)
        self.assertEqual(len(self.github.merges), 0)
        self.assertEqual((self.root / "sample2.txt").read_text(), "old\n")

    def test_manifest_change_refuses_resume(self):
        self.batch.run()
        self.batch.manifest["max_calls"] = 23
        with self.assertRaisesRegex(RunnerError, "Manifest changed"):
            self.batch.run()

    def test_unrelated_dirty_file_stops_before_model(self):
        (self.root / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(RunnerError, "Unrecorded"):
            self.batch.run()
        self.assertEqual((self.root / "keep.txt").read_text(), "keep")

    def test_control_owned_paths_rejected(self):
        for name in ("PLANS.md", "scripts/coordination/batch.py", "../escape", ".env"):
            value = copy.deepcopy(self.manifest)
            value["tasks"][0]["owned_files"] = [name]
            with self.assertRaises(RunnerError):
                validate_manifest(value)

    def test_unknown_usage_stops(self):
        for usage in ([{}], [{"reported": []}], [{"reported": [{"input_tokens": 1}]}]):
            with self.assertRaises(RunnerError):
                usage_count(usage)

    def test_budget_prevents_next_model_call(self):
        self.batch.manifest["max_reported_tokens"] = 30
        result = self.batch.run()
        self.assertEqual(result["phase"], "STOPPED", result)
        state = json.loads((self.directory / "run/selling-batch-1/runner/state.json").read_text())
        self.assertEqual(len(state["usage"]), 1)
        self.assertFalse(self.github.merges)

    def test_snapshot_excludes_ignored_data_and_configuration(self):
        (self.root / ".env").write_text("synthetic-private")
        (self.root / ".codex").mkdir()
        (self.root / ".codex/config.toml").write_text("untrusted config")
        (self.root / ".claude").mkdir()
        (self.root / ".claude/settings.json").write_text("untrusted config")
        (self.root / "CLAUDE.md").write_text("untrusted instructions")
        destination = self.directory / "snapshot"
        snapshot(self.root, destination)
        self.assertEqual((destination / "sample1.txt").read_text(), "old\n")
        self.assertFalse((destination / ".env").exists())
        self.assertFalse((destination / ".codex").exists())
        self.assertFalse((destination / ".claude").exists())
        self.assertFalse((destination / "CLAUDE.md").exists())

    def test_commit_response_loss_reconciles_exact_tree_once(self):
        self.batch.manifest["tasks"] = [task(1)]
        original = git
        failed = False
        def lose_response(root, *args, **kwargs):
            nonlocal failed
            result = original(root, *args, **kwargs)
            if args[0] == "commit" and args[-1] == "Implement selling-batch-1" and not failed:
                failed = True
                raise SystemExit("simulated parent death")
            return result
        with patch("scripts.coordination.batch.git", side_effect=lose_response):
            with self.assertRaises(SystemExit):
                self.batch.run()
        result = self.batch.run()
        self.assertEqual(result["phase"], "COMPLETE", result)
        self.assertEqual(len(self.github.merges), 1)
        messages = git(self.root, "log", "--format=%s").decode().splitlines()
        self.assertEqual(messages.count("Implement selling-batch-1"), 1)

    def test_merge_response_loss_reconciles_before_retry(self):
        self.batch.manifest["tasks"] = [task(1)]
        merge = self.github.merge
        def lose(*args):
            merge(*args)
            raise SystemExit("simulated parent death")
        with patch.object(self.github, "merge", side_effect=lose):
            with self.assertRaises(SystemExit):
                self.batch.run()
        result = self.batch.run()
        self.assertEqual(result["phase"], "COMPLETE", result)
        self.assertEqual(len(self.github.merges), 1)

    def test_prepare_control_write_crash_resumes_without_duplicate_writer(self):
        self.batch.manifest["tasks"] = [task(1)]
        original = Path.write_text
        failed = False
        def crash(path, text, *args, **kwargs):
            nonlocal failed
            result = original(path, text, *args, **kwargs)
            if path.name == "selling-batch-1.md" and "status: READY" in text and not failed:
                failed = True
                raise SystemExit("process died after plan write")
            return result
        with patch.object(Path, "write_text", crash):
            with self.assertRaises(SystemExit):
                self.batch.run()
        result = self.batch.run()
        self.assertEqual(result["phase"], "COMPLETE", result)
        self.assertEqual(result["calls"], 2)
        self.assertEqual(len(self.github.merges), 1)

    def test_exact_model_budget_allows_final_commit_and_merge(self):
        self.batch.manifest.update(tasks=[task(1)], max_calls=2, max_reported_tokens=60)
        result = self.batch.run()
        self.assertEqual(result["phase"], "COMPLETE", result)
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["tokens"], 60)

    def test_duplicate_checkout_lock_fails_before_state_change(self):
        from scripts.coordination.runner import checkout_lock
        with checkout_lock(self.root):
            with self.assertRaisesRegex(RunnerError, "Another runner"):
                self.batch.run()
        self.assertFalse((self.directory / "run/state.json").exists())

    def test_changed_runtime_refuses_resume(self):
        self.batch.run()
        with patch.object(self.batch, "runtime_hash", return_value="changed"):
            with self.assertRaisesRegex(RunnerError, "code changed"):
                self.batch.run()

    def test_stop_marker_prevents_dispatch(self):
        self.batch.directory.mkdir()
        (self.batch.directory / "STOP").write_text("operator stop")
        result = self.batch.run()
        self.assertEqual(result["phase"], "STOPPED", result)
        self.assertEqual(result["calls"], 0)
        self.assertFalse(self.github.merges)



class GitHubGateTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub("example/repo")
        self.sha = "a" * 40
        self.pull = {"head": {"sha": self.sha, "ref": "codex/fixture", "repo": {"full_name": "example/repo"}},
                     "base": {"ref": "main"}, "draft": False, "merged": False, "state": "open",
                     "mergeable": True, "mergeable_state": "clean"}
        self.check = {"id": 1, "name": "Storehouse required", "head_sha": self.sha,
                      "app": {"slug": "github-actions"}, "status": "completed", "conclusion": "success",
                      "check_suite": {"id": 2}}
        self.checks = [self.check]
        self.reviews = []
        self.workflow = {"id": 3, "check_suite_id": 2, "head_sha": self.sha,
                         "path": ".github/workflows/ci.yml", "event": "pull_request",
                         "status": "completed", "conclusion": "success"}
        self.mutations = []
        def api(path, method="GET", payload=None):
            if method != "GET":
                self.mutations.append((path, payload))
                self.pull["merged"] = True
                return {"merged": True}
            if path.startswith("/pulls/") and path.endswith("/reviews?per_page=100"):
                return self.reviews
            if path.startswith("/pulls/"):
                return self.pull
            if path.startswith("/commits/"):
                return {"check_runs": self.checks}
            if path.startswith("/check-suites/"):
                return {"id": 2}
            if path.startswith("/actions/runs"):
                return {"workflow_runs": [self.workflow]}
            raise AssertionError(path)
        self.mock = patch.object(self.github, "api", side_effect=api)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_exact_head_success_merges_with_compare_sha(self):
        self.github.merge(1, self.sha, "codex/fixture")
        self.assertEqual(self.mutations, [("/pulls/1/merge", {"sha": self.sha, "merge_method": "merge"})])

    def test_changed_head_prevents_mutation(self):
        self.pull["head"]["sha"] = "b" * 40
        with self.assertRaises(RunnerError):
            self.github.merge(1, self.sha, "codex/fixture")
        self.assertFalse(self.mutations)

    def test_missing_pending_failed_cancelled_checks_prevent_merge(self):
        for state in ("missing", "pending", "failure", "cancelled", "skipped"):
            with self.subTest(state=state):
                self.checks = [] if state == "missing" else [dict(self.check, conclusion=state,
                    status="in_progress" if state == "pending" else "completed")]
                with self.assertRaises(RunnerError):
                    self.github.merge(1, self.sha, "codex/fixture")
                self.assertFalse(self.mutations)

    def test_rerun_pending_overrides_old_success(self):
        self.checks.append(dict(self.check, id=4, status="in_progress", conclusion=None))
        self.assertEqual(self.github.gate(1, self.sha, "codex/fixture")[0], "WAIT")

    def test_matching_name_wrong_workflow_rejected(self):
        self.workflow["path"] = ".github/workflows/unrelated.yml"
        with self.assertRaisesRegex(RunnerError, "provenance"):
            self.github.merge(1, self.sha, "codex/fixture")
        self.assertFalse(self.mutations)

    def test_changes_requested_blocks_merge(self):
        self.reviews = [{"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"}]
        with self.assertRaisesRegex(RunnerError, "requested changes"):
            self.github.merge(1, self.sha, "codex/fixture")
        self.assertFalse(self.mutations)


    def test_merged_missing_required_check_waits(self):
        self.pull["merged"] = True
        self.pull["state"] = "closed"
        self.checks = []
        self.assertEqual(self.github.gate(1, self.sha, "codex/fixture")[0], "WAIT")
        self.assertFalse(self.mutations)

    def test_merged_failed_required_check_stops(self):
        self.pull["merged"] = True
        self.pull["state"] = "closed"
        self.check["conclusion"] = "failure"
        with self.assertRaisesRegex(RunnerError, "Required CI failed"):
            self.github.gate(1, self.sha, "codex/fixture")
        self.assertFalse(self.mutations)

    def test_merged_running_workflow_waits(self):
        self.pull["merged"] = True
        self.pull["state"] = "closed"
        self.workflow["status"] = "in_progress"
        self.workflow["conclusion"] = None
        self.assertEqual(self.github.gate(1, self.sha, "codex/fixture")[0], "WAIT")
        self.assertFalse(self.mutations)

    def test_merged_green_reconciles_without_put(self):
        self.pull["merged"] = True
        self.pull["state"] = "closed"
        self.assertEqual(self.github.gate(1, self.sha, "codex/fixture")[0], "MERGED")
        self.github.merge(1, self.sha, "codex/fixture")
        self.assertFalse(self.mutations)


class HighRiskTests(unittest.TestCase):
    def test_advisory_review_follows_present_high_risk_impacts(self):
        from scripts.coordination.batch import high_risk
        quiet = {k: "NONE - n/a" for k in ("schema", "data", "valuation", "reporting", "marketplace_write", "production")}
        self.assertFalse(high_risk({"impacts": dict(quiet, reporting="PRESENT - new view")}))
        for key in ("schema", "data", "valuation", "marketplace_write"):
            with self.subTest(key=key):
                self.assertTrue(high_risk({"impacts": dict(quiet, **{key: "PRESENT - yes"})}))


class QueueContractTests(unittest.TestCase):
    def write_status(self, root, task_id, status, publish=True):
        """Write a plan status; publish commits it and advances origin/main."""
        if not (root / ".git").exists():
            git(root, "init", "-b", "main")
            git(root, "config", "user.email", "fixture@example.invalid")
            git(root, "config", "user.name", "Fixture")
        path = root / "docs/plans/tasks" / (task_id + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Task\n\n<!-- storehouse-plan\nid: " + task_id + "\nstatus: " + status + "\n-->\n")
        if publish:
            git(root, "add", str(path))
            git(root, "commit", "-m", task_id + " " + status)
            git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

    def test_skip_complete_continues_after_merged_tasks(self):
        from scripts.coordination.configure import approved_tasks, manifest
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = [t["id"] for t in manifest(root, root / "auth", "full")["tasks"]]
            for task_id in ids[:2]:
                self.write_status(root, task_id, "COMPLETE")
            continuation = manifest(root, root / "auth", "continuation", skip_complete=True)
            self.assertEqual([t["id"] for t in continuation["tasks"]], ids[2:])
            self.assertEqual(continuation["tasks"][0]["depends_on"], [])
            self.assertEqual(continuation["tasks"][1]["depends_on"], [ids[2]])
            self.assertEqual(approved_tasks(root, ids[2:]), continuation["tasks"])
            # The first continuation task merging mid-batch keeps the contract stable.
            self.write_status(root, ids[2], "COMPLETE")
            self.assertEqual(approved_tasks(root, ids[2:]), continuation["tasks"])
            self.assertTrue(all(t["worker_model"] == "claude-opus-5-5" for t in continuation["tasks"]))

    def test_omitted_dependency_must_be_complete(self):
        from scripts.coordination.configure import approved_tasks, manifest
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = [t["id"] for t in manifest(root, root / "auth", "full")["tasks"]]
            for task_id in ids[:2]:
                self.write_status(root, task_id, "COMPLETE")
            continuation = manifest(root, root / "auth", "continuation", skip_complete=True)
            self.write_status(root, ids[1], "IN_PROGRESS")
            self.assertNotEqual(approved_tasks(root, ids[2:]), continuation["tasks"])
            # A stopped batch marks COMPLETE locally before its PR merges; that is not enough.
            self.write_status(root, ids[1], "COMPLETE", publish=False)
            self.assertNotEqual(approved_tasks(root, ids[2:]), continuation["tasks"])
            self.assertEqual([t["id"] for t in manifest(root, root / "auth", "unmerged",
                                                         skip_complete=True)["tasks"]], ids[1:])
            with self.assertRaises(ValueError):
                approved_tasks(root, [ids[3], ids[2]])
            with self.assertRaises(ValueError):
                approved_tasks(root, ["selling-not-queued"])

    def test_every_frozen_queue_plan_passes_all_lifecycle_states(self):
        from scripts.coordination.configure import manifest
        from scripts.validate_plans import validate_repository
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "docs/plans/tasks").mkdir(parents=True)
            configuration = manifest(root, root / "auth", "qualification")
            for task in configuration["tasks"]:
                path = "docs/plans/tasks/" + task["id"] + ".md"
                (root / "docs/plans/INBOX.md").write_text("# Inbox\n- [Task](tasks/" + task["id"] + ".md)\n")
                for status in ("DRAFT", "READY", "IN_PROGRESS", "COMPLETE"):
                    (root / path).write_text(plan_text(task, root / "external-run", status))
                    target = "NONE" if status in {"DRAFT", "COMPLETE"} else path
                    (root / "docs/plans/ACTIVE.md").write_text(ACTIVE_TEXT.format(target))
                    self.assertEqual(validate_repository(root), [], (task["id"], status))
                (root / path).unlink()
