#!/usr/bin/env python3
"""Validate the deterministic Storehouse plan lifecycle."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


STATUSES = {"DRAFT", "READY", "IN_PROGRESS", "BLOCKED", "COMPLETE", "SUPERSEDED"}
EXECUTABLE_STATUSES = {"READY", "IN_PROGRESS", "BLOCKED"}
PHASE_STATES = {"PENDING", "ACTIVE", "BLOCKED", "COMPLETE", "SKIPPED"}
AGENT_STRATEGIES = {"PRIMARY", "DISJOINT_LANES", "SEQUENTIAL_WORKER"}
SELLING_WORKER_MODELS = {"gpt-5.6-terra": "medium", "gpt-5.6-sol": "high", "claude-opus-5-5": "high"}
COORDINATOR_ONLY_PATHS = (
    ".git/**", ".agents/**", ".github/**", "AGENTS.md", "PLANS.md",
    "docs/plans/**", "docs/projects/**", "scripts/validate_plans.py",
    "scripts/tests/test_validate_plans.py", "scripts/classify_ci_changes.py",
    "scripts/tests/test_classify_ci_changes.py", "scripts/tests/test_ci_contract.py",
    "scripts/verify_main_provenance.py", "scripts/verify_release_ci.py",
    "scripts/tests/test_verify_main_provenance.py", "scripts/tests/test_verify_release_ci.py",
)
IMPLEMENTATION_AUTHORITIES = {"PLAN_ONLY", "GOAL_REQUIRED", "GOAL_GRANTED", "CLOSED"}
MERGE_AUTHORITIES = {"OWNER_REQUIRED", "OWNER_GRANTED"}
IMPACT_PREFIXES = {"NONE", "PRESENT"}
REQUIRED_KEYS = {
    "id",
    "status",
    "active_phase",
    "agent_strategy",
    "implementation_authority",
    "merge_authority",
    "release_authority",
    "schema_impact",
    "data_impact",
    "valuation_impact",
    "reporting_impact",
    "marketplace_write_impact",
    "production_impact",
    "tbd",
}
IMPACT_KEYS = {
    "schema_impact",
    "data_impact",
    "valuation_impact",
    "reporting_impact",
    "marketplace_write_impact",
    "production_impact",
}
COMPLETE_EVIDENCE = {"Outcome", "Verification", "Review", "Pull request", "Merge", "Production"}
UNRESOLVED_RE = re.compile(r"\b(?:TBD|TODO|TO BE DETERMINED)\b|\?\?\?", re.IGNORECASE)
NON_SUCCESS_EVIDENCE = {"PENDING", "FAILED", "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


@dataclass(frozen=True)
class Plan:
    path: Path
    relative_path: str
    text: str
    control: dict[str, str]
    is_task: bool


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_key_values(body: str, label: str, errors: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(body.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            errors.append(f"{label}: line {line_number} must use 'key: value'")
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key in values:
            errors.append(f"{label}: duplicate key '{key}'")
        elif not value:
            errors.append(f"{label}: '{key}' must not be empty")
        else:
            values[key] = value
    return values


def _parse_plan(path: Path, root: Path, is_task: bool, errors: list[str]) -> Plan | None:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r"<!--\s*storehouse-plan\s*\n(.*?)\n\s*-->", text, re.DOTALL)
    label = _display(path, root)
    if len(matches) != 1:
        errors.append(f"{label}: expected exactly one storehouse-plan control block")
        return None
    control = _parse_key_values(matches[0], label, errors)
    missing = REQUIRED_KEYS - control.keys()
    unknown = control.keys() - REQUIRED_KEYS
    if missing:
        errors.append(f"{label}: missing control fields: {', '.join(sorted(missing))}")
    if unknown:
        errors.append(f"{label}: unknown control fields: {', '.join(sorted(unknown))}")
    return Plan(path, label, text, control, is_task)


def _section(text: str, heading: str) -> str | None:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else None


def _bullet_fields(section: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for key, value in re.findall(r"^-\s+([^:\n]+):\s*(.+)$", section, re.MULTILINE)
    }


def _visible_text(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _parse_phases(plan: Plan, errors: list[str]) -> dict[str, str]:
    phases: dict[str, str] = {}
    lines = _visible_text(plan.text).splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^###\s+(P\d+)\s+-\s+\S", line)
        if not match:
            continue
        phase_id = match.group(1)
        if phase_id in phases:
            errors.append(f"{plan.relative_path}: duplicate phase '{phase_id}'")
            continue
        state = None
        for detail in lines[index + 1 :]:
            if detail.startswith("### ") or detail.startswith("## "):
                break
            state_match = re.match(r"^- State:\s*(\S+)\s*$", detail)
            if state_match:
                state = state_match.group(1)
                break
        if state is None:
            errors.append(f"{plan.relative_path}: phase {phase_id} is missing '- State:'")
        else:
            phases[phase_id] = state
    if not phases:
        errors.append(f"{plan.relative_path}: task plan must define at least one phase")
    for phase_id, state in phases.items():
        if state not in PHASE_STATES:
            errors.append(f"{plan.relative_path}: phase {phase_id} has invalid state '{state}'")
    return phases


def _validate_phase_lifecycle(plan: Plan, phases: dict[str, str], errors: list[str]) -> None:
    status = plan.control.get("status")
    active_phase = plan.control.get("active_phase")
    active = [phase for phase, state in phases.items() if state == "ACTIVE"]
    blocked = [phase for phase, state in phases.items() if state == "BLOCKED"]
    if status in {"DRAFT", "SUPERSEDED", "COMPLETE"} and active_phase != "NONE":
        errors.append(f"{plan.relative_path}: {status} requires active_phase: NONE")
    if status == "DRAFT" and (active or blocked):
        errors.append(f"{plan.relative_path}: DRAFT phases cannot be ACTIVE or BLOCKED")
    if status == "READY":
        if active_phase not in phases or phases.get(active_phase) != "PENDING":
            errors.append(f"{plan.relative_path}: READY active_phase must name a PENDING phase")
        if active or blocked:
            errors.append(f"{plan.relative_path}: READY cannot contain ACTIVE or BLOCKED phases")
    if status == "IN_PROGRESS":
        if len(active) != 1 or active_phase != active[0]:
            errors.append(f"{plan.relative_path}: IN_PROGRESS requires one matching ACTIVE phase")
        if blocked:
            errors.append(f"{plan.relative_path}: IN_PROGRESS cannot contain a BLOCKED phase")
    if status == "BLOCKED":
        if len(blocked) != 1 or active_phase != blocked[0]:
            errors.append(f"{plan.relative_path}: BLOCKED requires one matching BLOCKED phase")
        if active:
            errors.append(f"{plan.relative_path}: BLOCKED cannot contain an ACTIVE phase")
    if status == "COMPLETE" and any(state not in {"COMPLETE", "SKIPPED"} for state in phases.values()):
        errors.append(f"{plan.relative_path}: COMPLETE requires every phase COMPLETE or SKIPPED")


def _ownership_overlaps(left: str, right: str) -> bool:
    def base(value: str) -> str:
        value = value.strip().replace("\\", "/").rstrip("/")
        return value[:-3].rstrip("/") if value.endswith("/**") else value

    left_base, right_base = base(left), base(right)
    return (
        left_base == right_base
        or left_base.startswith(f"{right_base}/")
        or right_base.startswith(f"{left_base}/")
    )


def _sequential_owned_paths(value: str, label: str, errors: list[str]) -> list[str]:
    paths = [item.strip().replace("\\", "/") for item in value.split(",") if item.strip()]
    for item in paths:
        path = PurePosixPath(item)
        stem = item[:-3] if item.endswith("/**") else item
        if (
            path.is_absolute() or ":" in item or ".." in path.parts
            or stem in {"", ".", "/"} or "*" in stem or "?" in item
            or "[" in item or "]" in item
        ):
            errors.append(f"{label}: unbounded or invalid owned path '{item}'")
    return [PurePosixPath(item).as_posix() for item in paths]


def _validate_sequential_worker(plan: Plan, workers: list[re.Match], errors: list[str]) -> None:
    label = plan.relative_path
    if len(workers) != 1:
        errors.append(f"{label}: SEQUENTIAL_WORKER requires exactly one worker section")
        return
    fields = _bullet_fields(workers[0].group(2))
    keys = re.findall(r"^-\s+([^:\n]+):", workers[0].group(2), re.MULTILINE)
    if len(keys) != len(set(key.strip() for key in keys)):
        errors.append(f"{label}: sequential worker has duplicate contract fields")
    required = (
        "Project", "Model", "Reasoning", "Max workers", "Owns", "Deliverable",
        "Coordinator owns", "Review owner",
    )
    for key in required:
        if not fields.get(key):
            errors.append(f"{label}: sequential worker is missing {key}")
    if fields.get("Project") != "unified-selling" or not plan.control.get("id", "").startswith("selling-"):
        errors.append(f"{label}: SEQUENTIAL_WORKER is limited to unified-selling task IDs starting selling-")
    model = fields.get("Model")
    if model not in SELLING_WORKER_MODELS or fields.get("Reasoning") != SELLING_WORKER_MODELS.get(model):
        errors.append(f"{label}: sequential worker requires Terra medium, Sol high or Claude Opus 5.5 high")
    if fields.get("Max workers") != "1":
        errors.append(f"{label}: sequential worker Max workers must be 1")
    if fields.get("Review owner") != "PRIMARY":
        errors.append(f"{label}: sequential worker Review owner must be PRIMARY")
    owned = _sequential_owned_paths(fields.get("Owns", ""), label, errors)
    coordinator = _sequential_owned_paths(fields.get("Coordinator owns", ""), label, errors)
    if not owned or not coordinator:
        errors.append(f"{label}: sequential worker and coordinator must have owned paths")
    for path in owned:
        for protected in COORDINATOR_ONLY_PATHS:
            if _ownership_overlaps(path.casefold(), protected.casefold()):
                errors.append(f"{label}: worker ownership includes coordinator-only path '{path}'")
                break
        if any(_ownership_overlaps(path.casefold(), other.casefold()) for other in coordinator):
            errors.append(f"{label}: sequential worker/coordinator ownership overlaps: '{path}'")


def _validate_agent_strategy(plan: Plan, errors: list[str]) -> None:
    strategy = plan.control.get("agent_strategy")
    if strategy not in AGENT_STRATEGIES:
        errors.append(f"{plan.relative_path}: invalid agent_strategy '{strategy}'")
        return
    if not plan.is_task:
        if strategy == "SEQUENTIAL_WORKER":
            errors.append(f"{plan.relative_path}: SEQUENTIAL_WORKER requires a task lifecycle plan")
        return
    visible = _visible_text(plan.text)
    lane_matches = list(
        re.finditer(
            r"^### Lane:\s*([^\n]+)$\n(.*?)(?=^###\s|^##\s|\Z)",
            visible,
            re.MULTILINE | re.DOTALL,
        )
    )
    worker_matches = list(re.finditer(
        r"^### Worker:\s*([^\n]+)$\n(.*?)(?=^###\s|^##\s|\Z)",
        visible, re.MULTILINE | re.DOTALL,
    ))
    if strategy == "SEQUENTIAL_WORKER":
        if lane_matches:
            errors.append(f"{plan.relative_path}: SEQUENTIAL_WORKER must not define parallel lanes")
        _validate_sequential_worker(plan, worker_matches, errors)
        return
    if worker_matches:
        errors.append(f"{plan.relative_path}: worker sections require SEQUENTIAL_WORKER")
    if strategy == "PRIMARY":
        if lane_matches:
            errors.append(f"{plan.relative_path}: PRIMARY plan must not define agent lanes")
        return
    if len(lane_matches) != 2:
        errors.append(f"{plan.relative_path}: DISJOINT_LANES requires exactly two lane sections")
        return
    lane_ownership: list[list[str]] = []
    for lane in lane_matches:
        name = lane.group(1).strip()
        fields = _bullet_fields(lane.group(2))
        for required in ("Owns", "Deliverable", "Depends on"):
            if not fields.get(required):
                errors.append(f"{plan.relative_path}: lane '{name}' is missing {required}")
        ownership = [item.strip() for item in fields.get("Owns", "").split(",") if item.strip()]
        if not ownership:
            errors.append(f"{plan.relative_path}: lane '{name}' has no owned paths")
        for item in ownership:
            if item in {".", "/", "*", "**", "**/*"} or ("*" in item and not item.endswith("/**")):
                errors.append(f"{plan.relative_path}: lane '{name}' has unbounded ownership '{item}'")
        lane_ownership.append(ownership)
    if len(lane_ownership) == 2:
        for left in lane_ownership[0]:
            for right in lane_ownership[1]:
                if _ownership_overlaps(left, right):
                    errors.append(
                        f"{plan.relative_path}: agent lane ownership overlaps: '{left}' and '{right}'"
                    )


def _validate_control(plan: Plan, errors: list[str]) -> None:
    control = plan.control
    status = control.get("status")
    if status not in STATUSES:
        errors.append(f"{plan.relative_path}: invalid status '{status}'")
    plan_id = control.get("id", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", plan_id):
        errors.append(f"{plan.relative_path}: id must use lowercase hyphenated words")
    implementation = control.get("implementation_authority")
    if implementation not in IMPLEMENTATION_AUTHORITIES:
        errors.append(f"{plan.relative_path}: invalid implementation_authority '{implementation}'")
    expected_implementation = {
        "DRAFT": "PLAN_ONLY",
        "READY": "GOAL_REQUIRED",
        "IN_PROGRESS": "GOAL_GRANTED",
        "BLOCKED": "GOAL_GRANTED",
        "COMPLETE": "CLOSED",
        "SUPERSEDED": "CLOSED",
    }.get(status)
    if expected_implementation and implementation != expected_implementation:
        errors.append(
            f"{plan.relative_path}: {status} requires implementation_authority: {expected_implementation}"
        )
    if control.get("merge_authority") not in MERGE_AUTHORITIES:
        errors.append(f"{plan.relative_path}: invalid merge_authority '{control.get('merge_authority')}'")
    if control.get("release_authority") != "OWNER_REQUIRED":
        errors.append(f"{plan.relative_path}: release_authority must be OWNER_REQUIRED")
    for key in IMPACT_KEYS:
        value = control.get(key, "")
        prefix = value.split(maxsplit=1)[0] if value else ""
        if prefix not in IMPACT_PREFIXES:
            errors.append(f"{plan.relative_path}: {key} must start with NONE or PRESENT")
    if status != "DRAFT":
        if control.get("tbd") != "NONE":
            errors.append(f"{plan.relative_path}: {status} requires tbd: NONE")
        if UNRESOLVED_RE.search(_visible_text(plan.text)):
            errors.append(f"{plan.relative_path}: {status} contains an unresolved TBD/TODO placeholder")


def _validate_terminal_evidence(plan: Plan, errors: list[str]) -> None:
    status = plan.control.get("status")
    if status == "COMPLETE":
        section = _section(plan.text, "Completion evidence")
        if section is None:
            errors.append(f"{plan.relative_path}: COMPLETE requires a Completion evidence section")
            return
        fields = _bullet_fields(section)
        missing = COMPLETE_EVIDENCE - fields.keys()
        if missing:
            errors.append(f"{plan.relative_path}: missing completion evidence: {', '.join(sorted(missing))}")
        for key in COMPLETE_EVIDENCE & fields.keys():
            evidence_status = fields[key].split(maxsplit=1)[0].rstrip(":").upper()
            if (
                UNRESOLVED_RE.search(fields[key])
                or fields[key].upper() in {"NONE", "N/A"}
                or evidence_status in NON_SUCCESS_EVIDENCE
            ):
                errors.append(f"{plan.relative_path}: completion evidence '{key}' is unresolved")
    if status == "SUPERSEDED":
        section = _section(plan.text, "Supersession evidence")
        fields = _bullet_fields(section or "")
        for key in ("Replacement", "Reason"):
            if not fields.get(key) or UNRESOLVED_RE.search(fields[key]):
                errors.append(f"{plan.relative_path}: SUPERSEDED requires supersession evidence '{key}'")


def _parse_inbox(root: Path, errors: list[str]) -> set[str]:
    path = root / "docs/plans/INBOX.md"
    if not path.is_file():
        errors.append("docs/plans/INBOX.md: file is missing")
        return set()
    entries: set[str] = set()
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)\s*$")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        match = pattern.fullmatch(line[2:].strip())
        if not match:
            errors.append(
                "docs/plans/INBOX.md: "
                f"line {line_number} must be one status-free plan link"
            )
            continue
        raw_target = match.group(1)
        target = (path.parent / raw_target).resolve()
        try:
            relative = target.relative_to(root.resolve()).as_posix()
        except ValueError:
            errors.append(f"docs/plans/INBOX.md: target escapes repository: {raw_target}")
            continue
        if relative in entries:
            errors.append(f"docs/plans/INBOX.md: duplicate plan entry for {relative}")
        else:
            entries.add(relative)
    return entries


def _parse_active(root: Path, errors: list[str]) -> str | None:
    path = root / "docs/plans/ACTIVE.md"
    if not path.is_file():
        errors.append("docs/plans/ACTIVE.md: file is missing")
        return None
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r"<!--\s*storehouse-active\s*\n(.*?)\n\s*-->", text, re.DOTALL)
    if len(matches) != 1:
        errors.append("docs/plans/ACTIVE.md: expected exactly one storehouse-active block")
        return None
    fields = _parse_key_values(matches[0], "docs/plans/ACTIVE.md", errors)
    if set(fields) != {"target"}:
        errors.append("docs/plans/ACTIVE.md: active block must contain only target")
    return fields.get("target")


def validate_repository(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    task_dir = root / "docs/plans/tasks"
    task_paths = sorted(path for path in task_dir.glob("*.md") if path.name != "README.md")
    marked_paths: set[Path] = set()
    for path in (root / "docs").rglob("*.md"):
        if "docs/plans/templates" in path.as_posix():
            continue
        if re.search(r"<!--\s*storehouse-plan\s*\n", path.read_text(encoding="utf-8")):
            marked_paths.add(path.resolve())
    for path in task_paths:
        if path.resolve() not in marked_paths:
            errors.append(f"{_display(path, root)}: task markdown is missing a storehouse-plan block")
    plans: list[Plan] = []
    for path in sorted(marked_paths):
        plan = _parse_plan(path, root, path.parent.resolve() == task_dir.resolve(), errors)
        if plan:
            plans.append(plan)
            _validate_control(plan, errors)
            _validate_agent_strategy(plan, errors)
            _validate_terminal_evidence(plan, errors)
            if plan.is_task:
                _validate_phase_lifecycle(plan, _parse_phases(plan, errors), errors)
    ids: dict[str, str] = {}
    for plan in plans:
        plan_id = plan.control.get("id")
        if plan_id in ids:
            errors.append(f"{plan.relative_path}: duplicate plan id also used by {ids[plan_id]}")
        elif plan_id:
            ids[plan_id] = plan.relative_path
    inbox = _parse_inbox(root, errors)
    plan_by_path = {plan.relative_path: plan for plan in plans}
    for relative in plan_by_path:
        if relative not in inbox:
            errors.append(f"docs/plans/INBOX.md: missing entry for {relative}")
    for relative in inbox:
        if relative not in plan_by_path:
            errors.append(f"docs/plans/INBOX.md: {relative} is not a lifecycle plan")
    active_target = _parse_active(root, errors)
    executable = [plan for plan in plans if plan.is_task and plan.control.get("status") in EXECUTABLE_STATUSES]
    if active_target == "NONE":
        if executable:
            errors.append("docs/plans/ACTIVE.md: target is NONE but an executable task exists")
    elif active_target:
        active_path = (root / active_target).resolve()
        try:
            relative = active_path.relative_to(root).as_posix()
        except ValueError:
            errors.append(f"docs/plans/ACTIVE.md: target escapes repository: {active_target}")
        else:
            target_plan = plan_by_path.get(relative)
            if not target_plan or not target_plan.is_task:
                errors.append("docs/plans/ACTIVE.md: target must be a task lifecycle plan")
            elif target_plan.control.get("status") not in EXECUTABLE_STATUSES:
                errors.append("docs/plans/ACTIVE.md: target status must be READY, IN_PROGRESS, or BLOCKED")
            if len(executable) != 1 or not executable or executable[0].relative_path != relative:
                errors.append("docs/plans/ACTIVE.md: target must be the only executable task")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = validate_repository(args.root)
    if errors:
        print("Plan validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Plan validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
