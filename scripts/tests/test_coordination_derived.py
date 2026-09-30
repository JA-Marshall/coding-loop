"""Hidden check overlay and derived-packet launcher, on synthetic Git fixtures: no network or models."""
import contextlib
import io
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import unittest

from scripts.coordination import derived
from scripts.coordination.isolated import snapshot
from scripts.coordination.runner import Runner, RunnerError, fingerprint, git, hidden_overlay, shown_packet, validate_packet

PRODUCT = "def double(value):\n    return value\n"
FIXED = "def double(value):\n    return value * 2\n"
OLD_TEST = "from product import double\nassert double(0) == 0\n"
NEW_TEST = "from product import double\nassert double(0) == 0\nassert double(3) == 6\nprint('HIDDEN TEST RAN')\n"
RUN_TEST = "import runpy; runpy.run_path('tests/test_product.py')"
PATCH = """diff --git a/product.py b/product.py
--- a/product.py
+++ b/product.py
@@ -1,2 +1,2 @@
 def double(value):
-    return value
+    return value * 2
"""


class Worker:
    """Returns the fix, then a clean review; remembers what the checkout held whenever a model was called."""
    def __init__(self, patch_text=PATCH):
        self.patch_text, self.roles, self.seen = patch_text, [], []

    def __call__(self, runner, role, feedback):
        self.roles.append(role)
        self.seen.append((runner.root / "tests" / "test_product.py").read_text())
        if role == "worker":
            return {"patch": self.patch_text, "summary": "Double the value"}
        return {"candidate": runner.state["candidate"], "covered_files": ["product.py"],
                "acceptance": runner.packet["acceptance"], "findings": []}


