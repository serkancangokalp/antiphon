"""Managed workers: one task, one worker of the other kind, one record.

`.antiphon/tasks-v2/<task-id>.json` holds what a task is and what became of it —
its kind and class, the task text's digest and size (never the text), the
worker's pid, start time and exit — validated on every read the way the
delivery ledger is, retained for a week after it becomes terminal (and kept
while its liveness is uncertain), under a directory this code owns outright.

`.antiphon/workers/<task-id>/` is the worker's directory. The bridge's
worker-visible files `log` and `exit` live at its top. The supervisor's lock
sits beside the trusted task record as `.antiphon/tasks-v2/<task-id>.live`,
outside the adapter's writable directory; a diff too large to inline sits
there as `.antiphon/tasks-v2/<task-id>.diff` — and the work happens in
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
import base64
import errno
import fcntl
import hashlib
import hmac
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
# A pinned hand-off writes its preparation without the old store lock, then
# spends bounded transport time before taking that lock to finalize.  Give a
# freshly observed preparation one settlement window; an older orphan remains
# honest incomplete evidence and must not block the storage epoch forever.
LEGACY_HANDOFF_PATIENCE = 60
# A new wrapper waits behind its admission pipe while its exact process birth
# is sampled. Without that identity Antiphon could keep it live, but could
# never safely authorize timeout or cancellation signals after a pid reuse.
START_IDENTITY_PATIENCE = 0.5
# The trusted wrapper performs only marker publication before READY, but a
# newly spawned process can remain unscheduled for more than a second on a
# loaded host. The adapter still waits on the separate commit pipe, so this
# wider observation window cannot admit work after a refusal.
START_ACTIVE_PATIENCE = 5.0
# A complete process-table snapshot can fail transiently while the adapter is
# rapidly creating and reaping descendants. The supervisor retries only
# inside this fixed window; persistent uncertainty still withholds outcome
# publication instead of pretending the group is gone.
GROUP_OBSERVATION_PATIENCE = 0.25
LEGACY_STATES = ("accepted", "running", "completed", "failed", "cancelled",
                 "timed_out", "blocked", "outcome_unknown", "handed")
V2_STATES = ("handing", "tracking_incomplete", "delivery_refused")
STATES = LEGACY_STATES + V2_STATES
KINDS = ("claude", "codex")
CLASSES = ("read", "write")
MAX_TIME = float(2 ** 40)
RECORD_CEILING = 64 * 1024

TASK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
GIT_SHA = re.compile(r"[0-9a-f]{40}")
# C-locale macOS and procps `ps -o stat=` primary states plus their documented
# BSD modifiers. A free-form token is not process-liveness evidence.
PROCESS_STATE = re.compile(r"[DIRSTtUWXZ][+<>AELNSsVWXl]{0,15}")
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
LIVE_GIT_MUTATOR = b"git-mutator:"
LIVE_CEILING = 96
LOCAL_CONTROL_PREFIX = "antiphon-local:"
STOP_INTENTS = ("cancelled", "timed_out")
WORKER_TERMINAL = ("completed", "failed", "cancelled", "timed_out", "blocked",
                   "outcome_unknown")
TASK_STORE = "tasks-v2"
LEGACY_TASK_STORE = "tasks"
TESTS_FILE = "tests.txt"
WORK_DIR = "work"
WORK_STORE = ".antiphon"
GIT_CLEANUP_FILE = ".git-cleanup"
GIT_CLEANUP_TOKEN = b"antiphon git-worktree cleanup v1\n"
GITDIR_CEILING = 16 * 1024
GIT_GUARDIAN_OUTPUT_CEILING = 64 * 1024
LEGACY_FENCE_TOKEN = b"antiphon task-store v2 frozen\n"
LEGACY_WRITABLE_MODE = 0o700
LEGACY_FROZEN_MODE = 0o500


def tasks_dir(cwd):
    return os.path.join(cwd, ".antiphon", TASK_STORE)


def _legacy_tasks_dir(cwd):
    return os.path.join(cwd, ".antiphon", LEGACY_TASK_STORE)


def _task_store(cwd, create=False):
    """Return this protocol epoch's store, never the legacy task directory.

    Old and current readers may overlap during an upgrade, so current workers
    use a disjoint namespace. Before current admission the old directory is
    fenced read-only under its own lock; existing legacy tasks remain owned by
    the client that created them, and the current reader neither imports nor
    mutates them.
    """
    return _sound_dir(tasks_dir(cwd), create=create)


def workers_dir(cwd):
    return os.path.join(cwd, ".antiphon", "workers")


def _fsync_directory(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sound_dir(path, create=False):
    """The directory as one this code owns — never a link — or None."""
    parent = os.path.dirname(path)
    if os.path.islink(parent) or (os.path.exists(parent) and not os.path.isdir(parent)):
        return None
    if create:
        if not os.path.lexists(parent):
            try:
                os.mkdir(parent, 0o700)
                _fsync_directory(os.path.dirname(parent))
            except FileExistsError:
                pass
        if not os.path.lexists(path):
            # Two first delegations at once both see no store; the second
            # mkdir must not be the one that fails.
            try:
                os.mkdir(path, 0o700)
                _fsync_directory(parent)
            except FileExistsError:
                pass
    # `lstat`, never `stat`: a link to a directory elsewhere is somebody
    # else's directory, and the store's whole premise is ownership.
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return None
    if create and info.st_mode & 0o077:
        os.chmod(path, 0o700)
    elif not create and info.st_mode & 0o077:
        return None
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


def _read_record(directory, task_id):
    """One validated record from an already-approved directory."""
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        return None
    path = os.path.join(directory, task_id + ".json")
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        raw = os.read(fd, RECORD_CEILING + 1)
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
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


def read_task(cwd, task_id):
    """The current-epoch record, or None for anything else — never raised."""
    directory = _task_store(cwd)
    return _read_record(directory, task_id) if directory is not None else None


def legacy_task(cwd, task_id):
    """A prior-epoch record, exposed only to name the upgrade boundary."""
    directory = _sound_dir(_legacy_tasks_dir(cwd))
    return _read_record(directory, task_id) if directory is not None else None


def legacy_tasks(cwd):
    """Validated prior-epoch rows; current lifecycle code never mutates them."""
    directory = _sound_dir(_legacy_tasks_dir(cwd))
    if directory is None:
        return []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    found = []
    for name in names:
        if name.endswith(".json"):
            record = _read_record(directory, name[:-5])
            if record is not None:
                found.append(record)
    found.sort(key=lambda record: (record["created_at"], record["id"]))
    return found


def legacy_admitted(cwd):
    """Prior-epoch workers that require their creating client to finish."""
    return [record for record in legacy_tasks(cwd)
            if record["state"] in ("accepted", "running")]


def _write_record_in(directory, record):
    """Atomically publish one validated row in an explicit owned directory."""
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, os.path.join(directory, record["id"] + ".json"))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _legacy_fence_token(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, len(LEGACY_FENCE_TOKEN) + 1) == LEGACY_FENCE_TOKEN
    except OSError:
        return False


def _write_legacy_fence_token(fd):
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    if os.write(fd, LEGACY_FENCE_TOKEN) != len(LEGACY_FENCE_TOKEN):
        raise OSError("the task protocol fence token was only partly written")
    os.fsync(fd)


def _legacy_store_rejects_writes(directory):
    """Prove the host enforces this epoch boundary's POSIX directory mode."""
    try:
        fd, path = tempfile.mkstemp(dir=directory, prefix=".v2-write-probe-")
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return True
        raise
    else:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
        return False


def _check_legacy_quiescent(directory):
    """Fail closed unless every prior-protocol operation is durably over."""
    observed_at = time.time()
    try:
        names = os.listdir(directory)
    except OSError as error:
        raise OSError("the previous task protocol cannot be inspected") from error
    for name in names:
        if not name.endswith(".json"):
            continue
        task_id = name[:-5]
        if not TASK_ID.fullmatch(task_id):
            continue
        record = _read_record(directory, task_id)
        if record is None:
            raise Refused(
                "not delegated: the previous task protocol contains an "
                f"unreadable task row ({task_id}); current admission fails closed")
        if record["state"] in ("accepted", "running"):
            raise Refused(
                "not delegated: an operation belongs to the previous task "
                f"protocol ({task_id}); finish it with the Antiphon client "
                "that started it, then retry")
        if (record["state"] == "handing"
                and observed_at - record["created_at"]
                <= LEGACY_HANDOFF_PATIENCE):
            raise Refused(
                "not delegated: a previous-protocol hand-off "
                f"({task_id}) may still be sending; let that call finish "
                "with the Antiphon client that started it, then retry")
        if (record["state"] == "completed" and record["task_class"] == "write"
                and record["collected_at"] is None):
            raise Refused(
                "not delegated: an uncollected write result belongs to the "
                f"previous task protocol ({task_id}); collect it with the "
                "Antiphon client that started it, then retry")
        # A terminal label from the old protocol is not proof that its process
        # group is gone: it trusted a worker-writable exit mirror and used
        # best-effort signals. Only two positive absence observations suffice.
        if record["pid"] is not None:
            process = _process_liveness(record)
            group = _group_process_liveness(record["pid"])
            if process != "dead" or group != "dead":
                raise Refused(
                    "not delegated: a task from the previous protocol has "
                    f"unresolved process liveness ({task_id}); let the client "
                    "that started it prove the process group gone")


def _ensure_legacy_epoch_fence(cwd):
    """Freeze the prior protocol's store before current workers are admitted.

    Revoking directory writes is the one semantic commit. It stops both old
    calls waiting on ``tasks/.lock`` and hand-off writes that never took that
    lock. A durable token in the lock file distinguishes committed read-only
    state from a crash between chmod and the post-freeze scan. No legacy task
    row is created, changed, moved, or deleted.
    """
    directory = _sound_dir(_legacy_tasks_dir(cwd), create=True)
    if directory is None:
        raise OSError("the previous task protocol's store cannot be fenced")
    lock_path = os.path.join(directory, ".lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    deadline = time.monotonic() + 0.25
    while True:
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as error:
            readonly = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                readonly |= os.O_NOFOLLOW
            try:
                fd = os.open(lock_path, readonly)
            except OSError:
                raise OSError(
                    "the previous task protocol's lock cannot be fenced") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(
                    "the previous task protocol's lock is not a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                os.close(fd)
                if time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
                raise Refused(
                    "not delegated: a previous Antiphon client is mutating the "
                    "task store; let that call finish and restart the client") from error
        except Exception:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        break

    committed = False
    directory_fd = None
    try:
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        directory_fd = os.open(directory, dir_flags)
        mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
        token = _legacy_fence_token(fd)
        committed = mode == LEGACY_FROZEN_MODE and token
        if committed:
            _check_legacy_quiescent(directory)
            if not _legacy_store_rejects_writes(directory):
                raise OSError(
                    "the host does not enforce the previous task store fence")
            return
        if mode not in (LEGACY_WRITABLE_MODE, LEGACY_FROZEN_MODE):
            raise OSError(
                "the previous task protocol's store has an unsupported mode")

        _check_legacy_quiescent(directory)
        if mode == LEGACY_WRITABLE_MODE:
            os.fchmod(directory_fd, LEGACY_FROZEN_MODE)
        if not _legacy_store_rejects_writes(directory):
            raise OSError("the host does not enforce the previous task store fence")
        # A direct old hand-off writer does not use `.lock`. Anything it
        # committed before fchmod is now stable and must pass the same gate;
        # anything still in a temporary file can no longer rename into place.
        _check_legacy_quiescent(directory)
        os.fsync(directory_fd)
        _write_legacy_fence_token(fd)
        committed = True
    except Exception:
        # A crash may leave 0500 without the token. The next current caller
        # recovers under this same lock. An ordinary pre-commit failure restores
        # the cooperative old client's writable state so it can finish.
        if not committed:
            if directory_fd is not None:
                with contextlib.suppress(OSError):
                    os.fchmod(directory_fd, LEGACY_WRITABLE_MODE)
                    os.fsync(directory_fd)
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def tasks(cwd):
    """Every validated record, oldest first."""
    directory = _task_store(cwd)
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
    directory = _task_store(cwd, create=True)
    if directory is None:
        raise OSError(f"the task store under {tasks_dir(cwd)} cannot be used")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, _path(cwd, record["id"]))
        _fsync_directory(directory)
    except Exception:
        # Validation keeps normal records serializable, but a serializer or
        # encoding failure must not strand a temporary file in the owned task
        # directory. Preserve the original exception for the caller.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _sync_task_store(cwd):
    """A fresh durability acknowledgement for every currently visible row."""
    directory = _task_store(cwd)
    if directory is None:
        return False
    try:
        _fsync_directory(directory)
        return True
    except OSError:
        return False


def _bounded_timeout(timeout):
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or (isinstance(timeout, float) and not math.isfinite(timeout))
            or timeout <= 0):
        return DEFAULT_TIMEOUT
    return max(1, min(int(round(timeout)), MAX_TIMEOUT))


