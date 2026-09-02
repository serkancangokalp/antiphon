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
import time
import unittest

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
            raw = open(path, "rb").read().decode()
            self.assertNotIn("run the suite", raw, "never the content")
            self.assertNotIn("antiphon-channel-", raw, "never a route")
            uuids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw))
            self.assertEqual(uuids, {UUID}, "the delivery id is the only uuid-shaped value")
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID])

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
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID])
            self.assertIsNone(ledger.read_entry(project, OTHER))
            self.assertIsNone(ledger.read_entry(project, THIRD))
            # Well-formed JSON that is not an entry: every one is refused by
            # the validator, not by the parser, so a validator that stopped
            # looking would let each of them onto the ledger.
            good = json.load(open(os.path.join(directory, UUID + ".json")))
            for label, mutate in (
                    ("wrong kind", lambda e: e.update(to_kind="gemini")),
                    ("future version", lambda e: e.update(version=2)),
                    ("time as text", lambda e: e.update(sent_at="yesterday")),
                    ("unknown key", lambda e: e.update(route="/tmp/x.sock")),
                    ("missing key", lambda e: e.pop("proof")),
                    ("bool size", lambda e: e.update(size=True)),
                    ("id mismatch", lambda e: e.update(id=THIRD)),
                    ("path as attachment", lambda e: e.update(attachment="../x.txt")),
                    ("state unknown", lambda e: e.update(state="delivered"))):
                bad = dict(good)
                mutate(bad)
                with open(os.path.join(directory, OTHER + ".json"), "w") as f:
                    json.dump(bad, f)
                self.assertIsNone(ledger.read_entry(project, OTHER), label)
                self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID], label)
        self.assertEqual(ledger.entries("/nonexistent/project"), [])

    def test_a_receipt_keeps_the_earliest_time_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project)
            self.assertTrue(ledger.mark_received(project, UUID, 2_000.0))
            self.assertTrue(ledger.mark_received(project, UUID, 1_500.0))
            self.assertTrue(ledger.mark_received(project, UUID, 3_000.0))
            self.assertEqual(ledger.read_entry(project, UUID)["received_at"], 1_500.0)
            self.assertFalse(ledger.mark_received(project, OTHER, 2_000.0),
                             "a receipt for nothing on the ledger is not an entry")
            self.assertEqual([e["id"] for e in ledger.entries(project)], [UUID])

    def test_a_read_receipt_marks_every_entry_naming_the_file(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")
            self._sent(project, OTHER, attachment="a.txt", at=1_100.0)
            self._sent(project, THIRD, attachment="b.txt", at=1_200.0)
            self.assertTrue(ledger.mark_read(project, "a.txt", 5_000.0))
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5_000.0)
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 5_000.0)
            self.assertIsNone(ledger.read_entry(project, THIRD)["read_at"])
            self.assertFalse(ledger.mark_read(project, "c.txt", 5_000.0))

    def test_read_times_is_the_earliest_receipt_per_file(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")
            self._sent(project, OTHER, attachment="a.txt", at=1_100.0)
            self._sent(project, THIRD, attachment="b.txt", at=1_200.0)
            self.assertEqual(ledger.read_times(project), {})
            ledger.mark_read(project, "a.txt", 5_000.0)
            self.assertEqual(ledger.read_times(project), {"a.txt": 5_000.0})
            self.assertEqual(ledger.mark_expired_unread(project, "b.txt", 9_000.0), 1)
            self.assertEqual(ledger.mark_expired_unread(project, "b.txt", 9_500.0), 0,
                             "marked once")
            self.assertEqual(ledger.mark_expired_unread(project, "a.txt", 9_000.0), 0,
                             "a read file never expires unread")
            self.assertEqual(ledger.mark_expired_unread(project, "zz.txt", 9_000.0), 0,
                             "a file from before the ledger")

    def test_record_receipts_applies_both_kinds(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")
            ledger.record_receipts(project, [("received", UUID, 2_000.0),
                                             ("read", "a.txt", 2_500.0),
                                             ("received", OTHER, 2_000.0),
                                             ("bogus", UUID, 1.0)])
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
            ledger.mark_read(project, "a.txt", 4_000.0)
            self._sent(project, OTHER, attachment="b.txt", at=2.0)
            ledger.mark_read(project, "b.txt", 3.0)
            ledger.mark_expired_unread(project, "b.txt", 4.0)
            self.assertIsNone(ledger.read_entry(project, OTHER)["expired_unread_at"],
                              "a read attachment never expires unread")

    def test_awaiting_receipt_excludes_received_and_refused(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, at=100.0)
            self._sent(project, OTHER, at=200.0)
            ledger.mark_received(project, OTHER, 250.0)
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


if __name__ == "__main__":
    unittest.main()
