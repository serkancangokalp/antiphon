#!/usr/bin/env python3
"""Antiphon — an open-identity bridge between Claude Code and the Codex CLI.

Two terminals, two separate agents: what you tell one, the other finds out.

Usage:
  antiphon setup               # installs the hook on both sides
  antiphon status              # shows what's happening on both sides (for humans)
  antiphon summary [side]      # the text that side would see (claude | codex)
  antiphon hook <side>         # prompt and session hook (reads JSON from stdin)
  antiphon push <target>       # Stop hook: pushes `@codex` / `@claude` lines
  antiphon reply               # sends a Claude Channel reply to Codex (stdin JSON)
  antiphon channel             # long-lived Node.js MCP Channel server (started by Claude Code)
  antiphon mcp                 # MCP stdio server for Codex (fallback path)

Design: NO SHARED LOG IS KEPT. Both CLIs already write transcripts; Antiphon
reads and derives from them. That way there's no write race, no stale record,
and no second source of truth. The only persistent state is a cursor tracking
how far each side has read.

Both sides are symmetric: Claude Code and Codex CLI speak the same hook
contract (the same input fields, the same output wrapper), so a single `hook`
function serves both. Only `UserPromptSubmit` injects context; Codex also runs
it at `SessionStart`, where the session id arrives and nothing is injected.

The pull and hook layer uses the Python standard library; the Claude Channel
server runs on Node.js with the official MCP SDK.
"""

import glob
import collections
import contextlib
import errno
import fcntl
import hashlib
import heapq
import itertools
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import uuid

import peers
from datetime import datetime

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")

TAIL_BYTES = 300_000      # amount to read from the tail of each transcript file
EVENT_LIMIT = 40          # completed source records per page
PAGE_BUDGET = 8_000       # UTF-8 bytes in an ordinary complete page envelope
RECENT_FILES = 3          # transcript files per side the summary reads at all
LOOKBACK = 6 * 3600       # anything older than this doesn't count as part of "this session"

# EVENT_LIMIT and PAGE_BUDGET bound a complete page. RECENT_FILES still bounds
# discovery; it does not authorize cutting any record selected from that set.

# A marker at the start of a line in a reply says that line should be pushed
# to the target. The line-start requirement is deliberate: mentioning the
# marker inside prose shouldn't trigger it.
# Parsed in two stages so no line addressed at the other side can vanish. Every
# single-regex version tried here dropped something silently: one refused
# `@claude:BAD run` outright, another swallowed the comma in `@claude:api, run
# it` into the name, a third lost `@claude:api,run it` entirely. A marker line
# that disappears because its name was punctuated oddly is exactly the failure
# this bridge exists to remove.
MARKER_SIDES = ("claude", "codex")
PUSH_MARKERS = re.compile(r"^\s*@(?P<side>claude|codex)\b(?P<rest>.*)$", re.MULTILINE)
# A colon followed by whitespace is the unaddressed form and means what it has
# always meant; a colon followed by anything else is a name being claimed,
# however malformed.
MARKER_ALIAS = re.compile(r"^:(?P<claim>\S+)")
# The unaddressed form's delimiter, consumed once and only when no name was
# claimed. Stripping a *set* of characters here ate the message's own
# punctuation: `@claude:api .NET issue` arrived as "NET issue".
MARKER_DELIMITER = re.compile(r"^[:,]?[ \t]*")


def parse_markers(target, text):
    """[(alias, message)] for every marker line addressed at `target`.

    `alias` is None when no name was claimed, `""` when one was claimed and is
    empty, and the raw string otherwise. `message` may be empty. Nothing is
    filtered here: whether a name exists is routing's decision, and a line the
    human wrote and the bridge swallowed without a word is the thing to avoid.
    """
    found = []
    for match in PUSH_MARKERS.finditer(text or ""):
        if match.group("side") != target:
            continue
        rest, alias = match.group("rest"), None
        claim = MARKER_ALIAS.match(rest)
        if claim:
            # The claim already swallowed any delimiter attached to the name, so
            # only whitespace separates it from the message. Anything else here
            # belongs to the message.
            alias = claim.group("claim").rstrip(",;:.")
            rest = rest[claim.end():].lstrip(" \t")
        else:
            rest = MARKER_DELIMITER.sub("", rest, count=1)
        found.append((alias, rest.rstrip()))
    return found


def group_by_recipient(target, text):
    """{recipient or None: [messages]}, in the order they were written.

    Keyed by the alias itself. `alias or ""` would fold None and "" together and
    hand `@claude:: fix` to the unaddressed path, undoing the parser's care in
    telling them apart.
    """
    batches = {}
    for alias, message in parse_markers(target, text):
        batches.setdefault(alias, []).append(message)
    return batches


