"""collect() against examples/smoke-run and synthetic batch trees. No writes, ever."""
import json
import os
import re
from pathlib import Path
import shutil
import stat
import tempfile
import time
import unittest

from scripts.monitor.collect import collect, log_digest, resolve_log

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "examples" / "smoke-run"


def snapshot(root):
    """Every path under root with its size and mtime, for before/after comparison."""
    seen = {}
    for base, dirs, names in os.walk(root):
        for name in dirs + names:
            path = Path(base) / name
            info = path.lstat()
            seen[str(path)] = (info.st_size, info.st_mtime_ns, info.st_mode)
    return seen


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def usage(call, role, tokens_in, tokens_out):
    return {"call": call, "role": role, "reported": [{"input_tokens": tokens_in, "output_tokens": tokens_out}]}


def make_task(task_id, title, advisory=False):
    return {"id": task_id, "title": title, "objective": "Do " + task_id + ".", "acceptance": ["works"],
            "owned_files": ["a.py", "b.py"], "depends_on": [],
            "checks": [{"id": "django", "argv": ["python", "manage.py", "test"], "timeout": 600},
                       {"id": "plans", "argv": ["python", "scripts/validate_plans.py"], "timeout": 60}],
            "worker_model": "claude-opus-5-5", "worker_reasoning": "high", "advisory_review": advisory,
            "impacts": {"schema": "NONE", "data": "NONE", "valuation": "NONE", "reporting": "NONE",
                        "marketplace_write": "NONE", "production": "NONE"}}


def make_manifest(tasks):
    return {"id": "ebay-01", "checkout": "/srv/checkout", "repository": "org/repo", "auth_home": "/home/x/.codex",
            "authority": "docs/workflows/x.md", "total_timeout": 28800, "max_calls": 24,
            "max_reported_tokens": 4000000, "ci_timeout": 3600, "tasks": tasks}


def make_packet(task, run_dir_checks_log_root):
    return {"id": task["id"], "checkout": "/srv/checkout", "base_sha": "0" * 40, "branch": "codex/ebay-01-1",
            "objective": task["objective"], "acceptance": task["acceptance"], "owned_files": task["owned_files"],
            "checks": task["checks"], "worker_model": task["worker_model"],
            "worker_reasoning": task["worker_reasoning"], "plan": "docs/plans/tasks/" + task["id"] + ".md",
            "max_calls": 6, "max_corrections": 2, "call_timeout": 2700, "total_timeout": 28800,
            "luna_triage": False, "advisory_review": task["advisory_review"]}


def build_batch_tree(root, *, phase, index, completed, runner_states, stop=False, extra_state=None, now=1_790_400_000.0):
    """A synthetic evidence directory in the shape batch.py and runner.py produce."""
    tasks = [make_task("packet-12", "Import orders promptly"), make_task("packet-13", "Apply stock updates"),
             make_task("packet-14", "Create listings", advisory=True)]
    manifest = make_manifest(tasks)
    write_json(root / "manifest.json", manifest)
    state = {"manifest_hash": "m" * 64, "runtime_hash": "r" * 64, "phase": phase, "index": index,
             "calls": 3 * len(completed), "tokens": 190211 * len(completed),
             "completed": [{"id": c, "url": "https://github.com/org/repo/pull/" + str(110 + i), "head": "h" * 40,
                            "merge": "m" * 40} for i, c in enumerate(completed)],
             "deadline": now + 5 * 3600}
    if index < len(tasks):
        state["packet"] = make_packet(tasks[index], root)
    state.update(extra_state or {})
    write_json(root / "state.json", state)
    if stop:
        (root / "STOP").write_text("")
    for i, task in enumerate(tasks):
        if task["id"] in runner_states or task["id"] in completed:
            item = root / task["id"]
            write_json(item / "prepare.json", {"base": "b" * 40, "branch": "codex/x"})
            run_dir = item / "runner"
            write_json(run_dir / "packet.json", make_packet(task, root))
            runner_state = runner_states.get(task["id"])
            if runner_state is None:
                runner_state = {"packet_hash": "p" * 64, "phase": "LOCAL_REVIEWED", "calls": 3, "corrections": 0,
                                "candidate": "c" * 64, "inflight": None, "deadline": now + 3600, "feedback": "",
                                "prompts": [{"call": 1, "role": "worker", "characters": 10},
                                            {"call": 2, "role": "reviewer", "characters": 10},
                                            {"call": 3, "role": "worker", "characters": 10}],
                                "usage": [usage(1, "worker", 100000, 211), usage(2, "reviewer", 60000, 0),
                                          usage(3, "worker", 30000, 0)],
                                "checks": {"candidate": "c" * 64, "results": [
                                    {"id": "django", "exit_code": 0, "log": str(run_dir / "check-0-django.log")}]},
                                "review": {"candidate": "c" * 64, "covered_files": ["a.py"], "acceptance": ["works"],
                                           "findings": []}}
            write_json(run_dir / "state.json", runner_state)
            for result in runner_state.get("checks", {}).get("results", []):
                Path(result["log"]).parent.mkdir(parents=True, exist_ok=True)
                Path(result["log"]).write_text("Ran 214 tests\nOK\n" if result["exit_code"] == 0 else "FAIL: test_x\nFAILED (failures=1)\n")
            for n in range(1, runner_state.get("calls", 0) + 1):
                (run_dir / f"prompt-{n}.txt").write_text("prompt")
                role = next((p["role"] for p in runner_state.get("prompts", []) if p["call"] == n), "worker")
                if role == "worker":
                    write_json(run_dir / f"result-{n}.json", {"patch": "diff --git a/a.py b/a.py\n", "summary": f"Worker call {n} did the thing."})
                else:
                    write_json(run_dir / f"result-{n}.json", runner_state.get("review") or {"findings": []})
            (run_dir / "candidate.diff").write_text(
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n-old\n+new\n+newer\n"
                "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n")
    return manifest, state


