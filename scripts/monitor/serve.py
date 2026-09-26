"""Serve the batch monitor on loopback. Run with python -m scripts.monitor.serve.

Three routes, nothing clever:

- ``GET /``               the page, ``index.html`` beside this file
- ``GET /api/state``      ``collect(directory)`` as JSON
- ``GET /api/log?path=…`` the last 8,000 characters of one ``.log`` file that
                          lives inside the evidence directory; anything else
                          is refused

The server binds a loopback address only. The evidence holds full prompts
and diffs from a private repository, so a phone on the same LAN uses an SSH
tunnel rather than a wider bind. Nothing here writes to the directory.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if __package__:
    from .collect import collect, is_phase_run, load_json, log_digest
    from .plans import collect_plans, render_plan
else:  # run as a plain script from any directory: python3 /path/to/scripts/monitor/serve.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.monitor.collect import collect, is_phase_run, load_json, log_digest
    from scripts.monitor.plans import collect_plans, render_plan

PAGE = Path(__file__).with_name("index.html")
PLANS_PAGE = Path(__file__).with_name("plans.html")
LOG_TAIL_CHARS = 8000
LOOPBACK_NAMES = {"localhost"}


DISCOVERY_DEPTH = 4
DISCOVERY_CACHE_S = 5
SKIP_DIRS = {"preflight", "source", "fixture", "checkout", "codex"}


def evidence_dir(path):
    """A directory collect() can read: state.json here, in run/ beneath, or a phase run's coordinator.log."""
    path = Path(path)
    if (path / "state.json").is_file() or (path / "run" / "state.json").is_file() or is_phase_run(path):
        return path
    return None


def discover_batches(root):
    """Every batch or runner checkpoint under root, at any depth up to DISCOVERY_DEPTH.

    The runner directory holds several trees (batches/<id>/run, runs/<id>,
    smoke/<id>/run, plus continuation runs beside a run/), so this walks down
    until it meets a state.json and does not descend past one: the packet
    directories inside a batch are that batch's evidence, not batches.
    """
    root = Path(root).resolve()
    found = []

    def visit(directory, depth):
        if is_phase_run(directory):
            relative = directory.relative_to(root).as_posix() if directory != root else root.name
            found.append(phase_entry(relative, directory))
            return
        if (directory / "state.json").is_file():
            target = directory.parent if directory.name == "run" and directory != root else directory
            relative = target.relative_to(root).as_posix() if target != root else root.name
            found.append(batch_entry(relative, target, directory / "state.json"))
            return
        if depth >= DISCOVERY_DEPTH:
            return
        try:
            children = sorted(c for c in directory.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name not in SKIP_DIRS)
        except OSError:
            return
        for child in children:
            visit(child, depth + 1)

    visit(root, 0)
    found.sort(key=lambda b: -(b["updated"] or 0))
    return found


def phase_entry(batch_id, target):
    log_path = target / "coordinator.log"
    phase, reason, done, total = "RUN", None, 0, None
    try:
        lines = log_path.read_text(errors="replace").splitlines()
        updated = log_path.stat().st_mtime
    except OSError:
        lines, updated = [], None
    seen_done = set()
    for line in lines:
        rest = line.split(" ", 1)[1] if " " in line else line
        if rest.startswith("ALL "):
            phase = "COMPLETE"
        elif rest.startswith("STOP"):
            phase, reason = "STOPPED", rest[4:].lstrip(": ").strip()
        elif rest.startswith("START phase ") or re.match(r"^phase \d+ already done$", rest):
            phase, reason = "RUN", None  # a rerun reopens a finished or stopped run
        m = re.match(r"^END phase (\d+) exit=\d+ staging-status=done", rest) or re.match(r"^phase (\d+) already done", rest)
        if m:
            seen_done.add(m.group(1))
    done = len(seen_done)
    script = target / "run_phases.sh"
    if script.is_file():
        try:
            text = script.read_text(errors="replace")
            m = re.search(r'^PHASES="\$\{PHASES:-([\d\s]+)\}"', text, re.MULTILINE) or re.search(r"^for n in ((?:\d+\s*)+); do", text, re.MULTILINE)
            total = len(m.group(1).split()) if m else None
        except OSError:
            total = None
    return {"id": batch_id, "path": str(target.resolve()), "phase": phase, "kind": "phases",
            "group": batch_id.split("/")[0] if "/" in batch_id else "",
            "stopped_phase": None, "reason": reason, "updated": updated, "merged": done, "total": total,
            "calls": None, "tokens": None, "deadline": None}


