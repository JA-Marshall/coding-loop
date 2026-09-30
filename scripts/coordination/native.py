"""Native worker calls: the model edits a copy of the candidate with its own tools, inside a Docker container.

Every model is trained to edit files with its own tools, not to hand-write a diff inside a JSON
answer. So a worker call here gets a writable copy of the candidate (source-N in the run
directory, committed in a Git repository of its own), and the loop takes `git diff` of that copy
when the call ends, whether the model ended its turn itself or was killed at the call timeout.
Only changes to owned files are kept; they reach the real checkout through the runner's
apply_patch, with its path, symlink and mode guards. Other changed paths are recorded, never
applied.

Only the model CLI runs in the container, as the user who owns the copy. It sees:
  /work         the copy, read-write
  /home/agent   a per-call home holding a copy of the CLI's login, deleted when the call ends
  /opt/loop/bin the CLI's own install directory, read-only
  the packet's Python environment, read-only at its own paths, to run the visible tests
and nothing else: never the hidden tests, the checkout (whose history holds the merged change),
the run directory or the real logins. The container's only network is an internal one whose
single way out is an allowlisting proxy to the model providers. Container isolation replaces
each CLI's own sandbox, which is therefore off inside it.

The loop, the hidden checks and the reviewer stay outside, as before. Build the image once:

    python3 -m scripts.coordination.native build-image
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time

from .isolated import IsolatedAdapter, META_PREFIX, meta_provider, snapshot
from .runner import (CLAUDE_SETTINGS, RunnerError, canonical, claude_usage, codex_model, is_claude, is_muse,
                     record_prompt, shown_packet)

IMAGE = "coding-loop-worker:1"
IMAGE_DIR = Path(__file__).with_name("container")
WORK = "/work"
HOME = "/home/agent"
# The CLI's own install directory, read-only: Codex runs a helper binary that sits beside it.
CLI_DIR = "/opt/loop/bin"
NETWORK = "coding-loop-egress"
PROXY_PORT = 3128
# The model providers' API and login hosts; the proxy refuses every other destination.
PROVIDER_HOSTS = ("chatgpt.com", "auth.openai.com", "api.openai.com", "api.meta.ai",
                  "api.anthropic.com", "platform.claude.com", "console.anthropic.com")
LABEL = "coding-loop"
# A backstop inside the container: even if the loop dies, the CLI is killed this long after its timeout.
KILL_MARGIN = 60
# Places a packet's Python environment is never taken from: the image has its own system.
SYSTEM_DIRS = ("/bin", "/sbin", "/lib", "/lib64", "/usr", "/etc", "/proc", "/sys", "/dev", "/opt")
OTHER_PATHS_KEPT = 50
TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search")


def docker(*args, timeout=60, check=True):
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError("Docker unavailable or timed out: " + args[0]) from exc
    if check and result.returncode:
        raise RunnerError(f"Docker {args[0]} failed: " + " ".join(result.stderr.split())[-300:])
    return result


def proxy_name():
    """The egress proxy's container name; it changes with the proxy's code or allowlist, so a stale one is never reused."""
    h = hashlib.sha256((IMAGE_DIR / "egress_proxy.py").read_bytes() + canonical(PROVIDER_HOSTS).encode() + IMAGE.encode())
    return "coding-loop-egress-proxy-" + h.hexdigest()[:10]


def ensure_egress():
    """The internal network worker containers join, and the one proxy on it that reaches the providers. Idempotent."""
    if docker("image", "inspect", IMAGE, check=False).returncode:
        raise RunnerError(f"Worker image {IMAGE} is missing; run python3 -m scripts.coordination.native build-image")
    if docker("network", "inspect", NETWORK, check=False).returncode:
        made = docker("network", "create", "--internal", "--label", LABEL + ".role=egress-network", NETWORK, check=False)
        if made.returncode and "already exists" not in made.stderr:
            raise RunnerError("Could not create the worker network: " + " ".join(made.stderr.split())[-300:])
    name = proxy_name()
    if docker("inspect", "-f", "{{.State.Running}}", name, check=False).stdout.strip() != "true":
        docker("rm", "-f", name, check=False)
        started = docker("run", "-d", "--pull", "never", "--name", name, "--restart", "unless-stopped",
                         "--label", LABEL + ".role=egress-proxy", "--user", "65534:65534", "--cap-drop", "ALL",
                         "--security-opt", "no-new-privileges", "-v", f"{IMAGE_DIR / 'egress_proxy.py'}:/proxy.py:ro",
                         IMAGE, "python3", "/proxy.py", str(PROXY_PORT), *PROVIDER_HOSTS, check=False)
        if started.returncode and "already in use" not in started.stderr:
            raise RunnerError("Could not start the egress proxy: " + " ".join(started.stderr.split())[-300:])
    joined = docker("network", "connect", NETWORK, name, check=False)
    if joined.returncode and "already exists" not in joined.stderr:
        raise RunnerError("Could not attach the egress proxy: " + " ".join(joined.stderr.split())[-300:])
    return name


def build_image():
    docker("build", "-t", IMAGE, str(IMAGE_DIR), timeout=1800)
    return IMAGE


# --- the copy -------------------------------------------------------------------------------------------------

def base_git(git_dir, work, *args, data=None, timeout=120, file_mode=False):
    """Git on the copy through the loop's own repository, kept outside the copy.

    The model can rewrite /work/.git, including hooks or config that would run a program, so the
    loop never uses it. No user or system config either, so .gitattributes in the copy can name
    no filter that exists here. Mode changes are ignored (file_mode=False) unless asked for.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(git_dir), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "LANG": "C"}
    try:
        result = subprocess.run(
            ["git", "--git-dir=" + str(git_dir), "--work-tree=" + str(work), "-c", "core.fsmonitor=false",
             "-c", "core.hooksPath=/dev/null", "-c", "core.fileMode=" + str(file_mode).lower(), "-c", "core.quotePath=false",
             "-c", "user.name=coding-loop", "-c", "user.email=coding-loop@invalid", "-c", "commit.gpgSign=false",
             *args], input=data, cwd=work, env=env, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError("Git on the worker copy unavailable or timed out") from exc
    if result.returncode:
        raise RunnerError("Git on the worker copy failed: " + args[0])
    return result.stdout


def make_copy(root, source, git_dir):
    """A writable copy of the candidate at source, committed twice: for the model in source/.git, and for the loop in git_dir."""
    snapshot(root, source)
    base_git(git_dir, source, "init", "-q")
    for repository in (source / ".git", git_dir):
        # The model's repository records the files' real modes, so its own `git status` starts clean.
        mode = repository != git_dir
        base_git(repository, source, "add", "-A", "-f", file_mode=mode)
        base_git(repository, source, "commit", "-q", "--allow-empty", "--no-verify", "-m", "Candidate before this call",
                 file_mode=mode)
    return base_git(git_dir, source, "rev-parse", "HEAD").decode().strip()


def take_changes(git_dir, source, base, owned):
    """What the call changed: the owned files' diff, and the other paths changed, which are recorded, never applied."""
    base_git(git_dir, source, "add", "-A")
    present = [name for name in owned if (source / name).exists() or (source / name).is_symlink()]
    if present:
        base_git(git_dir, source, "add", "-f", "--", *present)
    changed = [os.fsdecode(n) for n in base_git(git_dir, source, "diff", "--cached", "--no-renames", "--name-only",
                                                "-z", base).split(b"\0") if n]
    mine = sorted(name for name in changed if name in owned)
    other = sorted(name for name in changed if name not in owned)
    diff = base_git(git_dir, source, "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv",
                    "--binary", base, "--", *mine).decode("utf-8", "replace") if mine else ""
    return diff, mine, other


# --- what the container sees ----------------------------------------------------------------------------------

def link_chain(path, links, depth=0):
    """The real path of path, resolved one component at a time; every symbolic link met is recorded in links."""
    if depth > 40:
        raise RunnerError("Interpreter path has too many symbolic links")
    current = Path("/")
    for part in Path(path).parts[1:]:
        candidate = current / part
        if candidate.is_symlink():
            text = os.readlink(candidate)
            links[str(candidate)] = text
            current = link_chain(Path(os.path.normpath(candidate.parent / text)), links, depth + 1)
        else:
            current = candidate
    return current


def inside(path, directory):
    path, directory = PurePosixPath(path), PurePosixPath(directory)
    return path == directory or directory in path.parents


def python_mounts(interpreter, forbidden, shims):
    """Read-only mounts that make the packet's interpreter run at its own path in the container.

    The virtual environment and the interpreter it was made from are mounted at their real paths.
    Symbolic links on the way, such as bin/python3.12 -> ~/.local/bin/python3.12, are recreated in
    small directories under shims, mounted where the links were, so nothing else in those
    directories is exposed. Returns ([(source, target)], the environment's bin directory).
    """
    interpreter = Path(interpreter).absolute()
    if not interpreter.is_file():
        raise RunnerError("Packet interpreter not found for the worker container")
    executables = [interpreter]
    head = interpreter.read_bytes()[:4096]
    if head.startswith(b"#!"):
        # A wrapper script such as pysrc: the interpreters it names by absolute path are needed too.
        for text in re.findall(r"/[^\s\"'$;:]+", head.decode("utf-8", "replace")):
            if Path(text).is_file() and not any(inside(text, d) for d in SYSTEM_DIRS):
                executables.append(Path(text))
    links, roots = {}, set()
    for executable in executables:
        link_chain(executable, links)
        environment = next((p for p in executable.parents if (p / "pyvenv.cfg").is_file()), None)
        if environment is None:
            raise RunnerError("The packet interpreter must belong to a virtual environment")
        roots.add(link_chain(environment, links))
        base = link_chain(environment / "bin" / "python", links)
        roots.add(base.parent.parent)
    for root in roots:
        if len(root.parts) < 3 or any(inside(root, d) for d in SYSTEM_DIRS):
            raise RunnerError("The packet interpreter lives in a system directory; use a virtual environment")
        for path in forbidden:
            if inside(root, path) or inside(path, root):
                raise RunnerError("The packet interpreter shares a directory with something the worker must not see")
    mounts = [(str(root), str(root)) for root in sorted(roots)]
    groups = {}
    for link, text in links.items():
        parent = str(PurePosixPath(link).parent)
        if not any(inside(parent, root) for root in roots):
            groups.setdefault(parent, {})[PurePosixPath(link).name] = text
    for index, (parent, entries) in enumerate(sorted(groups.items())):
        shim = shims / str(index)
        shim.mkdir(parents=True)
        for name, text in entries.items():
            os.symlink(text, shim / name)
        mounts.append((str(shim), parent))
    # A mount inside a shim needs its mount point there, since the shim is mounted read-only.
    for source, target in mounts:
        for shim, parent in mounts:
            if shim.startswith(str(shims)) and target != parent and inside(target, parent):
                (Path(shim) / PurePosixPath(target).relative_to(parent)).mkdir(parents=True, exist_ok=True)
    return mounts, str(interpreter.parent)


def login_root():
    """A private per-user directory in memory for per-call login copies: nothing is left on disk, even after a crash."""
    base = Path("/dev/shm") if Path("/dev/shm").is_dir() else None
    if base is None:
        return None
    directory = base / f"coding-loop-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None


def codex_home(home, model, auth_home):
    """Per-call CODEX_HOME inside the container home: a copy of the login and a config with the CLI's sandbox off."""
    directory = home / ".codex"
    directory.mkdir(mode=0o700)
    (directory / "config.toml").write_text(
        'approval_policy = "never"\nsandbox_mode = "danger-full-access"\nweb_search = "disabled"\n'
        '[features]\napps = false\nplugins = false\nmulti_agent = false\nhooks = false\n')
    auth = Path(auth_home) / "auth.json"
    if not auth.is_file():
        raise RunnerError("Codex file login unavailable for the worker container")
    shutil.copyfile(auth, directory / "auth.json")
    os.chmod(directory / "auth.json", 0o600)
    if model.startswith(META_PREFIX):
        # The Meta key goes in the call's own config and the ChatGPT login is not sent along.
        meta_provider(directory)
        return None  # an API key in the call's config: nothing to refresh
    return directory / "auth.json"


def claude_login():
    directory = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    credentials = directory / ".credentials.json"
    state = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR") \
        else Path.home() / ".claude.json"
    try:
        oauth = json.loads(credentials.read_text())["claudeAiOauth"]
        account = json.loads(state.read_text()) if state.is_file() else {}
    except (OSError, ValueError, KeyError, TypeError):
        raise RunnerError("Claude login unavailable for the worker container") from None
    return oauth, {key: account[key] for key in ("oauthAccount", "userID") if key in account}


def claude_home(home):
    """Per-call Claude login in the container home: the OAuth tokens only, none of the owner's projects or history."""
    oauth, account = claude_login()
    directory = home / ".claude"
    directory.mkdir(mode=0o700)
    credentials = directory / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": oauth}))
    os.chmod(credentials, 0o600)
    (home / ".claude.json").write_text(json.dumps(dict(account, hasCompletedOnboarding=True)))
    os.chmod(home / ".claude.json", 0o600)
    return credentials


# --- the call -------------------------------------------------------------------------------------------------

def container_name(runner, number):
    return f"coding-loop-{runner.state['attempt_id'][:12]}-call-{number}"


def stop_container(name):
    """Kill the container itself, not just the local docker client, and wait until it is gone."""
    docker("kill", name, check=False, timeout=30)
    for _ in range(30):
        docker("rm", "-f", name, check=False, timeout=30)
        if not docker("ps", "-a", "-q", "--filter", f"name=^{name}$", check=False, timeout=30).stdout.strip():
            return
        time.sleep(1)
    raise RunnerError("Worker container " + name + " could not be removed; kill it by name")


def run_container(runner, spec):
    """Run one model call in its container, logging to model-N.log; returns (exit code, killed at the timeout).

    Tests replace this function, so they need no Docker and never reach a model CLI.
    """
    if any(":" in path or "," in path for mount in spec["mounts"] for path in mount[:2]):
        raise RunnerError("A worker container mount path holds ':' or ','")
    argv = ["docker", "run", "--rm", "--init", "-i", "--pull", "never", "--name", spec["name"],
            *(arg for key, value in spec["labels"].items() for arg in ("--label", f"{key}={value}")),
            "--user", spec["user"], "--network", NETWORK, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--workdir", WORK,
            *(arg for key, value in spec["env"].items() for arg in ("-e", f"{key}={value}")),
            *(arg for source, target, mode in spec["mounts"] for arg in ("-v", f"{source}:{target}:{mode}")),
            IMAGE, "timeout", "-s", "KILL", str(int(spec["timeout"]) + KILL_MARGIN), *spec["argv"]]
    killed = False
    try:
        code = runner.command(argv, spec["timeout"], spec["log"], spec["stdin"], cwd=runner.run_dir,
                              container=spec["name"])
    except subprocess.TimeoutExpired:
        code, killed = None, True
    finally:
        stop_container(spec["name"])
    if killed:
        # A worker's timeout ends the call, not the run: the diff is taken next.
        runner.checkpoint(inflight=None)
    return code, killed


WORKER_PROMPT = """
WORKING COPY: {work} is a writable copy of the current candidate, a Git repository whose last
commit is the candidate as it stands. Edit files there with your own tools. You may run commands
and the visible tests; the packet's Python environment is `{python}` (for example
`{python} -m pytest -q PATH`). There is no network apart from your model provider.
When your turn ends, the supervisor takes `git diff` of this copy and keeps changes to the owned
files only; other changes are discarded. Do not commit. Finish with a short summary of what you
changed. If the task cannot be done within the frozen scope, change nothing and give the blocker
as your summary.
"""


def read_events(log):
    events = []
    if log.is_file() and log.stat().st_size <= 64_000_000:
        for line in log.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def codex_record(events, home):
    """Usage, tool events, end-of-call context and any provider error, from Codex's --json events and its session log."""
    usage = [e.get("usage") for e in events if e.get("type") == "turn.completed"]
    tools = sum(1 for e in events if e.get("type") == "item.completed"
                and isinstance(e.get("item"), dict) and e["item"].get("type") in TOOL_ITEMS)
    errors = [e.get("message") or (e.get("error") or {}).get("message") for e in events
              if e.get("type") in ("error", "turn.failed")]
    context, window, total = None, None, None
    for log in sorted((home / ".codex" / "sessions").rglob("*.jsonl")):
        for line in log.read_text(errors="replace").splitlines():
            try:
                payload = json.loads(line).get("payload") or {}
            except (ValueError, AttributeError):
                continue
            info = payload.get("info") if payload.get("type") == "token_count" else None
            if isinstance(info, dict):
                last = info.get("last_token_usage") or {}
                context = last.get("total_tokens", context)
                window = info.get("model_context_window", window)
                total = info.get("total_token_usage", total)
    if not usage and isinstance(total, dict):
        # A killed call never reports turn.completed; its session log still has the running total.
        usage = [total]
    error = next((" ".join(str(m).split())[:300] for m in errors if m), None)
    return usage, tools, context, window, error, any(e.get("type") == "turn.completed" for e in events)


def claude_record(events):
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    tools, context = 0, None
    for event in events:
        message = event.get("message") if event.get("type") == "assistant" else None
        if isinstance(message, dict):
            tools += sum(1 for block in message.get("content") or [] if isinstance(block, dict) and block.get("type") == "tool_use")
            usage = message.get("usage")
            if isinstance(usage, dict):
                context = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens", "output_tokens")
                              if type(usage.get(k, 0)) is int)
    return result, tools, context


