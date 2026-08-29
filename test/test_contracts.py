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

import contextlib
import io
import json
import re
import tempfile
import unittest
from unittest.mock import patch

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
        self.assertIn(antiphon.TOOLS[0]["name"], mentioned)


if __name__ == "__main__":
    unittest.main()
