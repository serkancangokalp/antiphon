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

    def test_read_times_is_the_earliest_receipt_per_file(self):
        with tempfile.TemporaryDirectory() as project:
            self._sent(project, UUID, attachment="a.txt")
            self._sent(project, OTHER, attachment="a.txt", at=1_100.0)
            self._sent(project, THIRD, attachment="b.txt", at=1_200.0)
            self.assertEqual(ledger.read_times(project), {})
            ledger.mark_read(project, "a.txt", 5_000.0, reader_alias="build")
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
            self.assertTrue(ledger.mark_read(project, "a.txt", 5.0, to_kind="codex",
                                             reader_alias="build"))
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5.0)
            self.assertIsNone(ledger.read_entry(project, OTHER)["read_at"],
                              "a Claude reader's transcript proves nothing about a Codex file")
            ledger.record_receipts(project, [("read", "a.txt", 7.0)], read_by="claude",
                                   reader_alias="ui")
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 7.0)
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5.0)

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
                ledger.record_receipts(project, [("read", "a.txt", 9.0), ("read", "a.txt", 5.0),
                                                 ("read", "a.txt", 7.0)], read_by="codex",
                                       reader_alias="build")
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 5.0, "the earliest")
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
            self.assertFalse(ledger.mark_read(project, "a.txt", 5.0, to_kind="claude"),
                             "a reader with no alias is nobody's named receiver")
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "a reader with no alias cannot be the named receiver")
            self.assertIsNone(ledger.read_entry(project, OTHER)["read_at"],
                              "a named cross-kind delivery is its named receiver's too")
            ledger.mark_read(project, "a.txt", 6.0, to_kind="claude", reader_alias="ui")
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "the sender's own verification")
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 6.0,
                             "the cross-kind delivery's named receiver")
            ledger.mark_read(project, "a.txt", 7.0, to_kind="claude", reader_alias="api")
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 7.0, "the receiver")
            ledger.record_receipts(project, [("read", "a.txt", 8.0)], read_by="claude",
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
            self.assertTrue(ledger.mark_read(project, "a.txt", 5.0, to_kind="codex",
                                             reader_alias="review"))
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "the wrong peer of the right kind")
            self.assertEqual(ledger.read_entry(project, OTHER)["read_at"], 5.0,
                             "a bare delivery: any reader of its kind")
            ledger.mark_read(project, "a.txt", 6.0, to_kind="codex")
            self.assertIsNone(ledger.read_entry(project, UUID)["read_at"],
                              "a reader nobody can name is not the named receiver")
            ledger.mark_read(project, "a.txt", 7.0, to_kind="codex", reader_alias="build")
            self.assertEqual(ledger.read_entry(project, UUID)["read_at"], 7.0, "the receiver")
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