def batch_fingerprint(messages):
    """Canonical JSON, full digest.

    A newline join collides: ["a\nb", "c"] and ["a", "b\nc"] hash identically,
    so one batch would suppress a different one.
    """
    return hashlib.sha256(json.dumps(
        messages, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


# Where a pre-digest cursor value is parked when a record has to be written
# beside it. `\0` cannot appear in a peer name, so it can never be mistaken for
# a recipient slot — `""` is the unaddressed one and every other is `@alias`.
LEGACY_SLOT = "\0legacy"


def migrate_pushed(sent, unaddressed):
    """(record, already_delivered) for a cursor that may hold the old format.

    The old format stored the joined text rather than a digest, so comparing it
    against a digest is always unequal and would resend the last message once on
    upgrade. It is compared in its own form instead — either as the whole value,
    or parked in `LEGACY_SLOT` beside a record that had to be written next to
    it. It is only given up when something really supersedes it: a matching bare
    batch here, or a new unaddressed delivery via `forget_superseded`. It cannot
    be converted into a digest, because a batch of two lines and one line that
    joins to the same string are different things.

    Any other shape — a list, a number, a hand-edited mistake — starts over as
    an empty record rather than raising. `dict()` on a list is a TypeError, and
    it would surface as a traceback out of the Stop hook, which is the last
    place a malformed file should be able to reach.
    """
    if isinstance(sent, str):
        if bool(unaddressed) and sent == "\n".join(unaddressed):
            return {}, True           # the caller sets `""` in its place
        # Not superseded: this turn may be named-only, or the bare batch may be
        # a different message, or its delivery may fail. Parked rather than
        # dropped, because it is the only record that the last bare message
        # already went, and losing it sends that message a second time.
        return {LEGACY_SLOT: sent}, False
    if isinstance(sent, dict):
        record = dict(sent)
        legacy = record.get(LEGACY_SLOT)
        already = (bool(unaddressed) and isinstance(legacy, str)
                   and legacy == "\n".join(unaddressed))
        if already:
            record.pop(LEGACY_SLOT)   # the caller is about to set `""` instead
        return record, already
    return {}, False


def forget_superseded(record):
    """Drops the legacy value once the unaddressed slot holds a digest.

    Until then it is kept. It describes the last *unaddressed* delivery, so a
    turn that sent only named lines has superseded nothing, and clearing it
    there would resend that message the next time somebody writes a bare line.
    """
    if "" in record:
        record.pop(LEGACY_SLOT, None)
    return record


def push_fingerprint(turn_key, messages):
    """The dedupe fingerprint for one batch, scoped to the turn that said it.

    Content alone is not identity: `@claude do SAME` in one turn and the exact
    same line in a later turn hash identically under `batch_fingerprint`, so a
    genuinely new instruction that happens to repeat old wording was silently
    swallowed — measured, `send_to_claude` called once where two turns each
    said it once. A non-empty `turn_key` (the matched Codex `turn_id`, or the
    `uuid` of the Claude boundary record that opened the turn) folds the
    turn's own identity into the fingerprint as a structured `[key, messages]`
    pair, which cannot collide with the flat legacy shape below. An empty key
    — no `turn_id` on a pre-`turn_id` Codex hook, or a Claude boundary that
    scrolled out of the tail window or carries no `uuid` — has no turn to
    scope to, and falls back to the original content-only digest unchanged:
    both for continuity with every cursor already on disk, and because a
    repeat with no nameable turn is exactly the case content-only dedupe was
    always meant for.
    """
    if turn_key:
        return batch_fingerprint([turn_key, messages])
    return batch_fingerprint(messages)


def deliver_batches(batches, sent, deliver, turn_key=""):
    """Calls `deliver(recipient, messages)` for each batch that has not gone yet.

    A recipient's fingerprint advances only if its own delivery succeeded, so one
    failure does not suppress its retry while another recipient's success is
    kept. The key is `""` for unaddressed and `"@alias"` otherwise, so a peer
    named the empty string cannot collide with the unaddressed slot.

    `turn_key` scopes every fingerprint computed here to the turn `push`
    resolved it from; see `push_fingerprint`. The default keeps every other
    caller's content-only behaviour exactly as it was.

    A slot can also hold the flat, content-only digest this exact batch
    already carried before turn scoping shipped — written by an older
    binary, or by an earlier push that resolved no turn key for this same
    content. Once `turn_key` is non-empty that flat value can no longer equal
    the scoped fingerprint, so comparing only against the new shape would
    call already-delivered content new and resend it — measured: one
    resend, then the slot converts. Recognised here the same way the
    string-cursor migration is: matched against its own old shape, then
    upgraded in place to the scoped digest without sending again.
    """
    for recipient, messages in batches.items():
        key = "" if recipient is None else f"@{recipient}"
        fingerprint = push_fingerprint(turn_key, messages)
        if sent.get(key) == fingerprint:
            continue
        if turn_key and sent.get(key) == batch_fingerprint(messages):
            sent[key] = fingerprint
            continue
        if deliver(recipient, messages):
            sent[key] = fingerprint
    return sent
SESSION_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                        r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")

# The labels every Claude→Codex message carries. `push()` and `reply()` send
# these, and the self-injection guard below recognizes them — one definition,
# so the two can't drift apart.
PUSH_LABEL = "[Antiphon bridge] Claude:"
CHANNEL_LABEL = "[Antiphon channel] Claude:"

# A message this bridge pushed arrives on the other side as an ordinary
# `role: user` event. Skipping those keeps the summary from echoing the
# bridge's own traffic back at it. Matched on the label, anchored at the start:
# a substring test would also swallow a user typing "antiphon is dropping
# messages", and that message would then never reach the other agent.
_SELF_INJECTION_PREFIXES = tuple(
    label.lower() for label in (PUSH_LABEL, CHANNEL_LABEL))


def _is_self_injected(text):
    """True if `text` is a message this bridge itself delivered."""
    return text.lstrip().lower().startswith(_SELF_INJECTION_PREFIXES)


def _join_text_blocks(blocks):
    """Join present text blocks without treating whitespace as absence."""
    return "\n\n".join(block for block in blocks
                        if isinstance(block, str) and block != "")


# A host writes records of its own into the same transcript a person types
# into: slash commands, their output, task notifications, and this bridge's
# own deliveries. They are recognised by what they are — the host's
# `promptSource`, or a complete opening tag from a closed, measured set —
# never by a leading `<`. A user pasting HTML, JSX or a stack trace starts
# with `<` too, and used to vanish.
#
# The sets are per side because the tags are: `ide_opened_file` is Claude
# Code's, `recommended_plugins` and `realtime_delegation` are Codex's, and
# `local-command-caveat` was seen only on the Claude side. Shared, one side's
# tag would silence the other side's user for typing that text.
# `image` belongs in neither: it is a person's attachment.

# Strictly what was measured on that side, and nothing else. A tag missing
# here costs one stray host line in a summary — visible, and fixed by adding
# it. A tag here that a person could type costs that person's whole message,
# silently. Adding a plausible sibling by symmetry is how `local-command-caveat`
# — measured only on the Claude side — first reached the Codex set.
# Measured on 2026-08-30 over 1,575 Claude and 445 Codex `role: user` records;
# see BACKLOG.md for when this census has to be re-run.
CLAUDE_HOST_WRAPPERS = (
    "channel", "task-notification", "ide_opened_file",
    "command-name", "command-message",
    "local-command-caveat", "local-command-stdout",
    "bash-input", "bash-stdout",
)

CODEX_HOST_WRAPPERS = (
    "task-notification", "recommended_plugins", "realtime_delegation",
    "subagent_notification", "environment_context",
    "command-name", "command-message", "local-command-stdout",
    "bash-input", "bash-stdout",
)

# `promptSource` values measured carrying host records as well as people's
# words, so neither answer can be taken from the field alone and the shape of
# the record decides. An absent field is the same case. Every other value,
# including one this code has never seen, means a person: refusing an unknown
# source would let a future host version silence someone in silence.
MIXED_SOURCES = ("sdk",)


def _wrapper_pattern(names):
    """`(?=[\\s>/])` makes it a whole tag name: `<channels of …>` is not `<channel>`."""
    return re.compile(r"<(?:" + "|".join(re.escape(name) for name in names) + r")(?=[\s>/])")


CLAUDE_WRAPPER_OPENING = _wrapper_pattern(CLAUDE_HOST_WRAPPERS)
CODEX_WRAPPER_OPENING = _wrapper_pattern(CODEX_HOST_WRAPPERS)


def _is_host_record(text, wrappers, prompt_source=None):
    """True if `text` is something the host put in the transcript.

    `wrappers` is the compiled tag set for the side being read.
    `prompt_source` is Claude Code's `promptSource` field, absent on the Codex
    side and on older Claude records. `system` settles it: the host wrote this.
    A value measured to carry both kinds — or no field at all — leaves only the
    shape of the record, where a known wrapper tag is the one thing refused.
    Any other value means a person. Unknown provenance delivers; it never
    silences.
    """
    if prompt_source == "system":
        return True
    if prompt_source and prompt_source not in MIXED_SOURCES:
        return False
    return wrappers.match((text or "").lstrip()) is not None


def sender_alias(candidate):
    """A candidate alias if it could be one, else None.

    A pure check, with no fallback to the environment. Every caller is handed
    its candidate by whatever actually established it — the registry claim, or
    the channel server that holds it — and reaching for `ANTIPHON_NAME` here
    would put back the assumption the callers exist to replace.
    """
    return candidate if peers.valid_name(candidate) else None


def claimed_alias(cwd, kind):
    """This session's alias, but only if this session really holds it.

    `ANTIPHON_NAME` is a request, not a claim. Two sessions can be started with
    the same one and exactly one wins the registry. The loser publishing it
    anyway would attribute its words to the winner, and a reply addressed back
    would reach a session that never spoke — the misidentification the registry
    exists to end, arriving through the label meant to prevent it.

    So the alias is published only when the live record under it belongs to
    this session, matched on the owner key. Anything that cannot be shown —
    no key, no record, a record from another owner, a record written before
    owner keys existed — yields None. A wrong identity is worse than none.
    """
    alias = sender_alias(peers.explicit_name())
    if not alias:
        return None                   # nothing asked for; nothing to check
    owner = peers.owner_key()
    if not owner:
        return None
    for peer in peers.read_peers(cwd, kind):
        if peer.get("name") == alias:
            return alias if peer.get("owner") == owner else None
    return None


def delivery_id():
    """An id for one delivery attempt.

    It says which attempt, and nothing more. It is deliberately not a
    correlation id: holding one logical id across a retry needs pending-delivery
    state this release does not have, and calling it correlation would promise
    reply routing that is not implemented.
    """
    return str(uuid.uuid4())


# What an unaddressable sender renders as, and the registry key such a peer
# occupies: one word for "this peer has no name", wherever that has to be said.
# Angle brackets are chosen precisely because `valid_name` cannot produce them —
# `unnamed` on its own is a perfectly legal `ANTIPHON_NAME`, so a bare
# `from=unnamed` would mean either "this peer has no name" or "this peer is
# called unnamed", and the reader could not tell which.
NO_ALIAS = peers.UNNAMED


def queue_label(alias, message_id):
    """`[from=<alias> id=<uuid>]` for the paths that carry only text.

    `codex queue` takes a message and no metadata, so what the socket puts in
    `meta` has to be visible here. It goes **after** the bridge or channel
    prefix, never before: those prefixes anchor the self-injection filter and
    the echo guard, and a message that no longer starts with one would be read
    back as new traffic and delivered again.

    `alias` is already validated, so it cannot close this bracket and open
    another.
    """
    return f"[from={alias or NO_ALIAS} id={message_id}]"


# ---------- helpers ----------

def project_dir():
    return os.path.abspath(os.environ.get("ANTIPHON_CWD") or os.getcwd())


def state_path(cwd, kind):
    """Where this peer's cursor lives. `kind` is the side the caller runs on.

    A named peer owns its own file. An unnamed one keeps the project-wide path:
    without a name there is one peer per side by definition, so there is nothing
    to race with, and moving the path would strand every existing install.
    """
    name = peers.explicit_name()
    if peers.valid_kind(kind) and peers.valid_name(name):
        return os.path.join(peers.peer_dir(cwd, kind, name), "cursor.json")
    return os.path.join(cwd, ".antiphon", "cursor.json")


CURSOR_LOCK_PATIENCE = 2.0        # seconds; a stuck holder must not hang a turn
CURSOR_LOCK_RETRY_DELAY = 0.05


@contextlib.contextmanager
def cursor_lock(cwd, kind, patience=None):
    """Serializes one peer's whole read-select-deliver-advance transaction.

    Yields True when the lock was taken and False when it was not, having
    already said on stderr why not. A caller that could not take it must
    deliver nothing rather than proceed unserialized, and must exit non-zero:
    on exit 0 the host sends stderr to a debug log and shows the person
    nothing, so a leaked descriptor would deafen the bridge with no symptom.

    Locking only the selection would not do. A takes the lock, picks a page and
    releases before writing; B then reads a cursor that has not moved and picks
    the same page. The exclusion has to cover the write and the advance too —
    and every other writer of this file has to take the same lock, or it can
    write back a snapshot from before the advance.

    This is a lock beside the cursor, never the project-wide registry lock.
    That one serializes every claim, refresh, prune and release in the project,
    and holding it across a model-facing write would make an unrelated peer's
    start, stop or refresh queue behind this peer's context page. Named peers
    each own a separate cursor file, so a lock beside each one gives exactly
    the exclusion required without coupling their lifetimes — for a named
    install. The default, unnamed install has no name to split on: `state_path`
    returns the same file for `claude` and for `codex` alike, so this lock
    guards both sides of the project at once. A caller that holds it for long
    is not only making its own peer wait; it is making **the other agent**
    wait, on a bridge with no name in play to tell them apart.

    The wait is bounded because the hook runs on a person's every prompt: a
    holder that is stuck rather than dead would otherwise hang the turn. A
    holder that dies has its `flock` released by the kernel, so a crash frees
    the lock rather than wedging the project.

    Not reentrant. `flock` is held per open file description, so a second
    attempt from this same process on a fresh descriptor blocks exactly as
    another process would. Nothing called while this is held may take it again.
    """
    if patience is None:
        # Read at call time, not bound in the signature, so the constant stays
        # the single place this is set — and a test can lower it.
        patience = CURSOR_LOCK_PATIENCE
    path = state_path(cwd, kind) + ".lock"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        # The lock file could not even be opened — a directory that is not
        # writable, or not a directory at all.
        print(f"antiphon: no delivery lock at {path}: {exc}", file=sys.stderr)
        yield False
        return
    held = False
    deadline = time.monotonic() + patience
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                # The only errno that means "somebody else has it".
                if time.monotonic() >= deadline:
                    # Neutral on purpose: this fires for callers that deliver
                    # context and for callers that only record a push, and
                    # "context not delivered" was a lie for the second kind.
                    # The caller knows what it was trying to do and says so
                    # itself, on its own line.
                    print("antiphon: another delivery for this peer is still "
                          f"running after {patience:g}s", file=sys.stderr)
                    break
                time.sleep(CURSOR_LOCK_RETRY_DELAY)
            except OSError as exc:
                # ENOTSUP, EIO, ENOLCK: a filesystem whose lock manager cannot
                # answer. Retrying that for the full patience and then giving
                # up quietly would turn a broken mount into a bridge that
                # stopped delivering for no stated reason.
                print(f"antiphon: cannot lock {path}: {exc}", file=sys.stderr)
                break
        yield held
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def sender_side(target):
    """The side a message addressed to `target` is being sent from."""
    return "codex" if target == "claude" else "claude"


LEGACY_KEYS = {
    "codex_gordu": "codex_seen",
    "claude_gordu": "claude_seen",
    "son_itilen_codex": "last_pushed_codex",
    "son_itilen_claude": "last_pushed_claude",
    "son_itilen": "last_pushed_codex",
}


def _translate_cursor_keys(data):
    return {LEGACY_KEYS.get(k, k): v for k, v in data.items()}


def _read_cursor_state(cwd, kind):
    """Return ``(cursor, state)`` without confusing absence with corruption."""
    new_path = state_path(cwd, kind)
    try:
        with open(new_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print("antiphon: the existing cursor could not be read safely; "
              "restarting discovered transcript history", file=sys.stderr)
        return {}, "invalid"
    if not isinstance(data, dict):
        print("antiphon: the existing cursor is not a usable object; "
              "restarting discovered transcript history", file=sys.stderr)
        return {}, "invalid"

    # Translated for the caller and not written back. This runs inside the
    # delivery hold, and `flock` is per open file description, so taking the
    # lock here would block this process against itself; writing without it
    # would let a read put back a snapshot from before somebody's advance.
    # The translation is idempotent, so the next write from a locked path
    # persists it and nothing behaves differently until then.
    return _translate_cursor_keys(data), "valid"


def read_cursor(cwd, kind):
    """Return the translated cursor object for backwards-compatible callers."""
    return _read_cursor_state(cwd, kind)[0]


def cursor_time(cursor, key, default=None):
    """The timestamp `key` holds, or the normal lookback when it holds no time.

    Every reader of a `_seen` value went through `float(cursor.get(key) or ...)`,
    which raises on a string that is not a number and passes `NaN` and
    `Infinity` straight through — and `json` parses both of those literals by
    default, so a cursor really can hold them. A `NaN` start makes every
    comparison against it false, so nothing is ever new again and the bridge
    goes quiet without saying so; an infinite one survives that far and raises
    in `datetime.fromtimestamp` instead. So does a finite `1e308`, which is a
    number and not any time this machine can name. All of them are answered the
    way a missing value already is: the normal lookback.

    Finite numeric strings are still accepted, because `float()` accepted them
    and a peer upgrading with one on disk must not silently replay six hours.
    """
    if default is None:
        default = time.time() - LOOKBACK
    value = cursor.get(key) if isinstance(cursor, dict) else None
    if isinstance(value, bool) or not value:
        # `True` is an `int` and `float(True)` is 1.0 — a 1970 start that would
        # replay the whole transcript. It is not a time; neither is 0 or None.
        return default
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return default
    if not isinstance(value, (int, float)):
        return default
    try:
        value = float(value)
        datetime.fromtimestamp(value)
    except (ValueError, OverflowError, OSError):
        # Finite, and still not a time. `1e308` passes every check `NaN` and the
        # infinities fail, and no clock can render it — used as a start it makes
        # every transcript line look old, so the bridge goes quiet and stays
        # quiet. `fromtimestamp` is the only authority on what this platform can
        # hold as a local time, so asking it is the whole check.
        return default
    return value


CURSOR_VERSION = 2
PAGE_CURSOR_VERSION = 3


def page_cursor_key(side):
    return "%s_pages" % side


def _valid_position(entry):
    """A cursor entry is trusted only when every part of it is what it claims.

    This file is hand-edited, restored from the wrong place, and written by
    other versions — the suite has a class about exactly that. An entry that is
    not a position must send the caller to the lookback, which repeats, rather
    than into a seek to byte 1 or an exception out of a hook.
    """
    return (isinstance(entry, dict)
            and isinstance(entry.get("gen"), str)
            and isinstance(entry.get("offset"), int)
            and not isinstance(entry.get("offset"), bool)
            and entry["offset"] >= 0)


def positions_for(cursor, side, loader_state="valid"):
    """Return ``(positions, since, replay_reason)`` for one paging reader.

    A v2 value is deliberately never reinterpreted as a delivered page
    frontier. Its presence requests a bounded byte-zero replay under the
    separate v3 key, which keeps an overlapping old process from advancing a
    new reader past content it did not deliver.
    """
    if loader_state == "invalid":
        return {}, None, "cursor_recovery"
    cursor = cursor if isinstance(cursor, dict) else {}
    key = page_cursor_key(side)
    legacy_key = "%s_seen" % side
    since = time.time() - LOOKBACK
    if key in cursor:
        value = cursor.get(key)
        if (isinstance(value, dict)
                and value.get("v") == PAGE_CURSOR_VERSION
                and isinstance(value.get("sources"), dict)):
            sources = {sid: entry for sid, entry in value["sources"].items()
                       if _valid_position(entry)}
            if len(sources) == len(value["sources"]):
                replay = value.get("replay")
                if (replay is not None
                        and (not isinstance(replay, str)
                             or replay not in REPLAY_NOTICES)):
                    print("antiphon: cursor replay metadata was invalid and was "
                          "ignored", file=sys.stderr)
                    replay = None
                return sources, since, replay
        print("antiphon: paging cursor state was invalid; restarting discovered "
              "transcript history", file=sys.stderr)
        return {}, None, "cursor_recovery"
    if legacy_key in cursor:
        return {}, None, "legacy_upgrade"
    return {}, since, None


def _advance_page_cursor(cwd, kind, cursor, side, positions, advance):
    """Persist the delivered source prefix and replay lifecycle as one value."""
    if advance is None:
        return True
    merged = dict(positions)
    merged.update(advance.sources)
    value = {"v": PAGE_CURSOR_VERSION, "sources": merged}
    if advance.has_more and advance.replay_reason in REPLAY_NOTICES:
        value["replay"] = advance.replay_reason
    cursor[page_cursor_key(side)] = value
    return write_cursor(cwd, cursor, kind)


def write_cursor(cwd, data, kind):
    path = state_path(cwd, kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def update_cursor(cwd, kind, mutate):
    """Read-modify-write one peer's cursor inside its lock.

    Every writer of `cursor.json` goes through here. The file holds both the
    `_seen` timestamps and the push fingerprints, and each writer rewrites the
    whole object, so a snapshot read outside the lock and written inside it
    silently reverts whatever happened in between — measured: an advance from
    1.0 to 2.0 undone by a push that had read the file first.

    `mutate` is called with the freshly read cursor and returns the object to
    write. There are three outcomes, and only the middle one means anything
    was lost: `False` if the lock could not be taken (nothing was read,
    changed or written), `True` with nothing written when `mutate` changed
    nothing, and `True` after a real write otherwise.

    A `mutate` that changes nothing writes nothing. It is handed a copy that
    nothing else holds, so it may edit in place; the value read from disk is
    kept intact to compare against. Two reads would have been cheaper and
    wrong: a caller cannot see whether `read_cursor` returns a fresh object,
    and under a test double that returns the same one, an in-place edit makes
    every write look unnecessary.

    `mutate` is expected to return a dict — the docstring above says "it may
    edit in place", and a lambda built for `.update(...)`'s return value
    returns `None` instead. Written as-is, that overwrites the cursor with
    `null`: every `_seen` timestamp and every push fingerprint gone, silently,
    and `updated == before` never catches it because `None != {}`. Refused
    here instead, because this is the one funnel every writer shares.
    """
    with cursor_lock(cwd, kind) as locked:
        if not locked:
            return False
        before, state = _read_cursor_state(cwd, kind)
        if state == "invalid":
            print("antiphon: refusing to update an invalid cursor", file=sys.stderr)
            return False
        updated = mutate(json.loads(json.dumps(before)))
        if not isinstance(updated, dict):
            print(f"antiphon: mutate returned {type(updated).__name__}, not a "
                  f"dict; refusing to overwrite {state_path(cwd, kind)}",
                  file=sys.stderr)
            return False
        if updated == before:
            return True
        return write_cursor(cwd, updated, kind)


def truncate(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n].rstrip() + "…"


def tail_lines(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def source_id(path):
    """What a transcript is, as opposed to where it currently sits.

    Both hosts name a transcript after the session that wrote it — Claude Code
    as `<uuid>.jsonl`, Codex as `rollout-<timestamp>-<uuid>.jsonl` — and that
    uuid outlives a move, a rename of the project directory, or a copy. A path
    does not, and a cursor keyed on one would start again from nothing the
    first time anything moved. Anything without a uuid falls back to the
    basename, which is still narrower than the path.
    """
    match = SESSION_ID.search(path)
    return match.group(1) if match else os.path.basename(path)


def source_generation(path):
    """An identity for *this* file at this path, or None if it cannot be read.

    An offset is only meaningful inside one immutable run of a file. Rotation
    puts a different file at the same name and an offset into the old one lands
    anywhere in the new one, so something has to be able to say "no, this is not
    what you were reading". Device and inode catch a replacement; the hash of
    the first record catches the case where an inode is reused, which happens
    more often than it sounds on a busy temporary filesystem.
    """
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            first = f.readline()
            if not first.endswith(b"\n"):
                # A file whose only line is still being written has no stable
                # first record yet. Treat it as unidentifiable rather than
                # fingerprinting a half-line that will change.
                return None
    except OSError:
        return None
    digest = hashlib.sha256(first).hexdigest()[:16]
    return "%d:%d:%s" % (st.st_dev, st.st_ino, digest)


def read_records(path, offset=0):
    """Yield `(start, end, line)` for each complete line at or after `offset`.

    `line` is decoded text without its newline; `start` and `end` are byte
    offsets, so `end` of the last record is where the next read begins. A
    trailing partial line is not a record: it yields nothing and leaves `end`
    before it, so the writer can finish it and the next read picks it up whole.

    This replaces reading a fixed window at the end of the file. That window
    made a record larger than itself invisible — not truncated, never seen —
    while an offset costs only the bytes that are actually new.
    """
    try:
        with open(path, "rb") as f:
            if offset:
                f.seek(offset)
            position = offset
            for raw in f:
                if not raw.endswith(b"\n"):
                    return          # incomplete: not a record yet
                start, position = position, position + len(raw)
                yield start, position, raw[:-1].decode("utf-8", "replace")
    except OSError:
        return


def head_lines(path, limit=12, num_bytes=64 * 1024):
    """Returns the lines at the start of the file, used for session metadata."""
    try:
        with open(path, "rb") as f:
            return f.read(num_bytes).decode("utf-8", "replace").splitlines()[:limit]
    except OSError:
        return []


def iso_epoch(s):
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


Event = collections.namedtuple("Event", "time kind text source generation offset end")
Record = collections.namedtuple(
    "Record", "time source generation offset end events")
PageAdvance = collections.namedtuple(
    "PageAdvance", "sources has_more replay_reason")
REPLAY_NOTICES = {
    "legacy_upgrade": (
        "replay: replaying discovered history after an upgrade; duplicates "
        "are expected until this backlog drains"),
    "cursor_recovery": (
        "replay: replaying discovered history because the previous cursor "
        "could not be trusted; duplicates are expected until this backlog "
        "drains"),
}


def offset_at_or_after(path, timestamp):
    """The offset of the first record at or after `timestamp`, or the file's end.

    Run only for a source a peer has genuinely never read, to place the normal
    lookback window. It is deliberately NOT how legacy cursors arrive here: a
    present v2/`_seen` value, like a malformed or unreadable cursor file, takes
    the conservative byte-zero replay instead, because an old process may still
    be moving that value and its boundary cannot be trusted. `>=` rather than
    `>` repeats a record sharing the boundary timestamp — a duplicate, which
    this bridge accepts where it never accepts a gap.
    """
    end = 0
    for start, end, line in read_records(path):
        try:
            when = iso_epoch(json.loads(line).get("timestamp"))
        except (json.JSONDecodeError, AttributeError):
            continue
        if when >= timestamp:
            return start
    return end


def _source_size(path):
    """The file's size, or None when it could not be measured at all.

    `None` is not zero: a file `stat` cannot reach -- vanished, permissions
    changed underneath the bridge -- has no size to compare a recorded offset
    against, and treating that as "zero bytes long" would tell `_start_offset`
    the file had shrunk, which is a different fact and points at the wrong
    cause.
    """
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _start_offset(path, sid, generation, positions, since):
    """Where to start reading one source: its recorded offset, when the file
    is still the one that offset was measured against and has not shrunk
    underneath it; from byte zero, when the recorded offset cannot be
    trusted; from the lookback, or byte zero, only when there is no
    recorded entry to distrust in the first place.

    Every reason to distrust a recorded offset resolves the same way —
    because repeating records is the error this bridge accepts and skipping
    them is the one it does not.
    """
    recorded = (positions or {}).get(sid)
    if recorded:
        # Both branches below return 0, not the shared fallback at the end of
        # this function. An offset that cannot be trusted says nothing about
        # what this peer has already seen, so the whole source is offered
        # again; bounding that by the lookback (the shared fallback) would
        # skip everything older than it -- a gap, where a repeat is the error
        # this bridge accepts everywhere else. That fallback answers a
        # different question: a source with no recorded entry at all.
        if recorded.get("gen") != generation:
            print("antiphon: a transcript was replaced since it was last read; "
                  "reading it again", file=sys.stderr)
            return 0
        size = _source_size(path)
        if size is None:
            print("antiphon: a transcript could not be measured; reading it again",
                  file=sys.stderr)
            return 0
        if recorded["offset"] > size:
            print("antiphon: a transcript is shorter than the %d bytes already "
                  "read from it; reading it again" % recorded["offset"],
                  file=sys.stderr)
            return 0
        return recorded["offset"]
    return offset_at_or_after(path, since) if since is not None else 0


# ---------- Claude side ----------

def _claude_slug(cwd):
    """The ~/.claude/projects directory name Claude Code derives from a path.

    Every character that isn't alphanumeric becomes `-`, not just `/`: an
    underscore, a dot and an existing `-` all end up as `-`. Getting this
    wrong is silent — the slug simply names no directory, and the whole
    Claude→Codex direction goes empty forever."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _transcript_cwd(path):
    """The project directory a Claude transcript records, or None."""
    for line in head_lines(path, limit=40):
        if '"cwd"' not in line:       # cheap pre-filter, skips parsing big lines
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("cwd"), str):
            return d["cwd"]
    return None


def _find_claude_project_dir(cwd):
    """Finds the project directory by the cwd its transcripts carry.

    The fallback for when the slug rule names no existing directory — an older
    naming rule, or a new one. Only reached when the slug misses, so the
    common path never pays for the scan."""
    try:
        entries = [os.path.join(CLAUDE_PROJECTS, name)
                   for name in os.listdir(CLAUDE_PROJECTS)]
    except OSError:
        return None
    def mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:               # vanished mid-scan; sort it last
            return 0.0

    candidates = [p for p in entries if os.path.isdir(p)]
    candidates.sort(key=mtime, reverse=True)               # most recently used first
    for directory in candidates:
        transcripts = sorted(glob.glob(os.path.join(directory, "*.jsonl")),
                             key=mtime, reverse=True)
        for path in transcripts[:3]:
            recorded = _transcript_cwd(path)
            if recorded is not None:
                if recorded == cwd:
                    return directory
                break                 # this directory belongs to another project
    return None


def claude_project_dir(cwd):
    """The ~/.claude/projects directory holding this project's transcripts."""
    directory = os.path.join(CLAUDE_PROJECTS, _claude_slug(cwd))
    if os.path.isdir(directory):
        return directory
    return _find_claude_project_dir(cwd)


def claude_transcripts(cwd):
    """Claude Code transcript files belonging to this project directory (newest first)."""
    directory = claude_project_dir(cwd)
    if not directory:
        return []
    files = [p for p in glob.glob(os.path.join(directory, "*.jsonl"))
             if os.path.getsize(p) > 0]
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def claude_events(cwd, positions=None, since=None, visible_record_limit=None):
    """Return visible events and the safe scanned position for each source.

    A completed JSONL record consumes at most one visible lookahead slot even
    when it contains several text and tool blocks. Filtered records consume no
    slot, so the scanner can pass them to EOF or to the next visible record.
    """
    events = []
    reached = {}
    position = itertools.count()
    for path in claude_transcripts(cwd)[:RECENT_FILES]:
        visible_records = 0
        sid = source_id(path)
        gen = source_generation(path)
        offset = _start_offset(path, sid, gen, positions, since)
        if gen is not None:
            reached[sid] = {"gen": gen, "offset": offset}
        for start, end, line in read_records(path, offset):
            if gen is not None:
                reached[sid] = {"gen": gen, "offset": end}
            before = len(events)
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                d = None
            if isinstance(d, dict) and not d.get("isMeta"):
                ts = iso_epoch(d.get("timestamp"))
                kind = d.get("type")
                msg = d.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                if kind == "user":
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = _join_text_blocks(
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
                    if (text != ""
                            and not _is_host_record(text, CLAUDE_WRAPPER_OPENING,
                                                    d.get("promptSource"))
                            and not _is_self_injected(text)):
                        events.append((ts, path, next(position),
                                      Event(ts, "you", text, sid, gen, start, end)))
                elif kind == "assistant":
                    for c in content if isinstance(content, list) else []:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text":
                            text = c.get("text")
                            if isinstance(text, str) and text != "":
                                events.append((ts, path, next(position),
                                              Event(ts, "claude", text,
                                                    sid, gen, start, end)))
                        elif c.get("type") == "tool_use":
                            arguments = c.get("input") or {}
                            arguments = arguments if isinstance(arguments, dict) else {}
                            detail = (arguments.get("file_path")
                                      or arguments.get("command")
                                      or arguments.get("pattern") or "")
                            events.append((ts, path, next(position),
                                          Event(ts, "tool",
                                                f"{c.get('name', '?')} {detail}".strip(),
                                                sid, gen, start, end)))
            if len(events) > before:
                visible_records += 1
                if (visible_record_limit is not None
                        and visible_records >= visible_record_limit):
                    break
    events.sort(key=lambda item: (
        item[0], item[3].source, item[3].generation or "",
        item[3].offset, item[2]))
    return [item[3] for item in events], reached


# ---------- Codex side ----------

def _rollout_cwd(lines):
    """The cwd a Codex rollout records in its session metadata (None if absent)."""
    for line in lines:
        if '"cwd"' not in line:       # cheap pre-filter, skips parsing big lines
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        for holder in (d, d.get("payload")):
            if isinstance(holder, dict) and isinstance(holder.get("cwd"), str):
                return holder["cwd"]
    return None


def codex_rollout_files(cwd, days=3):
    """Codex rollout files whose cwd matches this project (newest first)."""
    pattern = os.path.join(CODEX_SESSIONS, "**", "rollout-*.jsonl")
    candidates = []
    now = time.time()
    for path in glob.glob(pattern, recursive=True):
        try:
            if now - os.path.getmtime(path) > days * 86400:
                continue
        except OSError:
            continue
        candidates.append(path)
    candidates.sort(key=os.path.getmtime, reverse=True)
    matched = []
    for path in candidates[:60]:
        # cwd lives in the session metadata. Reading the tail of a growing,
        # active rollout could miss it and match the wrong, already-closed
        # subsession instead.
        lines = head_lines(path)
        recorded = _rollout_cwd(lines)
        if recorded is not None:
            # An equality test, never a substring one: `/x/api` used to match
            # a rollout recorded for `/x/api-v2`, and a push then landed in
            # the sibling project's Codex.
            if recorded == cwd:
                matched.append(path)
            continue
        # No head line carries a cwd field: fall back to the old substring
        # test rather than dropping a file we can't read properly.
        if any(cwd in line for line in lines):
            matched.append(path)
    return matched


def codex_events(cwd, positions=None, since=None, visible_record_limit=None):
    """Return visible events and the safe scanned position for each rollout."""
    events = []
    reached = {}
    position = itertools.count()
    for path in codex_rollout_files(cwd)[:RECENT_FILES]:
        visible_records = 0
        sid = source_id(path)
        gen = source_generation(path)
        offset = _start_offset(path, sid, gen, positions, since)
        if gen is not None:
            reached[sid] = {"gen": gen, "offset": offset}
        for start, end, line in read_records(path, offset):
            if gen is not None:
                reached[sid] = {"gen": gen, "offset": end}
            before = len(events)
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                d = None
            if isinstance(d, dict):
                ts = iso_epoch(d.get("timestamp"))
                kind, payload = d.get("type"), d.get("payload") or {}
                payload = payload if isinstance(payload, dict) else {}
                if kind == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    text = _join_text_blocks(
                        (c.get("text") or c.get("input_text") or "")
                        for c in payload.get("content") or []
                        if isinstance(c, dict))
                    if text != "" and role != "developer":
                        if role == "user":
                            if (not _is_host_record(text, CODEX_WRAPPER_OPENING)
                                    and not _is_self_injected(text)):
                                events.append((ts, path, next(position),
                                              Event(ts, "you", text, sid, gen,
                                                    start, end)))
                        elif role == "assistant":
                            events.append((ts, path, next(position),
                                          Event(ts, "codex", text, sid, gen,
                                                start, end)))
                elif (kind == "event_msg"
                      and payload.get("type") == "exec_command_begin"):
                    command = payload.get("command")
                    if isinstance(command, list):
                        command = " ".join(command)
                    if command:
                        events.append((ts, path, next(position),
                                      Event(ts, "tool", f"shell {command}",
                                            sid, gen, start, end)))
            if len(events) > before:
                visible_records += 1
                if (visible_record_limit is not None
                        and visible_records >= visible_record_limit):
                    break
    events.sort(key=lambda item: (
        item[0], item[3].source, item[3].generation or "",
        item[3].offset, item[2]))
    return [item[3] for item in events], reached


# ---------- summary ----------

LABEL = {"you": "YOU", "claude": "Claude", "codex": "Codex", "tool": "·"}


# side -> (the other side's key, its display name for headings, its phrasing in notices)
OTHER_SIDE = {
    "claude": ("codex", "Codex", "from Codex"),
    "codex": ("claude", "Claude Code", "from Claude Code"),
}


def _ordered_records(events):
    """Return completed source records in a source-prefix-preserving merge."""
    grouped = {}
    for event in events:
        key = (event.source, event.generation, event.offset, event.end)
        grouped.setdefault(key, []).append(event)

    streams = {}
    for (source, generation, offset, end), record_events in grouped.items():
        record = Record(record_events[0].time, source, generation, offset, end,
                        tuple(record_events))
        streams.setdefault((source, generation), []).append(record)

    ordered_streams = []
    for stream_key in sorted(streams, key=lambda key: (key[0], key[1] or "")):
        stream = streams[stream_key]
        stream.sort(key=lambda record: (record.offset, record.end))
        ordered_streams.append(stream)

    heap = []
    for stream_number, stream in enumerate(ordered_streams):
        record = stream[0]
        heapq.heappush(heap, (record.time, record.source, record.generation or "",
                            record.offset, stream_number, 0, record))

    records = []
    while heap:
        _time, _source, _generation, _offset, stream_number, index, record = heapq.heappop(heap)
        records.append(record)
        next_index = index + 1
        stream = ordered_streams[stream_number]
        if next_index < len(stream):
            next_record = stream[next_index]
            heapq.heappush(
                heap,
                (next_record.time, next_record.source, next_record.generation or "",
                 next_record.offset, stream_number, next_index, next_record))
    return records


def _render_record(record):
    """Render one completed source record without cutting its non-tool text."""
    pieces = []
    tools = []
    run_kind = None
    run_time = None
    run_texts = []

    def flush_tools():
        if tools:
            pieces.append("  · {} tool calls: {}".format(
                len(tools), truncate(" | ".join(tools[-3:]), 130)))
            del tools[:]

    def flush_run():
        nonlocal run_kind, run_time, run_texts
        if run_kind is not None:
            clock = datetime.fromtimestamp(run_time).strftime("%H:%M")
            pieces.append("[{}] {}:\n{}".format(
                clock, LABEL.get(run_kind, run_kind), "\n\n".join(run_texts)))
            run_kind = None
            run_time = None
            run_texts = []

    for event in record.events:
        if event.kind == "tool":
            flush_run()
            tools.append(truncate(event.text, 70))
            continue
        flush_tools()
        if run_kind != event.kind:
            flush_run()
            run_kind = event.kind
            run_time = event.time
        run_texts.append(event.text)
    flush_run()
    flush_tools()
    return "\n".join(pieces), int(any(event.kind != "tool" for event in record.events))


def _append_page_section(text, section):
    """Separate envelope sections without changing a record's trailing bytes."""
    if not text:
        return section
    if text.endswith("\n"):
        return text + section
    return text + "\n" + section


def _render_page(side, records, has_more, replay_reason):
    """Render the exact visible envelope whose UTF-8 size is page-bounded."""
    other = OTHER_SIDE[side][1]
    text = "## What happened on the {} side (since your last turn)".format(other)
    text = _append_page_section(text, "has_more: {}".format(str(has_more).lower()))
    text = _append_page_section(text, "has_more_scope: currently discovered sources")
    if replay_reason is not None:
        text = _append_page_section(text, REPLAY_NOTICES[replay_reason])
    for record in records:
        rendered, _count = _render_record(record)
        text = _append_page_section(text, rendered)
    if has_more:
        if side == "codex":
            text = _append_page_section(
                text, "More remains; call antiphon_read again or continue on a later turn.")
        else:
            text = _append_page_section(text, "More remains; it will continue on a later turn.")
    return _append_page_section(
        text, "This record belongs to the Antiphon bridge — this is what actually happened "
        "there. Do not assume anything that is not in it.")


def _page_frontier(records, selected, scanned):
    """Return offsets that stop at each source's first undelivered record."""
    first_remaining = {}
    for record in records[selected:]:
        first_remaining.setdefault(record.source, record.offset)
    frontier = {}
    for source, position in scanned.items():
        offset = first_remaining.get(source, position["offset"])
        frontier[source] = dict(position, offset=offset)
    return frontier


def _build_page(events, scanned, side, replay_reason=None):
    """Build one bounded, whole-record page and its safe source frontier."""
    if replay_reason not in (None, "legacy_upgrade", "cursor_recovery"):
        raise ValueError("unknown replay reason")
    records = _ordered_records(events)
    if not records:
        if not scanned:
            return "", None, 0
        if replay_reason is None:
            return "", PageAdvance(dict(scanned), False, None), 0
        text = _render_page(side, [], False, replay_reason)
        return text, PageAdvance(dict(scanned), False, replay_reason), 0

    maximum = min(EVENT_LIMIT, len(records))
    selected = 0
    text = ""
    for length in range(1, maximum + 1):
        has_more = length < len(records)
        candidate = _render_page(side, records[:length], has_more, replay_reason)
        if len(candidate.encode("utf-8")) <= PAGE_BUDGET:
            selected = length
            text = candidate

    if selected == 0:
        selected = 1
        text = _render_page(side, records[:selected], len(records) > selected,
                            replay_reason)

    has_more = selected < len(records)
    frontier = _page_frontier(records, selected, scanned)
    count = sum(_render_record(record)[1] for record in records[:selected])
    return text, PageAdvance(frontier, has_more, replay_reason), count


def build_summary(cwd, side, positions=None, since=None, replay_reason=None):
    """`side` is the side that will READ the summary ('claude' | 'codex').
    Turns what happened on the other side, and what the user said, into
    compact text.

    Returns ``(text, page_advance, message_count)``. The page advance is the
    safe contiguous delivered prefix, plus filtered bytes before the first
    undelivered visible record; it is not the parser's scanned EOF."""
    if side == "claude":
        events, reached = codex_events(
            cwd, positions, since, visible_record_limit=EVENT_LIMIT + 1)
    else:
        events, reached = claude_events(
            cwd, positions, since, visible_record_limit=EVENT_LIMIT + 1)
    return _build_page(events, reached, side, replay_reason)


# ---------- hook (both sides share the same contract) ----------

def hook(side="claude"):
    """Injects the other side's summary into the context, and on the Codex side
    records which session is behind this alias.

    `side` is which CLI this hook is running inside ('claude' | 'codex').
    Claude Code and Codex CLI speak the same input fields (`cwd`,
    `hook_event_name`, `session_id`) and the same output wrapper, so a single
    `hook` function serves both."""
    if side not in OTHER_SIDE:
        print(f"hook: unknown side {side!r} (claude | codex)", file=sys.stderr)
        return 1
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}
    if not isinstance(input_data, dict):
        # `[]` and `"x"` are valid JSON. `.get` on either raises, and out of a
        # Stop hook that is a traceback in somebody's terminal.
        input_data = {}
    cwd = os.path.abspath(input_data.get("cwd") or project_dir())
    event = input_data.get("hook_event_name") or "UserPromptSubmit"

    if side == "codex":
        # On every event, not only `SessionStart`. A missed one then costs a
        # turn of routability rather than the whole session's.
        record_codex_session(cwd, input_data.get("session_id"),
                             input_data.get("transcript_path"))

    if event != "UserPromptSubmit":
        # Only a prompt has something for context to attach to. Anything else —
        # `SessionStart`, or an event this version has never heard of — records
        # and returns without a word, rather than emitting a wrapper naming an
        # event that did not happen. The cursor stays where it was too: a
        # summary nobody was shown has not been seen.
        return 0

    with cursor_lock(cwd, side) as locked:
        if not locked:
            # `cursor_lock` has already said why on stderr, but its message is
            # deliberately neutral about what was lost — this is the detail
            # only this caller knows. Non-zero so the person actually sees it:
            # on exit 0 that line reaches a debug log and nothing else, and a
            # bridge that stopped delivering would look exactly like a
            # counterpart with nothing to say.
            print("antiphon: context not delivered this turn", file=sys.stderr)
            return 1
        cursor, cursor_state = _read_cursor_state(cwd, side)
        positions, since, replay_reason = positions_for(
            cursor, side, cursor_state)
        text, advance, _ = build_summary(
            cwd, side, positions, since, replay_reason)
        if not text:
            # Nothing to deliver this turn, so the write-then-advance order
            # below does not protect anything -- there is no page to lose.
            # The parser's own high-water mark still has to move, or a
            # source with nothing visible in it (filtered records, or one a
            # v1 cursor just placed) is read again from scratch every turn.
            if not _advance_page_cursor(
                    cwd, side, cursor, side, positions, advance):
                print("antiphon: nothing to show, but could not record cursor "
                      "progress", file=sys.stderr)
            return 0

        # The hook prints nothing to the terminal. The counter used to say
        # "message" but it was counting the other side's transcript events;
        # incoming channel messages already show up via their own notices.
        # Context is injected silently.
        if not _deliver(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": text,
            }
        }, ensure_ascii=False)):
            # The page never left this process, so it has not been delivered
            # and the cursor stays where it was. The next turn offers it again.
            print("antiphon: could not write this turn's context", file=sys.stderr)
            return 1
        if not _advance_page_cursor(
                cwd, side, cursor, side, positions, advance):
            # The page WAS delivered, so the exit code stays 0: a non-zero
            # exit suppresses plain-text stdout as context, and whether it
            # also suppresses `additionalContext` is undocumented and
            # unmeasured. Risking the page we just handed over to report a
            # cursor failure would trade a visible repeat for a silent
            # loss. The symptom of this branch is the same context
            # arriving every turn.
            print("antiphon: delivered, but could not record the cursor",
                  file=sys.stderr)
        return 0


def notice_text(side, count):
    """The one-line notice in `status` output (the hook no longer uses this)."""
    noun = "message" if count == 1 else "messages"
    return f"💬 {count} new {noun} {OTHER_SIDE[side][2]}"


# ---------- push (both directions) ----------

def _claude_turn(transcript_path):
    """(text, boundary_uuid) for the last Claude turn in the window.

    `text` is every assistant text of that turn, joined in order — a turn can
    span several assistant records, one per model response before and after
    each tool call in between, so the newest assistant record alone is not
    the whole reply. The last turn is everything after the last `user` record
    that is a real turn boundary; see `is_boundary` for what disqualifies one.
    Claude's hook payload carries no turn id, so unlike `last_codex_reply`
    this has only the window to decide with.

    `boundary_uuid` is that boundary record's own top-level `uuid` — measured
    present on 1,220 of 1,220 sampled boundary records — or `None` when the
    window holds no boundary at all, or the boundary record itself carries
    none. `push` uses it to scope the Claude-turn side of the dedupe
    fingerprint to the turn that produced the text, so an identical
    instruction repeated in a later turn is not silently deduped against the
    one an earlier turn already sent.
    """
    records = []
    for line in tail_lines(transcript_path):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Measured `origin.kind` values for a mid-turn `isMeta` record that
    # carries neither `sourceToolUseID` nor `turnCompanion`. This is an
    # allowlist, not a denylist: an unmeasured kind, a missing `origin`, and
    # `"channel"` itself (the bridge's own injection) all stay boundaries.
    MID_TURN_ORIGIN_KINDS = {"coordinator", "task-notification"}

    def is_boundary(d):
        if d.get("type") != "user":
            return False
        if d.get("isMeta"):
            if d.get("sourceToolUseID") or d.get("turnCompanion"):
                return False
            origin = d.get("origin")
            kind = origin.get("kind") if isinstance(origin, dict) else None
            return kind not in MID_TURN_ORIGIN_KINDS
        content = (d.get("message") or {}).get("content")
        return not (isinstance(content, list) and content
                    and all(isinstance(c, dict) and c.get("type") == "tool_result"
                            for c in content))

    def assistant_texts(d):
        if d.get("type") != "assistant" or d.get("isMeta"):
            return None
        content = (d.get("message") or {}).get("content")
        texts = [c.get("text", "") for c in content or []
                if isinstance(c, dict) and c.get("type") == "text"]
        return texts if texts else None

    last_boundary = -1
    for i, d in enumerate(records):
        if is_boundary(d):
            last_boundary = i

    chunks = []
    for d in records[last_boundary + 1:]:
        texts = assistant_texts(d)
        if texts:
            chunks.extend(texts)

    boundary_uuid = None
    if last_boundary != -1:
        candidate = records[last_boundary].get("uuid")
        if isinstance(candidate, str) and candidate:
            boundary_uuid = candidate

    return "\n".join(chunks).strip(), boundary_uuid


def last_claude_reply(transcript_path):
    """Returns every assistant text of the last Claude turn, joined in order.

    A thin wrapper over `_claude_turn`, which also names the turn's boundary
    record for `push`'s dedupe scoping; see its docstring for the rule.
    """
    return _claude_turn(transcript_path)[0]


def last_codex_reply(transcript_path, turn_id=None):
    """Returns the assistant text(s) of the turn the Stop hook is reporting on.

    `turn_id` is the hook payload's own id, threaded in from `push()`; a
    missing key, `null`, `""`, or any non-string value all mean "no id" —
    the hook predates the field. A matched `task_started` bounds the turn
    exactly; anything short of a provable boundary falls open to every
    assistant text in the window instead of guessing, because a duplicate of
    an old turn's tail is recoverable and a lost `@claude` marker is not.
    """
    records = []
    for line in tail_lines(transcript_path):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    def message_texts(d):
        p = d.get("payload") or {}
        if (d.get("type") != "response_item" or p.get("type") != "message"
                or p.get("role") != "assistant"):
            return None
        texts = [c.get("text") or c.get("output_text") or c.get("input_text") or ""
                for c in p.get("content") or [] if isinstance(c, dict)]
        return texts if any(texts) else None

    def task_marker(d):
        p = d.get("payload") or {}
        if d.get("type") != "event_msg" or p.get("type") not in (
                "task_started", "task_complete"):
            return None
        return p.get("type"), p.get("turn_id")

    def all_visible_texts():
        chunks = []
        for d in records:
            texts = message_texts(d)
            if texts:
                chunks.append("\n".join(texts))
        return "\n".join(chunks).strip()

    def span_after(start_index, ending_kind=None, ending_id=None):
        chunks = []
        for d in records[start_index:]:
            marker = task_marker(d)
            if ending_kind is not None and marker == (ending_kind, ending_id):
                break
            texts = message_texts(d)
            if texts:
                chunks.append("\n".join(texts))
        return "\n".join(chunks).strip()

    if isinstance(turn_id, str) and turn_id:
        for i, d in enumerate(records):
            if task_marker(d) == ("task_started", turn_id):
                # Case 1: bounded by this turn's own close, or EOF — a
                # nested child's complete carries a different id and does
                # not end it.
                return span_after(i + 1, "task_complete", turn_id)
        # Case 2: a real id whose start already scrolled out of the window.
        # Binding to a different task_started would attribute this reply to
        # the wrong turn; clipping at a task_complete can cut current-turn
        # text sitting after a closed nested span. Return everything visible.
        return all_visible_texts()

    # Case 3: no id to match against — decided on the window alone, since
    # the reader never sees what came before or after it.
    last_start = None
    any_marker = False
    for i, d in enumerate(records):
        marker = task_marker(d)
        if marker:
            any_marker = True
            if marker[0] == "task_started":
                last_start = i
    if last_start is not None:
        # The current turn is still open while the hook runs, so nothing —
        # not even its own task_complete — clips this span.
        return span_after(last_start + 1)
    if any_marker:
        return all_visible_texts()          # an orphan task_complete: same fail-open

    # No task marker at all in the window: today's newest-message behaviour.
    chunks = []
    for d in records:
        texts = message_texts(d)
        if texts:
            chunks = texts
    return "\n".join(chunks).strip()


def codex_session_id(cwd):
    """The UUID of the newest Codex session matching cwd (None if there isn't one)."""
    for path in codex_rollout_files(cwd)[:1]:
        m = SESSION_ID.search(os.path.basename(path))
        if m:
            return m.group(1)
    return None


def push(target="codex"):
    """Stop hook: pushes explicit target lines in the latest reply to the other side.

    `target=codex` is called from Claude's Stop hook, `target=claude` from
    Codex's Stop hook. Addressing is never inferred from free text — only an
    explicit `@codex` or `@claude` marker at the start of a line triggers a
    push.
    """
    if target not in MARKER_SIDES:
        print(f"push: unknown target {target!r} (claude | codex)", file=sys.stderr)
        return 1
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}
    if not isinstance(input_data, dict):
        # `[]` and `"x"` are valid JSON. `.get` on either raises, and out of a
        # Stop hook that is a traceback in somebody's terminal.
        input_data = {}
    if input_data.get("stop_hook_active"):
        return 0                          # don't re-enter a turn we triggered ourselves
    cwd = os.path.abspath(input_data.get("cwd") or project_dir())
    transcript = input_data.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return 0

    if target == "codex":
        # One read decides both. A second, independent call to re-derive
        # `boundary_uuid` looked cheap but was not safe: the transcript can
        # grow between two reads, so the boundary the second read sees can
        # differ from the one that actually produced `reply_text` — measured,
        # a send from turn A gets recorded under turn B's key, and a later
        # turn B that genuinely repeats the same words is then silently
        # suppressed as an apparent duplicate. A single call guarantees the
        # text and the key it is scoped under always name the same turn.
        reply_text, boundary_uuid = _claude_turn(transcript)
        turn_key = boundary_uuid or ""
    else:
        # Only Codex's own Stop hook carries this id; Claude's hook has
        # nothing of the kind for `last_claude_reply` to use.
        turn_id = input_data.get("turn_id")
        turn_key = turn_id if isinstance(turn_id, str) and turn_id else ""
        reply_text = last_codex_reply(transcript, turn_id)
    batches = {}
    for recipient, messages in group_by_recipient(target, reply_text).items():
        # Reported per line, not per recipient: a batch holding one empty marker
        # and one real message is not empty, so a per-batch check would let the
        # empty line disappear without a word.
        for blank in (m for m in messages if not m.strip()):
            named = f":{recipient}" if recipient is not None else ""
            print(f"antiphon: a @{target}{named} line carried no message, "
                  "nothing sent for it", file=sys.stderr)
        said = [m for m in messages if m.strip()]
        if said:
            batches[recipient] = said
    if not batches:
        return 0

    side = sender_side(target)
    key = f"last_pushed_{target}"

    # Once for the turn, not once per recipient. Who is speaking cannot change
    # between two lines of one reply, and working it out again for each would
    # walk the process tree again for an answer that is already known.
    who = claimed_alias(cwd, side)

    def deliver(recipient, messages):
        outgoing = "\n".join(messages)
        attempt = delivery_id()       # but each attempt is its own attempt
        if target == "codex":
            ok, detail = send_to_codex(
                cwd, f"{PUSH_LABEL} {queue_label(who, attempt)} {outgoing}",
                recipient)
        else:
            ok, detail = send_to_claude(cwd, outgoing, recipient,
                                        sender_alias=who, message_id=attempt)
        named = f":{recipient}" if recipient else ""
        if ok:
            print(f"antiphon: delivered to {target.title()}{named} "
                  f"({len(outgoing)} characters)", file=sys.stderr)
        else:
            # Returning False leaves this recipient's fingerprint where it was,
            # so the line is offered again next turn instead of being recorded
            # as delivered and lost.
            print(f"antiphon: delivery failed — {detail}", file=sys.stderr)
        return ok

    # The send happens here, outside any lock — reversing an earlier ruling
    # that held it inside, on the grounds that read-check-send-record is one
    # transaction. Measurement overturned that: holding this peer's cursor
    # lock across a send that hangs was measured at a 5,008 ms hold, against a
    # concurrent reader's own patience of 2,038 ms — it gave up and delivered
    # no context at all. `_queue_codex` allows 15 s per recipient and
    # `send_to_claude` up to 1.5 s of connect patience plus a 5 s socket
    # timeout, both well past `CURSOR_LOCK_PATIENCE` (2.0 s): a waiter does not
    # wait behind a slow send, it is *guaranteed* to give up. And for the
    # default unnamed install this was never only this peer's lock —
    # `state_path` returns the one cursor file both `claude` and `codex` share
    # — so a slow push on one side was blocking the *other agent's* context
    # delivery, not merely delaying a retry of its own.
    #
    # So the dedupe decision and the send both run against a read taken
    # without the lock. A stale read can at worst send a message that was
    # already sent — a duplicate, the trade this project makes everywhere else
    # — never a drop. `deliver_batches` below sends whatever `raw_sent`
    # doesn't already recognise and mutates it in place with the fingerprints
    # of what actually went out.
    raw_sent, already = migrate_pushed(read_cursor(cwd, side).get(key),
                                       batches.get(None) or [])
    before_send = dict(raw_sent)
    if already:
        # The exact bare message already went out under the old string
        # format; nothing left to send for it, but the migration to the new
        # shape still needs recording — through the same scoped helper every
        # other fingerprint in this function goes through, so the shapes
        # cannot drift apart.
        raw_sent[""] = push_fingerprint(turn_key, batches[None])
    updated = forget_superseded(deliver_batches(batches, raw_sent, deliver, turn_key))
    # The delta: only the slots this call actually resolved — never the whole
    # map computed from the read above. Writing that map back, whole, would
    # reintroduce exactly the lost update the cursor lock exists to prevent:
    # a cursor snapshot must never be carried across the lock boundary, only
    # a fact about what this call just did.
    delivered = {slot: fingerprint for slot, fingerprint in updated.items()
                if before_send.get(slot) != fingerprint}
    if not delivered:
        return 0                      # nothing sent, nothing to record

    def mutate(cursor):
        # `cursor` is what `update_cursor` just read under the lock, moments
        # ago — the only cursor state this may build on. `delivered` is
        # applied on top of *this*, never on top of `raw_sent` above, which
        # may already be stale by the time this runs.
        fresh, _ = migrate_pushed(cursor.get(key), [])
        merged = dict(fresh)
        merged.update(delivered)
        cursor[key] = forget_superseded(merged)
        return cursor

    if not update_cursor(cwd, side, mutate):
        # The message already left this process — `deliver` above said so on
        # its own line. What failed is only the bookkeeping, so the
        # consequence is a possible duplicate next turn, never a second copy
        # of a drop that already happened: say exactly that, and let it be
        # seen. `push` injects nothing into anyone's context, so returning
        # non-zero here costs nobody the page a hook's non-zero would.
        missed = ", ".join(sorted(
            "(unaddressed)" if slot == "" else slot[1:] for slot in delivered))
        print(f"antiphon: sent to {target} but could not record delivery for "
              f"{missed} in {state_path(cwd, side)}; a duplicate send is "
              "possible next turn, not a drop", file=sys.stderr)
        return 1
    return 0


def _queue_codex(session, message):
    """Leaves a message with one Codex session via `codex queue`.

    The transport, not the decision. `send_to_codex` picks the session; this
    only carries. Keeping them apart is what lets a refusal be tested without
    ever starting a process.

    Returns: (success, detail).
    """
    try:
        result = subprocess.run(
            ["codex", "queue", "--thread", session, "--message", message],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, "codex command not found"
    except subprocess.SubprocessError as e:
        return False, f"{type(e).__name__}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "unknown error").strip()[:200]
    return True, ""


def claude_socket_path(cwd):
    """Deterministic path to this project's MCP Channel Unix socket."""
    key = hashlib.sha256(os.path.abspath(cwd).encode()).hexdigest()[:20]
    return os.path.join(os.environ.get("TMPDIR") or "/tmp",
                        f"antiphon-channel-{key}.sock")


def _legacy_target(cwd, kind):
    """Where a message went before any of this existed.

    Reached only when the registry holds nothing at all, which is the unnamed
    single-pair case. An older channel server still serving the project-wide
    socket is a working peer, and the newest rollout matching this directory is
    still the one Codex session in it; upgrading must not cut either off.
    """
    if kind == "claude":
        return claude_socket_path(cwd), ""
    session = codex_session_id(cwd)
    if not session:
        return None, "not delivered: no Codex session found in this directory"
    return session, ""


def _peer_states(live):
    """Every live peer, and whether anything could actually reach it.

    A refusal that named only the peers would leave the reader wondering which
    one to wait for. Naming the states answers that in the same breath.
    """
    return ", ".join(sorted(
        "{}: {}".format(peer.get("name") or "?",
                        "ready" if peer.get("address") is not None
                        else "waiting for its first turn")
        for peer in live))


def resolve_target(cwd, kind, alias=None):
    """Which peer a message goes to. Returns (address, detail).

    `address` is None when nothing can be delivered safely. The bridge does not
    choose between peers and does not broadcast: a choice made here is invisible
    to everyone, which is the failure the registry exists to end, and a message
    sent to three agents starts three agents on it. An agent picking a name is a
    different thing — that choice is written in its own words and can be read
    back and disagreed with.

    The count that decides a bare message is how many peers are **live**, never
    how many are ready. Readiness is not permission to guess: a second peer
    between its start and its first turn is as much a candidate as the one that
    happens to be routable already, and picking the ready one would be choosing
    by timing.

    `read_peers` returns a Claude endpoint as it stands, and a Codex endpoint
    with an address only when a session record under the *same owner key*
    supplies one, so an address of `None` means the same thing on both sides:
    live, and nothing can reach it yet.
    """
    if not peers.valid_kind(kind):
        return None, f"not delivered: unknown peer kind {kind!r} (claude | codex)"

    live = peers.read_peers(cwd, kind)
    names = ", ".join(sorted(p.get("name") or "?" for p in live))

    if alias is not None:
        if not peers.valid_name(alias):
            return None, (f"not delivered: {alias!r} is not a usable peer name"
                          + (f"; live {kind} peers: {names}" if names else ""))
        # Exact, or not at all. A near miss is a different peer.
        match = [p for p in live if p.get("name") == alias]
        if not match:
            return None, (f"not delivered: no live {kind} peer named {alias!r}"
                          + (f"; live peers: {names}" if names else ""))
        if match[0].get("address") is None:
            return None, (f"not delivered: {alias!r} is live but not yet routable "
                          "— it has not run a turn yet")
        return match[0]["address"], ""

    if not live:
        # Nothing registered at all: the unnamed single pair, exactly as it was
        # before any of this. Not provably unique either, but it is the shipped
        # behaviour of every existing install and breaking it would cost far
        # more than the guess it makes.
        return _legacy_target(cwd, kind)
    if len(live) > 1:
        return None, (f"not delivered: {len(live)} {kind} peers are live "
                      f"({_peer_states(live)}); address one by name")
    if kind == "codex":
        # One record is not one session. A Codex session registers only when it
        # was given a name, so any number of unnamed ones can be running beside
        # this one and none of them appears here — delivering to the visible one
        # would be a guess wearing a certainty. The asymmetry stops here: a
        # Claude channel server always registers, named or not, so one live
        # record on that side really is one live peer.
        return None, (f"not delivered: {_peer_states(live)} is the only "
                      "registered Codex peer, but unnamed Codex sessions are "
                      "not discoverable and cannot be ruled out — address a "
                      "peer by name")
    # Reached only for Claude, whose live records always carry a usable address:
    # the addressless shape is Codex-only and `read_peers` skips every other
    # unusable one.
    return live[0]["address"], ""


def send_to_codex(cwd, message, alias=None):
    """Sends a message to a Codex peer, chosen by `alias` or by there being one.

    Returns (ok, detail). Nothing is started when the recipient cannot be
    decided: the refusal happens before the transport is touched.
    """
    address, detail = resolve_target(cwd, "codex", alias)
    if address is None:
        return False, detail
    return _queue_codex(address, message)


# The channel server refuses anything larger. Checking here too means a sender
# is told before transport instead of halfway through it. A contract test keeps
# the two numbers equal.
MAX_CHANNEL_BYTES = 128 * 1024


# A channel that is not there *yet* looks exactly like one that is not there at
# all. Measured: Claude's MCP handshake completes 27-41ms before the socket is
# bound, and a message sent the moment the channel looked ready was refused ten
# times out of ten. The first thing a session says is precisely when that
# happens, so the sender waits briefly rather than reporting a channel that is
# about to exist as down.
NOT_LISTENING_YET = frozenset({errno.ENOENT, errno.ECONNREFUSED})
CONNECT_PATIENCE = 1.5            # seconds; a real outage still fails promptly
CONNECT_RETRY_DELAY = 0.05


def send_to_claude(cwd, text, alias=None, sender_alias=None, message_id=None):
    """Sends a Codex message to a Claude peer's MCP Channel socket.

    `sender_alias` and `message_id` travel in the payload and become the
    notification's metadata, so the receiving agent can see who spoke and
    address a reply deliberately.
    """
    request = {
        "content": text,
        "message_id": message_id or delivery_id(),
        "sender_alias": sender_alias,
    }
    payload = json.dumps(request, ensure_ascii=False).encode()
    if len(payload) > MAX_CHANNEL_BYTES:
        return False, (f"message is {len(payload)} bytes; the channel accepts at "
                       f"most {MAX_CHANNEL_BYTES}")

    deadline = time.monotonic() + CONNECT_PATIENCE
    while True:
        # Re-resolved every attempt: a named peer can register in the meantime,
        # which moves the address from the project-wide path to its own.
        address, detail = resolve_target(cwd, "claude", alias)
        if address is None:
            # `mcp.connect` finishes before channel.mjs publishes its registry
            # claim. With an explicit, valid alias the first lookup can
            # therefore miss the peer altogether, before there is even an
            # address to connect to. That absence is as transient and as
            # indistinguishable from a real outage as ENOENT below. Invalid
            # aliases and bare ambiguity are decisions, not readiness races,
            # and still fail immediately.
            if (alias is not None and peers.valid_name(alias)
                    and time.monotonic() < deadline):
                time.sleep(CONNECT_RETRY_DELAY)
                continue
            return False, detail
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(address)
        except OSError as error:
            if sock is not None:
                sock.close()
            if error.errno in NOT_LISTENING_YET and time.monotonic() < deadline:
                time.sleep(CONNECT_RETRY_DELAY)
                continue
            return False, ("Claude MCP Channel is down: "
                           f"{error.strerror or type(error).__name__}")
        break

    # Connected. Nothing past this point is retried: the bytes may already have
    # been accepted, and a second attempt would deliver the message twice.
    try:
        with sock:
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
            reply_bytes = b""
            while len(reply_bytes) < 64 * 1024:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                reply_bytes += chunk
    except OSError as error:
        return False, ("Claude MCP Channel is down: "
                       f"{error.strerror or type(error).__name__}")
    try:
        result = json.loads(reply_bytes.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, "Claude MCP Channel returned an invalid response"
    if not isinstance(result, dict):
        # Decoded, and not an answer: `[]` and `null` are valid JSON and `.get`
        # on either raises out of a path whose caller is only ever told success
        # or a reason.
        return False, "Claude MCP Channel returned an invalid response"
    if not result.get("ok"):
        return False, str(result.get("error") or "channel delivery failed")[:200]
    return True, ""


def reply(*_):
    """Sends the reply from the Claude channel reply tool to the running Codex session."""
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        input_data = {}
    if not isinstance(input_data, dict):
        # `[]` and `"x"` are valid JSON. `.get` on either raises, and out of a
        # Stop hook that is a traceback in somebody's terminal.
        input_data = {}
    text = input_data.get("text")
    if not isinstance(text, str) or not text.strip():
        print("reply: empty text", file=sys.stderr)
        return 1
    to = input_data.get("to")
    if to is not None and not isinstance(to, str):
        print("reply: to must be a string naming one live Codex peer",
              file=sys.stderr)
        return 1
    cwd = project_dir()
    text = text.strip()
    # `channel.mjs` passes the peer name it validated for itself.
    who = sender_alias(input_data.get("sender_alias"))
    label = queue_label(who, delivery_id())
    ok, detail = send_to_codex(cwd, f"{CHANNEL_LABEL} {label} {text}", to)
    if not ok:
        print(f"reply: {detail}", file=sys.stderr)
        return 1
    _record_delivery(cwd, "codex", text, to)
    return 0


def _record_delivery(cwd, target, text, alias=None):
    """Remembers what was just delivered, in the shape `push` dedupes on.

    Without it a message sent mid-turn through a channel tool arrives twice:
    once from the tool, once more when the same text ends the turn as an
    `@claude` / `@codex` line.

    Per recipient, in the same `""` / `"@alias"` scheme `deliver_batches` uses.
    This used to write a bare string over the whole record, so a delivery to
    `api` erased what `ui` had already been sent and `ui` received it again.

    A record from before that scheme is carried forward rather than dropped.
    It holds the joined text, not a digest, so it cannot be converted — a batch
    of two lines and one line joining to the same string are different things —
    and it is kept in its own form for `migrate_pushed` to compare. Dropping it
    would resend the last unaddressed message once.
    """
    side = sender_side(target)
    key = f"last_pushed_{target}"

    def mutate(cursor):
        held = cursor.get(key)
        sent = dict(held) if isinstance(held, dict) else {}
        if isinstance(held, str):
            sent[LEGACY_SLOT] = held
        sent["" if alias is None else f"@{alias}"] = batch_fingerprint([text])
        cursor[key] = forget_superseded(sent)
        return cursor

    update_cursor(cwd, side, mutate)


def register_peer(*_):
    """Records a peer on behalf of the Node channel server.

    Kept in Python so the registry has exactly one implementation; the channel
    server shells out here the way it already does for `reply`. The `pid` in the
    payload is the long-lived owner — this process exits immediately, and a
    record carrying its pid would read as dead at once.
    """
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}                     # valid JSON of the wrong shape is not a payload
    kind, name, address = data.get("kind"), data.get("name"), data.get("address")
    if kind not in ("claude", "codex") or not isinstance(address, str):
        print("register_peer: kind and address are required", file=sys.stderr)
        return 1
    # The owner key this subprocess can see is the CLI root above the channel
    # server, which is what a Stop hook in the same session computes too. It is
    # how that hook later shows the alias is genuinely this session's.
    ok, detail = peers.register(project_dir(), kind, name, address,
                                pid=data.get("pid"), owner_key=peers.owner_key())
    if not ok:
        print(f"register_peer: {detail}", file=sys.stderr)
        return 1
    return 0


def unregister_peer(*_):
    """Releases a claim the channel server made but could not honour.

    A record whose socket never came up is a lie the registry would keep
    telling: senders would be handed an address nothing serves.
    """
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    kind, name = data.get("kind"), data.get("name")
    if kind not in ("claude", "codex"):
        print("unregister_peer: kind is required", file=sys.stderr)
        return 1
    peers.unregister(project_dir(), kind, name, pid=data.get("pid"))
    return 0


# ---------- Codex MCP server ----------

def _tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


# The same sentence on both sides of the bridge. A contract test compares them,
# because two tool descriptions saying different things about one argument is
# how an agent learns a rule that is not true.
TO_DESCRIPTION = ("Alias of the peer to send to. Required whenever the recipient "
                  "cannot be shown to be the only one, because the send is then "
                  "refused rather than guessed — so pass it whenever you know "
                  "which peer you mean.")

TOOLS = [{
    "name": "antiphon_read",
    "description": ("Returns one page of what happened on the Claude Code side since "
                    "your last turn, oldest first. When the page ends with "
                    "`has_more: true`, more completed records are already waiting: call "
                    "this tool again, or let later turns drain them. `has_more: false` "
                    "covers only the transcripts discovery can currently see, not all "
                    "project history. If the next record alone is larger than an "
                    "ordinary page, this tool refuses it instead of truncating: nothing "
                    "is read or marked seen, and the next automatic prompt hook — whose "
                    "host can spill an oversized record to a file — delivers it whole. "
                    "Pages normally arrive automatically via the hook; reach for this "
                    "tool to drain a backlog or when the bridge seems quiet."),
    "inputSchema": {"type": "object", "properties": {}},
}, {
    "name": "antiphon_send",
    "description": ("Sends a message to a Claude Code peer working in this project, "
                    "without waiting for your turn to end. It arrives as your words, "
                    "attributed to you, and wakes Claude immediately — so you can hand "
                    "work over and carry on. It does not block: call `antiphon_read` "
                    "later in the same turn to pick up whatever Claude did. Name the "
                    "peer with `to` when more than one is live; with a single peer "
                    "you can leave it out."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Message for Claude"},
            "to": {"type": "string", "description": TO_DESCRIPTION},
        },
        "required": ["text"],
    },
}]


def _deliver(line):
    """Write one model-facing line to stdout and get it out of this process.

    Returns whether that succeeded. Neither host acknowledges hook output or a
    tool result, so nothing here can learn whether the model was actually shown
    the text; "delivered" means only what is locally observable — the write and
    the flush both returned. That is the whole reason a cursor is advanced
    after this and never before, and why the contract is at-least-once: a crash
    in the window redelivers a page, which both agents can see, where advancing
    first would drop it in silence.
    """
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        return False
    return True


def _mcp_result(mid, result):
    """Writes one JSON-RPC response; returns whether it left this process."""
    return _deliver(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result},
                               ensure_ascii=False))


