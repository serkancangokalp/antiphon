"""The delivery ledger: what was sent, refused, received and read.

One directory, one file per delivery attempt, written by the sends and updated
with receipts by the page readers. It exists because the tools said
"delivered" when a queue accepted a row nobody drained, and because a refused
Stop-hook line reached a debug log and not the agent that wrote it.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import ledger

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

UUID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"
OTHER = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"
THIRD = "3f7c25a2-2760-455b-a9e5-67e7fdb90b59"
SHA = hashlib.sha256(b"run the suite").hexdigest()


class LedgerTest(unittest.TestCase):

    def _sent(self, project, delivery_id=UUID, **over):
        fields = dict(sender="ui", to_kind="codex", to_alias="build",
                      transport="queue", proof="live", sha256=SHA, size=13,
                      attachment=None, at=1_000.0)
        fields.update(over)
        self.assertTrue(ledger.record_sent(project, delivery_id, **fields))

    def test_a_sent_entry_round_trips_with_every_field_and_nothing_private(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project)
            entry = ledger.read_entry(project, UUID)
            self.assertEqual(entry["state"], "sent")
            self.assertEqual((entry["sender"], entry["to_kind"], entry["to_alias"],
                              entry["transport"], entry["proof"], entry["sha256"],
                              entry["size"], entry["attachment"], entry["sent_at"]),
                             ("ui", "codex", "build", "queue", "live", SHA, 13, None, 1_000.0))
            self.assertIsNone(entry["received_at"])
            path = os.path.join(ledger.ledger_dir(project), UUID + ".json")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(ledger.ledger_dir(project)).st_mode & 0o777, 0o700)
            with open(path, "rb") as stream:
                raw = stream.read().decode()
            self.assertNotIn("run the suite", raw, "never the content")
            self.assertNotIn("antiphon-channel-", raw, "never a route")
            uuids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw))
            self.assertEqual(uuids, {UUID}, "the delivery id is the only uuid-shaped value")
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID])

    def test_an_unknown_transport_outcome_is_durable_and_a_receipt_resolves_it(self):
        self.assertEqual(ledger.LEDGER_VERSION, 2,
                         "the new unknown state must not masquerade as v1")
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_unknown(
                project, UUID, sender="ui", to_kind="claude", to_alias="api",
                transport="channel", sha256=SHA, size=13,
                reason="channel reply was lost", preview="run the suite",
                attachment="message.txt", sender_kind="codex", at=1_000.0))
            entry = ledger.read_entry(project, UUID)
            self.assertEqual((entry["state"], entry["proof"], entry["reason"],
                              entry["preview"], entry["attachment"], entry["version"]),
                             ("unknown", "unproven", "channel reply was lost",
                              "run the suite", "message.txt", 2))
            notice = ledger.pending_notices(project, "codex", "ui")
            self.assertEqual([delivery for delivery, _text in notice], [UUID])
            self.assertIn("outcome is unknown", notice[0][1])
            self.assertIn("do not retry automatically", notice[0][1])
            self.assertEqual(
                [waiting["id"] for waiting, _age in
                 ledger.awaiting_receipt(project, 1_010.0)],
                [UUID])

            missing_kind = dict(entry, id=OTHER)
            del missing_kind["sender_kind"]
            with open(os.path.join(ledger.ledger_dir(project), OTHER + ".json"),
                      "w") as f:
                json.dump(missing_kind, f)
            self.assertIsNone(
                ledger.read_entry(project, OTHER),
                "v2 cannot infer the sender kind of a same-kind unknown send")

            self.assertTrue(ledger.mark_received(
                project, UUID, 1_020.0, to_kind="claude", reader_alias="api"))
            resolved = ledger.read_entry(project, UUID)
            self.assertEqual(resolved["state"], "sent")
            self.assertEqual(resolved["version"], 1,
                             "a receipt makes the record readable by v1 again")
            self.assertEqual(resolved["received_at"], 1_020.0)
            self.assertEqual(ledger.pending_notices(project, "codex", "ui"), [])

            legacy = dict(resolved, id=OTHER, version=1, state="sent")
            with open(os.path.join(ledger.ledger_dir(project), OTHER + ".json"), "w") as f:
                json.dump(legacy, f)
            self.assertEqual(ledger.read_entry(project, OTHER), legacy,
                             "the new reader keeps v1 delivery history")
            impossible_v1 = dict(legacy, state="unknown")
            with open(os.path.join(ledger.ledger_dir(project), OTHER + ".json"), "w") as f:
                json.dump(impossible_v1, f)
            self.assertIsNone(ledger.read_entry(project, OTHER))

            self._sent(project, THIRD)
            self.assertEqual(ledger.read_entry(project, THIRD)["version"], 1,
                             "ordinary sent entries remain visible to v1 readers")

    def test_a_receipt_that_arrives_before_the_delivery_row_is_reconciled(self):
        """The receiver may commit its transcript while the sender is still
        returning from transport.  That proof must outlive the page cursor."""
        for state in ("sent", "unknown"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as project:
                self.assertTrue(ledger.record_receipts(
                    project, [("received", UUID, 900.0, "api")],
                    read_by="claude"))
                self.assertEqual(ledger.entries(project), [])
                fields = dict(
                    sender="ui", to_kind="claude", to_alias="api",
                    transport="channel", sha256=SHA, size=13,
                    attachment=None, sender_kind="codex", at=1_000.0)
                if state == "sent":
                    written = ledger.record_sent(project, UUID, proof="channel", **fields)
                else:
                    written = ledger.record_unknown(
                        project, UUID, reason="reply lost", preview="run the suite",
                        **fields)
                self.assertTrue(written)
                entry = ledger.read_entry(project, UUID)
                self.assertEqual((entry["state"], entry["received_at"],
                                  entry["reason"], entry["preview"]),
                                 ("sent", 900.0, None, None))
                self.assertEqual(ledger.pending_receipt_health(project)["pending"], 0)

    def test_an_attachment_read_before_the_delivery_row_is_reconciled(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_receipts(
                project, [("read", "message.txt", 800.0, "api")],
                read_by="claude"))
            self.assertTrue(ledger.record_unknown(
                project, UUID, sender="ui", to_kind="claude", to_alias="api",
                transport="channel", sha256=SHA, size=13,
                reason="reply lost", preview="run the suite",
                attachment="message.txt", sender_kind="codex", at=700.0))
            entry = ledger.read_entry(project, UUID)
            self.assertEqual((entry["state"], entry["read_at"],
                              entry["expired_unread_at"]),
                             ("sent", 800.0, None))

    def test_a_read_proof_marks_every_prior_attempt_but_no_later_reuse(self):
        """One attachment basename may be reused.  A transcript read proves
        every matching attempt already in flight, never a future envelope."""
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_receipts(
                project, [("read", "message.txt", 150.0, "api")],
                read_by="claude"))

            for delivery_id, sent_at in ((UUID, 100.0), (OTHER, 120.0),
                                         (THIRD, 160.0)):
                self.assertTrue(ledger.record_sent(
                    project, delivery_id, sender="ui", to_kind="claude",
                    to_alias="api", transport="channel", proof="channel",
                    sha256=SHA, size=13, attachment="message.txt",
                    sender_kind="codex", at=sent_at))

            self.assertEqual(
                [ledger.read_entry(project, delivery_id)["read_at"]
                 for delivery_id in (UUID, OTHER, THIRD)],
                [150.0, 150.0, None])
            self.assertEqual(ledger.pending_receipt_health(project)["pending"], 1,
                             "read proofs stay bounded for a late old writer")

    def test_one_page_preserves_each_read_horizon_for_a_reused_attachment(self):
        """Two opens in one delayed page can bracket two sends of one file.
        The earlier attempt gets the first qualifying proof, the reuse gets
        the later one, and the retained sidecar keeps the strongest horizon.
        """
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="message.txt", at=100.0)
            self._sent(project, OTHER, attachment="message.txt", at=200.0)

            self.assertTrue(ledger.record_receipts(
                project, [("read", "message.txt", 150.0, "build"),
                          ("read", "message.txt", 250.0, "build")],
                read_by="codex"))

            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 150.0)
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 250.0)
            retained = [record for _path, record in ledger._pending_receipts(project)
                        if record["kind"] == "read"]
            self.assertEqual([record["at"] for record in retained], [250.0])

    def test_a_partial_horizon_write_holds_the_cursor_for_exact_replay(self):
        """The latest retained sidecar is enough for receipt truth but not for
        the earlier per-row timestamp. A failed row write must replay the page.
        """
        receipts = [("read", "message.txt", 150.0, "build"),
                    ("read", "message.txt", 250.0, "build")]
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="message.txt", at=100.0)
            self._sent(project, OTHER, attachment="message.txt", at=200.0)
            real_write = ledger._write
            failed = False

            def fail_first_horizon(cwd, entry):
                nonlocal failed
                if (not failed and entry["id"] == UUID
                        and entry["read_at"] == 150.0):
                    failed = True
                    return False
                return real_write(cwd, entry)

            with patch.object(ledger, "_write", side_effect=fail_first_horizon):
                self.assertFalse(ledger.record_receipts(
                    project, receipts, read_by="codex"))
            self.assertEqual(
                [ledger.read_entry(project, task_id)["read_at"]
                 for task_id in (UUID, OTHER)], [None, 250.0])
            self.assertEqual(
                [record["at"] for _path, record in ledger._pending_receipts(project)],
                [250.0])

            self.assertTrue(ledger.record_receipts(
                project, receipts, read_by="codex"))
            self.assertEqual(
                [ledger.read_entry(project, task_id)["read_at"]
                 for task_id in (UUID, OTHER)], [150.0, 250.0])

    def test_a_late_old_writer_cannot_make_a_future_reuse_steal_a_read(self):
        """The exact old-writer interleaving: A appears without consulting
        the sidecar, then a later B triggers reconciliation. A gets the proof;
        B started after the read and must remain unread."""
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_receipts(
                project, [("read", "message.txt", 150.0, "api")],
                read_by="claude"))

            old = ledger._new(
                UUID, "ui", "claude", "api", "channel", "channel", "sent",
                120.0, "codex")
            old["sha256"], old["size"] = SHA, 13
            old["attachment"] = "message.txt"
            self.assertTrue(ledger._write(project, old),
                            "simulate a lock-unaware older sender")

            self.assertTrue(ledger.record_sent(
                project, OTHER, sender="ui", to_kind="claude", to_alias="api",
                transport="channel", proof="channel", sha256=SHA, size=13,
                attachment="message.txt", sender_kind="codex", at=160.0))

            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 150.0)
            self.assertIsNone(ledger.read_entry(project, OTHER)["read_at"])
            self.assertEqual(ledger.pending_receipt_health(project)["pending"], 1)

    def test_an_old_post_transport_timestamp_stays_ambiguously_unread(self):
        """A shipped v1 sender timestamps after transport. If its late row
        says it began after the observed read, there is no safe way to tell it
        from a real future basename reuse: retain the proof, never guess."""
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_receipts(
                project, [("read", "message.txt", 150.0, "api")],
                read_by="claude"))
            old = ledger._new(
                UUID, "ui", "claude", "api", "channel", "channel", "sent",
                160.0, "codex")
            old["sha256"], old["size"] = SHA, 13
            old["attachment"] = "message.txt"
            self.assertTrue(ledger._write(project, old))

            self.assertTrue(ledger.record_receipts(project, [], read_by="claude"))
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"])
            self.assertEqual(ledger.pending_receipt_health(project)["pending"], 1)

    def test_a_pending_receipt_is_credited_only_by_the_live_reader_rule(self):
        for read_by, reader_alias in (("codex", "api"),
                                      ("claude", "review")):
            with self.subTest(read_by=read_by, reader_alias=reader_alias), \
                 tempfile.TemporaryDirectory() as project:
                self.assertTrue(ledger.record_receipts(
                    project, [("received", UUID, 900.0, reader_alias)],
                    read_by=read_by))
                self.assertTrue(ledger.record_sent(
                    project, UUID, sender="ui", to_kind="claude", to_alias="api",
                    transport="channel", proof="channel", sha256=SHA, size=13,
                    sender_kind="codex", at=1_000.0))
                self.assertIsNone(ledger.read_entry(project, UUID)["received_at"])
                self.assertEqual(ledger.pending_receipt_health(project)["pending"], 0,
                                 "an immutable mismatch cannot become mine later")

    def test_a_pending_receipt_write_failure_is_not_safe_cursor_progress(self):
        with tempfile.TemporaryDirectory() as project, \
             patch.object(ledger, "_write_pending_receipt", return_value=False):
            self.assertFalse(ledger.record_receipts(
                project, [("received", UUID, 900.0, "api")],
                read_by="claude"))
            self.assertFalse(ledger.mark_received(
                project, UUID, 900.0, to_kind="claude", reader_alias="api"))

    def test_a_weaker_replayed_receipt_needs_no_sidecar_rewrite(self):
        """An existing sidecar is already the durability proof. A later
        filesystem failure must not pin the page cursor on a no-op replay.
        """
        cases = (("received", UUID, 100.0, 150.0),
                 ("read", "message.txt", 200.0, 150.0))
        for kind, key, durable_at, replay_at in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as project:
                self.assertTrue(ledger.record_receipts(
                    project, [(kind, key, durable_at, "api")],
                    read_by="claude"))
                with patch.object(ledger, "_atomic_pending_json",
                                  return_value=False) as rewrite:
                    self.assertTrue(ledger.record_receipts(
                        project, [(kind, key, replay_at, "api")],
                        read_by="claude"))
                rewrite.assert_not_called()
                retained = ledger._pending_receipts(project)
                self.assertEqual([record["at"] for _path, record in retained],
                                 [durable_at])

    def test_a_failed_read_sidecar_cannot_be_reclassified_after_releasing_the_lock(self):
        """A sender may create its row immediately after the receipt flock is
        released.  A second snapshot must not turn `unresolved` into safe
        cursor progress after the proof itself was lost."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(ledger, "_mark_read_horizons",
                          return_value="unresolved"), \
             patch.object(ledger, "entries") as after_lock:
            self.assertFalse(ledger.record_receipts(
                project, [("read", "message.txt", 900.0, "api")],
                read_by="claude"))
            after_lock.assert_not_called()

    def test_pending_receipts_are_bounded_expire_and_report_the_loss(self):
        with tempfile.TemporaryDirectory() as project:
            ids = [str(uuid.UUID(int=index + 1))
                   for index in range(ledger.PENDING_RECEIPT_LIMIT + 1)]
            with patch.object(ledger.time, "time", return_value=2_000.0):
                for index, delivery in enumerate(ids):
                    self.assertTrue(ledger.record_receipts(
                        project, [("received", delivery, 1_000.0 + index, "api")],
                        read_by="claude"))
            health = ledger.pending_receipt_health(project, now=2_000.0)
            self.assertEqual(health["pending"], ledger.PENDING_RECEIPT_LIMIT)
            self.assertEqual(health["evicted"], 1)
            self.assertGreaterEqual(ledger.PENDING_RECEIPT_TTL, ledger.LEDGER_TTL)

            ledger.prune(project, 2_001.0 + ledger.PENDING_RECEIPT_TTL)
            health = ledger.pending_receipt_health(
                project, now=2_001.0 + ledger.PENDING_RECEIPT_TTL)
            self.assertEqual(health["pending"], 0)
            self.assertGreaterEqual(health["expired"], ledger.PENDING_RECEIPT_LIMIT)

    def test_a_symlinked_pending_receipt_store_fails_closed(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as outside:
            os.makedirs(ledger.ledger_dir(project), mode=0o700)
            pending = os.path.join(ledger.ledger_dir(project), "pending-receipts")
            os.symlink(outside, pending)
            self.assertFalse(ledger.record_receipts(
                project, [("received", UUID, 900.0, "api")],
                read_by="claude"))
            self.assertEqual(os.listdir(outside), [])
            health = ledger.pending_receipt_health(project)
            self.assertTrue(health["store_invalid"],
                            "diagnosis distinguishes an absent store from a symlink")

    def test_an_unlistable_pending_receipt_store_is_not_reported_as_empty(self):
        with tempfile.TemporaryDirectory() as project:
            directory = ledger._sound_pending_dir(project, create=True)
            self.assertIsNotNone(directory)
            real_listdir = ledger.os.listdir

            def fail_pending(path):
                if path == directory:
                    raise OSError("permission denied")
                return real_listdir(path)

            with patch.object(ledger.os, "listdir", side_effect=fail_pending):
                health = ledger.pending_receipt_health(project)
            self.assertEqual(health["pending"], 0)
            self.assertTrue(health["store_invalid"])

    def test_a_malformed_exact_pending_receipt_is_visible_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertIsNotNone(ledger._sound_pending_dir(project, create=True))
            path = ledger._pending_path(
                project, "received", UUID, "claude", "api")
            with open(path, "w") as stream:
                stream.write("{")

            self.assertFalse(ledger.record_receipts(
                project, [("received", UUID, 900.0, "api")],
                read_by="claude"))
            with open(path) as stream:
                self.assertEqual(stream.read(), "{")
            health = ledger.pending_receipt_health(project, now=1_000.0)
            self.assertEqual((health["pending"], health["invalid"]), (0, 1))

    def test_receipt_first_and_row_first_threads_converge(self):
        """Both schedules serialize through the one delivery flock."""
        with tempfile.TemporaryDirectory() as project:
            barrier = threading.Barrier(2)
            results = []

            def receipt():
                barrier.wait()
                results.append(ledger.record_receipts(
                    project, [("received", UUID, 900.0, "api")],
                    read_by="claude"))

            def delivery():
                barrier.wait()
                results.append(ledger.record_unknown(
                    project, UUID, sender="ui", to_kind="claude", to_alias="api",
                    transport="channel", sha256=SHA, size=13,
                    reason="reply lost", preview="run the suite",
                    sender_kind="codex", at=1_000.0))

            threads = [threading.Thread(target=receipt), threading.Thread(target=delivery)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(results, [True, True])
            entry = ledger.read_entry(project, UUID)
            self.assertEqual((entry["state"], entry["received_at"]), ("sent", 900.0))

    def test_a_new_reader_reconciles_a_row_later_written_by_an_old_sender(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_receipts(
                project, [("received", UUID, 900.0, "api")],
                read_by="claude"))
            old = ledger._new(
                UUID, "ui", "claude", "api", "channel", "channel", "sent",
                1_000.0, "codex")
            old["sha256"], old["size"] = SHA, 13
            self.assertTrue(ledger._write(project, old),
                            "simulate the v1 writer that does not know sidecars")
            self.assertIsNone(ledger.read_entry(project, UUID)["received_at"])

            self.assertTrue(ledger.record_receipts(project, [], read_by="claude"))
            self.assertEqual(ledger.read_entry(project, UUID)["received_at"], 900.0)
            self.assertEqual(ledger.pending_receipt_health(project)["pending"], 0)

    def test_the_senders_kind_is_recorded_and_inferred_for_older_entries(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID)
            self.assertEqual(ledger.read_entry(project, UUID)["sender_kind"], "claude",
                             "a send to Codex was always Claude's before same-kind sends")
            self._sent(project, OTHER, sender_kind="codex", to_kind="codex", to_alias="review")
            self.assertEqual(ledger.sender_kind_of(ledger.read_entry(project, OTHER)), "codex")
            path = os.path.join(ledger.ledger_dir(project), THIRD + ".json")
            older = ledger.read_entry(project, UUID)
            older["id"] = THIRD
            del older["sender_kind"]
            with open(path, "w") as f:
                json.dump(older, f)
            legacy = ledger.read_entry(project, THIRD)
            self.assertIsNotNone(legacy, "an entry from before the field is still read")
            self.assertEqual(ledger.sender_kind_of(legacy), "claude")
            self.assertEqual(len(ledger.entries(project)), 3)
            bad = dict(older, id=THIRD, sender_kind="human")
            with open(path, "w") as f:
                json.dump(bad, f)
            self.assertIsNone(ledger.read_entry(project, THIRD), "a kind that is not one")

    def test_a_same_kind_refusal_is_reported_to_its_own_side(self):
        with tempfile.TemporaryDirectory() as project:
            ledger.record_refused(project, UUID, sender="ui", to_kind="claude", to_alias=None,
                                  reason="not delivered: a bare @claude line", preview="x",
                                  sender_kind="claude", at=1_000.0)
            self.assertEqual([i for i, _ in ledger.pending_notices(project, "claude", "ui")],
                             [UUID], "a Claude session's own refusal, whatever the kind sent to")
            self.assertEqual(ledger.pending_notices(project, "codex", "ui"), [],
                             "a Codex session named ui is a different session")

    def test_advice_never_names_a_peer_of_the_other_kind(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, sender="api", sender_kind="claude", to_kind="claude",
                       to_alias="ui", at=100.0)
            self._sent(project, OTHER, sender="build", to_kind="claude", to_alias="ui", at=50.0)
            self.assertEqual(ledger.last_unanswered_sender(project, "claude", "ui", 200.0,
                                                           sender_kind="codex"),
                             ("build", 150.0), "advice for a Codex reply names Codex peers")
            self.assertEqual(ledger.last_unanswered_sender(project, "claude", "ui", 200.0,
                                                           sender_kind="claude"),
                             ("api", 100.0))
            self.assertEqual(ledger.last_unanswered_sender(project, "claude", "ui", 200.0),
                             ("api", 100.0), "unfiltered, the newest of any kind")

    def test_an_update_that_would_break_the_entry_is_refused_not_written(self):
        """Round-2 review 2026-09-03: `_update` wrote whatever a mutate left
        behind. No caller leaves an invalid entry today; if one did, the file
        would be one the next reader skips — the delivery gone from the
        ledger without a word."""
        with tempfile.TemporaryDirectory() as project:
            self._sent(project)
            self.assertFalse(ledger._update(project, UUID,
                                            lambda entry: entry.update(read_at="soon")))
            entry = ledger.read_entry(project, UUID)
            self.assertIsNotNone(entry, "the file is still an entry")
            self.assertIsNone(entry["read_at"])

    def test_a_malformed_entry_is_skipped_never_raised(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project)
            directory = ledger.ledger_dir(project)
            with open(os.path.join(directory, OTHER + ".json"), "w") as f:
                f.write("{")
            with open(os.path.join(directory, THIRD + ".json"), "w") as f:
                f.write('{"version": 1, "id": "%s", "size": %s}' % (THIRD, "9" * 65_000))
            with open(os.path.join(directory, "not-a-uuid.json"), "w") as f:
                f.write("{}")
            with open(os.path.join(directory, "note.txt"), "w") as f:
                f.write("x")
            # Nested brackets well under RECORD_CEILING: RecursionError out
            # of the parser on Python 3.9 (review 2026-09-03: it reached the
            # hook's exit code, `status` and `doctor`), a list that is not an
            # entry on 3.14. Either way not an entry, never a raise.
            deep = "4a9e1c3b-5d2f-4e6a-8b7c-9d0e1f2a3b4c"
            with open(os.path.join(directory, deep + ".json"), "w") as f:
                f.write("[" * 30_000 + "]" * 30_000)
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID])
            self.assertIsNone(ledger.read_entry(project, OTHER))
            self.assertIsNone(ledger.read_entry(project, THIRD))
            self.assertIsNone(ledger.read_entry(project, deep))
            # 3.14's parser takes that nesting as a list, so the guard is
            # exercised there only when the parser is made to raise what
            # 3.9's did: pinned on every interpreter (round-2 review).
            with patch.object(ledger.json, "loads",
                              side_effect=RecursionError("maximum recursion depth exceeded")):
                self.assertIsNone(ledger.read_entry(project, UUID))
                self.assertEqual(ledger.entries(project), [])
            # Well-formed JSON that is not an entry: every one is refused by
            # the validator, not by the parser, so a validator that stopped
            # looking would let each of them onto the ledger.
            with open(os.path.join(directory, UUID + ".json")) as stream:
                good = json.load(stream)
            for label, mutate in (
                    ("wrong kind", lambda e: e.update(to_kind="gemini")),
                    ("future version", lambda e: e.update(version=999)),
                    ("time as text", lambda e: e.update(sent_at="yesterday")),
                    ("unknown key", lambda e: e.update(route="/tmp/x.sock")),
                    ("missing key", lambda e: e.pop("proof")),
                    ("bool size", lambda e: e.update(size=True)),
                    ("id mismatch", lambda e: e.update(id=UUID)),
                    ("path as attachment", lambda e: e.update(attachment="../x.txt")),
                    ("state unknown", lambda e: e.update(state="delivered")),
                    ("surrogate sender", lambda e: e.update(sender="ui\ud800")),
                    ("surrogate recipient", lambda e: e.update(to_alias="api\ud800")),
                    ("surrogate reason", lambda e: e.update(reason="bad\ud800reason")),
                    ("surrogate preview", lambda e: e.update(preview="bad\ud800preview"))):
                bad = dict(good)
                bad["id"] = OTHER
                mutate(bad)
                with open(os.path.join(directory, OTHER + ".json"), "w") as f:
                    json.dump(bad, f)
                self.assertIsNone(ledger.read_entry(project, OTHER), label)
                self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID], label)
                self.assertEqual(
                    ledger.pending_notices(project, "claude", "ui"), [], label)
        self.assertEqual(ledger.entries("/nonexistent/project"), [])

    def test_a_non_string_key_in_a_stop_outcome_is_ignored_everywhere(self):
        with tempfile.TemporaryDirectory() as project:
            scope = dict(side="codex", key="last_pushed_claude", slot="",
                         fingerprint=SHA)
            self.assertTrue(ledger.record_stop_outcome(
                project, **scope, delivery_id=UUID, at=1_000.0))
            path = ledger._stop_outcome_path(project, **scope)
            with open(path, "w") as stream:
                json.dump({
                    "version": ledger.STOP_OUTCOME_VERSION,
                    "side": "codex", "key": None, "slot": "",
                    "fingerprint": SHA, "delivery_id": UUID,
                    "recorded_at": 1_000.0,
                }, stream)

            self.assertEqual(ledger.stop_outcome_health(project, now=1_001.0),
                             {"pending": 0, "oldest": 0.0, "invalid": 1,
                              "store_invalid": False})
            self.assertIsNone(ledger.find_stop_outcome(project, **scope))
            self.assertFalse(ledger.record_stop_outcome(
                project, **scope, delivery_id=OTHER, at=1_001.0))

    def test_a_symlinked_stop_outcome_store_is_visible_to_health(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as outside:
            os.makedirs(ledger.ledger_dir(project), mode=0o700)
            directory = os.path.join(
                ledger.ledger_dir(project), ledger.STOP_OUTCOME_DIRECTORY)
            os.symlink(outside, directory)
            health = ledger.stop_outcome_health(project)
            self.assertEqual((health["pending"], health["invalid"]), (0, 0))
            self.assertTrue(health["store_invalid"])

    def test_a_valid_stop_outcome_bound_to_another_scope_is_not_success(self):
        """A schema-valid object at the hashed target path is not proof that
        the target scope was retained.  The next Stop lookup checks that
        binding, so the writer must not promise success for evidence the
        reader will reject and retransmit.
        """
        with tempfile.TemporaryDirectory() as project:
            other_scope = dict(side="codex", key="last_pushed_claude",
                               slot="@api", fingerprint=SHA)
            target_scope = dict(side="codex", key="last_pushed_claude",
                                slot="@build", fingerprint=SHA)
            self.assertTrue(ledger.record_stop_outcome(
                project, **other_scope, delivery_id=UUID, at=1_000.0))
            with open(ledger._stop_outcome_path(project, **other_scope)) as stream:
                misplaced = json.load(stream)
            target_path = ledger._stop_outcome_path(project, **target_scope)
            with open(target_path, "w") as stream:
                json.dump(misplaced, stream)

            self.assertIsNone(ledger.find_stop_outcome(project, **target_scope))
            self.assertFalse(ledger.record_stop_outcome(
                project, **target_scope, delivery_id=OTHER, at=1_001.0),
                "misbound evidence cannot justify exact-suppression success")

    def test_a_receipt_keeps_the_earliest_time_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project)
            self.assertTrue(ledger.mark_received(project, UUID, 2_000.0, reader_alias="build"))
            self.assertTrue(ledger.mark_received(project, UUID, 1_500.0, reader_alias="build"))
            self.assertTrue(ledger.mark_received(project, UUID, 3_000.0, reader_alias="build"))
            self.assertEqual(ledger.read_entry(project, UUID)["received_at"], 1_500.0)
            self.assertFalse(ledger.mark_received(project, OTHER, 2_000.0, reader_alias="build"),
                             "a receipt for nothing on the ledger is not an entry")
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID])

    def test_a_read_receipt_marks_every_entry_naming_the_file(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")
            self._sent(project, OTHER, attachment="a.txt", at=1_100.0)
            self._sent(project, THIRD, attachment="b.txt", at=1_200.0)
            self.assertTrue(ledger.mark_read(project, "a.txt", 5_000.0, reader_alias="build"))
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5_000.0)
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 5_000.0)
            self.assertIsNone(ledger.read_entry(project, THIRD)["read_at"])
            self.assertFalse(ledger.mark_read(project, "c.txt", 5_000.0))

    def test_read_times_requires_every_attempt_and_uses_the_latest_receipt(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt", at=100.0)
            self.assertEqual(ledger.read_times(project), {})
            ledger.mark_read(project, "a.txt", 150.0, to_kind="codex",
                             reader_alias="build")
            self.assertEqual(ledger.read_times(project), {"a.txt": 150.0})

            self._sent(project, OTHER, attachment="a.txt", at=160.0)
            self.assertEqual(ledger.read_times(project), {},
                             "one unread reuse vetoes read-grace collection")
            ledger.mark_read(project, "a.txt", 200.0, to_kind="codex",
                             reader_alias="build")

            old = ledger._new(
                THIRD, "ui", "codex", "build", "queue", "live", "sent",
                180.0, "claude")
            old["sha256"], old["size"], old["attachment"] = SHA, 13, "a.txt"
            self.assertTrue(ledger._write(project, old))
            self.assertTrue(ledger.record_receipts(project, [], read_by="codex"))
            self.assertEqual(ledger.read_entry(project, THIRD)["read_at"], 200.0,
                             "the later proof remains for a late old writer")
            self.assertEqual(ledger.read_times(project), {"a.txt": 200.0},
                             "grace begins at the last attempt's read")

    def test_a_durable_pending_read_vetoes_attachment_reuse(self):
        with tempfile.TemporaryDirectory() as project:
            store = os.path.join(project, ".antiphon", "messages")
            os.makedirs(store, mode=0o700)
            with open(os.path.join(store, "a.txt"), "w") as stream:
                stream.write("[Antiphon attachment]\n\nz")
            self._sent(project, UUID, attachment="a.txt", at=100.0)
            with patch.object(ledger, "_write", return_value=False):
                self.assertEqual(
                    ledger._mark_read(project, "a.txt", 150.0,
                                      to_kind="codex", reader_alias="build"),
                    "pending")
            self.assertIsNone(
                ledger.reusable_attachment(
                    project, SHA, "codex", "build", 200.0, sender="ui"),
                "a durable proof is authoritative even before its row update")

    def test_record_receipts_applies_both_kinds(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")
            ledger.record_receipts(project, [("received", UUID, 2_000.0),
                                             ("read", "a.txt", 2_500.0),
                                             ("received", OTHER, 2_000.0),
                                             ("bogus", UUID, 1.0), ("read",)],
                                   reader_alias="build")
            entry = ledger.read_entry(project, UUID)
            self.assertEqual((entry["received_at"], entry["read_at"]), (2_000.0, 2_500.0))

    def test_pending_notices_name_the_refusal_once_for_the_sender(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_refused(
                project, UUID, sender="ui", to_kind="codex", to_alias=None,
                reason="not delivered: no Codex session found in this directory",
                preview="run the suite", at=time.mktime((2026, 9, 2, 21, 44, 0, 0, 0, -1))))
            notices = ledger.pending_notices(project, "claude", "ui")
            self.assertEqual([i for i, _ in notices], [UUID])
            text = notices[0][1]
            self.assertIn("Antiphon: your @codex line at 21:44", text)
            self.assertIn('("run the suite")', text)
            self.assertIn("was not delivered", text)
            self.assertIn("no Codex session found", text)
            self.assertEqual(ledger.pending_notices(project, "claude", "api"), [],
                             "another sender's refusal is not this session's")
            self.assertEqual(ledger.pending_notices(project, "codex", "ui"), [],
                             "the Codex side never reports a Claude refusal")
            ledger.mark_reported(project, [UUID], 9_000.0)
            self.assertEqual(ledger.pending_notices(project, "claude", "ui"), [])
            self.assertEqual(ledger.read_entry(project, UUID)["reported_at"], 9_000.0)

    def test_an_unnamed_sender_hears_only_unnamed_refusals(self):
        with tempfile.TemporaryDirectory() as project:
            ledger.record_refused(project, UUID, sender="<unnamed>", to_kind="codex",
                                  to_alias=None, reason="r", preview="p", at=1.0)
            ledger.record_refused(project, OTHER, sender="ui", to_kind="codex",
                                  to_alias=None, reason="r", preview="p", at=2.0)
            self.assertEqual([i for i, _ in ledger.pending_notices(project, "claude", None)],
                             [UUID])
            self.assertEqual([i for i, _ in ledger.pending_notices(project, "claude", "<unnamed>")],
                             [UUID])

    def test_an_expired_unread_attachment_is_a_notice_to_its_sender(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt",
                       at=time.mktime((2026, 9, 2, 21, 44, 0, 0, 0, -1)))
            ledger.mark_expired_unread(project, "a.txt", 5_000.0)
            notices = ledger.pending_notices(project, "claude", "ui")
            self.assertEqual([i for i, _ in notices], [UUID])
            self.assertIn("the attachment you sent to Codex at 21:44 expired unread", notices[0][1])
            self.assertIn("7 days", notices[0][1])
            ledger.mark_read(project, "a.txt", 4_000.0, reader_alias="build")
            self._sent(project, OTHER, attachment="b.txt", at=2.0)
            ledger.mark_read(project, "b.txt", 3.0, reader_alias="build")
            ledger.mark_expired_unread(project, "b.txt", 4.0)
            self.assertIsNone(ledger.read_entry(project, OTHER)["expired_unread_at"],
                              "a read attachment never expires unread")

    def test_awaiting_receipt_excludes_received_and_refused(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, at=100.0)
            self._sent(project, OTHER, at=200.0)
            ledger.mark_received(project, OTHER, 250.0, reader_alias="build")
            ledger.record_refused(project, THIRD, sender="ui", to_kind="codex",
                                  to_alias=None, reason="r", preview="p", at=300.0)
            waiting = ledger.awaiting_receipt(project, 1_000.0)
            self.assertEqual([(e["id"], age) for e, age in waiting], [(UUID, 900.0)])

    def test_last_unanswered_sender_is_the_newest_delivery_to_me_without_a_later_reply(self):
        with tempfile.TemporaryDirectory() as project:
            # build wrote to me at 100 and 400; I answered build at 200; review wrote at 300.
            self._sent(project, UUID, sender="build", to_kind="claude", to_alias="ui", at=100.0)
            self._sent(project, OTHER, sender="ui", to_kind="codex", to_alias="build", at=200.0)
            self._sent(project, THIRD, sender="review", to_kind="claude", to_alias="ui", at=300.0)
            self.assertIsNone(ledger.last_unanswered_sender(project, "claude", "api", 460.0),
                              "a delivery addressed to ui is not an unanswered one for api")
            self._sent(project, "4a8d36b3-3871-466c-bbf6-78f8fec01c6a", sender="build",
                       to_kind="claude", to_alias=None, at=400.0)
            self.assertEqual(ledger.last_unanswered_sender(project, "claude", "ui", 460.0),
                             ("build", 60.0))
            self.assertEqual(ledger.last_unanswered_sender(project, "claude", "api", 460.0),
                             ("build", 60.0), "a bare delivery to Claude concerns every Claude session")
            self._sent(project, "5b9e47c4-4982-477d-8cc7-89f90fdd12d7", sender="ui",
                       to_kind="codex", to_alias="build", at=450.0)
            self.assertEqual(ledger.last_unanswered_sender(project, "claude", "ui", 460.0),
                             ("review", 160.0))
            self._sent(project, "6cae58d5-5a93-488e-9dd8-9a0a0eee23e8", sender="ui",
                       to_kind="codex", to_alias="review", at=455.0)
            self.assertIsNone(ledger.last_unanswered_sender(project, "claude", "ui", 460.0))

    def test_reusable_attachment_needs_the_same_words_the_same_recipient_and_the_file(self):
        with tempfile.TemporaryDirectory() as project:
            store = os.path.join(project, ".antiphon", "messages")
            os.makedirs(store, mode=0o700)
            with open(os.path.join(store, "a.txt"), "w") as f:
                f.write("[Antiphon attachment from=ui id=x sha256=y bytes=1]\n\nz")
            self._sent(project, UUID, attachment="a.txt", at=100.0)
            self.assertEqual(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0), "a.txt")
            self.assertIsNone(ledger.reusable_attachment(project, "0" * 64, "codex", "build", 200.0))
            self.assertIsNone(ledger.reusable_attachment(project, SHA, "codex", "review", 200.0))
            self.assertIsNone(ledger.reusable_attachment(project, SHA, "claude", "build", 200.0))
            self.assertEqual(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0,
                                                        sender="ui"), "a.txt")
            self.assertIsNone(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0,
                                                         sender="other"),
                              "another session's words never travel under this one's name")
            self.assertIsNone(ledger.reusable_attachment(
                project, SHA, "codex", "build", 100.0 + ledger.LEDGER_TTL + 1), "expired")
            os.unlink(os.path.join(store, "a.txt"))
            self.assertIsNone(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0),
                              "the file is gone; nothing to reuse")

    def test_prune_keeps_an_unheard_notice_for_a_second_ttl(self):
        with tempfile.TemporaryDirectory() as project:
            week = ledger.LEDGER_TTL
            ledger.record_refused(project, UUID, sender="ui", to_kind="codex", to_alias=None,
                                  reason="not delivered: nobody", preview="x", at=1_000.0)
            self._sent(project, OTHER, attachment="a.txt", at=1_000.0)
            ledger.mark_expired_unread(project, "a.txt", 1_000.0 + week + 1)
            self._sent(project, THIRD, at=1_000.0)
            ledger.prune(project, 1_000.0 + week + 10)
            self.assertIsNotNone(ledger.read_entry(project, UUID), "a refusal not yet reported")
            self.assertIsNotNone(ledger.read_entry(project, OTHER), "an expiry not yet reported")
            self.assertIsNone(ledger.read_entry(project, THIRD), "nothing to tell: pruned on time")
            ledger.mark_reported(project, [UUID], 1_000.0 + week + 20)
            ledger.prune(project, 1_000.0 + week + 30)
            self.assertIsNone(ledger.read_entry(project, UUID), "reported: pruned")
            ledger.prune(project, 1_000.0 + 2 * week + 1)
            self.assertIsNone(ledger.read_entry(project, OTHER), "a second TTL is the cap")

    # ---- review 2026-09-03: what a receipt may and may not do ----

    def test_a_time_beyond_the_platform_is_not_a_time(self):
        """A validated entry could make the hook raise: `sent_at: 1e300`
        passed validation and `_clock` overflowed `time_t` out of
        `pending_notices`, inside the cursor lock, every turn."""
        with tempfile.TemporaryDirectory() as project:
            self.assertFalse(ledger.record_sent(
                project, UUID, sender="ui", to_kind="codex", to_alias=None,
                transport="queue", proof="live", sha256=SHA, size=1, at=1e300))
            self._sent(project, UUID)
            path = os.path.join(ledger.ledger_dir(project), UUID + ".json")
            with open(path) as stream:
                entry = json.load(stream)
            entry["sent_at"] = 1e300
            with open(path, "w") as stream:
                json.dump(entry, stream)
            self.assertIsNone(ledger.read_entry(project, UUID))
            self.assertEqual(ledger.pending_notices(project, "claude", "ui"), [])

    def test_a_read_receipt_is_scoped_to_the_recipients_kind(self):
        """The sender verifying its own parked file (`tail -n +3 | shasum`,
        the ritual the envelope teaches) is not the recipient reading it."""
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")                       # to codex
            self._sent(project, OTHER, to_kind="claude", to_alias="ui", attachment="a.txt")
            self.assertTrue(ledger.mark_read(project, "a.txt", 5_000.0, to_kind="codex",
                                             reader_alias="build"))
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5_000.0)
            self.assertIsNone(ledger.read_entry(project, OTHER)["read_at"],
                              "a Claude reader's transcript proves nothing about a Codex file")
            ledger.record_receipts(project, [("read", "a.txt", 7_000.0)], read_by="claude",
                                   reader_alias="ui")
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 7_000.0)
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5_000.0)

    def test_a_read_attachment_is_never_reported_expired_unread(self):
        """The sweep marks day seven; the reader, a day behind, then sees the
        day-six read. The sender must not be told the file expired unread."""
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt", at=1.0)
            self.assertEqual(ledger.mark_expired_unread(project, "a.txt", 700_000.0), 1)
            self.assertEqual([i for i, _ in ledger.pending_notices(project, "claude", "ui")],
                             [UUID])
            ledger.mark_read(project, "a.txt", 600_000.0, to_kind="codex", reader_alias="build")
            entry = ledger.read_entry(project, UUID)
            self.assertIsNone(entry["expired_unread_at"], "a read file did not expire unread")
            self.assertEqual(entry["read_at"], 600_000.0)
            self.assertEqual(ledger.pending_notices(project, "claude", "ui"), [])
            # An entry written before reads cleared expiries carries both;
            # the read still wins.
            entry["expired_unread_at"] = 700_000.0
            path = os.path.join(ledger.ledger_dir(project), UUID + ".json")
            with open(path, "w") as stream:
                json.dump(entry, stream)
            self.assertEqual(ledger.pending_notices(project, "claude", "ui"), [])

    def test_a_read_file_is_never_reused(self):
        """Reusing a file the peer already read makes an envelope the next
        sweep deletes: the read grace keys off the receipt, not the mtime."""
        with tempfile.TemporaryDirectory() as project:
            store = os.path.join(project, ".antiphon", "messages")
            os.makedirs(store, mode=0o700)
            with open(os.path.join(store, "a.txt"), "w") as stream:
                stream.write("[Antiphon attachment]\n\nz")
            self._sent(project, UUID, attachment="a.txt", at=100.0)
            self.assertEqual(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0),
                             "a.txt")
            ledger.mark_read(project, "a.txt", 150.0, to_kind="codex", reader_alias="build")
            self.assertIsNone(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0),
                              "a fresh file, whose receipt is its own")

    def test_receipts_are_applied_once_per_key_with_one_read_of_the_ledger(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")
            with patch.object(ledger, "entries", wraps=ledger.entries) as reads:
                ledger.record_receipts(project, [("read", "a.txt", 9_000.0),
                                                 ("read", "a.txt", 5_000.0),
                                                 ("read", "a.txt", 7_000.0)], read_by="codex",
                                       reader_alias="build")
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5_000.0,
                             "the earliest")
            self.assertEqual(reads.call_count, 1, "one snapshot for the whole batch")

    def test_an_update_holds_the_ledger_lock(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID)
            calls = []
            real = ledger.fcntl.flock

            def flock(fd, operation):
                calls.append(operation)
                return real(fd, operation)
            with patch.object(ledger.fcntl, "flock", side_effect=flock):
                ledger.mark_received(project, UUID, 5.0, reader_alias="build")
            self.assertEqual(calls, [ledger.fcntl.LOCK_EX, ledger.fcntl.LOCK_UN])
            self.assertTrue(os.path.exists(os.path.join(ledger.ledger_dir(project), ".lock")))
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID],
                             "the lock file is not an entry")

    def test_ledger_lock_syscall_failures_are_bounded_results(self):
        fields = dict(
            sender="ui", to_kind="codex", to_alias="build",
            transport="queue", proof="live", sha256=SHA, size=13,
            sender_kind="claude")
        with tempfile.TemporaryDirectory() as project, \
             patch.object(ledger.fcntl, "flock",
                          side_effect=OSError("lock unavailable")):
            self.assertFalse(ledger.record_sent(project, UUID, **fields))
            self.assertFalse(ledger.record_stop_outcome(
                project, side="claude", key="last_pushed_codex", slot="@build",
                fingerprint=SHA, delivery_id=UUID))

        real_flock = ledger.fcntl.flock

        def unlock_fails(fd, operation):
            if operation == ledger.fcntl.LOCK_UN:
                raise OSError("unlock unavailable")
            return real_flock(fd, operation)

        with tempfile.TemporaryDirectory() as project, \
             patch.object(ledger.fcntl, "flock", side_effect=unlock_fails):
            self.assertTrue(ledger.record_sent(project, UUID, **fields),
                            "the durable row survives a diagnostic unlock error")

        real_close = ledger.os.close

        def close_then_fail(fd):
            real_close(fd)
            raise OSError("close report unavailable")

        with tempfile.TemporaryDirectory() as project, \
             patch.object(ledger.os, "close", side_effect=close_then_fail):
            self.assertTrue(ledger.record_sent(project, UUID, **fields),
                            "a close-report error cannot erase a durable row")

    def test_the_unnamed_receivers_advice_clears_when_it_writes_back(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, sender="build", to_kind="claude", to_alias=None, at=100.0)
            self.assertEqual(ledger.last_unanswered_sender(project, "claude", None, 300.0),
                             ("build", 200.0))
            self._sent(project, OTHER, sender="<unnamed>", to_kind="codex", to_alias="build",
                       at=200.0)
            self.assertIsNone(ledger.last_unanswered_sender(project, "claude", None, 300.0),
                              "the unnamed session wrote back")

    def test_the_grammar_refuses_dot_dot_and_a_digest_that_is_not_hex(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertFalse(ledger.record_sent(
                project, UUID, sender="ui", to_kind="codex", to_alias=None,
                transport="queue", proof="live", sha256=SHA, size=1, attachment=".."))
            self.assertFalse(ledger.record_sent(
                project, UUID, sender="ui", to_kind="codex", to_alias=None,
                transport="queue", proof="live", sha256="g" * 64, size=1))

    def test_a_same_kind_delivery_is_read_only_by_its_named_receiver(self):
        """Review 2026-09-03, critical: a same-kind sender verifying its own
        parked file has the recipient's kind, so the kind guard alone let
        its read collect the file before the recipient ever saw it."""
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, sender="ui", sender_kind="claude", to_kind="claude",
                       to_alias="api", attachment="a.txt")
            self._sent(project, OTHER, sender="build", to_kind="claude", to_alias="ui",
                       attachment="a.txt")
            self.assertFalse(ledger.mark_read(project, "a.txt", 5_000.0, to_kind="claude"),
                             "a reader with no alias is nobody's named receiver")
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "a reader with no alias cannot be the named receiver")
            self.assertIsNone(ledger.read_entry(project, OTHER)["read_at"],
                              "a named cross-kind delivery is its named receiver's too")
            ledger.mark_read(project, "a.txt", 6_000.0, to_kind="claude",
                             reader_alias="ui")
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "the sender's own verification")
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 6_000.0,
                             "the cross-kind delivery's named receiver")
            ledger.mark_read(project, "a.txt", 7_000.0, to_kind="claude",
                             reader_alias="api")
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 7_000.0,
                             "the receiver")
            ledger.record_receipts(project, [("read", "a.txt", 8_000.0)], read_by="claude",
                                   reader_alias="api")

    def test_reuse_matches_the_senders_kind(self):
        with tempfile.TemporaryDirectory() as project:
            store = os.path.join(project, ".antiphon", "messages")
            os.makedirs(store, mode=0o700)
            for name in ("a.txt", "b.txt"):
                with open(os.path.join(store, name), "w") as stream:
                    stream.write("[Antiphon attachment]\n\nz")
            self._sent(project, UUID, sender="ui", sender_kind="claude", attachment="a.txt",
                       at=100.0)
            self._sent(project, OTHER, sender="ui", sender_kind="codex", attachment="b.txt",
                       at=110.0)
            self.assertEqual(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0,
                                                        sender="ui", sender_kind="claude"),
                             "a.txt")
            self.assertEqual(ledger.reusable_attachment(project, SHA, "codex", "build", 200.0,
                                                        sender="ui", sender_kind="codex"),
                             "b.txt")

    def test_prune_removes_only_what_is_older_than_the_ttl(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, at=100.0)
            self._sent(project, OTHER, at=100.0 + ledger.LEDGER_TTL + 10)
            ledger.prune(project, 100.0 + ledger.LEDGER_TTL + 20)
            self.assertEqual([e["id"] for e in ledger.entries(project)], [OTHER])
            ledger.prune("/nonexistent/project", 1.0)

    def test_the_store_is_refused_when_it_is_a_symlink(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as elsewhere:
            os.makedirs(os.path.join(project, ".antiphon"))
            os.symlink(elsewhere, os.path.join(project, ".antiphon", "deliveries"))
            self.assertFalse(ledger.record_sent(
                project, UUID, sender="ui", to_kind="codex", to_alias=None,
                transport="queue", proof="live", sha256=SHA, size=1, at=1.0))
            self.assertEqual(os.listdir(elsewhere), [], "nothing was written elsewhere")
            self.assertEqual(ledger.entries(project), [])

    def test_a_cross_kind_receipt_is_the_named_receivers_or_a_bare_deliverys(self):
        """Review 2026-09-03: Claude parks words for Codex `build`; Codex
        `review` opens the file, which sits in the shared project directory.
        That is not `build` reading it — the file would be collected an hour
        later, the resend refused reuse, `build`'s expired-unread notice
        suppressed. A transcript nobody can name proves nothing about a
        named delivery, only about a bare one."""
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")                       # to codex build
            self._sent(project, OTHER, to_alias=None, attachment="a.txt")       # to codex, bare
            self.assertTrue(ledger.mark_read(project, "a.txt", 5_000.0, to_kind="codex",
                                             reader_alias="review"))
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "the wrong peer of the right kind")
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 5_000.0,
                             "a bare delivery: any reader of its kind")
            ledger.mark_read(project, "a.txt", 6_000.0, to_kind="codex")
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "a reader nobody can name is not the named receiver")
            ledger.mark_read(project, "a.txt", 7_000.0, to_kind="codex",
                             reader_alias="build")
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 7_000.0,
                             "the receiver")
            # `received` is scoped the same way, and a receipt may carry its
            # own reader: the page road names each transcript it walked.
            self.assertFalse(ledger.mark_received(project, UUID, 8.0, to_kind="codex",
                                                  reader_alias="review"))
            self.assertFalse(ledger.mark_received(project, UUID, 8.0, to_kind="claude",
                                                  reader_alias="build"),
                             "a Claude transcript proves nothing about a Codex delivery")
            self.assertIsNone(ledger.read_entry(project, UUID)["received_at"])
            ledger.record_receipts(project, [("received", UUID, 9.0, "review"),
                                             ("received", UUID, 10.0, "build"),
                                             ("received", OTHER, 11.0, None)],
                                   read_by="codex")
            self.assertEqual(ledger.read_entry(project, UUID)["received_at"], 10.0)
            self.assertEqual(ledger.read_entry(project, OTHER)["received_at"], 11.0)

    def test_a_reader_never_repairs_the_store_a_writer_does(self):
        """`status` and `doctor` read the ledger. A directory somebody
        loosened is tightened by the next write, not by a report that says
        it only reads."""
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID)
            directory = ledger.ledger_dir(project)
            os.chmod(directory, 0o755)
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID])
            ledger.read_times(project)
            ledger.awaiting_receipt(project, 2_000.0)
            ledger.pending_notices(project, "claude", "ui")
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o755,
                             "a read leaves the mode as it found it")
            self._sent(project, OTHER)
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700, "a write repairs it")

    def test_a_refusals_reason_is_bounded_when_it_is_written(self):
        """The notices ride ahead of the page, outside its budget; a reason
        is a transport's own words, bounded only here."""
        with tempfile.TemporaryDirectory() as project:
            self.assertTrue(ledger.record_refused(
                project, UUID, sender="ui", to_kind="codex", to_alias=None,
                reason="not delivered: " + "x" * 10_000, preview="p", at=1_000.0))
            entry = ledger.read_entry(project, UUID)
            self.assertEqual(ledger.REASON_LENGTH, 400)
            self.assertEqual(len(entry["reason"]), ledger.REASON_LENGTH)
            notice = ledger.pending_notices(project, "claude", "ui")[0][1]
            self.assertLess(len(notice), ledger.REASON_LENGTH + 120)
            # An entry from before the bound is bounded where it is rendered.
            entry["reason"] = "not delivered: " + "y" * 10_000
            with open(os.path.join(ledger.ledger_dir(project), UUID + ".json"), "w") as f:
                json.dump(entry, f)
            notice = ledger.pending_notices(project, "claude", "ui")[0][1]
            self.assertLess(len(notice), ledger.REASON_LENGTH + 120)


if __name__ == "__main__":
    unittest.main()
