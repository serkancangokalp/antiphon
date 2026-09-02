"""Managed workers: one task, one worker of the other kind, one record.

`.antiphon/tasks/<task-id>.json` holds what a task is and what became of it —
its kind and class, the task text's digest and size (never the text), the
worker's pid and times, its exit — validated on every read the way the
delivery ledger is, kept a week, under a directory this code owns outright.
`.antiphon/workers/<task-id>/` is the worker's own directory: a fresh git
worktree for a write task, a log for every task.

Nothing here merges a patch, forwards a task or guesses a peer; see
docs/superpowers/specs/2026-09-03-managed-workers-design.md.
"""

import contextlib
import fcntl
import json
import os
import re
import stat
import tempfile
import time
import uuid

TASK_VERSION = 1
TASK_TTL = 7 * 24 * 3600
MAX_WORKERS = 4
DEFAULT_TIMEOUT = 900
MAX_TIMEOUT = 3600
STATES = ("accepted", "running", "completed", "failed", "cancelled",
          "timed_out", "blocked", "handed")
KINDS = ("claude", "codex")
CLASSES = ("read", "write")
MAX_TIME = float(2 ** 40)
RECORD_CEILING = 64 * 1024

TASK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
OPTIONAL_TIMES = ("started_at", "finished_at", "collected_at")
KEYS = frozenset({
    "version", "id", "kind", "task_class", "state", "sha256", "size", "parent",
    "timeout", "hop", "created_at", "pid", "birth", "exit_code", "to",
    *OPTIONAL_TIMES})


def tasks_dir(cwd):
    return os.path.join(cwd, ".antiphon", "tasks")


def workers_dir(cwd):
    return os.path.join(cwd, ".antiphon", "workers")


def _sound_dir(path, create=False):
    """The directory as one this code owns — never a link — or None."""
    parent = os.path.dirname(path)
    if os.path.islink(parent) or (os.path.exists(parent) and not os.path.isdir(parent)):
        return None
    if create:
        os.makedirs(parent, exist_ok=True)
        if not os.path.lexists(path):
            os.mkdir(path, 0o700)
    # `lstat`, never `stat`: a link to a directory elsewhere is somebody
    # else's directory, and the store's whole premise is ownership.
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    if create and info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def _time_or_none(value):
    if value is None:
        return True
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf"))
            and 0 <= value <= MAX_TIME)


def _no_duplicate_keys(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen.add(key)
    return dict(pairs)


def _int_or_none(value, floor=0):
    return value is None or (type(value) is int and value >= floor)


def _valid(record, expected_id):
    if not isinstance(record, dict) or set(record) != KEYS:
        return False
    if record["version"] != TASK_VERSION or type(record["version"]) is not int:
        return False
    if record["id"] != expected_id or not TASK_ID.fullmatch(expected_id):
        return False
    if record["kind"] not in KINDS or record["task_class"] not in CLASSES:
        return False
    if record["state"] not in STATES:
        return False
    if not (isinstance(record["sha256"], str) and SHA256_HEX.fullmatch(record["sha256"])):
        return False
    if type(record["size"]) is not int or record["size"] < 0:
        return False
    if record["parent"] is not None and not (
            isinstance(record["parent"], str) and TASK_ID.fullmatch(record["parent"])):
        return False
    if type(record["timeout"]) is not int or not 1 <= record["timeout"] <= MAX_TIMEOUT:
        return False
    if type(record["hop"]) is not int or record["hop"] < 0:
        return False
    if not _time_or_none(record["created_at"]) or record["created_at"] is None:
        return False
    if not _int_or_none(record["pid"], 1):
        return False
    if record["birth"] is not None and not (
            isinstance(record["birth"], str) and 0 < len(record["birth"]) <= 80):
        return False
    if record["exit_code"] is not None and (type(record["exit_code"]) is not int):
        return False
    if record["to"] is not None and not (isinstance(record["to"], str) and record["to"]):
        return False
    return all(_time_or_none(record[key]) for key in OPTIONAL_TIMES)


def _path(cwd, task_id):
    return os.path.join(tasks_dir(cwd), task_id + ".json")


def read_task(cwd, task_id):
    """The validated record, or None for anything else — never raised."""
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        return None
    if _sound_dir(tasks_dir(cwd)) is None:
        return None
    try:
        with open(_path(cwd, task_id), "rb") as f:
            raw = f.read(RECORD_CEILING + 1)
    except OSError:
        return None
    if len(raw) > RECORD_CEILING:
        return None
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, UnicodeDecodeError):
        return None
    return record if _valid(record, task_id) else None


