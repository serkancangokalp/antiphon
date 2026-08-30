"""Contracts that only exist as matching strings across a language boundary.

Nothing in Python or Node checks these; they hold only because two files
happen to spell the same word. One of them already broke the live bridge
silently — the Node channel called a `reply` subcommand while Python still
named it `yanit` — so each one gets an assertion that reads the string from
the real source on both sides. A regex that fails to find its target must
fail the test, never quietly pass."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import antiphon
import peers

import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

# The suite describes a project, not the terminal it happens to run in.
# `ANTIPHON_NAME` moves cursors and sockets, so `ANTIPHON_NAME=ui npm test` —
# a reasonable thing to run now — would otherwise exercise a different layout
# than a bare run. Tests that want a name set one with `patch.dict`.
os.environ.pop("ANTIPHON_NAME", None)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def capture(pattern, text):
    """The single capture group of `pattern`, or None if it doesn't match."""
    match = re.search(pattern, text)
    return match.group(1) if match else None


class CrossBoundaryContractTest(unittest.TestCase):
    def test_channel_server_calls_a_python_subcommand_that_exists(self):
        """lib/channel.mjs shells out to the Python entry point by name."""
        source = read("lib", "channel.mjs")

        script = capture(r'bridgeScript\s*=\s*join\(\s*here\s*,\s*"([^"]+)"\s*\)',
                         source)
        self.assertIsNotNone(script, "bridgeScript assignment not found in channel.mjs")
        self.assertEqual(script, os.path.basename(antiphon.__file__))

        verb = capture(r'execFileAsync\(\s*"python3"\s*,\s*\[\s*bridgeScript\s*,'
                       r'\s*"([^"]+)"\s*[,\]]', source)
        self.assertIsNotNone(verb, "python3 invocation not found in channel.mjs")
        self.assertIn(verb, antiphon.COMMANDS)

    def test_installed_hook_commands_name_subcommands_that_exist(self):
        """The commands setup writes into the two hook files."""
        for template, argument in ((antiphon.HOOK_COMMAND, "side"),
                                   (antiphon.PUSH_COMMAND, "target")):
            words = template.format(**{argument: "claude"}).split()
            self.assertEqual(words[0], "antiphon", template)
            self.assertIn(words[1], antiphon.COMMANDS, template)

    def test_mcp_entry_setup_writes_is_dispatched_by_the_node_binary(self):
        """`.mcp.json` starts `antiphon channel`; bin/antiphon.mjs must branch on
        exactly that word, and the command must be the binary package.json ships."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.setup(), 0)
            with open(os.path.join(project, ".mcp.json"), encoding="utf-8") as f:
                server = json.load(f)["mcpServers"]["antiphon"]

        package = json.loads(read("package.json"))
        self.assertIn(server["command"], package["bin"])

        branch = capture(r'subcommand\s*===\s*"([^"]+)"',
                         read("bin", "antiphon.mjs"))
        self.assertIsNotNone(branch, "subcommand comparison not found in bin/antiphon.mjs")
        self.assertEqual(server["args"], [branch])

    def test_agents_rule_names_the_mcp_tool_codex_actually_gets(self):
        """AGENTS_RULE tells Codex to call a tool by name; that name has to be
        the one the MCP server advertises."""
        mentioned = set(re.findall(r"`([a-z][a-z0-9_]*)`", antiphon.AGENTS_RULE))
        self.assertTrue(mentioned, "AGENTS_RULE mentions no tool name at all")
        for tool in antiphon.TOOLS:
            self.assertIn(tool["name"], mentioned)

    def test_every_lib_module_ships_in_the_package(self):
        """`files` is an allowlist. A module missing from it vanishes from the
        published tarball and breaks the CLI on install — never in a test here."""
        listed = json.loads(read("package.json"))["files"]
        for entry in sorted(os.listdir(os.path.join(ROOT, "lib"))):
            if entry.endswith((".py", ".mjs")):
                self.assertIn(f"lib/{entry}", listed, entry)

    def test_subcommands_the_channel_server_calls_are_dispatchable(self):
        """`channel.mjs` runs `antiphon.py <subcommand>`. A function that exists
        but is missing from COMMANDS fails only at runtime, inside a hook, with
        the usage text printed where nobody reads it."""
        source = read("lib", "channel.mjs")
        called = set(re.findall(r'bridgeScript,\s*"([a-z_]+)"', source))
        self.assertTrue(called, "no subcommand call found in channel.mjs")
        for name in sorted(called):
            self.assertIn(name, antiphon.COMMANDS, name)

    def test_node_and_python_derive_the_same_socket_key(self):
        """Two languages spelling one hash. When they drifted before, the bridge
        went quiet and nothing failed anywhere."""
        probe = subprocess.run(
            ["node", "-e",
             "const {createHash}=require('node:crypto');"
             "const d=process.argv[1],n=process.argv[2];"
             "console.log(createHash('sha256').update(n?`${d}\\0${n}`:d)"
             ".digest('hex').slice(0,20))",
             "--", "/tmp/project", "ui"],
            capture_output=True, text=True, check=True)
        self.assertEqual(probe.stdout.strip(), peers.socket_key("/tmp/project", "ui"))

    def test_the_channel_server_claims_the_address_before_it_binds(self):
        """Ordering is the whole fix. Probe, unlink and listen are three steps, so
        two servers can each find the path free; only an atomic claim taken first
        keeps one of them from binding over the other. A later edit that moves the
        claim down would restore the race without failing any behavioural test."""
        source = read("lib", "channel.mjs")
        # Source position would only measure where things are written, and
        # `serveSocket` is defined above the branch that calls it. What matters is
        # evaluation order in the chain, plus binding living in exactly one place
        # so there is no second path to it.
        self.assertLess(source.index("await claimPeer()"),
                        source.index("await serveSocket()"),
                        "the address must be claimed before anything binds")
        self.assertEqual(source.count("socketServer.listen("), 1,
                         "binding must happen in exactly one place")
        serve_body = source[source.index("async function serveSocket()"):]
        self.assertLess(serve_body.index("socketServer.listen("),
                        serve_body.index("\nawait mkdir("),
                        "the bind must sit inside serveSocket, after the claim")

    def test_the_id_read_off_a_rollout_is_one_the_registry_will_store(self):
        """`antiphon.SESSION_ID` pulls the id out of a rollout file name;
        `peers.valid_session_id` decides whether it may become an address. Two
        patterns, one shape — and if they drift, a real session id is read and
        then silently refused, leaving a peer live and unroutable forever."""
        for name in ("rollout-2026-08-28T15-55-43-"
                     "01a04870-bd35-7732-9dea-e564f731dba7.jsonl",
                     "rollout-2026-08-30T00-01-02-"
                     "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7.jsonl"):
            match = antiphon.SESSION_ID.search(name)
            self.assertIsNotNone(match, name)
            self.assertTrue(peers.valid_session_id(match.group(1)), name)

    def test_every_surface_that_shows_the_event_shows_who_sent_it(self):
        """Three places describe the same event to a Claude reader: the channel
        server's MCP instructions, the CLAUDE.md rule and the README. One of
        them omitting `sender_alias` teaches an agent that the field is not
        there, and it never passes it back as `to`."""
        surfaces = {
            "channel.mjs instructions": re.sub(r'"\s*\+\s*\n\s*"', "",
                                               read("lib", "channel.mjs")),
            "CLAUDE.md rule": antiphon.CLAUDE_RULE,
            "README": read("README.md"),
        }
        for where, text in surfaces.items():
            example = re.search(r"<channel source=.{0,200}?>", text, re.S)
            self.assertIsNotNone(example, f"no example event in {where}")
            self.assertIn("sender_alias", example.group(0), where)

    def test_no_surface_tells_an_agent_to_pass_a_null_recipient(self):
        """Three sentences that have to agree: `sender_alias` may be null, `to`
        must be a string, and the reader is told when to pass one as the other.
        "Always" made those three incompatible, and an agent following it would
        send `to: null` and be refused for doing as it was told."""
        surfaces = {
            "channel.mjs instructions": re.sub(r'"\s*\+\s*\n\s*"', "",
                                               read("lib", "channel.mjs")),
            "CLAUDE.md rule": antiphon.CLAUDE_RULE,
            "README": read("README.md"),
        }
        schema = next(t for t in antiphon.TOOLS
                      if t["name"] == "antiphon_send")["inputSchema"]
        self.assertEqual(schema["properties"]["to"]["type"], "string",
                         "the premise of this test: null is not a valid `to`")
        for where, text in surfaces.items():
            instruction = re.search(r"[^.]*sender_alias[^.]*as [`]?to[`]?[^.]*\.",
                                    text, re.S)
            self.assertIsNotNone(instruction,
                                 f"{where} does not say when to pass it at all")
            said = instruction.group(0)
            self.assertNotIn("always", said.lower(), where)
            self.assertIn("non-null", said, where)

    def test_the_codex_side_is_told_the_same_thing_in_its_own_form(self):
        """The two directions carry identity differently — metadata one way, a
        visible label the other — but an agent on either side needs the same
        three facts: who spoke, how to answer that one, and what it means when
        nobody can be named. Only the Claude surfaces said so at first, which
        left a Codex agent unable to address a reply at all."""
        for where, text in (("AGENTS.md rule", antiphon.AGENTS_RULE),
                            ("README", read("README.md"))):
            self.assertIn("[from=", text, where)
            self.assertIn(antiphon.NO_ALIAS, text, where)
        self.assertIn("antiphon_send(to=", antiphon.AGENTS_RULE)
        self.assertIn("@claude:<alias>", antiphon.AGENTS_RULE)
        # And the label the rule describes is the one the code produces.
        self.assertIn(antiphon.queue_label("ui", "an-id"),
                      "[from=ui id=an-id]")

    def test_both_tools_describe_their_recipient_argument_identically(self):
        """`antiphon_send` and `reply_to_codex` take the same argument for the
        same reason, one in Python and one in Node. Two descriptions that drift
        apart is how an agent learns a rule that is not true on its side."""
        node = read("lib", "channel.mjs")
        collapsed = re.sub(r'"\s*\+\s*\n\s*"', "", node)
        self.assertIn(antiphon.TO_DESCRIPTION, collapsed,
                      "channel.mjs must carry the same sentence verbatim")
        self.assertIn('required: ["text"]', node,
                      "and must not make it required on its side alone")

    def test_both_sides_agree_on_the_channel_message_limit(self):
        """The sender refuses before transport and the server refuses on arrival.
        If the two numbers drift the sender lets through what the server then
        rejects, and the message is lost between honest-looking components."""
        source = read("lib", "channel.mjs")
        node_limit = capture(r"const MAX_MESSAGE_BYTES = ([^;]+);", source)
        self.assertEqual(eval(node_limit, {"__builtins__": {}}),
                         antiphon.MAX_CHANNEL_BYTES)


if __name__ == "__main__":
    unittest.main()