def _send_tool(cwd, text, to=None, sender=None):
    """Delivers `text` to a Claude peer now, and reports honestly if it can't.

    A silent success would be the worst outcome: Codex would believe Claude had
    been told, and neither side would notice the message never arrived. So
    every way this can fail — an alias that is not a name, one nobody answers
    to, one that is live but not routable yet, or no alias where two peers are
    live — comes back as a tool error before any transport is opened.

    `to` is passed to the resolver exactly as given: an alias matches one peer
    or none. `sender` is the alias this server actually won at start-up, not
    what the environment asked for: a server refused its name holds nothing and
    says nothing.
    """
    if not isinstance(text, str) or not text.strip():
        return _tool_error("text must be a non-empty string")
    if to is not None and not isinstance(to, str):
        return _tool_error("to must be a string naming one live Claude peer")
    text = text.strip()
    ok, detail = send_to_claude(cwd, text, to, sender_alias=sender_alias(sender),
                                message_id=delivery_id())
    if not ok:
        return _tool_error(f"Not delivered to Claude: {detail}")
    _record_delivery(cwd, "claude", text, to)
    # Naming the peer back is what lets the sender notice it addressed the wrong
    # one. With a single peer there is nothing to distinguish, so the old
    # wording stands.
    where = f"peer {to!r}" if to else "channel"
    return {"content": [{"type": "text",
                         "text": f"Delivered to the Claude Code {where}."}]}


