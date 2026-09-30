"""Synthetic subprocess/Git fixtures: no network, models or application database."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import signal
from unittest.mock import patch
import unittest

from scripts.coordination.hooks import respond
from scripts.coordination.runner import (
    CodexAdapter, PatchFormatError, Runner, RunnerError, apply_patch, authorize_live, checkout_lock,
    fingerprint, git, main, reviewer_model, save_json, validate_packet,
)


PATCH = """diff --git a/sample.txt b/sample.txt
index 3367afd..3e75765 100644
--- a/sample.txt
+++ b/sample.txt
@@ -1 +1 @@
-old
+new
"""


def finding(severity="blocking", summary="sample.txt fails acceptance", file="sample.txt", scenario="new is not accepted"):
    return {"file": file, "severity": severity, "summary": summary, "failure_scenario": scenario}


class Adapter:
    def __init__(self, findings=None):
        self.roles = []
        self.findings = findings or []

    def __call__(self, runner, role, feedback):
        self.roles.append(role)
        if role == "worker":
            return {"patch": PATCH, "summary": "Replace fixture text"}
        if role == "coordinator":
            return {"action": "FIX", "reason": "Bounded correction"}
        return {"candidate": runner.state["candidate"], "covered_files": ["sample.txt"],
                "acceptance": runner.packet["acceptance"], "findings": self.findings}


@unittest.skipUnless(sys.platform.startswith("linux"), "Runner requires Linux/WSL")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        # Tests must never reach a real model CLI: failing stubs sit first on PATH
        # unless a test installs its own fakes over them.
        guard = self.home / "guard-bin"
        guard.mkdir()
        for name in ("claude", "codex"):
            stub = guard / name
            stub.write_text("#!/bin/sh\necho 'live model CLI called from a test' >&2\nexit 97\n")
            stub.chmod(0o755)
        env = patch.dict(os.environ, {"PATH": str(guard) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.home / "repo"
        self.root.mkdir()
        git(self.root, "init", "-b", "codex/fixture")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        git(self.root, "config", "user.name", "Fixture")
        (self.root / "sample.txt").write_text("old\n")
        git(self.root, "add", "sample.txt")
        git(self.root, "commit", "-m", "fixture")
        self.packet = {
            "id": "selling-fixture", "checkout": str(self.root),
            "base_sha": git(self.root, "rev-parse", "HEAD").decode().strip(),
            "branch": "codex/fixture", "objective": "Replace old with new",
            "acceptance": ["sample.txt contains new"], "owned_files": ["sample.txt"],
            "checks": [{"id": "content", "argv": [sys.executable, "-c",
                "from pathlib import Path; assert Path('sample.txt').read_text() == 'new\\n'"], "timeout": 5}],
            "worker_model": "gpt-5.6-terra", "worker_reasoning": "medium",
            "plan": "docs/plans/tasks/selling-fixture.md",
        }
        self.run_dir = self.home / "run"

    def runner(self, adapter=None):
        return Runner(self.packet, self.run_dir, adapter or Adapter())

    def test_complete_cycle_binds_checks_and_review(self):
        adapter = Adapter()
        state = self.runner(adapter).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(adapter.roles, ["worker", "reviewer"])
        self.assertEqual(state["checks"]["candidate"], fingerprint(self.root))
        self.assertTrue((self.run_dir / "candidate.diff").is_file())
        # The reviewed diff is also kept under the reviewer call number for replay.
        self.assertEqual((self.run_dir / "candidate-2.diff").read_text(),
                         (self.run_dir / "candidate.diff").read_text())
        self.assertEqual((self.run_dir.stat().st_mode & 0o777), 0o700)
        self.assertIn("No PR", (self.run_dir / "report.md").read_text())
        self.assertEqual(self.runner().run(resume=True)["phase"], "LOCAL_REVIEWED")

    def test_checkpoint_records_the_pair_and_when_each_phase_began(self):
        before = time.time()
        state = self.runner().run()
        self.assertEqual(state["pair"], {"worker": "codex:gpt-5.6-terra:medium", "reviewer": "claude:claude-opus-5-5:high"})
        self.assertEqual([entry["phase"] for entry in state["timeline"]],
                         ["IMPLEMENT", "APPLYING", "CHECKS", "REVIEW", "LOCAL_REVIEWED"])
        times = [entry["at"] for entry in state["timeline"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(before <= times[0] and times[-1] <= time.time())
        self.assertEqual(json.loads((self.run_dir / "state.json").read_text())["timeline"], state["timeline"])
        # A finished run is returned as recorded: resuming adds nothing.
        self.assertEqual(self.runner().run(resume=True)["timeline"], state["timeline"])

    def test_timeline_follows_corrections_and_ends_at_the_stop(self):
        self.packet.update(max_corrections=1, luna_triage=False, advisory_review=True,
                           worker_model="claude-opus-5-5", worker_reasoning="high")
        state = self.runner(lambda *_: {"patch": "not a diff", "summary": "broken"}).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertEqual([entry["phase"] for entry in state["timeline"]],
                         ["IMPLEMENT", "APPLYING", "IMPLEMENT", "APPLYING", "STOPPED"])
        self.assertEqual(state["pair"], {"worker": "claude:claude-opus-5-5:high", "reviewer": "claude:claude-opus-5-5:high",
                                         "advisory": "codex:gpt-5.6-sol:high"})

    def test_checkpoint_from_before_the_timeline_still_resumes(self):
        self.runner().run()
        state = json.loads((self.run_dir / "state.json").read_text())
        del state["timeline"], state["pair"]
        state["phase"] = "REVIEW"
        save_json(self.run_dir / "state.json", state)
        resumed = self.runner().run(resume=True)
        self.assertEqual(resumed["phase"], "LOCAL_REVIEWED")
        self.assertEqual([entry["phase"] for entry in resumed["timeline"]], ["LOCAL_REVIEWED"])
        self.assertNotIn("pair", resumed)

    def test_wrong_branch_and_base_rejected_without_calls(self):
        for key, value in (("branch", "codex/wrong"), ("base_sha", "a" * 40)):
            with self.subTest(key=key):
                original = self.packet[key]
                self.packet[key] = value
                with self.assertRaises(RunnerError):
                    self.runner().run()
                self.packet[key] = original

    def test_dirty_start_rejected(self):
        (self.root / "unrelated.txt").write_text("preserve")
        with self.assertRaisesRegex(RunnerError, "clean checkout"):
            self.runner().run()
        self.assertEqual((self.root / "unrelated.txt").read_text(), "preserve")

    def test_exact_root_and_external_state_required(self):
        with self.assertRaises(RunnerError):
            Runner(self.packet, self.root / "state", Adapter())
        (self.root / "sub").mkdir()
        self.packet["checkout"] = str(self.root / "sub")
        with self.assertRaisesRegex(RunnerError, "exact repository root"):
            self.runner().run()

    def test_duplicate_runner_even_with_different_state_directory(self):
        with checkout_lock(self.root):
            with self.assertRaisesRegex(RunnerError, "Another runner"):
                Runner(self.packet, self.home / "other-run", Adapter()).run()

    def test_owned_path_validation(self):
        for name in ("../escape", "/tmp/escape", "a/../b", "a//b", "a/*", "AGENTS.md",
                     ".git/config", ".env", "x\\y", "docs/plans/x.md"):
            with self.subTest(name=name):
                packet = dict(self.packet, owned_files=[name])
                with self.assertRaises(RunnerError):
                    validate_packet(packet)

    def test_recounts_incorrect_hunk_lengths(self):
        self.assertEqual(apply_patch(self.root, PATCH.replace("@@ -1 +1 @@", "@@ -1,9 +1,8 @@"), ["sample.txt"]), ["sample.txt"])
        self.assertEqual((self.root / "sample.txt").read_text(), "new\n")

    def test_recount_does_not_bypass_scope(self):
        with self.assertRaises(RunnerError):
            apply_patch(self.root, PATCH.replace("@@ -1 +1 @@", "@@ -1,9 +1,8 @@"), ["different.txt"])
        self.assertEqual((self.root / "sample.txt").read_text(), "old\n")

    def test_patch_context_correction_is_a_free_format_retry_without_triage(self):
        adapter = Adapter()
        def fix(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "worker" and runner.state["calls"] == 1:
                result["patch"] = PATCH.replace("-old", "-not the current content")
            elif role == "worker":
                self.assertIn("Patch context", feedback)
            return result
        state = self.runner(fix).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(state["calls"], 3)
        self.assertEqual((state["corrections"], state["format_retries"]), (0, 1))
        self.assertEqual(adapter.roles, ["worker", "worker", "reviewer"])

    def test_patch_feedback_quotes_git_and_names_foreign_headers(self):
        envelope = "*** Begin Patch\n*** Update File: sample.txt\n@@\n-not in the file\n+new\n*** End Patch\n"
        with self.assertRaises(PatchFormatError) as caught:
            apply_patch(self.root, envelope, ["sample.txt"])
        feedback = str(caught.exception)
        self.assertIn("git apply said", feedback)
        self.assertIn("`*** Begin Patch`", feedback)
        self.assertIn("not a verdict on the change", feedback)
        (self.root / "other.txt").write_text("old\n")
        indented = PATCH + " diff --git a/other.txt b/other.txt\n--- a/other.txt\n+++ b/other.txt\n@@ -1 +1 @@\n-missing\n+new\n"
        with self.assertRaises(PatchFormatError) as caught:
            apply_patch(self.root, indented, ["sample.txt", "other.txt"])
        self.assertIn("is indented", str(caught.exception))
        self.assertEqual((self.root / "sample.txt").read_text(), "old\n")

    def test_an_indented_file_header_is_repaired(self):
        (self.root / "other.txt").write_text("old\n")
        repairs = []
        # As gpt-6.1-sol writes it: the next file's header lands after a space, with no index line.
        second = "diff --git a/other.txt b/other.txt\n--- a/other.txt\n+++ b/other.txt\n@@ -1 +1 @@\n-old\n+new\n"
        indented = PATCH + " " + second + "*** End Patch\n"
        self.assertEqual(apply_patch(self.root, indented, ["sample.txt", "other.txt"], repairs), ["other.txt", "sample.txt"])
        self.assertEqual(((self.root / "sample.txt").read_text(), (self.root / "other.txt").read_text()), ("new\n", "new\n"))
        self.assertEqual(repairs, ["stray header whitespace or envelope markers"])

    def test_a_codex_envelope_is_placed_by_its_context(self):
        (self.root / "code.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 1\n")
        envelope = ("*** Begin Patch\n*** Update File: code.py\n@@ def b():\n-    return 1\n+    return 2\n"
                    "*** Add File: notes.txt\n+first\n+second\n*** End Patch\n")
        repairs = []
        self.assertEqual(apply_patch(self.root, envelope, ["code.py", "notes.txt"], repairs), ["code.py", "notes.txt"])
        self.assertEqual((self.root / "code.py").read_text(), "def a():\n    return 1\n\n\ndef b():\n    return 2\n")
        self.assertEqual((self.root / "notes.txt").read_text(), "first\nsecond\n")
        self.assertEqual(repairs, ["codex envelope"])

    def test_a_git_diff_that_switches_to_the_envelope_midway_is_rebuilt(self):
        # As gpt-5.6-luna writes it: git sections first, then `*** Update File:` with numbered hunks, then `*** End Patch`.
        (self.root / "code.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
        mixed = PATCH + "*** Update File: code.py\n@@ -2,2 +2,2 @@ a = 1\n b = 2\n-c = 3\n+c = 30\n*** End Patch\n"
        repairs = []
        self.assertEqual(apply_patch(self.root, mixed, ["sample.txt", "code.py"], repairs), ["code.py", "sample.txt"])
        self.assertEqual(((self.root / "sample.txt").read_text(), (self.root / "code.py").read_text()),
                         ("new\n", "a = 1\nb = 2\nc = 30\nd = 4\n"))
        self.assertEqual(repairs, ["codex envelope"])

    def test_a_codex_envelope_keeps_the_files_own_context_and_scope(self):
        (self.root / "code.py").write_text("x = 1\ny = 2\nz = 3\n")
        apply_patch(self.root, "*** Begin Patch\n*** Update File: code.py\n x = 1   \n-y = 2\n+y = 20\n z = 3\n*** End Patch\n", ["code.py"])
        self.assertEqual((self.root / "code.py").read_text(), "x = 1\ny = 20\nz = 3\n")
        with self.assertRaises(RunnerError):
            apply_patch(self.root, "*** Begin Patch\n*** Update File: code.py\n-z = 3\n+z = 30\n*** End Patch\n", ["sample.txt"])
        with self.assertRaises(PatchFormatError):
            apply_patch(self.root, "*** Begin Patch\n*** Update File: code.py\n*** Move to: moved.py\n*** End Patch\n", ["code.py"])
        self.assertEqual((self.root / "code.py").read_text(), "x = 1\ny = 20\nz = 3\n")

    def test_a_repaired_patch_is_recorded_in_the_attempt_log(self):
        def worker(runner, role, feedback):
            result = Adapter()(runner, role, feedback)
            if role == "worker":
                result["patch"] = "*** Begin Patch\n*** Update File: sample.txt\n-old\n+new\n*** End Patch\n"
            return result
        state = self.runner(worker).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual((state["corrections_log"], [r["repair"] for r in state["patch_repairs"]]), ([], ["codex envelope"]))
        self.assertEqual(self.log_lines()[-1]["patch_repairs"], state["patch_repairs"])

    def test_a_different_broken_patch_is_not_a_repeated_failure(self):
        broken = ["not a diff", "*** Begin Patch\nstill not a diff"]
        def worker(runner, role, feedback):
            result = Adapter()(runner, role, feedback)
            if role == "worker" and broken:
                result["patch"] = broken.pop(0)
            return result
        state = self.runner(worker).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual((state["corrections"], state["format_retries"]), (0, 2))

    def test_the_same_broken_patch_twice_is_a_repeated_failure(self):
        state = self.runner(lambda *_: {"patch": "not a diff", "summary": "broken"}).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertEqual(state["reason"], "Repeated failure; stronger primary review required")

    def test_bad_patch_exhaustion_retains_counters(self):
        self.packet["max_corrections"] = 1
        calls = []
        def broken(*_):
            calls.append(1)
            return {"patch": f"not a diff {len(calls)}", "summary": "broken"}
        state = self.runner(broken).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertEqual(state["reason"], "Correction budget exhausted")
        self.assertEqual(state["calls"], 4)
        self.assertEqual((state["corrections"], state["format_retries"]), (1, 2))
        self.assertEqual([e.get("format_retry", False) for e in state["corrections_log"]], [True, True, False])
        self.assertEqual((self.root / "sample.txt").read_text(), "old\n")

    def test_patch_outside_scope_does_not_apply(self):
        with self.assertRaises(RunnerError):
            apply_patch(self.root, PATCH, ["different.txt"])
        self.assertEqual((self.root / "sample.txt").read_text(), "old\n")

    def test_symlink_mode_patch_denied(self):
        with self.assertRaises(RunnerError):
            apply_patch(self.root, "diff --git a/x b/x\nnew file mode 120000\n", ["x"])

    def test_symlink_parent_denied(self):
        (self.root / "link").symlink_to(self.home, target_is_directory=True)
        patch = PATCH.replace("sample.txt", "link/sample.txt")
        with self.assertRaises(RunnerError):
            apply_patch(self.root, patch, ["link/sample.txt"])

    def test_candidate_hash_tracks_untracked_and_mode_changes(self):
        original = fingerprint(self.root)
        (self.root / "new.txt").write_text("untracked")
        self.assertNotEqual(original, fingerprint(self.root))
        (self.root / "new.txt").unlink()
        (self.root / "sample.txt").chmod(0o755)
        self.assertNotEqual(original, fingerprint(self.root))

    def test_model_exit_or_prose_is_not_evidence(self):
        state = self.runner(lambda *_: "done").run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("must be an object", state["reason"])

    def test_readonly_model_mutation_is_detected(self):
        def mutate(runner, *_):
            (runner.root / "sample.txt").write_text("unauthorized")
            return {"patch": PATCH, "summary": "done"}
        state = self.runner(mutate).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("changed outside", state["reason"])

    def test_missing_review_coverage_cannot_complete(self):
        adapter = Adapter()
        def missing(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "reviewer":
                result["covered_files"] = []
            return result
        state = self.runner(missing).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("coverage", state["reason"])

    def test_review_stale_hash_and_findings_stop(self):
        self.packet["max_corrections"] = 0
        state = self.runner(Adapter([finding()])).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("Correction budget", state["reason"])

    def test_non_blocking_findings_become_notes_not_corrections(self):
        self.packet["max_corrections"] = 0
        adapter = Adapter([finding("should_fix", "Consider a guard"), finding("nit", "Rename variable")])
        state = self.runner(adapter).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(adapter.roles, ["worker", "reviewer"])
        self.assertEqual([f["severity"] for f in state["review"]["findings"]], ["should_fix", "nit"])
        report = (self.run_dir / "report.md").read_text()
        self.assertIn("Reviewer notes (non-blocking)", report)
        self.assertIn("Consider a guard", report)

    def test_malformed_finding_stops(self):
        for index, bad in enumerate(("a plain string",
                                     {"file": "sample.txt", "severity": "urgent", "summary": "x", "failure_scenario": "y"},
                                     {"file": "sample.txt", "severity": "blocking", "summary": "", "failure_scenario": "y"})):
            self.run_dir = self.home / f"run-{index}"
            git(self.root, "reset", "-q", "--hard")
            state = self.runner(Adapter([bad])).run()
            self.assertEqual(state["phase"], "STOPPED", bad)
            self.assertIn("malformed", state["reason"])

    def test_second_review_receives_previous_blocking_findings(self):
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; assert Path('sample.txt').is_file()"]
        adapter = Adapter()
        seen = []
        def reviewer_then_clean(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "reviewer":
                seen.append(json.loads(feedback)["previous_findings"])
                result["findings"] = [finding()] if len(seen) == 1 else []
            elif role == "worker" and len(seen) == 1:
                self.assertIn("Review findings", feedback)
                self.assertIn("fails acceptance", feedback)
                result["patch"] = PATCH.replace("-old", "-new").replace("+new", "+newer")
            return result
        state = self.runner(reviewer_then_clean).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(seen, [[], [finding()]])
        self.assertEqual(state["corrections"], 1)

    def test_failed_checks_never_reviewed_without_fix(self):
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(3)"]
        self.packet["max_corrections"] = 0
        adapter = Adapter()
        state = self.runner(adapter).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertEqual(adapter.roles, ["worker"])
        self.assertEqual(state["checks"]["results"][0]["exit_code"], 3)

    def test_luna_cannot_override_call_budget(self):
        self.packet["max_calls"] = 2
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(3)"]
        adapter = Adapter()
        state = self.runner(adapter).run()
        self.assertEqual(adapter.roles, ["worker", "coordinator"])
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("call budget", state["reason"])

    def test_malformed_coordinator_action_stops_with_checkpoint(self):
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(3)"]
        adapter = Adapter()
        def malformed(runner, role, feedback):
            if role == "coordinator":
                return {"action": ["FIX"], "reason": "invalid schema"}
            return adapter(runner, role, feedback)
        state = self.runner(malformed).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertEqual(state["reason"], "Invalid coordinator result")

    def test_check_mutation_invalidates_evidence(self):
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('sample.txt').write_text('oops')"]
        state = self.runner().run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("changed outside", state["reason"])

    def test_timeout_records_ambiguous_operation(self):
        self.packet["checks"][0].update(argv=[sys.executable, "-c", "import time; time.sleep(10)"], timeout=1)
        state = self.runner().run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("TimeoutExpired", state["reason"])
        self.assertIsNotNone(state["inflight"])
        with self.assertRaisesRegex(RunnerError, "reconciliation"):
            self.runner().run(resume=True)

    def test_changed_packet_cannot_resume(self):
        self.runner().run()
        self.packet["objective"] = "Changed objective"
        with self.assertRaisesRegex(RunnerError, "Packet changed"):
            self.runner().run(resume=True)

    def test_changed_candidate_cannot_resume(self):
        self.runner().run()
        (self.root / "sample.txt").write_text("changed later")
        with self.assertRaisesRegex(RunnerError, "changed outside"):
            self.runner().run(resume=True)

    def test_completed_check_boundary_resumes_without_new_worker(self):
        self.runner().run()
        state = json.loads((self.run_dir / "state.json").read_text())
        state["phase"] = "REVIEW"
        save_json(self.run_dir / "state.json", state)
        adapter = Adapter()
        self.assertEqual(self.runner(adapter).run(resume=True)["phase"], "LOCAL_REVIEWED")
        self.assertEqual(adapter.roles, ["reviewer"])

    def test_correction_cycle_reaches_review_with_new_evidence(self):
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; assert Path('sample.txt').read_text() == 'fixed\\n'"]
        adapter = Adapter()
        def repair(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "worker" and runner.state["corrections"]:
                result["patch"] = PATCH.replace("-old", "-new").replace("+new", "+fixed")
                self.assertIn("Prescribed checks failed", feedback)
            return result
        state = self.runner(repair).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(adapter.roles, ["worker", "coordinator", "worker", "reviewer"])
        self.assertEqual(state["corrections"], 1)
        self.assertEqual(state["checks"]["candidate"], fingerprint(self.root))

    def test_correction_feedback_includes_the_failing_test_output(self):
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", (
            "from pathlib import Path\n"
            "if Path('sample.txt').read_text() != 'fixed\\n':\n"
            "    print('noise ' * 50)\n"
            "    print('=' * 70)\n"
            "    print('ERROR: test_create (operations.test_x.Tests.test_create)')\n"
            "    print('ValidationError: Listing 1100 has no imported listing detail.')\n"
            "    raise SystemExit(1)\n")]
        adapter = Adapter()
        seen = []
        def repair(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "worker" and runner.state["corrections"]:
                seen.append(feedback)
                result["patch"] = PATCH.replace("-old", "-new").replace("+new", "+fixed")
            return result
        state = self.runner(repair).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertIn("has no imported listing detail", seen[0])
        self.assertIn("ERROR: test_create", seen[0])
        self.assertNotIn("noise", seen[0])
        self.assertIn("untrusted data", seen[0])

    def test_failure_excerpt_is_bounded_and_falls_back_to_the_tail(self):
        from scripts.coordination.runner import failure_excerpt
        log = self.home / "check.log"
        log.write_text("\n".join(f"line {n} " + "x" * 200 for n in range(500)))
        excerpt = failure_excerpt(log, 1000)
        self.assertLessEqual(len(excerpt), 1000)
        self.assertIn("[truncated]", excerpt)
        self.assertIn("line 499", excerpt)
        self.assertEqual(failure_excerpt(self.home / "missing.log", 1000), "(check log unavailable)")

    def test_empty_worker_patch_stops_with_its_blocker_summary(self):
        def blocked(runner, role, feedback):
            return {"patch": "", "summary": "Blocked: the failing test output is not visible."}
        state = self.runner(blocked).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("Worker returned no patch: Blocked: the failing test output", state["reason"])

    def test_codex_worker_with_claude_review_in_real_subprocess(self):
        with self.fake_claude_cli(None, claude_worker=False):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual([u["role"] for u in state["usage"]], ["worker", "reviewer"])
        self.assertTrue(all(p["characters"] < 6000 for p in state["prompts"]))
        self.assertEqual(state["usage"][0]["reported"][0]["input_tokens"], 100)
        for record in state["prompts"]:
            prompt = (self.run_dir / f"prompt-{record['call']}.txt").read_text()
            self.assertEqual(len(prompt), record["characters"])
            self.assertIn("PACKET:", prompt)
        self.assertIn(PATCH.splitlines()[0], (self.run_dir / "prompt-2.txt").read_text())

    def fake_claude_cli(self, result, review_findings=(), advisory_findings=(), claude_worker=True):
        """Fake claude and codex CLIs. Claude patches (when it is the worker) and reviews; codex advises."""
        fake = self.home / "bin"
        fake.mkdir(exist_ok=True)
        claude = fake / "claude"
        claude.write_text("#!" + sys.executable + "\n" + r"""import json, sys
