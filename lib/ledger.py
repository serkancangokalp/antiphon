"""The delivery ledger: what was sent, refused, received and read.

One directory, `.antiphon/deliveries/`, one file per delivery attempt. Sends
write their entry after the transport returns, because writing it first could
leave a ghost attempt after a crash before transport. The page readers, which
already walk the peer's transcript and already recognise the bridge's own
injected messages, add a receipt when they meet one; the attachment sweep and
the hook consult it. A receipt that wins the race with the sender waits in the
bounded `pending-receipts/` sidecar store and is reconciled under the same lock
when the delivery row appears. `status` and `doctor` only read.

It exists because the tools said "delivered" when a queue had merely accepted
a row nobody drained — measured, a message queued to an open ChatGPT-app
thread stayed in the queue for over an hour — and because a refused Stop-hook
line printed on exit-0 stderr, which reaches a debug log and never the agent
that wrote it.

Concurrency: both sides' hooks write here. An update takes one exclusive
flock on `.antiphon/deliveries/.lock` around its read-modify-write; a write
of a new entry is one atomic replace of its own file and needs no lock.

An entry holds no message content, no route, no session id and no socket
path: public aliases, kinds, a transport, a proof class, times, a content
digest and size, an attachment file name, a state, a redacted reason (bounded
to `REASON_LENGTH`) and a short preview of the sender's own marker line (their
own words, in their own project).

A receipt is credited to the session whose transcript proved it: a delivery to
a named recipient is marked only from that recipient's own transcript, and a
transcript nobody can name proves a bare delivery alone. The readers say whose
transcript they walked; the ledger never guesses.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time

LEGACY_LEDGER_VERSION = 1
LEDGER_VERSION = 2
LEDGER_TTL = 7 * 24 * 3600            # seconds an entry is kept
RECORD_CEILING = 64 * 1024
PENDING_RECEIPT_VERSION = 1
PENDING_RECEIPT_TTL = LEDGER_TTL
PENDING_RECEIPT_LIMIT = 256
PENDING_RECEIPT_CEILING = 4 * 1024
PENDING_RECEIPT_DIRECTORY = "pending-receipts"
PENDING_DIAGNOSTICS_VERSION = 1
STOP_OUTCOME_VERSION = 1
STOP_OUTCOME_TTL = LEDGER_TTL
STOP_OUTCOME_LIMIT = 1024
STOP_OUTCOME_CEILING = 4 * 1024
STOP_OUTCOME_DIRECTORY = "stop-outcomes"
INTEGER_TOKEN_CEILING = 20
LEGACY_STATES = ("sent", "refused")
V2_STATES = ("unknown",)
STATES = LEGACY_STATES + V2_STATES
TRANSPORTS = ("queue", "channel")
PROOFS = ("live", "unproven", "registered", "automatic", "legacy", "channel")
KINDS = ("claude", "codex")
PREVIEW_LENGTH = 60
# A refusal's reason on the record and in the notice it becomes: the notices
# ride ahead of the page, outside its budget, and a reason is bounded only
# here (a transport's refusal is its own words, not this side's).
REASON_LENGTH = 400
LABEL = {"claude": "Claude", "codex": "Codex"}

DELIVERY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ATTACHMENT_BASENAME = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.txt"
    r"|[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# The largest time a validated entry may carry: past this `time.localtime`
# overflows the platform's time_t, and a validated entry that made the hook
# raise every turn was the finding that put it here. Far past any clock.
MAX_TIME = float(2 ** 40)

OPTIONAL_TIMES = ("received_at", "read_at", "reported_at", "expired_unread_at")
KEYS = frozenset({
    "version", "id", "sender", "sender_kind", "to_kind", "to_alias", "transport",
    "proof", "state", "sent_at", "sha256", "size", "attachment", "reason",
    "preview", *OPTIONAL_TIMES})
# Entries written before same-kind sends existed carry no `sender_kind`; the
# sender was then always the other kind of the recipient.
LEGACY_KEYS = KEYS - {"sender_kind"}


def _other(kind):
    return "claude" if kind == "codex" else "codex"


def _utf8_string(value, nonempty=False):
    """Whether a decoded JSON string is representable as strict UTF-8."""
    if not isinstance(value, str) or (nonempty and not value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def sender_kind_of(entry):
    """The kind of the session that sent this entry, inferred for an entry
    from before the field existed."""
    return entry.get("sender_kind") or _other(entry["to_kind"])


def ledger_dir(cwd):
    return os.path.join(cwd, ".antiphon", "deliveries")


def _sound_dir(cwd, create=False, repair=False):
    """The ledger directory when it is a directory this code owns, else None.

    Never through a symlink: a link at somebody else's directory would have
    this bridge writing its bookkeeping there and counting it as here.

    A writer (`create` or `repair`) tightens a directory somebody loosened; a
    reader leaves the mode as it found it, so `status` and `doctor` are
    read-only in the file system's terms as well as the ledger's.
    """
    directory = ledger_dir(cwd)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.makedirs(os.path.dirname(directory), exist_ok=True)
            os.mkdir(directory, 0o700)
        except FileExistsError:
            return _sound_dir(cwd, create=False)
        except OSError:
            return None
        return directory
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    if (create or repair) and info.st_mode & 0o077:
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    return directory


def _bounded_int(token):
    if len(token.lstrip("-")) > INTEGER_TOKEN_CEILING:
        raise ValueError("integer token too long")
    return int(token)


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _time_or_none(value):
    if value is None:
        return True
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf"))
            and 0 <= value <= MAX_TIME)


def _pending_dir(cwd):
    return os.path.join(ledger_dir(cwd), PENDING_RECEIPT_DIRECTORY)


def _sound_pending_dir(cwd, create=False):
    """The receipt sidecar directory, never through a symlink."""
    parent = _sound_dir(cwd, create=create, repair=create)
    if parent is None:
        return None
    directory = _pending_dir(cwd)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            return _sound_pending_dir(cwd, create=False)
        except OSError:
            return None
        return directory
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    if create and info.st_mode & 0o077:
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    return directory


def _pending_identity(kind, key, to_kind, reader_alias):
    try:
        raw = json.dumps(
            [kind, key, to_kind, reader_alias], ensure_ascii=True,
            separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(raw).hexdigest()


def _pending_path(cwd, kind, key, to_kind, reader_alias):
    identity = _pending_identity(kind, key, to_kind, reader_alias)
    if identity is None:
        return None
    return os.path.join(_pending_dir(cwd), identity + ".json")


def _valid_pending_receipt(record):
    if (not isinstance(record, dict)
            or set(record) != {"version", "kind", "key", "to_kind",
                               "reader_alias", "at", "stored_at"}
            or record.get("version") != PENDING_RECEIPT_VERSION
            or record.get("kind") not in ("received", "read")
            or record.get("to_kind") not in KINDS
            or not _time_or_none(record.get("at"))
            or record.get("at") is None
            or not _time_or_none(record.get("stored_at"))
            or record.get("stored_at") is None):
        return False
    alias = record.get("reader_alias")
    if alias is not None:
        if not isinstance(alias, str) or not alias or len(alias) > 128:
            return False
        try:
            alias.encode("utf-8")
        except UnicodeError:
            return False
    key = record.get("key")
    if not isinstance(key, str):
        return False
    if record["kind"] == "received":
        return bool(DELIVERY_ID.fullmatch(key))
    return bool(ATTACHMENT_BASENAME.fullmatch(key) and "/" not in key)


def _read_small_json(path, ceiling):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        raw = os.read(fd, ceiling + 1)
        if len(raw) > ceiling:
            return None
        # A short read from a regular file is not guaranteed to be EOF.
        if len(raw) == ceiling + 1 or os.read(fd, 1):
            return None
        return json.loads(raw.decode("utf-8"),
                          object_pairs_hook=_no_duplicate_keys,
                          parse_int=_bounded_int)
    except Exception:  # malformed durable input never escapes a hook
        return None
    finally:
        os.close(fd)


def _atomic_pending_json(path, value):
    """One bounded sidecar write; refuse an existing non-regular target."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        if not stat.S_ISREG(info.st_mode):
            return False
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(value, stream, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)
        os.replace(tmp, path)
        return True
    except Exception:  # encoding, shape and filesystem failures are all refusal
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False


