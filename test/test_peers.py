import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import peers

import contextlib
import errno
import hashlib
import io
import json
import pathlib
import multiprocessing
import subprocess
import tempfile
import time
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
            self.assertEqual(peers.owner_key(100),
                             "300:v1:Sat Aug 30 01:00:00 2026")

    def test_a_claude_root_is_recognised_too(self):
        with patch.object(peers, "_process_info", side_effect=[
                ("200", "Sat Aug 30 01:00:01 2026", "node lib/channel.mjs"),
                ("300", "Sat Aug 30 01:00:00 2026", "/usr/local/bin/claude")]):
            self.assertEqual(peers.owner_key(100),
                             "200:v1:Sat Aug 30 01:00:00 2026")

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
        """The C-locale fields, not an inherited-width slice, divide the line."""
        line = "74544 Sun Aug 30 05:20:57 2026     /bin/zsh -c something"
        with patch.object(peers.subprocess, "run",
                          return_value=SimpleNamespace(stdout=line)):
            self.assertEqual(peers._process_info(1),
                             ("74544", "Sun Aug 30 05:20:57 2026",
                              "/bin/zsh -c something"))

    def test_process_table_is_rendered_in_one_canonical_environment(self):
        """A writer and reader inherit different host environments routinely.

        The environment belongs on the `ps` child itself: changing Python's
        idea of a timezone after the fact cannot recover how an old string was
        rendered.
        """
        line = "1 Sun Aug 30 05:20:57 2026 /bin/zsh"
        with patch.dict(os.environ, {"KEEP_ME": "yes", "TZ": "Europe/Istanbul",
                                     "LC_ALL": "tr_TR.UTF-8"}, clear=True), \
             patch.object(peers.subprocess, "run",
                          return_value=SimpleNamespace(stdout=line)) as run:
            self.assertEqual(peers._process_info(1),
                             ("1", "Sun Aug 30 05:20:57 2026", "/bin/zsh"))
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["KEEP_ME"], "yes")

    def test_one_live_process_has_one_birth_across_reader_timezones(self):
        """The exact product fault: the same pid must not look recycled merely
        because two hosts supplied different `TZ` values to their MCP server."""
        with patch.dict(os.environ, {"TZ": "UTC"}):
            utc = peers._process_birth(os.getpid())
        with patch.dict(os.environ, {"TZ": "Europe/Istanbul"}):
            istanbul = peers._process_birth(os.getpid())
        if utc is None or istanbul is None:
            self.skipTest("no readable process table")
        self.assertEqual(utc, istanbul)

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

    def test_owner_key_schema_distinguishes_current_from_legacy(self):
        self.assertEqual(
            peers.owner_key_version("300:v1:Sat Aug 30 01:00:00 2026"), 1)
        self.assertIsNone(
            peers.owner_key_version("300:Sat Aug 30 01:00:00 2026"))
        self.assertIsNone(peers.owner_key_version("not-an-owner"))

    def test_current_owner_liveness_requires_a_reproducible_fingerprint(self):
        key = "300:v1:Sat Aug 30 01:00:00 2026"
        with patch.object(peers, "alive", return_value=True), \
             patch.object(peers, "_process_birth",
                          return_value="Sat Aug 30 01:00:00 2026"):
            self.assertEqual(peers._owner_liveness(key), "live")
        with patch.object(peers, "alive", return_value=True), \
             patch.object(peers, "_process_birth", return_value=None):
            self.assertEqual(peers._owner_liveness(key), "unknown")
        with patch.object(peers, "alive", return_value=True), \
             patch.object(peers, "_process_birth",
                          return_value="Sun Aug 31 09:00:00 2026"):
            self.assertEqual(peers._owner_liveness(key), "dead")
        with patch.object(peers, "alive", return_value=False), \
             patch.object(peers, "_process_birth") as process_birth:
            self.assertEqual(peers._owner_liveness(key), "dead")
            process_birth.assert_not_called()

    def test_legacy_or_future_owner_identity_is_unknown(self):
        for key in ("300:Sat Aug 30 01:00:00 2026",
                    "300:v2:Sat Aug 30 01:00:00 2026",
                    "not-an-owner"):
            with patch.object(peers, "alive") as alive:
                self.assertEqual(peers._owner_liveness(key), "unknown")
                alive.assert_not_called()

    def test_owner_liveness_observation_is_cached_by_owner_key(self):
        key = "300:v1:Sat Aug 30 01:00:00 2026"
        cache = {}
        with patch.object(peers, "alive", return_value=True) as alive, \
             patch.object(peers, "_process_birth",
                          return_value="Sat Aug 30 01:00:00 2026") as birth:
            self.assertEqual(peers._owner_liveness(key, cache), "live")
            self.assertEqual(peers._owner_liveness(key, cache), "live")
        self.assertEqual(alive.call_count, 1)
        self.assertEqual(birth.call_count, 1)

    def test_the_walk_finds_this_session_for_real(self):
        """Not a stub: this test process is running under a real CLI, so the
        walk has something to find. Skipped where it is not."""
        key = peers.owner_key()
        if key is None:
            self.skipTest("not running under a claude or codex process")
        pid, _, start = key.partition(":")
        self.assertTrue(pid.isdigit())
        self.assertTrue(start.startswith("v1:"), start)
        self.assertTrue(start.removeprefix("v1:").strip())


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
        self.assertEqual(record["birth_version"], 1)
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

    def test_an_unversioned_fingerprint_is_legacy_not_a_recycled_pid(self):
        """0.3.3 wrote a rendered time without recording its TZ or locale.

        A new UTC/C reader cannot compare that string honestly. Treating the
        mismatch as death deletes a live endpoint once during every upgrade.
        """
        with tempfile.TemporaryDirectory() as project:
            directory = peers.peer_dir(project, "claude", "ui")
            os.makedirs(directory)
            with open(os.path.join(directory, "endpoint.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"kind": "claude", "name": "ui",
                           "pid": os.getpid(), "address": "/tmp/ui.sock",
                           "birth": self.LIVE, "started_at": 1.0}, f)
            with patch.object(peers, "alive", return_value=True), \
                 self._ps(self.RECYCLED):
                self.assertEqual([p["name"] for p in peers.read_peers(project)],
                                 ["ui"])
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
    CURRENT_KEY = "300:v1:Sat Aug 30 01:00:00 2026"

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
        """Equal legacy keys remain joinable during rolling upgrade."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            ok, detail = peers.write_session(project, "codex", "build", self.UUID,
                                             "/t/r.jsonl", self.KEY)
            self.assertTrue(ok, detail)
            peer = peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], self.UUID)
        self.assertEqual(peer["pid"], os.getpid())

    def test_two_current_owner_keys_join(self):
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project, self.CURRENT_KEY)
            ok, detail = peers.write_session(
                project, "codex", "build", self.UUID, "/t/r.jsonl",
                self.CURRENT_KEY)
            self.assertTrue(ok, detail)
            peer = peers.read_peers(project, "codex")[0]
        self.assertEqual(peer["address"], self.UUID)

    def test_mixed_owner_key_generations_never_join_by_pid_alone(self):
        """The legacy half may be stale from a process whose pid was reused.

        Matching the numeric prefix would silently attach that old session to
        a new endpoint. A rolling upgrade remains visible but unroutable until
        the older writer refreshes its half.
        """
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project, self.CURRENT_KEY)
            self._write_raw(project, json.dumps({
                "owner": "300:Sat Aug 30 01:00:00 2026",
                "session_id": self.UUID,
            }))
            peer = peers.read_peers(project, "codex")[0]
        self.assertIsNone(peer["address"])
        self.assertTrue(peers.owner_generations_mixed(
            self.CURRENT_KEY, "300:Sat Aug 30 01:00:00 2026"))

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

    def test_an_endpoint_that_records_no_owner_is_refused_without_an_accusation(self):
        """An absent owner key is evidence of nothing, and the record is most
        often the writer's own — registered before the field existed, or by a
        tree `owner_key` could not walk. `_owner_of` returns None there, so the
        comparison reads it as *a different owner* and the old wording named the
        reader's own pid as somebody else's live session, once per turn.

        The write is still refused: an endpoint whose owner cannot be read
        cannot be shown to be this session's, and guessing is what this registry
        exists to end. Only the sentence changes."""
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(project, "codex", "build", "/t/b.sock",
                                        pid=os.getpid())
            self.assertTrue(ok, detail)
            ok, detail = peers.write_session(project, "codex", "build", self.UUID,
                                             "/t/r.jsonl", self.KEY)
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(self._session_path(project)))
        self.assertEqual(detail,
                         "alias 'build' is held by a live codex endpoint that "
                         f"records no owner (pid {os.getpid()}); its record was "
                         "not touched")
        self.assertNotIn("another live", detail,
                         "nobody has been shown to be another session")

    def test_a_known_different_owner_is_still_named_as_another_session(self):
        """The accusation is true exactly where an owner key was readable and
        differed, so that sentence stays."""
        with tempfile.TemporaryDirectory() as project:
            self._endpoint(project)
            ok, detail = peers.write_session(project, "codex", "build", self.OTHER,
                                             "/t/second.jsonl", self.OTHER_KEY)
        self.assertFalse(ok)
        self.assertEqual(detail,
                         "alias 'build' is held by another live codex session "
                         f"(pid {os.getpid()}); its record was not touched")

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


class CodexObservationTest(unittest.TestCase):
    """Hook-owned evidence that a host session id was observed.

    It is deliberately not a peer record: no alias, endpoint, address or
    transcript is present, and nothing in this module calls it live.
    """

    UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
    OTHER = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"

    @staticmethod
    def _root(project):
        return os.path.join(project, ".antiphon", "observations", "codex")

    def test_one_canonical_id_is_written_atomically_without_transcript_data(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(peers.time, "time", return_value=123.5):
            self.assertTrue(peers.write_observation(project, self.UUID))
            root = self._root(project)
            names = os.listdir(root)
            self.assertEqual(names, [self.UUID + ".json"])
            with open(os.path.join(root, names[0]), encoding="utf-8") as stream:
                record = json.load(stream)
        self.assertEqual(record, {
            "version": 1,
            "kind": "codex",
            "session_id": self.UUID,
            "observed_at": 123.5,
        })
        self.assertFalse(any(name.endswith(".tmp") for name in names))
        self.assertNotIn("transcript", record)
        self.assertNotIn("address", record)
        self.assertNotIn("name", record)

    def test_each_session_owns_one_file_and_refresh_is_idempotent(self):
        with tempfile.TemporaryDirectory() as project:
            with patch.object(peers.time, "time", return_value=10.0):
                self.assertTrue(peers.write_observation(project, self.UUID))
                self.assertTrue(peers.write_observation(project, self.OTHER))
            with patch.object(peers.time, "time", return_value=20.0):
                self.assertTrue(peers.write_observation(project, self.UUID))
            records = {record["session_id"]: record
                       for record in peers.read_observations(project)}
        self.assertEqual(sorted(records), [self.UUID, self.OTHER])
        self.assertEqual(records[self.UUID]["observed_at"], 20.0)
        self.assertEqual(records[self.OTHER]["observed_at"], 10.0)

    def test_invalid_ids_write_nothing_and_cannot_escape(self):
        with tempfile.TemporaryDirectory() as project:
            for bad in (None, 12, "", self.UUID.upper(), self.UUID + "\n",
                        "../../escape", self.UUID.replace("-", "")):
                self.assertFalse(peers.write_observation(project, bad), repr(bad))
            self.assertFalse(os.path.exists(os.path.join(project, ".antiphon")))

    def test_reader_accepts_only_matching_versioned_records(self):
        with tempfile.TemporaryDirectory() as project:
            root = self._root(project)
            os.makedirs(root)
            bad = {
                "broken.json": "{not json",
                self.UUID + ".tmp": json.dumps({
                    "version": 1, "kind": "codex",
                    "session_id": self.UUID, "observed_at": 1.0}),
                self.UUID + ".json": json.dumps({
                    "version": 2, "kind": "codex",
                    "session_id": self.UUID, "observed_at": 1.0}),
                self.OTHER + ".json": json.dumps({
                    "version": 1, "kind": "claude",
                    "session_id": self.OTHER, "observed_at": 1.0}),
            }
            for name, content in bad.items():
                with open(os.path.join(root, name), "w", encoding="utf-8") as stream:
                    stream.write(content)
            self.assertEqual(peers.read_observations(project), [])

            with open(os.path.join(root, self.UUID + ".json"), "w",
                      encoding="utf-8") as stream:
                json.dump({"version": 1, "kind": "codex",
                           "session_id": self.OTHER,
                           "observed_at": 1.0}, stream)
            self.assertEqual(peers.read_observations(project), [],
                             "a record cannot claim another file's identity")

    def test_reader_rejects_non_finite_and_negative_observation_times(self):
        with tempfile.TemporaryDirectory() as project:
            root = self._root(project)
            os.makedirs(root)
            for observed_at in (float("nan"), float("inf"),
                                float("-inf"), -1.0):
                with self.subTest(observed_at=observed_at), \
                     open(os.path.join(root, self.UUID + ".json"), "w",
                          encoding="utf-8") as stream:
                    json.dump({"version": 1, "kind": "codex",
                               "session_id": self.UUID,
                               "observed_at": observed_at}, stream)
                self.assertEqual(peers.read_observations(project), [])

    def test_reader_rejects_non_integer_versions_and_overflow_sized_times(self):
        with tempfile.TemporaryDirectory() as project:
            root = self._root(project)
            os.makedirs(root)
            malformed = (
                {"version": True, "observed_at": 1.0},
                {"version": 1.0, "observed_at": 1.0},
                {"version": 1, "observed_at": 10 ** 310},
            )
            for fields in malformed:
                with self.subTest(fields=fields), \
                     open(os.path.join(root, self.UUID + ".json"), "w",
                          encoding="utf-8") as stream:
                    json.dump({"kind": "codex", "session_id": self.UUID,
                               **fields}, stream)
                self.assertEqual(peers.read_observations(project), [])


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
        all it is. An unnamed Codex session deliberately has no peer record: a
        hook observation carries neither this key nor a route. A peer record
        under this key on that side would be live but impossible to name."""
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


