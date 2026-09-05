"""Static contracts for the CI failure-log artifact."""

import re
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
UPLOAD_ARTIFACT = (
    "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
)


def workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


def named_step(source, name):
    match = re.search(
        r"^      - name: " + re.escape(name) + r"\n(?P<body>.*?)(?=^      - name: |^  \S|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("workflow step not found: " + name)
    return match.group(0)


class WorkflowFailureLogTest(unittest.TestCase):
    def test_protocol_suite_tees_both_streams_and_preserves_its_exit_code(self):
        step = named_step(workflow_text(), "Run the Python and MCP protocol suites")
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            for name, variable in (("npm", "NPM_TEST_STATUS"),
                                   ("python3", "CI_TEST_STATUS")):
                executable = binaries / name
                executable.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "' + name + '-stdout"\n'
                    'printf "%s\\n" "' + name + '-stderr" >&2\n'
                    'exit "$' + variable + '"\n', encoding="ascii")
                executable.chmod(0o755)
            for npm_status, ci_status, expected in ((0, 0, 0), (23, 0, 23), (0, 29, 29)):
                with self.subTest(npm=npm_status, ci=ci_status):
                    env = dict(os.environ, RUNNER_TEMP=directory,
                               PATH=str(binaries) + os.pathsep + os.environ["PATH"],
                               NPM_TEST_STATUS=str(npm_status), CI_TEST_STATUS=str(ci_status))
                    done = subprocess.run(["bash", "-c", script], env=env,
                                          capture_output=True, text=True, timeout=10)
                    self.assertEqual(done.returncode, expected, done.stderr)
                    expected_output = "npm-stdout\nnpm-stderr\n"
                    if npm_status == 0:
                        expected_output += "python3-stdout\npython3-stderr\n"
                    self.assertEqual(done.stdout, expected_output)
                    self.assertEqual((root / "antiphon-test-output.log").read_text(),
                                     expected_output)
            # A broken logger must not turn a successful suite into a green job.
            tee = binaries / "tee"
            tee.write_text('#!/bin/sh\ncat >/dev/null\nexit 31\n', encoding="ascii")
            tee.chmod(0o755)
            env.update(NPM_TEST_STATUS="0", CI_TEST_STATUS="0")
            done = subprocess.run(["bash", "-c", script], env=env,
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(done.returncode, 31, done.stderr)

    def test_failure_artifact_is_scoped_unique_and_short_lived(self):
        step = named_step(workflow_text(), "Upload failing protocol-suite output")
        self.assertIn(
            "        if: ${{ failure() && steps.protocol_suites.outcome == 'failure' }}\n",
            step,
        )
        self.assertIn("        uses: " + UPLOAD_ARTIFACT + " # v6.0.0\n", step)
        self.assertIn(
            "          name: antiphon-test-output-${{ matrix.name }}-${{ github.run_attempt }}\n",
            step,
        )
        self.assertIn(
            "          path: ${{ runner.temp }}/antiphon-test-output.log\n", step
        )
        self.assertIn("          if-no-files-found: error\n", step)
        self.assertIn("          retention-days: 3\n", step)
        self.assertNotIn("secrets.", step)
        self.assertNotIn("publish", step.lower())


if __name__ == "__main__":
    unittest.main()