def _read_pending_receipt(path):
    record = _read_small_json(path, PENDING_RECEIPT_CEILING)
    return record if _valid_pending_receipt(record) else None


def _sidecar_names(cwd, directory):
    """Names in one optional plain sidecar directory and its health.

    Absence is the normal empty state.  A present ledger or child path that
    cannot be proved to be a readable plain directory is different: writers
    will fail closed there, and diagnosis must not silently report zero work.
    """
    parent = ledger_dir(cwd)
    try:
        parent_info = os.lstat(parent)
    except FileNotFoundError:
        return [], False
    except OSError:
        return [], True
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        return [], True
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return [], False
    except OSError:
        return [], True
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [], True
    try:
        return sorted(os.listdir(directory)), False
    except OSError:
        return [], True


def _pending_receipt_scan(cwd):
    """Validated pending proofs and candidate-shaped durable rejects."""
    directory = _pending_dir(cwd)
    names, store_invalid = _sidecar_names(cwd, directory)
    if store_invalid or not names:
        return [], 0, store_invalid
    records = []
    invalid = 0
    for name in names:
        if not name.endswith(".json") or not SHA256_HEX.fullmatch(name[:-5]):
            continue
        path = os.path.join(directory, name)
        record = _read_pending_receipt(path)
        if record is None:
            invalid += 1
            continue
        expected = _pending_path(
            cwd, record["kind"], record["key"], record["to_kind"],
            record["reader_alias"])
        if expected == path:
            records.append((path, record))
        else:
            invalid += 1
    return records, invalid, False


def _pending_receipts(cwd):
    return _pending_receipt_scan(cwd)[0]


def _diagnostics_path(cwd):
    return os.path.join(_pending_dir(cwd), ".diagnostics")


def _valid_pending_diagnostics(value):
    return (isinstance(value, dict)
            and set(value) == {"version", "evicted", "expired", "last_event_at"}
            and value.get("version") == PENDING_DIAGNOSTICS_VERSION
            and type(value.get("evicted")) is int and value["evicted"] >= 0
            and type(value.get("expired")) is int and value["expired"] >= 0
            and _time_or_none(value.get("last_event_at")))


def _read_pending_diagnostics(cwd):
    path = _diagnostics_path(cwd)
    if not os.path.lexists(path):
        return {"version": PENDING_DIAGNOSTICS_VERSION, "evicted": 0,
                "expired": 0, "last_event_at": None}, False
    value = _read_small_json(path, PENDING_RECEIPT_CEILING)
    if not _valid_pending_diagnostics(value):
        return None, True
    return value, False


def _note_pending_loss(cwd, field, amount, now):
    diagnostics, invalid = _read_pending_diagnostics(cwd)
    if invalid or diagnostics is None:
        return False
    changed = dict(diagnostics)
    changed[field] += amount
    changed["last_event_at"] = float(now)
    return _atomic_pending_json(_diagnostics_path(cwd), changed)


def _prune_pending_locked(cwd, now):
    """Expire sidecars only after their loss is durably observable."""
    expired = [(path, record) for path, record in _pending_receipts(cwd)
               if now - record["stored_at"] > PENDING_RECEIPT_TTL]
    if not expired:
        return True
    if not _note_pending_loss(cwd, "expired", len(expired), now):
        return False
    ok = True
    for path, _record in expired:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            ok = False
    return ok


