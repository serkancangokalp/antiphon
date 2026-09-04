import io
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import host_wrapper_census as census
import antiphon


class HostWrapperCensusTest(unittest.TestCase):

    @staticmethod
    def write_lines(root, name, records):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
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
        self.write_lines(claude, "project-one/one.jsonl", [
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
        self.write_lines(claude, "project-one/subagents/worker.jsonl", [
            {"type": "user", "promptSource": "system", "message": {
                "content": "<fork-boilerplate>\nNESTED-CLAUDE-SECRET"}},
        ])
        self.write_lines(codex, "2026/09/rollout-two.jsonl", [
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
                     "# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>\n"
                     "AGENTS-SECRET\n</INSTRUCTIONS>"}]}},
            {"type": "response_item", "payload": {"type": "message",
                "role": "assistant", "content": [
                    {"type": "output_text", "text":
                     "[external_agent_tool_call: Bash]\ncommand: CALL-SECRET"}]}},
            {"type": "response_item", "payload": {"type": "message",
                "role": "assistant", "content": [
                    {"type": "output_text", "text":
                     "[external_agent_tool_result]\nRESULT-SECRET"}]}},
        ])
        self.write_lines(codex, "2026/09/session-notes.jsonl", [
            {"type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [{"type": "input_text",
                    "text": "<not-a-rollout>\nEXCLUDED-CODEX-SECRET"}]}},
        ])
        return claude, codex

    def test_census_counts_only_user_opening_tags_and_sources(self):
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            result = census.census(claude, codex)
        self.assertEqual(result["claude"]["all_files"], 2)
        self.assertEqual(result["claude"]["production"]["files"], 1)
        self.assertEqual(result["claude"]["production"]["malformed_lines"], 1)
        self.assertEqual(result["claude"]["production"]["user_messages"], 6)
        self.assertEqual(
            result["claude"]["production"]["tags"]["channel"]["system"], 1)
        self.assertEqual(
            result["claude"]["production"]["tags"]["task-notification"]["sdk"], 1)
        self.assertEqual(
            result["claude"]["production"]["tags"]["html"]["typed"], 1)
        self.assertEqual(result["codex"]["all_files"], 2)
        self.assertEqual(result["codex"]["production"]["files"], 1)
        self.assertEqual(result["codex"]["production"]["user_messages"], 3)
        self.assertEqual(
            result["codex"]["production"]["tags"]["environment_context"]["<absent>"],
            1)

    def test_nested_claude_and_non_rollout_codex_files_are_excluded(self):
        """A host tag outside production's file set cannot justify a filter."""
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            result = census.census(claude, codex)

        self.assertEqual(result["claude"]["excluded"]["files"], 1)
        self.assertEqual(
            result["claude"]["excluded"]["tags"]["fork-boilerplate"]["system"], 1)
        self.assertNotIn("fork-boilerplate",
                         result["claude"]["production"]["tags"])
        self.assertEqual(result["codex"]["excluded"]["files"], 1)
        self.assertEqual(
            result["codex"]["excluded"]["tags"]["not-a-rollout"]["<absent>"], 1)
        self.assertNotIn("not-a-rollout", result["codex"]["production"]["tags"])

    def test_transcript_derived_output_keys_are_bounded_categories(self):
        """Aggregate output may name a plausible wrapper, but it must never
        copy an arbitrary prompt source or an unbounded tag-shaped secret.
        Cardinality is bounded too, so many adversarial tags cannot turn this
        release diagnostic into an unbounded output channel.
        """
        source_secret = "SOURCE-SECRET-" + "s" * 300
        tag_secret = "TAG-SECRET-" + "t" * 300
        with tempfile.TemporaryDirectory() as root:
            claude = os.path.join(root, "claude")
            codex = os.path.join(root, "codex")
            os.makedirs(codex)
            first_records = [{
                "type": "user", "promptSource": source_secret,
                "message": {"content": f"<{tag_secret}>\nbody"},
            }]
            first_records.extend({
                "type": "user", "promptSource": "system",
                "message": {"content": f"<candidate-{number}>\nbody"},
            } for number in range(200))
            second_records = [{
                "type": "user", "promptSource": "system",
                "message": {"content": f"<candidate-{number}>\nbody"},
            } for number in range(200, 400)]
            self.write_lines(claude, "project/one.jsonl", first_records)
            self.write_lines(claude, "project/two.jsonl", second_records)

            result = census.census(claude, codex)

        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn(source_secret, rendered)
        self.assertNotIn(tag_secret, rendered)
        stats = result["claude"]["production"]
        self.assertEqual(stats["prompt_sources"][census.OTHER_SOURCE], 1)
        self.assertEqual(
            stats["tags"][census.OVERLONG_TAG][census.OTHER_SOURCE], 1)
        self.assertLessEqual(len(stats["tags"]), census.MAX_TAG_KEYS)
        self.assertIn(census.ADDITIONAL_TAGS, stats["tags"])

    def test_hidden_codex_paths_match_production_glob_scope(self):
        """Python's recursive glob does not descend through dot directories;
        the census must report their files and ambiguous links as excluded,
        never as evidence for a production filter.
        """
        record = {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{
                "type": "input_text", "text": "<candidate>\nbody",
            }]}}
        with tempfile.TemporaryDirectory() as root, \
             tempfile.TemporaryDirectory() as outside:
            claude = os.path.join(root, "claude")
            codex = os.path.join(root, "codex")
            os.makedirs(claude)
            visible = self.write_lines(
                codex, "2026/rollout-visible.jsonl", [record])
            hidden = self.write_lines(
                codex, ".hidden/rollout-hidden.jsonl", [record])
            os.makedirs(os.path.join(outside, "nested"))
            os.symlink(os.path.join(outside, "nested"),
                       os.path.join(codex, ".hidden-link"))

            result = census.census(claude, codex)
            with patch.object(antiphon, "CODEX_SESSIONS", codex):
                production = antiphon._enumerate_catalog_candidates(
                    root, "codex")

        self.assertEqual(
            production.relative_paths,
            (os.path.relpath(visible, codex),),
            "the premise: production excludes the hidden rollout")
        side = result["codex"]
        self.assertEqual(side["production"]["files"], 1)
        self.assertEqual(side["production"]["refused_files"], 0)
        self.assertEqual(side["excluded"]["files"], 1)
        self.assertEqual(side["excluded"]["refused_files"], 1)
        self.assertNotEqual(hidden, visible)

    def test_symlinked_files_and_directories_are_refused_not_aggregated(self):
        """The census must not learn from paths production will refuse."""
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            outside = os.path.join(root, "outside")
            leaf_target = self.write_lines(outside, "leaf.jsonl", [
                {"type": "user", "promptSource": "system", "message": {
                    "content": "<symlink-leaf>\nLEAF-SYMLINK-SECRET"}},
            ])
            linked_dir = os.path.join(outside, "linked-dir")
            self.write_lines(linked_dir, "rollout-escape.jsonl", [
                {"type": "response_item", "payload": {"type": "message",
                    "role": "user", "content": [{"type": "input_text",
                        "text": "<symlink-dir>\nDIR-SYMLINK-SECRET"}]}},
            ])
            os.symlink(leaf_target, os.path.join(claude, "project-one", "linked.jsonl"))
            os.symlink(leaf_target, os.path.join(
                claude, "project-one", "subagents", "linked.jsonl"))
            os.symlink(leaf_target, os.path.join(
                claude, "project-one", "readme.link"))
            os.mkfifo(os.path.join(claude, "project-one", "pipe.jsonl"))
            os.symlink(linked_dir, os.path.join(codex, "2026", "linked"))
            os.symlink(leaf_target, os.path.join(
                codex, "2026", "session-linked.jsonl"))
            os.mkfifo(os.path.join(codex, "2026", "session-pipe.jsonl"))

            result = census.census(claude, codex)

        self.assertEqual(result["claude"]["refused_paths"], 4)
        self.assertEqual(result["claude"]["production"]["refused_files"], 2)
        self.assertEqual(result["claude"]["excluded"]["refused_files"], 2)
        self.assertEqual(result["codex"]["refused_paths"], 3)
        self.assertEqual(result["codex"]["production"]["refused_files"], 2)
        self.assertEqual(result["codex"]["excluded"]["refused_files"], 1)
        self.assertNotIn("symlink-leaf", result["claude"]["production"]["tags"])
        self.assertNotIn("symlink-dir", result["codex"]["production"]["tags"])

    def test_candidate_shaped_directories_are_refused_and_still_walked(self):
        """Production enumerators admit names, not only regular files.  A
        matching directory is therefore one refused candidate while its
        descendants remain independently inventoryable.
        """
        with tempfile.TemporaryDirectory() as root:
            claude = os.path.join(root, "claude")
            codex = os.path.join(root, "codex")
            self.write_lines(claude, "project/session.jsonl/child.jsonl", [
                {"type": "user", "promptSource": "system", "message": {
                    "content": "<nested-child>\nnot production"}},
            ])
            self.write_lines(
                codex,
                "2026/rollout-dir.jsonl/rollout-child.jsonl", [{
                    "type": "response_item", "payload": {
                        "type": "message", "role": "user", "content": [{
                            "type": "input_text",
                            "text": "<child-rollout>\nproduction",
                        }],
                    },
                }])

            result = census.census(claude, codex)

        self.assertEqual(result["claude"]["refused_paths"], 1)
        self.assertEqual(result["claude"]["production"]["refused_files"], 1)
        self.assertEqual(result["claude"]["excluded"]["files"], 1,
                         "the nested child remains inventoried")
        self.assertEqual(result["codex"]["refused_paths"], 1)
        self.assertEqual(result["codex"]["production"]["refused_files"], 1)
        self.assertEqual(result["codex"]["production"]["files"], 1,
                         "the child rollout remains independently admitted")
        self.assertEqual(
            result["codex"]["production"]["tags"]["child-rollout"]
                  ["<absent>"], 1)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Linux exposes undecodable directory bytes via surrogateescape")
    def test_an_invalid_utf8_filename_is_refused_at_production_boundary(self):
        """Production rejects surrogateescaped catalog paths before opening;
        the census must not learn a wrapper tag from one Linux can enumerate.
        """
        with tempfile.TemporaryDirectory() as root:
            claude = os.path.join(root, "claude")
            codex = os.path.join(root, "codex")
            os.makedirs(claude)
            os.makedirs(codex)
            raw = (os.fsencode(codex)
                   + b"/rollout-invalid-\xff.jsonl")
            fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, json.dumps({
                    "type": "response_item", "payload": {
                        "type": "message", "role": "user", "content": [{
                            "type": "input_text",
                            "text": "<must-not-be-learned>\nsecret",
                        }],
                    },
                }).encode("utf-8") + b"\n")
            finally:
                os.close(fd)

            result = census.census(claude, codex)

        side = result["codex"]
        self.assertEqual(side["all_files"], 0)
        self.assertEqual(side["refused_paths"], 1)
        self.assertEqual(side["production"]["refused_files"], 1)
        self.assertNotIn("must-not-be-learned",
                         side["production"]["tags"])

    def test_a_raced_candidate_directory_is_one_merged_refusal(self):
        """A candidate-shaped directory is one filesystem entry even when
        opening it races. Its candidate and may-hide-children facts merge."""
        original = census.os.open

        def raced(path, *args, **kwargs):
            if path == "session.jsonl" and kwargs.get("dir_fd") is not None:
                raise OSError(errno.EIO, "directory changed after stat")
            return original(path, *args, **kwargs)

        dir_fd = set(getattr(census.os, "supports_dir_fd", set()))
        dir_fd.discard(original)
        dir_fd.add(raced)
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "project", "session.jsonl"))
            with patch.object(census.os, "open", raced), \
                 patch.object(census.os, "supports_dir_fd", dir_fd):
                inventory = census._safe_inventory(root)

        self.assertEqual(inventory,
                         ([], [("project/session.jsonl", True)], False, None))

    def test_missing_descriptor_primitives_are_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            with patch.object(census.os, "supports_dir_fd", set()):
                result = census.census(claude, codex)

            with patch.object(census.os, "supports_dir_fd", set()), \
                 patch.object(sys, "stdout", new_callable=io.StringIO):
                code = census.main([
                    "--claude-projects-root", claude,
                    "--codex-root", codex])

        self.assertIs(result["claude"]["unsupported_platform"], True)
        self.assertIs(result["codex"]["unsupported_platform"], True)
        self.assertEqual(result["claude"]["all_files"], 0)
        self.assertEqual(result["codex"]["all_files"], 0)
        self.assertEqual(code, 1)

    def test_missing_no_follow_stat_support_is_reported_not_called(self):
        """A platform may support dir_fd for stat without supporting its
        follow_symlinks keyword. The census must fail closed before walking.
        """
        supported = set(getattr(census.os, "supports_follow_symlinks", set()))
        supported.discard(census.os.stat)
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            with patch.object(census.os, "supports_follow_symlinks", supported), \
                 patch.object(census.os, "stat",
                              side_effect=AssertionError("stat must not be called")):
                result = census.census(claude, codex)

        self.assertIs(result["claude"]["unsupported_platform"], True)
        self.assertIs(result["codex"]["unsupported_platform"], True)
        self.assertEqual(result["claude"]["all_files"], 0)
        self.assertEqual(result["codex"]["all_files"], 0)

    def test_advertised_no_follow_stat_runtime_refusal_discards_partial_walk(self):
        """Capability sets are advisory: a runtime may still refuse the
        keyword. No prefix of that incomplete inventory may be reported.
        """
        original = census.os.stat

        def unavailable(*_args, **_kwargs):
            raise NotImplementedError("follow_symlinks is unavailable")

        dir_fd = set(getattr(census.os, "supports_dir_fd", set()))
        follow = set(getattr(census.os, "supports_follow_symlinks", set()))
        dir_fd.discard(original)
        follow.discard(original)
        dir_fd.add(unavailable)
        follow.add(unavailable)
        with tempfile.TemporaryDirectory() as root:
            self.write_lines(root, "project/one.jsonl", [{"type": "user"}])
            with patch.object(census.os, "stat", unavailable), \
                 patch.object(census.os, "supports_dir_fd", dir_fd), \
                 patch.object(census.os, "supports_follow_symlinks", follow):
                inventory = census._safe_inventory(root)

        self.assertEqual(inventory, ([], [], True, None))

    def test_advertised_open_runtime_refusal_is_bounded(self):
        original = census.os.open

        def unavailable(*_args, **_kwargs):
            raise NotImplementedError("dir_fd is unavailable")

        dir_fd = set(getattr(census.os, "supports_dir_fd", set()))
        dir_fd.discard(original)
        dir_fd.add(unavailable)
        with tempfile.TemporaryDirectory() as root:
            with patch.object(census.os, "open", unavailable), \
                 patch.object(census.os, "supports_dir_fd", dir_fd):
                opened = census._open_safe(root, "one.jsonl")
        self.assertIsNone(opened)

    def test_a_root_listdir_failure_is_a_root_error_not_a_clean_partial_walk(self):
        original = census.os.listdir

        def denied(_descriptor):
            raise PermissionError(errno.EACCES, "root changed after open")

        supports_fd = set(getattr(census.os, "supports_fd", set()))
        supports_fd.discard(original)
        supports_fd.add(denied)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(census.os, "listdir", denied), \
             patch.object(census.os, "supports_fd", supports_fd):
            inventory = census._safe_inventory(root)
        self.assertEqual(inventory, ([], [], False, "unreadable"))

    def test_stat_failures_are_conservative_directory_ambiguities(self):
        original = census.os.stat

        def raced(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path in ("project-one", "09"):
                raise OSError(errno.EIO, "metadata unavailable")
            return original(path, *args, **kwargs)

        dir_fd = set(getattr(census.os, "supports_dir_fd", set()))
        follow = set(getattr(census.os, "supports_follow_symlinks", set()))
        dir_fd.discard(original)
        follow.discard(original)
        dir_fd.add(raced)
        follow.add(raced)
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            with patch.object(census.os, "stat", raced), \
                 patch.object(census.os, "supports_dir_fd", dir_fd), \
                 patch.object(census.os, "supports_follow_symlinks", follow):
                result = census.census(claude, codex)
        self.assertEqual(result["claude"]["production"]["refused_files"], 1)
        self.assertEqual(result["codex"]["production"]["refused_files"], 1)

    def test_a_child_fstat_failure_is_a_scoped_refusal_not_a_crash(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_lines(root, "project/one.jsonl", [{"type": "user"}])
            with patch.object(census.os, "fstat",
                              side_effect=OSError(errno.EIO, "fstat raced")):
                inventory = census._safe_inventory(root)
        self.assertEqual(inventory, ([], [("project", True)], False, None))

    def test_a_late_file_read_failure_discards_partial_counts(self):
        class RacedSource:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                yield json.dumps({"type": "user", "promptSource": "system",
                                  "message": {"content": "<channel>\nSECRET"}})
                raise OSError(errno.EIO, "read failed after one line")

        with patch.object(census, "_open_safe", return_value=RacedSource()):
            stats = census._stats(
                "/root", ["one.jsonl"], census.claude_user_blocks,
                census.claude_shapes)
        self.assertEqual(stats["refused_files"], 1)
        self.assertEqual(stats["user_messages"], 0)
        self.assertEqual(stats["tags"], {})
        self.assertEqual(stats["prompt_sources"], {})

    def test_missing_roots_are_explicit_and_make_the_cli_fail(self):
        with tempfile.TemporaryDirectory() as parent:
            claude = os.path.join(parent, "missing-claude")
            codex = os.path.join(parent, "missing-codex")
            done = subprocess.run(
                [sys.executable, census.__file__,
                 "--claude-projects-root", claude, "--codex-root", codex],
                capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL)

        self.assertEqual(done.returncode, 1)
        result = json.loads(done.stdout)
        self.assertEqual(result["claude"]["root_error"], "missing")
        self.assertEqual(result["codex"]["root_error"], "missing")
        self.assertIs(result["claude"]["unsupported_platform"], False)
        self.assertEqual(done.stderr, "")

    def test_unreadable_roots_are_not_reported_as_clean_zeroes(self):
        original = census.os.open

        def denied(*_args, **_kwargs):
            raise PermissionError("not readable")

        dir_fd = set(getattr(census.os, "supports_dir_fd", set()))
        dir_fd.discard(original)
        dir_fd.add(denied)
        with patch.object(census.os, "open", denied), \
             patch.object(census.os, "supports_dir_fd", dir_fd):
            result = census.census("/claude", "/codex")

        self.assertEqual(result["claude"]["root_error"], "unreadable")
        self.assertEqual(result["codex"]["root_error"], "unreadable")
        self.assertEqual(result["claude"]["all_files"], 0)
        self.assertEqual(result["codex"]["all_files"], 0)

    def test_existing_empty_roots_are_truthful_successful_zeroes(self):
        with tempfile.TemporaryDirectory() as claude, \
             tempfile.TemporaryDirectory() as codex, \
             patch.object(sys, "stdout", new_callable=io.StringIO) as out:
            code = census.main([
                "--claude-projects-root", claude, "--codex-root", codex])

        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertIsNone(result["claude"]["root_error"])
        self.assertIsNone(result["codex"]["root_error"])
        self.assertEqual(result["claude"]["all_files"], 0)
        self.assertEqual(result["codex"]["all_files"], 0)

    def test_census_counts_the_prefix_shapes_beside_the_tags(self):
        """The shapes that are not tags: the AGENTS.md injection (a user
        prefix plus a fence), the external-agent relays (assistant prefixes)
        and, on the Claude side, the host-set compact summary flag and the
        exact interruption literals. Counts only, never text."""
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            result = census.census(claude, codex)
        self.assertEqual(result["codex"]["production"]["shapes"], {
            "agents_md_block": 1, "external_agent_call": 1,
            "external_agent_result": 1})
        self.assertEqual(result["claude"]["production"]["shapes"], {
            "compact_summary": 1, "interruption_literal": 1})

    def test_an_unclosed_agents_draft_is_not_a_host_block(self):
        record = {"type": "response_item", "payload": {"type": "message",
                  "role": "user", "content": [{"type": "input_text", "text":
                  "# AGENTS.md instructions for /p\n\n<INSTRUCTIONS>\ndraft"}]}}
        self.assertEqual(census.codex_shapes(record), [])
        closed = json.loads(json.dumps(record))
        closed["payload"]["content"][0]["text"] += "\n</INSTRUCTIONS>"
        self.assertEqual(census.codex_shapes(closed), ["agents_md_block"])
        self.assertFalse(antiphon._is_codex_host_block(
            record["payload"]["content"][0]["text"]))
        self.assertTrue(antiphon._is_codex_host_block(
            closed["payload"]["content"][0]["text"]))

    def test_deeply_nested_json_is_counted_malformed_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            claude, codex = self.fixtures(root)
            self.write_lines(codex, "2026/09/rollout-deep.jsonl",
                             ["[" * 1_500 + "0" + "]" * 1_500])
            result = census.census(claude, codex)
        self.assertEqual(result["codex"]["production"]["malformed_lines"], 1)

    def test_the_census_shapes_are_productions_own(self):
        """A census that mirrors production has to mirror it exactly, or the
        release check counts a shape the reader does not filter."""
        self.assertEqual(census.CLAUDE_HOST_LITERALS, antiphon.CLAUDE_HOST_LITERALS)
        self.assertEqual(census.AGENTS_INJECTION_HEAD, antiphon.AGENTS_INJECTION_HEAD)
        self.assertEqual(census.EXTERNAL_AGENT_CALL.pattern,
                         antiphon.EXTERNAL_AGENT_CALL.pattern)
        self.assertEqual(census.EXTERNAL_AGENT_RESULT_HEAD,
                         antiphon.EXTERNAL_AGENT_RESULT_HEAD)
        for value in ("rollout.jsonl", "nested/session.jsonl", "",
                      "nul\0name.jsonl", "surrogate\udcff.jsonl"):
            self.assertEqual(census._filesystem_safe_relative(value),
                             antiphon._filesystem_safe_relative(value), value)

    def test_parsers_match_production_meta_empty_and_join_rules(self):
        for source in ("system", "sdk", "typed", "queued",
                       "suggestion_accepted"):
            self.assertEqual(
                census._prompt_source({"promptSource": source}), source)
        self.assertEqual(
            census._prompt_source({"promptSource": "future-private-value"}),
            census.OTHER_SOURCE)
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
                [sys.executable, census.__file__, "--claude-projects-root", claude,
                 "--codex-root", codex], capture_output=True, text=True,
                timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, 0, done.stderr)
            parsed = json.loads(done.stdout)
            self.assertEqual(parsed, census.census(claude, codex))
            for secret in ("CLAUDE-SECRET-ONE", "CLAUDE-SECRET-TWO",
                           "PERSON-SECRET", "CODEX-SECRET", "joined tail",
                           "SUMMARY-SECRET", "PERSON-TAIL", "AGENTS-SECRET",
                           "CALL-SECRET", "RESULT-SECRET", "NESTED-CLAUDE-SECRET",
                           "EXCLUDED-CODEX-SECRET"):
                self.assertNotIn(secret, done.stdout)
            self.assertNotIn(claude, done.stdout)
            self.assertNotIn(codex, done.stdout)


if __name__ == "__main__":
    unittest.main()