def batch_entry(batch_id, target, state_path):
    state = load_json(state_path, target, [])
    state = state if isinstance(state, dict) else {}
    try:
        updated = state_path.stat().st_mtime
    except OSError:
        updated = None
    kind = "batch" if "manifest_hash" in state else "runner" if "packet_hash" in state else "unknown"
    total = None
    for name in ("manifest.json", "run/manifest.json"):
        if (target / name).is_file():
            manifest = load_json(target / name, target, [])
            if isinstance(manifest, dict) and isinstance(manifest.get("tasks"), list):
                total = len(manifest["tasks"])
            break
    if kind == "runner":
        total = 1
    completed = state.get("completed") if isinstance(state.get("completed"), list) else []
    return {"id": batch_id, "path": str(target.resolve()), "phase": state.get("phase"), "kind": kind,
            "group": batch_id.split("/")[0] if "/" in batch_id else "",
            "stopped_phase": state.get("stopped_phase"), "reason": state.get("reason"), "updated": updated,
            "merged": len(completed) if kind == "batch" else (1 if state.get("phase") == "LOCAL_REVIEWED" else 0),
            "total": total, "calls": state.get("calls"),
            "tokens": state.get("tokens") if not state.get("usage_incomplete") else None,
            "deadline": state.get("deadline")}


def validate_host(host):
    """Return the host if it is a loopback address; raise ValueError otherwise."""
    if host in LOOPBACK_NAMES:
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"Refusing to bind {host!r}: not a loopback address") from exc
    if not address.is_loopback:
        raise ValueError(f"Refusing to bind {host!r}: the monitor serves private evidence on loopback only")
    return host


def log_tail(directory, raw_path, limit=LOG_TAIL_CHARS):
    """Return (status, payload) for a log request.

    The path must resolve to a regular ``.log`` or ``.err`` file inside one of
    the served roots after following symlinks; otherwise 403. Missing is 404.
    """
    if not raw_path:
        return HTTPStatus.BAD_REQUEST, {"error": "path query parameter required"}
    roots = [Path(d).resolve() for d in (directory if isinstance(directory, (list, tuple)) else [directory])]
    directory = roots[0]
    requested = Path(raw_path)
    if not requested.is_absolute():
        requested = directory / requested
    try:
        resolved = requested.resolve()
    except OSError:
        return HTTPStatus.FORBIDDEN, {"error": "path refused"}
    if not any(resolved.is_relative_to(root) for root in roots) or resolved.suffix not in {".log", ".err"}:
        return HTTPStatus.FORBIDDEN, {"error": "path refused: logs are served from inside the evidence directory only"}
    if not resolved.is_file():
        return HTTPStatus.NOT_FOUND, {"error": "no such log"}
    try:
        text = resolved.read_text(errors="replace")
    except OSError as exc:
        return HTTPStatus.NOT_FOUND, {"error": exc.strerror or str(exc)}
    return HTTPStatus.OK, {"path": str(resolved), "size": len(text), "truncated": len(text) > limit,
                           "tail": text[-limit:], "digest": log_digest(text)}


# ---------------------------------------------------------------- controls
# The only writes the server ever makes: one empty STOP file, its removal, and
# launching the phase runner's own subcommands. Batches start from the shell,
# because they need a manifest and a preflight.

def state_dir(target):
    """Where a batch looks for STOP: the directory holding its state.json (batches/<id>/run), else the target."""
    target = Path(target)
    if not (target / "state.json").is_file() and (target / "run" / "state.json").is_file():
        return target / "run"
    return target


def write_stop(target):
    path = state_dir(target) / "STOP"
    if path.exists():
        return {"path": str(path), "created": False}
    with open(path, "x"):
        pass
    return {"path": str(path), "created": True}


def clear_stop(target):
    path = state_dir(target) / "STOP"
    if not path.exists():
        return {"path": str(path), "removed": False}
    path.unlink()
    return {"path": str(path), "removed": True}


