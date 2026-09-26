"""serve.py: the two routes, the log route's refusals, the 404 and the bind address."""
import io
import json
import os
import re
import time
from urllib.request import Request
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from scripts.monitor import serve
from scripts.monitor.serve import Notifier, discover_batches, log_tail, make_server, notification_text, read_webhook, validate_host

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "examples" / "smoke-run"


class BindAddressTest(unittest.TestCase):
    def test_loopback_addresses_are_accepted(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
            self.assertEqual(validate_host(host), host)

    def test_anything_else_is_refused(self):
        for host in ("0.0.0.0", "", "192.168.1.5", "10.0.0.1", "::", "example.com", "2001:db8::1"):
            with self.assertRaises(ValueError, msg=host):
                validate_host(host)

    def test_main_refuses_a_wide_bind_without_binding(self):
        stderr = io.StringIO()
        with patch("scripts.monitor.serve.MonitorServer.__init__", side_effect=AssertionError("must not bind")):
            with patch("sys.stderr", stderr):
                code = serve.main(["--directory", str(SMOKE), "--host", "0.0.0.0"])
        self.assertEqual(code, 2)
        self.assertIn("loopback", stderr.getvalue())

    def test_server_binds_loopback_only(self):
        server = make_server(SMOKE, "127.0.0.1", 0)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()
        with self.assertRaises(ValueError):
            make_server(SMOKE, "0.0.0.0", 0)


class LogTailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "evidence"
        shutil.copytree(SMOKE, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_inside_log_is_served_with_a_bounded_tail(self):
        big = self.root / "run" / "check-2-big.log"
        big.write_text("x" * 20000 + "END")
        status, payload = log_tail(self.root, str(big))
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["tail"]), 8000)
        self.assertTrue(payload["tail"].endswith("END"))
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["size"], 20003)
        status, payload = log_tail(self.root, "run/check-1-clamp-acceptance.log")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tail"], "Six clamp acceptance cases passed\n")
        self.assertFalse(payload["truncated"])

    def test_paths_outside_the_directory_are_refused(self):
        outside = Path(self.tmp.name) / "secret.log"
        outside.write_text("private")
        for raw in (str(outside), "../secret.log", "run/../../secret.log", "/etc/passwd", "/etc/hostname.log"):
            status, payload = log_tail(self.root, raw)
            self.assertEqual(status, 403, raw)
            self.assertNotIn("private", json.dumps(payload))

    def test_symlink_escaping_the_directory_is_refused(self):
        outside = Path(self.tmp.name) / "secret.log"
        outside.write_text("private")
        link = self.root / "run" / "link.log"
        os.symlink(outside, link)
        status, _payload = log_tail(self.root, str(link))
        self.assertEqual(status, 403)

    def test_only_log_files_are_served(self):
        status, _payload = log_tail(self.root, "run/state.json")
        self.assertEqual(status, 403)
        status, _payload = log_tail(self.root, "packet.json")
        self.assertEqual(status, 403)

    def test_missing_log_is_404_and_empty_path_is_400(self):
        self.assertEqual(log_tail(self.root, "run/check-9-nope.log")[0], 404)
        self.assertEqual(log_tail(self.root, "")[0], 400)


