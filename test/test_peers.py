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

# The suite describes a project, not the terminal it happens to run in.
# `ANTIPHON_NAME` moves cursors and sockets, so `ANTIPHON_NAME=ui npm test` —
# a reasonable thing to run now — would otherwise exercise a different layout
# than a bare run. Tests that want a name set one with `patch.dict`.
os.environ.pop("ANTIPHON_NAME", None)


class PeerNameTest(unittest.TestCase):
    def test_explicit_name_is_read_from_the_environment_and_lowercased(self):
        with patch.dict(os.environ, {"ANTIPHON_NAME": "  UI  "}):
            self.assertEqual(peers.explicit_name(), "ui")

    def test_explicit_name_is_empty_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(peers.explicit_name(), "")

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

    def test_a_name_or_kind_that_is_not_a_string_is_refused_not_raised(self):
        """Both reach here from JSON — a tool argument, a marker, a record read
        off disk. `re.fullmatch(42)` raises, and it would raise inside routing."""
        for value in (42, [], {}, 3.5, True, object()):
            self.assertFalse(peers.valid_name(value), repr(value))
            self.assertFalse(peers.valid_kind(value), repr(value))
        self.assertFalse(peers.valid_name(None))
        self.assertFalse(peers.valid_kind(None))

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
        """The socket path is the contended resource, and it is a different race
        from the name. Two peers under separate aliases carrying one address
        would leave the registry showing two peers while a message addressed to
        either reached whichever actually held the socket."""
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
        """The name race and the address race are different races. This is the
        one the name check cannot catch: two peers under different aliases both
        claiming a single address."""
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


