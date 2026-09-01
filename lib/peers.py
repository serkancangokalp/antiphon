"""Peer identity: naming, the socket key, and the registry both sides read.

A peer is one agent session working in one project directory. Antiphon assumed
exactly one per side and never said so; this module is the part that lets
several coexist without taking each other's sockets and cursors.

An explicit name is the operator's identity override. Without one, a canonical
host session UUID may derive a stable public `auto-` alias, but only after the
host-specific proof described by each caller. A Claude session whose proof
fails occupies the reserved `UNNAMED` key below; a Codex hook records only a
private observation until positive writer-lock evidence projects its automatic
alias. The full UUID and digest remain internal.

A Codex peer is written by two processes that never meet. The MCP server owns
`endpoint.json` and knows the pid; the hook owns `session.json` and knows the
session id, which is the address. Each writes its own file, so neither can lose
the other's fields, and `read_peers` joins the two on the owner key when it is
read. Anything that cannot be joined is listed as live and unroutable rather
than guessed at.
"""

import base64
import bisect
import collections
import contextlib
import fcntl
import hashlib
import json
import math
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
VERSIONED_OWNER_PATTERN = re.compile(
    r"([1-9][0-9]*):v([1-9][0-9]*):\S(?:.*\S)?")
PROCESS_FINGERPRINT_VERSION = 1
OWNER_KEY_VERSION = f"v{PROCESS_FINGERPRINT_VERSION}"
OBSERVATION_VERSION = 1
IDENTITY_PROOF_VERSION = 1
# The canonical UUID a Codex session is named by, lowercase as the CLI writes it
# and as `antiphon.SESSION_ID` reads it back off a rollout file name. A contract
# test keeps the two spellings from drifting apart.
SESSION_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                r"[0-9a-f]{4}-[0-9a-f]{12}")
IDENTITY_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
AUTO_NAME_PREFIX = "auto-"


def explicit_name():
    """The name set for this session, or "" when none was set."""
    return (os.environ.get("ANTIPHON_NAME") or "").strip().lower()


# The registry key a peer with no name occupies. It is not a name: the angle
# brackets are outside the alias grammar, so nothing anyone can type or write in
# an `@claude:` marker can ever be it, and the two can never collide. It is the
# same spelling the visible label uses, because it is the same idea — one word
# for "this peer has no name", wherever that has to be said.
#
# The check against it is exact, never a prefix or a shape: `claude-abc` is a
# name somebody may deliberately choose, and inferring from the look of a name
# would take their alias away over a resemblance. An earlier version generated
# `claude-<3hex>` for an unnamed session, which was a real name in the registry
# for a peer that told the other side it had none — and a message addressed to
# that key resolved.
UNNAMED = "<unnamed>"


def valid_name(name):
    """Whether `name` is a public alias: what a person may type, what an
    `@claude:` marker may carry, what a reply may be addressed to.

    Both this and `valid_kind` are handed values that came out of JSON — a
    tool argument, a marker, a record read off disk — so a non-string is
    refused rather than passed to `fullmatch`, which raises on one."""
    return isinstance(name, str) and bool(NAME_PATTERN.fullmatch(name))


def valid_key(kind, name):
    """Whether `name` may be this kind of peer's place in the registry.

    Every explicit or automatic public alias may, on either side. The reserved
    key may only on the Claude side, because that is the only thing it
    represents: a channel server whose automatic proof failed but which still
    serves the legacy socket. An unproved Codex session deliberately has no
    **peer** record; its separate hook-owned observation carries no key or
    address until a read-only positive-liveness projection derives the public
    alias. A peer record under the reserved key would be live but impossible to
    name, making every bare message ambiguous while remaining unreachable.

    Directory names and record fields are checked with this; addressing is
    checked with `valid_name`, which is narrower still.
    """
    return valid_name(name) or (kind == "claude" and name == UNNAMED)


def valid_kind(kind):
    """`kind` is concatenated into a directory name; unvalidated, `../..` walks
    out of the project."""
    return isinstance(kind, str) and bool(KIND_PATTERN.fullmatch(kind))


def valid_owner_key(key):
    """A key the registry is willing to record as an identity.

    Anything else is refused rather than stored and ignored: a malformed key
    would register cleanly and then join nothing, which looks like a peer that
    simply never came back.
    """
    return isinstance(key, str) and bool(OWNER_PATTERN.fullmatch(key))


def owner_key_version(key):
    """The explicit owner-key generation, or None for legacy/invalid keys."""
    if not valid_owner_key(key):
        return None
    matched = VERSIONED_OWNER_PATTERN.fullmatch(key)
    return int(matched.group(2)) if matched else None


def owner_generations_mixed(left, right):
    """Whether two valid keys for one pid use different schema generations.

    A legacy key has no generation. It is deliberately not normalised from its
    rendered clock: the timezone and locale that produced it were never stored.
    """
    if not (valid_owner_key(left) and valid_owner_key(right)):
        return False
    left_pid, _, _ = left.partition(":")
    right_pid, _, _ = right.partition(":")
    if left_pid != right_pid:
        return False
    return owner_key_version(left) != owner_key_version(right)


def valid_session_id(value):
    """A canonical UUID and nothing else.

    This becomes an address. An id that is not one routes a message at nothing
    and does it silently, which is the whole failure this registry exists to
    end. `fullmatch`, like every other pattern here: `$` also matches before a
    trailing newline.
    """
    return isinstance(value, str) and bool(SESSION_ID_PATTERN.fullmatch(value))


def looks_like_session_id(value):
    """Whether a configured/routing value has the private host UUID shape.

    Address validation stays canonical and lowercase in `valid_session_id`.
    Secrecy is broader on purpose: case or surrounding whitespace must not turn
    the same host identifier into text an error is allowed to echo.
    """
    return (isinstance(value, str)
            and bool(SESSION_ID_PATTERN.fullmatch(value.strip().lower())))


def valid_identity_digest(value):
    """A complete lower-case SHA-256 digest, never its public truncation."""
    return (isinstance(value, str)
            and bool(IDENTITY_DIGEST_PATTERN.fullmatch(value)))


def auto_name_from_digest(digest):
    """The public alias carried by one complete identity digest, or None."""
    if not valid_identity_digest(digest):
        return None
    raw = bytes.fromhex(digest)
    public = base64.b32encode(raw[:16]).decode("ascii").rstrip("=").lower()
    return AUTO_NAME_PREFIX + public


def auto_identity(session_id):
    """``(public alias, full digest)`` for one canonical host UUID."""
    if not valid_session_id(session_id):
        return None
    digest = hashlib.sha256(session_id.encode("ascii")).hexdigest()
    return auto_name_from_digest(digest), digest


