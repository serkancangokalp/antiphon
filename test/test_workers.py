"""The managed-worker task store: one record per task, validated like the
ledger, kept a week, under a directory this code owns outright."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import workers

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch

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

    def test_the_store_is_refused_when_it_is_a_symlink(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as elsewhere:
            os.makedirs(os.path.join(project, ".antiphon"))
            os.symlink(elsewhere, os.path.join(project, ".antiphon", "tasks"))
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

        barrier = threading.Barrier(8, timeout=30)

        def counted_together(cwd):
            barrier.wait()
            return real_count(cwd)

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
        terminal failure.  The wrapper's held lock is positive liveness and
        stays held until the worker has written its exit code.
        """
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir, \
             patch.object(workers, "_process_start",
                          side_effect=("recorded birth", None)) as process_start:
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
                                 "status trusts the worker lock, not another `ps`")
                with open(workers.live_path(project, started["id"]), "rb") as live:
                    with self.assertRaises(BlockingIOError):
                        workers.fcntl.flock(
                            live.fileno(), workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
            finally:
                with open(release, "w", encoding="ascii"):
                    pass
            final = self._settle(project, started["id"], "completed")
            self.assertEqual(final["exit_code"], 0)

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
                    if observed is not None and observed["state"] == "failed":
                        break
                    time.sleep(0.01)
                self.assertEqual(observed["state"], "failed")
                release_gate.set()
                starter.join(5)

            self.assertFalse(starter.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], workers.Refused)
            final = workers.read_task(project, record["id"])
            self.assertIsNotNone(final)
            self.assertEqual(final["state"], "failed")

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
                    exit_file, ["/usr/bin/true"])

            self.assertEqual(code, 0)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            with open(lock_path, "rb") as stream:
                self.assertEqual(stream.read(), workers._published_marker(0))
            competing = os.open(lock_path, os.O_RDWR)
            try:
                workers.fcntl.flock(
                    competing, workers.fcntl.LOCK_EX | workers.fcntl.LOCK_NB)
            finally:
                os.close(competing)

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
                ["/bin/sh", "-c", f"echo ran > {marker!r}"])

            self.assertEqual(code, 125)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            self.assertFalse(os.path.exists(marker))
            with open(exit_file, encoding="ascii") as stream:
                self.assertEqual(stream.read(), "125\n")
            with open(lock_path, "rb") as stream:
                self.assertEqual(stream.read(), workers._published_marker(125))

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
                    os.path.join(directory, "exit"), ["/usr/bin/true"])

            self.assertEqual(code, 0)
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)
            with open(lock_path, "rb") as stream:
                self.assertEqual(stream.read(), workers._published_marker(0))

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
                 str(lock_fd), str(gate_read), str(ready_write), str(commit_read)])
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

            final = self._settle(project, started["id"], "failed")

            self.assertEqual(final["exit_code"], None)
            with open(os.path.join(workers.worker_dir(project, started["id"]),
                                   "live.lock"), encoding="ascii") as stream:
                self.assertEqual(stream.read(), "published\n")

    def test_an_adapter_descendant_cannot_replace_the_supervisor_exit(self):
        with tempfile.TemporaryDirectory() as project, \
             tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(
                project, bin_dir,
                'task_id=${ANTIPHON_WORKER_DIR##*/}; '
                'control="$ANTIPHON_CWD/.antiphon/tasks/$task_id.live"; '
                'nohup /bin/sh -c \'until grep -q "^published" "$1"; do '
                'sleep 0.01; done; printf "0\\n" > "$2"; : > "$3"\' '
                'adapter-child "$control" "$ANTIPHON_WORKER_DIR/exit" '
                '"$ANTIPHON_WORKER_DIR/overwrite-done" >/dev/null 2>&1 & '
                'exit 3')
            done = os.path.join(workers.worker_dir(project, started["id"]),
                                "overwrite-done")
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(done):
                time.sleep(0.01)
            self.assertTrue(os.path.exists(done), "the descendant replaced the exit")

            final = workers.status(project, started["id"])

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
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=os.getpid(), birth=birth,
                started_at=time.time()))
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
                workers._write_live_marker(lock_fd, workers._published_marker(0))
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

    def test_a_published_exit_does_not_wait_for_the_wrapper_to_be_reaped(self):
        """The unlocked trusted marker is the outcome publication proof."""
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
                              return_value=("published", 0)), \
                 patch.object(workers, "_worker_liveness") as liveness:
                final = workers.status(project, record["id"])

            liveness.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

    def test_a_legacy_zombie_is_finished_not_a_live_worker(self):
        """A separate live parent keeps an old shell wrapper as a zombie."""
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
                    state="running", pid=pid, birth=birth, started_at=time.time()))
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

                final = workers.status(project, record["id"])
                self.assertEqual((final["state"], final["exit_code"]),
                                 ("completed", 0))
            finally:
                parent.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    parent.wait(timeout=2)

    def test_a_legacy_worker_finishing_during_liveness_is_reconciled_from_its_exit(self):
        """An old wrapper has no live lock.  If it writes exit 0 between the
        first exit read and the liveness observation, that later evidence wins
        instead of being frozen as `failed` with no exit code.
        """
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=999_999, birth="legacy birth",
                started_at=time.time()))

            def finish_between_observations(_cwd, _record, lock=None):
                with open(workers.exit_path(project, record["id"]),
                          "w", encoding="ascii") as stream:
                    stream.write("0\n")
                return "dead"

            with patch.object(workers, "_worker_liveness",
                              side_effect=finish_between_observations):
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
                              side_effect=(("live", None), ("published", 3))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "dead")), \
                 patch.object(workers, "_kill_group") as kill:
                first = workers.status(project, record["id"], now=1.5)
                final = workers.status(project, record["id"], now=3.0)
            kill.assert_not_called()
            self.assertEqual(first["state"], "running")
            self.assertEqual((final["state"], final["exit_code"]), ("failed", 3))

    def test_a_valid_running_record_with_no_pid_still_reconciles_its_exit(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA, size=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=None, started_at=1.0))
            with open(workers.exit_path(project, record["id"]), "w", encoding="ascii") as stream:
                stream.write("0\n")
            final = workers.status(project, record["id"], now=2.0)
            self.assertEqual((final["state"], final["exit_code"]), ("completed", 0))

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

    def test_a_reused_legacy_pid_is_failed_without_signalling_the_new_owner(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            os.makedirs(workers.worker_dir(project, record["id"]), exist_ok=True)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=os.getpid(), birth="somebody else",
                started_at=1.0))
            with patch.object(workers, "_process_start", return_value="new owner"), \
                 patch.object(workers, "_kill_group") as kill:
                final = workers.status(project, record["id"], now=3.0)

            kill.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("failed", None))

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
                with patch.object(workers, "_process_start", return_value="new owner"), \
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

            with open(workers.exit_path(project, record["id"]),
                      "w", encoding="ascii") as stream:
                stream.write("0\n")
            resolved = workers.status(project, record["id"], now=10.0)
            self.assertEqual((resolved["version"], resolved["state"],
                              resolved["exit_code"]),
                             (workers.LEGACY_TASK_VERSION, "completed", 0))

    def test_an_unknown_current_worker_stays_admitted_to_the_published_reader(self):
        """The uncertainty overlay must not mint a record an in-memory 0.5.0
        reader ignores. That would let two versions disagree on the worker
        cap while the process may still be live.
        """
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
            record = workers.new_task(
                project, kind="codex", task_class="read", sha256=SHA,
                size=1, timeout=1)
            workers.update_task(project, record["id"], lambda changed: changed.update(
                state="running", pid=4242, birth="recorded birth", started_at=1.0))
            with patch.object(workers, "_worker_liveness", return_value="unknown"):
                current = workers.status(project, record["id"], now=3.0)

            published = old.read_task(project, record["id"])
            self.assertEqual((current["version"], current["state"]), (1, "running"))
            self.assertEqual((published["version"], published["state"]), (1, "running"))
            self.assertIn(record["id"], [item["id"] for item in old._admitted(project)])

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
                              side_effect=(("live", None), ("published", 0))), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group") as kill:
                final = workers.status(project, record["id"], now=3.0, patience=0.0)

            kill.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

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
                                           ("published", 0))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "dead")), \
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
                                           ("published", 3))), \
                 patch.object(workers, "_process_identity", return_value="live"), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "dead")), \
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

            self.assertEqual((final["state"], final["exit_code"]), ("failed", None))

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
                                           ("published", 0))), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group") as kill:
                final = workers.cancel(project, record["id"])

            kill.assert_not_called()
            self.assertEqual((final["state"], final["exit_code"]),
                             ("completed", 0))

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
                                           ("live", None), ("published", 0))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "dead")), \
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
                                           ("live", None), ("published", 3))), \
                 patch.object(workers, "_worker_liveness",
                              side_effect=("live", "live", "dead")), \
                 patch.object(workers, "_signal_authorized", return_value=True), \
                 patch.object(workers, "_kill_group", return_value="not_sent"):
                final = workers.cancel(project, record["id"])

            self.assertEqual((final["state"], final["exit_code"]), ("failed", 3))

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

            self.assertEqual((final["state"], final["exit_code"]), ("failed", None))

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
            with patch.object(workers.os, "makedirs", side_effect=OSError(28, "No space left")):
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

    def test_a_stale_accepted_record_is_swept(self):
        with tempfile.TemporaryDirectory() as project:
            record = workers.new_task(project, kind="codex", task_class="read", sha256=SHA, size=1)
            workers.sweep(project, time.time())
            self.assertIsNotNone(workers.read_task(project, record["id"]), "just accepted")
            workers.sweep(project, time.time() + workers.START_PATIENCE + 1)
            self.assertIsNone(workers.read_task(project, record["id"]),
                              "a start that died mid-way leaves an accepted record; swept")

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
            # A sweep dated a week ahead: the failed task's record and directory
            # expire; the running worker is first reconciled — past its timeout
            # by then, so it is killed and timed out — and only then swept.
            workers.sweep(project, time.time() + workers.TASK_TTL + 1)
            self.assertFalse(os.path.exists(workers.worker_dir(project, failed["id"])))
            self.assertIsNone(workers.read_task(project, failed["id"]))
            self.assertIsNone(workers.read_task(project, live["id"]),
                              "timed out by the reconciliation, then expired")
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
