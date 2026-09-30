import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "coordination" / "derived_env.sh"

# Stands in for python3.12: "-m venv DIR" makes a directory whose pip only records what it was asked to install.
FAKE_PYTHON = """#!/bin/sh
[ "$1 $2" = "-m venv" ] || exit 9
mkdir -p "$3/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/python"
printf '#!/bin/sh\\necho "$@" >> "%s/pip.log"\\n' "$3" > "$3/bin/pip"
chmod +x "$3/bin/python" "$3/bin/pip"
"""


class DerivedEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.python = self.home / "python-fake"
        self.python.write_text(FAKE_PYTHON)
        self.python.chmod(0o755)
        self.envs = self.home / "envs"

    def run_script(self, *args):
        env = dict(os.environ, DERIVED_ENVS=str(self.envs), DERIVED_PYTHON=str(self.python))
        return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def test_an_environment_is_made_once_and_a_second_run_only_adds_what_is_missing(self):
        first = self.run_script("black")
        self.assertEqual(first.returncode, 0, first.stderr)
        wrapper = self.envs / "black" / "bin" / "pysrc"
        self.assertEqual(first.stdout.strip(), str(wrapper))
        self.assertEqual(wrapper.read_text(), '#!/bin/sh\nPYTHONPATH="$PWD/src:$PWD${PYTHONPATH:+:$PYTHONPATH}" exec '
                                              f'{self.envs}/black/bin/python "$@"\n')
        self.assertTrue(os.access(wrapper, os.X_OK))
        marker = self.envs / "black" / "bin" / "python"
        marker.write_text("#!/bin/sh\nexit 0\n# kept\n")  # an environment that exists is not rebuilt
        stamp = wrapper.stat().st_mtime_ns
        second = self.run_script("black")
        self.assertEqual((second.returncode, second.stdout), (0, first.stdout))
        self.assertIn("# kept", marker.read_text())
        self.assertEqual(wrapper.stat().st_mtime_ns, stamp)
        installs = (self.envs / "black" / "pip.log").read_text().splitlines()
        self.assertEqual(len(installs), 2)
        self.assertTrue(all("--upgrade" not in line and "tokenize-rt" in line for line in installs))

    def test_a_pydantic_environment_is_named_for_the_core_the_checkout_pins(self):
        checkout = self.home / "checkout"
        checkout.mkdir()
        self.assertEqual(self.run_script("pydantic").returncode, 2)
        (checkout / "pyproject.toml").write_text("dependencies = [\n    'pydantic-core==2.49.0',\n]\n")
        result = self.run_script("pydantic", str(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.envs / "pydantic-core-2.49.0" / "bin" / "pysrc"))
        self.assertIn("pydantic-core==2.49.0", (self.envs / "pydantic-core-2.49.0" / "pip.log").read_text())
        (checkout / "pyproject.toml").write_text("dependencies = []\n")
        self.assertEqual(self.run_script("pydantic", str(checkout)).returncode, 2)

    def test_an_unknown_repository_is_refused(self):
        for args in ((), ("django",)):
            result = self.run_script(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage", result.stderr)
        self.assertFalse(self.envs.exists())


if __name__ == "__main__":
    unittest.main()