class BatchPickerTest(unittest.TestCase):
    """A folder of batches, as ~/.local/share/storehouse-runner/batches is laid out."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "batches"
        for name, phase, age in (("ebay-01", "STOPPED", 300), ("ebay-02", "RUN", 10)):
            run = cls.root / name / "run"
            run.mkdir(parents=True)
            (run / "state.json").write_text(json.dumps({"manifest_hash": "m", "phase": phase, "index": 0, "calls": 0,
                                                        "tokens": 0, "completed": [], "deadline": 1e10}))
            (run / "manifest.json").write_text(json.dumps({"id": name, "tasks": [], "total_timeout": 100, "checkout": "/nowhere"}))
            (run / "packet-x" / "runner").mkdir(parents=True)
            (run / "packet-x" / "runner" / "state.json").write_text(json.dumps({"packet_hash": "p", "phase": "STOPPED", "calls": 0}))
            stamp = os.path.getmtime(run / "state.json") - age
            os.utime(run / "state.json", (stamp, stamp))
        (cls.root / "not-a-batch").mkdir()
        # A continuation run beside run/, a runner-only tree without run/, and a deeper group.
        cont = cls.root / "ebay-01" / "run-continuation-01"
        cont.mkdir()
        (cont / "state.json").write_text(json.dumps({"manifest_hash": "m", "phase": "STOPPED", "index": 0, "calls": 0,
                                                      "tokens": 0, "completed": [], "deadline": 1e10}))
        stamp = os.path.getmtime(cont / "state.json") - 600
        os.utime(cont / "state.json", (stamp, stamp))
        pilot = Path(cls.tmp.name) / "runs" / "pilot-02"
        pilot.mkdir(parents=True)
        (pilot / "state.json").write_text(json.dumps({"packet_hash": "p", "phase": "LOCAL_REVIEWED", "calls": 1, "corrections": 0,
                                                       "candidate": "c", "deadline": 1e10}))
        stamp = os.path.getmtime(pilot / "state.json") - 900
        os.utime(pilot / "state.json", (stamp, stamp))
        cls.server = make_server(cls.root, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get_json(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read())

    def test_discovery_walks_every_tree_and_sorts_newest_first(self):
        found = discover_batches(self.root)
        self.assertEqual([b["id"] for b in found], ["ebay-02", "ebay-01", "ebay-01/run-continuation-01"])
        self.assertEqual(found[0]["path"], str((self.root / "ebay-02").resolve()))
        self.assertEqual(found[1]["phase"], "STOPPED")
        self.assertEqual(found[0]["kind"], "batch")
        self.assertNotIn("packet-x", json.dumps(found), "packet directories inside a batch are not runs")
        whole = discover_batches(self.tmp.name)
        self.assertEqual([b["id"] for b in whole], ["batches/ebay-02", "batches/ebay-01", "batches/ebay-01/run-continuation-01", "runs/pilot-02"])
        self.assertEqual([b["group"] for b in whole], ["batches", "batches", "batches", "runs"])
        self.assertEqual(whole[-1]["kind"], "runner")
        self.assertEqual([b["id"] for b in discover_batches(self.root / "ebay-01")], ["ebay-01", "run-continuation-01"])
        self.assertEqual(discover_batches(SMOKE)[0]["path"], str(SMOKE.resolve()))
        self.assertEqual(discover_batches(self.root / "not-a-batch"), [])

    def test_state_defaults_to_newest_and_switches_with_batch(self):
        doc = self.get_json("/api/state")
        self.assertEqual(doc["batch_id"], "ebay-02")
        self.assertEqual([b["id"] for b in doc["batches"]], ["ebay-02", "ebay-01", "ebay-01/run-continuation-01"])
        self.assertEqual(doc["batch"]["phase"], "RUN")
        doc = self.get_json("/api/state?batch=ebay-01")
        self.assertEqual(doc["batch_id"], "ebay-01")
        self.assertEqual(doc["batch"]["phase"], "STOPPED")
        self.assertEqual(doc["batch"]["evidence_dir"], str((self.root / "ebay-01" / "run").resolve()), "read from run/")
        doc = self.get_json("/api/state?batch=nope")
        self.assertEqual(doc["batch_id"], "ebay-02", "an unknown id falls back to the newest")
        self.assertEqual(self.get_json("/api/batches")["batches"][1]["id"], "ebay-01")

    def test_id_folder_without_run_suffix_still_works(self):
        server = make_server(self.root / "ebay-01", "127.0.0.1", 0)
        try:
            target, batches = server.select("")
            self.assertEqual(target, (self.root / "ebay-01").resolve())
            self.assertEqual([b["id"] for b in batches], ["ebay-01", "run-continuation-01"])
        finally:
            server.server_close()


class MultipleRootsTest(unittest.TestCase):
    """--directory given twice: the coding loop's runs and a phase run, one picker."""

    @classmethod
    def setUpClass(cls):
        from scripts.tests.test_monitor_collect import build_phase_run
        cls.tmp = tempfile.TemporaryDirectory()
        cls.runner_root = Path(cls.tmp.name) / "storehouse-runner"
        run = cls.runner_root / "batches" / "ebay-01" / "run"
        run.mkdir(parents=True)
        (run / "state.json").write_text(json.dumps({"manifest_hash": "m", "phase": "STOPPED", "index": 0, "calls": 0,
                                                    "tokens": 0, "completed": [], "deadline": 1e10}))
        cls.phase_root = Path(cls.tmp.name) / "proof-hardware-phases"
        build_phase_run(cls.phase_root)
        cls.server = make_server([cls.runner_root, cls.phase_root], "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_both_roots_are_listed_with_root_prefixed_ids(self):
        status, doc = self.get("/api/batches")
        self.assertEqual(status, 200)
        ids = {b["id"]: b for b in doc["batches"]}
        self.assertEqual(set(ids), {"storehouse-runner/batches/ebay-01", "proof-hardware-phases"})
        self.assertEqual(ids["proof-hardware-phases"]["kind"], "phases")
        self.assertEqual(ids["proof-hardware-phases"]["phase"], "STOPPED")
        self.assertEqual((ids["proof-hardware-phases"]["merged"], ids["proof-hardware-phases"]["total"]), (1, 3))
        self.assertEqual(ids["storehouse-runner/batches/ebay-01"]["group"], "storehouse-runner")
        status, doc = self.get("/api/state?batch=proof-hardware-phases")
        self.assertEqual(doc["batch"]["mode"], "phases")
        self.assertEqual(doc["batch_id"], "proof-hardware-phases")

    def test_err_files_are_served_from_any_root_and_nothing_else(self):
        status, payload = self.get("/api/log?path=" + quote(str(self.phase_root / "phase-02.err")))
        self.assertEqual(status, 200)
        self.assertEqual(payload["tail"], "warning: something\n")
        self.assertEqual(self.get("/api/log?path=" + quote(str(self.phase_root / "phase-02.json")))[0], 403)
        self.assertEqual(self.get("/api/log?path=" + quote(str(Path(self.tmp.name) / "x.log")))[0], 403)

    def test_discovery_is_cached_briefly(self):
        first = self.server.batches()
        (self.runner_root / "batches" / "ebay-02" / "run").mkdir(parents=True)
        (self.runner_root / "batches" / "ebay-02" / "run" / "state.json").write_text(json.dumps({"manifest_hash": "m", "phase": "RUN", "index": 0, "calls": 0, "tokens": 0, "completed": [], "deadline": 1e10}))
        self.assertEqual(len(self.server.batches()), len(first), "within the cache window")
        self.server._cache = (0.0, [])
        self.assertEqual(len(self.server.batches()), len(first) + 1)


class ControlsTest(unittest.TestCase):
    """The only writes: one STOP file, its removal, and the phase runner's own subcommands."""

    def setUp(self):
        from scripts.tests.test_monitor_collect import build_phase_run
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.batch = root / "batches" / "ebay-01"
        run = self.batch / "run"
        run.mkdir(parents=True)
        (run / "state.json").write_text(json.dumps({"manifest_hash": "m", "phase": "RUN", "index": 0, "calls": 0,
                                                    "tokens": 0, "completed": [], "deadline": 1e10}))
        self.phases = root / "proof-hardware-phases"
        build_phase_run(self.phases)
        # A stand-in runner that records how it was called and exits at once.
        (self.phases / "run_phases.sh").write_text('#!/usr/bin/env bash\necho "$@" >> "$LOG/launched.txt"\n')
        os.chmod(self.phases / "run_phases.sh", 0o755)
        self.server = make_server(root, "127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, header=True, method="POST"):
        data = json.dumps(body or {}).encode()
        headers = {"Content-Type": "application/json"}
        if header:
            headers["X-Requested-With"] = "monitor"
        request = Request(self.base + path, data=data if method == "POST" else None, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_stop_writes_exactly_one_empty_file_where_the_loop_looks(self):
        before = sorted(p.name for p in (self.batch / "run").iterdir())
        status, payload = self.call("/api/stop?batch=batches/ebay-01")
        self.assertEqual((status, payload["created"]), (200, True))
        stop = self.batch / "run" / "STOP"
        self.assertTrue(stop.is_file())
        self.assertEqual(stop.stat().st_size, 0)
        self.assertEqual(sorted(p.name for p in (self.batch / "run").iterdir()), sorted(before + ["STOP"]))
        status, payload = self.call("/api/stop?batch=batches/ebay-01")
        self.assertEqual((status, payload["created"]), (200, False), "a second press is a no-op")
        self.assertTrue(json.loads(urlopen(self.base + "/api/state?batch=batches/ebay-01").read())["batch"]["stop_requested"])
        status, payload = self.call("/api/stop/clear?batch=batches/ebay-01")
        self.assertEqual((status, payload["removed"]), (200, True))
        self.assertFalse(stop.exists())

    def test_refusals(self):
        self.assertEqual(self.call("/api/stop?batch=batches/ebay-01", header=False)[0], 403)
        self.assertEqual(self.call("/api/stop?batch=batches/ebay-01", method="GET")[0], 405)
        self.assertEqual(self.call("/api/phases/launch?batch=batches/ebay-01", {"command": "run"})[0], 400, "batches do not launch from the page")
        self.assertEqual(self.call("/api/phases/launch?batch=proof-hardware-phases", {"command": "rm"})[0], 400)
        self.assertEqual(self.call("/api/phases/launch?batch=proof-hardware-phases", {"command": "review", "phase": "1; rm"})[0], 400)
        (self.phases / "STOP").write_text("")
        self.assertEqual(self.call("/api/phases/launch?batch=proof-hardware-phases", {"command": "run"})[0], 409)
        (self.phases / "STOP").unlink()
        self.assertFalse((self.phases / "launched.txt").exists(), "nothing was launched by a refused call")

    def test_launch_runs_the_script_with_the_subcommand(self):
        status, payload = self.call("/api/phases/launch?batch=proof-hardware-phases", {"command": "review", "phase": "02"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["argv"][1:], ["review", "02"])
        for _ in range(50):
            if (self.phases / "launched.txt").exists():
                break
            time.sleep(0.05)
        self.assertEqual((self.phases / "launched.txt").read_text().strip(), "review 02")
        self.assertTrue(Path(payload["log"]).is_file())
        doc = json.loads(urlopen(self.base + "/api/state?batch=proof-hardware-phases").read())
        self.assertEqual(doc["control"]["phases"], True)
        self.assertIn("locked", doc["control"])


class NotifierTest(unittest.TestCase):
    def setUp(self):
        from scripts.tests.test_monitor_collect import build_phase_run
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "runs"
        self.phases = self.root / "proof-hardware-phases"
        build_phase_run(self.phases)  # stopped: "phase 02 is not done on staging"
        self.server = make_server(self.root, "127.0.0.1", 0)
        self.posted = []
        self.state = Path(self.tmp.name) / "state.json"
        self.notifier = Notifier(self.server, "https://example.invalid/hook", "http://127.0.0.1:8790/", state_path=self.state,
                                 poster=lambda url, text: self.posted.append(text))

    def tearDown(self):
        self.server.server_close()
        self.tmp.cleanup()

    def test_first_pass_is_silent_then_changes_are_posted_once(self):
        self.assertEqual(self.notifier.pass_once(), [], "existing stops are not announced on startup")
        self.assertEqual(self.posted, [])
        self.assertTrue(self.state.is_file())
        self.assertEqual(self.notifier.pass_once(), [], "nothing changed")
        with open(self.phases / "coordinator.log", "a") as log:
            log.write("2026-09-26T16:00:00+01:00 START phase 03 (/x/phase-03-landing.md) model=claude-sonnet-5\n"
                      "2026-09-26T16:20:00+01:00 END phase 03 exit=0 staging-status=todo :: success | PR https://github.com/o/r/pull/9\n"
                      "2026-09-26T16:25:00+01:00 REVIEW phase 03 round 0: verdict CLEAN (advisory CLEAN), posted to PR #9\n"
                      "2026-09-26T16:25:00+01:00 STOP: phase 03 reviewed clean; merge its PR, then rerun\n")
        self.server._cache = (0.0, [])
        self.assertEqual(self.notifier.pass_once(), ["proof-hardware-phases"])
        self.assertEqual(len(self.posted), 1)
        self.assertIn("waits for your merge", self.posted[0])
        self.assertIn("https://github.com/o/r/pull/9", self.posted[0])
        self.assertIn("?batch=proof-hardware-phases", self.posted[0])
        self.assertEqual(self.notifier.pass_once(), [], "the same need is not repeated")
        # A restart reads the saved state and stays quiet about what it already announced.
        again = Notifier(self.server, "https://example.invalid/hook", "http://127.0.0.1:8790/", state_path=self.state,
                         poster=lambda url, text: self.posted.append(text))
        self.assertEqual(again.pass_once(), [])
        self.assertEqual(len(self.posted), 1)

    def test_a_failed_post_is_retried_next_pass(self):
        self.notifier.pass_once()
        with open(self.phases / "coordinator.log", "a") as log:
            log.write("2026-09-26T16:25:00+01:00 STOP: operator requested stop (x) before phase 03\n")
        (self.phases / "STOP").write_text("")
        self.server._cache = (0.0, [])
        calls = []
        def flaky(url, text):
            calls.append(text)
            if len(calls) == 1:
                raise OSError("network down")
        self.notifier.poster = flaky
        self.assertEqual(self.notifier.pass_once(), [])
        self.assertEqual(self.notifier.pass_once(), ["proof-hardware-phases"])
        self.assertEqual(len(calls), 2)

    def test_webhook_is_read_from_the_private_file_not_the_repo(self):
        env = Path(self.tmp.name) / "notify.env"
        env.write_text("# comment\nNOTIFY_WEBHOOK=https://discord.com/api/webhooks/1/abc\n")
        self.assertEqual(read_webhook(env), "https://discord.com/api/webhooks/1/abc")
        self.assertIsNone(read_webhook(Path(self.tmp.name) / "missing.env"))
        # A real webhook id is a long snowflake; the fake one above is not.
        real = re.compile(r"discord(?:app)?\.com/api/webhooks/\d{17,}")
        repo = Path(__file__).resolve().parents[2]
        for path in list((repo / "scripts").rglob("*.py")) + list((repo / "scripts").rglob("*.html")) + list((repo / "scripts").rglob("*.sh")):
            self.assertIsNone(real.search(path.read_text(errors="replace")), path)

    def test_notification_text(self):
        doc = {"batch": {"phase": "COMPLETE", "needs_you": None}}
        self.assertIn("finished", notification_text("x", doc, "http://h/"))
        self.assertIsNone(notification_text("x", {"batch": {"phase": "RUN", "needs_you": None}}, "http://h/"))


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(SMOKE, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except HTTPError as error:
            return error.code, error.headers.get("Content-Type", ""), error.read()

    def test_page(self):
        status, content_type, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        text = body.decode()
        self.assertIn("<title>Batch Board</title>", text)
        self.assertIn("/api/state", text)
        self.assertNotIn("https://cdn", text)
        self.assertNotIn("fonts.googleapis", text)
        self.assertNotIn("<script src", text)
        self.assertNotIn('rel="stylesheet"', text)
        for tag in text.split("<link")[1:]:  # only the data: favicon, which fetches nothing
            self.assertTrue(tag.startswith(' rel="icon" href="data:,">'), tag[:60])
        self.assertNotIn("http://", text.split("<script>")[0].split("<body>")[0].replace("http://www.w3.org", ""))

    def test_state(self):
        status, content_type, body = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("application/json"))
        doc = json.loads(body)
        self.assertEqual(doc["packets"][0]["phase"], "LOCAL_REVIEWED")
        self.assertEqual(doc["batch"]["evidence_dir"], str(SMOKE.resolve()))

    def test_log_inside_and_outside(self):
        inside = SMOKE / "run" / "check-1-clamp-acceptance.log"
        status, _content_type, body = self.get("/api/log?path=" + quote(str(inside)))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tail"], "Six clamp acceptance cases passed\n")
        status, _content_type, body = self.get("/api/log?path=" + quote(str(REPO / "README.md")))
        self.assertEqual(status, 403)
        status, _content_type, body = self.get("/api/log?path=" + quote("../../README.md"))
        self.assertEqual(status, 403)
        status, _content_type, body = self.get("/api/log?path=" + quote(str(SMOKE / "run" / "missing.log")))
        self.assertEqual(status, 404)
        status, _content_type, body = self.get("/api/log")
        self.assertEqual(status, 400)

    def test_unknown_route_is_404(self):
        status, content_type, body = self.get("/api/stop")
        self.assertEqual(status, 405, "control routes are POST only")
        status, content_type, body = self.get("/api/nothing")
        self.assertEqual(status, 404)
        self.assertTrue(content_type.startswith("application/json"))
        status, _content_type, _body = self.get("/index.html")
        self.assertEqual(status, 404)

    def test_no_writes_under_the_evidence_directory(self):
        before = {str(p): p.stat().st_mtime_ns for p in SMOKE.rglob("*")}
        self.get("/api/state")
        self.get("/api/log?path=" + quote(str(SMOKE / "run" / "check-1-clamp-acceptance.log")))
        self.assertEqual({str(p): p.stat().st_mtime_ns for p in SMOKE.rglob("*")}, before)


if __name__ == "__main__":
    unittest.main()
