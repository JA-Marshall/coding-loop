"""Write a private frozen operator manifest for the owner-authorized eBay batch."""
import argparse
import json
import os
from pathlib import Path
import re
from .batch import validate_manifest
from .runner import git, save_json

# Skipped tasks must be COMPLETE where the batch will branch from, not merely in
# the working tree: a stopped batch writes COMPLETE locally before its PR merges.
EXECUTION_BASE = "origin/main"


def completed_tasks(root, ref=EXECUTION_BASE):
    """Queue task IDs whose canonical plan is COMPLETE on the fetched execution base."""
    listed = {os.fsdecode(name) for name in
              git(root, "ls-tree", "-z", "--name-only", ref, "docs/plans/tasks/").split(b"\0") if name}
    done = set()
    for task in json.loads(Path(__file__).with_name("ebay_queue.json").read_text()):
        name = "docs/plans/tasks/" + task["id"] + ".md"
        if name not in listed:
            continue
        block = re.search(r"<!-- storehouse-plan\n(.*?)\n-->", git(root, "show", ref + ":" + name).decode(), re.S)
        if block and "status: COMPLETE" in block[1].splitlines():
            done.add(task["id"])
    return done


def approved_tasks(root, ids):
    """Frozen queue entries for ids, in queue order; only COMPLETE omitted dependencies drop."""
    queue = queue_tasks(root)
    omitted = any(d not in ids for task in queue if task["id"] in ids for d in task["depends_on"])
    done = completed_tasks(root) if omitted else set()
    selected = [task for task in queue if task["id"] in ids]
    if [task["id"] for task in selected] != list(ids):
        raise ValueError("Batch tasks must be approved queue entries in queue order")
    return [dict(task, depends_on=[d for d in task["depends_on"] if d in ids or d not in done])
            for task in selected]


def queue_tasks(root):
    tasks = json.loads(Path(__file__).with_name("ebay_queue.json").read_text())
    for task in tasks:
        labels = task.pop("test_labels")
        task["checks"] = [{"id": "focused-postgres", "argv": [str(root / ".venv/bin/python"),
                           "-m", "scripts.coordination.checks", *labels], "timeout": 1800},
                          {"id": "application-baseline", "argv": [str(root / ".venv/bin/python"),
                           "-m", "scripts.coordination.checks", "operations", "accounting"], "timeout": 1800},
                          {"id": "plans", "argv": [str(root / ".venv/bin/python"),
                           "scripts/validate_plans.py"], "timeout": 60}]
    return tasks


def manifest(root, auth_home, batch_id, skip_complete=False):
    tasks = queue_tasks(root)
    if skip_complete:
        done = completed_tasks(root)
        tasks = approved_tasks(root, [task["id"] for task in tasks if task["id"] not in done])
    return validate_manifest({"id": batch_id, "checkout": str(root.resolve()),
        "repository": "JA-Marshall/Ebay-Inventory-and-Order-Management-System.",
        "auth_home": str(auth_home.resolve()), "tasks": tasks, "total_timeout": 28800,
        "max_calls": 24, "max_reported_tokens": 4000000, "ci_timeout": 3600,
        "authority": "docs/workflows/ebay-auto-run-authority.md"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--auth-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--id", default="ebay-overnight-01")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-complete", action="store_true",
                        help="Omit queue tasks whose plan is already COMPLETE in the checkout")
    args = parser.parse_args()
    if args.skip_complete:
        git(args.checkout, "fetch", "origin", "main")
    if args.output.exists():
        parser.error("Choose a new output; existing manifests are immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    save_json(args.output, manifest(args.checkout, args.auth_home, args.id, args.skip_complete))
    print("Frozen manifest: " + str(args.output))


if __name__ == "__main__":
    main()
