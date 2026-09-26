from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.validate_plans import validate_repository


class PlanValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "docs/plans/tasks").mkdir(parents=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_repository(
        self,
        *,
        status: str = "COMPLETE",
        active_phase: str = "NONE",
        phase_state: str = "COMPLETE",
        strategy: str = "PRIMARY",
        implementation_authority: str = "CLOSED",
        tbd: str = "NONE",
        target: str = "NONE",
        lanes: str = "",
        plan_id: str = "example",
        evidence: str | None = None,
    ) -> Path:
        if evidence is None:
            evidence = """
## Completion evidence

- Outcome: The focused outcome works.
- Verification: Focused and baseline checks passed.
- Review: Diff and safety review passed.
- Pull request: GITHUB_AUTHORITATIVE - final-head checks and review live in PR metadata.
- Merge: OWNER_GATE - protected-main merge remains an external owner decision.
- Production: NOT DEPLOYED - separately owner controlled.
"""
        plan = self.root / "docs/plans/tasks/example.md"
        plan.write_text(
            f"""# Example plan

<!-- storehouse-plan
id: {plan_id}
status: {status}
active_phase: {active_phase}
agent_strategy: {strategy}
implementation_authority: {implementation_authority}
merge_authority: OWNER_REQUIRED
release_authority: OWNER_REQUIRED
schema_impact: NONE - No schema change.
data_impact: NONE - No data change.
valuation_impact: NONE - No valuation change.
reporting_impact: NONE - No reporting change.
marketplace_write_impact: NONE - No marketplace write.
production_impact: NONE - No deployment.
tbd: {tbd}
-->

## Agent strategy

Primary agent owns the task.
{lanes}
## Phases

### P1 - Implement

- State: {phase_state}
- Depends on: NONE
- Files: example.py
- Acceptance: Behaviour is verified.
{evidence}
""",
            encoding="utf-8",
        )
        (self.root / "docs/plans/INBOX.md").write_text(
            "# Inbox\n\n## Plans\n\n- [Example](tasks/example.md)\n",
            encoding="utf-8",
        )
        (self.root / "docs/plans/ACTIVE.md").write_text(
            f"# Active\n\n<!-- storehouse-active\ntarget: {target}\n-->\n",
            encoding="utf-8",
        )
        return plan

    def assert_has_error(self, errors: list[str], fragment: str) -> None:
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_valid_complete_plan(self):
        self.write_repository()
        self.assertEqual(validate_repository(self.root), [])

    def test_valid_ready_primary_plan(self):
        self.write_repository(
            status="READY",
            active_phase="P1",
            phase_state="PENDING",
            implementation_authority="GOAL_REQUIRED",
            target="docs/plans/tasks/example.md",
            evidence="",
        )
        self.assertEqual(validate_repository(self.root), [])

    def test_valid_ready_disjoint_lanes(self):
        lanes = """
### Lane: application

- Owns: operations/views.py
- Deliverable: Focused application change
- Depends on: NONE

### Lane: documentation

- Owns: docs/plans/**
- Deliverable: Focused documentation change
- Depends on: NONE

"""
        self.write_repository(
            status="READY",
            active_phase="P1",
            phase_state="PENDING",
            strategy="DISJOINT_LANES",
            implementation_authority="GOAL_REQUIRED",
            target="docs/plans/tasks/example.md",
            lanes=lanes,
            evidence="",
        )
        self.assertEqual(validate_repository(self.root), [])

    def test_rejects_invalid_plan_status(self):
        self.write_repository(status="DONE")
        self.assert_has_error(validate_repository(self.root), "invalid status 'DONE'")

    def test_rejects_executable_plan_without_active_target(self):
        self.write_repository(
            status="READY",
            active_phase="P1",
            phase_state="PENDING",
            implementation_authority="GOAL_REQUIRED",
            evidence="",
        )
        self.assert_has_error(validate_repository(self.root), "target is NONE but an executable task exists")

    def test_rejects_active_target_that_is_not_the_executable_plan(self):
        self.write_repository(target="docs/plans/tasks/example.md")
        self.assert_has_error(validate_repository(self.root), "target status must be READY")

    def test_rejects_phase_state_inconsistent_with_status(self):
        self.write_repository(
            status="IN_PROGRESS",
            active_phase="P1",
            phase_state="PENDING",
            implementation_authority="GOAL_GRANTED",
            target="docs/plans/tasks/example.md",
            evidence="",
        )
        self.assert_has_error(validate_repository(self.root), "requires one matching ACTIVE phase")

    def test_rejects_invalid_agent_strategy(self):
        self.write_repository(strategy="HELPERS")
        self.assert_has_error(validate_repository(self.root), "invalid agent_strategy 'HELPERS'")

    def selling_worker(self, **overrides):
        fields = {
            "Project": "unified-selling",
            "Model": "gpt-5.6-terra",
            "Reasoning": "medium",
            "Max workers": "1",
            "Owns": "operations/selling_workspace.py, operations/test_selling_workspace.py",
            "Deliverable": "Implement the frozen read-only Selling page and focused checks.",
            "Coordinator owns": "docs/plans/tasks/example.md, docs/plans/ACTIVE.md, docs/projects/unified-selling.md",
            "Review owner": "PRIMARY",
        }
        fields.update(overrides)
        return "\n### Worker: implementation\n\n" + "\n".join(
            f"- {key}: {value}" for key, value in fields.items() if value is not None
        ) + "\n"

    def write_selling_worker_plan(self, worker=None, **overrides):
        arguments = dict(
            status="READY", active_phase="P1", phase_state="PENDING",
            strategy="SEQUENTIAL_WORKER", implementation_authority="GOAL_REQUIRED",
            target="docs/plans/tasks/example.md", plan_id="selling-example", evidence="",
            lanes=self.selling_worker() if worker is None else worker,
        )
        arguments.update(overrides)
        return self.write_repository(**arguments)

    def test_valid_ready_sequential_worker_models(self):
        for model, reasoning in (("gpt-5.6-terra", "medium"), ("gpt-5.6-sol", "high"), ("claude-opus-5-5", "high")):
            with self.subTest(model=model):
                self.write_selling_worker_plan(self.selling_worker(Model=model, Reasoning=reasoning))
                self.assertEqual(validate_repository(self.root), [])

    def test_sequential_worker_still_requires_execution_authority(self):
        self.write_selling_worker_plan(
            status="IN_PROGRESS", phase_state="ACTIVE", implementation_authority="GOAL_REQUIRED",
        )
        self.assert_has_error(validate_repository(self.root), "IN_PROGRESS requires implementation_authority: GOAL_GRANTED")

    def test_rejects_multiple_or_missing_sequential_workers(self):
        for workers in ("", self.selling_worker() * 2):
            with self.subTest(workers=bool(workers)):
                self.write_selling_worker_plan(workers)
                self.assert_has_error(validate_repository(self.root), "requires exactly one worker section")
        self.write_selling_worker_plan(self.selling_worker(**{"Max workers": "2"}))
        self.assert_has_error(validate_repository(self.root), "Max workers must be 1")

    def test_sequential_worker_is_scoped_to_the_selling_project(self):
        for plan_id, project in (("unrelated", "unified-selling"), ("selling-example", "other")):
            with self.subTest(plan_id=plan_id, project=project):
                self.write_selling_worker_plan(self.selling_worker(Project=project), plan_id=plan_id)
                self.assert_has_error(validate_repository(self.root), "limited to unified-selling")

    def test_rejects_missing_worker_contract_fields(self):
        for field in ("Project", "Model", "Reasoning", "Max workers", "Owns", "Deliverable", "Coordinator owns", "Review owner"):
            with self.subTest(field=field):
                self.write_selling_worker_plan(self.selling_worker(**{field: None}))
                self.assert_has_error(validate_repository(self.root), f"missing {field}")

    def test_rejects_unbounded_escaped_and_protected_worker_paths(self):
        invalid = (".", "**", "../operations/**", "C:/outside.py", "/tmp/file.py", "operations/*", "operations/[ab].py")
        for path in invalid:
            with self.subTest(path=path):
                self.write_selling_worker_plan(self.selling_worker(Owns=path))
                self.assert_has_error(validate_repository(self.root), "unbounded or invalid owned path")
        for path in ("AGENTS.md", "./AGENTS.md", "agents.md", "docs/plans/**", "docs/**", ".agents/config.md", ".github/workflows/ci.yml", ".git/config", "scripts/validate_plans.py", "scripts/verify_main_provenance.py", "scripts/verify_release_ci.py"):
            with self.subTest(path=path):
                self.write_selling_worker_plan(self.selling_worker(Owns=path))
                self.assert_has_error(validate_repository(self.root), "coordinator-only path")

    def test_rejects_overlapping_sequential_ownership(self):
        self.write_selling_worker_plan(self.selling_worker(**{"Coordinator owns": "operations/**"}))
        self.assert_has_error(validate_repository(self.root), "worker/coordinator ownership overlaps")

    def test_rejects_worker_model_or_review_drift(self):
        for overrides, error in (
            ({"Model": "gpt-6-astra"}, "Terra medium, Sol high or Claude Opus 5.5 high"),
            ({"Reasoning": "low"}, "Terra medium, Sol high or Claude Opus 5.5 high"),
            ({"Model": "claude-opus-5-5", "Reasoning": "medium"}, "Terra medium, Sol high or Claude Opus 5.5 high"),
            ({"Review owner": "WORKER"}, "Review owner must be PRIMARY"),
        ):
            with self.subTest(overrides=overrides):
                self.write_selling_worker_plan(self.selling_worker(**overrides))
                self.assert_has_error(validate_repository(self.root), error)

    def test_rejects_hidden_delegation_in_primary_mode(self):
        self.write_selling_worker_plan(strategy="PRIMARY")
        self.assert_has_error(validate_repository(self.root), "worker sections require SEQUENTIAL_WORKER")

    def test_rejects_mixing_parallel_and_sequential_modes(self):
        lanes = self.selling_worker() + "\n### Lane: extra\n\n- Owns: another.py\n"
        self.write_selling_worker_plan(lanes)
        self.assert_has_error(validate_repository(self.root), "must not define parallel lanes")

    def test_rejects_duplicate_worker_limits(self):
        worker = self.selling_worker().replace("- Max workers: 1", "- Max workers: 2\n- Max workers: 1")
        self.write_selling_worker_plan(worker)
        self.assert_has_error(validate_repository(self.root), "duplicate contract fields")

    def test_rejects_sequential_strategy_outside_task_directory(self):
        plan = self.write_selling_worker_plan(
            status="DRAFT", active_phase="NONE", implementation_authority="PLAN_ONLY", target="NONE",
        )
        plan.rename(self.root / "docs/legacy.md")
        (self.root / "docs/plans/INBOX.md").write_text(
            "# Inbox\n\n- [Example](../legacy.md)\n", encoding="utf-8",
        )
        self.assert_has_error(validate_repository(self.root), "requires a task lifecycle plan")

    def test_rejects_overlapping_disjoint_lanes(self):
        lanes = """
### Lane: application

- Owns: operations/**
- Deliverable: Application change
- Depends on: NONE

### Lane: tests

- Owns: operations/tests/**
- Deliverable: Focused tests
- Depends on: NONE

"""
        self.write_repository(strategy="DISJOINT_LANES", lanes=lanes)
        self.assert_has_error(validate_repository(self.root), "agent lane ownership overlaps")

    def test_rejects_missing_required_impact_field(self):
        plan = self.write_repository()
        text = plan.read_text(encoding="utf-8").replace(
            "reporting_impact: NONE - No reporting change.\n", ""
        )
        plan.write_text(text, encoding="utf-8")
        self.assert_has_error(validate_repository(self.root), "missing control fields: reporting_impact")

    def test_rejects_invalid_authority_transition(self):
        self.write_repository(implementation_authority="GOAL_GRANTED")
        self.assert_has_error(validate_repository(self.root), "COMPLETE requires implementation_authority: CLOSED")

    def test_rejects_unresolved_tbd_after_draft(self):
        self.write_repository(
            status="READY",
            active_phase="P1",
            phase_state="PENDING",
            implementation_authority="GOAL_REQUIRED",
            target="docs/plans/tasks/example.md",
            tbd="Choose one option",
            evidence="",
        )
        self.assert_has_error(validate_repository(self.root), "READY requires tbd: NONE")

    def test_rejects_complete_plan_without_evidence(self):
        self.write_repository(evidence="")
        self.assert_has_error(validate_repository(self.root), "requires a Completion evidence section")

    def test_rejects_status_prefixed_inbox_entry(self):
        self.write_repository()
        inbox = self.root / "docs/plans/INBOX.md"
        inbox.write_text(
            inbox.read_text(encoding="utf-8").replace(
                "- [Example](tasks/example.md)",
                "- [COMPLETE] [Example](tasks/example.md)",
            ),
            encoding="utf-8",
        )
        self.assert_has_error(validate_repository(self.root), "must be one status-free plan link")

    def test_rejects_duplicate_inbox_entry(self):
        self.write_repository()
        inbox = self.root / "docs/plans/INBOX.md"
        inbox.write_text(
            inbox.read_text(encoding="utf-8") + "- [Example again](tasks/example.md)\n",
            encoding="utf-8",
        )
        self.assert_has_error(validate_repository(self.root), "duplicate plan entry")

    def test_rejects_missing_inbox_entry(self):
        self.write_repository()
        inbox = self.root / "docs/plans/INBOX.md"
        inbox.write_text("# Inbox\n\n## Plans\n", encoding="utf-8")
        self.assert_has_error(validate_repository(self.root), "missing entry")

    def test_rejects_complete_plan_with_failed_verification(self):
        evidence = """
## Completion evidence

- Outcome: The focused outcome works.
- Verification: FAILED - required checks did not pass.
- Review: Diff review passed.
- Pull request: GITHUB_AUTHORITATIVE - PR metadata owns final-head facts.
- Merge: OWNER_GATE - owner merge remains external.
- Production: NOT DEPLOYED - separately owner controlled.
"""
        self.write_repository(evidence=evidence)
        self.assert_has_error(validate_repository(self.root), "completion evidence 'Verification' is unresolved")


if __name__ == "__main__":
    unittest.main()