def phase_lock_held(target):
    lock = Path(target) / "lock"
    if not lock.exists():
        return False
    import fcntl
    try:
        with open(lock, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                return False
            except OSError:
                return True
    except OSError:
        return False


def control_state(target):
    target = Path(target)
    phases = is_phase_run(target)
    return {"stop": (state_dir(target) / "STOP").exists(), "phases": phases,
            "locked": phase_lock_held(target) if phases else None,
            "script": str(target / "run_phases.sh") if phases and (target / "run_phases.sh").is_file() else None}


def launch_phases(target, command, phase=None):
    """Run the phase runner's subcommand detached, exactly as the shell would. Returns (status, payload)."""
    target = Path(target)
    script = target / "run_phases.sh"
    if not is_phase_run(target) or not script.is_file():
        return HTTPStatus.BAD_REQUEST, {"error": "not a phase run"}
    if command not in {"run", "review", "correct"}:
        return HTTPStatus.BAD_REQUEST, {"error": "command must be run, review or correct"}
    argv = [str(script), command]
    if command in {"review", "correct"}:
        if not phase or not re.fullmatch(r"\d{2}", phase):
            return HTTPStatus.BAD_REQUEST, {"error": "phase must be two digits"}
        argv.append(phase)
    if (target / "STOP").exists():
        return HTTPStatus.CONFLICT, {"error": "STOP file present; clear it first"}
    if phase_lock_held(target):
        return HTTPStatus.CONFLICT, {"error": "a loop is already running (lock held)"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = target / f"launch-{stamp}.log"
    with open(out, "ab") as handle:
        proc = subprocess.Popen(argv, cwd=str(target), stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                                start_new_session=True, env=dict(os.environ, LOG=str(target)))
    return HTTPStatus.OK, {"pid": proc.pid, "argv": argv, "log": str(out)}


class MonitorHandler(BaseHTTPRequestHandler):
    server_version = "coding-loop-monitor/0.1"
    directory = None  # set on the server class per instance

    def do_GET(self):  # noqa: N802 - http.server naming
        parts = urlsplit(self.path)
        if parts.path in ("/", "/runs"):
            return self.send_page()
        query = parse_qs(parts.query)
        wanted = query.get("batch", [""])[0]
        if parts.path == "/api/batches":
            return self.send_json(HTTPStatus.OK, {"root": str(self.server.directory), "batches": self.server.batches()})
        if parts.path == "/api/state":
            target, batches = self.server.select(wanted)
            document = collect(target) if target is not None else collect(self.server.directory)
            document["batches"] = batches
            document["control"] = control_state(target) if target is not None else None
            document["root"] = " · ".join(str(r) for r in self.server.roots)
            document["batch_id"] = next((b["id"] for b in batches if b["path"] == str(target)), None) if target else None
            return self.send_json(HTTPStatus.OK, document)
        if parts.path == "/api/log":
            raw = query.get("path", [""])[0]
            status, payload = log_tail(self.server.roots, raw)
            return self.send_json(status, payload)
        if parts.path == "/plans":
            return self.send_page(PLANS_PAGE)
        if parts.path == "/api/plans":
            return self.send_json(HTTPStatus.OK, collect_plans(self.server.checkout_for(wanted)))
        if parts.path == "/api/plan":
            raw = query.get("path", [""])[0]
            checkout = self.server.checkout_for(wanted)
            if not checkout:
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "no checkout configured"})
            if ".." in Path(raw).parts or Path(raw).is_absolute():
                return self.send_json(HTTPStatus.FORBIDDEN, {"error": "plan paths are relative to the checkout's docs/"})
            payload = render_plan(checkout, raw)
            if payload is None:
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such plan under docs/"})
            return self.send_json(HTTPStatus.OK, payload)
        if parts.path in ("/api/stop", "/api/stop/clear", "/api/phases/launch"):
            return self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "POST only"})
        return self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such route"})

    def do_POST(self):  # noqa: N802 - http.server naming
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        # A custom header keeps a cross-site form from posting here; browsers cannot add it without CORS consent.
        if self.headers.get("X-Requested-With") != "monitor":
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "missing X-Requested-With: monitor"})
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be JSON"})
        wanted = query.get("batch", [""])[0] or str(data.get("batch") or "")
        target, _batches = self.server.select(wanted)
        if target is None:
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "no run selected"})
        if parts.path == "/api/stop":
            return self.send_json(HTTPStatus.OK, write_stop(target))
        if parts.path == "/api/stop/clear":
            return self.send_json(HTTPStatus.OK, clear_stop(target))
        if parts.path == "/api/phases/launch":
            status, payload = launch_phases(target, str(data.get("command") or ""), data.get("phase"))
            return self.send_json(status, payload)
        return self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such route"})

    def send_page(self, page=PAGE):
        try:
            body = page.read_bytes()
        except OSError as exc:
            return self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "index.html unreadable: " + str(exc)})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - http.server naming
        if "GET /api/state" in str(args[0] if args else ""):
            return  # one line every three seconds is noise
        sys.stderr.write("%s %s\n" % (self.address_string(), format % args))


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, directory, host="127.0.0.1", port=8790, checkout=None):
        roots = [directory] if isinstance(directory, (str, Path)) else list(directory)
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.directory = self.roots[0]
        self.explicit_checkout = Path(checkout).expanduser().resolve() if checkout is not None else None
        self._cache = (0.0, [])
        super().__init__((validate_host(host), port), MonitorHandler)

    def batches(self):
        """Discovered runs across every root, cached briefly: the page polls every three seconds."""
        stamp, found = self._cache
        if time.monotonic() - stamp < DISCOVERY_CACHE_S:
            return found
        found = []
        for root in self.roots:
            entries = discover_batches(root)
            if len(self.roots) > 1:
                for entry in entries:
                    if entry["id"] != root.name:
                        entry["id"] = root.name + "/" + entry["id"]
                    entry["group"] = root.name
            found.extend(entries)
        found.sort(key=lambda b: -(b["updated"] or 0))
        self._cache = (time.monotonic(), found)
        return found

    def contains(self, path):
        path = Path(path)
        return any(path.is_relative_to(root) for root in self.roots)

    def select(self, wanted=""):
        """(evidence directory to show, all batches). Unknown or empty id: the newest."""
        batches = self.batches()
        if not batches:
            return None, []
        for batch in batches:
            if wanted and batch["id"] == wanted:
                return Path(batch["path"]), batches
        return Path(batches[0]["path"]), batches

    def checkout_for(self, wanted=""):
        if self.explicit_checkout is not None:
            return self.explicit_checkout
        target, _batches = self.select(wanted)
        return default_checkout(target if target is not None else self.directory)

    @property
    def checkout(self):
        return self.checkout_for("")


