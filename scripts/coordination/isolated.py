"""Source snapshot + explicit read permissions for unattended Codex, Claude and Muse calls."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
from .runner import (REVIEW_MODEL, REVIEW_SCHEMA, ROLE_SCHEMAS, ROLE_TEMPLATES, RunnerError, canonical,
                     claude_call, claude_worker, codex_model, git, is_claude, is_muse, record_prompt, save_json,
                     shown_packet)


def snapshot(root, destination):
    """Copy tracked/current candidate source only; never copy ignored local data."""
    destination.mkdir(mode=0o700)
    names = set(git(root, "ls-files", "-z").split(b"\0"))
    names.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"))
    for raw in sorted(names - {b""}):
        name = os.fsdecode(raw)
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            raise RunnerError("Invalid snapshot path")
        # Project agent configuration and hooks cannot override runtime isolation.
        if (p.parts[0] in {".codex", ".agents", ".claude", ".git", "CLAUDE.md"}
                or any(x.startswith(".env") for x in p.parts)):
            continue
        source = root / name
        if source.is_symlink():
            raise RunnerError("Snapshot symlink requires manual handling")
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    git(destination, "init", "-q")


def config_text(source):
    executable = shutil.which("codex")
    if not executable:
        raise RunnerError("Codex executable is not on PATH")
    return ('approval_policy = "never"\n'
            'default_permissions = "runner"\n'
            'web_search = "disabled"\n'
            'allow_login_shell = false\n'
            '[features]\napps = false\nplugins = false\nmulti_agent = false\nhooks = false\n'
            'shell_snapshot = false\n'
            '[permissions.runner.filesystem]\n":minimal" = "read"\n'
            + json.dumps(str(Path(executable).resolve())) + ' = "read"\n'
            + json.dumps(str(source)) + ' = "read"\n'
            '[permissions.runner.network]\nenabled = false\n')


def runtime_home(destination, source, auth_home):
    destination.mkdir(mode=0o700)
    auth = Path(auth_home) / "auth.json"
    if not auth.is_file():
        raise RunnerError("Codex file login unavailable for isolated runtime")
    shutil.copyfile(auth, destination / "auth.json")
    os.chmod(destination / "auth.json", 0o600)
    (destination / "config.toml").write_text(config_text(source))
    os.chmod(destination / "config.toml", 0o600)
    return destination


def muse_binary():
    """The installed Muse Code binary, not its launcher: the launcher updates itself over the network."""
    launcher = shutil.which("muse")
    if not launcher:
        raise RunnerError("Muse executable is not on PATH")
    launcher = Path(launcher).resolve()
    version = launcher.with_name(".muse-version")
    pinned = launcher.with_name("muse-bin-" + version.read_text().strip()) if version.is_file() else launcher
    return pinned if pinned.is_file() else launcher


def muse_config_text(source, home, binary):
    """Codex sandbox profile for one Muse call: read the snapshot and the binaries, write only its own home.

    Muse's file tools are not confined to its workspace, so the read boundary is the sandbox around
    the whole process. The network stays on because the model is served remotely; the call is given
    no shell and no web tool to reach anything else with.
    """
    executable = shutil.which("codex")
    if not executable:
        raise RunnerError("Codex executable is not on PATH")
    readable = [Path(executable).resolve(), binary, source]
    resolver = Path("/etc/resolv.conf").resolve()
    if resolver.is_file() and Path("/etc") not in resolver.parents:
        readable.append(resolver)  # WSL keeps the resolver file outside /etc; without it no name resolves.
    return ('approval_policy = "never"\n'
            'default_permissions = "muse"\n'
            '[permissions.muse.filesystem]\n":minimal" = "read"\n'
            + "".join(json.dumps(str(path)) + ' = "read"\n' for path in readable)
            + json.dumps(str(home)) + ' = "write"\n'
            '[permissions.muse.network]\nenabled = true\n')


def muse_login():
    """The Muse Code login file, which holds the Meta API key both Muse routes are billed to."""
    login = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "muse" / "auth.json"
    if not login.is_file():
        raise RunnerError("Muse login unavailable for isolated runtime")
    return login


META_PREFIX = "meta/"
META_PROVIDER = ('[model_providers.meta]\nname = "Meta"\nbase_url = "https://api.meta.ai/v1"\n'
                 'wire_api = "responses"\nexperimental_bearer_token = ')


def meta_provider(home):
    """Point one Codex runtime at Meta's API, for a Muse model run in Codex's harness (worker model "meta/<id>").

    The key goes in the runtime's private config, which the sandboxed commands cannot read, and is
    taken out again by scrub_meta_provider when the call ends. The ChatGPT login is not needed and
    is not left beside a request to another provider.
    """
    try:
        key = json.loads(muse_login().read_text())["providers"]["meta"]["api_key"]
    except (ValueError, KeyError, TypeError):
        key = None
    if not isinstance(key, str) or not key:
        raise RunnerError("Muse login holds no Meta API key")
    config = home / "config.toml"
    config.write_text('model_provider = "meta"\n' + config.read_text() + META_PROVIDER + json.dumps(key) + "\n")
    (home / "auth.json").unlink()


def scrub_meta_provider(home):
    config = home / "config.toml"
    config.write_text("".join(line for line in config.read_text().splitlines(keepends=True)
                              if not line.startswith("experimental_bearer_token")))


def muse_home(destination, source, binary, prompt, schema):
    """A runtime of its own for one Muse call: no memories, rules or sessions from the owner's home."""
    login = muse_login()
    home = destination / "home"
    config = home / ".config" / "muse"
    config.mkdir(mode=0o700, parents=True)
    os.chmod(destination, 0o700)
    shutil.copyfile(login, config / "auth.json")
    os.chmod(config / "auth.json", 0o600)
    save_json(config / "settings.json", {"schema_version": 1, "provider": "meta"})
    (home / "prompt.txt").write_text(prompt)
    save_json(home / "schema.json", schema)
    (destination / "config.toml").write_text(muse_config_text(source, home, binary))
    os.chmod(destination / "config.toml", 0o600)
    return home