class SmokeRunTest(unittest.TestCase):
    def test_smoke_run_packet_is_local_reviewed_with_three_calls(self):
        doc = collect(SMOKE)
        self.assertIsNone(doc["empty"])
        self.assertEqual(doc["errors"], [])
        self.assertEqual(doc["batch"]["mode"], "runner")
        self.assertEqual(doc["batch"]["phase"], "LOCAL_REVIEWED")
        self.assertEqual(doc["batch"]["reason"], "Prescribed checks and complete-diff review passed")
        self.assertEqual(len(doc["packets"]), 1)
        packet = doc["packets"][0]
        self.assertEqual(packet["id"], "runner-smoke")
        self.assertEqual(packet["phase"], "LOCAL_REVIEWED")
        self.assertEqual(packet["status"], "reviewed")
        self.assertEqual(packet["calls"], 3)
        self.assertEqual(packet["corrections"], 1)
        runner = packet["runner"]
        self.assertEqual([c["exit_code"] for c in runner["checks"]], [0])
        self.assertEqual(runner["checks"][0]["id"], "clamp-acceptance")
        self.assertEqual(runner["checks"][0]["correction"], 1)
        self.assertTrue(runner["checks"][0]["log_available"])
        self.assertEqual(Path(runner["checks"][0]["log"]), (SMOKE / "run" / "check-1-clamp-acceptance.log").resolve())
        self.assertEqual(runner["checks"][0]["tail"], "Six clamp acceptance cases passed")
        self.assertEqual([c["role"] for c in runner["call_list"]], ["worker", "worker", "reviewer"])
        self.assertEqual([c["model"] for c in runner["call_list"]], ["gpt-5.6-sol", "gpt-5.6-sol", "claude-opus-5-5"])
        self.assertEqual(packet["tokens"], 27016 + 27070 + 27745)
        self.assertEqual(doc["batch"]["budgets"]["tokens"]["used"], 81831)
        self.assertFalse(doc["batch"]["budgets"]["tokens"]["incomplete"])
        self.assertEqual(doc["batch"]["budgets"]["packet"]["calls"], {"used": 3, "limit": 6})
        self.assertEqual(doc["batch"]["budgets"]["packet"]["corrections"], {"used": 1, "limit": 2})
        self.assertEqual(runner["review"]["blocking"], [])
        self.assertEqual(runner["claim"]["call"], 2)
        self.assertIn("inclusive clamping", runner["claim"]["summary"])
        self.assertEqual(runner["diff"]["files"], 1)
        self.assertEqual((runner["diff"]["added"], runner["diff"]["removed"]), (3, 1))
        self.assertEqual(runner["candidate_short"], "b4046656")
        self.assertEqual(doc["time_source"], "mtime")
        self.assertEqual(doc["batch"]["alive"]["status"], "terminal")

    def test_collect_never_writes_to_the_evidence_directory(self):
        before = snapshot(SMOKE)
        collect(SMOKE)
        self.assertEqual(snapshot(SMOKE), before)
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "smoke-run"
            shutil.copytree(SMOKE, copy)
            locked = []
            for base, dirs, names in os.walk(copy):
                for name in names:
                    os.chmod(Path(base) / name, stat.S_IRUSR)
                locked.append(Path(base))
            for path in sorted(locked, key=lambda p: len(str(p)), reverse=True):
                os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
            try:
                frozen = snapshot(copy)
                doc = collect(copy)
                self.assertEqual(doc["packets"][0]["calls"], 3)
                self.assertEqual(snapshot(copy), frozen)
            finally:
                for path in locked:
                    os.chmod(path, stat.S_IRWXU)
                for base, dirs, names in os.walk(copy):
                    for name in names:
                        os.chmod(Path(base) / name, stat.S_IRUSR | stat.S_IWUSR)

    def test_document_is_json_serialisable(self):
        json.dumps(collect(SMOKE))


class BatchTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ebay-01"
        self.root.mkdir()
        self.now = 1_790_400_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_stopped_tree_produces_the_stop_card_fields(self):
        live = {"packet_hash": "p" * 64, "phase": "CHECKS", "calls": 2, "corrections": 1,
                "candidate": "c" * 64, "inflight": None, "deadline": self.now + 3600,
                "feedback": "Prescribed checks failed: django",
                "prompts": [{"call": 1, "role": "worker", "characters": 1}, {"call": 2, "role": "worker", "characters": 1}],
                "usage": [usage(1, "worker", 1000, 10)],
                "checks": {"candidate": "c" * 64, "results": [
                    {"id": "django", "exit_code": 1, "log": str(self.root / "packet-13/runner/check-0-django.log")}]}}
        build_batch_tree(self.root, phase="STOPPED", index=1, completed=["packet-12"],
                         runner_states={"packet-13": live}, stop=True, now=self.now,
                         extra_state={"stopped_phase": "RUN", "reason": "Operator requested stop",
                                      "calls": 5, "tokens": 191221, "usage_incomplete": True})
        doc = collect(self.root, now=self.now)
        batch = doc["batch"]
        self.assertEqual(batch["phase"], "STOPPED")
        self.assertEqual(batch["stopped_phase"], "RUN")
        self.assertEqual(batch["reason"], "Operator requested stop")
        self.assertTrue(batch["stop_requested"])
        self.assertEqual(batch["evidence_dir"], str(self.root.resolve()))
        self.assertEqual(batch["alive"]["status"], "terminal")
        self.assertEqual(batch["needs_you"]["kind"], "inspect")
        self.assertIn("Operator requested stop", batch["needs_you"]["text"])
        self.assertIsNone(batch["budgets"]["tokens"]["used"])
        self.assertTrue(batch["budgets"]["tokens"]["incomplete"])
        self.assertEqual(batch["budgets"]["calls"]["used"], 5)
        statuses = [(p["id"], p["status"]) for p in doc["packets"]]
        self.assertEqual(statuses, [("packet-12", "merged"), ("packet-13", "stopped"), ("packet-14", "queued")])
        stopped = doc["packets"][1]
        self.assertEqual(stopped["batch_phase"], "RUN")
        self.assertEqual(stopped["phase"], "CHECKS")
        self.assertIsNone(stopped["tokens"])
        django = [c for c in stopped["runner"]["checks"] if c["id"] == "django" and c["exit_code"] is not None]
        self.assertEqual([(c["exit_code"], c["correction"], c["current"]) for c in django], [(1, 0, False)])
        self.assertEqual([c for c in stopped["runner"]["checks"] if c.get("pending")], [])  # nothing runs after a stop
        self.assertEqual(stopped["runner"]["feedback"], "Prescribed checks failed: django")
        self.assertIsNotNone(stopped["finished"])
        self.assertEqual(doc["errors"], [])

    def test_running_tree_marks_merged_running_and_queued(self):
        live = {"packet_hash": "p" * 64, "phase": "CHECKS", "calls": 3, "corrections": 1,
                "candidate": "c" * 64, "inflight": {"label": "check-1-django", "pid": 4242}, "deadline": self.now + 3600,
                "feedback": "", "prompts": [{"call": 1, "role": "worker", "characters": 1},
                                            {"call": 2, "role": "reviewer", "characters": 1},
                                            {"call": 3, "role": "worker", "characters": 1}],
                "usage": [usage(1, "worker", 70000, 1000), usage(2, "reviewer", 40000, 0), usage(3, "worker", 20000, 0)],
                "checks": {"candidate": "c" * 64, "results": [
                    {"id": "django", "exit_code": 1, "log": str(self.root / "packet-13/runner/check-0-django.log")},
                    {"id": "plans", "exit_code": 0, "log": str(self.root / "packet-13/runner/check-0-plans.log")}]},
                "review": {"candidate": "c" * 64, "covered_files": ["a.py"], "acceptance": ["works"],
                           "findings": [{"file": "a.py", "severity": "blocking", "summary": "Not atomic", "failure_scenario": "loss"},
                                        {"file": "a.py", "severity": "nit", "summary": "Rename", "failure_scenario": "none"}]}}
        build_batch_tree(self.root, phase="RUN", index=1, completed=["packet-12"], runner_states={"packet-13": live}, now=self.now)
        (self.root / "packet-13/runner/check-1-django.log").write_text("running...\n")
        doc = collect(self.root, now=self.now)
        batch = doc["batch"]
        self.assertEqual(batch["id"], "ebay-01")
        self.assertEqual(batch["repository"], "org/repo")
        self.assertEqual(batch["phase"], "RUN")
        self.assertEqual((batch["index"], batch["total"]), (1, 3))
        self.assertEqual(batch["unit"], "storehouse-ebay-01")
        self.assertFalse(batch["stop_requested"])
        self.assertEqual(batch["budgets"]["calls"], {"used": 6, "limit": 24})
        self.assertEqual(batch["budgets"]["tokens"]["used"], 190211 + 131000)
        self.assertEqual(batch["budgets"]["clock"]["limit"], 28800)
        self.assertAlmostEqual(batch["budgets"]["clock"]["used"], 3 * 3600, delta=1)
        self.assertEqual(batch["budgets"]["packet"], {"calls": {"used": 3, "limit": 6}, "corrections": {"used": 1, "limit": 2}, "id": "packet-13"})
        self.assertEqual(batch["alive"]["status"], "running")
        self.assertIsNone(batch["needs_you"], "a running batch needs nothing from the operator")
        merged, running, queued = doc["packets"]
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["pr"]["number"], 110)
        self.assertEqual(merged["pr"]["url"], "https://github.com/org/repo/pull/110")
        self.assertEqual(merged["calls"], 3)
        self.assertEqual(merged["tokens"], 190211)
        self.assertIsNotNone(merged["started"])
        self.assertIsNotNone(merged["finished"])
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["phase"], "CHECKS")
        self.assertEqual(running["batch_phase"], "RUN")
        self.assertEqual(running["title"], "Apply stock updates")
        self.assertIsNone(running["finished"])
        runner = running["runner"]
        self.assertEqual(runner["inflight"]["label"], "check-1-django")
        running_checks = [c for c in runner["checks"] if c.get("running")]
        self.assertEqual([c["id"] for c in running_checks], ["django"])
        pending = [c for c in runner["checks"] if c.get("pending")]
        self.assertEqual([c["id"] for c in pending], ["plans"])
        previous = [c for c in runner["checks"] if c["exit_code"] is not None]
        self.assertEqual({(c["id"], c["exit_code"], c["correction"], c["current"]) for c in previous},
                         {("django", 1, 0, False), ("plans", 0, 0, False)})
        self.assertIn("FAIL: test_x", next(c for c in previous if c["id"] == "django")["tail"])
        self.assertEqual(runner["checks"][0]["id"], "django")  # this round first, running before pending
        self.assertEqual(len(runner["review"]["blocking"]), 1)
        self.assertEqual(len(runner["review"]["notes"]), 1)
        self.assertEqual(runner["call_list"][1]["blocking"], 1)
        self.assertEqual(runner["call_list"][1]["model"], "claude-opus-5-5")
        self.assertEqual(runner["claim"]["call"], 3)
        self.assertEqual(runner["diff"], {"files": 2, "added": 3, "removed": 2, "bytes": runner["diff"]["bytes"],
                                          "path": str((self.root / "packet-13/runner/candidate.diff").resolve()),
                                          "at": runner["diff"]["at"]})
        self.assertEqual(queued["status"], "queued")
        self.assertTrue(queued["advisory_review"])
        self.assertIsNone(queued["runner"])
        self.assertIsNone(queued["calls"])

    def test_resumed_batch_borrows_packet_evidence_from_the_sibling_run(self):
        first = self.root / "run"
        first.mkdir()
        build_batch_tree(first, phase="STOPPED", index=1, completed=["packet-12"], runner_states={}, now=self.now,
                         extra_state={"stopped_phase": "RUN", "reason": "x"})
        cont = self.root / "run-continuation-01"
        cont.mkdir()
        build_batch_tree(cont, phase="STOPPED", index=1, completed=["packet-12"], runner_states={"packet-13": None}, now=self.now,
                         extra_state={"stopped_phase": "RUN", "reason": "y"})
        shutil.rmtree(cont / "packet-12")
        doc = collect(cont, now=self.now)
        merged = doc["packets"][0]
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["calls"], 3)
        self.assertEqual(merged["evidence_from"], str(first.resolve()))
        self.assertIsNone(doc["packets"][1]["evidence_from"])

    def test_batch_phase_after_run_shows_pr_and_stage(self):
        build_batch_tree(self.root, phase="CI", index=1, completed=["packet-12"], runner_states={"packet-13": None},
                         now=self.now, extra_state={"pr": 112, "url": "https://github.com/org/repo/pull/112",
                                                    "calls": 6, "tokens": 380422})
        doc = collect(self.root, now=self.now)
        running = doc["packets"][1]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["batch_phase"], "CI")
        self.assertEqual(running["phase"], "LOCAL_REVIEWED")
        self.assertEqual(running["pr"], {"number": 112, "url": "https://github.com/org/repo/pull/112"})
        self.assertEqual(doc["batch"]["budgets"]["calls"]["used"], 6)
        self.assertEqual(doc["batch"]["budgets"]["tokens"]["used"], 380422)
        self.assertEqual(doc["batch"]["alive"]["expected_s"], 3600)

    def test_unknown_usage_is_none_never_zero(self):
        live = {"packet_hash": "p" * 64, "phase": "REVIEW", "calls": 2, "corrections": 0, "candidate": "c" * 64,
                "inflight": None, "deadline": self.now + 3600, "feedback": "",
                "prompts": [{"call": 1, "role": "worker", "characters": 1}, {"call": 2, "role": "reviewer", "characters": 1}],
                "usage": [{"call": 1, "role": "worker", "reported": []}, {"call": 2, "role": "reviewer", "reported": []}]}
        build_batch_tree(self.root, phase="RUN", index=0, completed=[], runner_states={"packet-12": live}, now=self.now)
        doc = collect(self.root, now=self.now)
        packet = doc["packets"][0]
        self.assertIsNone(packet["tokens"])
        self.assertTrue(packet["runner"]["tokens_incomplete"])
        self.assertEqual([c["tokens"] for c in packet["runner"]["call_list"]], [None, None])
        tokens = doc["batch"]["budgets"]["tokens"]
        self.assertIsNone(tokens["used"])
        self.assertTrue(tokens["incomplete"])
        self.assertIn("without reported usage", tokens["reason"])

    def test_in_flight_and_never_dispatched_calls_do_not_poison_token_accounting(self):
        live = {"packet_hash": "p" * 64, "phase": "IMPLEMENT", "calls": 2, "corrections": 1, "candidate": "c" * 64,
                "inflight": {"label": "model-2", "pid": 7}, "deadline": self.now + 3600,
                "feedback": 'Prescribed checks failed: [{"exit_code":1,"id":"django","log":"/x/check-0-django.log"},'
                            '{"exit_code":0,"id":"plans","log":"/x/check-0-plans.log"}] Failure excerpts from the supervisor',
                "prompts": [{"call": 1, "role": "worker", "characters": 1}, {"call": 2, "role": "worker", "characters": 1}],
                "usage": [usage(1, "worker", 1000, 10)]}
        build_batch_tree(self.root, phase="RUN", index=0, completed=[], runner_states={"packet-12": live}, now=self.now)
        (self.root / "packet-12/runner/result-2.json").unlink()
        doc = collect(self.root, now=self.now)
        runner = doc["packets"][0]["runner"]
        self.assertTrue(runner["call_list"][1]["inflight"])
        self.assertFalse(runner["call_list"][1]["result"])
        self.assertEqual(runner["tokens"], 1010, "an in-flight call has no usage yet, by definition")
        self.assertFalse(runner["tokens_incomplete"])
        self.assertEqual(doc["batch"]["budgets"]["tokens"]["used"], 1010)
        self.assertEqual(runner["feedback_summary"], "failed checks: django (log excerpts were sent to the worker)")
        # The same tree stopped by the budget check before call 2 was dispatched.
        stopped = dict(live, phase="STOPPED", inflight=None, reason="Global model budget exhausted")
        write_json(self.root / "packet-12/runner/state.json", stopped)
        doc = collect(self.root, now=self.now)
        runner = doc["packets"][0]["runner"]
        self.assertTrue(runner["call_list"][1]["never_dispatched"])
        self.assertEqual(runner["tokens"], 1010)
        self.assertFalse(runner["tokens_incomplete"])

    def test_stale_checkpoint_is_flagged(self):
        written = time.time()
        build_batch_tree(self.root, phase="PR", index=0, completed=[], runner_states={"packet-12": None}, now=written)
        fresh = collect(self.root, now=os.stat(self.root / "state.json").st_mtime + 60)
        self.assertEqual(fresh["batch"]["alive"]["status"], "running")
        stale = collect(self.root, now=os.stat(self.root / "state.json").st_mtime + 4000)
        self.assertEqual(stale["batch"]["alive"]["status"], "stale")
        self.assertAlmostEqual(stale["batch"]["alive"]["state_age_s"], 4000, delta=1)
        self.assertFalse(stale["batch"]["abandoned"])
        self.assertIsNone(stale["batch"]["budgets"]["clock"]["ended"])

    def test_deadline_passed_without_a_final_checkpoint_is_abandoned(self):
        """A batch killed mid-run never writes STOPPED; its clocks must stop at the last write, not run to now."""
        written = time.time() - 9 * 3600  # started 9 h ago with an 8 h budget, so the deadline has passed
        build_batch_tree(self.root, phase="RUN", index=0, completed=[], runner_states={"packet-12": {
            "packet_hash": "p" * 64, "phase": "IMPLEMENT", "calls": 1, "corrections": 0, "candidate": "c" * 64,
            "inflight": None, "deadline": written + 1800, "feedback": "", "prompts": [{"call": 1, "role": "worker", "characters": 1}],
            "usage": []}}, now=written)
        for path in self.root.rglob("*"):
            os.utime(path, (written + 60, written + 60))
        doc = collect(self.root)
        batch = doc["batch"]
        self.assertTrue(batch["abandoned"])
        self.assertEqual(batch["phase"], "RUN")
        self.assertEqual(batch["alive"]["status"], "abandoned")
        clock = batch["budgets"]["clock"]
        self.assertAlmostEqual(clock["ended"], written + 60, delta=1)
        self.assertAlmostEqual(clock["used"], clock["ended"] - clock["started"], delta=1)
        self.assertLess(clock["used"], clock["limit"], "the clock stopped at the last write, not at now")
        packet = doc["packets"][0]
        self.assertEqual(packet["status"], "running")
        self.assertAlmostEqual(packet["finished"], written + 60, delta=1)
        self.assertLess(packet["elapsed_s"], 120)

    def test_foreign_log_path_falls_back_to_runner_directory_or_nothing(self):
        run_dir = self.root / "run"
        run_dir.mkdir()
        (run_dir / "check-0-x.log").write_text("ok")
        path, available = resolve_log("/elsewhere/machine/run/check-0-x.log", run_dir, self.root.resolve())
        self.assertTrue(available)
        self.assertEqual(Path(path), (run_dir / "check-0-x.log").resolve())
        path, available = resolve_log("/elsewhere/machine/run/check-0-y.log", run_dir, self.root.resolve())
        self.assertFalse(available)
        self.assertEqual(path, "/elsewhere/machine/run/check-0-y.log")
        outside = Path(self.tmp.name) / "outside.log"
        outside.write_text("secret")
        path, available = resolve_log(str(outside), run_dir, self.root.resolve())
        self.assertFalse(available)