def _identity_digest_of(record):
    """A valid automatic record's full digest, else None.

    Explicit and legacy records carry neither field. A partial or malformed
    automatic marker is not silently treated as explicit: callers that need to
    distinguish corruption use ``_record_identity_valid`` alongside this.
    """
    if not hasattr(record, "get") or record.get("automatic") is not True:
        return None
    digest = record.get("identity_digest")
    if not valid_identity_digest(digest):
        return None
    return (digest if auto_name_from_digest(digest) == record.get("name")
            else None)


def _record_identity_valid(record):
    """Whether explicit/legacy or automatic identity metadata is coherent."""
    if not hasattr(record, "get"):
        return False
    automatic = record.get("automatic")
    digest_present = "identity_digest" in record
    if automatic is None and not digest_present:
        return True
    if automatic is not True:
        return False
    digest = _identity_digest_of(record)
    if digest is None:
        return False
    if "session_id" in record:
        identity = auto_identity(record.get("session_id"))
        return identity is not None and identity[1] == digest
    return True


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


RETIRED_HALF_VERSION = 1


def retired_half_path(cwd, kind, name):
    """Evidence that this peer's session half was withdrawn, not never written.

    Beside the two halves, and never joinable by anything. Withdrawal deletes
    the half so nothing can route through it; without this record the deletion
    is indistinguishable from a peer whose first hook has not run, and the two
    call for opposite actions — one listener must wait, the other must retire.
    """
    return os.path.join(peer_dir(cwd, kind, name), "retired.json")


def _valid_retired_half(record, owner, identity_digest):
    """Whether a tombstone belongs to this endpoint, exactly.

    Total, like every other record read here. A tombstone from another owner or
    another identity says nothing about this one, and a record that cannot be
    trusted must never authorise the one destructive action in this contract.
    The session id is part of that: a tombstone that names none cannot tell
    "the owner moved on from me" from "the owner came back to me".
    """
    if not isinstance(record, dict) or set(record) != RETIRED_HALF_KEYS:
        return False
    version = record.get("version")
    withdrawn = record.get("session_id")
    return (isinstance(version, int) and not isinstance(version, bool)
            and version == RETIRED_HALF_VERSION
            and record.get("kind") == "claude"
            and record.get("owner") == owner
            and valid_owner_key(owner)
            and record.get("identity_digest") == identity_digest
            and valid_identity_digest(identity_digest)
            and valid_session_id(withdrawn)
            and auto_identity(withdrawn) == (
                auto_name_from_digest(identity_digest), identity_digest))


def session_half_missing(cwd, kind, name):
    """True only when the hook's half is genuinely absent.

    `_session_address` answers None for six different reasons and only one of
    them is "no half was ever written". A read that failed is evidence of
    nothing, and a torn record is evidence of corruption — neither is evidence
    that this owner moved on, and only that may retire a listener.
    """
    if not (valid_kind(kind) and valid_key(kind, name)):
        return False
    try:
        os.stat(_session_file(cwd, kind, name))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def retired_half(cwd, kind, name, owner, identity_digest, current_session_id):
    """True when a rotation withdrew this endpoint's half and has not returned.

    Positive on both halves of the question: the tombstone names the session it
    withdrew, and the owner's current session must be a different one. A host
    that resumes a session id is not a rotation, and a listener reconnecting
    under an identity its owner is on again must not read stale about itself.
    """
    if not (valid_kind(kind) and valid_key(kind, name)):
        return False
    if not session_half_missing(cwd, kind, name):
        return False
    record = _read_record(retired_half_path(cwd, kind, name))
    if not (record and _valid_retired_half(record, owner, identity_digest)):
        return False
    if not (valid_session_id(current_session_id)
            and current_session_id != record.get("session_id")):
        return False
    # The caller's proof was read before any of this observed the filesystem,
    # and a rotation can land in between. `record_claude_session` writes the
    # new proof before the new half, so an A→B→A resume has a window where the
    # proof names A, the half is still gone and the tombstone still names A —
    # and a reader holding the older snapshot would retire the identity that
    # just became current again. Re-read and require agreement; a snapshot that
    # moved is not one this may destroy anything on.
    state, current = read_identity_proof(cwd, owner)
    return state == "valid" and current.get("session_id") == current_session_id


def _session_file(cwd, kind, name):
    """The hook's record, beside the server's.

    The server knows the pid and never the session id; the hook knows the
    session id and must never claim a pid, having usually exited by the time
    anyone reads it. Two files, one writer each.
    """
    return os.path.join(peer_dir(cwd, kind, name), "session.json")


IdentityProofInventory = collections.namedtuple(
    "IdentityProofInventory", "proofs completeness")


def identity_proofs_dir(cwd):
    """Owner-current automatic-Claude identity proofs, one file per owner."""
    return os.path.join(cwd, ".antiphon", "identity", "claude")


def _owner_digest(owner_key):
    return hashlib.sha256(owner_key.encode("utf-8")).hexdigest()


def identity_proof_path(cwd, owner_key):
    """The proof file for one owner, named from a digest and never the key."""
    return os.path.join(identity_proofs_dir(cwd),
                        _owner_digest(owner_key) + ".json")


IDENTITY_PROOF_KEYS = frozenset({
    "version", "kind", "owner_key", "owner_digest", "session_id",
    "identity_digest"})
RETIRED_HALF_KEYS = frozenset({
    "version", "kind", "owner", "identity_digest", "session_id"})


def _valid_identity_proof(record, owner_digest):
    """Whether a record is a usable proof for the owner whose digest names it.

    Total on purpose. Every field is checked, including the two relations that
    make the record self-consistent: the filename must be the digest of the
    owner key stored inside, so a record cannot be planted or renamed under
    another owner's name, and the identity digest must be the one derived from
    the stored session id, so the two halves cannot disagree about who this is.
    """
    if not isinstance(record, dict):
        return False
    # Exactly these keys. An ignored key is still a key somebody wrote, and it
    # can carry text — the Node reader takes the version's spelling from the
    # source, so a value holding a `"version": 1.0` literal parted the two
    # readers. The version field is how a shape change gets coordinated; until
    # it is bumped, an unknown key means this is not that shape.
    if set(record) != IDENTITY_PROOF_KEYS:
        return False
    version = record.get("version")
    if (not isinstance(version, int) or isinstance(version, bool)
            or version != IDENTITY_PROOF_VERSION):
        return False
    if record.get("kind") != "claude":
        return False
    owner = record.get("owner_key")
    if not valid_owner_key(owner) or _owner_digest(owner) != owner_digest:
        return False
    if record.get("owner_digest") != owner_digest:
        return False
    session_id = record.get("session_id")
    if not valid_session_id(session_id):
        return False
    identity = auto_identity(session_id)
    return identity is not None and record.get("identity_digest") == identity[1]


