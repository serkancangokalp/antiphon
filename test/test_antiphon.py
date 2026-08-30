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
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

# The suite describes a project, not the terminal it happens to run in.
# `ANTIPHON_NAME` moves cursors and sockets, so `ANTIPHON_NAME=ui npm test` —
# a reasonable thing to run now — would otherwise exercise a different layout
# than a bare run. Tests that want a name set one with `patch.dict`.
os.environ.pop("ANTIPHON_NAME", None)


def read_source(*parts):
    with open(os.path.join(os.path.dirname(__file__), "..", *parts),
              encoding="utf-8") as f:
        return f.read()


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
                          side_effect=lambda cwd, msg, alias=None, **_:
                              sent.append((cwd, msg)) or (True, "")), \
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
                          side_effect=lambda cwd, msg, alias=None, **_:
                              sent.append(msg) or (True, "")), \
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

    # A mock that stands in for a transport nobody should touch must raise, not
    # record. A bare MagicMock socket answers `recv` with a truthy object for
    # ever, so a wrong turn does not fail the test — it hangs the suite.
    # Measured twice while mutating this very code.
    UNTOUCHABLE = AssertionError("this message must touch no transport")

    def test_a_named_marker_that_reaches_nobody_is_reported_not_redirected(self):
        """A name that does not resolve is refused out loud. Delivering it to
        whoever is around instead would be the silent misroute wearing a
        recipient's name."""
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as project:
            input_data["cwd"] = project
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_codex_reply",
                              return_value="@claude:api run"), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor") as write, \
                 patch.object(antiphon.socket, "socket",
                              side_effect=self.UNTOUCHABLE) as sock, \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(input_data))), \
                 contextlib.redirect_stderr(err):
                self.assertEqual(antiphon.push("claude"), 0)
                sock.assert_not_called()
                write.assert_not_called()
        self.assertIn("api", err.getvalue())
        self.assertIn("not delivered", err.getvalue())

    def test_a_refused_named_line_does_not_take_the_bare_one_down_with_it(self):
        """Each recipient stands alone. A name that cannot be resolved must cost
        that line and nothing else."""
        sent = []
        input_data = {"cwd": "/tmp/project", "transcript_path": "/tmp/rollout"}
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply",
                          return_value="@claude:api named\n@claude bare"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon, "send_to_claude",
                          side_effect=lambda cwd, msg, alias=None, **_:
                              (False, "not delivered: no live claude peer named "
                                      f"{alias!r}") if alias
                              else (sent.append(msg) or (True, ""))), \
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
                          side_effect=lambda cwd, msg, alias=None, **_:
                              sent.append((cwd, msg)) or (True, "")), \
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
        self.assertEqual(written, [{"last_pushed_claude":
                                    {"": antiphon.batch_fingerprint(["run the tests"])}}],
                         "recorded in the unaddressed slot, in the shape the "
                         "Stop hook compares against")

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
        self.assertEqual(written, [{"last_pushed_codex":
                                    {"": antiphon.batch_fingerprint(["hello"])}}])

    # ---- choosing which peer a message goes to: see RoutingTest ----

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
             patch.object(antiphon, "resolve_target",
                          side_effect=lambda cwd, kind, alias=None:
                              ("/tmp/ui.sock", "")) as resolve:
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
             patch.object(antiphon, "resolve_target",
                          side_effect=lambda cwd, kind, alias=None:
                              ("/tmp/ui.sock", "")):
            started = time.monotonic()
            ok, detail = antiphon.send_to_claude(project, "hello")
            elapsed = time.monotonic() - started
        self.assertFalse(ok)
        self.assertIn("down", detail)
        self.assertLess(elapsed, 3.0, "retrying must stay bounded")

    def test_an_ambiguous_target_is_not_retried(self):
        """Waiting cannot resolve ambiguity — more peers will not become fewer."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "resolve_target",
                          side_effect=lambda cwd, kind, alias=None:
                              (None, "not delivered: 2 peers")) as resolve:
            ok, detail = antiphon.send_to_claude(project, "hello")
        self.assertFalse(ok)
        self.assertIn("2 peers", detail)
        self.assertEqual(resolve.call_count, 1)

    def test_a_failure_after_the_bytes_went_out_is_not_retried(self):
        """Retrying here would deliver the message twice."""
        chan = self._Channel(missing=0, reply=b"not json at all")
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon.socket, "socket", chan), \
             patch.object(antiphon, "resolve_target",
                          side_effect=lambda cwd, kind, alias=None:
                              ("/tmp/ui.sock", "")):
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
        with patch.object(antiphon, "project_dir", return_value="/tmp/project"), \
             patch.object(antiphon, "send_to_codex", return_value=(True, "")) as send, \
             patch.object(antiphon.sys, "stdin",
                          io.StringIO('{"text":"reply"}')):
            self.assertEqual(antiphon.reply(), 0)
        self.assertEqual(send.call_args.args[0], "/tmp/project")
        self.assertTrue(send.call_args.args[1].startswith(
            "[Antiphon channel] Claude:"))

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

            # One per event, not one per file: the Codex hook is installed
            # under SessionStart as well as UserPromptSubmit, and upgrading a
            # legacy entry must still leave exactly one copy under each.
            for rel, hook_copies in ((".claude/settings.json", 1),
                                     (".codex/hooks.json", 2)):
                with open(os.path.join(project, rel), encoding="utf-8") as f:
                    content = f.read()
                self.assertNotIn(legacy, content)
                self.assertEqual(content.count("antiphon hook"), hook_copies, rel)
                self.assertEqual(content.count("antiphon push"), 1, rel)

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
        with patch.object(antiphon, "project_dir", return_value="/tmp/project"), \
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



class CodexPeerWiringTest(unittest.TestCase):
    """The two Codex writers, wired to the processes that actually run them.

    The MCP server registers an endpoint when it starts and releases it when it
    stops; the hook records which session is behind the alias on every event it
    sees. Everything here is a layer over a bridge that already works unnamed,
    so no failure in it may cost a session its tools or its context.
    """

    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
    OTHER = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"

    @staticmethod
    @contextlib.contextmanager
    def _named(name):
        """`ANTIPHON_NAME` as this session would really see it — set, or absent."""
        with patch.dict(os.environ, {}):
            os.environ.pop("ANTIPHON_NAME", None)
            if name:
                os.environ["ANTIPHON_NAME"] = name
            yield

    def _register(self, project, name="build", owner="300:x"):
        err = io.StringIO()
        with self._named(name), \
             patch.object(antiphon.peers, "owner_key", return_value=owner), \
             contextlib.redirect_stderr(err):
            alias = antiphon.register_codex_peer(project)
        return alias, err.getvalue()

    def _hook(self, project, event=None, session_id=None, name="build",
              owner="300:x", side="codex", summary=("", None, 0)):
        payload = {"cwd": project, "transcript_path": "/t/r.jsonl"}
        if event is not None:
            payload["hook_event_name"] = event
        if session_id is not None:
            payload["session_id"] = session_id
        out, err, written = io.StringIO(), io.StringIO(), []
        with self._named(name), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(antiphon.peers, "owner_key", return_value=owner), \
             patch.object(antiphon, "build_summary", return_value=summary), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor",
                          side_effect=lambda cwd, data, kind: written.append(kind)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = antiphon.hook(side)
        return code, out.getvalue(), err.getvalue(), written

    @staticmethod
    def _run_mcp(project, *requests, name=None, owner="300:x"):
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}):
            os.environ.pop("ANTIPHON_NAME", None)
            if name:
                os.environ["ANTIPHON_NAME"] = name
            with patch.object(antiphon, "project_dir", return_value=project), \
                 patch.object(antiphon.sys, "stdin", stdin), \
                 patch.object(antiphon.peers, "owner_key", return_value=owner), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                antiphon.mcp()
        return ([json.loads(line) for line in out.getvalue().splitlines()
                 if line.strip()], err.getvalue())

    # ---- the server claims its alias ----

    def test_a_named_server_claims_its_alias_and_is_told_which_one(self):
        """`mcp()` releases what this returns, so it has to be what was won."""
        with tempfile.TemporaryDirectory() as project:
            alias, err = self._register(project)
            self.assertEqual(alias, "build")
            self.assertEqual(err, "")
            peer = antiphon.peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["pid"], os.getpid())
        self.assertEqual(peer["owner"], "300:x")
        self.assertIsNone(peer["address"], "live, and nothing can reach it yet")

    def test_an_unnamed_server_says_nothing_and_asks_nobody_who_it_is(self):
        """The unchanged single-peer case. Nothing was asked for, so there is
        nothing to warn about — and no reason to walk a process tree either."""
        with tempfile.TemporaryDirectory() as project:
            with self._named(None), \
                 patch.object(antiphon.peers, "owner_key") as walk, \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(antiphon.register_codex_peer(project))
            walk.assert_not_called()
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(antiphon.peers.read_peers(project), [])

    def test_an_unusable_alias_names_the_rule_it_broke_and_asks_nobody(self):
        """They typed it. Silence would let them believe `@codex:Build!` works.

        And a name that can never be registered is not worth a process walk: the
        answer cannot change what happens next."""
        with tempfile.TemporaryDirectory() as project:
            with self._named("Build!"), \
                 patch.object(antiphon.peers, "owner_key") as walk, \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(antiphon.register_codex_peer(project))
            walk.assert_not_called()
        self.assertIn("a-z0-9", err.getvalue())

    def test_an_alias_that_cannot_be_identified_says_so(self):
        with tempfile.TemporaryDirectory() as project:
            alias, err = self._register(project, owner=None)
        self.assertIsNone(alias)
        self.assertIn("build", err)
        self.assertIn("named routing disabled", err)

    def test_a_registry_that_cannot_be_written_disables_naming_and_says_so(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon.peers, "register",
                          side_effect=OSError("read-only file system")):
            alias, err = self._register(project)
        self.assertIsNone(alias)
        self.assertIn("named routing disabled", err)
        self.assertIn("read-only file system", err)

    def test_a_claim_refused_by_a_live_holder_returns_nothing(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "codex", "build", None,
                                    pid=os.getppid(), owner_key="300:first")
            alias, err = self._register(project, owner="301:second")
        self.assertIsNone(alias, "nothing was claimed, so nothing may be released")
        self.assertIn("build", err)

    # ---- the server's lifetime ----

    def test_the_server_registers_before_it_reads_a_request(self):
        """A server that registered only once a message arrived would be
        invisible for exactly the window in which somebody is deciding who to
        address."""
        order = []

        class Watching(io.StringIO):
            def __iter__(self):
                order.append("read")
                return super().__iter__()

        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             patch.object(antiphon, "register_codex_peer",
                          side_effect=lambda cwd: order.append("register") or "build"), \
             patch.object(antiphon.peers, "unregister",
                          side_effect=lambda *a, **k: order.append("release")), \
             patch.object(antiphon.sys, "stdin", Watching("")), \
             contextlib.redirect_stdout(io.StringIO()):
            antiphon.mcp()
        self.assertEqual(order, ["register", "read", "release"])

    def test_the_server_releases_exactly_the_alias_it_won(self):
        """Not the alias it was asked for. A server refused `build` and then
        releasing `build` on the way out would be releasing somebody else's."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             patch.object(antiphon, "register_codex_peer", return_value="review"), \
             patch.object(antiphon.peers, "unregister") as release, \
             patch.object(antiphon.sys, "stdin", io.StringIO("")), \
             contextlib.redirect_stdout(io.StringIO()):
            antiphon.mcp()
        release.assert_called_once_with(project, "codex", "review", pid=os.getpid())

    def test_a_server_that_claimed_nothing_releases_nothing(self):
        """`unregister` is pid-guarded as well, but a process that never won the
        alias has no business asking about it: the guard is the second line of
        defence, not the first."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             patch.object(antiphon, "register_codex_peer", return_value=None), \
             patch.object(antiphon.peers, "unregister") as release, \
             patch.object(antiphon.sys, "stdin", io.StringIO("")), \
             contextlib.redirect_stdout(io.StringIO()):
            antiphon.mcp()
        release.assert_not_called()

    def test_the_server_releases_its_own_claim_on_the_way_out(self):
        with tempfile.TemporaryDirectory() as project:
            self._run_mcp(project, name="build")
            self.assertEqual(antiphon.peers.read_peers(project), [])

    def test_a_refused_server_releases_nothing_on_its_way_out(self):
        """Two servers, one alias. The loser must not delete the winner's record
        as it exits — that would hand the name to whoever asked next and take a
        working peer down with it."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "codex", "build", None,
                                    pid=os.getppid(), owner_key="300:first")
            endpoint = os.path.join(
                antiphon.peers.peer_dir(project, "codex", "build"), "endpoint.json")
            with open(endpoint, "rb") as f:
                before = f.read()
            self._run_mcp(project, name="build", owner="301:second")
            with open(endpoint, "rb") as f:
                self.assertEqual(f.read(), before, "the holder is untouched")

    def test_a_broken_registry_does_not_cost_the_session_its_tools(self):
        """Named routing is decoration on a bridge that already works. It may
        fail; it may not take `initialize` or `tools/list` down with it."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon.peers, "register",
                          side_effect=OSError("disk gone")), \
             patch.object(antiphon.peers, "unregister",
                          side_effect=OSError("disk gone")):
            replies, _ = self._run_mcp(
                project,
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                name="build")
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertIn("tools", replies[1]["result"])

    # ---- the hook records the session ----

    def test_every_codex_event_refreshes_the_session(self):
        """A missed SessionStart then costs one turn of routability rather than
        the whole session's."""
        for event in ("SessionStart", "UserPromptSubmit", None):
            with tempfile.TemporaryDirectory() as project:
                self._register(project)
                self._hook(project, event=event, session_id=self.UUID)
                peer = antiphon.peers.read_peers(project, "codex")[0]
                self.assertEqual(peer["address"], self.UUID, repr(event))

    def test_only_a_user_prompt_produces_context(self):
        """Nothing else has a prompt for context to attach to, and a wrapper
        naming an event that did not happen is worse than silence."""
        for event in ("SessionStart", "Notification", "SomethingNewInCodex"):
            with tempfile.TemporaryDirectory() as project:
                code, out, _, _ = self._hook(project, event=event,
                                             session_id=self.UUID,
                                             summary=("news", 5.0, 1))
                self.assertEqual(code, 0, event)
                self.assertEqual(out, "", event)

    def test_a_silent_event_never_reads_the_summary_at_all(self):
        """Producing no output is not enough. A summary that is read and not
        shown is a summary somebody could later mark as seen — the loss this
        bridge exists to prevent, arriving one refactor later."""
        payload = {"cwd": "/tmp/project", "hook_event_name": "SessionStart",
                   "session_id": self.UUID}
        with self._named("build"), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(antiphon.peers, "owner_key", return_value="300:x"), \
             patch.object(antiphon, "record_codex_session"), \
             patch.object(antiphon, "build_summary") as summary, \
             patch.object(antiphon, "read_cursor") as cursor, \
             patch.object(antiphon, "write_cursor") as write, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.hook("codex"), 0)
        summary.assert_not_called()
        cursor.assert_not_called()
        write.assert_not_called()

    def test_a_silent_event_does_not_advance_the_cursor(self):
        """The summary it would mark as seen was never shown to anybody."""
        with tempfile.TemporaryDirectory() as project:
            _, _, _, written = self._hook(project, event="SessionStart",
                                          session_id=self.UUID,
                                          summary=("news", 5.0, 1))
            self.assertEqual(written, [], "nothing was read, so nothing is seen")
            _, out, _, written = self._hook(project, event="UserPromptSubmit",
                                            session_id=self.UUID,
                                            summary=("news", 5.0, 1))
        self.assertEqual(written, ["codex"])
        self.assertIn("news", out)

    def test_a_missing_event_name_is_treated_as_a_prompt(self):
        """An older Codex sends no event name, and the only hook it installs is
        the prompt one. Guessing silence there would cost every injection."""
        with tempfile.TemporaryDirectory() as project:
            _, out, _, _ = self._hook(project, event=None, session_id=self.UUID,
                                      summary=("news", 5.0, 1))
        self.assertIn("news", out)

    def test_the_claude_hook_touches_no_part_of_the_codex_registry(self):
        """Claude's alias is settled by its socket, and `ANTIPHON_NAME` is
        shared by both sides of one terminal. A Claude hook that walked the
        process tree or wrote a session record would be describing a peer it is
        not."""
        payload = {"cwd": "/tmp/project", "hook_event_name": "UserPromptSubmit",
                   "session_id": self.UUID}
        with self._named("build"), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(payload))), \
             patch.object(antiphon.peers, "owner_key") as walk, \
             patch.object(antiphon.peers, "write_session") as write_session, \
             patch.object(antiphon, "build_summary", return_value=("", None, 0)), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.hook("claude"), 0)
        walk.assert_not_called()
        write_session.assert_not_called()

    def test_the_hook_still_injects_context_when_the_registry_is_broken(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon.peers, "write_session",
                          side_effect=OSError("disk gone")):
            _, out, err, _ = self._hook(project, event="UserPromptSubmit",
                                        session_id=self.UUID,
                                        summary=("news", 5.0, 1))
        self.assertIn("news", out)
        self.assertIn("disk gone", err)

    def test_a_hook_with_no_session_id_records_nothing_and_still_injects(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project)
            _, out, _, _ = self._hook(project, event="UserPromptSubmit",
                                      session_id=None, summary=("news", 5.0, 1))
            self.assertIsNone(antiphon.peers.read_peers(project, "codex")[0]["address"])
        self.assertIn("news", out)

    def test_a_second_owners_hook_cannot_repoint_a_live_alias(self):
        """The first session keeps working and the second one is told."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "codex", "build", None,
                                    pid=os.getppid(), owner_key="300:first")
            antiphon.peers.write_session(project, "codex", "build", self.UUID,
                                         "/t/first.jsonl", "300:first")
            _, _, err, _ = self._hook(project, event="UserPromptSubmit",
                                      session_id=self.OTHER, owner="301:second")
            peer = antiphon.peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], self.UUID, "the first session is untouched")
        self.assertIn("build", err)

    def test_the_hook_may_run_before_the_server_registers(self):
        with tempfile.TemporaryDirectory() as project:
            self._hook(project, event="SessionStart", session_id=self.UUID)
            self.assertEqual(antiphon.peers.read_peers(project), [],
                             "a session record alone is not a peer")
            self._register(project)
            peer = antiphon.peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], self.UUID)

    def test_the_server_may_register_before_the_hook_runs(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project)
            self.assertIsNone(antiphon.peers.read_peers(project, "codex")[0]["address"])
            self._hook(project, event="SessionStart", session_id=self.UUID)
            peer = antiphon.peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], self.UUID)

    # ---- setup installs the hook under both events ----

    @staticmethod
    def _codex_hooks(project):
        with open(os.path.join(project, ".codex", "hooks.json"),
                  encoding="utf-8") as f:
            return json.load(f)["hooks"]

    def test_setup_installs_the_codex_hook_under_both_events(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.setup(), 0)
            hooks = self._codex_hooks(project)
        for event in ("SessionStart", "UserPromptSubmit"):
            commands = [entry["command"] for group in hooks[event]
                        for entry in group["hooks"]]
            self.assertEqual(commands.count("antiphon hook codex"), 1, event)
        self.assertNotIn("SessionEnd", hooks,
                         "it can be delayed or missed; nothing may rely on it")

    def test_rerunning_setup_adds_no_second_copy_under_either_event(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            antiphon.setup()
            antiphon.setup()
            hooks = self._codex_hooks(project)
        for event in ("SessionStart", "UserPromptSubmit"):
            commands = [entry["command"] for group in hooks[event]
                        for entry in group["hooks"]]
            self.assertEqual(commands.count("antiphon hook codex"), 1, event)

    def test_setup_adds_session_start_to_a_config_that_predates_it(self):
        """An install from before this change has only the prompt hook. It gains
        the new event without gaining a second copy of the old one."""
        with tempfile.TemporaryDirectory() as project:
            os.makedirs(os.path.join(project, ".codex"))
            with open(os.path.join(project, ".codex", "hooks.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"hooks": {"UserPromptSubmit": [{"hooks": [
                    {"type": "command", "command": "antiphon hook codex"}]}]}}, f)
            with patch.object(antiphon, "project_dir", return_value=project), \
                 contextlib.redirect_stdout(io.StringIO()):
                antiphon.setup()
            hooks = self._codex_hooks(project)
        self.assertEqual(sum(len(g["hooks"]) for g in hooks["SessionStart"]), 1)
        self.assertEqual(
            [entry["command"] for group in hooks["UserPromptSubmit"]
             for entry in group["hooks"]].count("antiphon hook codex"), 1)


class RoutingTest(unittest.TestCase):
    """Which peer a message goes to, and what happens when that cannot be said.

    The bridge never chooses between peers and never broadcasts. A choice made
    here is invisible to everyone, which is the failure the registry exists to
    end; a message sent to three agents starts three agents on it. An agent
    picking a name is a different thing — that choice is written in its own
    words and can be read back and disagreed with.
    """

    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
    OTHER = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"

    @staticmethod
    def _codex_peer(project, alias, owner, session=None):
        antiphon.peers.register(project, "codex", alias, None,
                                pid=os.getpid(), owner_key=owner)
        if session:
            antiphon.peers.write_session(project, "codex", alias, session,
                                         f"/t/{alias}.jsonl", owner)

    # ---- an alias goes exactly where it says ----

    def test_a_named_peer_is_resolved_by_its_alias(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            self.assertEqual(antiphon.resolve_target(project, "claude", "api"),
                             ("/tmp/api.sock", ""))

    def test_an_alias_matches_exactly_or_not_at_all(self):
        """No prefix, no nearest, no case folding. A near miss is a different
        peer, and delivering to it is the guess this refuses to make."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            for alias in ("ap", "api2", "API", "apiv2"):
                address, detail = antiphon.resolve_target(project, "claude", alias)
                self.assertIsNone(address, alias)
                self.assertIn("api", detail)

    def test_an_unknown_alias_reports_the_live_ones(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            address, detail = antiphon.resolve_target(project, "claude", "docs")
        self.assertIsNone(address)
        self.assertIn("docs", detail)
        self.assertIn("ui", detail)

    def test_an_alias_that_could_never_be_a_name_says_so(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            for alias in ("API!", "", "../etc", "a" * 40):
                address, detail = antiphon.resolve_target(project, "claude", alias)
                self.assertIsNone(address, repr(alias))
                self.assertIn("usable peer name", detail)

    def test_a_kind_that_is_not_a_side_resolves_to_nothing(self):
        """`kind` reaches this from a marker and a tool argument. Falling through
        to a legacy path for a side that does not exist would deliver somewhere
        nobody asked for."""
        with tempfile.TemporaryDirectory() as project:
            for kind in ("ghost", "", None, "claude\n"):
                address, detail = antiphon.resolve_target(project, kind)
                self.assertIsNone(address, repr(kind))
                self.assertIn("kind", detail)

    def test_an_alias_or_kind_of_the_wrong_type_is_refused_not_raised(self):
        """Both arrive from JSON: a tool argument today, anything tomorrow. A
        traceback here would take the turn down over a bad field."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            for value in (42, [], {}, 3.5):
                address, detail = antiphon.resolve_target(project, "claude", value)
                self.assertIsNone(address, repr(value))
                self.assertIn("usable peer name", detail)
                address, detail = antiphon.resolve_target(project, value)
                self.assertIsNone(address, repr(value))
                self.assertIn("kind", detail)

    def test_an_alias_reaches_the_named_session_and_not_its_neighbour(self):
        """Two peers, both ready. The alias decides, and it decides exactly."""
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review", self.OTHER)
            self.assertEqual(antiphon.resolve_target(project, "codex", "review"),
                             (self.OTHER, ""))
            self.assertEqual(antiphon.resolve_target(project, "codex", "build"),
                             (self.UUID, ""))

    def test_a_named_push_queues_that_peers_session_and_no_other(self):
        """End to end: the marker names `review`, and `codex queue` is handed
        `review`\'s rollout id."""
        queued = []
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review", self.OTHER)
            payload = {"cwd": project, "transcript_path": "/tmp/transcript"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_claude_reply",
                              return_value="@codex:review ship it"), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor"), \
                 patch.object(antiphon, "_queue_codex",
                              side_effect=lambda session, message:
                                  queued.append((session, message)) or (True, "")), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(antiphon.push("codex"), 0)
        self.assertEqual([session for session, _ in queued], [self.OTHER])
        self.assertIn("ship it", queued[0][1])

    def test_an_unknown_alias_never_falls_back_to_the_legacy_path(self):
        """The legacy address is for the case where nothing is registered at
        all. Reaching for it because a *named* recipient was not found would
        deliver to a session nobody asked for, and report success."""
        # The transports raise rather than record. A bare mock socket answers
        # `recv` with a truthy object for ever, so a wrong fallback here hangs
        # the suite instead of failing it — measured, once.
        touched = AssertionError("a refused recipient must touch no transport")
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "codex_session_id",
                          return_value="sess-legacy") as rollout, \
             patch.object(antiphon.socket, "socket", side_effect=touched) as sock, \
             patch.object(antiphon.subprocess, "run", side_effect=touched) as run:
            for kind in ("claude", "codex"):
                address, detail = antiphon.resolve_target(project, kind, "ghost")
                self.assertIsNone(address, kind)
                self.assertIn("ghost", detail)
            self.assertFalse(antiphon.send_to_codex(project, "hi", "ghost")[0])
            self.assertFalse(antiphon.send_to_claude(project, "hi", "ghost")[0])
            rollout.assert_not_called()
            sock.assert_not_called()
            run.assert_not_called()

    def test_a_named_codex_peer_resolves_to_its_merged_session(self):
        """The alias returns the id the hook recorded, not whatever rollout
        happens to be newest on disk."""
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            with patch.object(antiphon, "codex_session_id",
                              return_value="sess-newest") as rollout:
                self.assertEqual(antiphon.resolve_target(project, "codex",
                                                         "build"),
                                 (self.UUID, ""))
            rollout.assert_not_called()

    def test_a_named_peer_that_is_not_routable_yet_says_so(self):
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build")
            address, detail = antiphon.resolve_target(project, "codex", "build")
        self.assertIsNone(address)
        self.assertIn("not yet routable", detail)
        self.assertIn("build", detail)

    # ---- a bare message is decided by how many are live, not how many are ready ----

    def test_one_ready_peer_does_not_win_over_another_live_peer(self):
        """Readiness is not permission to guess. `review` may simply be between
        SessionStart and its first hook; a bare message must not go to `build`
        because it happened to become routable first."""
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review")
            address, detail = antiphon.resolve_target(project, "codex")
        self.assertIsNone(address)
        self.assertIn("build", detail)
        self.assertIn("review", detail)
        self.assertIn("ready", detail)
        self.assertIn("waiting", detail)
        self.assertNotIn("broadcast", detail.lower())

    def test_several_peers_and_no_alias_is_refused_with_their_names(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            address, detail = antiphon.resolve_target(project, "claude")
        self.assertIsNone(address)
        self.assertIn("ui", detail)
        self.assertIn("api", detail)
        self.assertIn("address one by name", detail)

    def test_a_registered_peer_never_falls_back_to_the_old_path(self):
        """The legacy address belongs to the case where nothing is registered.
        Reaching for it because the registered peer is not ready, or because
        there is only one of it, would deliver to a session nobody named and
        look like success."""
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build")
            with patch.object(antiphon, "codex_session_id",
                              return_value="9999") as legacy:
                address, detail = antiphon.resolve_target(project, "codex")
                self.assertIsNone(antiphon.resolve_target(project, "codex",
                                                          "build")[0])
            legacy.assert_not_called()
        self.assertIsNone(address)
        self.assertIn("waiting", detail)

    def test_one_peer_and_no_alias_is_delivered_to(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            self.assertEqual(antiphon.resolve_target(project, "claude"),
                             ("/tmp/ui.sock", ""))

    # ---- one registered Codex peer is not proof of one Codex session ----

    def test_a_single_registered_codex_peer_still_refuses_a_bare_message(self):
        """A Codex session registers only when it was given a name, so one
        record does not mean one session: any number of unnamed ones can be
        running beside it, invisible here. Delivering to the one that happens
        to be visible is a guess dressed as a certainty."""
        touched = AssertionError("a refused send must touch no transport")
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            with patch.object(antiphon.subprocess, "run", side_effect=touched), \
                 patch.object(antiphon, "codex_session_id",
                              return_value="sess-legacy") as legacy:
                address, detail = antiphon.resolve_target(project, "codex")
                self.assertFalse(antiphon.send_to_codex(project, "hi")[0])
            legacy.assert_not_called()
        self.assertIsNone(address)
        self.assertIn("build", detail)
        self.assertIn("ready", detail)
        self.assertIn("not discoverable", detail)
        self.assertIn("by name", detail)

    def test_the_refusal_says_what_state_that_one_peer_is_in(self):
        """Named and waiting is a different situation from named and ready, and
        the reader has to act differently in each."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "codex", "build", None,
                                    pid=os.getpid(), owner_key="300:build")
            _, detail = antiphon.resolve_target(project, "codex")
        self.assertIn("build", detail)
        self.assertIn("waiting", detail)

    def test_an_explicit_alias_still_reaches_a_single_codex_peer(self):
        """Nothing is taken away: naming the peer resolves it as it always did.
        Only the guess is refused."""
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self.assertEqual(antiphon.resolve_target(project, "codex", "build"),
                             (self.UUID, ""))

    def test_a_single_claude_peer_is_still_delivered_to_without_a_name(self):
        """The asymmetry stops at the Codex side. A Claude channel server always
        registers — named or not — so one live record there really is one live
        peer, and refusing would break every single-session project."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            self.assertEqual(antiphon.resolve_target(project, "claude"),
                             ("/tmp/ui.sock", ""))

    def test_the_reply_tool_refuses_a_bare_message_to_a_named_peer(self):
        touched = AssertionError("a refused reply must start no process")
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            with patch.object(antiphon, "project_dir", return_value=project), \
                 patch.object(antiphon.subprocess, "run", side_effect=touched), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps({"text": "hi"}))), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(antiphon.reply(), 1)
        self.assertIn("not discoverable", err.getvalue())

    def test_a_bare_stop_marker_is_refused_when_a_named_peer_is_live(self):
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            payload = {"cwd": project, "transcript_path": "/tmp/transcript"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_claude_reply",
                              return_value="@codex ship it"), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor") as write, \
                 patch.object(antiphon, "_queue_codex") as queued, \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(antiphon.push("codex"), 0)
            queued.assert_not_called()
            write.assert_not_called()
        self.assertIn("not discoverable", err.getvalue())

    # ---- nothing registered: the unnamed pair, exactly as before ----

    def test_nothing_registered_falls_back_to_the_project_socket(self):
        """An older channel server still serving the project-wide path is still
        a working peer; upgrading must not cut it off."""
        with tempfile.TemporaryDirectory() as project:
            self.assertEqual(antiphon.resolve_target(project, "claude"),
                             (antiphon.claude_socket_path(project), ""))

    def test_nothing_registered_falls_back_to_the_newest_codex_rollout(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "codex_session_id", return_value="sess-1"):
            self.assertEqual(antiphon.resolve_target(project, "codex"),
                             ("sess-1", ""))

    def test_no_codex_session_and_nothing_registered_is_refused(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "codex_session_id", return_value=None):
            address, detail = antiphon.resolve_target(project, "codex")
        self.assertIsNone(address)
        self.assertIn("no Codex session", detail)

    def test_a_codex_peer_does_not_count_as_a_claude_recipient(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            self._codex_peer(project, "build", "300:build", self.UUID)
            self.assertEqual(antiphon.resolve_target(project, "claude"),
                             ("/tmp/ui.sock", ""))

    # ---- a refusal costs nothing ----

    def test_a_refused_send_opens_no_socket_and_starts_no_process(self):
        """The refusal has to happen before any transport is touched. A socket
        opened or a `codex queue` started for a message that is never sent is
        a side effect nobody asked for, and on the Codex side it would be a
        process spawned per refused turn."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review", self.OTHER)
            touched = AssertionError("a refused message must touch no transport")
            with patch.object(antiphon.socket, "socket", side_effect=touched) as sock, \
                 patch.object(antiphon.subprocess, "run", side_effect=touched) as run:
                claude_ok, claude_detail = antiphon.send_to_claude(project, "hi")
                codex_ok, codex_detail = antiphon.send_to_codex(project, "hi")
                sock.assert_not_called()
                run.assert_not_called()
        self.assertFalse(claude_ok)
        self.assertFalse(codex_ok)
        self.assertIn("address one by name", claude_detail)
        self.assertIn("address one by name", codex_detail)

    def test_the_low_level_queue_is_still_reachable_for_one_session(self):
        """`_queue_codex` is the transport; `send_to_codex` is the decision. The
        split is what lets the decision be tested without a subprocess."""
        with patch.object(antiphon.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout="",
                                                       stderr="")) as run:
            self.assertEqual(antiphon._queue_codex("sess-1", "hello"), (True, ""))
        self.assertEqual(run.call_args.args[0][:4],
                         ["codex", "queue", "--thread", "sess-1"])

    # ---- the Stop hook routes a named line ----

    def test_a_named_push_carries_the_alias_down_to_the_send(self):
        routed = []
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            payload = {"cwd": project, "transcript_path": "/tmp/rollout"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_codex_reply",
                              return_value="@claude:api run the tests"), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor"), \
                 patch.object(antiphon, "send_to_claude",
                              side_effect=lambda cwd, text, alias=None, **_:
                                  routed.append((alias, text)) or (True, "")), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(antiphon.push("claude"), 0)
        self.assertEqual(routed, [("api", "run the tests")])

    def test_an_alias_reaches_that_peers_own_socket_and_no_other(self):
        """The whole point of the alias, checked at the transport: two live
        peers, and the connection goes to the named one's address."""
        connected = []

        class Fake:
            def settimeout(self, _): pass
            def connect(self, address): connected.append(address)
            def sendall(self, _): pass
            def shutdown(self, _): pass
            def recv(self, _): return b'{"ok": true}' if not connected[1:] else b""
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *_): return False

        replies = [b'{"ok": true}', b""]

        class Reading(Fake):
            def recv(self, _):
                return replies.pop(0) if replies else b""

        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            with patch.object(antiphon.socket, "socket", lambda *a, **k: Reading()):
                ok, detail = antiphon.send_to_claude(project, "hi", "api")
        self.assertTrue(ok, detail)
        self.assertEqual(connected, ["/tmp/api.sock"])

    def test_only_the_recipient_that_was_delivered_advances_its_fingerprint(self):
        """One line lands and one is refused. Recording the refused one would
        lose it for ever — it would never be retried and never be seen."""
        written = []
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            payload = {"cwd": project, "transcript_path": "/tmp/rollout"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_codex_reply",
                              return_value="@claude:ui landed\n@claude:gone lost"), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor",
                              side_effect=lambda cwd, data, kind:
                                  written.append(dict(data))), \
                 patch.object(antiphon, "send_to_claude",
                              side_effect=lambda cwd, text, alias=None, **_:
                                  (True, "") if alias == "ui" else (False, "no")), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(antiphon.push("claude"), 0)
        self.assertEqual(list(written[0]["last_pushed_claude"]), ["@ui"])

    # ---- a bare tool call reports the ambiguity instead of picking ----

    def test_a_bare_reply_returns_the_ambiguity_honestly(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review", self.OTHER)
            with patch.object(antiphon, "project_dir", return_value=project), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO('{"text":"hello"}')), \
                 contextlib.redirect_stderr(err):
                self.assertEqual(antiphon.reply(), 1)
        self.assertIn("build", err.getvalue())
        self.assertIn("review", err.getvalue())

    def test_the_send_tool_returns_the_ambiguity_honestly(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            result = antiphon._send_tool(project, "hello")
        self.assertTrue(result.get("isError"))
        self.assertIn("api", result["content"][0]["text"])


class ToolRecipientTest(unittest.TestCase):
    """`to` on both channel tools, and the dedupe that has to follow it.

    A tool delivery and the `@alias` line that ends the same turn are the same
    message twice. The Stop hook already remembers per recipient; a tool that
    remembered only "the last thing sent" would either resend its own message or
    erase somebody else's record on the way past.
    """

    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
    OTHER = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"

    @staticmethod
    def _codex_peer(project, alias, owner, session):
        antiphon.peers.register(project, "codex", alias, None,
                                pid=os.getpid(), owner_key=owner)
        antiphon.peers.write_session(project, "codex", alias, session,
                                     f"/t/{alias}.jsonl", owner)

    @staticmethod
    def _reply(project, payload):
        err = io.StringIO()
        with patch.object(antiphon, "project_dir", return_value=project), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stderr(err):
            code = antiphon.reply()
        return code, err.getvalue()

    # ---- the schemas say what `to` is for ----

    def test_the_send_tool_takes_an_optional_recipient(self):
        schema = next(t for t in antiphon.TOOLS
                      if t["name"] == "antiphon_send")["inputSchema"]
        self.assertEqual(schema["properties"]["to"]["type"], "string")
        self.assertEqual(schema["required"], ["text"],
                         "one live peer needs no alias; requiring one would "
                         "break every single-peer project")
        self.assertEqual(schema["properties"]["to"]["description"],
                         antiphon.TO_DESCRIPTION)

    def test_the_tool_descriptions_do_not_promise_a_single_peer(self):
        """`reply_to_codex` used to say it answered "the Codex agent that
        contacted this channel". Nothing correlates an incoming message with a
        reply target, so that sentence described a routing rule that does not
        exist — and an agent reading it would never think to pass `to`."""
        send = next(t for t in antiphon.TOOLS
                    if t["name"] == "antiphon_send")["description"]
        # The Node string is written across lines with `+`. Collapse it first,
        # and fail loudly if the description cannot be found at all — a regex
        # that quietly matches nothing would let any wording through.
        node = re.sub(r'"\s*\+\s*\n\s*"', "", read_source("lib", "channel.mjs"))
        found = re.search(r'description:\s*\n?\s*"(Send[^"]*)"', node)
        self.assertIsNotNone(found, "reply_to_codex description not found")
        reply = found.group(1)
        for text in (send, reply):
            self.assertIn("peer", text)
            self.assertIn("to", text)
            self.assertNotIn("that contacted this channel", text)
        self.assertNotIn("the Claude Code session working in this project", send)
        # The Codex side has no shortcut left to promise: one registered peer
        # cannot be shown to be the only session, so a bare reply is refused.
        self.assertNotIn("you can leave it out", reply)
        self.assertIn("no Codex peer is registered", reply)

    # ---- sending to a named peer ----

    def test_the_send_tool_delivers_to_the_peer_it_names(self):
        connected = []

        class Fake:
            def settimeout(self, _): pass
            def connect(self, address): connected.append(address)
            def sendall(self, _): pass
            def shutdown(self, _): pass
            def recv(self, _): return b'{"ok": true}' if len(connected) == 1 \
                                     and not getattr(self, "done", False) else b""
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *_): return False

        replies = [b'{"ok": true}', b""]

        class Reading(Fake):
            def recv(self, _):
                return replies.pop(0) if replies else b""

        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            with patch.object(antiphon.socket, "socket", lambda *a, **k: Reading()), \
                 patch.object(antiphon, "_record_delivery"):
                result = antiphon._send_tool(project, "hello", "api")
        self.assertNotIn("isError", result)
        self.assertEqual(connected, ["/tmp/api.sock"])

    def test_the_send_tool_reports_an_unroutable_recipient_as_an_error(self):
        """Invalid, unknown, waiting and ambiguous all reach the caller as tool
        errors. A silent success would be the worst outcome: Codex would believe
        Claude had been told."""
        touched = AssertionError("a refused send must touch no transport")
        cases = [("ghost", "no live claude peer"), ("API!", "usable peer name"),
                 (None, "address one by name")]
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            with patch.object(antiphon.socket, "socket", side_effect=touched):
                for alias, expected in cases:
                    result = antiphon._send_tool(project, "hello", alias)
                    self.assertTrue(result.get("isError"), repr(alias))
                    self.assertIn(expected, result["content"][0]["text"])

    def test_a_recipient_that_is_not_a_string_is_refused_before_anything_else(self):
        """`to` arrives from JSON, so it can be any type at all."""
        touched = AssertionError("a malformed argument must touch no transport")
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            with patch.object(antiphon.socket, "socket", side_effect=touched):
                for value in (42, [], {}, 3.5, True):
                    result = antiphon._send_tool(project, "hello", value)
                    self.assertTrue(result.get("isError"), repr(value))
                    self.assertIn("to must be a string",
                                  result["content"][0]["text"], repr(value))

    def test_the_reply_tool_delivers_to_the_codex_peer_it_names(self):
        queued = []
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review", self.OTHER)
            with patch.object(antiphon, "_queue_codex",
                              side_effect=lambda session, message:
                                  queued.append(session) or (True, "")):
                code, err = self._reply(project, {"text": "ship", "to": "review"})
        self.assertEqual(code, 0, err)
        self.assertEqual(queued, [self.OTHER])

    def test_the_reply_tool_refuses_what_it_cannot_route(self):
        touched = AssertionError("a refused reply must start no process")
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review", self.OTHER)
            with patch.object(antiphon.subprocess, "run", side_effect=touched):
                for payload, expected in (({"text": "x", "to": "ghost"}, "ghost"),
                                          ({"text": "x"}, "address one by name"),
                                          ({"text": "x", "to": 42}, "to")):
                    code, err = self._reply(project, payload)
                    self.assertEqual(code, 1, repr(payload))
                    self.assertIn(expected, err)

    def test_a_named_reply_to_a_waiting_peer_starts_no_process(self):
        """`review` is live and between its start and its first turn. Saying so
        is the answer; queueing to whoever is ready is not."""
        touched = AssertionError("a waiting peer must start no process")
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "codex", "review", None,
                                    pid=os.getpid(), owner_key="301:review")
            with patch.object(antiphon.subprocess, "run", side_effect=touched):
                code, err = self._reply(project, {"text": "x", "to": "review"})
        self.assertEqual(code, 1)
        self.assertIn("not yet routable", err)
        self.assertIn("review", err)

    def test_a_successful_send_names_the_peer_it_reached(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")):
                named = antiphon._send_tool(project, "hello", "api")
                bare = antiphon._send_tool(project, "hello again")
        self.assertIn("api", named["content"][0]["text"])
        self.assertIn("channel", bare["content"][0]["text"])

    # ---- the dedupe follows the recipient ----

    def _pushed(self, project, target="claude"):
        return antiphon.read_cursor(project,
                                    antiphon.sender_side(target)).get(
                                        f"last_pushed_{target}") or {}

    def test_a_named_tool_delivery_is_not_repeated_by_the_stop_hook(self):
        """The same text, sent mid-turn to `api` and then ending the turn as an
        `@claude:api` line, is one message."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")):
                antiphon._send_tool(project, "run the tests", "api")
            payload = {"cwd": project, "transcript_path": "/tmp/rollout"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_codex_reply",
                              return_value="@claude:api run the tests"), \
                 patch.object(antiphon, "send_to_claude") as send, \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(antiphon.push("claude"), 0)
                send.assert_not_called()

    def test_a_named_reply_is_not_repeated_by_the_stop_hook(self):
        """The Codex direction of the same rule, and the proof that the alias
        really reaches `_record_delivery`: a record written under the wrong key
        would let this through."""
        queued = []
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            self._codex_peer(project, "review", "301:review", self.OTHER)
            with patch.object(antiphon, "_queue_codex",
                              side_effect=lambda session, message:
                                  queued.append(session) or (True, "")):
                code, err = self._reply(project, {"text": "ship", "to": "review"})
                self.assertEqual(code, 0, err)

                payload = {"cwd": project, "transcript_path": "/tmp/transcript"}
                with patch.object(antiphon.os.path, "exists", return_value=True), \
                     patch.object(antiphon, "last_claude_reply",
                                  return_value="@codex:review ship"), \
                     patch.object(antiphon.sys, "stdin",
                                  io.StringIO(json.dumps(payload))), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(antiphon.push("codex"), 0)
            record = self._pushed(project, "codex")
        self.assertEqual(queued, [self.OTHER], "the Stop hook must not send it again")
        self.assertEqual(sorted(record), ["@review"],
                         "and no other recipient's slot was touched")

    def test_a_named_tool_delivery_does_not_erase_another_recipients_record(self):
        """The old record was a single string, so a delivery to `api` replaced
        what `ui` had already been sent — and `ui` got it a second time."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")):
                antiphon._send_tool(project, "for ui", "ui")
                antiphon._send_tool(project, "for api", "api")
            record = self._pushed(project)
        self.assertEqual(sorted(record), ["@api", "@ui"])
        self.assertEqual(record["@ui"], antiphon.batch_fingerprint(["for ui"]))

    def test_an_unaddressed_tool_delivery_keeps_its_own_slot(self):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")):
                antiphon._send_tool(project, "bare", None)
                antiphon._send_tool(project, "named", "ui")
            record = self._pushed(project)
        self.assertEqual(sorted(record), ["", "@ui"])
        self.assertEqual(record[""], antiphon.batch_fingerprint(["bare"]))

    def test_a_named_delivery_leaves_the_legacy_record_alone(self):
        """The legacy value is the last *unaddressed* delivery. A turn that only
        sends named lines has not superseded it, and dropping it there would
        resend that message the next time somebody writes a bare line."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            antiphon.write_cursor(project, {"last_pushed_claude": "old text"},
                                  "codex")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")):
                antiphon._send_tool(project, "first", "api")
            payload = {"cwd": project, "transcript_path": "/tmp/rollout"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_codex_reply",
                              return_value="@claude:api second"), \
                 patch.object(antiphon, "send_to_claude",
                              return_value=(True, "")), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(antiphon.push("claude"), 0)
            record = self._pushed(project)
        self.assertIn(antiphon.LEGACY_SLOT, record,
                      "a named turn supersedes nothing unaddressed")
        _, already = antiphon.migrate_pushed(record, ["old text"])
        self.assertTrue(already, "so the old delivery is still recognised")

    def test_an_unaddressed_delivery_supersedes_the_legacy_record(self):
        """Once the unaddressed slot holds a digest, the old value describes the
        same thing worse. Keeping both would leave a record nothing clears."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            antiphon.write_cursor(project, {"last_pushed_claude": "old text"},
                                  "codex")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")):
                antiphon._send_tool(project, "new bare message")
            record = self._pushed(project)
        self.assertEqual(sorted(record), [""])
        self.assertEqual(record[""],
                         antiphon.batch_fingerprint(["new bare message"]))

    # ---- what arrives from outside is not trusted to be shaped ----

    def test_tool_arguments_that_are_not_an_object_do_not_crash_the_server(self):
        """`params` and `arguments` come off the wire. `.get` on a list is an
        AttributeError, and it would end the MCP server mid-session."""
        out = io.StringIO()
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "antiphon_send", "arguments": [1, 2]}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": [1, 2]},
                    [1, 2], "a string", 42, None,
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
        with tempfile.TemporaryDirectory() as project, \
             patch.dict(os.environ, {}), \
             patch.object(antiphon, "project_dir", return_value=project), \
             patch.object(antiphon.sys, "stdin", stdin), \
             contextlib.redirect_stdout(out):
            os.environ.pop("ANTIPHON_NAME", None)
            antiphon.mcp()
        replies = [json.loads(line) for line in out.getvalue().splitlines()
                   if line.strip()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3],
                         "the session survives both and keeps serving")
        self.assertTrue(replies[0]["result"].get("isError"))

    def test_a_named_turn_does_not_lose_a_cursor_from_before_this_format(self):
        """No tool call anywhere: an upgraded install whose first new event is a
        named Stop line. The old value is the only record of the last bare
        message, and writing the named one over it would resend that message."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            antiphon.write_cursor(project, {"last_pushed_claude": "old text"},
                                  "codex")
            payload = {"cwd": project, "transcript_path": "/tmp/rollout"}

            def stop(reply, send):
                with patch.object(antiphon.os.path, "exists", return_value=True), \
                     patch.object(antiphon, "last_codex_reply", return_value=reply), \
                     patch.object(antiphon, "send_to_claude", send), \
                     patch.object(antiphon.sys, "stdin",
                                  io.StringIO(json.dumps(payload))), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(antiphon.push("claude"), 0)

            stop("@claude:api named only", lambda *a, **k: (True, ""))
            must_not_send = Mock(side_effect=AssertionError(
                "the old bare message must not be delivered a second time"))
            stop("@claude old text", must_not_send)

    def test_stdin_that_is_valid_json_but_not_an_object_does_not_raise(self):
        """A hook or tool handed `[]` or `"x"` must fail as a bad request, not
        as a traceback out of somebody's Stop hook."""
        for body in ("[]", '"just a string"', "42", "null"):
            with tempfile.TemporaryDirectory() as project, \
                 patch.object(antiphon, "project_dir", return_value=project), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor"), \
                 patch.object(antiphon, "build_summary",
                              return_value=("", None, 0)), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                with patch.object(antiphon.sys, "stdin", io.StringIO(body)):
                    self.assertEqual(antiphon.reply(), 1, body)
                with patch.object(antiphon.sys, "stdin", io.StringIO(body)):
                    self.assertEqual(antiphon.hook("claude"), 0, body)
                with patch.object(antiphon.sys, "stdin", io.StringIO(body)):
                    self.assertEqual(antiphon.push("claude"), 0, body)

    def test_a_cursor_from_before_this_format_is_not_thrown_away(self):
        """The old cursor held the joined text, and it is still what the Stop
        hook compares against. Dropping it on the first tool call would resend
        the last unaddressed message once — a duplicate nobody asked for."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "api", "/tmp/api.sock")
            antiphon.write_cursor(project, {"last_pushed_claude": "old text"},
                                  "codex")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")):
                antiphon._send_tool(project, "new", "api")
            record = self._pushed(project)
            self.assertIn("@api", record)
            kept, already = antiphon.migrate_pushed(record, ["old text"])
            self.assertTrue(already, "the old delivery is still recognised")
            self.assertNotIn(antiphon.LEGACY_SLOT, kept,
                             "and it does not linger in the record it produces")


class SenderIdentityTest(unittest.TestCase):
    """Every inbound message says who sent it.

    With several peers live, a reply has to be addressed deliberately, so the
    receiver needs to know who spoke. There are four delivery paths and all four
    carry it — labelling only the tool path would leave a Stop-marker message
    arriving anonymous, which is exactly the case where the receiver has least
    context to work it out.

    The id names one delivery attempt and nothing more. It is deliberately not a
    correlation id: keeping one logical id across a retry needs pending-delivery
    state this release does not have.
    """

    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
    OWNER = "300:mine"

    @staticmethod
    @contextlib.contextmanager
    def _named(name):
        with patch.dict(os.environ, {}):
            os.environ.pop("ANTIPHON_NAME", None)
            if name:
                os.environ["ANTIPHON_NAME"] = name
            yield

    @contextlib.contextmanager
    def _holding(self, project, kind, name):
        """The sender's own claim. An alias is published only by the session
        the registry says holds it, so a label test has to establish that the
        same way the real paths do."""
        if name and antiphon.peers.valid_name(name):
            if kind == "codex":
                self._codex_peer(project, name, self.OWNER, self.UUID)
            else:
                antiphon.peers.register(project, "claude", name,
                                        f"/tmp/{name}.sock", pid=os.getpid(),
                                        owner_key=self.OWNER)
        with self._named(name), \
             patch.object(antiphon.peers, "owner_key", return_value=self.OWNER):
            yield

    @staticmethod
    def _codex_peer(project, alias, owner, session):
        antiphon.peers.register(project, "codex", alias, None,
                                pid=os.getpid(), owner_key=owner)
        antiphon.peers.write_session(project, "codex", alias, session,
                                     f"/t/{alias}.jsonl", owner)

    def _tool_send(self, name, to="ui"):
        """The MCP server passes the alias it won at start-up; the tool
        publishes that and never consults the environment."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            with patch.object(antiphon, "send_to_claude",
                              return_value=(True, "")) as send, \
                 patch.object(antiphon, "_record_delivery"):
                antiphon._send_tool(project, "run the tests", to, name)
        return send.call_args

    def _stop_to_claude(self, name, line="@claude:ui run the tests"):
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            payload = {"cwd": project, "transcript_path": "/tmp/rollout"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_codex_reply", return_value=line), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor"), \
                 patch.object(antiphon, "send_to_claude",
                              return_value=(True, "")) as send, \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()), \
                 self._holding(project, "codex", name):
                self.assertEqual(antiphon.push("claude"), 0)
        return send.call_args

    def _reply_text(self, name, payload=None):
        queued = []
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            # The channel server hands the reply the alias it holds; there is
            # no environment fallback for it to fall back to. `to` is named
            # because a bare reply is refused whenever a named Codex peer is
            # live — which is exactly the situation this sets up.
            body = {"text": "ship it", "sender_alias": name, "to": "build"}
            body.update(payload or {})
            with patch.object(antiphon, "project_dir", return_value=project), \
                 patch.object(antiphon, "_queue_codex",
                              side_effect=lambda session, message:
                                  queued.append(message) or (True, "")), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(body))), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(antiphon.reply(), 0)
        return queued[0]

    def _stop_to_codex(self, name):
        queued = []
        with tempfile.TemporaryDirectory() as project:
            self._codex_peer(project, "build", "300:build", self.UUID)
            payload = {"cwd": project, "transcript_path": "/tmp/transcript"}
            with patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_claude_reply",
                              return_value="@codex:build ship it"), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor"), \
                 patch.object(antiphon, "_queue_codex",
                              side_effect=lambda session, message:
                                  queued.append(message) or (True, "")), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()), \
                 self._holding(project, "claude", name):
                self.assertEqual(antiphon.push("codex"), 0)
        return queued[0]

    # ---- the two socket paths carry metadata ----

    def test_the_send_tool_names_its_sender_in_the_socket_payload(self):
        call = self._tool_send("build")
        self.assertEqual(call.kwargs["sender_alias"], "build")
        self.assertTrue(antiphon.peers.valid_session_id(call.kwargs["message_id"]))

    def test_a_stop_marker_to_claude_is_not_delivered_anonymously(self):
        """The path the first draft left out, and the one where the receiver has
        least other context about who is speaking."""
        call = self._stop_to_claude("build")
        self.assertEqual(call.kwargs["sender_alias"], "build")
        self.assertTrue(antiphon.peers.valid_session_id(call.kwargs["message_id"]))

    # ---- the two queue paths carry it in the text, after the prefix ----

    def test_the_reply_tool_labels_the_message_after_the_existing_prefix(self):
        text = self._reply_text("ui")
        self.assertTrue(text.startswith(antiphon.CHANNEL_LABEL),
                        "the prefix anchors the echo guard and must stay first")
        self.assertRegex(text, r"^\[Antiphon channel\] Claude: "
                               r"\[from=ui id=[0-9a-f-]{36}\] ship it$")

    def test_a_stop_push_to_codex_carries_the_same_visible_label(self):
        text = self._stop_to_codex("ui")
        self.assertTrue(text.startswith(antiphon.PUSH_LABEL))
        self.assertRegex(text, r"^\[Antiphon bridge\] Claude: "
                               r"\[from=ui id=[0-9a-f-]{36}\] ship it$")

    # ---- a session with no usable alias does not invent one ----

    def test_an_unnamed_sender_says_so_rather_than_inventing_a_name(self):
        self.assertIsNone(self._tool_send(None).kwargs["sender_alias"])
        self.assertIsNone(self._stop_to_claude(None).kwargs["sender_alias"])
        self.assertIn("[from=<unnamed> id=", self._reply_text(None))
        self.assertIn("[from=<unnamed> id=", self._stop_to_codex(None))

    def test_a_name_that_is_not_usable_is_not_a_sender_alias(self):
        """`ANTIPHON_NAME` can be anything a shell allows. Only what the registry
        would accept as a name can be one here — anything else is a name the
        other side could not address a reply to."""
        for name in ("Build!", "a" * 40, "../etc"):
            self.assertIsNone(self._tool_send(name).kwargs["sender_alias"], name)
            self.assertIn("[from=<unnamed> id=", self._reply_text(name), name)

    def test_an_alias_cannot_smuggle_a_label_into_the_visible_text(self):
        """The queue label is plain text in the message. An alias validated only
        after being formatted could close the bracket and open another."""
        for name in ("a id=x] [from=root", "a b", "a]b"):
            text = self._reply_text(name)
            self.assertIn("[from=<unnamed> id=", text, name)
            self.assertEqual(text.count("[from="), 1, name)

    def test_a_peer_actually_called_unnamed_is_told_apart_from_having_no_name(self):
        """`unnamed` is a name the registry would accept, so the sentinel cannot
        be that word: one of these two peers can be replied to by name and the
        other cannot, and the reader has only this label to tell them apart."""
        real = self._reply_text("unnamed")
        absent = self._reply_text(None)
        self.assertIn("[from=unnamed id=", real)
        self.assertIn("[from=<unnamed> id=", absent)
        self.assertNotIn("[from=<unnamed> id=", real,
                         "a peer that really is called `unnamed` must not read "
                         "as one that has no name")

    # ---- what the label must not break ----

    def test_every_labelled_message_is_still_seen_as_the_bridge_s_own(self):
        """The echo guard matches the prefix at the start. If the label went in
        front of it, every message this bridge delivered would come back as new
        traffic and be delivered again."""
        for text in (self._reply_text("ui"), self._stop_to_codex("ui"),
                     self._reply_text(None), self._stop_to_codex(None)):
            self.assertTrue(antiphon._is_self_injected(text), text)

    def test_the_dedupe_still_works_on_the_message_a_human_wrote(self):
        """The id changes on every attempt. Fingerprinting the labelled text
        would make every message look new and resend all of them."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock")
            with patch.object(antiphon, "send_to_claude", return_value=(True, "")), \
                 self._named("build"):
                antiphon._send_tool(project, "run the tests", "ui")
                # Read inside: a named session keeps its cursor in its own peer
                # directory, so reading it from outside finds the unnamed one.
                record = antiphon.read_cursor(project,
                                              "codex")["last_pushed_claude"]
        self.assertEqual(record["@ui"],
                         antiphon.batch_fingerprint(["run the tests"]))

    def test_each_delivery_attempt_carries_its_own_id(self):
        first, second = self._reply_text("ui"), self._reply_text("ui")
        self.assertNotEqual(first, second, "an id names one attempt, not a message")

    # ---- the channel server says who it is ----

    def test_the_reply_takes_the_alias_the_channel_passed_it(self):
        """`channel.mjs` knows which Claude peer it is; the subprocess is told
        rather than left to work it out."""
        self.assertIn("[from=api id=",
                      self._reply_text("ui", {"sender_alias": "api"}))

    def test_an_explicit_null_from_the_channel_is_not_second_guessed(self):
        """A channel server that lost its claim sends null on purpose. Reading
        the environment instead would put its name straight back on."""
        with self._named("ui"):
            self.assertIn("[from=<unnamed> id=",
                          self._reply_text("ui", {"sender_alias": None}))

    def test_an_unusable_alias_from_the_channel_is_not_taken_on_trust(self):
        self.assertIn("[from=<unnamed> id=",
                      self._reply_text(None, {"sender_alias": "Not A Name"}))
        self.assertIn("[from=<unnamed> id=",
                      self._reply_text(None, {"sender_alias": 42}))


class ClaimedAliasTest(unittest.TestCase):
    """An alias may be published only by the session that actually holds it.

    A valid `ANTIPHON_NAME` is a request, not a claim. Two sessions can be
    started with the same one and exactly one wins the registry. The loser
    publishing it anyway would attribute its words to the winner, and a reply
    addressed back would reach a session that never spoke — the silent
    misidentification this whole registry exists to end, arriving through the
    label that was meant to prevent it.
    """

    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
    MINE, THEIRS = "300:mine", "301:theirs"

    @staticmethod
    @contextlib.contextmanager
    def _named(name):
        with patch.dict(os.environ, {}):
            os.environ.pop("ANTIPHON_NAME", None)
            if name:
                os.environ["ANTIPHON_NAME"] = name
            yield

    def _claude_endpoint(self, project, owner):
        directory = antiphon.peers.peer_dir(project, "claude", "ui")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "endpoint.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"kind": "claude", "name": "ui", "pid": os.getpid(),
                       "address": "/tmp/ui.sock", "owner": owner,
                       "started_at": 1.0}, f)

    def _codex_endpoint(self, project, owner):
        antiphon.peers.register(project, "codex", "ui", None,
                                pid=os.getpid(), owner_key=owner)
        antiphon.peers.write_session(project, "codex", "ui", self.UUID,
                                     "/t/ui.jsonl", owner)

    def _stop_to_claude(self, project):
        """Codex's Stop hook: the sender is the Codex peer."""
        payload = {"cwd": project, "transcript_path": "/tmp/rollout"}
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_codex_reply",
                          return_value="@claude:ui run it"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon, "send_to_claude",
                          return_value=(True, "")) as send, \
             patch.object(antiphon.sys, "stdin",
                          io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(antiphon.push("claude"), 0)
        return send.call_args.kwargs["sender_alias"]

    def _stop_to_codex(self, project):
        """Claude's Stop hook: the sender is the Claude peer."""
        queued = []
        payload = {"cwd": project, "transcript_path": "/tmp/transcript"}
        with patch.object(antiphon.os.path, "exists", return_value=True), \
             patch.object(antiphon, "last_claude_reply",
                          return_value="@codex:ui run it"), \
             patch.object(antiphon, "read_cursor", return_value={}), \
             patch.object(antiphon, "write_cursor"), \
             patch.object(antiphon, "_queue_codex",
                          side_effect=lambda session, message:
                              queued.append(message) or (True, "")), \
             patch.object(antiphon.sys, "stdin",
                          io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(antiphon.push("codex"), 0)
        return queued[0]

    def test_the_session_that_holds_the_alias_publishes_it(self):
        with tempfile.TemporaryDirectory() as project:
            self._codex_endpoint(project, self.MINE)
            self._claude_endpoint(project, self.MINE)
            with self._named("ui"), \
                 patch.object(antiphon.peers, "owner_key", return_value=self.MINE):
                self.assertEqual(self._stop_to_claude(project), "ui")
                self.assertIn("[from=ui id=", self._stop_to_codex(project))

    def test_a_session_that_lost_the_alias_does_not_publish_it(self):
        """Both directions. The words are still delivered — only the claim to
        be `ui` is withheld, because it is not this session's to make."""
        with tempfile.TemporaryDirectory() as project:
            self._codex_endpoint(project, self.THEIRS)
            self._claude_endpoint(project, self.THEIRS)
            with self._named("ui"), \
                 patch.object(antiphon.peers, "owner_key", return_value=self.MINE):
                self.assertIsNone(self._stop_to_claude(project))
                self.assertIn("[from=<unnamed> id=", self._stop_to_codex(project))

    def test_one_turn_settles_who_is_speaking_exactly_once(self):
        """Three recipients, one sender. Deciding again per recipient would walk
        the process tree three times for an answer already known — and would let
        the label change mid-turn if the registry moved underneath it."""
        with tempfile.TemporaryDirectory() as project:
            self._claude_endpoint(project, self.MINE)
            for alias in ("a", "b", "c"):
                antiphon.peers.register(project, "codex", alias, f"sess-{alias}",
                                        pid=os.getpid(), owner_key=self.MINE)
            payload = {"cwd": project, "transcript_path": "/tmp/transcript"}
            with self._named("ui"), \
                 patch.object(antiphon.peers, "owner_key",
                              return_value=self.MINE) as walk, \
                 patch.object(antiphon.os.path, "exists", return_value=True), \
                 patch.object(antiphon, "last_claude_reply",
                              return_value="@codex:a one\n@codex:b two\n"
                                           "@codex:c three"), \
                 patch.object(antiphon, "read_cursor", return_value={}), \
                 patch.object(antiphon, "write_cursor"), \
                 patch.object(antiphon, "_queue_codex",
                              return_value=(True, "")) as queued, \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(antiphon.push("codex"), 0)
        self.assertEqual(queued.call_count, 3, "all three still went")
        self.assertEqual(walk.call_count, 1, "and the sender was settled once")
        labels = {m.split("id=")[0] for _, m in
                  (c.args for c in queued.call_args_list)}
        self.assertEqual(len(labels), 1, "one turn, one sender identity")
        ids = {m.split("id=")[1].split("]")[0] for _, m in
               (c.args for c in queued.call_args_list)}
        self.assertEqual(len(ids), 3, "but each attempt is its own attempt")

    def test_an_alias_nothing_holds_is_not_published_either(self):
        """Nothing registered under the name at all: the environment says `ui`
        and the registry has never heard of it."""
        with tempfile.TemporaryDirectory() as project:
            self._codex_endpoint(project, self.MINE)
            antiphon.peers.unregister(project, "codex", "ui", pid=os.getpid())
            with self._named("ui"), \
                 patch.object(antiphon.peers, "owner_key", return_value=self.MINE):
                self.assertIsNone(self._stop_to_claude(project))

    def test_a_session_that_cannot_identify_itself_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as project:
            self._codex_endpoint(project, self.MINE)
            with self._named("ui"), \
                 patch.object(antiphon.peers, "owner_key", return_value=None):
                self.assertIsNone(self._stop_to_claude(project))

    def test_a_record_written_before_owner_keys_publishes_nothing(self):
        """It cannot show whose it is, so it is not evidence that it is ours.
        A wrong identity is worse than no identity."""
        with tempfile.TemporaryDirectory() as project:
            antiphon.peers.register(project, "codex", "ui", "rollout-old",
                                    pid=os.getpid())
            with self._named("ui"), \
                 patch.object(antiphon.peers, "owner_key", return_value=self.MINE):
                self.assertIsNone(self._stop_to_claude(project))

    def test_the_mcp_server_publishes_only_an_alias_it_actually_won(self):
        """The direct tool path takes what `register_codex_peer` returned, not
        what the environment asks for. A server refused the alias holds nothing
        and says nothing."""
        sent = []
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "antiphon_send",
                                "arguments": {"text": "hello", "to": "ui"}}}]
        for claim, expected in (("build", "build"), (None, None)):
            sent.clear()
            with tempfile.TemporaryDirectory() as project, self._named("build"), \
                 patch.object(antiphon, "project_dir", return_value=project), \
                 patch.object(antiphon, "register_codex_peer", return_value=claim), \
                 patch.object(antiphon.peers, "unregister"), \
                 patch.object(antiphon, "_record_delivery"), \
                 patch.object(antiphon, "send_to_claude",
                              side_effect=lambda *a, **k:
                                  sent.append(k.get("sender_alias")) or (True, "")), \
                 patch.object(antiphon.sys, "stdin",
                              io.StringIO("".join(json.dumps(r) + "\n"
                                                  for r in requests))), \
                 contextlib.redirect_stdout(io.StringIO()):
                antiphon.mcp()
            self.assertEqual(sent, [expected], repr(claim))

    def test_the_reply_path_takes_no_alias_from_the_environment(self):
        """`channel.mjs` publishes its name only once it holds the claim and
        serves the socket. Falling back to the environment here would undo that
        in the one process that cannot check.

        Both wire forms: an explicit null is what a channel server that lost the
        claim actually sends, and an absent field is what an older one sends.
        The environment says `ui` throughout, and neither is allowed to become
        it."""
        for body in ({"text": "hi", "sender_alias": None, "to": "ui"},
                     {"text": "hi", "to": "ui"}):
            queued = []
            with tempfile.TemporaryDirectory() as project:
                self._codex_endpoint(project, self.MINE)
                with self._named("ui"), \
                     patch.object(antiphon, "project_dir", return_value=project), \
                     patch.object(antiphon, "_queue_codex",
                                  side_effect=lambda session, message:
                                      queued.append(message) or (True, "")), \
                     patch.object(antiphon.sys, "stdin",
                                  io.StringIO(json.dumps(body))), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(antiphon.reply(), 0)
            self.assertIn("[from=<unnamed> id=", queued[0], repr(body))

    def test_register_peer_records_the_owner_key_it_can_see(self):
        """The Stop hook checks the endpoint's owner against its own. Without
        one written here, a Claude session could never publish its alias."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             patch.object(antiphon.peers, "owner_key", return_value=self.MINE), \
             patch.object(antiphon.sys, "stdin", io.StringIO(json.dumps({
                 "kind": "claude", "name": "ui", "address": "/tmp/ui.sock",
                 "pid": os.getpid()}))):
            self.assertEqual(antiphon.register_peer(), 0)
            peer = antiphon.peers.read_peers(project, "claude")[0]
        self.assertEqual(peer["owner"], self.MINE)
        self.assertEqual(peer["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()
