"""Offline tests for exporting reviewer calls from private runner evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.coordination.export_reviews import export, main


REVIEW = {"candidate": "c" * 64, "covered_files": ["b.py", "a.py"],
          "acceptance": ["a.py handles empty input"], "findings": ["a.py: crashes on empty input"]}


def write_run(run_dir, *, results, diffs, usage=()):
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"phase": "STOPPED", "usage": list(usage)}))
    (run_dir / "packet.json").write_text(json.dumps({
        "id": "selling-sample", "base_sha": "b" * 40, "objective": "Handle empty input",
        "acceptance": REVIEW["acceptance"]}))
    for number, result in results.items():
        (run_dir / f"result-{number}.json").write_text(json.dumps(result))
    for name, text in diffs.items():
        (run_dir / name).write_text(text)


class ExportReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.evidence = self.home / "evidence"
        self.output = self.home / "cases"

    def test_exports_every_reviewed_candidate_with_its_own_diff(self):
        write_run(self.evidence / "batches/one/run", usage=[{"call": 2, "role": "reviewer", "reported": [{"input_tokens": 5}]}],
                  results={1: {"patch": "x", "summary": "worker"}, 2: REVIEW,
                           3: {"patch": "y", "summary": "fix"}, 4: dict(REVIEW, findings=[])},
                  diffs={"candidate-2.diff": "first\n", "candidate-4.diff": "second\n", "candidate.diff": "second\n"})
        names = export(self.evidence, self.output)
        self.assertEqual(names, ["batches__one__run-call-2.json", "batches__one__run-call-4.json"])
        first = json.loads((self.output / names[0]).read_text())
        self.assertEqual(first["diff"], "first\n")
        self.assertEqual(first["files"], ["a.py", "b.py"])
        self.assertEqual(first["findings"], REVIEW["findings"])
        self.assertEqual(first["usage"], [{"input_tokens": 5}])
        self.assertEqual(first["base_sha"], "b" * 40)
        self.assertEqual(first["run"], "batches/one/run")
        second = json.loads((self.output / names[1]).read_text())
        self.assertEqual((second["diff"], second["findings"], second["usage"]), ("second\n", [], None))

    def test_legacy_run_exports_only_the_final_review(self):
        write_run(self.evidence / "legacy", results={2: REVIEW, 4: dict(REVIEW, findings=[])},
                  diffs={"candidate.diff": "latest\n"})
        names = export(self.evidence, self.output)
        self.assertEqual(names, ["legacy-call-4.json"])
        self.assertEqual(json.loads((self.output / names[0]).read_text())["diff"], "latest\n")

    def test_skips_runs_without_review_or_with_broken_state(self):
        write_run(self.evidence / "applied-only", results={1: {"patch": "x", "summary": "s"}}, diffs={})
        broken = self.evidence / "broken"
        broken.mkdir()
        (broken / "state.json").write_text("{not json")
        self.assertEqual(export(self.evidence, self.output), [])

    def test_output_inside_evidence_is_refused(self):
        self.evidence.mkdir()
        with self.assertRaises(SystemExit):
            export(self.evidence, self.evidence / "cases")

    def test_main_reports_count(self):
        write_run(self.evidence / "run", results={2: REVIEW}, diffs={"candidate-2.diff": "d\n"})
        self.assertEqual(main([str(self.evidence), "--output", str(self.output)]), 0)
        self.assertTrue((self.output / "run-call-2.json").is_file())


if __name__ == "__main__":
    unittest.main()