UNITTEST_LOG = """Creating test database for alias 'default'...
System check identified no issues (0 silenced).
======================================================================
ERROR: test_gate_off (operations.test_website_catalogue.GateTests.test_gate_off)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "x.py", line 1, in inner
    raise EbayEnvironmentError("Unsafe environment combination")
operations.ebay_environment.EbayEnvironmentError: Unsafe environment combination

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "y.py", line 2, in test_gate_off
operations.ebay.EbayConfigurationError: Unsafe environment combination

======================================================================
ERROR: test_gate_on (operations.test_website_catalogue.GateTests.test_gate_on)
----------------------------------------------------------------------
Traceback (most recent call last):
operations.ebay.EbayConfigurationError: Unsafe environment combination

======================================================================
FAIL: test_panels (operations.test_selling_workspace.SellingWorkspaceTests.test_panels)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "z.py", line 3, in test_panels
AssertionError: False is not true : Couldn't find 'Not connected' in the following response
<html>lots</html>

----------------------------------------------------------------------
Ran 134 tests in 97.358s

FAILED (failures=1, errors=2)
Destroying test database for alias 'default'...
"""


class LogDigestTest(unittest.TestCase):
    def test_unittest_log_groups_identical_causes(self):
        digest = log_digest(UNITTEST_LOG)
        self.assertEqual((digest["failures"], digest["errors"]), (1, 2))
        self.assertEqual(digest["summary"], "Ran 134 tests in 97.358s · FAILED (failures=1, errors=2)")
        self.assertEqual([f["test"] for f in digest["findings"]], ["test_gate_off", "test_gate_on", "test_panels"])
        self.assertEqual(digest["findings"][0]["message"], "operations.ebay.EbayConfigurationError: Unsafe environment combination")
        self.assertEqual(digest["findings"][2]["message"], "AssertionError: False is not true : Couldn't find 'Not connected' in the following response")
        self.assertEqual([(g["kind"], g["tests"]) for g in digest["groups"]],
                         [("ERROR", ["test_gate_off", "test_gate_on"]), ("FAIL", ["test_panels"])])

    def test_passing_and_pytest_and_plain_logs(self):
        self.assertEqual(log_digest("Ran 3 tests in 0.1s\n\nOK\n")["summary"], "Ran 3 tests in 0.1s · OK")
        digest = log_digest("FAILED tests/test_a.py::test_x - AssertionError: nope\n=== 1 failed, 4 passed in 2.3s ===\n")
        self.assertEqual(digest["findings"][0]["test"], "test_x")
        self.assertEqual(digest["summary"], "1 failed, 4 passed in 2.3s")
        self.assertIsNone(log_digest("Six clamp acceptance cases passed\n"))
        self.assertIsNone(log_digest(""))

    def test_smoke_check_rows_carry_a_digest_field(self):
        doc = collect(SMOKE)
        self.assertIn("digest", doc["packets"][0]["runner"]["checks"][0])


