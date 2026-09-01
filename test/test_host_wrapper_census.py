import json
import os
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, os.path.dirname(__file__))
import host_wrapper_census as census


class HostWrapperCensusTest(unittest.TestCase):

    @staticmethod
    def write_lines(root, name, records):
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8") as target:
            for record in records:
                if isinstance(record, str):
                    target.write(record + "\n")
                else:
                    target.write(json.dumps(record) + "\n")
        return path

    def fixtures(self, root):
        claude = os.path.join(root, "claude")
        codex = os.path.join(root, "codex")
        self.write_lines(claude, "one.jsonl", [
            {"type": "user", "promptSource": "system", "message": {
                "content": "<channel>\nCLAUDE-SECRET-ONE"}},
            {"type": "user", "promptSource": "sdk", "message": {
                "content": [{"type": "text", "text":
                             "<task-notification>\nCLAUDE-SECRET-TWO"},
                            {"type": "tool_result", "content": "ignored"}]}},
            {"type": "user", "promptSource": "typed", "message": {
                "content": "<html>\nPERSON-SECRET"}},
            {"type": "assistant", "promptSource": "system", "message": {
                "content": "<channel>\nNOT-A-USER"}},
            "{malformed",
        ])
        self.write_lines(codex, "two.jsonl", [
            {"type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [
                    {"type": "input_text", "text":
                     "  <environment_context>\nCODEX-SECRET"},
                    {"type": "text", "text": "joined tail"}]}},
            {"type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [
                    {"type": "input_text", "text": "plain words"}]}},
            {"type": "response_item", "payload": {"type": "message",
                "role": "assistant", "content": [
                    {"type": "output_text", "text":
                     "<environment_context>\nNOT-A-USER"}]}},
        ])
        return claude, codex

    def test_census_counts_only_user_opening_tags_and_sources(self):
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            result = census.census(claude, codex)
        self.assertEqual(result["claude"]["files"], 1)
        self.assertEqual(result["claude"]["malformed_lines"], 1)
        self.assertEqual(result["claude"]["user_blocks"], 3)
        self.assertEqual(result["claude"]["tags"]["channel"]["system"], 1)
        self.assertEqual(
            result["claude"]["tags"]["task-notification"]["sdk"], 1)
        self.assertEqual(result["claude"]["tags"]["html"]["typed"], 1)
        self.assertEqual(result["codex"]["files"], 1)
        self.assertEqual(result["codex"]["user_blocks"], 2)
        self.assertEqual(
            result["codex"]["tags"]["environment_context"]["<absent>"],
            1)

    def test_cli_prints_aggregate_counts_without_content_or_paths(self):
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            done = subprocess.run(
                [sys.executable, census.__file__, "--claude-root", claude,
                 "--codex-root", codex], capture_output=True, text=True,
                timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, 0, done.stderr)
            parsed = json.loads(done.stdout)
            self.assertEqual(parsed, census.census(claude, codex))
            for secret in ("CLAUDE-SECRET-ONE", "CLAUDE-SECRET-TWO",
                           "PERSON-SECRET", "CODEX-SECRET", "joined tail"):
                self.assertNotIn(secret, done.stdout)
            self.assertNotIn(claude, done.stdout)
            self.assertNotIn(codex, done.stdout)


if __name__ == "__main__":
    unittest.main()