def _task_namespace_exists(cwd, task_id):
    """Whether any owned current or legacy object still claims ``task_id``."""
    paths = (
        _path(cwd, task_id),
        os.path.join(_legacy_tasks_dir(cwd), task_id + ".json"),
        live_path(cwd, task_id),
        _diff_path(cwd, task_id),
        worker_dir(cwd, task_id),
    )
    return any(os.path.lexists(path) for path in paths)


def _new_task_held(cwd, *, kind, task_class, sha256, size, parent=None,
                   timeout=DEFAULT_TIMEOUT, hop=1, to=None, task_id=None,
                   state="accepted"):
    """Validate and publish one new task while the task lock is held."""
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
    task_id = task_id or str(uuid.uuid4())
    if _task_namespace_exists(cwd, task_id):
        raise ValueError("the task id is already owned")
    record = {
        "version": (TASK_VERSION if state in V2_STATES else LEGACY_TASK_VERSION),
        "id": task_id, "kind": kind,
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


def new_task(cwd, *, kind, task_class, sha256, size, parent=None,
             timeout=DEFAULT_TIMEOUT, hop=1, to=None, task_id=None,
             state="accepted"):
    """A fresh `accepted` worker or `handing` peer record, written.

    Raises ValueError for a shape the store refuses and OSError for a store it
    cannot use. `task_id`, when the caller already put it on a message, must be
    a uuid. A hand-off is prepared separately so it consumes no worker slot and
    cannot be mistaken for a worker whose start died.
    """
    if _task_store(cwd, create=True) is None:
        raise OSError(f"the task store under {tasks_dir(cwd)} cannot be used")
    with _locked(cwd) as held:
        if not held:
            raise OSError(f"the task store under {tasks_dir(cwd)} cannot be locked")
        return _new_task_held(
            cwd, kind=kind, task_class=task_class, sha256=sha256, size=size,
            parent=parent, timeout=timeout, hop=hop, to=to,
            task_id=task_id, state=state)


@contextlib.contextmanager
def _locked(cwd):
    directory = _task_store(cwd)
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


def _update_task_held(cwd, task_id, mutate):
    """The update body for a caller already holding the task-store lock."""
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


def update_task(cwd, task_id, mutate):
    """Read-modify-write under the store's lock; an update that breaks the
    record is refused and nothing is written."""
    with _locked(cwd) as held:
        return bool(held and _update_task_held(cwd, task_id, mutate))


def _diff_path(cwd, task_id):
    """A completed write task's diff too large to inline, beside its record:
    evidence that outlives the worker's directory and goes with the record.
    Review 2026-09-03: it was written inside the directory that its own
    collection made sweepable, so the path the result named died on the next
    hook."""
    return os.path.join(tasks_dir(cwd), task_id + ".diff")


def _write_diff_held(cwd, task_id, diff):
    """Atomically publish bounded result evidence while its record is locked."""
    directory = _task_store(cwd)
    if directory is None:
        return False
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".diff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(diff)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _diff_path(cwd, task_id))
        _fsync_directory(directory)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        return False


def _retained_diff_path(cwd, task_id):
    """A regular, non-inline result sidecar still bound to this task id."""
    path = _diff_path(cwd, task_id)
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= DIFF_INLINE:
            return None
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            return None
        return path
    except OSError:
        return None
    finally:
        os.close(fd)


def _discard_record_held(cwd, task_id):
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
    # A pre-transaction reader could have been computing a large diff before
    # this process acquired the lock. Current publishers take the same lock;
    # the second pass also closes the historical write-after-first-unlink
    # window during an upgrade.
    for path in paths[:-1]:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
    absent = not any(os.path.lexists(path) for path in paths)
    return absent and _sync_task_store(cwd)


def _discard_record(cwd, task_id):
    """Discard one record and its evidence under the task-store lock."""
    with _locked(cwd) as held:
        return bool(held and _discard_record_held(cwd, task_id))


def _retention_origin(record):
    return (record["finished_at"]
            if record["state"] in WORKER_TERMINAL
            and record["finished_at"] is not None
            else record["created_at"])


def _expired(record, now):
    return (record["state"] not in ("accepted", "running")
            and now - _retention_origin(record) > TASK_TTL)


def _retire_expired(cwd, observed, now):
    """Delete one still-identical expired row before external work cleanup.

    The record path is removed and that absence is fsynced while the task lock
    excludes lifecycle refinement. A crash afterwards can leave only an
    orphan worker directory, which `_prune_orphan_workers` retries.
    """
    with _locked(cwd) as held:
        if not held:
            return False
        current = read_task(cwd, observed["id"])
        if current != observed or not _expired(current, now):
            return False
        # The JSON carries the fact that this worker had a Git-admin entry.
        # Preserve that retry key outside the record, durably, before the
        # record-first transaction makes the row disappear.
        if (current["base"] is not None
                and not _write_git_cleanup_witness(cwd, current["id"])):
            return False
        try:
            os.unlink(_path(cwd, current["id"]))
        except OSError:
            return False
        if not _sync_task_store(cwd):
            # The visible absence is not cleanup authority until a future
            # fsync acknowledges it. Keep the worker directory meanwhile.
            return False
        for path in (_diff_path(cwd, current["id"]),
                     live_path(cwd, current["id"])):
            with contextlib.suppress(OSError):
                os.unlink(path)
        _sync_task_store(cwd)
        return True


def prune(cwd, now):
    """Drop records after their full post-terminal retention window."""
    if _task_store(cwd) is None:
        return
    for record in tasks(cwd):
        if record["state"] == "accepted":
            if now - record["created_at"] > TASK_TTL:
                _discard_stale_accepted(cwd, record, now)
            continue
        if record["state"] == "running":
            continue
        if _expired(record, now):
            if os.path.lexists(worker_dir(cwd, record["id"])):
                continue
            _retire_expired(cwd, record, now)
    _prune_orphan_live(cwd, now)
    _prune_orphan_workers(cwd)


def _prune_orphan_live(cwd, now):
    """Bound orphan locks and result files without racing their publishers."""
    directory = _task_store(cwd)
    if directory is None:
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    candidates = []
    orphan_diffs = []
    for name in names:
        if name.endswith(LIVE_SUFFIX):
            task_id = name[:-len(LIVE_SUFFIX)]
            if TASK_ID.fullmatch(task_id):
                candidates.append((task_id, os.path.join(directory, name)))
        elif name.endswith(".diff"):
            task_id = name[:-5]
            if TASK_ID.fullmatch(task_id):
                orphan_diffs.append((task_id, os.path.join(directory, name)))
    with _locked(cwd) as held:
        if not held:
            return
        for task_id, path in candidates:
            current = _path(cwd, task_id)
            previous = os.path.join(
                _legacy_tasks_dir(cwd), task_id + ".json")
            # Parsed absence is not physical absence: a transient read error
            # or corrupt row cannot authorize erasing its recovery evidence.
            if os.path.lexists(current) or os.path.lexists(previous):
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
                marker = _read_live_bytes(fd)
                if (marker is None
                        or (marker not in (b"", LIVE_STARTING, LIVE_ACTIVE)
                            and _published_parts(marker) is None)):
                    # A Git-mutator identity, a partial marker, or a future
                    # marker is permanent uncertainty without its row.  Only
                    # an operator may resolve evidence Antiphon cannot name.
                    continue
                if os.path.lexists(current) or os.path.lexists(previous):
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
        for task_id, path in orphan_diffs:
            current = _path(cwd, task_id)
            previous = os.path.join(
                _legacy_tasks_dir(cwd), task_id + ".json")
            if os.path.lexists(current) or os.path.lexists(previous):
                continue
            flags = os.O_RDONLY | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags)
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode)
                        or now - info.st_mtime <= TASK_TTL):
                    continue
                if os.path.lexists(current) or os.path.lexists(previous):
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
                os.close(fd)


def _prune_orphan_workers(cwd):
    """Finish cleanup after a durably retired record outlived its sweep."""
    directory = _sound_dir(workers_dir(cwd))
    if directory is None or not _sync_task_store(cwd):
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for task_id in names:
        if not TASK_ID.fullmatch(task_id):
            continue
        current = _path(cwd, task_id)
        previous = os.path.join(_legacy_tasks_dir(cwd), task_id + ".json")
        # A physical but unreadable row is uncertainty, not absence. A live
        # marker likewise protects a supervisor whose record was damaged.
        if (os.path.lexists(current) or os.path.lexists(previous)
                or os.path.lexists(live_path(cwd, task_id))):
            continue
        path = os.path.join(directory, task_id)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if (not stat.S_ISDIR(info.st_mode)
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())):
            continue
        _remove_dir(cwd, {"id": task_id}, row_expected=False)


# ---------- the worker: one subprocess of the other kind ----------

class Refused(Exception):
    """A task this store will not start, with the reason a caller relays."""


class _GitMutationUnresolved(Exception):
    """A pre-start Git process may still publish owned worktree state."""


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


def _git_mutator_marker(pid, birth):
    """The exact gated Git process that may still publish worktree state."""
    if (type(pid) is not int or pid <= 0
            or peers.canonical_start(birth) != birth):
        raise ValueError("a Git mutator needs one exact process identity")
    marker = (LIVE_GIT_MUTATOR + str(pid).encode("ascii") + b":"
              + birth.encode("ascii") + b"\n")
    if len(marker) > LIVE_CEILING:
        raise ValueError("the Git mutator marker is too large")
    return marker


def _git_mutator_parts(marker):
    """A strict ``(pid, birth)`` from one Git-mutator marker, or None."""
    if (not isinstance(marker, bytes)
            or not marker.startswith(LIVE_GIT_MUTATOR)
            or not marker.endswith(b"\n")):
        return None
    pid_bytes, separator, birth_bytes = marker[
        len(LIVE_GIT_MUTATOR):-1].partition(b":")
    if (not separator or not 1 <= len(pid_bytes) <= peers.INTEGER_TOKEN_CEILING
            or not pid_bytes.isdigit() or pid_bytes.startswith(b"0")):
        return None
    try:
        pid = int(pid_bytes)
        birth = birth_bytes.decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return None
    if pid <= 0 or peers.canonical_start(birth) != birth:
        return None
    return pid, birth


def _write_live_marker(fd, marker):
    """Replace the bounded state inside an already-open regular lock file."""
    if (marker not in (LIVE_STARTING, LIVE_ACTIVE)
            and _published_parts(marker) is None
            and _git_mutator_parts(marker) is None):
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