def default_checkout(directory):
    """The checkout named by the batch manifest or the runner packet, when it exists and holds plans."""
    directory = Path(directory)
    for name in ("manifest.json", "run/manifest.json", "packet.json", "run/packet.json"):
        path = directory / name
        if not path.is_file():
            continue
        record = load_json(path, directory, [])
        checkout = Path(str(record.get("checkout", ""))) if isinstance(record, dict) else None
        if checkout and checkout.is_absolute() and (checkout / "docs").is_dir():
            return checkout
    return None


def make_server(directory, host="127.0.0.1", port=8790, checkout=None):
    validate_host(host)  # refuse before any socket exists
    return MonitorServer(directory, host, port, checkout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--directory", required=True, type=Path, action="append",
                        help="an evidence directory or a folder holding several; repeat the flag for more roots (read only)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--host", default="127.0.0.1", help="loopback only; any other address is refused")
    parser.add_argument("--checkout", type=Path, default=None,
                        help="repository whose docs/plans the plan board reads; defaults to the batch manifest's checkout")
    args = parser.parse_args(argv)
    try:
        server = make_server(args.directory, args.host, args.port, args.checkout)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("Cannot bind %s:%s: %s" % (args.host, args.port, exc.strerror or exc), file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    found = server.batches()
    print("Monitor: http://%s:%s/  (evidence: %s, read only)" % (host, port, ", ".join(str(r) for r in server.roots)))
    if len(found) > 1:
        print("Runs found: %d  (pick one in the header, or ?batch=<id>; newest is %s)" % (len(found), found[0]["id"]))
    elif not found:
        print("No state.json found under that directory; the page will say so.")
    print("Plans:   http://%s:%s/plans  (checkout: %s)" % (host, port, server.checkout or "none; pass --checkout"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
