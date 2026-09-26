"""Plan board: collect_plans() over a synthetic checkout, the renderer, and the routes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from scripts.monitor.plans import collect_plans, confine, markdown_html, render_plan
from scripts.monitor.serve import default_checkout, make_server

CONTROL = """<!-- storehouse-plan
id: {id}
status: {status}
active_phase: {phase}
agent_strategy: PRIMARY
implementation_authority: {authority}
merge_authority: OWNER_REQUIRED
release_authority: OWNER_REQUIRED
schema_impact: NONE
data_impact: NONE
valuation_impact: NONE
reporting_impact: NONE
marketplace_write_impact: NONE
production_impact: NONE
tbd: NONE
-->
"""


def plan_text(plan_id, title, status="DRAFT", phase="NONE", authority="PLAN_ONLY", body=""):
    return "# " + title + "\n\n" + CONTROL.format(id=plan_id, status=status, phase=phase, authority=authority) + "\n" + body


def build_checkout(root, now):
    tasks = root / "docs/plans/tasks"
    tasks.mkdir(parents=True)
    (tasks / "old-plan.md").write_text(plan_text("old-plan", "An old finished plan", "COMPLETE", "NONE", "CLOSED",
                                                 "## Objective\n\nDone long ago. See [the new one](new-plan.md) and [a design](../../design/x.md).\n"))
    (tasks / "new-plan.md").write_text(plan_text("new-plan", "The plan in flight", "IN_PROGRESS", "P1", "GOAL_GRANTED",
                                                 "## Objective\n\nShip `thing` **now**.\n\n```\ncode <b>here</b>\n```\n\n- one\n- two\n  continued\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n<script>alert(1)</script>\n"))
    (tasks / "ready-but-old.md").write_text(plan_text("ready-but-old", "Ready and waiting", "READY", "P1", "GOAL_REQUIRED"))
    (root / "docs/plans/INBOX.md").write_text("# Plan inbox\n\n- [An old finished plan](tasks/old-plan.md)\n- [The plan in flight](tasks/new-plan.md)\n")
    (root / "docs/plans/ACTIVE.md").write_text("# Active\n\n<!-- storehouse-active\ntarget: docs/plans/tasks/new-plan.md\n-->\n")
    (root / "docs/plans/templates").mkdir()
    (root / "docs/plans/templates/TASK.md").write_text(plan_text("template", "Template", "DRAFT"))
    (root / "docs/design").mkdir()
    (root / "docs/design/x.md").write_text("# Not a plan\n")
    old = now - 40 * 86400
    os.utime(tasks / "old-plan.md", (old, old))
    os.utime(tasks / "ready-but-old.md", (old, old))


class CollectPlansTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "checkout"
        self.root.mkdir()
        self.now = time.time()
        build_checkout(self.root, self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_plans_with_control_fields_and_recency(self):
        doc = collect_plans(self.root, now=self.now)
        self.assertEqual(doc["checkout"], str(self.root.resolve()))
        self.assertEqual(doc["active"], "docs/plans/tasks/new-plan.md")
        self.assertEqual(doc["time_source"], "mtime")
        by_id = {p["id"]: p for p in doc["plans"]}
        self.assertEqual(set(by_id), {"old-plan", "new-plan", "ready-but-old"})  # the template is skipped
        self.assertEqual(by_id["new-plan"]["title"], "The plan in flight")
        self.assertEqual(by_id["new-plan"]["status"], "IN_PROGRESS")
        self.assertEqual(by_id["new-plan"]["active_phase"], "P1")
        self.assertTrue(by_id["new-plan"]["active"])
        self.assertTrue(by_id["new-plan"]["recent"])
        self.assertTrue(by_id["new-plan"]["in_inbox"])
        self.assertFalse(by_id["old-plan"]["recent"])
        self.assertTrue(by_id["ready-but-old"]["recent"], "executable plans are always recent")
        self.assertFalse(by_id["ready-but-old"]["in_inbox"])
        self.assertEqual(doc["plans"][0]["id"], "new-plan", "newest first")
        self.assertEqual(doc["counts"], {"total": 3, "recent": 2})
        json.dumps(doc)

    def test_git_history_supplies_times_when_available(self):
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-02T03:04:05Z", GIT_COMMITTER_DATE="2026-01-02T03:04:05Z",
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "plans"], cwd=self.root, check=True, env=env)
        (self.root / "docs/plans/tasks/new-plan.md").write_text(plan_text("new-plan", "Edited", "IN_PROGRESS", "P1", "GOAL_GRANTED"))
        doc = collect_plans(self.root, now=self.now)
        self.assertEqual(doc["time_source"], "git")
        by_id = {p["id"]: p for p in doc["plans"]}
        self.assertEqual(by_id["old-plan"]["time_source"], "git")
        self.assertEqual(time.gmtime(by_id["old-plan"]["updated"])[:3], (2026, 1, 2))
        self.assertEqual(by_id["new-plan"]["time_source"], "mtime", "uncommitted edits use the file time")

    def test_no_checkout_or_no_docs(self):
        self.assertIsNone(collect_plans(None)["checkout"])
        self.assertTrue(collect_plans(None)["errors"])
        doc = collect_plans(self.tmp.name)
        self.assertEqual(doc["plans"], [])
        self.assertEqual(doc["errors"][0]["file"], "docs")

    def test_render_plan_escapes_and_rewrites_links(self):
        doc = render_plan(self.root, "docs/plans/tasks/new-plan.md")
        self.assertEqual(doc["title"], "The plan in flight")
        self.assertEqual(doc["control"]["status"], "IN_PROGRESS")
        html = doc["html"]
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<pre><code>code &lt;b&gt;here&lt;/b&gt;</code></pre>", html)
        self.assertIn("<code>thing</code> <strong>now</strong>", html)
        self.assertIn("<ul><li>one</li><li>two continued</li></ul>", html)
        self.assertIn("<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>", html)
        self.assertNotIn("storehouse-plan", html, "the control block is not rendered as text")
        old = render_plan(self.root, "docs/plans/tasks/old-plan.md")["html"]
        self.assertIn('<a href="#plan=docs/plans/tasks/new-plan.md">the new one</a>', old)
        self.assertIn('a design <span class="muted">(../../design/x.md)</span>', old, "non-plan repo links are shown, not linked")

    def test_render_refuses_paths_outside_docs(self):
        (self.root / "secret.md").write_text("# secret\n")
        self.assertIsNone(render_plan(self.root, "secret.md"))
        (Path(self.tmp.name) / "outside.md").write_text("# outside\n")
        self.assertIsNone(render_plan(self.root, "../outside.md"))
        self.assertIsNone(render_plan(self.root, "docs/../../outside.md"))
        self.assertIsNone(render_plan(self.root, "docs/plans/tasks/missing.md"))
        self.assertIsNone(confine(self.root, "/etc/passwd"))
        self.assertIsNone(render_plan(None, "docs/plans/tasks/new-plan.md"))

    def test_external_links_open_in_new_tab(self):
        html = markdown_html("See [docs](https://example.com/x) and [here](#anchor).")
        self.assertIn('<a href="https://example.com/x" target="_blank" rel="noopener">docs</a>', html)
        self.assertIn('<a href="#anchor">here</a>', html)


class PlanRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "checkout"
        cls.root.mkdir()
        build_checkout(cls.root, time.time())
        cls.evidence = Path(cls.tmp.name) / "evidence"
        cls.evidence.mkdir()
        (cls.evidence / "manifest.json").write_text(json.dumps({"id": "b", "checkout": str(cls.root)}))
        cls.server = make_server(cls.evidence, "127.0.0.1", 0)
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
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_checkout_defaults_to_the_manifest(self):
        self.assertEqual(self.server.checkout, self.root.resolve())
        self.assertIsNone(default_checkout(self.tmp.name))

    def test_routes(self):
        status, body = self.get("/plans")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Plan Board</title>", body)
        self.assertNotIn(b"<script src", body)
        status, body = self.get("/api/plans")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["counts"]["total"], 3)
        status, body = self.get("/api/plan?path=" + quote("docs/plans/tasks/new-plan.md"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["title"], "The plan in flight")
        self.assertEqual(self.get("/api/plan?path=" + quote("../evidence/manifest.json"))[0], 403)
        self.assertEqual(self.get("/api/plan?path=" + quote(str(self.root / "docs/plans/tasks/new-plan.md")))[0], 403)
        self.assertEqual(self.get("/api/plan?path=docs/plans/tasks/nope.md")[0], 404)

    def test_no_checkout_server_reports_it(self):
        server = make_server(self.tmp.name, "127.0.0.1", 0)
        try:
            self.assertIsNone(server.checkout)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