def register_codex_peer(cwd):
    """Registers this MCP server: alias, pid, owner key, and no address yet.

    Returns the alias it actually holds, or None. `mcp()` releases exactly what
    this returns, so a server that was refused the alias holds nothing and
    cannot delete the holder's record on its way out — that would hand the name
    to whoever asked next and take a working peer down with it.

    Three outcomes and only one of them is silent. A session with no alias is
    the unchanged single-peer case: nothing was asked for, so there is nothing
    to warn about, and no reason to walk a process tree either. A session that
    asked for an alias and cannot have one has to be told — somebody typed
    `ANTIPHON_NAME=build` and would otherwise believe `@codex:build` works while
    it silently never will.
    """
    alias = peers.explicit_name()
    if not alias:
        return None
    if not peers.valid_name(alias):
        print(f"antiphon: ANTIPHON_NAME={alias!r} is not a usable name "
              "([a-z0-9][a-z0-9_-]{0,31}); named routing is off for this session "
              "and the single unnamed peer still works.", file=sys.stderr)
        return None
    try:
        owner = peers.owner_key()
        if not owner:
            print("antiphon: named routing disabled — could not identify the "
                  f"owning Codex process, so {alias!r} cannot be addressed. The "
                  "bare single-peer fallback still works.", file=sys.stderr)
            return None
        ok, detail = peers.register(cwd, "codex", alias, None,
                                    pid=os.getpid(), owner_key=owner)
    except Exception as error:
        # Named routing is a layer over a bridge that already works without it.
        # Nothing here may cost this session its tools, so every failure is
        # caught — and every one is named in full, so a bug shows up loudly
        # instead of being swallowed as a shrug.
        print(f"antiphon: named routing disabled — the peer registry could not "
              f"be written ({type(error).__name__}: {error}). The bare "
              "single-peer fallback still works.", file=sys.stderr)
        return None
    if not ok:
        print(f"antiphon: {detail}", file=sys.stderr)
        return None
    return alias


