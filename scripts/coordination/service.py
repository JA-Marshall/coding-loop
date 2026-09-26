"""Start/status/stop the frozen batch under WSL systemd, independent of this chat."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from .batch import Batch, validate_manifest, clean, head
from .runner import RunnerError, git
from .isolated import config_text, permission_probe


def service_name(manifest):
    return "storehouse-" + manifest["id"]


def preflight(manifest, directory):
    batch = Batch(manifest, directory)
    batch.assert_authority()
    root = batch.root
    if not (directory / "state.json").exists():
        clean(root)
        git(root, "fetch", "origin", "main")
        git(root, "merge-base", "--is-ancestor", head(root), "origin/main")
        if "target: NONE" not in (root / "docs/plans/ACTIVE.md").read_text():
            raise RunnerError("Finish the active setup plan before launching the batch")
    probe = directory / "preflight"
    probe.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = probe / "source"
    source.mkdir(exist_ok=True)
    (source / "readable.txt").write_text("source probe")
    sentinel = probe / "private-sentinel"
    sentinel.write_text("synthetic private sentinel")
    home = probe / "codex"
    home.mkdir(mode=0o700, exist_ok=True)
    (home / "config.toml").write_text(config_text(source))
    evidence = permission_probe(source, home, sentinel)
    (probe / "permissions.txt").write_text(evidence + "\n")
    subprocess.run([str(root / ".venv/bin/python"), "-m", "scripts.coordination.checks", "--preflight"],
                   cwd=root, check=True, timeout=300)
    subprocess.run(["codex", "login", "status"],
                   env=dict(os.environ, CODEX_HOME=manifest["auth_home"]), check=True, timeout=30)
    # Claude always reviews, and may also be the worker.
    subprocess.run(["claude", "auth", "status"], stdout=subprocess.DEVNULL, check=True, timeout=30)
    repo = batch.github.api("")
    if not repo.get("permissions", {}).get("push"):
        raise RunnerError("GitHub identity lacks repository push/merge access")
    return batch


def windows_awake(manifest, directory):
    """Temporary execution-state request; no persistent power-policy mutation."""
    windows = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not windows.exists():
        return
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        raise RunnerError("WSL distribution identity missing for awake helper")
    script = Path(__file__).with_name("keep-awake.ps1").read_bytes()
    script_payload = base64.b64encode(script).decode()
    status = directory / "awake.json"
    if status.exists():
        previous = json.loads(status.read_text(encoding="utf-8-sig"))
        if previous.get("state") != "RELEASED":
            raise RunnerError("Existing awake helper may still be running; inspect before relaunch")
        status.replace(directory / ("awake-previous-" + str(time.time_ns()) + ".json"))
    windows_status = subprocess.check_output(["wslpath", "-w", str(status)], text=True).strip()
    def quote(value):
        return "'" + value.replace("'", "''") + "'"
    helper_name = "keep-awake-" + manifest["id"] + ".ps1"
    child = ("& (Join-Path $env:LOCALAPPDATA " + quote("StorehouseRunner/" + helper_name) + ") -Distro " + quote(distro) + " -Unit " +
             quote(service_name(manifest)) + " -StatusPath " + quote(windows_status) +
             " -MaxSeconds " + str(manifest["total_timeout"] + 90))
    encoded = base64.b64encode(child.encode("utf-16le")).decode()
    parent = ("$folder=Join-Path $env:LOCALAPPDATA 'StorehouseRunner'; "
              "[void][IO.Directory]::CreateDirectory($folder); "
              "[IO.File]::WriteAllBytes((Join-Path $folder " + quote(helper_name) + "),"
              "[Convert]::FromBase64String('" + script_payload + "')); "
              "Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -WindowStyle Hidden "
              "-RedirectStandardError " + quote(windows_status + ".stderr") + " -ArgumentList "
              "@('-NoProfile','-NonInteractive','-EncodedCommand','" + encoded + "')")
    subprocess.Popen([str(windows), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                      base64.b64encode(parent.encode("utf-16le")).decode()],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        if status.exists():
            try:
                evidence = json.loads(status.read_text(encoding="utf-8-sig"))
                if evidence.get("state") == "AWAKE":
                    return
                raise RunnerError("Temporary Windows awake request failed")
            except json.JSONDecodeError:
                pass
        time.sleep(0.5)
    raise RunnerError("Windows awake helper did not confirm startup")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop", "check"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = validate_manifest(json.loads(args.manifest.read_text()))
        directory = args.directory.resolve()
        unit = service_name(manifest)
        if args.action == "status":
            result = subprocess.run(["systemctl", "--user", "show", unit, "--property=ActiveState,SubState,MainPID,Result"],
                                    text=True, capture_output=True, timeout=15)
            print(result.stdout.strip())
            if (directory / "report.md").exists():
                print((directory / "report.md").read_text())
            return 0
        if args.action == "stop":
            # systemd kills the complete cgroup; it cannot leave a writer running.
            subprocess.run(["systemctl", "--user", "stop", unit], check=True, timeout=45)
            print("Service stopped; candidate and checkpoints preserved. Inspect before restarting.")
            return 0
        preflight(manifest, directory)
        if args.action == "check":
            print("Preflight passed; nothing launched.")
            return 0
        state = directory / "state.json"
        if state.exists() and json.loads(state.read_text())["phase"] in {"STOPPED", "COMPLETE"}:
            raise RunnerError("Run is terminal; inspect its report rather than resetting budgets")
        windows_awake(manifest, directory)
        root = Path(manifest["checkout"])
        argv = ["systemd-run", "--user", "--collect", "--unit=" + unit,
                "--property=WorkingDirectory=" + str(root), "--property=KillMode=control-group",
                "--property=RuntimeMaxSec=" + str(manifest["total_timeout"]),
                "--property=TimeoutStopSec=15", "--property=Restart=on-abnormal", "--property=RestartSec=10",
                "--setenv=PATH=" + os.environ["PATH"], "--setenv=PYTHONDONTWRITEBYTECODE=1",
                str(root / ".venv/bin/python"), "-m", "scripts.coordination.batch",
                str(args.manifest.resolve()), "--directory", str(directory), "--execute"]
        subprocess.run(argv, check=True, timeout=30)
        print("Started " + unit + ".service; report: " + str(directory / "report.md"))
        return 0
    except (RunnerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, RunnerError) else type(exc).__name__ + "; inspect local configuration", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
