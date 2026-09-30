"""Native worker calls: the loop takes the diff of the worker's copy. No model CLI and no network.

Most tests replace native.run_container with a fake model that edits the copy. The Docker tests
run a shell script as the "CLI" in the real container, skip when Docker or the local image is
unavailable, and never pull an image.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.coordination import native
from scripts.coordination.runner import Runner, RunnerError, git


def docker_ready():
    try:
        return (subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
                and subprocess.run(["docker", "image", "inspect", native.IMAGE], capture_output=True, timeout=20).returncode == 0)
    except (OSError, subprocess.TimeoutExpired):
        return False


def codex_events(tools=2, completed=True):
    lines = [{"type": "thread.started"}, {"type": "turn.started"}]
    lines += [{"type": "item.completed", "item": {"type": "command_execution"}}] * tools
    lines += [{"type": "item.completed", "item": {"type": "agent_message"}}]
    if completed:
        lines.append({"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 40}})
    return "".join(json.dumps(line) + "\n" for line in lines)


class FakeModel:
    """Stands in for run_container: edits the mounted copy as a model would, and writes what the CLI would have."""

    def __init__(self, edit=None, code=0, killed=False, events=None, message="Changed sample.txt"):
        self.edit, self.code, self.killed = edit, code, killed
        self.events, self.message, self.specs = events, message, []

    def __call__(self, runner, spec):
        self.specs.append(spec)
        mounts = {target: Path(source) for source, target, _ in spec["mounts"]}
        work, home = mounts[native.WORK], mounts[native.HOME]
        self.saw_login = (home / ".codex" / "auth.json").is_file() or (home / ".codex" / "config.toml").is_file()
        if self.edit:
            self.edit(work)
        (runner.run_dir / (spec["log"] + ".log")).write_text(self.events if self.events is not None else codex_events())
        if self.message is not None:
            (home / "last-message.txt").write_text(self.message)
        sessions = home / ".codex" / "sessions" / "2026" / "09" / "30"
        sessions.mkdir(parents=True)
        (sessions / "rollout.jsonl").write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 700, "output_tokens": 60},
            "last_token_usage": {"input_tokens": 300, "output_tokens": 20, "total_tokens": 320},
            "model_context_window": 258400}}}) + "\n")
        return self.code, self.killed


def write_new(work):
    (work / "sample.txt").write_text("new\n")


@unittest.skipUnless(sys.platform.startswith("linux"), "Runner requires Linux/WSL")
class NativeWorkerTests(unittest.TestCase):
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
        egress = patch.object(native, "ensure_egress", lambda: "proxy")
        egress.start()
        self.addCleanup(egress.stop)
        self.root = self.home / "repo"
        self.root.mkdir()
        git(self.root, "init", "-b", "codex/fixture")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        git(self.root, "config", "user.name", "Fixture")
        (self.root / "sample.txt").write_text("old\n")
        (self.root / "run.sh").write_text("#!/bin/sh\n")
        (self.root / "run.sh").chmod(0o755)
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "fixture")
        self.hidden = self.home / "hidden"
        self.hidden.mkdir()
        (self.hidden / "test_hidden.py").write_text("SECRET = 1\n")
        self.packet = {
            "id": "native-fixture", "checkout": str(self.root),
            "base_sha": git(self.root, "rev-parse", "HEAD").decode().strip(),
            "branch": "codex/fixture", "objective": "Replace old with new",
            "acceptance": ["sample.txt contains new"], "owned_files": ["sample.txt"],
            "checks": [{"id": "content", "argv": [sys.executable, "-c",
                "from pathlib import Path; assert Path('sample.txt').read_text() == 'new\\n'"], "timeout": 5}],
            "worker_model": "gpt-5.6-luna", "worker_reasoning": "high", "plan": "derived-from-merged-pull-request",
            "luna_triage": False, "hidden_overlay": str(self.hidden),
        }
        self.auth = self.home / "codex-auth"
        self.auth.mkdir()
        (self.auth / "auth.json").write_text('{"tokens": "fixture"}')
        self.python = self.fake_environment()
        self.run_dir = self.home / "run"
        self.log = self.home / "attempts.jsonl"

    def fake_environment(self):
        """A venv whose python is a symlink chain into a standalone install, like the packet environments here."""
        install = self.home / "toolchain" / "cpython-3.12.14"
        (install / "bin").mkdir(parents=True)
        (install / "bin" / "python3.12").write_text("#!/bin/sh\n")
        (self.home / "toolchain" / "cpython-3.12").symlink_to(install)
        links = self.home / "local-bin"
        links.mkdir()
        (links / "python3.12").symlink_to(self.home / "toolchain" / "cpython-3.12" / "bin" / "python3.12")
        (links / "unrelated-tool").write_text("not for the worker\n")
        environment = self.home / "envs" / "fixture"
        (environment / "bin").mkdir(parents=True)
        (environment / "pyvenv.cfg").write_text(f"home = {links}\n")
        (environment / "bin" / "python3.12").symlink_to(links / "python3.12")
        (environment / "bin" / "python").symlink_to("python3.12")
        wrapper = environment / "bin" / "pysrc"
        wrapper.write_text(f'#!/bin/sh\nPYTHONPATH="$PWD" exec {environment}/bin/python "$@"\n')
        wrapper.chmod(0o755)
        return str(wrapper)

    def attempt(self, model, reviewer_findings=()):
        adapter = native.NativeAdapter(self.auth, self.python)
        def route(runner, role, feedback):
            if role == "worker":
                return adapter(runner, role, feedback)
            files = json.loads(feedback)["files"]
            return {"candidate": runner.state["candidate"], "covered_files": files,
                    "acceptance": runner.packet["acceptance"], "findings": list(reviewer_findings)}
        with patch.object(native, "run_container", model):
            return Runner(self.packet, self.run_dir, route, attempt_log=self.log).run()

    def logged(self):
        (line,) = [json.loads(text) for text in self.log.read_text().splitlines()]
        return line

    def test_the_diff_of_the_workers_copy_becomes_the_candidate_and_the_call_is_recorded(self):
        def edit(work):
            write_new(work)
            (work / "scratch.txt").write_text("notes\n")
            (work / "run.sh").write_text("#!/bin/sh\necho changed\n")
        model = FakeModel(edit)
        state = self.attempt(model)
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual((self.root / "sample.txt").read_text(), "new\n")
        # Changes outside the owned files are recorded, never applied, and do not fail the attempt.
        self.assertFalse((self.root / "scratch.txt").exists())
        self.assertEqual((self.root / "run.sh").read_text(), "#!/bin/sh\n")
        (record,) = state["native_calls"]
        self.assertEqual({key: record[key] for key in ("call", "ended", "exit_code", "tool_events", "context_tokens",
                                                        "context_window", "owned_changed", "other_changed",
                                                        "other_changed_count", "login_refreshed")},
                         {"call": 1, "ended": "itself", "exit_code": 0, "tool_events": 2, "context_tokens": 320,
                          "context_window": 258400, "owned_changed": ["sample.txt"], "other_changed": ["run.sh", "scratch.txt"],
                          "other_changed_count": 2, "login_refreshed": False})
        self.assertEqual(self.logged()["native_calls"], state["native_calls"])
        self.assertEqual(state["usage"][0]["reported"], [{"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 40}])
        self.assertTrue(model.saw_login)
        spec = model.specs[0]
        self.assertEqual(spec["name"], f"coding-loop-{state['attempt_id'][:12]}-call-1")
        self.assertEqual(spec["labels"]["coding-loop.attempt"], state["attempt_id"])
        self.assertEqual(spec["labels"]["coding-loop.packet"], "native-fixture")
        self.assertIn("codex:gpt-5.6-luna:high", spec["labels"]["coding-loop.lineup"])
        self.assertEqual(spec["user"], f"{os.getuid()}:{os.getgid()}")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", spec["argv"])
        # The CLI's whole install directory is mounted, so Codex finds the helper binary beside it.
        codex = Path(shutil.which("codex")).resolve()
        self.assertIn((str(codex.parent), native.CLI_DIR, "ro"), spec["mounts"])
        self.assertEqual(spec["argv"][0], native.CLI_DIR + "/" + codex.name)
        self.assertIn("/work", spec["stdin"].decode())
        self.assertEqual(spec["env"]["PATH"].split(":")[0], str(Path(self.python).parent))
        # The login copy does not outlive the call.
        home = next(Path(source) for source, target, _ in spec["mounts"] if target == native.HOME)
        self.assertFalse(home.exists())
        # The candidate's executable bit reached the copy.
        self.assertTrue(os.access(self.run_dir / "source-1" / "run.sh", os.X_OK))

    def test_the_container_sees_the_copy_and_the_environment_but_not_the_hidden_tests_or_checkout(self):
        model = FakeModel(write_new)
        self.attempt(model)
        spec = model.specs[0]
        sources = [Path(source) for source, _, _ in spec["mounts"]]
        writable = sorted(target for _, target, mode in spec["mounts"] if mode == "rw")
        self.assertEqual(writable, [native.HOME, native.WORK])
        for secret in (self.hidden, self.root, self.run_dir, self.auth):
            self.assertFalse(any(source == secret or secret in source.parents or source in secret.parents
                                 for source in sources if source != self.run_dir / "source-1"
                                 and not str(source).startswith(str(self.run_dir / "native-1" / "shims"))), secret)
        self.assertNotIn("hidden_overlay", spec["stdin"].decode())
        self.assertNotIn(str(self.hidden), spec["stdin"].decode())

    def test_the_python_environment_is_mounted_at_its_own_paths_with_its_links_and_nothing_beside_them(self):
        shims = self.home / "shims"
        mounts, bin_dir = native.python_mounts(self.python, [self.hidden], shims)
        targets = dict((target, source) for source, target in mounts)
        self.assertEqual(bin_dir, str(self.home / "envs" / "fixture" / "bin"))
        self.assertEqual(targets[str(self.home / "envs" / "fixture")], str(self.home / "envs" / "fixture"))
        self.assertEqual(targets[str(self.home / "toolchain" / "cpython-3.12.14")], str(self.home / "toolchain" / "cpython-3.12.14"))
        # ~/.local/bin-like directories are recreated with only the links on the way.
        local = Path(targets[str(self.home / "local-bin")])
        self.assertEqual([p.name for p in local.iterdir()], ["python3.12"])
        self.assertEqual(os.readlink(local / "python3.12"), str(self.home / "toolchain" / "cpython-3.12" / "bin" / "python3.12"))
        toolchain = Path(targets[str(self.home / "toolchain")])
        self.assertTrue((toolchain / "cpython-3.12").is_symlink())
        self.assertTrue((toolchain / "cpython-3.12.14").is_dir())  # mount point for the real install
        with self.assertRaisesRegex(RunnerError, "must not see"):
            native.python_mounts(self.python, [self.home / "envs"], self.home / "shims-2")
        with self.assertRaisesRegex(RunnerError, "virtual environment"):
            native.python_mounts("/bin/sh", [], self.home / "shims-3")

    def test_the_models_own_repository_starts_clean_and_keeps_executable_bits(self):
        source, git_dir = self.home / "copy", self.home / "copy.git"
        base = native.make_copy(self.root, source, git_dir)
        self.assertEqual(git(source, "status", "--porcelain"), b"")
        self.assertIn(b"100755", git(source, "ls-files", "-s", "run.sh"))
        (source / "run.sh").chmod(0o644)
        (source / "sample.txt").write_text("new\n")
        diff, mine, other = native.take_changes(git_dir, source, base, ["sample.txt", "run.sh"])
        # A mode change alone is not a change the loop takes.
        self.assertEqual((mine, other), (["sample.txt"], []))
        self.assertNotIn("mode", diff)

    def test_an_unchanged_copy_with_a_summary_is_a_blocker(self):
        state = self.attempt(FakeModel(message="Blocked: the owned files cannot express this"))
        self.assertEqual((state["phase"], state["stop_category"]), ("STOPPED", "no_patch"))
        self.assertIn("Blocked", state["reason"])
        self.assertEqual(self.logged()["reason"], "Worker returned no patch")
        self.assertEqual(state["native_calls"][0]["owned_changed"], [])

    def test_changes_outside_the_owned_files_only_are_a_blocker_and_are_recorded(self):
        state = self.attempt(FakeModel(lambda work: (work / "notes.md").write_text("x\n")))
        self.assertEqual(state["stop_category"], "no_patch")
        self.assertEqual(state["native_calls"][0]["other_changed"], ["notes.md"])

    def test_a_worker_killed_at_the_timeout_still_hands_over_its_changes(self):
        state = self.attempt(FakeModel(write_new, code=None, killed=True, events=codex_events(completed=False), message=None))
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        record = state["native_calls"][0]
        self.assertEqual((record["ended"], record["owned_changed"]), ("killed", ["sample.txt"]))
        # A killed call reports no turn.completed; the session log's running total stands in.
        self.assertEqual(state["usage"][0]["reported"], [{"input_tokens": 700, "output_tokens": 60}])

    def test_a_worker_killed_with_no_change_stops_on_the_time_cap(self):
        state = self.attempt(FakeModel(code=None, killed=True, events=codex_events(completed=False), message=None))
        self.assertEqual((state["phase"], state["stop_category"]), ("STOPPED", "budget_cap"))
        self.assertIn("timed out", state["reason"])
        self.assertEqual(state["native_calls"][0]["ended"], "killed")

    def test_a_failed_call_with_no_change_is_a_harness_failure_naming_the_provider_error(self):
        events = json.dumps({"type": "turn.failed", "error": {"message": "Selected model is at capacity"}}) + "\n"
        state = self.attempt(FakeModel(code=1, events=events, message=None))
        self.assertEqual((state["phase"], state["stop_category"]), ("STOPPED", "harness_failure"))
        self.assertIn("at capacity", state["reason"])
        self.assertEqual(state["native_calls"][0]["ended"], "failed")

    def test_the_loop_never_runs_git_through_the_copys_own_repository(self):
        marker = self.home / "escaped"
        hook = self.home / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        def tamper(work):
            write_new(work)
            with (work / ".git" / "config").open("a") as config:
                config.write(f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {self.home}\n")
            (work / ".gitattributes").write_text("* filter=evil\n")
        state = self.attempt(FakeModel(tamper))
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertFalse(marker.exists())
        self.assertEqual(state["native_calls"][0]["other_changed"], [".gitattributes"])

    def test_a_correction_round_starts_from_a_fresh_copy_of_the_corrected_candidate(self):
        self.packet["checks"][0]["argv"] = [sys.executable, "-c",
            "from pathlib import Path; assert Path('sample.txt').read_text() == 'newer\\n'"]
        seen = []
        def edit(work):
            seen.append((work.name, (work / "sample.txt").read_text()))
            (work / "sample.txt").write_text("new\n" if len(seen) == 1 else "newer\n")
        state = self.attempt(FakeModel(edit))
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(seen, [("source-1", "old\n"), ("source-2", "new\n")])
        self.assertEqual([r["call"] for r in state["native_calls"]], [1, 2])

    def test_a_meta_worker_gets_the_meta_key_and_no_chatgpt_login(self):
        login = self.home / "xdg" / "muse"
        login.mkdir(parents=True)
        (login / "auth.json").write_text(json.dumps({"providers": {"meta": {"api_key": "fixture-key"}}}))
        self.packet["worker_model"] = "meta/muse-spark-1.3-contributor"
        seen = {}
        def edit(work):
            write_new(work)
        model = FakeModel(edit)
        original = model.__call__
        def call(runner, spec):
            home = next(Path(source) for source, target, _ in spec["mounts"] if target == native.HOME)
            seen["config"] = (home / ".codex" / "config.toml").read_text()
            seen["auth"] = (home / ".codex" / "auth.json").exists()
            seen["model"] = spec["argv"][spec["argv"].index("--model") + 1]
            return original(runner, spec)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / "xdg")}):
            state = self.attempt(call)
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertIn('model_provider = "meta"', seen["config"])
        self.assertIn("fixture-key", seen["config"])
        self.assertFalse(seen["auth"])
        self.assertEqual(seen["model"], "muse-spark-1.3-contributor")

    def test_a_claude_worker_gets_only_its_oauth_tokens_and_no_permission_prompts(self):
        claude_dir = self.home / "claude-config"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}, "mcpOAuth": {"x": 1}}))
        (claude_dir / ".claude.json").write_text(json.dumps({"userID": "u", "projects": {"/secret": {}}}))
        self.packet.update(worker_model="claude-opus-5-5", worker_reasoning="high")
        seen = {}
        def call(runner, spec):
            home = next(Path(source) for source, target, _ in spec["mounts"] if target == native.HOME)
            seen["credentials"] = json.loads((home / ".claude" / ".credentials.json").read_text())
            seen["state"] = json.loads((home / ".claude.json").read_text())
            seen["argv"] = spec["argv"]
            write_new(next(Path(s) for s, t, _ in spec["mounts"] if t == native.WORK))
            events = [{"type": "assistant", "message": {"content": [{"type": "tool_use"}, {"type": "text"}],
                                                        "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}}},
                      {"type": "result", "subtype": "success", "is_error": False, "result": "Done",
                       "usage": {"input_tokens": 5, "output_tokens": 7}}]
            (runner.run_dir / (spec["log"] + ".log")).write_text("".join(json.dumps(e) + "\n" for e in events))
            return 0, False
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir)}):
            state = self.attempt(call)
        self.assertEqual(state["phase"], "LOCAL_REVIEWED", state)
        self.assertEqual(seen["credentials"], {"claudeAiOauth": {"accessToken": "a"}})
        self.assertEqual(seen["state"], {"userID": "u", "hasCompletedOnboarding": True})
        self.assertIn("--dangerously-skip-permissions", seen["argv"])
        record = state["native_calls"][0]
        self.assertEqual((record["ended"], record["tool_events"], record["context_tokens"]), ("itself", 1, 112))


@unittest.skipUnless(sys.platform.startswith("linux") and docker_ready(), "needs Docker and the local worker image")
class ContainerTests(unittest.TestCase):
    """The real container, with a shell script for the CLI and no network at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        network = patch.object(native, "NETWORK", "none")
        network.start()
        self.addCleanup(network.stop)
        self.work = self.home / "work"
        self.work.mkdir()
        (self.work / "sample.txt").write_text("old\n")
        self.agent = self.home / "agent"
        self.agent.mkdir()
        self.run_dir = self.home / "run"
        self.run_dir.mkdir()

    def runner(self, timeout=60):
        runner = Runner.__new__(Runner)
        runner.run_dir, runner.lock_fd, runner.state = self.run_dir, os.open(os.devnull, os.O_RDONLY), {}
        self.addCleanup(os.close, runner.lock_fd)
        runner.remaining = lambda: timeout
        runner.checkpoint = lambda **updates: runner.state.update(updates)
        return runner

    def spec(self, script, timeout=60):
        cli = self.home / "cli"
        cli.write_text("#!/bin/sh\n" + script)
        cli.chmod(0o755)
        name = "coding-loop-test-" + os.urandom(4).hex()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", name], capture_output=True)
        owner = self.work.stat()
        return {"name": name, "log": "model-1", "stdin": b"prompt\n", "argv": [native.CLI_DIR + "/cli"], "timeout": timeout,
                "user": f"{owner.st_uid}:{owner.st_gid}", "labels": {"coding-loop.attempt": "test"},
                "env": {"HOME": native.HOME}, "mounts": [(str(self.work), native.WORK, "rw"), (str(self.agent), native.HOME, "rw"),
                                                         (str(self.home), native.CLI_DIR, "ro")]}

    def test_the_cli_edits_the_copy_as_the_owner_and_sees_nothing_else(self):
        spec = self.spec('cat > "$HOME/prompt"; echo new > sample.txt; id -u > uid; ls /home > "$HOME/root"; '
                         'python3 -c "import socket; socket.create_connection((\'1.1.1.1\', 443), 3)" 2> "$HOME/net"; '
                         'echo \'{"type":"turn.completed"}\'\n')
        code, killed = native.run_container(self.runner(), spec)
        self.assertEqual((code, killed), (0, False))
        self.assertEqual((self.work / "sample.txt").read_text(), "new\n")
        self.assertEqual((self.work / "uid").read_text().strip(), str(os.getuid()))
        self.assertEqual((self.work / "sample.txt").stat().st_uid, os.getuid())
        self.assertEqual((self.agent / "prompt").read_text(), "prompt\n")
        self.assertIn("turn.completed", (self.run_dir / "model-1.log").read_text())
        self.assertEqual((self.agent / "root").read_text().split(), ["agent"])
        self.assertIn("unreachable", (self.agent / "net").read_text().lower())

    def test_a_timeout_kills_the_container_itself(self):
        spec = self.spec("echo started; sleep 300\n", timeout=3)
        code, killed = native.run_container(self.runner(timeout=3), spec)
        self.assertEqual((code, killed), (None, True))
        listed = subprocess.run(["docker", "ps", "-a", "-q", "--filter", "name=" + spec["name"]],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(listed, "")


if __name__ == "__main__":
    unittest.main()
