"""Peer identity: naming and the socket key.

A peer is one agent session working in one project directory. Antiphon assumed
exactly one per side and never said so; this module is the part that lets
several coexist without taking each other's sockets and cursors.

An explicit name is what buys isolation. A Claude session's hook cannot work out
which peer it belongs to on its own — `channel.mjs` has no access to the
transcript UUID, so the two would invent different automatic names for one
session. When `ANTIPHON_NAME` is set they read the same value from the inherited
environment and agree. Automatic names identify a session in listings; they do
not isolate it.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# Both sides are matched with `fullmatch`, never `match`: `$` also matches just
# before a trailing newline, so `re.match` accepted "ui\n" as a peer name and it
# would have gone straight into a file name and a socket seed.
KIND_PATTERN = re.compile(r"claude|codex")
# A pid and the start time that tells it from a recycled one, as `owner_key`
# below produces it. A bare pid is refused deliberately: it is the recycled
# number the start time exists to rule out.
OWNER_PATTERN = re.compile(r"[1-9][0-9]*:\S(?:.*\S)?")


def explicit_name():
    """The name set for this session, or "" when none was set."""
    return (os.environ.get("ANTIPHON_NAME") or "").strip().lower()


def auto_name(kind, session_id):
    """`claude-a3f` — enough to tell two sessions apart in a listing."""
    short = re.sub(r"[^0-9a-f]", "", (session_id or "").lower())[:3] or "000"
    return f"{kind}-{short}"


def valid_name(name):
    return bool(name) and bool(NAME_PATTERN.fullmatch(name))


def valid_kind(kind):
    """`kind` is concatenated into a directory name; unvalidated, `../..` walks
    out of the project."""
    return bool(kind) and bool(KIND_PATTERN.fullmatch(kind))


def valid_owner_key(key):
    """A key the registry is willing to record as an identity.

    Anything else is refused rather than stored and ignored: a malformed key
    would register cleanly and then join nothing, which looks like a peer that
    simply never came back.
    """
    return isinstance(key, str) and bool(OWNER_PATTERN.fullmatch(key))


def socket_key(cwd, name=""):
    """Hashed, never appended: the path must not grow past the platform's limit.

    An empty name reproduces the pre-multi-peer key byte for byte, so an unnamed
    session keeps the socket it already has.
    """
    base = os.path.abspath(cwd)
    seed = base if not name else f"{base}\0{name}"
    return hashlib.sha256(seed.encode()).hexdigest()[:20]


def peers_dir(cwd):
    return os.path.join(cwd, ".antiphon", "peers")


def peer_dir(cwd, kind, name):
    """One directory per peer: its records and its cursor live together."""
    return os.path.join(peers_dir(cwd), f"{kind}-{name}")


def _peer_file(cwd, kind, name):
    """The record written only by the process that owns the peer.

    One file per writer: the hook writes `session.json` beside it, so the two
    never read-modify-write the same document and cannot lose each other's
    fields.
    """
    return os.path.join(peer_dir(cwd, kind, name), "endpoint.json")


@contextlib.contextmanager
def _registry_lock(cwd):
    """Serializes every claim, refresh, prune and release in this project.

    One lock for the whole registry rather than one per name: a claim has to
    check the name *and* the address, and two claims holding different per-name
    locks would not be serialized against each other at all. Contention is a
    handful of sessions, so a single lock costs nothing and removes the ordering
    problem entirely. It is not reentrant, so nothing called while it is held may
    take it again.
    """
    directory = peers_dir(cwd)
    os.makedirs(directory, exist_ok=True)
    fd = os.open(os.path.join(directory, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_record(path):
    """The record as a dict, or None. Valid JSON of the wrong shape is not a
    record: a bare array used to raise out of `read_peers` on `.get`."""
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _address_of(record):
    """A usable address, or None. An empty address is not a peer: stored, it made
    the single-peer resolver fall back to the legacy socket without saying so."""
    address = record.get("address") if hasattr(record, "get") else None
    if not isinstance(address, str) or not address.strip():
        return None
    return address


def _owner_of(record):
    """The record's owner key, or None. Never its pid: they are two different
    identities, and a reader that takes one for the other joins nothing."""
    owner = record.get("owner") if hasattr(record, "get") else None
    return owner if valid_owner_key(owner) else None


def _addressless(record):
    """True for the one shape that is live without being reachable.

    A Codex server is handed a project directory and nothing else; the rollout
    id it answers to arrives with the first message. Between those two moments
    the peer exists and can be named, which is what an ambiguity refusal needs,
    and it is stored with its address explicitly `None`.

    Every other unusable address stays skipped — empty, blank, wrong type, or
    absent altogether. Those say nothing about being on their way, and reading
    silence as a claim is the guess this registry exists to refuse.
    """
    return (record.get("kind") == "codex"
            and record.get("address", "") is None
            and _owner_of(record) is not None)


def _started_at(record):
    """The record's timestamp as a float, or 0. Sorting a float against a string
    raises, and it would raise inside every read of the registry."""
    try:
        return float(record.get("started_at"))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _pid_of(record):
    """A usable owner pid, or None when the record identifies nobody.

    Anything that is not a positive integer names no process, so it cannot be
    checked for liveness and must not hold a name hostage either.
    """
    try:
        pid = int(record.get("pid"))
    except (AttributeError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def alive(pid):
    """True if the process still exists. Signal 0 checks without delivering.

    A process that has exited but not yet been reaped is a zombie and still
    answers this, so a peer can read as live for the window before its parent
    reaps it. The cost is a delivery attempt that fails loudly against a socket
    nobody serves, never a silent misroute.
    """
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _prune(cwd, kind, name, dead_pid):
    """Removes a dead peer's record, but only if it is still that peer's.

    Re-read under the lock: between the unlocked read that spotted the corpse
    and this call, a new owner may have claimed the name, and deleting its fresh
    record would leave a live peer invisible. The directory stays, so a peer
    returning under the same name finds its cursor where it left it.
    """
    if not (valid_kind(kind) and valid_name(name)):
        return
    with _registry_lock(cwd):
        held = _read_record(_peer_file(cwd, kind, name))
        held_pid = _pid_of(held) if held else None
        if held_pid is None or held_pid != dead_pid:
            return
        if alive(held_pid):
            return
        path = _peer_file(cwd, kind, name)
        try:
            os.unlink(path)
        except OSError:
            pass


def _scan(cwd):
    """Every readable record, unlocked and unpruned. Safe to call under the lock."""
    try:
        entries = sorted(os.listdir(peers_dir(cwd)))
    except OSError:
        return []
    records = []
    for entry in entries:
        record = _read_record(os.path.join(peers_dir(cwd), entry, "endpoint.json"))
        if record is not None:
            records.append(record)
    return records


def read_peers(cwd, kind=None):
    """Live peers, newest first. Records left by dead processes are removed.

    Live is not the same as reachable. A Codex peer that has not been given its
    address yet is listed with `address` set to `None`, because a peer nobody
    can name is a peer an ambiguity refusal cannot mention. This is the single
    public reading of the registry, so every caller that intends to *deliver*
    something has to check the address it got rather than assume one.

    A record that cannot be parsed is skipped rather than raised: a half-written
    entry must never take the bridge down with it.
    """
    found = []
    for peer in _scan(cwd):
        peer_pid = _pid_of(peer)
        if peer_pid is None:
            continue                  # identifies nobody; not a peer, not prunable
        if _address_of(peer) is None and not _addressless(peer):
            continue                  # reaches nobody, and is not on its way
        if not alive(peer_pid):
            _prune(cwd, peer.get("kind"), peer.get("name"), peer_pid)
            continue
        if kind is None or peer.get("kind") == kind:
            found.append(peer)
    found.sort(key=_started_at, reverse=True)
    return found


def register(cwd, kind, name, address, pid=None, owner_key=None):
    """Claims `name` for `pid`. Returns (ok, detail).

    `pid` is the process whose life the peer's life follows, and it is often not
    the caller. `channel.mjs` registers by shelling out to a short-lived Python
    subprocess; recording that subprocess's pid would mark the peer dead the
    instant the call returned.

    The whole read-check-write runs under an exclusive `flock`. An `O_EXCL`
    create is not enough on its own: it makes an *empty* file visible before the
    record is written, so a second claimant reads nothing, concludes the record
    is unowned, and takes it. Measured — two racing claimants both won every
    time with `O_EXCL` and exactly one wins under the lock. The bridge is
    Unix-only already, so a lock file costs nothing in portability.

    `owner_key` is the session two writers share, and it is kept strictly apart
    from `pid`, which is the process whose life the record follows. The
    parameter is not called `owner` because that was already the local holding
    the resolved pid; the two would have shadowed each other and written a
    number where the join expects a key. The local is `owner_pid` now, and the
    field the record stores the key under is `owner`.

    A `None` address is accepted from a Codex peer that has a valid owner key,
    and from nothing else. It is stored as `None` rather than as a sentinel: a
    `"pending"` string would be an address as far as the collision check is
    concerned, and the second Codex server would be refused for one the first
    does not really serve.

    There is deliberately no `transcript` parameter. That field belongs to
    `session.json`, whose only writer is the hook; accepting it here would invite
    exactly the cross-writer overwrite the split exists to prevent.
    """
    if not valid_kind(kind):
        return False, f"invalid peer kind {kind!r}: expected 'claude' or 'codex'"
    if not valid_name(name):
        return False, (f"invalid peer name {name!r}: "
                       "expected [a-z0-9][a-z0-9_-]{0,31}")
    if address is None:
        if kind != "codex":
            return False, (f"invalid peer address {address!r}: only a Codex peer "
                           "may register before it has one")
        if not valid_owner_key(owner_key):
            return False, (f"invalid peer address {address!r}: omitting it takes a "
                           f"valid owner key, got {owner_key!r}")
    elif _address_of({"address": address}) is None:
        return False, f"invalid peer address {address!r}: expected a non-empty string"
    if owner_key is not None and not valid_owner_key(owner_key):
        return False, (f"invalid owner key {owner_key!r}: expected a pid and the "
                       "start time that tells it from a recycled one")
    owner_pid = _pid_of({"pid": pid}) if pid is not None else os.getpid()
    if owner_pid is None:
        return False, f"invalid owner pid {pid!r}: expected a positive integer"
    with _registry_lock(cwd):
        for other in _scan(cwd):
            other_pid = _pid_of(other)
            if other_pid is None or not alive(other_pid):
                continue
            if other.get("kind") != kind:
                continue                  # a rollout id and a socket path never collide
            if other.get("name") == name:
                if other_pid == owner_pid:
                    continue              # this process refreshing its own record
                if owner_key and _owner_of(other) == owner_key:
                    # Codex can bring up a second MCP server for one CLI session
                    # before the first has exited. Judged by pid alone the
                    # newcomer looks like an intruder, and the session locks
                    # itself out of its own name until its predecessor is
                    # reaped. A shared key excuses a differing pid; a dead owner
                    # is already gone above, so it never excuses a missing
                    # process.
                    continue
                return False, f"peer name {name!r} is already held by pid {other_pid}"
            if address is not None and _address_of(other) == address:
                # The contended resource is the address, not the name. Two
                # sessions that both found a socket path free would bind it in
                # turn and register under different automatic names carrying the
                # same address, and a message addressed to either would reach
                # whichever actually held the socket.
                return False, (f"address {address!r} is already served by peer "
                               f"{other.get('name')!r} (pid {other_pid})")
        path = _peer_file(cwd, kind, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {"kind": kind, "name": name, "pid": owner_pid,
                  "address": address, "started_at": time.time()}
        if owner_key:
            record["owner"] = owner_key
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
    return True, ""


def unregister(cwd, kind, name, pid=None):
    """Releases a name, but only if this owner still holds it."""
    if not (valid_kind(kind) and valid_name(name)):
        return
    owner = _pid_of({"pid": pid}) if pid is not None else os.getpid()
    if owner is None:
        return
    with _registry_lock(cwd):
        path = _peer_file(cwd, kind, name)
        held = _read_record(path)
        held_pid = _pid_of(held) if held else None
        if held_pid is not None and held_pid != owner:
            return
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------- owner key: pairing two writers on one session ----------

CLI_ROOTS = ("claude", "codex")
MAX_ANCESTRY = 8


def _process_info(pid):
    """(ppid, start time, command) for a live pid, or None.

    Separated from the walk so tests can drive it without building real process
    trees. The start time is `ps`'s `lstart`, a fixed 24 characters wide.
    """
    try:
        out = subprocess.run(["ps", "-o", "ppid=,lstart=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        ppid, rest = out.split(None, 1)
    except ValueError:
        return None
    return ppid, rest[:24].strip(), rest[24:].strip()


def owner_key(pid=None):
    """`"<root pid>:<start time>"` for the CLI session above `pid`, or None.

    On the Codex side no single process knows which session it belongs to: the
    hook is handed a session id and exits, and the long-lived server is handed
    only a project directory. They pair up by walking to the same CLI process.

    The start time is part of the key because a pid alone is recycled, and a
    recycled pid matching the wrong session is exactly the silent
    misidentification this refuses to make. For the same reason there is no
    environment override: a key anyone could set would let one session claim
    another's identity.

    None means no key, which means fall back to what the bridge does today. It
    never returns a best guess.
    """
    current = str(pid or os.getpid())
    for _ in range(MAX_ANCESTRY):
        info = _process_info(current)
        if not info:
            return None
        parent, start, command = info
        head = os.path.basename((command.split() or [""])[0])
        if head in CLI_ROOTS:
            return f"{current}:{start}"
        if parent in ("0", "1", current):
            return None
        current = parent
    return None
