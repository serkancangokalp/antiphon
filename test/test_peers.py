import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import peers

import hashlib
import json
import multiprocessing
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class PeerNameTest(unittest.TestCase):
    def test_explicit_name_is_read_from_the_environment_and_lowercased(self):
        with patch.dict(os.environ, {"ANTIPHON_NAME": "  UI  "}):
            self.assertEqual(peers.explicit_name(), "ui")

    def test_explicit_name_is_empty_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(peers.explicit_name(), "")

    def test_auto_name_uses_three_hex_characters_of_the_session_id(self):
        self.assertEqual(peers.auto_name("claude", "a3f0b1c2-dead"), "claude-a3f")

    def test_auto_name_survives_a_missing_session_id(self):
        self.assertEqual(peers.auto_name("codex", ""), "codex-000")

    def test_valid_name_accepts_what_the_spec_allows(self):
        for name in ("ui", "api-2", "a", "x" * 32):
            self.assertTrue(peers.valid_name(name), name)

    def test_valid_name_rejects_what_would_break_a_file_name(self):
        for name in ("", "-ui", "UI", "a/b", "a.b", "x" * 33, None):
            self.assertFalse(peers.valid_name(name), repr(name))

    def test_valid_name_rejects_a_trailing_newline(self):
        """`re.match` with `$` accepts a final newline, so `ui\n` passed and would
        have become a file name and a socket seed carrying a line break."""
        for name in ("ui\n", "ui\n\n", "a/b\n"):
            self.assertFalse(peers.valid_name(name), repr(name))

    def test_valid_kind_allows_only_the_two_sides(self):
        """`kind` is concatenated into a directory name. Unvalidated, `../..`
        walks out of the project."""
        self.assertTrue(peers.valid_kind("claude"))
        self.assertTrue(peers.valid_kind("codex"))
        for kind in ("", None, "../..", "Claude", "claude\n"):
            self.assertFalse(peers.valid_kind(kind), repr(kind))

    def test_an_unnamed_peer_keeps_todays_socket_key(self):
        """The backward-compatibility contract: an unnamed session must land on
        exactly the socket path it lands on today, or every existing install
        loses its channel on upgrade."""
        cwd = "/Users/me/dev/project"
        today = hashlib.sha256(os.path.abspath(cwd).encode()).hexdigest()[:20]
        self.assertEqual(peers.socket_key(cwd), today)

    def test_named_peers_get_different_keys_from_each_other_and_from_unnamed(self):
        cwd = "/Users/me/dev/project"
        bare, ui, api = (peers.socket_key(cwd), peers.socket_key(cwd, "ui"),
                         peers.socket_key(cwd, "api"))
        self.assertEqual(len({bare, ui, api}), 3)

    def test_socket_key_length_does_not_grow_with_the_name(self):
        """macOS caps a Unix socket path near 104 bytes and TMPDIR already spends
        much of it, so the key is hashed rather than appended."""
        cwd = "/Users/me/dev/project"
        self.assertEqual(len(peers.socket_key(cwd, "x" * 32)), 20)


def _claim_address(project, barrier, results):
    """Two processes, different names, one address — the shape of the real race.

    Both hold at the barrier, claim together, and hold again so neither can look
    dead to the other while the second is still deciding.
    """
    barrier.wait()
    ok, _ = peers.register(project, "claude", f"peer-{os.getpid()}", "/tmp/shared.sock",
                           pid=os.getpid())
    results.append(ok)
    barrier.wait()


def _claim(project, barrier, results):
    """Runs in a separate process; must be importable at module level for spawn.

    The second `wait` is load-bearing. If the winner exited as soon as it had
    registered, the loser could find the recorded pid gone by the time it took
    the lock and would win as well — the test would then pass or fail on timing
    rather than on the lock working.
    """
    barrier.wait()
    ok, _ = peers.register(project, "claude", "ui", f"/tmp/{os.getpid()}.sock",
                           pid=os.getpid())
    results.append(ok)
    barrier.wait()