def _read_live_bytes(fd):
    """The bounded bytes on an open regular live file, or None."""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIVE_CEILING:
            return None
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, LIVE_CEILING + 1)
    except OSError:
        return None


def _read_live_marker(fd):
    """The bounded, complete marker on an open live lock, or None."""
    marker = _read_live_bytes(fd)
    if (marker in (LIVE_STARTING, LIVE_ACTIVE)
            or _published_parts(marker) is not None
            or _git_mutator_parts(marker) is not None):
        return marker
    return None


def _published_marker(code, proof=None, stopped=False):
    """The supervisor-only terminal marker carrying one shell exit status.

    A proof-bearing supervisor also records whether its SIGTERM handler ran.
    That signed bit distinguishes an administrative stop from a coincident
    natural exit after the stop intent was durably claimed.
    """
    if type(code) is not int or not 0 <= code <= 255:
        raise ValueError("an exit status must be between 0 and 255")
    suffix = b""
    if proof is not None:
        if (not isinstance(proof, str) or len(proof) != 64
                or any(character not in "0123456789abcdef" for character in proof)):
            raise ValueError("a publication proof must be 32 hex-encoded bytes")
        if type(stopped) is not bool:
            raise ValueError("a publication stop flag must be boolean")
        suffix = (b":" + (b"stopped" if stopped else b"natural")
                  + b":" + proof.encode("ascii"))
    elif stopped:
        raise ValueError("a legacy publication cannot carry a stop flag")
    return LIVE_PUBLISHED + str(code).encode("ascii") + suffix + b"\n"


def _published_parts(marker):
    """A syntactically complete ``(code, stopped, proof)`` marker, or None."""
    if not isinstance(marker, bytes) or not marker.startswith(LIVE_PUBLISHED):
        return None
    raw = marker[len(LIVE_PUBLISHED):]
    if not raw.endswith(b"\n"):
        return None
    pieces = raw[:-1].split(b":")
    token = pieces[0]
    if not token or not token.isdigit() or len(token) > 3:
        return None
    code = int(token)
    if not 0 <= code <= 255:
        return None
    if len(pieces) == 1:
        return code, None, None
    if len(pieces) != 3 or pieces[1] not in (b"natural", b"stopped"):
        return None
    proof = pieces[2]
    if (len(proof) != 64
            or any(byte not in b"0123456789abcdef" for byte in proof)):
        return None
    return code, pieces[1] == b"stopped", proof.decode("ascii")


def _published_outcome(marker, expected_digest=None):
    """The authenticated ``(exit, stopped)`` fact, or None.

    This protocol epoch never accepts the short marker: old workers live in a
    disjoint task-store namespace.  A current record accepts only the proof
    whose digest was committed before the adapter crossed its start gate.  The
    guarantee assumes the host sandbox keeps the task store outside the
    adapter's writable roots; an unrestricted same-UID process can rewrite any
    user-owned file and is outside this file protocol's threat boundary.
    """
    parts = _published_parts(marker)
    if parts is None:
        return None
    code, stopped, proof = parts
    if expected_digest is None or proof is None:
        return None
    actual = hashlib.sha256(bytes.fromhex(proof)).hexdigest()
    return ((code, stopped)
            if hmac.compare_digest(actual, expected_digest) else None)


def _published_code(marker, expected_digest=None):
    """The exit-code compatibility view of an authenticated publication."""
    outcome = _published_outcome(marker, expected_digest)
    return outcome[0] if outcome is not None else None


def _encode_control(proof=None, birth=None, started=None, stop=None):
    """Encode current-reader facts in a v1 reader-compatible string field."""
    payload = {"proof": proof, "birth": birth, "started": started, "stop": stop}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return LOCAL_CONTROL_PREFIX + token


def _decode_control(record):
    value = record.get("to") if isinstance(record, dict) else None
    if not isinstance(value, str) or not value.startswith(LOCAL_CONTROL_PREFIX):
        return None
    token = value[len(LOCAL_CONTROL_PREFIX):]
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.b64decode(token + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except Exception:
        return None
    if (not isinstance(payload, dict)
            or set(payload) != {"proof", "birth", "started", "stop"}):
        return None
    proof = payload["proof"]
    if proof is not None and not (
            isinstance(proof, str) and SHA256_HEX.fullmatch(proof)):
        return None
    birth = payload["birth"]
    if birth is not None and not (
            _utf8_string(birth) and 0 < len(birth) <= 80):
        return None
    if not _time_or_none(payload["started"]):
        return None
    if payload["stop"] is not None and payload["stop"] not in STOP_INTENTS:
        return None
    return payload


def _control_digest(record):
    control = _decode_control(record)
    return control["proof"] if control is not None else None


def _runtime_birth(record):
    control = _decode_control(record)
    if control is not None and control["birth"] is not None:
        return control["birth"]
    return record.get("birth")


def _runtime_started(record):
    control = _decode_control(record)
    if control is not None and control["started"] is not None:
        return control["started"]
    return record.get("started_at") or record.get("created_at")


def _stop_intent(record):
    control = _decode_control(record)
    return control["stop"] if control is not None else None


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
    _ensure_legacy_epoch_fence(cwd)
    for record in running(cwd):
        status(cwd, record["id"], now, patience=SWEEP_PATIENCE)
    if _task_store(cwd, create=True) is None:
        raise OSError(f"the task store under {tasks_dir(cwd)} cannot be used")
    with _locked(cwd) as held:
        if not held:
            raise OSError(f"the task store under {tasks_dir(cwd)} cannot be locked")
        admit(cwd)
        return _new_task_held(cwd, **fields)


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


def _git(cwd, *args, timeout=60, lease_fd=None):
    command = ["git", "-C", cwd, *args]
    pass_fds = ()
    outer_timeout = timeout
    if lease_fd is not None:
        # A tiny supervisor, rather than Git, inherits the lifecycle lease.
        # It survives a killed caller until the exact mutating Git process
        # returns, but marks the descriptor close-on-exec before starting Git
        # so repository-controlled hooks cannot retain Antiphon's lock.
        command = [sys.executable, "-E", "-s", "-S",
                   os.path.abspath(__file__), "_git_guardian",
                   str(lease_fd), str(timeout), cwd, *args]
        pass_fds = (lease_fd,)
        outer_timeout = timeout + 5
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="surrogateescape", timeout=outer_timeout,
            pass_fds=pass_fds)
    except (OSError, subprocess.SubprocessError):
        done = None
    if lease_fd is not None and _read_live_marker(lease_fd) != LIVE_STARTING:
        # Missing positive completion is permanent outcome uncertainty, not
        # an ordinary Git failure.  pid/birth and the original process group
        # cannot cover a descendant that detached with setsid(), so no death
        # observation may substitute for the guardian's durable receipt.
        raise _GitMutationUnresolved
    return done


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
                              env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not isinstance(done.stdout, str):
        return None
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    fields = lines[0].split()
    if len(fields) != 5:
        return None
    # `ps` pads a one-digit day; the identity protocol already owns the
    # normalized C-locale grammar shared with Node, so use it here too.
    return peers.canonical_start(" ".join(fields))


def _process_snapshot(pid):
    """One atomic `(birth, state)` observation, `absent`, or None.

    Sampling these fields in separate `ps` calls can join the recorded
    worker's birth to a replacement process's live state after pid reuse.
    """
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=,stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    if not isinstance(done.stdout, str):
        return None
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if done.returncode != 0:
        return "absent" if not lines else None
    if len(lines) != 1:
        return None
    fields = lines[0].split()
    if len(fields) != 6:
        return None
    birth = peers.canonical_start(" ".join(fields[:5]))
    state = fields[5]
    if birth is None or PROCESS_STATE.fullmatch(state) is None:
        return None
    return birth, state


def _git_mutator_state(pid, birth):
    """``live``, ``dead``, or ``unknown`` for one gated Git identity."""
    snapshot = _process_snapshot(pid)
    if snapshot is None:
        return "unknown"
    if snapshot == "absent":
        return "dead"
    observed, state = snapshot
    if observed != birth or state.startswith("Z"):
        return "dead"
    return "live"


def _reconcile_unlocked_git_mutator(cwd, task_id, patience):
    """Classify an orphan Git marker without inventing a completion receipt."""
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(live_path(cwd, task_id), flags)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "owned"
        except OSError as error:
            return "owned" if error.errno in (errno.EACCES, errno.EAGAIN) else "unknown"
        marker = _read_live_bytes(fd)
        if marker is None:
            return "unknown"
        if (marker in (b"", LIVE_STARTING, LIVE_ACTIVE)
                or _published_parts(marker) is not None):
            return "absent"
        if _git_mutator_parts(marker) is None:
            # A partial or future marker carries no cleanup authority.  Known
            # non-Git phases above cannot follow an admitted worktree add;
            # every other nonempty shape fails closed.
            return "unknown"
        # The guardian alone can turn this marker back into STARTING after it
        # observes direct Git completion.  Even exact leader and group death
        # cannot rule out a detached descendant, so sweep never reclaims it.
        return "unknown"
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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


