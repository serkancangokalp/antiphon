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
            with open(path) as f:
                self.assertNotIn("review the diff", f.read())
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
                             "the control files sit beside the work, never inside it")

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
            with open(workers.log_path(project, failed["id"])) as f:
                self.assertIn("boom", f.read())

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
