"""Registry fixtures must exercise their assertion without a CLI ancestor."""
import io
import unittest
from unittest.mock import patch

import test_antiphon
import test_peers


class RegistryFixtureIsolationTest(unittest.TestCase):
    def test_rotation_and_doctor_fixtures_work_without_a_cli_owner(self):
        # Reintroducing a real owner_key() fixture makes these existing tests
        # fail before they reach rotation/doctor, just as on GitHub runners.
        cases = (
            (test_peers.RetiredHalfTombstoneTest,
             "test_retired_half_a_peer_that_never_joined_stays_unready"),
            (test_peers.TombstoneIsPositiveEvidenceTest,
             "test_tombstone_records_the_session_it_withdrew"),
            (test_antiphon.DoctorFingerprintNotesTest,
             "test_doctor_gives_no_claude_remedy_to_an_automatic_codex_record"),
            (test_antiphon.TornProofDoesNotWedgeTheHookTest,
             "test_a_torn_proof_leaves_the_new_identity_routable"),
        )
        for cls, method in cases:
            with self.subTest(case=method):
                output = io.StringIO()
                with patch.object(test_peers.peers, "owner_key", return_value=None):
                    result = unittest.TextTestRunner(stream=output).run(cls(method))
                self.assertTrue(result.wasSuccessful(), output.getvalue())
                self.assertEqual(result.skipped, [], "the fixture must run, not skip")
