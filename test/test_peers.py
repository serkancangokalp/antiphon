import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import peers

import hashlib
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


if __name__ == "__main__":
    unittest.main()
