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
import inspect
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
        # The claim now declares which kind of claim it is, so match the call
        # rather than one spelling of its argument: what this guards is the
        # order, and a literal that pins the argument too would fail the next
        # time the payload gains a field without the race ever returning.
        self.assertLess(source.index("await claimPeer("),
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

    def test_both_mcp_servers_publish_one_retrieval_contract(self):
        node = re.sub(r'"\s*\+\s*\n\s*"', "", read("lib", "channel.mjs"))
        self.assertIn(antiphon.RETRIEVE_DESCRIPTION, node)
        description = antiphon.RETRIEVE_DESCRIPTION.lower()
        for phrase in ("invocation only", "never the tool result", "read-only",
                       "write-free", "8000", "antiphon retrieve"):
            self.assertIn(phrase, description, phrase)

    def test_every_agent_surface_teaches_the_retrieval_limits(self):
        channel = re.sub(r'"\s*\+\s*\n\s*"', "", read("lib", "channel.mjs"))
        surfaces = {
            "AGENTS rule": antiphon.AGENTS_RULE,
            "CLAUDE rule": antiphon.CLAUDE_RULE,
            "channel instructions": channel,
            "README": read("README.md"),
        }
        for where, text in surfaces.items():
            self.assertIn("antiphon_retrieve", text, where)
            self.assertRegex(text, r"(?i)content-bound", where)
            self.assertRegex(text, r"(?i)invocation only", where)
            self.assertRegex(text, r"(?i)never (the |a )?(tool )?result", where)
            self.assertRegex(text, r"(?i)read-only|cursor-neutral", where)
            self.assertRegex(text, r"8,?000", where)
            self.assertIn("antiphon retrieve", text, where)
            self.assertRegex(text, r"(?i)retention|compact", where)
            self.assertRegex(text, r"(?i)unavailable", where)
            self.assertRegex(text, r"(?i)duplicate|two copies", where)
            self.assertRegex(text, r"(?i)untrusted", where)

    def test_docs_name_the_indexless_diagnostic_trade_and_closed_p0(self):
        readme = read("README.md")
        backlog = read("BACKLOG.md")
        for where, text in (("README", readme), ("BACKLOG", backlog)):
            self.assertRegex(text, r"(?i)no persistent (invocation )?index", where)
            self.assertRegex(text, r"(?i)tombstone", where)
            self.assertRegex(text, r"(?is)changed.{0,80}expired.{0,80}never[- ]existed|"
                                   r"never[- ]existed.{0,80}changed", where)
            self.assertRegex(text, r"(?i)earlier-prefix", where)
            self.assertRegex(text, r"(?i)old id.{0,100}(not|never).{0,40}new|"
                                   r"changed.{0,100}old id", where)
        p0 = section(backlog, "P0 — Lossless, paged context transfer")
        open_phase = capture(
            r"(?ms)^### Still open, by name\s*$(.*?)(?=^### |\Z)", p0)
        self.assertNotRegex(open_phase, r"(?i)stable event ids|tool-call retrieval")
        self.assertRegex(p0, r"(?i)Completed by Wave 1D")

    def test_readme_distinguishes_claude_details_from_codex_name_only_pages(self):
        limits = section(read("README.md"), "Limits")
        self.assertIsNotNone(limits)
        renderer = inspect.getsource(antiphon.claude_events)
        detail_expression = capture(
            r'(?s)detail = \((.*?)\)\n\s*events\.append', renderer)
        self.assertIsNotNone(detail_expression)
        detail_keys = re.findall(
            r'arguments\.get\("([^"]+)"\)', detail_expression)
        self.assertTrue(detail_keys)
        for key in detail_keys:
            self.assertIn("`%s`" % key, limits)
        self.assertRegex(
            limits,
            r"(?is)Claude.{0,160}(file_path|command|pattern).{0,240}Codex.{0,80}name-only")
        self.assertNotRegex(limits, r"(?i)Their arguments are absent")

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

    def test_the_horizon_is_one_moment_for_the_reader_and_named_on_both_rules(self):
        """The shipped horizon is cross-source and wall-clock bounded. The
        README once described the abandoned per-source design, and the pin on
        'older than 24 hours' passed on the wrong sentence."""
        limits = section(read("README.md"), "Limits")
        self.assertRegex(limits, r"newest complete\s+record the other side wrote in\s+any of its sources")
        self.assertRegex(limits, r"skipped\s+whole")
        self.assertRegex(limits, r"never later than the\s+wall clock")
        for rule in (antiphon.AGENTS_RULE, antiphon.CLAUDE_RULE):
            self.assertIn("`skipped:`", rule,
                          "the page's own word, so an agent seeing it knows what it is")
            self.assertRegex(rule, r"older than 24 hours before the newest record "
                                   r"in (Claude|Codex)'s transcripts")
            self.assertTrue(rule.rstrip("\n").endswith(antiphon.SECTION_END),
                            "the generated section closes itself")

    def test_paged_context_limits_match_code(self):
        """Every number the README states about the pull path is read back off
        the constant that enforces it, and the retired cuts are gone from both
        sides of the boundary: no 2,600-character trim, no 420-character
        per-event cut, in code or in prose."""
        self.assertEqual(antiphon.PAGE_BUDGET, 8_000)
        self.assertEqual(antiphon.EVENT_LIMIT, 40)
        self.assertEqual(antiphon.RECENT_FILES, 3)
        self.assertEqual(antiphon.PAGE_HORIZON, 24 * 3600)
        self.assertEqual(antiphon.LOOKBACK, 6 * 3600)
        self.assertEqual(antiphon.PAGE_CURSOR_VERSION, 3)
        for side in ("claude", "codex"):
            self.assertEqual(antiphon.page_cursor_key(side), side + "_pages")
        self.assertEqual(set(antiphon.REPLAY_NOTICES), {"legacy_upgrade",
                                                        "cursor_recovery",
                                                        "anchor_upgrade"})
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
                ("the catalog hook batch",
                 (str(antiphon.CATALOG_BATCH), "candidate", "records", "per", "hook")),
                ("the direct-channel cap",
                 (str(antiphon.MAX_CHANNEL_BYTES // 1024), "KiB")),
                ("the page horizon",
                 ("older", "than", str(antiphon.PAGE_HORIZON // 3600), "hours")),
                ("the new-reader lookback",
                 (str(antiphon.LOOKBACK // 3600), "hours", "back"))):
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
        # The two bullets that were open are what shipped: a read receipt from
        # the peer's transcript, collection an hour after it, expiry unread
        # that the sender hears about, and reuse on resend.
        self.assertEqual(antiphon.ATTACHMENT_READ_GRACE, 3600)
        self.assertRegex(limits, r"(?i)read\s+receipt", "the receipt is named")
        self.assertRegex(limits, r"(?i)one\s+hour\s+after\s+the\s+read",
                         "and the grace after it")
        # The recipient — the party whose read starts that clock — is told the
        # same by the envelope it opens and by the rule it works under.
        envelope = antiphon.attachment_envelope("/p/x.txt", "0" * 64, 5, "ui")
        self.assertIn("1 hour after this bridge sees it read", envelope)
        for rule in (antiphon.AGENTS_RULE, antiphon.CLAUDE_RULE):
            self.assertIn("1 hour after the bridge sees it read", rule)
        self.assertRegex(limits, r"(?i)expires\s+unread", "expiry without one")
        self.assertRegex(limits, r"(?i)hears\s+that\s+on\s+its\s+next\s+page",
                         "reaches the sender")
        self.assertRegex(limits, r"(?i)reused", "and a resend reuses")
        self.assertNotRegex(limits, r"(?i)acknowledgement[^.]*not part of this",
                            "the old disclaimer is gone")

    def test_every_surface_teaches_same_vendor_bridging(self):
        """A Claude session reaches another Claude session and a Codex
        session another Codex session, always by name; every surface says the
        form, the label and the two refusals, and none promises the passive
        page carries it."""
        readme = read("README.md")
        for row in ("| Claude → Claude | `@claude:api` | `reply_to_claude(to=\"api\", text=…)` |",
                    "| Codex → Codex | `@codex:review` | `antiphon_send(kind=\"codex\", "
                    "to=\"review\", text=…)` |",
                    "**Same-vendor.**", "`[Antiphon bridge] Codex:`", "`sender=\"claude\"`"):
            self.assertIn(row, readme, row)
        self.assertRegex(readme, r"(?i)bare\s+same-kind\s+line[^.]*refused")
        # Review 2026-09-03: "never on the passive page" was false — a Stop
        # marker is the sender's own reply and the other kind's page shows
        # that reply; a same-kind tool call's arguments stay retrievable by
        # id. The words say what the code does: no same-kind lane, and no
        # confidentiality.
        self.assertRegex(readme, r"(?i)passive pull page gains no same-kind lane")
        self.assertRegex(readme, r"(?i)addressed,\s+not\s+confidential")
        self.assertRegex(readme, r"(?i)retrievable by (their|its) public id")
        self.assertNotRegex(readme, r"(?i)never on the passive page")
        self.assertRegex(section(readme, "Limits"), r"(?i)same-vendor message")
        self.assertIn("`antiphon_send(kind=\"codex\", to=name)`", antiphon.AGENTS_RULE)
        self.assertIn("`[Antiphon bridge] Codex:`", antiphon.AGENTS_RULE)
        self.assertIn("`reply_to_claude(to=…)`", antiphon.CLAUDE_RULE)
        self.assertIn("`sender=\"claude\"`", antiphon.CLAUDE_RULE)
        node = read("lib", "channel.mjs")
        collapsed = re.sub(r'"\s*\+\s*\n\s*"', "", node)
        for surface in (antiphon.AGENTS_RULE, antiphon.CLAUDE_RULE, collapsed):
            self.assertIn("always named, not a lane of the passive page", surface)
            self.assertNotIn("never on the passive page", surface)
        self.assertIn("reply_to_claude(to=…)", collapsed)
        self.assertIn('An event with sender=\\"claude\\" is another Claude session', collapsed)
        self.assertIn("a same-kind message to nobody in particular has no meaning", collapsed)
        kind = next(t for t in antiphon.TOOLS if t["name"] == "antiphon_send")
        self.assertIn("another Codex session", kind["inputSchema"]["properties"]["kind"]["description"])
        self.assertIn("## P1 — Same-vendor bridging: Codex ↔ Codex and Claude ↔ Claude "
                      "(shipped 2026-09-03)", read("BACKLOG.md"))

    def test_every_surface_says_queued_and_names_the_receipt(self):
        """Measured 2026-09-01: the tool said "delivered" and the peer had
        received nothing. The rules, the channel instructions, the tool
        description and the README say what a result means — queued for
        Codex, delivered to Claude's channel — and where the receipt is."""
        self.assertIn("Its result says queued, never delivered", antiphon.CLAUDE_RULE)
        self.assertIn("`antiphon status` shows the receipt", antiphon.CLAUDE_RULE)
        self.assertIn("names the delivery id", antiphon.AGENTS_RULE)
        self.assertIn("`antiphon status` shows when Claude's transcript received it",
                      antiphon.AGENTS_RULE)
        node = read("lib", "channel.mjs")
        collapsed = re.sub(r'"\s*\+\s*\n\s*"', "", node)
        self.assertIn("Its result says queued, never delivered", collapsed)
        self.assertIn("The result says queued, never delivered.", collapsed)
        self.assertNotIn("Channel reply delivered", collapsed,
                         "the old success sentence is gone from the server")
        readme = read("README.md")
        self.assertRegex(readme, r"`reply_to_codex` answers\s+\*queued\*")
        self.assertIn(".antiphon/deliveries/<id>.json", readme)
        self.assertRegex(section(readme, "Limits"),
                         r"(?i)a tool result is a statement about the transport")
        self.assertRegex(section(readme, "Limits"), r"(?i)last unanswered sender")
        backlog = read("BACKLOG.md")
        self.assertIn("## P1 — `reply_to_codex` can report success while the peer "
                      "receives nothing (fixed)", backlog)
        self.assertIn("## P2 — Reply correlation (closed 2026-09-03: advice inside "
                      "the refusal)", backlog)
        self.assertIn("### What shipped (2026-09-03)", backlog)

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

    def test_every_agent_surface_teaches_the_same_multiline_marker_contract(self):
        channel = re.sub(r'"\s*\+\s*\n\s*"', "", read("lib", "channel.mjs"))
        surfaces = {
            "README": read("README.md"),
            "AGENTS rule": antiphon.AGENTS_RULE,
            "CLAUDE rule": antiphon.CLAUDE_RULE,
            "channel instructions": channel,
        }
        for where, text in surfaces.items():
            self.assertIn("<<TOKEN", text, where)
            self.assertIn("[A-Z][A-Z0-9_]{0,31}", text, where)
            self.assertRegex(text, r"(?i)exact[^.]*TOKEN[^.]*line[^.]*close", where)
            self.assertRegex(text, r"(?i)do not nest|not nestable", where)
            self.assertRegex(text, r"(?i)not[^.]*fence-aware", where)
            self.assertRegex(text, r"(?i)token absent from the body", where)
            self.assertRegex(
                text, r"(?i)malformed or unclosed[^.]*nothing[^.]*turn", where)
            self.assertRegex(
                text, r"(?i)literal text beginning with `?<<`?[^.]*block\s+body",
                where)
            self.assertRegex(
                text,
                r"(?i)oversized[^.]*Stop-marker block[^.]*refused[^.]*not parked",
                where,
            )
        self.assertRegex(surfaces["README"],
                         r"(?i)direct tools[^.]*long\s+content")
        self.assertRegex(surfaces["AGENTS rule"],
                         r"(?i)antiphon_send[^.]*long\s+content")
        for where in ("CLAUDE rule", "channel instructions"):
            self.assertRegex(surfaces[where],
                             r"(?i)reply_to_codex[^.]*long\s+content", where)
        self.assertIn("@claude[:name]", surfaces["README"])
        self.assertIn("@codex[:name]", surfaces["README"])

    def test_paged_context_surfaces_teach_has_more(self):
        """An agent that has not been told a page is one of several will treat
        the first page as the whole answer. Both the tool description and the
        agent instructions teach the loop. A false value proves the durable
        catalog only when discovery is complete; building or degraded is an
        explicitly incomplete boundary."""
        tool = next(t for t in antiphon.TOOLS if t["name"] == "antiphon_read")
        surfaces = (("the antiphon_read description", tool["description"]),
                    ("AGENTS_RULE", antiphon.AGENTS_RULE),
                    ("CLAUDE_RULE", antiphon.CLAUDE_RULE))
        for name, text in surfaces:
            self.assertIn("has_more", text, name)
            self.assertRegex(text, r"(?i)one page|a single page", name)
            self.assertIn("has_more_scope", text, name)
            self.assertRegex(text, r"(?i)catalogued project sources", name)
            self.assertRegex(text, r"(?i)building", name)
            self.assertRegex(text, r"(?i)degraded", name)
            self.assertRegex(text, r"(?i)incomplete", name)
            self.assertRegex(text, r"(?i)drain", name)
        for name, text in surfaces[:2]:
            self.assertRegex(text, r"(?i)again", name)
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
                ("unavailable tool results", r"(?i)tool results? remain unavailable"),
                ("backward paging", r"(?i)backward"),
                ("catalog failure visibility", r"(?i)building|degraded"),
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
        self.assertRegex(limits, r"(?i)exactly\s+three\s+fixed\s+explanation",
                         "the replay reasons are a closed set, and the README "
                         "says so rather than leaving the set open")
        self.assertRegex(limits, r"(?i)legacy\s+upgrade", "reason one, named")
        self.assertRegex(limits, r"(?i)cursor\s+recovery", "reason two, named")
        self.assertRegex(limits, r"(?i)anchor\s+(upgrade|adoption)",
                         "reason three, named")
        self.assertRegex(limits, r"(?i)(malformed|unreadable)[^.]*byte\s+zero",
                         "a malformed existing cursor replays; it is not a "
                         "fresh install")
        self.assertRegex(limits, r"(?i)missing\s+cursor[^.]*new\s+side",
                         "only a genuinely missing cursor means a new side")
        self.assertRegex(limits, r"(?i)(adopt[^.]*valid\s+v3\s+frontier|"
                                 r"valid\s+v3\s+frontier[^.]*adopt)",
                         "the v4 migration contract names bounded adoption")
        self.assertNotRegex(antiphon.offset_at_or_after.__doc__,
                            r"(?i)migrat",
                            "the helper's docstring described the rejected "
                            "boundary-migration model once already")
        self.assertRegex(limits, r"(?i)host.s own\s+lifecycle",
                         "spill files follow the host lifecycle, and the "
                         "README says whose files they are")
        backlog = read("BACKLOG.md")
        for gap, pattern in (
                ("stable invocation id", r"(?i)stable\s+(event\s+id|tool\s+invocation)"),
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
        open_phase = capture(
            r"(?ms)^### Still open, by name\s*$(.*?)(?=^### |\Z)",
            still_open)
        self.assertIsNotNone(open_phase, "the open P0 sub-ledger is gone")
        self.assertNotRegex(open_phase, r"(?i)durable\s+source\s+catalog|"
                             r"degraded-discovery|descriptor-safe",
                            "Wave 1A work is recorded as completed, not open")
        self.assertRegex(
            backlog,
            r"## P1 — Large direct-message attachments[^\n]*"
            r"acknowledgement and retry closed 2026-09-03",
            "and the entry that closed it names what it left open, and the "
            "day that closed too")
        self.assertRegex(backlog, r"(?i)### Closed 2026-09-03, with the delivery ledger",
                         "the two open items close in writing, not by deletion")
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

    def test_the_operational_ledger_matches_the_candidate_not_a_past_release(self):
        """Operational prose is itself a diagnostic contract.

        The old ledger simultaneously called delivered work future, gave
        status a weaker definition than doctor, and narrated a candidate fix
        as though it were present in published 0.3.3. Pin the distinctions a
        release reader needs rather than allowing another chronology rewrite.
        """
        backlog = read("BACKLOG.md")
        doctor = section(backlog, "Shipped — `antiphon doctor`")
        self.assertIsNotNone(doctor)
        self.assertRegex(doctor, r"(?i)default `antiphon doctor`[^.]*read-only")
        self.assertRegex(
            doctor,
            r"(?i)`antiphon doctor --fix`[^.]*project\s+configuration only")
        self.assertRegex(
            doctor,
            r"(?i)`status`[^.]*same[^.]*content-free[^.]*probe")
        self.assertNotIn("Doctor is authoritative over `status`", doctor)
        self.assertRegex(
            doctor,
            r"(?is)thread-writer lock.*queue.*read-only.*supported")
        self.assertRegex(
            doctor,
            r"(?is)active Codex reachability.*declined.*bounded.*non-spawning")
        self.assertNotIn("Whether Codex actually *forwards*", doctor)

        identity = section(
            backlog,
            "P0 — A named Claude session can identify itself as `<unnamed>` (fixed)")
        self.assertIsNotNone(identity)
        self.assertRegex(identity, r"Published 0\.3\.3 still")
        self.assertRegex(
            identity, r"candidate\s+branch[^.]*a4533d1[^.]*6902546")
        self.assertRegex(identity, r"(?i)doctor[^.]*dead-pid endpoint")
        self.assertRegex(
            identity,
            r"(?is)stdin close.*wrapper-forwarded signals.*fixed.*"
            r"abrupt.*SIGKILL.*remain")

        source_labels = section(
            backlog, "P1 — Source-aware multi-peer pull context (fixed)")
        self.assertIsNotNone(source_labels)
        self.assertIn("record_claude_session", source_labels)
        self.assertIn("Labels are per record block", source_labels)
        self.assertNotIn("there is no Claude-side writer", backlog)
        self.assertNotIn("read_session` has no production caller", backlog)
        self.assertNotRegex(
            backlog,
            r"(?i)relayed label should carry[^.]*alias[^.]*deferred")

        wrapper = section(backlog, "P1 — Re-run the host wrapper census before release")
        self.assertIsNotNone(wrapper)
        self.assertIn("test/host_wrapper_census.py", wrapper)
        self.assertRegex(wrapper, r"(?i)aggregate counts only")
        self.assertRegex(wrapper, r"Codex user messages")
        self.assertNotRegex(wrapper, r"Codex user blocks")
        self.assertRegex(
            doctor, r"(?is)default read-only command.*configuration repair")

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

    def test_every_agent_surface_teaches_automatic_identity_limits(self):
        """Automatic identity is public only after host-specific positive proof;
        every surface keeps the pre-hook window and privacy boundary honest."""
        channel = re.sub(r'"\s*\+\s*\n\s*"', "",
                         read("lib", "channel.mjs"))
        surfaces = {
            "README": read("README.md"),
            "AGENTS rule": antiphon.AGENTS_RULE,
            "CLAUDE rule": antiphon.CLAUDE_RULE,
            "channel instructions": channel,
        }
        for where, text in surfaces.items():
            self.assertRegex(text, r"(?i)automatic `auto-` peer alias", where)
            self.assertRegex(text, r"(?i)at least[^.]*first hook", where)
            self.assertRegex(text, r"(?i)fixed Claude probe", where)
            self.assertRegex(text, r"(?i)host display name is ignored", where)
            # Widened with the privacy sentence: the owner key and socket
            # route are now named private too, and the surfaces bound by that
            # promise are named beside it.
            self.assertRegex(
                text,
                r"(?i)identity digest, owner key and socket route stay private",
                where)
            self.assertRegex(text, r"(?i)expose only the public alias", where)
            self.assertIn("ANTIPHON_NAME", text, where)
            self.assertRegex(text, r"(?i)(two or more|multiple)[^.]*refus", where)

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


class IdentityPrivacyContractTest(unittest.TestCase):
    """Every surface that teaches the automatic identity says what stays
    private, in the same words, so a reader cannot learn a weaker rule from
    whichever one they happen to open."""

    def test_identity_privacy_every_written_surface_agrees(self):
        node = read("lib", "channel.mjs")
        start = node.index("    instructions:")
        end = node.index("\n  },\n);", start)
        channel = re.sub(r'"\s*\+\s*\n\s*"', "", node[start:end])
        surfaces = {
            "README": read("README.md"),
            "AGENTS rule": antiphon.AGENTS_RULE,
            "CLAUDE rule": antiphon.CLAUDE_RULE,
            "channel instructions": channel,
        }
        for where, text in surfaces.items():
            with self.subTest(surface=where):
                words = " ".join(text.split())
                for required in ("identity digest", "owner key",
                                 "socket route", "public alias"):
                    self.assertIn(required, words, f"{where}: {required}")
                self.assertRegex(
                    words, r"(?i)refusals[^.]*errors|errors[^.]*refusals",
                    f"{where}: error paths are covered, not only refusals")

    def test_reconnect_notice_every_written_surface_states_the_cost(self):
        """There is no dynamic rename of a live listener, so a rotation costs a
        person a reconnect. An agent that reads only "the alias moved" will keep
        addressing a name nothing answers; the remedy has to travel with it, in
        the same words `status` and `doctor` use."""
        node = read("lib", "channel.mjs")
        start = node.index("    instructions:")
        end = node.index("\n  },\n);", start)
        channel = re.sub(r'"\s*\+\s*\n\s*"', "", node[start:end])
        surfaces = {
            "README": read("README.md"),
            "AGENTS rule": antiphon.AGENTS_RULE,
            "CLAUDE rule": antiphon.CLAUDE_RULE,
            "channel instructions": channel,
        }
        for where, text in surfaces.items():
            with self.subTest(surface=where):
                words = " ".join(text.split())
                self.assertRegex(words, r"(?i)until a fresh endpoint exists",
                                 f"{where}: the window has a stated end")
                self.assertRegex(words, r"(?i)reconnect",
                                 f"{where}: the remedy is named")
                self.assertRegex(words, r"(?i)counted, never addressed",
                                 f"{where}: an unprovable owner fails closed")

    def test_proof_lifecycle_backlog_bounds_the_sweep_s_degradation(self):
        """The sweep swallows a cursor-write failure, which is right — the
        rotation has already committed and must not fail over housekeeping.
        But a *persistent* failure silently reduces the guarantee from "the
        whole inventory in a finite number of writes" to "the first eight
        records, forever". An unwritten limitation reads as a bug later."""
        words = " ".join(read("BACKLOG.md").split())
        self.assertIn("If that cursor can never be written", words)
        self.assertRegex(words, r"(?i)first eight")

    def test_identity_privacy_backlog_names_what_is_deliberately_not_redacted(self):
        """A blanket promise would be the wrong one, and an unwritten exception
        reads as an oversight later. Two shapes stay visible on purpose: an
        explicitly named peer's socket path, which is what makes `remove it`
        actionable for the operator who chose that name, and the channel's own
        readiness line, which is neither a refusal nor an error."""
        words = " ".join(read("BACKLOG.md").split())
        self.assertIn("Redaction is scoped, and its two exceptions are "
                      "deliberate", words)
        self.assertRegex(words, r"(?i)explicitly named peer keeps its socket "
                                r"path")
        self.assertRegex(words, r"(?i)`antiphon channel ready:` line")

    def test_every_node_error_surface_redacts_before_it_truncates(self):
        """Node prints and returns its own refusals and never crosses back into
        Python to have them cleaned. Three surfaces carried arbitrary detail —
        the retrieval bridge's stderr, the reply bridge's stderr, and whatever
        went wrong on the way to an emission — and two of them cut to 500
        characters first, which can leave half a session id behind where a
        whole-shape check finds nothing to remove."""
        node = read("lib", "channel.mjs")
        surfaces = [m.start() for m in
                    re.finditer(r"String\(error\?\.(?:stderr|message)", node)]
        self.assertGreaterEqual(len(surfaces), 3,
                                "the error surfaces are still here")
        for at in surfaces:
            # `redactPrivate(` wraps the expression, so it precedes it.
            self.assertIn("redactPrivate", node[max(0, at - 160):at],
                          "unredacted error surface at "
                          f"{node[at:at + 70]!r}")
        # And no surface cuts before it redacts.
        self.assertNotIn(".trim().slice(0, 500)", node)
        self.assertNotIn("detail.slice(0, 500)", node)
        # `error?.code || error` falls through to the whole error whenever
        # there is no code, and a message can carry a path, a session id or an
        # owner key. Every such interpolation goes through one helper.
        self.assertNotIn("error?.code || error", node,
                         "an uncoded error must not be interpolated raw")
        self.assertIn("function errorCode(", node)

    def test_the_proof_diagnosis_names_a_directory_a_person_can_find(self):
        """Doctor says a proof could not be read or could not be trusted, and
        correctly prints no path — the filename is a digest and the contract
        keeps it private. But a diagnosis whose subject appears nowhere a
        person would look is a diagnosis with no action behind it, so the
        directory is named once where the layout is described."""
        readme = read("README.md")
        self.assertIn(".antiphon/identity/claude/", readme)
        for verdict in ("could not be read", "cannot be trusted"):
            self.assertIn(verdict, antiphon._VERDICT_NOTE["UNKNOWN"]
                          + antiphon._VERDICT_NOTE["STRUCTURAL_INVALID"],
                          verdict)

    def test_the_shared_verdict_has_exactly_one_consumer(self):
        """Two consumers of one predicate are two predicates.

        Routing and signing must reach the same answer about the same moment,
        and they drifted the moment a rule was added to one wrapper and not the
        other: the fail-closed for a missing fingerprint lived in the inbound
        gate alone, so a listener refused everything sent to it and went on
        signing replies with an alias it could no longer prove was its own.
        Everything goes through `deliveryVerdict`, and the shared function is
        called in exactly one place.
        """
        node = read("lib", "channel.mjs")
        body = node[node.index("function automaticProofVerdict()"):]
        after = node[:node.index("function automaticProofVerdict()")] \
            + body[body.index("}") + 1:]
        self.assertEqual(after.count("automaticProofVerdict()"), 1,
                         "one call site, and it is inside `deliveryVerdict`")
        gate = node[node.index("function deliveryVerdict()"):]
        self.assertIn("automaticProofVerdict()",
                      gate[:gate.index("\n}")],
                      "which is where the one call site is")

    def test_the_birth_authority_never_comes_from_the_record_it_judges(self):
        """A listener that learns what it published by re-reading its own
        endpoint has no authority at all: the same bytes anyone could have
        changed answer both questions, and the comparison that follows always
        agrees with itself. The fingerprint comes from the register operation's
        own return instead.

        Pinned on the source, and the reason is that the difference is only
        observable inside the window between the claim returning and a
        read-back — a race a test cannot hold open without a production seam
        put there for it. What *is* measured behaviourally is the fail-closed
        beside it: `channel.test.mjs` drives a registry whose claim succeeds
        and returns no fingerprint, and the listener refuses.
        """
        node = read("lib", "channel.mjs")
        claim = node[node.index('if (subcommand === "register_peer")'):]
        claim = claim[:claim.index("return true;")]
        self.assertIn("JSON.parse(String(stdout)", claim,
                      "the fingerprint comes from the operation's return")
        self.assertNotIn("endpoint.json", claim,
                         "and never from the record the verdict will judge")
        python = read("lib", "antiphon.py")
        register = python[python.index("def register_peer("):]
        register = register[:register.index("\ndef ")]
        self.assertIn("register_claim(", register,
                      "the claim is the operation that returns it")
        self.assertIn('json.dumps({"birth": fingerprint', register,
                      "and the operation's own return is what is answered")
        self.assertNotIn("_process_birth(", register,
                         "never a second observation")

    def test_the_two_readers_select_the_fingerprint_the_same_way(self):
        """One selector per language, one grammar compared at runtime — never
        grepped from source, where Python's constant spans two adjacent
        literals — precedence by key presence, and the Node form sees the
        lexical scan."""
        node = read("lib", "identity.mjs")
        self.assertIn("export function fingerprintOf(wrapper)", node)
        self.assertIn('Object.hasOwn(record, "process_birth")', node)
        self.assertIn('spelledFractional(wrapper, "birth_version")', node)
        python = read("lib", "peers.py")
        self.assertIn("def _fingerprint_of(record)", python)
        self.assertIn('"process_birth" in record', python)
        self.assertNotIn("def _birth_of(", python)
        exported = json.loads(subprocess.run(
            ["node", "--input-type=module", "-e",
             'import * as m from "./lib/identity.mjs";'
             'process.stdout.write(JSON.stringify({'
             'grammar: m.CANONICAL_START, weekdays: m.WEEKDAYS, months: m.MONTHS,'
             'version: m.PROCESS_FINGERPRINT_VERSION,'
             'generation: m.GENERATION_TOKEN,'
             'ceiling: m.INTEGER_TOKEN_CEILING}));'],
            capture_output=True, text=True, cwd=ROOT, check=True).stdout)
        self.assertEqual(exported["grammar"], antiphon.peers.CANONICAL_START)
        self.assertEqual(exported["weekdays"], list(antiphon.peers.WEEKDAYS))
        self.assertEqual(exported["months"], list(antiphon.peers.MONTHS))
        self.assertEqual(exported["version"],
                         antiphon.peers.PROCESS_FINGERPRINT_VERSION)
        self.assertEqual(exported["ceiling"],
                         antiphon.peers.INTEGER_TOKEN_CEILING)
        # The generation token too: a positive integer with no leading zero,
        # anchored at the start on both sides. It was the one selector
        # constant not compared here, so `v0:` could have parted the readers.
        self.assertEqual(exported["generation"], antiphon.peers.GENERATION_TOKEN)
        self.assertEqual(antiphon.peers._GENERATION.pattern,
                         antiphon.peers.GENERATION_TOKEN)
        channel = read("lib", "channel.mjs")
        self.assertIn('fingerprint_field: "process_birth"', channel)
        self.assertIn('answer?.fingerprint_field !== "process_birth"', channel)
        register = read("lib", "antiphon.py")
        register = register[register.index("def register_peer("):]
        register = register[:register.index("\ndef ")]
        self.assertIn('"fingerprint_field": "process_birth"', register)

    def test_the_agent_surfaces_stay_small(self):
        """Every byte here is paid on every turn (the rules) or every session
        (the instructions), and the host truncates a long instructions
        string. Measured before the rewrite: 7,354 / 8,120 / ~6,000 bytes;
        after it 4,740 / 5,338 / 3,183. The contract facts the tests around
        this one pin fill roughly 4.5 KB on their own, so the ceilings sit
        just above the rewrite rather than at the 3,000 the campaign aimed
        for; they exist so the surfaces cannot regrow unnoticed. The
        instructions are measured as the collapsed source slice, a few bytes
        over the string itself (quotes and escapes)."""
        node = read("lib", "channel.mjs")
        start = node.index("    instructions:")
        end = node.index("\n  },\n);", start)
        channel = re.sub(r'"\s*\+\s*\n\s*"', "", node[start:end])
        # +100 each on 2026-09-03 for the read-grace clause the review asked
        # for ("or 1 hour after the bridge sees it read"): the recipient is the
        # party whose read starts that clock, and it was being told 7 days.
        # +200 each on 2026-09-03 for the same-vendor sentence: the one road
        # the rules did not know existed.
        for where, text, ceiling in (("CLAUDE_RULE", antiphon.CLAUDE_RULE, 5_300),
                                     ("AGENTS_RULE", antiphon.AGENTS_RULE, 5_800),
                                     ("channel instructions", channel, 3_500)):
            self.assertLessEqual(len(text.encode("utf-8")), ceiling,
                                 f"{where}: {len(text.encode('utf-8'))} bytes")

    def test_the_token_entry_names_what_it_measured_and_what_it_reversed(self):
        """The horizon reverses one documented sentence — repeat, never skip
        — for records beyond it, and the entry has to say so beside the
        measurement, or the next reader files the skip as a bug."""
        entry = section(read("BACKLOG.md"),
                        "P1 — Token cost of the passive page and the static surfaces (fixed)")
        self.assertIsNotNone(entry, "the token entry is gone")
        for phrase in ("skipped:", "24 hours", "external_agent_tool_call",
                       "isCompactSummary", "codex_internal_context",
                       "AGENTS.md instructions for", "Request interrupted by user",
                       "more than 400 pages", "20 pages",
                       "never delivers a record older than", "reverses",
                       "antiphon catch-up", "ceilings pinned",
                       "section is missing or differs"):
            self.assertIn(phrase, entry, phrase)
        self.assertEqual(antiphon.PAGE_HORIZON // 3600, 24,
                         "the entry's 24 hours is the constant's")
        limits = section(read("README.md"), "Limits")
        self.assertRegex(limits, r"(?i)skipped:", "README §Limits names the skip line")
        self.assertRegex(read("README.md"), r"(?i)never rendered as either agent's speech")

    def test_the_socket_path_is_never_unlinked_after_a_close(self):
        """`close()` removes the socket file the server bound. An explicit
        unlink after it is a second removal with an await in front of it, and
        a successor can bind the path inside that gap — which is how one
        session came to delete another's live socket, the failure
        `owningSocket` was added to prevent, reached through the guard itself.

        Pinned on the source rather than on behaviour, and deliberately so:
        the property *is* about the source — this call must not appear in these
        places. Two behavioural attempts are recorded here because they looked
        right and measured nothing. Binding a successor after the process has
        exited never opens the window; and a second channel started against a
        held path refuses at the liveness check and never reaches the branch
        under test at all. Reproducing either needs a production hook that
        holds a process between a close and an unlink, which is a seam this
        contract does not otherwise want.
        """
        node = read("lib", "channel.mjs")
        self.assertEqual(node.count("unlink(socketPath)"), 1,
                         "exactly one removal, and it is the pre-bind clear")
        clear = node.index("await unlink(socketPath);")
        serve = node.index("async function serveSocket()")
        self.assertLess(serve, clear,
                        "the one removal is inside `serveSocket`, before the "
                        "bind — nothing removes the path after a close")
        for after in ("close(resolve));\n    await unlink",
                      "close(resolve));\n  await unlink",
                      "socketServer.close();\n    await unlink"):
            self.assertNotIn(after, node)

    def test_a_retiring_listener_refuses_to_republish_itself(self):
        """Retirement must be the last registry mutation a process makes.

        It sets `retiring`, closes the server and then releases the endpoint —
        and the release is a subprocess, so a connection accepted beforehand
        can still ask for a reassert while it runs. Without the flag in that
        branch the answer re-creates the record naming a listener on its way
        out.

        The ordering half is measured behaviourally: `channel.test.mjs` holds
        the release open through the production registry seam and asserts that
        nothing can connect inside the window. The flag is a different matter,
        and the distinction is worth stating rather than hiding. Retirement now
        destroys open connections and closes the server *before* the release,
        so no request can reach the control branch at all while `retiring` is
        true — which means no behavioural test can exercise the flag without
        first breaking the ordering that makes it unreachable. It is belt
        against a future reordering, and a pin on the source is the honest
        instrument for a guard that cannot otherwise be reached. Measured: with
        both halves reverted a reassert is answered `reasserted`; with either
        one alone the other masks it.
        """
        node = read("lib", "channel.mjs")
        branch = node[node.index("payload?.control === CHANNEL_CONTROL"):]
        branch = branch[:branch.index("if (typeof payload.content")]
        self.assertIn("shuttingDown || retiring", branch,
                      "the reassert path must refuse while retirement runs")
        # And the flag has to be set before the first await inside retirement,
        # or a reassert already queued behind that turn would not see it.
        retire = node[node.index("async function retireSelf()"):]
        retire = retire[:retire.index("\n}")]
        # Comments first: this one talks about awaiting, and a substring search
        # that reads prose is a search that measures the prose.
        code = "\n".join(line for line in retire.splitlines()
                         if not line.strip().startswith("//"))
        self.assertLess(code.index("retiring = true"), code.index("await "),
                        "set before anything yields")

    def test_the_lockfile_agrees_with_the_package_it_locks(self):
        """A version lives in two tracked files, and one of them is easy to
        forget: `npm version` writes both, a hand-edit writes one. The lockfile
        is not published, so nothing about npm catches the drift — but
        `npm ci` installs from it, and a repository that states two versions of
        itself is one nobody can reason about.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            declared = json.load(f)["version"]
        with open(os.path.join(root, "package-lock.json"), encoding="utf-8") as f:
            lock = json.load(f)
        self.assertEqual(lock.get("version"), declared,
                         "the lockfile's own version")
        self.assertEqual(lock.get("packages", {}).get("", {}).get("version"),
                         declared, "and the root package it records")
        self.assertEqual(lock.get("name"), declared and "antiphon")

    def test_identity_proof_has_one_validator_on_each_side(self):
        """The verdict reads the proof twice — once before it observes the
        halves, once after, to close the window a rotation can land in. Two
        readers of one file was already the hazard this whole module exists to
        manage; two *validators* inside one reader is the same hazard at a
        smaller scale, and it happened: the second read was parse-plus-session-id
        while the first was total, so a malformed proof carrying the same
        session id could authorise a retirement on one side alone.
        """
        node = read("lib", "identity.mjs")
        self.assertEqual(node.count("function readIdentityProof("), 1,
                         "one validator")
        # Its definition plus the two call sites.
        self.assertEqual(node.count("readIdentityProof(projectDir"), 3,
                         "and both reads go through it")
        # Nothing may reach the proof file except that validator.
        self.assertEqual(
            node.count('".antiphon", "identity", "claude"'), 1,
            "no second path to the proof file")
        python = read("lib", "peers.py")
        self.assertEqual(python.count("def _read_identity_proof_file("), 1)
        self.assertEqual(python.count("def read_identity_proof("), 1)

    def test_reconnect_notice_backlog_records_the_accepted_cost(self):
        """A live listener is never renamed, so a rotation costs a person a
        reconnect. That is a decision with a price, not an oversight, and the
        ledger has to carry the price beside the decision or the next reader
        will file it as a bug to fix."""
        words = " ".join(read("BACKLOG.md").split())
        self.assertIn("There is no dynamic rename of a live listener", words)
        self.assertRegex(words, r"(?i)until a fresh endpoint exists")
        self.assertRegex(words, r"(?i)counted, never addressed")
