"""Managed workers: one task, one worker of the other kind, one record.

`.antiphon/tasks/<task-id>.json` holds what a task is and what became of it —
its kind and class, the task text's digest and size (never the text), the
worker's pid, start time and exit — validated on every read the way the
delivery ledger is, retained for a week after it becomes terminal (and kept
while its liveness is uncertain), under a directory this code owns outright.

`.antiphon/workers/<task-id>/` is the worker's directory. The bridge's
worker-visible files `log` and `exit` live at its top. The supervisor's lock
sits beside the trusted task record as `.antiphon/tasks/<task-id>.live`,
outside the adapter's writable directory; a diff too large to inline sits
there as `.antiphon/tasks/<task-id>.diff` — and the work happens in
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
import errno
import fcntl
import json
import math
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import peers

LEGACY_TASK_VERSION = 1
TASK_VERSION = 2
TASK_TTL = 7 * 24 * 3600
MAX_WORKERS = 4
DEFAULT_TIMEOUT = 900
MAX_TIMEOUT = 3600
# Seconds an `accepted` record may wait for its `start` before a sweep
# treats it as a start that died mid-way.
START_PATIENCE = 60
# A new wrapper waits behind its admission pipe while its exact process birth
# is sampled. Without that identity Antiphon could keep it live, but could
# never safely authorize timeout or cancellation signals after a pid reuse.
START_IDENTITY_PATIENCE = 0.5
START_ACTIVE_PATIENCE = 1.0
LEGACY_STATES = ("accepted", "running", "completed", "failed", "cancelled",
                 "timed_out", "blocked", "handed")
V2_STATES = ("handing", "tracking_incomplete", "delivery_refused")
STATES = LEGACY_STATES + V2_STATES
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

# The worker-visible log and legacy-reader exit mirror sit beside the work;
# the trusted live marker sits with the task record; only the test summary is
# inside the worktree, where the worker's sandbox binds it.
LOG_FILE = "log"
EXIT_FILE = "exit"
LIVE_SUFFIX = ".live"
LIVE_STARTING = b"starting\n"
LIVE_ACTIVE = b"active\n"
LIVE_PUBLISHED = b"published:"
LIVE_CEILING = 16
TESTS_FILE = "tests.txt"
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


def _utf8_string(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid(record, expected_id):
    if not isinstance(record, dict) or set(record) != KEYS:
        return False
    version = record["version"]
    if type(version) is not int or version not in (LEGACY_TASK_VERSION, TASK_VERSION):
        return False
    if record["id"] != expected_id or not TASK_ID.fullmatch(expected_id):
        return False
    if record["kind"] not in KINDS or record["task_class"] not in CLASSES:
        return False
    allowed_states = LEGACY_STATES if version == LEGACY_TASK_VERSION else V2_STATES
    if record["state"] not in allowed_states:
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
            _utf8_string(record["birth"]) and 0 < len(record["birth"]) <= 80):
        return False
    if record["base"] is not None and not (
            isinstance(record["base"], str) and GIT_SHA.fullmatch(record["base"])):
        return False
    if record["exit_code"] is not None and (type(record["exit_code"]) is not int):
        return False
    if record["to"] is not None and not (
            _utf8_string(record["to"]) and record["to"]):
        return False
    if record["state"] in ("handing", "handed", "tracking_incomplete",
                           "delivery_refused"):
        if not peers.valid_name(record["to"]):
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
    except Exception:       # noqa: BLE001 — malformed task state is never control flow
        # A few kilobytes of nested arrays can raise RecursionError even below
        # RECORD_CEILING. Match the delivery ledger's fail-closed reader: any
        # decoder failure makes this one record invalid and never escapes into
        # status, doctor, pruning, or handoff read-back.
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
    except Exception:
        # Validation keeps normal records serializable, but a serializer or
        # encoding failure must not strand a temporary file in the owned task
        # directory. Preserve the original exception for the caller.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _bounded_timeout(timeout):
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or (isinstance(timeout, float) and not math.isfinite(timeout))
            or timeout <= 0):
        return DEFAULT_TIMEOUT
    return max(1, min(int(round(timeout)), MAX_TIMEOUT))


def new_task(cwd, *, kind, task_class, sha256, size, parent=None,
             timeout=DEFAULT_TIMEOUT, hop=1, to=None, task_id=None,
             state="accepted"):
    """A fresh `accepted` worker or `handing` peer record, written.

    Raises ValueError for a shape the store refuses and OSError for a store it
    cannot use. `task_id`, when the caller already put it on a message, must be
    a uuid. A hand-off is prepared separately so it consumes no worker slot and
    cannot be mistaken for a worker whose start died.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if task_class not in CLASSES:
        raise ValueError(f"task must be one of {CLASSES}")
    if state not in ("accepted", "handing"):
        raise ValueError("a new task must be accepted or handing")
    if state == "handing" and not to:
        raise ValueError("a handing task must name its peer")
    if task_id is not None and not (isinstance(task_id, str) and TASK_ID.fullmatch(task_id)):
        raise ValueError("a task id is a uuid")
    record = {
        "version": (TASK_VERSION if state in V2_STATES else LEGACY_TASK_VERSION),
        "id": task_id or str(uuid.uuid4()), "kind": kind,
        "task_class": task_class, "state": state, "sha256": sha256,
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
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def update_task(cwd, task_id, mutate):
    """Read-modify-write under the store's lock; an update that breaks the
    record is refused and nothing is written."""
    with _locked(cwd) as held:
        if not held:
            return False
        record = read_task(cwd, task_id)
        if record is None:
            return False
        changed = dict(record)
        mutate(changed)
        changed["version"] = (
            TASK_VERSION if changed.get("state") in V2_STATES
            else LEGACY_TASK_VERSION)
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
    """Remove one task's durable files and say whether all are absent.

    Callers that report a refused peer hand-off need this answer: silently
    ignoring an unlink failure leaves a `handing` record that looks like an
    outcome nobody actually knows.
    """
    # Ancillary evidence goes first and the state-bearing JSON goes last. If
    # any earlier unlink fails, callers can still turn that record into an
    # explicit `delivery_refused` instead of being left with an unreadable id.
    paths = (_diff_path(cwd, task_id), live_path(cwd, task_id),
             _path(cwd, task_id))
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return not any(os.path.lexists(path) for path in paths)


def prune(cwd, now):
    """Drop records older than the TTL that no worker still runs under."""
    if _sound_dir(tasks_dir(cwd)) is None:
        return
    for record in tasks(cwd):
        if record["state"] == "running":
            continue
        if now - record["created_at"] > TASK_TTL:
            _discard_record(cwd, record["id"])
    _prune_orphan_live(cwd, now)


def _prune_orphan_live(cwd, now):
    """Bound old-reader orphan locks without ever unlinking a held one."""
    directory = _sound_dir(tasks_dir(cwd))
    if directory is None:
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    candidates = []
    for name in names:
        if not name.endswith(LIVE_SUFFIX):
            continue
        task_id = name[:-len(LIVE_SUFFIX)]
        if TASK_ID.fullmatch(task_id):
            candidates.append((task_id, os.path.join(directory, name)))
    with _locked(cwd) as held:
        if not held:
            return
        for task_id, path in candidates:
            if read_task(cwd, task_id) is not None:
                continue
            flags = os.O_RDWR | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode)
                        or now - info.st_mtime <= TASK_TTL):
                    continue
                try:
                    named = os.lstat(path)
                except OSError:
                    continue
                if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                    continue
                with contextlib.suppress(OSError):
                    os.unlink(path)
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


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


