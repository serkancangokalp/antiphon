"""The managed-worker task store: one record per task, validated like the
ledger, kept a week, under a directory this code owns outright."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import workers

import contextlib
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch

SHA = hashlib.sha256(b"review the diff").hexdigest()
PROOF = "ab" * 32


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
                             (workers.LEGACY_TASK_VERSION, "codex", "read", "accepted", SHA, 15,
                              None, 900, 1, None, None, None, None, None))
            self.assertIsInstance(record["created_at"], float)
            again = workers.read_task(project, record["id"])
            self.assertEqual(again, record)
            path = os.path.join(workers.tasks_dir(project), record["id"] + ".json")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(workers.tasks_dir(project)).st_mode & 0o777, 0o700)
            with open(path) as f:
                self.assertNotIn("review the diff", f.read())
            self.assertEqual([t["id"] for t in workers.tasks(project)], [record["id"]])

    def test_a_task_id_cannot_replace_any_owned_namespace(self):
        for occupied in ("row", "legacy-row", "live", "diff", "worker-dir"):
            with self.subTest(occupied=occupied), \
                 tempfile.TemporaryDirectory() as project:
                task_id = "12345678-1234-1234-1234-123456789abc"
                original = None
                self.assertIsNotNone(workers._task_store(project, create=True))
                if occupied == "row":
                    original = self._new(project, task_id=task_id)
                elif occupied == "legacy-row":
                    legacy = workers._legacy_tasks_dir(project)
                    os.makedirs(legacy)
                    with open(os.path.join(legacy, task_id + ".json"), "wb") as stream:
                        stream.write(b"legacy residue")
                elif occupied == "live":
                    with open(workers.live_path(project, task_id), "wb") as stream:
                        stream.write(workers.LIVE_STARTING)
                elif occupied == "diff":
                    with open(workers._diff_path(project, task_id), "wb") as stream:
                        stream.write(b"retained result")
                else:
                    os.makedirs(workers.worker_dir(project, task_id))

                with self.assertRaisesRegex(ValueError, "already owned"):
                    self._new(project, task_id=task_id, sha256="cd" * 32)

                self.assertEqual(workers.read_task(project, task_id), original)

    def test_first_task_creation_fsyncs_each_new_directory_boundary(self):
        with tempfile.TemporaryDirectory() as project:
            synced = []
            real = workers._fsync_directory

            def observe(path):
                synced.append(os.path.realpath(path))
                return real(path)

            with patch.object(workers, "_fsync_directory", side_effect=observe):
                self._new(project)

            self.assertIn(os.path.realpath(project), synced)
            self.assertIn(os.path.realpath(os.path.join(project, ".antiphon")), synced)
            self.assertIn(os.path.realpath(workers.tasks_dir(project)), synced)

    def test_directory_fsync_opens_and_syncs_the_directory_descriptor(self):
        """The durability helper performs the syscall its callers rely on."""
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(workers.os, "fsync") as fsync:
            workers._fsync_directory(directory)

            fsync.assert_called_once()
            descriptor = fsync.call_args.args[0]
            self.assertIs(type(descriptor), int)

    def test_the_timeout_and_the_class_are_bounded(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertEqual(self._new(project, timeout=99_999)["timeout"], workers.MAX_TIMEOUT)
            self.assertEqual(self._new(project, timeout=0)["timeout"], workers.DEFAULT_TIMEOUT)
            for timeout in (float("inf"), float("-inf"), float("nan")):
                self.assertEqual(self._new(project, timeout=timeout)["timeout"],
                                 workers.DEFAULT_TIMEOUT)
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

    def test_a_deeply_nested_record_is_skipped_never_raised(self):
        """A small JSON document can exceed the decoder's recursion limit;
        malformed task state remains data, never control flow out of readers.
        """
        bad_id = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"
        with tempfile.TemporaryDirectory() as project:
            good = self._new(project)
            path = os.path.join(workers.tasks_dir(project), bad_id + ".json")
            with open(path, "w", encoding="ascii") as stream:
                stream.write("[" * 1_500 + "0" + "]" * 1_500)
            self.assertLess(os.path.getsize(path), workers.RECORD_CEILING)
            self.assertIsNone(workers.read_task(project, bad_id))
            self.assertEqual([task["id"] for task in workers.tasks(project)],
                             [good["id"]])

        with tempfile.TemporaryDirectory() as project:
            good = self._new(project)
            with patch.object(workers.json, "loads",
                              side_effect=RecursionError("decoder limit")):
                self.assertIsNone(workers.read_task(project, good["id"]))

    def test_an_old_reader_orphaned_live_sidecar_is_pruned_only_when_unlocked(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            path = workers.live_path(project, record["id"])
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(fd, workers.LIVE_ACTIVE)
            os.utime(path, (0, 0))
            os.unlink(os.path.join(workers.tasks_dir(project),
                                   record["id"] + ".json"))
            try:
                workers.prune(project, workers.TASK_TTL + 1)
                self.assertTrue(os.path.exists(path), "a live supervisor keeps its lock")
            finally:
                workers.fcntl.flock(fd, workers.fcntl.LOCK_UN)
                os.close(fd)

            workers.prune(project, workers.TASK_TTL + 1)
            self.assertFalse(os.path.exists(path))

    def test_a_transient_row_read_failure_cannot_erase_git_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project, task_class="write")
            os.makedirs(workers.worker_dir(project, record["id"]))
            self.assertTrue(
                workers._write_git_cleanup_witness(project, record["id"]))
            birth = workers._process_start(os.getpid())
            self.assertIsNotNone(birth)
            marker = workers._git_mutator_marker(os.getpid(), birth)
            path = workers.live_path(project, record["id"])
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(descriptor, marker)
            finally:
                os.close(descriptor)
            os.utime(path, (0, 0))

            with patch.object(workers, "read_task", return_value=None):
                workers.prune(project, record["created_at"] + workers.TASK_TTL + 1)

            self.assertEqual(workers.read_task(project, record["id"]), record)
            self.assertEqual(
                workers._git_cleanup_witness(project, record["id"]), "present")
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), marker)

    def test_an_orphan_git_mutator_marker_is_never_expired_automatically(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            birth = workers._process_start(os.getpid())
            self.assertIsNotNone(birth)
            marker = workers._git_mutator_marker(os.getpid(), birth)
            path = workers.live_path(project, record["id"])
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(descriptor, marker)
            finally:
                os.close(descriptor)
            os.unlink(os.path.join(
                workers.tasks_dir(project), record["id"] + ".json"))
            os.utime(path, (0, 0))

            workers.prune(project, workers.TASK_TTL + 1)

            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), marker)
            self.assertEqual(
                workers.start_recovery_health(project),
                {"orphaned": 1, "row_unreadable": 0})

    def test_git_mutator_markers_are_exact_bounded_process_identities(self):
        birth = "Fri Sep 4 12:34:56 2026"
        marker = workers._git_mutator_marker(4242, birth)
        self.assertEqual(workers._git_mutator_parts(marker), (4242, birth))

        malformed = (
            b"git-mutator:0:Fri Sep 4 12:34:56 2026\n",
            b"git-mutator:04242:Fri Sep 4 12:34:56 2026\n",
            b"git-mutator:4242:not-a-birth\n",
            b"git-mutator:4242:Fri Sep 4 12:34:56 2026",
            b"git-mutator:" + b"9" * (
                workers.peers.INTEGER_TOKEN_CEILING + 1)
            + b":Fri Sep 4 12:34:56 2026\n",
            b"git-mutator:4242:Fri Sep 4 12:34:56 2026\nextra\n",
        )
        for candidate in malformed:
            with self.subTest(candidate=candidate[:40]):
                self.assertIsNone(workers._git_mutator_parts(candidate))

    def test_a_missing_guardian_receipt_never_signals_a_recycled_pid(self):
        birth = "Fri Sep 4 12:34:56 2026"
        replacement = "Fri Sep 4 12:34:57 2026"
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            path = workers.live_path(project, record["id"])
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(
                    descriptor, workers._git_mutator_marker(4242, birth))
            finally:
                os.close(descriptor)
            with patch.object(workers, "_process_snapshot",
                              return_value=(replacement, "S")) as snapshot, \
                 patch.object(workers, "_kill_group") as kill:
                workers.sweep(
                    project,
                    record["created_at"] + workers.START_PATIENCE + 1)

            snapshot.assert_not_called()
            kill.assert_not_called()
            self.assertEqual(workers.read_task(project, record["id"]), record)
            with open(path, "rb") as stream:
                self.assertEqual(
                    stream.read(), workers._git_mutator_marker(4242, birth))

    def test_an_unrecognized_lifecycle_marker_keeps_the_recovery_witness(self):
        marker = b"git-mutator:partial\n"
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            path = workers.live_path(project, record["id"])
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                self.assertEqual(os.write(descriptor, marker), len(marker))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            workers.sweep(
                project,
                record["created_at"] + workers.START_PATIENCE + 1)

            self.assertEqual(workers.read_task(project, record["id"]), record)
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), marker)

    def test_a_start_retry_cannot_overwrite_an_unresolved_git_mutator(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            birth = workers._process_start(os.getpid())
            self.assertIsNotNone(birth)
            marker = workers._git_mutator_marker(os.getpid(), birth)
            descriptor = os.open(
                workers.live_path(project, record["id"]),
                os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(descriptor, marker)
            finally:
                os.close(descriptor)

            claimed = None
            try:
                with self.assertRaisesRegex(
                        workers.Refused, "Git worktree creation.*unresolved"):
                    claimed = workers._claim_start(project, record)
            finally:
                if claimed is not None:
                    os.close(claimed)

            with open(workers.live_path(project, record["id"]), "rb") as stream:
                self.assertEqual(stream.read(), marker)
            self.assertEqual(workers.read_task(project, record["id"]), record)

    def test_a_missing_git_receipt_is_visible_and_cannot_be_cancelled(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project, task_class="write")
            os.makedirs(workers.worker_dir(project, record["id"]))
            self.assertTrue(
                workers._write_git_cleanup_witness(project, record["id"]))
            birth = workers._process_start(os.getpid())
            self.assertIsNotNone(birth)
            marker = workers._git_mutator_marker(os.getpid(), birth)
            descriptor = os.open(
                workers.live_path(project, record["id"]),
                os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers.fcntl.flock(descriptor, workers.fcntl.LOCK_EX)
                workers._write_live_marker(descriptor, marker)
                self.assertIsNone(
                    workers.accepted_start_recovery(project, record),
                    "an owned marker is still an in-flight start")
            finally:
                workers.fcntl.flock(descriptor, workers.fcntl.LOCK_UN)
                os.close(descriptor)

            self.assertEqual(
                workers.accepted_start_recovery(project, record),
                "git_completion_receipt_missing")
            status = workers.reported_status(project, record["id"])
            self.assertEqual(
                status["start_recovery"],
                "git_completion_receipt_missing")
            self.assertIn("do not retry automatically", status["recovery_detail"])
            result = workers.result(project, record["id"])
            self.assertEqual(
                result["start_recovery"],
                "git_completion_receipt_missing")
            self.assertIn("operator intervention", result["recovery_detail"])
            with self.assertRaisesRegex(
                    workers.Refused,
                    "no durable completion receipt.*do not retry automatically"):
                workers.cancel(project, record["id"])
            self.assertEqual(workers.read_task(project, record["id"]), record)
            self.assertEqual(
                workers._git_cleanup_witness(project, record["id"]), "present")

    def test_one_unreadable_git_marker_observation_cannot_authorize_sweep(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            birth = workers._process_start(os.getpid())
            self.assertIsNotNone(birth)
            marker = workers._git_mutator_marker(os.getpid(), birth)
            descriptor = os.open(
                workers.live_path(project, record["id"]),
                os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(descriptor, marker)
            finally:
                os.close(descriptor)
            real_read = workers._read_live_bytes
            calls = 0

            def transient_read(fd):
                nonlocal calls
                calls += 1
                return None if calls == 1 else real_read(fd)

            with patch.object(workers, "_read_live_bytes",
                              side_effect=transient_read):
                workers.sweep(
                    project,
                    record["created_at"] + workers.START_PATIENCE + 1)

            self.assertGreaterEqual(calls, 1)
            self.assertEqual(workers.read_task(project, record["id"]), record)
            with open(workers.live_path(project, record["id"]), "rb") as stream:
                self.assertEqual(stream.read(), marker)

    @unittest.skipUnless(hasattr(os, "fork"), "requires process groups")
    def test_a_dead_git_leader_with_a_live_group_keeps_its_witness(self):
        with tempfile.TemporaryDirectory() as directory:
            release = os.path.join(directory, "release")
            program = """
import os, sys, time
child = os.fork()
if child == 0:
    null = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(null, descriptor)
    os.close(null)
    while not os.path.exists(sys.argv[1]):
        time.sleep(0.01)
    raise SystemExit(0)
print(child, flush=True)
while not os.path.exists(sys.argv[2]):
    time.sleep(0.01)