def muse_usage(home):
    """Tokens of every model step Muse logged for the call, its helper sessions included, as one Codex-shaped entry."""
    keys = {"input_tokens": "input_tokens", "cached_tokens": "cached_input_tokens", "output_tokens": "output_tokens",
            "reasoning_tokens": "reasoning_output_tokens"}
    total, steps = dict.fromkeys(keys.values(), 0), 0
    for log in sorted((home / ".local" / "share" / "muse" / "sessions").rglob("session.jsonl")):
        for line in log.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)["payload"]["event"]
            except (ValueError, KeyError, TypeError):
                continue
            usage = event.get("usage") if isinstance(event, dict) and event.get("kind") == "model_completed" else None
            if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                                                  for k in ("input_tokens", "output_tokens")):
                continue
            steps += 1
            for theirs, ours in keys.items():
                total[ours] += usage[theirs] if type(usage.get(theirs)) is int else 0
    return dict(total, model_steps=steps) if steps else None


def muse_worker(runner, number, prompt, source, schema):
    """Implementation call through Muse Code, the whole process inside a Codex sandbox."""
    binary, destination = muse_binary(), runner.run_dir / f"muse-{number}"
    home = muse_home(destination, source, binary, prompt, schema)
    argv = ["env", "HOME=" + str(home), "CODEX_HOME=" + str(destination), "codex", "sandbox", "-P", "muse",
            "-C", str(source), "--", str(binary), "exec", "--json", "--workspace", str(source),
            "--disable-write", "--disable-shell", "--disable-web-tools", "--no-foreign-personal-context",
            "--approval-mode", "never", "--approval-judge", "off",
            "--model", runner.packet["worker_model"], "--reasoning-effort", runner.packet["worker_reasoning"],
            "--output-schema", str(home / "schema.json"), "--prompt-file", str(home / "prompt.txt")]
    try:
        code = runner.command(argv, runner.packet["call_timeout"], f"model-{number}", cwd=source)
    finally:
        (home / ".config" / "muse" / "auth.json").unlink(missing_ok=True)
    usage = muse_usage(home)
    runner.checkpoint(usage=runner.state.get("usage", []) + [
        {"call": number, "role": "worker", "reported": [usage] if usage else []}])
    final, log = None, runner.run_dir / f"model-{number}.log"
    if log.stat().st_size <= 64_000_000:
        for line in log.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("payload_type") == "run.terminal.completed":
                final = event.get("payload")
    text = final.get("text") if isinstance(final, dict) and final.get("terminal") == "completed" else None
    try:
        # Muse repeats the structured answer in its final text; the first object is the whole answer.
        output = json.JSONDecoder().raw_decode(text.lstrip())[0] if isinstance(text, str) else None
    except ValueError:
        output = None
    if code or not isinstance(output, dict) or len(text) > 4_200_000:
        raise RunnerError("Isolated Muse call failed; inspect private model log")
    if not usage:
        raise RunnerError("Model usage missing; stop rather than lose batch accounting")
    return output