def live_path(cwd, task_id):
    """The supervisor-owned lock, outside the adapter's writable directory."""
    return os.path.join(tasks_dir(cwd), task_id + LIVE_SUFFIX)


def _write_live_marker(fd, marker):
    """Replace the bounded state inside an already-open regular lock file."""
    if (marker not in (LIVE_STARTING, LIVE_ACTIVE)
            and _published_code(marker) is None):
        raise ValueError("unknown live-lock marker")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    remaining = memoryview(marker)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("the live-lock marker could not be written")
        remaining = remaining[written:]
    os.fsync(fd)


def _read_live_marker(fd):
    """The bounded marker on an open live lock, or None."""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIVE_CEILING:
            return None
        os.lseek(fd, 0, os.SEEK_SET)
        marker = os.read(fd, LIVE_CEILING + 1)
    except OSError:
        return None
    if marker in (LIVE_STARTING, LIVE_ACTIVE) or _published_code(marker) is not None:
        return marker
    return None


def _published_marker(code):
    """The supervisor-only terminal marker carrying one shell exit status."""
    if type(code) is not int or not 0 <= code <= 255:
        raise ValueError("an exit status must be between 0 and 255")
    return LIVE_PUBLISHED + str(code).encode("ascii") + b"\n"


def _published_code(marker):
    """The exit status bound into a complete terminal marker, or None."""
    if not isinstance(marker, bytes) or not marker.startswith(LIVE_PUBLISHED):
        return None
    raw = marker[len(LIVE_PUBLISHED):]
    if not raw.endswith(b"\n"):
        return None
    token = raw[:-1]
    if not token or not token.isdigit() or len(token) > 3:
        return None
    code = int(token)
    return code if 0 <= code <= 255 else None


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
    # The store's own creation can raise as well as return None (a full or
    # read-only disk); either way the start did not happen and leaves
    # nothing. Measured at the release gate, round 3: the raise escaped.
    try:
        usable = _sound_dir(workers_dir(cwd), create=True)
    except OSError as error:
        usable, why = None, f": {error}"
    else:
        why = ""
    if usable is None:
        _refuse(cwd, record, f"not delegated: {workers_dir(cwd)} cannot be used{why}")
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
    argv = adapter(record["kind"], record["task_class"], text, task_id,
                   tests=tests_path(cwd, task_id))
    # The wrapper inherits an already-held task-store lock and releases it
    # only after binding the exit code into its terminal marker. A later
    # process can therefore prove liveness and outcome without a racy `ps`
    # observation or worker-writable result. It also waits on a pipe:
    # the adapter starts only after the running record is durable; EOF aborts
    # it. The adapter inherits neither control descriptor. Old workers without
    # this file retain the pid/birth reader below for rolling compatibility.
    lock_fd = None
    gate_read = None
    gate_write = None
    ready_read = None
    ready_write = None
    commit_read = None
    commit_write = None
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(live_path(cwd, task_id), flags, 0o600)
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_live_marker(lock_fd, LIVE_STARTING)
        gate_read, gate_write = os.pipe()
        ready_read, ready_write = os.pipe()
        commit_read, commit_write = os.pipe()
    except OSError as error:
        for fd in (gate_read, gate_write, ready_read, ready_write,
                   commit_read, commit_write, lock_fd):
            if fd is None:
                continue
            with contextlib.suppress(OSError):
                os.close(fd)
        _refuse(cwd, record, f"not delegated: the worker's live lock could not be "
                             f"created: {error}")
    wrapped = [sys.executable, os.path.abspath(__file__), "_worker_wrapper",
               str(lock_fd), str(gate_read), str(ready_write), str(commit_read),
               exit_path(cwd, task_id)] + argv
    try:
        log = open(log_path(cwd, task_id), "ab")
    except OSError as error:
        for fd in (gate_read, gate_write, ready_read, ready_write,
                   commit_read, commit_write, lock_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
        _refuse(cwd, record, f"not delegated: the worker's log could not be opened: {error}")
    try:
        child = subprocess.Popen(wrapped, cwd=run_in, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True,
                                 pass_fds=(lock_fd, gate_read, ready_write, commit_read))
    except OSError as error:
        log.close()
        for fd in (gate_read, gate_write, ready_read, ready_write,
                   commit_read, commit_write, lock_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
        _refuse(cwd, record, f"not delegated: the {record['kind']} CLI could not be "
                             f"started: {error}")
    log.close()
    os.close(lock_fd)
    os.close(gate_read)
    os.close(ready_write)
    os.close(commit_read)

    def abort_unadmitted():
        """Close the gate and reap its wrapper; no adapter can have started."""
        nonlocal gate_write, ready_read, commit_write
        if gate_write is not None:
            with contextlib.suppress(OSError):
                os.close(gate_write)
            gate_write = None
        if ready_read is not None:
            with contextlib.suppress(OSError):
                os.close(ready_read)
            ready_read = None
        if commit_write is not None:
            with contextlib.suppress(OSError):
                os.close(commit_write)
            commit_write = None
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            _kill_group(child.pid, 0.1)
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=0.5)

    birth = _process_start(child.pid)
    identity_deadline = time.time() + START_IDENTITY_PATIENCE
    while birth is None and time.time() < identity_deadline:
        time.sleep(0.02)
        birth = _process_start(child.pid)
    if birth is None:
        abort_unadmitted()
        _refuse(cwd, record, "not delegated: the worker's process identity "
                             "could not be recorded")

    def mutate(changed):
        if changed["state"] != "accepted":
            return
        changed["state"] = "running"
        changed["pid"] = child.pid
        changed["birth"] = birth
        changed["base"] = base
        changed["started_at"] = time.time()
    updated = update_task(cwd, task_id, mutate)
    started = read_task(cwd, task_id)
    if not (updated and started is not None and started["state"] == "running"
            and started["pid"] == child.pid):
        # EOF is the abort token. The wrapper has not started the adapter and
        # never will, so cleanup cannot erase a live task even if the wrapper
        # itself takes a moment to notice the closed pipe.
        abort_unadmitted()
        if started is None or started["state"] == "accepted":
            _refuse(cwd, record, "not delegated: the task record could not be updated")
        raise Refused(
            "not delegated: the task start was superseded before its adapter was admitted")
    try:
        os.write(gate_write, b"1")
    except OSError:
        # No byte means the adapter was never admitted. Preserve refusal's
        # no-record/no-work contract instead of claiming a task was started.
        # A concurrent reconciler may already have recorded the wrapper's
        # terminal failure, though; that durable outcome must not disappear.
        abort_unadmitted()
        current = read_task(cwd, task_id)
        if current is not None and not (
                current["state"] == "running" and current["pid"] == child.pid):
            raise Refused(
                "not delegated: the worker start gate failed after another "
                "lifecycle outcome was recorded")
        _refuse(cwd, record, "not delegated: the worker start gate could not be released")
    finally:
        if gate_write is not None:
            with contextlib.suppress(OSError):
                os.close(gate_write)
            gate_write = None
    ready = False
    try:
        readable, _writable, _exceptional = select.select(
            [ready_read], [], [], START_ACTIVE_PATIENCE)
        ready = bool(readable) and os.read(ready_read, 1) == b"1"
    except (OSError, ValueError):
        ready = False
    finally:
        if ready_read is not None:
            with contextlib.suppress(OSError):
                os.close(ready_read)
            ready_read = None
    if not ready:
        # The wrapper waits for the separate commit pipe after READY, so even
        # an acknowledgement racing this timeout cannot admit the adapter.
        abort_unadmitted()
        current = read_task(cwd, task_id)
        if current is not None and not (
                current["state"] == "running" and current["pid"] == child.pid):
            raise Refused(
                "not delegated: worker activation failed after another "
                "lifecycle outcome was recorded")
        _refuse(cwd, record, "not delegated: the worker did not acknowledge its start")
    try:
        os.write(commit_write, b"1")
    except OSError:
        abort_unadmitted()
        current = read_task(cwd, task_id)
        if current is not None and not (
                current["state"] == "running" and current["pid"] == child.pid):
            raise Refused(
                "not delegated: worker activation failed after another "
                "lifecycle outcome was recorded")
        _refuse(cwd, record, "not delegated: the worker start commit failed")
    finally:
        if commit_write is not None:
            with contextlib.suppress(OSError):
                os.close(commit_write)
            commit_write = None
    # Detached on purpose: the published marker, not waitpid, is the answer,
    # and a Popen left with no return code warns at shutdown as if forgotten.
    child.returncode = 0
    return started


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


def _reap(pid, patience=0.0):
    """Collect the wrapper if this process is its parent, optionally waiting."""
    if type(pid) is not int or pid <= 0:
        return
    deadline = time.time() + patience
    while True:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError, OverflowError, ValueError):
            return
        if reaped or time.time() >= deadline:
            return
        time.sleep(0.01)


