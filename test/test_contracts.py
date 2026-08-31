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

    def test_both_sides_reserve_the_same_key_for_a_peer_with_no_name(self):
        """Node writes the registry entry and Python reads it. Two spellings
        would leave an unnamed session registered under a key the resolver does
        not recognise as reserved — and `valid_name` would then be the only
        thing standing between it and being addressable."""
        node = capture(r'const UNNAMED_KEY = "([^"]+)"', read("lib", "channel.mjs"))
        self.assertIsNotNone(node, "channel.mjs no longer declares the key")
        self.assertEqual(node, peers.UNNAMED)
        self.assertFalse(peers.valid_name(node),
                         "and it must stay outside the alias grammar")

    def test_no_surface_tells_an_agent_to_pass_a_null_recipient(self):
        """Three sentences that have to agree: `sender_alias` is always a string
        (`<unnamed>` when the peer has no name — measured, Claude Code 2.1.251
        rejects a null there and the event never arrives), `to` must be a
        string, and the reader is told when to pass one as the other. "Always"
        made those incompatible, and an agent following it would send the
        sentinel as `to` and be refused for doing as it was told; "non-null"
        described a wire that no longer carries null at all."""
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
            self.assertNotIn("null", said.lower(),
                             f"{where} still describes a null the wire no longer carries")
            self.assertIn(peers.UNNAMED, said,
                          f"{where} must name the sentinel the reader will actually see")

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


def section(text, heading):
    """The body of one `## heading` section of a markdown document.

    Prose contracts are checked against the section that owns them, not the
    whole file: a number that survives only in an unrelated paragraph is drift
    that happens to still match."""
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
                      text, re.M | re.S)
    return match.group(1) if match else None


NODE_FLOOR = re.compile(r"^\s*>=\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$")


def node_floor(spec):
    """The lowest Node version a plain `>=X.Y.Z` range admits, as a tuple.

    Returns None for anything that is not a simple floor. Callers must fail on
    None rather than skip: an unparsed range is a range nobody is checking."""
    match = NODE_FLOOR.match(spec)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