def record_codex_session(cwd, session_id, transcript):
    """Writes the hook's half: which session is behind this alias.

    Returns whether it wrote. Silent when this session has no usable alias or
    cannot identify itself — the server already said so once at start-up, which
    is the right number of times to say it, and repeating it on every turn would
    be noise. A refusal is different: it means somebody else holds the alias
    right now, and that stays true and stays worth saying.
    """
    alias = peers.explicit_name()
    if not (peers.valid_name(alias) and session_id):
        return False
    try:
        owner = peers.owner_key()
        if not owner:
            return False
        ok, detail = peers.write_session(cwd, "codex", alias, session_id,
                                         transcript, owner)
    except Exception as error:
        print(f"antiphon: {alias!r} could not be recorded "
              f"({type(error).__name__}: {error}); it is not addressable this "
              "turn. The bare single-peer fallback still works.", file=sys.stderr)
        return False
    if not ok:
        print(f"antiphon: {detail}", file=sys.stderr)
    return ok


def mcp():
    """The MCP stdio server Codex connects to."""
    cwd = project_dir()
    alias = register_codex_peer(cwd)
    try:
        # The alias this process won, carried in rather than re-derived: the
        # environment cannot tell whether the claim succeeded.
        return _mcp_serve(cwd, alias)
    finally:
        if alias:
            try:
                # Only what this process actually claimed, and `unregister` is
                # pid-guarded on top of that. A `SIGKILL` leaves the record
                # behind, which is what pid-based pruning in `read_peers` is
                # for: the clean path releases the name at once, the fallback
                # catches the rest.
                peers.unregister(cwd, "codex", alias, pid=os.getpid())
            except OSError:
                pass