def tasks(cwd):
    """Every validated record, oldest first."""
    directory = _sound_dir(tasks_dir(cwd))
    if directory is None:
        return []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    found = []
    for name in names:
        if name.endswith(".json"):
            record = read_task(cwd, name[:-5])
            if record is not None:
                found.append(record)
    found.sort(key=lambda r: (r["created_at"], r["id"]))
    return found


def _write(cwd, record):
    directory = _sound_dir(tasks_dir(cwd), create=True)
    if directory is None:
        raise OSError(f"the task store under {tasks_dir(cwd)} cannot be used")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, _path(cwd, record["id"]))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def new_task(cwd, *, kind, task_class, sha256, size, parent=None,
             timeout=DEFAULT_TIMEOUT, hop=1, to=None):
    """A fresh `accepted` record, written. Raises ValueError for a shape the
    store refuses and OSError for a store it cannot use."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if task_class not in CLASSES:
        raise ValueError(f"task must be one of {CLASSES}")
    timeout = int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT
    timeout = min(timeout, MAX_TIMEOUT)
    record = {
        "version": TASK_VERSION, "id": str(uuid.uuid4()), "kind": kind,
        "task_class": task_class, "state": "accepted", "sha256": sha256,
        "size": size, "parent": parent, "timeout": timeout, "hop": hop,
        "created_at": time.time(), "pid": None, "birth": None, "exit_code": None,
        "to": to,
        "started_at": None, "finished_at": None, "collected_at": None,
    }
    if not _valid(record, record["id"]):
        raise ValueError("the task record is not valid")
    _write(cwd, record)
    return record


@contextlib.contextmanager
def _locked(cwd):
    directory = _sound_dir(tasks_dir(cwd))
    if directory is None:
        yield False
        return
    try:
        fd = os.open(os.path.join(directory, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def update_task(cwd, task_id, mutate):
    """Read-modify-write under the store's lock; an update that breaks the
    record is refused and nothing is written."""
    with _locked(cwd):
        record = read_task(cwd, task_id)
        if record is None:
            return False
        changed = dict(record)
        mutate(changed)
        if not _valid(changed, task_id):
            return False
        if changed == record:
            return True
        try:
            _write(cwd, changed)
        except OSError:
            return False
        return True


def prune(cwd, now):
    """Drop records older than the TTL that no worker still runs under."""
    if _sound_dir(tasks_dir(cwd)) is None:
        return
    for record in tasks(cwd):
        if record["state"] == "running":
            continue
        if now - record["created_at"] > TASK_TTL:
            with contextlib.suppress(OSError):
                os.unlink(_path(cwd, record["id"]))


# ---------- the worker: one subprocess of the other kind ----------

class Refused(Exception):
    """A task this store will not start, with the reason a caller relays."""


WORKER_LABEL = "[Antiphon worker {kind}:{task_id}]"
HOP_BUDGET_DEFAULT = 1
# The permission-widening flags no worker is ever started with; named here
# so a test can pin their absence rather than trust the adapter's author.
FORBIDDEN_FLAGS = ("--dangerously-skip-permissions", "--full-auto",
                   "--dangerously-bypass-approvals-and-sandbox")


def worker_dir(cwd, task_id):
    return os.path.join(workers_dir(cwd), task_id)


def log_path(cwd, task_id):
    return os.path.join(worker_dir(cwd, task_id), "log")


def hop_budget(env):
    try:
        value = int(str(env.get("ANTIPHON_HOP_BUDGET", "")).strip())
    except ValueError:
        return HOP_BUDGET_DEFAULT
    return value if value >= 1 else HOP_BUDGET_DEFAULT


def current_hop(env):
    try:
        value = int(str(env.get("ANTIPHON_HOP", "")).strip())
    except ValueError:
        return 0
    return max(0, value)


def check_hop(env):
    """Refuse a delegation from a session already at the hop budget: the
    bridge forwards nothing on its own, so this is the only recursion there
    can be, and it is bounded here."""
    budget, hop = hop_budget(env), current_hop(env)
    if hop >= budget:
        raise Refused(f"not delegated: hop budget {budget} reached (this session is "
                      f"hop {hop}); set ANTIPHON_HOP_BUDGET to allow a bounded "
                      "deeper chain")


def running(cwd):
    return [record for record in tasks(cwd) if record["state"] == "running"]


def admit(cwd):
    """Refuse a fifth worker, naming the four that run."""
    live = running(cwd)
    if len(live) >= MAX_WORKERS:
        raise Refused(f"not delegated: {MAX_WORKERS} workers already run in this "
                      f"project ({', '.join(record['id'] for record in live)}); "
                      "wait for one, or cancel it")


def prompt_for(kind, task_id, text):
    """The task text behind one line that names the worker — the label the
    worker's words carry, which it is told not to remove."""
    label = WORKER_LABEL.format(kind=kind, task_id=task_id)
    return (f"{label} You are a managed worker started by Antiphon for one task; "
            "keep this label at the start of your final message and do not "
            "delegate further. The task:\n\n" + text)


