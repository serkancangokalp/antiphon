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



class AdapterTest(unittest.TestCase):
    """One subprocess per kind, with the host's own default permission class
    and never a flag that widens it; a write task in its own worktree."""

    def _stub(self, root, kind, body="exit 0"):
        """A `claude` or `codex` on PATH that records its argv and does as told."""
        path = os.path.join(root, kind)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$0.argv\"\n"
                    "printf 'HOP=%s CWD=%s NAME=%s\\n' \"$ANTIPHON_HOP\" \"$ANTIPHON_CWD\" "
                    "\"$ANTIPHON_NAME\" >> \"$0.env\"\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def _env(self, bin_dir):
        return dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))

    def test_the_argv_per_kind_and_class_never_widens_the_permission_class(self):
        claude_read = workers.adapter("claude", "read", "do it", "t1")
        codex_read = workers.adapter("codex", "read", "do it", "t1")
        codex_write = workers.adapter("codex", "write", "do it", "t1")
        self.assertEqual(claude_read[:2], ["claude", "-p"])
        self.assertEqual(codex_read[:5], ["codex", "exec", "-s", "read-only", "--color"])
        self.assertEqual(codex_write[:4], ["codex", "exec", "-s", "workspace-write"])
        for argv in (claude_read, codex_read, codex_write,
                     workers.adapter("claude", "write", "do it", "t1")):
            joined = " ".join(argv)
            self.assertNotIn("--dangerously-skip-permissions", joined)
            self.assertNotIn("--full-auto", joined)
            self.assertNotIn("bypass", joined)
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
            argv = open(stub + ".argv").read()
            self.assertIn("exec -s read-only", argv)
            self.assertIn("review the diff", argv)
            seen = open(stub + ".env").read()
            self.assertIn("HOP=2", seen, "the record's hop, one deeper than the parent")
            self.assertIn(f"NAME=worker-{record['id'][:8]}", seen)
            self.assertIn(f"CWD={project}", seen, "a read task runs in the project")
            self.assertTrue(os.path.isdir(workers.worker_dir(project, record["id"])))
            self.assertTrue(os.path.exists(workers.log_path(project, record["id"])))

    def test_a_write_task_needs_a_git_checkout_and_gets_its_own_worktree(self):
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            self._stub(bin_dir, "codex")
            record = workers.new_task(project, kind="codex", task_class="write",
                                      sha256=SHA, size=15)
            with self.assertRaises(workers.Refused) as refused:
                workers.start(project, record, "edit it", env=self._env(bin_dir))
            self.assertIn("a write task needs a git checkout", str(refused.exception))
            self.assertEqual(workers.read_task(project, record["id"])["state"], "failed")
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            self._stub(bin_dir, "claude")
            record = workers.new_task(project, kind="claude", task_class="write",
                                      sha256=SHA, size=15)
            started = workers.start(project, record, "edit it", env=self._env(bin_dir))
            worktree = workers.worker_dir(project, record["id"])
            self.assertTrue(os.path.exists(os.path.join(worktree, ".git")), "a worktree")
            listed = subprocess.run(["git", "-C", project, "worktree", "list", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertIn(record["id"], listed)
            self.assertEqual(started["state"], "running")

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



class LifecycleTest(unittest.TestCase):
    """What becomes of a worker: seen through its exit file and its process
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
            self.assertIn("boom", open(workers.log_path(project, failed["id"])).read())

    def test_a_permission_the_class_denies_is_blocked_not_failed(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir,
                                "echo 'Bash requires approval: permission denied'; exit 2")
            record = self._settle(project, started["id"], "blocked")
            self.assertEqual(record["exit_code"], 2)

    def test_a_worker_past_its_timeout_is_killed_and_timed_out(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            started = self._run(project, bin_dir, "sleep 30", timeout=1)
            time.sleep(1.2)
            record = self._settle(project, started["id"], "timed_out", seconds=15)
            with self.assertRaises(OSError):
                os.killpg(record["pid"], 0)

    def test_cancel_kills_the_worker_and_removes_its_directory(self):
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            started = self._run(project, bin_dir, "sleep 30", task_class="write")
            directory = workers.worker_dir(project, started["id"])
            self.assertTrue(os.path.isdir(directory))
            record = workers.cancel(project, started["id"])
            self.assertEqual(record["state"], "cancelled")
            self.assertFalse(os.path.exists(directory))
            listed = subprocess.run(["git", "-C", project, "worktree", "list", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertNotIn(started["id"], listed, "the worktree is gone from git too")
            with self.assertRaises(OSError):
                os.killpg(record["pid"], 0)
            self.assertEqual(workers.cancel(project, started["id"])["state"], "cancelled",
                             "cancelling twice is one cancel")

    def test_result_waits_boundedly_and_carries_the_evidence_of_a_write_task(self):
        import subprocess
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as bin_dir:
            subprocess.run(["git", "init", "-q", project], check=True)
            subprocess.run(["git", "-C", project, "-c", "user.email=a@b", "-c", "user.name=a",
                            "commit", "-q", "--allow-empty", "-m", "root"], check=True)
            started = self._run(project, bin_dir,
                                "sleep 0.5; echo hello > made.txt; echo 'suite: 3 passed' > tests.txt; exit 0",
                                task_class="write")
            early = workers.result(project, started["id"], wait=0)
            self.assertEqual(early["state"], "running")
            self.assertNotIn("diff", early)
            final = workers.result(project, started["id"], wait=10)
            self.assertEqual(final["state"], "completed")
            self.assertIn("+hello", final["diff"])
            self.assertIn("made.txt", final["diff"])
            self.assertEqual(final["tests"], "suite: 3 passed\n")
            self.assertEqual(final["log_path"], workers.log_path(project, started["id"]))
            self.assertEqual(final["worker"]["kind"], "codex")
            self.assertEqual(final["worker"]["name"], f"worker-{started['id'][:8]}")
            self.assertIsNotNone(workers.read_task(project, started["id"])["collected_at"])
            self.assertLessEqual(workers.MAX_WAIT, 300)

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