def _refuse(cwd, record, reason, expected=None, lease_fd=None):
    """A start that did not happen leaves nothing: not the record, not the
    worktree it may have created, unless another observer superseded it or a
    different starter now owns the lifecycle claim."""
    # The durable record disappears first. If its unlink cannot be fsynced,
    # preserve the work: a power loss may restore the record and it must not
    # then point at evidence this process already destroyed. The equality test
    # and discard share one lock: a concurrent status may publish a terminal
    # wrapper outcome after the start path's last read, and stale cleanup must
    # never erase that newer fact.
    expected = record if expected is None else expected
    discarded = False
    acquired_here = False
    try:
        with _locked(cwd) as held:
            current = read_task(cwd, record["id"]) if held else None
            if current != expected:
                current = None
            if current is not None and lease_fd is None:
                flags = os.O_RDWR | os.O_NONBLOCK
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    lease_fd = os.open(live_path(cwd, record["id"]), flags)
                except FileNotFoundError:
                    pass
                except OSError:
                    current = None
                else:
                    try:
                        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        current = None
                    else:
                        acquired_here = True
            if current is not None and lease_fd is not None:
                try:
                    info = os.fstat(lease_fd)
                    named = os.lstat(live_path(cwd, record["id"]))
                except OSError:
                    current = None
                else:
                    if (not stat.S_ISREG(info.st_mode)
                            or (named.st_dev, named.st_ino)
                            != (info.st_dev, info.st_ino)):
                        current = None
                    else:
                        marker = _read_live_bytes(lease_fd)
                        safe = {b"", LIVE_STARTING, LIVE_ACTIVE}
                        if (marker not in safe
                                and _published_parts(marker) is None):
                            current = None
            if current is not None:
                discarded = _discard_record_held(cwd, record["id"])
    finally:
        if lease_fd is not None:
            if acquired_here:
                with contextlib.suppress(OSError):
                    fcntl.flock(lease_fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(lease_fd)
    if discarded:
        _remove_dir(cwd, record, row_expected=False)
    raise Refused(reason)


def _close_fds(*descriptors):
    """Best-effort cleanup for optional descriptors in a partial start."""
    for descriptor in descriptors:
        if descriptor is None:
            continue
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _claim_start(cwd, record):
    """Atomically bind an accepted row to one held STARTING lifecycle lock."""
    with _locked(cwd) as held:
        if not held:
            raise Refused(
                "not delegated: the task store could not lock the worker start")
        current = read_task(cwd, record["id"])
        if current != record or current["state"] != "accepted":
            raise Refused(
                "not delegated: the accepted task was superseded before start")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = None
        try:
            fd = os.open(live_path(cwd, record["id"]), flags, 0o600)
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                raise Refused(
                    "not delegated: this task is already starting") from error
            previous = _read_live_bytes(fd)
            if previous not in (b"", LIVE_STARTING):
                raise Refused(
                    "not delegated: this task's Git worktree creation is "
                    "still unresolved; do not retry automatically; operator "
                    "intervention outside Antiphon is required")
            _write_live_marker(fd, LIVE_STARTING)
            return fd
        except Refused:
            _close_fds(fd)
            raise
        except OSError as error:
            _close_fds(fd)
            # This caller owned the accepted row but could not establish its
            # start lease. Cleanup is done directly while the task lock is
            # already held; `_refuse()` would try to re-enter this lock.
            _discard_record_held(cwd, record["id"])
            raise Refused(
                "not delegated: the worker's live lock could not be created: "
                f"{error}") from error


def start(cwd, record, text, env=None):
    """Start the worker for an accepted record; returns the running record.

    The work happens in a detached git worktree under the worker's directory
    whenever the project is a checkout — a write task needs one and is
    refused without; a read task in a plain directory runs in the project
    under the host's read-only class. The worker is its own session leader,
    its output goes to the task's log beside the work, and its environment
    carries the hop, its name and its directories — never a widened
    permission class. An ordinary refusal leaves no record. A missing Git
    completion receipt instead keeps every recovery witness and refuses
    automatic retry."""
    env = dict(os.environ if env is None else env)
    task_id = record["id"]
    lock_fd = _claim_start(cwd, record)
    gate_read = None
    gate_write = None
    ready_read = None
    ready_write = None
    commit_read = None
    commit_write = None
    proof_read = None
    proof_write = None

    def refuse_before_spawn(reason):
        nonlocal lock_fd, gate_read, gate_write, ready_read, ready_write
        nonlocal commit_read, commit_write, proof_read, proof_write
        lease_fd = lock_fd
        _close_fds(gate_read, gate_write, ready_read, ready_write,
                   commit_read, commit_write, proof_read, proof_write)
        lock_fd = gate_read = gate_write = ready_read = ready_write = None
        commit_read = commit_write = proof_read = proof_write = None
        _refuse(cwd, record, reason, lease_fd=lease_fd)

    def retain_unresolved_git(reason):
        """Return control without erasing a mutator recovery obligation."""
        nonlocal lock_fd, gate_read, gate_write, ready_read, ready_write
        nonlocal commit_read, commit_write, proof_read, proof_write
        _close_fds(gate_read, gate_write, ready_read, ready_write,
                   commit_read, commit_write, proof_read, proof_write, lock_fd)
        lock_fd = gate_read = gate_write = ready_read = ready_write = None
        commit_read = commit_write = proof_read = proof_write = None
        raise Refused(reason)

    try:
        _ensure_legacy_epoch_fence(cwd)
    except (OSError, Refused) as error:
        refuse_before_spawn(str(error))
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
        refuse_before_spawn(
            f"not delegated: {workers_dir(cwd)} cannot be used{why}")
    directory = worker_dir(cwd, task_id)
    try:
        usable_worker_dir = _sound_dir(directory, create=True)
    except OSError as error:
        refuse_before_spawn(
            "not delegated: the worker's directory could not be created: "
            f"{error}")
    if usable_worker_dir is None:
        refuse_before_spawn(
            "not delegated: the worker's directory could not be created safely")
    work = work_dir(cwd, task_id)
    base = None
    # A checkout with no commit yet (measured: the E2E's own `git init`
    # project) has nothing to branch a worktree from: a read task runs in
    # the project under the host's read-only class, a write task is refused.
    if _git_checkout(cwd) and _head(cwd) is None:
        if record["task_class"] == "write":
            refuse_before_spawn(
                "not delegated: a write task needs a commit to branch its "
                "worker's worktree from")
        run_in = cwd
    elif _git_checkout(cwd):
        # Git can publish its admin entry before `worktree add` returns, and
        # this process can die before `base` reaches the accepted row. Keep
        # the exact cleanup key durable in the already-owned outer directory
        # before Git is touched, so stale-start recovery never has to infer it
        # from a physical worktree that may also have disappeared.
        if not _write_git_cleanup_witness(cwd, task_id):
            refuse_before_spawn(
                "not delegated: the worker's Git cleanup witness could not "
                "be recorded")
        # Git can publish its physical checkout and admin entry after the
        # caller waiting for it has been killed.  Its narrow guardian owns the
        # accepted start's already-held lease until worktree add returns, but
        # Git and repository-controlled hook descendants never receive it.
        try:
            done = _git(
                cwd, "worktree", "add", "--detach", "-q", work, "HEAD",
                lease_fd=lock_fd)
        except _GitMutationUnresolved:
            retain_unresolved_git(
                f"not delegated: task {task_id}'s Git worktree creation could "
                "not produce a durable completion receipt; its accepted "
                "record, cleanup witness and work are kept; process death "
                "cannot authorize recovery; do not retry automatically; "
                "operator intervention outside Antiphon is required")
        if done is None or done.returncode != 0:
            diagnostic = (done.stderr if done else "").strip()
            diagnostic = diagnostic.encode(
                "utf-8", "backslashreplace").decode("utf-8")[:200]
            refuse_before_spawn(
                "not delegated: the worker's worktree could not be created: "
                f"{diagnostic}")
        base = _head(work)
        if base is None:
            refuse_before_spawn(
                "not delegated: the worker's worktree base could not be verified")
        run_in = work
        try:
            _ignored_store(work)
        except OSError as error:
            # A refusal leaves no record, no directory, no worktree entry:
            # measured before this, a tracked file named `.antiphon` raised
            # out of here with all three in place (review 2026-09-03).
            refuse_before_spawn(
                "not delegated: the worker's store could not be made in its "
                f"worktree: {error}")
    elif record["task_class"] == "write":
        refuse_before_spawn(
            "not delegated: a write task needs a git checkout to give its "
            "worker a worktree of its own")
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
    proof = os.urandom(32).hex()
    proof_digest = hashlib.sha256(bytes.fromhex(proof)).hexdigest()
    # The wrapper inherits an already-held task-store lock and releases it
    # only after binding the exit code into its terminal marker. A later
    # process can therefore prove liveness and outcome without a racy `ps`
    # observation or worker-writable result. It also waits on a pipe:
    # the adapter starts only after the running record is durable; EOF aborts
    # it. The adapter inherits neither control descriptor. Prior-protocol
    # workers remain in the disjoint legacy store and are never mixed here.
    try:
        gate_read, gate_write = os.pipe()
        ready_read, ready_write = os.pipe()
        commit_read, commit_write = os.pipe()
        proof_read, proof_write = os.pipe()
        if os.write(proof_write, proof.encode("ascii")) != len(proof):
            raise OSError("the worker's publication proof could not be staged")
        os.close(proof_write)
        proof_write = None
    except OSError as error:
        refuse_before_spawn(
            "not delegated: the worker's control pipes could not be created: "
            f"{error}")
    wrapped = [sys.executable, "-E", "-s", "-S",
               os.path.abspath(__file__), "_worker_wrapper",
               str(lock_fd), str(gate_read), str(ready_write), str(commit_read),
               str(proof_read), exit_path(cwd, task_id)] + argv
    try:
        log = open(log_path(cwd, task_id), "ab")
    except OSError as error:
        refuse_before_spawn(
            f"not delegated: the worker's log could not be opened: {error}")
    try:
        child = subprocess.Popen(wrapped, cwd=run_in, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True,
                                 pass_fds=(lock_fd, gate_read, ready_write,
                                           commit_read, proof_read))
    except OSError as error:
        log.close()
        refuse_before_spawn(
            f"not delegated: the {record['kind']} CLI could not be started: "
            f"{error}")
    log.close()
    os.close(lock_fd)
    os.close(gate_read)
    os.close(ready_write)
    os.close(commit_read)
    os.close(proof_read)

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
        started_at = time.time()
        changed["state"] = "running"
        changed["pid"] = child.pid
        changed["birth"] = birth
        changed["base"] = base
        changed["started_at"] = started_at
        changed["to"] = _encode_control(
            proof=proof_digest, birth=birth, started=started_at)
    updated = update_task(cwd, task_id, mutate)
    started = read_task(cwd, task_id)
    if not (updated and started is not None and started["state"] == "running"
            and started["pid"] == child.pid):
        # EOF is the abort token. The wrapper has not started the adapter and
        # never will, so cleanup cannot erase a live task even if the wrapper
        # itself takes a moment to notice the closed pipe.
        abort_unadmitted()
        if started is None or started["state"] == "accepted":
            _refuse(cwd, record,
                    "not delegated: the task record could not be updated",
                    expected=started or record)
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
        _refuse(cwd, record,
                "not delegated: the worker start gate could not be released",
                expected=started)
    finally:
        if gate_write is not None:
            with contextlib.suppress(OSError):
                os.close(gate_write)
            gate_write = None
    ready = False
    ready_started = time.monotonic()
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
    ready_elapsed = time.monotonic() - ready_started
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
        wrapper_log = _log_tail(cwd, task_id).strip()
        diagnostic = (f"wrapper log tail: {json.dumps(wrapper_log)}"
                      if wrapper_log else "wrapper log was empty")
        _refuse(cwd, record,
                "not delegated: the worker did not acknowledge its start "
                f"after {ready_elapsed:.3f} s; {diagnostic}",
                expected=started)
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
        _refuse(cwd, record, "not delegated: the worker start commit failed",
                expected=started)
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
TERMINAL = WORKER_TERMINAL


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
    if not isinstance(done.stdout, str):
        return None
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if done.returncode == 0:
        if len(lines) != 1 or PROCESS_STATE.fullmatch(lines[0]) is None:
            return None
        return lines[0]
    return "absent" if not lines else None


def _group_process_liveness(pgid):
    """Use process-table states to distinguish a zombie-only process group."""
    members = _settled_group_members(pgid)
    if members is None:
        return "unknown"
    states = [state for _pid, state in members]
    if not states or all(state.startswith("Z") for state in states):
        return "dead"
    return "live"


def _group_members(pgid):
    """The bounded process-table members of one group, or None if unreadable.

    Every row must identify a positive pid and process group.  Once a row's
    process group is proved different, its state is irrelevant to this group;
    target-group rows still need a fully valid state before the snapshot can
    prove liveness or absence.
    """
    if pgid is None:
        return []
    if type(pgid) is not int or pgid <= 0:
        return None
    try:
        done = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="],
                              capture_output=True, text=True, timeout=5,
                              env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
                              start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not isinstance(done.stdout, str):
        return None
    members = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pieces = line.split(None, 2)
        if len(pieces) < 2:
            return None
        try:
            member_pid, member_group = int(pieces[0]), int(pieces[1])
        except ValueError:
            return None
        if member_pid <= 0 or member_group <= 0:
            return None
        if member_group != pgid:
            continue
        if len(pieces) != 3 or PROCESS_STATE.fullmatch(pieces[2]) is None:
            return None
        members.append((member_pid, pieces[2]))
    return members


def _settled_group_members(pgid, patience=GROUP_OBSERVATION_PATIENCE):
    """One complete group snapshot, tolerating only bounded uncertainty."""
    deadline = time.time() + max(0.0, patience)
    while True:
        members = _group_members(pgid)
        if members is not None:
            return members
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        time.sleep(min(0.02, remaining))


def _drain_adapter_group(pgid, supervisor_pid, patience=1.0):
    """End adapter descendants before the supervisor publishes completion.

    Shell tasks can background a descendant and exit zero.  The descendant is
    still task work, so publication is withheld unless every non-zombie member
    other than the supervisor is gone.  Signals target enumerated members, not
    the whole group, because SIGKILLing the group would kill the publisher too.
    """
    for signum, wait in ((signal.SIGTERM, patience), (signal.SIGKILL, 0.5)):
        members = _settled_group_members(pgid)
        if members is None:
            return False
        live = [pid for pid, state in members
                if pid != supervisor_pid and not state.startswith("Z")]
        if not live:
            return True
        for pid in live:
            try:
                if os.getpgid(pid) != pgid:
                    continue
                os.kill(pid, signum)
            except ProcessLookupError:
                continue
            except (OSError, OverflowError, ValueError):
                return False
        deadline = time.time() + wait
        while time.time() < deadline:
            members = _group_members(pgid)
            if (members is not None
                    and not any(pid != supervisor_pid
                                and not state.startswith("Z")
                                for pid, state in members)):
                return True
            time.sleep(0.05)
    members = _settled_group_members(pgid)
    return (members is not None
            and not any(pid != supervisor_pid and not state.startswith("Z")
                        for pid, state in members))


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
    birth = _runtime_birth(record)
    if birth is None:
        return "unknown"
    snapshot = _process_snapshot(pid)
    if snapshot is None:
        return "unknown"
    if snapshot == "absent":
        return "absent"
    observed, state = snapshot
    if observed != birth:
        return "recycled"
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


def _lock_observation(cwd, task_id, record=None):
    """One lock's lifecycle state and its bound published exit, if any.

    This task-store epoch always has a supervisor lock for a running worker.
    A missing or unreadable lock is uncertainty, never permission to fall back
    to an adapter-writable legacy exit token. A held lock is live; once
    acquired, its authenticated marker distinguishes a wrapper that atomically
    published an exit from one that died before doing so.
    """
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(live_path(cwd, task_id), flags)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unknown", None
    if record is None:
        record = read_task(cwd, task_id)
    expected_digest = _control_digest(record)
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
            if _published_outcome(marker, expected_digest) is not None:
                return "settling", None
            if marker == LIVE_STARTING:
                return "starting", None
            return "held_unknown", None
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                marker = _read_live_marker(fd)
                if marker == LIVE_ACTIVE:
                    return "live", None
                if _published_outcome(marker, expected_digest) is not None:
                    return "settling", None
                if marker == LIVE_STARTING:
                    return "starting", None
                return "held_unknown", None
            return "unknown", None
        marker = _read_live_marker(fd)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        outcome = _published_outcome(marker, expected_digest)
        if outcome is not None:
            return "published", outcome
        if marker in (LIVE_STARTING, LIVE_ACTIVE):
            return "dead", None
        return "unlocked_unknown", None
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
        # The held lock proves activity, but only the pid/birth pair proves
        # ownership. Keeping those answers separate is what prevents a
        # process-table read failure from authorizing a signal.
        return "live" if _process_identity(record) == "live" else "unknown"
    identity = _process_identity(record)
    if identity == "recycled":
        return "dead"
    if identity == "live":
        # An unreadable lock can fall back to positive liveness for reporting,
        # but it cannot authorize a signal. A current unlocked wrapper should
        # already have published its exit and is in a tiny teardown window (or
        # broke the invariant), so do not claim a terminal outcome from it.
        return "live" if lock == "unknown" else "unknown"
    if identity == "unknown":
        return "unknown"
    if identity in ("zombie", "absent"):
        # A shell wrapper can remain as a zombie or already be reaped while
        # zombie descendants still retain the group number. Only a non-zombie
        # member keeps the task live; killpg(0) alone cannot tell that apart.
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
    return lock == "live" and _process_identity(record) == "live"


def _action_ready(cwd, record, lock):
    """Linearize a stop request after identity, before its first signal.

    Publication observed by the final protected-lock read wins immediately.
    A still-ACTIVE marker lets the caller durably claim and attempt the stop;
    the supervisor's authenticated stopped bit later says whether that action
    actually reached it, so a coincident natural exit is not mislabelled.
    """
    if not _signal_authorized(cwd, record, lock=lock):
        return "unknown", None
    current, code = _lock_observation(cwd, record["id"])
    if current == "published":
        return "published", code
    if current != lock or current != "live":
        return "unknown", None
    return "ready", None


def _claim_stop(cwd, record, intent, now=None):
    """First durable stop intent wins under the task-store lock."""
    if intent not in STOP_INTENTS:
        raise ValueError("unknown stop intent")
    with _locked(cwd) as held:
        if not held:
            return read_task(cwd, record["id"]), None
        current = read_task(cwd, record["id"])
        if current is None or current["state"] != "running":
            return current, None
        control = _decode_control(current) or {
            "proof": None, "birth": None, "started": None, "stop": None}
        if control["stop"] is None:
            def mutate(changed):
                control["stop"] = intent
                changed["to"] = _encode_control(**control)

            if not _update_task_held(cwd, record["id"], mutate):
                return read_task(cwd, record["id"]), None
            current = read_task(cwd, record["id"])
        elif not _sync_task_store(cwd):
            # A prior caller can have published the rename but lost the
            # directory-fsync acknowledgement. Merely seeing that intent on a
            # retry is not authority to signal; establish a fresh durability
            # acknowledgement while the exact row is locked first.
            return current, None
        return current, _stop_intent(current) if current is not None else None


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
    deadline = (_runtime_started(record) or now) + record["timeout"]
    if observation is None:
        observation = _worker_liveness(cwd, record)
    if now < deadline:
        return False
    if observation in ("unknown", "unknown_after_signal", "live_after_signal"):
        return True
    # A lock or group can prove worker-associated activity without proving
    # that the recorded pid still owns the group Antiphon would signal.
    return observation == "live" and not _signal_authorized(cwd, record)


def accepted_start_recovery(cwd, record, now=None):
    """Name durable uncertainty for one accepted start, without changing it.

    ``git_completion_receipt_missing`` means the exact Git identity was
    published but the unlocked guardian never replaced it with its sole
    direct-return receipt. ``unknown`` means an old accepted row has lifecycle
    evidence that cannot be safely interpreted. A held descriptor is still an
    in-flight start and receives no recovery label.
    """
    if record is None or record.get("state") != "accepted":
        return None
    now = time.time() if now is None else now
    stale = now - record["created_at"] > START_PATIENCE
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(live_path(cwd, record["id"]), flags)
    except FileNotFoundError:
        return None
    except OSError:
        return "unknown" if stale else None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return None
            return "unknown" if stale else None
        marker = _read_live_bytes(fd)
        if _git_mutator_parts(marker) is not None:
            return "git_completion_receipt_missing"
        if marker is None:
            return "unknown" if stale else None
        if (marker in (b"", LIVE_STARTING, LIVE_ACTIVE)
                or _published_parts(marker) is not None):
            return None
        return "unknown" if stale else None
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def start_recovery_health(cwd):
    """Count unresolved lifecycle evidence that has no readable task row."""
    directory = _task_store(cwd)
    health = {"orphaned": 0, "row_unreadable": 0}
    if directory is None:
        return health
    try:
        names = os.listdir(directory)
    except OSError:
        return health
    for name in names:
        if not name.endswith(LIVE_SUFFIX):
            continue
        task_id = name[:-len(LIVE_SUFFIX)]
        if TASK_ID.fullmatch(task_id) is None:
            continue
        current = _path(cwd, task_id)
        previous = os.path.join(
            _legacy_tasks_dir(cwd), task_id + ".json")
        physical_row = os.path.lexists(current) or os.path.lexists(previous)
        if read_task(cwd, task_id) is not None:
            continue
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(os.path.join(directory, name), flags)
        except OSError:
            unresolved = True
        else:
            try:
                marker = _read_live_bytes(fd)
            finally:
                os.close(fd)
            unresolved = (marker is None
                          or (marker not in (b"", LIVE_STARTING, LIVE_ACTIVE)
                              and _published_parts(marker) is None))
        if unresolved:
            key = "row_unreadable" if physical_row else "orphaned"
            health[key] += 1
    return health


LIVENESS_UNKNOWN_DETAIL = (
    "worker liveness or ownership could not be proved after its deadline; "
    "no terminal outcome was claimed, and its work and worker slot are kept")
LIVENESS_UNKNOWN_AFTER_SIGNAL_DETAIL = (
    "a stop signal was attempted after the task deadline, but the worker's "
    "resulting liveness could not be proved; no terminal outcome was claimed, "
    "and its work and worker slot are kept")
LIVENESS_LIVE_AFTER_SIGNAL_DETAIL = (
    "a stop signal was attempted after the task deadline, but the worker still "
    "appears live; no terminal outcome was claimed, and its work and worker "
    "slot are kept")
GIT_START_RECEIPT_MISSING_DETAIL = (
    "Git worktree creation has no durable completion receipt; its accepted "
    "record, cleanup witness, work and worker slot are kept; do not retry "
    "automatically; operator intervention outside Antiphon is required")
START_RECOVERY_UNKNOWN_DETAIL = (
    "accepted-start recovery evidence could not be read safely; its record, "
    "work and worker slot are kept; do not retry automatically")


def _liveness_detail(observation):
    if observation == "unknown_after_signal":
        return LIVENESS_UNKNOWN_AFTER_SIGNAL_DETAIL
    if observation == "live_after_signal":
        return LIVENESS_LIVE_AFTER_SIGNAL_DETAIL
    return LIVENESS_UNKNOWN_DETAIL


def _start_recovery_detail(recovery):
    if recovery == "git_completion_receipt_missing":
        return GIT_START_RECEIPT_MISSING_DETAIL
    return START_RECOVERY_UNKNOWN_DETAIL


def _public_record(record):
    """One task row without current-reader lifecycle control metadata."""
    if record is not None and _decode_control(record) is not None:
        record = dict(record)
        record["to"] = None
    return record


def reported_status(cwd, task_id, now=None, patience=KILL_PATIENCE):
    """A status record plus a current, read-only liveness qualification."""
    record, observation = _reconcile_status(
        cwd, task_id, now=now, patience=patience)
    unknown = liveness_unknown(
        cwd, record, now=now, observation=observation)
    recovery = accepted_start_recovery(cwd, record, now=now)
    record = _public_record(record)
    if not unknown and recovery is None:
        return record
    answer = dict(record)
    if unknown:
        answer["worker_liveness"] = "unknown"
        answer["liveness_detail"] = _liveness_detail(observation)
    if recovery is not None:
        answer["start_recovery"] = recovery
        answer["recovery_detail"] = _start_recovery_detail(recovery)
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


def _kill_group(pid, patience=KILL_PATIENCE, revalidate=None):
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
    for signum, wait in ((signal.SIGTERM, patience),
                         (signal.SIGKILL, min(2.0, patience + 0.5))):
        # A process group id can be reused between TERM and KILL.  The caller
        # that owns a pid/birth record revalidates immediately before every
        # signal; losing that proof leaves the group alone.
        if revalidate is not None and not revalidate():
            return "unresolved" if sent else "not_sent"
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


def _stop_group(cwd, record, patience=KILL_PATIENCE):
    """Stop only while every signal still targets this task's exact worker."""
    def authorized():
        current = read_task(cwd, record["id"])
        if (current is None or current["state"] != "running"
                or current["pid"] != record["pid"]):
            return False
        lock, _outcome = _lock_observation(
            cwd, record["id"], record=current)
        return _signal_authorized(cwd, current, lock=lock)

    return _kill_group(record["pid"], patience, revalidate=authorized)


def _finish(cwd, task_id, state, exit_code=None, now=None,
            from_states=("running",), stop_resolution=None):
    def mutate(changed):
        # Two readers can reconcile the same worker concurrently.  Once one
        # has written a terminal fact, a stale observation must not replace it.
        # The one exception is a one-way refinement of `outcome_unknown` by an
        # authenticated natural publication or by the caller that both sent
        # the already-durable stop intent and positively observed group death.
        refining_unknown = changed["state"] == "outcome_unknown"
        if changed["state"] not in from_states and not refining_unknown:
            return
        intent = _stop_intent(changed)
        if refining_unknown:
            if stop_resolution == "natural":
                pass
            elif stop_resolution == "action" and intent is not None:
                pass
            else:
                return
            # Evidence returned for the conservative unknown observation is
            # not evidence for this newly resolved outcome. In particular, a
            # completed write must expose its diff before sweep may clean it.
            changed["collected_at"] = None
        # A stop claim is durable before its syscall so another reader cannot
        # race a signal exit into `failed`.  Until authenticated publication
        # or the signalling caller resolves it, however, the claim alone says
        # nothing about whether a signal was delivered.
        if intent is not None and stop_resolution is None:
            return
        if stop_resolution == "unknown":
            state_to_write = "outcome_unknown"
            exit_to_write = None
        else:
            action_won = intent is not None and stop_resolution == "action"
            state_to_write = intent if action_won else state
            exit_to_write = None if action_won else exit_code
        changed["state"] = state_to_write
        changed["exit_code"] = exit_to_write
        changed["finished_at"] = time.time() if now is None else now
        # Unknown keeps the durable intent so a concurrent signalling caller
        # can refine it. Every resolved outcome discards internal control.
        if state_to_write != "outcome_unknown" and _decode_control(changed) is not None:
            changed["to"] = None
    update_task(cwd, task_id, mutate)
    return read_task(cwd, task_id)


def _exit_fact(publication):
    """Normalize one authenticated current-epoch publication."""
    if (isinstance(publication, tuple) and len(publication) == 2
            and type(publication[0]) is int
            and type(publication[1]) is bool):
        return publication
    return None, None


def _finish_exit(cwd, record, publication, now, stop_resolution=None):
    # The durable exit is enough to answer, but the long-lived MCP server may
    # also be the wrapper's parent. Reap its now-finished child so successful
    # workers do not accumulate as zombies between tool calls.
    _reap(record["pid"], 0.25)
    code, stopped = _exit_fact(publication)
    if code is None:
        return record
    if stopped is not None:
        stop_resolution = "action" if stopped else "natural"
    if code == 0:
        state = "completed"
    elif BLOCKED_PATTERN.search(_log_tail(cwd, record["id"])):
        state = "blocked"
    else:
        state = "failed"
    return _finish(cwd, record["id"], state, code, now,
                   stop_resolution=stop_resolution)


def _finish_published(cwd, record, publication, now, stop_resolution=None,
                      group_dead=False):
    """Commit an authenticated publication only after positive group death."""
    # `_kill_group` returning `stopped` or `absent` already observed exact
    # process-group absence after its last signal decision. Re-sampling `ps`
    # cannot strengthen that fact and can turn a successful stop into a false
    # unknown when the process table is transiently unreadable.
    group = "dead" if group_dead else _group_process_liveness(record["pid"])
    if group != "dead":
        return record, group
    return (_finish_exit(
        cwd, record, publication, now, stop_resolution=stop_resolution), None)


def _reconcile_status(cwd, task_id, now=None, patience=KILL_PATIENCE):
    """Reconcile a task from its protected marker and process evidence.

    A published marker binds the outcome. A positively owned process past its
    timeout is killed (with `patience` before SIGKILL) and timed out. Unreadable or unowned
    liveness is reported out of band while the compatible `running` record
    keeps its slot. Never guessed from the log.
    """
    now = time.time() if now is None else now
    record = read_task(cwd, task_id)
    if record is None:
        return record, None
    if record["state"] == "outcome_unknown":
        # This conservative terminal observation is the one mutable outcome:
        # a marker that was temporarily unreadable can become available on a
        # later status/result call. Accept only the authenticated marker and
        # the same positive process-group death proof as the running path.
        lock, publication = _lock_observation(
            cwd, task_id, record=record)
        if lock == "published":
            return _finish_published(cwd, record, publication, now)
        return record, None
    if record["state"] != "running":
        return record, None
    lock, published_code = _lock_observation(cwd, task_id, record=record)
    if lock == "published":
        # PUBLISHED can only be written through the supervisor-owned lock,
        # after its atomic exit write and before unlock. It is stronger than
        # an adapter-writable exit mirror, but its process group must still be
        # positively dead before the record gives up its slot and work.
        return _finish_published(cwd, record, published_code, now)
    liveness = _worker_liveness(cwd, record, lock=lock)
    if liveness == "dead":
        # The supervisor writes its marker before it dies.  Re-open only after
        # the positive death observation so a publication that landed between
        # the first lock read and process reconciliation is not lost.
        final_lock, final_code = _lock_observation(cwd, task_id, record=record)
        if final_lock == "published":
            return _finish_published(cwd, record, final_code, now)
        # A positively dead group without authenticated publication says only
        # that the outcome was lost.  It does not say whether the adapter
        # failed, exited naturally, or a prior stop caller died after sending
        # its signal.
        return (_finish(cwd, task_id, "outcome_unknown", None, now,
                        stop_resolution="unknown"), None)
    if liveness == "unknown":
        # A transient process-table failure keeps the durable state running.
        # The disjoint task-store epoch keeps old readers away; current
        # reporting names why no signal or terminal claim was made.
        return record, "unknown"
    intent = _stop_intent(record)
    if intent is not None and not _sync_task_store(cwd):
        # A visible intent whose record-directory fsync previously failed is
        # not yet authority to signal. A later probe can establish the ack.
        return record, "unknown"
    overdue = now - (_runtime_started(record) or now) > record["timeout"]
    if intent is not None or overdue:
        # The protected ACTIVE marker and exact pid/birth authorize a signal.
        # `_action_ready` then reads the marker once more: publication before
        # that point wins; an unchanged ACTIVE marker linearizes the action.
        # Ambiguity keeps the task and its slot.
        action, natural_code = _action_ready(cwd, record, lock)
        if action == "published":
            return _finish_published(cwd, record, natural_code, now)
        if action != "ready":
            return record, "unknown"
        if intent is None:
            record, intent = _claim_stop(cwd, record, "timed_out", now=now)
            if record is None or record["state"] != "running":
                return record, None
            if intent is None:
                return record, "unknown"
        stop = _stop_group(cwd, record, patience)
        after_lock, after_code = _lock_observation(cwd, task_id, record=record)
        group_dead = stop in ("stopped", "absent")
        after = ("dead" if group_dead
                 else _worker_liveness(cwd, record, lock=after_lock))
        if after == "dead":
            code = after_code if after_lock == "published" else None
            if code is not None:
                resolution = ("action" if stop not in ("absent", "not_sent")
                              else "natural")
                return _finish_published(
                    cwd, record, code, now, stop_resolution=resolution,
                    group_dead=group_dead)
            state = intent
            resolution = ("action" if stop not in ("absent", "not_sent")
                          else "unknown")
            return _finish(cwd, task_id, state, None, now,
                           stop_resolution=resolution), None
        observation = (f"{after}_after_signal"
                       if after in ("live", "unknown")
                       and stop in ("stopped", "unresolved") else after)
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
    recovery = accepted_start_recovery(cwd, record)
    if recovery is not None:
        answer["start_recovery"] = recovery
        answer["recovery_detail"] = _start_recovery_detail(recovery)
    if liveness_unknown(cwd, record, observation=observation):
        answer["worker_liveness"] = "unknown"
        answer["liveness_detail"] = _liveness_detail(observation)
    if record["state"] not in TERMINAL:
        return answer
    evidence = True
    large_diff = None
    if record["task_class"] == "write" and record["state"] == "completed":
        retained = (_retained_diff_path(cwd, task_id)
                    if record["collected_at"] is not None else None)
        if retained is not None:
            answer["diff_path"] = retained
        else:
            diff = _worktree_diff(cwd, record)
            if diff is None:
                evidence = False
                kept = os.path.isdir(worker_dir(cwd, task_id))
                answer["diff_missing"] = (
                    "the diff could not be produced; "
                    + (f"the work is kept at {work_dir(cwd, task_id)}"
                       if kept else "the worker directory is no longer available"))
            elif len(diff) <= DIFF_INLINE:
                answer["diff"] = diff.decode("utf-8", "replace")
            else:
                large_diff = diff
    try:
        with open(tests_path(cwd, task_id), encoding="utf-8", errors="replace") as f:
            answer["tests"] = f.read(DIFF_INLINE)
    except OSError:
        pass
    if evidence and large_diff is not None:
        with _locked(cwd) as held:
            current = read_task(cwd, task_id) if held else None
            if (current is None or current["state"] != record["state"]
                    or not _write_diff_held(cwd, task_id, large_diff)):
                evidence = False
            else:
                answer["diff_path"] = _diff_path(cwd, task_id)
                if current["collected_at"] is None:
                    _update_task_held(
                        cwd, task_id,
                        lambda changed: changed.update(collected_at=time.time()))
    elif (evidence and record["collected_at"] is None
          and record["state"] != "outcome_unknown"):
        def collect_if_unchanged(changed):
            # A late authenticated publication can refine outcome_unknown
            # while this call assembles its old answer. Never let collection
            # of that stale observation mark the new outcome collected.
            if changed == record:
                changed["collected_at"] = time.time()

        update_task(cwd, task_id, collect_if_unchanged)
    if not evidence and "diff_missing" not in answer:
        answer["diff_missing"] = (
            "the diff could not be published with its task record; the work is kept "
            f"at {work_dir(cwd, task_id)}")
    return answer


def _git_cleanup_path(cwd, task_id):
    return os.path.join(worker_dir(cwd, task_id), GIT_CLEANUP_FILE)


def _git_cleanup_witness(cwd, task_id):
    """`present`, `absent`, or `unknown` for the owned Git retry marker."""
    path = _git_cleanup_path(cwd, task_id)
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or info.st_size != len(GIT_CLEANUP_TOKEN)):
            return "unknown"
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            return "unknown"
        return ("present" if os.read(fd, len(GIT_CLEANUP_TOKEN) + 1)
                == GIT_CLEANUP_TOKEN else "unknown")
    except OSError:
        return "unknown"
    finally:
        os.close(fd)