@unittest.skipUnless(sys.platform.startswith("linux"), "Runner requires Linux/WSL")
class DerivedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        guard = self.home / "guard-bin"
        guard.mkdir()
        for name in ("claude", "codex"):
            stub = guard / name
            stub.write_text("#!/bin/sh\necho 'live model CLI called from a test' >&2\nexit 97\n")
            stub.chmod(0o755)
        env = patch.dict(os.environ, {"PATH": str(guard) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        # A public-style repository: a base commit, then the merged fix with its updated test.
        self.source = self.home / "source"
        (self.source / "tests").mkdir(parents=True)
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "user.name", "Fixture")
        (self.source / "product.py").write_text(PRODUCT)
        (self.source / "tests" / "test_product.py").write_text(OLD_TEST)
        git(self.source, "add", "-A")
        git(self.source, "commit", "-m", "base")
        self.base = git(self.source, "rev-parse", "HEAD").decode().strip()
        (self.source / "product.py").write_text(FIXED)
        (self.source / "tests" / "test_product.py").write_text(NEW_TEST)
        git(self.source, "add", "-A")
        git(self.source, "commit", "-m", "merged fix")
        self.merge = git(self.source, "rev-parse", "HEAD").decode().strip()
        self.derived = {
            "id": "widgets-pr7", "base_sha": self.base, "branch": "codex/widgets-pr7", "merge_sha": self.merge,
            "objective": "double(value) returns twice the value", "acceptance": ["double(3) is 6"],
            "owned_files": ["product.py"], "repo": "https://github.com/acme/widgets",
            "checks": [{"id": "hidden-tests", "argv": ["python", "-B", "-c", RUN_TEST], "timeout": 30}],
            "hidden_checks": {"ref": self.merge, "files": ["tests/test_product.py"]},
            "contract": ["product.double"], "source": {"pr": 7},
        }
        self.packet_file = self.home / "packet.json"
        self.packet_file.write_text(json.dumps(self.derived))
        self.directory = self.home / "work"

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = derived.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def prepared(self):
        code, text = self.call("prepare", str(self.packet_file), "--source", str(self.source), "--directory", str(self.directory))
        self.assertEqual(code, 0, text)
        return self.directory / "checkout"

    def loop_packet(self, root, **changes):
        return dict({
            "id": "widgets-pr7", "checkout": str(root), "base_sha": self.base, "branch": "codex/widgets-pr7",
            "objective": "double(value) returns twice the value", "acceptance": ["double(3) is 6"],
            "owned_files": ["product.py"], "worker_model": "gpt-5.6-terra", "worker_reasoning": "medium",
            "checks": [{"id": "hidden-tests", "argv": [sys.executable, "-B", "-c", RUN_TEST], "timeout": 30}],
            "plan": derived.PLAN, "luna_triage": False, "hidden_overlay": str(self.directory / "hidden")}, **changes)

    def test_hidden_files_judge_the_candidate_and_are_never_part_of_it(self):
        root = self.prepared()
        adapter = Worker()
        state = Runner(self.loop_packet(root), self.home / "run", adapter).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(adapter.roles, ["worker", "reviewer"])
        # The check ran the merged test; no model call and no review diff ever saw it.
        self.assertIn("HIDDEN TEST RAN", (self.home / "run" / "check-0-hidden-tests.log").read_text())
        self.assertEqual(adapter.seen, [OLD_TEST, OLD_TEST])
        self.assertEqual((root / "tests" / "test_product.py").read_text(), OLD_TEST)
        diff = (self.home / "run" / "candidate.diff").read_text()
        self.assertIn("product.py", diff)
        self.assertNotIn("test_product", diff)
        self.assertEqual(state["checks"]["candidate"], fingerprint(root))

    def test_a_candidate_that_only_passes_the_old_test_fails_the_hidden_one(self):
        root = self.prepared()
        wrong = PATCH.replace("+    return value * 2", "+    return value + 0")
        state = Runner(self.loop_packet(root, max_corrections=0), self.home / "run", Worker(wrong)).run()
        self.assertEqual((state["phase"], state["reason"]), ("STOPPED", "Correction budget exhausted"))
        self.assertEqual(state["checks"]["results"][0]["exit_code"], 1)

    def improving(self):
        """A worker whose first two answers fail the hidden test and whose third passes it."""
        def step(old, new):
            return (f"diff --git a/product.py b/product.py\n--- a/product.py\n+++ b/product.py\n@@ -1,2 +1,2 @@\n"
                    f" def double(value):\n-    return {old}\n+    return {new}\n")
        steps = [step("value", "value + 0"), step("value + 0", "value + 1"), step("value + 1", "value * 2")]
        class Improving(Worker):
            def __call__(self, runner, role, feedback):
                if role == "worker":
                    self.patch_text = steps[self.roles.count("worker")]
                return super().__call__(runner, role, feedback)
        return Improving()

    def test_failed_hidden_checks_have_their_own_correction_rounds(self):
        root = self.prepared()
        adapter = self.improving()
        # Review rounds are spent, but failed checks draw on their own budget.
        state = Runner(self.loop_packet(root, max_corrections=0, max_check_corrections=2), self.home / "run", adapter).run()
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual((state["corrections"], state["check_corrections"]), (0, 2))
        self.assertEqual(adapter.roles, ["worker", "worker", "worker", "reviewer"])
        self.assertEqual([e["cause"] for e in state["corrections_log"]], ["check_failed", "check_failed"])
        for number in range(3):  # each round's check log is kept
            self.assertTrue((self.home / "run" / f"check-{number}-hidden-tests.log").is_file())

    def test_the_check_budget_stops_an_attempt_that_review_rounds_would_not(self):
        root = self.prepared()
        state = Runner(self.loop_packet(root, max_corrections=2, max_check_corrections=1), self.home / "run",
                       self.improving()).run()
        self.assertEqual((state["phase"], state["reason"], state["stop_category"]),
                         ("STOPPED", "Check correction budget exhausted", "corrections_exhausted"))
        self.assertEqual((state["corrections"], state["check_corrections"]), (0, 1))

    def test_overlay_creates_and_removes_new_files_and_is_restored_when_the_check_raises(self):
        root = self.prepared()
        hidden = self.directory / "hidden"
        (hidden / "tests" / "deep" / "more").mkdir(parents=True)
        (hidden / "tests" / "deep" / "more" / "test_new.py").write_text("new")
        before = fingerprint(root)
        with self.assertRaises(ZeroDivisionError):
            with hidden_overlay(root, hidden, ["product.py"]) as names:
                self.assertEqual(names, ["tests/deep/more/test_new.py", "tests/test_product.py"])
                self.assertEqual((root / "tests" / "deep" / "more" / "test_new.py").read_text(), "new")
                self.assertEqual((root / "tests" / "test_product.py").read_text(), NEW_TEST)
                1 / 0
        self.assertFalse((root / "tests" / "deep").exists())
        self.assertEqual(fingerprint(root), before)

    def test_unusable_overlays_are_refused_before_any_model_call(self):
        root = self.prepared()
        hidden = self.directory / "hidden"
        adapter = Worker()
        cases = {"owned": lambda: (hidden / "product.py").write_text("x"),
                 "link": lambda: (hidden / "link.py").symlink_to(hidden / "tests" / "test_product.py"),
                 "control": lambda: (hidden / "AGENTS.md").write_text("x")}
        for name, spoil in cases.items():
            spoil()
            with self.assertRaises(RunnerError, msg=name):
                Runner(self.loop_packet(root), self.home / ("run-" + name), adapter).run()
            for extra in ("product.py", "link.py", "AGENTS.md"):
                (hidden / extra).unlink(missing_ok=True)
        for where in (root / "tests", self.home / "missing"):
            with self.assertRaises(RunnerError):
                Runner(self.loop_packet(root, hidden_overlay=str(where)), self.home / "run-where", adapter).run()
        empty = self.home / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(RunnerError, "empty"):
            Runner(self.loop_packet(root, hidden_overlay=str(empty)), self.home / "run-empty", adapter).run()
        with self.assertRaisesRegex(RunnerError, "absolute"):
            validate_packet(self.loop_packet(root, hidden_overlay="hidden"))
        self.assertEqual(adapter.roles, [])

    def test_models_are_not_told_where_the_hidden_files_are(self):
        packet = validate_packet(self.loop_packet(self.home / "anywhere"))
        self.assertNotIn(str(self.directory), json.dumps(shown_packet(packet)))
        self.assertEqual(set(packet) - set(shown_packet(packet)), {"hidden_overlay"})

    def test_prepare_pins_the_checkout_and_keeps_the_merged_tests_outside_it(self):
        root = self.prepared()
        self.assertEqual(git(root, "rev-parse", "HEAD").decode().strip(), self.base)
        self.assertEqual(git(root, "branch", "--show-current").decode().strip(), "codex/widgets-pr7")
        self.assertEqual(git(root, "remote", "get-url", "origin").decode().strip(), "https://github.com/acme/widgets")
        self.assertEqual((root / "tests" / "test_product.py").read_text(), OLD_TEST)
        self.assertEqual((self.directory / "hidden" / "tests" / "test_product.py").read_text(), NEW_TEST)
        code, text = self.call("prepare", str(self.packet_file), "--source", str(self.source), "--directory", str(self.directory))
        self.assertEqual((code, "never reset" in text), (2, True))
        (self.source / "docs" / "plans").mkdir(parents=True)
        (self.source / "docs" / "plans" / "ACTIVE.md").write_text("")
        code, text = self.call("prepare", str(self.packet_file), "--source", str(self.source), "--directory", str(self.home / "other"))
        self.assertEqual((code, "plan lifecycle" in text), (2, True))
        self.packet_file.write_text(json.dumps({key: value for key, value in self.derived.items() if key != "hidden_checks"}))
        code, text = self.call("prepare", str(self.packet_file), "--source", str(self.source), "--directory", str(self.home / "third"))
        self.assertEqual((code, "Not a derived packet" in text), (2, True))

    def test_validation_needs_the_checks_to_fail_at_base_and_pass_with_the_merged_change(self):
        root = self.prepared()
        code, text = self.call("validate", "--directory", str(self.directory), "--python", sys.executable)
        self.assertEqual(code, 0, text)
        self.assertIn("checks at base fail, with the merged change pass -> valid", text)
        verdict = json.loads((self.directory / "validation.json").read_text())
        self.assertEqual((verdict["valid"], verdict["at_base"][0]["exit_code"], verdict["with_merged_change"][0]["exit_code"]),
                         (True, 1, 0))
        # The checkout is left as it was found: at base, clean, with the old test.
        self.assertEqual(git(root, "status", "--porcelain"), b"")
        self.assertEqual(((root / "product.py").read_text(), (root / "tests" / "test_product.py").read_text()), (PRODUCT, OLD_TEST))

    def test_a_packet_whose_tests_already_pass_at_base_is_not_usable(self):
        self.prepared()
        (self.directory / "hidden" / "tests" / "test_product.py").write_text(OLD_TEST)
        code, text = self.call("validate", "--directory", str(self.directory), "--python", sys.executable)
        self.assertEqual(code, 1)
        self.assertIn("PASS (nothing to do)", text)
        self.assertIs(json.loads((self.directory / "validation.json").read_text())["valid"], False)
        code, text = self.call("run", "--directory", str(self.directory), "--worker-model", "gpt-5.6-terra",
                               "--worker-reasoning", "medium", "--auth-home", str(self.home), "--live")
        self.assertEqual((code, "did not validate" in text), (2, True))

    def test_an_attempt_runs_in_its_own_checkout_with_one_reviewer_and_no_advisory(self):
        self.prepared()
        self.assertEqual(self.call("validate", "--directory", str(self.directory), "--python", sys.executable)[0], 0)
        argv = ["run", "--directory", str(self.directory), "--worker-model", "gpt-5.6-terra", "--worker-reasoning", "medium",
                "--auth-home", str(self.home)]
        code, text = self.call(*argv)
        self.assertEqual((code, "--live is required" in text), (2, True))
        adapter, made = Worker(), []
        def native(auth_home, python):
            made.append(python)
            return adapter
        with patch.object(derived, "NativeAdapter", native):
            code, text = self.call(*argv, "--live")
            self.assertEqual(code, 0, text)
            again, refused = self.call(*argv, "--live")
        self.assertEqual((again, "attempt exists" in refused), (2, True))
        # The worker edits a copy in a container, with the validated interpreter for the visible tests.
        self.assertEqual(made, [sys.executable])
        attempt = self.directory / "attempts" / "gpt-5-6-terra-medium--claude-opus-5-5-high"
        packet = json.loads((attempt / "packet.json").read_text())
        self.assertEqual((packet["advisory_review"], packet["luna_triage"], packet["plan"]), (False, False, derived.PLAN))
        self.assertEqual(packet["checks"][0]["argv"][0], sys.executable)
        self.assertEqual(packet["checkout"], str(attempt / "checkout"))
        state = json.loads((attempt / "run" / "state.json").read_text())
        self.assertEqual((state["phase"], state["calls"], adapter.roles), ("LOCAL_REVIEWED", 2, ["worker", "reviewer"]))
        self.assertEqual(state["pair"], {"worker": "codex:gpt-5.6-terra:medium", "reviewer": "claude:claude-opus-5-5:high"})
        self.assertNotIn("advisory", state)
        # The candidate is in the attempt's checkout; the validation checkout is untouched.
        self.assertEqual((attempt / "checkout" / "product.py").read_text(), FIXED)
        self.assertEqual((self.directory / "checkout" / "product.py").read_text(), PRODUCT)

    def test_the_read_only_worker_remains_available_by_flag(self):
        self.prepared()
        self.assertEqual(self.call("validate", "--directory", str(self.directory), "--python", sys.executable)[0], 0)
        adapter = Worker()
        with patch.object(derived, "IsolatedAdapter", lambda auth_home: adapter), \
                patch.object(derived, "NativeAdapter", side_effect=AssertionError("native adapter used")):
            code, text = self.call("run", "--directory", str(self.directory), "--worker-model", "gpt-5.6-terra",
                                   "--worker-reasoning", "medium", "--auth-home", str(self.home), "--live",
                                   "--read-only-worker")
        self.assertEqual((code, adapter.roles), (0, ["worker", "reviewer"]), text)

    def test_two_reviewers_on_one_packet_get_their_own_attempts_and_the_launcher_raises_a_limit(self):
        self.prepared()
        self.assertEqual(self.call("validate", "--directory", str(self.directory), "--python", sys.executable)[0], 0)
        argv = ["run", "--directory", str(self.directory), "--worker-model", "gpt-5.6-terra", "--worker-reasoning", "medium",
                "--auth-home", str(self.home), "--live"]
        with patch.object(derived, "NativeAdapter", lambda auth_home, python: Worker()):
            self.assertEqual(self.call(*argv)[0], 0)
            code, text = self.call(*argv, "--reviewer-model", "meta/muse-spark-1.3-contributor", "--reviewer-reasoning", "medium",
                                   "--total-timeout", "30000", "--max-corrections", "1")
            self.assertEqual(code, 0, text)
            self.assertEqual(self.call(*argv)[0], 2)  # the first pair's attempt still exists
        default = json.loads((self.directory / "attempts" / "gpt-5-6-terra-medium--claude-opus-5-5-high" / "packet.json").read_text())
        muse = self.directory / "attempts" / "gpt-5-6-terra-medium--meta-muse-spark-1-3-contributor-medium"
        packet = json.loads((muse / "packet.json").read_text())
        # A packet made without the flags records no reviewer and keeps the default limits.
        self.assertEqual(({"reviewer_model", "reviewer_reasoning"} & default.keys(), default["total_timeout"]), (set(), 14400))
        # Failed hidden checks get their own rounds; the call budget is raised to cover them.
        self.assertEqual((default["max_check_corrections"], default["max_corrections"], default["max_calls"]), (5, 2, 12))
        self.assertEqual((packet["reviewer_model"], packet["reviewer_reasoning"], packet["total_timeout"], packet["max_corrections"]),
                         ("meta/muse-spark-1.3-contributor", "medium", 30000, 1))
        state = json.loads((muse / "run" / "state.json").read_text())
        self.assertEqual(state["pair"], {"worker": "codex:gpt-5.6-terra:medium",
                                         "reviewer": "codex:meta/muse-spark-1.3-contributor:medium"})
        self.assertFalse(packet["advisory_review"] or packet["luna_triage"])

    def test_black_data_cases_are_run_through_its_format_test(self):
        black = {"repo": "https://github.com/psf/black"}
        one = ["python", "-m", "pytest", "-q", "tests/data/cases/fmtskip10.py"]
        two = one[:4] + ["tests/data/cases/a_1.py", "tests/data/line_ranges_formatted/b.py"]
        self.assertEqual(derived.runnable_argv(black, one), one[:4] + ["tests/test_format.py", "-k", "fmtskip10"])
        self.assertEqual(derived.runnable_argv(black, two), two[:4] + ["tests/test_format.py", "-k", "a_1 or b"])
        # Ordinary test files, mixed targets and other repositories are left alone.
        mixed = one + ["tests/test_black.py"]
        self.assertEqual(derived.runnable_argv(black, mixed), mixed)
        self.assertEqual(derived.runnable_argv(black, ["python", "-m", "pytest", "-q", "tests/test_black.py"]),
                         ["python", "-m", "pytest", "-q", "tests/test_black.py"])
        self.assertEqual(derived.runnable_argv({"repo": "https://github.com/pallets/click"}, one), one)

    def test_symbolic_links_tracked_at_base_are_left_out_so_the_loop_accepts_the_checkout(self):
        # Rebuild the public repository with two tracked links at its base commit: one to a file, one to a directory.
        git(self.source, "checkout", "-q", "-B", "main", self.base)
        os.symlink("product.py", self.source / "ALIAS.py")
        os.symlink("../tests", self.source / "tests" / "again")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-m", "base with links")
        base = git(self.source, "rev-parse", "HEAD").decode().strip()
        (self.source / "product.py").write_text(FIXED)
        (self.source / "tests" / "test_product.py").write_text(NEW_TEST)
        git(self.source, "commit", "-am", "merged fix")
        merge = git(self.source, "rev-parse", "HEAD").decode().strip()
        self.packet_file.write_text(json.dumps(dict(
            self.derived, base_sha=base, merge_sha=merge, hidden_checks={"ref": merge, "files": ["tests/test_product.py"]})))
        root = self.prepared()
        self.assertEqual(json.loads((self.directory / "derived.json").read_text())["omitted_symlinks"],
                         ["ALIAS.py", "tests/again"])
        self.assertFalse(os.path.lexists(root / "ALIAS.py") or os.path.lexists(root / "tests" / "again"))
        self.assertEqual((git(root, "rev-parse", "HEAD").decode().strip(), git(root, "status", "--porcelain")), (base, b""))
        fingerprint(root)
        snapshot(root, self.home / "snapshot")
        self.assertEqual(sorted(path.name for path in (self.home / "snapshot").iterdir() if path.name != ".git"),
                         ["product.py", "tests"])
        self.assertEqual(self.call("validate", "--directory", str(self.directory), "--python", sys.executable)[0], 0)
        adapter = Worker()
        with patch.object(derived, "NativeAdapter", lambda auth_home, python: adapter):
            code, text = self.call("run", "--directory", str(self.directory), "--worker-model", "gpt-5.6-terra",
                                   "--worker-reasoning", "medium", "--auth-home", str(self.home), "--live")
        self.assertEqual(code, 0, text)
        attempt = self.directory / "attempts" / "gpt-5-6-terra-medium--claude-opus-5-5-high" / "checkout"
        self.assertEqual(((attempt / "product.py").read_text(), os.path.lexists(attempt / "ALIAS.py")), (FIXED, False))
        # A link the worker adds is still refused.
        os.symlink("product.py", attempt / "added.py")
        with self.assertRaisesRegex(RunnerError, "Symlinks require manual handling"):
            fingerprint(attempt)


if __name__ == "__main__":
    unittest.main()