def adapter(kind, task_class, text, task_id):
    """The argv for one worker: the host's own CLI, its default permission
    class, never a flag that widens it."""
    prompt = prompt_for(kind, task_id, text)
    if kind == "claude":
        argv = ["claude", "-p", prompt]
    else:
        sandbox = "read-only" if task_class == "read" else "workspace-write"
        argv = ["codex", "exec", "-s", sandbox, "--color", "never", prompt]
    assert not any(flag in argv for flag in FORBIDDEN_FLAGS)
    return argv


def _git_checkout(cwd):
    import subprocess
    try:
        done = subprocess.run(["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def _fail(cwd, task_id, reason):
    def mutate(changed):
        changed["state"] = "failed"
        changed["finished_at"] = time.time()
    update_task(cwd, task_id, mutate)
    raise Refused(reason)


def start(cwd, record, text, env=None):
    """Start the worker for an accepted record; returns the running record.

    A write task gets a fresh git worktree at `.antiphon/workers/<id>` (refused
    without a checkout); a read task gets a plain directory there and runs in
    the project. The worker is its own session leader, its output goes to the
    task's log, and its environment carries the hop, its name and its
    directory — never a widened permission class."""
    import subprocess
    env = dict(os.environ if env is None else env)
    task_id = record["id"]
    if _sound_dir(workers_dir(cwd), create=True) is None:
        _fail(cwd, task_id, f"not delegated: {workers_dir(cwd)} cannot be used")
    directory = worker_dir(cwd, task_id)
    if record["task_class"] == "write":
        if not _git_checkout(cwd):
            _fail(cwd, task_id, "not delegated: a write task needs a git checkout "
                                "to give its worker a worktree of its own")
        done = subprocess.run(["git", "-C", cwd, "worktree", "add", "--detach", "-q",
                               directory, "HEAD"], capture_output=True, text=True)
        if done.returncode != 0:
            _fail(cwd, task_id, "not delegated: the worker's worktree could not be "
                                f"created: {done.stderr.strip()[:200]}")
        run_in = directory
    else:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        run_in = cwd
    # The record's hop is the worker's: `delegate` computed it as the parent's
    # plus one, so a worker at the budget refuses to delegate further.
    env["ANTIPHON_HOP"] = str(record["hop"])
    env["ANTIPHON_NAME"] = f"worker-{task_id[:8]}"
    env["ANTIPHON_CWD"] = run_in
    argv = adapter(record["kind"], record["task_class"], text, task_id)
    # A shell wrapper writes the worker's exit code to the task's `exit` file:
    # the process that asks later is not the one that started it, so there
    # is no waitpid, only the file. Its own session, so a cancel or a timeout
    # reaches the whole group.
    env["ANTIPHON_WORKER_EXIT"] = exit_path(cwd, task_id)
    wrapped = ["/bin/sh", "-c", '"$@"; echo $? > "$ANTIPHON_WORKER_EXIT"', "antiphon-worker"] + argv
    log = open(log_path(cwd, task_id), "ab")
    try:
        child = subprocess.Popen(wrapped, cwd=run_in, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    except OSError as error:
        log.close()
        _fail(cwd, task_id, f"not delegated: the {record['kind']} CLI could not be "
                            f"started: {error}")
    log.close()
    birth = _process_start(child.pid)

    def mutate(changed):
        changed["state"] = "running"
        changed["pid"] = child.pid
        changed["birth"] = birth
        changed["started_at"] = time.time()
    update_task(cwd, task_id, mutate)
    return read_task(cwd, task_id)


# ---------- the lifecycle: status, result, cancel, sweep ----------

MAX_WAIT = 300
DIFF_INLINE = 256 * 1024
KILL_PATIENCE = 10.0
LOG_TAIL = 4096
# A worker that asked for a permission its class denies: the hosts' own
# words for it, in the log, beside a non-zero exit.
BLOCKED_PATTERN = re.compile(r"(?i)permission (denied|required)|requires approval|not allowed")
TERMINAL = ("completed", "failed", "cancelled", "timed_out", "blocked")


def exit_path(cwd, task_id):
    return os.path.join(worker_dir(cwd, task_id), "exit")


def _process_start(pid):
    """The process's start time as `ps` prints it, or None: with it a pid
    the system recycled is not mistaken for the worker."""
    import subprocess
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5,
                              env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    start = done.stdout.strip()
    return start[:80] if done.returncode == 0 and start else None


def _alive(record):
    pid = record["pid"]
    if not pid:
        return False
    _reap(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if record["birth"] is not None:
        return _process_start(pid) == record["birth"]
    return True


def _read_exit(cwd, task_id):
    try:
        with open(exit_path(cwd, task_id), encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    return int(token) if token.isdigit() else None


def _log_tail(cwd, task_id):
    try:
        with open(log_path(cwd, task_id), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _reap(pid):
    """Collect the wrapper if this process is its parent; a zombie leader
    makes macOS answer `killpg(pgid, 0)` with EPERM, which is "gone"."""
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def _group_gone(pid):
    _reap(pid)
    try:
        os.killpg(pid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def _kill_group(pid):
    """SIGTERM the worker's session, wait KILL_PATIENCE, then SIGKILL."""
    import signal
    for signum, patience in ((signal.SIGTERM, KILL_PATIENCE), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            _reap(pid)
            return
        deadline = time.time() + patience
        while time.time() < deadline:
            if _group_gone(pid):
                return
            time.sleep(0.05)


def _finish(cwd, task_id, state, exit_code=None, now=None):
    def mutate(changed):
        changed["state"] = state
        changed["exit_code"] = exit_code
        changed["finished_at"] = time.time() if now is None else now
    update_task(cwd, task_id, mutate)
    return read_task(cwd, task_id)


def status(cwd, task_id, now=None):
    """The task's record, reconciled with what its worker did: the exit file
    decides, a vanished process without one is a failure, a process past
    its timeout is killed and timed out. Never guessed from the log alone."""
    now = time.time() if now is None else now
    record = read_task(cwd, task_id)
    if record is None or record["state"] != "running":
        return record
    code = _read_exit(cwd, task_id)
    if code is not None:
        if code == 0:
            state = "completed"
        elif BLOCKED_PATTERN.search(_log_tail(cwd, task_id)):
            state = "blocked"
        else:
            state = "failed"
        return _finish(cwd, task_id, state, code, now)
    if not _alive(record):
        return _finish(cwd, task_id, "failed", None, now)
    if now - (record["started_at"] or now) > record["timeout"]:
        _kill_group(record["pid"])
        return _finish(cwd, task_id, "timed_out", None, now)
    return record


def _worktree_diff(cwd, task_id):
    import subprocess
    directory = worker_dir(cwd, task_id)
    try:
        subprocess.run(["git", "-C", directory, "add", "-A", "--intent-to-add"],
                       capture_output=True, timeout=30)
        done = subprocess.run(["git", "-C", directory, "diff", "--no-color"],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def result(cwd, task_id, wait=0):
    """The task's state and evidence, after waiting up to `wait` seconds
    (at most MAX_WAIT) for it to finish. A completed write task carries its
    diff — inline up to DIFF_INLINE bytes, else as a file beside the worker
    directory — and the worker's own `tests.txt` when it wrote one."""
    wait = min(max(0.0, float(wait or 0)), MAX_WAIT)
    deadline = time.time() + wait
    record = status(cwd, task_id)
    while record is not None and record["state"] == "running" and time.time() < deadline:
        time.sleep(0.1)
        record = status(cwd, task_id)
    if record is None:
        return None
    answer = {"id": task_id, "state": record["state"], "exit_code": record["exit_code"],
              "log_path": log_path(cwd, task_id), "log_tail": _log_tail(cwd, task_id),
              "worker": {"kind": record["kind"], "name": f"worker-{task_id[:8]}",
                         "directory": worker_dir(cwd, task_id),
                         "task_class": record["task_class"]}}
    if record["state"] in TERMINAL:
        if record["task_class"] == "write" and record["state"] == "completed":
            diff = _worktree_diff(cwd, task_id)
            if diff is not None:
                if len(diff) <= DIFF_INLINE:
                    answer["diff"] = diff.decode("utf-8", "replace")
                else:
                    path = worker_dir(cwd, task_id) + ".diff"
                    with open(path, "wb") as f:
                        f.write(diff)
                    answer["diff_path"] = path
        tests_path = os.path.join(worker_dir(cwd, task_id), "tests.txt")
        try:
            with open(tests_path, encoding="utf-8", errors="replace") as f:
                answer["tests"] = f.read(DIFF_INLINE)
        except OSError:
            pass
        if record["collected_at"] is None:
            update_task(cwd, task_id,
                        lambda changed: changed.update(collected_at=time.time()))
    return answer


def _remove_dir(cwd, record):
    import shutil
    import subprocess
    directory = worker_dir(cwd, record["id"])
    if record["task_class"] == "write":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["git", "-C", cwd, "worktree", "remove", "--force", directory],
                           capture_output=True, timeout=60)
            subprocess.run(["git", "-C", cwd, "worktree", "prune"],
                           capture_output=True, timeout=60)
    shutil.rmtree(directory, ignore_errors=True)
    with contextlib.suppress(OSError):
        os.unlink(directory + ".diff")


def cancel(cwd, task_id):
    """Stop the worker and remove its directory; a second cancel is one."""
    record = status(cwd, task_id)
    if record is None:
        return None
    if record["state"] == "running":
        _kill_group(record["pid"])
        record = _finish(cwd, task_id, "cancelled", None)
    elif record["state"] != "cancelled":
        return record
    _remove_dir(cwd, record)
    return record


def sweep(cwd, now):
    """Remove the directories of collected results and of expired tasks;
    never a running worker's. Then prune the records."""
    for record in tasks(cwd):
        record = status(cwd, record["id"], now) or record
        # Belt and braces: a running worker is at most MAX_TIMEOUT old
        # before status times it out, and the TTL is a week, so this arm
        # cannot be reached by an expired record — it states the rule.
        if record["state"] == "running":
            continue
        expired = now - record["created_at"] > TASK_TTL
        collected = record["state"] in ("completed", "cancelled") and record["collected_at"]
        if expired or collected:
            _remove_dir(cwd, record)
    prune(cwd, now)