def _write_git_cleanup_witness(cwd, task_id):
    """Durably retain the retry key before a Git-backed row is retired."""
    directory = _sound_dir(worker_dir(cwd, task_id), create=True)
    if directory is None:
        return False
    existing = _git_cleanup_witness(cwd, task_id)
    if existing == "present":
        try:
            # A visible marker can be the residue of a failed directory fsync.
            # Re-acknowledge both the marker's entry and its containing task
            # directory before it authorizes removal of the durable row.
            _fsync_directory(directory)
            _fsync_directory(os.path.dirname(directory))
        except OSError:
            return False
        return True
    if existing != "absent":
        return False
    try:
        fd, temporary = tempfile.mkstemp(
            dir=directory, prefix=".git-cleanup-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(GIT_CLEANUP_TOKEN)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _git_cleanup_path(cwd, task_id))
            _fsync_directory(directory)
            _fsync_directory(os.path.dirname(directory))
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    except OSError:
        return False
    return _git_cleanup_witness(cwd, task_id) == "present"


def _gitdir_target(entry):
    """One trustworthy absolute target from a Git worktree admin entry.

    Any malformed entry makes the namespace incomplete. The reader is
    bounded, refuses links and non-regular files, checks pathname identity
    after reading, decodes strictly, and accepts exactly Git's one-line path
    format. It never follows a metadata path outside the admin entry.
    """
    try:
        entry_info = os.lstat(entry)
    except OSError:
        return None
    if not stat.S_ISDIR(entry_info.st_mode):
        return None
    path = os.path.join(entry, "gitdir")
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or info.st_size <= 1 or info.st_size > GITDIR_CEILING):
            return None
        raw = os.read(fd, GITDIR_CEILING + 1)
        named = os.lstat(path)
        if ((named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or len(raw) != info.st_size):
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        return None
    try:
        registered = raw[:-1].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not registered or "\x00" in registered or not os.path.isabs(registered):
        return None
    try:
        normalized = os.path.normpath(registered)
        if normalized != registered or os.path.basename(normalized) != ".git":
            return None
        return os.path.realpath(normalized)
    except (OSError, ValueError):
        return None


def _worktree_registration(cwd, work):
    """Return `present`, `absent`, or `unknown` for the exact worktree path.

    Git owns mutation and serialization of its admin namespace. Antiphon only
    performs a bounded, fail-closed read: deleting an admin pathname itself
    could race Git reusing that basename for an unrelated worktree.
    """
    done = _git(cwd, "rev-parse", "--git-common-dir", timeout=10)
    if done is None or done.returncode != 0:
        return "unknown"
    common = done.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    admin = os.path.join(common, "worktrees")
    try:
        names = os.listdir(admin)
    except FileNotFoundError:
        try:
            _fsync_directory(common)
        except OSError:
            return "unknown"
        return "absent"
    except OSError:
        return "unknown"
    wanted = os.path.realpath(os.path.join(work, ".git"))
    for name in names:
        entry = os.path.join(admin, name)
        registered = _gitdir_target(entry)
        if registered is None:
            return "unknown"
        if registered == wanted:
            return "present"
    # Absence, like process death, is authority only when the complete owned
    # namespace was structurally read and its current state is durable.
    try:
        _fsync_directory(admin)
    except OSError:
        return "unknown"
    return "absent"


def _forget_worktree(cwd, work):
    """Verification-only compatibility helper: never mutates Git metadata."""
    return _worktree_registration(cwd, work) == "absent"


_CLEANUP_STABLE_FIELDS = (
    "id", "kind", "task_class", "sha256", "size", "parent", "timeout",
    "hop", "created_at", "pid", "birth", "base", "started_at",
)


def _same_cleanup_generation(left, right):
    """Whether two rows name one lifecycle despite terminal refinement."""
    return (left is not None and right is not None
            and all(left.get(key) == right.get(key)
                    for key in _CLEANUP_STABLE_FIELDS))


def _cleanup_generation_held(cwd, record, row_expected):
    """Whether ``record`` still owns this id's cleanup while locked.

    A terminal row may deliberately outlive its worker directory. Record-first
    retirement instead authorizes cleanup only while every state-bearing row
    and lifecycle marker remains physically absent. A same-id replacement is
    therefore a different generation even if cleanup began before it existed.
    """
    current_path = _path(cwd, record["id"])
    previous_path = os.path.join(
        _legacy_tasks_dir(cwd), record["id"] + ".json")
    if os.path.lexists(previous_path):
        return False
    if not row_expected:
        return (not os.path.lexists(current_path)
                and not os.path.lexists(live_path(cwd, record["id"])))
    if not os.path.lexists(current_path):
        return False
    current = read_task(cwd, record["id"])
    return _same_cleanup_generation(current, record)


def _remove_dir_held(cwd, record, row_expected=True):
    """Best-effort cleanup while one task-id generation is locked."""
    if not _cleanup_generation_held(cwd, record, row_expected):
        return False
    directory = worker_dir(cwd, record["id"])
    work = work_dir(cwd, record["id"])
    witness = _git_cleanup_witness(cwd, record["id"])
    if witness == "unknown":
        return False
    requires_git_proof = (record.get("base") is not None
                          or witness == "present"
                          or os.path.lexists(os.path.join(work, ".git")))
    if requires_git_proof and witness != "present":
        if not _write_git_cleanup_witness(cwd, record["id"]):
            return False
    if requires_git_proof:
        registration = _worktree_registration(cwd, work)
        if registration == "unknown":
            return False
        # Real Git removes the registration by exact physical path even when
        # the directory itself has vanished. It owns the lock and any admin
        # basename reuse; the verifier below never deletes Git metadata.
        _git(cwd, "worktree", "remove", "--force", work)
    # `base` is immutable proof that this worker was registered as a Git
    # worktree. A failed present-day checkout probe must not erase that fact
    # and turn uninspected admin metadata into successful cleanup. The marker
    # keeps the same fact discoverable after record-first retirement.
    if requires_git_proof and not _forget_worktree(cwd, work):
        return False
    shutil.rmtree(directory, ignore_errors=True)
    if os.path.lexists(directory):
        return False
    try:
        parent = workers_dir(cwd)
        if os.path.isdir(parent):
            _fsync_directory(parent)
    except OSError:
        return False
    return True


def _remove_dir(cwd, record, row_expected=True):
    """Remove only the task-id generation observed under the store lock.

    The lock spans Git-admin reconciliation and filesystem deletion. New task
    creation takes the same lock, so no same-id owner can appear between a
    successful generation check and the final directory removal.
    """
    with _locked(cwd) as held:
        return bool(held and _remove_dir_held(
            cwd, record, row_expected=row_expected))


def _finish_cancel(cwd, task_id, record):
    """Return one terminal cancel observation only after durable cleanup."""
    if record["state"] not in TERMINAL:
        raise Refused(
            f"not cancelled: task {task_id}'s terminal state could not be "
            "committed; its work and worker slot are kept")
    if not _sync_task_store(cwd):
        raise Refused(
            f"not cancelled: task {task_id}'s terminal state could not be "
            "durably acknowledged; its work is kept")
    # Cleanup and the final observation share one linearization point.  Once
    # the lock is released, an expired row may be retired and its UUID reused;
    # that later generation must never become this cancel call's answer.
    with _locked(cwd) as held:
        current = read_task(cwd, task_id) if held else None
        if not _same_cleanup_generation(current, record):
            raise Refused(
                f"not cancelled: task {task_id}'s lifecycle changed before "
                "cleanup; its record and work are kept, so inspect it and retry")
        if current["state"] not in WORKER_TERMINAL:
            raise Refused(
                f"not cancelled: task {task_id} is no longer terminal; its "
                "record and work are kept, so inspect it and retry")
        # An unresolved outcome deliberately remains open to a late,
        # authenticated supervisor publication.  Even an unchanged row is not
        # cleanup authority: the publication can already be durable in the
        # live marker while its row-refining reader waits for this lock.
        if record["state"] == "outcome_unknown":
            if current["state"] != "outcome_unknown":
                raise Refused(
                    f"not cancelled: task {task_id} refined to "
                    f"{current['state']} while cancel was finalizing; inspect "
                    "its result before retrying cancel")
            raise Refused(
                f"not cancelled: task {task_id}'s outcome is still unknown; "
                "its possible later result and work are kept until status "
                "resolves it")
        record = current
        if not _remove_dir_held(cwd, record):
            raise Refused(
                f"not cancelled: task {task_id}'s worker directory could not be "
                "removed completely; its terminal record is kept, so retry cancel")
        # Keep the returned row inside the same generation boundary as the
        # completed cleanup.  No writer can enter while this lock is held.
        current = read_task(cwd, task_id)
        if (_same_cleanup_generation(current, record)
                and current["state"] in WORKER_TERMINAL):
            record = current
    return _public_record(record)


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
        recovery = accepted_start_recovery(cwd, record)
        if recovery is not None:
            raise Refused(
                f"not cancelled: task {task_id}: "
                f"{_start_recovery_detail(recovery)}")
        raise Refused(
            f"not cancelled: task {task_id} is still starting; retry after its "
            "running record or refusal is visible")
    if record["state"] == "running":
        lock, _publication_before = _lock_observation(
            cwd, task_id, record=record)
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
                record, pending_group = _finish_published(
                    cwd, record, natural_code, time.time())
                if pending_group is not None:
                    raise Refused(
                        f"not cancelled: task {task_id}'s result was published, "
                        "but its process group has not been proved gone; its "
                        "work and worker slot are kept")
            elif action != "ready":
                raise Refused(
                    f"not cancelled: task {task_id}'s worker identity could not be "
                    "verified; retry when worker liveness can be observed")
            else:
                record, intent = _claim_stop(
                    cwd, record, "cancelled", now=time.time())
                if record is None:
                    return None
                if record["state"] != "running":
                    return _finish_cancel(cwd, task_id, record)
                if intent is None:
                    raise Refused(
                        f"not cancelled: task {task_id}'s stop intent could not be "
                        "recorded; its worker and work are kept")
                stop = _stop_group(cwd, record)
                after_lock, after_code = _lock_observation(
                    cwd, task_id, record=record)
                group_dead = stop in ("stopped", "absent")
                after = ("dead" if group_dead
                         else _worker_liveness(cwd, record, lock=after_lock))
                if after == "dead":
                    code = after_code if after_lock == "published" else None
                    resolution = ("action"
                                  if stop not in ("absent", "not_sent")
                                  else "unknown")
                    if code is not None:
                        record, pending_group = _finish_published(
                            cwd, record, code, time.time(),
                            stop_resolution=resolution,
                            group_dead=group_dead)
                        if pending_group is not None:
                            raise Refused(
                                f"not cancelled: task {task_id}'s result was "
                                "published, but its process group has not been "
                                "proved gone; its work and worker slot are kept")
                    else:
                        state = intent
                        record = _finish(
                            cwd, task_id, state, None,
                            stop_resolution=resolution)
                elif after == "live":
                    raise Refused(
                        f"not cancelled: task {task_id}'s worker still appears live "
                        "after the signal attempt; its work is kept")
                else:
                    raise Refused(
                        f"not cancelled: task {task_id}'s worker could not be proved "
                        "stopped after the signal attempt; its work is kept")
    return _finish_cancel(cwd, task_id, record)


def _discard_stale_accepted(cwd, stale, now):
    """Conditionally retire one dead start from a possibly stale snapshot."""
    discarded = False
    with _locked(cwd) as held:
        if not held:
            return False
        current = read_task(cwd, stale["id"])
        if (current is None or current["state"] != "accepted"
                or now - current["created_at"] <= START_PATIENCE):
            return False
        mutator = _reconcile_unlocked_git_mutator(
            cwd, current["id"], patience=SWEEP_PATIENCE)
        if mutator != "absent":
            return False
        live, _outcome = _lock_observation(
            cwd, current["id"], record=current)
        # A held or unreadable supervisor is not a dead start. An acquired,
        # unlocked lifecycle file is enough for an `accepted` row: no adapter
        # can cross its gate before the durable row becomes `running`.
        if live not in ("absent", "dead", "unlocked_unknown"):
            return False
        # The JSON is removed under the same lock as the state re-read. A
        # concurrent start that has not yet made its live lock cannot commit
        # `running`; its unopened admission gate then makes it self-clean.
        discarded = _discard_record_held(cwd, current["id"])
    if discarded:
        _remove_dir(cwd, stale, row_expected=False)
    return discarded


def _terminal_cleanup_ready(cwd, observed):
    """Revalidate one durable cleanup authority after a fresh store fsync.

    Completed work stays until its promised evidence has been collected.  A
    cancelled task promises no result evidence: its durable terminal row alone
    authorizes retrying the idempotent worker-directory cleanup after a crash.
    """
    if not _sync_task_store(cwd):
        return None
    with _locked(cwd) as held:
        current = read_task(cwd, observed["id"]) if held else None
        if (current != observed
                or current["state"] not in ("completed", "cancelled")
                or (current["state"] == "completed"
                    and current["collected_at"] is None)):
            return None
        return current


def sweep(cwd, now):
    """Remove the directories of collected results and of expired tasks,
    never a running worker's; stop a worker past its timeout without waiting
    for it; drop an `accepted` record whose worker start died. A `handing`
    record is peer-delivery evidence, not a pending worker start, and survives
    until the ordinary task TTL. Then prune."""
    for record in tasks(cwd):
        if record["state"] == "accepted":
            if now - record["created_at"] > START_PATIENCE:
                _discard_stale_accepted(cwd, record, now)
            continue
        record = status(cwd, record["id"], now, patience=SWEEP_PATIENCE) or record
        # Uncertain liveness deliberately keeps a compatible `running` row,
        # its directory and its slot even beyond the ordinary task TTL.
        if record["state"] == "running":
            continue
        expired = _expired(record, now)
        cleanup_ready = (record["state"] == "cancelled"
                         or (record["state"] == "completed"
                             and record["collected_at"] is not None))
        if expired:
            if _retire_expired(cwd, record, now):
                _remove_dir(cwd, record, row_expected=False)
            continue
        if cleanup_ready:
            current = _terminal_cleanup_ready(cwd, record)
            if current is not None:
                _remove_dir(cwd, current)
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


def _write_published_marker(lock_fd, code, proof, stopped):
    """Atomically bind the observed stop bit to the terminal marker.

    Blocking SIGTERM is the publication linearization point. A handler that
    ran before it is included in ``stopped``; a signal arriving afterwards is
    pending until the complete marker is durable and therefore loses to the
    already-started natural publication.
    """
    try:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGTERM})
    except (AttributeError, OSError, ValueError) as error:
        raise OSError("SIGTERM could not be fenced for publication") from error
    try:
        _write_live_marker(
            lock_fd, _published_marker(code, proof, stopped=stopped()))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _worker_wrapper(lock_fd, gate_fd, ready_fd, commit_fd, exit_file, argv,
                    proof=None):
    """Wait for admission, run one adapter, then publish before unlocking."""
    code = 125
    terminate_requested = False
    publishable = True

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
                    # The supervisor retains the previous-epoch lease; the
                    # untrusted adapter inherits no lifecycle or capacity fd.
                    child = subprocess.Popen(argv)
                    if terminate_requested:
                        # Covers a signal delivered between the commit check
                        # and Popen. The group signal also reaches this child.
                        child.terminate()
                    code = child.wait()
                    if code < 0:
                        code = min(255, 128 + abs(code))
                    if (os.getpgrp() == os.getpid()
                            and not _drain_adapter_group(
                                os.getpgrp(), os.getpid())):
                        publishable = False
                        print("antiphon worker: adapter descendants could not be "
                              "proved stopped; outcome was not published",
                              file=sys.stderr)
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
        if publishable:
            try:
                _write_worker_exit(exit_file, code)
            except OSError as error:
                print(f"antiphon worker: legacy outcome mirror could not be written: {error}",
                      file=sys.stderr)
            try:
                _write_published_marker(
                    lock_fd, code, proof, lambda: terminate_requested)
            except OSError as error:
                print(f"antiphon worker: outcome could not be published: {error}",
                      file=sys.stderr)
                # STARTING, ACTIVE or a partial transition remains. Once the
                # lock is released, a reader can tell that no outcome was published.
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
    if len(args) < 8 or args[0] != "_worker_wrapper":
        return 2
    try:
        lock_fd, gate_fd, ready_fd, commit_fd, proof_fd = (
            int(args[1]), int(args[2]), int(args[3]), int(args[4]), int(args[5]))
    except (TypeError, ValueError):
        return 2
    if (lock_fd < 0 or gate_fd < 0 or ready_fd < 0 or commit_fd < 0
            or proof_fd < 0 or not args[6] or not args[7:]):
        return 2
    proof = b""
    try:
        while len(proof) <= 64:
            chunk = os.read(proof_fd, 65 - len(proof))
            if not chunk:
                break
            proof += chunk
    except OSError:
        proof = b""
    finally:
        with contextlib.suppress(OSError):
            os.close(proof_fd)
    try:
        proof = proof.decode("ascii")
    except UnicodeDecodeError:
        return 2
    if (len(proof) != 64
            or any(character not in "0123456789abcdef" for character in proof)):
        return 2
    return _worker_wrapper(
        lock_fd, gate_fd, ready_fd, commit_fd, args[6], args[7:], proof=proof)