class IdentityProofTest(unittest.TestCase):
    """The owner-current proof: which session an owner is running right now.

    A stale automatic session half stays authoritative forever without it, and
    a message addressed to the retired alias lands in the session that replaced
    it. Everything here is about refusing to guess: an absent proof, an
    unreadable one and a corrupt one are three different facts, and collapsing
    them is what would let a corrupt file open a claim it must have refused.
    """

    OWNER = "4242:Mon Sep  1 00:00:00 2026"
    SESSION = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    OTHER = "0199a1b2-2222-7000-8000-00000000000b"

    def _digest(self, session_id=None):
        return peers.auto_identity(session_id or self.SESSION)[1]

    def _write(self, project, **over):
        """The record `write_identity_proof` would have written, then edited."""
        peers.write_identity_proof(project, self.OWNER, self.SESSION,
                                   self._digest())
        path = peers.identity_proof_path(project, self.OWNER)
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
        record.update(over)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        return path

    def test_an_identity_proof_round_trips_under_the_registry_lock(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(peers.write_identity_proof(
                project, self.OWNER, self.SESSION, self._digest()))
            state, proof = peers.read_identity_proof(project, self.OWNER)
            self.assertEqual(state, "valid")
            self.assertEqual(proof["session_id"], self.SESSION)
            self.assertEqual(proof["identity_digest"], self._digest())
            self.assertEqual(proof["owner_key"], self.OWNER)
            self.assertEqual(proof["kind"], "claude")
            self.assertNotIn("written_at", proof,
                             "nothing reads it, so it is not stored")

    def test_identity_proof_read_separates_absent_unreadable_and_invalid(self):
        """Three facts, never one None. A corrupt proof read as absent would
        open the candidate claim that §2a exists to refuse."""
        with tempfile.TemporaryDirectory() as project:
            self.assertEqual(peers.read_identity_proof(project, self.OWNER),
                             ("absent", None))

            path = self._write(project, kind="codex")
            self.assertEqual(peers.read_identity_proof(project, self.OWNER)[0],
                             "invalid")

            def refuse(*_a, **_k):
                raise OSError(5, "Input/output error", path)

            self._write(project)
            with patch.object(peers.io, "open", side_effect=refuse) \
                    if hasattr(peers, "io") else patch("builtins.open",
                                                       side_effect=refuse):
                self.assertEqual(
                    peers.read_identity_proof(project, self.OWNER)[0],
                    "unreadable")

    def test_every_malformed_identity_proof_reads_as_no_proof(self):
        cases = {
            "version is a string": {"version": "1"},
            "version is a bool": {"version": True},
            "version is the wrong number": {"version": 99},
            "kind is codex": {"kind": "codex"},
            "session id is missing": {"session_id": None},
            "session id is not canonical": {"session_id": "not-a-uuid"},
            "identity digest is malformed": {"identity_digest": "zz"},
            "identity digest is another session's":
                {"identity_digest": peers.auto_identity(OTHER := "0199a1b2-"
                                                        "2222-7000-8000-"
                                                        "00000000000b")[1]},
            "owner key is not canonical": {"owner_key": "no-start-time"},
        }
        for name, over in cases.items():
            with self.subTest(case=name), \
                 tempfile.TemporaryDirectory() as project:
                self._write(project, **over)
                state, proof = peers.read_identity_proof(project, self.OWNER)
                self.assertEqual(state, "invalid", name)
                self.assertIsNone(proof, name)

    def test_a_proof_under_another_owners_filename_is_invalid(self):
        """Without this a record could be planted or renamed under another
        owner's digest and read as that owner's proof."""
        other_owner = "4243:Mon Sep  1 00:00:00 2026"
        with tempfile.TemporaryDirectory() as project:
            source = self._write(project)
            target = peers.identity_proof_path(project, other_owner)
            os.replace(source, target)
            self.assertEqual(
                peers.read_identity_proof(project, other_owner)[0], "invalid")

    def test_a_torn_or_empty_identity_proof_is_invalid(self):
        for name, body in (("torn", '{"version": 1, "kind": "cla'),
                           ("empty", "")):
            with self.subTest(case=name), \
                 tempfile.TemporaryDirectory() as project:
                path = self._write(project)
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(body)
                self.assertEqual(
                    peers.read_identity_proof(project, self.OWNER)[0],
                    "invalid", name)

    def test_the_identity_proof_inventory_is_read_only_and_validated(self):
        second = "4243:Mon Sep  1 00:00:00 2026"
        with tempfile.TemporaryDirectory() as project:
            peers.write_identity_proof(project, self.OWNER, self.SESSION,
                                       self._digest())
            peers.write_identity_proof(project, second, self.OTHER,
                                       self._digest(self.OTHER))
            before = sorted(os.listdir(peers.identity_proofs_dir(project)))
            inventory = peers.identity_proofs(project)
            self.assertEqual(inventory.completeness, "exact")
            self.assertEqual(len(inventory.proofs), 2)
            self.assertEqual(
                sorted(os.listdir(peers.identity_proofs_dir(project))), before,
                "reading the inventory writes nothing")

    def test_identity_proof_inventory_never_reports_a_confident_zero(self):
        """An unreadable directory is not an empty one."""
        with tempfile.TemporaryDirectory() as project:
            peers.write_identity_proof(project, self.OWNER, self.SESSION,
                                       self._digest())

            def refuse(*_a, **_k):
                raise OSError(5, "Input/output error")

            with patch.object(peers.os, "scandir", side_effect=refuse):
                inventory = peers.identity_proofs(project)
            self.assertEqual(inventory.completeness, "unknown")
            self.assertEqual(inventory.proofs, ())

        with tempfile.TemporaryDirectory() as project:
            peers.write_identity_proof(project, self.OWNER, self.SESSION,
                                       self._digest())
            broken = peers.identity_proof_path(
                project, "4243:Mon Sep  1 00:00:00 2026")
            with open(broken, "w", encoding="utf-8") as stream:
                stream.write("{")
            inventory = peers.identity_proofs(project)
            self.assertEqual(inventory.completeness, "lower-bound",
                             "one unreadable neighbour must not hide a live "
                             "peer, and must not be counted as complete")
            self.assertEqual(len(inventory.proofs), 1)


class HookIdentityRotationTest(unittest.TestCase):
    """One transaction: capture the prior proof, replace it, retire what it
    made stale. Assembled from separately locked helpers it would not be a
    transaction at all, and the registry lock is not reentrant, so it could not
    be nested either."""

    OWNER = "4242:v1:Mon Sep  1 00:00:00 2026"
    OTHER_OWNER = "4243:v1:Mon Sep  1 00:00:00 2026"
    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"
    C = "0199a1b2-3333-7000-8000-00000000000c"

    def _automatic_half(self, project, session_id, owner=None):
        alias, digest = peers.auto_identity(session_id)
        owner = owner or self.OWNER
        peers.register(project, "claude", alias,
                       os.path.join(project, alias + ".sock"),
                       pid=os.getpid(), owner_key=owner,
                       identity_digest=digest)
        peers.write_session(project, "claude", alias, session_id,
                            f"/t/{alias}.jsonl", owner, digest, True)
        return alias

    def _half_exists(self, project, session_id):
        alias = peers.auto_identity(session_id)[0]
        return os.path.exists(peers._session_file(project, "claude", alias))

    def test_hook_identity_rotation_returns_prior_and_current(self):
        with tempfile.TemporaryDirectory() as project:
            first = peers.rotate_identity_proof(
                project, self.OWNER, self.A, peers.auto_identity(self.A)[1])
            self.assertTrue(first.ok)
            self.assertIsNone(first.prior, "nothing preceded the first proof")
            self.assertEqual(first.current["session_id"], self.A)

            second = peers.rotate_identity_proof(
                project, self.OWNER, self.B, peers.auto_identity(self.B)[1])
            self.assertTrue(second.ok)
            self.assertEqual(second.prior["session_id"], self.A)
            self.assertEqual(second.current["session_id"], self.B)

    def test_hook_identity_withdraws_only_same_owner_stale_halves(self):
        with tempfile.TemporaryDirectory() as project:
            self._automatic_half(project, self.A)
            self._automatic_half(project, self.C, owner=self.OTHER_OWNER)
            peers.register(project, "claude", "build",
                           os.path.join(project, "build.sock"),
                           pid=os.getpid(), owner_key=self.OWNER)
            peers.write_session(project, "claude", "build", self.A,
                                "/t/build.jsonl", self.OWNER)

            # The half for the session becoming current, which every Stop
            # hook after the first actually rotates into. Without it the test
            # measured "same owner" and "automatic" but never the word `stale`
            # in the function's own name: dropping that check tombstoned and
            # unlinked the live identity's own half on every turn, and a
            # lock-acquisition later `write_session` wrote it back — a
            # per-turn window in which the current peer reads UNREADY.
            self._automatic_half(project, self.B)

            peers.rotate_identity_proof(project, self.OWNER, self.B,
                                        peers.auto_identity(self.B)[1])

            self.assertTrue(self._half_exists(project, self.B),
                            "the half becoming current is not stale")
            self.assertFalse(
                os.path.exists(peers.retired_half_path(
                    project, "claude", peers.auto_identity(self.B)[0])),
                "and nothing tombstones it")
            self.assertFalse(self._half_exists(project, self.A),
                             "the same owner's stale automatic half goes")
            self.assertTrue(self._half_exists(project, self.C),
                            "another owner's half is untouched")
            self.assertTrue(
                os.path.exists(peers._session_file(project, "claude", "build")),
                "an explicit peer is never withdrawn by this rotation")
            self.assertTrue(
                os.path.exists(os.path.join(
                    peers.peer_dir(project, "claude",
                                   peers.auto_identity(self.A)[0]),
                    "endpoint.json")),
                "the hook withdraws a session half, never an endpoint")

    def test_hook_identity_rotation_is_one_locked_transaction(self):
        """A gate inside the lock, so the interleaving is proved rather than
        raced: B acquires and holds, C is shown blocked, B commits, then C."""
        import threading
        with tempfile.TemporaryDirectory() as project:
            self._automatic_half(project, self.A)
            entered, release = threading.Event(), threading.Event()
            real = peers._write_identity_proof_locked

            def gated(cwd, owner, session_id, digest):
                if session_id == self.B:
                    entered.set()
                    release.wait(5)
                return real(cwd, owner, session_id, digest)

            outcomes = {}

            def rotate(session_id):
                outcomes[session_id] = peers.rotate_identity_proof(
                    project, self.OWNER, session_id,
                    peers.auto_identity(session_id)[1])

            with patch.object(peers, "_write_identity_proof_locked", gated):
                b = threading.Thread(target=rotate, args=(self.B,))
                b.start()
                self.assertTrue(entered.wait(5), "B never entered the lock")
                c = threading.Thread(target=rotate, args=(self.C,))
                c.start()
                c.join(0.5)
                self.assertTrue(c.is_alive(),
                                "C must be blocked behind B's lock, not racing")
                release.set()
                b.join(5)
                c.join(5)

            state, proof = peers.read_identity_proof(project, self.OWNER)
            self.assertEqual(state, "valid")
            self.assertEqual(proof["session_id"], self.C,
                             "the last committed transaction is current")
            self.assertTrue(self._half_exists(project, self.C)
                            or not self._half_exists(project, self.A),
                            "A's stale half did not survive both rotations")
            self.assertFalse(self._half_exists(project, self.A))


class AutomaticRegistrationModeTest(unittest.TestCase):
    """Initial and reassert carry different rules, so they must be told apart.

    Both send the same payload today, and once an endpoint is pruned Python
    cannot tell which it is looking at. The check also belongs inside the lock
    that writes the endpoint: validated one hop earlier in Node, the proof could
    move in the window before the write.
    """

    OWNER = "4242:v1:Mon Sep  1 00:00:00 2026"
    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"

    def _claim(self, project, session_id, mode, owner=None):
        alias, digest = peers.auto_identity(session_id)
        return peers.register(project, "claude", alias,
                              os.path.join(project, alias + ".sock"),
                              pid=os.getpid(), owner_key=owner or self.OWNER,
                              identity_digest=digest, mode=mode)

    def test_automatic_registration_first_claim_needs_a_genuinely_absent_proof(self):
        with tempfile.TemporaryDirectory() as project:
            ok, _ = self._claim(project, self.A, "initial")
            self.assertTrue(ok, "no proof exists yet: an UNREADY candidate")

    def test_automatic_registration_refuses_a_claim_on_an_unreadable_proof(self):
        """Invalid and unreadable must not be mistaken for absent."""
        with tempfile.TemporaryDirectory() as project:
            peers.write_identity_proof(project, self.OWNER, self.A,
                                       peers.auto_identity(self.A)[1])
            path = peers.identity_proof_path(project, self.OWNER)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{")
            ok, detail = self._claim(project, self.A, "initial")
            self.assertFalse(ok, detail)
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "{",
                                 "a refused claim overwrites nothing")

    def test_automatic_registration_must_match_an_existing_proof(self):
        with tempfile.TemporaryDirectory() as project:
            peers.write_identity_proof(project, self.OWNER, self.B,
                                       peers.auto_identity(self.B)[1])
            ok, detail = self._claim(project, self.A, "initial")
            self.assertFalse(ok, detail)
            ok, _ = self._claim(project, self.B, "initial")
            self.assertTrue(ok)

    def test_automatic_registration_reassert_always_needs_an_exact_proof(self):
        with tempfile.TemporaryDirectory() as project:
            ok, detail = self._claim(project, self.A, "reassert")
            self.assertFalse(ok, "reassert never opens a candidate slot")
            peers.write_identity_proof(project, self.OWNER, self.A,
                                       peers.auto_identity(self.A)[1])
            ok, _ = self._claim(project, self.A, "reassert")
            self.assertTrue(ok)

    def test_automatic_registration_modes_are_allowlisted(self):
        with tempfile.TemporaryDirectory() as project:
            for mode in ("Initial", "retire", "", "reassert-now"):
                with self.subTest(mode=mode):
                    ok, _ = self._claim(project, self.A, mode)
                    self.assertFalse(ok, "an unknown mode fails closed")

    def test_automatic_registration_leaves_explicit_and_codex_alone(self):
        """Scope control: register is shared, this contract is not."""
        with tempfile.TemporaryDirectory() as project:
            ok, detail = peers.register(
                project, "claude", "build", os.path.join(project, "b.sock"),
                pid=os.getpid(), owner_key=self.OWNER)
            self.assertTrue(ok, detail)
            ok, detail = peers.register(
                project, "codex", "worker", None,
                pid=os.getpid(), owner_key=self.OWNER)
            self.assertTrue(ok, detail)

    def test_automatic_registration_checks_the_proof_under_the_write_lock(self):
        """A Node-side precheck would pass; the locked check must not.

        The proof is moved after the caller has read it and before the write
        happens, which is exactly the window a precheck cannot close.
        """
        with tempfile.TemporaryDirectory() as project:
            peers.write_identity_proof(project, self.OWNER, self.A,
                                       peers.auto_identity(self.A)[1])
            real = peers._read_identity_proof_file
            moved = []

            def move_then_read(path, digest):
                if not moved:
                    moved.append(True)
                    # The unlocked core: `register` already holds the registry
                    # lock here and it is not reentrant.
                    peers._write_identity_proof_locked(
                        project, self.OWNER, self.B,
                        peers.auto_identity(self.B)[1])
                return real(path, digest)

            with patch.object(peers, "_read_identity_proof_file",
                              move_then_read):
                ok, detail = self._claim(project, self.A, "initial")
            self.assertFalse(ok, detail)


class ProofLifecycleTest(unittest.TestCase):
    """A proof outlives endpoints, and is removed only on proved death.

    Two failures are possible here and only one of them is visible. Deleting a
    proof too early erases the only evidence that the current identity exists,
    and `status` and `doctor` fall silent in exactly the window they were built
    for. Never deleting one leaks a file per dead session forever, quietly.

    So reclamation happens on the write path that already holds the lock —
    `rotate_identity_proof`, which is what production actually calls — bounded,
    cheap, and only on positive proof of death.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"

    def _owner(self, pid, start="Mon Sep  1 00:00:00 2026"):
        return f"{pid}:v1:{start}"

    def _proof(self, project, owner, session_id):
        _alias, digest = peers.auto_identity(session_id)
        self.assertTrue(
            peers.write_identity_proof(project, owner, session_id, digest))
        return peers.identity_proof_path(project, owner)

    def _rotate(self, project, owner, session_id):
        _alias, digest = peers.auto_identity(session_id)
        return peers.rotate_identity_proof(project, owner, session_id, digest)

    _reaped = 0

    @classmethod
    def _dead_pid(cls):
        """A pid that raises ProcessLookupError: started, exited and reaped.

        A real child rather than a made-up number. An arbitrary integer could
        belong to a live process on the machine running this, and the test
        would then measure the opposite of what it claims.
        """
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        cls._reaped += 1
        return child.pid

    def _sorting_after(self, pid, floor):
        """An owner key for `pid` whose proof filename sorts after `floor`."""
        for n in range(60 * 60 * 24):
            owner = self._owner(pid, f"Wed Sep  3 {n // 3600:02d}:"
                                     f"{(n // 60) % 60:02d}:{n % 60:02d} 2026")
            if peers._owner_digest(owner) > floor:
                return owner
        raise AssertionError("no owner key digest sorted after the fixture")

    def test_proof_lifecycle_survives_a_zero_endpoint_window(self):
        """B is current and owns no endpoint at all. Deleting its proof here
        because the owner has no automatic peer would erase the only evidence
        that B exists — which is the reconnect window, not garbage."""
        with tempfile.TemporaryDirectory() as project:
            owner = self._owner(os.getpid())
            self._proof(project, owner, self.A)
            outcome = self._rotate(project, owner, self.B)
            self.assertTrue(outcome.ok)
            state, record = peers._read_identity_proof_file(
                peers.identity_proof_path(project, owner),
                peers._owner_digest(owner))
            self.assertEqual(state, "valid", "the current proof survives")
            self.assertEqual(record.get("session_id"), self.B)
            self.assertEqual(peers.read_peers(project, "claude"), [],
                             "the window under test has zero endpoints")

    def test_proof_lifecycle_reclaims_a_dead_owner_through_the_rotation(self):
        """Driven through `rotate_identity_proof` — the call production makes.
        A reclaimer with no real caller would let a unit test pass while
        production collected nothing, forever. The hook above that call has its
        own gate in `ProofLifecycleSurfaceTest.test_proof_lifecycle_the_real_
        hook_collects`; this one names the transaction it actually drives."""
        with tempfile.TemporaryDirectory() as project:
            dead = self._owner(self._dead_pid())
            stale = self._proof(project, dead, self.A)
            self.assertTrue(os.path.exists(stale))
            self._rotate(project, self._owner(os.getpid()), self.B)
            self.assertFalse(os.path.exists(stale),
                             "a proved-dead owner's proof is collected")

    def test_proof_lifecycle_reclamation_spares_a_live_successor(self):
        """Only ProcessLookupError proves death. A live owner, and the owner
        doing the rotating, both survive every sweep."""
        with tempfile.TemporaryDirectory() as project:
            mine = self._owner(os.getpid())
            neighbour = self._owner(os.getppid())
            kept = self._proof(project, neighbour, self.A)
            self._rotate(project, mine, self.B)
            self.assertTrue(os.path.exists(kept),
                            "a live neighbour is never reclaimed")
            self.assertTrue(
                os.path.exists(peers.identity_proof_path(project, mine)),
                "a rotation never collects the proof it just wrote")

    def test_proof_lifecycle_sweep_makes_progress_past_live_records(self):
        """Scanning the first eight of a sorted inventory on every write is a
        latency bound with no progress guarantee: eight live records at the
        front would starve a dead one behind them forever. The cursor is what
        turns the bound into progress."""
        with tempfile.TemporaryDirectory() as project:
            mine = self._owner(os.getpid())
            live = [self._owner(os.getpid(), f"Mon Sep  1 00:00:{n:02d} 2026")
                    for n in range(1, 12)]
            for owner in live:
                self._proof(project, owner, self.A)
            # Proof files are named from a digest, so their sort order is
            # effectively random — and with 13 records the dead one landed
            # inside the fixed first-eight window most of the time. Measured
            # over 20 fresh fixtures, a cursor-less sweep survived 14 of them.
            # Placing the dead record beyond every other digest is what makes
            # this test about the cursor rather than about luck.
            dead_owner = self._sorting_after(
                self._dead_pid(),
                max(peers._owner_digest(o) for o in live + [mine]))
            dead = self._proof(project, dead_owner, self.A)
            for attempt in range(12):
                self._rotate(project, mine, self.B)
                if not os.path.exists(dead):
                    break
            self.assertFalse(os.path.exists(dead),
                             "a dead record behind eleven live ones is still "
                             f"reached; {attempt + 1} writes were not enough")
            for owner in live:
                self.assertTrue(
                    os.path.exists(peers.identity_proof_path(project, owner)),
                    "every live record survives the sweep that passed it")

    def test_proof_lifecycle_a_malformed_sweep_cursor_resets_to_the_start(self):
        """The cursor is an optimisation with no correctness role. Refusing to
        sweep because it cannot be parsed would stall reclamation forever over
        a file nobody can repair."""
        with tempfile.TemporaryDirectory() as project:
            mine = self._owner(os.getpid())
            dead = self._proof(project, self._owner(self._dead_pid()), self.A)
            path = peers.identity_sweep_cursor_path(project)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{not json")
            self._rotate(project, mine, self.B)
            self.assertFalse(os.path.exists(dead),
                             "a malformed cursor resets rather than refusing")

    def test_proof_lifecycle_sweep_uses_only_cheap_death_evidence(self):
        """The five-second `ps` path must never be reached from a hook. Death
        here is `ProcessLookupError` and nothing else: a recycled pid answers
        signal 0 and is conservatively kept, which is the safe direction."""
        with tempfile.TemporaryDirectory() as project:
            self._proof(project, self._owner(self._dead_pid()), self.A)
            # A *live* owner in the same window is what makes this bite. Asked
            # about a dead pid alone, even the `ps` path short-circuits on
            # `alive()` and never reaches a fingerprint — so a sweep rewritten
            # to use it would have passed this guard untouched. The live record
            # is the one where a start-time comparison would happen.
            self._proof(project, self._owner(os.getpid(), "Tue Sep  2 "
                                             "00:00:00 2026"), self.A)
            # Recorded, never raised. The sweep swallows every exception a
            # record can produce — that is its promise to the rotation above
            # it — so a guard that raises is a guard the sweep hides. Asking
            # afterwards whether the call happened is observable either way.
            calls = []
            with patch.object(peers, "_process_birth",
                              side_effect=lambda *a: calls.append(a)), \
                 patch.object(peers, "_process_info",
                              side_effect=lambda *a: calls.append(a)):
                for _ in range(4):
                    self._rotate(project, self._owner(os.getpid()), self.B)
            self.assertEqual(calls, [],
                             "the five-second `ps` path is never reached from "
                             "a hook; death here is ProcessLookupError alone")

    def test_proof_lifecycle_sweep_stops_on_its_cooperative_budget(self):
        """A fake clock, never a real elapsed time: the budget is cooperative
        rather than a wall-clock cap, and asserting on a real scheduler would
        be flaky. At least one record of progress is made regardless."""
        with tempfile.TemporaryDirectory() as project:
            mine = self._owner(os.getpid())
            owners = [self._owner(self._dead_pid()) for _ in range(4)]
            paths = [self._proof(project, owner, self.A) for owner in owners]
            ticks = iter([0.0] + [1.0] * 50)
            with patch.object(peers, "_sweep_clock",
                              side_effect=lambda: next(ticks)):
                self._rotate(project, mine, self.B)
            self.assertTrue(peers._read_sweep_cursor(project),
                            "one record of progress was made and recorded")
            remaining = [path for path in paths if os.path.exists(path)]
            # Progress is a record examined, which is not the same as a record
            # reclaimed: the one this window reached may be the rotation's own
            # protected proof. What the budget promises is that the loop stopped
            # between records — without it, one window would have swept all four.
            self.assertGreaterEqual(
                len(remaining), len(paths) - 1,
                "the budget stops the loop after one record, not after eight")
            # And the same project, on a real clock, still converges.
            for _ in range(len(paths) + 2):
                self._rotate(project, mine, self.B)
            self.assertEqual([path for path in paths if os.path.exists(path)],
                             [], "the budget delays reclamation, never ends it")

    def test_proof_lifecycle_a_failing_sweep_never_costs_the_rotation(self):
        """The sweep runs after the proof has committed. Every error it can
        raise is swallowed there, because a hook that already made the routing
        decision correct must not then report failure."""
        with tempfile.TemporaryDirectory() as project:
            mine = self._owner(os.getpid())
            alias_a, digest_a = peers.auto_identity(self.A)
            peers.register(project, "claude", alias_a,
                           os.path.join(project, alias_a + ".sock"),
                           pid=os.getpid(), owner_key=mine,
                           identity_digest=digest_a, mode="initial")
            peers.write_session(project, "claude", alias_a, self.A,
                                f"/t/{alias_a}.jsonl", mine, digest_a, True)
            peers.write_identity_proof(project, mine, self.A, digest_a)
            printed = io.StringIO()
            with patch.object(peers.os, "replace",
                              side_effect=self._replace_that_fails_the_cursor(
                                  project)), \
                 contextlib.redirect_stderr(printed):
                outcome = self._rotate(project, mine, self.B)
            self.assertTrue(outcome.ok, "the rotation still succeeds")
            state, record = peers._read_identity_proof_file(
                peers.identity_proof_path(project, mine),
                peers._owner_digest(mine))
            self.assertEqual(state, "valid")
            self.assertEqual(record.get("session_id"), self.B,
                             "the current proof is the one just written")
            self.assertEqual(list(outcome.withdrawn), [alias_a],
                             "the session the rotation outgrew is still retired")
            self.assertNotIn(project, printed.getvalue(),
                             "a swallowed sweep failure prints no private path")

    def _replace_that_fails_the_cursor(self, project):
        """`os.replace` that works everywhere except the sweep cursor."""
        real = os.replace
        cursor = peers.identity_sweep_cursor_path(project)

        def replace(src, dst, *args, **kwargs):
            if str(dst) == cursor:
                raise OSError(errno.EIO, "Input/output error")
            return real(src, dst, *args, **kwargs)

        return replace


class ProofDecodeTest(unittest.TestCase):
    """Bytes that are not UTF-8 are a torn record, not an unclassified event.

    `UnicodeDecodeError` subclasses `ValueError`, not `OSError`, so it escapes
    both arms of the read and travels out of every caller: the inventory
    `status` and `doctor` read, the resolver's proof lookup, and — worst — the
    sweep, which runs after the rotation has committed and promises never to
    raise. There it is not a crash but a wedge: the hook's `except Exception`
    catches it before the new session half is written, so the new identity
    never becomes routable, and the sweep bails before advancing its cursor, so
    every later hook in that project repeats the same failure forever.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"

    def _torn(self, project, owner):
        path = peers.identity_proof_path(project, owner)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(b'{"kind": "\xff\xfe\x00bad"}')
        return path

    def test_proof_decode_a_non_utf8_record_reads_invalid(self):
        with tempfile.TemporaryDirectory() as project:
            owner = f"{os.getpid()}:v1:Mon Sep  1 00:00:00 2026"
            self._torn(project, owner)
            state, record = peers.read_identity_proof(project, owner)
            self.assertEqual(state, "invalid",
                             "a torn record is invalid, never unclassified")
            self.assertIsNone(record)

    def test_proof_decode_the_inventory_survives_a_torn_record(self):
        with tempfile.TemporaryDirectory() as project:
            self._torn(project, f"{os.getpid()}:v1:Mon Sep  1 00:00:00 2026")
            inventory = peers.identity_proofs(project)
            self.assertEqual(inventory.proofs, ())
            self.assertEqual(inventory.completeness, "unknown",
                             "one unreadable neighbour is a lower bound, not "
                             "a crash and not a confident zero")

    def test_proof_decode_the_sweep_never_raises(self):
        with tempfile.TemporaryDirectory() as project:
            mine = f"{os.getpid()}:v1:Mon Sep  1 00:00:00 2026"
            self._torn(project, f"{os.getpid()}:v1:Tue Sep  2 00:00:00 2026")
            _alias, digest = peers.auto_identity(self.B)
            outcome = peers.rotate_identity_proof(project, mine, self.B, digest)
            self.assertTrue(outcome.ok, "the rotation still commits")

    def test_proof_decode_the_sweep_cursor_still_advances(self):
        """Bailing before the cursor write is what turns one bad file into a
        permanent wedge: the same window is retried forever and nothing behind
        it is ever reached."""
        with tempfile.TemporaryDirectory() as project:
            mine = f"{os.getpid()}:v1:Mon Sep  1 00:00:00 2026"
            self._torn(project, f"{os.getpid()}:v1:Tue Sep  2 00:00:00 2026")
            _alias, digest = peers.auto_identity(self.B)
            peers.rotate_identity_proof(project, mine, self.B, digest)
            self.assertTrue(peers._read_sweep_cursor(project),
                            "the sweep recorded where it got to")

    def test_proof_decode_an_out_of_range_pid_is_not_proved_dead(self):
        """`OWNER_PATTERN` puts no ceiling on the pid, and `os.kill` raises
        `OverflowError` — not an `OSError` — above the platform's signed int.
        Unproved is the only safe answer, and it must not escape the sweep."""
        self.assertFalse(peers._proved_dead(
            "2147483653:v1:Mon Sep  1 00:00:00 2026"))


class RetiredHalfTombstoneTest(unittest.TestCase):
    """Withdrawal must supersede a half without erasing that it existed.

    The contract asks for two things that its first implementation could not
    both keep. Rotation withdraws every same-owner stale automatic session
    half; the listener self-retires only on `PROVED_STALE`; and cleanup is
    "already guaranteed by the first stale delivery attempt". Deleting the half
    outright makes the verdict `UNREADY` — indistinguishable from a listener
    whose first hook has not run yet — so the guaranteed cleanup never happens
    and the outgrown socket lives until its process exits.

    A tombstone keeps both. The half is gone, so nothing can join it; the
    evidence that it was once joined stays, so "never joined" and "joined, then
    outgrown" remain two different facts.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"

    def _owner(self):
        owner = peers.owner_key()
        self.assertIsNotNone(owner)
        return owner

    def _joined(self, project, owner, session_id):
        alias, digest = peers.auto_identity(session_id)
        peers.register(project, "claude", alias,
                       os.path.join(project, alias + ".sock"),
                       pid=os.getpid(), owner_key=owner,
                       identity_digest=digest, mode="initial")
        peers.write_session(project, "claude", alias, session_id,
                            f"/t/{alias}.jsonl", owner, digest, True)
        return alias, digest

    def _verdict(self, project, alias):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
        import antiphon
        peer = next((p for p in peers.read_peers(project, "claude")
                     if p.get("name") == alias), None)
        if peer is None:
            return "NO-RECORD"
        return antiphon.automatic_verdict(
            project, "claude", peer,
            peers.read_identity_proof(project, peer.get("owner")))

    def test_retired_half_a_rotation_leaves_the_outgrown_peer_proved_stale(self):
        with tempfile.TemporaryDirectory() as project:
            owner = self._owner()
            alias_a, _digest_a = self._joined(project, owner, self.A)
            peers.write_identity_proof(project, owner, self.A,
                                       peers.auto_identity(self.A)[1])
            self.assertEqual(self._verdict(project, alias_a), "READY")
            peers.rotate_identity_proof(project, owner, self.B,
                                        peers.auto_identity(self.B)[1])
            self.assertFalse(
                os.path.exists(peers._session_file(project, "claude", alias_a)),
                "the half is still withdrawn: nothing may join it")
            self.assertEqual(
                self._verdict(project, alias_a), "PROVED_STALE",
                "and the listener can now tell that it was outgrown rather "
                "than never joined")

    def test_retired_half_a_peer_that_never_joined_stays_unready(self):
        """The bootstrap case the verdict exists to keep apart. A fresh
        endpoint with no half and no tombstone must not read stale, or the
        listener kills itself before its first hook ever runs."""
        with tempfile.TemporaryDirectory() as project:
            owner = self._owner()
            alias, digest = peers.auto_identity(self.B)
            peers.register(project, "claude", alias,
                           os.path.join(project, alias + ".sock"),
                           pid=os.getpid(), owner_key=owner,
                           identity_digest=digest, mode="initial")
            peers.write_identity_proof(project, owner, self.B, digest)
            self.assertEqual(self._verdict(project, alias), "UNREADY")

    def test_retired_half_rejoining_clears_the_tombstone(self):
        """A host can resume a session id. The evidence is about the current
        state, not a permanent mark."""
        with tempfile.TemporaryDirectory() as project:
            owner = self._owner()
            alias_a, digest_a = self._joined(project, owner, self.A)
            peers.write_identity_proof(project, owner, self.A, digest_a)
            peers.rotate_identity_proof(project, owner, self.B,
                                        peers.auto_identity(self.B)[1])
            self.assertEqual(self._verdict(project, alias_a), "PROVED_STALE")
            peers.rotate_identity_proof(project, owner, self.A, digest_a)
            peers.write_session(project, "claude", alias_a, self.A,
                                f"/t/{alias_a}.jsonl", owner, digest_a, True)
            self.assertEqual(self._verdict(project, alias_a), "READY",
                             "a rejoined peer is not haunted by its tombstone")
            # The verdict alone cannot prove this: it consults the tombstone
            # only when the half is missing, so with the half back it
            # short-circuits and would read READY whether or not the unlink
            # happened. Assert the file directly, which is what this test is
            # named for.
            self.assertFalse(
                os.path.exists(
                    peers.retired_half_path(project, "claude", alias_a)),
                "writing a half is the event that ends `withdrawn`")

    def test_retired_half_a_tombstone_without_an_endpoint_is_collected(self):
        """Once the listener has withdrawn its endpoint the tombstone has no
        reader left. Collected on the same locked pass that writes them, so it
        costs no new traversal and cannot grow without bound."""
        with tempfile.TemporaryDirectory() as project:
            owner = self._owner()
            alias_a, digest_a = self._joined(project, owner, self.A)
            peers.write_identity_proof(project, owner, self.A, digest_a)
            peers.rotate_identity_proof(project, owner, self.B,
                                        peers.auto_identity(self.B)[1])
            tombstone = peers.retired_half_path(project, "claude", alias_a)
            self.assertTrue(os.path.exists(tombstone))
            peers.unregister(project, "claude", alias_a, pid=os.getpid())
            peers.rotate_identity_proof(project, owner, self.B,
                                        peers.auto_identity(self.B)[1])
            self.assertFalse(os.path.exists(tombstone),
                             "an endpointless tombstone has no reader")

    def test_retired_half_another_owner_s_peer_is_never_marked(self):
        """Withdrawal is scoped to one owner, and so is its evidence."""
        with tempfile.TemporaryDirectory() as project:
            owner = self._owner()
            other = f"{os.getppid()}:v1:Mon Sep  1 00:00:00 2026"
            alias_a, digest_a = self._joined(project, other, self.A)
            peers.write_identity_proof(project, owner, self.A, digest_a)
            peers.rotate_identity_proof(project, owner, self.B,
                                        peers.auto_identity(self.B)[1])
            self.assertFalse(
                os.path.exists(
                    peers.retired_half_path(project, "claude", alias_a)),
                "another owner's peer is not this rotation's business")


class TombstoneIsPositiveEvidenceTest(unittest.TestCase):
    """A tombstone authorises the one destructive action here, so it must prove
    a rotation happened — not merely that one once did.

    Two ways the first version was weaker than the action it authorised.

    `_session_address` returns None for six different reasons, and only one of
    them is "no half was ever written". Upgrading all six to PROVED_STALE meant
    an `EIO` on the half destroyed a healthy endpoint, which §2 forbids in so
    many words: an unreadable record is evidence of nothing.

    And a tombstone that names no session cannot tell "the owner moved on from
    me" from "the owner came back to me". A host that resumes a session id —
    or a listener that reconnects while a stale tombstone is still on disk —
    then reads stale about itself and retires at bootstrap.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"
    C = "0199a1b2-3333-7000-8000-00000000000c"

    def _verdict(self, project, alias):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
        import antiphon
        peer = next((p for p in peers.read_peers(project, "claude")
                     if p.get("name") == alias), None)
        if peer is None:
            return "NO-RECORD"
        return antiphon.automatic_verdict(
            project, "claude", peer,
            peers.read_identity_proof(project, peer.get("owner")))

    def _rotated(self, project):
        """The real state: A joined, rotation to B, tombstone left behind."""
        owner = peers.owner_key()
        alias, digest = peers.auto_identity(self.A)
        peers.register(project, "claude", alias,
                       os.path.join(project, alias + ".sock"),
                       pid=os.getpid(), owner_key=owner,
                       identity_digest=digest, mode="initial")
        peers.write_session(project, "claude", alias, self.A,
                            f"/t/{alias}.jsonl", owner, digest, True)
        peers.write_identity_proof(project, owner, self.A, digest)
        peers.rotate_identity_proof(project, owner, self.B,
                                    peers.auto_identity(self.B)[1])
        return owner, alias

    def test_tombstone_an_unreadable_half_never_authorises_retirement(self):
        with tempfile.TemporaryDirectory() as project:
            owner, alias = self._rotated(project)
            self.assertEqual(self._verdict(project, alias), "PROVED_STALE")
            # The half comes back, unreadable. Nothing about this says the
            # owner moved on; it says one read failed.
            half = peers._session_file(project, "claude", alias)
            with open(half, "w", encoding="utf-8") as stream:
                stream.write("{}")
            os.chmod(half, 0)
            try:
                self.assertNotEqual(
                    self._verdict(project, alias), "PROVED_STALE",
                    "an unreadable record must not destroy a listener")
                # `chmod 000` does not stop `os.stat`, so the assertion above
                # exercises the existence check rather than the read-failure
                # branch it is named for. A directory in place of the half is
                # what makes `os.stat` succeed and the read fail — and an
                # unreadable *parent* is what makes `os.stat` itself fail.
                os.chmod(half, 0o600)
                os.unlink(half)
                os.makedirs(half)
                self.assertFalse(
                    peers.session_half_missing(project, "claude", alias),
                    "a directory in place of the half is not an absent half")
                self.assertNotEqual(self._verdict(project, alias),
                                    "PROVED_STALE")
            finally:
                if os.path.isdir(half):
                    os.rmdir(half)

    def test_tombstone_a_torn_half_never_authorises_retirement(self):
        with tempfile.TemporaryDirectory() as project:
            owner, alias = self._rotated(project)
            self._write_half(project, alias, "{")
            self.assertEqual(self._verdict(project, alias), "UNREADY",
                             "a torn half reads exactly as it did before the "
                             "tombstone existed")

    def test_tombstone_a_half_from_another_owner_never_authorises_retirement(self):
        with tempfile.TemporaryDirectory() as project:
            owner, alias = self._rotated(project)
            _n, digest = peers.auto_identity(self.A)
            self._write_half(project, alias, json.dumps(
                {"kind": "claude", "name": alias,
                 "owner": "4243:v1:Mon Sep  1 00:00:00 2026",
                 "session_id": self.A, "automatic": True,
                 "identity_digest": digest}))
            self.assertEqual(self._verdict(project, alias), "UNREADY")

    def test_tombstone_the_owner_coming_back_is_not_a_rotation(self):
        """The bootstrap failure this design exists to avoid. The tombstone
        names the session it withdrew; when the proof names that same session
        again, nothing was outgrown and nothing may be retired."""
        with tempfile.TemporaryDirectory() as project:
            owner, alias = self._rotated(project)
            self.assertEqual(self._verdict(project, alias), "PROVED_STALE")
            peers.rotate_identity_proof(project, owner, self.A,
                                        peers.auto_identity(self.A)[1])
            self.assertNotEqual(
                self._verdict(project, alias), "PROVED_STALE",
                "the owner is on this identity again; the stale tombstone must "
                "not retire the listener that just reconnected")

    def _tombstoned(self, project, **over):
        """The state a real rotation leaves: proof names B, tombstone names A.

        Every negative tombstone fixture has to be built on this. Written with
        the proof naming A instead, the `current_session_id != withdrawn` guard
        short-circuits first and the field under test is never consulted — so a
        fixture meant to protect the owner, kind or digest check protects
        nothing, and dropping that check leaves the whole suite green.
        """
        owner = peers.owner_key()
        alias, digest = peers.auto_identity(self.A)
        peers.register(project, "claude", alias,
                       os.path.join(project, alias + ".sock"),
                       pid=os.getpid(), owner_key=owner,
                       identity_digest=digest, mode="initial")
        peers.write_identity_proof(project, owner, self.B,
                                   peers.auto_identity(self.B)[1])
        record = {"version": 1, "kind": "claude", "owner": owner,
                  "identity_digest": digest, "session_id": self.A}
        record.update(over)
        path = peers.retired_half_path(project, "claude", alias)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        return owner, alias

    def test_tombstone_every_field_is_load_bearing(self):
        """One case per field, each on the rotated state so the field decides.

        Without this the guards beside `session_id` were dead weight: measured,
        dropping the owner, kind, digest or id-derives-digest check left all
        1020 tests green.
        """
        cases = {
            "owner names another CLI root":
                {"owner": "4243:v1:Mon Sep  1 00:00:00 2026"},
            "kind is not claude": {"kind": "codex"},
            "digest names another identity": {"identity_digest": "0" * 64},
            # A third session, not the current one: with `self.B` the
            # current-session guard answers first and the binding under test is
            # never consulted. That is the same short-circuit the previous
            # round fixed one guard further up.
            "withdrawn id does not derive the digest": {"session_id": self.C},
            "withdrawn id is not canonical": {"session_id": "not-a-uuid"},
            "version is a float": {"version": 1.0},
            "version is a bool": {"version": True},
        }
        for name, over in cases.items():
            with self.subTest(field=name):
                with tempfile.TemporaryDirectory() as project:
                    _owner, alias = self._tombstoned(project, **over)
                    self.assertEqual(
                        self._verdict(project, alias), "UNREADY",
                        f"{name}: a tombstone this cannot trust must not "
                        "authorise retirement")

    def test_tombstone_the_rotated_state_itself_is_proved_stale(self):
        """The positive control beside the seven negatives above: with every
        field right, the same construction does reach PROVED_STALE — so those
        seven fail for the field under test and not for the fixture."""
        with tempfile.TemporaryDirectory() as project:
            _owner, alias = self._tombstoned(project)
            self.assertEqual(self._verdict(project, alias), "PROVED_STALE")

    def test_tombstone_records_the_session_it_withdrew(self):
        with tempfile.TemporaryDirectory() as project:
            owner, alias = self._rotated(project)
            with open(peers.retired_half_path(project, "claude", alias),
                      encoding="utf-8") as stream:
                record = json.load(stream)
            self.assertEqual(record.get("session_id"), self.A,
                             "positive evidence: which session was withdrawn")

    def test_tombstone_is_written_before_the_half_is_unlinked(self):
        """A crash between the two recreates the bug the tombstone fixes, and
        the rotation would have reported the withdrawal as done."""
        with tempfile.TemporaryDirectory() as project:
            owner = peers.owner_key()
            alias, digest = peers.auto_identity(self.A)
            peers.register(project, "claude", alias,
                           os.path.join(project, alias + ".sock"),
                           pid=os.getpid(), owner_key=owner,
                           identity_digest=digest, mode="initial")
            peers.write_session(project, "claude", alias, self.A,
                                f"/t/{alias}.jsonl", owner, digest, True)
            peers.write_identity_proof(project, owner, self.A, digest)
            real = peers.os.replace
            tombstone = peers.retired_half_path(project, "claude", alias)

            def failing(src, dst, *a, **k):
                if str(dst) == tombstone:
                    raise OSError(errno.EIO, "Input/output error")
                return real(src, dst, *a, **k)

            with patch.object(peers.os, "replace", side_effect=failing):
                outcome = peers.rotate_identity_proof(
                    project, owner, self.B, peers.auto_identity(self.B)[1])
            self.assertTrue(os.path.exists(
                peers._session_file(project, "claude", alias)),
                "the half is kept when its tombstone could not be written")
            self.assertEqual(list(outcome.withdrawn), [],
                             "and the rotation does not claim a withdrawal it "
                             "could not make evidence of")

    def _write_half(self, project, alias, body):
        path = peers._session_file(project, "claude", alias)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(body)


class TombstoneRecordCeilingTest(unittest.TestCase):
    """The strictness the proof reader has, the tombstone reader needs too.

    Both authorise the same destructive action, and the tombstone's version
    check accepted a JSON float where the proof's refuses one. Parity held only
    because both languages were lax in the same place, which is agreement about
    the wrong answer.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"

    def test_tombstone_a_float_version_is_not_a_tombstone(self):
        _alias, digest = peers.auto_identity(self.A)
        record = json.loads('{"version": 1.0, "kind": "claude", '
                            f'"owner": "4242:v1:Mon Sep  1 00:00:00 2026", '
                            f'"identity_digest": "{digest}", '
                            f'"session_id": "{self.A}"}}')
        self.assertFalse(
            peers._valid_retired_half(
                record, "4242:v1:Mon Sep  1 00:00:00 2026", digest),
            "a version that is not exactly the current integer is not one")

    def test_tombstone_a_true_version_is_not_a_tombstone(self):
        """`True == 1` in Python, and a bool must never pass for a version."""
        _alias, digest = peers.auto_identity(self.A)
        record = {"version": True, "kind": "claude",
                  "owner": "4242:v1:Mon Sep  1 00:00:00 2026",
                  "identity_digest": digest, "session_id": self.A}
        self.assertFalse(peers._valid_retired_half(
            record, "4242:v1:Mon Sep  1 00:00:00 2026", digest))


class WithdrawalPathValidationTest(unittest.TestCase):
    """Two loops turn a directory name off disk into a path.

    `_scan` validates the same way. These did not, and the guards that fixed
    that had no test — removing both left the whole suite green.
    """

    def test_withdrawal_ignores_a_peer_directory_with_an_unusable_name(self):
        with tempfile.TemporaryDirectory() as project:
            owner = peers.owner_key()
            bad = os.path.join(peers.peers_dir(project), "claude-Not A Name")
            os.makedirs(bad, exist_ok=True)
            with open(os.path.join(bad, "session.json"), "w",
                      encoding="utf-8") as stream:
                json.dump({"kind": "claude", "name": "Not A Name",
                           "owner": owner, "session_id": "x",
                           "automatic": True, "identity_digest": "0" * 64},
                          stream)
            with open(os.path.join(bad, "retired.json"), "w",
                      encoding="utf-8") as stream:
                stream.write("{}")
            session = "0199a1b2-2222-7000-8000-00000000000b"
            outcome = peers.rotate_identity_proof(
                project, owner, session, peers.auto_identity(session)[1])
            self.assertTrue(outcome.ok)
            self.assertEqual(list(outcome.withdrawn), [],
                             "a name that could never be a peer is not one to "
                             "withdraw")
            self.assertTrue(os.path.exists(os.path.join(bad, "session.json")),
                            "and nothing under it is touched")
            self.assertTrue(os.path.exists(os.path.join(bad, "retired.json")))


class TombstoneReadSkewTest(unittest.TestCase):
    """The proof is read before the filesystem is observed, and it can move.

    `record_claude_session` rotates the proof and only then writes the half, so
    during an A→B→A resume there is a window where the proof names A, the half
    is still gone and the tombstone still names A. A reader holding the earlier
    snapshot (B) sees a missing half plus a valid tombstone and answers
    PROVED_STALE about the identity that has just become current again —
    costing that terminal a reconnect it did not need.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"

    def test_tombstone_read_skew_a_moved_proof_is_not_a_rotation(self):
        with tempfile.TemporaryDirectory() as project:
            owner = peers.owner_key()
            alias, digest = peers.auto_identity(self.A)
            peers.register(project, "claude", alias,
                           os.path.join(project, alias + ".sock"),
                           pid=os.getpid(), owner_key=owner,
                           identity_digest=digest, mode="initial")
            peers.write_identity_proof(project, owner, self.A, digest)
            path = peers.retired_half_path(project, "claude", alias)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "kind": "claude", "owner": owner,
                           "identity_digest": digest,
                           "session_id": self.A}, stream)
            # The caller's snapshot still says B; the proof on disk says A.
            self.assertFalse(
                peers.retired_half(project, "claude", alias, owner, digest,
                                   self.B),
                "a decision taken against a proof that has since moved is not "
                "a decision this may act on")