def _mcp_serve(cwd, alias=None):
    """The request loop, split out so `mcp()` reads as what it now is: a
    lifetime around it."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            # Valid JSON, and not a request. Skipping it costs this line; a
            # traceback would cost the session and every tool with it.
            continue
        method, mid = request.get("method"), request.get("id")
        if method == "initialize":
            _mcp_result(mid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "antiphon", "version": "0.3.0"},
            })
        elif method == "tools/list":
            _mcp_result(mid, {"tools": TOOLS})
        elif method == "tools/call":
            # `params` and `arguments` come off the wire, so neither is trusted
            # to be an object. `.get` on a list raises, and it would end the
            # session rather than the request.
            p = request.get("params")
            p = p if isinstance(p, dict) else {}
            name = p.get("name")
            if name == "antiphon_read":
                with cursor_lock(cwd, "codex") as locked:
                    if not locked:
                        # A JSON-RPC request must always be answered: a tool
                        # call with no response leaves the caller waiting on a
                        # request that never completes. It is an error result,
                        # not content — this tool's content is the other side's
                        # words, and a plain string here would read as some.
                        _mcp_result(mid, _tool_error(
                            "another read is in flight; nothing was read and "
                            "nothing was marked seen — try again in a moment"))
                    else:
                        cursor, cursor_state = _read_cursor_state(cwd, "codex")
                        positions, since, replay_reason = positions_for(
                            cursor, "codex", cursor_state)
                        text, advance, _ = build_summary(
                            cwd, "codex", positions, since, replay_reason)
                        oversized = (text
                                     and len(text.encode("utf-8")) > PAGE_BUDGET)
                        if oversized:
                            output = _tool_error(
                                "The next complete record is too large for a safe "
                                "antiphon_read result. Nothing was read or marked "
                                "seen; the next prompt hook will deliver it whole.")
                        else:
                            output = {"content": [{
                                "type": "text",
                                "text": text or (
                                    "Nothing new on the Claude Code side "
                                    "since your last turn."),
                            }]}
                        # Answer first, mark seen second — the same order as
                        # the hook, for the same reason: a result that was
                        # never written is a page the model never saw. That
                        # ordering protects a page that was actually
                        # selected; when there is nothing to deliver, no page
                        # depends on it, and the parser's own high-water mark
                        # still has to move, or a source with nothing visible
                        # in it is read again from scratch every turn.
                        delivered = _mcp_result(mid, output)
                        if oversized:
                            continue
                        if not text and delivered:
                            if not _advance_page_cursor(
                                    cwd, "codex", cursor, "codex", positions,
                                    advance):
                                print("antiphon: could not record progress "
                                      "in the cursor",
                                      file=sys.stderr)
                        elif delivered:
                            if not _advance_page_cursor(
                                    cwd, "codex", cursor, "codex", positions,
                                    advance):
                                # Symmetric with the hook: the page was
                                # delivered, so the tool result already went
                                # out and this stays a diagnostic rather than
                                # a second, failing response — the model
                                # would just see the same context again next
                                # turn.
                                print("antiphon: delivered, but could not "
                                      "record the cursor",
                                      file=sys.stderr)
            elif name == "antiphon_send":
                arguments = p.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                _mcp_result(mid, _send_tool(cwd, arguments.get("text"),
                                            arguments.get("to"), alias))
            else:
                _mcp_result(mid, _tool_error(f"unknown tool: {name}"))
        elif mid is not None:
            _mcp_result(mid, {})
    return 0


# ---------- setup ----------

HOOK_COMMAND = "antiphon hook {side}"
PUSH_COMMAND = "antiphon push {target}"

SECTION_HEADING = "## The Antiphon bridge"

AGENTS_RULE = ("\n## The Antiphon bridge\n\n"
               "You are working alongside Claude Code on this project. What happens on the "
               "other side is injected into your context automatically at the start of each "
               "turn — you don't need to do anything else. It arrives as one page of "
               "completed records, oldest first; a `has_more: true` line means more is "
               "already waiting, so call the `antiphon_read` tool again (or let later turns "
               "drain it) until it reports `has_more: false` — which covers only the "
               "currently discovered transcripts, not all project history. A page carrying "
               "a replay notice is re-delivering history after an upgrade or cursor "
               "recovery and can contain duplicates; it is complete when the notice "
               "disappears. If the single next record is larger than an ordinary page, "
               "`antiphon_read` refuses it instead of truncating it — nothing is read or marked seen — and the next "
               "automatic prompt hook delivers it whole. That injected context is "
               "project-wide awareness rather than mail addressed to you: it may merge "
               "activity from several project transcripts under one Claude label, so read "
               "it as what is happening nearby.\n\n"
               "When Claude wants to tell you something directly, you'll see it as a user "
               "message starting with `[Antiphon bridge] Claude:` (pushed from Claude's Stop "
               "hook) or `[Antiphon channel] Claude:` (a direct reply through the channel) — "
               "either way, these are Claude's words, not the user's. After that prefix comes "
               "`[from=<alias> id=<uuid>]`, naming which Claude peer spoke: reply to that one "
               "with `antiphon_send(to=<alias>)` or `@claude:<alias>`. A literal "
               "`from=<unnamed>` means that peer has no name and cannot be addressed back — "
               "with only one Claude peer live you can leave the recipient out entirely. The "
               "id names one delivery attempt; nothing routes replies by it.\n\n"
               "When you want to hand Claude a task directly, put `@claude` at the start of a "
               "line in your reply; only that line is sent to the Claude session as an MCP "
               "Channel event. To reach Claude without ending your turn, call the "
               "`antiphon_send` tool instead: it delivers immediately, so Claude can start "
               "working while you carry on, and `antiphon_read` picks up the answer later in "
               "the same turn.\n\n"
               "A direct send reaches one peer and is never broadcast. Write `@claude:name`, "
               "or `antiphon_send(to=name)`, whenever more than one Claude peer is live: an "
               "unaddressed send is refused rather than delivered to a guess. For the same "
               "reason every terminal in a project with more than one session per side has to "
               "be started with `ANTIPHON_NAME` set — a session without a name is live but "
               "unaddressable, and nothing can be sent back to it.\n")

CLAUDE_RULE = ("\n## The Antiphon bridge\n\n"
               "You are working alongside another agent on this project. What happens on the "
               "other side is injected into your context at the start of each turn. That "
               "injected context is project-wide awareness rather than mail addressed to you: "
               "it may merge activity from several project transcripts under one Codex label, "
               "so read it as what is happening nearby.\n\n"
               "Events that come directly from that agent are marked "
               "`<channel source=\"antiphon\" sender=\"codex\" sender_kind=\"agent\" "
               "sender_alias=\"...\">`; they "
               "are the words of the Codex agent, not of the human user. Use the "
               "`reply_to_codex` tool to answer them, passing `sender_alias` back "
               "as `to` whenever it is non-null: a bare reply is refused as soon "
               "as any named Codex peer is live, because unnamed sessions leave "
               "no registry record and cannot be ruled out. A null `sender_alias` "
               "means that peer has no name: it cannot be answered by name, and "
               "a bare reply reaches it only where nothing is registered.\n\n"
               "A reply reaches one peer and is never broadcast, and the same holds when you "
               "open the exchange: `@codex:name` at the start of a line addresses one peer, "
               "and an unaddressed line is refused rather than delivered to a guess. For the "
               "same reason every terminal in a project with more than one session per side "
               "has to be started with `ANTIPHON_NAME` set — Codex terminals above all, "
               "because an unnamed Codex session leaves no record at all, and one that exists "
               "unseen is why a bare message to Codex is refused.\n")


class ConfigFileError(Exception):
    """A config file exists but can't be read, so it must not be rewritten.

    A trailing comma, a `//` comment or a UTF-8 BOM is enough to make a
    hand-edited settings file unparseable. Overwriting it would silently throw
    away the user's permissions, env, statusLine and every other tool's hooks,
    so `setup` reports the file and leaves it exactly as it found it."""


def _read_json_object(path):
    """Reads a JSON object from `path`. A missing (or empty) file reads as {}.

    Raises ConfigFileError for anything that exists but can't be understood —
    the caller must not overwrite such a file."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}                     # the normal first install
    except OSError as e:
        raise ConfigFileError(
            f"{path} could not be read ({e.strerror or type(e).__name__}); "
            "refusing to overwrite it. Fix the file's permissions or move it "
            "aside, then run `antiphon setup` again.") from e
    except UnicodeDecodeError as e:
        raise ConfigFileError(
            f"{path} is not valid UTF-8; refusing to overwrite it. Fix the "
            "file or move it aside, then run `antiphon setup` again.") from e
    if not text.strip():
        return {}                     # an empty file has nothing to lose
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigFileError(
            f"{path} is not valid JSON ({e.msg}, line {e.lineno} column "
            f"{e.colno}); refusing to overwrite it — that would throw away "
            "everything else in the file. Fix the file (a trailing comma, a "
            "`//` comment and a byte-order mark are the usual culprits) or "
            "move it aside, then run `antiphon setup` again.") from e
    if not isinstance(data, dict):
        raise ConfigFileError(
            f"{path} holds a JSON {type(data).__name__}, not an object; "
            "refusing to overwrite it. Fix the file or move it aside, then "
            "run `antiphon setup` again.")
    return data


