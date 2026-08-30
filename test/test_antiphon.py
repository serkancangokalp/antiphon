import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import antiphon

import contextlib
import io
import json
import subprocess
import tempfile
import time
try:
    import tomllib          # Python 3.11+
except ModuleNotFoundError:  # the hooks run whatever bare `python3` resolves to
    tomllib = None
import unittest
from unittest.mock import patch


class AntiphonTest(unittest.TestCase):
    def test_markers_are_only_recognised_at_the_start_of_a_line(self):
        self.assertEqual(antiphon.parse_markers("codex", "@codex one"), [(None, "one")])
        self.assertEqual(antiphon.parse_markers("claude", "  @claude: two"),
                         [(None, "two")])
        self.assertEqual(antiphon.parse_markers("claude", "example @claude three"), [])

    def test_a_marker_can_name_its_recipient(self):
        self.assertEqual(antiphon.parse_markers("claude", "@claude:api run the tests"),
                         [("api", "run the tests")])

    def test_punctuation_after_a_name_is_not_part_of_it(self):
        """People type `@claude:api, run it`. Reading the name as `api,` refuses
        a peer that exists."""
        for line in ("@claude:api, run it", "@claude:api. run it",
                     "@claude:api; run it"):
            self.assertEqual(antiphon.parse_markers("claude", line),
                             [("api", "run it")], line)

    def test_the_message_keeps_its_own_leading_punctuation(self):
        """Stripping a set of characters after the marker turned `.NET issue`
        into `NET issue`. A message that arrives altered and looks fine is worse
        than one that does not arrive."""
        for line, expected in (
                ("@claude:api .NET issue", ("api", ".NET issue")),
                ("@claude:api: :foo", ("api", ":foo")),
                ("@claude .NET issue", (None, ".NET issue")),
                ("@claude: .NET issue", (None, ".NET issue")),
                ("@claude, ,leading comma", (None, ",leading comma")),
                ("@claude:api ```py", ("api", "```py")),
        ):
            self.assertEqual(antiphon.parse_markers("claude", line), [expected], line)

    def test_a_claimed_name_is_reported_even_when_it_is_not_usable(self):
        """A name that vanishes because it was punctuated oddly is the failure
        this bridge exists to remove. Routing refuses it; the parser reports it."""
        for line, expected in (("@claude:BAD run", ("BAD", "run")),
                               ("@claude:../etc fix", ("../etc", "fix")),
                               ("@claude:: fix", ("", "fix"))):
            self.assertEqual(antiphon.parse_markers("claude", line), [expected], line)

    def test_a_marker_with_no_message_is_reported_not_dropped(self):
        self.assertEqual(antiphon.parse_markers("claude", "@claude:api"),
                         [("api", "")])
        self.assertEqual(antiphon.parse_markers("claude", "@claude"), [(None, "")])

    def test_each_marker_line_is_its_own_message(self):
        text = "@claude:ui look at this\nsome prose\n@claude:api and this"
        self.assertEqual(antiphon.parse_markers("claude", text),
                         [("ui", "look at this"), ("api", "and this")])

    def test_a_marker_for_the_other_side_is_not_ours(self):
        self.assertEqual(antiphon.parse_markers("claude", "@codex:build run"), [])

    # ---- grouping, fingerprinting and dedupe, as pure helpers ----

    def test_messages_group_by_recipient_in_order(self):
        text = "@claude:ui first\n@claude:api other\n@claude:ui second\n@claude bare"
        self.assertEqual(antiphon.group_by_recipient("claude", text),
                         {"ui": ["first", "second"], "api": ["other"],
                          None: ["bare"]})

    def test_an_unaddressed_line_and_an_empty_name_do_not_merge(self):
        """`alias or ""` folds None and "" together and hands `@claude:: fix` to
        the unaddressed path, undoing the parser telling them apart."""
        groups = antiphon.group_by_recipient("claude", "@claude a\n@claude:: b")
        self.assertEqual(groups, {None: ["a"], "": ["b"]})

    def test_a_fingerprint_distinguishes_where_the_breaks_fall(self):
        """A newline join collides: ["a\nb", "c"] and ["a", "b\nc"] hash the
        same, so one batch would suppress a different one."""
        self.assertNotEqual(antiphon.batch_fingerprint(["a\nb", "c"]),
                            antiphon.batch_fingerprint(["a", "b\nc"]))
        self.assertEqual(antiphon.batch_fingerprint(["a", "b"]),
                         antiphon.batch_fingerprint(["a", "b"]))

    def test_a_cursor_field_of_the_wrong_shape_starts_over_instead_of_raising(self):
        """`dict()` on a list is a TypeError and would surface as a traceback out
        of the Stop hook — the last place a hand-edited file should reach."""
        for broken in ([1, 2, 3], ["bad"], 42, 0.5, True):
            self.assertEqual(antiphon.migrate_pushed(broken, ["m"]), ({}, False),
                             repr(broken))

    def test_the_old_string_record_migrates_without_resending(self):
        """The old format stored the joined text, not a digest. Compared against
        a digest it is always unequal and would resend once on upgrade."""
        sent, already = antiphon.migrate_pushed("one\ntwo", ["one", "two"])
        self.assertEqual(sent, {})
        self.assertTrue(already)

        sent, already = antiphon.migrate_pushed("something else", ["one", "two"])
        self.assertFalse(already)

        sent, already = antiphon.migrate_pushed({"@ui": "abc"}, ["one"])
        self.assertEqual(sent, {"@ui": "abc"})
        self.assertFalse(already)

    def test_a_batch_is_delivered_once_however_often_the_hook_repeats(self):
        """Two markers to one name, pushed twice. Keeping only the last text per
        name resends both for ever: the first differs from the stored last, then
        the second differs from the newly stored first."""
        calls = []
        batches = antiphon.group_by_recipient("claude", "@claude:ui a\n@claude:ui b")
        sent = antiphon.deliver_batches(batches, {},
                                        lambda r, m: calls.append((r, m)) or True)
        antiphon.deliver_batches(batches, sent,
                                 lambda r, m: calls.append((r, m)) or True)
        self.assertEqual(calls, [("ui", ["a", "b"])])

    def test_only_the_recipient_that_succeeded_stops_being_retried(self):
        batches = antiphon.group_by_recipient("claude", "@claude:ui a\n@claude:api b")
        sent = antiphon.deliver_batches(batches, {},
                                        lambda r, m: r == "ui")
        retried = []
        antiphon.deliver_batches(batches, sent,
                                 lambda r, m: retried.append(r) or True)
        self.assertEqual(retried, ["api"])

    def test_the_unaddressed_slot_cannot_collide_with_a_peer_named_empty(self):
        batches = antiphon.group_by_recipient("claude", "@claude a\n@claude:: b")
        sent = antiphon.deliver_batches(batches, {}, lambda r, m: True)
        self.assertEqual(sorted(sent), ["", "@"])

    def test_last_codex_reply(self):
        lines = [
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "old"}],
            }}),
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "@claude new"}],
            }}),
        ]
        with patch.object(antiphon, "tail_lines", return_value=lines):
            self.assertEqual(antiphon.last_codex_reply("rollout"), "@claude new")

    def test_large_codex_rollout_reads_cwd_from_head(self):
        with tempfile.NamedTemporaryFile(prefix="rollout-", suffix=".jsonl") as f:
            f.write(b'{"cwd":"/tmp/project"}\n')
            f.write(b'x' * (antiphon.TAIL_BYTES + 1))
            f.flush()
            os.utime(f.name, None)
            with patch.object(antiphon.glob, "glob", return_value=[f.name]):
                self.assertEqual(antiphon.codex_rollout_files("/tmp/project"),
                                 [f.name])

    def test_push_claude_target(self):
        sent = []
        written = []
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply", return_value="@claude test"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor",
                          side_effect=lambda cwd, data, kind: written.append(dict(data))), \
             patch.object(antiphon, "send_to_claude",
                          side_effect=lambda cwd, msg: (sent.append((cwd, msg)) or (True, ""))), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(input_data))), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(antiphon.push("claude"), 0)
        self.assertEqual(sent, [("/tmp/project", "test")])
        # The record is now a fingerprint per recipient rather than the last
        # text: keeping the text resent both lines for ever when one reply
        # addressed the same peer twice.
        self.assertEqual(list(written[0]), ["last_pushed_claude"])
        self.assertEqual(written[0]["last_pushed_claude"],
                         {"": antiphon.batch_fingerprint(["test"])})

    def test_push_uses_separate_dedupe_cursors(self):
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply", return_value="@claude same"), \
             patch.object(antiphon, "read_cursor",
                          return_value={"last_pushed_claude": {"": antiphon.batch_fingerprint(["same"])},
                                        "last_pushed_codex": {"": "other"}}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon, "send_to_claude") as send, \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(input_data))):
            self.assertEqual(antiphon.push("claude"), 0)
            send.assert_not_called()

    def test_a_cursor_in_the_old_string_format_does_not_resend_on_upgrade(self):
        """Installed bridges hold the joined text, not a digest. Comparing the
        two is always unequal, so the last message would go out once more the
        first time the new code ran."""
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        written = []
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply", return_value="@claude same"), \
             patch.object(antiphon, "read_cursor",
                          return_value={"last_pushed_claude": "same"}), \
             patch.object(antiphon, "write_cursor",
                          side_effect=lambda cwd, data, kind: written.append(dict(data))), \
             patch.object(antiphon, "send_to_claude") as send, \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(input_data))), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(antiphon.push("claude"), 0)
            send.assert_not_called()
        self.assertEqual(written[0]["last_pushed_claude"],
                         {"": antiphon.batch_fingerprint(["same"])},
                         "and the record migrates to the new form")

    def test_an_empty_marker_is_reported_even_beside_a_real_one(self):
        """A batch holding one empty marker and one real message is not empty, so
        a per-batch check let the empty line disappear without a word."""
        sent = []
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        err = io.StringIO()
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply",
                          return_value="@claude\n@claude run it"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon, "send_to_claude",
                          side_effect=lambda cwd, msg: (sent.append(msg) or (True, ""))), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(input_data))), \
             contextlib.redirect_stderr(err):
            self.assertEqual(antiphon.push("claude"), 0)
        self.assertEqual(sent, ["run it"], "the real message still goes")
        self.assertEqual(err.getvalue().count("carried no message"), 1,
                         "and the empty line is reported, not swallowed")

    def test_an_empty_named_marker_names_the_recipient_it_meant(self):
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        err = io.StringIO()
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply",
                          return_value="@claude:api\n@claude:api run"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(input_data))), \
             contextlib.redirect_stderr(err):
            self.assertEqual(antiphon.push("claude"), 0)
        self.assertIn("@claude:api line carried no message", err.getvalue())

    def test_a_named_marker_is_refused_until_routing_exists(self):
        """Resolving a name is a later task. Refusing out loud is honest;
        delivering it to whoever is around would not be."""
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        err = io.StringIO()
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply", return_value="@claude:api run"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor") as write, \
             patch.object(antiphon, "send_to_claude") as send, \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(input_data))), \
             contextlib.redirect_stderr(err):
            self.assertEqual(antiphon.push("claude"), 0)
            send.assert_not_called()
            write.assert_not_called()
        self.assertIn("not available yet", err.getvalue())
        self.assertIn("api", err.getvalue())

    def test_an_unaddressed_line_still_delivers_alongside_a_named_one(self):
        """The refusal must not take the working path down with it."""
        sent = []
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply",
                          return_value="@claude:api named\n@claude bare"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon, "send_to_claude",
                          side_effect=lambda cwd, msg: (sent.append(msg) or (True, ""))), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(input_data))), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(antiphon.push("claude"), 0)
        self.assertEqual(sent, ["bare"])

    # ---- the Codex-side MCP server ----

    @staticmethod
    def _run_mcp(project, *requests):
        """Drives the stdio server with JSON-RPC lines; returns parsed responses."""
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
        out = io.StringIO()
        with patch.object(antiphon, "project_dir", return_value=project), \
             patch.object(antiphon.sys, "stdin", stdin), \
             contextlib.redirect_stdout(out):
            antiphon.mcp()
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    @staticmethod
    def _call(name, **arguments):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}

    def test_mcp_offers_codex_both_a_read_and_a_send_tool(self):
        """Reading was live from the start; sending was not. Codex could only reach
        Claude by ending its turn with `@claude`, so it could never hand over work
        and keep going."""
        self.assertEqual(sorted(t["name"] for t in antiphon.TOOLS),
                         ["antiphon_read", "antiphon_send"])

    def test_antiphon_send_delivers_the_text_to_the_claude_channel(self):
        sent = []
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "send_to_claude",
                          side_effect=lambda cwd, msg: (sent.append((cwd, msg)) or (True, ""))), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor", return_value=True):
            responses = self._run_mcp(project, self._call("antiphon_send", text="run the tests"))
            self.assertEqual(sent, [(project, "run the tests")])
        self.assertNotEqual(responses[0]["result"].get("isError"), True)

    def test_antiphon_send_records_the_delivery_so_the_stop_hook_will_not_repeat_it(self):
        """`push` skips a message equal to `last_pushed_claude`. Without this entry
        the same text arrives twice when Codex calls the tool and then ends its turn
        with the same `@claude` line."""
        written = []
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "send_to_claude", return_value=(True, "")), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor",
                          side_effect=lambda cwd, data, kind: written.append(dict(data))):
            self._run_mcp(project, self._call("antiphon_send", text="run the tests"))
        self.assertEqual(written, [{"last_pushed_claude": "run the tests"}])

    def test_antiphon_send_reports_a_dead_channel_as_an_error(self):
        """A silent success is the worst outcome here: Codex would believe Claude had
        been told, and neither side would ever notice the message vanished."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "send_to_claude", return_value=(False, "channel is down")):
            responses = self._run_mcp(project, self._call("antiphon_send", text="hi"))
        result = responses[0]["result"]
        self.assertIs(result.get("isError"), True)
        self.assertIn("channel is down", result["content"][0]["text"])

    def test_antiphon_send_refuses_empty_text(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "send_to_claude") as send:
            responses = self._run_mcp(project, self._call("antiphon_send", text="   "))
            send.assert_not_called()
        self.assertIs(responses[0]["result"].get("isError"), True)

    def test_reply_records_the_delivery_so_the_stop_hook_will_not_repeat_it(self):
        """The same defect mirrored on Claude's side: `reply_to_codex` delivered
        without recording it, so ending the turn with the same `@codex` line sent
        the message a second time."""
        written = []
        with patch.object(antiphon, "project_dir", return_value="/tmp/project"), \
             patch.object(antiphon, "codex_session_id", return_value="sess"), \
             patch.object(antiphon, "send_to_codex", return_value=(True, "")), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor",
                          side_effect=lambda cwd, data, kind: written.append(dict(data))), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps({"text": "hello"}))):
            self.assertEqual(antiphon.reply(), 0)
        self.assertEqual(written, [{"last_pushed_codex": "hello"}])

    # ---- choosing which Claude peer a message goes to ----

    def test_one_live_claude_peer_is_delivered_to(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            address, detail = antiphon.resolve_claude_target(project)
        self.assertEqual(address, "/tmp/ui.sock")
        self.assertEqual(detail, "")

    def test_several_live_peers_deliver_to_nobody_and_name_them_all(self):
        """The bridge never guesses a recipient. Guessing is what the silent
        misrouting was, and a cleverer guess is still a guess."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            address, detail = antiphon.resolve_claude_target(project)
        self.assertIsNone(address)
        self.assertIn("ui", detail)
        self.assertIn("api", detail)

    def test_a_codex_peer_does_not_count_as_a_claude_recipient(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "codex", "build", "sess-1")
            address, _ = antiphon.resolve_claude_target(project)
        self.assertEqual(address, "/tmp/ui.sock")

    def test_no_registered_peer_falls_back_to_the_project_socket(self):
        """An older channel server still serving the project-wide path is still a
        working peer; upgrading must not cut it off."""
        with tempfile.TemporaryDirectory() as project:
            address, detail = antiphon.resolve_claude_target(project)
        self.assertEqual(address, antiphon.claude_socket_path(project))
        self.assertEqual(detail, "")

    def test_send_to_claude_reports_the_ambiguity_instead_of_picking(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            ok, detail = antiphon.send_to_claude(project, "hello")
        self.assertFalse(ok)
        self.assertIn("ui", detail)
        self.assertIn("not delivered", detail.lower())

    def test_an_oversized_message_never_reaches_the_socket(self):
        """The server refuses on arrival, but a sender should not have to learn
        that from a dropped connection. The limit is in bytes, so a multi-byte
        message must not be measured in characters and let through at twice it."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            oversized = "ç" * antiphon.MAX_CHANNEL_BYTES        # two bytes each
            with patch.object(antiphon.socket, "socket") as opened:
                ok, detail = antiphon.send_to_claude(project, oversized)
                opened.assert_not_called()
        self.assertFalse(ok)
        self.assertIn(str(antiphon.MAX_CHANNEL_BYTES), detail)

    def test_the_send_tool_reports_an_oversized_message_as_an_error(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            with patch.object(antiphon.socket, "socket") as opened:
                result = antiphon._send_tool(project, "ç" * antiphon.MAX_CHANNEL_BYTES)
                opened.assert_not_called()
        self.assertIs(result.get("isError"), True)
        self.assertIn("bytes", result["content"][0]["text"])

    def test_a_message_just_under_the_limit_is_still_attempted(self):
        """The refusal must not be a blanket one: the boundary is where it is."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            with patch.object(antiphon.socket, "socket") as opened:
                opened.side_effect = OSError("no listener")
                ok, detail = antiphon.send_to_claude(project, "x" * 1000)
                opened.assert_called()
        self.assertFalse(ok)
        self.assertNotIn(str(antiphon.MAX_CHANNEL_BYTES), detail)

    class _Channel:
        """A channel socket that is not there for the first `missing` connects."""

        def __init__(self, missing=0, reply=b'{"ok": true}'):
            self.missing = missing
            self.reply = reply
            self.connects = 0
            self.sent = b""

        def __call__(self, *_a, **_k):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def settimeout(self, _):
            pass

        def close(self):
            pass

        def connect(self, _path):
            self.connects += 1
            if self.connects <= self.missing:
                raise FileNotFoundError(2, "No such file or directory")

        def sendall(self, data):
            self.sent += data

        def shutdown(self, _how):
            pass

        def recv(self, _n):
            data, self.reply = self.reply, b""
            return data

    def test_a_message_sent_before_the_socket_exists_still_arrives(self):
        """Measured: the MCP handshake completes 27-41ms before the channel socket
        is bound, so a message sent the moment the channel looked ready was
        refused 10 times out of 10. The first thing a session says is exactly when
        this happens."""
        chan = self._Channel(missing=2)
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon.socket, "socket", chan), \
             patch.object(antiphon, "resolve_claude_target",
                          side_effect=lambda cwd: ("/tmp/ui.sock", "")) as resolve:
            ok, detail = antiphon.send_to_claude(project, "the first thing said")
        self.assertTrue(ok, detail)
        self.assertEqual(chan.connects, 3)
        self.assertEqual(resolve.call_count, 3,
                         "each attempt must re-resolve: a named peer can register "
                         "between them and move the address")

    def test_a_channel_that_never_appears_fails_within_a_bounded_time(self):
        chan = self._Channel(missing=10_000)
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon.socket, "socket", chan), \
             patch.object(antiphon, "resolve_claude_target",
                          side_effect=lambda cwd: ("/tmp/ui.sock", "")):
            started = time.monotonic()
            ok, detail = antiphon.send_to_claude(project, "hello")
            elapsed = time.monotonic() - started
        self.assertFalse(ok)
        self.assertIn("down", detail)
        self.assertLess(elapsed, 3.0, "retrying must stay bounded")

    def test_an_ambiguous_target_is_not_retried(self):
        """Waiting cannot resolve ambiguity — more peers will not become fewer."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "resolve_claude_target",
                          side_effect=lambda cwd: (None, "not delivered: 2 peers")
                          ) as resolve:
            ok, detail = antiphon.send_to_claude(project, "hello")
        self.assertFalse(ok)
        self.assertIn("2 peers", detail)
        self.assertEqual(resolve.call_count, 1)

    def test_a_failure_after_the_bytes_went_out_is_not_retried(self):
        """Retrying here would deliver the message twice."""
        chan = self._Channel(missing=0, reply=b"not json at all")
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon.socket, "socket", chan), \
             patch.object(antiphon, "resolve_claude_target",
                          side_effect=lambda cwd: ("/tmp/ui.sock", "")):
            ok, detail = antiphon.send_to_claude(project, "hello")
        self.assertFalse(ok)
        self.assertIn("invalid response", detail)
        self.assertEqual(chan.connects, 1, "the message must not be sent twice")

    def test_send_to_claude_uses_mcp_channel_socket(self):
        class FakeSocket:
            def __init__(self, *_):
                self.sent = b""
                self.received = False

            def __enter__(self): return self
            def __exit__(self, *_): return False
            def settimeout(self, _): pass
            def connect(self, path): self.path = path
            def sendall(self, data): self.sent = data
            def shutdown(self, _): pass
            def recv(self, _):
                if self.received: return b""
                self.received = True
                return b'{"ok": true, "message_id": "m1"}'

        sock = FakeSocket()
        with patch.object(antiphon.socket, "socket", return_value=sock):
            self.assertEqual(antiphon.send_to_claude("/tmp/project", "test"), (True, ""))
        self.assertEqual(sock.path, antiphon.claude_socket_path("/tmp/project"))
        self.assertEqual(json.loads(sock.sent)["content"], "test")

    def test_channel_path_is_project_specific(self):
        self.assertNotEqual(antiphon.claude_socket_path("/tmp/a"),
                            antiphon.claude_socket_path("/tmp/b"))

    def test_channel_reply_goes_to_codex_queue_with_agent_label(self):
        with patch.object(antiphon, "codex_session_id", return_value="session"), \
             patch.object(antiphon, "send_to_codex", return_value=(True, "")) as send, \
             patch.object(antiphon.sys, "stdin",
                          io.StringIO('{"text":"reply"}')):
            self.assertEqual(antiphon.reply(), 0)
        send.assert_called_once_with("session", "[Antiphon channel] Claude: reply")

    def test_hook_is_silent_and_only_injects_context(self):
        """The hook prints nothing to the terminal: the summary goes into the context, the user is not disturbed."""
        out = io.StringIO()
        with patch.object(antiphon, "project_dir", return_value="/tmp/project"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon, "build_summary", return_value=("summary", 123.0, 2)), \
             patch.object(antiphon.sys, "stdin",
                          io.StringIO('{"cwd": "/tmp/project"}')), \
             contextlib.redirect_stdout(out):
            self.assertEqual(antiphon.hook("claude"), 0)
        output = json.loads(out.getvalue())
        self.assertEqual(output["hookSpecificOutput"]["additionalContext"], "summary")
        self.assertNotIn("systemMessage", output)

    def test_status_does_not_crash_on_a_string_cursor(self):
        out = io.StringIO()
        with patch.object(antiphon, "project_dir", return_value="/tmp/project"), \
             patch.object(antiphon, "claude_transcripts", return_value=[]), \
             patch.object(antiphon, "codex_rollout_files", return_value=[]), \
             patch.object(antiphon, "read_cursor",
                          return_value={"codex_seen": 1.0, "last_pushed_claude": "message"}), \
             patch.object(antiphon, "build_summary", return_value=("", 0.0, 0)), \
             contextlib.redirect_stdout(out):
            self.assertEqual(antiphon.status(), 0)
        self.assertIn("cursor last_pushed_claude: message", out.getvalue())

    def test_setup_writes_path_based_commands(self):
        """Hooks must not stay pinned to an absolute file path, so the package can move
        (or be installed globally) without breaking them. `.mcp.json` is the one
        exception: its `env` legitimately carries the absolute project path, because
        that's the only way the channel server (invoked as a bare `antiphon` command,
        with no project argument) can know which project it serves. This test checks
        the `command`/`args` of `.mcp.json` for path-independence and leaves `env` out
        of that check."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.setup(), 0)

            with open(os.path.join(project, ".claude", "settings.json"),
                      encoding="utf-8") as f:
                claude = json.load(f)
            with open(os.path.join(project, ".codex", "hooks.json"),
                      encoding="utf-8") as f:
                codex = json.load(f)
            with open(os.path.join(project, ".mcp.json"), encoding="utf-8") as f:
                mcp = json.load(f)
            with open(os.path.join(project, ".claude", "settings.local.json"),
                      encoding="utf-8") as f:
                local = json.load(f)

        configs = json.dumps([claude, codex])
        self.assertNotIn("/", configs)  # no absolute paths
        self.assertNotIn("python3 ", configs)
        self.assertIn("antiphon hook claude", configs)
        self.assertIn("antiphon push codex", configs)
        self.assertIn("antiphon hook codex", configs)
        self.assertIn("antiphon push claude", configs)

        # .mcp.json's `command`/`args` must be PATH-resolved too — but its `env` is
        # exempt: ANTIPHON_CWD is deliberately an absolute path (see docstring above).
        mcp_server = mcp["mcpServers"]["antiphon"]
        command_and_args = json.dumps([mcp_server["command"], mcp_server["args"]])
        self.assertNotIn("/", command_and_args)
        self.assertNotIn("python3", command_and_args)
        self.assertEqual(mcp_server["command"], "antiphon")
        self.assertEqual(mcp_server["args"], ["channel"])
        self.assertEqual(mcp_server["env"], {"ANTIPHON_CWD": project})

        self.assertEqual(local["enabledMcpjsonServers"], ["antiphon"])

    def test_setup_registers_the_codex_side_mcp_server(self):
        """Codex reads a project-local `.codex/config.toml`. Without an entry there,
        Codex never sees `antiphon_read` — the README used to ask the user to add one
        to `~/.codex/config.toml` by hand. setup owns this file like the other six."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.setup(), 0)
            with open(os.path.join(project, ".codex", "config.toml"),
                      encoding="utf-8") as f:
                config = f.read()

        self.assertEqual(config.count("[mcp_servers.antiphon]"), 1)
        self.assertIn('command = "antiphon"', config)
        self.assertIn('args = ["mcp"]', config)
        self.assertIn(f'ANTIPHON_CWD = "{project}"', config)

    def test_setup_forwards_the_alias_to_the_codex_server(self):
        """Codex does not pass the parent environment to an MCP server: measured
        on live processes, the Claude child carried 46 variables and the Codex
        child 10 — a curated set plus whatever `env` declares. So `ANTIPHON_NAME`
        never reaches `antiphon mcp` however the terminal was started, and an
        alias only the hook can read joins nothing. `env_vars` names a variable
        to forward rather than a value to set."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.setup(), 0)
            with open(os.path.join(project, ".codex", "config.toml"),
                      encoding="utf-8") as f:
                config = f.read()
        self.assertIn('env_vars = ["ANTIPHON_NAME"]', config)

    def test_setup_adds_the_forward_to_a_config_that_predates_it(self):
        """An existing install gets it on the next `setup`, exactly once."""
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, ".codex"))
            with open(os.path.join(project, ".codex", "config.toml"), "w",
                      encoding="utf-8") as f:
                f.write('[mcp_servers.unrelated]\ncommand = "keep-me"\n\n'
                        '[mcp_servers.antiphon]\ncommand = "antiphon"\n'
                        'args = ["mcp"]\n\n'
                        '[mcp_servers.antiphon.env]\n'
                        f'ANTIPHON_CWD = "{project}"\n')
            with patch.object(antiphon, "project_dir", return_value=project), \
                 contextlib.redirect_stdout(io.StringIO()):
                antiphon.setup()
                antiphon.setup()
            with open(os.path.join(project, ".codex", "config.toml"),
                      encoding="utf-8") as f:
                config = f.read()
        self.assertEqual(config.count("env_vars"), 1)
        self.assertEqual(config.count("[mcp_servers.antiphon]"), 1)
        self.assertIn('command = "keep-me"', config,
                      "an unrelated table still survives the rewrite")

    @unittest.skipUnless(tomllib, "tomllib needs Python 3.11+")
    def test_the_forwarded_variable_parses_as_a_list_of_strings(self):
        """The text assertions above run on every Python 3; where the stdlib can
        parse TOML, prove the shape Codex will actually read."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.setup(), 0)
            with open(os.path.join(project, ".codex", "config.toml"), "rb") as f:
                config = tomllib.load(f)
        server = config["mcp_servers"]["antiphon"]
        self.assertEqual(server["env_vars"], ["ANTIPHON_NAME"])
        self.assertEqual(server["env"]["ANTIPHON_CWD"], project,
                         "the forward is additive: the directory is still set")

    def test_setup_repairs_a_codex_config_aimed_at_the_wrong_server(self):
        """The bug this file was written for: a hand-written entry naming the
        `channel` server (Claude's, not Codex's) and a project directory left over
        from before a rename. Codex then loads `reply_to_codex` instead of
        `antiphon_read`. setup must repair it and leave other sections alone."""
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, ".codex"))
            with open(os.path.join(project, ".codex", "config.toml"), "w",
                      encoding="utf-8") as f:
                f.write('[mcp_servers.unrelated]\ncommand = "keep-me"\n\n'
                        '[mcp_servers.antiphon]\ncommand = "antiphon"\n'
                        'args = ["channel"]\n\n'
                        '[mcp_servers.antiphon.env]\n'
                        'ANTIPHON_CWD = "/gone/old-project-name"\n')

            with patch.object(antiphon, "project_dir", return_value=project), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(antiphon.setup(), 0)
            with open(os.path.join(project, ".codex", "config.toml"),
                      encoding="utf-8") as f:
                config = f.read()

        self.assertEqual(config.count("[mcp_servers.antiphon]"), 1)
        self.assertIn('args = ["mcp"]', config)
        self.assertNotIn('args = ["channel"]', config)
        self.assertIn(f'ANTIPHON_CWD = "{project}"', config)
        self.assertNotIn("/gone/old-project-name", config)
        self.assertIn('command = "keep-me"', config)   # unrelated section survived

    @unittest.skipUnless(tomllib, "tomllib needs Python 3.11+")
    def test_generated_codex_config_parses_as_toml(self):
        """The text assertions above pin the contract on every Python 3; where the
        stdlib can parse TOML, also prove the file is structurally what Codex reads."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.setup(), 0)
            with open(os.path.join(project, ".codex", "config.toml"), "rb") as f:
                config = tomllib.load(f)

        server = config["mcp_servers"]["antiphon"]
        self.assertEqual(server["command"], "antiphon")
        self.assertEqual(server["args"], ["mcp"])
        self.assertEqual(server["env"]["ANTIPHON_CWD"], project)

    def test_setup_upgrades_legacy_absolute_path_hooks(self):
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, ".claude"))
            os.makedirs(os.path.join(project, ".codex"))
            legacy = "/some/other/install/antiphon.py"
            with open(os.path.join(project, ".claude", "settings.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command":
                                                       f"python3 {legacy} kanca claude"}]}],
                    "Stop": [{"hooks": [{"type": "command", "command":
                                           f"python3 {legacy} it codex"}]}],
                }}, f)
            with open(os.path.join(project, ".codex", "hooks.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command":
                                                       f"python3 {legacy} kanca codex"}]}],
                    "Stop": [{"hooks": [{"type": "command", "command":
                                           f"python3 {legacy} it claude"}]}],
                }}, f)

            with patch.object(antiphon, "project_dir", return_value=project), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(antiphon.setup(), 0)

            for rel in (".claude/settings.json", ".codex/hooks.json"):
                with open(os.path.join(project, rel), encoding="utf-8") as f:
                    content = f.read()
                self.assertNotIn(legacy, content)
                self.assertEqual(content.count("antiphon hook"), 1)
                self.assertEqual(content.count("antiphon push"), 1)

    # ---- Minor: the CLI must not answer a typo with a traceback ----

    def test_cli_rejects_extra_arguments_with_a_usage_error(self):
        """`antiphon status foo` used to be a raw TypeError traceback."""
        script = os.path.join(os.path.dirname(__file__), "..", "lib", "antiphon.py")
        result = subprocess.run([sys.executable, script, "status", "foo"],
                                capture_output=True, text=True, timeout=60,
                                stdin=subprocess.DEVNULL)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("status", result.stderr)

    # ---- Ruling 6 correction: the self-injection guard is a label test ----

    @staticmethod
    def _claude_user_texts(text):
        line = json.dumps({"type": "user", "timestamp": "2026-08-30T10:00:00.000Z",
                           "message": {"content": text}})
        with patch.object(antiphon, "claude_transcripts", return_value=["t.jsonl"]), \
             patch.object(antiphon, "tail_lines", return_value=[line]):
            return [e[2] for e in antiphon.claude_events("/tmp/project")]

    @staticmethod
    def _codex_user_texts(text):
        line = json.dumps({"type": "response_item", "timestamp": "2026-08-30T10:00:00.000Z",
                           "payload": {"type": "message", "role": "user",
                                       "content": [{"type": "input_text", "text": text}]}})
        with patch.object(antiphon, "codex_rollout_files", return_value=["r.jsonl"]), \
             patch.object(antiphon, "tail_lines", return_value=[line]):
            return [e[2] for e in antiphon.codex_events("/tmp/project")]

    def test_a_user_message_naming_the_tool_is_not_swallowed(self):
        """A bare substring test over the first 40 characters silently dropped a
        genuine user message from the other side's summary — the worst possible
        failure for a tool whose users type its own name."""
        for text in ("antiphon is dropping messages",
                     "quoting the label: [Antiphon bridge] Claude: hi"):
            self.assertEqual(self._claude_user_texts(text), [text], text)
            self.assertEqual(self._codex_user_texts(text), [text], text)

    def test_pushed_messages_are_still_filtered_out(self):
        """What the guard is actually for: messages this bridge pushed land in the
        other side's transcript as `role: user` and must not echo back."""
        labels = [antiphon.PUSH_LABEL, antiphon.CHANNEL_LABEL,
                  "[antiphon BRIDGE] claude:"]
        for label in labels:
            text = f"{label} run the tests"
            self.assertEqual(self._claude_user_texts(text), [], label)
            self.assertEqual(self._codex_user_texts(text), [], label)

    def test_push_and_reply_use_the_same_labels_the_guard_matches(self):
        """The guard is derived from the constants push() and reply() send, so the
        two can never drift apart."""
        with patch.object(antiphon, "codex_session_id", return_value="session"), \
             patch.object(antiphon, "send_to_codex", return_value=(True, "")) as send, \
             patch.object(antiphon.sys, "stdin", io.StringIO('{"text":"reply"}')):
            self.assertEqual(antiphon.reply(), 0)
        sent = send.call_args.args[1]
        self.assertTrue(sent.startswith(antiphon.CHANNEL_LABEL))
        self.assertEqual(self._codex_user_texts(sent), [])

    # ---- Important 2: upgrading a legacy hook must not leave a duplicate ----

    @staticmethod
    def _hook_commands(hooks):
        return [entry.get("command")
                for group in hooks for entry in group.get("hooks") or []]

    def test_add_hook_collapses_a_legacy_entry_onto_an_already_migrated_one(self):
        """A half-migrated config (one legacy entry, one already upgraded) used to
        end up with two identical entries, so the hook fired twice per turn."""
        command = antiphon.HOOK_COMMAND.format(side="claude")
        hooks = [
            {"hooks": [{"type": "command",
                        "command": "python3 /old/install/antiphon.py kanca claude"}]},
            {"hooks": [{"type": "command", "command": command}]},
        ]
        legacy = antiphon._legacy_commands("/x/lib/antiphon.py", "kanca", "claude")
        self.assertTrue(antiphon._add_hook(hooks, command, legacy))
        self.assertEqual(self._hook_commands(hooks), [command])

    def test_add_hook_collapses_two_legacy_entries_upgrading_to_the_same_command(self):
        command = antiphon.HOOK_COMMAND.format(side="claude")
        hooks = [
            {"hooks": [{"type": "command", "command": "python3 /a/antiphon.py kanca claude"}]},
            {"hooks": [{"type": "command", "command": "python3 /b/antiphon.py kanca claude"}]},
        ]
        legacy = antiphon._legacy_commands("/x/lib/antiphon.py", "kanca", "claude")
        self.assertTrue(antiphon._add_hook(hooks, command, legacy))
        self.assertEqual(self._hook_commands(hooks), [command])

    def test_add_hook_leaves_another_tool_alone(self):
        command = antiphon.HOOK_COMMAND.format(side="claude")
        hooks = [{"hooks": [{"type": "command", "command": "some-other-tool run"}]}]
        self.assertTrue(antiphon._add_hook(hooks, command))
        self.assertEqual(self._hook_commands(hooks), ["some-other-tool run", command])

    def test_setup_never_creates_duplicates_from_a_mixed_state_config(self):
        """README's promise: re-running setup migrates in place and never
        duplicates — even when a config holds both an old and a new entry."""
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, ".claude"))
            with open(os.path.join(project, ".claude", "settings.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command",
                                    "command": "python3 /old/antiphon.py kanca claude"}]},
                        {"hooks": [{"type": "command",
                                    "command": "antiphon hook claude"}]},
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command",
                                    "command": "python3 /a/antiphon.py it codex"}]},
                        {"hooks": [{"type": "command",
                                    "command": "python3 /b/antiphon.py it codex"}]},
                    ],
                }}, f)

            with patch.object(antiphon, "project_dir", return_value=project), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(antiphon.setup(), 0)

            with open(os.path.join(project, ".claude", "settings.json"),
                      encoding="utf-8") as f:
                settings = json.load(f)
            self.assertEqual(
                self._hook_commands(settings["hooks"]["UserPromptSubmit"]),
                ["antiphon hook claude"])
            self.assertEqual(self._hook_commands(settings["hooks"]["Stop"]),
                             ["antiphon push codex"])

    # ---- Important 1: a rollout's cwd must match exactly, not by prefix ----

    SIBLING_UUID = "11111111-2222-3333-4444-555555555555"
    MINE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    @staticmethod
    def _write_rollout(directory, uuid, cwd, body=""):
        """A rollout whose head line carries cwd where Codex really puts it."""
        path = os.path.join(directory, f"rollout-2026-08-30T10-00-00-{uuid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-08-30T10:00:00.000Z",
                                "type": "session_meta",
                                "payload": {"session_id": uuid, "cwd": cwd,
                                            "base_instructions": "You are Codex."}}) + "\n")
            f.write(body)
        return path

    def test_codex_rollout_files_reject_a_prefix_colliding_sibling(self):
        """`/Users/x/api` must not match a rollout recorded for `/Users/x/api-v2`
        — an `@codex` push would land in the wrong project's Codex."""
        with tempfile.TemporaryDirectory() as sessions:
            day = os.path.join(sessions, "2026", "08", "30")
            os.makedirs(day)
            self._write_rollout(day, self.SIBLING_UUID, "/Users/x/api-v2")
            mine = self._write_rollout(day, self.MINE_UUID, "/Users/x/api")
            with patch.object(antiphon, "CODEX_SESSIONS", sessions):
                self.assertEqual(antiphon.codex_rollout_files("/Users/x/api"), [mine])

    def test_codex_session_id_is_none_when_only_a_sibling_project_is_open(self):
        with tempfile.TemporaryDirectory() as sessions:
            day = os.path.join(sessions, "2026", "08", "30")
            os.makedirs(day)
            self._write_rollout(day, self.SIBLING_UUID, "/Users/x/api-v2")
            with patch.object(antiphon, "CODEX_SESSIONS", sessions):
                self.assertIsNone(antiphon.codex_session_id("/Users/x/api"))

    def test_codex_rollout_files_keep_the_substring_test_without_a_cwd_field(self):
        """Behaviour is preserved for heads that carry no cwd field at all."""
        with tempfile.TemporaryDirectory() as sessions:
            day = os.path.join(sessions, "2026", "08", "30")
            os.makedirs(day)
            path = os.path.join(day, f"rollout-2026-08-30T10-00-00-{self.MINE_UUID}.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json at all /Users/x/api\n")
            with patch.object(antiphon, "CODEX_SESSIONS", sessions):
                self.assertEqual(antiphon.codex_rollout_files("/Users/x/api"), [path])

    # ---- Critical 2: finding this project's Claude transcript directory ----

    def test_claude_slug_matches_claude_code_directory_naming(self):
        """Claude Code replaces every non-alphanumeric character with `-`, not
        just `/`. Each case below mirrors the shape of a real directory seen
        under ~/.claude/projects: a plain project path, a nested scratchpad whose
        own name already contains the encoded form of another path, a macOS
        temporary directory, and a path with an underscore."""
        cases = {
            "/Users/ada/Documents/antiphon":
                "-Users-ada-Documents-antiphon",
            "/private/tmp/claude-501/-Users-ada-Documents-widgets"
            "/a1b2c3d4-5e6f-7a8b-9c0d-1e2f3a4b5c6d/scratchpad":
                "-private-tmp-claude-501--Users-ada-Documents-widgets"
                "-a1b2c3d4-5e6f-7a8b-9c0d-1e2f3a4b5c6d-scratchpad",
            "/private/var/folders/ab/cd3_ef_gh4ijklmnop5qrstuvwx0000gn/T/tmp.9XyZaBcDeF":
                "-private-var-folders-ab-cd3-ef-gh4ijklmnop5qrstuvwx0000gn-T-tmp-9XyZaBcDeF",
            "/Users/me/dev/my_app": "-Users-me-dev-my-app",
        }
        for cwd, expected in cases.items():
            self.assertEqual(antiphon._claude_slug(cwd), expected, cwd)

    def test_claude_transcripts_found_for_a_path_with_an_underscore(self):
        """`~/dev/my_app` used to produce a slug matching no directory at all,
        so Codex never saw anything Claude did — silently, forever."""
        with tempfile.TemporaryDirectory() as projects:
            directory = os.path.join(projects, "-Users-me-dev-my-app")
            os.makedirs(directory)
            transcript = self._write_claude_transcript(directory, "a.jsonl",
                                                       "/Users/me/dev/my_app")
            with patch.object(antiphon, "CLAUDE_PROJECTS", projects):
                self.assertEqual(antiphon.claude_transcripts("/Users/me/dev/my_app"),
                                 [transcript])

    def test_claude_transcripts_do_not_scan_when_the_slug_hits(self):
        """The common path must stay cheap: no directory scan when the slug
        names a directory that exists."""
        with tempfile.TemporaryDirectory() as projects:
            directory = os.path.join(projects, "-tmp-plain")
            os.makedirs(directory)
            transcript = self._write_claude_transcript(directory, "a.jsonl", "/tmp/plain")
            with patch.object(antiphon, "CLAUDE_PROJECTS", projects), \
                 patch.object(antiphon, "_find_claude_project_dir") as scan:
                self.assertEqual(antiphon.claude_transcripts("/tmp/plain"), [transcript])
            scan.assert_not_called()

    def test_claude_transcripts_fall_back_to_scanning_when_the_slug_misses(self):
        """If the slug rule ever stops matching, identify the directory by the
        cwd its transcripts actually carry rather than going silently blind."""
        with tempfile.TemporaryDirectory() as projects:
            decoy = os.path.join(projects, "-tmp-other")
            os.makedirs(decoy)
            self._write_claude_transcript(decoy, "b.jsonl", "/tmp/other")

            renamed = os.path.join(projects, "a-rule-we-do-not-know")
            os.makedirs(renamed)
            transcript = self._write_claude_transcript(renamed, "a.jsonl", "/tmp/wanted")

            with patch.object(antiphon, "CLAUDE_PROJECTS", projects):
                self.assertEqual(antiphon.claude_transcripts("/tmp/wanted"), [transcript])

    @staticmethod
    def _write_claude_transcript(directory, name, cwd):
        """A minimal Claude transcript: the cwd sits in the head, as it really does."""
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "last-prompt", "prompt": "hi"}) + "\n")
            f.write(json.dumps({"type": "attachment", "cwd": cwd}) + "\n")
        return path

    # ---- Critical 1: a malformed config file must never be overwritten ----

    def test_update_json_never_clobbers_an_unparseable_file(self):
        """A settings file with a trailing comma (or a comment, or a BOM) must
        survive untouched: rewriting it would silently drop the user's
        permissions, env and every other tool's hooks."""
        with tempfile.TemporaryDirectory() as project:
            path = os.path.join(project, "settings.json")
            original = '{\n  "permissions": {"allow": ["Bash(ls:*)"]},\n}\n'
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)

            with self.assertRaises(antiphon.ConfigFileError):
                antiphon._update_json(path, lambda data: True)

            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), original)

    def test_update_json_still_creates_a_missing_file(self):
        """A missing file is not an error — that is the normal first install."""
        with tempfile.TemporaryDirectory() as project:
            path = os.path.join(project, "nested", "settings.json")
            self.assertTrue(antiphon._update_json(
                path, lambda data: data.setdefault("hooks", {}) is not None))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"hooks": {}})

    def test_setup_reports_failure_instead_of_destroying_a_broken_config(self):
        """setup must not claim success while a file it could not read is left
        alone: the user's own settings stay put, the other files still get
        installed, and the exit code says something went wrong."""
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, ".claude"))
            broken = os.path.join(project, ".claude", "settings.json")
            original = '{\n  "permissions": {"allow": ["Bash(ls:*)"]},\n}\n'
            with open(broken, "w", encoding="utf-8") as f:
                f.write(original)

            out, err = io.StringIO(), io.StringIO()
            with patch.object(antiphon, "project_dir", return_value=project), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = antiphon.setup()

            with open(broken, encoding="utf-8") as f:
                self.assertEqual(f.read(), original)      # untouched
            self.assertEqual(code, 1)                     # and setup says so
            self.assertIn(broken, out.getvalue() + err.getvalue())
            # one unreadable file does not skip the rest of the installation
            self.assertTrue(os.path.exists(
                os.path.join(project, ".codex", "hooks.json")))
            self.assertTrue(os.path.exists(os.path.join(project, ".mcp.json")))

    # ---- one cursor per named peer ----

    def test_an_unnamed_peer_keeps_the_project_wide_cursor(self):
        """The compatibility contract: with no name there is one peer per side,
        nothing to race with, and the path must not move."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(antiphon.state_path("/tmp/project", "claude"),
                             os.path.join("/tmp/project", ".antiphon", "cursor.json"))

    def test_a_named_peer_gets_its_own_cursor_file(self):
        with patch.dict(os.environ, {"ANTIPHON_NAME": "ui"}):
            self.assertEqual(
                antiphon.state_path("/tmp/project", "claude"),
                os.path.join(antiphon.peers.peer_dir("/tmp/project", "claude", "ui"),
                             "cursor.json"))

    def test_the_two_sides_do_not_share_a_cursor_under_one_name(self):
        """A user may well set the same ANTIPHON_NAME in both terminals."""
        with patch.dict(os.environ, {"ANTIPHON_NAME": "ui"}):
            self.assertNotEqual(antiphon.state_path("/tmp/project", "claude"),
                                antiphon.state_path("/tmp/project", "codex"))

    def test_two_named_peers_do_not_consume_each_others_events(self):
        """One shared cursor meant the first reader advanced it for everyone and
        the others never saw the event at all."""
        with tempfile.TemporaryDirectory() as project:
            with patch.dict(os.environ, {"ANTIPHON_NAME": "ui"}):
                antiphon.write_cursor(project, {"codex_seen": 100.0}, "claude")
            with patch.dict(os.environ, {"ANTIPHON_NAME": "api"}):
                self.assertEqual(antiphon.read_cursor(project, "claude"), {})
                antiphon.write_cursor(project, {"codex_seen": 200.0}, "claude")
            with patch.dict(os.environ, {"ANTIPHON_NAME": "ui"}):
                self.assertEqual(antiphon.read_cursor(project, "claude"),
                                 {"codex_seen": 100.0})

    def test_an_invalid_name_falls_back_to_the_project_cursor(self):
        """A malformed name must not put a cursor somewhere unreachable."""
        with patch.dict(os.environ, {"ANTIPHON_NAME": "Not Valid"}):
            self.assertEqual(antiphon.state_path("/tmp/project", "codex"),
                             os.path.join("/tmp/project", ".antiphon", "cursor.json"))

    def test_cursor_keys_from_the_turkish_era_are_translated_in_place(self):
        """A cursor written before the English rename carries Turkish keys; reading
        it must translate them and persist the translation."""
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, ".antiphon"))
            path = os.path.join(project, ".antiphon", "cursor.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"codex_gordu": 12.0, "son_itilen_claude": "hello"}, f)

            self.assertEqual(antiphon.read_cursor(project, "claude"), {
                "codex_seen": 12.0,
                "last_pushed_claude": "hello",
            })
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {
                    "codex_seen": 12.0,
                    "last_pushed_claude": "hello",
                })



if __name__ == "__main__":
    unittest.main()