from pathlib import Path
args = sys.argv[1:]
for flag in ('-p', '--restricted', '--strict-mcp-config', '--no-session-persistence', '--disable-slash-commands'):
    assert flag in args, flag
assert args[args.index('--tools') + 1] == 'Read,Grep,Glob'
assert args[args.index('--permission-mode') + 1] == 'dontAsk'
assert args[args.index('--model') + 1] == 'claude-opus-5-5'
assert args[args.index('--effort') + 1] == 'high'
assert json.loads(args[args.index('--settings') + 1])['permissions']['deny'] == ['Read(**/.env*)']
assert 'Grep' in args[args.index('--append-system-prompt') + 1]
schema = json.loads(args[args.index('--json-schema') + 1])
prompt = sys.stdin.read()
assert 'PACKET:' in prompt
print('startup notice on stderr', file=sys.stderr)
if 'patch' in schema['properties']:
    print(json.dumps(RESULT))
else:
    packet = json.loads(prompt.split('PACKET:\n', 1)[1].split('\n', 1)[0])
    evidence = json.loads(prompt.split('\nEVIDENCE:\n', 1)[1])
    review = {'candidate': evidence['candidate'], 'covered_files': evidence['files'],
              'acceptance': packet['acceptance'], 'findings': REVIEW_FINDINGS}
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': review,
                      'usage': {'input_tokens': 20, 'output_tokens': 10}}))