class WithdrawalRequiresAValidAutomaticHalfTest(unittest.TestCase):
    """Withdrawal deletes a record, so it needs the same proof everything else
    here needs before deleting anything.

    The predicate was: same owner, and some string digest that differs from the
    current one. That admits a half that is not automatic at all, one whose
    digest derives a different alias than the directory it sits in, and one
    whose session id is not canonical — and for the last, no tombstone is
    written either, so the record is silently gone and the listener that owned
    it can never learn why.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"

    def _half(self, project, alias, record):
        path = peers._session_file(project, "claude", alias)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        return path

    def test_withdrawal_leaves_a_half_it_cannot_structurally_trust(self):
        owner = peers.owner_key()
        alias, digest = peers.auto_identity(self.A)
        cases = {
            "not marked automatic": {
                "kind": "claude", "name": alias, "owner": owner,
                "session_id": self.A, "identity_digest": "0" * 64},
            "digest derives another alias": {
                "kind": "claude", "name": alias, "owner": owner,
                "session_id": self.A, "automatic": True,
                "identity_digest": peers.auto_identity(self.B)[1]},
            "session id is not canonical": {
                "kind": "claude", "name": alias, "owner": owner,
                "session_id": "not-a-uuid", "automatic": True,
                "identity_digest": "0" * 64},
            # The silent path. Five lines below the guard, the tombstone write
            # is `if valid_session_id(withdrawn) and not _write(...)`: with no
            # `session_id` at all that `and` short-circuits, the `continue` is
            # skipped, and the record is unlinked with nothing written to say
            # why — the exact "vanished with nothing to explain it" case the
            # tombstone was added to close.
            "session id is absent entirely": {
                "kind": "claude", "name": alias, "owner": owner,
                "automatic": True, "identity_digest": digest},
            "digest is not a digest": {
                "kind": "claude", "name": alias, "owner": owner,
                "session_id": self.A, "automatic": True,
                "identity_digest": "zz"},
        }
        for name, record in cases.items():
            with self.subTest(half=name):
                with tempfile.TemporaryDirectory() as project:
                    path = self._half(project, alias, record)
                    before = pathlib.Path(path).read_bytes()
                    outcome = peers.rotate_identity_proof(
                        project, owner, self.B,
                        peers.auto_identity(self.B)[1])
                    self.assertTrue(outcome.ok)
                    self.assertTrue(
                        os.path.exists(path),
                        f"{name}: a record this cannot read is not one to "
                        "delete")
                    self.assertEqual(before, pathlib.Path(path).read_bytes(),
                                     f"{name}: byte-identical")
                    self.assertEqual(list(outcome.withdrawn), [], name)

    def test_withdrawal_still_takes_a_structurally_valid_stale_half(self):
        """The positive control: the case it is meant to take, it still takes."""
        with tempfile.TemporaryDirectory() as project:
            owner = peers.owner_key()
            alias, digest = peers.auto_identity(self.A)
            # With no endpoint the tombstone has no reader and is collected on
            # the same pass that writes it, which is right — so the positive
            # control has to be the realistic state, not half of it.
            peers.register(project, "claude", alias,
                           os.path.join(project, alias + ".sock"),
                           pid=os.getpid(), owner_key=owner,
                           identity_digest=digest, mode="initial")
            path = self._half(project, alias, {
                "kind": "claude", "name": alias, "owner": owner,
                "session_id": self.A, "automatic": True,
                "identity_digest": digest})
            outcome = peers.rotate_identity_proof(
                project, owner, self.B, peers.auto_identity(self.B)[1])
            self.assertEqual(list(outcome.withdrawn), [alias])
            self.assertFalse(os.path.exists(path))
            self.assertTrue(os.path.exists(
                peers.retired_half_path(project, "claude", alias)))


class SweepWindowIsBoundedTest(unittest.TestCase):
    """The per-rotation latency bound, asserted rather than assumed.

    The progress test retries up to twelve rotations and stops as soon as the
    dead record goes, so a window of any size passes it. The bound is the other
    half of the contract: one rotation examines at most eight records, however
    many are there, because this runs inside a hook.
    """

    A = "8261c119-2c20-4bf4-87ab-f152ac87dbda"
    B = "0199a1b2-2222-7000-8000-00000000000b"

    def test_sweep_window_examines_at_most_eight_records_per_rotation(self):
        with tempfile.TemporaryDirectory() as project:
            owner = peers.owner_key()
            for n in range(1, 26):
                peers.write_identity_proof(
                    project, f"{os.getpid()}:v1:Mon Sep  1 00:00:{n:02d} 2026",
                    self.A, peers.auto_identity(self.A)[1])
            reads = []
            real = peers._read_identity_proof_file

            def counted(path, digest):
                reads.append(path)
                return real(path, digest)

            with patch.object(peers, "_read_identity_proof_file",
                              side_effect=counted):
                peers.rotate_identity_proof(project, owner, self.B,
                                            peers.auto_identity(self.B)[1])
            swept = [path for path in reads
                     if path.startswith(peers.identity_proofs_dir(project))]
            # The rotation's own capture and re-read of its proof are not sweep
            # reads; the window is what the sweep itself examined.
            self.assertLessEqual(
                len(swept), peers.IDENTITY_SWEEP_WINDOW + 2,
                "one rotation examines a bounded window, not the inventory")


class PidCeilingHalvesAreSeparateTest(unittest.TestCase):
    """Two guards, each of which alone is invisible behind the other.

    `_pid_of` refuses a pid above the platform's signed int, and `alive()`
    catches the `OverflowError` `os.kill` raises for one. Reverting either on
    its own left the whole suite green — the record never reached `alive()`
    because `_pid_of` had already refused it, and `_pid_of`'s ceiling was never
    the thing that stopped the raise. Each is asserted where it acts.
    """

    HUGE = 10 ** 100

    def test_pid_ceiling_refuses_a_pid_no_kernel_hands_out(self):
        self.assertIsNone(peers._pid_of({"pid": self.HUGE}))
        self.assertIsNone(peers._pid_of({"pid": peers.PID_CEILING + 1}))
        self.assertEqual(peers._pid_of({"pid": peers.PID_CEILING}),
                         peers.PID_CEILING)

    def test_pid_ceiling_liveness_answers_rather_than_raises(self):
        """`alive()` is asked directly, past the ceiling that would normally
        keep such a number away from it — the arm exists for the day some other
        caller does the same."""
        self.assertFalse(peers.alive(self.HUGE))
        self.assertFalse(peers.alive(-1))
        self.assertFalse(peers.alive("not a pid"))


OLD_READER_SHA256 = "4bb3ea14ab9415f84a734b16472638c09d0acfd56eba83ee96d11d3ea29a060b"
OLD_READER_COMMITS = ("943da8a", "a076723")   # 0.3.3 as published; the same bytes one release earlier


class FrozenReaderFixtureTest(unittest.TestCase):
    """The cross-version tests drive the reader 0.3.3 actually shipped, not a
    model of it. The fixture is byte-exact, and when git history is at hand the
    blobs are compared too, so a hand edit to the fixture cannot quietly turn
    the old reader into a kinder one."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "peers_0_3_3.py")
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_the_fixture_is_the_shipped_reader(self):
        with open(self.FIXTURE, "rb") as stream:
            self.assertEqual(hashlib.sha256(stream.read()).hexdigest(),
                             OLD_READER_SHA256)

    def test_the_fixture_matches_both_pinned_blobs(self):
        for commit in OLD_READER_COMMITS:
            with self.subTest(commit=commit):
                try:
                    blob = subprocess.run(
                        ["git", "show", f"{commit}:lib/peers.py"],
                        capture_output=True, check=True, timeout=10,
                        cwd=self.REPO).stdout
                except (OSError, subprocess.SubprocessError):
                    self.skipTest("no git history in this checkout")
                self.assertEqual(hashlib.sha256(blob).hexdigest(),
                                 OLD_READER_SHA256)

    def test_the_fixture_imports_and_is_the_old_reader(self):
        from test.fixtures import peers_0_3_3 as old
        self.assertFalse(hasattr(old, "PROCESS_FINGERPRINT_VERSION"),
                         "0.3.3 had no fingerprint generation at all")
        self.assertTrue(callable(old._record_alive))
