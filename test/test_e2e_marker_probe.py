import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "test" / "e2e" / "marker_probe.py"
TURN = ROOT / "test" / "e2e" / "marker_turn.sh"
CONTRACT = ROOT / "test" / "e2e" / "marker_contract.sh"


class ExactAssistantMarkerProbeTest(unittest.TestCase):

    def run_probe(self, root, marker):
        return subprocess.run(
            [sys.executable, str(PROBE), str(root), marker],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL)

    @staticmethod
    def write_records(root, name, records, *, complete=True, mtime=100):
        path = Path(root, name)
        with path.open("w", encoding="utf-8") as target:
            for index, record in enumerate(records):
                if isinstance(record, str):
                    encoded = record
                else:
                    encoded = json.dumps(record)
                target.write(encoded)
                if complete or index < len(records) - 1:
                    target.write("\n")
        os.utime(path, (mtime, mtime))
        return path

    @staticmethod
    def user(text):
        return {"type": "user", "message": {"content": text}}

    @staticmethod
    def assistant(text):
        return {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": text},
            ]},
        }

    def test_a_prompt_containing_the_marker_is_not_assistant_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            marker = "@codex exact-prompt-only"
            self.write_records(root, "prompt.jsonl", [self.user(marker)])
            done = self.run_probe(root, marker)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_a_non_directory_root_is_a_probe_error_not_no_match(self):
        with tempfile.TemporaryDirectory() as root:
            marker = "@codex bad-root"
            not_a_directory = Path(root, "transcript-root")
            not_a_directory.write_text("not a directory", encoding="utf-8")
            done = self.run_probe(not_a_directory, marker)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("transcript root", done.stderr)
        self.assertNotIn(marker, done.stderr)

    def test_only_an_exact_trimmed_assistant_text_block_matches(self):
        with tempfile.TemporaryDirectory() as root:
            marker = "@codex exact-answer"
            rejected = [
                self.assistant("preamble " + marker),
                self.assistant(marker + " suffix"),
                self.assistant("```\n" + marker + "\n```"),
            ]
            self.write_records(root, "rejected.jsonl", rejected, mtime=200)
            accepted = self.write_records(
                root, "accepted.jsonl", [self.assistant(" \n" + marker + "\n")],
                mtime=100)
            done = self.run_probe(root, marker)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), str(accepted))
        self.assertNotIn(marker, done.stdout)

    def test_malformed_and_partial_records_are_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            marker = "@codex incomplete"
            self.write_records(root, "malformed.jsonl", ["{broken"])
            self.write_records(
                root, "partial.jsonl", [self.assistant(marker)], complete=False)
            done = self.run_probe(root, marker)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_the_newest_exact_matching_transcript_is_selected(self):
        with tempfile.TemporaryDirectory() as root:
            marker = "@codex newest"
            self.write_records(root, "old.jsonl", [self.assistant(marker)], mtime=100)
            newest = self.write_records(
                root, "new.jsonl", [self.assistant(marker)], mtime=300)
            done = self.run_probe(root, marker)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), str(newest))