def _emit_guardian_output(descriptor, content):
    """Best-effort forwarding after Git is done, including an orphaned run."""
    view = memoryview(content or b"")
    while view:
        try:
            written = os.write(descriptor, view)
        except OSError:
            return
        if written <= 0:
            return
        view = view[written:]


def _drain_guardian_pipe(fd, captured, reads=16):
    """Drain one nonblocking pipe without letting its buffer grow in memory."""
    for _attempt in range(reads):
        try:
            chunk = os.read(fd, 64 * 1024)
        except BlockingIOError:
            return True
        except OSError:
            return False
        if not chunk:
            return False
        room = GIT_GUARDIAN_OUTPUT_CEILING - len(captured)
        if room > 0:
            captured.extend(chunk[:room])
    return True


def _wait_guardian_child(child, pipes, timeout):
    """Wait for the direct Git child while draining, never waiting for EOF."""
    deadline = time.monotonic() + timeout
    while True:
        code = child.poll()
        if code is not None:
            # At most one kernel pipe-buffer remains after the direct writer
            # exits.  Capture what is ready now; a detached descendant does
            # not get to delay the receipt or extend the bounded buffer.
            if pipes:
                ready, _writable, _errors = select.select(
                    list(pipes), [], [], 0)
                for fd in ready:
                    if not _drain_guardian_pipe(fd, pipes[fd]):
                        pipes.pop(fd, None)
            return code
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(["git"], timeout)
        if not pipes:
            try:
                return child.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
        ready, _writable, _errors = select.select(
            list(pipes), [], [], min(0.05, remaining))
        for fd in ready:
            if not _drain_guardian_pipe(fd, pipes[fd]):
                pipes.pop(fd, None)