"""
            let_leader_exit = os.path.join(directory, "leader-exit")
            leader = subprocess.Popen(
                [sys.executable, "-c", program, release, let_leader_exit],
                stdout=subprocess.PIPE, text=True, start_new_session=True)
            child_pid = None
            child_birth = None
            descriptor = None
            try:
                child_pid = int(leader.stdout.readline().strip())
                child_birth = workers._process_start(child_pid)
                deadline = time.time() + 5
                birth = None
                while birth is None and time.time() < deadline:
                    birth = workers._process_start(leader.pid)
                    if birth is None:
                        time.sleep(0.01)
                self.assertIsNotNone(birth)
                record = self._new(directory)
                path = workers.live_path(directory, record["id"])
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                marker = workers._git_mutator_marker(leader.pid, birth)
                workers._write_live_marker(descriptor, marker)
                os.close(descriptor)
                descriptor = None

                with open(let_leader_exit, "w", encoding="ascii"):
                    pass
                leader.wait(timeout=5)
                self.assertEqual(
                    workers._group_process_liveness(leader.pid), "live")

                workers.sweep(
                    directory,
                    record["created_at"] + workers.START_PATIENCE + 1)
                self.assertEqual(
                    workers.read_task(directory, record["id"]), record)
                with open(path, "rb") as stream:
                    self.assertEqual(stream.read(), marker)
            finally:
                with open(release, "a", encoding="ascii"):
                    pass
                if leader.poll() is None:
                    leader.kill()
                    leader.wait(timeout=5)
                if (child_pid is not None and child_birth is not None
                        and workers._process_start(child_pid) == child_birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)
                if leader.stdout is not None:
                    leader.stdout.close()
                if descriptor is not None:
                    os.close(descriptor)

    def test_non_utf8_task_metadata_is_invalid_and_writer_cleanup_is_total(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            path = os.path.join(workers.tasks_dir(project), record["id"] + ".json")
            poisoned = dict(record, birth="bad\ud800birth")
            with open(path, "w", encoding="ascii") as stream:
                json.dump(poisoned, stream)
            self.assertIsNone(workers.read_task(project, record["id"]))

        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            directory = workers.tasks_dir(project)
            before = set(os.listdir(directory))
            with patch.object(workers.json, "dump",
                              side_effect=RuntimeError("serializer failed")):
                with self.assertRaisesRegex(RuntimeError, "serializer failed"):
                    workers._write(project, record)
            self.assertEqual(set(os.listdir(directory)), before,
                             "a failed serializer leaves no temporary file")

    def test_non_utf8_peer_metadata_in_a_legacy_running_task_is_inert(self):
        """Every durable string is a UTF-8 boundary, not only v2 handoffs.
        Otherwise status accepts this v1 row and crashes while finishing it."""
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            poisoned = dict(record, state="running", pid=999_999,
                            started_at=1_000.0, to="bad\ud800peer")
            path = os.path.join(workers.tasks_dir(project), record["id"] + ".json")
            with open(path, "w", encoding="ascii") as stream:
                json.dump(poisoned, stream)

            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertIsNone(workers.status(project, record["id"], now=2_000.0))

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

    def test_an_update_lock_failure_is_a_false_result_not_an_exception(self):
        """Callers may already have put the task id in a peer's hands.  A
        kernel lock refusal must leave the prepared row intact and let those
        callers return their structured incomplete state.
        """
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            with patch.object(workers.fcntl, "flock",
                              side_effect=OSError("lock unavailable")):
                self.assertFalse(workers.update_task(
                    project, record["id"],
                    lambda changed: changed.update(state="running")))
            self.assertEqual(workers.read_task(project, record["id"]), record)

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

    def test_prune_keeps_an_old_accepted_row_while_its_start_lock_is_held(self):
        with tempfile.TemporaryDirectory() as project:
            record = self._new(project)
            workers.update_task(
                project, record["id"],
                lambda changed: changed.update(created_at=1.0))
            lock_fd = os.open(workers.live_path(project, record["id"]),
                              os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lock_fd, workers.LIVE_STARTING)
            try:
                workers.prune(project, workers.TASK_TTL + 2.0)
                self.assertIsNotNone(workers.read_task(project, record["id"]))
                self.assertTrue(os.path.exists(workers.live_path(project, record["id"])))
            finally:
                workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_UN)
                os.close(lock_fd)

    def test_the_store_is_refused_when_it_is_a_symlink(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as elsewhere:
            os.makedirs(os.path.join(project, ".antiphon"))
            os.symlink(elsewhere, os.path.join(
                project, ".antiphon", workers.TASK_STORE))
            with self.assertRaises(OSError):
                self._new(project)
            self.assertEqual(os.listdir(elsewhere), [])
            self.assertEqual(workers.tasks(project), [])



class AdapterTest(unittest.TestCase):
    """One subprocess per kind, with the host's own default permission class
    and never a flag that widens it; a write task in its own worktree."""

    def _stub(self, root, kind, body="exit 0"):
        """A `claude` or `codex` on PATH that records its argv and does as told."""
        path = os.path.join(root, kind)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$0.argv\"\n"
                    "printf 'HOP=%s CWD=%s NAME=%s DIR=%s TESTS=%s PWD=%s\\n' \"$ANTIPHON_HOP\" "
                    "\"$ANTIPHON_CWD\" \"$ANTIPHON_NAME\" \"$ANTIPHON_WORKER_DIR\" "
                    "\"$ANTIPHON_WORKER_TESTS\" \"$PWD\" >> \"$0.env\"\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def _env(self, bin_dir):
        return dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))

    def test_the_argv_per_kind_and_class_never_widens_the_permission_class(self):
        claude_read = workers.adapter("claude", "read", "do it", "t1")
        codex_read = workers.adapter("codex", "read", "do it", "t1")
        codex_write = workers.adapter("codex", "write", "do it", "t1")
        self.assertEqual(claude_read[:4], ["claude", "-p", "--permission-mode", "plan"],
                         "a read task on the claude adapter is the host's read-only class")
        self.assertEqual(workers.adapter("claude", "write", "do it", "t1")[:2], ["claude", "-p"])
        self.assertNotIn("--permission-mode", workers.adapter("claude", "write", "do it", "t1"),
                         "a write task keeps the host's default class, in its own worktree")
        self.assertEqual(codex_read[:5], ["codex", "exec", "-s", "read-only", "--color"])
        self.assertEqual(codex_write[:4], ["codex", "exec", "-s", "workspace-write"])
        self.assertIn(workers.TESTS_FILE, workers.adapter("codex", "write", "do it", "t1")[-1],
                      "the write task is told where to leave its test summary")
        # Review 2026-09-03: a class is widened through a value as easily as
        # through a flag, and the module's own assert must know both.
        for widening in ("--yolo", "danger-full-access", "bypassPermissions"):
            self.assertIn(widening, workers.FORBIDDEN_FLAGS, widening)
        # Round 2, 2026-09-03: `--sandbox=danger-full-access` is one element,
        # and an element match saw neither half. Either side of the `=`
        # counts; the prompt, last, is never read for one.
        for argv in (["codex", "exec", "--sandbox=danger-full-access", "the task"],
                     ["claude", "-p", "--permission-mode=bypassPermissions", "the task"],
                     ["claude", "-p", "--permission-mode", "bypassPermissions", "the task"],
                     ["codex", "exec", "--yolo", "the task"]):
            self.assertIsNotNone(workers.widening(argv), argv)
        self.assertIsNone(workers.widening(["codex", "exec", "the task says =--yolo"]))
        self.assertIsNone(workers.widening(["codex", "exec", "-s", "read-only", "x=--yolo"]))
        for argv in (claude_read, codex_read, codex_write,
                     workers.adapter("claude", "write", "do it", "t1")):
            joined = " ".join(argv)
            self.assertNotIn("--dangerously-skip-permissions", joined)
            self.assertNotIn("--full-auto", joined)
            self.assertNotIn("bypass", joined)
            self.assertNotIn("danger-full-access", joined)
            self.assertIn("[Antiphon worker", argv[-1], "the prompt names the label")
            self.assertIn("do it", argv[-1])
            self.assertIn("t1", argv[-1])

    def test_start_runs_the_stub_in_its_own_directory_with_the_hop_and_a_name(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            stub = self._stub(bin_dir, "codex")
            record = workers.new_task(project, kind="codex", task_class="read",
                                      sha256=SHA, size=15, hop=2)
            started = workers.start(project, record, "review the diff", env=self._env(bin_dir))
            self.assertEqual(started["state"], "running")
            self.assertIsInstance(started["pid"], int)
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(stub + ".env"):
                time.sleep(0.05)
            with open(stub + ".argv") as f:
                argv = f.read()
            self.assertIn("exec -s read-only", argv)
            self.assertIn("review the diff", argv)
            with open(stub + ".env") as f:
                seen = f.read()
            self.assertIn("HOP=2", seen, "the record's hop, one deeper than the parent")
            self.assertIn(f"NAME=worker-{record['id'][:8]}", seen)
            self.assertIn(f"CWD={project}", seen,
                          "a read task in a project that is not a git checkout runs in it")
            self.assertIn(f"DIR={workers.worker_dir(project, record['id'])}", seen)
            self.assertIn(f"TESTS={workers.tests_path(project, record['id'])}", seen)
            self.assertTrue(os.path.isdir(workers.worker_dir(project, record["id"])))
            self.assertTrue(os.path.exists(workers.log_path(project, record["id"])))
            self.assertEqual(os.path.dirname(workers.exit_path(project, record["id"])),
                             workers.worker_dir(project, record["id"]),
                             "old readers keep their exit mirror beside the work")
            self.assertEqual(os.path.dirname(workers.live_path(project, record["id"])),
                             workers.tasks_dir(project),
                             "current lifecycle authority stays outside worker writes")

    def test_a_write_task_needs_a_git_checkout_and_gets_its_own_worktree(self):
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            self._stub(bin_dir, "codex")
            record = workers.new_task(project, kind="codex", task_class="write",
                                      sha256=SHA, size=15)
            with self.assertRaises(workers.Refused) as refused:
                workers.start(project, record, "edit it", env=self._env(bin_dir))
            self.assertIn("a write task needs a git checkout", str(refused.exception))
            self.assertIsNone(workers.read_task(project, record["id"]),
                              "a refusal leaves no record")
            self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            self._stub(bin_dir, "claude")
            record = workers.new_task(project, kind="claude", task_class="write",
                                      sha256=SHA, size=15)
            started = workers.start(project, record, "edit it", env=self._env(bin_dir))
            worktree = workers.work_dir(project, record["id"])
            self.assertEqual(worktree, os.path.join(workers.worker_dir(project, record["id"]), "work"))
            self.assertTrue(os.path.exists(os.path.join(worktree, ".git")), "a worktree")
            listed = subprocess.run(["git", "-C", project, "worktree", "list", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertIn(record["id"], listed)
            self.assertEqual(started["state"], "running")
            self.assertEqual(started["base"], subprocess.run(
                ["git", "-C", project, "rev-parse", "HEAD"], capture_output=True,
                text=True).stdout.strip(), "the base the diff is taken against")
            # A read task in a git checkout gets a worktree too: nothing it
            # does can touch the user's own tree.
            self._stub(bin_dir, "codex")
            reading = workers.new_task(project, kind="codex", task_class="read",
                                       sha256=SHA, size=15)
            workers.start(project, reading, "look", env=self._env(bin_dir))
            self.assertTrue(os.path.exists(os.path.join(
                workers.work_dir(project, reading["id"]), ".git")))

    def test_the_workers_bridge_directory_is_the_project_not_its_worktree(self):
        """Review 2026-09-03: `ANTIPHON_CWD` named the worktree, so a bridge
        call from the worker landed in a store deleted with the work. The
        worker runs in its worktree and talks to the project's store."""
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            stub = self._stub(bin_dir, "codex")
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.start(project, record, "look", env=self._env(bin_dir))
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(stub + ".env"):
                time.sleep(0.05)
            with open(stub + ".env") as f:
                seen = f.read()
            work = os.path.realpath(workers.work_dir(project, record["id"]))
            self.assertIn(f"CWD={project} ", seen, "the bridge directory is the project")
            self.assertRegex(seen, rf"PWD={work}\b|PWD={workers.work_dir(project, record['id'])}\b",
                             "the work happens in the worktree")

    def test_a_checkout_without_a_commit_runs_a_read_task_in_place_and_refuses_a_write(self):
        """Measured on the E2E's own `git init` project: `worktree add` fails
        on `HEAD` before the first commit."""
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            stub = self._stub(bin_dir, "codex")
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA, size=1)
            started = workers.start(project, record, "look", env=self._env(bin_dir))
            self.assertEqual(started["state"], "running")
            self.assertIsNone(started["base"])
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(stub + ".env"):
                time.sleep(0.05)
            with open(stub + ".env") as f:
                self.assertIn(f"CWD={project}", f.read())
            writing = workers.new_task(project, kind="codex", task_class="write", sha256=SHA, size=1)
            with self.assertRaises(workers.Refused) as refused:
                workers.start(project, writing, "edit", env=self._env(bin_dir))
            self.assertIn("needs a commit", str(refused.exception))
            self.assertIsNone(workers.read_task(project, writing["id"]))

    def test_a_fifth_worker_is_refused_with_the_four(self):
        with tempfile.TemporaryDirectory() as project:
            ids = []
            for _ in range(4):
                record = workers.new_task(project, kind="codex", task_class="read",
                                          sha256=SHA, size=1)
                workers.update_task(project, record["id"],
                                    lambda c: c.update(state="running", pid=1, started_at=1.0))
                ids.append(record["id"])
            with self.assertRaises(workers.Refused) as refused:
                workers.admit(project)
            for task_id in ids:
                self.assertIn(task_id, str(refused.exception))
            workers.update_task(project, ids[0],
                                lambda c: c.update(state="completed", finished_at=2.0))
            workers.admit(project)

    def test_handoffs_in_flight_do_not_consume_worker_capacity(self):
        with tempfile.TemporaryDirectory() as project:
            for _ in range(workers.MAX_WORKERS):
                workers.new_task(
                    project, kind="codex", task_class="read", sha256=SHA,
                    size=1, to="build", state="handing")

            accepted = workers.accept(
                project, kind="claude", task_class="read", sha256=SHA, size=1)

        self.assertEqual(accepted["state"], "accepted")

    def test_an_in_flight_handoff_is_not_swept_as_a_dead_worker_start(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, to="build", state="handing")

            workers.sweep(
                project, record["created_at"] + workers.START_PATIENCE + 1)
            after = workers.read_task(project, record["id"])

        self.assertIsNotNone(after)
        self.assertEqual(after["state"], "handing")

    def test_a_peer_handoff_state_requires_a_named_recipient(self):
        with tempfile.TemporaryDirectory() as project:
            with self.assertRaisesRegex(ValueError, "must name its peer"):
                workers.new_task(
                    project, kind="codex", task_class="read", sha256=SHA,
                    size=1, state="handing")

            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            changed = workers.update_task(
                project, record["id"],
                lambda candidate: candidate.update(state="tracking_incomplete"))

        self.assertFalse(changed)

        with tempfile.TemporaryDirectory() as project:
            with self.assertRaisesRegex(ValueError, "task record is not valid"):
                workers.new_task(
                    project, kind="codex", task_class="read", sha256=SHA,
                    size=1, to="bad name", state="handing")

    def test_task_schema_v2_names_handoff_states_and_still_reads_v1_workers(self):
        self.assertEqual(workers.TASK_VERSION, 2,
                         "new states must not masquerade as the v1 schema")
        with tempfile.TemporaryDirectory() as project:
            current = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, to="build", state="handing")
            self.assertEqual(workers.read_task(project, current["id"])["version"], 2)

            ordinary = workers.new_task(
                project, kind="claude", task_class="read", sha256=SHA, size=1)
            self.assertEqual(ordinary["version"], 1,
                             "unchanged worker states remain visible to v1 readers")
            workers.update_task(
                project, current["id"],
                lambda candidate: candidate.update(state="handed"))
            self.assertEqual(workers.read_task(project, current["id"])["version"], 1,
                             "v1 already understands a completed handoff")

            legacy = dict(current, id="2e6b14f1-1659-544a-98d4-56d6eca8fa48",
                          version=1, state="accepted", to=None)
            workers._write(project, legacy)
            self.assertEqual(workers.read_task(project, legacy["id"]), legacy)

            legacy_handed = dict(legacy, state="handed", to="build")
            workers._write(project, legacy_handed)
            self.assertEqual(workers.read_task(project, legacy["id"]), legacy_handed,
                             "v1 already shipped the handed state")

            impossible_v1 = dict(legacy, state="handing", to="build")
            workers._write(project, impossible_v1)
            self.assertIsNone(workers.read_task(project, legacy["id"]),
                              "v1 never defined the peer-handoff states")

    def test_an_unfinished_handoff_never_claims_the_send_is_still_running(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, to="build", state="handing")
            refusals = []
            for action in (workers.result, workers.cancel):
                with self.assertRaises(workers.Refused) as refused:
                    action(project, record["id"])
                refusals.append(str(refused.exception))

        for refusal in refusals:
            self.assertIn("hand-off tracking is incomplete", refusal)
            self.assertIn("peer may already act", refusal)
            self.assertNotIn("still being handed", refusal)

    def test_admission_and_the_record_are_one_locked_step(self):
        """Review 2026-09-03: eight concurrent delegations started seven
        workers past a cap of four, because `admit` counted and `new_task`
        wrote with nothing between them. The count made slow on purpose:
        under the store's lock exactly four are admitted; with the lock
        gone, more than four are. The control half counts behind a barrier
        rather than a sleep, so all eight count the empty store before any
        writes whatever the machine's load — a 200 ms sleep let the
        control come out at exactly four under a concurrent suite."""
        import contextlib
        import threading
        from unittest.mock import patch
        real_count = workers._admitted

        def slow_count(cwd):
            time.sleep(0.2)
            return real_count(cwd)

        count_start = threading.Barrier(8, timeout=30)
        count_done = threading.Barrier(8, timeout=30)

        def counted_together(cwd):
            count_start.wait()
            observed = real_count(cwd)
            count_done.wait()
            return observed

        def storm(project):
            admitted, refused = [], []

            def one():
                try:
                    admitted.append(workers.accept(project, kind="codex", task_class="read",
                                                   sha256=SHA, size=1)["id"])
                except workers.Refused:
                    refused.append(1)
            threads = [threading.Thread(target=one) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            return len(admitted), len(refused)
        with tempfile.TemporaryDirectory() as project, \
             patch.object(workers, "_admitted", side_effect=slow_count):
            self.assertEqual(storm(project), (workers.MAX_WORKERS, 8 - workers.MAX_WORKERS))
            self.assertEqual(len(workers.tasks(project)), workers.MAX_WORKERS)
        with tempfile.TemporaryDirectory() as project, \
             patch.object(workers, "_admitted", side_effect=counted_together), \
             patch.object(workers, "_locked", lambda cwd: contextlib.nullcontext(True)):
            admitted, _refused = storm(project)
            self.assertEqual(admitted, 8,
                             "without the lock the race is real — the control")

    def test_a_fractional_timeout_is_clamped_not_refused(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertEqual(workers.new_task(project, kind="codex", task_class="read",
                                              sha256=SHA, size=1, timeout=0.5)["timeout"], 1)

    def test_a_task_id_may_be_chosen_by_the_caller(self):
        with tempfile.TemporaryDirectory() as project:
            chosen = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA,
                                      size=1, task_id=chosen)
            self.assertEqual(record["id"], chosen)
            with self.assertRaises(ValueError):
                workers.new_task(project, kind="codex", task_class="read", sha256=SHA,
                                 size=1, task_id="../x")

    def test_the_hop_budget_refuses_at_the_budget(self):
        self.assertEqual(workers.hop_budget({}), 1)
        self.assertEqual(workers.hop_budget({"ANTIPHON_HOP_BUDGET": "3"}), 3)
        self.assertEqual(workers.hop_budget({"ANTIPHON_HOP_BUDGET": "nonsense"}), 1)
        self.assertEqual(workers.current_hop({}), 0)
        self.assertEqual(workers.current_hop({"ANTIPHON_HOP": "1"}), 1)
        with self.assertRaises(workers.Refused) as refused:
            workers.check_hop({"ANTIPHON_HOP": "1"})
        self.assertIn("hop budget 1 reached", str(refused.exception))
        self.assertIn("ANTIPHON_HOP_BUDGET", str(refused.exception))
        workers.check_hop({})
        workers.check_hop({"ANTIPHON_HOP": "1", "ANTIPHON_HOP_BUDGET": "2"})

    def test_a_hop_that_is_not_a_count_is_refused_never_read_as_zero(self):
        """Review 2026-09-03: a negative or nonsense `ANTIPHON_HOP` read as
        hop 0, which is the top of the budget — a session carrying one is
        refused instead, and a record never carries a negative hop."""
        for value in ("-3", "nonsense", "1.5"):
            self.assertEqual(workers.current_hop({"ANTIPHON_HOP": value}), 0, value)
            with self.assertRaises(workers.Refused, msg=value) as refused:
                workers.check_hop({"ANTIPHON_HOP": value, "ANTIPHON_HOP_BUDGET": "9"})
            self.assertIn("is not a hop count", str(refused.exception))
        workers.check_hop({"ANTIPHON_HOP": "  "}, alias="ui")
        self.assertEqual(workers.current_hop({"ANTIPHON_HOP": ""}), 0, "blank is unset")
        # Round 2, 2026-09-03: unset and blank are one thing to a worker's
        # server, so a blank `ANTIPHON_HOP=` beside a worker's name is the
        # same fail-closed as no variable at all.
        with self.assertRaises(workers.Refused) as refused:
            workers.check_hop({"ANTIPHON_HOP": "", "ANTIPHON_HOP_BUDGET": "9"},
                              alias="worker-abc12345")
        self.assertIn("managed worker", str(refused.exception))



class LifecycleTest(unittest.TestCase):
    """What becomes of a worker: seen through its protected marker and process
    group, never guessed; killed on the task's timeout or on cancel; its
    directory swept only after its result was collected."""

    def _stub(self, root, kind, body):
        path = os.path.join(root, kind)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def _env(self, bin_dir):
        return dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))

    def _run(self, project, bin_dir, body, kind="codex", task_class="read", timeout=900):
        self._stub(bin_dir, kind, body)
        record = workers.new_task(project, kind=kind, task_class=task_class, sha256=SHA,
                                  size=5, timeout=timeout)
        return workers.start(project, record, "do it", env=self._env(bin_dir))

    def _settle(self, project, task_id, wanted, seconds=6):
        deadline = time.time() + seconds
        while time.time() < deadline:
            record = workers.status(project, task_id)
            if record["state"] == wanted:
                return record
            time.sleep(0.05)
        self.fail(f"never {wanted}: {workers.read_task(project, task_id)}")

    def _legacy_record(self, task_id=None, **changes):
        record = {
            "version": workers.LEGACY_TASK_VERSION,
            "id": task_id or str(workers.uuid.uuid4()),
            "kind": "codex", "task_class": "read", "state": "completed",
            "sha256": SHA, "size": 1, "parent": None, "timeout": 900,
            "hop": 1, "created_at": 1.0, "pid": None, "birth": None,
            "base": None, "exit_code": 0, "to": None, "started_at": 1.0,
            "finished_at": 2.0, "collected_at": None,
        }
        record.update(changes)
        self.assertTrue(workers._valid(record, record["id"]))
        return record

    def test_an_exit_is_read_off_the_exit_file_never_guessed(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            done = self._run(project, bin_dir, "echo finished; exit 0")
            record = self._settle(project, done["id"], "completed")
            self.assertEqual(record["exit_code"], 0)
            self.assertIsNotNone(record["finished_at"])
            failed = self._run(project, bin_dir, "echo boom >&2; exit 3")
            record = self._settle(project, failed["id"], "failed")
            self.assertEqual(record["exit_code"], 3)
            with open(workers.log_path(project, failed["id"])) as f:
                self.assertIn("boom", f.read())

    def test_untrusted_control_paths_neither_block_nor_follow_links(self):
        with tempfile.TemporaryDirectory() as project:
            task_id = "00000000-0000-0000-0000-000000000000"
            directory = workers.worker_dir(project, task_id)
            os.makedirs(directory)
            target = os.path.join(directory, "target")
            with open(target, "w", encoding="ascii") as stream:
                stream.write("0\n")
            os.symlink(target, workers.exit_path(project, task_id))
            self.assertIsNone(workers._read_exit(project, task_id))
            os.unlink(workers.exit_path(project, task_id))
            os.mkfifo(workers.exit_path(project, task_id))
            observed = []
            reader = threading.Thread(
                target=lambda: observed.append(workers._read_exit(project, task_id)),
                daemon=True)
            reader.start()
            reader.join(0.25)
            self.assertFalse(reader.is_alive(), "a FIFO must not block lifecycle status")
            self.assertEqual(observed, [None])

    def test_a_new_worker_is_live_by_its_lock_not_a_second_process_table_read(self):
        """A transient `ps` failure cannot turn a running worker into a
        terminal failure. The held lock and exact identity remain separate.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir, \
             patch.object(workers, "_process_start",
                          return_value="recorded birth") as process_start, \
             patch.object(workers, "_process_snapshot",
                          return_value=None) as process_snapshot:
            started = self._run(
                project, bin_dir,
                "echo ready > \"$ANTIPHON_WORKER_DIR/ready\"; "
                "while [ ! -e \"$ANTIPHON_WORKER_DIR/release\" ]; do sleep 0.01; done; "
                "exit 0")
            ready = os.path.join(workers.worker_dir(project, started["id"]), "ready")
            release = os.path.join(workers.worker_dir(project, started["id"]), "release")
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(ready):
                time.sleep(0.01)
            self.assertTrue(os.path.exists(ready), "the controlled worker started")
            try:
                observed = workers.status(project, started["id"])
                self.assertEqual(observed["state"], "running", observed)
                self.assertEqual(process_start.call_count, 1,
                                 "admission records one durable birth")
                self.assertGreaterEqual(process_snapshot.call_count, 1,
                                        "status uses the atomic identity snapshot")
                with open(workers.live_path(project, started["id"]), "rb") as live:
                    with self.assertRaises(BlockingIOError):
                        workers.fcntl.flock(
                            live.fileno(), workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
            finally:
                with open(release, "w", encoding="ascii"):
                    pass
            final = self._settle(project, started["id"], "completed")
            self.assertEqual(final["exit_code"], 0)

    def test_worker_birth_is_timezone_canonical_across_readers(self):
        with patch.dict(os.environ, {"TZ": "UTC"}):
            utc = workers._process_start(os.getpid())
        with patch.dict(os.environ, {"TZ": "Europe/Istanbul"}):
            istanbul = workers._process_start(os.getpid())
        self.assertIsNotNone(utc)
        self.assertEqual(utc, istanbul)

    def test_malformed_process_birth_is_never_recorded_or_signal_authority(self):
        malformed = subprocess.CompletedProcess(
            args=["ps"], returncode=0, stdout="schema drift\n", stderr="")
        with patch.object(workers.subprocess, "run", return_value=malformed):
            self.assertIsNone(workers._process_start(4242))

        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="schema drift", started_at=1.0))
            record = workers.read_task(project, record["id"])
            with patch.object(workers, "_reap"), \
                 patch.object(workers.os, "kill"), \
                 patch.object(workers.subprocess, "run", return_value=malformed), \
                 patch.object(workers, "_process_state", return_value="S"):
                self.assertEqual(workers._process_identity(record), "unknown")
                self.assertFalse(workers._signal_authorized(
                    project, record, lock="live"))

    def test_the_adapter_never_starts_before_its_running_record_is_durable(self):
        """Once a process exists, a failed bookkeeping write used to delete
        its record and directory even when stopping it could not be proved.
        The wrapper now waits behind a pipe until that write succeeds.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            marker = os.path.join(project, "adapter-ran")
            self._stub(bin_dir, "codex", 'echo ran > "$PROBE"; exit 0')
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            env = self._env(bin_dir)
            env["PROBE"] = marker
            with patch.object(workers, "update_task", return_value=False), \
                 patch.object(workers, "_kill_group", return_value="unknown") as kill:
                with self.assertRaisesRegex(workers.Refused,
                                            "task record could not be updated"):
                    workers.start(project, record, "do it", env=env)
            self.assertFalse(os.path.exists(marker), "the adapter never crossed its gate")
            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))
            kill.assert_not_called()

    def test_a_worker_without_an_exact_process_identity_is_never_admitted(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            marker = os.path.join(project, "adapter-ran")
            self._stub(bin_dir, "codex", 'echo ran > "$PROBE"; exit 0')
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            env = self._env(bin_dir)
            env["PROBE"] = marker
            with patch.object(workers, "START_IDENTITY_PATIENCE", 0.0), \
                 patch.object(workers, "_process_start", return_value=None):
                with self.assertRaisesRegex(workers.Refused,
                                            "process identity could not be recorded"):
                    workers.start(project, record, "do it", env=env)
            self.assertFalse(os.path.exists(marker), "the adapter never crossed its gate")
            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))

    def test_cancel_cannot_overtake_the_start_gate_and_erase_its_task(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            self._stub(bin_dir, "codex", "sleep 30")
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            at_gate = threading.Event()
            release_gate = threading.Event()
            original_write = workers.os.write
            outcome = []

            def controlled_write(fd, data):
                if data == b"1":
                    at_gate.set()
                    release_gate.wait(5)
                return original_write(fd, data)

            def launch():
                try:
                    outcome.append(workers.start(
                        project, record, "do it", env=self._env(bin_dir)))
                except Exception as error:  # captured for the test thread
                    outcome.append(error)

            with patch.object(workers.os, "write", side_effect=controlled_write):
                starter = threading.Thread(target=launch)
                starter.start()
                self.assertTrue(at_gate.wait(5), "start reached its admission gate")
                running = workers.read_task(project, record["id"])
                self.assertEqual(running["state"], "running")
                with patch.object(workers, "_kill_group") as kill:
                    overdue = workers.status(
                        project, record["id"],
                        now=running["started_at"] + running["timeout"] + 1,
                        patience=0.0)
                    self.assertEqual(overdue["state"], "running")
                    with self.assertRaisesRegex(workers.Refused, "still starting"):
                        workers.cancel(project, record["id"])
                kill.assert_not_called()
                release_gate.set()
                starter.join(5)

            self.assertFalse(starter.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], dict)
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "running")
            deadline = time.time() + 5
            while (time.time() < deadline
                   and workers._lock_liveness(project, record["id"]) == "starting"):
                time.sleep(0.01)
            workers.cancel(project, record["id"])

    def test_a_gate_failure_preserves_a_concurrently_recorded_terminal_outcome(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            self._stub(bin_dir, "codex", "exit 0")
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            at_gate = threading.Event()
            release_gate = threading.Event()
            original_write = workers.os.write
            outcome = []

            def controlled_write(fd, data):
                if data == b"1":
                    at_gate.set()
                    release_gate.wait(5)
                return original_write(fd, data)

            def launch():
                try:
                    workers.start(project, record, "do it", env=self._env(bin_dir))
                except Exception as error:  # captured for the test thread
                    outcome.append(error)

            with patch.object(workers.os, "write", side_effect=controlled_write):
                starter = threading.Thread(target=launch)
                starter.start()
                self.assertTrue(at_gate.wait(5), "start reached its admission gate")
                running = workers.read_task(project, record["id"])
                os.kill(running["pid"], workers.signal.SIGKILL)
                deadline = time.time() + 5
                while time.time() < deadline:
                    observed = workers.status(project, record["id"])
                    if (observed is not None
                            and observed["state"] == "outcome_unknown"):
                        break
                    time.sleep(0.01)
                self.assertEqual(observed["state"], "outcome_unknown")
                release_gate.set()
                starter.join(5)

            self.assertFalse(starter.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], workers.Refused)
            final = workers.read_task(project, record["id"])
            self.assertIsNotNone(final)
            self.assertEqual(final["state"], "outcome_unknown")

    def test_start_refusal_does_not_delete_a_superseding_terminal_state(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0))
            stale = workers.read_task(project, record["id"])
            workers._finish(
                project, record["id"], "outcome_unknown", None, now=2.0,
                stop_resolution="unknown")

            with self.assertRaisesRegex(workers.Refused, "activation failed"):
                workers._refuse(
                    project, record, "not delegated: activation failed",
                    expected=stale)

            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "outcome_unknown")
            self.assertTrue(os.path.isdir(directory))

    def test_a_ready_timeout_cannot_start_the_adapter_after_refusal(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            marker = os.path.join(project, "adapter-ran")
            self._stub(bin_dir, "codex", 'echo ran > "$PROBE"; exit 0')
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            env = self._env(bin_dir)
            env["PROBE"] = marker
            real_select = workers.select.select

            def timeout_after_ack(reads, _writes, _errors, _patience):
                readable, _writable, _exceptional = real_select(reads, [], [], 5)
                self.assertTrue(readable, "the wrapper queued READY before the timeout")
                return [], [], []

            with patch.object(workers.select, "select", side_effect=timeout_after_ack):
                with self.assertRaisesRegex(workers.Refused,
                                            "did not acknowledge its start"):
                    workers.start(project, record, "do it", env=env)
            self.assertFalse(os.path.exists(marker))
            self.assertIsNone(workers.read_task(project, record["id"]))

    def test_log_open_failure_closes_every_partial_start_descriptor(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            opened = []
            real_pipe = workers.os.pipe
            real_open = open

            def tracked_pipe():
                pair = real_pipe()
                opened.extend(pair)
                return pair

            def fail_log(path, *args, **kwargs):
                if path == workers.log_path(project, record["id"]):
                    raise OSError("injected log failure")
                return real_open(path, *args, **kwargs)

            with patch.object(workers.os, "pipe", side_effect=tracked_pipe), \
                 patch.object(workers, "open", side_effect=fail_log, create=True):
                with self.assertRaisesRegex(workers.Refused, "log could not be opened"):
                    workers.start(project, record, "do it")

            for descriptor in opened:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertIsNone(workers.read_task(project, record["id"]))

    def test_popen_failure_closes_descriptors_and_the_log(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            descriptors = []
            logs = []
            real_pipe = workers.os.pipe
            real_open = open

            def tracked_pipe():
                pair = real_pipe()
                descriptors.extend(pair)
                return pair

            def tracked_open(path, *args, **kwargs):
                stream = real_open(path, *args, **kwargs)
                if path == workers.log_path(project, record["id"]):
                    logs.append(stream)
                return stream

            with patch.object(workers.os, "pipe", side_effect=tracked_pipe), \
                 patch.object(workers, "open", side_effect=tracked_open, create=True), \
                 patch.object(workers.subprocess, "Popen",
                              side_effect=OSError("injected Popen failure")):
                with self.assertRaisesRegex(workers.Refused, "CLI could not be started"):
                    workers.start(project, record, "do it")

            self.assertEqual(len(logs), 1)
            self.assertTrue(logs[0].closed)
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertIsNone(workers.read_task(project, record["id"]))

    def test_start_claims_starting_lease_before_slow_preparation(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            self._stub(bin_dir, "codex", "sleep 30")
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            preparing = threading.Event()
            release = threading.Event()
            outcome = []

            def slow_checkout(_cwd):
                preparing.set()
                release.wait(5)
                return False

            def launch():
                try:
                    outcome.append(workers.start(
                        project, record, "do it", env=self._env(bin_dir)))
                except Exception as error:  # captured for the test thread
                    outcome.append(error)

            with patch.object(workers, "_git_checkout",
                              side_effect=slow_checkout):
                thread = threading.Thread(target=launch)
                thread.start()
                self.assertTrue(preparing.wait(5), "start entered slow preparation")
                workers.sweep(
                    project, record["created_at"] + workers.START_PATIENCE + 1)
                kept = workers.read_task(project, record["id"])
                self.assertIsNotNone(kept)
                self.assertEqual(kept["state"], "accepted")
                self.assertEqual(
                    workers._lock_liveness(project, record["id"]), "starting")
                release.set()
                thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], dict)
            workers.cancel(project, record["id"])

    def test_second_start_cannot_delete_the_first_start_claim(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            self._stub(bin_dir, "codex", "sleep 30")
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            preparing = threading.Event()
            release = threading.Event()
            outcome = []

            def slow_checkout(_cwd):
                preparing.set()
                release.wait(5)
                return False

            def launch():
                try:
                    outcome.append(workers.start(
                        project, record, "do it", env=self._env(bin_dir)))
                except Exception as error:  # captured for the test thread
                    outcome.append(error)

            with patch.object(workers, "_git_checkout",
                              side_effect=slow_checkout):
                thread = threading.Thread(target=launch)
                thread.start()
                self.assertTrue(preparing.wait(5), "first start owns its lease")
                with self.assertRaisesRegex(workers.Refused, "already starting"):
                    workers.start(project, record, "duplicate", env=self._env(bin_dir))
                self.assertIsNotNone(workers.read_task(project, record["id"]))
                self.assertEqual(
                    workers._lock_liveness(project, record["id"]), "starting")
                release.set()
                thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], dict)
            workers.cancel(project, record["id"])

    def test_a_late_refusal_cannot_delete_a_new_start_claim(self):
        """Exact row equality is not ownership of its lifecycle sidecar."""
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            first = workers._claim_start(project, record)
            os.close(first)
            second = workers._claim_start(project, record)
            try:
                with self.assertRaisesRegex(workers.Refused, "first refused"):
                    workers._refuse(
                        project, record, "not delegated: first refused")

                self.assertEqual(
                    workers.read_task(project, record["id"]), record)
                self.assertEqual(
                    workers._read_live_marker(second), workers.LIVE_STARTING)
                self.assertEqual(os.fstat(second).st_nlink, 1)
                self.assertTrue(os.path.exists(
                    workers.live_path(project, record["id"])))
            finally:
                os.close(second)

    def test_refusal_cleanup_cannot_erase_a_reused_task_id(self):
        """External cleanup remains bound to the generation it retired."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(workers.time, "time", return_value=1234.0):
            task_id = "12345678-1234-1234-1234-123456789abc"
            original = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, task_id=task_id)
            original_lease = workers._claim_start(project, original)
            entered_cleanup = threading.Event()
            release_cleanup = threading.Event()
            outcome = []
            real_remove = workers._remove_dir

            def paused_remove(cwd, record, **kwargs):
                entered_cleanup.set()
                release_cleanup.wait(5)
                return real_remove(cwd, record, **kwargs)

            def refuse_original():
                try:
                    workers._refuse(
                        project, original, "not delegated: original refused",
                        lease_fd=original_lease)
                except Exception as error:  # captured for the test thread
                    outcome.append(error)

            with patch.object(workers, "_remove_dir", side_effect=paused_remove):
                thread = threading.Thread(target=refuse_original)
                thread.start()
                self.assertTrue(
                    entered_cleanup.wait(5), "the retired task reached cleanup")
                self.assertIsNone(workers.read_task(project, task_id))

                replacement = workers.new_task(
                    project, kind="codex", task_class="read", sha256=SHA,
                    size=1, task_id=task_id)
                self.assertEqual(replacement, original,
                                 "the regression does not rely on timestamp identity")
                replacement_lease = workers._claim_start(project, replacement)
                sentinel = os.path.join(
                    workers.worker_dir(project, task_id), "new-owner")
                os.makedirs(os.path.dirname(sentinel), exist_ok=True)
                with open(sentinel, "w", encoding="utf-8") as stream:
                    stream.write("new generation\n")
                release_cleanup.set()
                thread.join(5)

            try:
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(outcome), 1)
                self.assertIsInstance(outcome[0], workers.Refused)
                self.assertEqual(workers.read_task(project, task_id), replacement)
                self.assertTrue(os.path.exists(workers.live_path(project, task_id)))
                self.assertTrue(os.path.isfile(sentinel))
            finally:
                os.close(replacement_lease)

    def test_cleanup_holds_task_admission_closed_through_directory_removal(self):
        """Two cleaners cannot authorize deletion around a same-id reuse."""
        with tempfile.TemporaryDirectory() as project:
            task_id = "12345678-1234-1234-1234-123456789abc"
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, task_id=task_id)
            directory = workers.worker_dir(project, task_id)
            os.makedirs(directory)
            self.assertTrue(workers._discard_record(project, task_id))

            entered_removal = threading.Event()
            release_removal = threading.Event()
            outcome = []
            real_rmtree = workers.shutil.rmtree

            def paused_rmtree(path, *args, **kwargs):
                if path == directory:
                    entered_removal.set()
                    release_removal.wait(5)
                return real_rmtree(path, *args, **kwargs)

            def clean():
                outcome.append(workers._remove_dir(
                    project, record, row_expected=False))

            with patch.object(workers.shutil, "rmtree", side_effect=paused_rmtree):
                thread = threading.Thread(target=clean)
                thread.start()
                self.assertTrue(
                    entered_removal.wait(5), "cleanup reached directory removal")
                lock_fd = os.open(
                    os.path.join(workers.tasks_dir(project), ".lock"),
                    os.O_RDWR)
                try:
                    with self.assertRaises((BlockingIOError, OSError)):
                        workers.fcntl.flock(
                            lock_fd,
                            workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
                finally:
                    os.close(lock_fd)
                release_removal.set()
                thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [True])
            replacement = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, task_id=task_id)
            replacement_lease = workers._claim_start(project, replacement)
            try:
                sentinel = os.path.join(directory, "new-owner")
                os.makedirs(directory, exist_ok=True)
                with open(sentinel, "w", encoding="utf-8") as stream:
                    stream.write("new generation\n")
                self.assertTrue(os.path.isfile(sentinel))
            finally:
                os.close(replacement_lease)

    def test_early_start_lease_failure_does_not_reenter_the_task_lock(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            began = time.monotonic()
            with patch.object(workers, "_write_live_marker",
                              side_effect=OSError("injected marker failure")):
                with self.assertRaisesRegex(workers.Refused,
                                            "live lock could not be created"):
                    workers.start(project, record, "do it")
            self.assertLess(time.monotonic() - began, 0.5)
            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))

    def test_the_wrapper_publishes_its_exit_before_releasing_the_live_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "live.lock")
            exit_file = os.path.join(directory, "exit")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lock_fd, workers.LIVE_STARTING)
            gate_read, gate_write = os.pipe()
            ready_read, ready_write = os.pipe()
            commit_read, commit_write = os.pipe()
            os.write(gate_write, b"1")
            os.close(gate_write)
            os.write(commit_write, b"1")
            os.close(commit_write)
            real_write = workers._write_worker_exit

            def exit_while_locked(path, code):
                competing = os.open(lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        workers.fcntl.flock(
                            competing,
                            workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
                finally:
                    os.close(competing)
                real_write(path, code)

            with patch.object(workers, "_write_worker_exit",
                              side_effect=exit_while_locked):
                code = workers._worker_wrapper(
                    lock_fd, gate_read, ready_write, commit_read,
                    exit_file, ["/usr/bin/true"], proof=PROOF)

            self.assertEqual(code, 0)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            with open(lock_path, "rb") as stream:
                self.assertEqual(
                    stream.read(), workers._published_marker(0, PROOF))
            competing = os.open(lock_path, os.O_RDWR)
            try:
                workers.fcntl.flock(
                    competing, workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
            finally:
                os.close(competing)

    @unittest.skipUnless(hasattr(signal, "pthread_sigmask"),
                         "POSIX signal masks are required by the worker protocol")
    def test_publication_fences_sigterm_before_snapshot_and_marker_commit(self):
        """A stop cannot land between the signed bit and its durable marker.

        SIGTERM raised from inside the marker writer is deliberately after the
        publication linearization point.  It therefore remains pending until
        the natural marker is durable instead of changing an already-sampled
        bit under the writer.
        """
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "live.lock")
            exit_file = os.path.join(directory, "exit")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lock_fd, workers.LIVE_STARTING)
            gate_read, gate_write = os.pipe()
            ready_read, ready_write = os.pipe()
            commit_read, commit_write = os.pipe()
            os.write(gate_write, b"1")
            os.close(gate_write)
            os.write(commit_write, b"1")
            os.close(commit_write)
            real_write = workers._write_live_marker
            terminal_masks = []

            def interrupt_terminal_write(fd, marker):
                if workers._published_parts(marker) is not None:
                    current = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                    terminal_masks.append(signal.SIGTERM in current)
                    signal.raise_signal(signal.SIGTERM)
                real_write(fd, marker)

            with patch.object(workers, "_write_live_marker",
                              side_effect=interrupt_terminal_write):
                code = workers._worker_wrapper(
                    lock_fd, gate_read, ready_write, commit_read,
                    exit_file, ["/usr/bin/true"], proof=PROOF)

            self.assertEqual(code, 0)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            self.assertEqual(terminal_masks, [True])
            with open(lock_path, "rb") as stream:
                self.assertEqual(
                    stream.read(), workers._published_marker(0, PROOF))

    def test_term_observed_before_publication_is_signed_as_a_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "live.lock")
            exit_file = os.path.join(directory, "exit")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lock_fd, workers.LIVE_STARTING)
            gate_read, gate_write = os.pipe()
            ready_read, ready_write = os.pipe()
            commit_read, commit_write = os.pipe()
            os.write(gate_write, b"1")
            os.close(gate_write)
            os.write(commit_write, b"1")
            os.close(commit_write)
            real_exit = workers._write_worker_exit

            def interrupt_before_publication(path, code):
                signal.raise_signal(signal.SIGTERM)
                real_exit(path, code)

            with patch.object(workers, "_write_worker_exit",
                              side_effect=interrupt_before_publication):
                code = workers._worker_wrapper(
                    lock_fd, gate_read, ready_write, commit_read,
                    exit_file, ["/usr/bin/true"], proof=PROOF)

            self.assertEqual(code, 0)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            with open(lock_path, "rb") as stream:
                self.assertEqual(
                    stream.read(),
                    workers._published_marker(0, PROOF, stopped=True))

    def test_the_wrapper_never_starts_an_adapter_without_the_final_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "live.lock")
            exit_file = os.path.join(directory, "exit")
            marker = os.path.join(directory, "adapter-ran")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lock_fd, workers.LIVE_STARTING)
            gate_read, gate_write = os.pipe()
            ready_read, ready_write = os.pipe()
            commit_read, commit_write = os.pipe()
            os.write(gate_write, b"1")
            os.close(gate_write)
            os.close(commit_write)

            code = workers._worker_wrapper(
                lock_fd, gate_read, ready_write, commit_read, exit_file,
                ["/bin/sh", "-c", f"echo ran > {marker!r}"], proof=PROOF)

            self.assertEqual(code, 125)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            self.assertFalse(os.path.exists(marker))
            with open(exit_file, encoding="ascii") as stream:
                self.assertEqual(stream.read(), "125\n")
            with open(lock_path, "rb") as stream:
                self.assertEqual(
                    stream.read(), workers._published_marker(125, PROOF))

    def test_a_legacy_exit_mirror_failure_does_not_hide_the_current_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "live.lock")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lock_fd, workers.LIVE_STARTING)
            gate_read, gate_write = os.pipe()
            ready_read, ready_write = os.pipe()
            commit_read, commit_write = os.pipe()
            os.write(gate_write, b"1")
            os.close(gate_write)
            os.write(commit_write, b"1")
            os.close(commit_write)

            with patch.object(workers, "_write_worker_exit",
                              side_effect=OSError("mirror unavailable")):
                code = workers._worker_wrapper(
                    lock_fd, gate_read, ready_write, commit_read,
                    os.path.join(directory, "exit"), ["/usr/bin/true"],
                    proof=PROOF)

            self.assertEqual(code, 0)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            with open(lock_path, "rb") as stream:
                self.assertEqual(
                    stream.read(), workers._published_marker(0, PROOF))

    def test_the_adapter_inherits_neither_wrapper_control_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "live.lock")
            exit_file = os.path.join(directory, "exit")
            observed = os.path.join(directory, "observed")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lock_fd, workers.LIVE_STARTING)
            gate_read, gate_write = os.pipe()
            ready_read, ready_write = os.pipe()
            commit_read, commit_write = os.pipe()
            os.write(gate_write, b"1")
            os.close(gate_write)
            os.write(commit_write, b"1")
            os.close(commit_write)
            script = (
                "import os,sys\n"
                "out=[]\n"
                "for raw in sys.argv[2:]:\n"
                " try: os.fstat(int(raw)); out.append('open')\n"
                " except OSError: out.append('closed')\n"
                "open(sys.argv[1], 'w').write(','.join(out))\n")
            code = workers._worker_wrapper(
                lock_fd, gate_read, ready_write, commit_read, exit_file,
                [sys.executable, "-c", script, observed,
                 str(lock_fd), str(gate_read), str(ready_write), str(commit_read)],
                proof=PROOF)
            self.assertEqual(code, 0)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            with open(observed, encoding="ascii") as stream:
                self.assertEqual(stream.read(), "closed,closed,closed,closed")

    def test_a_current_worker_cannot_finish_itself_before_releasing_its_lock(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'echo 0 > "$ANTIPHON_WORKER_DIR/exit"; '
                'printf "published\\n" > "$ANTIPHON_WORKER_DIR/live.lock"; '
                'echo ready > "$ANTIPHON_WORKER_DIR/ready"; '
                'while [ ! -e "$ANTIPHON_WORKER_DIR/release" ]; do sleep 0.01; done; '
                "exit 0")
            ready = os.path.join(workers.worker_dir(project, started["id"]), "ready")
            release = os.path.join(workers.worker_dir(project, started["id"]), "release")
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(ready):
                time.sleep(0.01)
            try:
                self.assertTrue(os.path.exists(ready))
                self.assertEqual(workers.status(project, started["id"])["state"],
                                 "running")
            finally:
                with open(release, "w", encoding="ascii"):
                    pass
            final = self._settle(project, started["id"], "completed")
            self.assertEqual(final["exit_code"], 0)

    def test_a_current_worker_cannot_finish_itself_by_deleting_its_lock(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'rm -f "$ANTIPHON_WORKER_DIR/live.lock"; '
                'echo 0 > "$ANTIPHON_WORKER_DIR/exit"; '
                'echo $$ > "$ANTIPHON_WORKER_DIR/child.pid"; sleep 30')
            child = self._child_pid(project, started["id"])
            try:
                self.assertEqual(
                    workers._lock_liveness(project, started["id"]), "live")
                observed = workers.status(project, started["id"])
                self.assertEqual(observed["state"], "running", observed)
                os.kill(child, 0)
            finally:
                try:
                    os.killpg(started["pid"], workers.signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def test_a_replacement_worker_lock_cannot_forge_a_successful_exit(self):
        """The adapter owns its worker directory, not lifecycle authority."""
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'rm -f "$ANTIPHON_WORKER_DIR/live.lock"; '
                'printf "published\\n" > "$ANTIPHON_WORKER_DIR/live.lock"; '
                'printf "0\\n" > "$ANTIPHON_WORKER_DIR/exit"; '
                'kill -9 "$PPID"; exit 3')

            final = self._settle(project, started["id"], "outcome_unknown")

            self.assertEqual(final["exit_code"], None)
            with open(os.path.join(workers.worker_dir(project, started["id"]),
                                   "live.lock"), encoding="ascii") as stream:
                self.assertEqual(stream.read(), "published\n")

    def test_an_adapter_cannot_publish_the_task_store_marker_by_path(self):
        """The adapter runs as the same OS user and knows the project path.
        Advisory locking alone therefore cannot authenticate marker bytes.
        Killing the supervisor after writing a plausible terminal marker must
        retain the worker and its slot, never manufacture completion.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'task_id=${ANTIPHON_WORKER_DIR##*/}; '
                f'control="$ANTIPHON_CWD/.antiphon/{workers.TASK_STORE}/$task_id.live"; '
                'printf "published:0\\n" > "$control"; '
                'kill -9 "$PPID"; '
                'echo $$ > "$ANTIPHON_WORKER_DIR/child.pid"; sleep 30')
            child = self._child_pid(project, started["id"])
            deadline = time.time() + 5
            while (time.time() < deadline
                   and workers._lock_liveness(project, started["id"])
                       == "settling"):
                time.sleep(0.01)
            try:
                observed = workers.status(project, started["id"])
                self.assertEqual(observed["state"], "running")
                self.assertEqual(len(workers._admitted(project)), 1)
                os.kill(child, 0)
            finally:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(started["pid"], workers.signal.SIGKILL)

    def test_a_forged_marker_cannot_publish_after_the_group_ends(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            digest = hashlib.sha256(bytes.fromhex(PROOF)).hexdigest()
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=999_999, birth="recorded birth",
                started_at=time.time(),
                to=workers._encode_control(proof=digest)))
            fd = os.open(workers.live_path(project, record["id"]),
                         os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(fd, workers._published_marker(0))
            finally:
                os.close(fd)
            with patch.object(workers, "_process_identity", return_value="absent"), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                final = workers.status(project, record["id"])
            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_a_well_formed_marker_with_the_wrong_proof_cannot_publish(self):
        """Authentication is the digest match, not merely marker grammar."""
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            digest = hashlib.sha256(bytes.fromhex(PROOF)).hexdigest()
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=999_999, birth="recorded birth",
                started_at=time.time(),
                to=workers._encode_control(proof=digest)))
            marker = os.open(workers.live_path(project, record["id"]),
                             os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(
                    marker, workers._published_marker(0, "cd" * 32))
            finally:
                os.close(marker)

            with patch.object(workers, "_process_identity", return_value="absent"), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                final = workers.status(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_current_generation_cannot_downgrade_to_a_bare_publication(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=999_999, birth="recorded birth",
                started_at=time.time(), to=None))
            fd = os.open(workers.live_path(project, record["id"]),
                         os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(fd, workers._published_marker(0))
            finally:
                os.close(fd)
            with patch.object(workers, "_process_identity", return_value="absent"), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                final = workers.status(project, record["id"])
            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_status_recovers_a_durable_stop_after_unpublished_wrapper_death(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=999_999, birth="recorded birth",
                started_at=time.time(),
                to=workers._encode_control(stop="cancelled")))
            with patch.object(workers, "_lock_observation",
                              return_value=("dead", None)), \
                 patch.object(workers, "_worker_liveness", return_value="dead"):
                final = workers.status(project, record["id"])
            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_supervisor_drains_background_descendants_before_publication(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'sleep 30 & echo $! > "$ANTIPHON_WORKER_DIR/child.pid"; exit 0')
            child = self._child_pid(project, started["id"])
            final = self._settle(project, started["id"], "completed")
            self.assertEqual(final["exit_code"], 0)
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_an_adapter_descendant_is_drained_before_it_can_replace_the_exit(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'task_id=${ANTIPHON_WORKER_DIR##*/}; '
                f'control="$ANTIPHON_CWD/.antiphon/{workers.TASK_STORE}/$task_id.live"; '
                'nohup /bin/sh -c \'until grep -q "^published" "$1"; do '
                'sleep 0.01; done; printf "0\\n" > "$2"; : > "$3"\' '
                'adapter-child "$control" "$ANTIPHON_WORKER_DIR/exit" '
                '"$ANTIPHON_WORKER_DIR/overwrite-done" >/dev/null 2>&1 & '
                'exit 3')
            done = os.path.join(workers.worker_dir(project, started["id"]),
                                "overwrite-done")
            final = self._settle(project, started["id"], "failed")
            self.assertFalse(os.path.exists(done), "the descendant was drained")
            self.assertEqual((final["state"], final["exit_code"]),
                             ("failed", 3))

    def test_partial_live_markers_never_authorize_cancel_or_timeout(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=900)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            birth = workers._process_start(os.getpid())
            digest = hashlib.sha256(bytes.fromhex(PROOF)).hexdigest()
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=os.getpid(), birth=birth,
                started_at=time.time(), to=workers._encode_control(
                    proof=digest, birth=birth, started=time.time())))
            lock_fd = os.open(workers.live_path(project, record["id"]),
                              os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            try:
                os.ftruncate(lock_fd, 0)  # STARTING -> ACTIVE write gap
                with patch.object(workers, "_kill_group") as kill:
                    with self.assertRaisesRegex(workers.Refused,
                                                "identity could not be verified"):
                        workers.cancel(project, record["id"])
                kill.assert_not_called()

                workers._write_live_marker(lock_fd, workers.LIVE_ACTIVE)
                with open(workers.exit_path(project, record["id"]),
                          "w", encoding="ascii") as stream:
                    stream.write("0\n")
                os.ftruncate(lock_fd, 0)  # ACTIVE -> PUBLISHED write gap
                with patch.object(workers, "_kill_group") as kill:
                    observed = workers.status(
                        project, record["id"],
                        now=time.time() + record["timeout"] + 1,
                        patience=0.0)
                kill.assert_not_called()
                self.assertEqual(observed["state"], "running")
                workers._write_live_marker(
                    lock_fd, workers._published_marker(0, PROOF))
                with patch.object(workers, "_kill_group") as kill:
                    settling = workers.status(
                        project, record["id"],
                        now=time.time() + record["timeout"] + 1,
                        patience=0.0)
                kill.assert_not_called()
                self.assertEqual(settling["state"], "running")
            finally:
                workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_UN)
                os.close(lock_fd)

            with patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                final = workers.status(project, record["id"])
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

    def test_a_dead_wrapper_does_not_make_its_live_group_safe_to_signal(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'echo 0 > "$ANTIPHON_WORKER_DIR/exit"; '
                'printf "published\\n" > "$ANTIPHON_WORKER_DIR/live.lock"; '
                'echo $$ > "$ANTIPHON_WORKER_DIR/child.pid"; sleep 30')
            child = self._child_pid(project, started["id"])
            os.kill(started["pid"], workers.signal.SIGKILL)
            deadline = time.time() + 5
            while time.time() < deadline and workers._lock_liveness(
                    project, started["id"]) == "live":
                time.sleep(0.01)
            try:
                observed = workers.status(project, started["id"])
                self.assertEqual(observed["state"], "running", observed)
                os.kill(child, 0)
                with self.assertRaisesRegex(workers.Refused,
                                            "identity could not be verified"):
                    workers.cancel(project, started["id"])
                os.kill(child, 0)
            finally:
                try:
                    os.killpg(started["pid"], workers.signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def test_an_exit_reaps_the_wrapper_when_this_reader_is_its_parent(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir, "exit 0")
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(
                    workers.exit_path(project, started["id"])):
                time.sleep(0.01)
            with patch.object(workers, "_reap", wraps=workers._reap) as reap:
                self.assertEqual(
                    self._settle(project, started["id"], "completed")["state"],
                    "completed")
            self.assertIn(
                call(started["pid"], 0.25), reap.call_args_list,
                "the terminal exit path gives its wrapper a bounded reap")
            with self.assertRaises(ChildProcessError):
                os.waitpid(started["pid"], os.WNOHANG)

    def test_a_published_exit_needs_the_process_group_to_be_finished(self):
        """Publication plus group death, not either fact alone, is terminal."""
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with open(workers.exit_path(project, record["id"]),
                      "w", encoding="ascii") as stream:
                stream.write("0\n")

            with patch.object(workers, "_lock_observation",
                              return_value=("published", (0, False))), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead") as group:
                final = workers.status(project, record["id"])

            group.assert_called_once_with(4242)
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

    def test_a_published_exit_cannot_finish_a_live_process_group(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_lock_observation",
                              return_value=("published", (0, False))), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="live"):
                final = workers.status(project, record["id"])
            self.assertEqual((final["state"], final["exit_code"]),
                             ("running", None))
            self.assertEqual(len(workers._admitted(project)), 1)

    def test_malformed_process_table_never_proves_group_death(self):
        """A successful `ps` exit is not an empty-group proof when even one
        nonblank row cannot be parsed under the requested schema.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            malformed = subprocess.CompletedProcess(
                args=["ps"], returncode=0, stdout="schema drift\n", stderr="")

            with patch.object(workers, "_lock_observation",
                              return_value=("published", (0, False))), \
                 patch.object(workers.subprocess, "run", return_value=malformed):
                self.assertEqual(workers._group_process_liveness(4242), "unknown")
                final = workers.status(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]),
                             ("running", None))
            self.assertEqual(len(workers._admitted(project)), 1)

    def test_malformed_group_process_state_never_proves_group_death(self):
        malformed = subprocess.CompletedProcess(
            args=["ps"], returncode=0,
            stdout="4242 4242 Zombie\n", stderr="")
        with patch.object(workers.subprocess, "run", return_value=malformed):
            self.assertIsNone(workers._group_members(4242))
            self.assertEqual(workers._group_process_liveness(4242), "unknown")

    def test_adapter_drain_tolerates_one_unreadable_process_snapshot(self):
        """A transient `ps` failure must not abandon descendants and leave
        the supervisor without an authenticated outcome.
        """
        snapshots = [None, [(4343, "S")], None, []]
        with patch.object(workers, "_group_members", side_effect=snapshots), \
             patch.object(workers.os, "getpgid", return_value=4242), \
             patch.object(workers.os, "kill") as kill:
            self.assertTrue(workers._drain_adapter_group(
                4242, supervisor_pid=4242, patience=0.1))

        kill.assert_called_once_with(4343, signal.SIGTERM)

    def test_group_liveness_tolerates_one_unreadable_process_snapshot(self):
        """Status and admission share the same bounded transient tolerance;
        one failed process-table read cannot pin a finished worker's slot.
        """
        with patch.object(workers, "_group_members", side_effect=(None, [])):
            self.assertEqual(workers._group_process_liveness(4242), "dead")

    def test_malformed_process_state_never_authorizes_a_signal(self):
        with tempfile.TemporaryDirectory() as project:
            birth = "Fri Sep 4 13:06:57 2026"
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth=birth, started_at=1.0))
            record = workers.read_task(project, record["id"])
            malformed = subprocess.CompletedProcess(
                args=["ps"], returncode=0, stdout="schema drift\n", stderr="")

            with patch.object(workers, "_reap"), \
                 patch.object(workers.os, "kill"), \
                 patch.object(workers.subprocess, "run", return_value=malformed):
                self.assertIsNone(workers._process_state(4242))
                self.assertEqual(workers._process_identity(record), "unknown")
                self.assertFalse(workers._signal_authorized(
                    project, record, lock="live"))

    def test_process_identity_uses_one_atomic_birth_and_state_snapshot(self):
        """Birth from one owner and STAT from its replacement must never be
        composed into signal authority; one `ps` row owns both observations.
        """
        with tempfile.TemporaryDirectory() as project:
            birth = "Fri Sep 4 13:06:57 2026"
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth=birth, started_at=1.0))
            record = workers.read_task(project, record["id"])
            snapshot = subprocess.CompletedProcess(
                args=["ps"], returncode=0,
                stdout="Fri Sep  4 13:06:57 2026 S\n", stderr="")

            with patch.object(workers, "_reap"), \
                 patch.object(workers.os, "kill"), \
                 patch.object(workers.subprocess, "run", return_value=snapshot) as run:
                self.assertEqual(workers._process_identity(record), "live")

            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][2], "lstart=,stat=")

    def test_an_authenticated_zombie_is_finished_not_a_live_worker(self):
        """A separate live parent keeps a finished shell wrapper as a zombie."""
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            pid_file = os.path.join(project, "legacy.pid")
            release = os.path.join(project, "release")
            exit_file = workers.exit_path(project, record["id"])
            helper = (
                "import subprocess,sys,time\n"
                "child=subprocess.Popen(['/bin/sh','-c',"
                "'while [ ! -e \"$1\" ]; do sleep 0.01; done; '"
                "'printf \"0\\\\n\" > \"$2\"',"
                "'legacy-wrapper',sys.argv[2],sys.argv[3]],start_new_session=True)\n"
                "open(sys.argv[1],'w').write(str(child.pid))\n"
                "time.sleep(30)\n")
            parent = subprocess.Popen(
                [sys.executable, "-c", helper, pid_file, release, exit_file])
            try:
                deadline = time.time() + 5
                pid = None
                while time.time() < deadline and pid is None:
                    try:
                        with open(pid_file, encoding="ascii") as stream:
                            pid = int(stream.read())
                    except (OSError, ValueError):
                        time.sleep(0.01)
                self.assertIsNotNone(pid, "the helper published its child pid")
                birth = workers._process_start(pid)
                self.assertIsNotNone(birth)
                workers.update_task(project, record["id"], lambda changed: changed.update(
                    state="running", pid=pid, birth=birth, started_at=time.time(),
                    to=workers._encode_control(
                        proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
                with open(release, "w", encoding="ascii"):
                    pass
                state = ""
                while time.time() < deadline:
                    observed = subprocess.run(
                        ["ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True)
                    state = observed.stdout.strip()
                    if os.path.exists(exit_file) and state.startswith("Z"):
                        break
                    time.sleep(0.01)
                self.assertTrue(state.startswith("Z"), state)

                marker = os.open(workers.live_path(project, record["id"]),
                                 os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    workers._write_live_marker(
                        marker, workers._published_marker(0, PROOF))
                finally:
                    os.close(marker)

                final = workers.status(project, record["id"])
                self.assertEqual((final["state"], final["exit_code"]),
                                 ("completed", 0))
            finally:
                parent.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    parent.wait(timeout=2)

    def test_publication_between_the_first_lock_read_and_death_is_reconciled(self):
        """The post-death lock read observes a supervisor publication that
        landed after the first lock snapshot."""
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=999_999, birth="legacy birth",
                started_at=time.time(), to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))

            def finish_between_observations(_cwd, _record, lock=None):
                marker = os.open(workers.live_path(project, record["id"]),
                                 os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    workers._write_live_marker(
                        marker, workers._published_marker(0, PROOF))
                finally:
                    os.close(marker)
                return "dead"

            with patch.object(workers, "_worker_liveness",
                              side_effect=finish_between_observations), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                final = workers.status(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

    def test_a_nonzero_exit_published_during_the_probe_beats_timeout(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None),
                                           ("published", (3, False)))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live")), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"), \
                 patch.object(workers, "_kill_group") as kill:
                first = workers.status(project, record["id"], now=1.5)
                final = workers.status(project, record["id"], now=3.0)
            kill.assert_not_called()
            self.assertEqual(first["state"], "running")
            self.assertEqual((final["state"], final["exit_code"]), ("failed", 3))

    def test_a_running_record_with_no_supervisor_cannot_trust_the_exit_mirror(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=None, started_at=1.0))
            with open(workers.exit_path(project, record["id"]), "w", encoding="ascii") as stream:
                stream.write("0\n")
            final = workers.status(project, record["id"], now=2.0)
            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_an_unrepresentable_pid_is_unknown_and_never_signalled(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=10 ** 100, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_kill_group") as kill:
                final = workers.reported_status(project, record["id"], now=3.0)
            kill.assert_not_called()
            self.assertEqual(final["state"], "running")
            self.assertEqual(final["worker_liveness"], "unknown")

    def test_an_unreadable_legacy_process_table_is_unknown_not_dead(self):
        """During a rolling upgrade a pre-lock worker still uses pid/birth.
        A live pid plus an unreadable birth is uncertainty, never permission
        to write failed or send a timeout signal.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=os.getpid(), birth="recorded birth",
                started_at=1.0))
            with patch.object(workers, "_worker_liveness", return_value="unknown"), \
                 patch.object(workers, "_kill_group") as kill:
                first = workers.status(project, record["id"], now=1.5)
                final = workers.status(project, record["id"], now=3.0)
                unknown = workers.liveness_unknown(project, final, now=3.0)

            kill.assert_not_called()
            self.assertEqual(first["state"], "running")
            self.assertEqual(final["state"], "running")
            self.assertTrue(unknown)

    def test_a_reused_pid_has_unknown_outcome_without_signalling_the_new_owner(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=os.getpid(), birth="somebody else",
                started_at=1.0))
            with patch.object(workers, "_process_snapshot",
                              return_value=("new owner", "S")), \
                 patch.object(workers, "_kill_group") as kill:
                final = workers.status(project, record["id"], now=3.0)

            kill.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_a_permission_the_class_denies_is_blocked_not_failed(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir,
                                "echo 'Bash requires approval: permission denied'; exit 2")
            record = self._settle(project, started["id"], "blocked")
            self.assertEqual(record["exit_code"], 2)

    def _child_pid(self, project, task_id):
        path = os.path.join(workers.worker_dir(project, task_id), "child.pid")
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with open(path) as f:
                    return int(f.read().strip())
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("the stub never wrote its pid")

    def test_a_worker_past_its_timeout_is_killed_and_timed_out(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir,
                                "echo $$ > \"$ANTIPHON_WORKER_DIR/child.pid\"; sleep 30",
                                timeout=1)
            child = self._child_pid(project, started["id"])
            os.kill(child, 0)
            os.killpg(started["pid"], 0)
            time.sleep(1.2)
            record = self._settle(project, started["id"], "timed_out", seconds=15)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertTrue(workers._group_gone(record["pid"]))

    def test_an_unreadable_live_lock_never_authorizes_a_timeout_signal(self):
        """Even after its deadline, an observation failure is not proof that
        this pid is the worker Antiphon owns. Status must not signal it, and
        current uncertainty becomes explicit while the compatible running
        record continues to hold its worker slot.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_worker_liveness", return_value="unknown"), \
                 patch.object(workers, "_kill_group") as kill:
                before_deadline = workers.reported_status(
                    project, record["id"], now=1.5)
                observed = workers.status(project, record["id"], now=3.0)
                reported = workers.reported_status(project, record["id"], now=3.0)
                answer = workers.result(project, record["id"])

            kill.assert_not_called()
            self.assertEqual(before_deadline["state"], "running")
            self.assertNotIn("worker_liveness", before_deadline)
            self.assertEqual(observed["state"], "running")
            self.assertIn(record["id"], [task["id"] for task in workers.running(project)])
            self.assertEqual(reported["worker_liveness"], "unknown")
            self.assertEqual(answer["worker_liveness"], "unknown")
            self.assertIn("worker slot are kept", answer["liveness_detail"])

    def test_a_held_replacement_lock_never_authorizes_a_signal_to_a_recycled_pid(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=os.getpid(), birth="somebody else",
                started_at=1.0))
            lock_fd = os.open(workers.live_path(project, record["id"]),
                              os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_EX)
            try:
                with patch.object(workers, "_process_snapshot",
                                  return_value=("new owner", "S")), \
                     patch.object(workers, "_kill_group") as kill:
                    observed = workers.reported_status(
                        project, record["id"], now=3.0, patience=0.0)
            finally:
                os.close(lock_fd)

            kill.assert_not_called()
            self.assertEqual(observed["state"], "running")
            self.assertEqual(observed["worker_liveness"], "unknown")

    def test_public_status_reports_the_same_unknown_probe_that_suppressed_timeout(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_worker_liveness",
                              side_effect=("unknown", "live")) as liveness, \
                 patch.object(workers, "_kill_group") as kill:
                reported = workers.reported_status(project, record["id"], now=3.0)
            kill.assert_not_called()
            self.assertEqual(liveness.call_count, 1)
            self.assertEqual(reported["worker_liveness"], "unknown")

    def test_unknown_after_a_signal_never_claims_that_no_signal_was_sent(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("unknown", None))), \
                 patch.object(workers, "_read_exit", return_value=None), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "unknown")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="unresolved"):
                reported = workers.reported_status(
                    project, record["id"], now=3.0, patience=0.0)

            self.assertEqual(reported["worker_liveness"], "unknown")
            self.assertIn("stop signal was attempted", reported["liveness_detail"])
            self.assertNotIn("no signal was sent", reported["liveness_detail"])

    def test_reporting_names_a_live_worker_after_an_unresolved_timeout_signal(self):
        for reporter in ("status", "result"):
            with self.subTest(reporter=reporter), \
                 tempfile.TemporaryDirectory() as project:
                record = workers.new_task(
                    project, kind="codex", task_class="read", sha256=SHA,
                    size=1, timeout=1)
                workers.update_task(
                    project, record["id"], lambda changed: changed.update(
                        state="running", pid=4242, birth="recorded birth",
                        started_at=1.0))
                with patch.object(workers, "_lock_observation",
                                  return_value=("live", None)), \
                     patch.object(workers, "_worker_liveness",
                                  side_effect=("live", "live")), \
                     patch.object(workers, "_signal_authorized",
                                  return_value=True), \
                     patch.object(workers, "_kill_group",
                                  return_value="unresolved"):
                    if reporter == "status":
                        answer = workers.reported_status(
                            project, record["id"], now=3.0, patience=0.0)
                    else:
                        answer = workers.result(project, record["id"])
                self.assertEqual(answer["state"], "running")
                self.assertEqual(answer["worker_liveness"], "unknown")
                self.assertIn("still appears live", answer["liveness_detail"])

    def test_the_unknown_diagnostic_is_a_current_observation_not_stale_state(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            record = workers.read_task(project, record["id"])
            with patch.object(workers, "_worker_liveness",
                              side_effect=("unknown", "live")), \
                 patch.object(workers, "_signal_authorized", return_value=True):
                self.assertTrue(workers.liveness_unknown(project, record, now=3.0))
                self.assertFalse(workers.liveness_unknown(project, record, now=3.0))
            observed = workers.read_task(project, record["id"])
            self.assertEqual(observed["state"], "running")

    def test_unknown_liveness_keeps_its_work_slot_and_accepts_a_late_exit(self):
        """An unobservable worker may still run. Its slot and directory remain
        held, no signal is sent without identity proof, and a late exit can
        still resolve the uncertainty.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0,
                created_at=1.0))
            with patch.object(workers, "_worker_liveness", return_value="unknown"), \
                 patch.object(workers, "_kill_group") as kill:
                final = workers.status(project, record["id"], now=3.0)
                self.assertEqual(final["state"], "running")
                self.assertTrue(workers.liveness_unknown(project, final, now=3.0))
                with self.assertRaisesRegex(workers.Refused,
                                            "identity could not be verified"):
                    workers.cancel(project, record["id"])
                workers.sweep(project, now=1.0 + workers.TASK_TTL + 1)

            kill.assert_not_called()
            self.assertTrue(os.path.isdir(directory))
            self.assertIsNotNone(workers.read_task(project, record["id"]))
            self.assertTrue(os.path.isdir(directory))

            digest = hashlib.sha256(bytes.fromhex(PROOF)).hexdigest()
            workers.update_task(
                project, record["id"],
                lambda changed: changed.update(to=workers._encode_control(
                    proof=digest, birth="recorded birth", started=1.0)))
            marker = os.open(workers.live_path(project, record["id"]),
                             os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(
                    marker, workers._published_marker(0, PROOF))
            finally:
                os.close(marker)
            with patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                resolved = workers.status(project, record["id"], now=10.0)
            self.assertEqual((resolved["version"], resolved["state"],
                              resolved["exit_code"]),
                             (workers.LEGACY_TASK_VERSION, "completed", 0))

    def test_the_current_store_is_invisible_to_the_pinned_reader(self):
        """Current task rows live in a namespace the old reader never opens."""
        import subprocess
        import types

        root = os.path.dirname(os.path.dirname(__file__))
        try:
            shallow = subprocess.check_output(
                ["git", "rev-parse", "--is-shallow-repository"], cwd=root,
                stderr=subprocess.DEVNULL, text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("aff2f9e unavailable: not a git checkout")
        if shallow == "true":
            self.skipTest("aff2f9e unavailable: shallow clone")
        try:
            source = subprocess.check_output(
                ["git", "show", "aff2f9e:lib/workers.py"], cwd=root,
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            self.fail(f"complete history lacks pinned worker reader aff2f9e: {error.stderr!r}")
        old = types.ModuleType("workers_aff2f9e")
        old.__file__ = "aff2f9e:lib/workers.py"
        exec(compile(source, old.__file__, "exec"), old.__dict__)

        with tempfile.TemporaryDirectory() as project:
            workers._ensure_legacy_epoch_fence(project)
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_worker_liveness", return_value="unknown"):
                current = workers.status(project, record["id"], now=3.0)

            published = old.read_task(project, record["id"])
            self.assertEqual((current["version"], current["state"]), (1, "running"))
            self.assertIsNone(published)
            self.assertNotIn(record["id"], [item["id"] for item in old._admitted(project)])

    def test_the_storage_epoch_fences_every_pinned_reader_task_surface(self):
        """Pinned readers see history, but every old write fails promptly."""
        import types

        root = os.path.dirname(os.path.dirname(__file__))
        try:
            source = subprocess.check_output(
                ["git", "show", "aff2f9e:lib/workers.py"], cwd=root,
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            self.skipTest(f"aff2f9e unavailable: {error.stderr!r}")
        old = types.ModuleType("workers_aff2f9e_admission")
        old.__file__ = "aff2f9e:lib/workers.py"
        exec(compile(source, old.__file__, "exec"), old.__dict__)

        with tempfile.TemporaryDirectory() as project:
            workers._ensure_legacy_epoch_fence(project)
            records = []
            for _number in range(workers.MAX_WORKERS):
                record = workers.new_task(
                    project, kind="codex", task_class="read", sha256=SHA,
                    size=1, timeout=1)
                workers.update_task(
                    project, record["id"], lambda changed: changed.update(
                        state="running", pid=os.getpid(),
                        birth="temporarily unreadable", started_at=1.0))
                records.append(record)
            legacy = os.path.join(project, ".antiphon", workers.LEGACY_TASK_STORE)
            self.assertTrue(os.path.isdir(legacy))
            self.assertEqual(stat.S_IMODE(os.stat(legacy).st_mode),
                             workers.LEGACY_FROZEN_MODE)
            with open(os.path.join(legacy, ".lock"), "rb") as stream:
                self.assertEqual(stream.read(), workers.LEGACY_FENCE_TOKEN)
            self.assertEqual(old._admitted(project), [])
            with patch.object(old, "_kill_group") as kill:
                self.assertIsNone(old.status(project, records[0]["id"], now=3.0))
                self.assertIsNone(old.result(project, records[0]["id"]))
                self.assertIsNone(old.cancel(project, records[0]["id"]))
                old.sweep(project, now=workers.TASK_TTL + 10.0)
            kill.assert_not_called()
            self.assertEqual(len(workers._admitted(project)), workers.MAX_WORKERS)
            began = time.monotonic()
            with self.assertRaises(OSError):
                old.accept(
                    project, now=3.0, kind="codex", task_class="read",
                    sha256=SHA, size=1, timeout=900)
            self.assertLess(time.monotonic() - began, 0.5,
                            "a pinned client is refused rather than wedged")
            began = time.monotonic()
            with self.assertRaises(OSError):
                old.new_task(
                    project, kind="claude", task_class="read", sha256=SHA,
                    size=1, state="handing", to="peer")
            self.assertLess(time.monotonic() - began, 0.5,
                            "a pinned lock-free hand-off cannot publish")
            self.assertEqual(
                [name for name in os.listdir(legacy) if name.endswith(".json")],
                [])

    def test_a_live_previous_epoch_worker_refuses_current_admission(self):
        import types

        root = os.path.dirname(os.path.dirname(__file__))
        try:
            source = subprocess.check_output(
                ["git", "show", "aff2f9e:lib/workers.py"], cwd=root,
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            self.skipTest(f"aff2f9e unavailable: {error.stderr!r}")
        old = types.ModuleType("workers_aff2f9e_live")
        old.__file__ = "aff2f9e:lib/workers.py"
        exec(compile(source, old.__file__, "exec"), old.__dict__)

        with tempfile.TemporaryDirectory() as project:
            legacy = old.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            with self.assertRaisesRegex(workers.Refused,
                                        "previous task protocol"):
                workers.accept(
                    project, kind="codex", task_class="read", sha256=SHA, size=1)
            self.assertEqual(old.read_task(project, legacy["id"]), legacy)
            self.assertFalse(os.path.exists(workers.tasks_dir(project)))

    def test_an_unreadable_previous_epoch_row_fails_admission_closed(self):
        with tempfile.TemporaryDirectory() as project:
            directory = os.path.join(project, ".antiphon",
                                     workers.LEGACY_TASK_STORE)
            os.makedirs(directory)
            bad_id = "2e6b14f1-1659-544a-98d4-56d6eca8fa48"
            with open(os.path.join(directory, bad_id + ".json"),
                      "w", encoding="ascii") as stream:
                stream.write("{")
            with self.assertRaisesRegex(workers.Refused, "unreadable task row"):
                workers.accept(
                    project, kind="codex", task_class="read", sha256=SHA, size=1)
            self.assertFalse(os.path.exists(workers.tasks_dir(project)))

    def test_epoch_fence_preserves_every_legal_legacy_uuid_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as project:
            directory = workers._sound_dir(
                os.path.join(project, ".antiphon", workers.LEGACY_TASK_STORE),
                create=True)
            task_ids = tuple(
                f"00000000-0000-0000-0000-{number:012d}"
                for number in range(1, workers.MAX_WORKERS + 1))
            snapshots = {}
            for task_id in task_ids:
                durable = self._legacy_record(task_id, kind="claude")
                workers._write_record_in(directory, durable)
                path = os.path.join(directory, task_id + ".json")
                with open(path, "rb") as stream:
                    snapshots[task_id] = stream.read()

            workers._ensure_legacy_epoch_fence(project)

            self.assertEqual(
                sorted(name[:-5] for name in os.listdir(directory)
                       if name.endswith(".json")), sorted(task_ids))
            for task_id, before in snapshots.items():
                with open(os.path.join(directory, task_id + ".json"), "rb") as stream:
                    self.assertEqual(stream.read(), before)

    def test_failed_epoch_fence_restores_the_writable_mode_before_commit(self):
        with tempfile.TemporaryDirectory() as project:
            with patch.object(workers, "_write_legacy_fence_token",
                              side_effect=OSError("injected token failure")):
                with self.assertRaisesRegex(OSError, "token failure"):
                    workers._ensure_legacy_epoch_fence(project)

            directory = os.path.join(project, ".antiphon",
                                     workers.LEGACY_TASK_STORE)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_WRITABLE_MODE)
            with open(os.path.join(directory, ".lock"), "rb") as stream:
                self.assertNotEqual(stream.read(), workers.LEGACY_FENCE_TOKEN)

    def test_a_host_that_does_not_enforce_the_epoch_fence_is_rejected(self):
        """The permission probe is an admission gate, not documentation."""
        with tempfile.TemporaryDirectory() as project, \
             patch.object(workers, "_legacy_store_rejects_writes",
                          return_value=False):
            with self.assertRaisesRegex(
                    OSError, "host does not enforce.*task store fence"):
                workers._ensure_legacy_epoch_fence(project)

            directory = os.path.join(
                project, ".antiphon", workers.LEGACY_TASK_STORE)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_WRITABLE_MODE)
            with open(os.path.join(directory, ".lock"), "rb") as stream:
                self.assertNotEqual(stream.read(), workers.LEGACY_FENCE_TOKEN)
            self.assertFalse(os.path.exists(workers.tasks_dir(project)))

    def test_crash_after_fchmod_before_commit_is_recovered(self):
        with tempfile.TemporaryDirectory() as project:
            with patch.object(workers, "_write_legacy_fence_token",
                              side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    workers._ensure_legacy_epoch_fence(project)

            directory = os.path.join(project, ".antiphon",
                                     workers.LEGACY_TASK_STORE)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_FROZEN_MODE)
            workers._ensure_legacy_epoch_fence(project)
            with open(os.path.join(directory, ".lock"), "rb") as stream:
                self.assertEqual(stream.read(), workers.LEGACY_FENCE_TOKEN)

    def test_committed_epoch_fence_is_never_reopened_by_revalidation_failure(self):
        with tempfile.TemporaryDirectory() as project:
            workers._ensure_legacy_epoch_fence(project)
            directory = os.path.join(project, ".antiphon",
                                     workers.LEGACY_TASK_STORE)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_FROZEN_MODE)

            with patch.object(workers, "_check_legacy_quiescent",
                              side_effect=OSError("inspection unavailable")):
                with self.assertRaisesRegex(OSError, "inspection unavailable"):
                    workers._ensure_legacy_epoch_fence(project)

            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_FROZEN_MODE)
            with open(os.path.join(directory, ".lock"), "rb") as stream:
                self.assertEqual(stream.read(), workers.LEGACY_FENCE_TOKEN)

    def test_transitional_read_only_mode_is_not_a_committed_fence(self):
        with tempfile.TemporaryDirectory() as project:
            frozen_before_commit = threading.Event()
            release_commit = threading.Event()
            calls = 0
            original = workers._check_legacy_quiescent
            outcomes = []

            def pause_post_freeze(directory):
                nonlocal calls
                calls += 1
                if calls == 2:
                    frozen_before_commit.set()
                    release_commit.wait(5)
                return original(directory)

            def fence():
                try:
                    workers._ensure_legacy_epoch_fence(project)
                    outcomes.append("ok")
                except Exception as error:  # captured for the test thread
                    outcomes.append(error)

            with patch.object(workers, "_check_legacy_quiescent",
                              side_effect=pause_post_freeze):
                first = threading.Thread(target=fence)
                first.start()
                self.assertTrue(frozen_before_commit.wait(5))
                directory = os.path.join(project, ".antiphon",
                                         workers.LEGACY_TASK_STORE)
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                                 workers.LEGACY_FROZEN_MODE)
                with open(os.path.join(directory, ".lock"), "rb") as stream:
                    self.assertNotEqual(stream.read(), workers.LEGACY_FENCE_TOKEN)
                second = threading.Thread(target=fence)
                second.start()
                time.sleep(0.05)
                self.assertTrue(second.is_alive(),
                                "mode 0500 alone was not treated as committed")
                release_commit.set()
                first.join(5)
                second.join(5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(outcomes, ["ok", "ok"])

    def test_pinned_writer_with_a_preopened_lock_cannot_publish_after_freeze(self):
        import types

        root = os.path.dirname(os.path.dirname(__file__))
        try:
            source = subprocess.check_output(
                ["git", "show", "aff2f9e:lib/workers.py"], cwd=root,
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            self.skipTest(f"aff2f9e unavailable: {error.stderr!r}")
        old = types.ModuleType("workers_aff2f9e_preopened")
        old.__file__ = "aff2f9e:lib/workers.py"
        exec(compile(source, old.__file__, "exec"), old.__dict__)

        with tempfile.TemporaryDirectory() as project:
            directory = workers._sound_dir(
                os.path.join(project, ".antiphon", workers.LEGACY_TASK_STORE),
                create=True)
            lock_path = os.path.join(directory, ".lock")
            old_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._ensure_legacy_epoch_fence(project)
                workers.fcntl.flock(old_fd, workers.fcntl.LOCK_EX)
                old.admit(project)
                began = time.monotonic()
                with self.assertRaises(OSError):
                    old.new_task(
                        project, kind="codex", task_class="read",
                        sha256=SHA, size=1)
                self.assertLess(time.monotonic() - began, 0.5)
            finally:
                with contextlib.suppress(OSError):
                    workers.fcntl.flock(old_fd, workers.fcntl.LOCK_UN)
                os.close(old_fd)
            self.assertEqual(
                [name for name in os.listdir(directory) if name.endswith(".json")],
                [])

    def test_epoch_transition_refuses_a_terminal_legacy_row_with_live_group(self):
        with tempfile.TemporaryDirectory() as project:
            process = subprocess.Popen(
                ["/bin/sh", "-c", "sleep 30"], start_new_session=True)
            try:
                birth = workers._process_start(process.pid)
                self.assertIsNotNone(birth)
                directory = workers._sound_dir(
                    os.path.join(project, ".antiphon",
                                 workers.LEGACY_TASK_STORE), create=True)
                task_id = str(workers.uuid.uuid4())
                durable = self._legacy_record(
                    task_id, pid=process.pid, birth=birth)
                workers._write_record_in(directory, durable)

                with self.assertRaisesRegex(workers.Refused,
                                            "unresolved process liveness"):
                    workers._ensure_legacy_epoch_fence(project)

                self.assertEqual(workers.legacy_task(project, task_id), durable)
                self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                                 workers.LEGACY_WRITABLE_MODE)
            finally:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, workers.signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)

    def test_epoch_transition_refuses_unmeasurable_terminal_liveness(self):
        with tempfile.TemporaryDirectory() as project:
            directory = workers._sound_dir(
                os.path.join(project, ".antiphon", workers.LEGACY_TASK_STORE),
                create=True)
            task_id = str(workers.uuid.uuid4())
            durable = self._legacy_record(
                task_id, pid=4242, birth="recorded birth")
            workers._write_record_in(directory, durable)

            with patch.object(workers, "_process_liveness",
                              return_value="unknown"), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                with self.assertRaisesRegex(workers.Refused,
                                            "unresolved process liveness"):
                    workers._ensure_legacy_epoch_fence(project)

            self.assertEqual(workers.legacy_task(project, task_id), durable)

    def test_uncollected_legacy_write_result_blocks_the_read_only_transition(self):
        with tempfile.TemporaryDirectory() as project:
            directory = workers._sound_dir(
                os.path.join(project, ".antiphon", workers.LEGACY_TASK_STORE),
                create=True)
            durable = self._legacy_record(task_class="write", collected_at=None)
            workers._write_record_in(directory, durable)

            with self.assertRaisesRegex(workers.Refused,
                                        "uncollected write result"):
                workers._ensure_legacy_epoch_fence(project)

            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_WRITABLE_MODE)

    def test_crash_stale_legacy_handing_does_not_block_current_workers(self):
        with tempfile.TemporaryDirectory() as project:
            directory = workers._sound_dir(
                os.path.join(project, ".antiphon", workers.LEGACY_TASK_STORE),
                create=True)
            durable = self._legacy_record(
                version=workers.TASK_VERSION, state="handing", to="peer",
                exit_code=None, started_at=None, finished_at=None,
                created_at=(time.time()
                            - workers.LEGACY_HANDOFF_PATIENCE - 1))
            workers._write_record_in(directory, durable)

            current = workers.accept(
                project, kind="codex", task_class="read", sha256=SHA, size=1)

            self.assertEqual(current["state"], "accepted")
            self.assertEqual(workers.legacy_task(project, durable["id"]), durable)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_FROZEN_MODE)

    def test_a_fresh_lock_free_legacy_handoff_can_finish_before_the_fence(self):
        import types

        root = os.path.dirname(os.path.dirname(__file__))
        try:
            source = subprocess.check_output(
                ["git", "show", "aff2f9e:lib/workers.py"], cwd=root,
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            self.skipTest(f"aff2f9e unavailable: {error.stderr!r}")
        old = types.ModuleType("workers_aff2f9e_handoff")
        old.__file__ = "aff2f9e:lib/workers.py"
        exec(compile(source, old.__file__, "exec"), old.__dict__)

        with tempfile.TemporaryDirectory() as project:
            directory = workers._sound_dir(
                os.path.join(project, ".antiphon", workers.LEGACY_TASK_STORE),
                create=True)
            original = workers._check_legacy_quiescent
            prepared = []
            checks = 0

            def inject_after_precheck(legacy_directory):
                nonlocal checks
                original(legacy_directory)
                checks += 1
                if checks == 1:
                    prepared.append(old.new_task(
                        project, kind="claude", task_class="read",
                        sha256=SHA, size=1, state="handing", to="peer"))

            with patch.object(workers, "_check_legacy_quiescent",
                              side_effect=inject_after_precheck):
                with self.assertRaisesRegex(
                        workers.Refused, "previous.*hand-off.*finish"):
                    workers.accept(
                        project, kind="codex", task_class="read",
                        sha256=SHA, size=1)

            self.assertEqual(len(prepared), 1)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_WRITABLE_MODE)
            with open(os.path.join(directory, ".lock"), "rb") as stream:
                self.assertNotEqual(stream.read(), workers.LEGACY_FENCE_TOKEN)
            self.assertTrue(old.update_task(
                project, prepared[0]["id"],
                lambda changed: changed.update(state="handed")))

            admitted = workers.accept(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1)
            self.assertEqual(admitted["state"], "accepted")
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode),
                             workers.LEGACY_FROZEN_MODE)

    def test_a_natural_exit_before_the_timeout_signal_keeps_its_outcome(self):
        """The worker may finish before the timeout path sends any signal.
        Its published exit is then the stronger fact.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))

            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None),
                                           ("published", (0, False)))), \
                 patch.object(workers, "_worker_liveness", return_value="live"), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group") as kill:
                final = workers.status(project, record["id"], now=3.0, patience=0.0)

            kill.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

    def test_publication_at_final_stop_check_still_requires_group_death(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None),
                                           ("published", (0, False)))), \
                 patch.object(workers, "_worker_liveness", return_value="live"), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="live") as group, \
                 patch.object(workers, "_kill_group") as kill:
                observed = workers.status(
                    project, record["id"], now=3.0, patience=0.0)

            group.assert_called_once_with(4242)
            kill.assert_not_called()
            self.assertEqual(observed["state"], "running")
            self.assertEqual(len(workers._admitted(project)), 1)

    def test_exit_zero_after_a_timeout_signal_does_not_override_timed_out(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("published", (0, True)))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "dead")), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="stopped"):
                final = workers.status(project, record["id"], now=3.0, patience=0.0)

            self.assertEqual((final["state"], final["exit_code"]),
                             ("timed_out", None))

    def test_a_timeout_signal_that_does_not_stop_the_worker_stays_running(self):
        """A failed signal attempt is not a timeout outcome.  The held lock
        remains positive proof that the worker still owns its process.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_lock_observation",
                              return_value=("live", None)), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group",
                              return_value="unresolved") as kill:
                observed = workers.status(project, record["id"], now=3.0, patience=0.0)

            kill.assert_called_once()
            self.assertEqual(observed["state"], "running")

    def test_kill_group_distinguishes_no_signal_from_an_unproved_stop(self):
        with patch.object(workers.os, "killpg", side_effect=PermissionError):
            self.assertEqual(workers._kill_group(4242, patience=0.0), "not_sent")
        with patch.object(workers.os, "killpg",
                          side_effect=(None, PermissionError)), \
             patch.object(workers, "_group_gone", return_value=False):
            self.assertEqual(workers._kill_group(4242, patience=0.0), "unresolved")

    def test_kill_group_revalidates_identity_before_sigkill(self):
        decisions = iter((True, False))
        with patch.object(workers.os, "killpg") as kill, \
             patch.object(workers, "_group_gone", return_value=False):
            outcome = workers._kill_group(
                4242, patience=0.0,
                revalidate=lambda: next(decisions))
        self.assertEqual(outcome, "unresolved")
        self.assertEqual(kill.call_args_list,
                         [call(4242, workers.signal.SIGTERM)])

    def test_absent_wrapper_with_zombie_only_group_is_dead(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_lock_observation",
                              return_value=("dead", None)), \
                 patch.object(workers, "_process_identity", return_value="absent"), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead") as detailed, \
                 patch.object(workers, "_group_liveness",
                              return_value="live") as coarse:
                final = workers.status(project, record["id"])
            detailed.assert_called_once_with(4242)
            coarse.assert_not_called()
            self.assertEqual(final["state"], "outcome_unknown")

    def test_a_natural_exit_after_an_unsent_timeout_signal_keeps_its_code(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("published", (3, False)))), \
                 patch.object(workers, "_process_identity", return_value="live"), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "dead")), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"), \
                 patch.object(workers, "_kill_group", return_value="not_sent"):
                final = workers.status(project, record["id"], now=3.0, patience=0.0)

            self.assertEqual((final["state"], final["exit_code"]), ("failed", 3))

    def test_a_dead_worker_without_an_exit_is_not_timed_out_when_no_signal_was_sent(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("dead", None))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "dead")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="absent"):
                final = workers.status(project, record["id"], now=3.0, patience=0.0)

            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_a_recycled_pid_is_not_the_worker(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA,
                                      size=1)
            workers.update_task(project, record["id"], lambda c: c.update(
                state="running", pid=os.getpid(), birth="not this process",
                started_at=time.time()))
            self.assertFalse(workers._alive(workers.read_task(project, record["id"])),
                             "a live pid with another start time is somebody else")
            workers.update_task(project, record["id"], lambda c: c.update(
                birth=workers._process_start(os.getpid())))
            self.assertTrue(workers._alive(workers.read_task(project, record["id"])))
            self.assertEqual(workers.status(project, record["id"])["state"], "running")
            workers.update_task(project, record["id"], lambda c: c.update(
                state="cancelled", finished_at=2.0))

    def test_cancel_kills_the_worker_and_removes_its_directory(self):
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            started = self._run(project, bin_dir,
                                "echo $$ > \"$ANTIPHON_WORKER_DIR/child.pid\"; sleep 30",
                                task_class="write")
            directory = workers.worker_dir(project, started["id"])
            self.assertTrue(os.path.isdir(directory))
            child = self._child_pid(project, started["id"])
            os.kill(child, 0)
            record = workers.cancel(project, started["id"])
            self.assertEqual(record["state"], "cancelled")
            self.assertFalse(os.path.exists(directory))
            listed = subprocess.run(["git", "-C", project, "worktree", "list", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertNotIn(started["id"], listed, "the worktree is gone from git too")
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertTrue(workers._group_gone(record["pid"]))
            self.assertEqual(workers.cancel(project, started["id"])["state"], "cancelled",
                             "cancelling twice is one cancel")

    def test_confirmed_group_stop_needs_no_second_process_snapshot(self):
        """`_kill_group` has already observed process-group absence. A
        transient `ps` failure after that proof must not turn cancel into a
        retryable false negative.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            record = workers.read_task(project, record["id"])

            with patch.object(workers, "status", return_value=record), \
                 patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("dead", None))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", AssertionError(
                                  "a proved stop was sampled again"))), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="stopped"):
                final = workers.cancel(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]),
                             ("cancelled", None))

    def test_cancel_claim_wins_an_interleaved_published_signal_exit(self):
        """Once cancel is durably claimed, another status call must interpret
        the wrapper's signal exit as that action instead of writing `failed`
        first and making the terminal result unrecoverable.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=900)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            interleaved = []

            def publish_signal_exit(_pid, _patience=workers.KILL_PATIENCE,
                                    revalidate=None):
                interleaved.append(workers._finish_exit(
                    project, workers.read_task(project, record["id"]),
                    (128 + workers.signal.SIGTERM, True), time.time()))
                return "stopped"

            with patch.object(workers, "_lock_observation",
                              return_value=("live", None)), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "dead")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group",
                              side_effect=publish_signal_exit):
                final = workers.cancel(project, record["id"])

            self.assertEqual(interleaved[0]["state"], "cancelled")
            self.assertEqual(final["state"], "cancelled")

    def test_cancel_cleans_up_when_terminal_state_wins_during_stop_claim(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            running = workers.read_task(project, record["id"])

            def terminal_wins(_cwd, _record, _intent, now=None):
                return (workers._finish(
                    project, record["id"], "completed", 0, now=now,
                    stop_resolution="natural"), None)

            with patch.object(workers, "status", return_value=running), \
                 patch.object(workers, "_lock_observation",
                              return_value=("live", None)), \
                 patch.object(workers, "_worker_liveness", return_value="live"), \
                 patch.object(workers, "_action_ready",
                              return_value=("ready", None)), \
                 patch.object(workers, "_claim_stop",
                              side_effect=terminal_wins), \
                 patch.object(workers, "_stop_group") as stop:
                final = workers.cancel(project, record["id"])

            stop.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))
            self.assertFalse(os.path.exists(directory))

    def test_cancel_keeps_work_when_terminal_record_commit_fails(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=900)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            record = workers.read_task(project, record["id"])
            claimed = dict(record)
            claimed["to"] = workers._encode_control(stop="cancelled")
            with patch.object(workers, "status", return_value=record), \
                 patch.object(workers, "_lock_observation",
                              return_value=("live", None)), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "dead")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_claim_stop",
                              return_value=(claimed, "cancelled")), \
                 patch.object(workers, "_kill_group", return_value="stopped"), \
                 patch.object(workers, "update_task", return_value=False):
                with self.assertRaisesRegex(workers.Refused,
                                            "terminal state could not be committed"):
                    workers.cancel(project, record["id"])
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "running")
            self.assertTrue(os.path.isdir(directory))

    def test_cancel_refuses_a_task_whose_start_is_still_in_progress(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            with self.assertRaisesRegex(workers.Refused, "still starting"):
                workers.cancel(project, record["id"])
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "accepted")
            self.assertTrue(os.path.isdir(directory))

    def test_cancel_never_signals_a_worker_whose_live_lock_is_unreadable(self):
        """A status probe can fail without proving the wrapper dead.  Cancel
        must refuse that observation instead of sending a signal to an
        identity it cannot currently prove.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_worker_liveness",
                              side_effect=("live", "unknown")), \
                 patch.object(workers, "_kill_group") as kill:
                with self.assertRaisesRegex(workers.Refused,
                                            "identity could not be verified"):
                    workers.cancel(project, record["id"])
            kill.assert_not_called()
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "running")

    def test_a_natural_exit_before_the_cancel_signal_keeps_its_outcome(self):
        """Cancel preserves a final exit when no signal was actually sent.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))

            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("published", (0, False)))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live")), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group") as kill:
                final = workers.cancel(project, record["id"])

            kill.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

    def test_cancel_keeps_a_late_publication_until_group_death(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("published", (0, False)))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="live") as group, \
                 patch.object(workers, "_kill_group") as kill:
                with self.assertRaisesRegex(workers.Refused,
                                            "process group has not been proved gone"):
                    workers.cancel(project, record["id"])

            group.assert_called_once_with(4242)
            kill.assert_not_called()
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "running")
            self.assertTrue(os.path.isdir(directory))

    def test_exit_zero_after_a_cancel_signal_does_not_override_cancelled(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("live", None),
                                           ("published", (0, True)))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "dead")), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="stopped"):
                final = workers.cancel(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]),
                             ("cancelled", None))

    def test_cancel_reads_exit_only_after_post_stop_publication(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("live", None),
                                           ("published", (3, False)))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "dead")), \
                 patch.object(workers, "_group_process_liveness",
                              return_value="dead"), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="not_sent"):
                final = workers.cancel(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]), ("failed", 3))

    def test_known_stop_action_refines_a_concurrent_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(stop="cancelled")))

            unknown = workers._finish(
                project, record["id"], "outcome_unknown", now=2.0,
                stop_resolution="unknown")
            self.assertEqual(unknown["state"], "outcome_unknown")
            self.assertEqual(workers._stop_intent(unknown), "cancelled")

            refined = workers._finish(
                project, record["id"], "cancelled", now=3.0,
                stop_resolution="action")
            self.assertEqual((refined["state"], refined["exit_code"],
                              refined["finished_at"], refined["to"]),
                             ("cancelled", None, 3.0, None))

    def test_cancel_never_returns_internal_control_from_an_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(
                    proof="a" * 64, birth="recorded birth", started=1.0,
                    stop="cancelled")))
            workers._finish(
                project, record["id"], "outcome_unknown", None, now=2.0,
                stop_resolution="unknown")
            self.assertTrue(workers.read_task(project, record["id"])["to"].startswith(
                workers.LOCAL_CONTROL_PREFIX))

            answer = workers.cancel(project, record["id"])

            self.assertEqual(answer["state"], "outcome_unknown")
            self.assertIsNone(answer["to"])

    def test_authenticated_natural_exit_refines_an_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
            workers._finish(
                project, record["id"], "outcome_unknown", now=2.0,
                stop_resolution="unknown")

            refined = workers._finish_exit(
                project, workers.read_task(project, record["id"]),
                (0, False), now=3.0)

            self.assertEqual((refined["state"], refined["exit_code"],
                              refined["finished_at"]),
                             ("completed", 0, 3.0))

    def test_status_refines_unknown_when_authenticated_marker_becomes_readable(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
            marker = os.open(workers.live_path(project, record["id"]),
                             os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(
                    marker, workers._published_marker(0, PROOF))
            finally:
                os.close(marker)

            with patch.object(workers, "_lock_observation",
                              side_effect=(("dead", None), ("dead", None))), \
                 patch.object(workers, "_worker_liveness", return_value="dead"):
                first = workers.status(project, record["id"], now=2.0)
            self.assertEqual(first["state"], "outcome_unknown")

            with patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                refined = workers.status(project, record["id"], now=3.0)

            self.assertEqual((refined["state"], refined["exit_code"],
                              refined["finished_at"]),
                             ("completed", 0, 3.0))
            self.assertIsNone(refined["collected_at"])

    def test_status_refines_unknown_from_a_late_authenticated_stop_marker(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest(),
                    stop="cancelled")))
            unknown = workers._finish(
                project, record["id"], "outcome_unknown", now=2.0,
                stop_resolution="unknown")
            marker = os.open(workers.live_path(project, record["id"]),
                             os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(
                    marker,
                    workers._published_marker(
                        128 + signal.SIGTERM, PROOF, stopped=True))
            finally:
                os.close(marker)

            with patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                refined = workers.status(project, unknown["id"], now=3.0)

            self.assertEqual((refined["state"], refined["exit_code"],
                              refined["finished_at"]),
                             ("cancelled", None, 3.0))
            self.assertIsNone(refined["to"])

    def test_reading_an_unknown_outcome_does_not_collect_future_evidence(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
            workers._finish(
                project, record["id"], "outcome_unknown", now=2.0,
                stop_resolution="unknown")

            answer = workers.result(project, record["id"])

            self.assertEqual(answer["state"], "outcome_unknown")
            self.assertIsNone(
                workers.read_task(project, record["id"])["collected_at"])
            refined = workers._finish(
                project, record["id"], "completed", 0, now=3.0,
                stop_resolution="natural")
            self.assertEqual(refined["state"], "completed")
            self.assertIsNone(refined["collected_at"])
            workers.sweep(project, 3.0)
            self.assertTrue(os.path.isdir(directory))

    def test_cancel_keeps_work_until_terminal_rename_has_a_durable_ack(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            syncs = 0
            original_sync = workers._fsync_directory

            def fail_terminal_ack(path):
                nonlocal syncs
                if os.path.realpath(path) == os.path.realpath(
                        workers.tasks_dir(project)):
                    syncs += 1
                    if syncs in (2, 3):
                        raise OSError("injected directory fsync failure")
                return original_sync(path)

            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("live", None), ("dead", None))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "dead")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="stopped"), \
                 patch.object(workers, "_fsync_directory",
                              side_effect=fail_terminal_ack):
                with self.assertRaisesRegex(workers.Refused,
                                            "durably acknowledged"):
                    workers.cancel(project, record["id"])

            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "cancelled")
            self.assertTrue(os.path.isdir(directory))
            workers.cancel(project, record["id"])
            self.assertFalse(os.path.exists(directory))

    def test_unacknowledged_stop_intent_never_authorizes_a_signal(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(stop="timed_out")))
            with patch.object(workers, "_lock_observation",
                              return_value=("live", None)), \
                 patch.object(workers, "_worker_liveness", return_value="live"), \
                 patch.object(workers, "_sync_task_store", return_value=False), \
                 patch.object(workers, "_kill_group") as kill:
                observed = workers.status(project, record["id"], now=3.0)

            kill.assert_not_called()
            self.assertEqual(observed["state"], "running")

    def test_cancel_does_not_reuse_an_unacknowledged_visible_stop_intent(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=1.0,
                to=workers._encode_control(stop="cancelled")))
            with patch.object(workers, "_lock_observation",
                              return_value=("live", None)), \
                 patch.object(workers, "_worker_liveness", return_value="live"), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_sync_task_store", return_value=False), \
                 patch.object(workers, "_kill_group") as kill:
                with self.assertRaisesRegex(
                        workers.Refused, "stop intent could not be recorded"):
                    workers.cancel(project, record["id"])

            kill.assert_not_called()
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "running")

    def test_a_dead_worker_without_an_exit_is_not_cancelled_when_no_signal_was_sent(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_lock_observation",
                              side_effect=(("live", None), ("live", None),
                                           ("live", None), ("dead", None))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "dead")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="not_sent"):
                final = workers.cancel(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]),
                             ("outcome_unknown", None))

    def test_cancel_keeps_work_when_the_worker_survives_its_signal(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory, exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                started_at=time.time()))
            with patch.object(workers, "_lock_observation",
                              return_value=("live", None)), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "live")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="unresolved"):
                with self.assertRaisesRegex(workers.Refused, "still appears live"):
                    workers.cancel(project, record["id"])

            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "running")
            self.assertTrue(os.path.isdir(directory))

    def test_result_waits_boundedly_and_carries_the_evidence_of_a_write_task(self):
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            started = self._run(project, bin_dir,
                                "sleep 0.5; echo hello > made.txt; "
                                "echo 'suite: 3 passed' > \"$ANTIPHON_WORKER_TESTS\"; exit 0",
                                task_class="write")
            early = workers.result(project, started["id"], wait=0)
            self.assertEqual(early["state"], "running")
            self.assertNotIn("diff", early)
            final = workers.result(project, started["id"], wait=10)
            self.assertEqual(final["state"], "completed")
            self.assertIn("+hello", final["diff"])
            self.assertIn("made.txt", final["diff"])
            self.assertEqual(final["tests"], "suite: 3 passed\n")
            # Review 2026-09-03: the summary's path was beside the worktree,
            # outside the root `codex exec -s workspace-write` binds a worker
            # to. It lives inside, under a directory git ignores there.
            work = workers.work_dir(project, started["id"])
            self.assertEqual(workers.tests_path(project, started["id"]),
                             os.path.join(work, ".antiphon", "tests.txt"))
            with open(os.path.join(work, ".antiphon", ".gitignore")) as f:
                self.assertEqual(f.read(), "*\n")
            self.assertNotIn(".antiphon", final["diff"], "the summary is not in the diff")
            self.assertEqual(final["log_path"], workers.log_path(project, started["id"]))
            self.assertEqual(final["worker"]["kind"], "codex")
            self.assertEqual(final["worker"]["name"], f"worker-{started['id'][:8]}")
            self.assertIsNotNone(workers.read_task(project, started["id"])["collected_at"])
            self.assertLessEqual(workers.MAX_WAIT, 300)

    def test_the_diff_is_against_the_base_whatever_the_worker_did_with_the_index(self):
        """Review 2026-09-03, critical: `git diff` against the index showed
        nothing for staged or committed work and showed the bridge's own
        control files for unstaged work."""
        import subprocess
        for body in ("echo one > staged.txt; git add staged.txt",
                     "echo two > committed.txt; git add committed.txt; "
                     "git -c user.email=w@b -c user.name=w commit -q -m work",
                     "echo three > loose.txt"):
            with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
                subprocess.run(["git", "init", "-q", project], check=True)
                subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                                "commit", "-q", "--allow-empty", "-m", "root"], check=True)
                started = self._run(project, bin_dir, body + "; exit 0", task_class="write")
                final = workers.result(project, started["id"], wait=10)
                self.assertEqual(final["state"], "completed", final)
                self.assertRegex(final["diff"], r"\+(one|two|three)", body)
                for control in ("exit", "log", "tests.txt", "child.pid"):
                    self.assertNotRegex(final["diff"], rf"\+\+\+ b/{control}$", control)

    def test_the_collected_diff_preserves_non_utf8_repository_bytes(self):
        with tempfile.TemporaryDirectory() as project:
            subprocess.run(["git", "init", "-q", project], check=True)
            tracked = os.path.join(project, "tracked")
            with open(tracked, "wb") as stream:
                stream.write(b"before\n")
            subprocess.run(["git", "-C", project, "add", "tracked"], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "-m", "root"],
                check=True)
            base = subprocess.run(
                ["git", "-C", project, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip()
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            work = workers.work_dir(project, record["id"])
            subprocess.run([
                "git", "-C", project, "worktree", "add", "--detach", "-q",
                work, base], check=True)
            try:
                with open(os.path.join(work, "tracked"), "wb") as stream:
                    stream.write(b"after-\xff-byte\n")
                expected = subprocess.run(
                    ["git", "-C", work, "diff", "--no-color", base],
                    capture_output=True, check=True).stdout
                observed = workers._worktree_diff(
                    project, dict(record, base=base))

                self.assertIn(b"\xff", expected)
                self.assertEqual(observed, expected)
            finally:
                subprocess.run([
                    "git", "-C", project, "worktree", "remove", "--force",
                    work], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_a_repo_that_tracks_a_file_named_exit_cannot_finish_the_worker(self):
        """Review 2026-09-03, critical: with the exit file inside the worktree
        a tracked `exit` containing `0` made a running worker `completed`."""
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            for name, content in (("exit", "0\n"), ("log", "old\n"), ("tests.txt", "fake\n")):
                with open(os.path.join(project, name), "w") as f:
                    f.write(content)
            subprocess.run(["git", "-C", project, "add", "-A"], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "-m", "root"], check=True)
            started = self._run(project, bin_dir,
                                "echo $$ > \"$ANTIPHON_WORKER_DIR/child.pid\"; sleep 30",
                                task_class="write")
            self._child_pid(project, started["id"])
            self.assertEqual(workers.status(project, started["id"])["state"], "running")
            early = workers.result(project, started["id"], wait=0)
            self.assertNotIn("tests", early)
            workers.cancel(project, started["id"])

    def test_no_evidence_no_collection(self):
        """Review 2026-09-03: a completed write task whose diff could not be
        produced was marked collected, and the sweep then deleted the work."""
        import subprocess
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            started = self._run(project, bin_dir, "echo x > made.txt; exit 0", task_class="write")
            with patch.object(workers, "_worktree_diff", return_value=None):
                final = workers.result(project, started["id"], wait=10)
            self.assertEqual(final["state"], "completed")
            self.assertNotIn("diff", final)
            self.assertIsNone(workers.read_task(project, started["id"])["collected_at"])
            workers.sweep(project, time.time())
            self.assertTrue(os.path.isdir(workers.work_dir(project, started["id"])),
                            "uncollected work is kept")
            final = workers.result(project, started["id"], wait=0)
            self.assertIn("+x", final["diff"])
            self.assertIsNotNone(workers.read_task(project, started["id"])["collected_at"])

    def _rooted(self, project, *paths):
        """A checkout with one commit carrying `paths` (a path may be a
        symlink or a file); the project's own `.antiphon` is then a real
        directory, as the bridge's store would be."""
        import subprocess
        subprocess.run(["git", "init", "-q", project], check=True)
        subprocess.run(["git", "-C", project, "add", "-A"], check=True)
        subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                        "commit", "-q", "--allow-empty", "-m", "root"], check=True)
        store = os.path.join(project, ".antiphon")
        if os.path.lexists(store) and not (os.path.isdir(store) and not os.path.islink(store)):
            os.unlink(store)
        os.makedirs(store, exist_ok=True)

    def test_a_checkout_carrying_the_store_as_a_link_or_a_file_is_refused_cleanly(self):
        """Round 2, 2026-09-03: a `.antiphon` the checkout carries as a link
        sent the worker's file through it to a directory outside its
        worktree, and one it carries as a file raised out of `start` with
        the record, the directory and the worktree entry all left behind.
        Both are refusals, and a refusal leaves nothing."""
        import subprocess
        for shape in ("link", "file"):
            with tempfile.TemporaryDirectory() as project, \
                 tempfile.TemporaryDirectory() as bin_dir, \
                 tempfile.TemporaryDirectory() as elsewhere:
                store = os.path.join(project, ".antiphon")
                if shape == "link":
                    os.symlink(elsewhere, store)
                else:
                    with open(store, "w") as f:
                        f.write("not a directory\n")
                self._rooted(project)
                self._stub(bin_dir, "codex", "exit 0")
                record = workers.new_task(project, kind="codex", task_class="write",
                                          sha256=SHA, size=5)
                with self.assertRaises(workers.Refused, msg=shape) as refused:
                    workers.start(project, record, "do it", env=self._env(bin_dir))
                self.assertIn("not delegated", str(refused.exception))
                self.assertEqual(workers.tasks(project), [], "a refusal leaves no record")
                self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])),
                                 "nor a directory")
                listed = subprocess.run(["git", "-C", project, "worktree", "list"],
                                        capture_output=True, text=True).stdout
                self.assertNotIn(record["id"], listed, "nor a worktree entry")
                self.assertEqual(os.listdir(elsewhere), [], "nothing written through the link")

    def test_the_summary_stays_out_of_the_diff_whatever_gitignore_the_checkout_tracks(self):
        """Round 2, 2026-09-03: a tracked `.antiphon/.gitignore` of the
        checkout's own is not overwritten, so it did not ignore the test
        summary and the diff carried it. The store is excluded by pathspec."""
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            os.makedirs(os.path.join(project, ".antiphon"))
            with open(os.path.join(project, ".antiphon", ".gitignore"), "w") as f:
                f.write("cursor.json\n")
            self._rooted(project)
            started = self._run(project, bin_dir,
                                "echo hello > made.txt; "
                                "echo 'suite: 1 passed' > \"$ANTIPHON_WORKER_TESTS\"; exit 0",
                                task_class="write")
            final = workers.result(project, started["id"], wait=10)
            self.assertEqual(final["state"], "completed", final)
            self.assertIn("made.txt", final["diff"])
            self.assertEqual(final["tests"], "suite: 1 passed\n")
            self.assertNotIn("tests.txt", final["diff"], "the summary is evidence, not a change")
            self.assertNotIn(".antiphon", final["diff"])

    def test_a_tracked_file_under_the_store_the_worker_edits_is_in_the_diff(self):
        """Round 3, 2026-09-03: the store's pathspec was on the diff as well
        as on the intent-to-add, so a worker's edit to a file the checkout
        tracks under `.antiphon/` — exactly the pre-0.5.0 install setup now
        warns about — vanished from the evidence. The summary still stays
        out: it is never intent-added."""
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            os.makedirs(os.path.join(project, ".antiphon"))
            with open(os.path.join(project, ".antiphon", "cursor.json"), "w") as f:
                f.write("old\n")
            self._rooted(project)
            started = self._run(project, bin_dir,
                                "echo edited > .antiphon/cursor.json; echo hi > made.txt; "
                                "echo 'suite: 1 passed' > \"$ANTIPHON_WORKER_TESTS\"; exit 0",
                                task_class="write")
            final = workers.result(project, started["id"], wait=10)
            self.assertEqual(final["state"], "completed", final)
            self.assertIn("made.txt", final["diff"])
            self.assertIn(".antiphon/cursor.json", final["diff"], "a tracked edit is evidence")
            self.assertIn("+edited", final["diff"])
            self.assertNotIn("tests.txt", final["diff"])
            self.assertEqual(final["tests"], "suite: 1 passed\n")

    def test_a_worker_directory_that_cannot_be_created_is_a_refusal_that_leaves_nothing(self):
        """Round 3, 2026-09-03: the guarded branch was right and unpinned —
        and pinning it found the store's own creation, one line above it,
        raising out of `start` with the record left behind."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA, size=1)
            real_sound_dir = workers._sound_dir

            def full_at_worker(path, create=False):
                if path == workers.worker_dir(project, record["id"]):
                    raise OSError(28, "No space left")
                return real_sound_dir(path, create=create)

            with patch.object(workers, "_sound_dir", side_effect=full_at_worker):
                with self.assertRaises(workers.Refused) as refused:
                    workers.start(project, record, "do it")
            self.assertIn("not delegated", str(refused.exception))
            self.assertIsNone(workers.read_task(project, record["id"]), "no record")
            self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))

    def test_an_oversized_diff_outlives_the_workers_directory_and_goes_with_the_record(self):
        """Round 2, 2026-09-03: a diff too large to inline was written inside
        the worker's directory, which its own collection made sweepable, so
        the path the result named died on the next hook. It lives beside the
        record now and goes with it at the TTL."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            self._rooted(project)
            started = self._run(project, bin_dir, "echo hello > made.txt; exit 0",
                                task_class="write")
            with patch.object(workers, "DIFF_INLINE", 8):
                final = workers.result(project, started["id"], wait=10)
            self.assertEqual(final["state"], "completed", final)
            self.assertNotIn("diff", final)
            path = final["diff_path"]
            self.assertEqual(os.path.dirname(path), workers.tasks_dir(project), "beside the record")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path, "rb") as f:
                self.assertIn(b"+hello", f.read())
            self.assertIsNotNone(workers.read_task(project, started["id"])["collected_at"])
            workers.sweep(project, time.time())
            self.assertFalse(os.path.isdir(workers.worker_dir(project, started["id"])),
                             "the directory goes with the collection")
            self.assertTrue(os.path.exists(path), "the evidence stays")
            self.assertEqual([t["id"] for t in workers.tasks(project)], [started["id"]],
                             "the diff file is not a record")
            workers.sweep(project, time.time() + workers.TASK_TTL + 1)
            self.assertFalse(os.path.exists(path), "and goes with the record at the TTL")
            self.assertEqual(workers.tasks(project), [])

    def test_four_finished_workers_do_not_refuse_a_fifth(self):
        """Review 2026-09-03: a worker that had exited kept its slot until
        somebody asked after it, and the refusal named four finished tasks.
        Admission reconciles first."""
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = [self._run(project, bin_dir, "exit 0") for _ in range(4)]
            deadline = time.time() + 5
            while time.time() < deadline and not all(
                    os.path.exists(workers.exit_path(project, r["id"])) for r in started):
                time.sleep(0.05)
            self.assertEqual([r["state"] for r in workers.tasks(project)], ["running"] * 4,
                             "nothing has asked yet, so the records still say running")
            fifth = workers.accept(project, kind="codex", task_class="read", sha256=SHA, size=1)
            self.assertEqual(fifth["state"], "accepted")
            self.assertEqual(sorted(r["state"] for r in workers.tasks(project)),
                             ["accepted"] + ["completed"] * 4)

    def test_a_handed_task_has_no_worker_to_collect_or_cancel(self):
        """Review 2026-09-03: `cancel` on a handed task reported success and
        stopped nothing. Both are refused with the one thing that can be
        done — telling the peer; `status` still reads the record."""
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA,
                                      size=1, to="build")
            workers.update_task(project, record["id"], lambda c: c.update(state="handed"))
            for action in (workers.result, workers.cancel):
                with self.assertRaises(workers.Refused, msg=action.__name__) as refused:
                    action(project, record["id"])
                self.assertIn("handed to the codex peer 'build' and has no worker here",
                              str(refused.exception))
                self.assertIn("only the peer can be told to stop", str(refused.exception))
            self.assertEqual(workers.status(project, record["id"])["state"], "handed")
            self.assertEqual(workers.read_task(project, record["id"])["state"], "handed",
                             "a refusal changes nothing")

    def test_the_result_wait_is_clamped_to_max_wait(self):
        """Review 2026-09-03, unpinned: without the clamp `result` waits
        whatever it is asked, and a hundred thousand seconds is a day."""
        from unittest.mock import patch
        self.assertEqual(workers._bounded_wait(100_000), workers.MAX_WAIT)
        self.assertEqual(workers._bounded_wait(-5), 0.0)
        self.assertEqual(workers._bounded_wait(None), 0.0)
        self.assertEqual(workers._bounded_wait("soon"), 0.0)
        self.assertEqual(workers._bounded_wait(float("nan")), 0.0)
        self.assertEqual(workers._bounded_wait(2.5), 2.5)
        self.assertEqual(workers._bounded_wait(True), 0.0, "a bool is not a second")
        self.assertEqual(workers._bounded_wait(False), 0.0)
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir, "sleep 30")
            began = time.perf_counter()
            with patch.object(workers, "MAX_WAIT", 0.3):
                answer = workers.result(project, started["id"], wait=100_000)
            self.assertEqual(answer["state"], "running")
            self.assertLess(time.perf_counter() - began, 3.0)
            workers.cancel(project, started["id"])

    def test_removing_a_worktree_forgets_ours_and_never_prunes_the_users(self):
        """Review 2026-09-03: `git worktree prune` after every removal pruned
        the admin data of any missing worktree of the user's repository. A
        worktree whose directory is already gone is forgotten by its own
        entry; a stale entry that is not ours stays."""
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir, \
             tempfile.TemporaryDirectory() as elsewhere:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            foreign = os.path.join(elsewhere, "theirs")
            subprocess.run(["git", "-C", project, "worktree", "add", "--detach", "-q",
                            foreign, "HEAD"], check=True)
            import shutil
            shutil.rmtree(foreign)
            started = self._run(project, bin_dir,
                                "echo $$ > \"$ANTIPHON_WORKER_DIR/child.pid\"; sleep 30",
                                task_class="write")
            self._child_pid(project, started["id"])
            shutil.rmtree(workers.worker_dir(project, started["id"]))
            listed = subprocess.run(["git", "-C", project, "worktree", "list", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertIn(started["id"], listed, "gone from disk, still on git's books")
            self.assertIn("theirs", listed)
            workers.cancel(project, started["id"])
            listed = subprocess.run(["git", "-C", project, "worktree", "list", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertNotIn(started["id"], listed, "ours is forgotten")
            self.assertIn("theirs", listed, "the user's stale worktree is not ours to prune")

    def test_cancel_on_a_finished_task_removes_its_directory(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            done = self._run(project, bin_dir, "exit 0")
            self._settle(project, done["id"], "completed")
            self.assertTrue(os.path.isdir(workers.worker_dir(project, done["id"])))
            self.assertEqual(workers.cancel(project, done["id"])["state"], "completed",
                             "a finished task keeps its state; cancel only clears it")
            self.assertFalse(os.path.exists(workers.worker_dir(project, done["id"])))

    def test_cancel_never_reports_success_when_worker_directory_removal_fails(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="completed", exit_code=0, finished_at=2.0))

            with patch.object(workers.shutil, "rmtree", return_value=None):
                with self.assertRaisesRegex(workers.Refused,
                                            "worker directory could not be removed"):
                    workers.cancel(project, record["id"])

            self.assertTrue(os.path.isdir(directory))
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "completed")
            self.assertEqual(workers.cancel(project, record["id"])["state"],
                             "completed", "an explicit retry completes cleanup")
            self.assertFalse(os.path.exists(directory))

    def test_cancel_never_reports_success_when_git_admin_cleanup_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            import subprocess
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            done = self._run(project, bin_dir, "exit 0", task_class="write")
            self._settle(project, done["id"], "completed")

            with patch.object(workers, "_git", return_value=None), \
                 patch.object(workers, "_git_checkout", return_value=False):
                with self.assertRaisesRegex(workers.Refused,
                                            "worker directory could not be removed"):
                    workers.cancel(project, done["id"])

            self.assertTrue(os.path.isdir(workers.worker_dir(project, done["id"])),
                            "unverified Git cleanup keeps its retry witness")
            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            self.assertIn(done["id"], listed, "the first cleanup could not inspect Git")
            self.assertEqual(workers.cancel(project, done["id"])["state"],
                             "completed", "a later cancel safely retries Git cleanup")
            self.assertFalse(os.path.exists(workers.worker_dir(project, done["id"])))
            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            self.assertNotIn(done["id"], listed)

    def test_removing_the_last_git_admin_directory_fsyncs_its_parent(self):
        """Git may remove `.git/worktrees` with its last linked worktree.

        Missing is cleanup authority only after the containing common Git
        directory has made that namespace deletion durable; only then may the
        task-owned retry witness disappear.
        """
        with tempfile.TemporaryDirectory() as project:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            os.makedirs(workers.worker_dir(project, record["id"]))
            self.assertTrue(workers._write_git_cleanup_witness(project, record["id"]))
            subprocess.run([
                "git", "-C", project, "worktree", "add", "--detach", "-q",
                workers.work_dir(project, record["id"]), "HEAD"], check=True)
            base = subprocess.run(
                ["git", "-C", project, "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True).stdout.strip()
            self.assertTrue(workers.update_task(
                project, record["id"],
                lambda changed: changed.update(base=base)))
            record = workers.read_task(project, record["id"])
            common = subprocess.run(
                ["git", "-C", project, "rev-parse", "--git-common-dir"],
                check=True, capture_output=True, text=True).stdout.strip()
            if not os.path.isabs(common):
                common = os.path.join(project, common)
            common = os.path.realpath(common)
            synced = []
            real_fsync = workers._fsync_directory

            def observe(directory):
                synced.append(os.path.realpath(directory))
                return real_fsync(directory)

            with patch.object(workers, "_fsync_directory", side_effect=observe):
                self.assertTrue(workers._remove_dir(project, record))

            self.assertFalse(os.path.exists(os.path.join(common, "worktrees")),
                             "the fixture exercises Git's last-admin removal")
            self.assertIn(common, synced,
                          "the absent admin namespace is durable before its witness goes")

    def test_malformed_git_admin_never_consumes_the_cleanup_witness(self):
        """A readable `gitdir` is not necessarily a trustworthy path.

        The complete namespace must be structurally readable before absence
        is authority. Empty, multiline, relative, undecodable, linked, and
        non-regular entries all retain the exact retry witness.
        """
        for shape in ("empty", "multiline", "relative", "absolute-non-git",
                      "non-normal", "non-utf8", "nul", "symlink", "directory"):
            with self.subTest(shape=shape), \
                 tempfile.TemporaryDirectory() as project:
                subprocess.run(["git", "init", "-q", project], check=True)
                subprocess.run([
                    "git", "-C", project, "-c", "user.email=a@b",
                    "-c", "user.name=a", "commit", "-q", "--allow-empty",
                    "-m", "root"], check=True)
                record = workers.new_task(
                    project, kind="codex", task_class="write", sha256=SHA,
                    size=1)
                os.makedirs(workers.worker_dir(project, record["id"]))
                self.assertTrue(
                    workers._write_git_cleanup_witness(project, record["id"]))
                work = workers.work_dir(project, record["id"])
                subprocess.run([
                    "git", "-C", project, "worktree", "add", "--detach", "-q",
                    work, "HEAD"], check=True)
                base = subprocess.run(
                    ["git", "-C", project, "rev-parse", "HEAD"], check=True,
                    capture_output=True, text=True).stdout.strip()
                self.assertTrue(workers.update_task(
                    project, record["id"],
                    lambda changed: changed.update(base=base)))
                record = workers.read_task(project, record["id"])
                common = subprocess.run(
                    ["git", "-C", project, "rev-parse", "--git-common-dir"],
                    check=True, capture_output=True, text=True).stdout.strip()
                if not os.path.isabs(common):
                    common = os.path.join(project, common)
                admin = os.path.join(common, "worktrees")
                entries = os.listdir(admin)
                self.assertEqual(len(entries), 1)
                entry = os.path.join(admin, entries[0])
                gitdir = os.path.join(entry, "gitdir")
                wanted = os.path.realpath(os.path.join(work, ".git"))
                shutil.rmtree(work)

                if shape == "empty":
                    with open(gitdir, "wb") as stream:
                        stream.write(b"")
                elif shape == "multiline":
                    with open(gitdir, "wb") as stream:
                        stream.write((wanted + "\n/another/path\n").encode())
                elif shape == "relative":
                    with open(gitdir, "wb") as stream:
                        stream.write(b"relative/work/.git\n")
                elif shape == "absolute-non-git":
                    with open(gitdir, "wb") as stream:
                        stream.write(b"/tmp/not-a-worktree-dotgit\n")
                elif shape == "non-normal":
                    with open(gitdir, "wb") as stream:
                        stream.write(b"/tmp/one/../two/.git\n")
                elif shape == "non-utf8":
                    with open(gitdir, "wb") as stream:
                        stream.write(b"/tmp/\xff/.git\n")
                elif shape == "nul":
                    with open(gitdir, "wb") as stream:
                        stream.write(b"/tmp/impossible\x00path/.git\n")
                elif shape == "symlink":
                    alternate = os.path.join(entry, "linked-gitdir")
                    with open(alternate, "w", encoding="utf-8") as stream:
                        stream.write(wanted + "\n")
                    os.unlink(gitdir)
                    os.symlink(alternate, gitdir)
                else:
                    os.unlink(gitdir)
                    os.mkdir(gitdir)

                self.assertFalse(workers._remove_dir(project, record))
                self.assertTrue(os.path.lexists(entry))
                self.assertTrue(os.path.isdir(workers.worker_dir(project, record["id"])))
                self.assertEqual(
                    workers._git_cleanup_witness(project, record["id"]), "present")

    def test_gitdir_reader_charges_the_complete_file_to_its_byte_ceiling(self):
        """A ceiling-plus-one complete line cannot bypass the size guard."""
        with tempfile.TemporaryDirectory() as root:
            entry = os.path.join(root, "entry")
            os.mkdir(entry)
            suffix = b"/.git"
            registered = (b"/" + b"a" * (
                workers.GITDIR_CEILING - len(suffix) - 1) + suffix)
            self.assertEqual(len(registered), workers.GITDIR_CEILING)
            with open(os.path.join(entry, "gitdir"), "wb") as stream:
                stream.write(registered + b"\n")

            with patch.object(workers.os.path, "realpath",
                              side_effect=lambda path: path):
                self.assertIsNone(workers._gitdir_target(entry))

    def test_git_removal_never_deletes_a_reused_admin_basename(self):
        """Git, not an Antiphon pathname delete, owns admin serialization.

        After the exact missing worktree is removed, Git may immediately reuse
        its admin basename for an unrelated physical path. Verification must
        leave that new worktree usable.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as unrelated_root:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            os.makedirs(workers.worker_dir(project, record["id"]))
            self.assertTrue(workers._write_git_cleanup_witness(project, record["id"]))
            work = workers.work_dir(project, record["id"])
            subprocess.run([
                "git", "-C", project, "worktree", "add", "--detach", "-q",
                work, "HEAD"], check=True)
            base = subprocess.run(
                ["git", "-C", project, "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True).stdout.strip()
            self.assertTrue(workers.update_task(
                project, record["id"],
                lambda changed: changed.update(base=base)))
            record = workers.read_task(project, record["id"])
            shutil.rmtree(work)
            unrelated = os.path.join(unrelated_root, "work")
            real_git = workers._git
            replaced = False

            def replace_after_exact_remove(cwd, *args, **kwargs):
                nonlocal replaced
                result = real_git(cwd, *args, **kwargs)
                if args[:3] == ("worktree", "remove", "--force"):
                    self.assertEqual(os.path.realpath(args[3]), os.path.realpath(work))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    subprocess.run([
                        "git", "-C", project, "worktree", "add", "--detach",
                        "-q", unrelated, "HEAD"], check=True)
                    replaced = True
                return result

            with patch.object(workers, "_git", side_effect=replace_after_exact_remove):
                self.assertTrue(workers._remove_dir(project, record))

            self.assertTrue(replaced, "the exact path is offered to Git even after it vanished")
            healthy = subprocess.run(
                ["git", "-C", unrelated, "status", "--porcelain"],
                capture_output=True, text=True)
            self.assertEqual(healthy.returncode, 0, healthy.stderr)
            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            listed_paths = [os.path.realpath(line[len("worktree "):])
                            for line in listed.splitlines()
                            if line.startswith("worktree ")]
            self.assertIn(os.path.realpath(unrelated), listed_paths)

    def test_sweep_retries_cleanup_after_durable_cancelled_record(self):
        """A crash after committing cancellation but before rmtree leaves a
        cleanup obligation, not an uncollected result.  The durable terminal
        row stays while a later sweep retries the idempotent external cleanup.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            with open(os.path.join(directory, "left-by-crash"), "w") as stream:
                stream.write("work")
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="cancelled", finished_at=2.0))

            workers.sweep(project, 3.0)

            final = workers.read_task(project, record["id"])
            self.assertEqual(final["state"], "cancelled")
            self.assertIsNone(final["collected_at"])
            self.assertFalse(os.path.exists(directory))

    def test_a_stale_accepted_record_is_swept(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.sweep(project, time.time())
            self.assertIsNotNone(workers.read_task(project, record["id"]), "just accepted")
            workers.sweep(project, time.time() + workers.START_PATIENCE + 1)
            self.assertIsNone(workers.read_task(project, record["id"]),
                              "a start that died mid-way leaves an accepted record; swept")

    def test_stale_accepted_with_uncommitted_proof_marker_is_reaped(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            marker = os.open(workers.live_path(project, record["id"]),
                             os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(
                    marker, workers._published_marker(125, PROOF))
            finally:
                os.close(marker)

            workers.sweep(
                project, record["created_at"] + workers.START_PATIENCE + 1)

            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertFalse(os.path.exists(workers.live_path(project, record["id"])))

    def test_stale_accepted_with_missing_worktree_retries_stranded_git_admin(self):
        """Crash after `git worktree add`, before `base` reaches the row.

        If the physical worktree then disappears, only a witness written
        before Git was touched can retain the exact admin-cleanup obligation.
        """
        with tempfile.TemporaryDirectory() as project:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            workers.update_task(
                project, record["id"],
                lambda changed: changed.update(created_at=1.0))
            record = workers.read_task(project, record["id"])
            script = """
import os
import sys
sys.path.insert(0, sys.argv[3])
import workers

real_git = workers._git
def crash_after_add(cwd, *args, **kwargs):
    result = real_git(cwd, *args, **kwargs)
    if args[:2] == ("worktree", "add"):
        os._exit(91)
    return result

workers._git = crash_after_add
workers.start(sys.argv[1], workers.read_task(sys.argv[1], sys.argv[2]), "x")
"""
            crashed = subprocess.run([
                sys.executable, "-c", script, project, record["id"],
                os.path.dirname(workers.__file__)])
            self.assertEqual(crashed.returncode, 91)
            self.assertEqual(
                workers._git_cleanup_witness(project, record["id"]),
                "present", "cleanup authority precedes git worktree add")
            work = workers.work_dir(project, record["id"])
            shutil.rmtree(work)
            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            self.assertIn(record["id"], listed,
                          "only the physical half was removed")

            with patch.object(workers, "_git", return_value=None):
                workers.sweep(project, workers.START_PATIENCE + 2.0)

            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertEqual(
                workers._git_cleanup_witness(project, record["id"]), "present")
            self.assertTrue(os.path.isdir(workers.worker_dir(project, record["id"])))
            workers.sweep(project, workers.START_PATIENCE + 2.0)
            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            self.assertNotIn(record["id"], listed)
            self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))

    def test_an_inflight_worktree_add_keeps_the_start_lease_after_its_parent_dies(self):
        """The process that can still publish Git state owns the start lease.

        A killed ``start`` parent must not make its accepted row sweepable
        while its ``git worktree add`` child is still in flight.  A narrow
        guardian holds the lifecycle descriptor without exposing it to Git;
        after Git exits, the next sweep can retire the row and the exact
        registration together.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            subprocess.run([real_git, "init", "-q", project], check=True)
            subprocess.run([
                real_git, "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            workers.update_task(
                project, record["id"],
                lambda changed: changed.update(created_at=1.0))
            started_path = os.path.join(control, "add-started")
            release_path = os.path.join(control, "release-add")
            done_path = os.path.join(control, "add-done")
            fake_git = os.path.join(control, "git")
            with open(fake_git, "w", encoding="utf-8") as stream:
                stream.write(
                    f"#!{sys.executable}\n"
                    "import os, subprocess, sys, time\n"
                    "args = sys.argv[1:]\n"
                    "if 'worktree' in args and "
                    "args[args.index('worktree') + 1:][:1] == ['add']:\n"
                    "    try:\n"
                    "        os.setsid()\n"
                    "    except PermissionError:\n"
                    "        pass\n"
                    "    with open(os.environ['ANTIPHON_TEST_ADD_STARTED'], 'w') as f:\n"
                    "        f.write(str(os.getpid()))\n"
                    "    while not os.path.exists(os.environ['ANTIPHON_TEST_ADD_RELEASE']):\n"
                    "        time.sleep(0.01)\n"
                    "    try:\n"
                    "        result = subprocess.run(\n"
                    "            [os.environ['ANTIPHON_TEST_REAL_GIT']] + args,\n"
                    "            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
                    "            stderr=subprocess.DEVNULL, timeout=10)\n"
                    "        code = result.returncode\n"
                    "    except subprocess.TimeoutExpired:\n"
                    "        code = 124\n"
                    "    temporary = os.environ['ANTIPHON_TEST_ADD_DONE'] + '.tmp'\n"
                    "    with open(temporary, 'w') as f:\n"
                    "        f.write(str(code))\n"
                    "    os.replace(temporary, os.environ['ANTIPHON_TEST_ADD_DONE'])\n"
                    "    raise SystemExit(code)\n"
                    "os.execv(os.environ['ANTIPHON_TEST_REAL_GIT'], "
                    "[os.environ['ANTIPHON_TEST_REAL_GIT']] + args)\n")
            os.chmod(fake_git, 0o755)
            script = """
import sys
sys.path.insert(0, sys.argv[3])
import workers
workers.start(sys.argv[1], workers.read_task(sys.argv[1], sys.argv[2]), "x")
"""
            env = dict(
                os.environ,
                PATH=control + os.pathsep + os.environ.get("PATH", ""),
                ANTIPHON_TEST_ADD_STARTED=started_path,
                ANTIPHON_TEST_ADD_RELEASE=release_path,
                ANTIPHON_TEST_ADD_DONE=done_path,
                ANTIPHON_TEST_REAL_GIT=real_git)
            parent = subprocess.Popen([
                sys.executable, "-c", script, project, record["id"],
                os.path.dirname(workers.__file__)], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            add_pid = None
            add_birth = None
            work = workers.work_dir(project, record["id"])
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not os.path.exists(started_path):
                    if parent.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(
                    os.path.exists(started_path),
                    "the fake Git child did not reach the controlled add barrier")
                with open(started_path, encoding="ascii") as stream:
                    add_pid = int(stream.read())
                add_birth = workers._process_start(add_pid)
                self.assertIsNotNone(add_birth)
                os.kill(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)

                workers.sweep(project, workers.START_PATIENCE + 2.0)

                self.assertIsNotNone(
                    workers.read_task(project, record["id"]),
                    "the in-flight Git child still owns the accepted start")
                self.assertEqual(
                    workers._git_cleanup_witness(project, record["id"]),
                    "present")
                self.assertTrue(os.path.isdir(workers.worker_dir(project, record["id"])))

                with open(release_path, "w", encoding="ascii") as stream:
                    stream.write("go\n")
                deadline = time.time() + 10
                while time.time() < deadline and not os.path.exists(done_path):
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(done_path),
                                "the controlled Git command did not finish")
                with open(done_path, encoding="ascii") as stream:
                    self.assertEqual(stream.read(), "0")
                self.assertTrue(os.path.isfile(os.path.join(work, ".git")))
                listed = subprocess.run(
                    [real_git, "-C", project, "worktree", "list", "--porcelain"],
                    capture_output=True, text=True, check=True).stdout
                self.assertIn(record["id"], listed,
                              "Git published the exact registration before unlock")
                deadline = time.time() + 5
                while time.time() < deadline:
                    lock, _outcome = workers._lock_observation(
                        project, record["id"], record=record)
                    if lock in ("dead", "unlocked_unknown"):
                        break
                    time.sleep(0.01)
                self.assertIn(lock, ("dead", "unlocked_unknown"))

                workers.sweep(project, workers.START_PATIENCE + 2.0)

                self.assertIsNone(workers.read_task(project, record["id"]))
                self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))
                listed = subprocess.run(
                    [real_git, "-C", project, "worktree", "list", "--porcelain"],
                    capture_output=True, text=True, check=True).stdout
                self.assertNotIn(record["id"], listed)
            finally:
                with open(release_path, "a", encoding="ascii"):
                    pass
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if add_pid is not None and not os.path.exists(done_path):
                    deadline = time.time() + 5
                    while time.time() < deadline and not os.path.exists(done_path):
                        time.sleep(0.01)
                    snapshot = workers._process_snapshot(add_pid)
                    if (not os.path.exists(done_path)
                            and isinstance(snapshot, tuple)
                            and snapshot[0] == add_birth):
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(add_pid, signal.SIGKILL)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "remove", "--force", work],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "prune"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_the_worktree_add_lease_does_not_escape_to_hook_descendants(self):
        """Repo-controlled hook descendants cannot retain Antiphon's lease.

        The process supervising ``git worktree add`` must own the lifecycle
        descriptor without exposing it to Git.  Otherwise a detached child of
        ``post-checkout`` can keep a successfully finished worker permanently
        in the nonterminal ``settling`` observation.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control, \
             tempfile.TemporaryDirectory() as bin_dir:
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            subprocess.run([real_git, "init", "-q", project], check=True)
            subprocess.run([
                real_git, "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            hook_started = os.path.join(control, "hook-started")
            hook_release = os.path.join(control, "release-hook")
            hook = os.path.join(project, ".git", "hooks", "post-checkout")
            with open(hook, "w", encoding="utf-8") as stream:
                stream.write(
                    "#!/bin/sh\n"
                    "(\n"
                    f"  echo $$ > \"{hook_started}\"\n"
                    f"  while [ ! -e \"{hook_release}\" ]; do sleep 0.01; done\n"
                    ") </dev/null >/dev/null 2>&1 &\n"
                    "exit 0\n")
            os.chmod(hook, 0o755)
            self._stub(bin_dir, "codex", "exit 0")
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1)
            started = None
            try:
                started = workers.start(
                    project, record, "x", env=self._env(bin_dir))
                deadline = time.time() + 5
                while time.time() < deadline and not os.path.exists(hook_started):
                    time.sleep(0.01)
                self.assertTrue(
                    os.path.exists(hook_started),
                    "the real post-checkout hook did not start its held child")

                final = workers.result(project, record["id"], wait=5)

                self.assertEqual(
                    final["state"], "completed",
                    "a hook descendant cannot retain the supervisor's lease")
                self.assertEqual(final["exit_code"], 0)
            finally:
                with open(hook_release, "a", encoding="ascii"):
                    pass
                if started is not None:
                    final = workers.result(project, record["id"], wait=5)
                    if final is not None and final["state"] == "completed":
                        workers.sweep(project, time.time())

    @unittest.skipUnless(hasattr(os, "fork") and hasattr(os, "setsid"),
                         "requires a detached hook process")
    def test_a_detached_hook_pipe_cannot_hide_direct_git_completion(self):
        """Direct Git completion is independent of descendant stdio EOF."""
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            subprocess.run([real_git, "init", "-q", project], check=True)
            subprocess.run([
                real_git, "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            started = os.path.join(control, "hook-started")
            release = os.path.join(control, "hook-release")
            write_outcome = os.path.join(control, "hook-write-outcome")
            hook = os.path.join(project, ".git", "hooks", "post-checkout")
            with open(hook, "w", encoding="utf-8") as stream:
                stream.write(
                    f"#!{sys.executable}\n"
                    "import os, time\n"
                    "child = os.fork()\n"
                    "if child == 0:\n"
                    "    os.setsid()\n"
                    f"    with open({started!r}, 'w') as marker:\n"
                    "        marker.write(str(os.getpid()))\n"
                    f"    while not os.path.exists({release!r}):\n"
                    "        time.sleep(0.01)\n"
                    "    try:\n"
                    "        written = os.write(1, b'x' * "
                    f"{4 * workers.GIT_GUARDIAN_OUTPUT_CEILING})\n"
                    "    except OSError as error:\n"
                    "        outcome = f'error:{error.errno}'\n"
                    "    else:\n"
                    "        outcome = f'wrote:{written}'\n"
                    f"    with open({write_outcome!r}, 'w') as result:\n"
                    "        result.write(outcome)\n"
                    "    os._exit(0)\n"
                    "os._exit(0)\n")
            os.chmod(hook, 0o755)
            live = os.path.join(control, "live")
            lease = os.open(live, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lease, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lease, workers.LIVE_STARTING)
            work = os.path.join(control, "work")
            child_pid = None
            child_birth = None
            try:
                before = time.monotonic()
                result = workers._git(
                    project, "worktree", "add", "--detach", "-q", work,
                    "HEAD", timeout=1.0, lease_fd=lease)
                elapsed = time.monotonic() - before

                self.assertIsNotNone(result)
                self.assertEqual(result.returncode, 0)
                self.assertLess(elapsed, 3.0)
                self.assertEqual(
                    workers._read_live_marker(lease), workers.LIVE_STARTING)
                deadline = time.time() + 5
                while time.time() < deadline and not os.path.exists(started):
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(started))
                with open(started, encoding="ascii") as stream:
                    child_pid = int(stream.read())
                child_birth = workers._process_start(child_pid)
                self.assertIsNotNone(child_birth)
                self.assertEqual(
                    workers._process_start(child_pid), child_birth,
                    "the detached pipe owner is still live after Git returns")
                with open(release, "w", encoding="ascii"):
                    pass
                deadline = time.time() + 5
                while time.time() < deadline and not os.path.exists(write_outcome):
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(write_outcome))
                with open(write_outcome, encoding="ascii") as stream:
                    self.assertTrue(
                        stream.read().startswith("error:"),
                        "a detached writer must not retain a growing capture sink")
            finally:
                with open(release, "a", encoding="ascii"):
                    pass
                deadline = time.time() + 5
                while (child_pid is not None and time.time() < deadline
                       and workers._process_start(child_pid) == child_birth):
                    time.sleep(0.01)
                if (child_pid is not None and child_birth is not None
                        and workers._process_start(child_pid) == child_birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)
                workers.fcntl.flock(lease, workers.fcntl.LOCK_UN)
                os.close(lease)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "remove", "--force", work],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "prune"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_a_guardian_timeout_never_mints_a_completion_receipt(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            fake_git = os.path.join(control, "git")
            with open(fake_git, "w", encoding="utf-8") as stream:
                stream.write(
                    f"#!{sys.executable}\n"
                    "import time\n"
                    "time.sleep(30)\n")
            os.chmod(fake_git, 0o755)
            live = os.path.join(control, "live")
            lease = os.open(live, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lease, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lease, workers.LIVE_STARTING)
            try:
                with patch.dict(os.environ, {
                        "PATH": control + os.pathsep + os.environ.get("PATH", "")}):
                    with self.assertRaises(workers._GitMutationUnresolved):
                        workers._git(
                            project, "worktree", "add", "elsewhere", "HEAD",
                            timeout=0.1, lease_fd=lease)
                self.assertIsNotNone(workers._git_mutator_parts(
                    workers._read_live_marker(lease)))
            finally:
                workers.fcntl.flock(lease, workers.fcntl.LOCK_UN)
                os.close(lease)

    def test_a_dead_guardian_receipt_is_never_inferred_from_late_git_exit(self):
        """A guardian failure permanently withholds automatic cleanup.

        An exact Git leader cannot account for a descendant that detached from
        its process group.  When the sole completion-receipt writer dies, the
        accepted row and its witnesses therefore remain even after the known
        mutator later exits and publishes a worktree.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            subprocess.run([real_git, "init", "-q", project], check=True)
            subprocess.run([
                real_git, "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            started_path = os.path.join(control, "mutator-started")
            release_path = os.path.join(control, "release-mutator")
            done_path = os.path.join(control, "mutator-done")
            fake_git = os.path.join(control, "git")
            with open(fake_git, "w", encoding="utf-8") as stream:
                stream.write(
                    f"#!{sys.executable}\n"
                    "import os, subprocess, sys, time\n"
                    "args = sys.argv[1:]\n"
                    "if 'worktree' in args and "
                    "args[args.index('worktree') + 1:][:1] == ['add']:\n"
                    "    try:\n"
                    "        os.setsid()\n"
                    "    except PermissionError:\n"
                    "        pass\n"
                    "    with open(os.environ['ANTIPHON_TEST_MUTATOR_STARTED'], 'w') as f:\n"
                    "        f.write(f'{os.getpid()} {os.getppid()}')\n"
                    "    while not os.path.exists(os.environ['ANTIPHON_TEST_MUTATOR_RELEASE']):\n"
                    "        time.sleep(0.01)\n"
                    "    result = subprocess.run(\n"
                    "        [os.environ['ANTIPHON_TEST_REAL_GIT']] + args,\n"
                    "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
                    "        stderr=subprocess.DEVNULL, timeout=10)\n"
                    "    temporary = os.environ['ANTIPHON_TEST_MUTATOR_DONE'] + '.tmp'\n"
                    "    with open(temporary, 'w') as f:\n"
                    "        f.write(str(result.returncode))\n"
                    "    os.replace(temporary, os.environ['ANTIPHON_TEST_MUTATOR_DONE'])\n"
                    "    raise SystemExit(result.returncode)\n"
                    "os.execv(os.environ['ANTIPHON_TEST_REAL_GIT'], "
                    "[os.environ['ANTIPHON_TEST_REAL_GIT']] + args)\n")
            os.chmod(fake_git, 0o755)
            script = """
import sys
sys.path.insert(0, sys.argv[3])
import workers
workers.start(sys.argv[1], workers.read_task(sys.argv[1], sys.argv[2]), "x")
"""
            env = dict(
                os.environ,
                PATH=control + os.pathsep + os.environ.get("PATH", ""),
                ANTIPHON_TEST_MUTATOR_STARTED=started_path,
                ANTIPHON_TEST_MUTATOR_RELEASE=release_path,
                ANTIPHON_TEST_MUTATOR_DONE=done_path,
                ANTIPHON_TEST_REAL_GIT=real_git)
            starter = subprocess.Popen([
                sys.executable, "-c", script, project, record["id"],
                os.path.dirname(workers.__file__)], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            mutator_pid = None
            mutator_birth = None
            work = workers.work_dir(project, record["id"])
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not os.path.exists(started_path):
                    if starter.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(started_path),
                                "the controlled Git mutator did not start")
                with open(started_path, encoding="ascii") as stream:
                    mutator_pid, guardian_pid = map(int, stream.read().split())
                mutator_birth = workers._process_start(mutator_pid)
                self.assertIsNotNone(mutator_birth)

                os.kill(guardian_pid, signal.SIGKILL)
                starter.wait(timeout=10)
                self.assertNotEqual(starter.returncode, 0)
                self.assertEqual(
                    workers.read_task(project, record["id"])["state"],
                    "accepted")
                self.assertEqual(
                    workers._git_cleanup_witness(project, record["id"]),
                    "present")
                with open(release_path, "w", encoding="ascii") as stream:
                    stream.write("go\n")

                deadline = time.time() + 10
                while time.time() < deadline and not os.path.exists(done_path):
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(done_path))
                with open(done_path, encoding="ascii") as stream:
                    self.assertEqual(stream.read(), "0")

                workers.sweep(
                    project,
                    record["created_at"] + workers.START_PATIENCE + 1)
                self.assertEqual(
                    workers.read_task(project, record["id"])["state"],
                    "accepted")
                self.assertEqual(
                    workers._git_cleanup_witness(project, record["id"]),
                    "present")
                self.assertTrue(
                    os.path.isdir(workers.worker_dir(project, record["id"])))
                listed = subprocess.run(
                    [real_git, "-C", project, "worktree", "list", "--porcelain"],
                    capture_output=True, text=True, check=True).stdout
                self.assertIn(record["id"], listed)
            finally:
                with open(release_path, "a", encoding="ascii"):
                    pass
                if starter.poll() is None:
                    starter.kill()
                    starter.wait(timeout=5)
                snapshot = (workers._process_snapshot(mutator_pid)
                            if mutator_pid is not None else None)
                if (mutator_pid is not None and isinstance(snapshot, tuple)
                        and snapshot[0] == mutator_birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(mutator_pid, signal.SIGKILL)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "remove", "--force", work],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "prune"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_sweep_never_infers_a_missing_guardian_receipt_from_process_death(self):
        """No process-table observation substitutes for a missing receipt."""
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            subprocess.run([real_git, "init", "-q", project], check=True)
            subprocess.run([
                real_git, "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            workers.update_task(
                project, record["id"],
                lambda changed: changed.update(created_at=1.0))
            started_path = os.path.join(control, "mutator-started")
            release_path = os.path.join(control, "release-mutator")
            done_path = os.path.join(control, "mutator-done")
            fake_git = os.path.join(control, "git")
            with open(fake_git, "w", encoding="utf-8") as stream:
                stream.write(
                    f"#!{sys.executable}\n"
                    "import os, subprocess, sys, time\n"
                    "args = sys.argv[1:]\n"
                    "if 'worktree' in args and "
                    "args[args.index('worktree') + 1:][:1] == ['add']:\n"
                    "    try:\n"
                    "        os.setsid()\n"
                    "    except PermissionError:\n"
                    "        pass\n"
                    "    with open(os.environ['ANTIPHON_TEST_MUTATOR_STARTED'], 'w') as f:\n"
                    "        f.write(f'{os.getpid()} {os.getppid()}')\n"
                    "    while not os.path.exists(os.environ['ANTIPHON_TEST_MUTATOR_RELEASE']):\n"
                    "        time.sleep(0.01)\n"
                    "    result = subprocess.run(\n"
                    "        [os.environ['ANTIPHON_TEST_REAL_GIT']] + args,\n"
                    "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
                    "        stderr=subprocess.DEVNULL, timeout=10)\n"
                    "    temporary = os.environ['ANTIPHON_TEST_MUTATOR_DONE'] + '.tmp'\n"
                    "    with open(temporary, 'w') as f:\n"
                    "        f.write(str(result.returncode))\n"
                    "    os.replace(temporary, os.environ['ANTIPHON_TEST_MUTATOR_DONE'])\n"
                    "    raise SystemExit(result.returncode)\n"
                    "os.execv(os.environ['ANTIPHON_TEST_REAL_GIT'], "
                    "[os.environ['ANTIPHON_TEST_REAL_GIT']] + args)\n")
            os.chmod(fake_git, 0o755)
            script = """
import sys
sys.path.insert(0, sys.argv[3])
import workers
workers.start(sys.argv[1], workers.read_task(sys.argv[1], sys.argv[2]), "x")
"""
            env = dict(
                os.environ,
                PATH=control + os.pathsep + os.environ.get("PATH", ""),
                ANTIPHON_TEST_MUTATOR_STARTED=started_path,
                ANTIPHON_TEST_MUTATOR_RELEASE=release_path,
                ANTIPHON_TEST_MUTATOR_DONE=done_path,
                ANTIPHON_TEST_REAL_GIT=real_git)
            starter = subprocess.Popen([
                sys.executable, "-c", script, project, record["id"],
                os.path.dirname(workers.__file__)], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            mutator_pid = None
            mutator_birth = None
            work = workers.work_dir(project, record["id"])
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not os.path.exists(started_path):
                    if starter.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(started_path),
                                "the controlled Git mutator did not start")
                with open(started_path, encoding="ascii") as stream:
                    mutator_pid, guardian_pid = map(int, stream.read().split())
                mutator_birth = workers._process_start(mutator_pid)
                self.assertIsNotNone(mutator_birth)

                os.kill(starter.pid, signal.SIGSTOP)
                os.kill(guardian_pid, signal.SIGKILL)
                os.kill(starter.pid, signal.SIGKILL)
                starter.wait(timeout=5)

                workers.sweep(project, workers.START_PATIENCE + 2.0)

                self.assertEqual(
                    workers.read_task(project, record["id"])["state"],
                    "accepted")
                self.assertEqual(
                    workers._git_cleanup_witness(project, record["id"]),
                    "present")
                self.assertTrue(
                    os.path.isdir(workers.worker_dir(project, record["id"])))
                with open(release_path, "w", encoding="ascii") as stream:
                    stream.write("go\n")
                deadline = time.time() + 10
                while time.time() < deadline and not os.path.exists(done_path):
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(done_path))
                workers.sweep(project, workers.START_PATIENCE + 2.0)

                self.assertEqual(
                    workers.read_task(project, record["id"])["state"],
                    "accepted")
                self.assertEqual(
                    workers._git_cleanup_witness(project, record["id"]),
                    "present")
                listed = subprocess.run(
                    [real_git, "-C", project, "worktree", "list", "--porcelain"],
                    capture_output=True, text=True, check=True).stdout
                self.assertIn(record["id"], listed)
                self.assertTrue(os.path.exists(work))
            finally:
                with open(release_path, "a", encoding="ascii"):
                    pass
                if starter.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(starter.pid, signal.SIGCONT)
                    starter.kill()
                    starter.wait(timeout=5)
                snapshot = (workers._process_snapshot(mutator_pid)
                            if mutator_pid is not None else None)
                if (mutator_pid is not None and isinstance(snapshot, tuple)
                        and snapshot[0] == mutator_birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(mutator_pid, signal.SIGKILL)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "remove", "--force", work],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(
                    [real_git, "-C", project, "worktree", "prune"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_worker_directory_entry_is_durable_before_git_registration(self):
        """The cleanup witness is useful after a crash only if its containing
        task-directory entry was committed to the parent before Git can make
        its own durable worktree-admin entry.
        """
        with tempfile.TemporaryDirectory() as project:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            events = []
            real_fsync = workers._fsync_directory
            real_git = workers._git

            def observe_fsync(directory):
                events.append(("fsync", os.path.realpath(directory)))
                return real_fsync(directory)

            def refuse_add(cwd, *args, **kwargs):
                if args[:2] == ("worktree", "add"):
                    events.append(("git-add", None))
                    return subprocess.CompletedProcess(
                        args, 1, stdout="", stderr="held at test gate")
                return real_git(cwd, *args, **kwargs)

            with patch.object(workers, "_fsync_directory", side_effect=observe_fsync), \
                 patch.object(workers, "_git", side_effect=refuse_add):
                with self.assertRaisesRegex(workers.Refused,
                                            "worktree could not be created"):
                    workers.start(project, record, "do it")

            parent_sync = ("fsync", os.path.realpath(workers.workers_dir(project)))
            self.assertIn(parent_sync, events)
            self.assertLess(events.index(parent_sync), events.index(("git-add", None)),
                            "the task-directory entry is durable before Git is touched")

    def test_non_utf8_git_diagnostics_are_an_ordinary_cleanup_safe_refusal(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            subprocess.run([real_git, "init", "-q", project], check=True)
            subprocess.run([
                real_git, "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            fake_git = os.path.join(control, "git")
            with open(fake_git, "w", encoding="utf-8") as stream:
                stream.write(
                    f"#!{sys.executable}\n"
                    "import os, sys\n"
                    "args = sys.argv[1:]\n"
                    "if 'worktree' in args and "
                    "args[args.index('worktree') + 1:][:1] == ['add']:\n"
                    "    os.write(2, b'\\xffbroken diagnostic\\n')\n"
                    "    raise SystemExit(1)\n"
                    "os.execv(os.environ['ANTIPHON_TEST_REAL_GIT'], "
                    "[os.environ['ANTIPHON_TEST_REAL_GIT']] + args)\n")
            os.chmod(fake_git, 0o755)
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            with patch.dict(os.environ, {
                    "PATH": control + os.pathsep + os.environ.get("PATH", ""),
                    "ANTIPHON_TEST_REAL_GIT": real_git}):
                with self.assertRaisesRegex(
                        workers.Refused, "worktree could not be created") as raised:
                    workers.start(project, record, "do it")

            self.assertTrue(workers._utf8_string(str(raised.exception)))
            self.assertIn(r"\udcffbroken diagnostic", str(raised.exception))
            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertFalse(os.path.exists(
                workers.worker_dir(project, record["id"])))

    def test_python_startup_customization_cannot_inherit_the_git_lease(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            subprocess.run(["git", "init", "-q", project], check=True)
            live = os.path.join(control, "live")
            lease = os.open(live, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(lease, workers.fcntl.LOCK_EX)
            workers._write_live_marker(lease, workers.LIVE_STARTING)
            identity = os.fstat(lease)
            started = os.path.join(control, "startup-child")
            release = os.path.join(control, "startup-release")
            sitecustomize = os.path.join(control, "sitecustomize.py")
            with open(sitecustomize, "w", encoding="utf-8") as stream:
                stream.write(
                    "import os, time\n"
                    "try:\n"
                    "    descriptor = int(os.environ['LEASE_DESCRIPTOR'])\n"
                    "    info = os.fstat(descriptor)\n"
                    "    matches = f'{info.st_dev}:{info.st_ino}' == "
                    "os.environ['LEASE_IDENTITY']\n"
                    "except (KeyError, OSError, ValueError):\n"
                    "    matches = False\n"
                    "if matches:\n"
                    "    child = os.fork()\n"
                    "    if child == 0:\n"
                    "        null = os.open(os.devnull, os.O_RDWR)\n"
                    "        for target in (0, 1, 2):\n"
                    "            os.dup2(null, target)\n"
                    "        os.close(null)\n"
                    "        with open(os.environ['STARTUP_CHILD'], 'w') as f:\n"
                    "            f.write(str(os.getpid()))\n"
                    "        while not os.path.exists(os.environ['STARTUP_RELEASE']):\n"
                    "            time.sleep(0.01)\n"
                    "        os.close(descriptor)\n"
                    "        os._exit(0)\n")
            observer = None
            child_pid = None
            child_birth = None
            try:
                with patch.dict(os.environ, {
                        "PYTHONPATH": control,
                        "LEASE_DESCRIPTOR": str(lease),
                        "LEASE_IDENTITY": f"{identity.st_dev}:{identity.st_ino}",
                        "STARTUP_CHILD": started,
                        "STARTUP_RELEASE": release}):
                    result = workers._git(
                        project, "rev-parse", "--is-inside-work-tree",
                        lease_fd=lease)
                self.assertIsNotNone(result)
                self.assertEqual(result.returncode, 0)
                os.close(lease)
                lease = None

                observer = os.open(live, os.O_RDWR)
                workers.fcntl.flock(
                    observer, workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
                self.assertFalse(
                    os.path.exists(started),
                    "Python bootstrap code ran before the guardian narrowed its fd")
            finally:
                with open(release, "a", encoding="ascii"):
                    pass
                if os.path.exists(started):
                    with open(started, encoding="ascii") as stream:
                        child_pid = int(stream.read())
                    child_birth = workers._process_start(child_pid)
                if observer is not None:
                    with contextlib.suppress(OSError):
                        workers.fcntl.flock(observer, workers.fcntl.LOCK_UN)
                    os.close(observer)
                if lease is not None:
                    with contextlib.suppress(OSError):
                        workers.fcntl.flock(lease, workers.fcntl.LOCK_UN)
                    os.close(lease)
                deadline = time.time() + 5
                while (child_pid is not None and time.time() < deadline
                       and workers._process_start(child_pid) == child_birth):
                    time.sleep(0.01)
                if (child_pid is not None and child_birth is not None
                        and workers._process_start(child_pid) == child_birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)

    def test_python_startup_customization_cannot_inherit_worker_control_fds(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control, \
             tempfile.TemporaryDirectory() as bin_dir:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1)
            live = workers.live_path(project, record["id"])
            descriptor = os.open(live, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            identity = os.stat(live)
            started = os.path.join(control, "startup-child")
            release = os.path.join(control, "startup-release")
            sitecustomize = os.path.join(control, "sitecustomize.py")
            with open(sitecustomize, "w", encoding="utf-8") as stream:
                stream.write(
                    "import os, time\n"
                    "wanted = os.environ['LIVE_IDENTITY']\n"
                    "held = None\n"
                    "for candidate in range(3, 256):\n"
                    "    try:\n"
                    "        info = os.fstat(candidate)\n"
                    "    except OSError:\n"
                    "        continue\n"
                    "    if f'{info.st_dev}:{info.st_ino}' == wanted:\n"
                    "        held = candidate\n"
                    "        break\n"
                    "if held is not None:\n"
                    "    child = os.fork()\n"
                    "    if child == 0:\n"
                    "        try:\n"
                    "            os.setsid()\n"
                    "        except OSError:\n"
                    "            pass\n"
                    "        null = os.open(os.devnull, os.O_RDWR)\n"
                    "        for target in (0, 1, 2):\n"
                    "            os.dup2(null, target)\n"
                    "        os.close(null)\n"
                    "        with open(os.environ['STARTUP_CHILD'], 'w') as f:\n"
                    "            f.write(str(os.getpid()))\n"
                    "        while not os.path.exists(os.environ['STARTUP_RELEASE']):\n"
                    "            time.sleep(0.01)\n"
                    "        os.close(held)\n"
                    "        os._exit(0)\n")
            self._stub(bin_dir, "codex", "exit 0")
            env = self._env(bin_dir)
            env.update({
                "PYTHONPATH": control,
                "LIVE_IDENTITY": f"{identity.st_dev}:{identity.st_ino}",
                "STARTUP_CHILD": started,
                "STARTUP_RELEASE": release,
            })
            begun = None
            final = None
            child_pid = None
            child_birth = None
            try:
                begun = workers.start(project, record, "x", env=env)
                final = workers.result(project, record["id"], wait=5)
                self.assertEqual((final["state"], final["exit_code"]),
                                 ("completed", 0))
                self.assertFalse(
                    os.path.exists(started),
                    "Python bootstrap ran before worker fd protocol setup")
            finally:
                with open(release, "a", encoding="ascii"):
                    pass
                if os.path.exists(started):
                    with open(started, encoding="ascii") as stream:
                        child_pid = int(stream.read())
                    child_birth = workers._process_start(child_pid)
                deadline = time.time() + 5
                while (child_pid is not None and time.time() < deadline
                       and workers._process_start(child_pid) == child_birth):
                    time.sleep(0.01)
                if (child_pid is not None and child_birth is not None
                        and workers._process_start(child_pid) == child_birth):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)
                if begun is not None:
                    recovered = workers.result(project, record["id"], wait=5)
                    if recovered is not None and recovered["state"] in workers.TERMINAL:
                        workers.sweep(project, time.time())

    def test_guardian_launch_ignores_python_environment_configuration(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="")
        with patch.object(workers.subprocess, "run", return_value=completed) as run, \
             patch.object(workers, "_read_live_marker",
                          return_value=workers.LIVE_STARTING):
            result = workers._git(
                "/project", "worktree", "add", "work", "HEAD", lease_fd=19)

        self.assertEqual(result, completed)
        self.assertEqual(
            run.call_args.args[0][:4],
            [sys.executable, "-E", "-s", "-S"])

    def test_worker_wrapper_launch_ignores_python_environment_configuration(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            with patch.object(workers, "_git_checkout", return_value=False), \
                 patch.object(workers.subprocess, "Popen",
                              side_effect=OSError("held for argv inspection")) as spawn:
                with self.assertRaisesRegex(workers.Refused,
                                            "CLI could not be started"):
                    workers.start(project, record, "do it")

            self.assertEqual(
                spawn.call_args.args[0][:4],
                [sys.executable, "-E", "-s", "-S"])

    def test_gated_git_exec_has_no_pre_gate_python_startup_code(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as control:
            subprocess.run(["git", "init", "-q", project], check=True)
            probe = os.path.join(control, "python-startup-ran")
            with open(os.path.join(control, "sitecustomize.py"), "w",
                      encoding="utf-8") as stream:
                stream.write(
                    "import os\n"
                    "with open(os.environ['PYTHON_STARTUP_PROBE'], 'a') as f:\n"
                    "    f.write(str(os.getpid()) + '\\n')\n")
            live = os.path.join(control, "live")
            descriptor = os.open(live, os.O_CREAT | os.O_RDWR, 0o600)
            workers.fcntl.flock(descriptor, workers.fcntl.LOCK_EX)
            workers._write_live_marker(descriptor, workers.LIVE_STARTING)
            try:
                with patch.dict(os.environ, {
                        "PYTHONPATH": control,
                        "PYTHON_STARTUP_PROBE": probe}):
                    result = workers._git(
                        project, "rev-parse", "--is-inside-work-tree",
                        lease_fd=descriptor)
                self.assertIsNotNone(result)
                self.assertEqual(result.returncode, 0)
                self.assertFalse(os.path.exists(probe))
            finally:
                workers.fcntl.flock(descriptor, workers.fcntl.LOCK_UN)
                os.close(descriptor)

    def test_a_guardian_without_an_exact_child_birth_never_signals(self):
        class UnidentifiedChild:
            pid = 4242

            def poll(self):
                return None

            def wait(self, timeout):
                raise subprocess.TimeoutExpired(["python"], timeout)

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "live")
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            workers._write_live_marker(descriptor, workers.LIVE_STARTING)
            with patch.object(workers.subprocess, "Popen",
                              return_value=UnidentifiedChild()) as spawn, \
                 patch.object(workers, "START_IDENTITY_PATIENCE", 0.0), \
                 patch.object(workers, "_process_start", return_value=None), \
                 patch.object(workers, "_kill_group") as kill, \
                 patch.object(workers, "_emit_guardian_output"):
                result = workers._git_guardian_main([
                    "_git_guardian", str(descriptor), "1", directory,
                    "worktree", "add"])

            self.assertEqual(result, 125)
            kill.assert_not_called()
            self.assertEqual(
                spawn.call_args.args[0][:4],
                [sys.executable, "-E", "-s", "-S"])

    def test_a_worktree_without_a_verifiable_base_is_refused_and_fully_removed(self):
        """A successful `worktree add` is not enough to publish a worker.

        The checkout's immutable base is part of the task's cleanup and diff
        authority.  If it cannot be read after Git returns, refuse before the
        host starts and prove both the task-owned directory and Git's admin
        entry absent.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            self._stub(bin_dir, "codex", "exit 0")
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            base = subprocess.run(
                ["git", "-C", project, "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True).stdout.strip()

            with patch.object(workers, "_head", side_effect=(base, None)):
                with self.assertRaisesRegex(
                        workers.Refused, "worktree base could not be verified"):
                    workers.start(
                        project, record, "do it", env=self._env(bin_dir))

            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertFalse(os.path.exists(workers.worker_dir(project, record["id"])))
            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            self.assertNotIn(record["id"], listed)

    def test_sweep_rechecks_stale_accepted_before_cleanup(self):
        with tempfile.TemporaryDirectory() as project:
            stale = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, stale["id"])
            os.makedirs(directory)
            workers.update_task(project, stale["id"], lambda changed: changed.update(
                state="running", pid=os.getpid(),
                birth=workers._process_start(os.getpid()), started_at=2.0))
            current = workers.read_task(project, stale["id"])
            with patch.object(workers, "tasks",
                              side_effect=([stale], [current])):
                workers.sweep(
                    project, stale["created_at"] + workers.START_PATIENCE + 1)
            self.assertEqual(workers.read_task(project, stale["id"])["state"],
                             "running")
            self.assertTrue(os.path.isdir(directory))

    def test_late_reconciled_worker_gets_full_terminal_retention_window(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=999_999, birth="recorded birth",
                started_at=1.0, created_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
            marker = os.open(workers.live_path(project, record["id"]),
                             os.O_CREAT | os.O_RDWR, 0o600)
            try:
                workers._write_live_marker(
                    marker, workers._published_marker(0, PROOF))
            finally:
                os.close(marker)
            now = workers.TASK_TTL + 2.0
            with patch.object(workers, "_group_process_liveness",
                              return_value="dead"):
                workers.sweep(project, now)
            final = workers.read_task(project, record["id"])
            self.assertEqual(final["state"], "completed")
            self.assertEqual(final["finished_at"], now)
            self.assertTrue(os.path.isdir(directory))

    def test_prune_cannot_leave_a_result_diff_without_its_record(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="failed", created_at=1.0, finished_at=1.0))
            diff_path = workers._diff_path(project, record["id"])
            with open(diff_path, "wb") as stream:
                stream.write(b"old diff")
            record_path = workers._path(project, record["id"])
            original_unlink = workers.os.unlink
            recreated = False

            def unlink_with_late_publisher(path):
                nonlocal recreated
                original_unlink(path)
                if path == record_path and not recreated:
                    recreated = True
                    with open(diff_path, "wb") as stream:
                        stream.write(b"late diff")

            with patch.object(workers.os, "unlink",
                              side_effect=unlink_with_late_publisher):
                workers.prune(project, workers.TASK_TTL + 2.0)
            self.assertTrue(recreated)
            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertFalse(os.path.exists(diff_path))

    def test_prune_never_discards_a_concurrently_refined_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                created_at=1.0, started_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
            workers._finish(
                project, record["id"], "outcome_unknown", now=2.0,
                stop_resolution="unknown")
            original_retire = workers._retire_expired

            def refine_before_retirement(cwd, observed, now):
                workers._finish(
                    cwd, observed["id"], "completed", 0, now=now,
                    stop_resolution="natural")
                return original_retire(cwd, observed, now)

            now = workers.TASK_TTL + 3.0
            with patch.object(workers, "_retire_expired",
                              side_effect=refine_before_retirement):
                workers.prune(project, now)

            final = workers.read_task(project, record["id"])
            self.assertEqual(final["state"], "completed")
            self.assertEqual(final["finished_at"], now)

    def test_sweep_never_deletes_work_after_concurrent_unknown_refinement(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                created_at=1.0, started_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
            workers._finish(
                project, record["id"], "outcome_unknown", now=2.0,
                stop_resolution="unknown")
            original_retire = workers._retire_expired

            def refine_before_retirement(cwd, observed, now):
                workers._finish(
                    cwd, observed["id"], "completed", 0, now=now,
                    stop_resolution="natural")
                return original_retire(cwd, observed, now)

            now = workers.TASK_TTL + 3.0
            with patch.object(workers, "_retire_expired",
                              side_effect=refine_before_retirement):
                workers.sweep(project, now)

            final = workers.read_task(project, record["id"])
            self.assertEqual(final["state"], "completed")
            self.assertEqual(final["finished_at"], now)
            self.assertIsNone(final["collected_at"])
            self.assertTrue(os.path.isdir(directory))

    def test_cancel_returns_a_concurrently_refined_terminal_outcome(self):
        """Cleanup adopts only a same-generation refinement under its lock.

        A late authenticated publication may refine `outcome_unknown` between
        the caller's observation and cleanup. The caller must not receive the
        stale conservative state after the durable row says completed.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            os.makedirs(directory)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth",
                created_at=1.0, started_at=1.0,
                to=workers._encode_control(
                    proof=hashlib.sha256(bytes.fromhex(PROOF)).hexdigest())))
            workers._finish(
                project, record["id"], "outcome_unknown", now=2.0,
                stop_resolution="unknown")
            observed = workers.read_task(project, record["id"])
            workers._finish(
                project, observed["id"], "completed", 0, now=3.0,
                stop_resolution="natural")

            answer = workers._finish_cancel(project, record["id"], observed)

            self.assertEqual(answer["state"], "completed")
            self.assertEqual(answer["exit_code"], 0)
            self.assertEqual(workers.read_task(project, record["id"])["state"],
                             "completed")

    def test_cancel_observes_its_final_row_before_releasing_cleanup_lock(self):
        """A reused UUID cannot become the answer after cleanup linearizes."""
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]))
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="completed", exit_code=0, finished_at=2.0))
            record = workers.read_task(project, record["id"])
            cleanup_returned = []
            final_read_held = []
            real_remove = workers._remove_dir_held
            real_read = workers.read_task

            def observe_remove(cwd, observed, **kwargs):
                result = real_remove(cwd, observed, **kwargs)
                cleanup_returned.append(True)
                return result

            def observe_read(cwd, task_id):
                current = real_read(cwd, task_id)
                if cleanup_returned and not final_read_held:
                    lock_fd = os.open(
                        os.path.join(workers.tasks_dir(cwd), ".lock"),
                        os.O_RDWR)
                    try:
                        try:
                            workers.fcntl.flock(
                                lock_fd,
                                workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
                        except (BlockingIOError, OSError):
                            final_read_held.append(True)
                        else:
                            final_read_held.append(False)
                            workers.fcntl.flock(lock_fd, workers.fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                return current

            with patch.object(workers, "_remove_dir_held",
                              side_effect=observe_remove), \
                 patch.object(workers, "read_task", side_effect=observe_read):
                answer = workers._finish_cancel(project, record["id"], record)

            self.assertEqual(answer["state"], "completed")
            self.assertEqual(final_read_held, [True])

    def test_prune_retries_worker_cleanup_after_durable_record_retirement(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            directory = workers.worker_dir(project, record["id"])
            self.assertIsNotNone(
                workers._sound_dir(workers.workers_dir(project), create=True))
            os.mkdir(directory, 0o700)
            with open(os.path.join(directory, "kept"), "w", encoding="ascii") as stream:
                stream.write("evidence")
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="failed", created_at=1.0, finished_at=1.0))
            observed = workers.read_task(project, record["id"])
            now = workers.TASK_TTL + 2.0

            self.assertTrue(workers._retire_expired(project, observed, now))
            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertTrue(os.path.isdir(directory), "simulated crash before cleanup")

            workers.prune(project, now)

            self.assertFalse(os.path.exists(directory))

    def test_visible_git_cleanup_witness_requires_fresh_durability_ack(self):
        """A failed directory fsync may leave a marker visible but not durable.

        A later retirement must re-acknowledge that same marker before it
        commits the task row's absence and gives cleanup permission.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="write", sha256=SHA,
                size=1)
            self.assertIsNotNone(
                workers._sound_dir(workers.worker_dir(project, record["id"]),
                                   create=True))
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="failed", base="a" * 40, created_at=1.0,
                finished_at=1.0))
            record = workers.read_task(project, record["id"])
            directory = workers.worker_dir(project, record["id"])
            real_fsync = workers._fsync_directory
            failed = False

            def fail_marker_ack(path):
                nonlocal failed
                if os.path.realpath(path) == os.path.realpath(directory) and not failed:
                    failed = True
                    raise OSError("injected marker directory fsync failure")
                return real_fsync(path)

            now = workers.TASK_TTL + 2.0
            with patch.object(workers, "_fsync_directory",
                              side_effect=fail_marker_ack):
                self.assertFalse(workers._retire_expired(project, record, now))
            self.assertIsNotNone(workers.read_task(project, record["id"]))
            self.assertEqual(
                workers._git_cleanup_witness(project, record["id"]), "present",
                "replace was visible even though its directory ack failed")

            synced = []

            def observe(path):
                synced.append(os.path.realpath(path))
                return real_fsync(path)

            with patch.object(workers, "_fsync_directory", side_effect=observe):
                self.assertTrue(workers._retire_expired(project, record, now))

            self.assertIsNone(workers.read_task(project, record["id"]))
            self.assertIn(os.path.realpath(directory), synced,
                          "visible is re-acknowledged before the row is unlinked")

    def test_sweep_retries_git_admin_cleanup_after_record_retirement(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            import subprocess
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run([
                "git", "-C", project, "-c", "user.email=a@b",
                "-c", "user.name=a", "commit", "-q", "--allow-empty",
                "-m", "root"], check=True)
            failed = self._run(project, bin_dir, "exit 1", task_class="write")
            self._settle(project, failed["id"], "failed")
            workers.update_task(project, failed["id"], lambda changed: changed.update(
                created_at=1.0, finished_at=1.0))
            now = workers.TASK_TTL + 2.0

            def strand_git_admin(cwd, *args, **kwargs):
                if args[:2] == ("worktree", "remove"):
                    # Reproduce the dangerous half-cleanup exactly: physical
                    # work is gone, but Git still owns its admin entry.
                    shutil.rmtree(workers.work_dir(project, failed["id"]))
                    return subprocess.CompletedProcess(
                        args=["git", *args], returncode=1,
                        stdout="", stderr="injected admin failure")
                if args[:2] == ("rev-parse", "--git-common-dir"):
                    return None
                raise AssertionError(f"unexpected git call: {args!r}")

            with patch.object(workers, "_git", side_effect=strand_git_admin), \
                 patch.object(workers, "_git_checkout", return_value=False):
                workers.sweep(project, now)

            self.assertIsNone(workers.read_task(project, failed["id"]))
            self.assertTrue(os.path.isdir(workers.worker_dir(project, failed["id"])),
                            "failed Git cleanup keeps its durable retry witness")
            self.assertTrue(os.path.exists(workers.work_dir(project, failed["id"])),
                            "unreadable Git metadata authorizes no external deletion")
            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            self.assertIn(failed["id"], listed,
                          "the first cleanup could not inspect Git metadata")

            workers.sweep(project, now)

            listed = subprocess.run(
                ["git", "-C", project, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, check=True).stdout
            self.assertNotIn(failed["id"], listed,
                             "durable retirement leaves a retryable cleanup witness")
            self.assertFalse(os.path.exists(workers.worker_dir(project, failed["id"])))

    def test_an_exit_file_the_worker_wrote_is_bounded(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir, "sleep 30")
            with open(workers.exit_path(project, started["id"]), "w") as f:
                f.write("0" * 100_000)
            self.assertEqual(workers.status(project, started["id"])["state"], "running")
            workers.cancel(project, started["id"])

    def test_the_sweep_stops_a_stuck_worker_without_waiting_for_it(self):
        """Review 2026-09-03: the hook's sweep escalated with ten seconds of
        patience per worker, past the host's hook budget."""
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir,
                                "trap '' TERM; echo $$ > \"$ANTIPHON_WORKER_DIR/child.pid\"; sleep 30",
                                timeout=1)
            child = self._child_pid(project, started["id"])
            time.sleep(1.2)
            began = time.perf_counter()
            workers.sweep(project, time.time())
            self.assertLess(time.perf_counter() - began, 3.0)
            self.assertEqual(workers.read_task(project, started["id"])["state"], "timed_out")
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    os.kill(child, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    break
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_a_large_diff_is_a_path_not_a_payload(self):
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            started = self._run(project, bin_dir,
                                "head -c 400000 /dev/zero | tr '\\0' 'x' > big.txt; exit 0",
                                task_class="write")
            final = workers.result(project, started["id"], wait=10)
            self.assertEqual(final["state"], "completed")
            self.assertNotIn("diff", final)
            self.assertTrue(os.path.isfile(final["diff_path"]))
            self.assertGreater(os.path.getsize(final["diff_path"]), workers.DIFF_INLINE)
            retained = final["diff_path"]

            workers.sweep(project, time.time())
            self.assertFalse(os.path.exists(
                workers.worker_dir(project, started["id"])))
            repeated = workers.result(project, started["id"])
            self.assertEqual(repeated["state"], "completed")
            self.assertEqual(repeated["diff_path"], retained)
            self.assertTrue(os.path.isfile(retained))

    def test_the_sweep_removes_only_a_collected_directory(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            done = self._run(project, bin_dir, "exit 0")
            self._settle(project, done["id"], "completed")
            failed = self._run(project, bin_dir, "exit 1")
            self._settle(project, failed["id"], "failed")
            live = self._run(project, bin_dir, "sleep 30")
            workers.sweep(project, time.time())
            self.assertTrue(os.path.isdir(workers.worker_dir(project, done["id"])),
                            "completed but not collected: kept")
            workers.result(project, done["id"])
            workers.sweep(project, time.time())
            self.assertFalse(os.path.exists(workers.worker_dir(project, done["id"])))
            self.assertTrue(os.path.isdir(workers.worker_dir(project, failed["id"])),
                            "failed: kept for inspection until the record expires")
            self.assertTrue(os.path.isdir(workers.worker_dir(project, live["id"])))
            # A sweep dated a week ahead expires the already-failed task. The
            # running worker is terminalized now and receives its own full
            # post-terminal retention window.
            workers.sweep(project, time.time() + workers.TASK_TTL + 1)
            self.assertFalse(os.path.exists(workers.worker_dir(project, failed["id"])))
            self.assertIsNone(workers.read_task(project, failed["id"]))
            self.assertEqual(workers.read_task(project, live["id"])["state"],
                             "timed_out")
            self.assertTrue(os.path.isdir(workers.worker_dir(project, live["id"])))
            with self.assertRaises(OSError):
                os.killpg(live["pid"], 0)

    def test_the_sweep_never_touches_a_running_worker(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            live = self._run(project, bin_dir, "sleep 30")
            workers.sweep(project, time.time())
            self.assertEqual(workers.read_task(project, live["id"])["state"], "running")
            self.assertTrue(os.path.isdir(workers.worker_dir(project, live["id"])))
            workers.cancel(project, live["id"])


if __name__ == "__main__":
    unittest.main()
