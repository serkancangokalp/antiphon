"""Managed workers: one task, one worker of the other kind, one record.

`.antiphon/tasks/<task-id>.json` holds what a task is and what became of it —
its kind and class, the task text's digest and size (never the text), the
worker's pid, start time and exit — validated on every read the way the
delivery ledger is, kept a week, under a directory this code owns outright.

`.antiphon/workers/<task-id>/` is the worker's directory. The bridge's own
files live at its top — `log`, `exit`, `diff` — and the work happens in
`work/` underneath: a detached git worktree at HEAD whenever the project is
a checkout with a commit, so nothing a worker does touches the user's own
tree (and nothing uncommitted is visible to it), and a tracked file that
happens to be named `exit` is never read as the worker's exit. The one file
the worker itself writes for the bridge, its test summary, sits inside the
worktree at `work/.antiphon/tests.txt`, where a write task's sandbox can
reach it and where git ignores it. A read task in a project that is not a
checkout runs in the project, under the host's read-only class.

A worker is followed by its task id — status, result, log. It is a peer
only where its working directory carries the bridge's configuration: a
worktree without it (the generated files are not committed) registers
nothing and appears on no page; a task run in place, or a checkout that
commits the generated files, makes it a live named peer `worker-<id8>` for
its duration. It is started with `ANTIPHON_NAME=worker-<id8>` and
`ANTIPHON_CWD=<project>`, so whatever it records lands in the project's own
store under that name.

Nothing here merges a patch, forwards a task or guesses a peer; see
docs/superpowers/specs/2026-09-03-managed-workers-design.md.
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid

TASK_VERSION = 1
TASK_TTL = 7 * 24 * 3600
MAX_WORKERS = 4
DEFAULT_TIMEOUT = 900
MAX_TIMEOUT = 3600
# Seconds an `accepted` record may wait for its `start` before a sweep
# treats it as a start that died mid-way.
START_PATIENCE = 60
STATES = ("accepted", "running", "completed", "failed", "cancelled",
          "timed_out", "blocked", "handed")
KINDS = ("claude", "codex")
CLASSES = ("read", "write")
MAX_TIME = float(2 ** 40)
RECORD_CEILING = 64 * 1024

TASK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
GIT_SHA = re.compile(r"[0-9a-f]{40}")
OPTIONAL_TIMES = ("started_at", "finished_at", "collected_at")
KEYS = frozenset({
    "version", "id", "kind", "task_class", "state", "sha256", "size", "parent",
    "timeout", "hop", "created_at", "pid", "birth", "base", "exit_code", "to",
    *OPTIONAL_TIMES})

# The bridge's own files, beside the work and never inside it — except the
# test summary, which the worker writes and its sandbox binds to the work.
LOG_FILE = "log"
EXIT_FILE = "exit"
TESTS_FILE = "tests.txt"
DIFF_FILE = "diff"
WORK_DIR = "work"
WORK_STORE = ".antiphon"


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
            # Two first delegations at once both see no store; the second
            # mkdir must not be the one that fails.
            with contextlib.suppress(FileExistsError):
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
    if record["base"] is not None and not (
            isinstance(record["base"], str) and GIT_SHA.fullmatch(record["base"])):
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


def _bounded_timeout(timeout):
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return DEFAULT_TIMEOUT
    return max(1, min(int(round(timeout)), MAX_TIMEOUT))


def new_task(cwd, *, kind, task_class, sha256, size, parent=None,
             timeout=DEFAULT_TIMEOUT, hop=1, to=None, task_id=None):
    """A fresh `accepted` record, written. Raises ValueError for a shape the
    store refuses and OSError for a store it cannot use. `task_id`, when the
    caller already put it on a message, must be a uuid."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if task_class not in CLASSES:
        raise ValueError(f"task must be one of {CLASSES}")
    if task_id is not None and not (isinstance(task_id, str) and TASK_ID.fullmatch(task_id)):
        raise ValueError("a task id is a uuid")
    record = {
        "version": TASK_VERSION, "id": task_id or str(uuid.uuid4()), "kind": kind,
        "task_class": task_class, "state": "accepted", "sha256": sha256,
        "size": size, "parent": parent, "timeout": _bounded_timeout(timeout),
        "hop": hop, "created_at": time.time(), "pid": None, "birth": None,
        "base": None, "exit_code": None, "to": to,
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


def _diff_path(cwd, task_id):
    """A completed write task's diff too large to inline, beside its record:
    evidence that outlives the worker's directory and goes with the record.
    Review 2026-09-03: it was written inside the directory that its own
    collection made sweepable, so the path the result named died on the next
    hook."""
    return os.path.join(tasks_dir(cwd), task_id + ".diff")


def _discard_record(cwd, task_id):
    for path in (_path(cwd, task_id), _diff_path(cwd, task_id)):
        with contextlib.suppress(OSError):
            os.unlink(path)


def prune(cwd, now):
    """Drop records older than the TTL that no worker still runs under."""
    if _sound_dir(tasks_dir(cwd)) is None:
        return
    for record in tasks(cwd):
        if record["state"] == "running":
            continue
        if now - record["created_at"] > TASK_TTL:
            _discard_record(cwd, record["id"])


# ---------- the worker: one subprocess of the other kind ----------

class Refused(Exception):
    """A task this store will not start, with the reason a caller relays."""


WORKER_LABEL = "[Antiphon worker {kind}:{task_id}]"
HOP_BUDGET_DEFAULT = 1
# The permission-widening flags — and the values that widen a class through
# an innocent flag — no worker is ever started with; named here so a test
# can pin their absence rather than trust the adapter's author.
FORBIDDEN_FLAGS = ("--dangerously-skip-permissions", "--full-auto",
                   "--dangerously-bypass-approvals-and-sandbox", "--yolo",
                   "danger-full-access", "bypassPermissions")


def worker_dir(cwd, task_id):
    return os.path.join(workers_dir(cwd), task_id)


def work_dir(cwd, task_id):
    return os.path.join(worker_dir(cwd, task_id), WORK_DIR)


def log_path(cwd, task_id):
    return os.path.join(worker_dir(cwd, task_id), LOG_FILE)


def exit_path(cwd, task_id):
    return os.path.join(worker_dir(cwd, task_id), EXIT_FILE)


def tests_path(cwd, task_id):
    """Inside the worktree: `codex exec -s workspace-write` binds the worker
    to its cwd, so a path beside the worktree was one it could not write
    (review 2026-09-03). Git ignores the directory, so the diff never
    carries it."""
    return os.path.join(work_dir(cwd, task_id), WORK_STORE, TESTS_FILE)


def hop_budget(env):
    try:
        value = int(str(env.get("ANTIPHON_HOP_BUDGET", "")).strip())
    except ValueError:
        return HOP_BUDGET_DEFAULT
    return value if value >= 1 else HOP_BUDGET_DEFAULT


def _hop_value(env):
    """`ANTIPHON_HOP` as the bridge set it: None when unset or blank, an int
    when it is a hop count, and the raw string when it is neither."""
    raw = str(env.get("ANTIPHON_HOP", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def current_hop(env):
    """The session's hop, 0 for a session the bridge did not start. A value
    that is not a non-negative count reads as 0 here and is refused by
    `check_hop`: a record's hop is never negative."""
    value = _hop_value(env)
    return value if isinstance(value, int) and value >= 0 else 0


def is_worker_name(alias):
    """Whether an alias is the name a worker is started under."""
    return isinstance(alias, str) and alias.startswith("worker-")


def check_hop(env, alias=None):
    """Refuse a delegation from a session already at the hop budget: the
    bridge forwards nothing on its own, so this is the only recursion there
    can be, and it is bounded here.

    Fail closed for a worker that cannot see its own hop: a host that hands
    its MCP server a curated environment (measured: Codex passes ten
    variables, and only those its config names) may drop `ANTIPHON_HOP`,
    but it forwards `ANTIPHON_NAME`, and a worker's name says what it is."""
    budget, hop = hop_budget(env), current_hop(env)
    value = _hop_value(env)
    if value is not None and not (isinstance(value, int) and value >= 0):
        raise Refused(f"not delegated: ANTIPHON_HOP={value!r} is not a hop count; "
                      "the bridge sets it to a non-negative integer, so a session "
                      "carrying anything else may not delegate")
    if is_worker_name(alias) and value is None:
        raise Refused(f"not delegated: this session is a managed worker ({alias}) "
                      "whose hop is not visible to its server, so it may not "
                      "delegate; a deeper chain needs ANTIPHON_HOP forwarded and "
                      "ANTIPHON_HOP_BUDGET raised")
    if hop >= budget:
        raise Refused(f"not delegated: hop budget {budget} reached (this session is "
                      f"hop {hop}); set ANTIPHON_HOP_BUDGET to allow a bounded "
                      "deeper chain")


def running(cwd):
    return [record for record in tasks(cwd) if record["state"] == "running"]


def _admitted(cwd):
    """What holds a slot: a worker recorded as running, and a task accepted
    whose start is under way."""
    return [record for record in tasks(cwd) if record["state"] in ("accepted", "running")]


def admit(cwd):
    """Refuse a fifth worker, naming the four that hold the slots."""
    live = _admitted(cwd)
    if len(live) >= MAX_WORKERS:
        raise Refused(f"not delegated: {MAX_WORKERS} workers already run in this "
                      f"project ({', '.join(record['id'] for record in live)}); "
                      "wait for one, or cancel it")


def accept(cwd, *, now=None, **fields):
    """Admit and record one task in a single locked step.

    Review 2026-09-03: `admit` then `new_task` with nothing between them let
    eight concurrent delegations start seven workers past a cap of four, and
    a worker that had exited still held its slot until somebody asked after
    it. So what is recorded as running is reconciled first — outside the
    lock, because reconciling writes under it — and the count and the write
    then happen under the store's lock, where nothing can slip between."""
    for record in running(cwd):
        status(cwd, record["id"], now, patience=SWEEP_PATIENCE)
    if _sound_dir(tasks_dir(cwd), create=True) is None:
        raise OSError(f"the task store under {tasks_dir(cwd)} cannot be used")
    with _locked(cwd) as held:
        if not held:
            raise OSError(f"the task store under {tasks_dir(cwd)} cannot be locked")
        admit(cwd)
        return new_task(cwd, **fields)


def prompt_for(kind, task_id, text, task_class="read", tests=None):
    """The task text behind one line that names the worker — the label the
    worker is asked to keep at the start of its final message — and, for a
    write task, the file its test summary belongs in."""
    label = WORKER_LABEL.format(kind=kind, task_id=task_id)
    head = (f"{label} You are a managed worker started by Antiphon for one task; "
            "keep this label at the start of your final message and do not "
            "delegate further.")
    if task_class == "write" and tests:
        head += (f" Work in the current directory, which is your own git worktree; "
                 f"if you run tests, write a short summary to {tests} "
                 f"({WORK_STORE}/{TESTS_FILE} inside your worktree, which git "
                 "ignores). Do not commit unless the task says so; your diff is "
                 "collected either way.")
    return head + " The task:\n\n" + text


def adapter(kind, task_class, text, task_id, tests=None):
    """The argv for one worker: the host's own CLI, its read-only class for a
    read task, its default class for a write task, never a flag that widens
    either."""
    if task_class == "write" and not tests:
        tests = os.path.join(WORK_STORE, TESTS_FILE)
    prompt = prompt_for(kind, task_id, text, task_class, tests)
    if kind == "claude":
        argv = ["claude", "-p"]
        if task_class == "read":
            argv += ["--permission-mode", "plan"]
        argv.append(prompt)
    else:
        sandbox = "read-only" if task_class == "read" else "workspace-write"
        argv = ["codex", "exec", "-s", sandbox, "--color", "never", prompt]
    assert widening(argv) is None
    return argv


def widening(argv):
    """The option, if any, that widens the worker's class — by flag or by
    value, on either side of an `=` (review 2026-09-03: `--sandbox=danger-full-
    access` is one element, and an element match saw only the two halves).
    The prompt, last, is the task's own words and is never read for one."""
    for option in argv[:-1]:
        for piece in option.split("=", 1):
            if piece in FORBIDDEN_FLAGS:
                return option
    return None


def _git(cwd, *args, timeout=60):
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _git_checkout(cwd):
    done = _git(cwd, "rev-parse", "--is-inside-work-tree", timeout=10)
    return done is not None and done.returncode == 0 and done.stdout.strip() == "true"


def _head(cwd):
    done = _git(cwd, "rev-parse", "HEAD", timeout=10)
    if done is None or done.returncode != 0:
        return None
    sha = done.stdout.strip()
    return sha if GIT_SHA.fullmatch(sha) else None


def _process_start(pid):
    """The process's start time as `ps` prints it, or None: with it a pid
    the system recycled is not mistaken for the worker."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5,
                              env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    start = done.stdout.strip()
    return start[:80] if done.returncode == 0 and start else None


def _ignored_store(work):
    """`work/.antiphon/`, ignored by git there, for the one file the worker
    writes for the bridge. Never overwrites a `.gitignore` the checkout
    itself carries at that path, and refuses a `.antiphon` the checkout
    carries as a link or a file: a link would carry the worker's file
    outside its worktree (review 2026-09-03), a file cannot hold it."""
    store = os.path.join(work, WORK_STORE)
    if os.path.islink(store) or (os.path.lexists(store) and not os.path.isdir(store)):
        raise OSError(f"{WORK_STORE} in the checkout is not a directory of the "
                      "worktree's own")
    os.makedirs(store, exist_ok=True)
    ignore = os.path.join(store, ".gitignore")
    if not os.path.lexists(ignore):
        with open(ignore, "w", encoding="utf-8") as f:
            f.write("*\n")


def _refuse(cwd, record, reason):
    """A start that did not happen leaves nothing: not the record, not the
    worktree it may have created."""
    _remove_dir(cwd, record)
    _discard_record(cwd, record["id"])
    raise Refused(reason)


def start(cwd, record, text, env=None):
    """Start the worker for an accepted record; returns the running record.

    The work happens in a detached git worktree under the worker's directory
    whenever the project is a checkout — a write task needs one and is
    refused without; a read task in a plain directory runs in the project
    under the host's read-only class. The worker is its own session leader,
    its output goes to the task's log beside the work, and its environment
    carries the hop, its name and its directories — never a widened
    permission class. A refusal leaves no record."""
    env = dict(os.environ if env is None else env)
    task_id = record["id"]
    if _sound_dir(workers_dir(cwd), create=True) is None:
        _refuse(cwd, record, f"not delegated: {workers_dir(cwd)} cannot be used")
    directory = worker_dir(cwd, task_id)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as error:
        _refuse(cwd, record, f"not delegated: the worker's directory could not be "
                             f"created: {error}")
    work = work_dir(cwd, task_id)
    base = None
    # A checkout with no commit yet (measured: the E2E's own `git init`
    # project) has nothing to branch a worktree from: a read task runs in
    # the project under the host's read-only class, a write task is refused.
    if _git_checkout(cwd) and _head(cwd) is None:
        if record["task_class"] == "write":
            _refuse(cwd, record, "not delegated: a write task needs a commit to "
                                 "branch its worker's worktree from")
        run_in = cwd
    elif _git_checkout(cwd):
        done = _git(cwd, "worktree", "add", "--detach", "-q", work, "HEAD")
        if done is None or done.returncode != 0:
            _refuse(cwd, record, "not delegated: the worker's worktree could not be "
                                 f"created: {(done.stderr if done else '').strip()[:200]}")
        base = _head(work)
        run_in = work
        try:
            _ignored_store(work)
        except OSError as error:
            # A refusal leaves no record, no directory, no worktree entry:
            # measured before this, a tracked file named `.antiphon` raised
            # out of here with all three in place (review 2026-09-03).
            _refuse(cwd, record, f"not delegated: the worker's store could not be "
                                 f"made in its worktree: {error}")
    elif record["task_class"] == "write":
        _refuse(cwd, record, "not delegated: a write task needs a git checkout "
                             "to give its worker a worktree of its own")
    else:
        run_in = cwd
    # The record's hop is the worker's: `delegate` computed it as the parent's
    # plus one, so a worker at the budget refuses to delegate further. Its
    # bridge directory is the project, not the throwaway worktree: a bridge
    # call it makes must land in the project's own store, not in one deleted
    # with the work (review 2026-09-03).
    env["ANTIPHON_HOP"] = str(record["hop"])
    env["ANTIPHON_NAME"] = f"worker-{task_id[:8]}"
    env["ANTIPHON_CWD"] = cwd
    env["ANTIPHON_WORKER_DIR"] = directory
    env["ANTIPHON_WORKER_TESTS"] = tests_path(cwd, task_id)
    env["ANTIPHON_WORKER_EXIT"] = exit_path(cwd, task_id)
    argv = adapter(record["kind"], record["task_class"], text, task_id,
                   tests=tests_path(cwd, task_id))
    # A shell wrapper writes the worker's exit code to the task's exit file:
    # the process that asks later is not the one that started it, so there
    # is no waitpid, only the file. Its own session, so a cancel or a timeout
    # reaches the whole group.
    wrapped = ["/bin/sh", "-c", '"$@"; echo $? > "$ANTIPHON_WORKER_EXIT"',
               "antiphon-worker"] + argv
    try:
        log = open(log_path(cwd, task_id), "ab")
    except OSError as error:
        _refuse(cwd, record, f"not delegated: the worker's log could not be opened: {error}")
    try:
        child = subprocess.Popen(wrapped, cwd=run_in, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    except OSError as error:
        log.close()
        _refuse(cwd, record, f"not delegated: the {record['kind']} CLI could not be "
                             f"started: {error}")
    log.close()
    birth = _process_start(child.pid)
    # Detached on purpose: the exit file, not waitpid, is the answer, and a
    # Popen left with no return code warns at shutdown as if forgotten.
    child.returncode = 0

    def mutate(changed):
        changed["state"] = "running"
        changed["pid"] = child.pid
        changed["birth"] = birth
        changed["base"] = base
        changed["started_at"] = time.time()
    if not update_task(cwd, task_id, mutate):
        _kill_group(child.pid, 0.5)
        _refuse(cwd, record, "not delegated: the task record could not be updated")
    return read_task(cwd, task_id)


# ---------- the lifecycle: status, result, cancel, sweep ----------

MAX_WAIT = 300
DIFF_INLINE = 256 * 1024
KILL_PATIENCE = 10.0
# What a sweep may spend on a stuck worker: it runs on the host's prompt
# hook, whose budget is a minute for everything.
SWEEP_PATIENCE = 0.25
LOG_TAIL = 4096
EXIT_CEILING = 16
# A worker that asked for a permission its class denies: the hosts' own
# words for it, in the log, beside a non-zero exit.
BLOCKED_PATTERN = re.compile(r"(?i)permission (denied|required)|requires approval|not allowed")
TERMINAL = ("completed", "failed", "cancelled", "timed_out", "blocked")


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
    # No start time on record — `ps` gave none when the worker began — so a
    # live pid is taken as the worker: the alternative reads a running
    # worker as dead on the one platform that cannot date it, and a recycled
    # pid costs at most a late `failed`.
    return True


def _read_exit(cwd, task_id):
    """The worker's exit code from its exit file, or None: bounded, digits
    only — the file's path is in the worker's environment, and what a worker
    may write there is not a code."""
    try:
        with open(exit_path(cwd, task_id), "rb") as f:
            raw = f.read(EXIT_CEILING + 1)
    except OSError:
        return None
    token = raw.decode("ascii", "replace").strip()
    if len(raw) > EXIT_CEILING or not token.isdigit() or len(token) > 3:
        return None
    return int(token)


def _log_tail(cwd, task_id):
    try:
        with open(log_path(cwd, task_id), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _kill_group(pid, patience=KILL_PATIENCE):
    """SIGTERM the worker's session, wait `patience`, then SIGKILL."""
    for signum, wait in ((signal.SIGTERM, patience), (signal.SIGKILL, min(2.0, patience + 0.5))):
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            _reap(pid)
            return
        deadline = time.time() + wait
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


def status(cwd, task_id, now=None, patience=KILL_PATIENCE):
    """The task's record, reconciled with what its worker did: the exit file
    decides, a vanished process without one is a failure, a process past
    its timeout is killed (with `patience` before the SIGKILL) and timed
    out. Never guessed from the log alone."""
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
        _kill_group(record["pid"], patience)
        return _finish(cwd, task_id, "timed_out", None, now)
    return record


def _worktree_diff(cwd, record):
    """Everything the worker changed against the base it started from —
    committed, staged or loose — as bytes, or None when it cannot be told."""
    work = work_dir(cwd, record["id"])
    base = record["base"]
    if base is None or not os.path.isdir(work):
        return None
    # The bridge's own store is excluded by pathspec, not by the `.gitignore`
    # written there: a checkout that tracks a `.antiphon/.gitignore` of its
    # own would otherwise carry the test summary into the diff.
    outside_store = ("--", ".", f":!{WORK_STORE}")
    if _git(work, "add", "-A", "--intent-to-add", *outside_store, timeout=30) is None:
        return None
    done = _git(work, "diff", "--no-color", base, *outside_store, timeout=60)
    if done is None or done.returncode != 0:
        return None
    return done.stdout.encode("utf-8", "surrogateescape") if isinstance(done.stdout, str) \
        else done.stdout


def _bounded_wait(wait):
    """`wait` as seconds between 0 and MAX_WAIT; anything else is 0 — a
    bool included, which `float` would read as a second."""
    if isinstance(wait, bool):
        return 0.0
    try:
        seconds = float(wait or 0)
    except (TypeError, ValueError):
        return 0.0
    if seconds != seconds:
        return 0.0
    return min(max(0.0, seconds), float(MAX_WAIT))


def _handed(record, verb):
    """The refusal for a lifecycle action on a task that has no worker here:
    it was handed to a peer, whose work is its own."""
    return Refused(f"not {verb}: task {record['id']} was handed to the {record['kind']} "
                   f"peer {record['to']!r} and has no worker here; its answer comes in "
                   "the peer's own words (antiphon status shows the receipt), and only "
                   "the peer can be told to stop")


def result(cwd, task_id, wait=0):
    """The task's state and evidence, after waiting up to `wait` seconds
    (at most MAX_WAIT) for it to finish. A completed write task carries its
    diff against the base it started from — inline up to DIFF_INLINE bytes,
    else as a file beside the task's record, which outlives the work — and
    the worker's test summary when it wrote one. The task counts as collected only when the evidence its state
    promises was actually returned; until then its directory is kept. A
    handed task is refused: nothing is collected by id."""
    wait = _bounded_wait(wait)
    deadline = time.time() + wait
    record = status(cwd, task_id)
    if record is not None and record["state"] == "handed":
        raise _handed(record, "collected")
    while record is not None and record["state"] == "running" and time.time() < deadline:
        time.sleep(0.1)
        record = status(cwd, task_id)
    if record is None:
        return None
    answer = {"id": task_id, "state": record["state"], "exit_code": record["exit_code"],
              "log_path": log_path(cwd, task_id), "log_tail": _log_tail(cwd, task_id),
              "worker": {"kind": record["kind"], "name": f"worker-{task_id[:8]}",
                         "directory": worker_dir(cwd, task_id),
                         "work": work_dir(cwd, task_id) if record["base"] else None,
                         "task_class": record["task_class"]}}
    if record["state"] not in TERMINAL:
        return answer
    evidence = True
    if record["task_class"] == "write" and record["state"] == "completed":
        diff = _worktree_diff(cwd, record)
        if diff is None:
            evidence = False
            answer["diff_missing"] = ("the diff could not be produced; the work is kept "
                                      f"at {work_dir(cwd, task_id)}")
        elif len(diff) <= DIFF_INLINE:
            answer["diff"] = diff.decode("utf-8", "replace")
        else:
            path = _diff_path(cwd, task_id)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(diff)
            answer["diff_path"] = path
    try:
        with open(tests_path(cwd, task_id), encoding="utf-8", errors="replace") as f:
            answer["tests"] = f.read(DIFF_INLINE)
    except OSError:
        pass
    if evidence and record["collected_at"] is None:
        update_task(cwd, task_id, lambda changed: changed.update(collected_at=time.time()))
    return answer


def _forget_worktree(cwd, work):
    """Drop git's own entry for this worktree when its directory is already
    gone — what `git worktree prune` does for every missing worktree of the
    user's repository, done for ours alone (review 2026-09-03)."""
    done = _git(cwd, "rev-parse", "--git-common-dir", timeout=10)
    if done is None or done.returncode != 0:
        return
    common = done.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    admin = os.path.join(common, "worktrees")
    try:
        names = os.listdir(admin)
    except OSError:
        return
    wanted = os.path.realpath(os.path.join(work, ".git"))
    for name in names:
        entry = os.path.join(admin, name)
        try:
            with open(os.path.join(entry, "gitdir"), encoding="utf-8", errors="replace") as f:
                registered = f.read().strip()
        except OSError:
            continue
        if registered and os.path.realpath(registered) == wanted:
            shutil.rmtree(entry, ignore_errors=True)


def _remove_dir(cwd, record):
    directory = worker_dir(cwd, record["id"])
    work = work_dir(cwd, record["id"])
    if os.path.isdir(work):
        _git(cwd, "worktree", "remove", "--force", work)
    shutil.rmtree(directory, ignore_errors=True)
    if _git_checkout(cwd):
        _forget_worktree(cwd, work)


def cancel(cwd, task_id):
    """Stop a running worker and remove its directory; on a finished task,
    remove the directory and keep the state. A second cancel is one. A
    handed task is refused: there is no worker here to stop."""
    record = status(cwd, task_id)
    if record is None:
        return None
    if record["state"] == "handed":
        raise _handed(record, "cancelled")
    if record["state"] == "running":
        _kill_group(record["pid"])
        record = _finish(cwd, task_id, "cancelled", None)
    _remove_dir(cwd, record)
    return record


def sweep(cwd, now):
    """Remove the directories of collected results and of expired tasks,
    never a running worker's; stop a worker past its timeout without waiting
    for it; drop an `accepted` record whose start died. Then prune."""
    for record in tasks(cwd):
        if record["state"] == "accepted":
            if now - record["created_at"] > START_PATIENCE:
                _remove_dir(cwd, record)
                _discard_record(cwd, record["id"])
            continue
        record = status(cwd, record["id"], now, patience=SWEEP_PATIENCE) or record
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