""".replace("RESULT", repr(result)).replace("REVIEW_FINDINGS", repr(list(review_findings))))
        claude.chmod(0o755)
        codex = fake / "codex"
        codex.write_text("#!" + sys.executable + "\n" + r"""import json, sys
from pathlib import Path
import os
args = sys.argv[1:]
assert '--json' in args and '--output-schema' in args and '-o' in args
if '--strict-config' in args:
    # Isolated path: its own CODEX_HOME permission profile, nothing persisted.
    assert os.environ.get('CODEX_HOME') and '--ephemeral' in args
else:
    assert args[args.index('--sandbox') + 1] == 'read-only'
    assert 'approval_policy="never"' in args
prompt = sys.stdin.read()
packet = json.loads(prompt.split('PACKET:\n', 1)[1].split('\n', 1)[0])
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
if 'patch' in schema['properties']:
    assert not CLAUDE_WORKER, 'a Claude worker must not use Codex'
    assert args[args.index('--model') + 1] == packet['worker_model']
    result = {'patch': PATCH_VALUE, 'summary': 'fake Codex patch'}
else:
    assert args[args.index('--model') + 1] == 'gpt-5.6-sol', 'Codex only advises'
    evidence = json.loads(prompt.split('\nEVIDENCE:\n', 1)[1])
    result = {'candidate': evidence['candidate'], 'covered_files': evidence['files'],
              'acceptance': packet['acceptance'], 'findings': ADVISORY_FINDINGS}