def _read_identity_proof_file(path, owner_digest):
    """`(state, proof)` for one proof file: valid, absent, unreadable, invalid.

    Three facts, never one `None`. Absent means no proof was ever written and
    a first automatic endpoint may claim a candidate slot; unreadable means the
    answer is unknown and nothing may be concluded; invalid means a record is
    there and cannot be trusted. Collapsing them would let a corrupt file read
    as absent and open the claim it must have refused.
    """
    # Read bytes and decode beside the parse, not at `open`. A text-mode read
    # raises `UnicodeDecodeError`, which subclasses `ValueError` and not
    # `OSError`, so it escaped both arms above and travelled out of every
    # caller — including the sweep, which runs after the rotation has committed
    # and promises never to raise. Non-UTF-8 bytes are a torn record: `invalid`
    # is exactly what "a record is there and cannot be trusted" means.
    try:
        with open(path, "rb") as stream:
            raw = stream.read(RECORD_CEILING + 1)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    if len(raw) > RECORD_CEILING:
        return "invalid", None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "invalid", None
    if not _valid_identity_proof(record, owner_digest):
        return "invalid", None
    return "valid", record


def read_identity_proof(cwd, owner_key):
    """The classified proof for one owner. An unusable key is never absent."""
    if not valid_owner_key(owner_key):
        return "invalid", None
    return _read_identity_proof_file(identity_proof_path(cwd, owner_key),
                                     _owner_digest(owner_key))


def _write_identity_proof_locked(cwd, owner_key, session_id, identity_digest):
    """Replace one owner's proof atomically. The caller holds the lock."""
    if not valid_owner_key(owner_key) or not valid_session_id(session_id):
        return False
    identity = auto_identity(session_id)
    if identity is None or identity[1] != identity_digest:
        return False
    path = identity_proof_path(cwd, owner_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "version": IDENTITY_PROOF_VERSION,
        "kind": "claude",
        "owner_key": owner_key,
        "owner_digest": _owner_digest(owner_key),
        "session_id": session_id,
        "identity_digest": identity_digest,
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def write_identity_proof(cwd, owner_key, session_id, identity_digest):
    """Record which session this owner is running now, under the registry lock.

    The unlocked core is separate because the rotation transaction holds the
    lock across several steps and the registry lock is not reentrant.
    """
    with _registry_lock(cwd):
        return _write_identity_proof_locked(cwd, owner_key, session_id,
                                            identity_digest)


def identity_proofs(cwd):
    """Every valid proof, and how much of the truth this answer covers.

    A bare list would make a genuinely empty directory and an unreadable one
    both come back empty, and `status` would report a confident zero from a
    state it could not read. `completeness` is `exact` when every entry was
    read, `lower-bound` when some entry failed but a valid one survives, and
    `unknown` when nothing valid could be read at all. Reading mutates nothing.
    """
    directory = identity_proofs_dir(cwd)
    try:
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries
                           if entry.is_file() and entry.name.endswith(".json"))
    except FileNotFoundError:
        return IdentityProofInventory((), "exact")
    except OSError:
        return IdentityProofInventory((), "unknown")
    found, failed = [], False
    for name in names:
        state, proof = _read_identity_proof_file(
            os.path.join(directory, name), name[:-len(".json")])
        if state == "valid":
            found.append(proof)
        else:
            failed = True
    if not failed:
        return IdentityProofInventory(tuple(found), "exact")
    return IdentityProofInventory(
        tuple(found), "lower-bound" if found else "unknown")


# ---- proof reclamation -----------------------------------------------------
#
# A proof outlives endpoints on purpose, so nothing on a read path may remove
# one. Reclamation therefore lives on the write path that already holds the
# lock — `rotate_identity_proof`, which is the call production actually makes.
# A collector with no real caller would let a unit test pass while production
# collected nothing, forever.
#
# Three properties, and each of them is a decision:
#
# - **Cheap evidence only.** Death is `ProcessLookupError` and nothing else.
#   The `ps` fingerprint path costs seconds and this runs inside a hook; a
#   recycled pid answers signal 0 and is conservatively kept, which is the safe
#   direction. Keeping a dead owner's file costs one small file; deleting a live
#   owner's erases the evidence `status` and `doctor` need.
# - **Progress, not merely a bound.** Examining the first eight of a sorted
#   inventory on every write bounds latency and guarantees nothing: eight live
#   records at the front would starve a dead one behind them forever. A
#   persistent cursor makes each write resume where the last stopped and wrap,
#   so every record is reached within a bounded number of writes.
# - **Cooperative, not wall-clock.** The budget is checked between records and
#   only after one record of progress, so a slow machine finishes the record it
#   started rather than being cut mid-decision.
IDENTITY_SWEEP_WINDOW = 8              # records examined per rotation
IDENTITY_SWEEP_BUDGET = 0.050          # seconds, checked between records

# Indirection so a test can drive a deterministic clock. Asserting on real
# elapsed time under a real scheduler would be flaky, and the budget is
# cooperative rather than a cap anyone can measure from outside.
_sweep_clock = time.monotonic


def identity_sweep_cursor_path(cwd):
    """Where the sweep remembers what it examined last.

    Beside the proofs rather than among them: a cursor inside that directory
    would be enumerated as a proof, read as invalid, and would permanently
    downgrade the inventory's completeness to `lower-bound` — turning an
    optimisation into a lie about how much of the truth an answer covers.
    """
    return os.path.join(cwd, ".antiphon", "identity", "sweep.json")