class PeerRegistryTest(unittest.TestCase):
    def test_a_registered_peer_can_be_read_back(self):
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "claude", "ui", "/tmp/ui.sock")
            self.assertTrue(ok, detail)
            live = peers.read_peers(project, "claude")
        self.assertEqual([(p["name"], p["address"]) for p in live],
                         [("ui", "/tmp/ui.sock")])

    def test_registering_the_same_name_twice_from_one_process_refreshes_it(self):
        """A session re-registers when its own server restarts; that is not a
        clash with itself."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/old.sock")
            ok, _ = peers.register(project, "claude", "ui", "/tmp/new.sock")
            self.assertTrue(ok)
            live = peers.read_peers(project, "claude")
        self.assertEqual([p["address"] for p in live], ["/tmp/new.sock"])

    def test_a_name_held_by_another_live_process_is_refused(self):
        """Two peers sharing a name make addressing ambiguous in exactly the way
        the registry exists to prevent."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock")
            ok, detail = peers.register(project, "claude", "ui", "/tmp/other.sock",
                                        pid=os.getppid())
        self.assertFalse(ok)
        self.assertIn("ui", detail)

    def test_a_dead_peer_is_pruned_and_its_name_freed(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock", pid=999999)
            with patch.object(peers, "alive", return_value=False):
                self.assertEqual(peers.read_peers(project), [])
            self.assertFalse(os.path.exists(
                os.path.join(peers.peer_dir(project, "claude", "ui"), "endpoint.json")))
            ok, _ = peers.register(project, "claude", "ui", "/tmp/mine.sock")
        self.assertTrue(ok)

    def test_kinds_are_listed_separately(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock")
            peers.register(project, "codex", "build", "sess-1")
            self.assertEqual([p["name"] for p in peers.read_peers(project, "codex")],
                             ["build"])
            self.assertEqual(len(peers.read_peers(project)), 2)

    def test_an_invalid_name_is_refused_with_the_rule_in_the_message(self):
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "claude", "Not Valid", "/tmp/x.sock")
        self.assertFalse(ok)
        self.assertIn("a-z0-9", detail)

    def test_a_corrupt_record_is_ignored_rather_than_crashing_the_bridge(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock")
            broken = peers.peer_dir(project, "claude", "broken")
            os.makedirs(broken)
            with open(os.path.join(broken, "endpoint.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"])

    def test_the_owner_pid_may_differ_from_the_registering_process(self):
        """`channel.mjs` registers through a short-lived Python subprocess. If the
        record carried that subprocess's pid the peer would be pruned as dead the
        moment the call returned."""
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "claude", "ui", "/tmp/ui.sock",
                                        pid=os.getppid())
            self.assertTrue(ok, detail)
            live = peers.read_peers(project, "claude")
        self.assertEqual([p["pid"] for p in live], [os.getppid()])

    def test_a_record_survives_the_process_that_wrote_it(self):
        """The end-to-end shape of the bug above: register from a subprocess that
        then exits, and the peer must still be live."""
        with tempfile.TemporaryDirectory() as project:
            lib = os.path.join(os.path.dirname(__file__), "..", "lib")
            code = (f"import sys; sys.path.insert(0, {lib!r}); import peers; "
                    f"print(peers.register({project!r}, 'claude', 'ui', "
                    f"'/tmp/ui.sock', pid={os.getpid()})[0])")
            done = subprocess.run([sys.executable, "-c", code],
                                  capture_output=True, text=True)
            self.assertEqual(done.stdout.strip(), "True", done.stderr)
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"])

    def test_a_second_live_claimant_is_refused(self):
        with tempfile.TemporaryDirectory() as project:
            first, _ = peers.register(project, "claude", "ui", "/tmp/a.sock",
                                      pid=os.getpid())
            second, detail = peers.register(project, "claude", "ui", "/tmp/b.sock",
                                            pid=os.getppid())
            self.assertTrue(first)
            self.assertFalse(second, "the second live claimant must be refused")
            self.assertIn("already held", detail)

    def test_two_genuinely_simultaneous_claims_produce_one_winner(self):
        """Sequential calls cannot show this. Both processes are held at a barrier
        and released together, so they race the way two terminals started at the
        same moment do."""
        with tempfile.TemporaryDirectory() as project:
            barrier = multiprocessing.Barrier(2, timeout=10)
            results = multiprocessing.Manager().list()
            workers = [multiprocessing.Process(target=_claim,
                                               args=(project, barrier, results))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(20)
            self.assertEqual(sorted(results), [False, True],
                             f"exactly one claimant must win, got {list(results)}")

    def test_a_record_that_is_not_an_object_is_ignored(self):
        """A JSON array where an object was expected used to raise straight out of
        `read_peers` and take the bridge down with it."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock")
            odd = peers.peer_dir(project, "claude", "odd")
            os.makedirs(odd)
            with open(os.path.join(odd, "endpoint.json"), "w", encoding="utf-8") as f:
                f.write("[]")
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"])

    def test_a_record_with_an_unusable_pid_is_ignored_and_frees_its_name(self):
        """A pid that is not a positive integer identifies nobody, so the record
        cannot be trusted and must not block the name forever."""
        with tempfile.TemporaryDirectory() as project:
            broken = peers.peer_dir(project, "claude", "ui")
            os.makedirs(broken)
            for bad in ('"abc"', "0", "-1", "null"):
                with open(os.path.join(broken, "endpoint.json"), "w",
                          encoding="utf-8") as f:
                    f.write('{"kind":"claude","name":"ui","pid":%s,'
                            '"address":"/tmp/x.sock"}' % bad)
                self.assertEqual(peers.read_peers(project), [], bad)
                ok, detail = peers.register(project, "claude", "ui", "/tmp/mine.sock")
                self.assertTrue(ok, f"{bad}: {detail}")
                os.unlink(os.path.join(broken, "endpoint.json"))

    def test_an_unknown_kind_is_refused_rather_than_becoming_a_path(self):
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "../..", "ui", "/tmp/x.sock")
            self.assertFalse(ok)
            self.assertIn("claude", detail)
            self.assertEqual(peers.read_peers(project), [])

    def test_a_name_that_looks_like_a_kind_prefix_gets_its_own_directory(self):
        """The prefix is applied unconditionally, so `ui` and `claude-ui` are two
        different peers with two different directories. Stripping the prefix when
        a name happens to start with the kind is what would collide them."""
        with tempfile.TemporaryDirectory() as project:
            plain, _ = peers.register(project, "claude", "ui", "/tmp/a.sock",
                                      pid=os.getpid())
            prefixed, detail = peers.register(project, "claude", "claude-ui",
                                              "/tmp/b.sock", pid=os.getpid())
            self.assertTrue(plain)
            self.assertTrue(prefixed, detail)
            self.assertNotEqual(peers.peer_dir(project, "claude", "ui"),
                                peers.peer_dir(project, "claude", "claude-ui"))
            self.assertEqual(sorted(p["name"] for p in peers.read_peers(project)),
                             ["claude-ui", "ui"])

    def test_one_address_cannot_be_claimed_by_two_live_peers(self):
        """The socket path is the contended resource, not the name. Two sessions
        that both found the path free would bind it in turn and register under
        different automatic names carrying the same address — the registry would
        then show two peers while a message addressed to either reached whichever
        actually held the socket."""
        with tempfile.TemporaryDirectory() as project:
            first, _ = peers.register(project, "claude", "ui", "/tmp/shared.sock",
                                      pid=os.getpid())
            second, detail = peers.register(project, "claude", "api", "/tmp/shared.sock",
                                            pid=os.getppid())
            self.assertTrue(first)
            self.assertFalse(second, "a second live peer must not share an address")
            self.assertIn("ui", detail, "the reason must name the holder")
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"])

    def test_an_address_freed_by_a_dead_peer_can_be_claimed(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/shared.sock", pid=999999)
            with patch.object(peers, "alive", return_value=False):
                peers.read_peers(project)
            ok, detail = peers.register(project, "claude", "api", "/tmp/shared.sock")
            self.assertTrue(ok, detail)

    def test_the_same_peer_may_refresh_its_own_address(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/shared.sock", pid=os.getpid())
            ok, detail = peers.register(project, "claude", "ui", "/tmp/shared.sock",
                                        pid=os.getpid())
            self.assertTrue(ok, detail)

    def test_different_kinds_may_hold_the_same_address_string(self):
        """A Codex address is a rollout id, not a socket path; the two namespaces
        never collide and must not be made to."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "shared-string")
            ok, detail = peers.register(project, "codex", "build", "shared-string")
            self.assertTrue(ok, detail)

    def test_an_address_must_be_a_non_empty_string(self):
        """An empty address is not a peer. Stored, it made the single-peer
        resolver fall back to the legacy socket without saying so — a misroute
        dressed as a default."""
        with tempfile.TemporaryDirectory() as project:
            for address in ("", "   ", None, 0, ["/tmp/x.sock"]):
                ok, detail = peers.register(project, "claude", "ui", address)
                self.assertFalse(ok, repr(address))
                self.assertIn("address", detail)
            self.assertEqual(peers.read_peers(project), [])

    def test_a_record_without_a_usable_address_is_not_routed_to(self):
        with tempfile.TemporaryDirectory() as project:
            hand_written = peers.peer_dir(project, "claude", "ui")
            os.makedirs(hand_written)
            with open(os.path.join(hand_written, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "claude", "name": "ui", "pid": os.getpid(),
                           "address": ""}, f)
            self.assertEqual(peers.read_peers(project), [])

    def test_records_with_mixed_started_at_types_do_not_crash_the_sort(self):
        """Sorting a float against a string raises, and it would raise inside
        every read of the registry."""
        with tempfile.TemporaryDirectory() as project:
            for name, started in (("ui", 100.0), ("api", "not-a-time"), ("docs", None)):
                directory = peers.peer_dir(project, "claude", name)
                os.makedirs(directory)
                with open(os.path.join(directory, "endpoint.json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"kind": "claude", "name": name, "pid": os.getpid(),
                               "address": f"/tmp/{name}.sock", "started_at": started}, f)
            live = peers.read_peers(project, "claude")
        self.assertEqual(sorted(p["name"] for p in live), ["api", "docs", "ui"])
        self.assertEqual(live[0]["name"], "ui", "a usable timestamp still sorts first")

    def test_two_simultaneous_claims_on_one_address_produce_one_winner(self):
        """The name race and the address race are different races. This is the one
        that actually mattered: two sessions with different automatic names both
        binding the same socket path."""
        with tempfile.TemporaryDirectory() as project:
            barrier = multiprocessing.Barrier(2, timeout=10)
            results = multiprocessing.Manager().list()
            workers = [multiprocessing.Process(target=_claim_address,
                                               args=(project, barrier, results))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(20)
            self.assertEqual(sorted(results), [False, True],
                             f"exactly one claimant must win, got {list(results)}")
            # Both workers have been reaped by now, so `read_peers` correctly sees
            # no live peer. Count the records on disk instead: exactly one claim
            # was ever written.
            written = [name for name in os.listdir(peers.peers_dir(project))
                       if os.path.exists(os.path.join(peers.peers_dir(project), name,
                                                      "endpoint.json"))]
            self.assertEqual(len(written), 1, written)

    def test_unregister_releases_only_your_own_name(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock", pid=os.getpid())
            peers.unregister(project, "claude", "ui", pid=os.getppid())
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"],
                             "another owner's name must not be released")
            peers.unregister(project, "claude", "ui", pid=os.getpid())
            self.assertEqual(peers.read_peers(project), [])


class OwnerKeyTest(unittest.TestCase):
    """The key that lets a hook and a long-lived server on one Codex session
    recognise each other without either being told which session it is."""

    def test_the_key_names_the_cli_root_and_its_own_start_time(self):
        """`_process_info(pid)` returns `(ppid, start, command)` where `start`
        and `command` describe *that* pid; only `ppid` points at its parent.

        Written as three answers so the pid whose command is `codex` is
        unambiguous. Pairing one process's number with another's clock would
        undo the reuse protection while still looking correct — and the shape
        mirrors the real chain measured on this machine,
        `python3 → node → codex`.
        """
        with patch.object(peers, "_process_info", side_effect=[
                ("200", "Sat Aug 30 01:00:02 2026", "python3 antiphon.py mcp"),
                ("300", "Sat Aug 30 01:00:01 2026", "node antiphon mcp"),
                ("1", "Sat Aug 30 01:00:00 2026", "/usr/local/bin/codex")]):
            self.assertEqual(peers.owner_key(100), "300:Sat Aug 30 01:00:00 2026")

    def test_a_claude_root_is_recognised_too(self):
        with patch.object(peers, "_process_info", side_effect=[
                ("200", "Sat Aug 30 01:00:01 2026", "node lib/channel.mjs"),
                ("300", "Sat Aug 30 01:00:00 2026", "/usr/local/bin/claude")]):
            self.assertEqual(peers.owner_key(100), "200:Sat Aug 30 01:00:00 2026")

    def test_an_orphan_has_nothing_to_join_on(self):
        """A server whose parent died reports parent 1 — observed on this
        machine. No key means fall back, never a best guess."""
        with patch.object(peers, "_process_info", side_effect=[
                ("1", "Sat Aug 30 01:00:00 2026", "node lib/channel.mjs")]):
            self.assertIsNone(peers.owner_key(100))

    def test_a_cycle_does_not_hang_the_hook_that_asked(self):
        with patch.object(peers, "_process_info",
                          return_value=("100", "Sat Aug 30 01:00:00 2026", "node x")):
            self.assertIsNone(peers.owner_key(100))

    def test_a_tree_deeper_than_the_limit_gives_up(self):
        answers = [(str(200 + i), "Sat Aug 30 01:00:00 2026", "node x")
                   for i in range(peers.MAX_ANCESTRY + 2)]
        with patch.object(peers, "_process_info", side_effect=answers):
            self.assertIsNone(peers.owner_key(100))

    def test_an_unreadable_process_table_yields_no_key(self):
        with patch.object(peers, "_process_info", return_value=None):
            self.assertIsNone(peers.owner_key(100))

    def test_a_real_ps_line_parses_into_its_three_parts(self):
        """The 24-character slice is `lstart`'s fixed width. Taken from a live
        line on this machine."""
        line = "74544 Sun Aug 30 05:20:57 2026     /bin/zsh -c something"
        with patch.object(peers.subprocess, "run",
                          return_value=SimpleNamespace(stdout=line)):
            self.assertEqual(peers._process_info(1),
                             ("74544", "Sun Aug 30 05:20:57 2026",
                              "/bin/zsh -c something"))

    def test_a_dead_pid_reads_as_nothing_rather_than_raising(self):
        with patch.object(peers.subprocess, "run",
                          return_value=SimpleNamespace(stdout="")):
            self.assertIsNone(peers._process_info(999999))

    def test_a_process_table_that_cannot_be_run_reads_as_nothing(self):
        with patch.object(peers.subprocess, "run", side_effect=OSError("no ps")):
            self.assertIsNone(peers._process_info(1))

    def test_the_key_is_never_taken_from_the_environment(self):
        """There is no override. The owner key is what pairs a hook with its
        server, so a value anyone can set would let a session claim another's
        identity. A test seam, if one is ever needed, has to be narrower than an
        environment variable."""
        with patch.dict(os.environ, {"ANTIPHON_OWNER_KEY": "999:spoofed"}), \
             patch.object(peers, "_process_info", return_value=None):
            self.assertIsNone(peers.owner_key(100))

    def test_the_walk_finds_this_session_for_real(self):
        """Not a stub: this test process is running under a real CLI, so the
        walk has something to find. Skipped where it is not."""
        key = peers.owner_key()
        if key is None:
            self.skipTest("not running under a claude or codex process")
        pid, _, start = key.partition(":")
        self.assertTrue(pid.isdigit())
        self.assertTrue(start.strip())


if __name__ == "__main__":
    unittest.main()