Path(args[args.index('-o') + 1]).write_text(json.dumps(result))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 30}}))
""".replace("PATCH_VALUE", repr(PATCH)).replace("ADVISORY_FINDINGS", repr(list(advisory_findings)))
          .replace("CLAUDE_WORKER", repr(claude_worker)))
        codex.chmod(0o755)
        if claude_worker:
            self.packet.update(worker_model="claude-opus-5-5", worker_reasoning="high")
        return patch.dict(os.environ, {"PATH": str(fake) + os.pathsep + os.environ["PATH"]})

    CLAUDE_PATCH = {"type": "result", "subtype": "success", "is_error": False,
                    "structured_output": {"patch": PATCH, "summary": "Claude patch"},
                    "usage": {"input_tokens": 6, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 400,
                              "output_tokens": 50, "output_tokens_details": {"thinking_tokens": 10}}}
    BLOCKING = {"file": "sample.txt", "severity": "blocking", "summary": "Wrong value",
                "failure_scenario": "The check passes but the value is still wrong."}

    def test_claude_worker_and_claude_review_in_real_subprocess(self):
        with self.fake_claude_cli(self.CLAUDE_PATCH):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual([u["role"] for u in state["usage"]], ["worker", "reviewer"])
        self.assertEqual(state["usage"][0]["reported"], [
            {"input_tokens": 1046, "raw_input_tokens": 1406, "cached_input_tokens": 400,
             "cache_read_weight": "0.1", "output_tokens": 50}])
        self.assertEqual(state["usage"][1]["reported"][0]["input_tokens"], 20)
        self.assertNotIn("advisory", state)

    def test_high_risk_packet_gets_a_clean_advisory_review(self):
        self.packet["advisory_review"] = True
        with self.fake_claude_cli(self.CLAUDE_PATCH):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual([u["role"] for u in state["usage"]], ["worker", "reviewer", "advisory"])
        self.assertEqual(state["advisory"]["findings"], [])

    def test_advisory_blocking_finding_stops_for_the_owner_without_a_correction(self):
        self.packet["advisory_review"] = True
        with self.fake_claude_cli(self.CLAUDE_PATCH, advisory_findings=[self.BLOCKING]):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("owner review required", state["reason"])
        self.assertEqual(state["corrections"], 0)
        self.assertEqual(state["advisory"]["findings"], [self.BLOCKING])
        self.assertEqual((self.root / "sample.txt").read_text(), "new\n")  # candidate preserved

    def test_primary_blocking_finding_is_corrected_before_any_advisory_call(self):
        self.packet["advisory_review"] = True
        self.packet["max_corrections"] = 0
        with self.fake_claude_cli(self.CLAUDE_PATCH, review_findings=[self.BLOCKING]):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("Correction budget exhausted", state["reason"])
        self.assertNotIn("advisory", [u["role"] for u in state["usage"]])

    def test_advisory_is_independent_after_a_correction(self):
        """A defect the primary missed twice must still reach an unprimed advisory reviewer."""
        self.packet.update(advisory_review=True, max_calls=7)
        self.packet["checks"][0]["argv"] = [sys.executable, "-c", "pass"]
        seen = {"reviews": 0, "advisory": None}
        def adapter(runner, role, feedback):
            if role == "worker":
                return {"patch": PATCH if not runner.state["corrections"] else "", "summary": "fixture"}
            if role == "coordinator":
                return {"action": "FIX", "reason": "fix"}
            review = {"candidate": runner.state["candidate"], "covered_files": json.loads(feedback)["files"],
                      "acceptance": runner.packet["acceptance"], "findings": []}
            if role == "reviewer":
                seen["reviews"] += 1
                if seen["reviews"] == 1:
                    review["findings"] = [self.BLOCKING]
                return review
            seen["advisory"] = json.loads(feedback)
            return review
        # The correction worker returns an empty patch, so seed a second real change instead.
        def worker_then_fix(runner, role, feedback):
            if role == "worker" and runner.state["corrections"]:
                return {"patch": "diff --git a/extra.txt b/extra.txt\nnew file mode 100644\n--- /dev/null\n+++ b/extra.txt\n@@ -0,0 +1 @@\n+fix\n",
                        "summary": "fix"}
            return adapter(runner, role, feedback)
        self.packet["owned_files"] = ["sample.txt", "extra.txt"]
        state = self.runner(worker_then_fix).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(state["corrections"], 1)
        self.assertNotIn("previous_findings", seen["advisory"])
        self.assertIn("independent", (Path(__file__).resolve().parents[1] / "coordination/advisory.md").read_text())

    def test_live_authority_requires_advisory_for_a_high_risk_plan(self):
        plans = self.root / "docs/plans/tasks"
        plans.mkdir(parents=True)
        (plans.parent / "ACTIVE.md").write_text("target: " + self.packet["plan"])
        (self.root / self.packet["plan"]).write_text("""<!-- storehouse-plan
