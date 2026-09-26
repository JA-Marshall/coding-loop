"""Read the plan lifecycle in a checkout into one document for the plan board.

``collect_plans(checkout)`` lists every Markdown file under ``docs/`` that
carries a ``storehouse-plan`` control block, using the validator's own parser
so the board can never disagree with ``validate_plans.py``. Each plan gets a
last-changed time from git history, or from the file's mtime when the file is
uncommitted or the checkout is not a repository. ``render_plan`` turns one
plan into HTML with a deliberately small Markdown subset. Both read only.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    pass
else:  # pragma: no cover - script use
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.validate_plans import _parse_active, _parse_inbox, _parse_plan  # noqa: E402

EXECUTABLE = {"READY", "IN_PROGRESS", "BLOCKED"}
RECENT_DAYS = 7
CONTROL_KEYS = ["id", "status", "active_phase", "agent_strategy", "implementation_authority",
                "merge_authority", "release_authority"]


def plan_files(checkout):
    docs = Path(checkout) / "docs"
    found = []
    if not docs.is_dir():
        return found
    for path in sorted(docs.rglob("*.md")):
        if "docs/plans/templates" in path.as_posix():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"<!--\s*storehouse-plan\s*\n", text):
            found.append(path)
    return found


def git_times(checkout):
    """{relative path: last commit epoch} for docs/, plus the set of dirty paths."""
    try:
        log = subprocess.run(["git", "-C", str(checkout), "log", "--format=%ct", "--name-only", "--", "docs"],
                             capture_output=True, text=True, timeout=20, check=True).stdout
        status = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain", "--", "docs"],
                                capture_output=True, text=True, timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None, set()
    times, stamp = {}, None
    for line in log.splitlines():
        if not line.strip():
            continue
        if re.fullmatch(r"\d+", line.strip()):
            stamp = int(line.strip())
        elif stamp is not None:
            times.setdefault(line.strip(), stamp)
    dirty = {line[3:].strip() for line in status.splitlines() if len(line) > 3}
    return times, dirty


def title_of(text):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def collect_plans(checkout, now=None):
    now = time.time() if now is None else now
    document = {"generated": now, "checkout": None, "recent_days": RECENT_DAYS, "active": None,
                "plans": [], "errors": [], "time_source": "mtime"}
    if not checkout:
        document["errors"].append({"file": "", "message": "no checkout given; pass --checkout or run a batch whose manifest names one"})
        return document
    root = Path(checkout).expanduser().resolve()
    document["checkout"] = str(root)
    if not (root / "docs").is_dir():
        document["errors"].append({"file": "docs", "message": "no docs/ directory in " + str(root)})
        return document
    times, dirty = git_times(root)
    if times is not None:
        document["time_source"] = "git"
    errors = []
    inbox = _parse_inbox(root, errors)
    active = _parse_active(root, errors)
    document["active"] = active
    task_dir = (root / "docs/plans/tasks").resolve()
    for path in plan_files(root):
        plan_errors = []
        plan = _parse_plan(path, root, path.parent.resolve() == task_dir, plan_errors)
        relative = path.relative_to(root).as_posix()
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = None
        updated, source = mtime, "mtime"
        if times is not None and relative in times and relative not in dirty:
            updated, source = times[relative], "git"
        control = plan.control if plan else {}
        row = {"path": relative, "title": title_of(plan.text if plan else path.read_text(encoding="utf-8")) or path.stem,
               "is_task": bool(plan and plan.is_task), "in_inbox": relative in inbox,
               "active": active == relative, "updated": updated, "time_source": source,
               "errors": plan_errors}
        for key in CONTROL_KEYS:
            row[key] = control.get(key)
        row["recent"] = bool(row["status"] in EXECUTABLE or (updated is not None and now - updated <= RECENT_DAYS * 86400))
        document["plans"].append(row)
    document["plans"].sort(key=lambda r: (-(r["updated"] or 0), r["path"]))
    document["errors"] = [{"file": e.split(":")[0], "message": e.split(":", 1)[1].strip() if ":" in e else e} for e in errors]
    document["counts"] = {"total": len(document["plans"]), "recent": sum(1 for r in document["plans"] if r["recent"])}
    return document


# ---------------------------------------------------------------- rendering

def confine(checkout, relative):
    """Resolve a plan path inside <checkout>/docs or return None."""
    if not checkout or not relative:
        return None
    root = Path(checkout).resolve()
    try:
        target = (root / relative).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root / "docs") or target.suffix != ".md" or not target.is_file():
        return None
    return target


def render_plan(checkout, relative):
    target = confine(checkout, relative)
    if target is None:
        return None
    root = Path(checkout).resolve()
    text = target.read_text(encoding="utf-8", errors="replace")
    control = {}
    match = re.search(r"<!--\s*storehouse-plan\s*\n(.*?)\n\s*-->", text, re.DOTALL)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                control[key.strip()] = value.strip()
    body = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    body = re.sub(r"^\s*# [^\n]*\n", "", body, count=1)  # the page header already shows the title
    rel_dir = target.parent
    plans = {p.resolve() for p in plan_files(root)}

    def rewrite(url):
        if re.match(r"^[a-z]+:", url) or url.startswith("#"):
            return url, True
        path_part = url.split("#")[0]
        if not path_part:
            return url, True
        try:
            resolved = (rel_dir / path_part).resolve()
        except OSError:
            return url, True
        if resolved in plans:
            return "#plan=" + resolved.relative_to(root).as_posix(), False
        return None, False

    return {"path": target.relative_to(root).as_posix(), "title": title_of(text) or target.stem,
            "control": control, "html": markdown_html(body, rewrite)}


INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*(.+?)\*\*")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def inline(text, rewrite):
    parts, out, last = [], [], 0
    for m in INLINE_CODE.finditer(text):
        parts.append((text[last:m.start()], False))
        parts.append((m.group(1), True))
        last = m.end()
    parts.append((text[last:], False))
    for chunk, is_code in parts:
        if is_code:
            out.append("<code>" + html.escape(chunk) + "</code>")
            continue
        escaped = html.escape(chunk)

        def link(m):
            label, url = m.group(1), html.unescape(m.group(2))
            target, external = rewrite(url)
            if target is None:
                return label + ' <span class="muted">(' + html.escape(url) + ")</span>"
            attrs = ' target="_blank" rel="noopener"' if external and re.match(r"^https?:", target) else ""
            return '<a href="' + html.escape(target, quote=True) + '"' + attrs + ">" + label + "</a>"

        escaped = LINK.sub(link, escaped)
        escaped = BOLD.sub(r"<strong>\1</strong>", escaped)
        out.append(escaped)
    return "".join(out)


def markdown_html(text, rewrite=lambda url: (url, True)):
    """A small Markdown subset: headings, fences, lists, tables, paragraphs, inline code, bold, links."""
    lines = text.splitlines()
    out, i, para = [], 0, []

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(s.strip() for s in para), rewrite) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f"<h{level}>" + inline(heading.group(2), rewrite) + f"</h{level}>")
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" + "".join("<th>" + inline(c, rewrite) + "</th>" for c in header) + "</tr></thead><tbody>" +
                       "".join("<tr>" + "".join("<td>" + inline(c, rewrite) + "</td>" for c in row) + "</tr>" for row in rows) +
                       "</tbody></table>")
            continue
        item = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if item:
            flush()
            ordered = item.group(2)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines):
                m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if m and (m.group(2)[0].isdigit()) == ordered:
                    items.append(m.group(3))
                    i += 1
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and items:
                    items[-1] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join("<li>" + inline(x, rewrite) + "</li>" for x in items) + f"</{tag}>")
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote><p>" + inline(" ".join(quote), rewrite) + "</p></blockquote>")
            continue
        if not stripped:
            flush()
            i += 1
            continue
        para.append(line)
        i += 1
    flush()
    return "\n".join(out)


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Print the plan board document for a checkout.")
    parser.add_argument("--checkout", required=True, type=Path)
    args = parser.parse_args(argv)
    json.dump(collect_plans(args.checkout), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