def _update_json(path, mutate):
    """Update existing JSON in place without clobbering it. Returns True if the file changed."""
    data = _read_json_object(path)
    if not mutate(data):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


def _legacy_commands(script, verb, arg):
    """Values/patterns for finding legacy Python hooks pinned to an absolute path."""
    exact = [f"python3 {script} {verb} {arg}", f"python3 {script} {verb}"]
    # A global npm install has no way to know where the old clone lived; also
    # recognize any legacy absolute path safely, by filename and subcommand.
    pattern = re.compile(
        rf"^python3\s+\S*antiphon\.py\s+{re.escape(verb)}"
        rf"(?:\s+{re.escape(arg)})?$"
    )
    return [*exact, pattern]


def _dedupe_hooks(hooks, command):
    """Leaves a single entry for `command`. Returns True if anything was dropped.

    Upgrading a legacy entry can land on a command that is already installed —
    or two legacy entries can upgrade to the same one. Without this collapse
    the hook is listed twice and fires twice per turn."""
    seen = False
    dropped = False
    emptied = []
    for group in hooks:
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("command") == command:
                if seen:
                    dropped = True
                    continue          # a duplicate of one we already keep
                seen = True
            kept.append(entry)
        if len(kept) == len(entries):
            continue
        group["hooks"] = kept
        if not kept:
            emptied.append(id(group))
    if emptied:
        hooks[:] = [group for group in hooks if id(group) not in emptied]
    return dropped


def _add_hook(hooks, command, legacy_commands=None, label=None):
    """Adds the command to one event's list; does nothing if it's already there.

    `hooks` is the list for a single event, so the same command installed under
    two events is two calls and stays exactly one entry under each.

    If `legacy_commands` is given, upgrade those first — otherwise, once the
    side argument gets added, the old entry would stick around and the hook
    would fire twice."""
    changed = False
    if legacy_commands:
        if isinstance(legacy_commands, (str, re.Pattern)):
            legacy_commands = [legacy_commands]
        for group in hooks:
            for entry in group.get("hooks") or []:
                current = entry.get("command", "")
                matched = any(
                    (candidate.fullmatch(current) if isinstance(candidate, re.Pattern)
                     else current == candidate)
                    for candidate in legacy_commands
                )
                if matched:
                    entry["command"] = command
                    if label:
                        entry["statusMessage"] = label
                    changed = True
    if _dedupe_hooks(hooks, command):
        changed = True
    for group in hooks:
        for entry in group.get("hooks") or []:
            if entry.get("command") == command:
                if label and entry.get("statusMessage") != label:
                    entry["statusMessage"] = label
                    changed = True
                return changed
    new_entry = {"type": "command", "command": command}
    if label:
        new_entry["statusMessage"] = label
    hooks.append({"hooks": [new_entry]})
    return True


def _update_instructions(current, rule):
    """Adds the Antiphon section, or edits it in place if it's stale.

    Just checking the heading and skipping wasn't enough: when the rule text
    changed, the old text stayed put and kept telling the agent something no
    longer true."""
    heading = SECTION_HEADING
    start = current.find(heading)
    if start == -1:
        return current + rule, "added"
    end = current.find("\n## ", start + len(heading))
    old_section = current[start:] if end == -1 else current[start:end]
    if old_section.strip() == rule.strip():
        return current, "already up to date"
    tail = "" if end == -1 else current[end:]
    return current[:start].rstrip("\n") + rule + tail, "updated"


CODEX_MCP_TABLE = "mcp_servers.antiphon"
# Matches `[table]` and `[[table]]` headers, capturing the name between them.
TOML_HEADER = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*$")


def _codex_config_block(cwd):
    """The `.codex/config.toml` entry that gives Codex the `antiphon_read` tool.

    Note `args = ["mcp"]`, not `["channel"]`: the channel server is Claude's side
    and hands out `reply_to_codex`. Pointing Codex at it would let Codex publish
    messages labelled as Claude's — the one thing this bridge exists to prevent.

    `env_vars` names a variable to forward from the parent rather than a value to
    set. Codex does not pass the parent environment through: measured on live
    processes, the Claude MCP child carried 46 variables and the Codex child 10 —
    a curated set plus whatever `env` declares. Without this line `ANTIPHON_NAME`
    never reaches `antiphon mcp` however the terminal was started, so the server
    and the hook could not agree on which peer they belong to."""
    return (f'[{CODEX_MCP_TABLE}]\n'
            'command = "antiphon"\n'
            'args = ["mcp"]\n'
            '# read-only local bridge; no need to ask on every turn\n'
            'default_tools_approval_mode = "approve"\n'
            '# forwarded, not set: the peer name comes from the terminal that\n'
            "# started this session, and Codex does not pass it down otherwise\n"
            'env_vars = ["ANTIPHON_NAME"]\n'
            f'\n[{CODEX_MCP_TABLE}.env]\n'
            f'ANTIPHON_CWD = "{cwd}"\n')


def _strip_toml_table(text, table):
    """Drops `[table]` and its sub-tables, leaving every other section intact."""
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        header = TOML_HEADER.match(line)
        if header:
            name = header.group(1)
            skipping = name == table or name.startswith(table + ".")
        if not skipping:
            kept.append(line)
    return "".join(kept)


