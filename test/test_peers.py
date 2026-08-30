import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import peers

import hashlib
import multiprocessing
import subprocess
import tempfile
import unittest
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

    def test_unregister_releases_only_your_own_name(self):
        with tempfile.TemporaryDirectory() as project:
            peers.register(project, "claude", "ui", "/tmp/ui.sock", pid=os.getpid())
            peers.unregister(project, "claude", "ui", pid=os.getppid())
            self.assertEqual([p["name"] for p in peers.read_peers(project)], ["ui"],
                             "another owner's name must not be released")
            peers.unregister(project, "claude", "ui", pid=os.getpid())
            self.assertEqual(peers.read_peers(project), [])


if __name__ == "__main__":
    unittest.main()
