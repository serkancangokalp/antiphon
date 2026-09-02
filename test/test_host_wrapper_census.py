import json
import os
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import host_wrapper_census as census
import antiphon


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
            {"type": "user", "isCompactSummary": True, "message": {
                "content": "This session is being continued. SUMMARY-SECRET"}},
            {"type": "user", "message": {
                "content": "[Request interrupted by user]"}},
            {"type": "user", "message": {
                "content": "[Request interrupted by user] PERSON-TAIL"}},
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
            {"type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [
                    {"type": "input_text", "text":
                     "# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>\nAGENTS-SECRET"}]}},
            {"type": "response_item", "payload": {"type": "message",
                "role": "assistant", "content": [
                    {"type": "output_text", "text":
                     "[external_agent_tool_call: Bash]\ncommand: CALL-SECRET"}]}},
            {"type": "response_item", "payload": {"type": "message",
                "role": "assistant", "content": [
                    {"type": "output_text", "text":
                     "[external_agent_tool_result]\nRESULT-SECRET"}]}},
        ])
        return claude, codex

    def test_census_counts_only_user_opening_tags_and_sources(self):
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            result = census.census(claude, codex)
        self.assertEqual(result["claude"]["files"], 1)
        self.assertEqual(result["claude"]["malformed_lines"], 1)
        self.assertEqual(result["claude"]["user_messages"], 6)
        self.assertEqual(result["claude"]["tags"]["channel"]["system"], 1)
        self.assertEqual(
            result["claude"]["tags"]["task-notification"]["sdk"], 1)
        self.assertEqual(result["claude"]["tags"]["html"]["typed"], 1)
        self.assertEqual(result["codex"]["files"], 1)
        self.assertEqual(result["codex"]["user_messages"], 3)
        self.assertEqual(
            result["codex"]["tags"]["environment_context"]["<absent>"],
            1)

    def test_census_counts_the_prefix_shapes_beside_the_tags(self):
        """The shapes that are not tags: the AGENTS.md injection (a user
        prefix plus a fence), the external-agent relays (assistant prefixes)
        and, on the Claude side, the host-set compact summary flag and the
        exact interruption literals. Counts only, never text."""
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            result = census.census(claude, codex)
        self.assertEqual(result["codex"]["shapes"], {
            "agents_md_block": 1, "external_agent_call": 1,
            "external_agent_result": 1})
        self.assertEqual(result["claude"]["shapes"], {
            "compact_summary": 1, "interruption_literal": 1})

    def test_the_census_shapes_are_productions_own(self):
        """A census that mirrors production has to mirror it exactly, or the
        release check counts a shape the reader does not filter."""
        self.assertEqual(census.CLAUDE_HOST_LITERALS, antiphon.CLAUDE_HOST_LITERALS)
        self.assertEqual(census.AGENTS_INJECTION_HEAD, antiphon.AGENTS_INJECTION_HEAD)
        self.assertEqual(census.EXTERNAL_AGENT_CALL.pattern,
                         antiphon.EXTERNAL_AGENT_CALL.pattern)
        self.assertEqual(census.EXTERNAL_AGENT_RESULT_HEAD,
                         antiphon.EXTERNAL_AGENT_RESULT_HEAD)

    def test_parsers_match_production_meta_empty_and_join_rules(self):
        self.assertEqual(census.claude_user_blocks({
            "type": "user", "isMeta": True, "message": {
                "content": "<meta-only>\nnot eligible"}}), [])
        self.assertEqual(census.claude_user_blocks({
            "type": "user", "message": {"content": ""}}), [])
        self.assertEqual(census.claude_user_blocks({
            "type": "user", "promptSource": "sdk", "message": {"content": [
                {"type": "text", "text": "<channel>\nfirst"},
                {"type": "text", "text": "second"},
                {"type": "text", "text": ""},
            ]}}), [("<channel>\nfirst\n\nsecond", "sdk")])
        self.assertEqual(census.codex_user_blocks({
            "type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [
                    {"type": "input_text", "text": ""},
                    {"type": "text", "text": ""},
                ]}}), [])
        self.assertEqual(census.codex_user_blocks({
            "type": "response_item", "payload": {"type": "message",
                "role": "user", "content": "not a production Codex shape"}}), [])

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
                           "PERSON-SECRET", "CODEX-SECRET", "joined tail",
                           "SUMMARY-SECRET", "PERSON-TAIL", "AGENTS-SECRET",
                           "CALL-SECRET", "RESULT-SECRET"):
                self.assertNotIn(secret, done.stdout)
            self.assertNotIn(claude, done.stdout)
            self.assertNotIn(codex, done.stdout)


if __name__ == "__main__":
    unittest.main()