class IsolatedAdapter:
    def __init__(self, auth_home):
        self.auth_home = Path(auth_home)

    def __call__(self, runner, role, feedback):
        number = runner.state["calls"]
        source = runner.run_dir / f"source-{number}"
        snapshot(runner.root, source)
        schema = runner.run_dir / f"schema-{number}.json"
        output = runner.run_dir / f"output-{number}.json"
        save_json(schema, ROLE_SCHEMAS[role])
        packet = dict(shown_packet(runner.packet), checkout=str(source))
        prompt = (Path(__file__).with_name(ROLE_TEMPLATES.get(role, role) + ".md").read_text()
                  + "\nThis is an isolated source snapshot. Read with targeted searches. "
                    "No checks or writes here. Return your complete structured result.\nPACKET:\n"
                  + canonical(packet) + "\nEVIDENCE:\n" + feedback)
        record_prompt(runner, number, role, prompt, isolated=True)
        if role == "worker" and is_claude(packet["worker_model"]):
            # Claude's read boundary is its restricted tool set in the snapshot.
            return claude_worker(runner, number, prompt, source)
        if role == "worker" and is_muse(packet["worker_model"]):
            return muse_worker(runner, number, prompt, source, ROLE_SCHEMAS[role])
        if role == "reviewer":
            return claude_call(runner, number, prompt, source, role=role, model=REVIEW_MODEL[0],
                               effort=REVIEW_MODEL[1], schema=REVIEW_SCHEMA)
        home = runtime_home(runner.run_dir / f"codex-{number}", source, self.auth_home)
        model, effort = codex_model(packet, role)
        meta = model.startswith(META_PREFIX)
        if meta:
            meta_provider(home)
        argv = ["env", "CODEX_HOME=" + str(home), "codex", "exec", "--strict-config",
                "--ephemeral", "--json", "--cd", str(source), "--model", model[len(META_PREFIX):] if meta else model,
                "-c", 'model_reasoning_effort="' + effort + '"',
                "--output-schema", str(schema), "-o", str(output), "-"]
        try:
            code = runner.command(argv, packet["call_timeout"], f"model-{number}", prompt.encode())
        finally:
            if meta:
                scrub_meta_provider(home)
        usage = []
        for line in (runner.run_dir / f"model-{number}.log").read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed":
                usage.append(event.get("usage"))
        runner.checkpoint(usage=runner.state.get("usage", []) + [
            {"call": number, "role": role, "reported": usage}])
        if code or not output.is_file() or output.stat().st_size > 2_100_000:
            raise RunnerError("Isolated Codex call failed; inspect private model log")
        if not usage or any(not isinstance(u, dict) or not isinstance(u.get("input_tokens"), int)
                            or not isinstance(u.get("output_tokens"), int) for u in usage):
            raise RunnerError("Model usage missing; stop rather than lose batch accounting")
        return json.loads(output.read_text())


def permission_probe(source, home, private_file):
    """Real CLI sandbox probe with a harmless existing private sentinel."""
    probe = ("from pathlib import Path; import socket; errors=[]\n"
             "assert Path('readable.txt').read_text() == 'source probe'\n"
             "try: Path(" + repr(str(private_file)) + ").read_bytes(); errors.append('private read escaped')\n"
             "except (PermissionError, FileNotFoundError): pass\n"
             "try: Path('forbidden').write_text('bad'); errors.append('write escaped')\n"
             "except OSError: pass\n"
             "try:\n s=socket.socket(); s.settimeout(2); s.connect(('1.1.1.1',443)); errors.append('network escaped')\n"
             "except OSError: pass\n"
             "assert not errors, errors; print('SOURCE_READ_OK PRIVATE_READ_DENIED WRITE_DENIED NETWORK_DENIED')")
    result = subprocess.run(["codex", "sandbox", "-P", "runner", "-C", str(source),
                             "--", "/usr/bin/python3", "-c", probe], cwd=source,
                            env=dict(os.environ, CODEX_HOME=str(home)),
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RunnerError("Actual Codex permission probe failed: " + result.stderr[-1000:])
    return result.stdout.strip()
