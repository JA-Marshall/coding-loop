"""Explicit small live-model runner test; never touches application source.

Run: python -m scripts.coordination.smoke --live --directory /new/private/path
Add --exercise-recovery to inject one wrong-context patch and bad hunk counts.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .runner import CodexAdapter, Runner, RunnerError, git, save_json
from .isolated import IsolatedAdapter


FIXTURE = """def clamp_count(value, limit):
    \"\"\"Clamp an integer to the inclusive range zero..limit.\"\"\"
    return value
"""
VERIFY = """from fixture import clamp_count
assert clamp_count(-3, 5) == 0
assert clamp_count(3, 5) == 3
assert clamp_count(9, 5) == 5
assert clamp_count(0, 0) == 0
assert clamp_count(10, 0) == 0
try:
    clamp_count(1, -1)
except ValueError:
    pass
else:
    raise AssertionError('negative limit must raise ValueError')
print('Six clamp acceptance cases passed')
"""


class RecoveryProbe:
    """Fault injection around real model output, explicitly recorded as such."""
    def __init__(self):
        self.adapter = CodexAdapter()
        self.worker_calls = 0

    def __call__(self, runner, role, feedback):
        result = self.adapter(runner, role, feedback)
        if role == 'worker':
            self.worker_calls += 1
            if self.worker_calls == 1:
                result['patch'], count = re.subn(r'^-(?!--)([^\n]+)$',
                    '-INTENTIONALLY_INVALID_SMOKE_CONTEXT', result['patch'], count=1, flags=re.M)
                if not count:
                    raise RunnerError('Smoke fault injection requires a removed context line')
                fault = 'Injected invalid old-line context into first real worker patch'
            else:
                result['patch'] = re.sub(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@',
                    r'@@ -\1,999 +\2,999 @@', result['patch'])
                fault = 'Injected incorrect hunk counts into correction; recount must repair them'
            save_json(runner.run_dir / f'fault-{runner.state["calls"]}.json', {'fault': fault})
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly authorize small model test calls')
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--exercise-recovery', action='store_true')
    parser.add_argument('--isolated-auth-home', type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('--live is required; this test makes real model calls')
    directory = args.directory.resolve()
    if directory.exists():
        parser.error('Choose a new directory; existing test evidence is never reset')
    directory.mkdir(mode=0o700, parents=True)
    root = directory / 'fixture'
    root.mkdir()
    git(root, 'init', '-b', 'codex/runner-smoke')
    git(root, 'config', 'user.name', 'Runner synthetic fixture')
    git(root, 'config', 'user.email', 'runner@example.invalid')
    (root / 'fixture.py').write_text(FIXTURE)
    (root / 'verify.py').write_text(VERIFY)
    git(root, 'add', '--', 'fixture.py', 'verify.py')
    git(root, 'commit', '-m', 'Synthetic runner acceptance fixture')
    packet = {
        'id': 'runner-smoke', 'checkout': str(root), 'branch': 'codex/runner-smoke',
        'base_sha': git(root, 'rev-parse', 'HEAD').decode().strip(),
        'objective': 'Fix only fixture.py: clamp_count(value, limit) clamps integer value to inclusive zero..limit and raises ValueError for a negative limit. Inputs are integers. Read fixture.py and verify.py only. Do not change verify.py or add files. Keep the implementation minimal; return its unified diff.',
        'acceptance': ['Negative values clamp to zero; values above limit clamp to limit; in-range values are preserved.',
                       'Zero limit returns zero; negative limit raises ValueError.'],
        'owned_files': ['fixture.py'],
        'checks': [{'id': 'clamp-acceptance', 'argv': [sys.executable, '-B', 'verify.py'], 'timeout': 15}],
        'worker_model': 'gpt-5.6-sol', 'worker_reasoning': 'high',
        'plan': 'synthetic-fixture-authorized-by-explicit-live-test',
        'max_calls': 6, 'max_corrections': 2, 'call_timeout': 600, 'total_timeout': 1800,
        'luna_triage': False,
    }
    save_json(directory / 'packet.json', packet)
    adapter = RecoveryProbe() if args.exercise_recovery else CodexAdapter()
    if args.isolated_auth_home:
        if args.exercise_recovery:
            adapter.adapter = IsolatedAdapter(args.isolated_auth_home)
        else:
            adapter = IsolatedAdapter(args.isolated_auth_home)
    result = Runner(packet, directory / 'run', adapter).run()
    print(json.dumps({'phase': result['phase'], 'calls': result['calls'],
                      'corrections': result['corrections'], 'reason': result.get('reason'),
                      'report': str(directory / 'run/report.md')}, indent=2), flush=True)
    if result['phase'] != 'LOCAL_REVIEWED':
        return 1
    # Independent recheck of the delivered candidate, outside runner transition logic.
    subprocess.run([sys.executable, '-B', 'verify.py'], cwd=root, check=True,
                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=15)
    if args.exercise_recovery and result['corrections'] < 1:
        raise RunnerError('Recovery probe did not exercise a correction')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