class BoundedMarkerTurnTest(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.transcripts = self.root / "transcripts"
        self.bin = self.root / "bin"
        self.project.mkdir()
        self.transcripts.mkdir()
        self.bin.mkdir()
        self.count = self.root / "count"
        fake = self.bin / "claude"
        fake.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import re
            import sys

            count_path = Path(os.environ["FAKE_CLAUDE_COUNT"])
            count = int(count_path.read_text() or "0") + 1 if count_path.exists() else 1
            count_path.write_text(str(count))
            mode = os.environ.get("FAKE_CLAUDE_MODE", "exact")
            if mode == "nonzero":
                raise SystemExit(7)
            if mode == "probe-error":
                raise SystemExit(0)
            prompt = sys.argv[-1]
            match = re.search(r"(@codex [^\\s]+)$", prompt)
            if not match:
                raise SystemExit(8)
            marker = match.group(1)
            answer = "marker omitted"
            if mode == "exact" or (mode == "omit-first" and count > 1):
                answer = marker
            records = [
                {"type": "user", "message": {"content": prompt}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": answer},
                ]}},
            ]
            path = Path(os.environ["FAKE_CLAUDE_DIR"], f"turn-{count}.jsonl")
            path.write_text("".join(json.dumps(item) + "\\n" for item in records))
            os.utime(path, (100 + count, 100 + count))
            """), encoding="utf-8")
        fake.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_turn(self, mode):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env["PATH"],
            "FAKE_CLAUDE_COUNT": str(self.count),
            "FAKE_CLAUDE_DIR": str(self.transcripts),
            "FAKE_CLAUDE_MODE": mode,
        })
        marker = "@codex bounded-marker"
        done = subprocess.run(
            ["bash", str(TURN), str(self.project), str(self.transcripts), marker],
            capture_output=True, text=True, timeout=20, env=env,
            stdin=subprocess.DEVNULL)
        return done, marker

    def test_an_exit_zero_omission_retries_only_until_attempt_two(self):
        done, marker = self.run_turn("omit-first")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.count.read_text(), "2")
        self.assertIn("attempt=2", done.stdout)
        self.assertIn("transcript=", done.stdout)
        self.assertNotIn(marker, done.stdout)

    def test_a_nonzero_cli_exit_is_not_retried(self):
        done, _ = self.run_turn("nonzero")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(self.count.read_text(), "1")

    def test_three_exact_marker_omissions_exhaust_the_bound(self):
        done, marker = self.run_turn("always-omit")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(self.count.read_text(), "3")
        self.assertNotIn(marker, done.stdout + done.stderr)

    def test_a_transcript_root_failure_is_not_retried_as_an_omission(self):
        self.transcripts.rmdir()
        self.transcripts.write_text("not a directory", encoding="utf-8")
        done, _ = self.run_turn("probe-error")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(self.count.read_text(), "1")
        self.assertIn("probe failed", done.stderr)
        self.assertNotIn("absent after 3", done.stderr)

    def test_an_unexpected_probe_exit_one_is_fail_closed_and_not_retried(self):
        real_python = Path(sys.executable).resolve()
        wrapper = self.bin / "python3"
        wrapper.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            case "$1" in
              */marker_probe.py) exit 1 ;;
              *) exec "{real_python}" "$@" ;;
            esac
            """), encoding="utf-8")
        wrapper.chmod(0o755)
        done, _ = self.run_turn("probe-error")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(self.count.read_text(), "1")
        self.assertIn("probe failed", done.stderr)


class FreshUserMarkerContractTest(unittest.TestCase):

    def test_the_production_shell_guards_execute_each_stage_once_and_preserve(self):
        fixture = r'''
source "$1" || exit 90
KEEP=0
pushes=0
pages=0
continued=0
e2e_once push && pushes=$((pushes + 1))
e2e_once push && pushes=$((pushes + 1))
e2e_once page && pages=$((pages + 1))
e2e_once page && pages=$((pages + 1))
if preserve_marker_evidence "marker turn" "/tmp/evidence-a" "/tmp/evidence-b"; then
  continued=1
fi
printf 'pushes=%s pages=%s keep=%s continued=%s\n' \
  "$pushes" "$pages" "$KEEP" "$continued"
'''
        done = subprocess.run(
            ["bash", "-c", fixture, "fixture", str(CONTRACT)],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            done.stdout, "pushes=1 pages=1 keep=1 continued=0\n")
        self.assertIn("preserving evidence", done.stderr)

    def test_the_retry_helper_cannot_repeat_push_or_page_delivery(self):
        helper = TURN.read_text(encoding="utf-8")
        script = (ROOT / "test" / "e2e" / "fresh-user.sh").read_text(
            encoding="utf-8")
        self.assertNotIn("antiphon push", helper)
        self.assertNotIn("page_now", helper)
        t2_t3 = script[
            script.index('step "T2'):
            script.index('step "T5')
        ]
        self.assertEqual(t2_t3.count("antiphon push codex"), 1)
        self.assertEqual(t2_t3.count('PAGE="$(page_now)"'), 1)
        self.assertEqual(t2_t3.count("e2e_once push"), 1)
        self.assertEqual(t2_t3.count("e2e_once page"), 1)

    def test_the_exact_second_transcript_is_carried_to_the_single_push(self):
        script = (ROOT / "test" / "e2e" / "fresh-user.sh").read_text(
            encoding="utf-8")
        self.assertIn('TRANSCRIPT="$MARKER_TRANSCRIPT"', script)
        self.assertIn('"transcript_path":"%s"', script)
        self.assertNotIn('grep -qr "@codex $NONCE-two"', script)

    def test_final_marker_exhaustion_preserves_evidence_and_exits(self):
        script = (ROOT / "test" / "e2e" / "fresh-user.sh").read_text(
            encoding="utf-8")
        start = script.index("land_exact_marker()")
        end = script.index("\n}\n", start) + 3
        helper = script[start:end]
        self.assertIn("preserve_marker_evidence", helper)
        self.assertRegex(helper, r"1\)\n(?:.|\n)*?exit 1")


if __name__ == "__main__":
    unittest.main()