def _write_pending_receipt(cwd, record):
    """Store one proof under the ledger lock.

    Delivery-ID proof keeps its earliest sighting. A later attachment read is
    a stronger horizon for reused files, so it replaces the earlier one.
    """
    directory = _sound_pending_dir(cwd, create=True)
    if directory is None or not _valid_pending_receipt(record):
        return False
    path = _pending_path(cwd, record["kind"], record["key"],
                         record["to_kind"], record["reader_alias"])
    if path is None:
        return False
    current = _read_pending_receipt(path) if os.path.lexists(path) else None
    if os.path.lexists(path) and current is None:
        return False
    if current is not None:
        incoming = record
        record = dict(current)
        if current["kind"] == "received":
            record["at"] = min(float(current["at"]), float(incoming["at"]))
        elif incoming["at"] > current["at"]:
            # A later opening of a reused file proves attempts that did not
            # exist at the first opening. Keep that stronger horizon and give
            # it its own bounded retention window. An identical replay does
            # not extend TTL indefinitely.
            record["at"] = float(incoming["at"])
            record["stored_at"] = float(incoming["stored_at"])
        if record == current:
            # The existing bytes already prove at least as much. Rewriting a
            # no-op would turn a later filesystem failure into a permanent
            # cursor hold even though the receipt is durably represented.
            return True
        return _atomic_pending_json(path, record)

    now = float(record["stored_at"])
    if not _prune_pending_locked(cwd, now):
        return False
    rows = _pending_receipts(cwd)
    if len(rows) >= PENDING_RECEIPT_LIMIT:
        oldest_path, _oldest = min(
            rows, key=lambda item: (item[1]["stored_at"], item[0]))
        # Record the loss before removing its proof. A diagnostics over-count
        # after an unlink failure is noisy; the opposite ordering is silent.
        if not _note_pending_loss(cwd, "evicted", 1, now):
            return False
        try:
            os.unlink(oldest_path)
        except OSError:
            return False
    return _atomic_pending_json(path, record)


def pending_receipt_health(cwd, now=None):
    """Read-only aggregate for status/doctor; never a path, alias or id."""
    now = time.time() if now is None else now
    rows, malformed, store_invalid = _pending_receipt_scan(cwd)
    if store_invalid:
        # Do not follow a parent path already proved unsound merely to inspect
        # its diagnostics child.
        diagnostics, invalid = None, False
    else:
        diagnostics, invalid = _read_pending_diagnostics(cwd)
    oldest = max((max(0.0, now - row["stored_at"])
                  for _path, row in rows), default=0.0)
    return {
        "pending": len(rows), "oldest": oldest,
        "evicted": 0 if diagnostics is None else diagnostics["evicted"],
        "expired": 0 if diagnostics is None else diagnostics["expired"],
        "diagnostics_invalid": invalid,
        "invalid": malformed,
        "store_invalid": store_invalid,
    }


def _stop_outcome_dir(cwd):
    return os.path.join(ledger_dir(cwd), STOP_OUTCOME_DIRECTORY)


def _sound_stop_outcome_dir(cwd, create=False):
    parent = _sound_dir(cwd, create=create, repair=create)
    if parent is None:
        return None
    directory = _stop_outcome_dir(cwd)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            return _sound_stop_outcome_dir(cwd, create=False)
        except OSError:
            return None
        return directory
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    if create and info.st_mode & 0o077:
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    return directory


def _valid_stop_scope(side, key, slot):
    if side not in KINDS or not isinstance(key, str):
        return False
    expected = (f"last_pushed_{side}_same"
                if key.endswith("_same") else
                f"last_pushed_{_other(side)}")
    if key != expected:
        return False
    return (slot == "" or (isinstance(slot, str)
                           and bool(re.fullmatch(
                               r"@[a-z0-9][a-z0-9_-]{0,31}", slot))))


