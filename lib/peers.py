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
import time

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def explicit_name():
    """The name set for this session, or "" when none was set."""
    return (os.environ.get("ANTIPHON_NAME") or "").strip().lower()


def auto_name(kind, session_id):
    """`claude-a3f` — enough to tell two sessions apart in a listing."""
    short = re.sub(r"[^0-9a-f]", "", (session_id or "").lower())[:3] or "000"
    return f"{kind}-{short}"


def valid_name(name):
    return bool(name) and bool(NAME_PATTERN.match(name))


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
def _peer_lock(cwd, kind, name):
    """Serializes claim, refresh, prune and release for one peer name."""
    directory = peer_dir(cwd, kind, name)
    os.makedirs(directory, exist_ok=True)
    fd = os.open(os.path.join(directory, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_record(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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
    if not (kind and valid_name(name)):
        return
    with _peer_lock(cwd, kind, name):
        path = _peer_file(cwd, kind, name)
        held = _read_record(path)
        if not held or int(held.get("pid") or -1) != int(dead_pid or -1):
            return
        if alive(held.get("pid")):
            return
        try:
            os.unlink(path)
        except OSError:
            pass


def read_peers(cwd, kind=None):
    """Live peers, newest first. Records left by dead processes are removed.

    A record that cannot be parsed is skipped rather than raised: a half-written
    entry must never take the bridge down with it.
    """
    try:
        entries = sorted(os.listdir(peers_dir(cwd)))
    except OSError:
        return []
    found = []
    for entry in entries:
        peer = _read_record(os.path.join(peers_dir(cwd), entry, "endpoint.json"))
        if peer is None:
            continue
        if not alive(peer.get("pid")):
            _prune(cwd, peer.get("kind"), peer.get("name"), peer.get("pid"))
            continue
        if kind is None or peer.get("kind") == kind:
            found.append(peer)
    found.sort(key=lambda p: p.get("started_at") or 0, reverse=True)
    return found


def register(cwd, kind, name, address, pid=None):
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

    There is deliberately no `transcript` parameter. That field belongs to
    `session.json`, whose only writer is the hook; accepting it here would invite
    exactly the cross-writer overwrite the split exists to prevent.
    """
    if not valid_name(name):
        return False, (f"invalid peer name {name!r}: "
                       "expected [a-z0-9][a-z0-9_-]{0,31}")
    owner = int(pid or os.getpid())
    with _peer_lock(cwd, kind, name):
        path = _peer_file(cwd, kind, name)
        held = _read_record(path)
        if held and int(held.get("pid") or -1) != owner and alive(held.get("pid")):
            return False, f"peer name {name!r} is already held by pid {held.get('pid')}"
        record = {"kind": kind, "name": name, "pid": owner,
                  "address": address, "started_at": time.time()}
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
    return True, ""


def unregister(cwd, kind, name, pid=None):
    """Releases a name, but only if this owner still holds it."""
    if not valid_name(name):
        return
    owner = int(pid or os.getpid())
    with _peer_lock(cwd, kind, name):
        path = _peer_file(cwd, kind, name)
        held = _read_record(path)
        if held and int(held.get("pid") or -1) != owner:
            return
        try:
            os.unlink(path)
        except OSError:
            pass