def _git_guardian_main(args):
    """Hold one start lease around a gated, exactly identified Git process.

    Before Git can exec, its pid/birth is durable in the locked marker.  A
    dead caller can therefore leave this guardian to finish; a dead guardian
    leaves permanent uncertainty rather than cleanup authority.  Git receives
    no lifecycle descriptor, so a hook daemon cannot extend ownership beyond
    the command itself.  The guardian drains stdout and stderr only until the
    direct child returns, then closes its bounded capture pipes without
    waiting for a detached hook to close its copies.
    """
    if len(args) < 5 or args[0] != "_git_guardian":
        return 2
    try:
        lease_fd = int(args[1])
        timeout = float(args[2])
    except (TypeError, ValueError):
        return 2
    cwd = args[3]
    git_args = args[4:]
    if (lease_fd < 0 or not cwd or not git_args or not math.isfinite(timeout)
            or timeout <= 0):
        return 2
    try:
        os.set_inheritable(lease_fd, False)
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            os.close(lease_fd)
        return 2
    gate_read = None
    gate_write = None
    child = None
    stdout_pipe = None
    stderr_pipe = None
    pipes = {}
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    birth = None
    admitted = False
    completed = False
    stdout = b""
    stderr = b""
    code = 125
    try:
        try:
            gate_read, gate_write = os.pipe()
            child = subprocess.Popen(
                [sys.executable, "-E", "-s", "-S",
                 os.path.abspath(__file__), "_git_exec", str(gate_read),
                 cwd, *git_args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(gate_read,), start_new_session=True)
            stdout_pipe = getattr(child, "stdout", None)
            stderr_pipe = getattr(child, "stderr", None)
            for stream, captured in ((stdout_pipe, stdout_buffer),
                                     (stderr_pipe, stderr_buffer)):
                if stream is None:
                    continue
                os.set_blocking(stream.fileno(), False)
                pipes[stream.fileno()] = captured
            os.close(gate_read)
            gate_read = None
            deadline = time.time() + START_IDENTITY_PATIENCE
            while birth is None and time.time() < deadline:
                birth = _process_start(child.pid)
                if birth is None:
                    time.sleep(0.02)
            if birth is None:
                stderr += b"Git process identity could not be recorded\n"
            else:
                _write_live_marker(
                    lease_fd, _git_mutator_marker(child.pid, birth))
                if os.write(gate_write, b"1") != 1:
                    raise OSError("the Git start gate was only partly written")
                admitted = True
                os.close(gate_write)
                gate_write = None
                try:
                    code = _wait_guardian_child(child, pipes, timeout)
                except subprocess.TimeoutExpired:
                    # A return that won the timeout race is still directly
                    # observable.  Once a signal is attempted, however, death
                    # cannot stand in for the guardian's durable receipt: an
                    # untracked descendant may already have detached.
                    code = child.poll()
                    if code is None:
                        _kill_group(
                            child.pid, patience=1.0,
                            revalidate=lambda: _git_mutator_state(
                                child.pid, birth) == "live")
                        try:
                            _wait_guardian_child(child, pipes, 2.0)
                        except subprocess.TimeoutExpired:
                            stderr += b"Git did not stop after timeout\n"
                        code = 124
                    else:
                        code = _wait_guardian_child(child, pipes, 0.0)
                        _write_live_marker(lease_fd, LIVE_STARTING)
                        completed = True
                else:
                    # Direct Git return is the guardian's completion receipt.
                    # Detached hook code has no lifecycle descriptor and is
                    # arbitrary same-uid filesystem activity, outside this
                    # cooperative command boundary for success or failure.
                    _write_live_marker(lease_fd, LIVE_STARTING)
                    completed = True
        except OSError as error:
            stderr += f"git could not start: {error}\n".encode(
                "utf-8", "backslashreplace")
    finally:
        _close_fds(gate_read, gate_write)
        if child is not None and child.poll() is None:
            if not admitted:
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if birth is not None:
                        _kill_group(
                            child.pid, patience=0.25,
                            revalidate=lambda: _git_mutator_state(
                                child.pid, birth) == "live")
            else:
                _kill_group(
                    child.pid, patience=0.25,
                    revalidate=lambda: _git_mutator_state(
                        child.pid, birth) == "live")
            with contextlib.suppress(subprocess.TimeoutExpired):
                _wait_guardian_child(child, pipes, 1.0)
        # The marker is already either a positive direct-return receipt or the
        # durable in-flight identity.  Release lifecycle ownership before any
        # best-effort diagnostics. The bounded pipe buffers are closed next;
        # a detached hook can keep neither this lease nor a capture sink.
        with contextlib.suppress(OSError):
            os.close(lease_fd)
        for stream in (stdout_pipe, stderr_pipe):
            if stream is None:
                continue
            with contextlib.suppress(OSError):
                stream.close()
        stdout = bytes(stdout_buffer)
        stderr = bytes(stderr_buffer) + stderr
    _emit_guardian_output(sys.stdout.fileno(), stdout)
    _emit_guardian_output(sys.stderr.fileno(), stderr)
    return code if completed else 125


def _git_exec_main(args):
    """Cross a one-byte gate, then become Git without any lifecycle fd."""
    if len(args) < 4 or args[0] != "_git_exec":
        return 2
    try:
        gate_fd = int(args[1])
    except (TypeError, ValueError):
        return 2
    cwd = args[2]
    git_args = args[3:]
    if gate_fd < 0 or not cwd or not git_args:
        return 2
    try:
        admitted = os.read(gate_fd, 1) == b"1"
    except OSError:
        admitted = False
    finally:
        with contextlib.suppress(OSError):
            os.close(gate_fd)
    if not admitted:
        return 125
    try:
        os.execvp("git", ["git", "-C", cwd, *git_args])
    except OSError as error:
        print(f"git could not start: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments[:1] == ["_git_guardian"]:
        raise SystemExit(_git_guardian_main(arguments))
    if arguments[:1] == ["_git_exec"]:
        raise SystemExit(_git_exec_main(arguments))
    raise SystemExit(_worker_wrapper_main(arguments))