class ShippedContractTest(unittest.TestCase):
    """Promises made in prose, checked against the code that has to keep them.

    Same failure mode as the cross-boundary contracts above — two places
    spelling one fact — with one side unexecutable. Nothing runs a README, so a
    number that stops being true stays on the page, and the next reader trusts
    it. Each assertion here reads its number from the real source."""

    def test_the_declared_node_floor_admits_everything_npm_installs(self):
        """`engines.node` is a promise made to whoever types `npm i -g antiphon`;
        the lockfile decides what they actually get. When a transitive dependency
        raises its own floor above ours the promise is already broken: a default
        install only warns, and an `engine-strict` install fails outright."""
        package = json.loads(read("package.json"))
        declared = node_floor(package["engines"]["node"])
        self.assertIsNotNone(declared,
                             f"engines.node is not a plain floor: "
                             f"{package['engines']['node']}")

        locked = json.loads(read("package-lock.json"))["packages"]
        checked = 0
        for name, meta in sorted(locked.items()):
            if not name:                  # the root entry restates our own claim
                continue
            spec = (meta.get("engines") or {}).get("node")
            if not spec:
                continue
            needed = node_floor(spec)
            self.assertIsNotNone(needed, f"{name} engines.node unparsed: {spec}")
            checked += 1
            self.assertLessEqual(
                needed, declared,
                f"{name}@{meta.get('version')} requires node {spec}, but the "
                f"package promises {package['engines']['node']}")
        self.assertTrue(checked, "no locked dependency declares a node engine")

    def test_the_readme_names_the_floors_the_package_is_installed_under(self):
        """A reader decides whether to try the thing from the README, and finds
        out what it really needs from npm. Two numbers, one fact."""
        readme = read("README.md")
        major = node_floor(json.loads(read("package.json"))["engines"]["node"])[0]
        self.assertIn(f"Node {major}+", readme)
        stale = set(re.findall(r"Node (\d+)\+", readme)) - {str(major)}
        self.assertFalse(stale, f"the README also claims Node {stale}")
        self.assertRegex(readme, r"Python 3\.\d+\+",
                         "the README must name the Python floor it was tested "
                         "at, not a bare `Python 3`")

    def test_the_python_floor_doctor_enforces_is_the_one_the_readme_promises(self):
        """`doctor` fails a Python older than the floor, and the README tells a
        reader which floor that is. Two places, one number — and the number is
        read back, not merely looked for: `assertRegex(readme, r"Python 3\\.\\d+\\+")`
        passes against any floor at all, including one doctor stopped
        enforcing."""
        readme = read("README.md")
        stated = re.findall(r"Python (\d+)\.(\d+)\+", readme)
        self.assertTrue(stated, "the README must name the Python floor")
        self.assertEqual({tuple(int(part) for part in pair) for pair in stated},
                         {antiphon.PYTHON_FLOOR},
                         "the README's Python floor and antiphon.PYTHON_FLOOR "
                         "are one fact")

    def test_both_mcp_handshakes_name_the_package_version(self):
        """Two servers, one version. The Python side's handshake sat at 0.1.0
        through two releases because nothing tied it to package.json; a grep
        then pinned that one string — and the Node channel's handshake went on
        announcing 0.1.0 through 0.3.0 and 0.3.1, because the grep never looked
        at it. So this reads neither source: it runs each server's `initialize`
        and compares what comes back with the version npm installs."""
        version = json.loads(read("package.json"))["version"]
        initialize = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18",
                                            "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "0"}}})

        with self.subTest(server="python mcp"), \
             tempfile.TemporaryDirectory() as project:
            out = io.StringIO()
            with patch.object(antiphon, "project_dir", return_value=project), \
                 patch.object(antiphon.sys, "stdin", io.StringIO(initialize + "\n")), \
                 contextlib.redirect_stdout(out):
                antiphon.mcp()
            answered = json.loads(out.getvalue().splitlines()[0])
            self.assertEqual(answered["result"]["serverInfo"]["version"], version)

        with self.subTest(server="node channel"), \
             tempfile.TemporaryDirectory() as project:
            # Its own project directory, so the socket key never collides with
            # a live session's; stdin closing is the channel's own shutdown
            # signal, and a clean shutdown unlinks the socket it bound.
            env = {**os.environ, "ANTIPHON_CWD": project}
            env.pop("ANTIPHON_NAME", None)
            done = subprocess.run(
                ["node", os.path.join(ROOT, "lib", "channel.mjs")],
                input=initialize + "\n", capture_output=True, text=True,
                env=env, timeout=60)
            answers = [json.loads(line) for line in done.stdout.splitlines()
                       if line.strip()]
            self.assertTrue(answers, f"no handshake answer; stderr: {done.stderr}")
            self.assertEqual(answers[0]["result"]["serverInfo"]["version"], version)

    def test_paged_context_limits_match_code(self):
        """Every number the README states about the pull path is read back off
        the constant that enforces it, and the retired cuts are gone from both
        sides of the boundary: no 2,600-character trim, no 420-character
        per-event cut, in code or in prose."""
        self.assertEqual(antiphon.PAGE_BUDGET, 8_000)
        self.assertEqual(antiphon.EVENT_LIMIT, 40)
        self.assertEqual(antiphon.RECENT_FILES, 3)
        self.assertEqual(antiphon.PAGE_CURSOR_VERSION, 3)
        for side in ("claude", "codex"):
            self.assertEqual(antiphon.page_cursor_key(side), side + "_pages")
        self.assertEqual(set(antiphon.REPLAY_NOTICES), {"legacy_upgrade",
                                                        "cursor_recovery"})
        self.assertFalse(hasattr(antiphon, "SUMMARY_BUDGET"),
                         "the summary budget must not survive as an unused "
                         "constant a later reader mistakes for a live setting")
        self.assertFalse(hasattr(antiphon, "EVENT_BUDGET"),
                         "the per-event cut must not survive as an unused "
                         "constant a later reader mistakes for a live setting")
        limits = section(read("README.md"), "Limits")
        self.assertIsNotNone(limits, "the README has no Limits section")
        # `\s+` rather than a literal space: prose gets rewrapped, and a number
        # that moved to the next line is still stated next to its noun.
        for what, words in (
                ("the page target", ("8,000", "UTF-8", "bytes")),
                ("the page record limit", ("40", "completed", "source", "records")),
                ("the transcript window",
                 (str(antiphon.RECENT_FILES), "transcript", "files")),
                ("the direct-channel cap",
                 (str(antiphon.MAX_CHANNEL_BYTES // 1024), "KiB"))):
            self.assertRegex(limits, r"\s+".join(map(re.escape, words)), what)
        self.assertRegex(
            limits, r"(?i)8,000 UTF-8 bytes[^.]*measured[^.]*not a\s+permanent",
            "the 8,000-byte target's own sentence must call it a measured "
            "host observation and not a permanent guarantee — the word "
            "'measured' appearing somewhere else in the section is not that")
        self.assertNotIn("2,600", limits, "the retired summary budget")
        self.assertNotRegex(limits, r"\b420\b", "the retired per-event cut")

    def test_attachment_limits_match_code(self):
        """The three attachment numbers a person can act on are read back out
        of README §Limits, by the same technique the paging numbers use — the
        word-adjacency regex, so prose that rewraps still counts.

        The queue's own bound is deliberately absent from that list. It is not
        a constant: it is `SC_ARG_MAX` minus the live environment, measured
        falling from 1,044,820 to 444,759 bytes as the environment grew, and a
        number pinned here would be a promise about somebody else's shell.
        """
        self.assertEqual(antiphon.ATTACHMENT_MAX, 8 * 1024 * 1024)
        self.assertEqual(antiphon.ATTACHMENT_QUOTA, 64 * 1024 * 1024)
        self.assertEqual(antiphon.ATTACHMENT_TTL, 7 * 24 * 3600)
        self.assertFalse(hasattr(antiphon, "QUEUE_ARGV_BOUND"),
                         "a constant queue bound cannot be correct; the "
                         "predicate computes it at call time")
        limits = section(read("README.md"), "Limits")
        self.assertIsNotNone(limits, "the README has no Limits section")
        mib = 1024 * 1024
        for what, words in (
                ("the per-attachment cap",
                 (str(antiphon.ATTACHMENT_MAX // mib), "MiB")),
                ("the whole store's quota",
                 (str(antiphon.ATTACHMENT_QUOTA // mib), "MiB")),
                ("the attachment lifetime",
                 (str(antiphon.ATTACHMENT_TTL // 86400), "days"))):
            # `\b` in front, unlike the paging pins: these numbers are short
            # enough to hide inside a bigger one — measured, an `8 MiB` pin
            # matches the `8 MiB` at the end of `48 MiB`, so a drifted README
            # would have passed.
            self.assertRegex(limits, r"\b" + r"\s+".join(map(re.escape, words)),
                             what)
        # The two roads, stated separately rather than as one bound promise:
        # an agent that learns "oversized direct sends work now" and then loses
        # a `@claude` line to an exit-0 hook was told something untrue.
        self.assertRegex(limits, r"(?i)direct\s+tools[^.]*park",
                         "the tool road parks")
        self.assertRegex(limits, r"(?i)marker\s+road\s+does\s+not\s+park",
                         "the marker road does not, and the README says so")
        self.assertRegex(limits, r"(?i)still\s+refused",
                         "and says what happens there instead")
        # The hash rule is stated performably, and the locality bound is stated
        # at all — this is the first feature to make it load-bearing.
        self.assertRegex(limits, r"(?i)first\s+blank\s+line", "the hash's subject")
        self.assertRegex(limits, r"(?i)shasum", "and the command that runs it")
        self.assertRegex(limits, r"(?i)this\s+machine", "the same-machine bound")
        # There is no timer. A file is removed by the first hook after its TTL
        # and never at all in a project where neither side takes a turn, so
        # "deleted after 7 days" would be a promise nothing keeps.
        self.assertRegex(limits, r"(?i)eligible\s+for\s+removal",
                         "the TTL makes a file eligible, it does not delete it")
        self.assertRegex(limits, r"(?i)no\s+timer", "and the README says so")
        # The two spec bullets this does not deliver are named as undelivered.
        self.assertRegex(limits, r"(?i)acknowledgement[^.]*not|not[^.]*acknowledgement",
                         "acknowledgement is named as absent")
        self.assertRegex(limits, r"(?i)retry", "and so is retry")

    def test_both_surfaces_teach_the_attachment_envelope(self):
        """An envelope naming a path an agent has never been told about is a
        pointer to nothing. Both surfaces name the label, the file's origin,
        the hash rule with the command that performs it, the same-machine
        bound, and the road asymmetry — because after this change the direct
        tool and the marker line are no longer interchangeable at size, and the
        difference is invisible to the sender."""
        for name, text in (("AGENTS_RULE", antiphon.AGENTS_RULE),
                           ("CLAUDE_RULE", antiphon.CLAUDE_RULE)):
            self.assertIn(antiphon.ATTACHMENT_LABEL, text, name)
            self.assertIn(".antiphon/messages/", text, name)
            self.assertRegex(text, r"(?i)first blank line", name)
            self.assertRegex(text, r"(?i)shasum", name)
            self.assertRegex(text, r"(?i)own words", name)
            self.assertRegex(text, r"(?i)this same machine", name)
            self.assertRegex(text, r"(?i){} days".format(
                antiphon.ATTACHMENT_TTL // 86400), name)
            # The same honesty as the README: eligible, not deleted on a timer.
            self.assertRegex(text, r"(?i)eligible for removal", name)
            self.assertRegex(text, r"(?i)no timer", name)
            # Operational, not decorative: which road parks and which refuses.
            self.assertRegex(text, r"(?i)is not parked|is parked", name)
            self.assertRegex(text, r"(?i)passive\s+pages", name)
        self.assertIn("antiphon_send", antiphon.AGENTS_RULE)
        self.assertIn("@claude` line is not parked", antiphon.AGENTS_RULE)
        self.assertIn("reply_to_codex", antiphon.CLAUDE_RULE)
        self.assertIn("@codex` line is not parked", antiphon.CLAUDE_RULE)

    def test_paged_context_surfaces_teach_has_more(self):
        """An agent that has not been told a page is one of several will treat
        the first page as the whole answer. Both the tool description and the
        agent instructions teach the loop, and both scope `has_more: false` to
        the sources discovery can currently see — it is not an inventory of
        project history."""
        tool = next(t for t in antiphon.TOOLS if t["name"] == "antiphon_read")
        for name, text in (("the antiphon_read description", tool["description"]),
                           ("AGENTS_RULE", antiphon.AGENTS_RULE)):
            self.assertIn("has_more", text, name)
            self.assertRegex(text, r"(?i)one page|a single page", name)
            self.assertRegex(text, r"(?i)discover", name)
            # Operational, not decorative: the surface must tell the agent what
            # to DO while has_more is true — call again, or let later turns
            # drain it. Naming the field without the loop teaches nothing.
            self.assertRegex(text, r"(?i)again", name)
            self.assertRegex(text, r"(?i)drain", name)
        # The replay lifecycle lives on the agent surface too: an agent that
        # sees dozens of duplicate-history pages with no framing will treat
        # recovery as malfunction. Deleting this guidance left every test
        # green once; now it cannot.
        rule = antiphon.AGENTS_RULE
        self.assertRegex(rule, r"(?i)upgrade", "AGENTS_RULE")
        self.assertRegex(rule, r"(?i)cursor\s+recovery", "AGENTS_RULE")
        self.assertRegex(rule, r"(?i)duplicate", "AGENTS_RULE")
        self.assertRegex(rule, r"(?i)disappear|clear", "AGENTS_RULE")

    def test_paged_context_surfaces_explain_oversized_mcp(self):
        """Measured on the installed hosts: both hooks spill an oversized record
        and expose a path, but Codex's MCP tool result does not — the model saw
        neither content nor a path. So `antiphon_read` refuses that one record
        without advancing, and the surfaces have to say the safe route is the
        next automatic prompt hook, or the refusal reads as data loss."""
        tool = next(t for t in antiphon.TOOLS if t["name"] == "antiphon_read")
        for name, text in (("the antiphon_read description", tool["description"]),
                           ("AGENTS_RULE", antiphon.AGENTS_RULE)):
            self.assertRegex(text, r"(?i)nothing (is|was) (read or )?marked seen",
                             name)
            self.assertRegex(text, r"(?i)next (automatic )?prompt", name)
            # Review proved these decorative once: swapping both surfaces'
            # safe-refusal story for explicit truncation left everything
            # green. The refusal, the no-truncation promise and the whole
            # delivery are each load-bearing words.
            self.assertRegex(text, r"(?i)refus", name)
            self.assertRegex(text, r"(?i)truncat", name)
            self.assertRegex(text, r"(?i)whole", name)

    def test_paged_context_docs_name_the_remaining_losses(self):
        """A Limits section that reads as though the work were finished retires
        the reader's suspicion about exactly the paths that still lose content.
        The remaining gaps are named, the upgrade replay is quantified rather
        than waved off as one-time, and host spill files are named as holding
        verbatim transcript text."""
        limits = section(read("README.md"), "Limits")
        self.assertIsNotNone(limits, "the README has no Limits section")
        for gap, pattern in (
                ("compressed tool detail", r"(?i)tool (call|detail)s? (are|remain|stay)[a-z ]*compressed"),
                ("backward paging", r"(?i)backward"),
                ("catalog completeness", r"(?i)newest 3|newest three"),
                ("host spill contents", r"(?i)verbatim"),
                ("the replay size", r"69"),
                ("the replay size, codex side", r"53")):
            self.assertRegex(limits, pattern, gap)
        self.assertNotRegex(limits, r"(?i)one[- ]time",
                            "the measured replay is 69/53 pages, not a "
                            "hand-wave")
        self.assertRegex(limits, r"(?i)line structure[^.]*intact|"
                                 r"line structure[^.]*preserved",
                         "whitespace preservation is stated, not implied")
        self.assertRegex(limits, r"(?i)never\s+split\s+across\s+pages",
                         "record atomicity is stated")
        self.assertIn("_pages", limits, "the semantic key is named")
        self.assertIn("_seen", limits, "the preserved legacy key is named")
        self.assertRegex(limits, r"(?i)exactly\s+two\s+fixed\s+explanation",
                         "the replay reasons are a closed set, and the README "
                         "says so rather than leaving the set open")
        self.assertRegex(limits, r"(?i)legacy\s+upgrade", "reason one, named")
        self.assertRegex(limits, r"(?i)cursor\s+recovery", "reason two, named")
        self.assertRegex(limits, r"(?i)(malformed|unreadable)[^.]*byte\s+zero",
                         "a malformed existing cursor replays; it is not a "
                         "fresh install")
        self.assertRegex(limits, r"(?i)missing\s+cursor[^.]*new\s+side",
                         "only a genuinely missing cursor means a new side")
        self.assertRegex(limits, r"(?i)timestamp\s+cursor[^.]*boundary[^.]*gone",
                         "the retired boundary-migration promise is named as "
                         "retired")
        self.assertNotRegex(antiphon.offset_at_or_after.__doc__,
                            r"(?i)migrat",
                            "the helper's docstring described the rejected "
                            "boundary-migration model once already")
        self.assertRegex(limits, r"(?i)host.s own\s+lifecycle",
                         "spill files follow the host lifecycle, and the "
                         "README says whose files they are")
        backlog = read("BACKLOG.md")
        for gap, pattern in (
                ("stable event id", r"(?i)stable\s+event\s+id"),
                ("source catalog", r"(?i)source\s+catalog"),
                ("degraded-discovery marker", r"(?i)degraded-discovery"),
                ("backward paging", r"(?i)backward\s+paging"),
                ("last-record anchor", r"(?i)anchor"),
                ("descriptor-safe reading", r"(?i)descriptor-safe"),
                ("v2 retirement", r"(?i)retirement")):
            self.assertRegex(backlog, pattern,
                             f"BACKLOG's ledger must keep naming {gap}")
        # The direct-channel spill left this ledger by being closed in writing,
        # which is the only way a gap may leave it. Its entry says what it did
        # not deliver, so the two halves cannot drift into "it shipped" with
        # nothing recording what shipping meant.
        still_open = section(backlog, "P0 — Lossless, paged context transfer")
        self.assertIsNotNone(still_open, "the P0 ledger is gone")
        self.assertNotRegex(still_open, r"(?i)direct-channel\s+spill",
                            "the spill is no longer an open P0 loss")
        self.assertRegex(
            backlog,
            r"## P1 — Large direct-message attachments[^\n]*"
            r"minus acknowledgement and retry",
            "and the entry that closed it names what it left open")
        self.assertNotRegex(limits, r"(?i)\blossless\b(?![^.]*\bBACKLOG)",
                            "Limits calls the pull path lossless while tool "
                            "detail, discovery and backward paging still lose")
        self.assertIn("BACKLOG.md", limits,
                      "Limits must send the reader to the tracked work")
        # The retired cuts must not survive elsewhere in the same package as a
        # present-tense bug. This exact contradiction shipped once: P0 declared
        # the 2,600/420 cuts gone while a P1 paragraph three sections later
        # still called one "the 2,600-character pull bug".
        for doc in ("README.md", "BACKLOG.md"):
            text = read(doc)
            self.assertNotRegex(text, r"(?i)pull bug", doc)
            self.assertNotRegex(
                text, r"(?i)(2,600|420)[^.\n]*\b(keeps|cuts|is cut|loses)\b",
                f"{doc} still describes a retired cut in the present tense")

    def test_an_npm_reader_can_open_the_backlog_the_readme_points_at(self):
        """`files` is an allowlist. A pointer into a file that never entered the
        tarball is a dead end for exactly the readers who installed the way the
        README told them to."""
        self.assertIn("BACKLOG.md", json.loads(read("package.json"))["files"])

    def test_the_readme_shows_how_to_start_each_kind_of_named_peer(self):
        """Naming is not a flag on a command, it is an environment variable read
        at startup, and getting it wrong is invisible: the session comes up fine
        and simply cannot be addressed."""
        readme = read("README.md")
        for command in ("ANTIPHON_NAME=ui claude",
                        "ANTIPHON_NAME=api claude",
                        "ANTIPHON_NAME=build codex",
                        "ANTIPHON_NAME=review codex"):
            self.assertIn(command, readme, command)

    def test_the_multi_peer_section_addresses_both_sides_by_name(self):
        """The two sides are addressed with the same syntax and refuse on
        different rules. A section that showed only one of them would leave the
        reader to assume the other matches."""
        multi = section(read("README.md"), "Many peers")
        self.assertIsNotNone(multi, "the README has no multi-peer section")
        self.assertIn("@claude:ui", multi)
        self.assertIn("@codex:build", multi)

    def test_every_surface_rules_broadcast_out_rather_than_leaving_it_open(self):
        """Refusing is the whole design. An agent that has not been told a send
        can be refused reads a quiet failure as a broken bridge and stops using
        the addressed form."""
        for where, text in (("README", read("README.md")),
                            ("AGENTS.md rule", antiphon.AGENTS_RULE),
                            ("CLAUDE.md rule", antiphon.CLAUDE_RULE)):
            self.assertRegex(text, r"never broadcast", where)
            self.assertRegex(text, r"is refused|are refused", where)

    def test_both_generated_rules_tell_a_peer_how_to_become_addressable(self):
        """An agent that finds itself unaddressable can say so, but only if it
        knows what would have made it addressable. The rule is the only place it
        will read that."""
        for where, text in (("AGENTS.md rule", antiphon.AGENTS_RULE),
                            ("CLAUDE.md rule", antiphon.CLAUDE_RULE)):
            self.assertIn("ANTIPHON_NAME", text, where)

    def test_both_generated_rules_keep_the_ambient_pull_apart_from_a_direct_send(self):
        """One is addressed and reaches one peer; the other is project-wide
        awareness that today may merge several transcripts under a generic label. An
        agent that conflates them will read another peer's words as if they had
        been sent to it."""
        for where, text in (("AGENTS.md rule", antiphon.AGENTS_RULE),
                            ("CLAUDE.md rule", antiphon.CLAUDE_RULE)):
            self.assertIn("project-wide", text, where)

    def test_setup_tells_the_operator_to_name_every_terminal(self):
        """The closing guidance is the last thing printed before anyone starts a
        session, and the only place the launch line appears at the moment it is
        about to be typed. A named Claude beside an unnamed Codex cannot be
        replied to at all."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(antiphon, "project_dir", return_value=project):
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                self.assertEqual(antiphon.setup(), 0)
        guidance = printed.getvalue()
        self.assertRegex(guidance, r"ANTIPHON_NAME=\S+ claude ")
        self.assertRegex(guidance, r"ANTIPHON_NAME=\S+ codex")

if __name__ == "__main__":
    unittest.main()