def _update_codex_config(path, cwd):
    """Rewrites our own table in place; anything else in the file survives."""
    current = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            current = f.read()
    kept = _strip_toml_table(current, CODEX_MCP_TABLE).rstrip()
    block = _codex_config_block(cwd)
    new_text = f"{kept}\n\n{block}" if kept else block
    if new_text == current:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def setup():
    cwd = project_dir()
    script = os.path.abspath(__file__)
    failures = []

    def install(target, mutate, done, already):
        """Applies one config change and reports it.

        A file that can't be parsed is left alone and recorded as a failure;
        the rest of the installation still runs, so one broken file doesn't
        cost the user everything else. `setup` fails at the end instead."""
        if target in failures:
            return                    # already reported; two hooks share a file
        try:
            changed = _update_json(target, mutate)
        except ConfigFileError as error:
            failures.append(target)
            print(f"✗ {error}", file=sys.stderr)
            return
        print(f"{'✓' if changed else '·'} {done if changed else already}: {target}")

    # --- Claude Code side: .claude/settings.json ---
    claude_target = os.path.join(cwd, ".claude", "settings.json")
    claude_command = HOOK_COMMAND.format(side="claude")
    legacy_commands = _legacy_commands(script, "kanca", "claude")

    def claude_mutate(data):
        hooks = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
        changed = _add_hook(hooks, claude_command, legacy_commands)
        allowed = data.setdefault("permissions", {}).setdefault("allow", [])
        reply_tool = "mcp__antiphon__reply_to_codex"
        if reply_tool not in allowed:
            allowed.append(reply_tool)
            changed = True
        return changed

    install(claude_target, claude_mutate,
            "Claude hook installed", "Claude hook already installed")

    # --- Claude side: push to Codex (Stop hook) ---
    push_command = PUSH_COMMAND.format(target="codex")
    legacy_push_commands = _legacy_commands(script, "it", "codex")

    def push_mutate(data):
        hooks = data.setdefault("hooks", {}).setdefault("Stop", [])
        return _add_hook(hooks, push_command, legacy_push_commands)

    install(claude_target, push_mutate,
            "Push-to-Codex hook installed (Stop)",
            "Push-to-Codex hook already installed")

    # --- Codex side: .codex/hooks.json (same contract, same body) ---
    codex_target = os.path.join(cwd, ".codex", "hooks.json")
    codex_command = HOOK_COMMAND.format(side="codex")
    legacy_codex_commands = _legacy_commands(script, "kanca", "codex")

    def codex_mutate(data):
        hooks = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
        return _add_hook(hooks, codex_command, legacy_codex_commands,
                         label="Antiphon bridge")

    install(codex_target, codex_mutate,
            "Codex hook installed", "Codex hook already installed")

    # The Codex session id arrives at SessionStart, so the same command is
    # installed there too. Under both events is also the fallback: if
    # SessionStart is missed — an older CLI, a config predating this — the first
    # prompt records the session instead, and a peer becomes routable one turn
    # later rather than never. SessionEnd is deliberately not installed: it can
    # be delayed or missed, so nothing may depend on it.
    def codex_session_mutate(data):
        hooks = data.setdefault("hooks", {}).setdefault("SessionStart", [])
        return _add_hook(hooks, codex_command, label="Antiphon bridge")

    install(codex_target, codex_session_mutate,
            "Codex session hook installed (SessionStart)",
            "Codex session hook already installed")

    # --- Codex side: push to Claude (Stop hook) ---
    reverse_push_command = PUSH_COMMAND.format(target="claude")
    legacy_reverse_push_commands = _legacy_commands(script, "it", "claude")

    def reverse_push_mutate(data):
        hooks = data.setdefault("hooks", {}).setdefault("Stop", [])
        return _add_hook(hooks, reverse_push_command, legacy_reverse_push_commands)

    install(codex_target, reverse_push_mutate,
            "Push-to-Claude hook installed (Stop)",
            "Push-to-Claude hook already installed")

    # --- Codex side: the antiphon_read MCP tool (.codex/config.toml) ---
    codex_config = os.path.join(cwd, ".codex", "config.toml")
    written = _update_codex_config(codex_config, cwd)
    print(f"{'✓' if written else '·'} Codex MCP tool "
          f"{'registered' if written else 'already registered'}: {codex_config}")

    # --- Claude Code MCP Channel ---
    mcp_target = os.path.join(cwd, ".mcp.json")
    channel_config = {
        "command": "antiphon",
        "args": ["channel"],
        "env": {"ANTIPHON_CWD": cwd},
    }

    def mcp_mutate(data):
        servers = data.setdefault("mcpServers", {})
        if servers.get("antiphon") == channel_config:
            return False
        servers["antiphon"] = channel_config
        return True

    install(mcp_target, mcp_mutate,
            "Claude MCP Channel registered", "Claude MCP Channel already registered")

    # Claude Code may also keep .mcp.json servers in a local allowlist.
    local_target = os.path.join(cwd, ".claude", "settings.local.json")

    def local_mutate(data):
        enabled = data.setdefault("enabledMcpjsonServers", [])
        if "antiphon" in enabled:
            return False
        enabled.append("antiphon")
        return True

    install(local_target, local_mutate,
            "Claude MCP local permission updated",
            "Claude MCP local permission already up to date")

    # --- AGENTS.md rule ---
    agents = os.path.join(cwd, "AGENTS.md")
    current = ""
    if os.path.exists(agents):
        with open(agents, encoding="utf-8") as f:
            current = f.read()
    new_text, status_word = _update_instructions(current, AGENTS_RULE)
    if new_text != current:
        with open(agents, "w", encoding="utf-8") as f:
            f.write(new_text)
    print(f"{'✓' if new_text != current else '·'} AGENTS.md rule {status_word}: {agents}")

    # --- CLAUDE.md rule ---
    claude_md = os.path.join(cwd, "CLAUDE.md")
    current = ""
    if os.path.exists(claude_md):
        with open(claude_md, encoding="utf-8") as f:
            current = f.read()
    new_text, status_word = _update_instructions(current, CLAUDE_RULE)
    if new_text != current:
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(new_text)
    print(f"{'✓' if new_text != current else '·'} CLAUDE.md rule {status_word}: {claude_md}")

    print("\n— One last step: Codex hooks need a one-time security approval.")
    print("  Open `codex` in this directory; approve the hook at the 'New hook - review required' prompt.")
    print("  Approval is granted once and then persists (it asks again only if the file changes).")
    print("\n— Start Claude with the channel enabled:")
    print("  claude --dangerously-load-development-channels server:antiphon")
    print("  In the research preview, the first launch needs both a development channel and an MCP approval.")
    print("\n— More than one terminal on either side? Name every one of them:")
    print("  ANTIPHON_NAME=ui claude --dangerously-load-development-channels server:antiphon")
    print("  ANTIPHON_NAME=build codex")
    print("  An unnamed session still runs, but it cannot be addressed by name. Name the")
    print("  Codex terminals above all: an unnamed Codex session leaves no record at all,")
    print("  so once any Codex peer is named, an unaddressed message to Codex is refused")
    print("  rather than sent to a guess.")
    if failures:
        listed = "\n  ".join(failures)
        print(f"\n✗ setup did not finish. {len(failures)} file(s) were left untouched "
              f"because they could not be read:\n  {listed}\n"
              "  Fix or move them, then run `antiphon setup` again.", file=sys.stderr)
        return 1
    return 0


# ---------- status, for humans ----------

def _file_count(number):
    """`none`, `1 file`, `2 files` — the way a person would write it."""
    if not number:
        return "none"
    return f"{number} file" if number == 1 else f"{number} files"


def _live_by_kind(cwd):
    """One reading of the registry, grouped by side and sorted by name.

    One reading, because everything on screen has to describe the same moment.
    Read separately, a session that starts or stops between two reads makes the
    halves contradict each other — a live channel above an empty peer list, or
    a peer listed under a channel reported down. It also scans and prunes once
    instead of three times for one screen.

    Sorted by name rather than left in `read_peers` order. That order is by
    start time, which is right for resolution and wrong for a list a person
    reads: one that reshuffles whenever a session restarts cannot be read
    twice.
    """
    grouped = {"claude": [], "codex": []}
    for peer in peers.read_peers(cwd):
        grouped.setdefault(peer.get("kind"), []).append(peer)
    return {kind: sorted(found, key=lambda peer: peer.get("name") or "")
            for kind, found in grouped.items()}


def _peer_report(live):
    """The `Peers:` block and the addressing hints under it, as lines.

    Empty when nothing is registered, which is the unnamed single pair: there
    is nobody to choose between, so there is nothing to say.
    """
    if not (live["claude"] or live["codex"]):
        return []

    lines = ["", "Peers:"]
    for kind in ("claude", "codex"):
        for peer in live[kind]:
            # In words, and never the address itself. Whoever is reading this is
            # deciding who to address; a socket path or a rollout id answers
            # none of that, and puts both on screen and into whatever they
            # paste next.
            state = ("ready" if peer.get("address") is not None
                     else "waiting for first turn")
            lines.append(f"  {kind.title()} {peer.get('name')} — {state}")

    def addressable(kind):
        return [p.get("name") for p in live[kind]
                if peers.valid_name(p.get("name"))]

    # Readiness never narrows either list. A peer between its start and its
    # first turn is as much a candidate as one that happens to be routable
    # already, and letting readiness decide would hand routing to whichever
    # started first.
    if len(live["claude"]) > 1:
        named = ", ".join(f"@claude:{name}" for name in addressable("claude"))
        lines.append(f"  → a bare @claude line is refused; address one: {named}")
        if any(p.get("name") == peers.UNNAMED for p in live["claude"]):
            lines.append("  → one Claude peer has no name and cannot be "
                         "addressed; restart it with ANTIPHON_NAME set to "
                         "reach it while others are live")
    if live["codex"]:
        # Even one. A Codex session registers only when it was given a name, so
        # a single record cannot rule out the unnamed ones that leave none.
        named = ", ".join(f"@codex:{name}" for name in addressable("codex"))
        lines.append(f"  → a bare @codex line is refused, because unnamed Codex "
                     f"sessions leave no record; address one: {named}")
    return lines


_STATUS_SEEN_KEYS = frozenset(("claude_seen", "codex_seen"))
_STATUS_PAGE_KEYS = frozenset(("claude_pages", "codex_pages"))
_STATUS_CURSOR_KEYS = (_STATUS_SEEN_KEYS | _STATUS_PAGE_KEYS
                       | {"last_pushed_claude", "last_pushed_codex"})


def _cursor_entry(key, value):
    """How one cursor entry reads in `status`.

    Known cursor formats expose only the progress a person can act on. Unknown
    sibling entries are preserved on disk for rolling compatibility, but their
    values stay opaque here: a newer format may contain transcript paths,
    session ids or generation fingerprints that status must never print.
    """
    if key in _STATUS_SEEN_KEYS and isinstance(value, (int, float)) \
            and not isinstance(value, bool) and math.isfinite(value):
        if not value:
            return "—"
        try:
            return datetime.fromtimestamp(value).strftime("%H:%M:%S")
        except (ValueError, OverflowError, OSError):
            pass
    if key in _STATUS_SEEN_KEYS or key in _STATUS_PAGE_KEYS:
        expected = (PAGE_CURSOR_VERSION if key in _STATUS_PAGE_KEYS
                    else CURSOR_VERSION)
        if (isinstance(value, dict) and value.get("v") == expected
                and isinstance(value.get("sources"), dict)
                and all(_valid_position(entry)
                        for entry in value["sources"].values())):
            sources = value["sources"]
            offsets = sorted((entry["offset"] for entry in sources.values()),
                             reverse=True)
            noun = "source" if len(sources) == 1 else "sources"
            progress = ", ".join(str(offset) for offset in offsets) or "—"
            return truncate("%d %s, at %s"
                            % (len(sources), noun, progress), 80)
        return "invalid cursor state"
    return "opaque cursor state"


def _status_preview(text):
    """Clip only a display preview, preserving UTF-8 and delivery semantics."""
    encoded = text.encode("utf-8")
    if len(encoded) <= PAGE_BUDGET:
        return text
    marker = "\n(status preview ends here; the oversized next record remains unread)"
    marker_bytes = marker.encode("utf-8")
    prefix = encoded[:PAGE_BUDGET - len(marker_bytes)].decode(
        "utf-8", errors="ignore")
    return prefix + marker


def status():
    cwd = project_dir()
    print(f"project: {cwd}\n")
    c = claude_transcripts(cwd)
    x = codex_rollout_files(cwd)
    print(f"Claude transcripts: {_file_count(len(c))}")
    print(f"Codex rollouts:     {_file_count(len(x))}")
    # One snapshot for the channel line and the peer list both. Derived from the
    # registry when anything is registered: a named session serves its own
    # socket, so probing the project-wide path would report a working channel as
    # down. The path itself is never printed either way.
    live = _live_by_kind(cwd)
    channel = ("live" if live["claude"]
               else "live" if os.path.exists(claude_socket_path(cwd)) else "down")
    print(f"Claude channel:     {channel}")
    for line in _peer_report(live):
        print(line)
    snapshots = {}
    by_path = {}
    for side in ("claude", "codex"):
        path = state_path(cwd, side)
        if path not in by_path:
            by_path[path] = _read_cursor_state(cwd, side)
        snapshots[side] = by_path[path]
    printed = set()
    distinct = len(by_path) > 1
    for side in ("claude", "codex"):
        path = state_path(cwd, side)
        if path in printed:
            continue
        printed.add(path)
        cursor, _state = snapshots[side]
        label = side + " " if distinct else ""
        for key, value in (cursor or {}).items():
            shown_key = (key if key in _STATUS_CURSOR_KEYS
                         else "unknown cursor entry")
            print(f"cursor {label}{shown_key}: {_cursor_entry(key, value)}")
    for side in ("claude", "codex"):
        cursor, cursor_state = snapshots[side]
        positions, since, replay_reason = positions_for(
            cursor, side, cursor_state)
        text, _, count = build_summary(
            cwd, side, positions, since, replay_reason)
        print(f"\n=== what {side} would see ===")
        if count:
            print(notice_text(side, count))
        print(_status_preview(text) if text else "(nothing new)")
    return 0


def print_summary(side="claude"):
    cwd = project_dir()
    text, _, _ = build_summary(cwd, side, since=time.time() - LOOKBACK)
    print(text or "(nothing new)")
    return 0


COMMANDS = {
    "setup": setup, "status": status, "hook": hook, "summary": print_summary,
    "push": push, "reply": reply, "mcp": mcp, "register_peer": register_peer,
    "unregister_peer": unregister_peer,
    # Legacy aliases for old local installs, kept during the transition period.
    "kur": setup, "durum": status, "kanca": hook, "ozet": print_summary,
    "it": push, "yanit": reply,
}

_CO_VARARGS = 0x04                    # CO_VARARGS, without importing inspect


def _max_args(func):
    """How many positional arguments a command takes (None means any)."""
    if func.__code__.co_flags & _CO_VARARGS:
        return None
    return func.__code__.co_argcount


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    func = COMMANDS.get(command)
    if not func:
        print(__doc__)
        sys.exit(1)
    args = sys.argv[2:]
    limit = _max_args(func)
    if limit is not None and len(args) > limit:
        wanted = "no arguments" if limit == 0 else f"at most {limit} argument"
        print(f"antiphon: `{command}` takes {wanted}, got {len(args)}: "
              f"{' '.join(args)}", file=sys.stderr)
        print("Run `antiphon` with no arguments to see the usage.", file=sys.stderr)
        sys.exit(2)
    sys.exit(func(*args) or 0)