def build_phase_run(root):
    """A run_phases.sh evidence directory: coordinator.log, phase-NN.json and .err, the script."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "run_phases.sh").write_text("#!/usr/bin/env bash\nREPO=/mnt/c/proof\nPROMPTS=$REPO/docs/prompts\nLOG=" + str(root) + "\nfor n in 01 02 03; do\n  echo\ndone\n")
    (root / "coordinator.log").write_text(
        "2026-09-26T14:48:36+01:00 START phase 01 (/mnt/c/proof/docs/prompts/phase-01-astro-scaffold.md)\n"
        "2026-09-26T15:02:40+01:00 END phase 01 exit=0 staging-status=done :: success | built it; PR https://github.com/o/r/pull/3 merged\n"
        "2026-09-26T15:02:43+01:00 START phase 02 (/mnt/c/proof/docs/prompts/phase-02-catalogue-data.md)\n"
        "2026-09-26T15:10:36+01:00 END phase 02 exit=0 staging-status=todo :: success | forgot the status row\n"
        "2026-09-26T15:10:36+01:00 STOP: phase 02 is not done on staging\n")
    def session(cost, out):
        return json.dumps({"subtype": "success", "is_error": False, "result": "Did **things**.\n- one\n- two", "total_cost_usd": cost,
                           "duration_api_ms": 551655, "num_turns": 42, "session_id": "1f375c5f-2c3c",
                           "usage": {"input_tokens": 174, "cache_creation_input_tokens": 104922, "cache_read_input_tokens": 7717159,
                                     "output_tokens": out, "output_tokens_details": {"thinking_tokens": 12857}},
                           "modelUsage": {"claude-sonnet-5": {}}})
    (root / "phase-01.json").write_text("noise line\n" + session(2.3, 34315) + "\n")
    (root / "phase-01.err").write_text("")
    (root / "phase-02.json").write_text(session(1.2, 100) + "\n")
    (root / "phase-02.err").write_text("warning: something\n")


class PhaseRunTest(unittest.TestCase):
    def setUp(self):
        import scripts.monitor.collect as collect_module
        self.module = collect_module
        collect_module._HOLDERS_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proof-hardware-phases"
        build_phase_run(self.root)
        # A live runner holds its lock file open; this test process stands in for it.
        self.lock = open(self.root / "lock", "a")

    def tearDown(self):
        self.lock.close()
        self.tmp.cleanup()

    def test_log_says_running_but_no_process_means_it_died(self):
        (self.root / "coordinator.log").write_text("2026-09-26T14:48:36+01:00 START phase 01 (/x/phase-01-a.md)\n")
        doc = collect(self.root)
        self.assertEqual(doc["batch"]["phase"], "RUN")
        self.assertTrue(doc["batch"]["proof"]["alive"])
        self.assertFalse(doc["batch"]["proof"]["died"])
        self.lock.close()
        self.module._HOLDERS_CACHE.clear()
        doc = collect(self.root)
        self.assertEqual(doc["batch"]["phase"], "STOPPED")
        self.assertEqual(doc["batch"]["tone"], "broken")
        self.assertTrue(doc["batch"]["proof"]["died"])
        self.assertEqual(doc["packets"][0]["tone"], "broken")
        self.assertEqual(doc["batch"]["needs_you"]["kind"], "inspect")
        self.assertIn("died", doc["batch"]["needs_you"]["text"])

    def test_every_stretch_of_work_is_a_timeline_segment(self):
        log = self.root / "coordinator.log"
        log.write_text(log.read_text()
                       + "2026-09-26T15:11:00+01:00 REVIEW phase 02 round 0: PR #4, 900 bytes\n"
                       + "2026-09-26T15:13:00+01:00 REVIEW phase 02 round 0: verdict BLOCKING (advisory CLEAN), posted to PR #4\n"
                       + "2026-09-26T15:13:02+01:00 CORRECT phase 02 round 1 on phase-02-x (PR #4) model=m\n"
                       + "2026-09-26T15:16:00+01:00 CORRECTED phase 02 round 1 exit=0\n"
                       + "2026-09-26T15:16:01+01:00 REVIEW phase 02 round 1: PR #4, 950 bytes\n")
        segs = collect(self.root, now=1_790_000_000.0)["packets"][1]["phase_run"]["segments"]
        self.assertEqual([s["kind"] for s in segs], ["build", "review", "fix", "review"])
        self.assertTrue(all(s["end"] is not None for s in segs[:3]))
        self.assertIsNone(segs[3]["end"], "the review in progress runs to now")
        self.lock.close()
        self.module._HOLDERS_CACHE.clear()
        segs = collect(self.root, now=1_790_000_000.0)["packets"][1]["phase_run"]["segments"]
        self.assertIsNotNone(segs[3]["end"], "a dead runner's open segment ends when it died")

    def test_the_arbiter_step(self):
        log = self.root / "coordinator.log"
        base = log.read_text()
        log.write_text(base + "2026-09-26T15:20:00+01:00 ARBITER phase 02 round 4 on phase-02-x (PR #4) model=claude-fable-5-1 effort=xhigh\n")
        doc = collect(self.root, now=1_790_000_000.0)
        two = doc["packets"][1]
        self.assertEqual(doc["batch"]["phase"], "RUN", "an arbiter at work is work in progress")
        self.assertEqual(two["tone"], "working")
        self.assertEqual(two["phase_run"]["segments"][-1]["kind"], "arbiter")
        self.assertIsNone(two["phase_run"]["arbiter"]["finished"])
        log.write_text(log.read_text()
                       + "2026-09-26T15:40:00+01:00 ARBITER phase 02 round 4: handed to the owner (exit 0): The reviewers disagree on the data model.\n"
                       + "2026-09-26T15:40:01+01:00 STOP: phase 02 still blocking after 3 corrections; the arbiter handed it to the owner\n")
        doc = collect(self.root, now=1_790_000_000.0)
        arb = doc["packets"][1]["phase_run"]["arbiter"]
        self.assertEqual((arb["outcome"], arb["summary"]), ("handed over", "The reviewers disagree on the data model."))
        self.assertEqual(doc["batch"]["needs_you"]["kind"], "decide")
        self.assertIn("chose not to force a fix", doc["batch"]["needs_you"]["text"])
        self.assertIn("disagree on the data model", doc["batch"]["needs_you"]["text"])

    def test_auto_merge_and_the_last_phase(self):
        log = self.root / "coordinator.log"
        log.write_text(log.read_text()
                       + "2026-09-26T15:20:00+01:00 MERGED phase 02 PR #4 at abc1234def\n"
                       + "2026-09-26T15:20:05+01:00 START phase 03 (/mnt/c/proof/docs/prompts/phase-03-landing-page.md)\n"
                       + "2026-09-26T15:40:00+01:00 END phase 03 exit=0 staging-status=todo :: success | PR https://github.com/o/r/pull/5\n"
                       + "2026-09-26T15:50:00+01:00 STOP: phase 03, the last of the task, reviewed clean; merge its PR to finish\n")
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["packets"][1]["status"], "merged")
        self.assertEqual(doc["packets"][1]["phase_run"]["auto_merged"]["pr"], 4)
        self.assertEqual(doc["batch"]["tone"], "yours")
        self.assertIn("last of this task", doc["batch"]["needs_you"]["text"])
        log.write_text(log.read_text().replace("STOP: phase 03, the last of the task, reviewed clean; merge its PR to finish",
                                               "STOP: phase 03 reviewed clean but was not merged automatically: CI failed on abc1234: tests; merge it, then rerun"))
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["tone"], "yours")
        self.assertIn("did not merge itself: CI failed on abc1234: tests.", doc["batch"]["needs_you"]["text"])

    def test_input_receipts_attach_to_their_session(self):
        log = self.root / "coordinator.log"
        log.write_text(log.read_text()
                       + "2026-09-26T15:12:00+01:00 CORRECT phase 02 round 1 on phase-02-x (PR #4) model=m\n"
                       + "2026-09-26T15:12:00+01:00 INPUT fix phase 02 round 1 (phase-02-correct-r1-attempt-1-input.md): instructions 1.2 KB · review findings 3.8 KB [P0-1,P1-2] BLOCKING · GPT's blocking review left out (advisory only)\n")
        fix = collect(self.root, now=1_790_000_000.0)["packets"][1]["phase_run"]["corrections"][-1]
        self.assertIn("[P0-1,P1-2] BLOCKING", fix["input"]["receipt"])
        self.assertTrue(fix["input"]["file"].endswith("phase-02-correct-r1-attempt-1-input.md"))

    def test_tests_run_before_review(self):
        log = self.root / "coordinator.log"
        log.write_text(log.read_text() + "2026-09-26T15:12:00+01:00 CI phase 02 round 0: waiting for checks on abc1234\n")
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["phase"], "RUN", "waiting for CI is work in progress")
        self.assertEqual(doc["packets"][1]["tone"], "working")
        log.write_text(log.read_text() + "2026-09-26T15:14:00+01:00 CI phase 02 round 0: failed on abc1234: tests; skipping the review, the log goes to a fix\n")
        seg = collect(self.root, now=1_790_000_000.0)["packets"][1]["phase_run"]["segments"][-1]
        self.assertEqual((seg["kind"], seg["outcome"]), ("tests", "failed"))

    def test_an_open_correction_is_work_in_progress(self):
        log = self.root / "coordinator.log"
        log.write_text(log.read_text() + "2026-09-26T15:12:00+01:00 CORRECT phase 02 round 1 on phase-02-x (PR #4) model=m\n")
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["phase"], "RUN")
        self.assertEqual(doc["packets"][1]["tone"], "working")

    def test_stopped_phase_run(self):
        doc = collect(self.root, now=1_790_000_000.0)
        b = doc["batch"]
        self.assertEqual(b["mode"], "phases")
        self.assertEqual(b["phase"], "STOPPED")
        self.assertEqual(b["reason"], "phase 02 is not done on staging")
        self.assertEqual((b["index"], b["total"]), (1, 3))
        self.assertEqual(b["repository"], "/mnt/c/proof")
        self.assertAlmostEqual(b["budgets"]["cost_usd"], 3.5)
        self.assertEqual(b["budgets"]["tokens"]["used"], 2 * (174 + 104922 + 7717159) + 34315 + 100)
        self.assertEqual(b["budgets"]["clock"]["limit"], None)
        self.assertEqual(b["alive"]["status"], "terminal")
        self.assertEqual(doc["errors"], [])
        one, two, three = doc["packets"]
        self.assertEqual((one["id"], one["title"], one["status"]), ("phase-01", "astro scaffold", "merged"))
        self.assertEqual(one["pr"], {"number": 3, "url": "https://github.com/o/r/pull/3"})
        self.assertEqual(one["phase_run"]["exit_code"], 0)
        self.assertEqual(one["phase_run"]["staging_status"], "done")
        self.assertAlmostEqual(one["elapsed_s"], 14 * 60 + 4)
        self.assertEqual(one["phase_run"]["result"]["model"], "claude-sonnet-5")
        self.assertEqual(one["phase_run"]["result"]["tokens"]["cached"], 7717159)
        self.assertEqual(one["phase_run"]["result"]["result"], "Did **things**.\n- one\n- two")
        self.assertEqual(one["tokens"], 174 + 104922 + 7717159 + 34315)
        self.assertEqual(two["status"], "stopped")
        self.assertEqual(two["phase_run"]["staging_status"], "todo")
        self.assertEqual(two["phase_run"]["stderr_bytes"], len("warning: something\n"))
        self.assertEqual(three["status"], "queued")
        self.assertIsNone(three["started"])
        json.dumps(doc)

    def test_rerun_after_stop_completes(self):
        with open(self.root / "coordinator.log", "a") as log:
            log.write("2026-09-26T15:11:20+01:00 phase 01 already done\n"
                      "2026-09-26T15:11:21+01:00 phase 02 already done\n"
                      "2026-09-26T15:11:24+01:00 START phase 03 (/mnt/c/proof/docs/prompts/phase-03-landing-page.md)\n"
                      "2026-09-26T15:21:41+01:00 END phase 03 exit=0 staging-status=done :: success | done\n"
                      "2026-09-26T15:21:41+01:00 ALL phases 01-03 done\n")
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["phase"], "COMPLETE")
        self.assertIsNone(doc["batch"]["reason"])
        self.assertEqual([p["status"] for p in doc["packets"]], ["merged", "merged", "merged"])
        self.assertTrue(doc["packets"][1]["phase_run"]["already_done"])
        self.assertEqual(doc["batch"]["index"], 3)

    def test_new_runner_shape_reopens_after_all_done_and_records_reviews(self):
        (self.root / "run_phases.sh").write_text('#!/usr/bin/env bash\nREPO=/mnt/c/proof\nPROMPTS=$REPO/docs/prompts\nPHASES="${PHASES:-01 02 03 13}"\n')
        with open(self.root / "coordinator.log", "a") as log:
            log.write("2026-09-26T15:11:20+01:00 phase 02 already done\n"
                      "2026-09-26T15:11:24+01:00 START phase 03 (/mnt/c/proof/docs/prompts/phase-03-landing-page.md)\n"
                      "2026-09-26T15:21:41+01:00 END phase 03 exit=0 staging-status=done :: success | done\n"
                      "2026-09-26T15:21:41+01:00 ALL phases 01-03 done\n"
                      "2026-09-26T20:23:31+01:00 START phase 13 (/mnt/c/proof/docs/prompts/phase-13-single-module-catalogue.md) model=claude-opus-5-5\n")
        (self.root / "phase-13.json").write_text("")
        (self.root / "phase-13.err").write_text("")
        doc = collect(self.root, now=1_790_000_000.0)
        b = doc["batch"]
        self.assertEqual(b["phase"], "RUN")
        self.assertEqual((b["index"], b["total"]), (3, 4))
        ids = [p["id"] for p in doc["packets"]]
        self.assertEqual(ids, ["phase-01", "phase-02", "phase-03", "phase-13"])
        thirteen = doc["packets"][3]
        self.assertEqual(thirteen["status"], "running")
        self.assertEqual(thirteen["title"], "single module catalogue")
        self.assertEqual(thirteen["worker"]["model"], "claude-opus-5-5")
        self.assertEqual(b["alive"]["expected_s"], 10800)
        # The session ends, the review runs, the loop stops for the owner to merge.
        with open(self.root / "coordinator.log", "a") as log:
            log.write("2026-09-26T21:40:00+01:00 END phase 13 exit=0 staging-status=todo :: success | opened PR https://github.com/o/r/pull/14\n"
                      "2026-09-26T21:40:05+01:00 REVIEW phase 13: PR #14, 512000 bytes\n")
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["phase"], "RUN", "the review is still running")
        self.assertEqual(doc["batch"]["alive"]["reviewing"], ["13"])
        (self.root / "phase-13-review-claude.md").write_text("## Findings\n\n- **BLOCKING** src/x.astro:12 leaks a private field.\n")
        (self.root / "phase-13-review-gpt.md").write_text("No blocking findings.\n")
        with open(self.root / "coordinator.log", "a") as log:
            log.write("2026-09-26T21:52:00+01:00 REVIEW phase 13: posted to PR #14\n"
                      "2026-09-26T21:52:00+01:00 STOP: phase 13 is not done on staging (merge its PR after reading the review, then rerun)\n")
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["phase"], "STOPPED")
        self.assertIn("merge its PR", doc["batch"]["reason"])
        need = doc["batch"]["needs_you"]
        self.assertEqual(need["kind"], "merge")
        self.assertEqual(need["pr"]["number"], 14)
        self.assertIn("waits for your merge", need["text"])
        r = doc["packets"][3]["phase_run"]
        self.assertEqual(r["status"], "stopped")
        self.assertEqual(r["review"]["pr"], 14)
        self.assertTrue(r["review"]["posted"])
        # A review-only rerun with rounds, as the operator's later script writes it.
        with open(self.root / "coordinator.log", "a") as log:
            log.write("2026-09-26T22:05:46+01:00 REVIEW-ONLY phase 13\n"
                      "2026-09-26T22:05:47+01:00 REVIEW phase 13 round 1: PR #14, 181229 bytes\n")
        doc = collect(self.root, now=1_790_000_000.0)
        rv = doc["packets"][3]["phase_run"]["review"]
        self.assertEqual((rv["pr"], rv["round"], rv["posted"], rv.get("review_only")), (14, 1, False, True))
        self.assertEqual(doc["batch"]["phase"], "RUN", "a review round is in progress")
        self.assertEqual(doc["batch"]["alive"]["reviewing"], ["13"])
        self.assertIn("BLOCKING", r["review"]["claude"]["text"])
        self.assertEqual(r["review"]["gpt"]["text"], "No blocking findings.")
        self.assertEqual(doc["packets"][3]["pr"], {"number": 14, "url": "https://github.com/o/r/pull/14"})
        self.assertIsNone(doc["batch"]["needs_you"], "a review round in progress needs nothing yet")

    def test_events_corrections_and_stop_file(self):
        with open(self.root / "coordinator.log", "a") as log:
            log.write("2026-09-26T22:00:00+01:00 REVIEW-ONLY phase 02\n"
                      "2026-09-26T22:00:01+01:00 REVIEW phase 02 round 0: PR #4, 1000 bytes\n"
                      "2026-09-26T22:05:00+01:00 REVIEW phase 02 round 0: verdict BLOCKING (advisory CLEAN), posted to PR #4\n"
                      "2026-09-26T22:05:01+01:00 CORRECT phase 02 round 1 on phase-02-catalogue-data (PR #4) model=claude-sonnet-5\n"
                      "2026-09-26T22:20:00+01:00 CORRECTED phase 02 round 1 exit=0\n"
                      "2026-09-26T22:20:01+01:00 REVIEW phase 02 round 1: PR #4, 1100 bytes\n"
                      "2026-09-26T22:25:00+01:00 REVIEW phase 02 round 1: verdict CLEAN (advisory BLOCKING), posted to PR #4\n"
                      "2026-09-26T22:25:00+01:00 STOP: phase 02 reviewed clean; merge its PR, then rerun\n")
        (self.root / "events.jsonl").write_text(
            json.dumps({"at": 1.0, "event": "review_only", "phase": "02"}) + "\n"
            + json.dumps({"at": 2.0, "event": "verdict", "phase": "02", "round": 1, "pr": 4, "verdict": "CLEAN",
                          "advisory": "BLOCKING", "gating": "CLEAN", "posted": "yes", "attempt": 2}) + "\n"
            + "not json\n")
        (self.root / "STOP").write_text("")
        doc = collect(self.root, now=1_790_000_000.0)
        b = doc["batch"]
        self.assertEqual(b["phase"], "STOPPED")
        self.assertTrue(b["stop_requested"])
        self.assertTrue(b["events"])
        two = doc["packets"][1]
        self.assertEqual(two["corrections"], 1)
        r = two["phase_run"]
        self.assertEqual(r["corrections"][0]["exit_code"], 0)
        self.assertEqual(r["corrections"][0]["model"], "claude-sonnet-5")
        self.assertAlmostEqual(r["corrections"][0]["finished"] - r["corrections"][0]["started"], 14 * 60 + 59)
        self.assertEqual((r["review"]["round"], r["review"]["verdict"], r["review"]["advisory"], r["review"]["gating"]),
                         (1, "CLEAN", "BLOCKING", "CLEAN"))
        self.assertEqual(r["review"]["attempt"], 2)
        self.assertTrue(r["review"]["posted"])
        self.assertEqual(doc["errors"], [], "a bad event line is skipped, not fatal")
        self.assertIsNone(doc["packets"][2]["corrections"], "queued phases show no correction count")

    def test_stop_for_a_merge_is_waiting_not_failed(self):
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["tone"], "broken", "an unexplained stop is a failure")
        self.assertEqual(doc["packets"][1]["tone"], "broken")
        log = self.root / "coordinator.log"
        log.write_text(log.read_text() + "2026-09-26T15:20:00+01:00 STOP: phase 02 reviewed clean but its PR is open; merge it, then rerun\n")
        doc = collect(self.root, now=1_790_000_000.0)
        self.assertEqual(doc["batch"]["tone"], "yours")
        self.assertEqual([p["tone"] for p in doc["packets"]], [None, "yours", None])
        self.assertEqual(doc["batch"]["needs_you"]["kind"], "merge")

    def test_running_phase(self):
        (self.root / "coordinator.log").write_text("2026-09-26T14:48:36+01:00 START phase 01 (/x/phase-01-a.md)\n")
        import calendar, time as _t
        started = 1758894516.0  # not used for the clock; the log stamp is
        doc = collect(self.root, now=collect(self.root)["packets"][0]["started"] + 600)
        self.assertEqual(doc["batch"]["phase"], "RUN")
        self.assertEqual(doc["packets"][0]["status"], "running")
        self.assertAlmostEqual(doc["packets"][0]["elapsed_s"], 600, delta=1)
        self.assertEqual(doc["batch"]["alive"]["expected_s"], 10800)


def transcript_lines(prompt, sizes, sidechain=None):
    """A Claude Code transcript: the prompt as the first user message, then one assistant turn per size."""
    lines = [{"type": "queue-operation"}, {"type": "user", "message": {"role": "user", "content": prompt}}]
    for n, size in enumerate(sizes):
        usage = {"input_tokens": 10, "cache_read_input_tokens": size - 110, "cache_creation_input_tokens": 100, "output_tokens": 5}
        # Claude Code writes one line per content block, each repeating the message's usage.
        lines += [{"type": "assistant", "message": {"id": "msg_%d" % n, "usage": usage}}] * 2
    if sidechain:
        lines.append({"type": "assistant", "isSidechain": True, "message": {"id": "msg_side", "usage": {"input_tokens": sidechain}}})
    return "".join(json.dumps(line) + "\n" for line in lines)


class ContextWindowTest(unittest.TestCase):
    """Peak context comes from the session transcript, not from the summed usage."""

    def setUp(self):
        import scripts.monitor.collect as collect_module
        self.module = collect_module
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = collect_module.CLAUDE_PROJECTS
        collect_module.CLAUDE_PROJECTS = base / "projects"
        collect_module._CONTEXT_CACHE.clear()
        collect_module._TRANSCRIPT_CACHE.clear()
        self.repo = base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "docs").mkdir()
        self.prompt = self.repo / "docs" / "phase-01-a.md"
        self.prompt.write_text("# Phase 01: A\n\nDo it.\n")
        self.folder = collect_module.CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
        self.folder.mkdir(parents=True)
        self.root = base / "run"
        self.root.mkdir()
        (self.root / "run_phases.sh").write_text("#!/usr/bin/env bash\nfor n in 01; do\n  echo\ndone\n")

    def tearDown(self):
        self.module.CLAUDE_PROJECTS = self.saved
        self.module._CONTEXT_CACHE.clear()
        self.module._TRANSCRIPT_CACHE.clear()
        self.tmp.cleanup()

    def test_finished_phase_reports_peak_of_its_session(self):
        session = "0275348d-1377-4541-8c30-195b7a9181a8"
        (self.folder / (session + ".jsonl")).write_text(transcript_lines("# Phase 01: A", [1000, 5000, 3000], sidechain=900000))
        (self.root / "coordinator.log").write_text(
            "2026-09-26T22:33:23+01:00 START phase 01 (" + str(self.prompt) + ")\n"
            "2026-09-26T22:43:14+01:00 END phase 01 exit=0 staging-status=todo :: success | done\n")
        (self.root / "phase-01.json").write_text(json.dumps({"subtype": "success", "session_id": session, "usage": {},
            "modelUsage": {"claude-opus-5-5": {"contextWindow": 1000000}}}) + "\n")
        context = collect(self.root)["packets"][0]["phase_run"]["context"]
        self.assertEqual(context, {"peak": 5000, "last": 3000, "turns": 3, "window": 1000000, "live": False})

    def test_running_phase_reads_the_live_transcript_incrementally(self):
        (self.root / "coordinator.log").write_text("2026-09-26T22:33:23+01:00 START phase 01 (" + str(self.prompt) + ")\n")
        (self.root / "phase-01.json").write_text("")
        (self.folder / "other.jsonl").write_text(transcript_lines("# Something else", [90000]))
        live = self.folder / "live.jsonl"
        live.write_text(transcript_lines("# Phase 01: A", [2000]) + '{"type": "assistant", "message": {"id": "half')
        started = collect(self.root)["packets"][0]["started"]
        for path in (live, self.folder / "other.jsonl"):
            os.utime(path, (started + 60, started + 60))
        context = collect(self.root, now=started + 120)["packets"][0]["phase_run"]["context"]
        self.assertEqual((context["peak"], context["last"], context["turns"], context["live"]), (2000, 2000, 1, True))
        self.assertIsNone(context["window"])
        with open(live, "a") as fh:  # the half-written line completes, and another turn arrives
            fh.write('_x", "usage": {"input_tokens": 7000}}}\n'
                     + json.dumps({"type": "assistant", "message": {"id": "msg_new", "usage": {"input_tokens": 4000}}}) + "\n")
        os.utime(live, (started + 90, started + 90))
        context = collect(self.root, now=started + 120)["packets"][0]["phase_run"]["context"]
        self.assertEqual((context["peak"], context["last"], context["turns"]), (7000, 4000, 3))

    def test_no_transcript_means_unknown(self):
        (self.root / "coordinator.log").write_text("2026-09-26T22:33:23+01:00 START phase 01 (" + str(self.prompt) + ")\n")
        (self.root / "phase-01.json").write_text("")
        self.assertIsNone(collect(self.root)["packets"][0]["phase_run"]["context"])


class EmptyAndErrorStateTest(unittest.TestCase):
    def test_missing_directory(self):
        doc = collect("/nonexistent/evidence/dir")
        self.assertEqual(doc["empty"], "missing")
        self.assertIsNone(doc["batch"])
        self.assertEqual(doc["packets"], [])

    def test_directory_without_state_is_preflight_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "preflight").mkdir()
            doc = collect(tmp)
        self.assertEqual(doc["empty"], "preflight")
        self.assertIsNone(doc["batch"])

    def test_unreadable_state_reports_file_and_parser_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text("{not json")
            doc = collect(tmp)
        self.assertIsNone(doc["empty"])
        self.assertIsNone(doc["batch"])
        self.assertEqual(len(doc["errors"]), 1)
        self.assertEqual(doc["errors"][0]["file"], "state.json")
        self.assertIn("Expecting", doc["errors"][0]["message"])

    def test_unreadable_runner_state_keeps_the_rest_of_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_batch_tree(root, phase="RUN", index=0, completed=[], runner_states={"packet-12": None})
            (root / "packet-12/runner/state.json").write_text("garbage")
            doc = collect(root)
        self.assertEqual(doc["batch"]["phase"], "RUN")
        self.assertEqual(doc["packets"][0]["status"], "running")
        self.assertTrue(doc["packets"][0]["runner"]["unreadable"])
        self.assertEqual([e["file"] for e in doc["errors"]], ["packet-12/runner/state.json"])

    def test_missing_manifest_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_batch_tree(root, phase="PREPARE", index=0, completed=[], runner_states={})
            (root / "manifest.json").unlink()
            doc = collect(root)
        self.assertEqual(doc["batch"]["phase"], "PREPARE")
        self.assertEqual([p["id"] for p in doc["packets"]], ["packet-12"])
        self.assertEqual(doc["errors"][0]["file"], "manifest.json")


if __name__ == "__main__":
    unittest.main()
