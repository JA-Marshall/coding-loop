"""Narrow GitHub mutations. Reads reconcile every retry; merges compare exact SHA."""
from __future__ import annotations
import json
import subprocess
import time
from .runner import RunnerError


class GitHub:
    def __init__(self, repository):
        self.repository = repository
        self.prefix = "repos/" + repository

    def api(self, path, method="GET", payload=None):
        argv = ["gh", "api", "--method", method, self.prefix + path]
        if payload is not None:
            argv += ["--input", "-"]
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                result = subprocess.run(argv, input=json.dumps(payload) if payload is not None else None,
                                        text=True, capture_output=True, timeout=60)
                if result.returncode == 0:
                    return json.loads(result.stdout) if result.stdout else None
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
        raise RunnerError("GitHub request uncertain; reconcile before retry")

    def pull(self, number):
        return self.api(f"/pulls/{number}")

    def ensure_pull(self, branch, title, body):
        # Include closed PRs: never create another PR after a lost merge response.
        pulls = self.api("/pulls?state=all&base=main&head=" + self.repository.split("/")[0] + ":" + branch)
        if len(pulls) > 1:
            raise RunnerError("More than one PR matches the batch branch")
        if pulls:
            return pulls[0]
        try:
            return self.api("/pulls", "POST", {"head": branch, "base": "main", "title": title, "body": body})
        except RunnerError:
            # Do not repeat POST; a lost response may already have created it.
            pulls = self.api("/pulls?state=all&base=main&head=" + self.repository.split("/")[0] + ":" + branch)
            if len(pulls) == 1:
                return pulls[0]
            raise

    def checks(self, sha):
        result = []
        page = 1
        while True:
            runs = self.api(f"/commits/{sha}/check-runs?per_page=100&page={page}")["check_runs"]
            result.extend(runs)
            if len(runs) < 100:
                return result
            page += 1

    def gate(self, number, sha, branch):
        pull = self.pull(number)
        if (pull["head"]["sha"] != sha or pull["head"]["ref"] != branch
                or pull["head"]["repo"]["full_name"] != self.repository
                or pull["base"]["ref"] != "main" or pull.get("draft")):
            raise RunnerError("PR identity/head differs from reviewed candidate")
        merged = bool(pull.get("merged"))
        if pull["state"] != "open" and not merged:
            raise RunnerError("PR closed without merge")
        checks = self.checks(sha)
        required = [x for x in checks if x["name"] == "Storehouse required"
                    and x["head_sha"] == sha and x.get("app", {}).get("slug") == "github-actions"]
        if not required:
            return "WAIT", pull
        latest = max(required, key=lambda x: x["id"])
        if latest["status"] != "completed":
            return "WAIT", pull
        if latest["conclusion"] != "success":
            raise RunnerError("Required CI failed on candidate; preserve PR for correction")
        newest_checks = {}
        for check in checks:
            name = (check["name"], check.get("app", {}).get("slug"))
            if name not in newest_checks or check["id"] > newest_checks[name]["id"]:
                newest_checks[name] = check
        for check in newest_checks.values():
            if check["status"] != "completed":
                return "WAIT", pull
            if check["conclusion"] not in {"success", "skipped", "neutral"}:
                raise RunnerError("A candidate check failed; automatic merge stopped")
        # Verify the gate belongs to our existing trusted CI workflow, not a job
        # with a coincidentally matching name in another workflow.
        suite = self.api(f"/check-suites/{latest['check_suite']['id']}")
        runs = self.api(f"/actions/runs?check_suite_id={suite['id']}&per_page=100")["workflow_runs"]
        matching = [r for r in runs if r["check_suite_id"] == suite["id"]
                    and r["head_sha"] == sha and r["path"] == ".github/workflows/ci.yml"
                    and r["event"] == "pull_request"]
        if not matching:
            raise RunnerError("Required check has no verified repository CI provenance")
        run = max(matching, key=lambda r: r["id"])
        if run["status"] != "completed":
            return "WAIT", pull
        if run["conclusion"] != "success":
            raise RunnerError("Required workflow did not succeed")
        # Newest review from every reviewer must not request changes.
        reviews = self.api(f"/pulls/{number}/reviews?per_page=100")
        if len(reviews) >= 100:
            raise RunnerError("Review pagination requires manual inspection")
        newest = {}
        for review in reviews:
            if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                newest[review["user"]["login"]] = review["state"]
        if "CHANGES_REQUESTED" in newest.values():
            raise RunnerError("A GitHub reviewer requested changes")
        if merged:
            return "MERGED", pull
        if not pull.get("mergeable") or pull.get("mergeable_state") not in {"clean", "unstable"}:
            return "WAIT", pull
        return "READY", pull

    def merge(self, number, sha, branch):
        status, pull = self.gate(number, sha, branch)
        if status == "MERGED":
            return pull
        if status != "READY":
            raise RunnerError("Merge gate is not ready")
        try:
            result = self.api(f"/pulls/{number}/merge", "PUT", {"sha": sha, "merge_method": "merge"})
        except RunnerError:
            status, pull = self.gate(number, sha, branch)
            if status == "MERGED":
                return pull
            raise
        if not result.get("merged"):
            raise RunnerError("GitHub did not confirm merge")
        return self.pull(number)