def native_worker(runner, feedback, auth_home, python):
    number = runner.state["calls"]
    model, effort = runner.packet["worker_model"], runner.packet["worker_reasoning"]
    source, private = runner.run_dir / f"source-{number}", runner.run_dir / f"native-{number}"
    private.mkdir(mode=0o700)
    base = make_copy(runner.root, source, private / "base.git")
    prompt = (Path(__file__).with_name("worker-native.md").read_text()
              + WORKER_PROMPT.format(work=WORK, python=python)
              + "\nPACKET:\n" + canonical(dict(shown_packet(runner.packet), checkout=WORK))
              + "\nEVIDENCE:\n" + feedback)
    record_prompt(runner, number, "worker", prompt, native=True)
    name = container_name(runner, number)
    logins = login_root()
    home = (logins / name) if logins else private / "home"
    home.mkdir(mode=0o700)
    forbidden = [runner.root, runner.run_dir, Path.home() / ".codex", Path.home() / ".claude",
                 Path.home() / ".config"] + ([Path(runner.packet["hidden_overlay"])] if "hidden_overlay" in runner.packet else [])
    started = time.time()
    try:
        mounts, bin_dir = python_mounts(python, forbidden, private / "shims")
        claude = is_claude(model)
        executable = shutil.which("claude" if claude else "codex")
        if not executable:
            raise RunnerError(("Claude" if claude else "Codex") + " executable is not on PATH")
        executable = Path(executable).resolve()
        cli = f"{CLI_DIR}/{executable.name}"
        if claude:
            login = claude_home(home)
            argv = [cli, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
                    "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                    "--disallowed-tools", "WebFetch,WebSearch", "--settings", canonical(CLAUDE_SETTINGS),
                    "--model", model, "--effort", effort]
        else:
            login = codex_home(home, model, auth_home)
            argv = [cli, "exec", "--json", "--strict-config", "--dangerously-bypass-approvals-and-sandbox",
                    "--cd", WORK, "--model", model[len(META_PREFIX):] if model.startswith(META_PREFIX) else model,
                    "-c", f'model_reasoning_effort="{effort}"', "-o", HOME + "/last-message.txt", "-"]
        before = file_digest(login)
        owner = source.stat()
        pair = runner.state.get("pair") or {}
        spec = {
            "name": name, "log": f"model-{number}", "stdin": prompt.encode(), "argv": argv,
            "timeout": min(runner.packet["call_timeout"], runner.remaining()),
            "user": f"{owner.st_uid}:{owner.st_gid}",
            "labels": {LABEL + ".attempt": runner.state["attempt_id"], LABEL + ".packet": runner.packet["id"],
                       LABEL + ".lineup": f"{pair.get('worker')} -> {pair.get('reviewer')}",
                       LABEL + ".call": str(number), LABEL + ".run-dir": str(runner.run_dir)},
            "env": {"HOME": HOME, "PATH": bin_dir + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "HTTPS_PROXY": f"http://{proxy_name()}:{PROXY_PORT}", "https_proxy": f"http://{proxy_name()}:{PROXY_PORT}",
                    "NO_PROXY": "localhost,127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0",
                    "CODEX_HOME": HOME + "/.codex", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_AUTOUPDATER": "1"},
            "mounts": [(str(source), WORK, "rw"), (str(home), HOME, "rw"), (str(executable.parent), CLI_DIR, "ro")]
                      + [(s, t, "ro") for s, t in mounts],
        }
        ensure_egress()
        code, killed = run_container(runner, spec)
        # Whether the CLI refreshed the copied OAuth tokens; the real login was not updated.
        refreshed = login is not None and file_digest(login) != before
        events = read_events(runner.run_dir / f"model-{number}.log")
        if claude:
            result, tools, context = claude_record(events)
            usage = [claude_usage(result["usage"])] if result and isinstance(result.get("usage"), dict) else []
            summary = result.get("result") if result else None
            window, error = None, (result.get("result") if result and result.get("is_error") else None)
            ended_itself = result is not None and not result.get("is_error")
        else:
            usage, tools, context, window, error, ended_itself = codex_record(events, home)
            message = home / "last-message.txt"
            summary = message.read_text(errors="replace") if message.is_file() else None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    diff, mine, other = take_changes(private / "base.git", source, base, runner.packet["owned_files"])
    ended = "killed" if killed else "itself" if ended_itself and code == 0 else "failed"
    record = {"call": number, "container": name, "ended": ended, "exit_code": code, "seconds": round(time.time() - started, 1),
              "tool_events": tools, "context_tokens": context, "context_window": window,
              "owned_changed": mine, "other_changed": other[:OTHER_PATHS_KEPT], "other_changed_count": len(other),
              "login_refreshed": refreshed}
    runner.checkpoint(native_calls=runner.state.get("native_calls", []) + [record],
                      usage=runner.state.get("usage", []) + [{"call": number, "role": "worker", "reported": usage}])
    if not diff:
        if killed:
            raise RunnerError("Worker call timed out with no change to the owned files", category="budget_cap")
        if ended != "itself":
            raise RunnerError("Native worker call failed" + (": " + error if error else "; inspect private model log"))
    if ended == "itself" and not usage:
        raise RunnerError("Model usage missing; stop rather than lose batch accounting")
    summary = summary if isinstance(summary, str) and summary.strip() else "(no summary: the call " + ended + ")"
    return {"patch": diff, "summary": summary[:20_000], "native": True}


class NativeAdapter(IsolatedAdapter):
    """Worker calls edit a copy in a container; every other role, and a Muse Code worker, runs as IsolatedAdapter runs it."""

    def __init__(self, auth_home, python):
        super().__init__(auth_home)
        self.python = python

    def __call__(self, runner, role, feedback):
        if role != "worker" or is_muse(runner.packet["worker_model"]):
            return super().__call__(runner, role, feedback)
        return native_worker(runner, feedback, self.auth_home, self.python)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build-image", help=f"build {IMAGE} from {IMAGE_DIR}")
    commands.add_parser("egress", help="start the worker network and its provider-only proxy, if not running")
    args = parser.parse_args(argv)
    try:
        print(build_image() if args.command == "build-image" else ensure_egress())
    except RunnerError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