def _stop_outcome_identity(side, key, slot, fingerprint):
    if (not _valid_stop_scope(side, key, slot)
            or not isinstance(fingerprint, str)
            or not SHA256_HEX.fullmatch(fingerprint)):
        return None
    raw = json.dumps([side, key, slot, fingerprint], ensure_ascii=True,
                     separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _stop_outcome_path(cwd, side, key, slot, fingerprint):
    identity = _stop_outcome_identity(side, key, slot, fingerprint)
    return (None if identity is None else
            os.path.join(_stop_outcome_dir(cwd), identity + ".json"))


def _valid_stop_outcome(value):
    return (isinstance(value, dict)
            and set(value) == {"version", "side", "key", "slot",
                               "fingerprint", "delivery_id", "recorded_at"}
            and value.get("version") == STOP_OUTCOME_VERSION
            and _valid_stop_scope(value.get("side"), value.get("key"),
                                  value.get("slot"))
            and isinstance(value.get("fingerprint"), str)
            and bool(SHA256_HEX.fullmatch(value["fingerprint"]))
            and isinstance(value.get("delivery_id"), str)
            and bool(DELIVERY_ID.fullmatch(value["delivery_id"]))
            and _time_or_none(value.get("recorded_at"))
            and value.get("recorded_at") is not None)


def _read_stop_outcome(path):
    value = _read_small_json(path, STOP_OUTCOME_CEILING)
    return value if _valid_stop_outcome(value) else None


def _stop_outcome_scan(cwd):
    """Validated exact outcomes and the count of candidate-shaped rejects."""
    directory = _stop_outcome_dir(cwd)
    names, store_invalid = _sidecar_names(cwd, directory)
    if store_invalid or not names:
        return [], 0, store_invalid
    found = []
    invalid = 0
    for name in names:
        if not name.endswith(".json") or not SHA256_HEX.fullmatch(name[:-5]):
            continue
        path = os.path.join(directory, name)
        value = _read_stop_outcome(path)
        if value is None:
            invalid += 1
            continue
        expected = _stop_outcome_path(
            cwd, value["side"], value["key"], value["slot"],
            value["fingerprint"])
        if expected == path:
            found.append((path, value))
        else:
            invalid += 1
    return found, invalid, False


def _stop_outcomes(cwd):
    return _stop_outcome_scan(cwd)[0]


def find_stop_outcome(cwd, *, side, key, slot, fingerprint):
    """Read-only exact post-transport suppression evidence, if present."""
    path = _stop_outcome_path(cwd, side, key, slot, fingerprint)
    if path is None or _sound_stop_outcome_dir(cwd) is None:
        return None
    value = _read_stop_outcome(path)
    if value is None:
        return None
    return value if _stop_outcome_path(
        cwd, value["side"], value["key"], value["slot"],
        value["fingerprint"]) == path else None


def record_stop_outcome(cwd, *, side, key, slot, fingerprint, delivery_id,
                        at=None):
    """Persist exact Stop identity only after transport returned sent/unknown."""
    value = {
        "version": STOP_OUTCOME_VERSION, "side": side, "key": key,
        "slot": slot, "fingerprint": fingerprint,
        "delivery_id": delivery_id,
        "recorded_at": float(time.time() if at is None else at),
    }
    if not _valid_stop_outcome(value):
        return False
    with _locked(cwd, create=True) as locked:
        if not locked or _sound_stop_outcome_dir(cwd, create=True) is None:
            return False
        path = _stop_outcome_path(cwd, side, key, slot, fingerprint)
        current = _read_stop_outcome(path) if os.path.lexists(path) else None
        if os.path.lexists(path):
            return (current is not None
                    and _stop_outcome_path(
                        cwd, current["side"], current["key"], current["slot"],
                        current["fingerprint"]) == path)
        now = value["recorded_at"]
        for old_path, old in _stop_outcomes(cwd):
            if now - old["recorded_at"] > STOP_OUTCOME_TTL:
                with contextlib.suppress(OSError):
                    os.unlink(old_path)
        if len(_stop_outcomes(cwd)) >= STOP_OUTCOME_LIMIT:
            return False
        return _atomic_pending_json(path, value)


def clear_stop_outcome(cwd, *, side, key, slot, fingerprint,
                       delivery_id=None):
    """Remove only the exact evidence a durable cursor just superseded."""
    path = _stop_outcome_path(cwd, side, key, slot, fingerprint)
    if path is None:
        return False
    with _locked(cwd) as locked:
        if not locked:
            return False
        value = _read_stop_outcome(path)
        if value is None:
            return not os.path.lexists(path)
        if delivery_id is not None and value["delivery_id"] != delivery_id:
            return False
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


def stop_outcome_health(cwd, now=None):
    """Read-only aggregate; invalid durable evidence stays visible."""
    now = time.time() if now is None else now
    rows, invalid, store_invalid = _stop_outcome_scan(cwd)
    return {"pending": len(rows),
            "oldest": max((max(0.0, now - row["recorded_at"])
                           for _path, row in rows), default=0.0),
            "invalid": invalid,
            "store_invalid": store_invalid}


def _valid(entry, expected_id):
    if not isinstance(entry, dict):
        return False
    version = entry.get("version")
    if type(version) is not int or version not in (
            LEGACY_LEDGER_VERSION, LEDGER_VERSION):
        return False
    keys = set(entry)
    if ((version == LEDGER_VERSION and keys != KEYS)
            or (version == LEGACY_LEDGER_VERSION
                and keys not in (KEYS, LEGACY_KEYS))):
        return False
    if "sender_kind" in entry and entry["sender_kind"] not in KINDS:
        return False
    if entry["id"] != expected_id or not DELIVERY_ID.fullmatch(expected_id):
        return False
    if not _utf8_string(entry["sender"], nonempty=True):
        return False
    if entry["to_kind"] not in KINDS:
        return False
    if entry["to_alias"] is not None and not _utf8_string(
            entry["to_alias"], nonempty=True):
        return False
    if entry["transport"] not in TRANSPORTS or entry["proof"] not in PROOFS:
        return False
    allowed_states = LEGACY_STATES if version == LEGACY_LEDGER_VERSION else V2_STATES
    if entry["state"] not in allowed_states:
        return False
    if not _time_or_none(entry["sent_at"]) or entry["sent_at"] is None:
        return False
    if entry["sha256"] is not None and not (
            isinstance(entry["sha256"], str)
            and SHA256_HEX.fullmatch(entry["sha256"])):
        return False
    if entry["size"] is not None and (
            type(entry["size"]) is not int or entry["size"] < 0):
        return False
    if entry["attachment"] is not None and not (
            isinstance(entry["attachment"], str)
            and ATTACHMENT_BASENAME.fullmatch(entry["attachment"])
            and "/" not in entry["attachment"]):
        return False
    for key in ("reason", "preview"):
        if entry[key] is not None and not _utf8_string(entry[key]):
            return False
    return all(_time_or_none(entry[key]) for key in OPTIONAL_TIMES)


def _path(cwd, delivery_id):
    return os.path.join(ledger_dir(cwd), delivery_id + ".json")


def read_entry(cwd, delivery_id):
    """One validated entry, or None."""
    if not isinstance(delivery_id, str) or not DELIVERY_ID.fullmatch(delivery_id):
        return None
    if _sound_dir(cwd) is None:
        return None
    try:
        with open(_path(cwd, delivery_id), "rb") as stream:
            raw = stream.read(RECORD_CEILING + 1)
        if len(raw) > RECORD_CEILING:
            return None
        entry = json.loads(raw.decode("utf-8"),
                           object_pairs_hook=_no_duplicate_keys,
                           parse_int=_bounded_int)
    except Exception:       # noqa: BLE001 — a malformed file is skipped, never raised
        # Not only the parser's ValueError: a file of nested brackets well
        # under RECORD_CEILING raised RecursionError out of `json.loads`, and
        # through every caller — the hook's exit code, `status` and `doctor`
        # together (review 2026-09-03). Whatever a file does to the parser,
        # it is not an entry.
        return None
    return entry if _valid(entry, delivery_id) else None


def entries(cwd):
    """Every validated entry, oldest first (then by id)."""
    directory = _sound_dir(cwd)
    if directory is None:
        return []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        entry = read_entry(cwd, name[:-5])
        if entry is not None:
            found.append(entry)
    found.sort(key=lambda e: (e["sent_at"], e["id"]))
    return found


def _write(cwd, entry):
    directory = _sound_dir(cwd, create=True)
    if directory is None:
        return False
    path = _path(cwd, entry["id"])
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(entry, stream, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # encoding and filesystem failures are both non-durable
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def _new(delivery_id, sender, to_kind, to_alias, transport, proof, state, at,
         sender_kind=None):
    return {
        "version": (LEDGER_VERSION if state in V2_STATES else LEGACY_LEDGER_VERSION),
        "id": delivery_id, "sender": sender,
        "sender_kind": sender_kind or _other(to_kind),
        "to_kind": to_kind, "to_alias": to_alias, "transport": transport,
        "proof": proof, "state": state, "sent_at": float(at),
        "sha256": None, "size": None, "attachment": None, "reason": None,
        "preview": None, "received_at": None, "read_at": None,
        "reported_at": None, "expired_unread_at": None,
    }


def _apply_receipt(entry, receipt):
    """Apply one already validated proof to a matching delivery in memory."""
    if not _for_reader(entry, receipt["to_kind"], receipt["reader_alias"]):
        return False
    if receipt["kind"] == "received":
        current = entry["received_at"]
        if current is None or receipt["at"] < current:
            entry["received_at"] = float(receipt["at"])
    elif (entry["attachment"] == receipt["key"]
          and entry["sent_at"] <= receipt["at"]):
        # A basename can be reused by a later envelope.  Reading the older
        # one proves only attempts that had begun by the observation time;
        # it must never make a future reuse look read already.
        current = entry["read_at"]
        if current is None or receipt["at"] < current:
            entry["read_at"] = float(receipt["at"])
        entry["expired_unread_at"] = None
    else:
        return False
    if entry["state"] == "unknown":
        entry["state"] = "sent"
        entry["version"] = LEGACY_LEDGER_VERSION
        entry["reason"] = None
        entry["preview"] = None
    return True


def _pending_for_entry(cwd, entry):
    consume = []
    for path, receipt in _pending_receipts(cwd):
        if ((receipt["kind"] == "received" and receipt["key"] == entry["id"])
                or (receipt["kind"] == "read" and entry["attachment"] is not None
                    and receipt["key"] == entry["attachment"])):
            if _apply_receipt(entry, receipt):
                if receipt["kind"] == "received":
                    consume.append(path)
            elif receipt["kind"] == "received":
                # A delivery id has one immutable recipient. Once its row
                # exists, a proof rejected by `_for_reader` can never become
                # relevant to a later row.
                consume.append(path)
    return consume


def _record_with_pending(cwd, entry):
    """Write a sender row and reconcile receipt-first evidence atomically."""
    if not _valid(entry, entry.get("id")):
        return False
    with _locked(cwd, create=True) as locked:
        if not locked:
            return False
        # A v1 sender may have published a row without consulting sidecars.
        # Reconcile those rows whenever any new sender reaches this lock.
        # Read proofs remain retained; exact delivery-id receipts are consumed
        # once the immutable row proves where they belong.
        _reconcile_pending_locked(cwd)
        consume = _pending_for_entry(cwd, entry)
        if not _valid(entry, entry["id"]) or not _write(cwd, entry):
            return False
        # Row first, delete second. A crash here leaves an idempotent sidecar,
        # never a receipt that vanished before the row became durable.
        for path in consume:
            with contextlib.suppress(OSError):
                os.unlink(path)
        return True


def record_sent(cwd, delivery_id, *, sender, to_kind, to_alias, transport,
                proof, sha256, size, attachment=None, at=None, sender_kind=None):
    """An attempt the transport accepted. Returns whether it is on the ledger.
    `sender_kind` defaults to the other kind of the recipient; a same-kind
    send says so."""
    entry = _new(delivery_id, sender, to_kind, to_alias, transport, proof,
                 "sent", time.time() if at is None else at, sender_kind)
    entry["sha256"] = sha256
    entry["size"] = size
    entry["attachment"] = attachment
    return _record_with_pending(cwd, entry)


def record_refused(cwd, delivery_id, *, sender, to_kind, to_alias, reason,
                   preview, at=None, sender_kind=None):
    """An attempt that never left: the reason, and enough of the sender's own
    line to recognise it. Returns whether it is on the ledger."""
    entry = _new(delivery_id, sender, to_kind, to_alias, "queue"
                 if to_kind == "codex" else "channel", "unproven", "refused",
                 time.time() if at is None else at, sender_kind)
    entry["reason"] = str(reason)[:REASON_LENGTH]
    entry["preview"] = " ".join(str(preview).split())[:PREVIEW_LENGTH]
    if not _valid(entry, delivery_id):
        return False
    return _write(cwd, entry)


def record_unknown(cwd, delivery_id, *, sender, to_kind, to_alias, transport,
                   sha256, size, reason, preview, attachment=None, at=None,
                   sender_kind=None):
    """An attempt whose bytes may have left but whose acknowledgement was lost.

    It is neither `sent` nor `refused`: automatic retry could duplicate it,
    while a later transcript receipt can still resolve it to `sent`.
    """
    entry = _new(delivery_id, sender, to_kind, to_alias, transport, "unproven",
                 "unknown", time.time() if at is None else at, sender_kind)
    entry["sha256"] = sha256
    entry["size"] = size
    entry["attachment"] = attachment
    entry["reason"] = str(reason)[:REASON_LENGTH]
    entry["preview"] = " ".join(str(preview).split())[:PREVIEW_LENGTH]
    return _record_with_pending(cwd, entry)


@contextlib.contextmanager
def _locked(cwd, create=False):
    """One exclusive lock over the ledger for a read-modify-write.

    Both sides' hooks mark entries — a receipt from one, a notice reported by
    the other — and an unlocked read-modify-write let the loser's field
    vanish; a lost receipt is permanent, because the record that carried it
    was consumed when the cursor advanced. The section is microseconds and a
    process that dies releases its flock, so a plain blocking lock is safe."""
    directory = _sound_dir(cwd, create=create, repair=True)
    if directory is None:
        yield False
        return
    try:
        fd = os.open(os.path.join(directory, ".lock"),
                     os.O_CREAT | os.O_RDWR, 0o600)
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


def _update(cwd, delivery_id, mutate):
    """Read, `mutate`, validate, write, under the lock. A mutate that leaves
    the entry invalid is refused, never written: the next reader would skip
    the file and the delivery would be gone from the ledger without a word
    (round-2 review, 2026-09-03; no caller does it today)."""
    with _locked(cwd) as locked:
        if not locked:
            return False
        entry = read_entry(cwd, delivery_id)
        if entry is None:
            return False
        changed = dict(entry)
        if mutate(changed) is False:
            return False
        if changed == entry:
            return True
        if not _valid(changed, delivery_id):
            return False
        return _write(cwd, changed)


def _for_reader(entry, to_kind, reader_alias):
    """Whether a receipt read off a transcript — of a session of `to_kind`
    named `reader_alias`, or None when the reader could not say — is this
    entry's recipient's.

    The transcript that proves a receipt belongs to one session. A delivery
    to a named recipient is that session's alone: Codex `review` opening a
    file parked for Codex `build` is not `build` reading it, and a transcript
    nobody can name proves nothing about a named delivery — it proves a bare
    one, sent to nobody in particular of that kind. A same-kind delivery
    shares the sender's kind with its recipient, so only the named
    recipient's own transcript can prove it read."""
    if to_kind is not None and entry["to_kind"] != to_kind:
        return False
    if sender_kind_of(entry) == entry["to_kind"]:
        return reader_alias is not None and entry["to_alias"] == reader_alias
    return entry["to_alias"] in (None, reader_alias)


def _pending_record(kind, key, at, to_kind, reader_alias):
    record = {
        "version": PENDING_RECEIPT_VERSION, "kind": kind, "key": key,
        "to_kind": to_kind, "reader_alias": reader_alias,
        "at": float(at), "stored_at": float(time.time()),
    }
    return record if _valid_pending_receipt(record) else None


def _mark_received(cwd, delivery_id, at, to_kind=None, reader_alias=None):
    """`applied`, `ignored`, `pending` or `unresolved`."""
    if (not isinstance(delivery_id, str)
            or not DELIVERY_ID.fullmatch(delivery_id)
            or not _time_or_none(at) or at is None):
        return "ignored"
    with _locked(cwd, create=to_kind in KINDS) as locked:
        if not locked:
            return "unresolved" if to_kind in KINDS else "ignored"
        entry = read_entry(cwd, delivery_id)
        if entry is None:
            if to_kind not in KINDS:
                return "ignored"
            record = _pending_record(
                "received", delivery_id, at, to_kind, reader_alias)
            if record is None:
                return "ignored"
            return ("pending" if _write_pending_receipt(cwd, record)
                    else "unresolved")
        if not _for_reader(entry, to_kind, reader_alias):
            return "ignored"
        changed = dict(entry)
        receipt = _pending_record(
            "received", delivery_id, at, to_kind or entry["to_kind"],
            reader_alias)
        if receipt is None or not _apply_receipt(changed, receipt):
            return "ignored"
        if changed == entry:
            return "applied"
        if not _valid(changed, delivery_id) or not _write(cwd, changed):
            return "unresolved"
        return "applied"


def mark_received(cwd, delivery_id, at, to_kind=None, reader_alias=None):
    """The peer's transcript shows the delivery; the earliest sighting wins.
    Scoped like a read (`_for_reader`): the transcript belongs to a session of
    `to_kind` named `reader_alias`, and only its own deliveries are its."""
    return _mark_received(
        cwd, delivery_id, at, to_kind, reader_alias) in ("applied", "pending")


def _entries_naming(cwd, attachment):
    return [e for e in entries(cwd) if e["attachment"] == attachment]


def _mark_read_horizons(cwd, attachment, horizons, to_kind=None,
                        reader_alias=None):
    """Apply distinct read horizons for one attachment from one reader.

    Scoped to the recipient (`_for_reader`): with `to_kind`, only deliveries
    *to* that kind are marked, because the transcript that proved the read
    belongs to a session of that kind — the sender verifying its own parked
    file (the `tail -n +3 | shasum` ritual the envelope teaches) is not the
    recipient reading it. A named delivery is marked only when
    `reader_alias` — the alias of the session whose transcript proved the
    read — is its named recipient; a bare delivery, by any reader of its
    kind. A same-kind delivery shares the sender's kind with its recipient,
    so only the named recipient's own transcript proves it. A read clears an
    expiry marked in the meantime: a file that was read did not expire
    unread, whichever the sweep saw first. One ledger snapshot is enough even
    when a delayed page contains several opens: each row takes the earliest
    horizon at or after its own send, while the sidecar retains the latest for
    a row that can still arrive after this snapshot."""
    if (not isinstance(attachment, str)
            or not ATTACHMENT_BASENAME.fullmatch(attachment)
            or "/" in attachment):
        return "ignored"
    ordered = sorted(set(float(at) for at in horizons
                         if _time_or_none(at) and at is not None))
    if not ordered:
        return "ignored"
    with _locked(cwd, create=to_kind in KINDS) as locked:
        if not locked:
            return "unresolved" if to_kind in KINDS else "ignored"
        retained = None
        if to_kind in KINDS:
            retained = _pending_record(
                "read", attachment, ordered[-1], to_kind, reader_alias)
            # Even when every row visible now is updated, an older sender can
            # publish another in-flight row after this snapshot.  The durable,
            # bounded proof is therefore retained until TTL rather than
            # consumed by whichever row happens to arrive first.
            if retained is None:
                return "ignored"
            if not _write_pending_receipt(cwd, retained):
                return "unresolved"
        rows = [entry for entry in entries(cwd)
                if entry["attachment"] == attachment]
        matching = [entry for entry in rows
                    if _for_reader(entry, to_kind, reader_alias)]
        applied = False
        durable = True
        for entry in matching:
            at = next((horizon for horizon in ordered
                       if entry["sent_at"] <= horizon), None)
            receipt = (_pending_record(
                "read", attachment, at, to_kind or entry["to_kind"],
                reader_alias) if at is not None else None)
            changed = dict(entry)
            if receipt is None or not _apply_receipt(changed, receipt):
                continue
            applied = True
            if (changed != entry
                    and (not _valid(changed, entry["id"])
                         or not _write(cwd, changed))):
                durable = False
        if not durable and len(ordered) > 1:
            # The latest sidecar preserves the fact of a read, but not every
            # earlier horizon needed to assign each existing reuse its first
            # qualifying timestamp. Hold the page cursor so those horizons
            # replay instead of silently degrading to the latest one.
            return "unresolved"
        if applied and durable:
            return "applied"
        if retained is not None:
            # The row update can be retried from the retained proof; cursor
            # progress is safe once that proof itself is durable.
            if applied or not rows:
                return "pending"
            return "ignored"
        return "unresolved" if applied else "ignored"


def _mark_read(cwd, attachment, at, to_kind=None, snapshot=None,
               reader_alias=None):
    """Compatibility road for one transcript attachment-read proof."""
    del snapshot  # a snapshot taken before the flock cannot decide absence
    return _mark_read_horizons(
        cwd, attachment, [at], to_kind=to_kind, reader_alias=reader_alias)


def mark_read(cwd, attachment, at, to_kind=None, snapshot=None, reader_alias=None):
    """Boolean compatibility wrapper around the four-way receipt outcome."""
    return _mark_read(cwd, attachment, at, to_kind, snapshot,
                      reader_alias) in ("applied", "pending")


def mark_expired_unread(cwd, attachment, at):
    """The sweep removed the file and no receipt ever said it was read.
    Returns how many entries were marked — zero when nothing on the ledger
    names the file, which is a file from before the ledger."""
    marked = 0
    for entry in _entries_naming(cwd, attachment):
        if entry["read_at"] is not None or entry["expired_unread_at"] is not None:
            continue

        def mutate(changed):
            changed["expired_unread_at"] = float(at)
        if _update(cwd, entry["id"], mutate):
            marked += 1
    return marked


def read_times(cwd):
    """Read-grace eligibility by basename.

    A shared file is safe to collect only when every delivery attempt naming
    it has a read proof. The grace begins at the latest such proof, protecting
    a later reuse even when an older attempt was read much earlier.
    """
    grouped = {}
    for entry in entries(cwd):
        name, at = entry["attachment"], entry["read_at"]
        if name:
            grouped.setdefault(name, []).append(at)
    return {
        name: max(receipts)
        for name, receipts in grouped.items()
        if receipts and all(at is not None for at in receipts)
    }


def mark_reported(cwd, delivery_ids, at):
    """The sender's own page carried the notice; never again."""
    for delivery_id in delivery_ids:
        def mutate(changed):
            if changed["reported_at"] is None:
                changed["reported_at"] = float(at)
        _update(cwd, delivery_id, mutate)


def record_receipts(cwd, receipts, read_by=None, reader_alias=None):
    """Apply `("received", id, at)` / `("read", basename, at)` receipts, each
    optionally carrying a fourth element: the alias of the session whose
    transcript proved it, or None when the reader could not say.

    `read_by` is the kind of session whose transcript the receipts came from,
    and `reader_alias` the alias a three-element receipt is credited to (a
    session reading its own transcript passes its own). A receipt marks only
    deliveries to that kind, and a named delivery only when the reader is its
    named recipient (`_for_reader`). Exact delivery-id receipts fold to their
    earliest time. Attachment reads retain every distinct chronological
    horizon: one page can contain an early open, a basename reuse, and a later
    open, so collapsing those proofs would leave the later attempt unread."""
    durable = True
    if _pending_receipts(cwd):
        with _locked(cwd) as locked:
            if not locked or not _reconcile_pending_locked(cwd):
                durable = False
    earliest = {}
    read_horizons = {}
    for receipt in receipts:
        if len(receipt) == 3:
            (kind, key, at), alias = receipt, reader_alias
        elif len(receipt) == 4:
            kind, key, at, alias = receipt
        else:
            continue
        if kind not in ("received", "read"):
            continue
        if not _time_or_none(at) or at is None:
            continue
        if kind == "read":
            read_horizons.setdefault((key, alias), set()).add(float(at))
        elif ((kind, key, alias) not in earliest
              or at < earliest[(kind, key, alias)]):
            earliest[(kind, key, alias)] = at
    for (kind, key, alias), at in earliest.items():
        if kind == "received":
            outcome = _mark_received(
                cwd, key, at, to_kind=read_by, reader_alias=alias)
            if outcome == "unresolved":
                durable = False
    for (key, alias), horizons in read_horizons.items():
        outcome = _mark_read_horizons(
            cwd, key, horizons, to_kind=read_by, reader_alias=alias)
        if outcome == "unresolved":
            durable = False
    return durable


def _clock(stamp):
    return time.strftime("%H:%M", time.localtime(stamp))


def pending_notices(cwd, side, alias):
    """`[(id, text)]` the sender on `side` has not been told yet.

    A refusal is reported to the session that wrote the line: the entry's
    sender is this alias, or `<unnamed>` when this session has no name. An
    attachment that expired unread is reported to its sender the same way —
    never one that was read after all. Two unnamed sessions on one side are
    one sender here: whichever runs its hook first carries, and reports, the
    other's notice. That is what `<unnamed>` means, and the remedy is a name.
    """
    mine = {alias} if alias else set()
    if not alias or alias == "<unnamed>":
        mine.add("<unnamed>")
    notices = []
    for entry in entries(cwd):
        if entry["sender"] not in mine or entry["reported_at"] is not None:
            continue
        if sender_kind_of(entry) != side:
            continue                       # the other side's send, not this one's
        target = LABEL[entry["to_kind"]]
        if entry["state"] == "refused":
            named = f":{entry['to_alias']}" if entry["to_alias"] else ""
            # Every refusal the senders write starts "not delivered: "; the
            # notice says that once.
            reason = (re.sub(r"^\s*not delivered:\s*", "",
                             entry["reason"] or "")[:REASON_LENGTH]
                      or "no reason recorded")
            notices.append((entry["id"], (
                f"Antiphon: your @{entry['to_kind']}{named} line at "
                f"{_clock(entry['sent_at'])} (\"{entry['preview'] or ''}\") "
                f"was not delivered — {reason}")))
        elif entry["state"] == "unknown" and entry["received_at"] is None:
            named = f":{entry['to_alias']}" if entry["to_alias"] else ""
            notices.append((entry["id"], (
                f"Antiphon: your @{entry['to_kind']}{named} line at "
                f"{_clock(entry['sent_at'])} (\"{entry['preview'] or ''}\"): "
                "delivery outcome is unknown — do not retry automatically; "
                "the peer may already have received it")))
        elif entry["expired_unread_at"] is not None and entry["read_at"] is None:
            notices.append((entry["id"], (
                f"Antiphon: the attachment you sent to {target} at "
                f"{_clock(entry['sent_at'])} expired unread after "
                f"{LEDGER_TTL // 86400} days")))
    return notices


def awaiting_receipt(cwd, now):
    """`[(entry, age)]` for what was sent and never seen in the peer's transcript."""
    waiting = []
    for entry in entries(cwd):
        if entry["state"] in ("sent", "unknown") and entry["received_at"] is None:
            waiting.append((entry, max(0.0, now - entry["sent_at"])))
    return waiting


def last_unanswered_sender(cwd, to_kind, to_alias, now, sender_kind=None):
    """`(alias, age)` of the newest peer that wrote to this session — by its
    alias, or to nobody in particular — and has not been written back to
    since; None when nobody is owed a reply. With `sender_kind`, only peers of
    that kind count: advice for choosing among Codex peers must not name a
    Claude one. Advice for a refusal, never a route: the bridge does not
    choose."""
    latest = {}
    answered = {}
    for entry in entries(cwd):
        if entry["state"] != "sent":
            continue
        sender, kind = entry["sender"], sender_kind_of(entry)
        if (entry["to_kind"] == to_kind and kind == (sender_kind or kind)
                and entry["to_alias"] in (to_alias, None)
                and sender != "<unnamed>"
                and (sender, kind) != (to_alias, to_kind)):
            latest[(kind, sender)] = max(latest.get((kind, sender), 0.0),
                                         entry["sent_at"])
        # Written back: by this alias, or by the unnamed session of this kind
        # when this session has no name.
        mine = ((sender, kind) == (to_alias, to_kind)
                or (to_alias is None and sender == "<unnamed>" and kind == to_kind))
        if mine and entry["to_alias"]:
            key = (entry["to_kind"], entry["to_alias"])
            answered[key] = max(answered.get(key, 0.0), entry["sent_at"])
    open_senders = [(when, sender) for (kind, sender), when in latest.items()
                    if answered.get((kind, sender), -1.0) < when]
    if not open_senders:
        return None
    when, sender = max(open_senders)
    return sender, max(0.0, now - when)


def reusable_attachment(cwd, sha256, to_kind, to_alias, now, sender=None,
                        sender_kind=None):
    """The parked file an earlier, unexpired send of these exact words to this
    recipient left behind, when it is still there and nobody has read it;
    else None. With `sender`, only a send by that same sender counts: the
    file's header names whose words they are, and a reuse must not put one
    session's words under another's name. A file with a read receipt is never
    reused: the read grace would collect it under the fresh envelope."""
    retained_reads = [receipt for _path, receipt in _pending_receipts(cwd)
                      if receipt["kind"] == "read"]
    for entry in reversed(entries(cwd)):
        if (entry["state"] == "sent" and entry["attachment"]
                and entry["read_at"] is None
                and entry["sha256"] == sha256 and entry["to_kind"] == to_kind
                and entry["to_alias"] == to_alias
                and (sender is None or entry["sender"] == sender)
                and (sender_kind is None or sender_kind_of(entry) == sender_kind)
                and now - entry["sent_at"] <= LEDGER_TTL):
            # The receiver's proof may be durable while the row mutation is
            # still pending. Reusing here would let read grace collect the
            # shared file underneath a fresh envelope.
            if any(receipt["key"] == entry["attachment"]
                   and _apply_receipt(dict(entry), receipt)
                   for receipt in retained_reads):
                continue
            path = os.path.join(cwd, ".antiphon", "messages", entry["attachment"])
            try:
                if stat.S_ISREG(os.lstat(path).st_mode):
                    return entry["attachment"]
            except OSError:
                continue
    return None


def _reconcile_pending_locked(cwd):
    """Apply sidecars left for a row written by an older, lock-unaware sender."""
    ok = True
    for entry in entries(cwd):
        changed = dict(entry)
        consume = _pending_for_entry(cwd, changed)
        if changed != entry and (
                not _valid(changed, entry["id"]) or not _write(cwd, changed)):
            ok = False
            continue
        for path in consume:
            with contextlib.suppress(OSError):
                os.unlink(path)
    return ok


def prune(cwd, now):
    """Drop entries older than the ledger's own TTL. Best effort, never raises.

    An entry with something still to tell its sender — a refusal or an
    unread expiry not yet reported — is kept for a second TTL: the
    attachment TTL and this one are the same week, so the expiry a sweep
    marks on day seven would otherwise be pruned in the same hook, unheard."""
    directory = _sound_dir(cwd, repair=True)
    if directory is None:
        return
    with _locked(cwd) as locked:
        if locked:
            _reconcile_pending_locked(cwd)
            _prune_pending_locked(cwd, now)
            for path, outcome in _stop_outcomes(cwd):
                if now - outcome["recorded_at"] > STOP_OUTCOME_TTL:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
    for entry in entries(cwd):
        age = now - entry["sent_at"]
        if age <= LEDGER_TTL:
            continue
        unheard = entry["reported_at"] is None and (
            entry["state"] in ("refused", "unknown")
            or entry["expired_unread_at"] is not None)
        if unheard and age <= 2 * LEDGER_TTL:
            continue
        with contextlib.suppress(OSError):
            os.unlink(_path(cwd, entry["id"]))