def _read_sweep_cursor(cwd):
    """The proof filename the last sweep stopped after, or "" to start over.

    Malformed resets rather than refusing. The cursor has no correctness role —
    it only decides where to look first — so refusing to sweep because it
    cannot be parsed would stall reclamation forever over a file nobody can
    repair.
    """
    try:
        with open(identity_sweep_cursor_path(cwd), encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return ""
    after = record.get("after") if isinstance(record, dict) else None
    return after if isinstance(after, str) else ""


def _write_sweep_cursor_locked(cwd, after):
    """Persist where to resume. Best effort: the caller has already committed."""
    path = identity_sweep_cursor_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump({"version": IDENTITY_PROOF_VERSION, "after": after},
                      stream, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        # Same discipline as the proof write beside it: a failed replace must
        # not leave its temporary behind. The name is per-pid and the file
        # lives outside the enumerated proofs directory, so this is tidiness
        # rather than correctness — but the asymmetry was the kind that reads
        # as an oversight later.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _proved_dead(owner_key):
    """True only when the kernel says that pid does not exist.

    Deliberately weaker than `_owner_liveness`: no start-time comparison, so no
    `ps`. A recycled pid reads as not-dead here, and a proof whose owner is a
    stranger simply survives another sweep.
    """
    if not valid_owner_key(owner_key):
        return False
    try:
        pid = int(owner_key.split(":", 1)[0])
    except (ValueError, AttributeError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError, ValueError):
        # EPERM means somebody else's live process. `OWNER_PATTERN` puts no
        # ceiling on the pid, so a key naming one above the platform's signed
        # int raises `OverflowError` — not an `OSError` — and would otherwise
        # escape a sweep that promises never to raise. Anything that is not
        # `ProcessLookupError` is evidence of nothing, and none of it is proof
        # of death.
        return False
    return False


def _sweep_identity_proofs_locked(cwd, protect):
    """Reclaim proofs whose owner is proved dead. Lock held; never raises.

    Runs after the rotation has committed, so every failure here is swallowed:
    a hook that already made the routing decision correct must not then report
    failure because a housekeeping pass could not finish.
    """
    reclaimed = 0
    try:
        directory = identity_proofs_dir(cwd)
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries
                           if entry.is_file() and entry.name.endswith(".json"))
    except OSError:
        return reclaimed
    if not names:
        return reclaimed
    after = _read_sweep_cursor(cwd)
    # Resume after the cursor and wrap: `bisect` gives the first name strictly
    # greater, and a cursor naming a file since removed lands in the right place
    # anyway, which is why the cursor holds a name rather than an index.
    start = bisect.bisect_right(names, after) if after else 0
    if start >= len(names):
        start = 0
    examined = None
    deadline = _sweep_clock() + IDENTITY_SWEEP_BUDGET
    for step in range(min(IDENTITY_SWEEP_WINDOW, len(names))):
        # Between records, and never before the first: a slow machine finishes
        # the record it started rather than being cut mid-decision, so the
        # sweep always makes at least one record of progress.
        if step and _sweep_clock() >= deadline:
            break
        name = names[(start + step) % len(names)]
        examined = name
        digest = name[: -len(".json")]
        if digest in protect:
            continue
        # One record's failure is one record skipped, never the whole sweep and
        # never the rotation above it. The classification below is total today;
        # this belt is here so that staying total is not a precondition for the
        # promise in this function's docstring.
        try:
            state, record = _read_identity_proof_file(
                os.path.join(directory, name), digest)
            # An unreadable or invalid record is not proved dead. It survives:
            # this sweep removes evidence only where death is positive, and a
            # record it cannot read is not evidence of death but the absence
            # of evidence.
            if state != "valid" or not _proved_dead(record.get("owner_key")):
                continue
            os.unlink(os.path.join(directory, name))
            reclaimed += 1
        except Exception:
            continue
    if examined is not None:
        try:
            _write_sweep_cursor_locked(cwd, examined)
        except OSError:
            # The cursor is an optimisation. Losing it costs a repeated window
            # next time, never correctness.
            pass
    return reclaimed


RotationOutcome = collections.namedtuple(
    "RotationOutcome", "ok prior current withdrawn")


def _withdraw_stale_automatic_sessions_locked(cwd, owner_key, current_digest):
    """Retire this owner's outgrown automatic session halves. Lock held.

    Only a half that is automatic — it carries an identity digest — belongs to
    this owner, and no longer matches the current identity. An explicit or
    legacy record carries no digest and is never touched; another owner's
    record is never touched; and an endpoint is never touched at all, because
    only the process serving it may withdraw its own registration.
    """
    removed = []
    try:
        entries = sorted(os.listdir(peers_dir(cwd)))
    except OSError:
        return removed
    prefix = "claude-"
    for entry in entries:
        if not entry.startswith(prefix):
            continue
        name = entry[len(prefix):]
        # Off disk, and about to become a path. `_scan` validates the
        # same way; this is the one place in this module that did not.
        if not valid_key("claude", name):
            continue
        path = _session_file(cwd, "claude", name)
        record = _read_record(path)
        if not record or record.get("owner") != owner_key:
            continue
        # Deleting a record needs the same structural proof reading one does.
        # "Same owner, and some string digest that differs" admitted a half
        # that is not automatic at all, one whose digest derives a different
        # alias than the directory it sits in, and one whose session id is not
        # canonical — and that last one takes the silent path, because a
        # tombstone cannot be written without a session id to name.
        digest = _identity_digest_of(record)
        if (digest is None or digest == current_digest
                or not _record_identity_valid(record)
                or auto_name_from_digest(digest) != name
                or not valid_session_id(record.get("session_id"))):
            continue
        # Evidence first, then the deletion it explains. Unlinking first and
        # failing to write left exactly the state the tombstone exists to end —
        # a half that is gone with nothing saying why — while the rotation
        # reported the withdrawal as done.
        withdrawn = record.get("session_id")
        if valid_session_id(withdrawn) and not _write_retired_half_locked(
                cwd, name, owner_key, digest, withdrawn):
            continue
        with contextlib.suppress(OSError):
            os.unlink(path)
            removed.append(name)
    _collect_retired_halves_locked(cwd, entries)
    return removed


def _write_retired_half_locked(cwd, name, owner_key, identity_digest,
                               session_id):
    """Record that this peer was joined and is no longer. Lock held.

    Returns whether the evidence is on disk. A withdrawal this cannot evidence
    is not one the caller may make.
    """
    path = retired_half_path(cwd, "claude", name)
    record = {"version": RETIRED_HALF_VERSION, "kind": "claude",
              "owner": owner_key, "identity_digest": identity_digest,
              "session_id": session_id}
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def _collect_retired_halves_locked(cwd, entries):
    """Drop tombstones whose endpoint is gone. Lock held; best effort.

    Once the listener has withdrawn its own endpoint the tombstone has no
    reader left — nothing enumerates a peer without one. Collected on the pass
    that writes them, so it costs no extra traversal and cannot grow without
    bound. A live endpoint's tombstone is never touched: that is exactly the
    record the listener has not acted on yet.
    """
    prefix = "claude-"
    for entry in entries:
        if not entry.startswith(prefix):
            continue
        name = entry[len(prefix):]
        # Off disk, and about to become a path. `_scan` validates the
        # same way; this is the one place in this module that did not.
        if not valid_key("claude", name):
            continue
        path = retired_half_path(cwd, "claude", name)
        # `os.path.exists` is False on EACCES and ENOTDIR as well as on
        # absence, so a transient stat failure on a peer directory would have
        # read as "the endpoint is gone" and discarded the evidence that lets a
        # live stale listener retire — degrading it to UNREADY for good. Every
        # other deletion in this contract waits for positive proof; so does
        # this one.
        try:
            os.stat(path)
        except OSError:
            continue
        try:
            os.stat(_peer_file(cwd, "claude", name))
            continue                      # the endpoint is there; keep it
        except FileNotFoundError:
            pass
        except OSError:
            continue                      # unreadable is not gone
        with contextlib.suppress(OSError):
            os.unlink(path)


def rotate_identity_proof(cwd, owner_key, session_id, identity_digest):
    """Make this session current for its owner, in one locked transaction.

    Capture the prior proof, replace it, and retire the session halves that
    replacement made stale — all inside one acquisition. It cannot be assembled
    from helpers that each take the lock: the registry lock is not reentrant,
    and two concurrent rotations would interleave between acquisitions and
    corrupt both the prior-proof answer and the judgement of which half is now
    stale.

    Returns the prior and current records so the caller can wake exactly one
    listener — the previous current alias — rather than every stale half, which
    would grow hook latency with history.
    """
    with _registry_lock(cwd):
        digest = _owner_digest(owner_key) if valid_owner_key(owner_key) else ""
        prior_state, prior = _read_identity_proof_file(
            identity_proof_path(cwd, owner_key), digest
        ) if valid_owner_key(owner_key) else ("invalid", None)
        if not _write_identity_proof_locked(cwd, owner_key, session_id,
                                            identity_digest):
            return RotationOutcome(False, prior, None, ())
        withdrawn = _withdraw_stale_automatic_sessions_locked(
            cwd, owner_key, identity_digest)
        _state, current = _read_identity_proof_file(
            identity_proof_path(cwd, owner_key), digest)
        # After the transaction, under the same acquisition, and unable to fail
        # it: the proof this rotation just wrote is protected by name, and every
        # other error is swallowed inside the sweep.
        _sweep_identity_proofs_locked(cwd, {digest})
        return RotationOutcome(True, prior if prior_state == "valid" else None,
                               current, tuple(withdrawn))


def observations_dir(cwd):
    """Hook-owned Codex sightings, separate from the routable peer registry."""
    return os.path.join(cwd, ".antiphon", "observations", "codex")


def _observation_file(cwd, session_id):
    return os.path.join(observations_dir(cwd), session_id + ".json")


def write_observation(cwd, session_id):
    """Record that a Codex hook supplied one canonical host session id.

    This does not claim an alias, address or liveness. Each host id owns one
    atomically replaced file, so concurrent hooks for different sessions never
    read-modify-write shared state and a reader never sees a partial record.
    """
    if not valid_session_id(session_id):
        return False
    path = _observation_file(cwd, session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {"version": OBSERVATION_VERSION, "kind": "codex",
              "session_id": session_id, "observed_at": time.time()}
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False)
    os.replace(tmp, path)
    return True


def configured_name_present():
    """Whether this session was configured with a name at all.

    Presence and usability are different facts. Empty and whitespace-only are
    absence — `explicit_name` documents that, and the end-to-end harness uses a
    bare `ANTIPHON_NAME=` in ten places as the way to run unnamed — but a
    person who wrote anything else meant to be named, and must not be given a
    different identity silently.
    """
    raw = os.environ.get("ANTIPHON_NAME")
    return isinstance(raw, str) and bool(raw.strip())


def withdraw_observation(cwd, session_id):
    """Retire this session's own observation. Idempotent, and only its own.

    An observation carries version, kind, session id and time — nothing about
    which environment wrote it — so no later predicate can tell a
    configured-invalid record from a legitimate one. The hook that created it
    is the only thing that can say, so it withdraws durably rather than hoping
    a reader will ignore it.
    """
    if not valid_session_id(session_id):
        return False
    try:
        os.unlink(_observation_file(cwd, session_id))
    except FileNotFoundError:
        return True                       # already withdrawn: idempotent
    except OSError:
        return False
    return True


def read_observations(cwd):
    """Validated Codex sightings in deterministic host-id order, read-only."""
    try:
        names = sorted(os.listdir(observations_dir(cwd)))
    except OSError:
        return []
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        session_id = name[:-5]
        if not valid_session_id(session_id):
            continue
        record = _read_record(os.path.join(observations_dir(cwd), name))
        if not record:
            continue
        observed_at = record.get("observed_at")
        version = record.get("version")
        try:
            finite_time = math.isfinite(float(observed_at))
        except (OverflowError, TypeError, ValueError):
            finite_time = False
        if (type(version) is not int
                or version != OBSERVATION_VERSION
                or record.get("kind") != "codex"
                or record.get("session_id") != session_id
                or isinstance(observed_at, bool)
                or not isinstance(observed_at, (int, float))
                or not finite_time
                or observed_at < 0):
            continue
        found.append(record)
    return found


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


# Every record here is a handful of fields, and these readers sit on the
# inbound-delivery path. A ceiling costs nothing and matches the Node mirror,
# which grew one first — leaving Python willing to read a padded record the
# other reader refused, which is a divergence in the destructive direction.
RECORD_CEILING = 64 * 1024


def _read_record(path):
    """The record as a dict, or None. Valid JSON of the wrong shape is not a
    record: a bare array used to raise out of `read_peers` on `.get`."""
    try:
        with open(path, "rb") as f:
            raw = f.read(RECORD_CEILING + 1)
        if len(raw) > RECORD_CEILING:
            return None
        record = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
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


def _session_address(cwd, peer):
    """The session id this endpoint's own hook has claimed for it, or None.

    Kind-neutral, and used both ways. For a Codex endpoint the id is also the
    address: a Codex peer registers before it has one and this is what it comes
    to answer to. For a Claude endpoint it is not an address at all — the
    socket is — and the id says only which transcript that peer is writing,
    which is what a pull page joins a session label on. Same halves, same
    owner, same refusal either way.

    The two halves are joined on the owner key and nothing else. A missing
    session record, one with no owner, one from a different owner, and one whose
    id is not a canonical UUID all read the same way: live, not routable. There
    is no rule that reaches for the likeliest session, because reaching for the
    likeliest is what the silent misrouting was.

    `.get`, never `[...]`: a half-written record must not raise out of every
    read of the registry. `name` is validated before it becomes a path — it
    comes off disk, and `../..` would read a record from outside the project.
    """
    kind, name = peer.get("kind"), peer.get("name")
    if not (valid_kind(kind) and valid_key(kind, name)
            and _record_identity_valid(peer)):
        return None
    owner = _owner_of(peer)
    session = _read_record(_session_file(cwd, kind, name))
    if not (owner and session and _record_identity_valid(session)
            and session.get("owner") == owner):
        return None
    endpoint_digest = _identity_digest_of(peer)
    session_digest = _identity_digest_of(session)
    if endpoint_digest != session_digest:
        return None
    claimed = session.get("session_id")
    return claimed if valid_session_id(claimed) else None


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
    # A real integer, not something `int()` will accept. A numeric string and
    # an integral float both passed here and were refused by the Node reader,
    # so the same record made one side route and the other refuse — and `True`
    # is `1` in Python, which would have named pid 1.
    pid = record.get("pid") if hasattr(record, "get") else None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    return pid if pid > 0 else None


def _birth_of(record):
    """The start time of the process the record was written for, or None.

    None is two different histories with one correct reading. The record may
    predate this field, or `ps` may have had nothing to say when the claim was
    made. Neither is evidence that the pid has been recycled, so both fall back
    to the liveness the registry has always used.
    """
    birth = record.get("birth") if hasattr(record, "get") else None
    if not isinstance(birth, str) or not birth.strip():
        return None
    return birth


def _birth_is_current(record):
    """Whether `birth` was rendered by the canonical process reader.

    Records written before this marker carry a local-time, local-locale string.
    A mismatch with today's UTC/C rendering says nothing about their process,
    so only a current marker makes strict comparison safe.
    """
    return (hasattr(record, "get")
            and record.get("birth_version") == PROCESS_FINGERPRINT_VERSION)


def alive(pid):
    """True if the process still exists. Signal 0 checks without delivering.

    A process that has exited but not yet been reaped is a zombie and still
    answers this, so a peer can read as live for the window before its parent
    reaps it. The cost is a delivery attempt that fails loudly against a socket
    nobody serves, never a silent misroute.

    This answers "somebody holds that number", which is weaker than what any
    caller here wants to know. `_record_alive` is what they ask; this is one
    half of its answer.
    """
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _record_alive(record):
    """Whether the process this record was written for is the one still running.

    Every liveness decision in the registry goes through here, because they are
    all the same decision and one of them getting it wrong is enough. A pid is a
    number the kernel hands out again; `owner_key` has always said so by pairing
    a pid with a start time, and liveness used to contradict it by asking only
    whether the number was in use. An endpoint that crashed without releasing
    its claim therefore came back to life the moment its number was reassigned
    to an unrelated process — holding an alias, holding an address, and standing
    between a Codex session and the address it was entitled to.

    Three readings, and only the last one is a corpse:

    - no fingerprint in the record: it predates the field or its owner could not
      be fingerprinted at registration. The pid alone, exactly as before.
    - a fingerprint, and none readable now: `ps` failed, which is evidence of
      nothing. Releasing a peer that may well be live over a lookup that could
      not be made would trade a rare bug for a common one.
    - a fingerprint, and a different one: the process this record names is gone
      and its number belongs to somebody else. Dead, and prunable.
    """
    pid = _pid_of(record)
    if pid is None or not alive(pid):
        return False
    recorded = _birth_of(record)
    if recorded is None or not _birth_is_current(record):
        return True
    observed = _process_birth(pid)
    return observed is None or observed == recorded


def _record_liveness(record, cache=None):
    """Strict read-only endpoint liveness for scheduling evidence.

    Registry routing preserves legacy PID-only behavior in ``_record_alive``.
    Scheduling is a different decision: it may demote a source only from
    reproducible current-generation evidence, so legacy fingerprints and a
    failed process read are ``unknown`` rather than guessed live or dead.
    """
    pid = _pid_of(record)
    birth = _birth_of(record)
    version = (record.get("birth_version")
               if hasattr(record, "get") else None)
    key = (pid, version, birth)
    if cache is not None and key in cache:
        return cache[key]
    result = "unknown"
    if (pid is not None and version == PROCESS_FINGERPRINT_VERSION
            and birth is not None):
        if not alive(pid):
            result = "dead"
        else:
            observed = _process_birth(pid)
            if observed is not None:
                result = "live" if observed == birth else "dead"
    if cache is not None:
        cache[key] = result
    return result


def _prune(cwd, kind, name, dead_pid):
    """Removes a dead peer's record, but only if it is still that peer's.

    Re-read under the lock: between the unlocked read that spotted the corpse
    and this call, a new owner may have claimed the name, and deleting its fresh
    record would leave a live peer invisible. The directory stays, so a peer
    returning under the same name finds its cursor where it left it.
    """
    if not (valid_kind(kind) and valid_key(kind, name)):
        return
    with _registry_lock(cwd):
        held = _read_record(_peer_file(cwd, kind, name))
        held_pid = _pid_of(held) if held else None
        if held_pid is None or held_pid != dead_pid:
            return
        if _record_alive(held):
            return
        path = _peer_file(cwd, kind, name)
        try:
            os.unlink(path)
        except OSError:
            pass


def _scan(cwd):
    """Every readable record that agrees with the directory holding it.

    Unlocked and unpruned; safe to call under the lock.

    The directory is a peer's real identity — it is where every writer for that
    peer puts its files. The `kind` and `name` inside a record are what the rest
    of this module builds paths and decisions from, so a record claiming a name
    other than its own directory's would send the session join looking inside
    another peer, and report an address for an endpoint that does not exist. A
    record that disagrees with where it lives is not read at all.

    Split at the first hyphen only: neither kind contains one, so everything
    after it belongs to the alias and `codex-my-build` keeps its name.
    """
    try:
        entries = sorted(os.listdir(peers_dir(cwd)))
    except OSError:
        return []
    records = []
    for entry in entries:
        kind, _, name = entry.partition("-")
        if not (valid_kind(kind) and valid_key(kind, name)):
            continue
        record = _read_record(os.path.join(peers_dir(cwd), entry, "endpoint.json"))
        if record is None:
            continue
        if record.get("kind") != kind or record.get("name") != name:
            continue
        if not _record_identity_valid(record):
            continue
        records.append(record)
    return records


def _scan_sessions(cwd, kind=None):
    """Validated session records, without pruning or writing registry state."""
    try:
        entries = sorted(os.listdir(peers_dir(cwd)))
    except OSError:
        return []
    records = []
    for entry in entries:
        entry_kind, _, name = entry.partition("-")
        if not (valid_kind(entry_kind) and valid_key(entry_kind, name)):
            continue
        if kind is not None and entry_kind != kind:
            continue
        record = _read_record(os.path.join(
            peers_dir(cwd), entry, "session.json"))
        if record is None:
            continue
        if (record.get("kind") != entry_kind
                or record.get("name") != name):
            continue
        if not _record_identity_valid(record):
            continue
        records.append(record)
    return records


def read_peers(cwd, kind=None):
    """Live peers, newest first. Records left by dead processes are removed.

    Live is `_record_alive`, not a signal to a pid: a record whose process is
    gone is still removed when its number has been handed to somebody else.

    Live is not the same as reachable. A Codex peer that has not been given its
    address yet is listed with `address` set to `None`, because a peer nobody
    can name is a peer an ambiguity refusal cannot mention. This is the single
    public reading of the registry, so every caller that intends to *deliver*
    something has to check the address it got rather than assume one.

    It is also where the two halves of a Codex peer are joined: an addressless
    endpoint takes the session id its own hook recorded, and only its own. The
    join happens on the way out rather than at either write, so neither writer
    ever has to read the other's file.

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
        if not _record_alive(peer):
            _prune(cwd, peer.get("kind"), peer.get("name"), peer_pid)
            continue
        if kind is None or peer.get("kind") == kind:
            if _addressless(peer):
                peer["address"] = _session_address(cwd, peer)
            found.append(peer)
    found.sort(key=_started_at, reverse=True)
    return found


AUTOMATIC_REGISTRATION_MODES = ("initial", "reassert")


def _automatic_claim_refusal(cwd, kind, owner_key, identity_digest, mode):
    """Why this claim is refused, or "" to allow it. The lock is held.

    Scoped structurally to automatic Claude: every Codex path and every
    explicit or legacy Claude peer passes untouched, because `register` is
    shared and this contract is not.
    """
    if mode not in AUTOMATIC_REGISTRATION_MODES:
        return "unknown automatic registration mode; no claim was changed"
    if kind != "claude" or identity_digest is None:
        return ""
    if not valid_owner_key(owner_key):
        return "an automatic claim needs a canonical owner key"
    state, proof = _read_identity_proof_file(
        identity_proof_path(cwd, owner_key), _owner_digest(owner_key))
    if state == "valid":
        if proof.get("identity_digest") == identity_digest:
            return ""
        return ("the owner's current identity proof names another session; "
                "no claim was changed")
    if state == "absent":
        if mode == "initial":
            return ""        # nothing proved yet: an UNREADY candidate slot
        return "reassert needs a current identity proof; none exists"
    # Unreadable and invalid are not absent. Reading either as "no proof yet"
    # would open the very claim this exists to refuse.
    return f"the owner's identity proof is {state}; no claim was changed"


def register(cwd, kind, name, address, pid=None, owner_key=None,
             identity_digest=None, mode=None):
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
    if not valid_key(kind, name):
        return False, (f"invalid peer name {name!r} for a {kind} peer: "
                       "expected [a-z0-9][a-z0-9_-]{0,31}")
    automatic = identity_digest is not None
    if automatic and (not valid_identity_digest(identity_digest)
                      or auto_name_from_digest(identity_digest) != name):
        return False, ("invalid automatic identity: the full digest does not "
                       "derive the requested peer name")
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
    # Observed here and taken from nowhere else. There is no parameter for it
    # and no field of the payload reaches it: a fingerprint a caller could hand
    # in is a stale record vouching for itself, which is the one claim the
    # comparison exists to disbelieve. Read before the lock — `ps` is a
    # subprocess, and nothing about it needs the registry held still.
    birth = _process_birth(owner_pid)
    with _registry_lock(cwd):
        for other in _scan(cwd):
            other_pid = _pid_of(other)
            if other_pid is None or not _record_alive(other):
                continue
            if other.get("kind") != kind:
                continue                  # a rollout id and a socket path never collide
            if other.get("name") == name:
                other_digest = _identity_digest_of(other)
                if ((automatic and other_digest != identity_digest)
                        or (not automatic and other_digest is not None)):
                    return False, (f"automatic identity collision at peer name "
                                   f"{name!r}; no claim was changed")
                if other_pid == owner_pid:
                    continue              # this process refreshing its own record
                if kind == "codex" and owner_key and _owner_of(other) == owner_key:
                    # Codex can bring up a second MCP server for one CLI session
                    # before the first has exited. Judged by pid alone the
                    # newcomer looks like an intruder, and the session locks
                    # itself out of its own name until its predecessor is
                    # reaped. A shared key excuses a differing pid; a dead owner
                    # is already gone above, so it never excuses a missing
                    # process.
                    #
                    # Codex only, on purpose. A Claude endpoint is a socket
                    # this process is serving, and two channel servers under one
                    # CLI root would otherwise let the second overwrite the
                    # first's record while the first's socket is still the one
                    # answering — the registry would then describe a server
                    # nobody reaches. A rollout id is not owned that way.
                    continue
                return False, f"peer name {name!r} is already held by pid {other_pid}"
            if address is not None and _address_of(other) == address:
                # The contended resource is the address, not the name, and the
                # two races are different. Two sessions under one alias are
                # caught above; this catches two *different* aliases carrying
                # one address — a Codex session registering a second name
                # against its own rollout, or a hand-written record — where the
                # registry would show two peers while a message addressed to
                # either reached whichever actually held it.
                return False, (f"address {address!r} is already served by peer "
                               f"{other.get('name')!r} (pid {other_pid})")
        if mode is not None:
            # Inside the lock that writes the endpoint, never one hop earlier:
            # a proof validated in Node and a registration performed here are
            # two moments, and the proof can move in between.
            refusal = _automatic_claim_refusal(cwd, kind, owner_key,
                                               identity_digest, mode)
            if refusal:
                return False, refusal
        path = _peer_file(cwd, kind, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {"kind": kind, "name": name, "pid": owner_pid,
                  "address": address, "started_at": time.time()}
        if owner_key:
            record["owner"] = owner_key
        if automatic:
            record["automatic"] = True
            record["identity_digest"] = identity_digest
        if birth:
            # Kept apart from `started_at`, which is when the claim was made and
            # is what the listing sorts on. This is when the process was born,
            # and it is the half of its identity the number does not carry.
            record["birth"] = birth
            record["birth_version"] = PROCESS_FINGERPRINT_VERSION
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
    return True, ""


def unregister(cwd, kind, name, pid=None):
    """Releases a name, but only if this owner still holds it."""
    if not (valid_kind(kind) and valid_key(kind, name)):
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


def read_session(cwd, kind, name):
    """The hook's record for an alias as a dict, or None."""
    if not (valid_kind(kind) and valid_key(kind, name)):
        return None
    record = _read_record(_session_file(cwd, kind, name))
    return record if record and _record_identity_valid(record) else None


def write_session(cwd, kind, name, session_id, transcript, owner,
                  identity_digest=None, require_endpoint=False):
    """Records which session is behind an alias. Returns (ok, detail).

    Refuses when a live endpoint holds the alias for a different owner, and
    touches nothing at all in that case: the session that got there first keeps
    working and this one is told. The guard is on the **endpoint**, not on any
    session record already present. A guard that compared session owners could
    only refuse a second owner once the first one's hook had run, and a server
    that has registered without an id yet is precisely the peer that is live and
    about to become routable.

    An alias no endpoint holds is writable: the hook can fire before the server
    registers, and the record simply waits. It is not a peer until an endpoint
    appears — the registry is listed from `endpoint.json`, so a session record
    on its own describes nobody.

    No pid is written. The hook has usually exited by the time anyone reads
    this, and a pid it left behind would mark the peer dead on the next read.
    """
    if not (valid_kind(kind) and valid_key(kind, name)):
        return False, f"invalid peer {kind!r}/{name!r}"
    if not valid_session_id(session_id):
        return False, ("invalid session id: the supplied value is not echoed; "
                       "expected a canonical UUID")
    if not valid_owner_key(owner):
        return False, (f"invalid owner key {owner!r}: expected a pid and the "
                       "start time that tells it from a recycled one")
    automatic = identity_digest is not None
    if automatic and (not valid_identity_digest(identity_digest)
                      or auto_name_from_digest(identity_digest) != name
                      or auto_identity(session_id) != (name, identity_digest)):
        return False, ("invalid automatic identity: the full digest does not "
                       "derive the requested peer and session")
    with _registry_lock(cwd):
        endpoint = _read_record(_peer_file(cwd, kind, name))
        endpoint_alive = bool(
            endpoint and _record_identity_valid(endpoint)
            and _record_alive(endpoint))
        endpoint_digest = (_identity_digest_of(endpoint)
                           if endpoint_alive else None)
        if endpoint_alive and ((automatic and endpoint_digest != identity_digest)
                               or (not automatic and endpoint_digest is not None)):
            return False, (f"automatic identity collision at peer name {name!r}; "
                           "its record was not touched")
        if require_endpoint and (not endpoint_alive
                                 or _owner_of(endpoint) != owner
                                 or endpoint_digest != identity_digest):
            return False, (f"automatic peer {name!r} has no matching live "
                           "endpoint proof; its record was not touched")
        if endpoint and _owner_of(endpoint) != owner and _record_alive(endpoint):
            if _owner_of(endpoint) is None:
                # An unreadable owner is not a different owner. The write is
                # still refused — an endpoint that names nobody cannot be shown
                # to be this session's — but the endpoint is most often the
                # caller's own, from before the field existed or from a tree
                # `owner_key` could not walk, and calling it another live
                # session names the reader's own pid as somebody else.
                return False, (f"alias {name!r} is held by a live {kind} endpoint "
                               f"that records no owner (pid {_pid_of(endpoint)}); "
                               "its record was not touched")
            return False, (f"alias {name!r} is held by another live {kind} session "
                           f"(pid {_pid_of(endpoint)}); its record was not touched")
        record = {"kind": kind, "name": name, "owner": owner,
                  "session_id": session_id}
        if automatic:
            record["automatic"] = True
            record["identity_digest"] = identity_digest
        if isinstance(transcript, str) and transcript.strip():
            # Nothing is ever delivered to a transcript path. Refusing the whole
            # record over a missing one would cost the session its address for a
            # field no message travels through.
            record["transcript"] = transcript
        path = _session_file(cwd, kind, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
        # A host can resume a session id, and the tombstone is about the
        # current state rather than a permanent mark. Writing a half is the
        # event that ends "withdrawn"; leaving it would make a rejoined peer
        # read stale forever.
        with contextlib.suppress(OSError):
            os.unlink(retired_half_path(cwd, kind, name))
    return True, ""


# ---------- owner key: pairing two writers on one session ----------

CLI_ROOTS = ("claude", "codex")
MAX_ANCESTRY = 8


def _process_info(pid):
    """(ppid, start time, command) for a live pid, or None.

    Separated from the walk so tests can drive it without building real process
    trees. `ps` renders in UTC and the C locale regardless of its caller, so the
    same live process has one spelling in every host process. The C fields are
    split structurally instead of slicing at a locale-dependent width.
    """
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "TZ": "UTC"})
    try:
        out = subprocess.run(["ps", "-o", "ppid=,lstart=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5,
                             env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    fields = out.split(None, 6)
    if len(fields) != 7:
        return None
    ppid, weekday, month, day, clock, year, command = fields
    if not (ppid.isdigit() and day.isdigit() and year.isdigit()
            and re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", clock)):
        return None
    start = " ".join((weekday, month, day, clock, year))
    command = command.strip()
    return (ppid, start, command) if command else None


def _process_birth(pid):
    """The start time `ps` reports for `pid` itself, or None when it has none.

    The same reading `owner_key` builds its key from, asked about one process
    instead of a walk: two processes that hold one number in turn were born at
    different moments, and that difference is all the registry needs to tell
    them apart. Seconds of resolution is the resolution `owner_key` already
    trusts for the same purpose.
    """
    info = _process_info(pid)
    return (info[1] or None) if info else None


def _owner_liveness(key, cache=None):
    """Return ``live``, ``dead`` or ``unknown`` for a current owner key.

    Only the current canonical generation can prove either direction. Legacy
    and future keys have no reproducible fingerprint here, while a failed
    process observation is evidence of nothing. ``cache`` is keyed by the
    complete owner key so every page census asks about one CLI owner once.
    """
    if cache is not None and key in cache:
        return cache[key]
    result = "unknown"
    if owner_key_version(key) == PROCESS_FINGERPRINT_VERSION:
        pid_text, _version, recorded = key.split(":", 2)
        pid = int(pid_text)
        if not alive(pid):
            result = "dead"
        else:
            observed = _process_birth(pid)
            if observed is not None:
                result = "live" if observed == recorded else "dead"
    if cache is not None:
        cache[key] = result
    return result


def owner_key(pid=None):
    """A versioned `"<root pid>:vN:<start>"` for the CLI above `pid`.

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
            return f"{current}:{OWNER_KEY_VERSION}:{start}"
        if parent in ("0", "1", current):
            return None
        current = parent
    return None
