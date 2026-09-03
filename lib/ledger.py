"""The delivery ledger: what was sent, refused, received and read.

One directory, `.antiphon/deliveries/`, one file per delivery attempt. Every
direct send writes its entry before the transport is touched; the page
readers, which already walk the peer's transcript and already recognise the
bridge's own injected messages, add a receipt when they meet one; the
attachment sweep and the hook consult it. `status` and `doctor` only read.

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
import time

LEDGER_VERSION = 1
LEDGER_TTL = 7 * 24 * 3600            # seconds an entry is kept
RECORD_CEILING = 64 * 1024
INTEGER_TOKEN_CEILING = 20
STATES = ("sent", "refused")
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


def _valid(entry, expected_id):
    if not isinstance(entry, dict) or set(entry) not in (KEYS, LEGACY_KEYS):
        return False
    if "sender_kind" in entry and entry["sender_kind"] not in KINDS:
        return False
    if entry["version"] != LEDGER_VERSION or type(entry["version"]) is not int:
        return False
    if entry["id"] != expected_id or not DELIVERY_ID.fullmatch(expected_id):
        return False
    if not isinstance(entry["sender"], str) or not entry["sender"]:
        return False
    if entry["to_kind"] not in KINDS:
        return False
    if entry["to_alias"] is not None and (not isinstance(entry["to_alias"], str)
                                          or not entry["to_alias"]):
        return False
    if entry["transport"] not in TRANSPORTS or entry["proof"] not in PROOFS:
        return False
    if entry["state"] not in STATES:
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
        if entry[key] is not None and not isinstance(entry[key], str):
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
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def _new(delivery_id, sender, to_kind, to_alias, transport, proof, state, at,
         sender_kind=None):
    return {
        "version": LEDGER_VERSION, "id": delivery_id, "sender": sender,
        "sender_kind": sender_kind or _other(to_kind),
        "to_kind": to_kind, "to_alias": to_alias, "transport": transport,
        "proof": proof, "state": state, "sent_at": float(at),
        "sha256": None, "size": None, "attachment": None, "reason": None,
        "preview": None, "received_at": None, "read_at": None,
        "reported_at": None, "expired_unread_at": None,
    }


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
    if not _valid(entry, delivery_id):
        return False
    return _write(cwd, entry)


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


@contextlib.contextmanager
def _locked(cwd):
    """One exclusive lock over the ledger for a read-modify-write.

    Both sides' hooks mark entries — a receipt from one, a notice reported by
    the other — and an unlocked read-modify-write let the loser's field
    vanish; a lost receipt is permanent, because the record that carried it
    was consumed when the cursor advanced. The section is microseconds and a
    process that dies releases its flock, so a plain blocking lock is safe."""
    directory = _sound_dir(cwd, repair=True)
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
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _update(cwd, delivery_id, mutate):
    with _locked(cwd):
        entry = read_entry(cwd, delivery_id)
        if entry is None:
            return False
        changed = dict(entry)
        if mutate(changed) is False:
            return False
        if changed == entry:
            return True
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


def mark_received(cwd, delivery_id, at, to_kind=None, reader_alias=None):
    """The peer's transcript shows the delivery; the earliest sighting wins.
    Scoped like a read (`_for_reader`): the transcript belongs to a session of
    `to_kind` named `reader_alias`, and only its own deliveries are its."""
    def mutate(entry):
        if not _for_reader(entry, to_kind, reader_alias):
            return False
        if entry["received_at"] is None or at < entry["received_at"]:
            entry["received_at"] = float(at)
    return _update(cwd, delivery_id, mutate)


def _entries_naming(cwd, attachment):
    return [e for e in entries(cwd) if e["attachment"] == attachment]


def mark_read(cwd, attachment, at, to_kind=None, snapshot=None, reader_alias=None):
    """A transcript shows the attachment file being read.

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
    unread, whichever the sweep saw first."""
    found = False
    rows = entries(cwd) if snapshot is None else snapshot
    for entry in rows:
        if entry["attachment"] != attachment:
            continue
        if not _for_reader(entry, to_kind, reader_alias):
            continue
        found = True

        def mutate(changed):
            if changed["read_at"] is None or at < changed["read_at"]:
                changed["read_at"] = float(at)
            changed["expired_unread_at"] = None
        _update(cwd, entry["id"], mutate)
    return found


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
    """`{attachment basename: earliest read_at}` over every entry with a read
    receipt — one pass, for a sweep or a report that looks at many files."""
    times = {}
    for entry in entries(cwd):
        name, at = entry["attachment"], entry["read_at"]
        if name and at is not None and (name not in times or at < times[name]):
            times[name] = at
    return times


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
    named recipient (`_for_reader`). Receipts are folded to one per key (the
    earliest time) and the reads share one snapshot of the ledger, so a page
    naming a file several times costs one read of the directory, not one per
    mention."""
    earliest = {}
    for receipt in receipts:
        if len(receipt) == 3:
            (kind, key, at), alias = receipt, reader_alias
        elif len(receipt) == 4:
            kind, key, at, alias = receipt
        else:
            continue
        if kind not in ("received", "read"):
            continue
        if (kind, key, alias) not in earliest or at < earliest[(kind, key, alias)]:
            earliest[(kind, key, alias)] = at
    snapshot = None
    for (kind, key, alias), at in earliest.items():
        if kind == "received":
            mark_received(cwd, key, at, to_kind=read_by, reader_alias=alias)
        else:
            if snapshot is None:
                snapshot = entries(cwd)
            mark_read(cwd, key, at, to_kind=read_by, snapshot=snapshot,
                      reader_alias=alias)


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
        if entry["state"] == "sent" and entry["received_at"] is None:
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
    for entry in reversed(entries(cwd)):
        if (entry["state"] == "sent" and entry["attachment"]
                and entry["read_at"] is None
                and entry["sha256"] == sha256 and entry["to_kind"] == to_kind
                and entry["to_alias"] == to_alias
                and (sender is None or entry["sender"] == sender)
                and (sender_kind is None or sender_kind_of(entry) == sender_kind)
                and now - entry["sent_at"] <= LEDGER_TTL):
            path = os.path.join(cwd, ".antiphon", "messages", entry["attachment"])
            try:
                if stat.S_ISREG(os.lstat(path).st_mode):
                    return entry["attachment"]
            except OSError:
                continue
    return None


def prune(cwd, now):
    """Drop entries older than the ledger's own TTL. Best effort, never raises.

    An entry with something still to tell its sender — a refusal or an
    unread expiry not yet reported — is kept for a second TTL: the
    attachment TTL and this one are the same week, so the expiry a sweep
    marks on day seven would otherwise be pruned in the same hook, unheard."""
    directory = _sound_dir(cwd, repair=True)
    if directory is None:
        return
    for entry in entries(cwd):
        age = now - entry["sent_at"]
        if age <= LEDGER_TTL:
            continue
        unheard = entry["reported_at"] is None and (
            entry["state"] == "refused" or entry["expired_unread_at"] is not None)
        if unheard and age <= 2 * LEDGER_TTL:
            continue
        with contextlib.suppress(OSError):
            os.unlink(_path(cwd, entry["id"]))