id: selling-fixture
status: IN_PROGRESS
implementation_authority: GOAL_GRANTED
agent_strategy: SEQUENTIAL_WORKER
valuation_impact: PRESENT - Stock valuation changes.
-->
## Agent strategy
### Worker: implementation
- Model: gpt-5.6-terra
- Reasoning: medium
- Owns: sample.txt
""" + str(self.run_dir) + "\n")
        with patch("scripts.validate_plans.validate_repository", return_value=[]):
            with self.assertRaisesRegex(RunnerError, "require advisory_review"):
                authorize_live(dict(self.packet, advisory_review=False), self.run_dir)
            authorize_live(dict(self.packet, advisory_review=True), self.run_dir)

    def test_isolated_adapter_reviews_with_claude_and_advises_with_codex(self):
        from scripts.coordination.isolated import IsolatedAdapter
        auth = self.home / "codex-auth"
        auth.mkdir()
        (auth / "auth.json").write_text("{}")
        self.packet["advisory_review"] = True
        with self.fake_claude_cli(self.CLAUDE_PATCH):
            state = self.runner(IsolatedAdapter(auth)).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual([u["role"] for u in state["usage"]], ["worker", "reviewer", "advisory"])
        self.assertTrue(all(p.get("isolated") for p in state["prompts"]))
        self.assertTrue((self.run_dir / "codex-3").is_dir())  # advisory ran under its own CODEX_HOME

    def fake_muse_cli(self, steps=2):
        """Fake Muse install beside the fake Claude reviewer: a launcher, its pinned binary and a codex that only sandboxes."""
        context = self.fake_claude_cli(self.CLAUDE_PATCH, claude_worker=False)
        fake = self.home / "bin"
        (fake / "codex").write_text("#!" + sys.executable + "\n" + r"""import os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:3] == ['sandbox', '-P', 'muse'], 'Codex only sandboxes a Muse worker'
source, command = args[args.index('-C') + 1], args[args.index('--') + 1:]
home, profile = os.environ['HOME'], (Path(os.environ['CODEX_HOME']) / 'config.toml').read_text()
assert Path(home).parent == Path(os.environ['CODEX_HOME'])
assert '"' + source + '" = "read"' in profile and '"' + home + '" = "write"' in profile
assert '"' + command[0] + '" = "read"' in profile and profile.count('= "write"') == 1
assert '[permissions.muse.network]\nenabled = true' in profile
os.execv(command[0], command)
""")
        (fake / "muse").write_text("#!/bin/sh\necho 'the self-updating launcher must not run' >&2\nexit 96\n")
        (fake / ".muse-version").write_text("9.9.9-R1\n")
        binary = fake / "muse-bin-9.9.9-R1"
        binary.write_text("#!" + sys.executable + "\n" + r"""import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == 'exec'
for flag in ('--json', '--disable-write', '--disable-shell', '--disable-web-tools', '--no-foreign-personal-context'):
    assert flag in args, flag
assert args[args.index('--approval-mode') + 1] == 'never'
assert args[args.index('--model') + 1] == 'muse-spark-1.3-contributor'
assert args[args.index('--reasoning-effort') + 1] == 'medium'
home = Path(os.environ['HOME'])
assert json.loads((home / '.config/muse/auth.json').read_text()) == {'providers': {'meta': {'api_key': 'fixture'}}}
for flag in ('--output-schema', '--prompt-file'):
    assert home in Path(args[args.index(flag) + 1]).parents, flag
assert 'patch' in json.loads(Path(args[args.index('--output-schema') + 1]).read_text())['properties']
prompt = Path(args[args.index('--prompt-file') + 1]).read_text()
assert 'PACKET:' in prompt and args[args.index('--workspace') + 1] in prompt
def step(tokens_in, cached, out):
    return json.dumps({'payload': {'event': {'kind': 'model_completed', 'usage': {
        'input_tokens': tokens_in, 'cached_tokens': cached, 'output_tokens': out, 'reasoning_tokens': 1}}}})
session = home / '.local/share/muse/sessions/2026/09/30/main'
(session / 'subagent/helper').mkdir(parents=True)
(session / 'session.jsonl').write_text('\n'.join([step(1000, 400, 50)] * STEPS + ['not json', json.dumps(
    {'payload': {'event': {'kind': 'usage_recorded', 'usage': {'input_tokens': 9999, 'output_tokens': 9999}}}})]))
(session / 'subagent/helper/session.jsonl').write_text(step(30, 0, 5))
print('muse: workspace root', file=sys.stderr)
answer = json.dumps({'patch': PATCH_VALUE, 'summary': 'fake Muse patch'})
print(json.dumps({'payload_type': 'run.terminal.completed', 'payload': {'terminal': 'completed', 'text': answer + answer}}))
""".replace("PATCH_VALUE", repr(PATCH)).replace("STEPS", str(steps)))
        for path in (fake / "codex", fake / "muse", binary):
            path.chmod(0o755)
        login = self.home / "xdg" / "muse"
        login.mkdir(parents=True)
        (login / "auth.json").write_text(json.dumps({"providers": {"meta": {"api_key": "fixture"}}}))
        self.packet.update(worker_model="muse-spark-1.3-contributor", worker_reasoning="medium")
        return patch.dict(os.environ, dict(context.values, XDG_CONFIG_HOME=str(self.home / "xdg")))

    def test_isolated_adapter_runs_a_muse_worker_inside_the_codex_sandbox(self):
        from scripts.coordination.isolated import IsolatedAdapter
        with self.fake_muse_cli():
            state = self.runner(IsolatedAdapter(self.home / "no-codex-login")).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(state["pair"]["worker"], "muse:muse-spark-1.3-contributor:medium")
        # Every model step is counted once, helper sessions included; other usage records are not steps.
        self.assertEqual(state["usage"][0], {"call": 1, "role": "worker", "reported": [{
            "input_tokens": 2030, "cached_input_tokens": 800, "output_tokens": 105, "reasoning_output_tokens": 3,
            "model_steps": 3}]})
        self.assertEqual((self.root / "sample.txt").read_text(), "new\n")
        # The copied login does not outlive the call.
        self.assertTrue((self.run_dir / "muse-1" / "home" / "prompt.txt").is_file())
        self.assertFalse((self.run_dir / "muse-1" / "home" / ".config" / "muse" / "auth.json").exists())

    def test_a_muse_worker_stops_without_usage_or_outside_the_isolated_adapter(self):
        from scripts.coordination.isolated import IsolatedAdapter
        with self.fake_muse_cli(steps=0):
            (self.home / "bin" / "muse-bin-9.9.9-R1").write_text(
                (self.home / "bin" / "muse-bin-9.9.9-R1").read_text().replace("(session / 'subagent/helper/session.jsonl').write_text(step(30, 0, 5))", ""))
            state = self.runner(IsolatedAdapter(self.home / "no-codex-login")).run()
            self.assertEqual((state["phase"], state["reason"]), ("STOPPED", "Model usage missing; stop rather than lose batch accounting"))
            self.assertEqual((self.root / "sample.txt").read_text(), "old\n")
            self.run_dir = self.home / "run-2"
            state = self.runner(CodexAdapter()).run()
        self.assertEqual((state["phase"], state["reason"]), ("STOPPED", "A Muse worker runs only through the isolated adapter"))

    def test_a_meta_model_runs_in_codex_against_metas_api_and_the_key_does_not_outlive_the_call(self):
        from scripts.coordination.isolated import IsolatedAdapter
        context = self.fake_claude_cli(self.CLAUDE_PATCH, claude_worker=False)
        (self.home / "bin" / "codex").write_text("#!" + sys.executable + "\n" + r"""import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert '--strict-config' in args and '--ephemeral' in args
