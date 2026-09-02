"""The managed-worker task store: one record per task, validated like the
ledger, kept a week, under a directory this code owns outright."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import workers

import hashlib
import json
import os
import tempfile
import time
import unittest

SHA = hashlib.sha256(b"review the diff").hexdigest()


class TaskStoreTest(unittest.TestCase):

    def _new(self, project, **over):
        fields = dict(kind="codex", task_class="read", sha256=SHA, size=15,
                      parent=None, timeout=900, hop=1)
        fields.update(over)
        return workers.new_task(project, **fields)

    def test_a_task_round_trips_with_every_field_and_never_the_text(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            self.assertRegex(record["id"], r"^[0-9a-f-]{36}$")
            self.assertEqual((record["version"], record["kind"], record["task_class"],
                              record["state"], record["sha256"], record["size"],
                              record["parent"], record["timeout"], record["hop"],
                              record["pid"], record["started_at"], record["finished_at"],
                              record["exit_code"], record["collected_at"]),
                             (workers.TASK_VERSION, "codex", "read", "accepted", SHA, 15,
                              None, 900, 1, None, None, None, None, None))
            self.assertIsInstance(record["created_at"], float)
            again = workers.read_task(project, record["id"])
            self.assertEqual(again, record)
            path = os.path.join(workers.tasks_dir(project), record["id"] + ".json")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(workers.tasks_dir(project)).st_mode & 0o777, 0o700)
            self.assertNotIn("review the diff", open(path).read())
            self.assertEqual([t["id"] for t in workers.tasks(project)], [record["id"]])

    def test_the_timeout_and_the_class_are_bounded(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertEqual(self._new(project, timeout=99_999)["timeout"], workers.MAX_TIMEOUT)
            self.assertEqual(self._new(project, timeout=0)["timeout"], workers.DEFAULT_TIMEOUT)
            with self.assertRaises(ValueError):
                self._new(project, task_class="deploy")
            with self.assertRaises(ValueError):
                self._new(project, kind="human")

    def test_a_malformed_record_is_skipped_never_raised(self):
        with tempfile.TemporaryDirectory() as project:
            good = self._new(project)
            directory = workers.tasks_dir(project)
            bad = os.path.join(directory, "2e6b14f1-1659-544a-98d4-56d6eca8fa48.json")
            for content in ("{", "[]", json.dumps(dict(good, id="2e6b14f1-1659-544a-98d4-56d6eca8fa48",
                                                        state="done")),
                            json.dumps(dict(good, id="2e6b14f1-1659-544a-98d4-56d6eca8fa48",
                                            sha256="g" * 64)),
                            json.dumps(dict(good, id="2e6b14f1-1659-544a-98d4-56d6eca8fa48",
                                            created_at=1e300)),
                            json.dumps(dict(good, id="2e6b14f1-1659-544a-98d4-56d6eca8fa48",
                                            extra=1))):
                with open(bad, "w") as f:
                    f.write(content)
                self.assertIsNone(workers.read_task(project, "2e6b14f1-1659-544a-98d4-56d6eca8fa48"),
                                  content[:40])
                self.assertEqual([t["id"] for t in workers.tasks(project)], [good["id"]])

    def test_an_update_is_validated_and_locked(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            calls = []
            real = workers.fcntl.flock

            def flock(fd, operation):
                calls.append(operation)
                return real(fd, operation)

            def running(changed):
                changed["state"] = "running"
                changed["pid"] = 4242
                changed["started_at"] = 1_000.0
            from unittest.mock import patch
            with patch.object(workers.fcntl, "flock", side_effect=flock):
                self.assertTrue(workers.update_task(project, record["id"], running))
            self.assertEqual(calls, [workers.fcntl.LOCK_EX, workers.fcntl.LOCK_UN])
            self.assertEqual(workers.read_task(project, record["id"])["state"], "running")

            def broken(changed):
                changed["state"] = "done"
            self.assertFalse(workers.update_task(project, record["id"], broken),
                             "an update that breaks the record is refused, not written")
            self.assertEqual(workers.read_task(project, record["id"])["state"], "running")

    def test_prune_removes_only_what_is_older_than_the_ttl_and_not_running(self):
        with tempfile.TemporaryDirectory() as project:
            old = self._new(project)
            fresh = self._new(project)
            live = self._new(project)
            aged = time.time() - workers.TASK_TTL - 60
            for task in (old, live):
                workers.update_task(project, task["id"],
                                    lambda changed: changed.update(created_at=aged))
            workers.update_task(project, live["id"],
                                lambda changed: changed.update(state="running", pid=1,
                                                               started_at=aged))
            workers.prune(project, time.time())
            self.assertIsNone(workers.read_task(project, old["id"]))
            self.assertIsNotNone(workers.read_task(project, fresh["id"]))
            self.assertIsNotNone(workers.read_task(project, live["id"]),
                                 "a running task is never pruned, however old")

    def test_the_store_is_refused_when_it_is_a_symlink(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as elsewhere:
            os.makedirs(os.path.join(project, ".antiphon"))
            os.symlink(elsewhere, os.path.join(project, ".antiphon", "tasks"))
            with self.assertRaises(OSError):
                self._new(project)
            self.assertEqual(os.listdir(elsewhere), [])
            self.assertEqual(workers.tasks(project), [])


if __name__ == "__main__":
    unittest.main()