def _group_liveness(pid):
    """`live`, `dead`, or `unknown` for the worker's process group."""
    if not pid:
        return "dead"
    _reap(pid)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except (OverflowError, ValueError):
        return "unknown"
    return "live"


def _process_state(pid):
    """The kernel state letters for one pid, `absent`, or None if unreadable."""
    try:
        done = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5,
                              env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    state = done.stdout.strip().split(None, 1)[0] if done.stdout.strip() else ""
    if done.returncode == 0 and state:
        return state[:16]
    return "absent" if done.returncode != 0 and not state else None


def _group_process_liveness(pgid):
    """Use process-table states to distinguish a zombie-only process group."""
    try:
        done = subprocess.run(["ps", "-axo", "pgid=,stat="],
                              capture_output=True, text=True, timeout=5,
                              env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if done.returncode != 0:
        return "unknown"
    states = []
    for line in done.stdout.splitlines():
        pieces = line.split(None, 1)
        if len(pieces) != 2:
            continue
        try:
            member_group = int(pieces[0])
        except ValueError:
            continue
        if member_group == pgid:
            states.append(pieces[1])
    if not states or all(state.startswith("Z") for state in states):
        return "dead"
    return "live"


def _group_gone(pid):
    """Positive absence for a process group; permission proves nothing."""
    return _group_liveness(pid) == "dead"


def _process_identity(record):
    """`live`, `zombie`, `absent`, `recycled`, or `unknown` for one pid."""
    pid = record["pid"]
    if not pid:
        return "absent"
    _reap(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "unknown"
    except (OverflowError, ValueError):
        return "unknown"
    if record["birth"] is None:
        return "unknown"
    observed = _process_start(pid)
    if observed is None:
        return "unknown"
    if observed != record["birth"]:
        return "recycled"
    state = _process_state(pid)
    if state is None:
        return "unknown"
    if state == "absent":
        return "absent"
    return "zombie" if state.startswith("Z") else "live"


def _process_liveness(record):
    """`live`, `dead`, or `unknown` from a legacy pid/birth observation."""
    identity = _process_identity(record)
    if identity == "live":
        return "live"
    if identity in ("zombie", "absent", "recycled"):
        return "dead"
    return "unknown"


def _alive(record):
    """Compatibility boolean for callers that only need positive proof."""
    return _process_liveness(record) == "live"


def _lock_observation(cwd, task_id):
    """One lock's lifecycle state and its bound published exit, if any.

    The process that created a pre-lock task has no file, which is distinct
    from a current task whose owned lock cannot be inspected. A held lock is
    live. Once acquired, its marker distinguishes a wrapper that atomically
    published an exit from one that died before doing so.
    """
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(live_path(cwd, task_id), flags)
    except FileNotFoundError:
        return "legacy", None
    except OSError:
        return "unknown", None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Marker writes are truncate-then-write transitions. Only a
            # complete ACTIVE marker authorizes an action; STARTING and
            # partial bytes suppress signals before or between phases.
            marker = _read_live_marker(fd)
            if marker == LIVE_ACTIVE:
                return "live", None
            if _published_code(marker) is not None:
                return "settling", None
            if marker == LIVE_STARTING:
                return "starting", None
            return "held_unknown", None
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                marker = _read_live_marker(fd)
                if marker == LIVE_ACTIVE:
                    return "live", None
                if _published_code(marker) is not None:
                    return "settling", None
                if marker == LIVE_STARTING:
                    return "starting", None
                return "held_unknown", None
            return "unknown", None
        marker = _read_live_marker(fd)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        code = _published_code(marker)
        if code is not None:
            return "published", code
        if marker in (LIVE_STARTING, LIVE_ACTIVE):
            return "dead", None
        return "unknown", None
    finally:
        os.close(fd)


def _lock_liveness(cwd, task_id):
    """Compatibility view of `_lock_observation` without the bound outcome."""
    return _lock_observation(cwd, task_id)[0]


def _worker_liveness(cwd, record, lock=None):
    """Prove the wrapper or its orphaned process group live, dead, or unknown.

    A current wrapper's held lock is positive liveness. An unlocked lock is
    not by itself death: SIGKILL can remove the Python supervisor while an
    adapter-compatible process group remains. That group keeps the record
    quarantined but is not proof of ownership; a pid whose birth differs is
    likewise never signalled.
    """
    lock = _lock_liveness(cwd, record["id"]) if lock is None else lock
    if lock in ("starting", "live", "settling", "held_unknown"):
        return "live" if type(record["pid"]) is int and record["pid"] > 0 else "unknown"
    identity = _process_identity(record)
    if identity == "recycled":
        return "dead"
    if identity == "live":
        # A legacy or unreadable lock can fall back to the exact pid. A
        # current unlocked wrapper should already have published its exit and
        # is in a tiny teardown window (or broke the invariant), so do not
        # authorize a signal or terminal claim from that contradiction.
        return "live" if lock in ("legacy", "unknown") else "unknown"
    if identity == "unknown":
        return "unknown"
    if identity == "zombie":
        # A shell wrapper can remain in the process table until another
        # long-lived parent reaps it. It is no longer executing; only a
        # non-zombie member of its process group can keep the task live.
        return _group_process_liveness(record["pid"])
    return _group_liveness(record["pid"])


def _signal_authorized(cwd, record, lock=None):
    """Whether this observation identifies the recorded process group.

    The protected lock proves a lifecycle phase, not which kernel pid owns the
    process group. A recycled or unreadable pid identity remains unactionable.
    Once the recorded leader is absent, a surviving group may be an orphan or
    a later group that reused the number; only exact pid/birth identity can
    authorize a signal.
    """
    lock = _lock_liveness(cwd, record["id"]) if lock is None else lock
    # Only the supervisor's complete ACTIVE marker (or a pre-lock worker)
    # makes a process observation actionable. During STARTING no adapter has
    # been committed; during SETTLING its outcome is already being published;
    # and a partial/unreadable marker proves no lifecycle phase at all.
    return lock in ("live", "legacy") and _process_identity(record) == "live"


def _action_ready(cwd, record, lock):
    """Linearize a stop request after identity, before its first signal.

    Publication observed by the final protected-lock read wins as a natural
    outcome. A still-ACTIVE marker makes the action the later fact; publication
    after that point belongs to the requested stop even if kernel delivery and
    wrapper teardown overlap.
    """
    if not _signal_authorized(cwd, record, lock=lock):
        return "unknown", None
    current, code = _lock_observation(cwd, record["id"])
    if current == "published":
        return "published", code
    if current != lock or current not in ("live", "legacy"):
        return "unknown", None
    return "ready", None


def _read_exit(cwd, task_id):
    """A pre-supervisor worker's bounded exit token, or None.

    Current supervisors mirror their code here only for rolling old readers;
    current readers use the code bound into the protected live marker.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(exit_path(cwd, task_id), flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, EXIT_CEILING + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    token = raw.decode("ascii", "replace").strip()
    if len(raw) > EXIT_CEILING or not token.isdigit() or len(token) > 3:
        return None
    return int(token)


def liveness_unknown(cwd, record, now=None, observation=None):
    """Whether liveness is currently unknown after the task's deadline.

    The task timeout is the bound: before it, `running` needs no qualifier;
    afterwards every reporting surface names an observation failure. No
    sidecar is needed, so a stale or unwritable diagnostic file can neither
    manufacture nor hide the current fact.
    """
    if record is None or record.get("state") != "running":
        return False
    now = time.time() if now is None else now
    deadline = (record["started_at"] or now) + record["timeout"]
    if observation is None:
        observation = _worker_liveness(cwd, record)
    if now < deadline:
        return False
    if observation in ("unknown", "unknown_after_signal"):
        return True
    # A lock or group can prove worker-associated activity without proving
    # that the recorded pid still owns the group Antiphon would signal.
    return observation == "live" and not _signal_authorized(cwd, record)


LIVENESS_UNKNOWN_DETAIL = (
    "worker liveness or ownership could not be proved after its deadline; "
    "no terminal outcome was claimed, and its work and worker slot are kept")
LIVENESS_UNKNOWN_AFTER_SIGNAL_DETAIL = (
    "a stop signal was attempted after the task deadline, but the worker's "
    "resulting liveness could not be proved; no terminal outcome was claimed, "
    "and its work and worker slot are kept")


def _liveness_detail(observation):
    return (LIVENESS_UNKNOWN_AFTER_SIGNAL_DETAIL
            if observation == "unknown_after_signal"
            else LIVENESS_UNKNOWN_DETAIL)


def reported_status(cwd, task_id, now=None, patience=KILL_PATIENCE):
    """A status record plus a current, read-only liveness qualification."""
    record, observation = _reconcile_status(
        cwd, task_id, now=now, patience=patience)
    if not liveness_unknown(cwd, record, now=now, observation=observation):
        return record
    answer = dict(record)
    answer["worker_liveness"] = "unknown"
    answer["liveness_detail"] = _liveness_detail(observation)
    return answer


def _log_tail(cwd, task_id):
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(log_path(cwd, task_id), flags)
    except OSError:
        return ""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return ""
        os.lseek(fd, max(0, info.st_size - LOG_TAIL), os.SEEK_SET)
        return os.read(fd, LOG_TAIL).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


def _kill_group(pid, patience=KILL_PATIENCE):
    """Stop one session, distinguishing whether Antiphon sent a signal.

    `absent` means the first signal found no group, so a concurrently
    published nonzero exit is still the worker's natural outcome. `not_sent`
    means permission or malformed identity prevented the first signal.
    `stopped` means a signal was sent and absence was proved; `unresolved`
    means a signal was sent but absence was not proved.
    """
    if type(pid) is not int or pid <= 0:
        return "not_sent"
    sent = False
    for signum, wait in ((signal.SIGTERM, patience), (signal.SIGKILL, min(2.0, patience + 0.5))):
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            _reap(pid)
            return "stopped" if sent else "absent"
        except PermissionError:
            return "unresolved" if sent else "not_sent"
        except (OverflowError, ValueError):
            return "unresolved" if sent else "not_sent"
        sent = True
        deadline = time.time() + wait
        while time.time() < deadline:
            if _group_gone(pid):
                return "stopped"
            time.sleep(0.05)
    return "stopped" if _group_gone(pid) else "unresolved"


def _finish(cwd, task_id, state, exit_code=None, now=None,
            from_states=("running",)):
    def mutate(changed):
        # Two readers can reconcile the same worker concurrently.  Once one
        # has written a terminal fact, a stale observation must not replace it.
        if changed["state"] not in from_states:
            return
        changed["state"] = state
        changed["exit_code"] = exit_code
        changed["finished_at"] = time.time() if now is None else now
    update_task(cwd, task_id, mutate)
    return read_task(cwd, task_id)


def _finish_exit(cwd, record, code, now):
    # The durable exit is enough to answer, but the long-lived MCP server may
    # also be the wrapper's parent. Reap its now-finished child so successful
    # workers do not accumulate as zombies between tool calls.
    _reap(record["pid"], 0.25)
    if code == 0:
        state = "completed"
    elif BLOCKED_PATTERN.search(_log_tail(cwd, record["id"])):
        state = "blocked"
    else:
        state = "failed"
    return _finish(cwd, record["id"], state, code, now)


def _reconcile_status(cwd, task_id, now=None, patience=KILL_PATIENCE):
    """Reconcile a task from its protected marker or legacy process evidence.

    A published marker binds the outcome. A vanished old wrapper can use its
    legacy exit mirror. A positively owned process past its timeout is killed
    (with `patience` before SIGKILL) and timed out. Unreadable or unowned
    liveness is reported out of band while the compatible `running` record
    keeps its slot. Never guessed from the log.
    """
    now = time.time() if now is None else now
    record = read_task(cwd, task_id)
    if record is None:
        return record, None
    if record["state"] != "running":
        return record, None
    lock, published_code = _lock_observation(cwd, task_id)
    if lock == "published":
        # PUBLISHED can only be written through the supervisor-owned lock,
        # after its atomic exit write and before unlock. It is stronger than
        # a process-table observation, whose zombie/reaping state can lag.
        return _finish_exit(cwd, record, published_code, now), None
    liveness = _worker_liveness(cwd, record, lock=lock)
    if liveness == "dead":
        # The wrapper publishes before releasing its lock.  Legacy wrappers
        # have the same shell order but no lock. Read only after both facts;
        # a cached adapter-written token can never borrow later proof.
        if lock == "legacy":
            code = _read_exit(cwd, task_id)
            if code is not None:
                return _finish_exit(cwd, record, code, now), None
        return _finish(cwd, task_id, "failed", None, now), None
    if liveness == "unknown":
        # The durable v1 state stays `running`: an older reader must keep this
        # possibly-live worker in the four-slot cap.  Once both bounds pass,
        # reporting surfaces call `liveness_unknown` below and name why no
        # signal or terminal claim was made.
        return record, "unknown"
    if now - (record["started_at"] or now) > record["timeout"]:
        # The protected ACTIVE marker and exact pid/birth authorize a signal.
        # `_action_ready` then reads the marker once more: publication before
        # that point wins; an unchanged ACTIVE marker linearizes the action.
        # Ambiguity keeps the task and its slot.
        action, natural_code = _action_ready(cwd, record, lock)
        if action == "published":
            return _finish_exit(cwd, record, natural_code, now), None
        if action != "ready":
            return record, "unknown"
        stop = _kill_group(record["pid"], patience)
        after_lock, after_code = _lock_observation(cwd, task_id)
        after = _worker_liveness(cwd, record, lock=after_lock)
        if after == "dead":
            code = (_read_exit(cwd, task_id) if after_lock == "legacy"
                    else after_code if after_lock == "published" else None)
            if code is not None and stop in ("absent", "not_sent"):
                return _finish_exit(cwd, record, code, now), None
            state = "failed" if stop in ("absent", "not_sent") else "timed_out"
            return _finish(cwd, task_id, state, None, now), None
        observation = ("unknown_after_signal"
                       if after == "unknown" and stop in ("stopped", "unresolved")
                       else after)
        return record, observation
    return record, liveness


def status(cwd, task_id, now=None, patience=KILL_PATIENCE):
    """The reconciled durable task record; reporting uses the same probe."""
    return _reconcile_status(cwd, task_id, now=now, patience=patience)[0]


def _worktree_diff(cwd, record):
    """Everything the worker changed against the base it started from —
    committed, staged or loose — as bytes, or None when it cannot be told."""
    work = work_dir(cwd, record["id"])
    base = record["base"]
    if base is None or not os.path.isdir(work):
        return None
    # The bridge's own store is kept out by never intent-adding it, not by
    # the `.gitignore` written there: a checkout that tracks a
    # `.antiphon/.gitignore` of its own would otherwise carry the test
    # summary into the diff. The diff itself is unrestricted — a tracked file
    # under the store that the worker edited is a change, and a pathspec on
    # the diff hid it from the evidence (release gate, round 3).
    if _git(work, "add", "-A", "--intent-to-add", "--", ".", f":!{WORK_STORE}",
            timeout=30) is None:
        return None
    done = _git(work, "diff", "--no-color", base, timeout=60)
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
    if record["state"] == "tracking_incomplete":
        return Refused(
            f"not {verb}: task {record['id']} reached the {record['kind']} peer "
            f"{record['to']!r}, but tracking is incomplete and there is no "
            "worker here; the peer may already act on it")
    if record["state"] == "handing":
        return Refused(
            f"not {verb}: task {record['id']} hand-off tracking is incomplete "
            f"for the {record['kind']} peer {record['to']!r} and there is no "
            "worker here; the peer may already act on it")
    if record["state"] == "delivery_refused":
        return Refused(
            f"not {verb}: delivery of task {record['id']} to the "
            f"{record['kind']} peer {record['to']!r} was refused and there is "
            "no worker here")
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
    record, observation = _reconcile_status(cwd, task_id)
    if record is not None and record["state"] in (
            "handing", "handed", "tracking_incomplete", "delivery_refused"):
        raise _handed(record, "collected")
    while record is not None and record["state"] == "running" and time.time() < deadline:
        time.sleep(0.1)
        record, observation = _reconcile_status(cwd, task_id)
    if record is None:
        return None
    answer = {"id": task_id, "state": record["state"], "exit_code": record["exit_code"],
              "log_path": log_path(cwd, task_id), "log_tail": _log_tail(cwd, task_id),
              "worker": {"kind": record["kind"], "name": f"worker-{task_id[:8]}",
                         "directory": worker_dir(cwd, task_id),
                         "work": work_dir(cwd, task_id) if record["base"] else None,
                         "task_class": record["task_class"]}}
    if liveness_unknown(cwd, record, observation=observation):
        answer["worker_liveness"] = "unknown"
        answer["liveness_detail"] = _liveness_detail(observation)
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
    if record["state"] in (
            "handing", "handed", "tracking_incomplete", "delivery_refused"):
        raise _handed(record, "cancelled")
    if record["state"] == "accepted":
        raise Refused(
            f"not cancelled: task {task_id} is still starting; retry after its "
            "running record or refusal is visible")
    if record["state"] == "running":
        lock, _published_code_before = _lock_observation(cwd, task_id)
        if lock == "starting":
            raise Refused(
                f"not cancelled: task {task_id} is still starting; retry after "
                "its adapter has crossed the start gate")
        liveness = _worker_liveness(cwd, record, lock=lock)
        if liveness == "unknown":
            raise Refused(
                f"not cancelled: task {task_id}'s worker identity could not be "
                "verified; retry when worker liveness can be observed")
        if liveness == "dead":
            record = status(cwd, task_id)
            if record is not None and record["state"] == "running":
                raise Refused(
                    f"not cancelled: task {task_id}'s worker identity could not be "
                    "verified; retry after its state can be reconciled")
        else:
            action, natural_code = _action_ready(cwd, record, lock)
            if action == "published":
                record = _finish_exit(cwd, record, natural_code, time.time())
            elif action != "ready":
                raise Refused(
                    f"not cancelled: task {task_id}'s worker identity could not be "
                    "verified; retry when worker liveness can be observed")
            else:
                stop = _kill_group(record["pid"])
                after_lock, after_code = _lock_observation(cwd, task_id)
                after = _worker_liveness(cwd, record, lock=after_lock)
                if after == "dead":
                    code = (_read_exit(cwd, task_id) if after_lock == "legacy"
                            else after_code if after_lock == "published" else None)
                    if code is not None and stop in ("absent", "not_sent"):
                        record = _finish_exit(cwd, record, code, time.time())
                    else:
                        state = ("failed" if stop in ("absent", "not_sent")
                                 else "cancelled")
                        record = _finish(cwd, task_id, state, None)
                elif after == "live":
                    raise Refused(
                        f"not cancelled: task {task_id}'s worker still appears live "
                        "after the signal attempt; its work is kept")
                else:
                    raise Refused(
                        f"not cancelled: task {task_id}'s worker could not be proved "
                        "stopped after the signal attempt; its work is kept")
    _remove_dir(cwd, record)
    return record


def sweep(cwd, now):
    """Remove the directories of collected results and of expired tasks,
    never a running worker's; stop a worker past its timeout without waiting
    for it; drop an `accepted` record whose worker start died. A `handing`
    record is peer-delivery evidence, not a pending worker start, and survives
    until the ordinary task TTL. Then prune."""
    for record in tasks(cwd):
        if record["state"] == "accepted":
            if now - record["created_at"] > START_PATIENCE:
                _remove_dir(cwd, record)
                _discard_record(cwd, record["id"])
            continue
        record = status(cwd, record["id"], now, patience=SWEEP_PATIENCE) or record
        # Uncertain liveness deliberately keeps a compatible `running` row,
        # its directory and its slot even beyond the ordinary task TTL.
        if record["state"] == "running":
            continue
        expired = now - record["created_at"] > TASK_TTL
        collected = record["state"] in ("completed", "cancelled") and record["collected_at"]
        if expired or collected:
            _remove_dir(cwd, record)
    prune(cwd, now)


def _write_worker_exit(path, code):
    """Atomically publish one shell-compatible exit status."""
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".exit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(f"{code}\n".encode("ascii"))
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _worker_wrapper(lock_fd, gate_fd, ready_fd, commit_fd, exit_file, argv):
    """Wait for admission, run one adapter, then publish before unlocking."""
    code = 125
    terminate_requested = False

    def request_termination(_signum, _frame):
        nonlocal terminate_requested
        terminate_requested = True

    previous_term = signal.signal(signal.SIGTERM, request_termination)
    try:
        try:
            admitted = os.read(gate_fd, 1) == b"1"
        except OSError as error:
            admitted = False
            print(f"antiphon worker: start gate could not be read: {error}",
                  file=sys.stderr)
        finally:
            with contextlib.suppress(OSError):
                os.close(gate_fd)
        if admitted and not terminate_requested:
            code = 127
            child = None
            try:
                # This transition is what makes timeout/cancel actionable.
                # Before it, STARTING is negative evidence and readers refuse
                # to signal a wrapper whose adapter has not crossed the gate.
                _write_live_marker(lock_fd, LIVE_ACTIVE)
                os.write(ready_fd, b"1")
                os.close(ready_fd)
                ready_fd = None
                committed = os.read(commit_fd, 1) == b"1"
                os.close(commit_fd)
                commit_fd = None
                if not committed:
                    code = 125
                elif terminate_requested:
                    code = 128 + signal.SIGTERM
                else:
                    child = subprocess.Popen(argv)
                    if terminate_requested:
                        # Covers a signal delivered between the commit check
                        # and Popen. The group signal also reaches this child.
                        child.terminate()
                    code = child.wait()
                    if code < 0:
                        code = min(255, 128 + abs(code))
            except OSError as error:
                print(f"antiphon worker: command could not start: {error}",
                      file=sys.stderr)
                if child is not None and child.poll() is None:
                    with contextlib.suppress(OSError):
                        child.terminate()
                    try:
                        child.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(OSError):
                            child.kill()
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            child.wait(timeout=0.5)
        # Keep the worker-directory token for readers from before the
        # supervisor protocol. It is best-effort: current outcome publication
        # must not depend on an adapter-writable compatibility mirror.
        try:
            _write_worker_exit(exit_file, code)
        except OSError as error:
            print(f"antiphon worker: legacy outcome mirror could not be written: {error}",
                  file=sys.stderr)
        try:
            _write_live_marker(lock_fd, _published_marker(code))
        except OSError as error:
            print(f"antiphon worker: outcome could not be published: {error}",
                  file=sys.stderr)
            # STARTING, ACTIVE or a partial transition remains. Once the lock
            # is released, a reader can tell that no outcome was published.
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if ready_fd is not None:
            with contextlib.suppress(OSError):
                os.close(ready_fd)
        if commit_fd is not None:
            with contextlib.suppress(OSError):
                os.close(commit_fd)
        with contextlib.suppress(OSError):
            os.close(lock_fd)
    return code


def _worker_wrapper_main(args):
    if len(args) < 7 or args[0] != "_worker_wrapper":
        return 2
    try:
        lock_fd, gate_fd, ready_fd, commit_fd = (
            int(args[1]), int(args[2]), int(args[3]), int(args[4]))
    except (TypeError, ValueError):
        return 2
    if (lock_fd < 0 or gate_fd < 0 or ready_fd < 0 or commit_fd < 0
            or not args[5] or not args[6:]):
        return 2
    return _worker_wrapper(
        lock_fd, gate_fd, ready_fd, commit_fd, args[5], args[6:])


if __name__ == "__main__":
    raise SystemExit(_worker_wrapper_main(sys.argv[1:]))
