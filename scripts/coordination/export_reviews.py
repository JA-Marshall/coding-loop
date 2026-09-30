"""Export every recorded reviewer call from private runner evidence as replay cases.

Run with python -m scripts.coordination.export_reviews EVIDENCE_ROOT --output DIR.

Each reviewer call becomes one JSON file named <run>-call-<n>.json holding the
exact diff the reviewer saw, the files and acceptance strings it was asked to
cover, the findings it returned, and the reported usage. A review benchmark can
replay these against other reviewers. Older runs only kept the latest candidate
diff, so only the final reviewer call of such a run is exported. Model logs and
prompts are not copied; they may contain diagnostic data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


REVIEW_KEYS = {"candidate", "covered_files", "findings"}  # older reviews also carry "acceptance"


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def review_calls(run_dir):
    """Yield (call number, result) for every stored result shaped like a review."""
    for result_path in sorted(run_dir.glob("result-*.json")):
        stem = result_path.stem.split("-", 1)[1]
        result = load_json(result_path)
        if stem.isdigit() and isinstance(result, dict) and set(result) - {"acceptance"} == REVIEW_KEYS:
            yield int(stem), result


def export_run(run_dir, evidence_root):
    state = load_json(run_dir / "state.json")
    packet = load_json(run_dir / "packet.json")
    if not isinstance(state, dict) or not isinstance(packet, dict):
        return []
    calls = list(review_calls(run_dir))
    usage = {u.get("call"): u.get("reported") for u in state.get("usage", []) if isinstance(u, dict)}
    cases = []
    for number, result in calls:
        diff_path = run_dir / f"candidate-{number}.diff"
        if not diff_path.is_file():
            if number != calls[-1][0] or not (run_dir / "candidate.diff").is_file():
                continue  # Earlier candidates of legacy runs were overwritten.
            diff_path = run_dir / "candidate.diff"
        cases.append({
            "run": run_dir.relative_to(evidence_root).as_posix(),
            "call": number,
            "packet_id": packet.get("id"),
            "base_sha": packet.get("base_sha"),
            "candidate": result["candidate"],
            "files": sorted(result["covered_files"]),
            "acceptance": packet.get("acceptance", []),
            "objective": packet.get("objective", ""),
            "diff": diff_path.read_text(errors="replace"),
            "findings": result["findings"],
            "usage": usage.get(number),
            "final_phase": state.get("phase"),
        })
    return cases


def export(evidence_root, output):
    evidence_root = Path(evidence_root).resolve()
    output = Path(output).resolve()
    if output == evidence_root or evidence_root in output.parents:
        raise SystemExit("Output must be outside the evidence root")
    output.mkdir(parents=True, exist_ok=True)
    written = []
    for state_path in sorted(evidence_root.rglob("state.json")):
        for case in export_run(state_path.parent, evidence_root):
            name = case["run"].replace("/", "__") + f"-call-{case['call']}.json"
            (output / name).write_text(json.dumps(case, indent=1, sort_keys=True))
            written.append(name)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evidence_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    written = export(args.evidence_root, args.output)
    print(f"Exported {len(written)} reviewer call(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