assert args[args.index('--model') + 1] == 'muse-spark-1.3-contributor', 'the provider prefix is not a model name'
home = Path(os.environ['CODEX_HOME'])
config = (home / 'config.toml').read_text()
assert config.startswith('model_provider = "meta"\n') and 'base_url = "https://api.meta.ai/v1"' in config
assert 'experimental_bearer_token = "fixture-key"' in config and '[permissions.runner.network]\nenabled = false' in config
assert not (home / 'auth.json').exists(), 'the ChatGPT login is not sent along'
assert 'PACKET:' in sys.stdin.read()
Path(args[args.index('-o') + 1]).write_text(json.dumps({'patch': PATCH_VALUE, 'summary': 'fake Muse patch in Codex'}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 30}}))
""".replace("PATCH_VALUE", repr(PATCH)))
        login = self.home / "xdg" / "muse"
        login.mkdir(parents=True)
        (login / "auth.json").write_text(json.dumps({"providers": {"meta": {"api_key": "fixture-key"}}}))
        auth = self.home / "codex-auth"
        auth.mkdir()
        (auth / "auth.json").write_text("{}")
        self.packet.update(worker_model="meta/muse-spark-1.3-contributor", worker_reasoning="medium")
        with patch.dict(os.environ, dict(context.values, XDG_CONFIG_HOME=str(self.home / "xdg"))):
            state = self.runner(IsolatedAdapter(auth)).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(state["pair"]["worker"], "codex:meta/muse-spark-1.3-contributor:medium")
        self.assertEqual(state["usage"][0]["reported"], [{"input_tokens": 100, "output_tokens": 30}])
        left = (self.run_dir / "codex-1" / "config.toml").read_text()
        self.assertNotIn("fixture-key", left)
        self.assertIn('model_provider = "meta"', left)

    def fake_reviewer_codex(self, review_model):
        """A codex stub for a reviewer run as meta/<id> in Codex's harness: it patches for the worker and reviews for the reviewer."""
        context = self.fake_claude_cli(self.CLAUDE_PATCH, claude_worker=False)
        (self.home / "bin" / "codex").write_text("#!" + sys.executable + "\n" + r"""import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert '--strict-config' in args and '--ephemeral' in args
home = Path(os.environ['CODEX_HOME'])
config = (home / 'config.toml').read_text()
prompt = sys.stdin.read()
packet = json.loads(prompt.split('PACKET:\n', 1)[1].split('\n', 1)[0])
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
model = args[args.index('--model') + 1]
if 'patch' in schema['properties']:
    assert model == packet['worker_model'] and 'model_provider = "meta"' not in config, 'only the reviewer goes to Meta'
    result = {'patch': PATCH_VALUE, 'summary': 'fake worker patch'}
else:
    assert model == 'REVIEW_MODEL', 'the provider prefix is not a model name: ' + model
    assert config.startswith('model_provider = "meta"\n') and 'experimental_bearer_token = "fixture-key"' in config
    assert 'model_reasoning_effort="medium"' in args
    assert not (home / 'auth.json').exists(), 'the ChatGPT login is not sent along'
    evidence = json.loads(prompt.split('\nEVIDENCE:\n', 1)[1])
    result = {'candidate': evidence['candidate'], 'covered_files': evidence['files'],
              'acceptance': packet['acceptance'], 'findings': []}
Path(args[args.index('-o') + 1]).write_text(json.dumps(result))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 30}}))
""".replace("PATCH_VALUE", repr(PATCH)).replace("REVIEW_MODEL", review_model))
        login = self.home / "xdg" / "muse"
        login.mkdir(parents=True)
        (login / "auth.json").write_text(json.dumps({"providers": {"meta": {"api_key": "fixture-key"}}}))
        auth = self.home / "codex-auth"
        auth.mkdir()
        (auth / "auth.json").write_text("{}")
        return auth, patch.dict(os.environ, dict(context.values, XDG_CONFIG_HOME=str(self.home / "xdg")))

    def test_a_meta_reviewer_runs_in_codex_and_the_pair_shows_the_reviewer_that_ran(self):
        from scripts.coordination.isolated import IsolatedAdapter
        auth, environment = self.fake_reviewer_codex("muse-spark-1.3-contributor")
        self.packet.update(reviewer_model="meta/muse-spark-1.3-contributor", reviewer_reasoning="medium")
        with environment:
            state = self.runner(IsolatedAdapter(auth)).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(state["pair"], {"worker": "codex:gpt-5.6-terra:medium",
                                         "reviewer": "codex:meta/muse-spark-1.3-contributor:medium"})
        self.assertEqual([u["role"] for u in state["usage"]], ["worker", "reviewer"])
        # The reviewer's own runtime home, and the key does not outlive its call.
        left = (self.run_dir / "codex-2" / "config.toml").read_text()
        self.assertIn('model_provider = "meta"', left)
        self.assertNotIn("fixture-key", left)
        self.assertNotIn("meta", (self.run_dir / "codex-1" / "config.toml").read_text())

    def test_a_meta_reviewer_stops_outside_the_isolated_adapter(self):
        self.packet.update(reviewer_model="meta/muse-spark-1.3-contributor", reviewer_reasoning="medium")
        with self.fake_claude_cli(self.CLAUDE_PATCH):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual((state["phase"], state["reason"]), ("STOPPED", "A meta/ model runs only through the isolated adapter"))

    def test_a_claude_reviewer_of_another_model_is_called_with_that_model_and_effort(self):
        context = self.fake_claude_cli(self.CLAUDE_PATCH, claude_worker=False)
        claude = self.home / "bin" / "claude"
        claude.write_text(claude.read_text().replace("== 'claude-opus-5-5'", "== 'claude-sonnet-5-5'")
                          .replace("== 'high'", "== 'low'"))
        self.packet.update(reviewer_model="claude-sonnet-5-5", reviewer_reasoning="low")
        with context:
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(state["pair"]["reviewer"], "claude:claude-sonnet-5-5:low")

    def test_reviewer_fields_are_optional_and_leave_an_older_packet_as_it_was(self):
        before = dict(self.packet)
        self.assertEqual({"reviewer_model", "reviewer_reasoning"} & validate_packet(dict(self.packet)).keys(), set())
        self.assertEqual(reviewer_model(self.packet), ("claude-opus-5-5", "high"))
        self.assertEqual(reviewer_model(dict(self.packet, reviewer_model="claude-sonnet-5-5")), ("claude-sonnet-5-5", "high"))
        self.assertEqual(self.packet, before)
        for change in ({"reviewer_model": ""}, {"reviewer_model": 5}, {"reviewer_reasoning": "hi gh"},
                       {"reviewer_model": "muse-spark-1.3-contributor"}, {"reviewer_model": "../x"}):
            with self.assertRaises(RunnerError, msg=change):
                validate_packet(dict(self.packet, **change))

    def test_a_packet_cannot_raise_a_limit_but_the_launcher_can(self):
        raised = {"max_corrections": 3, "max_calls": 9, "call_timeout": 3000, "total_timeout": 30000}
        for key, value in raised.items():
            with self.assertRaises(RunnerError, msg=key):
                validate_packet(dict(self.packet, **{key: value}))
            with self.assertRaises(RunnerError, msg=key):
                Runner(dict(self.packet, **{key: value}), self.run_dir, Adapter())
            self.assertEqual(validate_packet(dict(self.packet, **{key: value}), {key: value})[key], value)
            # A ceiling raises the cap for that limit only.
            other = next(name for name in raised if name != key)
            with self.assertRaises(RunnerError, msg=other):
                validate_packet(dict(self.packet, **{other: raised[other]}), {key: value})
        self.assertEqual(Runner(dict(self.packet, max_calls=9), self.run_dir, Adapter(), ceilings={"max_calls": 9}).packet["max_calls"], 9)
        for bad in ({"max_calls": "9"}, {"nonsense": 9}):
            with self.assertRaises(RunnerError, msg=bad):
                validate_packet(dict(self.packet), bad)
        # Defaults are unchanged.
        packet = validate_packet(dict(self.packet))
        self.assertEqual([packet[k] for k in ("max_corrections", "max_calls", "call_timeout", "total_timeout")], [2, 6, 2700, 28800])

    def test_advisory_review_must_be_boolean(self):
        self.packet["advisory_review"] = "yes"
        with self.assertRaises(RunnerError):
            validate_packet(self.packet)

    def test_claude_error_stops_with_usage_recorded(self):
        with self.fake_claude_cli({"type": "result", "subtype": "error_max_turns", "is_error": True,
                                   "usage": {"input_tokens": 7, "output_tokens": 3}}):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("Claude failed", state["reason"])
        self.assertEqual(state["usage"][0]["reported"][0]["input_tokens"], 7)
        self.assertEqual((self.root / "sample.txt").read_text(), "old\n")

    def test_claude_missing_usage_stops_before_patch(self):
        with self.fake_claude_cli({"type": "result", "subtype": "success", "is_error": False,
                                   "structured_output": {"patch": PATCH, "summary": "no usage"}}):
            state = self.runner(CodexAdapter()).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("usage missing", state["reason"])
        self.assertEqual((self.root / "sample.txt").read_text(), "old\n")

    def test_worker_cannot_patch_claude_controls(self):
        self.packet["owned_files"] = ["CLAUDE.md"]
        with self.assertRaises(RunnerError):
            validate_packet(self.packet)

    def test_new_file_included_in_complete_review(self):
        self.packet["owned_files"].append("created.txt")
        adapter = Adapter()
        added = "diff --git a/created.txt b/created.txt\nnew file mode 100644\n--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1 @@\n+created\n"
        def create(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "worker":
                result["patch"] += added
            if role == "reviewer":
                self.assertIn("created.txt", feedback)
                result["covered_files"] = ["created.txt", "sample.txt"]
            return result
        state = self.runner(create).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)

    def test_malformed_coverage_types_stop_cleanly(self):
        adapter = Adapter()
        def invalid(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "reviewer":
                result["covered_files"] = [1, "sample.txt"]
            return result
        self.assertEqual(self.runner(invalid).run()["phase"], "STOPPED")

    def test_stale_review_hash_rejected(self):
        adapter = Adapter()
        def stale(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "reviewer":
                result["candidate"] = "stale"
            return result
        state = self.runner(stale).run()
        self.assertEqual(state["phase"], "STOPPED")
        self.assertIn("stale", state["reason"])

    def test_total_deadline_prevents_restarting_work(self):
        self.runner().run()
        path = self.run_dir / "state.json"
        state = json.loads(path.read_text())
        state.update(phase="REVIEW", deadline=0)
        save_json(path, state)
        adapter = Adapter()
        result = self.runner(adapter).run(resume=True)
        self.assertEqual(result["phase"], "STOPPED")
        self.assertEqual(adapter.roles, [])

    def test_check_environment_drops_production_credentials(self):
        with patch.dict(os.environ, {"DATABASE_URL": "do-not-inherit", "EBAY_TOKEN": "do-not-inherit", "AWS_SECRET_ACCESS_KEY": "do-not-inherit"}):
            env = Runner.environment()
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn("EBAY_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["EBAY_ENVIRONMENT"], "mocked")

    def test_child_retains_lock_after_parent_crash(self):
        # This exercises actual file-descriptor inheritance and process death,
        # not only nested locks inside this test process.
        project = str(Path(__file__).resolve().parents[2])
        pidfile = self.home / "child.pid"
        code = """import subprocess, sys, os
from pathlib import Path
from scripts.coordination.runner import checkout_lock
with checkout_lock(Path(sys.argv[1])) as fd:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], pass_fds=(fd,), start_new_session=True)
    Path(sys.argv[2]).write_text(str(child.pid))
    os._exit(0)
"""
        subprocess.run([sys.executable, "-c", code, str(self.root), str(pidfile)],
                       cwd=project, check=True, timeout=5)
        child = int(pidfile.read_text())
        try:
            with self.assertRaisesRegex(RunnerError, "Another runner"):
                self.runner().run()
        finally:
            os.killpg(child, signal.SIGKILL)
        # The kernel drops the inherited descriptor after the process exits.
        deadline = time.monotonic() + 3
        while True:
            try:
                with checkout_lock(self.root):
                    break
            except RunnerError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)

    def test_authority_not_granted_by_packet(self):
        with self.assertRaises((RunnerError, FileNotFoundError)):
            authorize_live(self.packet, self.run_dir)

    def test_authority_fields_must_come_from_worker_section(self):
        plans = self.root / "docs/plans/tasks"
        plans.mkdir(parents=True)
        (plans.parent / "ACTIVE.md").write_text("target: " + self.packet["plan"])
        path = self.root / self.packet["plan"]
        content = """<!-- storehouse-plan
id: selling-fixture
status: IN_PROGRESS
implementation_authority: GOAL_GRANTED
agent_strategy: SEQUENTIAL_WORKER
-->
## Agent strategy
### Worker: implementation
- Model: gpt-5.6-sol
- Reasoning: high
- Owns: another-file.txt
## Notes
- Model: gpt-5.6-terra
- Reasoning: medium
- Owns: sample.txt
""" + str(self.run_dir) + "\n"
        path.write_text(content)
        # Isolate the runner's contract matching from the separately tested full
        # plan validator: descriptive notes must never satisfy frozen fields.
        with patch("scripts.validate_plans.validate_repository", return_value=[]):
            with self.assertRaisesRegex(RunnerError, "frozen worker contract"):
                authorize_live(self.packet, self.run_dir)
            path.write_text(content.replace("- Model: gpt-5.6-sol", "- Model: gpt-5.6-terra")
                            .replace("- Reasoning: high", "- Reasoning: medium")
                            .replace("- Owns: another-file.txt", "- Owns: sample.txt"))
            authorize_live(self.packet, self.run_dir)

    def test_validation_mode_makes_no_calls(self):
        path = self.home / "packet.json"
        path.write_text(json.dumps(self.packet))
        self.assertEqual(main([str(path), "--run-dir", str(self.run_dir)]), 0)
        self.assertFalse(self.run_dir.exists())

    def test_empty_checks_and_bad_limits_rejected(self):
        for change in ({"checks": []}, {"max_calls": 100}, {"total_timeout": True}, {"luna_triage": "yes"}):
            with self.assertRaises(RunnerError):
                validate_packet(dict(self.packet, **change))

    # The attempt log: one line per attempt, written by the loop when the run ends.
    def log_lines(self, path=None):
        path = path or self.home / "attempts.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_finished_run_appends_one_line_and_resume_does_not_repeat_it(self):
        decision = {"policy": "random", "action": "cheap", "probability": 0.5, "exploration": True, "parent_packet": None}
        state = Runner(self.packet, self.run_dir, Adapter(), decision=decision).run()
        (line,) = self.log_lines()
        self.assertEqual(line["attempt_id"], state["attempt_id"])
        self.assertEqual((line["packet_id"], line["phase"], line["stop_category"]), ("selling-fixture", "LOCAL_REVIEWED", None))
        self.assertEqual((line["run_dir"], line["pair"], line["decision"]), (str(self.run_dir), state["pair"], decision))
        self.assertEqual((line["timeline"], line["corrections_log"], line["blocking_findings"]),
                         (state["timeline"], [], 0))
        self.assertEqual(line["checks"], [{"id": "content", "exit_code": 0}])
        self.assertEqual([u["role"] for u in line["usage"]], [u["role"] for u in state.get("usage", [])])
        self.assertEqual(line["packet_hash"], state["packet_hash"])
        self.assertTrue(line["loop_version"] is None or len(line["loop_version"]) == 40)
        # No packet text: the objective and acceptance stay in the packet file.
        self.assertNotIn("Replace old with new", json.dumps(line))
        self.assertTrue(state["logged"])
        self.runner().run(resume=True)
        self.assertEqual(len(self.log_lines()), 1)

    def test_a_crash_between_append_and_checkpoint_repeats_the_line_with_the_same_id(self):
        self.runner().run()
        state = json.loads((self.run_dir / "state.json").read_text())
        state["logged"] = False
        save_json(self.run_dir / "state.json", state)
        self.runner().run(resume=True)
        first, second = self.log_lines()
        self.assertEqual(first, second)
        self.assertEqual(first["attempt_id"], state["attempt_id"])

    def test_stopped_run_is_logged_once_with_its_category_and_resume_adds_nothing(self):
        self.packet["max_corrections"] = 0
        state = self.runner(Adapter([finding()])).run()
        (line,) = self.log_lines()
        self.assertEqual((line["phase"], line["stop_category"], line["blocking_findings"]), ("STOPPED", "corrections_exhausted", 1))
        self.assertEqual(line["reason"], state["reason"])
        self.runner().run(resume=True)
        self.assertEqual(len(self.log_lines()), 1)

    def test_every_kind_of_stop_has_its_category(self):
        def category(adapter, **packet):
            self.tmp2 = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp2.cleanup)
            git(self.root, "checkout", "--", "sample.txt")
            state = Runner(dict(self.packet, **packet), Path(self.tmp2.name) / "run", adapter).run()
            self.assertEqual(state["phase"], "STOPPED")
            return state["stop_category"], self.log_lines(Path(self.tmp2.name) / "attempts.jsonl")[0]

        def raises(exc):
            def adapter(runner, role, feedback):
                raise exc
            return adapter

        def advisory_blocks(runner, role, feedback):
            result = Adapter()(runner, role, feedback)
            if role == "advisory":
                result["findings"] = [finding()]
            return result

        self.assertEqual(category(Adapter([finding()]), max_corrections=0)[0], "corrections_exhausted")
        self.assertEqual(category(Adapter(), max_calls=1)[0], "budget_cap")
        self.assertEqual(category(raises(KeyboardInterrupt()))[0], "operator_stop")
        self.assertEqual(category(raises(RunnerError("Codex failed or returned no bounded structured result")))[0], "harness_failure")
        self.assertEqual(category(raises(OSError("disk")))[0], "harness_failure")
        self.assertEqual(category(advisory_blocks, advisory_review=True, luna_triage=False)[0], "advisory_blocking")
        secret = "Blocked: quotes private_module.py"
        cat, line = category(lambda *_: {"patch": "", "summary": secret})
        self.assertEqual((cat, line["reason"]), ("no_patch", "Worker returned no patch"))
        self.assertNotIn("private_module", json.dumps(line))

    def test_a_patch_outside_the_owned_files_is_a_scope_violation_not_a_harness_failure(self):
        stray = "diff --git a/stray.txt b/stray.txt\nnew file mode 100644\n--- /dev/null\n+++ b/stray.txt\n@@ -0,0 +1 @@\n+stray\n"
        adapter = Adapter()

        def outside(runner, role, feedback):
            result = adapter(runner, role, feedback)
            if role == "worker":
                result["patch"] += stray
            return result
        state = self.runner(outside).run()
        self.assertEqual((state["phase"], state["stop_category"]), ("STOPPED", "scope_violation"))
        (line,) = self.log_lines()
        self.assertEqual(line["stop_category"], "scope_violation")
        self.assertIn("stray.txt", line["reason"])

    def test_corrections_record_their_cause_call_and_time(self):
        self.packet["luna_triage"] = False
        worker_calls = []

        def adapter(runner, role, feedback):
            if role == "worker":
                worker_calls.append(1)
                if len(worker_calls) == 1:
                    return {"patch": "not a diff", "summary": "broken"}
                if len(worker_calls) == 2:
                    return {"patch": PATCH.replace("+new", "+bad"), "summary": "wrong text"}
                return {"patch": PATCH.replace("-old", "-bad"), "summary": "fix"}
            result = Adapter()(runner, role, feedback)
            if len(worker_calls) == 3 and runner.state["corrections"] == 2:
                result["findings"] = [finding()]
            return result
        self.packet["max_corrections"] = 2
        state = self.runner(adapter).run()
        self.assertEqual([(e["round"], e["cause"], e["call"]) for e in state["corrections_log"]],
                         [(1, "patch_failed", 1), (2, "check_failed", 2)])
        self.assertEqual(state["corrections"], sum(not e.get("format_retry") for e in state["corrections_log"]))
        self.assertTrue(all(isinstance(e["at"], float) for e in state["corrections_log"]))
        self.assertEqual(self.log_lines()[0]["corrections_log"], state["corrections_log"])

    def test_review_findings_are_a_correction_cause(self):
        reviews = []

        def adapter(runner, role, feedback):
            result = Adapter()(runner, role, feedback)
            if role == "worker" and reviews:
                return {"patch": PATCH.replace("-old", "-new").replace("+new", "+new "), "summary": "x"}
            if role == "reviewer":
                reviews.append(1)
                if len(reviews) == 1:
                    result["findings"] = [finding()]
            return result
        self.packet["luna_triage"] = False
        state = self.runner(adapter).run()
        self.assertEqual([(e["cause"], e["call"]) for e in state["corrections_log"]][:1], [("review_blocking", 2)])

    def test_checkpoint_from_before_the_attempt_log_resumes_and_is_adopted_or_left_alone(self):
        self.runner().run()
        legacy = json.loads((self.run_dir / "state.json").read_text())
        for key in ("attempt_id", "decision", "corrections_log", "logged", "stop_category"):
            legacy.pop(key, None)
        save_json(self.run_dir / "state.json", legacy)
        # Finished before the log existed: returned as recorded, nothing appended.
        self.assertEqual(self.runner().run(resume=True)["phase"], "LOCAL_REVIEWED")
        self.assertEqual(len(self.log_lines()), 1)
        # Unfinished: it completes and is logged under a new id.
        legacy["phase"] = "REVIEW"
        save_json(self.run_dir / "state.json", legacy)
        resumed = self.runner().run(resume=True)
        self.assertEqual(resumed["phase"], "LOCAL_REVIEWED")
        self.assertEqual(self.log_lines()[-1]["attempt_id"], resumed["attempt_id"])
        self.assertEqual(self.log_lines()[-1]["corrections_log"], [])

    def test_decision_is_stored_untouched_and_a_launcher_can_name_the_log(self):
        decision = {"policy": "p", "action": "split", "probability": 0.25, "exploration": False, "parent_packet": "big-1"}
        target = self.home / "elsewhere" / "log.jsonl"
        state = Runner(self.packet, self.run_dir, Adapter(), decision=decision, attempt_log=target).run()
        self.assertEqual(state["decision"], decision)
        self.assertEqual(self.log_lines(target)[0]["decision"], decision)
        self.assertEqual(self.log_lines(), [])
        with self.assertRaises(RunnerError):
            Runner(self.packet, self.home / "other", Adapter(), decision=["not", "an", "object"])
        with self.assertRaises(RunnerError):
            Runner(self.packet, self.home / "other", Adapter(), decision={"probability": {1}})

    def test_a_log_that_cannot_be_written_does_not_change_the_run(self):
        (self.home / "blocked").mkdir()
        state = Runner(self.packet, self.run_dir, Adapter(), attempt_log=self.home / "blocked").run()
        self.assertEqual((state["phase"], state["logged"]), ("LOCAL_REVIEWED", False))

    def test_cli_flags_carry_the_decision_and_log_path(self):
        (self.home / "decision.json").write_text('{"policy": "cli"}')
        (self.home / "packet.json").write_text(json.dumps(self.packet))
        with patch("scripts.coordination.runner.authorize_live"), patch("scripts.coordination.runner.CodexAdapter", Adapter):
            code = main([str(self.home / "packet.json"), "--run-dir", str(self.run_dir), "--execute",
                         "--decision-file", str(self.home / "decision.json"), "--attempt-log", str(self.home / "cli.jsonl")])
        self.assertEqual(code, 0)
        self.assertEqual(self.log_lines(self.home / "cli.jsonl")[0]["decision"], {"policy": "cli"})


class HookTests(unittest.TestCase):
    def test_pretool_uses_supported_deny_shape(self):
        result = respond({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stop_cannot_invent_infinite_continuation(self):
        for active in (False, True):
            self.assertEqual(respond({"hook_event_name": "Stop", "stop_hook_active": active}), {"continue": True})


if __name__ == "__main__":
    unittest.main()