class RecycledPidTest(unittest.TestCase):
    """A pid is a number the kernel hands out again, and the registry knew it.

    `owner_key` has always paired a pid with the start time that tells it from
    a recycled one, because a bare number identifies nobody. Liveness did not:
    it asked `os.kill(pid, 0)`, which answers "somebody is using that number",
    not "the process this record was written for is still running". So an
    endpoint that crashed without releasing its claim came back to life the
    moment its number was reassigned to an unrelated process — holding an
    alias, holding an address, and, on the Codex side, standing in the way of
    the session that legitimately wanted them.

    The fix is the same one the owner key already made: record what the process
    was, not only what it was called. Each endpoint record carries the start
    time `ps` reports for its own pid, and a live pid whose start time differs
    from the record's is the recycled number, not the peer.
    """

    LIVE = "Sat Aug 30 01:00:00 2026"
    RECYCLED = "Sun Aug 30 09:41:12 2026"
    KEY = "300:first"
    OTHER_KEY = "301:second"
    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"

    @staticmethod
    def _ps(start):
        """`ps` fixed at one answer, so a test can say when a process was born."""
        return patch.object(peers, "_process_info",
                            return_value=("1", start, "node server.js"))

    @staticmethod
    def _endpoint(project, kind="claude", name="ui"):
        return os.path.join(peers.peer_dir(project, kind, name), "endpoint.json")

    def _read(self, project, kind="claude", name="ui"):
        with open(self._endpoint(project, kind, name), encoding="utf-8") as f:
            return json.load(f)

    def _register(self, project, born, kind="claude", name="ui",
                  address="/tmp/ui.sock", pid=None, owner_key=None):
        with self._ps(born):
            ok, detail = peers.register(project, kind, name, address,
                                        pid=os.getpid() if pid is None else pid,
                                        owner_key=owner_key)
        self.assertTrue(ok, detail)

    # ---- what a record now carries ----

    def test_a_record_carries_the_fingerprint_of_the_process_it_names(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            record = self._read(project)
        self.assertEqual(record["birth"], self.LIVE)
        self.assertIsInstance(record["started_at"], float,
                              "when the claim was made is a different fact from "
                              "when the process was born, and stays its own field")

    def test_the_fingerprint_is_observed_here_and_never_supplied(self):
        """A fingerprint a caller could hand in would let a stale record assert
        its own liveness, which is the claim being checked."""
        with tempfile.TemporaryDirectory() as project:
            with self.assertRaises(TypeError):
                peers.register(project, "claude", "ui", "/tmp/ui.sock",
                               pid=os.getpid(), birth=self.RECYCLED)

    def test_an_owner_that_cannot_be_fingerprinted_registers_without_one(self):
        """`ps` is not guaranteed to answer. Refusing the claim would cost a
        working session its alias over a lookup, so the field is simply
        omitted and the record behaves exactly as records did before it."""
        with tempfile.TemporaryDirectory() as project:
            with patch.object(peers, "_process_info", return_value=None):
                ok, detail = peers.register(project, "claude", "ui",
                                            "/tmp/ui.sock", pid=os.getpid())
            self.assertTrue(ok, detail)
            self.assertNotIn("birth", self._read(project))
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"])

    # ---- the decision every caller now shares ----

    def test_a_matching_fingerprint_leaves_the_peer_live(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            with self._ps(self.LIVE):
                self.assertEqual([p["name"] for p in peers.read_peers(project)],
                                 ["ui"])
            self.assertTrue(os.path.exists(self._endpoint(project)))

    def test_a_recycled_pid_reads_as_dead_and_its_record_is_pruned(self):
        """The pid answers `os.kill`, and it is still not the peer."""
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            with patch.object(peers, "alive", return_value=True), \
                 self._ps(self.RECYCLED):
                self.assertEqual(peers.read_peers(project), [])
            self.assertFalse(os.path.exists(self._endpoint(project)),
                             "a corpse holding a live number is still a corpse")

    def test_a_recycled_pid_no_longer_holds_the_name(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE, pid=os.getppid())
            with patch.object(peers, "alive", return_value=True), \
                 self._ps(self.RECYCLED):
                ok, detail = peers.register(project, "claude", "ui",
                                            "/tmp/mine.sock", pid=os.getpid())
                self.assertTrue(ok, detail)
                self.assertEqual([p["address"] for p in peers.read_peers(project)],
                                 ["/tmp/mine.sock"],
                                 "and the new holder is the one listed")

    def test_a_recycled_pid_no_longer_holds_the_address(self):
        """The name check and the address check are two different refusals, and
        a stale record must lose both."""
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE, name="ui", pid=os.getppid())
            with patch.object(peers, "alive", return_value=True), \
                 self._ps(self.RECYCLED):
                ok, detail = peers.register(project, "claude", "api",
                                            "/tmp/ui.sock", pid=os.getpid())
            self.assertTrue(ok, detail)

    def test_a_recycled_pid_no_longer_blocks_the_session_that_wants_the_alias(self):
        """The Codex failure in full: a crashed server holds `build`, its number
        is reassigned, and the hook of the session actually running under that
        alias is refused its own address."""
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE, kind="codex", name="build",
                           address=None, pid=os.getppid(), owner_key=self.KEY)
            with patch.object(peers, "alive", return_value=True), \
                 self._ps(self.RECYCLED):
                ok, detail = peers.write_session(project, "codex", "build",
                                                 self.UUID, "/t/r.jsonl",
                                                 self.OTHER_KEY)
            self.assertTrue(ok, detail)
            self.assertEqual(peers.read_session(project, "codex", "build")["owner"],
                             self.OTHER_KEY)

    # ---- what must not change ----

    def test_a_record_without_a_fingerprint_keeps_pid_only_liveness(self):
        """Upgrading must not make the peers that are running right now vanish.
        Their records were written before this field existed, and the only
        honest reading of a missing fingerprint is the old one."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "claude", "ui")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "claude", "name": "ui", "pid": os.getpid(),
                           "address": "/tmp/ui.sock", "started_at": 1.0}, f)
            with self._ps(self.RECYCLED):
                self.assertEqual([p["name"] for p in peers.read_peers(project)],
                                 ["ui"], "no fingerprint is not a mismatch")
                ok, _ = peers.register(project, "claude", "ui", "/tmp/other.sock",
                                       pid=os.getppid())
            self.assertFalse(ok, "and it still holds its name")
            self.assertTrue(os.path.exists(self._endpoint(project)))

    def test_a_fingerprint_that_cannot_be_read_now_does_not_release_a_peer(self):
        """`ps` failing is not evidence of anything. Pruning on it would drop a
        live peer for a lookup that could not be made; only a fingerprint that
        is readable *and* different says the process is gone."""
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            with patch.object(peers, "alive", return_value=True), \
                 patch.object(peers, "_process_info", return_value=None):
                self.assertEqual([p["name"] for p in peers.read_peers(project)],
                                 ["ui"])
            self.assertTrue(os.path.exists(self._endpoint(project)))

    # ---- against a real process, with nothing mocked ----

    def test_a_real_process_matches_the_fingerprint_taken_from_it(self):
        if peers._process_info(os.getpid()) is None:
            self.skipTest("no readable process table")
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "claude", "ui", "/tmp/ui.sock",
                                        pid=os.getpid())
            self.assertTrue(ok, detail)
            self.assertEqual(self._read(project)["birth"],
                             peers._process_info(os.getpid())[1])
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"])

    def test_a_real_live_pid_with_a_foreign_fingerprint_is_not_the_peer(self):
        """This process is genuinely running, so `os.kill` says live and always
        will. Only the recorded start time can say the record belongs to
        somebody else who once held the number."""
        if peers._process_info(os.getpid()) is None:
            self.skipTest("no readable process table")
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock",
                           pid=os.getpid())
            record = self._read(project)
            record["birth"] = "Thu Jan  1 00:00:00 1970"
            with open(self._endpoint(project), "w", encoding="utf-8") as f:
                json.dump(record, f)
            self.assertTrue(peers.alive(os.getpid()))
            self.assertEqual(peers.read_peers(project), [])
            self.assertFalse(os.path.exists(self._endpoint(project)))


class AddresslessEndpointTest(unittest.TestCase):
    """A Codex peer is knowable before it is reachable.

    Codex hands its MCP server a project directory and nothing else; the rollout
    id that serves as the address arrives only with the first message. Refusing
    to register until then makes the peer invisible, and an invisible peer
    cannot be named in an ambiguity refusal — the sender is told one peer is
    live while two are.

    A `"pending"` sentinel would not do: two Codex servers would both carry it
    and the second would be refused for an address the first does not really
    serve. Absence is absence, and it stays absent in the record.
    """

    def test_two_codex_peers_may_both_be_registered_without_an_address(self):
        """Neither is routable yet; that is not a collision."""
        with tempfile.TemporaryDirectory() as project:
            first, _ = peers.register(project, "codex", "build", None,
                                      pid=os.getpid(), owner_key="300:a")
            second, detail = peers.register(project, "codex", "review", None,
                                            pid=os.getppid(), owner_key="301:b")
            self.assertTrue(first)
            self.assertTrue(second, detail)
            self.assertEqual(len(peers.read_peers(project, "codex")), 2)

    def test_only_a_codex_peer_with_an_owner_may_omit_its_address(self):
        """The shipped contract refuses a missing address for a reason. This
        widens it by exactly one shape and no further."""
        with tempfile.TemporaryDirectory() as project:
            refused = [
                ("claude", "ui", None, "300:a"),       # Claude knows its socket
                ("codex", "build", None, None),        # nothing could join it
            ]
            for kind, alias, address, key in refused:
                ok, detail = peers.register(project, kind, alias, address,
                                            pid=os.getpid(), owner_key=key)
                self.assertFalse(ok, f"{kind} {address!r} {key!r}")
                self.assertIn("address", detail)
            self.assertEqual(peers.read_peers(project), [])

    def test_a_malformed_address_is_still_refused_for_every_kind(self):
        with tempfile.TemporaryDirectory() as project:
            for kind in ("claude", "codex"):
                for address in ("", "   ", 0, ["/tmp/x.sock"]):
                    ok, _ = peers.register(project, kind, "ui", address,
                                           pid=os.getpid(), owner_key="300:a")
                    self.assertFalse(ok, f"{kind} {address!r}")
            self.assertEqual(peers.read_peers(project), [])

    def test_a_malformed_owner_key_is_refused_rather_than_ignored(self):
        """A bare pid is not a key: numbers are recycled, and the start time is
        the half that makes the join safe. Storing one anyway would leave a peer
        that registers cleanly and never pairs with anything."""
        with tempfile.TemporaryDirectory() as project:
            for key in ("", "   ", "300", ":x", "300:", "300:   ", "abc:x",
                        "0:x", "300:x\n", 300):
                ok, detail = peers.register(project, "codex", "build", None,
                                            pid=os.getpid(), owner_key=key)
                self.assertFalse(ok, repr(key))
                self.assertIn("owner key", detail)
            ok, detail = peers.register(project, "codex", "build", "rollout-a",
                                        pid=os.getpid(), owner_key="300")
            self.assertFalse(ok, "a real address does not excuse a broken key")
            self.assertEqual(peers.read_peers(project), [])

    def test_a_hand_written_record_without_an_address_is_not_listed(self):
        """Only a record in the accepted shape counts."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "build", "pid": os.getpid()}, f)
            self.assertEqual(peers.read_peers(project), [],
                             "no address and no owner is not a peer")

    def test_an_absent_address_field_is_not_the_same_as_a_null_one(self):
        """The accepted shape says the address is known to be missing. A record
        with no such field says nothing, and a reader that treats silence as a
        claim is guessing."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "build", "pid": os.getpid(),
                           "owner": "300:a"}, f)
            self.assertEqual(peers.read_peers(project), [])

    def test_a_legacy_addressed_codex_record_keeps_working(self):
        """Written before owner keys existed; it must not stop being routable."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "build", "pid": os.getpid(),
                           "address": "rollout-old"}, f)
            live = peers.read_peers(project, "codex")
        self.assertEqual([p["address"] for p in live], ["rollout-old"])

    def test_an_addressless_peer_is_listed_but_not_routable(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", None, pid=os.getpid(),
                           owner_key="300:a")
            peer = peers.read_peers(project, "codex")[0]
        self.assertIsNone(peer["address"])
        self.assertEqual(peer["name"], "build")

    def test_the_pid_and_the_owner_key_are_stored_as_different_things(self):
        """`owner` was already a local for the resolved pid in this function. A
        parameter of the same name would shadow it and write a number where the
        join expects a key."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", None, pid=os.getpid(),
                           owner_key="300:x")
            peer = peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["pid"], os.getpid())
        self.assertEqual(peer["owner"], "300:x")
        self.assertNotEqual(peer["owner"], peer["pid"])

    def test_two_peers_still_cannot_claim_one_real_address(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", "rollout-a", pid=os.getpid(),
                           owner_key="300:a")
            ok, detail = peers.register(project, "codex", "review", "rollout-a",
                                        pid=os.getppid(), owner_key="301:b")
        self.assertFalse(ok)
        self.assertIn("build", detail)

    def test_an_alias_held_by_a_different_live_owner_is_refused(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", "rollout-a",
                           pid=os.getppid(), owner_key="300:a")
            ok, detail = peers.register(project, "codex", "build", "rollout-b",
                                        pid=os.getpid(), owner_key="301:b")
        self.assertFalse(ok)
        self.assertIn("build", detail)

    def test_a_second_server_in_one_session_may_take_over_its_alias(self):
        """Codex can bring up a second MCP server for one CLI session before the
        first has exited. Judged by pid alone the newcomer looks like an
        intruder, and the session locks itself out of its own alias until its
        predecessor is reaped.

        """
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", None,
                           pid=os.getppid(), owner_key="300:a")
            ok, detail = peers.register(project, "codex", "build", "rollout-a",
                                        pid=os.getpid(), owner_key="300:a")
            self.assertTrue(ok, detail)
            live = peers.read_peers(project, "codex")
        self.assertEqual([(p["name"], p["address"], p["pid"]) for p in live],
                         [("build", "rollout-a", os.getpid())])

    def test_one_owner_may_not_serve_one_address_under_two_aliases(self):
        """The skip that lets a session refresh its own record ran before the
        address check, so a peer could register a second alias carrying the
        address it already serves. Two names and one socket: the registry would
        show two peers while a message to either arrived at the same place.

        Refreshing is about one alias. Claiming a second one is a new peer, and
        it meets the same address rule as anybody else."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", "rollout-a",
                           pid=os.getpid(), owner_key="300:a")
            same_process, detail = peers.register(project, "codex", "review",
                                                  "rollout-a", pid=os.getpid(),
                                                  owner_key="300:a")
            self.assertFalse(same_process, "one process, two aliases, one address")
            self.assertIn("build", detail)
            same_session, detail = peers.register(project, "codex", "review",
                                                  "rollout-a", pid=os.getppid(),
                                                  owner_key="300:a")
            self.assertFalse(same_session, "one session, two aliases, one address")
            self.assertIn("build", detail)

    def test_a_second_claude_server_in_one_session_does_not_take_the_socket(self):
        """The same-owner exception is for Codex alone. A Claude endpoint is a
        socket this process is serving: two channel servers under one CLI root
        would otherwise let the second overwrite the first's record while the
        first's socket is still the one answering, and the registry would
        describe a server nobody reaches."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock",
                           pid=os.getppid(), owner_key="300:a")
            ok, detail = peers.register(project, "claude", "ui", "/tmp/ui.sock",
                                        pid=os.getpid(), owner_key="300:a")
            self.assertFalse(ok, "the live holder stays")
            self.assertIn("ui", detail)
            live = peers.read_peers(project, "claude")
        self.assertEqual([p["pid"] for p in live], [os.getppid()])

    def test_an_addressless_codex_server_may_still_take_over(self):
        """Both shapes a Codex server can be in keep working."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", None,
                           pid=os.getppid(), owner_key="300:a")
            ok, detail = peers.register(project, "codex", "build", None,
                                        pid=os.getpid(), owner_key="300:a")
            self.assertTrue(ok, detail)

    def test_a_keyless_record_keeps_its_alias_against_a_keyed_claimant(self):
        """A record written before owner keys existed carries none, so a
        claimant cannot show it is the same session. The registry never resolves
        that doubt in the newcomer's favour."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "build", "pid": os.getppid(),
                           "address": "rollout-old", "started_at": 1.0}, f)
            ok, detail = peers.register(project, "codex", "build", "rollout-new",
                                        pid=os.getpid(), owner_key="300:a")
        self.assertFalse(ok)
        self.assertIn("build", detail)

    def test_an_addressless_record_left_by_a_dead_process_is_pruned(self):
        """The address check used to run before the liveness check, so a record
        without one never reached it. Left behind, it would hold its alias for
        the life of the project."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", None,
                           pid=999999, owner_key="300:a")
            with patch.object(peers, "alive", return_value=False):
                self.assertEqual(peers.read_peers(project), [])
            self.assertFalse(os.path.exists(
                os.path.join(peers.peer_dir(project, "codex", "build"),
                             "endpoint.json")))

    def test_an_alias_left_by_a_dead_owner_can_be_claimed(self):
        """An owner key excuses a differing pid, never a missing process."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "codex", "build", None,
                           pid=999999, owner_key="300:a")
            ok, detail = peers.register(project, "codex", "build", "rollout-b",
                                        pid=os.getpid(), owner_key="301:b")
            self.assertTrue(ok, detail)


class SessionRecordTest(unittest.TestCase):
    """The hook's half of a Codex peer, and how the two halves join.

    Two writers, one peer. The server owns `endpoint.json` and knows the pid;
    the hook owns `session.json` and knows the session id. Neither reads,
    modifies and writes the other's file, so neither can lose the other's
    fields — and the join between them is the owner key, never a guess.
    """

    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
    OTHER = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"
    KEY = "300:first"
    OTHER_KEY = "301:second"

    def _endpoint(self, project, owner=None):
        owner = owner or self.KEY
        ok, detail = peers.register(project, "codex", "build", None,
                                    pid=os.getpid(), owner_key=owner)
        self.assertTrue(ok, detail)

    def _session_path(self, project):
        return os.path.join(peers.peer_dir(project, "codex", "build"),
                            "session.json")

    def _write_raw(self, project, text):
        directory = peers.peer_dir(project, "codex", "build")
        os.makedirs(directory, exist_ok=True)
        with open(self._session_path(project), "w", encoding="utf-8") as f:
            f.write(text)

    # ---- the join ----

    def test_an_addressless_endpoint_takes_its_own_sessions_id_as_its_address(self):
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            ok, detail = peers.write_session(project, "codex", "build", self.UUID,
                                             "/t/r.jsonl", self.KEY)
            self.assertTrue(ok, detail)
            peer = peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], self.UUID)
        self.assertEqual(peer["pid"], os.getpid())

    def test_an_endpoint_with_no_session_record_stays_unroutable(self):
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            peer = peers.read_peers(project, "codex")[0]
        self.assertIsNone(peer["address"], "live, but nothing can reach it yet")

    def test_a_session_record_from_another_owner_is_not_an_address(self):
        """No match, no record, and a record from somebody else all read the
        same way: live, not routable. The join is the owner key or nothing."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            for owner in (self.OTHER_KEY, None, "", 300):
                record = {"session_id": self.UUID}
                if owner is not None:
                    record["owner"] = owner
                self._write_raw(project, json.dumps(record))
                peer = peers.read_peers(project, "codex")[0]
                self.assertIsNone(peer["address"], repr(owner))

    def test_an_endpoint_without_an_owner_joins_nothing(self):
        """An endpoint with no key cannot prove a session record is its own."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "build", "pid": os.getpid(),
                           "address": None, "started_at": 1.0}, f)
            self._write_raw(project, json.dumps({"session_id": self.UUID}))
            self.assertEqual(peers.read_peers(project), [],
                             "no owner is not the accepted addressless shape")

    def test_a_session_id_that_is_not_a_canonical_uuid_never_becomes_an_address(self):
        """It is about to be used as an address. An id that is not one routes a
        message at nothing, and does it silently."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            for claimed in (None, 12, "not-a-uuid", self.UUID.upper(),
                            self.UUID + "\n", self.UUID[:-1], ""):
                record = {"owner": self.KEY}
                if claimed is not None:
                    record["session_id"] = claimed
                self._write_raw(project, json.dumps(record))
                peer = peers.read_peers(project, "codex")[0]
                self.assertIsNone(peer["address"], repr(claimed))

    def test_a_malformed_session_file_leaves_the_peer_unroutable(self):
        """A half-written record must not take down every read of the registry."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            for text in ("[not an object", "[]", "null", ""):
                self._write_raw(project, text)
                peer = peers.read_peers(project, "codex")[0]
                self.assertIsNone(peer["address"], repr(text))

    def test_a_legacy_endpoint_with_a_real_address_is_left_alone(self):
        """The merge touches one shape. A Codex endpoint written before any of
        this keeps the address it already serves."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "build", "pid": os.getpid(),
                           "address": "rollout-old"}, f)
            self._write_raw(project, json.dumps({"owner": self.KEY,
                                                 "session_id": self.UUID}))
            peer = peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], "rollout-old")

    def test_a_session_record_without_an_endpoint_is_not_a_peer(self):
        """An endpoint is what makes a peer exist; the hook only describes one."""
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.write_session(project, "codex", "build", self.UUID,
                                             "/t/r.jsonl", self.KEY)
            self.assertTrue(ok, detail)
            self.assertEqual(peers.read_peers(project), [])

    def test_a_hook_that_ran_first_joins_when_its_endpoint_appears(self):
        """Either order works. The hook can fire before the server registers."""
        with tempfile.TemporaryDirectory() as project:
            peers.write_session(project, "codex", "build", self.UUID,
                                "/t/r.jsonl", self.KEY)
            self._endpoint(project)
            peer = peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], self.UUID)

    def test_a_session_record_left_by_a_pruned_endpoint_does_not_revive_it(self):
        """The endpoint is pruned when its process dies. A stale session record
        beside it must not make the alias look routable to the next reader."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            peers.write_session(project, "codex", "build", self.UUID,
                                "/t/r.jsonl", self.KEY)
            with patch.object(peers, "alive", return_value=False):
                self.assertEqual(peers.read_peers(project), [])
            self.assertTrue(os.path.exists(self._session_path(project)))
            self.assertEqual(peers.read_peers(project), [],
                             "a session record alone is not a peer")

    ESCAPE = "x/../../../escape"

    def _plant(self, project):
        """A session record where an unguarded join would land: outside
        `.antiphon` altogether, at `<project>/escape/session.json`. Without it
        this test cannot tell a guard from a lucky miss."""
        os.makedirs(os.path.join(peers.peers_dir(project), "codex-x"),
                    exist_ok=True)
        planted = peers._session_file(project, "codex", self.ESCAPE)
        os.makedirs(os.path.dirname(planted), exist_ok=True)
        with open(planted, "w", encoding="utf-8") as f:
            json.dump({"owner": self.KEY, "session_id": self.UUID}, f)
        self.assertEqual(os.path.realpath(planted),
                         os.path.realpath(os.path.join(project, "escape",
                                                       "session.json")),
                         "the escape must really escape, or this proves nothing")
        return planted

    def test_a_record_that_disagrees_with_its_own_directory_is_not_read(self):
        """The directory is where every writer for a peer puts its files, so it
        is the peer's identity. A record claiming another alias would send the
        join into another peer's directory and report an address for an
        endpoint that does not exist at all."""
        with tempfile.TemporaryDirectory() as project:
            impostor = peers.peer_dir(project, "codex", "build")
            os.makedirs(impostor)
            with open(os.path.join(impostor, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "review", "pid": os.getpid(),
                           "address": None, "owner": self.KEY}, f)
            victim = peers.peer_dir(project, "codex", "review")
            os.makedirs(victim)
            with open(os.path.join(victim, "session.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"owner": self.KEY, "session_id": self.UUID}, f)
            self.assertEqual(peers.read_peers(project), [],
                             "no review endpoint exists; nothing may be routable")

    def test_an_alias_with_a_hyphen_survives_the_directory_check(self):
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "codex", "my-build", None,
                                        pid=os.getpid(), owner_key=self.KEY)
            self.assertTrue(ok, detail)
            self.assertEqual([p["name"] for p in peers.read_peers(project)],
                             ["my-build"])

    def test_an_endpoint_that_cannot_name_itself_is_not_a_peer(self):
        """`name` comes off disk and goes straight into a path. A record whose
        name is not a name cannot be addressed, cannot be pruned, and must not
        be listed as though it were somebody."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": self.ESCAPE,
                           "pid": os.getpid(), "address": None,
                           "owner": self.KEY}, f)
            self._plant(project)
            self.assertEqual(peers.read_peers(project), [])

    def test_the_session_path_is_never_built_from_an_unchecked_name(self):
        """The join checks the name itself rather than trusting its caller to
        have done it. Two guards, because this one builds the path."""
        with tempfile.TemporaryDirectory() as project:
            self._plant(project)
            for kind, name in (("codex", self.ESCAPE), ("elsewhere", "build")):
                peer = {"kind": kind, "name": name, "pid": os.getpid(),
                        "address": None, "owner": self.KEY}
                self.assertIsNone(peers._session_address(project, peer),
                                  f"{kind} {name!r}")

    # ---- who may write ----

    def test_a_live_endpoint_owned_by_another_session_refuses_the_write(self):
        """The hole this closes, stated as the test that proves it.

        The guard is on the endpoint, not on any existing session record. A
        guard that only compared session owners would let the second owner write
        whenever the first one's hook had not run yet — and a server that has
        registered and not yet been given its id is exactly the peer that is
        live and about to become routable."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            endpoint = os.path.join(peers.peer_dir(project, "codex", "build"),
                                    "endpoint.json")
            with open(endpoint, "rb") as f:
                before = f.read()
            ok, detail = peers.write_session(project, "codex", "build", self.OTHER,
                                             "/t/second.jsonl", self.OTHER_KEY)
            self.assertFalse(ok, "no session record existed, and it is still refused")
            self.assertIn("build", detail)
            self.assertFalse(os.path.exists(self._session_path(project)),
                             "a refused write must leave nothing behind")
            with open(endpoint, "rb") as f:
                self.assertEqual(f.read(), before, "the holder is untouched")
            self.assertTrue(peers.write_session(project, "codex", "build",
                                                self.UUID, "/t/r.jsonl",
                                                self.KEY)[0])
            self.assertEqual(peers.read_peers(project, "codex")[0]["address"],
                             self.UUID, "the first session still becomes routable")

    def test_the_same_owner_overwrites_its_own_record(self):
        """One session's next turn, not a clash with itself."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            peers.write_session(project, "codex", "build", self.UUID,
                                "/t/first.jsonl", self.KEY)
            ok, detail = peers.write_session(project, "codex", "build", self.OTHER,
                                             "/t/second.jsonl", self.KEY)
            self.assertTrue(ok, detail)
            self.assertEqual(peers.read_peers(project, "codex")[0]["address"],
                             self.OTHER)

    def test_another_owner_may_write_once_the_endpoint_is_gone_or_dead(self):
        """An alias whose holder has died would otherwise be unusable for the
        rest of the project's life."""
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.write_session(project, "codex", "build", self.UUID,
                                             "/t/r.jsonl", self.OTHER_KEY)
            self.assertTrue(ok, detail)              # no endpoint holds the alias
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            with patch.object(peers, "alive", return_value=False):
                ok, detail = peers.write_session(project, "codex", "build",
                                                 self.OTHER, "/t/r.jsonl",
                                                 self.OTHER_KEY)
            self.assertTrue(ok, detail)              # the holder is gone

    def test_an_endpoint_that_identifies_nobody_does_not_hold_the_alias(self):
        """A record with an unusable pid names no process, so it cannot be
        checked for liveness and must not block a writer either."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", "build")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": "build", "pid": "nonsense",
                           "address": None, "owner": self.KEY}, f)
            ok, detail = peers.write_session(project, "codex", "build", self.UUID,
                                             "/t/r.jsonl", self.OTHER_KEY)
            self.assertTrue(ok, detail)

    # ---- the record itself ----

    def test_the_hook_never_writes_a_pid(self):
        """Liveness belongs to the process that has it. A pid written by a hook
        that has already exited would mark the peer dead on the next read."""
        with tempfile.TemporaryDirectory() as project:
            peers.write_session(project, "codex", "build", self.UUID,
                                "/t/r.jsonl", self.KEY)
            record = peers.read_session(project, "codex", "build")
        self.assertNotIn("pid", record)
        self.assertEqual(record["owner"], self.KEY)
        self.assertEqual(record["session_id"], self.UUID)
        self.assertEqual(record["transcript"], "/t/r.jsonl")

    def test_a_write_refuses_what_it_cannot_record_honestly(self):
        with tempfile.TemporaryDirectory() as project:
            refused = [
                ("codex", "build", "not-a-uuid", self.KEY),
                ("codex", "build", None, self.KEY),
                ("codex", "build", self.UUID, "300"),
                ("codex", "build", self.UUID, None),
                ("codex", "Build!", self.UUID, self.KEY),
                ("elsewhere", "build", self.UUID, self.KEY),
            ]
            for kind, alias, session_id, owner in refused:
                ok, _ = peers.write_session(project, kind, alias, session_id,
                                            "/t/r.jsonl", owner)
                self.assertFalse(ok, f"{kind} {alias!r} {session_id!r} {owner!r}")
            self.assertFalse(os.path.exists(peers.peers_dir(project)))

    def test_a_missing_transcript_is_recorded_as_absent_not_as_a_refusal(self):
        """Nothing routes on the transcript. Refusing the whole record over it
        would cost the session its address for a field nobody delivers to."""
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.write_session(project, "codex", "build", self.UUID,
                                             None, self.KEY)
            self.assertTrue(ok, detail)
            record = peers.read_session(project, "codex", "build")
        self.assertNotIn("transcript", record)

    def test_no_temporary_file_survives_a_write(self):
        """The record is replaced, never rewritten in place: a reader must find
        the old document or the new one, never half of either."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            peers.write_session(project, "codex", "build", self.UUID,
                                "/t/r.jsonl", self.KEY)
            leftovers = [e for e in os.listdir(peers.peer_dir(project, "codex",
                                                              "build"))
                         if e.endswith(".tmp") or ".tmp." in e]
        self.assertEqual(leftovers, [])

    def test_reading_a_session_for_an_unusable_alias_returns_nothing(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertIsNone(peers.read_session(project, "codex", "../../escape"))
            self.assertIsNone(peers.read_session(project, "elsewhere", "build"))
            self.assertIsNone(peers.read_session(project, "codex", "build"))

    def test_a_canonical_uuid_is_the_only_session_id(self):
        for good in (self.UUID, self.OTHER,
                     "01a04870-bd35-7732-9dea-e564f731dba7"):
            self.assertTrue(peers.valid_session_id(good), good)
        for bad in ("", None, 12, self.UUID.upper(), self.UUID + "\n",
                    self.UUID.replace("-", ""), "g" + self.UUID[1:]):
            self.assertFalse(peers.valid_session_id(bad), repr(bad))


class UnnamedKeyTest(unittest.TestCase):
    """The registry key an unnamed peer occupies, which is not a name.

    A peer with no name still needs a place in the registry — it is live, it
    serves a socket, and it has to be counted. But the key it occupies must not
    be something anyone can address, or the bridge would hand out a name for a
    peer that reports having none, and a reply sent to it would be sent to
    something that never claimed to be reachable that way.
    """

    def test_the_reserved_key_is_not_a_public_alias(self):
        """Two grammars, on purpose. The public one is what a person may type
        and what `@claude:` may carry; the key is only what a directory may be
        called. Nothing a user can write reaches the second."""
        self.assertFalse(peers.valid_name(peers.UNNAMED))
        self.assertTrue(peers.valid_key("claude", peers.UNNAMED))
        for name in ("ui", "my-build", "claude-abc"):
            self.assertTrue(peers.valid_name(name), name)
            self.assertTrue(peers.valid_key("claude", name), name)
            self.assertTrue(peers.valid_key("codex", name), name)

    def test_the_reserved_key_belongs_to_the_Claude_side_only(self):
        """It is how an unnamed Claude *endpoint* is represented, and that is
        all it is. An unnamed Codex session deliberately has no record at all:
        it never registers, which is precisely why one visible Codex peer
        cannot be shown to be the only one. A record under this key on that
        side would be a live peer nobody could ever name."""
        self.assertFalse(peers.valid_key("codex", peers.UNNAMED))
        self.assertTrue(peers.valid_key("claude", peers.UNNAMED))

    def test_a_codex_peer_may_not_take_the_reserved_key(self):
        with tempfile.TemporaryDirectory() as project:
            for address in (None, "rollout-a"):
                ok, detail = peers.register(project, "codex", peers.UNNAMED,
                                            address, pid=os.getpid(),
                                            owner_key="300:x")
                self.assertFalse(ok, repr(address))
                self.assertIn("name", detail)
            self.assertEqual(peers.read_peers(project), [])

    def test_a_hand_written_codex_record_under_the_key_is_ignored(self):
        """`_scan` checks the directory it found the record in, so a record
        placed there by hand is not read into a peer either."""
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "codex", peers.UNNAMED)
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "codex", "name": peers.UNNAMED,
                           "pid": os.getpid(), "address": "rollout-a"}, f)
            self.assertEqual(peers.read_peers(project), [])

    def test_a_codex_session_record_under_the_key_is_refused(self):
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.write_session(project, "codex", peers.UNNAMED,
                                             "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7",
                                             "/t/r.jsonl", "300:x")
            self.assertFalse(ok)
            self.assertIn("codex", detail)
            self.assertIsNone(peers.read_session(project, "codex", peers.UNNAMED))
            self.assertFalse(os.path.exists(peers.peers_dir(project)))

    def test_no_alias_a_user_could_choose_can_collide_with_it(self):
        """The check is exact, never a prefix or a shape. `claude-abc` is a name
        somebody may deliberately pick, and guessing at it would take their
        alias away from them."""
        self.assertNotEqual(peers.UNNAMED, "claude-abc")
        self.assertFalse(peers.valid_name(peers.UNNAMED))

    def test_an_unnamed_peer_registers_and_reads_back(self):
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "claude", peers.UNNAMED,
                                        "/tmp/ui.sock", pid=os.getpid())
            self.assertTrue(ok, detail)
            live = peers.read_peers(project, "claude")
        self.assertEqual([(p["name"], p["address"]) for p in live],
                         [(peers.UNNAMED, "/tmp/ui.sock")])

    def test_two_unnamed_peers_cannot_both_hold_the_key(self):
        """One unnamed peer per side per project, which is what unnamed means."""
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", peers.UNNAMED, "/tmp/ui.sock",
                           pid=os.getpid())
            ok, _ = peers.register(project, "claude", peers.UNNAMED,
                                   "/tmp/other.sock", pid=os.getppid())
            self.assertFalse(ok)

    def test_the_key_is_released_and_pruned_like_any_other(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", peers.UNNAMED, "/tmp/ui.sock",
                           pid=999999)
            with patch.object(peers, "alive", return_value=False):
                self.assertEqual(peers.read_peers(project), [])
            self.assertFalse(os.path.exists(
                os.path.join(peers.peer_dir(project, "claude", peers.UNNAMED),
                             "endpoint.json")))
            peers.register(project, "claude", peers.UNNAMED, "/tmp/ui.sock",
                           pid=os.getpid())
            peers.unregister(project, "claude", peers.UNNAMED, pid=os.getpid())
            self.assertEqual(peers.read_peers(project), [])

    def test_the_key_stays_inside_the_project(self):
        """It becomes a directory name, so it is checked like every other."""
        for hostile in ("<unnamed>/../..", "../<unnamed>", "<unnamed> ",
                        "<UNNAMED>", "unnamed"):
            self.assertNotEqual(hostile, peers.UNNAMED)
            if hostile == "unnamed":
                continue              # a legal alias, and a different peer
            self.assertFalse(peers.valid_key("claude", hostile), hostile)


if __name__ == "__main__":
    unittest.main()
