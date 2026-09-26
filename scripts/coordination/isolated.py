"""Source snapshot + explicit read permissions for unattended Codex and Claude calls."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
from .runner import (REVIEW_MODEL, REVIEW_SCHEMA, ROLE_SCHEMAS, ROLE_TEMPLATES, RunnerError, canonical,
                     claude_call, claude_worker, codex_model, git, is_claude, record_prompt, save_json)


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
        packet = dict(runner.packet, checkout=str(source))
        prompt = (Path(__file__).with_name(ROLE_TEMPLATES.get(role, role) + ".md").read_text()
                  + "\nThis is an isolated source snapshot. Read with targeted searches. "
                    "No checks or writes here. Return your complete structured result.\nPACKET:\n"
                  + canonical(packet) + "\nEVIDENCE:\n" + feedback)
        record_prompt(runner, number, role, prompt, isolated=True)
        if role == "worker" and is_claude(packet["worker_model"]):
            # Claude's read boundary is its restricted tool set in the snapshot.
            return claude_worker(runner, number, prompt, source)
        if role == "reviewer":
            return claude_call(runner, number, prompt, source, role=role, model=REVIEW_MODEL[0],
                               effort=REVIEW_MODEL[1], schema=REVIEW_SCHEMA)
        home = runtime_home(runner.run_dir / f"codex-{number}", source, self.auth_home)
        model, effort = codex_model(packet, role)
        argv = ["env", "CODEX_HOME=" + str(home), "codex", "exec", "--strict-config",
                "--ephemeral", "--json", "--cd", str(source), "--model", model,
                "-c", 'model_reasoning_effort="' + effort + '"',
                "--output-schema", str(schema), "-o", str(output), "-"]
        code = runner.command(argv, packet["call_timeout"], f"model-{number}", prompt.encode())
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
