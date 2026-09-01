#!/usr/bin/env python3
"""Antiphon — an open-identity bridge between Claude Code and the Codex CLI.

Two terminals, two separate agents: what you tell one, the other finds out.

Usage:
  antiphon setup               # installs the hook on both sides
  antiphon status              # shows what's happening on both sides (for humans)
  antiphon doctor              # read-only checkup: why is the bridge quiet?
  antiphon doctor --fix        # writes project configuration only, then re-checks
  antiphon summary [side]      # the text that side would see (claude | codex)
  antiphon hook <side>         # prompt and session hook (reads JSON from stdin)
  antiphon push <target>       # Stop hook: pushes `@codex` / `@claude` lines
  antiphon reply               # sends a Claude Channel reply to Codex (stdin JSON)
  antiphon channel             # long-lived Node.js MCP Channel server (started by Claude Code)
  antiphon mcp                 # MCP stdio server for Codex (fallback path)
  antiphon catch-up [side]     # skip undelivered history: page cursors jump to the live edge
  antiphon sources scan        # finish or refresh the durable source catalog
  antiphon sources compact     # retire aged gone sources proved safe by every reader
  antiphon retrieve <id>       # print one complete tool invocation (never its result)
  antiphon --version           # the installed version (also -V, version)

Design: NO SHARED MESSAGE LOG IS KEPT. Both CLIs already write transcripts;
Antiphon reads them as the content authority. Project-local metadata tracks
page cursors, routable peers and a catalog of transcript candidates that stays
monotone until cursor-aware retirement;
it never copies transcript content.

Both sides are symmetric: Claude Code and Codex CLI speak the same hook
contract (the same input fields, the same output wrapper), so a single `hook`
function serves both. Only `UserPromptSubmit` injects context; Codex also runs
it at `SessionStart`, where the session id arrives and nothing is injected.

The pull and hook layer uses the Python standard library; the Claude Channel
server runs on Node.js with the official MCP SDK.
"""

import base64
import glob
import collections
import selectors
import signal as signal_module
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
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import peers
from datetime import datetime

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
# Measured on Codex 0.151.0: a thread holds an exclusive flock on its file here
# from the moment it opens — two hours before its rollout existed, in the case
# that was measured — and the file is removed when the thread closes.
CODEX_THREAD_LOCKS = os.path.join(HOME, ".codex", "thread-writer-locks")
# Codex's own queue: `codex queue` writes here and a running thread drains it.
# Read by doctor only, read-only, to name messages waiting for a thread that
# is no longer running.
CODEX_QUEUE_DBS = os.path.join(HOME, ".codex", "queue_*.sqlite")

TAIL_BYTES = 300_000      # amount to read from the tail of each transcript file
EVENT_LIMIT = 40          # completed source records per page
PAGE_BUDGET = 8_000       # UTF-8 bytes in an ordinary complete page envelope
RECENT_FILES = 3          # bounded fallback/current-window discovery per side
LOOKBACK = 6 * 3600       # anything older than this doesn't count as part of "this session"
CATALOG_VERSION = 1
CATALOG_BATCH = 8
ANCHOR_HASH_CHUNK = 64 * 1024
CATALOG_MANIFEST_PATTERN = re.compile(
    r"[0-9a-f]{32}-(?:claude|codex)-(?:base|delta)\.json")

# EVENT_LIMIT and PAGE_BUDGET bound a complete page. The catalog is the
# correctness inventory; RECENT_FILES bounds only degraded fallback and cheap
# refresh detection. CATALOG_BATCH bounds transcript inspection per hook.

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
MARKER_BLOCK = re.compile(r"^<<(?P<token>[A-Z][A-Z0-9_]{0,31})$")


class MarkerSyntaxError(ValueError):
    """A bounded Stop-marker refusal that carries no sender-authored body.

    The only value retained from an unclosed block is its grammar-limited
    token. An invalid delimiter may contain arbitrary text, so not even the
    attempted token is kept on the exception a caller will print.
    """

    def __init__(self, reason, token=None):
        self.reason = reason
        self.token = token if reason == "unclosed" else None
        if self.token is None:
            message = "invalid multi-line marker delimiter"
        else:
            message = f"unclosed multi-line marker token {self.token}"
        super().__init__(message)


def _marker_line(target, line):
    """Decode one physical line with the pre-block marker grammar."""
    match = PUSH_MARKERS.match(line)
    if not match or match.group("side") != target:
        return None
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
    return alias, rest.rstrip()


def _marker_physical_lines(text):
    """Physical lines with their exact LF/CRLF terminators preserved."""
    start = 0
    while start < len(text):
        end = text.find("\n", start)
        if end < 0:
            yield text[start:], text[start:]
            return
        piece = text[start:end + 1]
        content = piece[:-2] if piece.endswith("\r\n") else piece[:-1]
        yield piece, content
        start = end + 1


def _marker_records(target, text):
    """Parse one-line and explicit delimited marker records atomically."""
    lines = list(_marker_physical_lines(text or ""))
    found, index = [], 0
    while index < len(lines):
        _piece, content = lines[index]
        marker = _marker_line(target, content)
        if marker is None:
            index += 1
            continue
        alias, message = marker
        if not message.startswith("<<"):
            found.append((alias, message))
            index += 1
            continue
        opener = MARKER_BLOCK.fullmatch(message)
        if opener is None:
            raise MarkerSyntaxError("invalid-delimiter")
        token = opener.group("token")
        body, closing = [], index + 1
        while closing < len(lines) and lines[closing][1] != token:
            body.append(lines[closing][0])
            closing += 1
        if closing == len(lines):
            raise MarkerSyntaxError("unclosed", token)
        found.append((alias, "".join(body)))
        index = closing + 1
    return found


def parse_markers(target, text):
    """[(alias, message)] for every marker addressed at `target`.

    `alias` is None when no name was claimed, `""` when one was claimed and is
    empty, and the raw string otherwise. `message` may be empty. Nothing is
    filtered here: whether a name exists is routing's decision, and a line the
    human wrote and the bridge swallowed without a word is the thing to avoid.
    An explicit `<<TOKEN` message consumes a delimited body atomically; invalid
    or unclosed block syntax raises MarkerSyntaxError before anything is sent.
    """
    return _marker_records(target, text)


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

# Where a mid-turn tool delivery's digest waits for the Stop hook that consumes
# it. A `\0` sibling for the same reason `LEGACY_SLOT` is one, and nested
# `{slot: digest}` rather than flat so the per-recipient separation the live
# slots pay for survives the move. It is not a recipient slot: `push` compares
# a batch against it, records the batch under its own current-shape
# fingerprint, and deletes the pair — a digest here has one Stop to be
# matched by, not an unbounded life in the map the next turn is compared
# against.
MID_TURN_SLOT = "\0midturn"


def parked_deliveries(record):
    """The mid-turn park inside one `last_pushed_*` value, as a plain copy.

    Anything else — a pre-digest string cursor, a hand-edited shape, no park
    at all — is no park. A copy, because the caller compares it against the
    cursor it re-reads under the lock much later, and the read it came from is
    stale by then.
    """
    if isinstance(record, dict):
        park = record.get(MID_TURN_SLOT)
        if isinstance(park, dict):
            return dict(park)
    return {}


def drop_parked(record, observed):
    """Deletes exactly the parked pairs in `observed` from `record`, in place.

    Compare-and-clear, never a blind pop: `push` reads, sends outside the lock
    for up to 15 s, and only then retires. A `reply_to_codex` issued inside
    that window parks a pair this run never saw, and deleting it would send
    that message's own Stop echo a second time.

    The key goes with its last pair. An empty park is *absent*, never `{}`:
    a lingering empty sibling is one more slot every later assertion about
    "which recipients this record holds" has to know about.
    """
    park = record.get(MID_TURN_SLOT)
    if not isinstance(park, dict):
        return
    for slot, digest in observed.items():
        if park.get(slot) == digest:
            park.pop(slot)
    if not park:
        record.pop(MID_TURN_SLOT, None)


PARK_LEFT_BEHIND = ("antiphon: a mid-turn record was left behind; an identical "
                    "marker may be suppressed one extra turn")


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
# The sets are per side because the tags are: `recommended_plugins` and
# `realtime_delegation` are Codex's, and `ide_opened_file` — Claude's in the
# first census — was measured on the Codex side too on 2026-08-31 (4 records,
# the host's own "The user opened the file … in the IDE" bookkeeping).
# Shared, one side's tag would silence the other side's user for typing that
# text.
# `image` belongs in neither: it is a person's attachment.

# Strictly what was measured on that side, and nothing else. A tag missing
# here costs one stray host line in a summary — visible, and fixed by adding
# it. A tag here that a person could type costs that person's whole message,
# silently. Adding a plausible sibling by symmetry is how an unmeasured tag
# once reached a set.
# Measured on 2026-08-30 over 1,575 Claude and 445 Codex `role: user`
# records; re-measured on 2026-08-31 over 4,309 and 872 (one change:
# `ide_opened_file` joined the Codex set on direct evidence); re-measured
# again on 2026-08-31 before 0.3.2 over 991 Claude text blocks in 86 files
# and 1,060 Codex in 134 (no `<` opening outside either set; no change);
# re-measured on 2026-09-01 with the production eligibility rules mirrored by
# `test/host_wrapper_census.py`: 1,181 Claude user messages in 508 files and
# 1,156 Codex user messages in 154 files. `channel` and
# `local-command-caveat` occurred only on Claude records already excluded by
# `isMeta`, so they were removed: leaving them here could silently discard a
# person's pasted text. If a future non-meta host record uses either tag, one
# visible host line may leak; `_is_self_injected` is not a second guard for
# them. The seven remaining Claude tags and all eleven Codex tags matched.
# See BACKLOG.md for the repeatable release check.
CLAUDE_HOST_WRAPPERS = (
    "task-notification", "ide_opened_file",
    "command-name", "command-message",
    "local-command-stdout",
    "bash-input", "bash-stdout",
)

CODEX_HOST_WRAPPERS = (
    "task-notification", "recommended_plugins", "realtime_delegation",
    "subagent_notification", "environment_context", "ide_opened_file",
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
    its candidate by whatever actually established it — a Codex registry
    claim, or the Claude channel's validated configured identity — and reaching
    for `ANTIPHON_NAME` here would put back the assumption the callers exist to
    replace.
    """
    return candidate if peers.valid_name(candidate) else None


def automatic_verdict(cwd, kind, peer, proof):
    """What an automatic Claude record is right now, or None if ungoverned.

    `READY`, `UNREADY`, `UNKNOWN`, `PROVED_STALE`, `STRUCTURAL_INVALID` — and
    `None` for every record this contract does not govern: any Codex path, and
    any explicit or legacy Claude peer. That scoping is structural rather than
    a fact about which caller happens to reach here today, because `register`
    is shared and a reader told "automatic endpoint" could otherwise require a
    proof of a peer that never had one.

    A bool could not drive self-retirement. The same false would cover a
    listener whose proof has not been written yet — bootstrap, or a transient
    read error — and a listener whose proof now names another session. The
    first must fail closed without destroying anything, because the next hook
    is about to make it ready; only the second is the stale identity worth
    retiring. So the answers stay apart, and only `PROVED_STALE` authorises
    anything destructive.
    """
    if kind != "claude":
        return None
    digest = peers._identity_digest_of(peer)
    if digest is None or peer.get("automatic") is not True:
        return None
    state, record = proof
    if state == "absent":
        return "UNREADY"
    if state == "unreadable":
        return "UNKNOWN"
    if state != "valid":
        return "STRUCTURAL_INVALID"
    bound = peers._session_address(cwd, peer)
    if bound is None:
        # Withdrawn and never joined are two different facts that both leave no
        # half behind, and they call for opposite actions: one listener must
        # wait for its first hook, the other must retire. Without the tombstone
        # the rotation's own withdrawal made every outgrown peer read UNREADY,
        # so the self-retirement this contract guarantees never happened.
        if peers.retired_half(cwd, "claude", peer.get("name"),
                              peer.get("owner"), digest,
                              record.get("session_id")):
            return "PROVED_STALE"
        return "UNREADY"
    if (record.get("session_id") == bound
            and record.get("identity_digest") == digest):
        return "READY"
    return "PROVED_STALE"


# The remedy, in one place. There is no dynamic rename of a live listener: a
# process serving under one identity must not silently become another. So after
# a session rotates A→B its terminal is unreachable by automatic identity until
# a fresh endpoint exists, which in practice means an MCP reconnect. That cost
# is accepted deliberately, and every surface says it with the same words.
RECONNECT_REMEDY = "reconnect that Claude session to be reachable"

ReconnectWindow = collections.namedtuple(
    "ReconnectWindow", "aliases counted completeness")


def reconnect_window(cwd, served=()):
    """Current automatic Claude identities that have no channel yet.

    The proof deliberately outlives endpoints, and this is the reason: in the
    window after a rotation the current identity owns no peer record at all, so
    a lookup finds nothing and only the read-only inventory can answer. What
    comes back is the public alias and a count — never a host session id, an
    identity digest, an owner key or a route.

    Liveness governs rendering, and only positively. An owner whose key belongs
    to a legacy or future generation has no reproducible fingerprint here, so it
    reads `unknown`, and unknown is not live: it is counted, never rendered as
    something a message could be addressed to, and never rewritten into the
    current generation to make it renderable. The same rule as everywhere else
    on this bridge — positive proof or nothing.

    Reading mutates nothing.
    """
    inventory = peers.identity_proofs(cwd)
    served = set(served)
    cache = {}
    aliases, counted = [], 0
    for proof in inventory.proofs:
        if peers._owner_liveness(proof.get("owner_key"), cache) != "live":
            counted += 1
            continue
        alias = peers.auto_name_from_digest(proof.get("identity_digest"))
        if alias is None:
            counted += 1
            continue
        # Already serving: this owner reconnected, and there is nothing to say.
        if alias in served:
            continue
        aliases.append(alias)
    return ReconnectWindow(tuple(sorted(set(aliases))), counted,
                           inventory.completeness)


def _reconnect_lines(window):
    """What `status` and `doctor` both say about that window, in one voice.

    Both are notes rather than faults. A terminal waiting for its reconnect is
    a state, not a breakage, and an owner that cannot be proved live is evidence
    of nothing — a `✗` on either would be one people learn to ignore.
    """
    lines = []
    for alias in window.aliases:
        lines.append(f"claude {alias}: current automatic identity with no "
                     f"channel yet — {RECONNECT_REMEDY}")
    if window.counted:
        noun = "identity" if window.counted == 1 else "identities"
        lines.append(f"identity proofs: {window.counted} automatic Claude "
                     f"{noun} could not be proved live; counted, never "
                     "addressed")
    if window.completeness == "lower-bound":
        lines.append("identity proofs: some records could not be read, so the "
                     "identities above are a lower bound, never a complete list")
    elif window.completeness == "unknown":
        lines.append("identity proofs: unreadable, so the number of automatic "
                     "Claude identities is unknown rather than zero")
    return lines


def _automatic_ready(cwd, kind, peer):
    """Whether this record may be used. Ungoverned records always may.

    One place asks the question, so the resolver, `status`, `doctor` and the
    Stop-signing identity cannot disagree about the same moment.
    """
    verdict = automatic_verdict(
        cwd, kind, peer, peers.read_identity_proof(cwd, peer.get("owner")))
    return verdict is None or verdict == "READY"


def claimed_alias(cwd, kind, session_id=None):
    """The identity this side may publish on a Stop-hook message.

    A valid Claude `ANTIPHON_NAME` is its configured identity even when this
    process did not win that name's return channel. Without that override, an
    automatic alias is published only after the channel endpoint and hook
    session join on owner and digest. A failed join remains unnamed. Channel
    loss makes an explicit identity unreachable, not unnamed; startup and
    doctor expose that broken return path separately.

    Codex is different: its server is not the session and an explicit
    environment name is only a request. It publishes that alias only when the
    live record belongs to this session, matched on the owner key. Without an
    override, its hook-owned observation must have positive writer-lock proof
    before the derived alias is published. Anything unproved yields None;
    otherwise a reply could be routed to a Codex session that never spoke.
    """
    requested = peers.explicit_name()
    alias = sender_alias(requested)
    if requested:
        if not alias:
            return None               # an invalid override never falls through
        if kind == "claude":
            return alias
        owner = peers.owner_key()
        if not owner:
            return None
        for peer in peers.read_peers(cwd, kind):
            if peer.get("name") == alias:
                return alias if peer.get("owner") == owner else None
        return None
    if kind == "codex" and peers.valid_session_id(session_id):
        snapshot = _codex_identity_snapshot(
            cwd, peers.read_peers(cwd, "codex"))
        for peer in snapshot.automatic:
            if peer.get("address") == session_id:
                return peer.get("name")
    if kind == "claude" and peers.valid_session_id(session_id):
        name, digest = peers.auto_identity(session_id)
        owner = peers.owner_key()
        if not owner:
            return None
        for peer in peers.read_peers(cwd, "claude"):
            if (peer.get("name") == name
                    and peer.get("owner") == owner
                    and peers._identity_digest_of(peer) == digest
                    and peers._session_address(cwd, peer) == session_id
                    and _automatic_ready(cwd, "claude", peer)):
                return name
    return None


def reply_available(cwd, kind, alias):
    """Whether a reply to this published alias returns to the same session.

    Only Claude separates configured identity from channel ownership. Codex
    publishes no alias until it owns the registry claim, and an unnamed sender
    carries no named return route to qualify. For Claude, the same owner-key
    join that used to suppress its identity now answers only reachability.
    """
    if kind != "claude" or not alias:
        return True
    owner = peers.owner_key()
    if not owner:
        return False
    for peer in peers.read_peers(cwd, kind):
        if peer.get("name") == alias:
            return peer.get("owner") == owner
    return False


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
NO_RETURN = "<unavailable>"


def queue_label(alias, message_id, reply_available=True):
    """`[from=<alias> id=<uuid>]` for the paths that carry only text.

    `codex queue` takes a message and no metadata, so what the socket puts in
    `meta` has to be visible here. It goes **after** the bridge or channel
    prefix, never before: those prefixes anchor the self-injection filter and
    the echo guard, and a message that no longer starts with one would be read
    back as new traffic and delivered again.

    `alias` is already validated, so it cannot close this bracket and open
    another. A configured Claude identity that does not own its return channel
    stays visible as `from`, while the deliberately invalid `reply_to` token
    makes even a literal-minded reply fail closed instead of reaching whichever
    other process owns that name.
    """
    route = f" reply_to={NO_RETURN}" if alias and not reply_available else ""
    return f"[from={alias or NO_ALIAS}{route} id={message_id}]"


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
CATALOG_LOCK_PATIENCE = 2.0       # same turn-safety boundary as delivery

_PROJECT_LOCK_STATE = threading.local()


def _refuse_nested_project_lock(kind):
    """Fail before catalog/cursor nesting can create an AB/BA deadlock."""
    held = getattr(_PROJECT_LOCK_STATE, "kind", None)
    if held is not None and held != kind:
        raise RuntimeError(
            f"nested project locks are forbidden: holding {held}, asked for {kind}")


def _mark_project_lock(kind):
    _PROJECT_LOCK_STATE.kind = kind


def _unmark_project_lock(kind):
    if getattr(_PROJECT_LOCK_STATE, "kind", None) == kind:
        del _PROJECT_LOCK_STATE.kind


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
    _refuse_nested_project_lock("cursor")
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
        if held:
            _mark_project_lock("cursor")
        yield held
    finally:
        if held:
            _unmark_project_lock("cursor")
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def catalog_lock(cwd, patience=None, shared=False):
    """Take a shared snapshot or exclusive mutation catalog lock, bounded."""
    _refuse_nested_project_lock("catalog")
    if patience is None:
        patience = CATALOG_LOCK_PATIENCE
    path = os.path.join(cwd, ".antiphon", "sources", ".lock")
    try:
        if shared:
            fd = os.open(path, os.O_RDONLY)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except FileNotFoundError as exc:
        if shared:
            yield False
            return
        detail = exc.strerror or "operating system error"
        print(f"antiphon: source catalog lock could not be opened: {detail}",
              file=sys.stderr)
        yield False
        return
    except OSError as exc:
        detail = exc.strerror or "operating system error"
        print(f"antiphon: source catalog lock could not be opened: {detail}",
              file=sys.stderr)
        yield False
        return
    held = False
    deadline = time.monotonic() + patience
    try:
        while True:
            try:
                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print("antiphon: source catalog is still busy after "
                          f"{patience:g}s; catalog work was skipped",
                          file=sys.stderr)
                    break
                time.sleep(CURSOR_LOCK_RETRY_DELAY)
            except OSError as exc:
                detail = exc.strerror or "operating system error"
                print(f"antiphon: source catalog cannot be locked: {detail}",
                      file=sys.stderr)
                break
        if held:
            _mark_project_lock("catalog")
        yield held
    finally:
        if held:
            _unmark_project_lock("catalog")
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


CatalogPhaseResult = collections.namedtuple(
    "CatalogPhaseResult", "ok reason value")


def _catalog_phase(cwd, operation):
    """Run one bounded catalog mutation phase, classified for hook callers."""
    with catalog_lock(cwd) as locked:
        if not locked:
            return CatalogPhaseResult(False, "lock-contention", None)
        try:
            if not _recover_prepared_compactions_locked(cwd):
                return CatalogPhaseResult(
                    False, "compaction-recovery-pending", None)
            return CatalogPhaseResult(True, None, operation())
        except OSError as exc:
            detail = exc.strerror or "operating system error"
            print(f"antiphon: source catalog work failed: {detail}",
                  file=sys.stderr)
            return CatalogPhaseResult(False, "catalog-error", None)


def _hook_catalog_update(cwd, side, payload):
    """Record the live source first, then advance one bounded batch per host."""
    transcript = payload.get("transcript_path") if isinstance(payload, dict) else None
    if isinstance(transcript, str) and transcript:
        current = _record_current_source(cwd, side, transcript)
        if not current.ok:
            print(f"antiphon: current {side} transcript was not catalogued "
                  f"({current.reason}); discovery is incomplete", file=sys.stderr)
            return False
    for kind in (side, OTHER_SIDE[side][0]):
        progress = _catalog_scan_step(cwd, kind)
        if progress.state == "degraded":
            print(f"antiphon: {kind} source catalog is degraded; recent "
                  "discovery remains available", file=sys.stderr)
            return False
    return True


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
V3_PAGE_CURSOR_VERSION = 3
PAGE_CURSOR_VERSION = V3_PAGE_CURSOR_VERSION
ANCHORED_PAGE_CURSOR_VERSION = 4


class RuntimePositions(dict):
    """Dict-compatible cursor view with non-persisted adoption provenance."""

    def __init__(self, values=None, adopting=(), next_lane="active"):
        super().__init__(values or {})
        self.adopting = frozenset(adopting)
        self.next_lane = next_lane if next_lane in ("active", "dead") else "active"


def page_cursor_key(side):
    return "%s_pages" % side


def anchored_page_cursor_key(side):
    return "%s_pages_v4" % side


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


def _valid_anchor(anchor, offset):
    if offset == 0:
        return anchor is None
    return (isinstance(anchor, dict)
            and set(anchor) == {"start", "sha256"}
            and isinstance(anchor.get("start"), int)
            and not isinstance(anchor.get("start"), bool)
            and 0 <= anchor["start"] < offset
            and isinstance(anchor.get("sha256"), str)
            and len(anchor["sha256"]) == 64
            and all(char in "0123456789abcdef"
                    for char in anchor["sha256"]))


def _valid_anchored_position(entry):
    return (_valid_position(entry)
            and set(entry) == {"gen", "offset", "anchor"}
            and _valid_anchor(entry.get("anchor"), entry["offset"]))


def _valid_v4_page_cursor(value):
    if (not isinstance(value, dict)
            or value.get("v") != ANCHORED_PAGE_CURSOR_VERSION
            or not isinstance(value.get("sources"), dict)
            or not isinstance(value.get("adopting_v3"), dict)
            or value.get("next_lane") not in ("active", "dead")
            or any(not isinstance(sid, str) or not sid
                   for sid in set(value["sources"]).union(value["adopting_v3"]))
            or set(value["sources"]).intersection(value["adopting_v3"])
            or not all(_valid_anchored_position(entry)
                       for entry in value["sources"].values())
            or not all(_valid_position(entry)
                       for entry in value["adopting_v3"].values())):
        return False
    allowed = {"v", "sources", "adopting_v3", "next_lane", "replay"}
    if set(value) - allowed:
        return False
    replay = value.get("replay")
    return replay is None or replay in REPLAY_NOTICES


def positions_for(cursor, side, loader_state="valid"):
    """Return ``(positions, since, replay_reason)`` for one paging reader.

    A v2 value is deliberately never reinterpreted as a delivered page
    frontier. Its presence requests a bounded byte-zero replay under the
    separate v3 key, which keeps an overlapping old process from advancing a
    new reader past content it did not deliver.
    """
    if loader_state == "invalid":
        return RuntimePositions(), None, "cursor_recovery"
    cursor = cursor if isinstance(cursor, dict) else {}
    key = page_cursor_key(side)
    v4_key = anchored_page_cursor_key(side)
    legacy_key = "%s_seen" % side
    since = time.time() - LOOKBACK
    if v4_key in cursor:
        value = cursor.get(v4_key)
        if _valid_v4_page_cursor(value):
            sources = json.loads(json.dumps(value["sources"]))
            sources.update(json.loads(json.dumps(value["adopting_v3"])))
            return RuntimePositions(
                sources, adopting=value["adopting_v3"],
                next_lane=value["next_lane"]), since, value.get("replay")
        print("antiphon: anchored paging cursor state was invalid; restarting "
              "discovered transcript history", file=sys.stderr)
        return RuntimePositions(), None, "cursor_recovery"
    if key in cursor:
        value = cursor.get(key)
        if (isinstance(value, dict)
                and value.get("v") == V3_PAGE_CURSOR_VERSION
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
                return RuntimePositions(
                    sources, adopting=sources), since, replay
        print("antiphon: paging cursor state was invalid; restarting discovered "
              "transcript history", file=sys.stderr)
        return RuntimePositions(), None, "cursor_recovery"
    if legacy_key in cursor:
        legacy = cursor.get(legacy_key)
        if isinstance(legacy, dict):
            # The v2 map (never published; 0.2.x lived only on dev machines)
            # records how far the old scanner *read*, not what it delivered —
            # it scanned the whole suffix and rendered the newest EVENT_LIMIT
            # — so no offset in it is a safe start. Byte zero, marked.
            return RuntimePositions(), None, "legacy_upgrade"
        # 0.1.0 — the published upgrade path — kept one epoch float: the time
        # of the last event it rendered. That time is taken as authoritative
        # and the page starts at the first record at or after it (`>=`, so
        # the cohort sharing that second repeats). What 0.1.0's own trim cut
        # before then stays cut: it had already declared that delivered, and
        # byte zero here cost the maintainer twenty hours of replay. A value
        # that is not a time trusts nothing and replays from byte zero.
        unset = object()
        moment = cursor_time(cursor, legacy_key, default=unset)
        return (RuntimePositions(),
                (None if moment is unset else moment), "legacy_upgrade")
    return RuntimePositions(), since, None


def _advance_page_cursor(cwd, kind, cursor, side, positions, advance):
    """Persist the delivered source prefix and replay lifecycle as one value."""
    if advance is None:
        return True
    key = anchored_page_cursor_key(side)
    held = cursor.get(key)
    if _valid_v4_page_cursor(held):
        sources = json.loads(json.dumps(held["sources"]))
        adopting = json.loads(json.dumps(held["adopting_v3"]))
        next_lane = held["next_lane"]
    else:
        sources, adopting, next_lane = {}, {}, "active"

    def merge(entries):
        for sid, raw in entries.items():
            entry = dict(raw)
            if _valid_anchored_position(entry):
                sources[sid] = entry
                adopting.pop(sid, None)
            elif _valid_position(entry):
                adopting[sid] = {
                    "gen": entry["gen"], "offset": entry["offset"]}
                sources.pop(sid, None)

    merge(positions)
    merge(advance.sources)
    if advance.next_lane in ("active", "dead"):
        next_lane = advance.next_lane
    value = {"v": ANCHORED_PAGE_CURSOR_VERSION, "sources": sources,
             "adopting_v3": adopting, "next_lane": next_lane}
    if advance.has_more and advance.replay_reason in REPLAY_NOTICES:
        value["replay"] = advance.replay_reason
    cursor[key] = value
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


def _anchor_from_stream(stream, offset):
    """Anchor the one complete raw record ending at ``offset``.

    Reads backward and hashes forward in fixed chunks. ``None`` means the
    requested byte is not a proved record boundary in this stream.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    if offset == 0:
        return None
    size = stream.seek(0, os.SEEK_END)
    if offset > size:
        return None
    stream.seek(offset - 1)
    if stream.read(1) != b"\n":
        return None
    position = offset - 1
    start = 0
    while position > 0:
        chunk_start = max(0, position - ANCHOR_HASH_CHUNK)
        stream.seek(chunk_start)
        block = stream.read(position - chunk_start)
        newline = block.rfind(b"\n")
        if newline >= 0:
            start = chunk_start + newline + 1
            break
        position = chunk_start
    digest = hashlib.sha256()
    remaining = offset - start
    stream.seek(start)
    while remaining:
        block = stream.read(min(ANCHOR_HASH_CHUNK, remaining))
        if not block:
            return None
        digest.update(block)
        remaining -= len(block)
    return {"start": start, "sha256": digest.hexdigest()}


def _retrieval_records_from_stream(stream):
    """Yield the raw bytes of every record in one captured complete prefix.

    Retrieval has a stronger trust contract than passive paging: a read fault
    must poison the answer instead of looking like an ordinary end of file.
    Capture the last newline-terminated frontier first, then prove that every
    byte up to it was traversed. A concurrently appended suffix and an
    unterminated tail are deliberately outside that snapshot.
    """
    size = os.fstat(stream.fileno()).st_size
    frontier = 0
    if size:
        stream.seek(size - 1)
        last = stream.read(1)
        if len(last) != 1:
            raise OSError(errno.EIO, "short read at retrieval frontier")
        if last == b"\n":
            frontier = size
        else:
            position = size - 1
            while position > 0:
                start = max(0, position - 65536)
                stream.seek(start)
                expected = position - start
                block = stream.read(expected)
                if len(block) != expected:
                    raise OSError(errno.EIO,
                                  "short read while finding retrieval frontier")
                newline = block.rfind(b"\n")
                if newline >= 0:
                    frontier = start + newline + 1
                    break
                position = start

    stream.seek(0)
    position = 0
    while position < frontier:
        raw = stream.readline(frontier - position)
        if not raw or not raw.endswith(b"\n"):
            raise OSError(errno.EIO,
                          "incomplete traversal of retrieval frontier")
        start, position = position, position + len(raw)
        if position > frontier:
            raise OSError(errno.EIO, "retrieval record crossed its frontier")
        yield start, position, raw[:-1]
    if position != frontier:
        raise OSError(errno.EIO, "retrieval frontier was not fully traversed")


def head_lines(path, limit=12, num_bytes=64 * 1024):
    """Returns the lines at the start of the file, used for session metadata."""
    try:
        with open(path, "rb") as f:
            return f.read(num_bytes).decode("utf-8", "replace").splitlines()[:limit]
    except OSError:
        return []


SourceCandidate = collections.namedtuple(
    "SourceCandidate", "kind relative_path expected_source project_prefix",
    defaults=(None,))
SourceRefusal = collections.namedtuple("SourceRefusal", "reason")


class DiscoveredSourcePath(str):
    """A path whose host root and relative candidate came from discovery."""

    def __new__(cls, path, root, candidate):
        value = str.__new__(cls, path)
        value.root = root
        value.candidate = candidate
        return value


class SafeSource:
    """One validated transcript object, read only through its open descriptor."""

    def __init__(self, fd, kind, relative_path, source, stat_result):
        self.fd = fd
        self.kind = kind
        self.relative_path = relative_path
        self.source = source
        self.stat = stat_result
        self.generation = self._generation()
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def close(self):
        if not self._closed:
            os.close(self.fd)
            self._closed = True

    @contextlib.contextmanager
    def _reader(self, offset=0):
        duplicate = os.dup(self.fd)
        try:
            with os.fdopen(duplicate, "rb") as stream:
                duplicate = None
                stream.seek(offset)
                yield stream
        finally:
            if duplicate is not None:
                os.close(duplicate)

    def _generation(self):
        try:
            with self._reader() as stream:
                first = stream.readline()
        except OSError:
            return None
        if not first.endswith(b"\n"):
            return None
        digest = hashlib.sha256(first).hexdigest()[:16]
        return "%d:%d:%s" % (self.stat.st_dev, self.stat.st_ino, digest)

    def head_lines(self, limit=12, num_bytes=64 * 1024):
        try:
            with self._reader() as stream:
                return stream.read(num_bytes).decode(
                    "utf-8", "replace").splitlines()[:limit]
        except OSError:
            return []

    def read_records(self, offset=0):
        try:
            with self._reader(offset) as stream:
                position = offset
                for raw in stream:
                    if not raw.endswith(b"\n"):
                        return
                    start, position = position, position + len(raw)
                    yield start, position, raw[:-1].decode("utf-8", "replace")
        except OSError:
            return

    def read_anchored_records(self, offset=0):
        try:
            with self._reader(offset) as stream:
                position = offset
                for raw in stream:
                    if not raw.endswith(b"\n"):
                        return
                    start, position = position, position + len(raw)
                    anchor = {"start": start,
                              "sha256": hashlib.sha256(raw).hexdigest()}
                    yield (start, position,
                           raw[:-1].decode("utf-8", "replace"), anchor)
        except OSError:
            return

    def read_retrieval_records(self):
        """Yield strict raw records or propagate any incomplete traversal."""
        with self._reader() as stream:
            yield from _retrieval_records_from_stream(stream)

    def anchor_at(self, offset):
        try:
            with self._reader() as stream:
                return _anchor_from_stream(stream, offset)
        except OSError:
            return None

    def size(self):
        try:
            return os.fstat(self.fd).st_size
        except OSError:
            return None

    def complete_prefix_end(self):
        try:
            size = os.fstat(self.fd).st_size
            if size == 0:
                return 0
            with self._reader() as stream:
                stream.seek(size - 1)
                if stream.read(1) == b"\n":
                    return size
                pos = size - 1
                while pos > 0:
                    start = max(0, pos - 65536)
                    stream.seek(start)
                    newline = stream.read(pos - start).rfind(b"\n")
                    if newline >= 0:
                        return start + newline + 1
                    pos = start
        except OSError:
            return 0
        return 0


def _source_open_refusal(error):
    if error.errno == errno.ENOENT:
        return SourceRefusal("missing")
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return SourceRefusal("unsafe-path")
    if error.errno in (errno.EACCES, errno.EPERM):
        return SourceRefusal("unreadable")
    return SourceRefusal("io-error")


def _open_safe_source(root, candidate, cwd):
    """Open and validate one transcript without trusting or reopening a path."""
    if not isinstance(candidate, SourceCandidate):
        return SourceRefusal("invalid-candidate")
    if candidate.kind not in ("claude", "codex"):
        return SourceRefusal("invalid-kind")
    relative = candidate.relative_path
    if (not _filesystem_safe_relative(relative)
            or os.path.isabs(relative)):
        return SourceRefusal("outside-root")
    parts = relative.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        return SourceRefusal("outside-root")
    if candidate.kind == "claude" and candidate.project_prefix is not None:
        prefix = candidate.project_prefix.split(os.sep)
        if (any(part in ("", ".", "..") for part in prefix)
                or parts[:len(prefix)] != prefix
                or len(parts) != len(prefix) + 1):
            return SourceRefusal("project-mismatch")
    required_flags = all(hasattr(os, name) for name in
                         ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
    if os.open not in getattr(os, "supports_dir_fd", set()) or not required_flags:
        return SourceRefusal("unsupported-platform")

    directories = []
    leaf = None
    try:
        resolved_root = os.path.realpath(root)
        current = os.open(resolved_root, os.O_RDONLY | os.O_DIRECTORY)
        directories.append(current)
        for component in parts[:-1]:
            current = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current)
            directories.append(current)
        leaf = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current)
        stat_result = os.fstat(leaf)
        if not stat.S_ISREG(stat_result.st_mode):
            os.close(leaf)
            leaf = None
            return SourceRefusal("non-regular")
        actual_source = source_id(relative)
        if actual_source != candidate.expected_source:
            os.close(leaf)
            leaf = None
            return SourceRefusal("identity-mismatch")
        source = SafeSource(
            leaf, candidate.kind, relative, actual_source, stat_result)
        leaf = None
        if candidate.kind == "codex":
            recorded = _rollout_cwd(source.head_lines())
            if recorded is None:
                source.close()
                return SourceRefusal("metadata-missing")
            if recorded != cwd:
                source.close()
                return SourceRefusal("project-mismatch")
        return source
    except (ValueError, UnicodeError):
        return SourceRefusal("invalid-candidate")
    except OSError as error:
        return _source_open_refusal(error)
    finally:
        if leaf is not None:
            os.close(leaf)
        for directory in reversed(directories):
            os.close(directory)


class _PathSource:
    """Compatibility view for isolated tests that inject bare path strings."""

    def __init__(self, path, kind):
        self.path = path
        self.kind = kind
        self.source = source_id(path)
        self.generation = source_generation(path)

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return None

    def head_lines(self, limit=12, num_bytes=64 * 1024):
        return head_lines(self.path, limit, num_bytes)

    def read_records(self, offset=0):
        return read_records(self.path, offset)

    def read_anchored_records(self, offset=0):
        # This path-only view exists for compatibility callers and isolated
        # tests. Production discovery uses ``SafeSource`` and hashes the raw
        # descriptor bytes directly. Keeping this adapter on the public
        # ``read_records`` seam preserves callers that inject virtual records
        # without weakening the descriptor-backed production path.
        for start, end, line in read_records(self.path, offset):
            raw = (line + "\n").encode("utf-8")
            anchor = {"start": start,
                      "sha256": hashlib.sha256(raw).hexdigest()}
            yield start, end, line, anchor

    def read_retrieval_records(self):
        with open(self.path, "rb") as stream:
            yield from _retrieval_records_from_stream(stream)

    def anchor_at(self, offset):
        try:
            with open(self.path, "rb") as stream:
                return _anchor_from_stream(stream, offset)
        except OSError:
            return None

    def size(self):
        return _source_size(self.path)

    def complete_prefix_end(self):
        return _complete_prefix_end(self.path)


def _discovered_source_path(path, root, kind, project_prefix=None):
    relative = os.path.relpath(path, root)
    return DiscoveredSourcePath(
        path, root, SourceCandidate(
            kind, relative, source_id(relative), project_prefix))


def _report_source_refusal(kind, refusal):
    print(f"antiphon: {kind} transcript refused ({refusal.reason}); "
          "discovery is incomplete", file=sys.stderr)


def _open_discovered_source(path, cwd, kind, report=True):
    if not isinstance(path, DiscoveredSourcePath):
        return _PathSource(path, kind)
    opened = _open_safe_source(path.root, path.candidate, cwd)
    if isinstance(opened, SourceRefusal):
        if report:
            _report_source_refusal(kind, opened)
        return opened
    expected = getattr(path, "expected_generation", None)
    if expected is not None and opened.generation != expected:
        opened.close()
        refusal = SourceRefusal("changed-after-discovery")
        if report:
            _report_source_refusal(kind, refusal)
        return refusal
    return opened


CatalogLoad = collections.namedtuple("CatalogLoad", "state status reason")
CatalogEnumeration = collections.namedtuple(
    "CatalogEnumeration", "kind relative_paths root_stamp")
CatalogReservation = collections.namedtuple(
    "CatalogReservation", "kind generation phase token relative_paths start end")
CatalogProgress = collections.namedtuple(
    "CatalogProgress", "state pending processed refusals gone")
CatalogObservation = collections.namedtuple(
    "CatalogObservation", "ok reason record")
CatalogView = collections.namedtuple(
    "CatalogView", "state pending candidates reason")
CatalogSnapshot = collections.namedtuple("CatalogSnapshot", "loaded view")
Discovery = collections.namedtuple(
    "Discovery", "sources state pending refusals gone reason")


def _catalog_root(cwd):
    return os.path.join(cwd, ".antiphon", "sources")


def _catalog_state_path(cwd):
    return os.path.join(_catalog_root(cwd), "state.json")


def _atomic_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
        return True
    except (OSError, UnicodeError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def _catalog_load_value(cwd, state):
    """Validate an already-decoded catalog state for this project."""
    if not isinstance(state, dict):
        return CatalogLoad(None, "invalid", "malformed")
    version = state.get("v")
    if isinstance(version, int) and version > CATALOG_VERSION:
        return CatalogLoad(None, "newer", "newer-version")
    kinds = state.get("kinds")
    if (version != CATALOG_VERSION
            or state.get("project") != os.path.abspath(cwd)
            or not isinstance(kinds, dict)
            or any(kind not in ("claude", "codex")
                   or not _valid_catalog_entry_shape(kind, entry)
                   for kind, entry in kinds.items())):
        return CatalogLoad(None, "invalid", "wrong-project-or-schema")
    return CatalogLoad(state, "valid", None)


def _read_source_catalog_raw(cwd):
    """Read physical state, without a pending-compaction safety overlay."""
    path = _catalog_state_path(cwd)
    try:
        with open(path, encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return CatalogLoad(None, "absent", None)
    except PermissionError:
        return CatalogLoad(None, "unreadable", "unreadable")
    except (OSError, json.JSONDecodeError, ValueError):
        return CatalogLoad(None, "invalid", "malformed")
    return _catalog_load_value(cwd, state)


def _read_source_catalog(cwd):
    """Read the catalog view safe for readers during interrupted compaction."""
    loaded = _read_source_catalog_raw(cwd)
    return _compaction_safe_catalog_load(cwd, loaded)


def _catalog_generation(value):
    return (isinstance(value, str) and len(value) == 32
            and all(char in "0123456789abcdef" for char in value))


def _catalog_manifest_name(kind, generation, phase):
    return f"{generation}-{kind}-{phase}.json"


def _filesystem_safe_relative(value):
    """Whether a JSON path string can reach path validation without raising."""
    if (not isinstance(value, str) or not value or "\0" in value
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        return False
    try:
        os.fsencode(value)
    except (UnicodeError, ValueError):
        return False
    return True


def _valid_catalog_enumeration(kind, enumeration):
    return (isinstance(enumeration, CatalogEnumeration)
            and enumeration.kind == kind
            and isinstance(enumeration.relative_paths, tuple)
            and all(_filesystem_safe_relative(path)
                    for path in enumeration.relative_paths))


def _finite_number(value):
    return ((isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, float) and math.isfinite(value)))


def _valid_catalog_entry_shape(kind, entry):
    """Validate nested state without opening anything it points at."""
    if not isinstance(entry, dict):
        return False
    generation = entry.get("generation")
    phase = entry.get("phase")
    base = entry.get("base_manifest")
    delta = entry.get("delta_manifest")
    base_next = entry.get("base_next")
    delta_next = entry.get("delta_next")
    if (not _catalog_generation(generation)
            or phase not in ("base", "reconcile", "delta", "complete")
            or base != _catalog_manifest_name(kind, generation, "base")
            or isinstance(base_next, bool) or not isinstance(base_next, int)
            or base_next < 0
            or isinstance(delta_next, bool) or not isinstance(delta_next, int)
            or delta_next < 0):
        return False
    if phase in ("base", "reconcile"):
        if delta is not None or delta_next != 0:
            return False
    elif delta != _catalog_manifest_name(kind, generation, "delta"):
        return False
    stamp = entry.get("root_stamp")
    if (stamp is not None
            and (not isinstance(stamp, list) or len(stamp) != 3
                 or any(isinstance(item, bool) or not isinstance(item, int)
                        for item in stamp))):
        return False
    inflight = entry.get("inflight")
    if inflight is None:
        return True
    if phase not in ("base", "delta") or not isinstance(inflight, dict):
        return False
    start, end = inflight.get("start"), inflight.get("end")
    paths = inflight.get("paths")
    return (inflight.get("phase") == phase
            and isinstance(inflight.get("token"), str)
            and bool(inflight["token"])
            and not isinstance(start, bool) and isinstance(start, int)
            and not isinstance(end, bool) and isinstance(end, int)
            and 0 <= start < end
            and isinstance(paths, list)
            and len(paths) == end - start
            and all(isinstance(path, str) for path in paths))


def _new_catalog_state(cwd):
    return {"v": CATALOG_VERSION, "project": os.path.abspath(cwd), "kinds": {}}


def _write_catalog_state(cwd, state, cleanup=True):
    written = _atomic_json(_catalog_state_path(cwd), state)
    if (written and cleanup
            and getattr(_PROJECT_LOCK_STATE, "kind", None) == "catalog"):
        # State is already durable. Cleanup is best effort and can only remove
        # grammar-valid manifests the just-written state does not reference.
        _cleanup_catalog_manifests(cwd)
    return written


def _catalog_manifest_path(cwd, filename):
    return os.path.join(_catalog_root(cwd), "manifests", filename)


def _referenced_catalog_manifests(cwd):
    """Referenced manifest basenames, or None when state cannot be trusted."""
    loaded = _read_source_catalog(cwd)
    if loaded.status == "absent":
        return set()
    if loaded.status != "valid":
        return None
    referenced = set()
    for entry in loaded.state["kinds"].values():
        for key in ("base_manifest", "delta_manifest"):
            filename = entry.get(key)
            if filename is not None:
                referenced.add(filename)
    # A prepared transaction serves the old state; a committed cleanup receipt
    # still needs both states to validate after a crash. Neither generation is
    # an orphan until the journal itself is gone.
    for kind in ("claude", "codex"):
        journal, status = _read_compaction_journal(cwd, kind)
        if status == "missing":
            continue
        if status != "valid":
            return None
        for state in (journal["old_state"], journal["new_state"]):
            for entry in state["kinds"].values():
                for key in ("base_manifest", "delta_manifest"):
                    filename = entry.get(key)
                    if filename is not None:
                        referenced.add(filename)
    return referenced


def _unreferenced_catalog_manifests(cwd):
    """Owned regular manifest files safe to unlink, never links or odd names."""
    referenced = _referenced_catalog_manifests(cwd)
    if referenced is None:
        return ()
    directory = os.path.join(_catalog_root(cwd), "manifests")
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return ()
    pending = []
    for entry in entries:
        if (not CATALOG_MANIFEST_PATTERN.fullmatch(entry.name)
                or entry.name in referenced):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        pending.append(os.path.join(directory, entry.name))
    return tuple(pending)


def _cleanup_catalog_manifests(cwd):
    """Best-effort removal of unreferenced owned manifests; return pending."""
    pending = 0
    for path in _unreferenced_catalog_manifests(cwd):
        try:
            os.unlink(path)
        except OSError:
            pending += 1
    return pending


def _catalog_cleanup_pending(cwd):
    """Count owned cleanup candidates under a read-only catalog snapshot."""
    root = _catalog_root(cwd)
    lock_path = os.path.join(root, ".lock")
    if (not os.path.exists(_catalog_state_path(cwd))
            and not os.path.exists(os.path.join(root, "manifests"))
            and not os.path.exists(lock_path)):
        return 0
    with catalog_lock(cwd, shared=True) as locked:
        if not locked or _referenced_catalog_manifests(cwd) is None:
            return None
        return len(_unreferenced_catalog_manifests(cwd))


def _write_catalog_manifest(cwd, filename, data):
    path = _catalog_manifest_path(cwd, filename)
    if os.path.exists(path):
        return False
    return _atomic_json(path, data)


def _read_catalog_manifest(cwd, filename, kind=None, generation=None,
                           phase=None):
    if (not _filesystem_safe_relative(filename)
            or os.path.basename(filename) != filename):
        return None
    try:
        with open(_catalog_manifest_path(cwd, filename), encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if (not isinstance(data, dict)
            or data.get("v") != CATALOG_VERSION
            or data.get("project") != os.path.abspath(cwd)
            or data.get("kind") not in ("claude", "codex")
            or not _catalog_generation(data.get("generation"))
            or data.get("phase") not in ("base", "delta")
            or not isinstance(data.get("paths"), list)
            or any(not _filesystem_safe_relative(path)
                   for path in data["paths"])
            or len(set(data["paths"])) != len(data["paths"])
            or (kind is not None and data.get("kind") != kind)
            or (generation is not None
                and data.get("generation") != generation)
            or (phase is not None and data.get("phase") != phase)
            or (kind is not None and generation is not None and phase is not None
                and filename != _catalog_manifest_name(
                    kind, generation, phase))):
        return None
    return data


def _catalog_view(cwd, kind, loaded=None):
    """Prove one kind's state/manifest join and expose committed candidates."""
    loaded = loaded or _read_source_catalog(cwd)
    if loaded.status in ("invalid", "newer", "unreadable"):
        return CatalogView("degraded", 0, (), loaded.reason)
    entry = (((loaded.state or {}).get("kinds") or {}).get(kind))
    if entry is None:
        return CatalogView("building", 0, (), None)
    if not _valid_catalog_entry_shape(kind, entry):
        return CatalogView("degraded", 0, (), "malformed-entry")
    generation = entry["generation"]
    base = _read_catalog_manifest(
        cwd, entry["base_manifest"], kind, generation, "base")
    if base is None or entry["base_next"] > len(base["paths"]):
        return CatalogView("degraded", 0, (), "untrusted-base-manifest")
    phase = entry["phase"]
    if phase in ("reconcile", "delta", "complete") \
            and entry["base_next"] != len(base["paths"]):
        return CatalogView("degraded", 0, (), "incomplete-base-manifest")
    candidates = list(base["paths"][:entry["base_next"]])
    pending = max(0, len(base["paths"]) - entry["base_next"])
    active_manifest = base
    active_next = entry["base_next"]
    if phase in ("delta", "complete"):
        delta = _read_catalog_manifest(
            cwd, entry["delta_manifest"], kind, generation, "delta")
        if delta is None or entry["delta_next"] > len(delta["paths"]):
            return CatalogView("degraded", 0, (), "untrusted-delta-manifest")
        if phase == "complete" and entry["delta_next"] != len(delta["paths"]):
            return CatalogView("degraded", 0, (), "incomplete-delta-manifest")
        candidates.extend(delta["paths"][:entry["delta_next"]])
        if len(set(candidates)) != len(candidates):
            return CatalogView("degraded", 0, (), "duplicate-manifest-candidate")
        pending = max(0, len(delta["paths"]) - entry["delta_next"])
        active_manifest = delta
        active_next = entry["delta_next"]
    inflight = entry.get("inflight")
    if inflight is not None:
        if (inflight["start"] != active_next
                or inflight["end"] > len(active_manifest["paths"])
                or inflight["paths"] != active_manifest["paths"][
                    inflight["start"]:inflight["end"]]):
            return CatalogView("degraded", 0, (), "malformed-reservation")
    state = "complete" if phase == "complete" else "building"
    return CatalogView(state, pending, tuple(candidates), None)


def _catalog_snapshot(cwd, kind):
    """Copy validated state and manifest candidates under one shared lock."""
    if not os.path.exists(_catalog_state_path(cwd)):
        loaded = _read_source_catalog(cwd)
        return CatalogSnapshot(loaded, _catalog_view(cwd, kind, loaded))
    with catalog_lock(cwd, shared=True) as locked:
        if not locked:
            loaded = CatalogLoad(None, "unreadable", "lock-contention")
            return CatalogSnapshot(
                loaded, CatalogView("degraded", 0, (), "lock-contention"))
        loaded = _read_source_catalog(cwd)
        return CatalogSnapshot(loaded, _catalog_view(cwd, kind, loaded))


def _catalog_record_path(cwd, kind, relative):
    digest = hashlib.sha256((kind + "\0" + relative).encode("utf-8")).hexdigest()
    return os.path.join(
        _catalog_root(cwd), "records", kind, digest[:2], digest + ".json")


def _read_catalog_record(cwd, kind, relative):
    if not _filesystem_safe_relative(relative):
        return None
    try:
        with open(_catalog_record_path(cwd, kind, relative),
                  encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    status = value.get("status") if isinstance(value, dict) else None
    generation = value.get("generation") if isinstance(value, dict) else None
    complete_size = value.get("complete_size") if isinstance(value, dict) else None
    if (not isinstance(value, dict)
            or value.get("v") != CATALOG_VERSION
            or value.get("project") != os.path.abspath(cwd)
            or value.get("kind") != kind
            or value.get("relative") != relative
            or value.get("source") != source_id(relative)
            or status not in ("ready", "unrelated", "refused")
            or (status == "unrelated" and kind != "codex")
            or not _finite_number(value.get("observed"))
            or (status == "ready"
                and (not isinstance(generation, str) or not generation
                     or isinstance(complete_size, bool)
                     or not isinstance(complete_size, int)
                     or complete_size < 0))
            or (status != "ready"
                and (generation is not None or complete_size is not None
                     or not isinstance(value.get("reason"), str)))):
        return None
    historical = (
        value.get("last_complete_generation"),
        value.get("last_complete_size"),
        value.get("last_complete_observed"),
    )
    if any(item is not None for item in historical):
        old_generation, old_size, old_observed = historical
        if (not isinstance(old_generation, str) or not old_generation
                or isinstance(old_size, bool) or not isinstance(old_size, int)
                or old_size < 0
                or not _finite_number(old_observed)):
            return None
    return value


def _write_catalog_record(cwd, kind, relative, record):
    path = _catalog_record_path(cwd, kind, relative)
    previous = None
    if os.path.exists(path):
        previous = _read_catalog_record(cwd, kind, relative)
        if previous is None:
            return False
    record = json.loads(json.dumps(record))
    if record.get("status") == "ready":
        record["last_complete_generation"] = record.get("generation")
        record["last_complete_size"] = record.get("complete_size")
        record["last_complete_observed"] = record.get("observed")
    elif previous is not None:
        for key in ("last_complete_generation", "last_complete_size",
                    "last_complete_observed"):
            if key in previous:
                record[key] = previous[key]
    return _atomic_json(path, record)


def _directory_stamp(path):
    try:
        info = os.stat(path)
        return [info.st_dev, info.st_ino, info.st_mtime_ns]
    except OSError:
        return None


def _enumerate_catalog_candidates(cwd, kind):
    """Capture names only; transcript descriptors are opened by the batch."""
    if kind == "claude":
        directory = claude_project_dir(cwd)
        if not directory:
            return CatalogEnumeration(kind, (), None)
        prefix = os.path.relpath(directory, CLAUDE_PROJECTS)
        try:
            names = [entry.name for entry in os.scandir(directory)
                     if entry.name.endswith(".jsonl")]
        except OSError:
            return None
        relative = tuple(sorted(os.path.join(prefix, name) for name in names))
        return CatalogEnumeration(kind, relative, _directory_stamp(directory))
    if kind == "codex":
        pattern = os.path.join(CODEX_SESSIONS, "**", "rollout-*.jsonl")
        try:
            relative = tuple(sorted(
                os.path.relpath(path, CODEX_SESSIONS)
                for path in glob.glob(pattern, recursive=True)))
        except OSError:
            return None
        return CatalogEnumeration(kind, relative, None)
    return None


def _claude_catalog_prefix(cwd):
    """The one host-selected Claude project directory this cwd may read."""
    directory = claude_project_dir(cwd)
    if not directory:
        return None
    relative = os.path.relpath(directory, CLAUDE_PROJECTS)
    if (relative in ("", ".", "..") or relative.startswith(".." + os.sep)
            or os.path.isabs(relative) or os.sep in relative):
        return None
    return relative


def _start_catalog_generation(cwd, kind, enumeration):
    if not _valid_catalog_enumeration(kind, enumeration):
        return False
    loaded = _read_source_catalog(cwd)
    if loaded.status in ("invalid", "newer", "unreadable"):
        return False
    state = (json.loads(json.dumps(loaded.state)) if loaded.state is not None
             else _new_catalog_state(cwd))
    generation = uuid.uuid4().hex
    filename = f"{generation}-{kind}-base.json"
    manifest = {
        "v": CATALOG_VERSION, "project": os.path.abspath(cwd),
        "kind": kind, "generation": generation, "phase": "base",
        "paths": list(enumeration.relative_paths),
        "root_stamp": enumeration.root_stamp,
    }
    if not _write_catalog_manifest(cwd, filename, manifest):
        return False
    state["kinds"][kind] = {
        "generation": generation, "phase": "base",
        "base_manifest": filename, "base_next": 0,
        "delta_manifest": None, "delta_next": 0,
        "root_stamp": enumeration.root_stamp, "inflight": None,
    }
    return _write_catalog_state(cwd, state)


def _phase_manifest(entry):
    if entry.get("phase") == "base":
        return entry.get("base_manifest"), entry.get("base_next", 0)
    if entry.get("phase") == "delta":
        return entry.get("delta_manifest"), entry.get("delta_next", 0)
    return None, 0


def _reserve_catalog_batch(cwd, kind, limit=None):
    limit = CATALOG_BATCH if limit is None else limit
    loaded = _read_source_catalog(cwd)
    if loaded.status != "valid":
        return None
    state = json.loads(json.dumps(loaded.state))
    entry = state["kinds"].get(kind)
    if not isinstance(entry, dict):
        return None
    view = _catalog_view(cwd, kind, loaded)
    if view.state == "degraded":
        return False
    inflight = entry.get("inflight")
    if isinstance(inflight, dict):
        return CatalogReservation(
            kind, entry["generation"], inflight["phase"], inflight["token"],
            tuple(inflight["paths"]), inflight["start"], inflight["end"])
    filename, start = _phase_manifest(entry)
    if not filename:
        return None
    manifest = _read_catalog_manifest(
        cwd, filename, kind, entry["generation"], entry["phase"])
    if manifest is None:
        return False
    paths = manifest["paths"]
    if start >= len(paths):
        entry["phase"] = "reconcile" if entry["phase"] == "base" else "complete"
        return None if _write_catalog_state(cwd, state) else False
    end = min(len(paths), start + max(1, int(limit)))
    token = uuid.uuid4().hex
    entry["inflight"] = {
        "phase": entry["phase"], "token": token,
        "paths": paths[start:end], "start": start, "end": end,
    }
    if not _write_catalog_state(cwd, state):
        return False
    return CatalogReservation(
        kind, entry["generation"], entry["phase"], token,
        tuple(paths[start:end]), start, end)


def _observe_catalog_candidate(cwd, kind, relative):
    root = CLAUDE_PROJECTS if kind == "claude" else CODEX_SESSIONS
    prefix = _claude_catalog_prefix(cwd) if kind == "claude" else None
    if kind == "claude" and prefix is None:
        return {
            "v": CATALOG_VERSION, "project": os.path.abspath(cwd),
            "kind": kind, "relative": relative,
            "source": source_id(os.path.basename(relative)),
            "status": "refused", "reason": "project-mismatch",
            "observed": time.time(), "generation": None,
            "complete_size": None,
        }
    candidate = SourceCandidate(
        kind, relative, source_id(os.path.basename(relative)), prefix)
    opened = _open_safe_source(root, candidate, cwd)
    observed = time.time()
    if isinstance(opened, SourceRefusal):
        status = ("unrelated" if opened.reason == "project-mismatch"
                  and kind == "codex" else "refused")
        return {
            "v": CATALOG_VERSION, "project": os.path.abspath(cwd),
            "kind": kind, "relative": relative,
            "source": candidate.expected_source, "status": status,
            "reason": opened.reason, "observed": observed,
            "generation": None, "complete_size": None,
        }
    with opened as source:
        if source.generation is None:
            return {
                "v": CATALOG_VERSION, "project": os.path.abspath(cwd),
                "kind": kind, "relative": relative, "source": source.source,
                "status": "refused", "reason": "partial-record",
                "observed": observed, "generation": None,
                "complete_size": None,
            }
        return {
            "v": CATALOG_VERSION, "project": os.path.abspath(cwd),
            "kind": kind, "relative": relative, "source": source.source,
            "status": "ready", "reason": None, "observed": observed,
            "generation": source.generation,
            "complete_size": source.complete_prefix_end(),
        }


def _merge_catalog_batch(cwd, reservation, observations):
    loaded = _read_source_catalog(cwd)
    if loaded.status != "valid":
        return False
    state = json.loads(json.dumps(loaded.state))
    entry = state["kinds"].get(reservation.kind)
    inflight = entry.get("inflight") if isinstance(entry, dict) else None
    if (not isinstance(inflight, dict)
            or entry.get("generation") != reservation.generation
            or inflight.get("token") != reservation.token):
        return False
    for relative, record in zip(reservation.relative_paths, observations):
        if not _write_catalog_record(cwd, reservation.kind, relative, record):
            return False
    key = "base_next" if reservation.phase == "base" else "delta_next"
    entry[key] = reservation.end
    entry["inflight"] = None
    filename = (entry["base_manifest"] if reservation.phase == "base"
                else entry["delta_manifest"])
    manifest = _read_catalog_manifest(
        cwd, filename, reservation.kind, reservation.generation,
        reservation.phase)
    if manifest is None:
        return False
    if reservation.end >= len(manifest["paths"]):
        entry["phase"] = "reconcile" if reservation.phase == "base" else "complete"
    return _write_catalog_state(cwd, state)


def _install_reconciliation(cwd, kind, enumeration):
    if not _valid_catalog_enumeration(kind, enumeration):
        return False
    loaded = _read_source_catalog(cwd)
    if loaded.status != "valid":
        return False
    state = json.loads(json.dumps(loaded.state))
    entry = state["kinds"].get(kind)
    if not isinstance(entry, dict) or entry.get("phase") != "reconcile":
        return False
    base = _read_catalog_manifest(
        cwd, entry["base_manifest"], kind, entry["generation"], "base")
    if base is None:
        return False
    unseen = sorted(set(enumeration.relative_paths) - set(base["paths"]))
    filename = f"{entry['generation']}-{kind}-delta.json"
    manifest = {
        "v": CATALOG_VERSION, "project": os.path.abspath(cwd),
        "kind": kind, "generation": entry["generation"], "phase": "delta",
        "paths": unseen, "root_stamp": enumeration.root_stamp,
    }
    if not _write_catalog_manifest(cwd, filename, manifest):
        existing = _read_catalog_manifest(
            cwd, filename, kind, entry["generation"], "delta")
        if existing != manifest:
            # The immutable publication landed, but the host snapshot changed
            # before state could name it. Leave that orphan untouched and
            # start a finite generation from the new snapshot plus everything
            # the committed base already retained; retrying the deterministic
            # old filename could never converge.
            replacement = enumeration._replace(relative_paths=tuple(sorted(
                set(enumeration.relative_paths).union(base["paths"]))))
            return _start_catalog_generation(cwd, kind, replacement)
    entry["delta_manifest"] = filename
    entry["delta_next"] = 0
    entry["root_stamp"] = enumeration.root_stamp
    entry["phase"] = "delta" if unseen else "complete"
    return _write_catalog_state(cwd, state)


def _catalog_pending(cwd, kind):
    view = _catalog_view(cwd, kind)
    return view.pending if view.state != "degraded" else 0


def _catalog_refresh_needed(cwd, kind, entry):
    if not isinstance(entry, dict) or entry.get("phase") != "complete":
        return False
    view = _catalog_view(cwd, kind)
    if view.state == "degraded":
        return False
    records, structural, _retryable = _catalog_record_inventory(
        cwd, kind, view)
    if structural or any(
            record.get("status") == "refused"
            and record.get("reason") != "missing"
            for record in records):
        return True
    if kind == "claude":
        directory = claude_project_dir(cwd)
        return bool(directory and _directory_stamp(directory) != entry.get("root_stamp"))
    if kind == "codex":
        for path in codex_rollout_files(cwd):
            if (isinstance(path, DiscoveredSourcePath)
                    and _read_catalog_record(
                        cwd, kind, path.candidate.relative_path) is None):
                return True
    return False


def _catalog_scan_step(cwd, kind, force=False):
    loaded = _read_source_catalog(cwd)
    if loaded.status in ("invalid", "newer", "unreadable"):
        return CatalogProgress("degraded", 0, 0, 1, 0)
    entry = ((loaded.state or {}).get("kinds") or {}).get(kind)
    view = _catalog_view(cwd, kind, loaded)
    if view.state == "degraded":
        return CatalogProgress("degraded", 0, 0, 1, 0)
    if (entry is None
            or ((force or _catalog_refresh_needed(cwd, kind, entry))
                and entry.get("phase") == "complete")):
        enumeration = _enumerate_catalog_candidates(cwd, kind)
        if enumeration is None:
            return CatalogProgress("degraded", 0, 0, 1, 0)
        if entry is not None:
            enumeration = enumeration._replace(relative_paths=tuple(sorted(
                set(enumeration.relative_paths).union(view.candidates))))
        result = _catalog_phase(
            cwd, lambda: _start_catalog_generation(cwd, kind, enumeration))
        if not result.ok or not result.value:
            return CatalogProgress("degraded", 0, 0, 1, 0)
    loaded = _read_source_catalog(cwd)
    view = _catalog_view(cwd, kind, loaded)
    if loaded.status != "valid" or view.state == "degraded":
        return CatalogProgress("degraded", 0, 0, 1, 0)
    entry = loaded.state["kinds"].get(kind)
    if entry.get("phase") == "reconcile":
        enumeration = _enumerate_catalog_candidates(cwd, kind)
        if enumeration is None:
            return CatalogProgress("degraded", 0, 0, 1, 0)
        result = _catalog_phase(
            cwd, lambda: _install_reconciliation(cwd, kind, enumeration))
        if not result.ok or not result.value:
            return CatalogProgress("degraded", 0, 0, 1, 0)
    reservation_result = _catalog_phase(
        cwd, lambda: _reserve_catalog_batch(cwd, kind))
    if not reservation_result.ok or reservation_result.value is False:
        return CatalogProgress("degraded", _catalog_pending(cwd, kind), 0, 1, 0)
    reservation = reservation_result.value
    if reservation is None:
        loaded = _read_source_catalog(cwd)
        view = _catalog_view(cwd, kind, loaded)
        if view.state == "degraded":
            return CatalogProgress("degraded", 0, 0, 1, 0)
        records, structural, retryable = _catalog_record_inventory(
            cwd, kind, view)
        unproved = structural + retryable
        retry_now = any(
            record.get("status") == "refused"
            and record.get("reason") != "missing"
            for record in records)
        state = ("degraded" if structural else
                 "building" if retry_now else view.state)
        return CatalogProgress(state, view.pending, 0, unproved, 0)
    observations = [
        _observe_catalog_candidate(cwd, kind, relative)
        for relative in reservation.relative_paths]
    merged = _catalog_phase(
        cwd, lambda: _merge_catalog_batch(cwd, reservation, observations))
    if not merged.ok or not merged.value:
        return CatalogProgress("degraded", _catalog_pending(cwd, kind), 0, 1, 0)
    refusals = sum(
        record.get("status") == "refused"
        and record.get("reason") != "missing"
        for record in observations)
    observed_refusals = refusals
    loaded = _read_source_catalog(cwd)
    view = _catalog_view(cwd, kind, loaded)
    _records, structural, retryable = _catalog_record_inventory(
        cwd, kind, view)
    unproved = structural + retryable
    refusals = max(refusals, unproved)
    state = ("degraded" if (view.state == "degraded" or structural
                            or observed_refusals) else
             view.state)
    return CatalogProgress(
        state,
        _catalog_pending(cwd, kind), len(observations), refusals, 0)


def _record_current_source(cwd, kind, transcript_path):
    if not isinstance(transcript_path, str) or not transcript_path:
        return CatalogObservation(False, "missing-path", None)
    root = CLAUDE_PROJECTS if kind == "claude" else CODEX_SESSIONS
    relative = os.path.relpath(transcript_path, root)
    if relative == ".." or relative.startswith(".." + os.sep):
        return CatalogObservation(False, "outside-root", None)
    record = _observe_catalog_candidate(cwd, kind, relative)
    if record["status"] != "ready":
        return CatalogObservation(False, record["reason"], record)
    loaded = _read_source_catalog(cwd)
    if loaded.status in ("invalid", "newer", "unreadable"):
        return CatalogObservation(False, loaded.reason, record)
    result = _catalog_phase(
        cwd, lambda: _write_catalog_record(cwd, kind, relative, record))
    if not result.ok or not result.value:
        return CatalogObservation(False, result.reason or "write-failed", record)
    return CatalogObservation(True, None, record)


def _catalog_records(cwd, kind):
    """Return valid candidate records plus a structural-refusal count."""
    pattern = os.path.join(_catalog_root(cwd), "records", kind, "*", "*.json")
    records, malformed = [], 0
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError, ValueError):
            malformed += 1
            continue
        relative = value.get("relative") if isinstance(value, dict) else None
        if (not _filesystem_safe_relative(relative)
                or _read_catalog_record(cwd, kind, relative) != value
                or os.path.abspath(_catalog_record_path(cwd, kind, relative))
                != os.path.abspath(path)):
            malformed += 1
            continue
        records.append(value)
    return records, malformed


def _catalog_record_inventory(cwd, kind, view):
    """Return records, structural gaps, and retryable stored refusals."""
    records, malformed = _catalog_records(cwd, kind)
    by_relative = {record["relative"]: record for record in records}
    missing = set(view.candidates) - set(by_relative)
    retryable = {
        record["relative"] for record in records
        if (record.get("status") not in ("ready", "unrelated")
            and not (record.get("status") == "refused"
                     and record.get("reason") == "missing"
                     and record.get("last_complete_generation") is not None))
    }
    return records, malformed + len(missing), len(retryable)


def _catalog_marker(cwd, kind, relative):
    root = CLAUDE_PROJECTS if kind == "claude" else CODEX_SESSIONS
    prefix = _claude_catalog_prefix(cwd) if kind == "claude" else None
    if kind == "claude" and prefix is None:
        return None
    path = os.path.join(root, relative)
    return DiscoveredSourcePath(
        path, root, SourceCandidate(
            kind, relative, source_id(os.path.basename(relative)), prefix))


def _gone_irrelevant(records, positions, boundary):
    for record in records:
        generation = record.get("last_complete_generation") or record.get("generation")
        size = record.get("last_complete_size")
        observed = record.get("last_complete_observed") or record.get("observed")
        position = (positions or {}).get(record.get("source"))
        consumed = (isinstance(position, dict)
                    and position.get("gen") == generation
                    and isinstance(position.get("offset"), int)
                    and isinstance(size, int)
                    and position["offset"] >= size)
        aged_out = _finite_number(observed) and observed < boundary
        if not consumed and not aged_out:
            return False
    return True


def _discover_sources(cwd, kind, reader_side, positions, since,
                      catalog_snapshot=None):
    """Safely union catalog and recent candidates for one reader, read-only."""
    del reader_side, since                 # the v3 position map is the authority here
    snapshot = catalog_snapshot or _catalog_snapshot(cwd, kind)
    loaded, view = snapshot.loaded, snapshot.view
    base_state, pending = view.state, view.pending
    structural = 1 if view.state == "degraded" else 0
    trust_inventory = view.state != "degraded"
    records, record_issues, _retryable = (
        _catalog_record_inventory(cwd, kind, view)
        if trust_inventory else ([], 0, 0))
    structural += record_issues
    by_relative = {record["relative"]: record for record in records}
    markers = {}
    for relative in set(by_relative).union(view.candidates):
        marker = _catalog_marker(cwd, kind, relative)
        if marker is None:
            structural += 1
            continue
        markers[relative] = marker
    recent = (claude_transcripts(cwd) if kind == "claude"
              else codex_rollout_files(cwd))[:RECENT_FILES]
    uncatalogued = 0
    legacy_recent = []
    for path in recent:
        if not isinstance(path, DiscoveredSourcePath):
            # Unit-level callers historically inject plain temporary paths.
            # Production discovery always returns a rooted candidate and is
            # never allowed onto this compatibility road.
            legacy_recent.append(path)
            continue
        relative = path.candidate.relative_path
        if relative not in markers:
            uncatalogued += 1
        markers.setdefault(relative, path)

    groups = collections.defaultdict(list)
    for relative, marker in markers.items():
        groups[marker.candidate.expected_source].append((relative, marker))

    selected = list(legacy_recent)
    refusals = structural
    gone = 0
    boundary = time.time() - LOOKBACK
    for sid, candidates in groups.items():
        ready, missing, excluded, unproven = [], [], [], []
        for relative, marker in candidates:
            opened = _open_safe_source(marker.root, marker.candidate, cwd)
            if isinstance(opened, SourceRefusal):
                if opened.reason == "missing":
                    missing.append(relative)
                elif kind == "codex" and opened.reason == "project-mismatch":
                    record = by_relative.get(relative)
                    if (record is not None
                            and record.get("status") != "unrelated"):
                        # This path was previously proved as ours. A later
                        # metadata mismatch is a replacement/refusal, not an
                        # ordinary candidate for another project.
                        unproven.append(relative)
                    else:
                        excluded.append(relative)
                else:
                    unproven.append(relative)
                continue
            identity = (opened.stat.st_dev, opened.stat.st_ino)
            marker.expected_generation = opened.generation
            ready.append((identity, marker))
            opened.close()
        if unproven:
            refusals += len(unproven)
        identities = {identity for identity, _marker in ready}
        if len(identities) > 1:
            refusals += len(identities)
            continue
        if ready:
            selected.append(ready[0][1])
            continue
        relevant_records = [by_relative[relative] for relative in missing
                            if relative in by_relative]
        recorded_candidates = [relative for relative, _marker in candidates
                               if relative in by_relative
                               and by_relative[relative].get("status") != "unrelated"]
        if (recorded_candidates and len(missing) == len(recorded_candidates)
                and not unproven):
            gone += 1
            if not _gone_irrelevant(relevant_records, positions, boundary):
                refusals += 1

    if structural or refusals:
        state = "degraded"
        reason = "some project sources could not be proved"
    elif base_state == "degraded":
        state, reason = base_state, "source catalog state could not be trusted"
    elif base_state == "building" or uncatalogued:
        state, reason = "building", "source catalog bootstrap is incomplete"
    else:
        state, reason = "complete", None
    return Discovery(tuple(selected), state, pending, refusals, gone, reason)


def iso_epoch(s):
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


Event = collections.namedtuple(
    "Event", "time kind text source generation offset end before_anchor anchor public_id",
    defaults=(None, None, None))
Record = collections.namedtuple(
    "Record", "time source generation offset end events before_anchor anchor",
    defaults=(None, None))
PageAdvance = collections.namedtuple(
    "PageAdvance", "sources has_more replay_reason next_lane", defaults=(None,))

TOOL_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
TOOL_ID = re.compile(r"tc1\.(?P<kind>[cx])\.(?P<digest>[A-Za-z0-9_-]{16})")
TOOL_KIND_CODE = {"claude": "c", "codex": "x"}
TOOL_CODE_KIND = {code: kind for kind, code in TOOL_KIND_CODE.items()}
ToolInvocation = collections.namedtuple(
    "ToolInvocation",
    "public_id side call_type name arguments namespace caller",
    defaults=(None, None))
RetrievalResult = collections.namedtuple(
    "RetrievalResult", "status invocation reason", defaults=(None, None))


def _public_invocation(invocation):
    """The complete invocation a public id names, with no source metadata."""
    value = {
        "id": invocation.public_id,
        "side": invocation.side,
        "call_type": invocation.call_type,
        "name": invocation.name,
    }
    if invocation.namespace is not None:
        value["namespace"] = invocation.namespace
    if invocation.caller is not None:
        value["caller"] = invocation.caller
    value["arguments"] = invocation.arguments
    return value


def _tool_digest(value):
    """The 96 content-bound bits carried in a tc1 public id."""
    return hashlib.sha256(value).digest()[:12]


def _make_invocation(side, call_type, name, arguments, source, native_id,
                     start, ordinal=0, namespace=None, caller=None):
    """Build one strict, canonical invocation or fail closed with ``None``."""
    if (side not in TOOL_KIND_CODE
            or not isinstance(call_type, str) or not call_type
            or not isinstance(name, str)
            or TOOL_COMPONENT.fullmatch(name) is None
            or not isinstance(source, str) or not source
            or not isinstance(start, int) or isinstance(start, bool) or start < 0
            or not isinstance(ordinal, int) or isinstance(ordinal, bool)
            or ordinal < 0):
        return None
    namespace = (namespace if isinstance(namespace, str)
                 and TOOL_COMPONENT.fullmatch(namespace) is not None else None)
    caller = caller if isinstance(caller, str) and caller else None
    fields = {
        "side": side, "call_type": call_type, "name": name,
        "arguments": arguments,
    }
    if namespace is not None:
        fields["namespace"] = namespace
    if caller is not None:
        fields["caller"] = caller
    identity = ({"native_id": native_id}
                if isinstance(native_id, str) and native_id
                else {"record_start": start, "block_ordinal": ordinal})
    bound = {
        "token": "tc1", "source_kind": side, "source_identity": source,
        "call_identity": identity, "invocation": fields,
    }
    try:
        canonical = json.dumps(
            bound, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    digest = base64.urlsafe_b64encode(_tool_digest(canonical)).decode("ascii")
    public_id = f"tc1.{TOOL_KIND_CODE[side]}.{digest.rstrip('=')}"
    return ToolInvocation(
        public_id, side, call_type, name, arguments, namespace, caller)


def _claude_invocation(block, source, start, ordinal):
    """Extract one real Claude ``tool_use`` invocation without its result."""
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return None
    arguments = block.get("input")
    if not isinstance(arguments, dict):
        return None
    caller = block.get("caller")
    caller = caller.get("type") if isinstance(caller, dict) else None
    return _make_invocation(
        "claude", "tool_use", block.get("name"), arguments, source,
        block.get("id"), start, ordinal, caller=caller)
REPLAY_NOTICES = {
    "legacy_upgrade": (
        "replay: replaying discovered history after an upgrade; duplicates "
        "are expected until this backlog drains — `antiphon catch-up` skips "
        "what is left"),
    "cursor_recovery": (
        "replay: replaying discovered history because the previous cursor "
        "could not be trusted; duplicates are expected until this backlog "
        "drains — `antiphon catch-up` skips what is left"),
    "anchor_upgrade": (
        "replay: adopting a delivered frontier from the previous cursor "
        "format; at most its boundary record repeats while the content "
        "anchor is established"),
}


def offset_at_or_after(path, timestamp):
    """The offset of the first record at or after `timestamp`, or the file's end.

    Run for a source a peer has never read, to place the normal lookback
    window — and, since 0.3.2, for the published upgrade path: a numeric
    0.1.0 `_seen` time arrives here as `since`. A v2 *map* does not: it is a
    scan high-water mark, not a delivery frontier, and like a malformed or
    unreadable cursor file it takes the byte-zero replay. `>=` rather than
    `>` repeats every record sharing the boundary timestamp — the whole
    cohort, per source (measured: up to 10 records share one second in real
    transcripts) — a duplicate, which this bridge accepts where it never
    accepts a gap.
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


def _source_offset_at_or_after(source, timestamp):
    """Descriptor/path-view equivalent of `offset_at_or_after`."""
    end = 0
    for start, end, line in source.read_records():
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


def _path_anchor_at(path, offset):
    try:
        with open(path, "rb") as stream:
            return _anchor_from_stream(stream, offset)
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
    start, reason = _resolve_start(path, sid, generation, positions, since)
    if reason == "replaced":
        print("antiphon: a transcript was replaced since it was last read; "
              "reading it again", file=sys.stderr)
    elif reason == "unmeasurable":
        print("antiphon: a transcript could not be measured; reading it again",
              file=sys.stderr)
    elif reason == "shrunk":
        print("antiphon: a transcript is shorter than the %d bytes already "
              "read from it; reading it again" % positions[sid]["offset"],
              file=sys.stderr)
    return start


def _start_source_offset(source, positions, since):
    """The existing start rule applied to one already-open source object."""
    start, reason = _resolve_source_start(source, positions, since)
    if reason == "replaced":
        print("antiphon: a transcript was replaced since it was last read; "
              "reading it again", file=sys.stderr)
    elif reason == "unmeasurable":
        print("antiphon: a transcript could not be measured; reading it again",
              file=sys.stderr)
    elif reason == "shrunk":
        print("antiphon: a transcript is shorter than the %d bytes already "
              "read from it; reading it again" %
              positions[source.source]["offset"], file=sys.stderr)
    elif reason == "rewritten":
        print("antiphon: the record anchoring a delivered transcript frontier "
              "was rewritten; reading that record again", file=sys.stderr)
    elif reason == "invalid-anchor":
        print("antiphon: a transcript frontier anchor could not be proved; "
              "reading it again", file=sys.stderr)
    return start


def _resolve_source_start(source, positions, since):
    recorded = (positions or {}).get(source.source)
    if recorded:
        if recorded.get("gen") != source.generation:
            return 0, "replaced"
        size = source.size()
        if size is None:
            return 0, "unmeasurable"
        if recorded["offset"] > size:
            return 0, "shrunk"
        if source.source in getattr(positions, "adopting", ()):
            if recorded["offset"] == 0:
                return 0, "adopting"
            actual = source.anchor_at(recorded["offset"])
            if actual is None:
                return 0, "invalid-anchor"
            return actual["start"], "adopting"
        if "anchor" in recorded:
            if recorded["offset"] == 0:
                return (0, "positioned") if recorded.get("anchor") is None \
                    else (0, "invalid-anchor")
            actual = source.anchor_at(recorded["offset"])
            expected = recorded.get("anchor")
            if actual is None or not _valid_anchor(
                    expected, recorded["offset"]):
                return 0, "invalid-anchor"
            if actual == expected:
                return recorded["offset"], "positioned"
            if actual["start"] == expected["start"]:
                return expected["start"], "rewritten"
            return 0, "invalid-anchor"
        return recorded["offset"], "positioned"
    if since is not None:
        return _source_offset_at_or_after(source, since), "since"
    return 0, "start"


def _resolve_start(path, sid, generation, positions, since):
    """The reader's start rule, pure: `(start, reason)`.

    `reason` is `"positioned"` for a trusted recorded offset, `"since"` for a
    source placed by time, `"start"` for byte zero with nothing recorded, and
    `"replaced"` / `"unmeasurable"` / `"shrunk"` for a recorded offset that
    cannot be trusted — each of those is byte zero too. One function, because
    the backlog `status` and `doctor` report re-derived this rule once and got
    it wrong exactly around migration and recovery (review of 6089336: a
    numeric v1 cursor showed 0 bytes unread while the reader would read 126).
    """
    recorded = (positions or {}).get(sid)
    if recorded:
        # Every untrusted branch returns 0, not the time-window fallback: an
        # offset that cannot be trusted says nothing about what this peer has
        # seen, so the whole source is offered again — bounding that by the
        # lookback would skip everything older than it, a gap, where a repeat
        # is the error this bridge accepts everywhere else.
        if recorded.get("gen") != generation:
            return 0, "replaced"
        size = _source_size(path)
        if size is None:
            return 0, "unmeasurable"
        if recorded["offset"] > size:
            return 0, "shrunk"
        if sid in getattr(positions, "adopting", ()):
            if recorded["offset"] == 0:
                return 0, "adopting"
            actual = _path_anchor_at(path, recorded["offset"])
            if actual is None:
                return 0, "invalid-anchor"
            return actual["start"], "adopting"
        if "anchor" in recorded:
            if recorded["offset"] == 0:
                return (0, "positioned") if recorded.get("anchor") is None \
                    else (0, "invalid-anchor")
            actual = _path_anchor_at(path, recorded["offset"])
            expected = recorded.get("anchor")
            if actual is None or not _valid_anchor(
                    expected, recorded["offset"]):
                return 0, "invalid-anchor"
            if actual == expected:
                return recorded["offset"], "positioned"
            if actual["start"] == expected["start"]:
                return expected["start"], "rewritten"
            return 0, "invalid-anchor"
        return recorded["offset"], "positioned"
    if since is not None:
        return offset_at_or_after(path, since), "since"
    return 0, "start"


# ---------- Claude side ----------

def _claude_slug(cwd):
    """The ~/.claude/projects directory name Claude Code derives from a path.

    Every character that isn't alphanumeric becomes `-`, not just `/`: an
    underscore, a dot and an existing `-` all end up as `-`. Getting this
    wrong is silent — the slug simply names no directory, and the whole
    Claude→Codex direction goes empty forever."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _transcript_cwd_lines(lines):
    """The first project directory recorded in Claude transcript head lines."""
    for line in lines:
        if '"cwd"' not in line:       # cheap pre-filter, skips parsing big lines
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("cwd"), str):
            return d["cwd"]
    return None


def _transcript_cwd(path):
    """Compatibility path helper for isolated discovery tests."""
    return _transcript_cwd_lines(head_lines(path, limit=40))


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
        prefix = os.path.relpath(directory, CLAUDE_PROJECTS)
        transcripts = sorted(glob.glob(os.path.join(directory, "*.jsonl")),
                             key=mtime, reverse=True)
        for path in transcripts[:3]:
            discovered = _discovered_source_path(
                path, CLAUDE_PROJECTS, "claude", prefix)
            opened = _open_discovered_source(discovered, cwd, "claude")
            if isinstance(opened, SourceRefusal):
                continue
            with opened as source:
                recorded = _transcript_cwd_lines(source.head_lines(limit=40))
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
    files = []
    for path in glob.glob(os.path.join(directory, "*.jsonl")):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if info.st_size > 0 or stat.S_ISLNK(info.st_mode):
            files.append((info.st_mtime, path))
    files.sort(key=lambda item: item[0], reverse=True)
    prefix = os.path.relpath(directory, CLAUDE_PROJECTS)
    return [_discovered_source_path(path, CLAUDE_PROJECTS, "claude", prefix)
            for _mtime, path in files]


def claude_events(cwd, positions=None, since=None, visible_record_limit=None,
                  source_paths=None):
    """Return visible events and the safe scanned position for each source.

    A completed JSONL record consumes at most one visible lookahead slot even
    when it contains several text and tool blocks. Filtered records consume no
    slot, so the scanner can pass them to EOF or to the next visible record.
    """
    events = []
    reached = {}
    position = itertools.count()
    paths = (claude_transcripts(cwd)[:RECENT_FILES]
             if source_paths is None else source_paths)
    for path in paths:
        opened = _open_discovered_source(path, cwd, "claude")
        if isinstance(opened, SourceRefusal):
            continue
        with opened as source:
            visible_records = 0
            sid, gen = source.source, source.generation
            offset = _start_source_offset(source, positions, since)
            previous_anchor = source.anchor_at(offset) if offset else None
            if gen is not None:
                reached[sid] = {
                    "gen": gen, "offset": offset,
                    "anchor": previous_anchor}
            for start, end, line, anchor in source.read_anchored_records(offset):
                if gen is not None:
                    reached[sid] = {
                        "gen": gen, "offset": end, "anchor": anchor}
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
                                          Event(ts, "you", text, sid, gen,
                                                start, end, previous_anchor,
                                                anchor)))
                    elif kind == "assistant":
                        for ordinal, c in enumerate(
                                content if isinstance(content, list) else []):
                            if not isinstance(c, dict):
                                continue
                            if c.get("type") == "text":
                                text = c.get("text")
                                if isinstance(text, str) and text != "":
                                    events.append((ts, path, next(position),
                                                  Event(ts, "claude", text,
                                                        sid, gen, start, end,
                                                        previous_anchor, anchor)))
                            elif c.get("type") == "tool_use":
                                invocation = _claude_invocation(
                                    c, sid, start, ordinal)
                                if invocation is None:
                                    continue
                                arguments = invocation.arguments
                                detail = (arguments.get("file_path")
                                          or arguments.get("command")
                                          or arguments.get("pattern") or "")
                                events.append((ts, path, next(position),
                                              Event(ts, "tool",
                                                    f"{invocation.name} {detail}".strip(),
                                                    sid, gen, start, end,
                                                    previous_anchor, anchor,
                                                    invocation.public_id)))
                previous_anchor = anchor
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
        if not isinstance(d, dict) or d.get("type") != "session_meta":
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
        discovered = _discovered_source_path(path, CODEX_SESSIONS, "codex")
        opened = _open_discovered_source(discovered, cwd, "codex", report=False)
        if isinstance(opened, SourceRefusal):
            # A different exact project is an ordinary exclusion. Every other
            # refusal narrows what the old scanner might have found and stays
            # visible until the page-level degraded marker lands in Task 4.
            if opened.reason != "project-mismatch":
                _report_source_refusal("codex", opened)
            continue
        opened.close()
        matched.append(discovered)
    return matched


CODEX_TOOL_ARGUMENT_KEYS = {
    "custom_tool_call": "input",
    "function_call": "arguments",
}


def _codex_tool_fields(payload):
    """Return validated host fields from one measured Codex call shape."""
    kind = payload.get("type")
    argument_key = CODEX_TOOL_ARGUMENT_KEYS.get(kind)
    if argument_key is None or not isinstance(payload.get(argument_key), str):
        return None
    name = payload.get("name")
    if not isinstance(name, str) or TOOL_COMPONENT.fullmatch(name) is None:
        return None
    namespace = payload.get("namespace")
    namespace = (namespace if isinstance(namespace, str)
                 and TOOL_COMPONENT.fullmatch(namespace) is not None else None)
    return kind, name, namespace, payload[argument_key]


def _codex_tool_name(payload):
    """Return the only safe page detail from one measured Codex tool call.

    Codex writes calls as `response_item` records. `custom_tool_call.input`
    and `function_call.arguments` contain the caller's private payload, while
    their output records contain the result; none belongs on a passive page.
    The name is useful without either. A namespace qualifies it only when both
    components can be rendered as one inert line. The retired
    `event_msg/exec_command_begin` shape is deliberately not accepted: no
    current rollout in the measured corpus writes it, and it exposed commands.
    """
    fields = _codex_tool_fields(payload)
    if fields is None:
        return None
    _kind, name, namespace, _arguments = fields
    if namespace is not None:
        return f"{namespace}.{name}"
    return name


def _codex_invocation(payload, source, start, ordinal=0):
    """Extract one real Codex invocation; arguments remain opaque strings."""
    if not isinstance(payload, dict):
        return None
    fields = _codex_tool_fields(payload)
    if fields is None:
        return None
    kind, name, namespace, arguments = fields
    return _make_invocation(
        "codex", kind, name, arguments, source, payload.get("call_id"),
        start, ordinal, namespace=namespace)


def _rejected_codex_tool_shape(payload):
    """Whether one call-like payload is omitted by the shared validator."""
    if not isinstance(payload, dict):
        return False
    kind = payload.get("type")
    call_like = (isinstance(kind, str)
                 and (kind in CODEX_TOOL_ARGUMENT_KEYS
                      or kind.endswith("_call")))
    return call_like and _codex_tool_fields(payload) is None


def _codex_tool_shape_count(cwd, discovery=None, source_paths=None):
    """Rejected call-like Codex records, or None when the count is unproved.

    This is an explicit-doctor full scan, never a hook path. It intentionally
    shares `_codex_tool_fields` with production parsing and reports only an
    aggregate; malformed and unrelated records are not guessed into calls.
    """
    if source_paths is not None:
        paths = tuple(source_paths)
    else:
        if discovery is None:
            cursor, cursor_state = _read_cursor_state(cwd, "claude")
            discovery = _reader_discovery(
                cwd, "claude", cursor, cursor_state)
        if discovery.state != "complete":
            return None
        paths = discovery.sources

    count = 0
    for path in paths:
        opened = _open_discovered_source(path, cwd, "codex", report=False)
        if isinstance(opened, SourceRefusal):
            return None
        with opened as source:
            try:
                for _start, _end, raw in source.read_retrieval_records():
                    try:
                        line = raw.decode("utf-8", "strict")
                    except UnicodeDecodeError:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if (isinstance(record, dict)
                            and record.get("type") == "response_item"
                            and _rejected_codex_tool_shape(
                                record.get("payload"))):
                        count += 1
            except OSError:
                return None
    return count


def codex_events(cwd, positions=None, since=None, visible_record_limit=None,
                 source_paths=None):
    """Return visible events and the safe scanned position for each rollout."""
    events = []
    reached = {}
    position = itertools.count()
    paths = (codex_rollout_files(cwd)[:RECENT_FILES]
             if source_paths is None else source_paths)
    for path in paths:
        opened = _open_discovered_source(path, cwd, "codex")
        if isinstance(opened, SourceRefusal):
            continue
        with opened as source:
            visible_records = 0
            sid, gen = source.source, source.generation
            offset = _start_source_offset(source, positions, since)
            previous_anchor = source.anchor_at(offset) if offset else None
            if gen is not None:
                reached[sid] = {
                    "gen": gen, "offset": offset,
                    "anchor": previous_anchor}
            for start, end, line, anchor in source.read_anchored_records(offset):
                if gen is not None:
                    reached[sid] = {
                        "gen": gen, "offset": end, "anchor": anchor}
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
                                                        start, end, previous_anchor,
                                                        anchor)))
                            elif role == "assistant":
                                events.append((ts, path, next(position),
                                              Event(ts, "codex", text, sid, gen,
                                                    start, end, previous_anchor,
                                                    anchor)))
                    elif kind == "response_item":
                        invocation = _codex_invocation(payload, sid, start)
                        if invocation is not None:
                            tool_name = (f"{invocation.namespace}."
                                         if invocation.namespace else "")
                            tool_name += invocation.name
                            events.append((ts, path, next(position),
                                          Event(ts, "tool", tool_name,
                                                sid, gen, start, end,
                                                previous_anchor, anchor,
                                                invocation.public_id)))
                previous_anchor = anchor
                if len(events) > before:
                    visible_records += 1
                    if (visible_record_limit is not None
                            and visible_records >= visible_record_limit):
                        break
    events.sort(key=lambda item: (
        item[0], item[3].source, item[3].generation or "",
        item[3].offset, item[2]))
    return [item[3] for item in events], reached


def _record_invocations(kind, record, source, start):
    """Extract invocations from one complete record through the page rules."""
    if not isinstance(record, dict):
        return []
    if kind == "claude":
        if record.get("isMeta") or record.get("type") != "assistant":
            return []
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        return [invocation for ordinal, block in enumerate(content)
                for invocation in (
                    _claude_invocation(block, source, start, ordinal),)
                if invocation is not None]
    if kind == "codex" and record.get("type") == "response_item":
        payload = record.get("payload")
        invocation = _codex_invocation(payload, source, start)
        return [] if invocation is None else [invocation]
    return []


def _scan_invocations(cwd, kind, paths, public_id):
    """Scan every trusted record, retaining matches only; unsafe poisons trust."""
    matches = []
    for path in paths:
        opened = _open_discovered_source(path, cwd, kind, report=False)
        if isinstance(opened, SourceRefusal):
            return [], True
        with opened as source:
            try:
                for start, _end, raw in source.read_retrieval_records():
                    try:
                        line = raw.decode("utf-8", "strict")
                    except UnicodeDecodeError:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    matches.extend(
                        invocation for invocation in _record_invocations(
                            kind, record, source.source, start)
                        if invocation.public_id == public_id)
            except OSError:
                return [], True
    return matches, False


def _retrieve_invocation(cwd, public_id, discovery=None, source_paths=None):
    """Resolve one content-bound invocation id without writing project state."""
    match = TOOL_ID.fullmatch(public_id) if isinstance(public_id, str) else None
    if match is None:
        return RetrievalResult(
            "invalid-id", None,
            "the id must match tc1.<kind>.<16 base64url characters>")
    kind = TOOL_CODE_KIND[match.group("kind")]
    if source_paths is not None:
        paths = tuple(source_paths)
        state = "complete"
    else:
        if discovery is None:
            reader = "codex" if kind == "claude" else "claude"
            discovery = _discover_sources(
                cwd, kind, reader, {}, None,
                catalog_snapshot=_catalog_snapshot(cwd, kind))
        if discovery.state == "degraded":
            return RetrievalResult(
                "untrusted", None,
                "project source discovery could not be proved safe")
        paths = discovery.sources
        state = discovery.state
    matches, unsafe = _scan_invocations(cwd, kind, paths, public_id)
    if unsafe:
        return RetrievalResult(
            "untrusted", None,
            "a project source changed or could not be opened safely")
    if len(matches) > 1:
        return RetrievalResult(
            "ambiguous", None,
            "more than one complete invocation matches this id")
    if len(matches) == 1:
        return RetrievalResult("found", matches[0], None)
    incomplete = ("; project source discovery is incomplete"
                  if state == "building" else "")
    return RetrievalResult(
        "unavailable", None,
        "no matching invocation is available" + incomplete)


def _invocation_json(invocation):
    """Render only the public invocation as one deterministic JSON value."""
    return json.dumps(
        _public_invocation(invocation), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"))


RETRIEVE_EXIT_CODES = {
    "invalid-id": 2,
    "unavailable": 3,
    "ambiguous": 4,
    "untrusted": 5,
}


def retrieve(public_id=None):
    """Print a full invocation by public id without moving bridge state."""
    result = _retrieve_invocation(project_dir(), public_id)
    if result.status == "found":
        print(_invocation_json(result.invocation))
        return 0
    print(f"antiphon retrieve: {result.status} — {result.reason}",
          file=sys.stderr)
    return RETRIEVE_EXIT_CODES.get(result.status, 5)


# ---------- summary ----------

LABEL = {"claude": "Claude", "codex": "Codex", "tool": "·"}


# side -> (the other side's key, its display name for headings, its phrasing in notices)
OTHER_SIDE = {
    "claude": ("codex", "Codex", "from Codex"),
    "codex": ("claude", "Claude Code", "from Claude Code"),
}


# What one page build knows about the sessions behind the other side's peers.
# The first two fields preserve the existing label contract; ``states`` is the
# conservative scheduler classification for source ids known to the registry.
SessionJoin = collections.namedtuple(
    "SessionJoin", "aliases unnamed states", defaults=({},))

# A page with nothing to join against: no source is labelled, no envelope line
# fires, and every byte is what it was before any of this existed.
NO_SESSION_JOIN = SessionJoin({}, False, {})


def _source_activity(cwd, kind, identities=None):
    """One read-only live/unknown/dead census for a source kind.

    Labels keep their rolling-compatible registry join. Scheduling is stricter:
    only a current owner fingerprint can prove a session live or dead, process
    lookup failure is unknown, and multiple claims for one source are never
    resolved by directory order.
    """
    endpoints = [record for record in peers._scan(cwd)
                 if record.get("kind") == kind]
    by_name = {record.get("name"): record for record in endpoints}

    claims = {}
    owner_cache = {}
    endpoint_cache = {}
    for session in peers._scan_sessions(cwd, kind):
        source = session.get("session_id")
        if not peers.valid_session_id(source):
            continue
        name = session.get("name")
        owner = peers._owner_of(session)
        owner_state = peers._owner_liveness(owner, owner_cache)
        endpoint = by_name.get(name)
        endpoint_state = (peers._record_liveness(endpoint, endpoint_cache)
                          if endpoint is not None else "unknown")
        joined_live = bool(
            endpoint is not None
            and peers.owner_key_version(owner)
            == peers.PROCESS_FINGERPRINT_VERSION
            and peers._owner_of(endpoint) == owner
            and owner_state == "live"
            and endpoint_state == "live")
        claims.setdefault(source, []).append(
            (name, owner_state, joined_live))

    states = {}
    for source, source_claims in claims.items():
        if len(source_claims) != 1:
            states[source] = "unknown"
        elif source_claims[0][2]:
            states[source] = "live"
        elif source_claims[0][1] == "dead":
            states[source] = "dead"
        else:
            states[source] = "unknown"

    # Existing display identity remains rolling-compatible. It still requires
    # one live endpoint/session join and drops a multiply claimed source.
    label_claims = {}
    unnamed = False
    for record in endpoints:
        if not peers._record_alive(record):
            continue
        name = record.get("name")
        if name == peers.UNNAMED:
            unnamed = True
            continue
        if not peers.valid_name(name):
            continue
        session = peers._session_address(cwd, record)
        if session is not None:
            label_claims.setdefault(session, set()).add(name)
    if kind == "codex":
        # The hook-owned observation has no endpoint by design. Project it
        # through the same positive-lock rule routing and diagnostics use, then
        # feed only its public alias into page labels. The source UUID remains
        # the internal map key because that is what transcript events carry.
        registered = []
        for record in endpoints:
            if not peers._record_alive(record):
                continue
            projected = dict(record)
            if peers._addressless(record):
                projected["address"] = peers._session_address(cwd, record)
            registered.append(projected)
        identities = (identities
                      if identities is not None
                      else _codex_identity_snapshot(cwd, registered))
        for source in identities.live_sessions:
            states[source] = "live"
        for record in identities.automatic:
            label_claims.setdefault(record["address"], set()).add(
                record["name"])
    aliases = {source: next(iter(names))
               for source, names in label_claims.items()
               if len(names) == 1}
    return SessionJoin(aliases, unnamed, states)


def _session_join(cwd, kind):
    """Which sessions of one kind can be named right now, from the registry as
    it stands. Returns `SessionJoin(aliases, unnamed)`.

    `aliases` maps a session id to the alias whose endpoint is live and claims
    it. The join is `peers._session_address`, which the registry already
    implements: liveness on the endpoint, session identity from the hook's
    half, and the owner key between them — a missing record, one with no owner,
    one from a different owner and one whose id is not a canonical UUID all
    read the same way.

    Pure `_scan`-family reads. `read_peers`, `_live_by_kind` and
    `resolve_target` all prune, and a page build that deleted a stale record
    would be answering a question about the registry by changing it — deleting
    exactly what the next `doctor` exists to explain.

    A session two live endpoints claim under different aliases is dropped
    rather than decided: `sorted(os.listdir(...))` order is not an answer, and
    reaching for the likeliest is what the misattribution was. The reserved key
    is filtered by `valid_name`, which refuses it — it is a place in the
    registry, not a name anything may print.

    `unnamed` is whether a live endpoint of this kind holds that reserved key.
    It is read here, once, with everything else: the page's remedy line needs
    it, and the budget loop must never go back to the registry for it.
    """
    return _source_activity(cwd, kind)


def _ordered_records(events):
    """Return completed source records in a source-prefix-preserving merge."""
    grouped = {}
    for event in events:
        key = (event.source, event.generation, event.offset, event.end)
        grouped.setdefault(key, []).append(event)

    streams = {}
    for (source, generation, offset, end), record_events in grouped.items():
        record = Record(record_events[0].time, source, generation, offset, end,
                        tuple(record_events), record_events[0].before_anchor,
                        record_events[0].anchor)
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


def _render_record(record, side, join=None):
    """Render one completed source record without cutting its non-tool text."""
    # A `you`-kind event is what the other side received as input — including
    # host text no human wrote — so the label names the slot, not an author.
    #
    # The session label is per record, not per kind of line. Every line this
    # block renders comes out of one source, so they take the suffix together
    # or none of them does: a labelled agent line beside a bare relayed line
    # from the same rollout would show two speakers where there is one.
    alias = (join or NO_SESSION_JOIN).aliases.get(record.source)
    named = LABEL[OTHER_SIDE[side][0]] + (" ({})".format(alias) if alias else "")
    labels = dict(LABEL, you="To {}".format(named))
    labels[OTHER_SIDE[side][0]] = named
    # The tool line has no `Name:` shape for a suffix to attach to, and it is
    # the majority render on the Codex side — measured, 26 of 40 records there
    # show a tool line and nothing else, so it is the only place their label
    # can go.
    mark = "· ({})".format(alias) if alias else "·"
    pieces = []
    tools = []
    run_kind = None
    run_time = None
    run_texts = []

    def flush_tools():
        if tools:
            pieces.append("  {} {} tool calls: {}".format(
                mark, len(tools), " | ".join(tools)))
            del tools[:]

    def flush_run():
        nonlocal run_kind, run_time, run_texts
        if run_kind is not None:
            clock = datetime.fromtimestamp(run_time).strftime("%H:%M")
            pieces.append("[{}] {}:\n{}".format(
                clock, labels.get(run_kind, run_kind), "\n\n".join(run_texts)))
            run_kind = None
            run_time = None
            run_texts = []

    for event in record.events:
        if event.kind == "tool":
            flush_run()
            public_id = f" [{event.public_id}]" if event.public_id else ""
            tools.append(truncate(event.text, 70) + public_id)
            continue
        flush_tools()
        if run_kind != event.kind:
            flush_run()
            run_kind = event.kind
            run_time = event.time
        run_texts.append(event.text)
    flush_run()
    flush_tools()
    return "\n".join(pieces)


def _record_message_count(record):
    """Count a whole record as one message unless it carries tool calls only."""
    return int(any(event.kind != "tool" for event in record.events))


def _append_page_section(text, section):
    """Separate envelope sections without changing a record's trailing bytes."""
    if not text:
        return section
    if text.endswith("\n"):
        return text + section
    return text + "\n" + section


def _render_page(side, records, has_more, replay_reason, join=None,
                 discovery=None):
    """Render the exact visible envelope whose UTF-8 size is page-bounded."""
    # Over the SELECTED records, never the candidates. The measured shape of a
    # real page is two sources discovered and one delivered — 55 of 60 — and a
    # count taken over the candidates would relabel every one of them for a
    # session whose words are not on the page.
    #
    # A page carries exactly one peer kind (`build_summary` reads the other
    # side's transcripts and nothing else), so "sources on the page" is the
    # whole count; there is no per-kind split to make.
    sources = {record.source for record in records}
    # A label takes BOTH halves: a live claim on the source, and a second
    # source to tell it from. One source is not a "which of these" — there is
    # no ambiguity there for a label to prevent, and naming terminals is the
    # recommended practice, so a claim-only rule would put a permanent suffix
    # on every page of every named session, the ordinary single-pair install
    # included. Dropped here rather than at each decision below, so nothing
    # downstream can label what the count refuses.
    join = join or NO_SESSION_JOIN
    if len(sources) < 2:
        join = NO_SESSION_JOIN
    labelled = {source for source in sources if source in join.aliases}
    name = LABEL[OTHER_SIDE[side][0]]

    other = OTHER_SIDE[side][1]
    text = "## What happened on the {} side (since your last turn)".format(other)
    text = _append_page_section(text, "has_more: {}".format(str(has_more).lower()))
    scope = ("catalogued project sources" if discovery is not None
             else "currently discovered sources")
    text = _append_page_section(text, f"has_more_scope: {scope}")
    if discovery is not None and discovery.state != "complete":
        text = _append_page_section(
            text, f"discovery: {discovery.state} — {discovery.reason}")
    if replay_reason is not None:
        text = _append_page_section(text, REPLAY_NOTICES[replay_reason])
    for record in records:
        text = _append_page_section(text, _render_record(record, side, join))
    if has_more:
        if side == "codex":
            text = _append_page_section(
                text, "More remains; call antiphon_read again or continue on a later turn.")
        else:
            text = _append_page_section(text, "More remains; it will continue on a later turn.")

    if labelled:
        # `labelled` is non-empty only when the count above let a claim
        # through, so this is the same two-source condition and there is only
        # one of it to drift. Anchored on a live claim: sources with none are
        # one terminal's earlier sessions — measured, 8% of this install's
        # pages — and telling their reader that "some of this is old" is noise
        # they learn to skip past.
        text = _append_page_section(text, (
            "This page interleaves {count} {name} sessions; unlabelled blocks "
            "are earlier or unnamed sessions.".format(count=len(sources),
                                                      name=name)))
        if join.unnamed and sources - labelled:
            # Only a Claude endpoint can hold the reserved key —
            # `valid_key("codex", UNNAMED)` is False — so this can only ever
            # appear on a page Codex reads. An unnamed Codex observation is not
            # an endpoint/session claim and therefore cannot label transcript
            # blocks; the page says nothing instead of joining on timing.
            text = _append_page_section(text, (
                "A {name} session is running now with no name; name each "
                "terminal (ANTIPHON_NAME) to tell them apart.".format(name=name)))

    closing = ("This record belongs to the Antiphon bridge — this is what actually "
               "happened there. Do not assume anything that is not in it.")
    # Predicated on the selected events, never on the rendered text: agent text
    # that quotes the label must not make the page assert relayed input.
    if any(event.kind == "you" for record in records for event in record.events):
        relayed = ('Lines marked "To {name}:" are what {name} received as input in its '
                   "own session — relayed here for awareness, not addressed to your "
                   "session. ".format(name=name))
        if labelled:
            # It rides the relayed sentence rather than the page. Measured, 4 of
            # the 6 real labellable pages carry no `you` event at all, and a
            # sentence about what follows a recipient, on a page with no
            # recipient line, is the same defect the relayed-words entry was
            # opened to close.
            relayed += ("A parenthesised session label after the recipient "
                        "names which live session's line it is. ")
        closing = relayed + closing
    return _append_page_section(text, closing)


def _page_frontier(records, selected, scanned):
    """Return offsets that stop at each source's first undelivered record."""
    delivered = {(record.source, record.generation, record.offset, record.end)
                 for record in selected}
    first_remaining = {}
    for record in records:
        key = (record.source, record.generation, record.offset, record.end)
        if key not in delivered:
            first_remaining.setdefault(record.source, record)
    frontier = {}
    for source, position in scanned.items():
        remaining = first_remaining.get(source)
        if remaining is None:
            frontier[source] = dict(position)
        else:
            frontier[source] = dict(
                position, offset=remaining.offset,
                anchor=remaining.before_anchor)
    return frontier


def _build_page(events, scanned, side, replay_reason=None, join=None,
                discovery=None, next_lane="active"):
    """Build one bounded, whole-record page and its safe source frontier."""
    if replay_reason not in REPLAY_NOTICES and replay_reason is not None:
        raise ValueError("unknown replay reason")
    records = _ordered_records(events)
    if not records:
        if not scanned:
            if discovery is not None and discovery.state == "degraded":
                text = _render_page(side, [], False, replay_reason,
                                    NO_SESSION_JOIN, discovery)
                return text, None, 0
            return "", None, 0
        if replay_reason is None:
            return "", PageAdvance(
                dict(scanned), False, None, next_lane), 0
        # No records, so no sources and nothing to label. The join this
        # replay would have used says the same thing; passing the empty one
        # says it where a reader can see it.
        text = _render_page(side, [], False, replay_reason, NO_SESSION_JOIN,
                            discovery)
        return text, PageAdvance(
            dict(scanned), False, replay_reason, next_lane), 0

    activity = (join or NO_SESSION_JOIN).states
    active = [record for record in records
              if activity.get(record.source, "unknown") != "dead"]
    dead = [record for record in records
            if activity.get(record.source, "unknown") == "dead"]
    mixed = bool(active and dead)
    scheduled = next_lane if next_lane in ("active", "dead") else "active"
    if mixed:
        candidates = active if scheduled == "active" else dead
    else:
        candidates = active or dead
    maximum = min(EVENT_LIMIT, len(candidates))
    selected = 0
    text = ""
    for length in range(1, maximum + 1):
        has_more = length < len(candidates) or (mixed and bool(
            dead if scheduled == "active" else active))
        candidate = _render_page(
            side, candidates[:length], has_more, replay_reason,
            join, discovery)
        if len(candidate.encode("utf-8")) <= PAGE_BUDGET:
            selected = length
            text = candidate

    if selected == 0:
        selected = 1
        has_more = selected < len(candidates) or mixed
        text = _render_page(
            side, candidates[:selected], has_more,
            replay_reason, join, discovery)

    chosen = candidates[:selected]
    has_more = selected < len(candidates) or mixed
    frontier = _page_frontier(records, chosen, scanned)
    count = sum(_record_message_count(record) for record in chosen)
    following = ({"active": "dead", "dead": "active"}[scheduled]
                 if mixed else scheduled)
    return text, PageAdvance(
        frontier, has_more, replay_reason, following), count


def build_summary(cwd, side, positions=None, since=None, replay_reason=None,
                  catalog_degraded=False, catalog_snapshot=None):
    """`side` is the side that will READ the summary ('claude' | 'codex').
    Turns what happened on the other side, and what the user said, into
    compact text.

    Returns ``(text, page_advance, message_count)``. The page advance is the
    safe contiguous delivered prefix, plus filtered bytes before the first
    undelivered visible record; it is not the parser's scanned EOF."""
    kind = "codex" if side == "claude" else "claude"
    discovery = _discover_sources(
        cwd, kind, side, positions, since, catalog_snapshot)
    if catalog_degraded:
        discovery = discovery._replace(
            state="degraded", refusals=max(1, discovery.refusals),
            reason="some project sources could not be proved")
    if side == "claude":
        events, reached = codex_events(
            cwd, positions, since, visible_record_limit=EVENT_LIMIT + 1,
            source_paths=discovery.sources)
    else:
        events, reached = claude_events(
            cwd, positions, since, visible_record_limit=EVENT_LIMIT + 1,
            source_paths=discovery.sources)
    expected = {
        (path.candidate.expected_source
         if isinstance(path, DiscoveredSourcePath) else source_id(path))
        for path in discovery.sources
    }
    missing = expected - set(reached)
    if missing:
        discovery = discovery._replace(
            state="degraded",
            refusals=discovery.refusals + len(missing),
            reason="some project sources could not be proved")
    adopting_readable = bool(
        set(reached).intersection(getattr(positions, "adopting", ())))
    if replay_reason is None and adopting_readable:
        replay_reason = "anchor_upgrade"
    # Once per build, and threaded down. `_render_page` runs once per prefix
    # length inside the budget loop — up to `EVENT_LIMIT` times — and the join
    # walks the registry, `ps` and all: measured, 343 ms per turn if it were
    # built there, against a 46 ms whole-page build.
    join = _session_join(cwd, OTHER_SIDE[side][0])
    return _build_page(
        events, reached, side, replay_reason, join, discovery,
        getattr(positions, "next_lane", "active"))


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
    stated_cwd = input_data.get("cwd")
    cwd = os.path.abspath(stated_cwd or project_dir())
    event = input_data.get("hook_event_name") or "UserPromptSubmit"

    # The sweep's moment: immediately after `cwd` is resolved, because there is
    # no store path before that, and before the page is built, because a sweep
    # at a successful tail never runs on the ordinary quiet turn — this
    # function has five exits and `if not text` is the common one. Outside
    # every lock: at 1.32 µs against a missing store and 93.9 µs across 50
    # files the cost is nothing, but a hold across `cursor_lock` was measured
    # at 5,008 ms against a concurrent reader's 2,038 ms of patience.
    #
    # Only on a `cwd` the payload stated. `project_dir()` is the fallback this
    # code deliberately does not trust for anything else, and deleting a
    # person's files off a guess about which project this is would be worse
    # than an unswept store.
    #
    # Its own `try/except` because a non-zero exit suppresses the page: this
    # function spends a paragraph below on why that costs the reader more than
    # any error it could report.
    if stated_cwd:
        try:
            sweep_attachments(cwd)
        except OSError as error:
            print(f"antiphon: the attachment sweep failed: {error}",
                  file=sys.stderr)

    # Every reserve and merge inside this bounded update releases the catalog
    # lock before transcript inspection. The whole update returns before any
    # cursor lock is attempted, keeping the two lock families unnested.
    catalog_ok = _hook_catalog_update(cwd, side, input_data)

    if side == "codex":
        # On every event, not only `SessionStart`. A missed one then costs a
        # turn of routability rather than the whole session's.
        record_codex_session(cwd, input_data.get("session_id"),
                             input_data.get("transcript_path"))
    else:
        # The same field, off the same payload, on the side that used to throw
        # it away. Claude Code installs this hook under `UserPromptSubmit`
        # only, so this is every event this side gets.
        record_claude_session(cwd, input_data.get("session_id"),
                              input_data.get("transcript_path"))

    if event != "UserPromptSubmit":
        # Only a prompt has something for context to attach to. Anything else —
        # `SessionStart`, or an event this version has never heard of — records
        # and returns without a word, rather than emitting a wrapper naming an
        # event that did not happen. The cursor stays where it was too: a
        # summary nobody was shown has not been seen.
        return 0

    # Snapshot the catalog before the cursor transaction. The snapshot owns
    # ordinary Python values, so the shared catalog lock is already released
    # when delivery takes the cursor lock; neither lock family nests.
    source_kind = "codex" if side == "claude" else "claude"
    catalog_snapshot = _catalog_snapshot(cwd, source_kind)

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
            cwd, side, positions, since, replay_reason,
            catalog_degraded=not catalog_ok,
            catalog_snapshot=catalog_snapshot)
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
    record for `push`'s dedupe scoping; see its docstring for the rule. This
    is the stable public name; `push` reads through the pair.
    """
    return _claude_turn(transcript_path)[0]


def _codex_turn(transcript_path, turn_id=None):
    """(text, bound_key) for the turn the Codex Stop hook is reporting on.

    `turn_id` is the hook payload's own id, threaded in from `push()`; a
    missing key, `null`, `""`, or any non-string value all mean "no id" —
    the hook predates the field. A matched `task_started` bounds the turn
    exactly; anything short of a provable boundary falls open to every
    assistant text in the window instead of guessing, because a duplicate of
    an old turn's tail is recoverable and a lost `@claude` marker is not.

    `bound_key` is that matched id, and only that: `""` wherever this reader
    fails open or has no id to match, because there the returned text is not
    a turn's text but a window's. `push` scopes its dedupe fingerprint to it,
    and a key naming a turn the text was never cut to is worse than no key —
    measured, a clipped window whose marker never changes re-delivered it
    once per turn (four sends over four turns) purely because the hook
    reported a new id each time.
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
                # not end it. The only branch that names the turn it read.
                return span_after(i + 1, "task_complete", turn_id), turn_id
        # Case 2: a real id whose start already scrolled out of the window.
        # Binding to a different task_started would attribute this reply to
        # the wrong turn; clipping at a task_complete can cut current-turn
        # text sitting after a closed nested span. Return everything visible.
        return all_visible_texts(), ""

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
        # not even its own task_complete — clips this span. The start's own
        # id is not the key: nothing proves it is the turn the hook is
        # reporting on, which is the whole reason case 1 needs the payload.
        return span_after(last_start + 1), ""
    if any_marker:
        return all_visible_texts(), ""      # an orphan task_complete: same fail-open

    # No task marker at all in the window: today's newest-message behaviour.
    chunks = []
    for d in records:
        texts = message_texts(d)
        if texts:
            chunks = texts
    return "\n".join(chunks).strip(), ""


def last_codex_reply(transcript_path, turn_id=None):
    """Returns the assistant text(s) of the turn the Stop hook is reporting on.

    A thin wrapper over `_codex_turn`, which also names the turn it bound the
    text to for `push`'s dedupe scoping; see its docstring for the rule. This
    is the stable public name; `push` reads through the pair.
    """
    return _codex_turn(transcript_path, turn_id)[0]


def codex_thread_alive(session):
    """Whether a Codex thread is running, read off its writer lock.

    True or False when this Codex keeps per-thread locks; None when it keeps
    none, so a caller can fall back rather than treat every thread as dead.
    Measured on Codex 0.151.0: `thread-writer-locks/<id>.lock` is created and
    held under an exclusive flock when the thread opens and removed when it
    closes — while the rollout file, which discovery reads, appears only on
    the user's first turn (7,832 s later in the measured case). A shared
    non-blocking probe is refused exactly while the writer holds it; the probe
    takes nothing when it succeeds and releases at once.
    """
    if not os.path.isdir(CODEX_THREAD_LOCKS):
        return None
    try:
        fd = os.open(os.path.join(CODEX_THREAD_LOCKS, session + ".lock"),
                     os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as e:
            return e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


CodexObservationSnapshot = collections.namedtuple(
    "CodexObservationSnapshot", "live unknown")

CodexIdentitySnapshot = collections.namedtuple(
    "CodexIdentitySnapshot", "automatic live_sessions unknown conflicts")


def _codex_observation_snapshot(cwd, registered):
    """Unnamed host ids, classified only by positive writer-lock evidence.

    Branch U deliberately cannot prove when the first hook runs. A held exact
    lock proves an observed id is live; every other result is unknown, never a
    corpse or proof of absence. A live named endpoint/session join suppresses
    its older unnamed sighting without deleting another writer's file.
    """
    named = {
        peer.get("address") for peer in registered
        if peer.get("kind") == "codex"
        and peers.valid_name(peer.get("name"))
        and peers.valid_session_id(peer.get("address"))
    }
    live, unknown = [], []
    for record in peers.read_observations(cwd):
        session_id = record.get("session_id")
        if session_id in named:
            continue
        target = live if codex_thread_alive(session_id) is True else unknown
        target.append(session_id)
    return CodexObservationSnapshot(tuple(live), tuple(unknown))


def _codex_identity_snapshot(cwd, registered):
    """Project positively live observations into public automatic peers.

    The projection is read-only. Its route remains the host UUID internally;
    every renderer below receives only the public alias. A complete digest is
    carried beside each projected peer so a 128-bit alias collision or an
    explicit record occupying the same name refuses instead of choosing.
    """
    observations = _codex_observation_snapshot(cwd, registered)
    occupied = {record.get("name") for record in registered
                if peers.valid_name(record.get("name"))}
    by_name = {}
    conflicts = set()
    for session_id in observations.live:
        identity = peers.auto_identity(session_id)
        if not identity:
            continue
        name, digest = identity
        if name in occupied:
            conflicts.add(name)
            continue
        prior = by_name.get(name)
        if prior is not None and prior.get("identity_digest") != digest:
            conflicts.add(name)
            continue
        by_name[name] = {
            "kind": "codex", "name": name, "address": session_id,
            "automatic": True, "identity_digest": digest,
            "started_at": 0.0,
        }
    automatic = tuple(by_name[name] for name in sorted(by_name)
                      if name not in conflicts)
    return CodexIdentitySnapshot(
        automatic, observations.live, observations.unknown,
        tuple(sorted(conflicts)))


def codex_session_id(cwd):
    """The Codex session a bare message goes to.

    The newest *running* session whose rollout records this directory. The
    newest file alone chose wrong, measured: a session opened at 13:03 had no
    rollout until 15:13, so a push at 15:04 was queued into the newest file's
    thread — an empty session from 12:55 that nothing would ever drain.
    Where this Codex keeps no thread locks, the old newest-file rule stands.
    None when nothing can be chosen; `_legacy_target` says why.
    """
    candidates = []
    for path in codex_rollout_files(cwd):
        m = SESSION_ID.search(os.path.basename(path))
        if m:
            candidates.append(m.group(1))
    if not candidates:
        return None
    alive = {sid: codex_thread_alive(sid) for sid in candidates}
    if all(state is None for state in alive.values()):
        return candidates[0]
    for sid in candidates:
        if alive[sid]:
            return sid
    return None


def _retire_park(cwd, side, key, observed):
    """Deletes the parked pairs `observed` on a `push` exit that delivered nothing.

    Only the two returns that reach the cursor stage without a delivery come
    here. The delivered path folds the same deletion into the `mutate` it
    already runs: `cursor_lock` is not reentrant, so a second acquisition from
    inside that one would burn the full patience on every delivered push and
    then retire nothing.

    Nothing observed means no lock at all — the markerless Stop is the hottest
    path on the bridge, and this must not put a write on it for the ordinary
    case where no tool spoke this turn.

    A lost lock exits 1 by `cursor_lock`'s own rule: on exit 0 the host sends
    stderr to a debug log and shows the person nothing, and the notice below
    exists precisely to be seen. `push` injects no context, so its non-zero
    costs nobody a page.
    """
    if not observed:
        return 0

    def mutate(cursor):
        held = cursor.get(key)
        if isinstance(held, dict):
            record = dict(held)
            drop_parked(record, observed)
            cursor[key] = record
        return cursor

    if not update_cursor(cwd, side, mutate):
        print(PARK_LEFT_BEHIND, file=sys.stderr)
        return 1
    return 0


def push(target="codex"):
    """Stop hook: pushes explicit target markers in the latest reply.

    `target=codex` is called from Claude's Stop hook, `target=claude` from
    Codex's Stop hook. Addressing is never inferred from free text — only an
    explicit `@codex` or `@claude` marker at the start of a line triggers a
    push. A marker whose message is `<<TOKEN` carries the explicitly delimited
    multi-line body that follows it; arbitrary prose is never continuation.
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
        # nothing of the kind for `_claude_turn` to use. One read again, and
        # the key is the reader's, not the payload's: the hook id names a
        # turn, the reader says whether the text it returned was actually cut
        # to that turn. Where it was not, there is no key.
        reply_text, turn_key = _codex_turn(transcript,
                                           input_data.get("turn_id"))
    # Every Stop consumes any mid-turn park written for it, including one whose
    # marker syntax is refused before transport. Hoisted so that syntax failure
    # can retire through the same compare-and-clear helper as a markerless Stop
    # without creating a second cursor mutation rule.
    side = sender_side(target)
    key = f"last_pushed_{target}"
    try:
        grouped = group_by_recipient(target, reply_text)
    except MarkerSyntaxError as failure:
        if failure.reason == "unclosed":
            print(f"antiphon: unclosed @{target} block token {failure.token}; "
                  "nothing sent for this turn", file=sys.stderr)
        else:
            print(f"antiphon: invalid multi-line @{target} marker delimiter; "
                  "nothing sent for this turn", file=sys.stderr)
        held = read_cursor(cwd, side).get(key)
        return _retire_park(cwd, side, key, parked_deliveries(held))

    batches = {}
    for recipient, messages in grouped.items():
        # Reported per line, not per recipient: a batch holding one empty marker
        # and one real message is not empty, so a per-batch check would let the
        # empty line disappear without a word.
        for blank in (m for m in messages if not m.strip()):
            # A refusal is still an output surface. Only a recipient that
            # could actually be registered is safe and useful to repeat;
            # invalid values can carry a host session id in decorated form.
            named = (f":{recipient}"
                     if recipient is not None and peers.valid_name(recipient)
                     else "")
            print(f"antiphon: a @{target}{named} line carried no message, "
                  "nothing sent for it", file=sys.stderr)
        said = [m for m in messages if m.strip()]
        if said:
            batches[recipient] = said

    # Hoisted above the markerless return: that return is a cursor-reaching
    # exit now, because a turn that addresses nobody is still the Stop the
    # mid-turn park was written for.
    if not batches:
        return _retire_park(cwd, side, key,
                            parked_deliveries(read_cursor(cwd, side).get(key)))

    # Once for the turn, not once per recipient. Who is speaking cannot change
    # between two lines of one reply, and working it out again for each would
    # walk the process tree again for an answer that is already known.
    who = claimed_alias(cwd, side, input_data.get("session_id"))
    can_reply = reply_available(cwd, side, who)

    def deliver(recipient, messages):
        outgoing = "\n".join(messages)
        attempt = delivery_id()       # but each attempt is its own attempt
        if target == "codex":
            ok, detail = send_to_codex(
                cwd, (f"{PUSH_LABEL} "
                      f"{queue_label(who, attempt, can_reply)} {outgoing}"),
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
            # Redacted at the surface as well as at each birth site: this
            # line prints whatever a send hands back, including a refusal
            # written by the peer's own listener, which is not this side's to
            # keep clean by construction.
            print(f"antiphon: delivery failed — {redact_private(str(detail))}",
                  file=sys.stderr)
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
    # The exact pairs this run may retire, noted before anything is sent. Only
    # these are deleted later, so a park written while the send was in flight
    # keeps its own Stop.
    observed = parked_deliveries(raw_sent)
    before_send = dict(raw_sent)
    if already:
        # The exact bare message already went out under the old string
        # format; nothing left to send for it, but the migration to the new
        # shape still needs recording — through the same scoped helper every
        # other fingerprint in this function goes through, so the shapes
        # cannot drift apart.
        raw_sent[""] = push_fingerprint(turn_key, batches[None])
    for recipient, messages in batches.items():
        # The park's one match. A batch whose *content* is what a tool already
        # delivered this turn is pre-seeded with the fingerprint it would have
        # been recorded under, so `deliver_batches` below recognises it and
        # records it without sending. Seeded here rather than above
        # `before_send`, following the `already` legacy-migration pre-seed:
        # seeded earlier the slot would be equal on both sides of the delta,
        # drop out of `delivered`, and never reach the cursor at all. And
        # deliberately not guarded by `turn_key` the way the flat→scoped
        # migration branch inside `deliver_batches` is — a key-less Stop is
        # exactly the case that echoes its own mid-turn delivery.
        slot = "" if recipient is None else f"@{recipient}"
        if observed.get(slot) == batch_fingerprint(messages):
            raw_sent[slot] = push_fingerprint(turn_key, messages)
    updated = forget_superseded(deliver_batches(batches, raw_sent, deliver, turn_key))
    # The delta: only the slots this call actually resolved — never the whole
    # map computed from the read above. Writing that map back, whole, would
    # reintroduce exactly the lost update the cursor lock exists to prevent:
    # a cursor snapshot must never be carried across the lock boundary, only
    # a fact about what this call just did.
    delivered = {slot: fingerprint for slot, fingerprint in updated.items()
                if before_send.get(slot) != fingerprint}
    if not delivered:
        # Nothing sent, nothing to record — but a park observed above has had
        # its Stop either way, whether the batch matched it, went to somebody
        # else, or was refused by the transport.
        return _retire_park(cwd, side, key, observed)

    def mutate(cursor):
        # `cursor` is what `update_cursor` just read under the lock, moments
        # ago — the only cursor state this may build on. `delivered` is
        # applied on top of *this*, never on top of `raw_sent` above, which
        # may already be stale by the time this runs.
        fresh, _ = migrate_pushed(cursor.get(key), [])
        merged = dict(fresh)
        merged.update(delivered)
        # The retire rides here rather than in a helper of its own: this
        # runs with the cursor lock already held, and `cursor_lock` is not
        # reentrant. Never expressed through `delivered` — that is a
        # slot→fingerprint delta with no vocabulary for a deletion, and
        # `before_send` is a shallow copy sharing this very park, so an
        # in-place edit would vanish from the diff.
        drop_parked(merged, observed)
        cursor[key] = forget_superseded(merged)
        return cursor

    if not update_cursor(cwd, side, mutate):
        # The message already left this process — `deliver` above said so on
        # its own line. What failed is the bookkeeping: the delivery record,
        # and with it whatever retire this write was carrying. Those are two
        # different costs to their reader, so they are two lines rather than
        # one blanket "not a drop" — a surviving park really can suppress a
        # next-turn marker. `push` injects nothing into anyone's context, so
        # returning non-zero here costs nobody the page a hook's non-zero
        # would.
        missed = ", ".join(sorted(
            "(unaddressed)" if slot == "" else slot[1:] for slot in delivered))
        print(f"antiphon: sent to {target} but could not record delivery for "
              f"{missed} in {state_path(cwd, side)}; a duplicate send is "
              "possible next turn", file=sys.stderr)
        if observed:
            print(PARK_LEFT_BEHIND, file=sys.stderr)
        return 1
    return 0


# Private shapes, and the one place that removes them.
#
# Applied *before* truncation, always: a cut taken first can leave half a
# session id behind, and a check that looks for a whole one would then pass
# over the fragment. The public `auto-` alias is deliberately not a private
# shape — it is the useful half of every message here, and the remedy beside it
# is what a person acts on.
_PRIVATE_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# `re.ASCII`, and not for tidiness: `\b` and `\d` are Unicode-aware in a
# Python `str` pattern and ASCII-only in a JS RegExp, so an owner key or a
# digest written immediately after a non-ASCII letter satisfied `\b` on one
# side and not the other. Python was the leakier side, and Python is the one
# that prints to a person's terminal. One flag aligns both classes at once.
# The boundary belongs to the shape, not to the alphabet. `\b` asks whether a
# word character sits beside a word character, so a digest wrapped in letters —
# `x<digest>x` — had no boundary and passed through whole. A hex run's boundary
# is hex: refuse a 64-run that is not part of a longer one. An owner key starts
# with digits, so its boundary is a digit; anything else in front of it is
# somebody's prose and the key still has to go.
_PRIVATE_DIGEST = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])", re.ASCII)
_PRIVATE_OWNER = re.compile(r"(?<!\d)\d+:(?:v\d+:)?[A-Z][a-z]{2} [A-Z][a-z]{2} "
                            r"[ \d]?\d \d{2}:\d{2}:\d{2} \d{4}", re.ASCII)
_PRIVATE_ROUTE = re.compile(r"\S*antiphon-channel-[0-9a-f]+\.sock")


def redact_private(text, limit=None):
    """Remove every private shape, then cut. Never the other way round."""
    if not isinstance(text, str):
        return text
    cleaned = _PRIVATE_ROUTE.sub("<route>", text)
    cleaned = _PRIVATE_OWNER.sub("<owner>", cleaned)
    cleaned = _PRIVATE_DIGEST.sub("<digest>", cleaned)
    cleaned = _PRIVATE_UUID.sub("<session>", cleaned)
    return cleaned if limit is None else cleaned[:limit]


def redact_refusal(refusal, limit=None):
    """Redact a refusal without costing it the class a caller acts on."""
    cleaned = redact_private(str(refusal), limit)
    given = getattr(refusal, "refusal_class", None)
    return _ClassifiedRefusal(cleaned, given) if given else cleaned


class _ClassifiedRefusal(str):
    """A refusal detail that also says which kind of refusal it is.

    A `str` subclass and not a wider return: every caller of `send_to_codex` and
    `send_to_claude` unpacks a pair, and widening that pair was measured at 72
    red tests against zero for this. Nothing downstream sees the annotation —
    `json`, printing, slicing and equality all treat this as the plain string it
    is — which is also why the wrap has to be the outermost call at a birth
    site: any string operation returns a plain `str` and drops the class.

    An attached class means "the sender needs telling where its words still
    travel". Its absence means "leave this message alone", so every refusal that
    already names its own fix stays byte-identical by construction rather than
    by anybody remembering to exclude it.
    """

    __slots__ = ("refusal_class",)

    def __new__(cls, detail, refusal_class):
        refusal = super().__new__(cls, detail)
        refusal.refusal_class = refusal_class
        return refusal


# Measured, per direction: both `reply_to_codex` and `antiphon_send` reach the
# peer's page as a bare tool-name line. Their text arguments are deliberately
# unreachable by both parsers, as are tool results and call ids. What carries
# the words either way is the visible reply, through the passive pages.
# No timing is promised: a page is bounded, so under a backlog the words land
# some turns later, and saying "next turn" would be the same false promise this
# sentence exists to replace.
TOOL_GUIDANCE = ("The peer's pull page will show {seen} of this attempt. Words "
                 "you put in your visible reply travel with the passive pages, "
                 "in order and in full — no delivery step involved.")


def _guided(detail, seen):
    """The detail as its surface should print it: with the guidance, or as-is.

    `seen` is the surface's own measurement, not the direction's: the same
    refusal read by two different readers leaves two different things on a page.
    The join is an em dash because these are two sentences by two authors —
    a bare space ran the host's last word into this one's first.
    """
    if getattr(detail, "refusal_class", None) is None:
        return detail
    return f"{detail} — {TOOL_GUIDANCE.format(seen=seen)}"


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
        return False, _ClassifiedRefusal("codex command not found", "transport")
    except OSError as e:
        # The crash-belt, and it must sit AFTER the arm above: a
        # `FileNotFoundError` is an `OSError` too, and catching the wider one
        # first would swallow "codex command not found".
        #
        # The exec itself was refused. Measured on this machine at a 1.1 MB
        # message: `PermissionError [Errno 13]`, which is neither a
        # `FileNotFoundError` nor a `SubprocessError` — and an uncaught one
        # propagates out of a Stop hook as a traceback in somebody's terminal.
        # The predicate above this transport keeps it from firing on size; the
        # belt is here for every other way an exec can be refused, and for the
        # platform whose errno is not this one.
        return False, _ClassifiedRefusal(
            f"codex queue could not be started: "
            f"{e.strerror or type(e).__name__}", "transport")
    except subprocess.SubprocessError as e:
        return False, _ClassifiedRefusal(f"{type(e).__name__}", "transport")
    if result.returncode != 0:
        # Codex's own words, and it names the thread it refused. Redacted
        # before the cut and not after: `[:200]` taken first leaves the head of
        # a session id on the end of the line, and a whole-shape check reading
        # that fragment finds nothing to remove. The wrap stays outermost —
        # any string operation returns a plain `str` and drops the class.
        return False, _ClassifiedRefusal(
            redact_private(
                (result.stderr or result.stdout or "unknown error").strip(),
                200),
            "transport")
    return True, ""


def claude_socket_path(cwd, alias=None):
    """Deterministic path to one Claude MCP Channel Unix socket."""
    seed = os.path.abspath(cwd)
    if alias:
        seed = f"{seed}\0{alias}"
    key = hashlib.sha256(seed.encode()).hexdigest()[:20]
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
        # Classified where it is born, so nothing downstream has to read the
        # prose to tell it apart from an addressing refusal. It says discovery
        # found no rollout to *address* — which is not a statement about a
        # peer's ability to read: the Codex-side page is built from Claude's
        # transcripts and carries these words either way, measured.
        if codex_rollout_files(cwd):
            # Rollouts exist and none belongs to a running thread. The one
            # that is running may simply not have a rollout yet: Codex writes
            # it on the first user turn, so until then it cannot be found from
            # here, and queueing into a closed thread would strand the words.
            return None, _ClassifiedRefusal(
                "not delivered: the Codex sessions recorded in this directory "
                "are not running, and a running one gets a transcript only on "
                "its first turn — until then it cannot be addressed", "no-peer")
        return None, _ClassifiedRefusal(
            "not delivered: no Codex session found in this directory", "no-peer")
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


ResolvedTarget = collections.namedtuple("ResolvedTarget",
                                        "address detail origin")


def _resolve_target(cwd, kind, alias=None):
    """Which peer a message goes to, including how its address was found.

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
        return ResolvedTarget(
            None, f"not delivered: unknown peer kind {kind!r} (claude | codex)",
            "refusal")

    registered = [peer for peer in peers.read_peers(cwd, kind)
                  if _automatic_ready(cwd, kind, peer)]
    registered_names = ", ".join(
        sorted(p.get("name") or "?" for p in registered))

    if alias is not None:
        if not peers.valid_name(alias):
            if peers.looks_like_session_id(alias):
                return ResolvedTarget(
                    None, ("not delivered: a host session id is diagnostic "
                           "identity, not a usable peer name; restart the "
                           "intended terminal with ANTIPHON_NAME set"),
                    "refusal")
            return ResolvedTarget(
                None, ("not delivered: the supplied recipient is not a usable "
                       "peer name"
                       + (f"; live {kind} peers: {registered_names}"
                          if registered_names else "")),
                "refusal")
        # Every generated name has this fixed prefix. A different valid alias
        # can be decided entirely from the registry and never needs to inspect
        # private observation state; this preserves the exact explicit-name
        # road and keeps an invalid or unrelated recipient census-free.
        if kind != "codex" or not alias.startswith(peers.AUTO_NAME_PREFIX):
            match = [p for p in registered if p.get("name") == alias]
            if not match:
                return ResolvedTarget(
                    None, (f"not delivered: no live {kind} peer named {alias!r}"
                           + (f"; live peers: {registered_names}"
                              if registered_names else "")),
                    "refusal")
            if match[0].get("address") is None:
                return ResolvedTarget(
                    None, (f"not delivered: {alias!r} is live but not yet "
                           "routable — it has not run a turn yet"), "refusal")
            return ResolvedTarget(match[0]["address"], "", "registered")

    identities = (_codex_identity_snapshot(cwd, registered)
                  if kind == "codex"
                  else CodexIdentitySnapshot((), (), (), ()))
    live = registered + list(identities.automatic)
    names = ", ".join(sorted(p.get("name") or "?" for p in live))

    if alias is not None:
        if alias in identities.conflicts:
            return ResolvedTarget(
                None, (f"not delivered: automatic identity collision at "
                       f"{alias!r}; no peer was chosen"), "refusal")
        # Exact, or not at all. A near miss is a different peer.
        match = [p for p in live if p.get("name") == alias]
        if not match:
            return ResolvedTarget(
                None, (f"not delivered: no live {kind} peer named {alias!r}"
                       + (f"; live peers: {names}" if names else "")),
                "refusal")
        if match[0].get("address") is None:
            return ResolvedTarget(
                None, (f"not delivered: {alias!r} is live but not yet routable "
                       "— it has not run a turn yet"), "refusal")
        return ResolvedTarget(match[0]["address"], "", "registered")

    if identities.conflicts:
        return ResolvedTarget(
            None,
            _ClassifiedRefusal(
                "not delivered: automatic peer identity collision; no peer "
                "was chosen", "no-peer"),
            "refusal")

    # Preserve the established addressing-refusal class and byte-for-byte
    # wording when the registry alone already proves ambiguity. Automatic
    # observation peers keep the no-peer class so passive-page guidance still
    # follows a refusal born before a registry endpoint exists.
    if len(live) > 1:
        detail = (f"not delivered: {len(live)} {kind} peers are live "
                  f"({_peer_states(live)}); address one by name")
        if kind == "codex" and identities.automatic:
            detail = _ClassifiedRefusal(detail, "no-peer")
        return ResolvedTarget(
            None, detail, "refusal")

    if not live:
        # Nothing registered at all: the unnamed single pair, exactly as it was
        # before any of this. Not provably unique either, but it is the shipped
        # behaviour of every existing install and breaking it would cost far
        # more than the guess it makes.
        address, detail = _legacy_target(cwd, kind)
        return ResolvedTarget(address, detail, "legacy")
    if kind == "codex":
        if live[0].get("automatic") is True:
            return ResolvedTarget(live[0]["address"], "", "automatic")
        # One named record is not proof of one session. An unnamed session has
        # no routable peer record, and one before its first hook may have no
        # observation either — delivering to the visible peer would be a guess
        # wearing a certainty. The asymmetry stops here: a Claude channel server
        # always registers, named or not, so one live record on that side really
        # is one live peer.
        return ResolvedTarget(
            None, (f"not delivered: {_peer_states(live)} is the only "
                   "registered Codex peer, but unnamed Codex sessions are "
                   "not all observable and cannot be ruled out — address a "
                   "peer by name"), "refusal")
    # Reached only for Claude, whose live records always carry a usable address:
    # the addressless shape is Codex-only and `read_peers` skips every other
    # unusable one.
    return ResolvedTarget(live[0]["address"], "", "registered")


def resolve_target(cwd, kind, alias=None):
    """Public routing answer as the stable `(address, detail)` pair."""
    target = _resolve_target(cwd, kind, alias)
    return target.address, target.detail


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
CHANNEL_CONTROL = "antiphon.channel"
CHANNEL_CONTROL_VERSION = 1


def _request_claude_reassert(cwd, alias):
    """Ask the exact named listener to restore its own endpoint record.

    The request carries no message content. A protocol-shaped reply is still
    only a claim, so success also requires the listener's matching pid and
    deterministic address to appear in the registry. The caller never writes a
    record on another process's behalf.
    """
    if not peers.valid_name(alias):
        return False
    nonce = delivery_id()
    request = {
        "control": CHANNEL_CONTROL,
        "version": CHANNEL_CONTROL_VERSION,
        "action": "reassert",
        "alias": alias,
        "nonce": nonce,
    }
    address = claude_socket_path(cwd, alias)
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(address)
        with sock:
            sock.sendall(json.dumps(request, ensure_ascii=False).encode())
            sock.shutdown(socket.SHUT_WR)
            reply_bytes = b""
            while len(reply_bytes) <= 64 * 1024:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                reply_bytes += chunk
        if len(reply_bytes) > 64 * 1024:
            return False
    except OSError:
        if sock is not None:
            sock.close()
        return False
    try:
        result = json.loads(reply_bytes.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    pid = result.get("pid") if isinstance(result, dict) else None
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and result.get("ok") is True
            and result.get("control") == CHANNEL_CONTROL
            and result.get("version") == CHANNEL_CONTROL_VERSION
            and result.get("action") == "reasserted"
            and result.get("alias") == alias
            and result.get("nonce") == nonce):
        return False
    return any(
        peer.get("name") == alias
        and peer.get("pid") == pid
        and peers._address_of(peer) == address
        for peer in peers.read_peers(cwd, "claude")
    )


def _notify_unregistered_claude(cwd, alias, sender_alias, message_id):
    """Best-effort notice to the exact named socket; never sends the words."""
    request = {
        "content": (
            "Antiphon delivery notice: a direct send was attempted at "
            f"{datetime.now().astimezone().isoformat(timespec='seconds')} for "
            f"Claude alias {alias!r}, but no live endpoint held that name; "
            "the original message was not delivered."
        ),
        "message_id": message_id,
        "sender_alias": sender_alias,
    }
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(claude_socket_path(cwd, alias))
        with sock:
            sock.sendall(json.dumps(request, ensure_ascii=False).encode())
            sock.shutdown(socket.SHUT_WR)
            while sock.recv(8192):
                pass
    except OSError:
        if sock is not None:
            sock.close()


LISTENER_REFUSAL_CLASSES = ("no-peer", "oversize")


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
        # Refused before any socket is opened, so not a transport failure — but
        # the visible reply is exactly where an oversized text still travels
        # whole, since the automatic hook hands an oversized record over without
        # splitting it.
        return False, _ClassifiedRefusal(
            f"message is {len(payload)} bytes; the channel accepts at "
            f"most {MAX_CHANNEL_BYTES}", "oversize")

    deadline = time.monotonic() + CONNECT_PATIENCE
    recovery_attempted = False
    while True:
        # Re-resolved every attempt: a named peer can register in the meantime,
        # which moves the address from the project-wide path to its own.
        target = _resolve_target(cwd, "claude", alias)
        address, detail = target.address, target.detail
        if address is None:
            valid_alias = alias is not None and peers.valid_name(alias)
            # The listener may be alive at its deterministic named socket even
            # though a bad legacy process fingerprint pruned its endpoint. It
            # alone may restore the record; after that, routing resolves again
            # through the registry before any user bytes are sent.
            if valid_alias and not recovery_attempted:
                recovery_attempted = True
                if _request_claude_reassert(cwd, alias):
                    continue
            # `mcp.connect` finishes before channel.mjs publishes its registry
            # claim. With an explicit, valid alias the first lookup can
            # therefore miss the peer altogether, before there is even an
            # address to connect to. That absence is as transient and as
            # indistinguishable from a real outage as ENOENT below. Invalid
            # aliases and bare ambiguity are decisions, not readiness races,
            # and still fail immediately.
            if valid_alias and time.monotonic() < deadline:
                time.sleep(CONNECT_RETRY_DELAY)
                continue
            if valid_alias:
                _notify_unregistered_claude(
                    cwd, alias, sender_alias, request["message_id"])
            # The resolver's words go straight to the MCP tool and to the Stop
            # hook. Cleaned here rather than left to each branch to stay clean
            # on its own — and the class rides through, because "no peer" and
            # "transport" ask a sender for different things.
            return False, redact_refusal(detail)
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
            if target.origin == "legacy" and error.errno == errno.ENOENT:
                return False, _ClassifiedRefusal(
                    "not delivered: no Claude peer is registered for this "
                    "project and its project-wide channel is not running; a "
                    "channel may be running under a name, so address it by "
                    "name", "no-peer")
            return False, _ClassifiedRefusal(
                "Claude MCP Channel is down: "
                f"{error.strerror or type(error).__name__}", "transport")
        break

    # Connected. Nothing past this point is retried: the bytes may already have
    # been accepted, and a second attempt would deliver the message twice.
    try:
        with sock:
            sock.sendall(payload)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError as error:
                # A proved-stale listener answers and withdraws, so its socket
                # can be gone before this half-close. On this platform that
                # raises ENOTCONN — and it sat in the arm below that relabels
                # everything `transport`, discarding a classified answer that
                # is often already in the receive buffer. Telling a sender to
                # blame the socket when the listener said "the identity moved"
                # is the exact substitution the class exists to prevent, so
                # this one failure is swallowed and the read below decides.
                if error.errno not in (errno.ENOTCONN, errno.EPIPE):
                    raise
            reply_bytes = b""
            while len(reply_bytes) < 64 * 1024:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                reply_bytes += chunk
    except OSError as error:
        return False, _ClassifiedRefusal(
            "Claude MCP Channel is down: "
            f"{error.strerror or type(error).__name__}", "transport")
    try:
        result = json.loads(reply_bytes.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, _ClassifiedRefusal(
            "Claude MCP Channel returned an invalid response", "transport")
    if not isinstance(result, dict):
        # Decoded, and not an answer: `[]` and `null` are valid JSON and `.get`
        # on either raises out of a path whose caller is only ever told success
        # or a reason.
        return False, _ClassifiedRefusal(
            "Claude MCP Channel returned an invalid response", "transport")
    if not result.get("ok"):
        # A class the listener supplied is kept. "The socket failed" and "that
        # alias is not this session any more" call for different actions, and
        # only the second tells a sender to reconnect; recasting everything as
        # transport threw that away.
        supplied = result.get("refusal_class")
        # Redact before the cut: a truncation taken first can leave half a
        # session id behind and a whole-shape check would pass over it.
        return False, _ClassifiedRefusal(
            redact_private(str(result.get("error")
                               or "channel delivery failed"), 200),
            supplied if supplied in LISTENER_REFUSAL_CLASSES else "transport")
    return True, ""


# ---------- large direct-message attachments ----------

# Content bytes in all three, header excluded: the cap, the quota and the
# status line count the words a peer actually sent, never the provenance line
# written above them.
ATTACHMENT_MAX = 8 * 1024 * 1024          # bytes one parked message may hold
ATTACHMENT_QUOTA = 64 * 1024 * 1024       # bytes the whole store may hold
ATTACHMENT_TTL = 7 * 24 * 3600            # seconds a parked message survives

# The envelope's opening words. Deliberately not part of the self-injection
# family: `_SELF_INJECTION_PREFIXES` exists so `PUSH_LABEL` and `CHANNEL_LABEL`
# cannot drift apart, and an envelope is not a bridge delivery of its own — on
# the queue road it travels *inside* one, behind the prefix that anchors the
# echo guard.
ATTACHMENT_LABEL = "[Antiphon attachment]"

# The only shape ever counted, swept or unlinked. A `mkstemp` leftover from a
# write that died mid-flight is `.tmpXXXXXX.tmp`, which this refuses — so the
# one foreign entry this feature can create is never swept as though it were an
# attachment, and neither is anything a person drops in the directory.
ATTACHMENT_NAME = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.txt\Z")

# What one parked file's own header says its content weighs. The header is one
# line, so reading it is one short read rather than arithmetic on a size nobody
# can check.
ATTACHMENT_BYTES = re.compile(r"\bbytes=(\d+)\]")


def attachment_dir(cwd):
    """Where this project parks words that would not fit a transport."""
    return os.path.join(cwd, ".antiphon", "messages")


def _sound_store(cwd, create=False):
    """The store as a directory this code owns outright, or None with a word.

    Checked without following a symlink, at the parent and at the leaf, before
    anything is written, counted or unlinked. Measured: with
    `.antiphon/messages` pointed at a directory outside the project,
    `write_attachment` put the words there and `attachment_usage` counted them
    as though they were here. This store's whole premise is that everything
    inside it belongs to this bridge, and a link is somebody else's claim.

    A loose mode found here is tightened rather than trusted:
    `makedirs(..., exist_ok=True)` leaves a pre-existing 0755 directory exactly
    as it found it, and this one holds one side's words for the other. It is
    tightened rather than refused because refusing would take the feature down
    over a mode a `mkdir -p` could have set; a mode that cannot be tightened
    fails closed instead, because the alternative is parking private words
    somewhere the machine can read.
    """
    path = attachment_dir(cwd)
    parent = os.path.dirname(path)
    if os.path.islink(parent) or (os.path.exists(parent)
                                  and not os.path.isdir(parent)):
        print(f"antiphon: the attachment store's parent {parent} is not a "
              "plain directory; nothing was touched", file=sys.stderr)
        return None
    if create:
        try:
            os.makedirs(parent, exist_ok=True)
            if not os.path.exists(path):
                os.mkdir(path, 0o700)
        except FileExistsError:
            # Another peer created it between the test and the call, or the
            # name is a dangling link. The open below decides which.
            pass
        except OSError as error:
            print(f"antiphon: the attachment store could not be created: "
                  f"{error}", file=sys.stderr)
            return None
    try:
        # `O_NOFOLLOW` is the atomic half of the check: a symlink here fails
        # the open rather than being examined and then followed.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        # No store at all is the overwhelming case and not a problem.
        return None
    except OSError as error:
        print(f"antiphon: the attachment store at {path} is not a plain "
              f"directory and was left alone: {error}", file=sys.stderr)
        return None
    try:
        mode = os.fstat(fd).st_mode & 0o777
        if mode & 0o077:
            try:
                os.fchmod(fd, 0o700)
            except OSError as error:
                print(f"antiphon: the attachment store is {mode:04o} and could "
                      f"not be tightened to 0700 ({error}); nothing was parked",
                      file=sys.stderr)
                return None
            print(f"antiphon: the attachment store was {mode:04o}; tightened "
                  "to 0700", file=sys.stderr)
    finally:
        os.close(fd)
    return path


# Beside the store, never inside it: only `{uuid4}.txt` names may appear in the
# directory, and a lock file there would be a foreign entry this code reported
# at itself on every hook.
ATTACHMENT_LOCK_PATIENCE = 2.0        # seconds, as the cursor lock allows


@contextlib.contextmanager
def attachment_lock(cwd):
    """Serializes read-usage → decide → write, and nothing else.

    Yields True when the lock was taken, having already said why not when it
    was not. Measured without it: two processes released from one barrier, a
    1,000-byte quota and two 700-byte messages — both passed the check, five
    rounds out of five, and the store held 1,400. A quota read and the write it
    authorizes are one transaction or they are not a quota.

    Never held across a transport. `push` records the ruling this follows: a
    lock held across a send was measured at a 5,008 ms hold against a
    concurrent reader's own 2,038 ms of patience, and that reader gave up
    having delivered no context at all. The send happens after this block, with
    the lock already released.
    """
    path = os.path.join(cwd, ".antiphon", "messages.lock")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        print(f"antiphon: no attachment lock at {path}: {error}",
              file=sys.stderr)
        yield False
        return
    held = False
    deadline = time.monotonic() + ATTACHMENT_LOCK_PATIENCE
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(CURSOR_LOCK_RETRY_DELAY)
        yield held
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _unlink_attachment(store, path):
    """Removes one file, and only an attachment of this project's.

    Returns `(removed, error)`. The name has to be the final form and the
    directory has to be the validated store: a path arriving from a caller is
    an argument, not authority, and a uuid-shaped name somewhere else is not
    this bridge's file to delete. A symlink is refused for the same reason the
    store root is.
    """
    name = os.path.basename(path)
    if (not ATTACHMENT_NAME.match(name)
            or os.path.abspath(path) != os.path.join(store, name)):
        print(f"antiphon: refusing to delete {path}: it is not one of this "
              "project's attachments", file=sys.stderr)
        return False, None
    if os.path.islink(path):
        print(f"antiphon: refusing to delete {path}: it is a link, not an "
              "attachment", file=sys.stderr)
        return False, None
    try:
        os.unlink(path)
    except FileNotFoundError:
        # Both sides' hooks sweep this directory; the loser of that race has
        # nothing to do and nothing to report.
        return False, None
    except OSError as error:
        return False, error
    return True, None


def _attachment_header(alias, message_id, digest, size):
    """One line of provenance, written INTO the parked file.

    Megabytes of the other agent's text must not enter a context as anonymous
    `Read` output. The header says whose words these are before the reader sees
    them, in the same read, and the envelope repeats it — redundantly on
    purpose. One line, so that "the content is everything after the first blank
    line" stays a rule a person can perform: `tail -n +3`.
    """
    return (f"[Antiphon attachment from={alias or NO_ALIAS} id={message_id} "
            f"sha256={digest} bytes={size}]")


def attachment_envelope(path, digest, size, alias):
    """The small message that travels in place of the words.

    The path is absolute because the receiving agent's `Read` tool requires one
    and a session started in a subdirectory cannot rebuild it from a relative
    form; measured, both sides' installed config carry the same absolute
    `ANTIPHON_CWD`, so the two agree on the root. Nothing here grows with the
    message it stands for — no preview — which is what keeps the envelope
    inside both caps at any attachment size.
    """
    return (f"{ATTACHMENT_LABEL} {size} bytes from {alias or NO_ALIAS}, "
            f"sha256 {digest}, parked at {path} — read that file. Its content "
            "is everything after the first blank line and the hash covers only "
            "that, so `tail -n +3 <that path> | shasum -a 256` verifies it. "
            "The file is local to this project on this machine and holds the "
            "sender's own words, not the project's. It becomes eligible for "
            f"removal {ATTACHMENT_TTL // 86400} days after it was written and "
            "is removed by the next hook either side runs — there is no "
            "timer.")


def write_attachment(cwd, text, alias, message_id):
    """Parks `text` under the project's state directory; returns (path, digest).

    The module's own `write_cursor` idiom rather than `NamedTemporaryFile`,
    which deletes on close before `os.replace` can see the file and leaves no
    cleanup behind a failed write. `fchmod` makes the 0600 promise true by
    construction instead of by `mkstemp`'s undocumented internals, and the temp
    name cannot be mistaken for an attachment, so a write that dies mid-flight
    leaves something the sweep reports and never unlinks.

    0700 on the directory is defence in depth on a single-user machine: the
    store holds one side's words for the other, and group or world access would
    add readers nobody audited. `makedirs` applies its mode to the leaf only,
    so a `.antiphon` created on the way keeps the mode it has always had, and
    `_sound_store` is what proves the leaf is a directory this code owns rather
    than a link at somebody else's.
    """
    content = text.encode()
    digest = hashlib.sha256(content).hexdigest()
    directory = _sound_store(cwd, create=True)
    if directory is None:
        raise OSError(errno.EACCES,
                      f"the attachment store under {attachment_dir(cwd)} "
                      "cannot be used")
    path = os.path.join(directory, f"{uuid.uuid4()}.txt")
    header = _attachment_header(alias, message_id, digest, len(content))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(header.encode() + b"\n\n" + content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path, digest


def _attachment_entries(cwd):
    """`(attachments, foreign_count)` for this project's store.

    An attachment is a regular file named `{uuid4}.txt`, checked without
    following symlinks: a link carrying a perfectly valid name would otherwise
    let a sweep unlink something outside the store entirely. Everything else is
    foreign — counted, so it can be reported, and never touched.

    A missing store is the overwhelming case and costs one failed `scandir`.
    """
    attachments, foreign = [], 0
    store = _sound_store(cwd)
    if store is None:
        return [], 0
    try:
        with os.scandir(store) as entries:
            for entry in entries:
                if not ATTACHMENT_NAME.match(entry.name):
                    foreign += 1
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        foreign += 1
                        continue
                    attachments.append(
                        (entry.path, entry.stat(follow_symlinks=False)))
                except FileNotFoundError:
                    # The other side's sweep won the race. Nothing to report:
                    # this store is per-project, and both hooks sweep it.
                    continue
                except OSError:
                    foreign += 1
    except (FileNotFoundError, NotADirectoryError):
        return [], 0
    except OSError as error:
        print(f"antiphon: the attachment store could not be read: {error}",
              file=sys.stderr)
        return [], 0
    return attachments, foreign


def _attachment_content_bytes(path, size):
    """What one parked file's own header says its content weighs.

    The header is excluded from every count this feature makes, and its length
    varies with the sender's alias, so the number is read back rather than
    derived. A file whose header will not parse is charged its whole size:
    over-charging something nobody can explain is the safe direction.
    """
    try:
        with open(path, "rb") as handle:
            first = handle.readline()
    except OSError:
        return size
    match = ATTACHMENT_BYTES.search(first.decode("utf-8", "replace"))
    if not match:
        return size
    stated = int(match.group(1))
    return stated if 0 <= stated <= size else size


def _mib(size):
    """Bytes as MiB, for the one refusal a person has to act on."""
    return f"{size / (1024 * 1024):.1f} MiB"


def attachment_usage(cwd, now=None):
    """`(count, content bytes, oldest age in seconds, foreign count)`.

    Unexpired files only: a store whose whole content is past its TTL is not
    full, it is unswept, and refusing a send against it would be a lie the next
    hook erases.
    """
    now = time.time() if now is None else now
    attachments, foreign = _attachment_entries(cwd)
    count, size, oldest = 0, 0, 0.0
    for path, info in attachments:
        age = now - info.st_mtime
        if age > ATTACHMENT_TTL:
            continue
        count += 1
        size += _attachment_content_bytes(path, info.st_size)
        oldest = max(oldest, age)
    return count, size, oldest, foreign


def sweep_attachments(cwd, now=None):
    """Deletes every parked attachment past its TTL, naming each one.

    Cheap enough to run on every hook — measured at 1.32 µs against a store
    that does not exist, which is the overwhelming case, and 93.9 µs across 50
    files. Nothing here may raise into a hook's exit code, so every unlink is
    guarded and a file that vanished under a concurrent sweep is simply gone.
    """
    now = time.time() if now is None else now
    store = _sound_store(cwd)
    attachments, foreign = _attachment_entries(cwd)
    for path, info in attachments:
        age = now - info.st_mtime
        if age <= ATTACHMENT_TTL:
            continue
        removed, error = _unlink_attachment(store, path)
        if error is not None:
            print(f"antiphon: an expired attachment could not be deleted: "
                  f"{path}: {error}", file=sys.stderr)
        elif removed:
            print(f"antiphon: attachment expired after {int(age // 86400)} "
                  f"days and was deleted: {path}", file=sys.stderr)
    if foreign:
        # Reported and left. This directory is not a namespace this code owns
        # outright, and a count says enough: a report is not a directory
        # listing, and this line repeats on every hook until somebody looks.
        noun = "entry" if foreign == 1 else "entries"
        print(f"antiphon: {foreign} {noun} in {attachment_dir(cwd)} left "
              "alone — not attachments", file=sys.stderr)


def drop_attachment(cwd, path):
    """Removes a parked file whose message never left, and says so.

    Write, send, and on any non-delivery unlink at once. Without this an agent
    retrying an oversized send against a channel that is down writes one
    full-size orphan per attempt, and a transport outage becomes a permanent
    storage refusal seven days long.

    Through the same validated helper the sweep uses. This runs on a failure
    path, on a path a caller handed over, so it removes an attachment of this
    project's or it removes nothing at all.
    """
    store = _sound_store(cwd)
    if store is None:
        print(f"antiphon: a send failed and its attachment at {path} could not "
              "be removed: this project has no usable attachment store",
              file=sys.stderr)
        return
    removed, error = _unlink_attachment(store, path)
    if error is not None:
        print(f"antiphon: a send failed and its attachment could not be "
              f"removed: {path}: {error}", file=sys.stderr)
    elif removed:
        print(f"antiphon: the send failed, so its attachment was removed: "
              f"{path}", file=sys.stderr)


def attachment_report(cwd, now=None):
    """The `status` line for the parked store. A reader, never a sweeper.

    Never a filename: `status` prints no path or address. A host session id is
    permitted only on its labelled unnamed-observation row; this attachment
    line is not that carve-out. The oldest age renders in whole days
    because two consecutive `status` runs are pinned equal, and a duration
    derived from `now` at any finer grain would make that pin flake.
    """
    parked, held, oldest, foreign = attachment_usage(cwd, now)
    if not parked:
        line = "Attachments:        none parked"
    else:
        days = int(oldest // 86400)
        aged = "today" if days == 0 else f"{days} day{'' if days == 1 else 's'} old"
        line = (f"Attachments:        {parked} parked, {held:,} bytes, "
                f"oldest {aged}")
    if foreign:
        noun = "entry" if foreign == 1 else "entries"
        line += f"; {foreign} other {noun} left alone"
    return line


def _oversized_for_claude(text, alias=None, message_id=None):
    """Whether `send_to_claude`'s cap would refuse `text`.

    The cap's own arithmetic over the cap's own dict. `len(text.encode())` and
    the serialized length are different numbers and not by a constant —
    measured, `"` costs two bytes and every control character six, so 22,000
    control characters serialize past a 131,072-byte cap while their raw length
    is one sixth of it, and the whole 130,982-131,072 ASCII band is over the
    cap while reading as under it. A predicate on the raw length would leave
    exactly that band refusing with the message this feature exists to replace.

    Callers hand over the very alias and `message_id` they will hand the
    transport, so the two cannot disagree by a byte.
    """
    request = {"content": text, "message_id": message_id or delivery_id(),
               "sender_alias": alias}
    payload = json.dumps(request, ensure_ascii=False).encode()
    return len(payload) > MAX_CHANNEL_BYTES


# `codex queue --thread <session> --message <message>` at exec time. Measured
# on this machine: `SC_ARG_MAX` (1,048,576) is one budget shared by argv and
# the environment, and the largest message that execs falls byte for byte as
# the environment grows — 1,044,820 at a 3,191-byte environment, 844,759 at
# 203,244, 444,759 at 603,244 — so no constant bound can be correct. What is
# stable is the per-exec overhead: 496 bytes over three session-id lengths, and
# 504 once the environment is large, the 8-byte spread being alignment.
QUEUE_EXEC_OVERHEAD = 502
# The session id `send_to_codex` will resolve is not known here — this runs
# before the target is picked — and it is a 36-byte uuid in practice. The
# margin covers a longer one many times over.
QUEUE_SESSION_BYTES = 36
# One page. The measured error in the accounting above is 8 bytes; this also
# absorbs a longer session id and any argument the transport gains later. The
# single-argument limit needs no term: measured on Darwin, a 1,047,587-byte
# single argument execs against an ARG_MAX of 1,048,576, so it is not
# separately binding here.
QUEUE_MARGIN = 4096


def _queue_message_limit():
    """The largest message `codex queue` could exec right now, or None.

    Computed at call time and never stored, because the bound moves with this
    process's own environment. None where the budget cannot be read at all,
    which leaves the decision to the crash-belt rather than to a guess.
    """
    try:
        budget = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError):
        return None
    if not isinstance(budget, int) or budget <= 0:
        return None
    # `key=value\0` per entry and `arg\0` per argument, which is what the
    # kernel copies. `surrogateescape` because that is how the environment was
    # decoded, and a bound that raised on an odd variable would be worse than
    # one that counted it.
    environ = sum(len(key.encode("utf-8", "surrogateescape"))
                  + len(value.encode("utf-8", "surrogateescape")) + 2
                  for key, value in os.environ.items())
    argv = sum(len(part) + 1 for part in
               ("codex", "queue", "--thread", "--message"))
    # The session id, and the message's own trailing NUL.
    return (budget - QUEUE_EXEC_OVERHEAD - QUEUE_MARGIN - environ - argv
            - QUEUE_SESSION_BYTES - 1 - 1)


def _oversized_for_queue(message):
    """Whether `codex queue` could not exec `message` as one argument."""
    limit = _queue_message_limit()
    if limit is None:
        return False
    return len(message.encode()) > limit


def _attachable(text):
    """Whether the store may hold `text` at all.

    Above `ATTACHMENT_MAX` the spill does not happen and the send proceeds to
    the refusal it always made: the guidance naming the visible-reply road is
    still the right message for words no store will take, and letting that path
    stand is why no new refusal class is born here.
    """
    return len(text.encode()) <= ATTACHMENT_MAX


_Attachment = collections.namedtuple("_Attachment", "envelope path size")


def _spill(cwd, text, alias, message_id):
    """Parks `text`; returns `(attachment, refusal)` with exactly one filled.

    A refusal here is a plain string, never a `_ClassifiedRefusal`: an attached
    class means "the sender needs telling where its words still travel", and
    these refusals name their own fix instead.

    The lock covers the usage read, the decision and the write, and is released
    before the caller sends: measured, without it two peers released from one
    barrier both passed a quota neither of them had invalidated.
    """
    size = len(text.encode())
    with attachment_lock(cwd) as locked:
        if not locked:
            return None, ("another peer in this project is parking an "
                          "attachment and did not finish within "
                          f"{ATTACHMENT_LOCK_PATIENCE:.0f} seconds; nothing "
                          "was written. Sending again is safe.")
        return _spill_locked(cwd, text, alias, message_id, size)


def _spill_locked(cwd, text, alias, message_id, size):
    """The half of `_spill` that must not interleave with another peer's."""
    parked, held, oldest, _foreign = attachment_usage(cwd)
    if held + size > ATTACHMENT_QUOTA:
        # Nothing is evicted to make room. An unexpired attachment is somebody
        # else's undelivered words, and deleting them to fit these would be
        # exactly the silent loss this store exists to remove. So the refusal
        # reports the state and names the two ways out of it: wait, or clear
        # the directory by hand.
        return None, (
            f"the attachment store is full: {parked} parked attachments hold "
            f"{_mib(held)} of the {_mib(ATTACHMENT_QUOTA)} it may hold, and "
            f"this message needs {_mib(size)} more. Attachments become "
            f"eligible for removal {ATTACHMENT_TTL // 86400} days after they "
            "are written and go on the next hook either side runs — there is "
            f"no timer; the oldest here is {int(oldest // 86400)} days old, "
            f"and {attachment_dir(cwd)} can also be cleared by hand.")
    try:
        path, digest = write_attachment(cwd, text, alias, message_id)
    except OSError as error:
        return None, (f"the message could not be parked in "
                      f"{attachment_dir(cwd)}: {error}")
    return _Attachment(attachment_envelope(path, digest, size, alias), path,
                       size), None


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
    can_reply = input_data.get("sender_reachable") is not False
    message_id = delivery_id()
    label = queue_label(who, message_id, can_reply)
    # The FULL outgoing message is composed first and that is what the
    # predicate measures: the prefix and label are 84 bytes the kernel counts
    # too, and a predicate on the bare text would let them straddle the
    # decision. The spill re-composes through the same path, so the envelope
    # keeps the prefix that anchors the echo guard and the `[from= id=]` a
    # reply is addressed by.
    outgoing, parked = text, None
    composed = f"{CHANNEL_LABEL} {label} {text}"
    if _oversized_for_queue(composed) and _attachable(text):
        attachment, refusal = _spill(cwd, text, who, message_id)
        if refusal is not None:
            print(f"reply: {refusal}", file=sys.stderr)
            return 1
        outgoing, parked = attachment.envelope, attachment
        composed = f"{CHANNEL_LABEL} {label} {outgoing}"
    ok, detail = send_to_codex(cwd, composed, to)
    if not ok:
        if parked is not None:
            drop_attachment(cwd, parked.path)
        # `only a tool-name line`: measured on 123 real `reply_to_codex`
        # records, whose `text` argument no parser path can reach.
        print(f"reply: {_guided(detail, 'only a tool-name line')}",
              file=sys.stderr)
        return 1
    # The BARE text, which is the shape `deliver_batches` compares: push
    # fingerprints bare marker lines, never composed ones, so parking the
    # composed string would leave the park matching nothing.
    _record_delivery(cwd, "codex", outgoing, to)
    return 0


def _record_delivery(cwd, target, text, alias=None):
    """Remembers what was just delivered, in the shape `push` dedupes on.

    Without it a message sent mid-turn through a channel tool arrives twice:
    once from the tool, once more when the same text ends the turn as an
    `@claude` / `@codex` line.

    Per recipient, in the same `""` / `"@alias"` scheme `deliver_batches` uses.
    This used to write a bare string over the whole record, so a delivery to
    `api` erased what `ui` had already been sent and `ui` received it again.

    Parked under `MID_TURN_SLOT`, not written into the live slot itself. The
    digest here is content-only — this path has no turn to scope to — and a
    content-only digest sitting in the slot the next turn is compared against
    swallowed that turn's genuine marker whenever it repeated the wording.
    Parked, it is matched by exactly one Stop hook and deleted there.

    A record from before that scheme is carried forward rather than dropped.
    It holds the joined text, not a digest, so it cannot be converted — a batch
    of two lines and one line joining to the same string are different things —
    and it is kept in its own form for `migrate_pushed` to compare. Dropping it
    would resend the last unaddressed message once.
    """
    side = sender_side(target)
    key = f"last_pushed_{target}"
    slot = "" if alias is None else f"@{alias}"

    def mutate(cursor):
        held = cursor.get(key)
        sent = dict(held) if isinstance(held, dict) else {}
        if isinstance(held, str):
            sent[LEGACY_SLOT] = held
        park = parked_deliveries(sent)
        park[slot] = batch_fingerprint([text])
        sent[MID_TURN_SLOT] = park
        if slot == "":
            # `forget_superseded` looks at the live unaddressed slot, which
            # this path no longer writes. The rule it encodes is about the
            # delivery, not about where the record sits: an unaddressed
            # delivery has superseded the pre-digest string, and keeping both
            # leaves a record nothing clears.
            sent.pop(LEGACY_SLOT, None)
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
    mode = data.get("mode")
    if data.get("identity_digest") is not None and kind == "claude" \
            and mode not in peers.AUTOMATIC_REGISTRATION_MODES:
        # The bridge always knows which kind of claim it is making, so it must
        # always say. The direct API stays usable without a mode; this caller
        # does not.
        print("register_peer: an automatic claim needs initial or reassert",
              file=sys.stderr)
        return 1
    ok, detail = peers.register(project_dir(), kind, name, address,
                                mode=mode,
                                pid=data.get("pid"), owner_key=peers.owner_key(),
                                identity_digest=data.get("identity_digest"))
    if not ok:
        print(f"register_peer: {detail}", file=sys.stderr)
        return 1
    # The fingerprint of the process this endpoint names, returned by the
    # operation rather than read back out of the file it wrote. A caller that
    # re-reads the record to learn what it published has no authority at all:
    # the same bytes anyone could have changed would answer both questions, and
    # the comparison that follows would always agree with itself.
    print(json.dumps({"birth": peers._process_birth(data.get("pid"))}))
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

RETRIEVE_DESCRIPTION = (
    "Read-only, write-free retrieval of the complete tool invocation named by a "
    "tc1 id from the original project transcript. It returns the invocation only, "
    "never the tool result, and does not move a cursor or write bridge state. MCP results "
    "larger than 8000 UTF-8 bytes are refused without truncation; use `antiphon "
    "retrieve <id>` for the full value.")

TOOLS = [{
    "name": "antiphon_read",
    "description": ("Returns one page of what happened on the Claude Code side since "
                    "your last turn, oldest first. When the page ends with "
                    "`has_more: true`, more completed records are already waiting: call "
                    "this tool again, or let later turns drain them. A `has_more: false` "
                    "page with `has_more_scope: catalogued project sources` covers the "
                    "durable project catalog only when it has no `discovery: building` "
                    "or `discovery: degraded` line; either marker means the boundary is "
                    "explicitly incomplete. If the next record alone is larger than an "
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
}, {
    "name": "antiphon_retrieve",
    "description": RETRIEVE_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The 22-character tc1 invocation id shown on a tool line.",
            },
        },
        "required": ["id"],
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
    live — comes back as a tool error before the message transport is opened.
    A valid explicit name with no registry match may probe only that name's
    deterministic socket with the content-free delivery notice; the original
    words never enter that probe and the refusal remains the result.

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
    who = sender_alias(sender)
    # Decided here rather than inside the transport, so the predicate can
    # mirror the cap over the very bytes the cap will measure. There is no
    # prefix on this road: the composition the cap sees is its own JSON
    # serialization, which `_oversized_for_claude` reproduces.
    message_id = delivery_id()
    outgoing, parked = text, None
    if _oversized_for_claude(text, who, message_id) and _attachable(text):
        attachment, refusal = _spill(cwd, text, who, message_id)
        if refusal is not None:
            return _tool_error(f"Not delivered to Claude: {refusal}")
        outgoing, parked = attachment.envelope, attachment
    ok, detail = send_to_claude(cwd, outgoing, to, sender_alias=who,
                                message_id=message_id)
    if not ok:
        if parked is not None:
            drop_attachment(cwd, parked.path)
        # The real Codex call record contributes its safe name only; its input,
        # result and ids are deliberately unreachable by Claude's pull page.
        return _tool_error(f"Not delivered to Claude: "
                           f"{_guided(detail, 'only a tool-name line')}")
    _record_delivery(cwd, "claude", outgoing, to)
    # Naming the peer back is what lets the sender notice it addressed the wrong
    # one. With a single peer there is nothing to distinguish, so the old
    # wording stands.
    where = f"peer {to!r}" if to else "channel"
    if parked is not None:
        # Announced, never silent: the sender asked for a message to be
        # delivered and something else was, so it is told what and where.
        return {"content": [{"type": "text", "text": (
            f"Delivered to the Claude Code {where} as an attachment: the "
            f"message was too large for the channel, so its {parked.size} "
            f"bytes are parked at {parked.path} and an envelope naming that "
            "file went in its place.")}]}
    return {"content": [{"type": "text",
                         "text": f"Delivered to the Claude Code {where}."}]}


def _retrieve_tool(cwd, public_id):
    """Return one bounded MCP result; the CLI is the lossless escape hatch."""
    result = _retrieve_invocation(cwd, public_id)
    if result.status != "found":
        return _tool_error(
            f"antiphon retrieve: {result.status} — {result.reason}")
    rendered = _invocation_json(result.invocation)
    if len(rendered.encode("utf-8")) > PAGE_BUDGET:
        return _tool_error(
            "The complete invocation is larger than the 8000-byte MCP limit; "
            "nothing was returned or truncated. Run `antiphon retrieve "
            f"{public_id}` for the full value.")
    return {"content": [{"type": "text", "text": rendered}]}


def _retrieve_mcp_bridge(public_id=None):
    """Private fixed-argv bridge used by the Node Channel MCP server."""
    print(json.dumps(_retrieve_tool(project_dir(), public_id),
                     ensure_ascii=False, separators=(",", ":")))
    return 0


CLAUDE_IDENTITY_TIMEOUT = 2.0
# Literal, not adjectival. The ceilings bound what is *retained*, so they
# trigger on the byte that would exceed them rather than after a read
# completes; captured pipe bytes therefore stay at or under their sum, and
# every allocation past that stays O(bound) rather than O(child).
CLAUDE_IDENTITY_STDOUT_CEILING = 32 * 1024
CLAUDE_IDENTITY_STDERR_CEILING = 8 * 1024
CLAUDE_IDENTITY_TERMINATE_GRACE = 0.25
CLAUDE_IDENTITY_REAP_WAIT = 0.25

ProbeResult = collections.namedtuple("ProbeResult",
                                     "text captured returncode reason")


def _stop_probe_child(child, isolated):
    """Terminate, then kill, then reap. Bounded at every step.

    The signal widens to the process group only when this process created that
    group. Where `setsid` failed there is no boundary of ours to signal, and
    widening past one we did not create could reach the session this probe
    belongs to. Descendant death is therefore promised only in the isolated
    case, and not claimed in the other.

    The group id is taken once, from the pid, and not looked up again. With
    `start_new_session` the child *is* the group leader, so its pid is the
    group id — but only while it is alive. Re-deriving it with `getpgid` after
    a leader that exits on SIGTERM raises `ProcessLookupError`, the follow-up
    SIGKILL was swallowed with it, and a descendant that ignores SIGTERM
    outlived the probe. That is the promise in the paragraph above going
    unkept in exactly the case it was written for.
    """
    pgid = child.pid if isolated else None

    def signal(sig):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                child.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass

    signal(signal_module.SIGTERM)
    try:
        child.wait(timeout=CLAUDE_IDENTITY_TERMINATE_GRACE)
        reaped = child.returncode
    except subprocess.TimeoutExpired:
        reaped = None
    # A group outlives its leader. Returning as soon as the leader was reaped
    # skipped the SIGKILL entirely, so a descendant that ignores SIGTERM
    # survived the probe — the one case the isolation was created for. With
    # nothing but the child to signal there is nothing left to do; with a group
    # there is, and signalling an empty one costs an ESRCH nobody reads.
    if reaped is not None and pgid is None:
        return reaped
    signal(signal_module.SIGKILL)
    if reaped is not None:
        return reaped
    try:
        child.wait(timeout=CLAUDE_IDENTITY_REAP_WAIT)
    except subprocess.TimeoutExpired:
        # A child that cannot be reaped is reported, never awaited forever.
        return None
    return child.returncode


def _bounded_identity_probe(argv, cwd):
    """Read a child's stdout under a byte ceiling and a deadline.

    `capture_output=True` buffered whatever the child wrote and only then
    compared a length, so the cap bounded nothing: a child that kept writing
    kept being read. This reads incrementally and stops at the byte that would
    cross a ceiling, so a flooding child costs a bounded amount of memory and a
    bounded amount of time whatever it does.
    """
    isolated = True
    try:
        child = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, shell=False)
    except OSError:
        # No session of our own to signal. Run without one and narrow every
        # later signal to the child itself.
        isolated = False
        try:
            child = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False)
        except OSError:
            return ProbeResult(None, b"", None, "spawn-failed")

    streams = {child.stdout: (bytearray(), CLAUDE_IDENTITY_STDOUT_CEILING),
               child.stderr: (bytearray(), CLAUDE_IDENTITY_STDERR_CEILING)}
    selector = selectors.DefaultSelector()
    for stream in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + CLAUDE_IDENTITY_TIMEOUT
    reason = "complete"
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "deadline"
                break
            for key, _events in selector.select(timeout=remaining):
                buffer, ceiling = streams[key.fileobj]
                chunk = key.fileobj.read(4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(buffer) + len(chunk) > ceiling:
                    buffer.extend(chunk[:max(0, ceiling - len(buffer))])
                    reason = "overflow"
                    break
                buffer.extend(chunk)
            if reason != "complete":
                break
    finally:
        selector.close()

    captured = bytes(streams[child.stdout][0])

    def close_streams():
        for stream in (child.stdout, child.stderr):
            with contextlib.suppress(Exception):
                stream.close()

    if reason != "complete":
        returncode = _stop_probe_child(child, isolated)
        close_streams()
        return ProbeResult(None, captured, returncode, reason)
    try:
        returncode = child.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        stopped = _stop_probe_child(child, isolated)
        close_streams()
        return ProbeResult(None, captured, stopped, "deadline")
    close_streams()
    if returncode != 0:
        return ProbeResult(None, captured, returncode, "exit")
    try:
        text = captured.decode("utf-8")
    except UnicodeDecodeError:
        return ProbeResult(None, captured, returncode, "decode")
    return ProbeResult(text, captured, returncode, "complete")


def _claude_auto_identity(cwd):
    """The one Claude session independently proved to own this channel root.

    The host command is a claim, not authority: its interactive record must
    agree with the canonical CLI root found through the process tree and with
    the exact project directory this server was configured for. Generated host
    display metadata is deliberately unread. Every failure returns None and
    leaves the existing unnamed channel behavior intact.
    """
    owner = peers.owner_key()
    if not peers.valid_owner_key(owner):
        return None
    root_pid, _separator, _fingerprint = owner.partition(":")
    if not root_pid.isdigit():
        return None
    probe = _bounded_identity_probe(
        ["claude", "agents", "--json", "--cwd", cwd], cwd)
    if probe.text is None:
        return None
    try:
        records = json.loads(probe.text)
    except (TypeError, ValueError):
        return None
    if not isinstance(records, list):
        return None
    matches = []
    for record in records:
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        session_id = record.get("sessionId")
        if (type(pid) is int and pid == int(root_pid)
                and record.get("kind") == "interactive"
                and record.get("cwd") == cwd
                and peers.valid_session_id(session_id)):
            matches.append(session_id)
    if len(matches) != 1:
        return None
    alias, digest = peers.auto_identity(matches[0])
    return {"alias": alias, "identity_digest": digest}


def _claude_identity_bridge():
    """Private fixed-argv identity bridge used only by ``channel.mjs``."""
    print(json.dumps(_claude_auto_identity(project_dir()),
                     ensure_ascii=False, separators=(",", ":")))
    return 0


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
        print("antiphon: ANTIPHON_NAME is not a usable name "
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
    """Record the hook's explicit join or automatic-identity observation.

    A usable alias writes the hook's half of the peer record: which session is
    behind that alias. Without one, a canonical host id writes private
    observation evidence — never a public alias, address or transcript. A
    separate read-only projection exposes its derived automatic alias only
    while a writer lock positively proves that session live. Returns whether
    either record was written. An unusable id is silent; an observation write
    failure is reported without its id or path, while a named claim refusal
    remains visible because somebody else holds the alias right now.
    """
    alias = peers.explicit_name()
    if not peers.valid_name(alias):
        if peers.configured_name_present():
            # Configured and unusable. Falling through to an automatic identity
            # would substitute one silently for a person who asked to be named,
            # and a stale observation from before this rule would keep
            # projecting, so withdraw this session's own record durably.
            peers.withdraw_observation(cwd, session_id)
            return False
        if not peers.valid_session_id(session_id):
            return False
        try:
            return peers.write_observation(cwd, session_id)
        except Exception as error:
            error_kind = type(error).__name__
            error_number = getattr(error, "errno", None)
            if isinstance(error_number, int):
                error_kind += f" errno {error_number}"
            print("antiphon: unnamed Codex observation could not be recorded "
                  f"({error_kind}); live-session counts "
                  "remain a lower bound.", file=sys.stderr)
            return False
    if not session_id:
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


def _endpoint_owner(cwd, kind, name):
    """The owner key on the endpoint holding an alias, or None if it names none.

    `_scan`, never `read_peers`: this runs on a hook turn's refusal path, and a
    pruning read there would delete a record on the strength of a liveness
    lookup nobody asked for.
    """
    for record in peers._scan(cwd):
        if record.get("kind") == kind and record.get("name") == name:
            return peers._owner_of(record)
    return None


RETIRE_CONTROL_PATIENCE = 0.25
RETIRE_CONTROL_VERSION = 1


def _retire_control(cwd, alias, address):
    """Tell one listener its identity moved. Content-free, bounded, best effort.

    Single-shot and non-patient, and every error is swallowed: the Stop hook is
    the hottest path on the bridge, and the proof has already made routing safe
    without this. The control can delay the hook by at most the patience below,
    which is a cost, but a bounded and known one.

    It carries no message, and it is not authentication: the listener decides
    by re-reading the proof for itself, never by trusting who connected.
    """
    # The envelope the listener actually validates, not one of its own. This
    # sent `{"antiphon": "control", ...}` while `channel.mjs` branches on
    # `control === "antiphon.channel"`, so every wakeup fell through to the
    # content check and was refused as a malformed message — a control nothing
    # anywhere received. The nonce is part of the validated shape and not a
    # secret: there is no shared secret here, and the listener's safety comes
    # from re-reading the proof itself, never from trusting who connected.
    payload = json.dumps({"control": CHANNEL_CONTROL,
                          "version": CHANNEL_CONTROL_VERSION,
                          "action": "identity-retire",
                          "alias": alias,
                          "nonce": delivery_id()}) + "\n"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(RETIRE_CONTROL_PATIENCE)
        try:
            sock.connect(address)
            sock.sendall(payload.encode("utf-8"))
        finally:
            sock.close()
    except Exception:
        return False
    return True


def _wake_retired_listener(cwd, alias):
    """One bounded wakeup to the alias this rotation just made stale.

    Only the previous current alias, never every stale half: waking each one
    would grow hook latency with history, and the others are already inert
    because routing consults the proof.
    """
    for peer in peers.read_peers(cwd, "claude"):
        if peer.get("name") == alias and peer.get("address"):
            with contextlib.suppress(Exception):
                _retire_control(cwd, alias, peer["address"])
            return


def record_claude_session(cwd, session_id, transcript):
    """Writes the Claude hook's half: which session is behind this alias.

    The mirror of `record_codex_session`, through the same `peers.write_session`
    and the same owner key. Two writers, one peer: the channel server owns
    `endpoint.json` and knows the socket, the hook owns `session.json` and knows
    the session id the host hands it.

    Every turn, on purpose. The id belongs to the transcript being written right
    now, and a claim taken once when the channel started would go on naming that
    session through a resume or a fork — putting the live alias on a transcript
    nobody is writing, which is the exact misattribution this join exists to
    end.

    Silent when this session has no usable alias or cannot identify itself, for
    the reason `record_codex_session` gives. Silent too when the endpoint
    holding the alias records no owner: `_owner_of` returns None there and the
    refusal reads as a different owner, but an unreadable owner is evidence of
    nothing — the record is usually this very session's own. Accusing the reader
    of being somebody else, once per prompt, forever, would be worse than saying
    nothing; `doctor` says it once, where somebody is asking.
    """
    requested = peers.explicit_name()
    automatic = not requested
    if requested and not peers.valid_name(requested):
        return False
    identity = peers.auto_identity(session_id) if automatic else None
    if automatic:
        if not identity:
            return False
        alias, identity_digest = identity
    else:
        alias, identity_digest = requested, None
        if not session_id:
            return False
    try:
        owner = peers.owner_key()
        if not owner:
            return False
        if automatic:
            # The proof is current before any join decision is made, so a
            # message addressed to the alias this rotation retires already
            # fails closed even if nothing below succeeds.
            rotation = peers.rotate_identity_proof(cwd, owner, session_id,
                                                   identity_digest)
            prior = rotation.prior if rotation.ok else None
            if prior and prior.get("session_id") != session_id:
                prior_identity = peers.auto_identity(prior.get("session_id"))
                if prior_identity:
                    _wake_retired_listener(cwd, prior_identity[0])
        ok, detail = peers.write_session(cwd, "claude", alias, session_id,
                                         transcript, owner,
                                         identity_digest=identity_digest,
                                         require_endpoint=automatic)
    except Exception as error:
        # Named routing is a layer over a bridge that already works without it.
        if automatic:
            error_kind = type(error).__name__
            error_number = getattr(error, "errno", None)
            if isinstance(error_number, int):
                error_kind += f" errno {error_number}"
            print(f"antiphon: automatic Claude identity could not be joined "
                  f"({error_kind}); this turn remains unnamed.", file=sys.stderr)
        else:
            print(f"antiphon: {alias!r} could not be recorded "
                  f"({type(error).__name__}: {error}); its session cannot be named "
                  "on the other side's pages this turn.", file=sys.stderr)
        return False
    if not ok and _endpoint_owner(cwd, "claude", alias) is not None:
        # The refusal quotes the owner key it would not accept — a pid and this
        # session's own start time, which is diagnostic identity rather than
        # anything the person reading the line can act on. The remedy in the
        # sentence survives the redaction; the key does not.
        print(f"antiphon: {redact_private(str(detail))}", file=sys.stderr)
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
                "serverInfo": {"name": "antiphon",
                               "version": _package_version(_package_root())
                               or "unknown"},
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
                catalog_snapshot = _catalog_snapshot(cwd, "claude")
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
                            cwd, "codex", positions, since, replay_reason,
                            catalog_snapshot=catalog_snapshot)
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
            elif name == "antiphon_retrieve":
                arguments = p.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                _mcp_result(mid, _retrieve_tool(cwd, arguments.get("id")))
            else:
                _mcp_result(mid, _tool_error(f"unknown tool: {name}"))
        elif mid is not None:
            _mcp_result(mid, {})
    return 0


# ---------- setup ----------

# --- what setup writes, as data ---
#
# Every shape below used to be a literal inside one of `setup`'s closures,
# reachable from nothing else. `doctor` has to read back exactly what `setup`
# wrote, and a diagnostic holding its own spelling of a string the writer may
# change is a bridge that reports itself healthy while one direction is silent.
# So the shapes are module data with one spelling each, and both the writer and
# the reader consume them.
#
# Measured before the split: `mcp__antiphon__reply_to_codex` and
# `default_tools_approval_mode` were asserted by no test in the suite, so an
# extraction that dropped either passed all 525 tests.

HOOK_COMMAND = "antiphon hook {side}"
PUSH_COMMAND = "antiphon push {target}"

# What a Codex user reads at the "New hook - review required" prompt.
CODEX_HOOK_LABEL = "Antiphon bridge"

# Without this in `permissions.allow`, Claude is asked to approve the reply tool
# on every single use, and the Claude → Codex direction goes quiet in practice.
REPLY_TOOL_PERMISSION = "mcp__antiphon__reply_to_codex"
RETRIEVE_TOOL_PERMISSION = "mcp__antiphon__antiphon_retrieve"
CLAUDE_TOOL_PERMISSIONS = (
    REPLY_TOOL_PERMISSION,
    RETRIEVE_TOOL_PERMISSION,
)

# The `.mcp.json` server key. `.claude/settings.local.json` allow-lists the same
# name — it is one server, so it is one fact and one spelling.
CHANNEL_SERVER_NAME = "antiphon"

# Paths relative to the project directory, so the writer and the reader cannot
# disagree about which file holds which shape.
CLAUDE_SETTINGS_FILE = os.path.join(".claude", "settings.json")
CLAUDE_LOCAL_SETTINGS_FILE = os.path.join(".claude", "settings.local.json")
CODEX_HOOKS_FILE = os.path.join(".codex", "hooks.json")
CODEX_CONFIG_FILE = os.path.join(".codex", "config.toml")
MCP_CONFIG_FILE = ".mcp.json"

CODEX_MCP_TABLE = "mcp_servers.antiphon"
CODEX_MCP_ENV_TABLE = CODEX_MCP_TABLE + ".env"

ConfigKeys = collections.namedtuple(
    "ConfigKeys",
    "hooks hook_entries hook_type hook_command hook_status "
    "permissions allow mcp_servers enabled_mcp_servers")
CONFIG_KEYS = ConfigKeys(
    "hooks", "hooks", "type", "command", "statusMessage",
    "permissions", "allow", "mcpServers", "enabledMcpjsonServers")

# Each assignment in `[mcp_servers.antiphon]` with the comment that precedes it
# in the written file. The comments belong to the writer alone: `doctor` looks
# up the assignments and never the prose, so re-wording a comment is not drift.
CODEX_TABLE_ASSIGNMENTS = (
    ('command = "antiphon"', ""),
    ('args = ["mcp"]', ""),
    ('default_tools_approval_mode = "approve"',
     "# read-only local bridge; no need to ask on every turn\n"),
    ('env_vars = ["ANTIPHON_NAME"]',
     "# forwarded, not set: the peer name comes from the terminal that\n"
     "# started this session, and Codex does not pass it down otherwise\n"),
)

HookShape = collections.namedtuple("HookShape", "path event command label")


def hook_shapes():
    """Every hook `setup` installs: which file, which event, which command.

    One table, two readers — `setup` installs each row and `doctor` looks each
    row back up in the file it finds. Two enumerations would be two spellings
    of one fact, and the one that drifts is the one nothing runs.

    The Codex pull hook appears twice on purpose. The session id arrives at
    `SessionStart`; `UserPromptSubmit` is the fallback for a CLI that never
    sends one, which makes a peer routable one turn later rather than never.
    """
    return (
        HookShape(CLAUDE_SETTINGS_FILE, "UserPromptSubmit",
                  HOOK_COMMAND.format(side="claude"), None),
        HookShape(CLAUDE_SETTINGS_FILE, "Stop",
                  PUSH_COMMAND.format(target="codex"), None),
        HookShape(CODEX_HOOKS_FILE, "UserPromptSubmit",
                  HOOK_COMMAND.format(side="codex"), CODEX_HOOK_LABEL),
        HookShape(CODEX_HOOKS_FILE, "SessionStart",
                  HOOK_COMMAND.format(side="codex"), CODEX_HOOK_LABEL),
        HookShape(CODEX_HOOKS_FILE, "Stop",
                  PUSH_COMMAND.format(target="claude"), None),
    )


def channel_server_entry(cwd):
    """The `.mcp.json` entry that starts Claude's MCP Channel server.

    `args = ["channel"]`, never `["mcp"]`: the `mcp` server is Codex's side and
    hands out `antiphon_read`. `env` carries the absolute project directory
    because the server is invoked as a bare `antiphon` with no path argument,
    and has no other way to know which project it serves."""
    return {"command": "antiphon", "args": ["channel"],
            "env": {"ANTIPHON_CWD": cwd}}


def codex_env_assignments(cwd):
    """The `[mcp_servers.antiphon.env]` assignments, in written order.

    The value, not just the key: a table left pointing at a renamed directory
    reads another project's registry and delivers nothing, which is a quiet
    bridge with every key present."""
    return [f'ANTIPHON_CWD = "{cwd}"']


def hook_installed(data, shape):
    """Whether one hook row is already present in a parsed settings file.

    The reading `_add_hook` performs when it decides there is nothing to add,
    without the mutation that makes that answer unusable to a read-only caller.
    A shape naming a label demands that label too: a stale one is what a Codex
    user is asked to approve.

    Everything here comes off disk, so every level is type-checked rather than
    indexed — a hand-edited file may hold any shape at all and a diagnostic
    must not raise on one."""
    events = data.get(CONFIG_KEYS.hooks) if isinstance(data, dict) else None
    groups = events.get(shape.event) if isinstance(events, dict) else None
    if not isinstance(groups, list):
        return False
    for group in groups:
        entries = (group.get(CONFIG_KEYS.hook_entries)
                   if isinstance(group, dict) else None)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            if entry.get(CONFIG_KEYS.hook_command) != shape.command:
                continue
            if entry.get(CONFIG_KEYS.hook_type) != "command":
                continue
            if (shape.label is not None
                    and entry.get(CONFIG_KEYS.hook_status) != shape.label):
                continue
            return True
    return False


SECTION_HEADING = "## The Antiphon bridge"

TOOL_RETRIEVAL_RULE = (
    "Every compact tool-call entry carries a 22-character opaque, content-bound "
    "`tc1` id. Call the `antiphon_retrieve` tool with `id=\"<id>\"` for the complete invocation "
    "only, never the tool result. Retrieval is read-only and cursor-neutral; it "
    "reports `invalid-id`, `unavailable`, `ambiguous` or `untrusted` without "
    "inventing content. An MCP value above 8,000 UTF-8 bytes is refused without "
    "truncation; run `antiphon retrieve <id>` for the full invocation. Host "
    "retention or `antiphon sources compact` can make an old id unavailable. Two "
    "copies of one transcript identity inside a host discovery root make retrieval "
    "untrusted; backups outside those roots do not affect it. There is no persistent "
    "invocation index or tombstone, so changed, expired and never-existed ids all "
    "honestly collapse to `unavailable`; binding invocation content into the id "
    "prevents changed bytes from being returned under the old id.")

MULTILINE_MARKER_RULE = (
    "A Stop marker can carry a block: make its one-line message exactly "
    "`<<TOKEN`, where TOKEN matches `[A-Z][A-Z0-9_]{0,31}`, put the body on "
    "following lines, and use an exact `TOKEN` line to close it. Blocks do not "
    "nest and the closer is not Markdown-fence-aware, so choose a token absent "
    "from the body. Marker-looking lines inside the body are content. A malformed "
    "or unclosed block sends nothing from that turn. To send literal text "
    "beginning with `<<`, put it inside a block body.")

LONG_MARKER_RULE = (
    "Use `{tool}` for long content: an oversized direct-tool message can be "
    "parked as an attachment, while an oversized Stop-marker block is refused "
    "and not parked.")

AUTOMATIC_PEER_IDENTITY_RULE = (
    "Without `ANTIPHON_NAME`, Antiphon may derive an automatic `auto-` peer alias "
    "from a canonical host session UUID. Codex publishes one only after its first "
    "hook records that UUID and a writer lock positively proves the session live. "
    "Every census remains `at least N` because sessions before their first hook "
    "may be invisible. "
    "Claude accepts one only from a fixed Claude probe that finds exactly one "
    "interactive record with this session's CLI-root pid and exact project cwd; "
    "the host display name is ignored, and the Claude hook must join the same "
    "endpoint, owner and identity. Probe or hook failure stays `<unnamed>`. "
    "`ANTIPHON_NAME` overrides automatic identity. One positively live automatic "
    "peer can be addressed by alias and is the only automatic case a bare send may "
    "choose; two or more positively live candidates make a bare send refused. "
    "Older or mixed-version peers are never guessed into automatic identity. The "
    "full host session id, identity digest, owner key and socket route stay "
    "private; status, doctor, labels, refusals and errors expose only the "
    "public alias and the remedy beside it. "
    "After a session rotates to a new host session, its old automatic alias "
    "stops resolving at once and its new one is unreachable until a fresh "
    "endpoint exists \u2014 in practice an MCP reconnect. `status` and `doctor` "
    "name the current alias beside that remedy; an identity whose owner cannot "
    "be proved live is counted, never addressed.")

AGENTS_RULE = ("\n## The Antiphon bridge\n\n"
               "You are working alongside Claude Code on this project. What happens on the "
               "other side is injected into your context automatically at the start of each "
               "turn — you don't need to do anything else. It arrives as one page of "
               "completed records, oldest first; a `has_more: true` line means more is "
               "already waiting, so call the `antiphon_read` tool again (or let later turns "
               "drain it) until it reports `has_more: false`. Its "
               "`has_more_scope: catalogued project sources` covers the durable project "
               "catalog only when the page has no `discovery: building` or "
               "`discovery: degraded` line; either marker means discovery is explicitly "
               "incomplete. A page carrying "
               "a replay notice is re-delivering history after an upgrade or cursor "
               "recovery and can contain duplicates; it is complete when the notice "
               "disappears. If the single next record is larger than an ordinary page, "
               "`antiphon_read` refuses it instead of truncating it — nothing is read or marked seen — and the next "
               "automatic prompt hook delivers it whole. That injected context is "
               "project-wide awareness rather than mail addressed to you: it may merge "
               "activity from several project transcripts under one Claude label, so read "
               "it as what is happening nearby. Paging writes `<side>_pages_v4` beside "
               "the preserved v3 sibling. During adoption, at most the v3 frontier's last "
               "record repeats while a content anchor is established. Live and unknown "
               "sources stay in the active lane; only a current process fingerprint can "
               "prove a source dead, and mixed backlog alternates whole pages between "
               "active and dead after successful delivery. Candidate retirement is never "
               "a hook side effect: `antiphon sources compact` explicitly retires only "
               "aged, gone sources every relevant v4 reader proves consumed. Hooks never "
               "retire candidates. " + TOOL_RETRIEVAL_RULE + "\n\n"
               "When Claude wants to tell you something directly, you'll see it as a user "
               "message starting with `[Antiphon bridge] Claude:` (pushed from Claude's Stop "
               "hook) or `[Antiphon channel] Claude:` (a direct reply through the channel) — "
               "either way, these are Claude's words, not the user's. After that prefix comes "
               "`[from=<alias> id=<uuid>]`, naming which Claude peer spoke; when the "
               "same label also contains `reply_to=<unavailable>`, follow the exception "
               "below instead of addressing it back. Otherwise reply with "
               "`antiphon_send(to=<alias>)` or `@claude:<alias>`. A literal "
               "`from=<unnamed>` means that peer has no name and cannot be addressed back — "
               "with only one Claude peer live you can leave the recipient out entirely. The "
               "id names one delivery attempt; nothing routes replies by it. A Claude "
               "alias is its configured identity, not proof that its named return channel "
               "is reachable. If a direct reply is refused, keep that refusal: `antiphon "
               "doctor` can name the broken channel, which needs the Claude session to "
               "restart. If a label carries `reply_to=<unavailable>`, do not reply to its "
               "`from` alias: that channel belongs to a different session. An `Antiphon "
               "delivery notice:` event is a bridge-authored "
               "diagnostic: it carries no original message content and does not turn the "
               "sender's refusal into delivery. Before that refusal, Antiphon makes one "
               "content-free recovery request to the exact named socket. A current listener "
               "can restore its own endpoint; the original words are sent only if the "
               "registry resolves again. An old or unverified listener stays refused and "
               "may need a restart. Doctor only reports this state; it never performs the "
               "recovery.\n\n"
               "When you want to hand Claude a task directly, put `@claude` at the start of a "
               "line in your reply; only that line is sent to the Claude session as an MCP "
               "Channel event. To reach Claude without ending your turn, call the "
               "`antiphon_send` tool instead: it delivers immediately, so Claude can start "
               "working while you carry on, and `antiphon_read` picks up the answer later in "
               "the same turn. " + MULTILINE_MARKER_RULE + " "
               + LONG_MARKER_RULE.format(tool="antiphon_send") + "\n\n"
               "A direct send reaches one peer and is never broadcast. Write `@claude:name`, "
               "or `antiphon_send(to=name)`, whenever more than one Claude peer is live: an "
               "unaddressed send is refused rather than delivered to a guess. For the same "
               "reason, use each peer's automatic alias or start every terminal with a "
               "distinct `ANTIPHON_NAME`; a session whose identity proof failed remains "
               "unaddressable. "
               + AUTOMATIC_PEER_IDENTITY_RULE + "\n\n"
               "A message too large for the channel arrives as an envelope instead of the "
               "words: a line starting with `[Antiphon attachment]` naming an absolute path "
               "under `.antiphon/messages/`, the content's size and its SHA-256. Read that "
               "file. It is on this same machine, in this project, and it holds the peer's "
               "own words rather than the project's — its first line says whose, and the "
               "content is everything after the first blank line, which is what the hash "
               "covers: `tail -n +3 <path> | shasum -a 256`. It becomes eligible for "
               f"removal {ATTACHMENT_TTL // 86400} days after it was written and goes on "
               "the next hook either side runs — there is no timer, so read it rather "
               "than assuming it waits. The two roads differ "
               "here: your own oversized `antiphon_send` is parked the same way and its "
               "result names the file, while an oversized `@claude` line is not parked — it "
               "is refused, and its words travel with your visible reply through the passive "
               "pages instead.\n")

CLAUDE_RULE = ("\n## The Antiphon bridge\n\n"
               "You are working alongside another agent on this project. What happens on the "
               "other side is injected into your context at the start of each turn, as one "
               "page of completed records, oldest first. A `has_more: true` line means more "
               "is waiting; let later turns drain it until `has_more: false`. Its "
               "`has_more_scope: catalogued project sources` covers the durable project "
               "catalog only when the page has no `discovery: building` or "
               "`discovery: degraded` line; either marker means discovery is explicitly "
               "incomplete. That "
               "injected context is project-wide awareness rather than mail addressed to you: "
               "it may merge activity from several project transcripts under one Codex label, "
               "so read it as what is happening nearby. Paging writes `<side>_pages_v4` "
               "beside the preserved v3 sibling. During adoption, at most the v3 frontier's "
               "last record repeats while a content anchor is established. Live and unknown "
               "sources stay in the active lane; only a current process fingerprint can "
               "prove a source dead, and mixed backlog alternates whole pages between active "
               "and dead after successful delivery. Candidate retirement is never a hook "
               "side effect: `antiphon sources compact` explicitly retires only aged, gone "
               "sources every relevant v4 reader proves consumed. Hooks never retire "
               "candidates. " + TOOL_RETRIEVAL_RULE + "\n\n"
               "Events that come directly from that agent are marked "
               "`<channel source=\"antiphon\" sender=\"codex\" sender_kind=\"agent\" "
               "sender_alias=\"...\">`; ordinary events "
               "are the words of the Codex agent, not of the human user. Use the "
               "`reply_to_codex` tool to answer them, passing `sender_alias` back "
               "as `to` whenever it is a name rather than the literal `<unnamed>`. "
               "A bare reply works when no Codex peer is registered, or when one "
               "positively live automatic peer is the only candidate. It is refused "
               "when an explicit named peer or multiple positive candidates are live, "
               "because unnamed sessions before their first hook cannot be ruled out. "
               "A `sender_alias` of "
               "`<unnamed>` means that peer has no name: it cannot be answered by "
               "name, and a bare reply reaches it only where nothing is registered "
               "— passing `<unnamed>` as `to` is the same as leaving it out. Your "
               "valid Claude `ANTIPHON_NAME` is also your configured identity on "
               "outgoing bridge messages, not proof that your named return channel is "
               "reachable. If startup warned that the channel was not acquired, run "
               "`antiphon doctor` and restart this Claude session. If your outgoing label "
               "carries `reply_to=<unavailable>`, do not reply to its `from` alias: that "
               "channel belongs to a different session. An `Antiphon delivery "
               "notice:` event is a bridge-authored diagnostic: it carries no original "
               "message content and does not turn the sender's refusal into delivery. Before "
               "that refusal, Antiphon makes one content-free recovery request to your exact "
               "named socket. A current listener can restore its own endpoint; the original "
               "words arrive only if the registry resolves again. An old or unverified "
               "listener stays refused and may need a restart. Doctor only reports this "
               "state; it never performs the recovery.\n\n"
               "A reply reaches one peer and is never broadcast, and the same holds when you "
               "open the exchange: `@codex:name` at the start of a line addresses one peer, "
               "and an unaddressed line is refused rather than delivered to a guess. For the "
               "same reason, use each peer's automatic alias or start every terminal with a "
               "distinct `ANTIPHON_NAME`; a session before its first hook can still be unseen, "
               "which is why ambiguous bare messages are refused. "
               + AUTOMATIC_PEER_IDENTITY_RULE + " "
               + MULTILINE_MARKER_RULE + " "
               + LONG_MARKER_RULE.format(tool="reply_to_codex") + "\n\n"
               "A message too large for the transport arrives as an envelope instead of the "
               "words: a line starting with `[Antiphon attachment]` naming an absolute path "
               "under `.antiphon/messages/`, the content's size and its SHA-256. Read that "
               "file. It is on this same machine, in this project, and it holds the Codex "
               "agent's own words rather than the project's — its first line says whose, and "
               "the content is everything after the first blank line, which is what the hash "
               "covers: `tail -n +3 <path> | shasum -a 256`. It becomes eligible for "
               f"removal {ATTACHMENT_TTL // 86400} days after it was written and goes on "
               "the next hook either side runs — there is no timer, so read it rather "
               "than assuming it waits. The two roads differ "
               "here: an oversized `reply_to_codex` is parked the same way, while an "
               "oversized `@codex` line is not parked — it is refused, and its words travel "
               "with your visible reply through the passive pages instead.\n")


class ConfigFileError(Exception):
    """A config file exists but can't be read, so it must not be rewritten.

    A trailing comma, a `//` comment or a UTF-8 BOM is enough to make a
    hand-edited settings file unparseable. Overwriting it would silently throw
    away the user's permissions, env, statusLine and every other tool's hooks,
    so `setup` reports the file and leaves it exactly as it found it.

    `reason` is the same finding without the writer's voice. The message is a
    writer's message — "refusing to overwrite it", "run `antiphon setup`
    again" — and a read-only reader that echoed it would claim it had declined
    to do something it was never going to do, and would print the repair twice.
    """

    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason or message


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
            "aside, then run `antiphon setup` again.",
            reason=f"could not be read ({e.strerror or type(e).__name__})") from e
    except UnicodeDecodeError as e:
        raise ConfigFileError(
            f"{path} is not valid UTF-8; refusing to overwrite it. Fix the "
            "file or move it aside, then run `antiphon setup` again.",
            reason="not valid UTF-8") from e
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
            "move it aside, then run `antiphon setup` again.",
            reason=f"not valid JSON ({e.msg}, line {e.lineno} column "
                   f"{e.colno})") from e
    if not isinstance(data, dict):
        raise ConfigFileError(
            f"{path} holds a JSON {type(data).__name__}, not an object; "
            "refusing to overwrite it. Fix the file or move it aside, then "
            "run `antiphon setup` again.",
            reason=f"holds a JSON {type(data).__name__}, not an object")
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


def _config_slot(holder, key, expected, path):
    """Return/create one mutable config container, or refuse the whole file."""
    if key not in holder:
        holder[key] = expected()
    value = holder[key]
    if isinstance(value, expected):
        return value
    noun = "object" if expected is dict else "array"
    actual = type(value).__name__
    raise ConfigFileError(
        f"{path}: `{key}` holds a JSON {actual}, not an {noun}; refusing to "
        "overwrite it. Fix the file or move it aside, then run `antiphon "
        "setup` again.",
        reason=f"`{key}` holds a JSON {actual}, not an {noun}")


def _config_dict(holder, key, path):
    return _config_slot(holder, key, dict, path)


def _config_list(holder, key, path):
    return _config_slot(holder, key, list, path)


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
        entries = group.get(CONFIG_KEYS.hook_entries)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if (isinstance(entry, dict)
                    and entry.get(CONFIG_KEYS.hook_command) == command):
                if seen:
                    dropped = True
                    continue          # a duplicate of one we already keep
                seen = True
            kept.append(entry)
        if len(kept) == len(entries):
            continue
        group[CONFIG_KEYS.hook_entries] = kept
        if not kept:
            emptied.append(id(group))
    if emptied:
        hooks[:] = [group for group in hooks if id(group) not in emptied]
    return dropped


def _validate_hook_groups(groups, event=None):
    """Refuse a malformed hook array before any setup pass writes its file."""
    where = f"hook event `{event}`" if event is not None else "hook event"
    if not isinstance(groups, list):
        raise ConfigFileError(
            f"{where} is not an array; refusing to overwrite it. Fix the "
            "file or move it aside, then run `antiphon setup` again.")
    for group in groups:
        if not isinstance(group, dict):
            raise ConfigFileError(
                f"{where} contains a non-object group; refusing to overwrite "
                "it. Fix the file or move it aside, then run `antiphon "
                "setup` again.")
        missing = object()
        entries = group.get(CONFIG_KEYS.hook_entries, missing)
        if entries is missing:
            continue
        if not isinstance(entries, list):
            raise ConfigFileError(
                f"a hook group has non-array `{CONFIG_KEYS.hook_entries}`; "
                "refusing to overwrite it. Fix the file or move it aside, "
                "then run `antiphon setup` again.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigFileError(
                    f"a `{CONFIG_KEYS.hook_entries}` array contains a "
                    "non-object entry; refusing to overwrite it. Fix the file "
                    "or move it aside, then run `antiphon setup` again.")
            command = CONFIG_KEYS.hook_command
            if command in entry and not isinstance(entry[command], str):
                raise ConfigFileError(
                    f"a hook entry has non-string `{command}`; refusing to "
                    "overwrite it. Fix the file or move it aside, then run "
                    "`antiphon setup` again.")


def _validate_hook_events(events):
    """Validate every event so two setup passes cannot partly rewrite a file."""
    for event, groups in events.items():
        _validate_hook_groups(groups, event)


def _add_hook(hooks, command, legacy_commands=None, label=None):
    """Adds the command to one event's list; does nothing if it's already there.

    `hooks` is the list for a single event, so the same command installed under
    two events is two calls and stays exactly one entry under each.

    If `legacy_commands` is given, upgrade those first — otherwise, once the
    side argument gets added, the old entry would stick around and the hook
    would fire twice."""
    _validate_hook_groups(hooks)

    changed = False
    if legacy_commands:
        if isinstance(legacy_commands, (str, re.Pattern)):
            legacy_commands = [legacy_commands]
        for group in hooks:
            for entry in group.get(CONFIG_KEYS.hook_entries) or []:
                current = entry.get(CONFIG_KEYS.hook_command, "")
                matched = any(
                    (candidate.fullmatch(current) if isinstance(candidate, re.Pattern)
                     else current == candidate)
                    for candidate in legacy_commands
                )
                if matched:
                    entry[CONFIG_KEYS.hook_type] = "command"
                    entry[CONFIG_KEYS.hook_command] = command
                    if label:
                        entry[CONFIG_KEYS.hook_status] = label
                    changed = True
    if _dedupe_hooks(hooks, command):
        changed = True
    for group in hooks:
        for entry in group.get(CONFIG_KEYS.hook_entries) or []:
            if entry.get(CONFIG_KEYS.hook_command) == command:
                if entry.get(CONFIG_KEYS.hook_type) != "command":
                    entry[CONFIG_KEYS.hook_type] = "command"
                    changed = True
                if label and entry.get(CONFIG_KEYS.hook_status) != label:
                    entry[CONFIG_KEYS.hook_status] = label
                    changed = True
                return changed
    new_entry = {CONFIG_KEYS.hook_type: "command",
                 CONFIG_KEYS.hook_command: command}
    if label:
        new_entry[CONFIG_KEYS.hook_status] = label
    hooks.append({CONFIG_KEYS.hook_entries: [new_entry]})
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
    table = "".join(f"{comment}{line}\n"
                    for line, comment in CODEX_TABLE_ASSIGNMENTS)
    env = "".join(f"{line}\n" for line in codex_env_assignments(cwd))
    return (f'[{CODEX_MCP_TABLE}]\n{table}'
            f'\n[{CODEX_MCP_ENV_TABLE}]\n{env}')


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

    # Every command, event and label comes from the shared table; only the
    # legacy spellings to upgrade and the lines to print belong to the writer.
    claude_pull, claude_push, codex_pull, codex_session, codex_push = hook_shapes()

    # --- Claude Code side: .claude/settings.json ---
    claude_target = os.path.join(cwd, CLAUDE_SETTINGS_FILE)
    legacy_commands = _legacy_commands(script, "kanca", "claude")

    def claude_mutate(data):
        events = _config_dict(data, CONFIG_KEYS.hooks, claude_target)
        _validate_hook_events(events)
        hooks = _config_list(events, claude_pull.event, claude_target)
        changed = _add_hook(hooks, claude_pull.command, legacy_commands)
        permissions = _config_dict(
            data, CONFIG_KEYS.permissions, claude_target)
        allowed = _config_list(
            permissions, CONFIG_KEYS.allow, claude_target)
        for permission in CLAUDE_TOOL_PERMISSIONS:
            if permission not in allowed:
                allowed.append(permission)
                changed = True
        return changed

    install(claude_target, claude_mutate,
            "Claude hook installed", "Claude hook already installed")

    # --- Claude side: push to Codex (Stop hook) ---
    legacy_push_commands = _legacy_commands(script, "it", "codex")

    def push_mutate(data):
        events = _config_dict(data, CONFIG_KEYS.hooks, claude_target)
        _validate_hook_events(events)
        hooks = _config_list(events, claude_push.event, claude_target)
        return _add_hook(hooks, claude_push.command, legacy_push_commands)

    install(claude_target, push_mutate,
            "Push-to-Codex hook installed (Stop)",
            "Push-to-Codex hook already installed")

    # --- Codex side: .codex/hooks.json (same contract, same body) ---
    codex_target = os.path.join(cwd, CODEX_HOOKS_FILE)
    legacy_codex_commands = _legacy_commands(script, "kanca", "codex")

    def codex_mutate(data):
        events = _config_dict(data, CONFIG_KEYS.hooks, codex_target)
        _validate_hook_events(events)
        hooks = _config_list(events, codex_pull.event, codex_target)
        return _add_hook(hooks, codex_pull.command, legacy_codex_commands,
                         label=codex_pull.label)

    install(codex_target, codex_mutate,
            "Codex hook installed", "Codex hook already installed")

    # The Codex session id arrives at SessionStart, so the same command is
    # installed there too (the second Codex pull row above). Under both events
    # is also the fallback: if SessionStart is missed — an older CLI, a config
    # predating this — the first prompt records the session instead, and a peer
    # becomes routable one turn later rather than never. SessionEnd is
    # deliberately not installed: it can be delayed or missed, so nothing may
    # depend on it.
    def codex_session_mutate(data):
        events = _config_dict(data, CONFIG_KEYS.hooks, codex_target)
        _validate_hook_events(events)
        hooks = _config_list(events, codex_session.event, codex_target)
        return _add_hook(hooks, codex_session.command, label=codex_session.label)

    install(codex_target, codex_session_mutate,
            "Codex session hook installed (SessionStart)",
            "Codex session hook already installed")

    # --- Codex side: push to Claude (Stop hook) ---
    legacy_reverse_push_commands = _legacy_commands(script, "it", "claude")

    def reverse_push_mutate(data):
        events = _config_dict(data, CONFIG_KEYS.hooks, codex_target)
        _validate_hook_events(events)
        hooks = _config_list(events, codex_push.event, codex_target)
        return _add_hook(hooks, codex_push.command, legacy_reverse_push_commands)

    install(codex_target, reverse_push_mutate,
            "Push-to-Claude hook installed (Stop)",
            "Push-to-Claude hook already installed")

    # --- Codex side: the antiphon_read MCP tool (.codex/config.toml) ---
    codex_config = os.path.join(cwd, CODEX_CONFIG_FILE)
    written = _update_codex_config(codex_config, cwd)
    print(f"{'✓' if written else '·'} Codex MCP tool "
          f"{'registered' if written else 'already registered'}: {codex_config}")

    # --- Claude Code MCP Channel ---
    mcp_target = os.path.join(cwd, MCP_CONFIG_FILE)
    channel_config = channel_server_entry(cwd)

    def mcp_mutate(data):
        servers = _config_dict(data, CONFIG_KEYS.mcp_servers, mcp_target)
        if servers.get(CHANNEL_SERVER_NAME) == channel_config:
            return False
        servers[CHANNEL_SERVER_NAME] = channel_config
        return True

    install(mcp_target, mcp_mutate,
            "Claude MCP Channel registered", "Claude MCP Channel already registered")

    # Claude Code may also keep .mcp.json servers in a local allowlist.
    local_target = os.path.join(cwd, CLAUDE_LOCAL_SETTINGS_FILE)

    def local_mutate(data):
        enabled = _config_list(
            data, CONFIG_KEYS.enabled_mcp_servers, local_target)
        if CHANNEL_SERVER_NAME in enabled:
            return False
        enabled.append(CHANNEL_SERVER_NAME)
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
    print("\n— More than one terminal? Explicit names are recommended:")
    print("  ANTIPHON_NAME=ui claude --dangerously-load-development-channels server:antiphon")
    print("  ANTIPHON_NAME=build codex")
    print("  Antiphon may assign an automatic auto- alias after host identity is proved.")
    print("  ANTIPHON_NAME overrides it and remains the clearest choice for several")
    print("  terminals; a bare send is refused when more than one candidate is live.")
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


def _codex_census_line(registered, identities):
    """One honest lower-bound census; branch U can never print an exact zero."""
    total = len(registered) + len(identities.live_sessions)
    line = (f"Codex session census: at least {total} live observed; additional "
            "sessions before their first hook may be invisible")
    unknown = len(identities.unknown)
    if unknown:
        noun = "observation" if unknown == 1 else "observations"
        verb = "has" if unknown == 1 else "have"
        line += f"; {unknown} stored {noun} {verb} unknown liveness"
    return line


def _peer_report(live, identities=None):
    """The `Peers:` block and the addressing hints under it, as lines.

    Empty when neither a registered nor projected automatic peer exists. The
    lower-bound census is printed separately even in that case; zero
    observations never proves zero sessions.
    """
    identities = identities or CodexIdentitySnapshot((), (), (), ())
    if not (live["claude"] or live["codex"] or identities.conflicts):
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
    for name in identities.conflicts:
        lines.append(f"  Codex automatic identity collision at {name} — not "
                     "addressable")

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
    automatic = [peer for peer in live["codex"]
                 if peer.get("automatic") is True]
    if len(live["codex"]) == 1 and automatic:
        name = automatic[0].get("name")
        lines.append(f"  → automatic peer: @codex:{name}; a bare @codex line "
                     "also reaches it while it is the only positive candidate")
    elif live["codex"] or identities.conflicts:
        named = ", ".join(f"@codex:{name}" for name in addressable("codex"))
        if named:
            lines.append(f"  → a bare @codex line is refused; address a named "
                         f"peer: {named}")
        else:
            lines.append("  → a bare @codex line is refused until the automatic "
                         "identity collision is resolved")
    return lines


_STATUS_SEEN_KEYS = frozenset(("claude_seen", "codex_seen"))
_STATUS_PAGE_KEYS = frozenset(("claude_pages", "codex_pages"))
_STATUS_V4_PAGE_KEYS = frozenset(("claude_pages_v4", "codex_pages_v4"))
_STATUS_CURSOR_KEYS = (_STATUS_SEEN_KEYS | _STATUS_PAGE_KEYS
                       | _STATUS_V4_PAGE_KEYS
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
    if key in _STATUS_V4_PAGE_KEYS:
        if _valid_v4_page_cursor(value):
            anchored = len(value["sources"])
            adopting = len(value["adopting_v3"])
            return (f"{anchored} anchored "
                    f"source{'' if anchored == 1 else 's'}; "
                    f"{adopting} adopting v3; next lane {value['next_lane']}")
        return "invalid cursor state"
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


def _backlog_line(key, backlog):
    """`unread <key>: …` for status — raw bytes, sources met and not, and
    the way out when the reader is replaying."""
    if backlog is None:
        return (f"unread {key}: unknown (the cursor could not be trusted; "
                "the next turn replays)")
    unread, positioned, unpositioned, replay = backlog
    line = (f"unread {key}: {unread:,} raw bytes across "
            f"{positioned + unpositioned} "
            f"source{'' if positioned + unpositioned == 1 else 's'}")
    if unpositioned:
        line += f"; {unpositioned} not yet positioned"
    if replay:
        line += " — replaying history; `antiphon catch-up` skips it"
    return line


def _reader_discovery(cwd, side, cursor, cursor_state="valid"):
    """The catalog view for one reader and that reader's own position map."""
    positions, since, _replay = positions_for(cursor, side, cursor_state)
    kind = "codex" if side == "claude" else "claude"
    return _discover_sources(cwd, kind, side, positions, since)


def _catalog_status_line(side, discovery):
    """Aggregate catalog truth without transcript paths or source identities."""
    return (f"source catalog {page_cursor_key(side)}: {discovery.state}; "
            f"{len(discovery.sources)} readable; {discovery.pending} pending; "
            f"{discovery.refusals} refused; {discovery.gone} gone")


def _source_activity_line(cwd, side, discovery, positions=None,
                          identities=None):
    """Aggregate scheduler evidence for readable sources, never identities."""
    kind = "codex" if side == "claude" else "claude"
    join = _source_activity(cwd, kind, identities)
    counts = {"live": 0, "unknown": 0, "dead": 0}
    for path in discovery.sources:
        source = (path.candidate.expected_source
                  if isinstance(path, DiscoveredSourcePath)
                  else source_id(path))
        state = join.states.get(source, "unknown")
        counts[state if state in counts else "unknown"] += 1
    lane = getattr(positions, "next_lane", "active")
    return (f"source activity {anchored_page_cursor_key(side)}: "
            f"{counts['live']} live; {counts['unknown']} unknown; "
            f"{counts['dead']} dead readable; next lane {lane}")


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
    # One registry snapshot for the channel line and the peer list both. The
    # headline is earned by an Antiphon answer, not by an endpoint record or a
    # socket-file-shaped pathname. Registered peers get the same startup
    # patience doctor gives them; the ordinary idle-project path is tried once.
    live = _live_by_kind(cwd)
    identities = _codex_identity_snapshot(cwd, live["codex"])
    live["claude"] = [peer for peer in live["claude"]
                      if _automatic_ready(cwd, "claude", peer)]
    displayed = {"claude": live["claude"],
                 "codex": live["codex"] + list(identities.automatic)}
    channel = ("live" if _channel_answering(cwd, live["claude"])
               else "down")
    print(f"Claude channel:     {channel}")
    print(attachment_report(cwd))
    print(_codex_census_line(live["codex"], identities))
    for line in _peer_report(displayed, identities):
        print(line)
    # Printed outside the peers block on purpose: the window this names is
    # precisely the one where that block is empty.
    for line in _reconnect_lines(reconnect_window(
            cwd, [peer.get("name") for peer in live["claude"]])):
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
    discoveries = {}
    runtime_positions = {}
    for side in ("claude", "codex"):
        cursor, cursor_state = snapshots[side]
        positions, _since, _replay = positions_for(
            cursor, side, cursor_state)
        discovery = _reader_discovery(cwd, side, cursor, cursor_state)
        discoveries[side] = discovery
        runtime_positions[side] = positions
        print(_catalog_status_line(side, discovery))
    for side in ("claude", "codex"):
        print(_source_activity_line(
            cwd, side, discoveries[side], runtime_positions[side], identities))
    cleanup_pending = _catalog_cleanup_pending(cwd)
    if cleanup_pending is None:
        print("source manifests: cleanup pending unknown")
    else:
        print(f"source manifests: {cleanup_pending} cleanup pending")
    for side in ("claude", "codex"):
        cursor, cursor_state = snapshots[side]
        print(_backlog_line(page_cursor_key(side),
                            reader_backlog(cwd, side, cursor, cursor_state)))
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


# ---------- doctor ----------

# The floor this package is tested and installed under. A contract test reads
# the same number back out of the README: one number, one fact.
PYTHON_FLOOR = (3, 9)

# Seconds to wait for the channel's answer to a half-closed connection.
# Measured against the real server: with `shutdown(SHUT_WR)` the reply arrives
# in 0 ms, so this is not a budget for a healthy reply — it bounds the one case
# a healthy reply cannot produce, a listener that accepts and never answers.
# Half a second is long enough that a loaded machine's scheduling is not read as
# silence, and short enough that a per-peer probe still returns promptly.
DOCTOR_REPLY_TIMEOUT = 0.5

# Enough for the channel's one-line answer; the probe is not a protocol client.
DOCTOR_REPLY_BYTES = 8192

NODE_ENGINE_FLOOR = re.compile(r"^\s*>=\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$")
VERSION_IN_BANNER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _which(name):
    """Where PATH resolves `name`, or None. One of doctor's two seams.

    Every external lookup goes through here or `_tool_version` so a test can
    state what the machine looks like without patching the standard library
    underneath unrelated code."""
    return shutil.which(name)


def _tool_version(command):
    """A tool's `--version` line, or None. Doctor's second and last seam.

    Bounded: a diagnostic that hangs on a wedged interpreter is worse than one
    that cannot name a version. Five seconds is the ceiling the registry's own
    process lookups use, and a version banner either prints at once or never.

    Measured trap this seam exists for: patching `shutil.which` alone leaves
    this subprocess reading the host, so a Node 18 machine reddens tests that
    have nothing to do with Node."""
    try:
        done = subprocess.run([command, "--version"], capture_output=True,
                              text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or done.stderr).strip() or None


def _version_key(version):
    """A dotted version as a tuple of ints, or None when it is not one.

    Measured: string comparison inverts on three of four realistic pairs —
    `0.9.0` reads as newer than `0.10.0`, `0.3.1` as newer than `0.10.0`. A
    version that will not parse this way gets no ordering guess at all; saying
    "cannot compare" is cheaper than telling somebody to downgrade."""
    if not isinstance(version, str):
        return None
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def _banner_key(banner):
    """The first dotted version in a `--version` line, as ints.

    `Python 3.9.6` and `v20.11.0` are the two shapes this reads; both put the
    number somewhere in one line and nowhere else."""
    match = VERSION_IN_BANNER.search(banner or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _package_root():
    """The directory holding `package.json` for the copy running right now.

    `realpath` because `bin/antiphon.mjs` hands Python an unnormalised
    `bin/../lib/antiphon.py`, and because a Homebrew or `npm link` shim on PATH
    resolves into a completely different tree — measured, comparing the
    unresolved paths calls a working `npm link` install broken."""
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _package_version(root):
    """The version in the `package.json` beside a package root, or None.

    Measured with `npm pack`: `package.json` sits beside `lib/` and `bin/` in
    the repo and under `node_modules/antiphon/` alike, so one join finds it in
    either layout."""
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def _node_floor():
    """`engines.node` as a tuple of ints, or None. The floor npm enforces."""
    try:
        with open(os.path.join(_package_root(), "package.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        spec = data["engines"]["node"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    match = NODE_ENGINE_FLOOR.match(spec) if isinstance(spec, str) else None
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


ConfigState = collections.namedtuple("ConfigState", "path data reason")


def _config_state(cwd):
    """Every configuration file read once, as facts the checks render from.

    Once, because two checks ask about the same files: the install check has to
    know whether anything calls `antiphon` from a hook before the configuration
    check prints its verdict, and a second parse is a second chance to disagree
    with the first.

    A file that cannot be read is a fact too — recorded, never raised. This is
    the command somebody runs *because* a file is broken.
    """
    states = {}
    for name in (CLAUDE_SETTINGS_FILE, CLAUDE_LOCAL_SETTINGS_FILE,
                 CODEX_HOOKS_FILE, MCP_CONFIG_FILE):
        path = os.path.join(cwd, name)
        try:
            states[name] = ConfigState(path, _read_json_object(path), None)
        except ConfigFileError as error:
            states[name] = ConfigState(path, None, error.reason)
    # The TOML file has no `_read_json_object` to raise for it — the writer
    # reads it with a bare `open` — so the pre-pass supplies its own bound and
    # its own reason, and the promise "every file, once, or a stated reason"
    # covers all five.
    path = os.path.join(cwd, CODEX_CONFIG_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            states[CODEX_CONFIG_FILE] = ConfigState(path, f.read(), None)
    except FileNotFoundError:
        states[CODEX_CONFIG_FILE] = ConfigState(path, "", None)
    except OSError as error:
        states[CODEX_CONFIG_FILE] = ConfigState(
            path, None, f"could not be read "
                        f"({error.strerror or type(error).__name__})")
    except UnicodeDecodeError:
        states[CODEX_CONFIG_FILE] = ConfigState(path, None, "not valid UTF-8")
    return states


def _hooks_configured(states):
    """Whether anything in this project calls `antiphon` from a hook.

    Three answers, not two. A settings file that cannot be parsed makes the
    question unanswerable, and the install check must say so rather than report
    a project whose hooks it could not read as one that has none.
    """
    unreadable = False
    for shape in hook_shapes():
        state = states[shape.path]
        if state.reason is not None:
            unreadable = True
        elif hook_installed(state.data, shape):
            return True
    return None if unreadable else False


def _toml_table_text(text, table):
    """Just `[table]` and its sub-tables — the complement of
    `_strip_toml_table`, which keeps everything else. The writer discards this
    section and rewrites it; a reader needs exactly the part it discards."""
    kept, taking = [], False
    for line in text.splitlines():
        header = TOML_HEADER.match(line)
        if header:
            name = header.group(1)
            taking = name == table or name.startswith(table + ".")
        if taking:
            kept.append(line.strip())
    return kept


Probe = collections.namedtuple("Probe", "error answered")
ChannelTarget = collections.namedtuple(
    "ChannelTarget", "name path state patient automatic")


def _probe_channel(path, patient):
    """Connect, half-close, read one reply. Returns (errno or None, answered).

    Nothing is ever sent. Three of the four steps are counter-intuitive, so:

    - The half-close is load-bearing. `lib/channel.mjs` answers from its `end`
      handler, so it replies only once the client has sent FIN. Measured
      against the real server: with `shutdown(SHUT_WR)` the reply arrives in
      0 ms; without it the read times out at 2 s and a working bridge reads as
      broken — a diagnostic telling every healthy user to restart.
    - `patient` is spent only where a registered live peer claims the address.
      `NOT_LISTENING_YET` includes `ENOENT`, so retrying at the unregistered
      project default spins 1,545 ms over 28 attempts on the perfectly normal
      no-socket state. The split cannot reintroduce the race the patience
      exists for: the channel server claims the registry *before* it binds
      (a contract test pins that ordering), so a session inside the measured
      27-41 ms window is always in the registered branch.
    - One timeout covers connect and read both, rather than adding a second
      spelling of a number this file already holds once.
    """
    deadline = time.monotonic() + CONNECT_PATIENCE
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(DOCTOR_REPLY_TIMEOUT)
        try:
            sock.connect(path)
        except OSError as error:
            sock.close()
            if (patient and error.errno in NOT_LISTENING_YET
                    and time.monotonic() < deadline):
                time.sleep(CONNECT_RETRY_DELAY)
                continue
            return Probe(error.errno, False)
        break
    try:
        with sock:
            sock.shutdown(socket.SHUT_WR)
            reply = sock.recv(DOCTOR_REPLY_BYTES)
    except OSError:
        return Probe(None, False)       # accepted, then said nothing
    try:
        answer = json.loads(reply.decode())
    except (UnicodeDecodeError, ValueError):
        return Probe(None, False)
    # The minimum shape, deliberately not the protocol: any process can bind
    # that path, and none of them answers a bare half-closed connection with a
    # JSON object carrying `ok`. The *presence* of the key is the signal and
    # its value is ignored on purpose — the healthy handshake is
    # `{"ok":false,"error":"Unexpected end of JSON input"}`, because doctor
    # deliberately sends nothing for the server to parse.
    return Probe(None, isinstance(answer, dict) and "ok" in answer)


def _channel_targets(cwd, live):
    """Claude channel addresses both status and doctor must tell the truth about.

    A registry claim earns startup patience. An explicitly configured alias is
    one exact address even before it has a record; falling through to the bare
    project path would answer a question the caller did not ask. Only a project
    with neither a Claude record nor an explicit alias gets the legacy target.
    """
    claude = [record for record in live
              if record.get("kind") == "claude"]
    requested = sender_alias(peers.explicit_name())
    registered_names = {record.get("name") for record in claude}
    # Automatic comes off the record's own marker, never off the alias. The
    # name grammar allows `auto-...` as a configured name, and reading identity
    # from a shape is the guess this whole contract refuses everywhere else —
    # here it hid an operator's own socket path and offered them the remedy for
    # a rotation that cannot happen to an explicitly named peer.
    targets = [ChannelTarget(record.get("name"),
                             peers._address_of(record),
                             "registered", True,
                             peers._identity_digest_of(record) is not None)
               for record in claude if peers._address_of(record) is not None]
    if requested and requested not in registered_names:
        targets.insert(0, ChannelTarget(
            requested, claude_socket_path(cwd, requested),
            "unregistered", False, False))
    if not claude and not requested:
        targets.append(ChannelTarget(
            None, claude_socket_path(cwd), "legacy", False, False))
    return targets


def _channel_answering(cwd, live):
    """Whether any relevant Claude address answers as an Antiphon channel."""
    return any(_probe_channel(target.path, patient=target.patient).answered
               for target in _channel_targets(cwd, live))


class _Report:
    """Doctor's lines and its exit code, in one place.

    Every line goes to stdout, including the bad news: `antiphon doctor >
    report.txt` has to capture the whole report, which is the point of a
    diagnostic somebody pastes into an issue. `setup` splits the streams
    because its failures are its own; doctor's findings are the output.

    Only `✗` moves the exit code. `·` means "nothing to do here" — an idle
    project is not a degraded one.
    """

    def __init__(self):
        self.broken = False

    def ok(self, text):
        print(f"✓ {text}")

    def note(self, text):
        print(f"· {text}")

    def bad(self, text):
        self.broken = True
        print(f"✗ {text}")


def _doctor_install(report, hooks_configured):
    """Which copy of the package the hooks actually run."""
    here = _package_root()
    mine = _package_version(here)
    found = _which("antiphon")
    if not found:
        if hooks_configured is None:
            report.note("install: no `antiphon` on PATH, and the hook "
                        "configuration could not be read — see below")
        elif hooks_configured:
            report.bad("install: hooks call `antiphon` but PATH has none — "
                       "install the package or fix PATH")
        else:
            report.note("install: no `antiphon` on PATH and no hooks "
                        "configured here")
        return
    theirs = os.path.dirname(os.path.dirname(os.path.realpath(found)))
    if theirs == here:
        report.ok(f"install: {found} — version {mine or 'unknown'}")
        return
    other = _package_version(theirs)
    if other is None:
        report.bad(f"install: the copy on PATH is broken — no readable "
                   f"package.json beside {found}; reinstall it")
        return
    if other == mine:
        report.note(f"install: {found} is a different copy of the same "
                    f"version {other}; hooks use that one")
        return
    mine_key, other_key = _version_key(mine), _version_key(other)
    if mine_key is None or other_key is None:
        # `mine`/`other` may be None when a package.json is unreadable; name
        # that plainly instead of interpolating the Python literal.
        mine_text = mine or "unreadable"
        other_text = other or "unreadable"
        report.note(f"install: hooks run {other_text} from {found} while this "
                    f"copy is {mine_text}; cannot compare versions")
    elif other_key < mine_key:
        report.bad(f"install: hooks run {other} from {found} while this copy "
                   f"is {mine} — update the PATH install")
    else:
        report.note(f"install: hooks run the newer {other} from {found}; this "
                    f"diagnostic is the older copy {mine}")


# The two long-lived servers this package runs, by the script in their argv.
# The wrapper (`node …/bin/antiphon mcp`) only spawns and is not matched.
_PY_SERVER = re.compile(r"(\S+/lib/antiphon\.py)\s+mcp(?:\s|$)")
_NODE_SERVER = re.compile(r"(\S+/lib/channel\.mjs)(?:\s|$)")
# What a server loads once at start. A change to any of these after that
# start is code the process is provably not running.
CODE_FILES = ("lib/antiphon.py", "lib/peers.py", "lib/channel.mjs", "package.json")


def _parse_process_table(text):
    """`(pid, started, args)` rows from `ps -eo pid=,lstart=,args=` output.

    `lstart` is five whitespace-separated tokens (`Sat Aug  1 09:05:00 2026`,
    day space-padded), so the line splits on at most six gaps and the seventh
    field is the command line with its own spaces intact. Lines that do not
    parse are skipped, never guessed at."""
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        try:
            started = int(time.mktime(time.strptime(" ".join(parts[1:6]),
                                                    "%a %b %d %H:%M:%S %Y")))
            rows.append((int(parts[0]), started, parts[6]))
        except (ValueError, OverflowError):
            continue
    return rows


def _process_table():
    """Every process, as `(pid, started, args)`, or None if `ps` cannot say.

    Doctor's third seam, beside `_which` and `_tool_version`: the suite runs
    on a machine with live bridge servers of its own, and their age must not
    redden a fixture's diagnosis. `LC_ALL=C` pins the month names the parser
    reads; the flags are the ones macOS and procps share."""
    try:
        done = subprocess.run(["ps", "-eo", "pid=,lstart=,args="],
                              capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL,
                              env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return _parse_process_table(done.stdout)


def _bridge_servers(table):
    """The bridge servers in a process table: `(pid, started, side, script)`."""
    servers = []
    for pid, started, args in table:
        match = _PY_SERVER.search(args)
        if match:
            servers.append((pid, started, "codex mcp", match.group(1)))
            continue
        match = _NODE_SERVER.search(args)
        if match:
            servers.append((pid, started, "claude channel", match.group(1)))
    return servers


def _code_changed_at(root):
    """The newest mtime among a package root's code files, or None if none
    can be read — a tree that is gone, or never was one."""
    latest = None
    for name in CODE_FILES:
        try:
            changed = os.stat(os.path.join(root, name)).st_mtime
        except OSError:
            continue
        latest = changed if latest is None else max(latest, changed)
    return latest


def _when(stamp):
    local = time.localtime(stamp)
    if local[:3] == time.localtime()[:3]:
        return time.strftime("%H:%M:%S", local)
    return time.strftime("%Y-%m-%d %H:%M:%S", local)


def _doctor_running(report, cwd):
    """Servers running code older than the code on disk.

    A hook reloads every turn; a server loads once. Measured on 2026-08-31:
    four bridge servers were running code from before the day's merges,
    doctor said 13/13 ✓, and the fix the person had just installed was
    provably not what was answering.

    Scoped to what this project can act on: a server running the copy this
    project's hooks run — `PATH`'s `antiphon`, realpath — or one registered in
    this project's registry, wherever it runs from. Judging every bridge
    server on the machine printed four ✗ about another project's sessions on a
    correctly set up fresh project, so a healthy project could not be told
    from a broken one; the rest are one `·` count, without paths or a verdict.
    The registry, scanned without pruning, also lends a pid its alias so the
    line names the session the person knows. An ✗, because the installed code
    is not the running code, and the repair is theirs: restart that session.
    """
    table = _process_table()
    if table is None:
        report.note("running: the process table could not be read (`ps`)")
        return
    servers = _bridge_servers(table)
    if not servers:
        report.note("running: no bridge server of this package is running — "
                    "a session starts one")
        return
    # Two different questions, and conflating them cost the default install:
    # being registered here is what puts a pid in scope, while a name is only
    # how the line reads. The unnamed peer registers under the reserved
    # `<unnamed>` key, which `valid_name` rejects on purpose — built from names
    # alone, the pid table excluded exactly the single-pair default.
    registered, names = set(), {}
    for record in peers._scan(cwd):
        pid, name = record.get("pid"), record.get("name")
        if not isinstance(pid, int):
            continue
        registered.add(pid)
        if isinstance(name, str) and peers.valid_name(name):
            names[pid] = name
    # The copy this project's hooks run, which is the one whose staleness this
    # project can act on. Measured on a fresh temp project: judging every
    # bridge server on the machine printed four ✗ about another project's
    # sessions and exited 1, so a correctly set up project could not be told
    # apart from a broken one. A peer registered here stays in scope wherever
    # it runs from — it is serving this project.
    found = _which("antiphon")
    install = (os.path.dirname(os.path.dirname(os.path.realpath(found)))
               if found else _package_root())
    here = _package_root()
    fresh, elsewhere = 0, 0
    for pid, started, side, script in sorted(servers):
        root = os.path.dirname(os.path.dirname(script))
        if pid not in registered and os.path.realpath(root) != os.path.realpath(install):
            elsewhere += 1
            continue
        who = (f'{side} "{names[pid]}" pid {pid}' if pid in names
               else f"{side} pid {pid}")
        changed = _code_changed_at(root)
        if changed is None:
            where = ("which no longer exists" if not os.path.isdir(root)
                     else "whose code files cannot be read")
            report.bad(f"running: {who} runs from {root}, {where} — an orphan; "
                       "stop it")
            continue
        if int(changed) > started:
            origin = "" if os.path.realpath(root) == here else f" (from {root})"
            restart = ("restart that Claude Code session, or reconnect the "
                       "antiphon MCP server" if side == "claude channel"
                       else "restart that Codex session")
            report.bad(f"running: {who} started {_when(started)}, its code "
                       f"changed {_when(changed)}{origin} — {restart}")
            continue
        fresh += 1
    if fresh:
        report.ok(f"running: {fresh} server(s) on their current code")
    if elsewhere:
        # Counted, never judged, and never by path: another install's servers
        # are another project's diagnosis, and its directories are not this
        # report's to print.
        report.note(f"running: {elsewhere} bridge server(s) from another "
                    "install are running; run doctor in their project to "
                    "judge them")


def _doctor_interpreters(report):
    """The two interpreters the bridge runs on, and the one the hooks get."""
    running = sys.version_info[:3]
    floor = ".".join(str(part) for part in PYTHON_FLOOR)
    if running[:len(PYTHON_FLOOR)] >= PYTHON_FLOOR:
        report.ok("python: this run is %d.%d.%d (floor %s)"
                  % (running + (floor,)))
    else:
        report.bad("python: this run is %d.%d.%d, below the %s floor"
                   % (running + (floor,)))

    # `bin/antiphon.mjs` spawns a bare `python3`, so the interpreter every hook
    # gets is whatever PATH resolves — measured on this machine, Anaconda 3.14
    # rather than the /usr/bin 3.9 the suite runs under. Reporting only
    # `sys.version_info` answers a different question from the diagnostic one.
    hook_python = _which("python3")
    if not hook_python:
        report.bad("python: PATH has no `python3` — every hook the wrapper "
                   "starts fails; install one or fix PATH")
    else:
        banner = _tool_version(hook_python)
        key = _banner_key(banner)
        if key is None:
            report.note(f"python: hooks run {hook_python}, whose version could "
                        "not be read")
        elif key[:len(PYTHON_FLOOR)] < PYTHON_FLOOR:
            report.bad(f"python: hooks run {hook_python} ({banner}), below the "
                       f"{floor} floor — fix PATH or install a newer python3")
        elif os.path.realpath(hook_python) != os.path.realpath(sys.executable):
            report.note(f"python: hooks run {hook_python} ({banner}), not the "
                        f"{sys.executable} running this check")
        else:
            report.ok(f"python: hooks run {hook_python} ({banner})")

    node = _which("node")
    declared = _node_floor()
    wanted = (".".join(str(part) for part in declared) if declared
              else "the declared floor")
    if not node:
        report.bad("node: PATH has no `node` — the Claude channel server "
                   f"cannot start; install Node {wanted} or newer")
        return
    banner = _tool_version(node)
    key = _banner_key(banner)
    if key is None or declared is None:
        report.note(f"node: {node}, version or floor could not be read")
    elif key < declared:
        report.bad(f"node: {node} is {banner}, below the {wanted} this package "
                   "declares — the channel server will not run")
    else:
        report.ok(f"node: {node} ({banner}), floor {wanted}")


def _doctor_config(report, cwd, states):
    """Every file `setup` writes, read back through the shapes it wrote.

    The expectations are the shared module data, never a second spelling: a
    diagnostic holding its own copy of a string the writer may change is the
    drift this whole arrangement exists to prevent.
    """
    def verdict(name, missing):
        state = states[name]
        if state.reason is not None:
            report.bad(f"{name}: unreadable: {state.reason} — fix or delete "
                       "it, then run `antiphon setup`")
        elif missing:
            report.bad(f"{name}: missing {', '.join(missing)} — run "
                       "`antiphon setup`")
        else:
            report.ok(f"{name}: complete")

    hooks_by_file = collections.defaultdict(list)
    for shape in hook_shapes():
        hooks_by_file[shape.path].append(shape)

    for name in (CLAUDE_SETTINGS_FILE, CODEX_HOOKS_FILE):
        data = states[name].data
        missing = [f"the {shape.event} hook `{shape.command}`"
                   for shape in hooks_by_file[name]
                   if not hook_installed(data, shape)]
        if name == CLAUDE_SETTINGS_FILE:
            allowed = (data or {}).get(CONFIG_KEYS.permissions) or {}
            allowed = (allowed.get(CONFIG_KEYS.allow)
                       if isinstance(allowed, dict) else None)
            for permission in CLAUDE_TOOL_PERMISSIONS:
                if not (isinstance(allowed, list) and permission in allowed):
                    missing.append(f"the `{permission}` permission")
        verdict(name, missing)

    # Claude Code gates every `.mcp.json` server behind this allowlist. Without
    # the entry the server never starts, no socket appears, and the channel
    # check reports a true "no socket" with a useless repair.
    enabled = (states[CLAUDE_LOCAL_SETTINGS_FILE].data or {}).get(
        CONFIG_KEYS.enabled_mcp_servers)
    verdict(CLAUDE_LOCAL_SETTINGS_FILE,
            [] if isinstance(enabled, list) and CHANNEL_SERVER_NAME in enabled
            else [f"`{CHANNEL_SERVER_NAME}` in enabledMcpjsonServers"])

    servers = (states[MCP_CONFIG_FILE].data or {}).get(CONFIG_KEYS.mcp_servers)
    servers = servers if isinstance(servers, dict) else {}
    verdict(MCP_CONFIG_FILE,
            [] if servers.get(CHANNEL_SERVER_NAME) == channel_server_entry(cwd)
            else [f"the `{CHANNEL_SERVER_NAME}` MCP server entry for {cwd}"])

    # The value, not the key. A table left pointing at a renamed directory
    # reads another project's registry and delivers nothing — a quiet bridge
    # with every key present.
    toml = states[CODEX_CONFIG_FILE].data
    present = set(_toml_table_text(toml or "", CODEX_MCP_TABLE))
    expected = ([line for line, _comment in CODEX_TABLE_ASSIGNMENTS]
                + codex_env_assignments(cwd))
    verdict(CODEX_CONFIG_FILE,
            [f"[{CODEX_MCP_TABLE}]"] if not present
            else [f"`{line}`" for line in expected if line not in present])


def _doctor_alias(report):
    """Through `peers.explicit_name()`, which is what the routing uses.

    Measured: it lower-cases, so `ANTIPHON_NAME=UI` is a working named session
    that a raw environment read calls invalid. The name printed is the one it
    returns — `@claude:UI` addresses nobody."""
    name = peers.explicit_name()
    if not name:
        report.ok("alias: unnamed (single-pair default)")
    elif peers.valid_name(name):
        report.ok(f'alias: named "{name}"')
    else:
        report.bad('alias: ANTIPHON_NAME is not usable and cannot '
                   "be addressed — use lower-case letters, digits, `_` and "
                   "`-`, starting with a letter or digit, at most 32 characters")


def _doctor_peers(report, cwd):
    """The registry as it stands, without changing it.

    `_scan` + `_record_alive`, never `read_peers`, `_live_by_kind` or
    `resolve_target`: all three prune, so any of them would delete the stale
    record somebody ran this command to ask about. Returns the live records,
    for the channel check to probe.
    """
    live = []
    records = peers._scan(cwd)
    if not records:
        report.note("peers: none registered — a session registers on its "
                    "first turn")
        return live
    for record in sorted(records, key=lambda r: (r.get("kind") or "",
                                                 r.get("name") or "")):
        # Named in words, never by session id or address: whoever reads this is
        # deciding who to address, and neither answers that.
        who = f"{record.get('kind')}/{record.get('name')}"
        if not peers._record_alive(record):
            report.note(f"peer {who}: stale record; a live session cleans this "
                        "up on its next pass")
            continue
        owner = peers._owner_of(record)
        session = peers.read_session(cwd, record.get("kind"), record.get("name"))
        session_owner = peers._owner_of(session) if session else None
        mixed_owner_generation = peers.owner_generations_mixed(
            owner, session_owner)
        if mixed_owner_generation:
            report.note(f"peer {who}: endpoint and session owner key generations "
                        "differ; restart the older writer to refresh its record")
        elif owner is None:
            # Two origins and no way to tell them apart from here: a record
            # written before the field existed, or an `owner_key()` that
            # returned nothing at registration. The note states what is
            # observable and offers the common one as a cause, not a diagnosis.
            report.note(f"peer {who}: this endpoint has no owner key, so "
                        "sessions cannot be joined to it; restarting that "
                        "session usually records one")
        diagnostic = dict(record)
        if (peers._addressless(record) and session_owner == owner
                and peers.valid_session_id(
                    session.get("session_id") if session else None)):
            diagnostic["address"] = session["session_id"]
        live.append(diagnostic)
        # The same verdict the resolver and `status` consult. Doctor kept its
        # own copy — record liveness alone — and so printed `✓ live and
        # addressed` for the very alias delivery was refusing, to the operator
        # who ran doctor *because* delivery was failing. The record stays in
        # `live`: this is about what the reader is told, not about hiding a
        # socket from the channel probe below.
        verdict = automatic_verdict(cwd, record.get("kind"), diagnostic,
                                    peers.read_identity_proof(cwd, owner))
        # The remedy belongs to the rotation window and nowhere else. `UNREADY`
        # is the state every automatic channel passes through before its first
        # Stop hook, and telling that operator to reconnect restarts the
        # identical wait; a reconnect does not repair an unreadable proof
        # either. Only `PROVED_STALE` names something a reconnect fixes.
        if verdict == "PROVED_STALE":
            report.note(f"peer {who}: {_VERDICT_NOTE[verdict]} — "
                        f"{RECONNECT_REMEDY}")
        elif verdict in ("UNKNOWN", "STRUCTURAL_INVALID"):
            report.note(f"peer {who}: {_VERDICT_NOTE[verdict]}")
        elif verdict == "UNREADY":
            # Same suppression the untyped path has always had: an endpoint and
            # session whose owner-key generations differ already got their own
            # note above, and two lines about one peer read as two faults.
            if not mixed_owner_generation:
                report.note(f"peer {who}: live, waiting for its first turn")
        elif peers._address_of(diagnostic) is None:
            if not mixed_owner_generation:
                report.note(f"peer {who}: live, waiting for its first turn")
        else:
            report.ok(f"peer {who}: live and addressed")
    return live


# One sentence per verdict, in the words a person acts on. The class names are
# for two readers agreeing with each other; nobody types `PROVED_STALE` at a
# terminal.
_VERDICT_NOTE = {
    "PROVED_STALE": "this alias is no longer its session's automatic identity",
    # `UNREADY` is deliberately absent: it renders as the ordinary
    # waiting-for-a-first-turn line, because that is what it is.
    "UNKNOWN": "this alias could not be checked because its identity proof "
               "could not be read; nothing is concluded from that",
    "STRUCTURAL_INVALID": "this alias has an identity proof that cannot be "
                          "trusted",
}


def _doctor_identity_window(report, cwd, registered):
    """Name the terminal that is current but has no channel yet."""
    served = [record.get("name") for record in registered
              if record.get("kind") == "claude"]
    for line in _reconnect_lines(reconnect_window(cwd, served)):
        report.note(line)


def _doctor_codex_observations(report, cwd, registered):
    """Report one read-only lower-bound automatic-identity snapshot."""
    codex = [record for record in registered
             if record.get("kind") == "codex"]
    identities = _codex_identity_snapshot(cwd, codex)
    report.note(_codex_census_line(codex, identities))
    for peer in identities.automatic:
        report.ok(f"peer codex/{peer.get('name')}: live and addressed "
                  "(automatic after first hook)")
    for name in identities.conflicts:
        report.bad(f"peer codex/{name}: automatic identity collision; no "
                   "delivery can choose this alias")
    return identities


def _doctor_channel(report, cwd, live):
    """Somebody answered, or nobody did — not "the file exists"."""
    for target in _channel_targets(cwd, live):
        name, path, state = target.name, target.path, target.state
        registered = state == "registered"
        who = f'channel: peer "{name}"' if name else "channel:"
        # An explicit or legacy peer's path is actionable: its operator chose
        # the name, and `remove it` is a thing they can do. An automatic peer's
        # path is derived from a host session id nobody typed, so printing it
        # publishes a private route in exchange for a remedy that was always
        # "restart that session" anyway.
        automatic = target.automatic
        route = "its address" if automatic else path
        probe = _probe_channel(path, patient=target.patient)
        if probe.answered and state == "unregistered":
            report.bad(f'{who} answers, but no live endpoint record holds '
                       f'alias "{name}" — restart that Claude session')
        elif probe.answered:
            report.ok(f"{who} answered")
        elif probe.error == errno.ENOENT and registered:
            report.bad(f"{who} the session's channel socket is gone — "
                       "restart that session")
        elif probe.error == errno.ENOENT:
            report.note(f"{who} no socket — the channel starts with the "
                        "Claude session")
        elif probe.error == errno.ECONNREFUSED:
            report.bad(f"{who} socket file present but nothing listening — "
                       + ("restart that Claude session" if automatic else
                          f"restart the Claude session or remove {path}"))
        elif probe.error == errno.ENOTSOCK:
            report.bad(f"{who} {route} is not a socket — "
                       + ("restart that Claude session" if automatic
                          else "remove it"))
        elif probe.error == errno.EACCES:
            report.bad(f"{who} the socket exists but this user cannot "
                       "connect to it"
                       + ("; restart that Claude session" if automatic
                          else f": {path}"))
        elif probe.error is not None:
            report.bad(f"{who} {os.strerror(probe.error)} at {route}")
        else:
            report.bad(f"{who} something is listening at {route}, but it does "
                       "not answer as an Antiphon channel — "
                       + ("restart that Claude session" if automatic else
                          "remove it or stop whatever holds it"))


def _doctor_codex(report, cwd):
    """Presence only. Executing `codex` from a diagnostic can block on auth or
    spawn a session; the boundary is stated in BACKLOG."""
    found = _which("codex")
    if found:
        report.ok(f"codex CLI: {found}")
    else:
        report.note("codex CLI: not on PATH — push-to-Codex needs it (fine on "
                    "a Claude-only install)")
    _doctor_codex_queue(report, cwd)


def _doctor_codex_tool_shapes(report, cwd):
    """Expose fail-closed Codex schema drift without printing its payload."""
    count = _codex_tool_shape_count(cwd)
    if count is None:
        report.note("codex tool shapes: amount unknown because source "
                    "discovery or reading is incomplete")
    elif count:
        noun = "record" if count == 1 else "records"
        verb = "is" if count == 1 else "are"
        report.bad(f"codex tool shapes: {count} unrecognized tool-call "
                   f"{noun} {verb} omitted from passive pages — Codex's host "
                   "schema may have changed; update Antiphon")
    else:
        report.ok("codex tool shapes: 0 unrecognized tool-call records in "
                  "the trusted complete source set")


def _doctor_codex_queue(report, cwd):
    """Messages `codex queue` accepted for a thread that is no longer running.

    Measured: two bridge messages sat in Codex's queue for closed threads —
    one since 12:17, one since 15:04 — and nothing anywhere said so. Read
    read-only (`mode=ro`), schema feature-detected, and silent on any failure:
    this is Codex's own database and its shape is not this project's to
    promise. A note, never ✗: only Codex can drain its queue, and a permanent
    ✗ over it is one people learn to ignore. No thread id is printed, not even
    a prefix: a truncated session id is still session identity, and it was
    never actionable — nobody can address a thread that is not running.
    """
    import sqlite3
    paths = sorted(glob.glob(CODEX_QUEUE_DBS))
    if not paths:
        return
    # Only threads this project could have queued to. Codex's queue is one
    # database for every project on the machine, and a message stranded in
    # another project's thread is not this reader's to act on — measured on a
    # fresh temp project, where the note named two threads belonging to a
    # different directory entirely. A thread whose rollout has aged out of
    # discovery drops out of this note with it.
    ours = set()
    for path in codex_rollout_files(cwd):
        sid = SESSION_ID.search(os.path.basename(path))
        if sid:
            ours.add(sid.group(1))
    if not ours:
        return
    try:
        con = sqlite3.connect(f"file:{paths[-1]}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT thread_id, COUNT(*) FROM queued_items "
                               "GROUP BY thread_id").fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return
    stranded = [(tid, n) for tid, n in rows
                if isinstance(tid, str) and tid in ours
                and codex_thread_alive(tid) is False]
    if stranded:
        # Aggregate, never a thread-id prefix. A truncated prefix is still
        # session identity, and it was never actionable anyway: nobody can
        # address a thread that is not running. The counts are what a person
        # can act on.
        waiting = sum(count for _tid, count in stranded)
        threads = len(stranded)
        noun = "thread" if threads == 1 else "threads"
        report.note(f"codex queue: {waiting} message(s) wait across {threads} "
                    f"non-running project {noun} — they are read only if those "
                    "threads are resumed")


def _doctor_readonly():
    """Explains a quiet bridge without touching it.

    Read-only by construction: nothing here opens a file for writing, takes the
    registry lock, or calls one of the three readers that prune. A test
    snapshots both roots — the project and the socket directory — before and
    after, with a dead peer record armed.

    `✓` fine, `·` nothing to do here, `✗` broken. Only `✗` sets the exit code,
    so a set-up project with no session running exits 0.
    """
    cwd = project_dir()
    print(f"project: {cwd}\n")
    report = _Report()
    states = _config_state(cwd)
    _doctor_install(report, _hooks_configured(states))
    _doctor_running(report, cwd)
    _doctor_interpreters(report)
    _doctor_config(report, cwd, states)
    _doctor_alias(report)
    live = _doctor_peers(report, cwd)
    _doctor_identity_window(report, cwd, live)
    _doctor_codex_observations(report, cwd, live)
    _doctor_channel(report, cwd, live)
    _doctor_codex(report, cwd)
    _doctor_sources(report, cwd)
    _doctor_codex_tool_shapes(report, cwd)
    _doctor_replay(report, cwd)
    return 1 if report.broken else 0


def doctor(mode=None):
    """Diagnose by default; optionally repair project configuration, then check."""
    if mode is None:
        return _doctor_readonly()
    if mode != "--fix":
        print(f"antiphon: doctor accepts only --fix, got {mode}",
              file=sys.stderr)
        return 2
    print("repair: project configuration only; runtime state is not changed")
    setup_status = setup()
    print("\n— doctor re-check (read-only) —")
    doctor_status = _doctor_readonly()
    return 1 if setup_status or doctor_status else 0


def _doctor_replay(report, cwd):
    """A reader still re-delivering history — slow, not broken, so a note.

    Measured 2026-08-31: both readers had replayed for twenty hours while
    doctor said 13/13 ✓ and each side was reading the other's yesterday.
    Read-only: the cursor is read, never migrated or advanced."""
    for side in ("claude", "codex"):
        cursor, state = _read_cursor_state(cwd, side)
        backlog = reader_backlog(cwd, side, cursor, state)
        if backlog is None:
            report.note(f"replay: the {side} reader's cursor could not be "
                        "trusted; its next turn replays discovered history "
                        "(amount unknown until then); `antiphon catch-up` "
                        "skips what is left after that page")
            continue
        if not backlog[3]:
            continue
        unread = backlog[0]
        report.note(f"replay: the {side} reader is re-delivering history "
                    f"({unread:,} raw bytes unread); `antiphon catch-up` skips it")


def _doctor_sources(report, cwd):
    """Report catalog completeness per reader without changing catalog/cursor."""
    snapshots = {}
    for side in ("claude", "codex"):
        path = state_path(cwd, side)
        if path not in snapshots:
            snapshots[path] = _read_cursor_state(cwd, side)
        cursor, cursor_state = snapshots[path]
        discovery = _reader_discovery(cwd, side, cursor, cursor_state)
        line = _catalog_status_line(side, discovery)
        if discovery.state == "complete":
            report.ok(line)
        elif discovery.state == "building":
            report.note(f"{line} — `antiphon sources scan` completes the "
                        "finite catalog build")
        else:
            report.bad(f"{line} — run `antiphon sources scan` and inspect "
                       "its stderr; delivery remains explicitly incomplete")
        v4_key = anchored_page_cursor_key(side)
        if isinstance(cursor, dict) and v4_key in cursor:
            value = cursor.get(v4_key)
            if not _valid_v4_page_cursor(value):
                report.bad(f"{v4_key}: invalid anchored cursor; the next "
                           "reader turn recovers conservatively")
            elif value["adopting_v3"]:
                count = len(value["adopting_v3"])
                report.note(f"{v4_key}: {count} v3 source "
                            f"boundar{'y' if count == 1 else 'ies'} still "
                            "adopting; at most each last record repeats")
        positions, _since, _replay = positions_for(
            cursor, side, cursor_state)
        if discovery.sources:
            report.note(_source_activity_line(
                cwd, side, discovery, positions))
    cleanup_pending = _catalog_cleanup_pending(cwd)
    if cleanup_pending is None:
        report.bad("source manifests: cleanup pending amount unknown because "
                   "the catalog snapshot could not be trusted")
    elif cleanup_pending:
        report.note(f"source manifests: {cleanup_pending} cleanup pending — "
                    "the next catalog mutation retries it")
    for kind in ("claude", "codex"):
        analysis = _analyze_compaction_kind(
            cwd, kind, lock_cursors=False)
        blocked = sum(analysis.result["blockers"].values())
        pending = analysis.result["pending"]
        if not analysis.result["considered"] and not blocked and not pending:
            continue
        state = "blocked" if blocked else "pending" if pending else "ready"
        blockers = ", ".join(
            f"{name}={analysis.result['blockers'][name]}"
            for name in COMPACTION_BLOCKERS)
        report.note(f"source compaction {kind}: {state}; {pending} cleanup "
                    f"transaction(s) pending; {blockers}")


def _complete_prefix_end(path):
    """The byte offset just past the last newline-terminated record.

    A resume that begins inside a line still being written drops that record
    when the line completes: `read_records` yields the tail, `json.loads`
    fails, and the parser skips it. So the frontier a catch-up pins is the end
    of the last complete record, found by walking back from EOF in chunks —
    a single record can run past 15 KB, so one fixed tail read is not enough.
    """
    try:
        size = os.path.getsize(path)
        if size == 0:
            return 0
        with open(path, "rb") as f:
            f.seek(size - 1)
            if f.read(1) == b"\n":
                return size
            pos = size - 1
            while pos > 0:
                start = max(0, pos - 65536)
                f.seek(start)
                newline = f.read(pos - start).rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                pos = start
    except OSError:
        return 0
    return 0


# What each side's page reader looks at: Claude's page is built from Codex
# The source kind each page reader consumes. Discovery itself is catalog-backed;
# this map is only the side/kind contract shared by backlog and catch-up.
CATCH_UP_SOURCES = {"claude": "codex", "codex": "claude"}


def reader_backlog(cwd, side, cursor, cursor_state="valid"):
    """How far one side's page reader is behind, in the unit that is true.

    `(unread, positioned, unpositioned, replay)`: raw transcript bytes the
    reader has still to read across the discovered sources — each counted
    from where `_resolve_start` says the reader will actually start, the
    same rule the reader runs — how many of those sources start from a
    trusted recorded position, how many do not (placed by time, restarted at
    byte zero for a replaced generation or an offset past EOF, or never
    positioned), and the replay marker if any. Raw bytes, never pages: a
    page is a rendered envelope and most raw bytes never reach one (measured:
    nearly all of a 44 MB span was filtered before rendering), so no page
    count can be derived from here. None only when the cursor file itself
    cannot be read — then the next turn recovers, and how much it will read
    is unknown until it does; a malformed page key is recovery from byte
    zero and counts the whole file.
    """
    if cursor_state == "invalid":
        return None
    positions, since, replay = positions_for(cursor, side, cursor_state)
    kind = CATCH_UP_SOURCES[side]
    discovery = _discover_sources(cwd, kind, side, positions, since)
    unread, positioned, unpositioned = 0, 0, 0
    for path in discovery.sources:
        opened = _open_discovered_source(path, cwd, kind)
        if isinstance(opened, SourceRefusal):
            continue
        with opened as source:
            size = source.size()
            if size is None:
                continue
            start, reason = _resolve_source_start(source, positions, since)
            unread += max(0, size - start)
            if reason == "positioned":
                positioned += 1
            else:
                unpositioned += 1
    return unread, positioned, unpositioned, replay


def catch_up(side=None):
    """Moves page cursors to the live edge, abandoning undelivered history.

    Measured on the maintainer's own project: the v2→v3 upgrade's byte-zero
    replay of two days of transcripts was still draining twenty hours later,
    one page per turn, with every new message queued behind it — the bridge
    was delivering yesterday. Nothing could skip it. This can: each discovered
    source is pinned at the end of its last complete record under the same
    lock every reader takes, and the `replay` marker goes with the history it
    described. The legacy `_seen` value is left where it is, because a pre-v3
    process still reads it and the rollback story depends on it.

    Unnamed, it moves both sides — they share one file. A named peer keeps one
    cursor per side under its own name, so there it needs to be told which
    side, and run in that side's terminal.
    """
    cwd = project_dir()
    if side is not None and side not in CATCH_UP_SOURCES:
        print("antiphon: catch-up takes `claude` or `codex`", file=sys.stderr)
        return 2
    if side is None:
        if peers.valid_name(peers.explicit_name()):
            print("antiphon: this terminal is named, so its cursor is its own — "
                  "say which side to move: `antiphon catch-up claude|codex`, "
                  "run in that side's terminal", file=sys.stderr)
            return 2
        sides = ("claude", "codex")
    else:
        sides = (side,)
    status = 0
    for one in sides:
        key = anchored_page_cursor_key(one)
        held_cursor, held_state = _read_cursor_state(cwd, one)
        positions, since, _replay = positions_for(
            held_cursor, one, held_state)
        kind = CATCH_UP_SOURCES[one]
        discovery = _discover_sources(cwd, kind, one, positions, since)
        frontier, unpinned = {}, discovery.refusals
        for path in discovery.sources:
            opened = _open_discovered_source(path, cwd, kind)
            if isinstance(opened, SourceRefusal):
                unpinned += 1
                continue
            with opened as source:
                if source.generation is None:
                    unpinned += 1
                    continue
                end = source.complete_prefix_end()
                anchor = source.anchor_at(end) if end else None
                entry = {"gen": source.generation, "offset": end,
                         "anchor": anchor}
                if not _valid_anchored_position(entry):
                    unpinned += 1
                    continue
                frontier[source.source] = entry
        skipped = {"bytes": 0, "sources": 0}

        def mutate(cursor, key=key, frontier=frontier, skipped=skipped,
                   reader_side=one):
            held = cursor.get(key)
            if _valid_v4_page_cursor(held):
                sources = json.loads(json.dumps(held["sources"]))
                adopting = json.loads(json.dumps(held["adopting_v3"]))
                next_lane = held["next_lane"]
            else:
                sources, adopting, next_lane = {}, {}, "active"
            current_positions, _current_since, _current_replay = positions_for(
                cursor, reader_side)
            def merge(entries):
                for sid, raw in entries.items():
                    entry = dict(raw)
                    if _valid_anchored_position(entry):
                        sources[sid] = entry
                        adopting.pop(sid, None)
                    elif _valid_position(entry):
                        adopting[sid] = {
                            "gen": entry["gen"], "offset": entry["offset"]}
                        sources.pop(sid, None)

            # The expensive source measurement happened before this cursor
            # transaction. Re-read the freshly held cursor and never let that
            # stale snapshot overwrite a hook that advanced in between.
            merge(current_positions)
            for sid, entry in frontier.items():
                current = current_positions.get(sid)
                if (isinstance(current, dict)
                        and current.get("gen") != entry.get("gen")):
                    continue
                current_offset = (current.get("offset")
                                  if isinstance(current, dict) else -1)
                if (isinstance(current_offset, int)
                        and current_offset > entry["offset"]):
                    continue
                if (current_offset == entry["offset"]
                        and _valid_anchored_position(current)):
                    continue
                was = current_offset if isinstance(current_offset, int) else 0
                skipped["bytes"] += max(0, entry["offset"] - was)
                skipped["sources"] += 1
                merge({sid: entry})
            cursor[key] = {
                "v": ANCHORED_PAGE_CURSOR_VERSION,
                "sources": sources,
                "adopting_v3": adopting,
                "next_lane": next_lane,
            }
            return cursor

        if not update_cursor(cwd, one, mutate):
            print(f"antiphon: {key}: could not take the cursor lock; nothing "
                  "moved", file=sys.stderr)
            status = 1
            continue
        print(f"{key}: {skipped['sources']} source(s) moved to the live edge; "
              f"{skipped['bytes']:,} bytes of undelivered history will not be "
              "delivered")
        if unpinned:
            print(f"{key}: {unpinned} source(s) could not be measured "
                  "safely and were left alone")
    return status


COMPACTION_BLOCKERS = (
    "selected-legacy", "invalid-or-unreadable", "unconsumed-v4",
    "unknown-owner", "source-not-gone", "snapshot-raced",
)
COMPACTION_COUNTERS = (
    "considered", "retired", "files", "bytes", "dormant", "pending",
    "retryable",
)
COMPACTION_INPUT_LIMIT = 4 * 1024 * 1024
COMPACTION_JOURNAL_VERSION = 1
CompactionCursor = collections.namedtuple(
    "CompactionCursor",
    "path status raw named owner_status owner_raw owner_state owner_unknown")
CompactionAnalysis = collections.namedtuple(
    "CompactionAnalysis",
    "result eligible detached loaded view records cursor_signature")


def _read_compaction_file(path):
    """Read one bounded regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return "missing", b""
    except OSError:
        return "invalid", b""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "invalid", b""
        chunks, total = [], 0
        while total <= COMPACTION_INPUT_LIMIT:
            chunk = os.read(fd, min(65536, COMPACTION_INPUT_LIMIT + 1 - total))
            if not chunk:
                return "valid", b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
        return "invalid", b""
    except OSError:
        return "invalid", b""
    finally:
        os.close(fd)


def _compaction_journal_path(cwd, kind):
    return os.path.join(
        _catalog_root(cwd), f"compaction-{kind}.json")


def _valid_compaction_receipt(value):
    return (isinstance(value, dict)
            and set(value) == {"relative", "sha256", "bytes"}
            and _filesystem_safe_relative(value.get("relative"))
            and isinstance(value.get("sha256"), str)
            and len(value["sha256"]) == 64
            and all(char in "0123456789abcdef"
                    for char in value["sha256"])
            and isinstance(value.get("bytes"), int)
            and not isinstance(value.get("bytes"), bool)
            and value["bytes"] >= 0)


def _read_compaction_journal(cwd, kind):
    """Return a validated durable retirement transaction, if one exists."""
    status, raw = _read_compaction_file(_compaction_journal_path(cwd, kind))
    if status == "missing":
        return None, "missing"
    if status != "valid":
        return None, "invalid"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "invalid"
    if (not isinstance(value, dict)
            or set(value) != {"v", "project", "kind", "phase",
                              "old_state", "new_state", "records"}
            or value.get("v") != COMPACTION_JOURNAL_VERSION
            or value.get("project") != os.path.abspath(cwd)
            or value.get("kind") != kind
            or value.get("phase") not in ("prepared", "committed")
            or _catalog_load_value(cwd, value.get("old_state")).status != "valid"
            or _catalog_load_value(cwd, value.get("new_state")).status != "valid"
            or not isinstance(value.get("records"), list)
            or not value["records"]
            or not all(_valid_compaction_receipt(item)
                       for item in value["records"])
            or len({item["relative"] for item in value["records"]})
            != len(value["records"])):
        return None, "invalid"
    old_load = _catalog_load_value(cwd, value["old_state"])
    new_load = _catalog_load_value(cwd, value["new_state"])
    old_view = _catalog_view(cwd, kind, old_load)
    new_view = _catalog_view(cwd, kind, new_load)
    other = "codex" if kind == "claude" else "claude"
    retired = {item["relative"] for item in value["records"]}
    if (old_view.state != "complete" or new_view.state != "complete"
            or value["old_state"]["kinds"].get(other)
            != value["new_state"]["kinds"].get(other)
            or set(old_view.candidates) - set(new_view.candidates) != retired
            or not set(new_view.candidates).issubset(old_view.candidates)):
        return None, "invalid"
    return value, "valid"


def _write_compaction_journal(cwd, journal):
    return _atomic_json(
        _compaction_journal_path(cwd, journal["kind"]), journal)


def _remove_compaction_journal(cwd, kind):
    try:
        os.unlink(_compaction_journal_path(cwd, kind))
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _compaction_safe_catalog_load(cwd, loaded):
    """Expose the old catalog while a prepared state switch is unresolved.

    The journal is written before the compact state. If the process dies or a
    rollback write fails, readers continue through the old manifests instead
    of treating an unfinalized retirement as authoritative.
    """
    if loaded.status != "valid":
        return loaded
    for kind in ("claude", "codex"):
        journal, status = _read_compaction_journal(cwd, kind)
        if status == "missing":
            continue
        if status != "valid":
            return CatalogLoad(None, "invalid", "compaction-journal")
        if journal["phase"] != "prepared":
            continue
        if loaded.state not in (journal["old_state"], journal["new_state"]):
            return CatalogLoad(None, "invalid", "compaction-journal-raced")
        return CatalogLoad(
            json.loads(json.dumps(journal["old_state"])), "valid", None)
    return loaded


def _recover_prepared_compactions_locked(cwd):
    """Roll a prepared transaction back; committed cleanup stays explicit."""
    if getattr(_PROJECT_LOCK_STATE, "kind", None) != "catalog":
        raise RuntimeError("compaction recovery requires the catalog lock")
    for kind in ("claude", "codex"):
        journal, status = _read_compaction_journal(cwd, kind)
        if status == "missing":
            continue
        if status != "valid":
            return False
        if journal["phase"] == "committed":
            continue
        current = _read_source_catalog_raw(cwd)
        if current.status != "valid":
            return False
        if current.state == journal["new_state"]:
            if not _write_catalog_state(
                    cwd, journal["old_state"], cleanup=False):
                return False
        elif current.state != journal["old_state"]:
            return False
        if not _remove_compaction_journal(cwd, kind):
            return False
        _cleanup_catalog_manifests(cwd)
    return True


@contextlib.contextmanager
def _compaction_cursor_lock(path, patience=None):
    """Take one existing or future cursor's ordinary adjacent lock."""
    _refuse_nested_project_lock("cursor")
    if patience is None:
        patience = CURSOR_LOCK_PATIENCE
    lock_path = path + ".lock"
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
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
                if time.monotonic() >= deadline:
                    break
                time.sleep(CURSOR_LOCK_RETRY_DELAY)
            except OSError:
                break
        if held:
            _mark_project_lock("cursor")
        yield held
    finally:
        if held:
            _unmark_project_lock("cursor")
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _compaction_cursor_paths(cwd, side):
    """The shared reader plus named reader cursors, with no symlink traversal."""
    paths = [(os.path.join(cwd, ".antiphon", "cursor.json"), None)]
    try:
        entries = list(os.scandir(peers.peers_dir(cwd)))
    except FileNotFoundError:
        return paths
    except OSError:
        return None
    prefix = side + "-"
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        name = entry.name[len(prefix):]
        if not peers.valid_name(name):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                return None
        except OSError:
            return None
        cursor_path = os.path.join(entry.path, "cursor.json")
        if os.path.lexists(cursor_path):
            paths.append((cursor_path, name))
    return sorted(paths, key=lambda item: (item[1] is not None, item[1] or ""))


def _parse_compaction_json(status, raw):
    if status == "missing":
        return {}, "missing"
    if status != "valid":
        return None, "invalid"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "invalid"
    if not isinstance(value, dict):
        return None, "invalid"
    return _translate_cursor_keys(value), "valid"


def _compaction_cursor_inputs(cwd, side, locked=True,
                              classify_owners=False):
    """Snapshot cursor and owner bytes; process liveness is sampled once."""
    candidates = _compaction_cursor_paths(cwd, side)
    if candidates is None:
        return None, None
    evidence = []
    for path, name in candidates:
        if locked:
            with _compaction_cursor_lock(path) as held:
                if not held:
                    return None, None
                status, raw = _read_compaction_file(path)
                owner_path = (os.path.join(os.path.dirname(path), "session.json")
                              if name is not None else None)
                owner_status, owner_raw = (
                    _read_compaction_file(owner_path)
                    if owner_path is not None else ("missing", b""))
        else:
            status, raw = _read_compaction_file(path)
            owner_path = (os.path.join(os.path.dirname(path), "session.json")
                          if name is not None else None)
            owner_status, owner_raw = (
                _read_compaction_file(owner_path)
                if owner_path is not None else ("missing", b""))

        owner_state, owner_unknown = "live", False
        if name is not None:
            owner = None
            if owner_status == "valid":
                try:
                    session = json.loads(owner_raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    session = None
                if (isinstance(session, dict)
                        and session.get("kind") == side
                        and session.get("name") == name):
                    owner = peers._owner_of(session)
            if (owner is None
                    or peers.owner_key_version(owner)
                    != peers.PROCESS_FINGERPRINT_VERSION):
                owner_state, owner_unknown = "unknown", True
            elif locked or classify_owners:
                owner_state = peers._owner_liveness(owner)
                owner_unknown = owner_state == "unknown"
            else:
                # Revalidation compares the captured owner bytes. It must not
                # repeat a process probe while the catalog lock is held.
                owner_state = "captured"
        evidence.append(CompactionCursor(
            path, status, raw, name is not None, owner_status, owner_raw,
            owner_state, owner_unknown))
    signature = tuple(
        (os.path.relpath(item.path, cwd), item.status,
         hashlib.sha256(item.raw).hexdigest(), item.owner_status,
         hashlib.sha256(item.owner_raw).hexdigest())
        for item in evidence)
    return evidence, signature


def _compaction_cursor_block(item, reader_side, source, generation, size):
    """Why one relevant reader cannot release one gone source, if anything."""
    if item.named and item.owner_state == "dead":
        return None, True
    cursor, state = _parse_compaction_json(item.status, item.raw)
    if state == "invalid":
        return "invalid-or-unreadable", False
    if state == "missing":
        return None, False
    v4_key = anchored_page_cursor_key(reader_side)
    v3_key = page_cursor_key(reader_side)
    legacy_key = reader_side + "_seen"
    if v4_key in cursor:
        value = cursor.get(v4_key)
        if not _valid_v4_page_cursor(value):
            return "invalid-or-unreadable", False
        position = value["sources"].get(source)
        consumed = (position is None and source not in value["adopting_v3"])
        if position is not None:
            consumed = (position.get("gen") == generation
                        and position.get("offset", -1) >= size)
        if consumed:
            return None, False
        return ("unknown-owner" if item.named and item.owner_unknown
                else "unconsumed-v4"), False
    if v3_key in cursor:
        value = cursor.get(v3_key)
        if (not isinstance(value, dict)
                or value.get("v") != V3_PAGE_CURSOR_VERSION
                or not isinstance(value.get("sources"), dict)
                or not all(_valid_position(entry)
                           for entry in value["sources"].values())):
            return "invalid-or-unreadable", False
        return "selected-legacy", False
    if legacy_key in cursor:
        return "selected-legacy", False
    return None, False


def _compaction_result():
    return {
        "considered": 0, "retired": 0, "files": 0, "bytes": 0,
        "dormant": 0, "pending": 0, "retryable": 0,
        "blockers": {name: 0 for name in COMPACTION_BLOCKERS},
    }


def _valid_compaction_result(result):
    """Validate one internal result before aggregate arithmetic trusts it."""
    if not isinstance(result, dict):
        return False
    blockers = result.get("blockers")
    if not isinstance(blockers, dict):
        return False
    for key in COMPACTION_COUNTERS:
        value = result.get(key)
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 0):
            return False
    for key in COMPACTION_BLOCKERS:
        value = blockers.get(key)
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 0):
            return False
    return result["retryable"] <= blockers["snapshot-raced"]


def _compaction_snapshot_race(result, count=1, retryable=False):
    """Classify an uncertain snapshot without widening blocker contracts."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        result["blockers"]["snapshot-raced"] += 1
        return
    if count == 0:
        return
    result["blockers"]["snapshot-raced"] += count
    if retryable:
        result["retryable"] += count


def _analyze_compaction_kind(cwd, kind, lock_cursors):
    """Read the proof set shared by explicit compaction and doctor."""
    result = _compaction_result()
    reader_side = "codex" if kind == "claude" else "claude"
    snapshot = _catalog_snapshot(cwd, kind)
    loaded, view = snapshot.loaded, snapshot.view
    if (loaded.status != "valid" or view.state != "complete"
            or kind not in loaded.state["kinds"]):
        result["blockers"]["source-not-gone"] += 1
        return CompactionAnalysis(
            result, {}, (), loaded, view, (), None)
    records, structural, _retryable = _catalog_record_inventory(cwd, kind, view)
    by_relative = {record["relative"]: record for record in records}
    if structural:
        result["blockers"]["source-not-gone"] += structural
        return CompactionAnalysis(
            result, {}, (), loaded, view, tuple(records), None)
    groups = collections.defaultdict(list)
    for relative in view.candidates:
        record = by_relative.get(relative)
        if record is not None:
            groups[record["source"]].append(record)
    result["considered"] = len(groups)
    if lock_cursors:
        cursor_inputs, cursor_signature = _compaction_cursor_inputs(
            cwd, reader_side, locked=True)
    else:
        cursor_inputs, cursor_signature = _compaction_cursor_inputs(
            cwd, reader_side, locked=False, classify_owners=True)
    if cursor_inputs is None:
        _compaction_snapshot_race(result)
        return CompactionAnalysis(
            result, {}, (), loaded, view, tuple(records), None)
    result["dormant"] = sum(
        item.named and item.owner_state == "dead" for item in cursor_inputs)

    boundary = time.time() - LOOKBACK
    eligible = {}
    for source, source_records in groups.items():
        proofs = {
            (record.get("last_complete_generation"),
             record.get("last_complete_size"))
            for record in source_records
        }
        aged = all(
            _finite_number(record.get("last_complete_observed"))
            and record["last_complete_observed"] < boundary
            for record in source_records)
        gone = all(record.get("status") == "refused"
                   and record.get("reason") == "missing"
                   for record in source_records)
        if not (gone and aged and len(proofs) == 1
                and next(iter(proofs))[0] is not None
                and isinstance(next(iter(proofs))[1], int)):
            result["blockers"]["source-not-gone"] += 1
            continue
        generation, size = next(iter(proofs))
        blocker = None
        for item in cursor_inputs:
            reason, _ignored = _compaction_cursor_block(
                item, reader_side, source, generation, size)
            if reason is not None:
                blocker = reason
                break
        if blocker:
            result["blockers"][blocker] += 1
            continue
        eligible[source] = tuple(record["relative"] for record in source_records)

    detached = tuple(record for record in records
                     if record["relative"] not in set(view.candidates))
    journal, journal_status = _read_compaction_journal(cwd, kind)
    proved_detached = ({item["relative"] for item in journal["records"]}
                       if journal_status == "valid"
                       and journal["phase"] == "committed" else set())
    if journal_status != "missing":
        result["pending"] = 1
    # A record detached without a committed retirement journal has no cursor
    # proof attached to it. Leaking metadata is safer than guessing it retired.
    _compaction_snapshot_race(result, sum(
        record["relative"] not in proved_detached for record in detached))
    return CompactionAnalysis(
        result, eligible, detached, loaded, view, tuple(records),
        cursor_signature)


def _compaction_receipts(cwd, kind, eligible):
    receipts = []
    for relative in sorted({item for group in eligible.values()
                            for item in group}):
        status, raw = _read_compaction_file(
            _catalog_record_path(cwd, kind, relative))
        if status != "valid":
            return None
        receipts.append({
            "relative": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        })
    return receipts


def _compaction_record_signature(records):
    """Type-preserving values that authorized one retirement decision.

    Only records for the sources actually being retired belong here. Signing
    the whole inventory would let an unrelated live hook update starve an
    otherwise independent compaction forever. Canonical JSON distinguishes the
    JSON types Python's ordinary equality conflates (notably ``true``/``1`` and
    ``1``/``1.0``) while ignoring irrelevant object-key order and whitespace.
    """
    try:
        return tuple(sorted(
            (record["relative"], json.dumps(
                record, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), allow_nan=False))
            for record in records))
    except (KeyError, TypeError, ValueError):
        return None


def _finish_committed_compaction_locked(cwd, kind, journal):
    """Delete only records named by a durable committed proof receipt."""
    current = _read_source_catalog_raw(cwd)
    view = _catalog_view(cwd, kind, current)
    if current.status != "valid" or view.state != "complete":
        return 0, 0, 1, True
    candidates = set(view.candidates)
    remaining = []
    files = reclaimed = raced = 0
    for receipt in journal["records"]:
        relative = receipt["relative"]
        path = _catalog_record_path(cwd, kind, relative)
        status, raw = _read_compaction_file(path)
        if status == "missing":
            continue
        if (relative in candidates or status != "valid"
                or len(raw) != receipt["bytes"]
                or hashlib.sha256(raw).hexdigest() != receipt["sha256"]):
            raced += 1
            continue
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raced += 1
                continue
            os.unlink(path)
        except OSError:
            remaining.append(receipt)
            continue
        files += 1
        reclaimed += info.st_size
    if remaining:
        pending = json.loads(json.dumps(journal))
        pending["records"] = remaining
        _write_compaction_journal(cwd, pending)
    else:
        _remove_compaction_journal(cwd, kind)
    _cleanup_catalog_manifests(cwd)
    pending = os.path.lexists(_compaction_journal_path(cwd, kind))
    return files, reclaimed, raced, pending


def _resume_compaction_kind(cwd, kind):
    """Recover a crash residue before considering another retirement."""
    result = _compaction_result()
    with catalog_lock(cwd) as locked:
        if not locked:
            _compaction_snapshot_race(result, retryable=True)
            return result, True
        journal, status = _read_compaction_journal(cwd, kind)
        if status == "missing":
            return result, False
        if status != "valid":
            _compaction_snapshot_race(result)
            result["pending"] = 1
            return result, True
        if journal["phase"] == "prepared":
            if not _recover_prepared_compactions_locked(cwd):
                _compaction_snapshot_race(result)
            pending = os.path.lexists(_compaction_journal_path(cwd, kind))
            result["pending"] = 1 if pending else 0
            return result, pending
        files, reclaimed, raced, pending = _finish_committed_compaction_locked(
            cwd, kind, journal)
        result["files"] += files
        result["bytes"] += reclaimed
        _compaction_snapshot_race(result, raced)
        result["pending"] = 1 if pending else 0
    return result, pending


def _compact_catalog_kind(cwd, kind):
    """Conservatively retire cursor-proved, aged, already-gone candidates."""
    result, pending_receipt = _resume_compaction_kind(cwd, kind)
    if result["blockers"]["snapshot-raced"] or pending_receipt:
        return result
    analysis = _analyze_compaction_kind(cwd, kind, lock_cursors=True)
    for key in ("considered", "dormant", "pending", "retryable"):
        result[key] += analysis.result[key]
    for key in COMPACTION_BLOCKERS:
        result["blockers"][key] += analysis.result["blockers"][key]
    eligible = analysis.eligible
    loaded, view = analysis.loaded, analysis.view
    cursor_signature = analysis.cursor_signature
    if not eligible:
        return result
    reader_side = "codex" if kind == "claude" else "claude"

    old_state = json.loads(json.dumps(loaded.state))
    old_entry = old_state["kinds"][kind]
    old_generation = old_entry["generation"]
    retired_relatives = {
        relative for relatives in eligible.values() for relative in relatives}
    analyzed_records = {
        record["relative"]: record for record in analysis.records
        if record["relative"] in retired_relatives}
    record_signature = _compaction_record_signature(
        analyzed_records.values())
    if (set(analyzed_records) != retired_relatives
            or record_signature is None):
        _compaction_snapshot_race(result, max(1, len(eligible)))
        return result
    retained = tuple(relative for relative in view.candidates
                     if relative not in retired_relatives)

    with catalog_lock(cwd) as locked:
        if not locked:
            _compaction_snapshot_race(
                result, max(1, len(eligible)), retryable=True)
            return result
        current = _read_source_catalog_raw(cwd)
        current_view = _catalog_view(cwd, kind, current)
        current_entry = (((current.state or {}).get("kinds") or {}).get(kind))
        if (current.status != "valid" or current_view.state != "complete"
                or not isinstance(current_entry, dict)
                or current.state != old_state
                or current_entry.get("generation") != old_generation
                or tuple(current_view.candidates) != tuple(view.candidates)):
            _compaction_snapshot_race(
                result, max(1, len(eligible)), retryable=True)
            return result
        current_records = [
            _read_catalog_record(cwd, kind, relative)
            for relative in sorted(retired_relatives)]
        if (any(record is None for record in current_records)
                or _compaction_record_signature(current_records)
                != record_signature):
            _compaction_snapshot_race(
                result, max(1, len(eligible)), retryable=True)
            return result
        _inputs, signature = _compaction_cursor_inputs(
            cwd, reader_side, locked=False)
        if signature != cursor_signature:
            _compaction_snapshot_race(
                result, max(1, len(eligible)), retryable=True)
            return result
        for relatives in eligible.values():
            if any(_observe_catalog_candidate(cwd, kind, relative).get("reason")
                   != "missing" for relative in relatives):
                _compaction_snapshot_race(result, retryable=True)
                return result

        generation = uuid.uuid4().hex
        base_name = _catalog_manifest_name(kind, generation, "base")
        delta_name = _catalog_manifest_name(kind, generation, "delta")
        common = {
            "v": CATALOG_VERSION, "project": os.path.abspath(cwd),
            "kind": kind, "generation": generation,
            "root_stamp": old_entry.get("root_stamp"),
        }
        base_manifest = dict(common, phase="base", paths=list(retained))
        delta_manifest = dict(common, phase="delta", paths=[])
        if (not _write_catalog_manifest(cwd, base_name, base_manifest)
                or not _write_catalog_manifest(
                    cwd, delta_name, delta_manifest)):
            _compaction_snapshot_race(result, len(eligible))
            return result
        new_state = json.loads(json.dumps(old_state))
        new_state["kinds"][kind] = {
            "generation": generation, "phase": "complete",
            "base_manifest": base_name, "base_next": len(retained),
            "delta_manifest": delta_name, "delta_next": 0,
            "root_stamp": old_entry.get("root_stamp"), "inflight": None,
        }
        receipts = _compaction_receipts(cwd, kind, eligible)
        if receipts is None:
            _compaction_snapshot_race(result, len(eligible))
            return result
        journal = {
            "v": COMPACTION_JOURNAL_VERSION,
            "project": os.path.abspath(cwd), "kind": kind,
            "phase": "prepared", "old_state": old_state,
            "new_state": new_state, "records": receipts,
        }
        if not _write_compaction_journal(cwd, journal):
            _compaction_snapshot_race(result, len(eligible))
            return result
        if not _write_catalog_state(cwd, new_state, cleanup=False):
            # A failed caller may have reached os.replace and then lost its
            # acknowledgement. Inspect through the prepared journal recovery;
            # never delete the only durable route back to the old view.
            _recover_prepared_compactions_locked(cwd)
            _compaction_snapshot_race(result, len(eligible))
            return result

        _inputs, after_signature = _compaction_cursor_inputs(
            cwd, reader_side, locked=False)
        sources_still_gone = all(
            _observe_catalog_candidate(cwd, kind, relative).get("reason")
            == "missing"
            for relatives in eligible.values() for relative in relatives)
        if after_signature != cursor_signature or not sources_still_gone:
            _recover_prepared_compactions_locked(cwd)
            # On rollback failure the prepared journal remains. Every current
            # reader overlays its old state until a later mutation recovers it.
            _compaction_snapshot_race(
                result, len(eligible), retryable=True)
            return result

        committed = json.loads(json.dumps(journal))
        committed["phase"] = "committed"
        if not _write_compaction_journal(cwd, committed):
            _recover_prepared_compactions_locked(cwd)
            _compaction_snapshot_race(result, len(eligible))
            return result

        files, reclaimed, raced, pending = _finish_committed_compaction_locked(
            cwd, kind, committed)
        result["files"] += files
        result["bytes"] += reclaimed
        _compaction_snapshot_race(result, raced)
        result["pending"] = 1 if pending else 0
        result["retired"] += len(eligible)
    return result


def _scan_source_catalogs(cwd):
    """Complete both catalogs and return scanner totals without printing."""
    processed = refused = gone = 0
    failed = False
    for kind in ("claude", "codex"):
        kind_refused = 0
        loaded = _read_source_catalog(cwd)
        entry = ((loaded.state or {}).get("kinds") or {}).get(kind)
        force = bool(entry and entry.get("phase") == "complete")
        for _iteration in range(100000):
            progress = _catalog_scan_step(cwd, kind, force=force)
            force = False
            processed += progress.processed
            kind_refused = progress.refusals
            gone += progress.gone
            if progress.state == "degraded":
                failed = True
                break
            if progress.state == "complete":
                break
        else:
            failed = True
        refused += kind_refused
    return failed, processed, refused, gone


def sources(action=None):
    """Build or refresh the durable transcript-source catalog explicitly."""
    if action not in ("scan", "compact"):
        print("antiphon: sources takes `scan` or `compact`: "
              "`antiphon sources scan` | `antiphon sources compact`",
              file=sys.stderr)
        return 2
    cwd = project_dir()
    failed, processed, refused, gone = _scan_source_catalogs(cwd)
    if action == "compact":
        combined = _compaction_result()
        classification_untrusted = False
        if failed or refused:
            combined["blockers"]["source-not-gone"] = max(1, refused)
        else:
            for kind in ("claude", "codex"):
                partial = _compact_catalog_kind(cwd, kind)
                if not _valid_compaction_result(partial):
                    classification_untrusted = True
                    _compaction_snapshot_race(combined)
                    continue
                for key in COMPACTION_COUNTERS:
                    combined[key] += partial[key]
                for key in COMPACTION_BLOCKERS:
                    combined["blockers"][key] += partial["blockers"][key]
        blockers = ", ".join(
            f"{key}={combined['blockers'][key]}"
            for key in COMPACTION_BLOCKERS)
        blocked = sum(combined["blockers"].values())
        state = ("refused" if blocked else
                 "pending" if combined["pending"] else "complete")
        print(f"source compaction: {state}; {combined['considered']} considered; "
              f"{combined['retired']} retired; {combined['files']} files / "
              f"{combined['bytes']} bytes reclaimed; {combined['dormant']} "
              f"dormant readers ignored; {combined['pending']} cleanup "
              f"transaction(s) pending; blockers {blockers}")
        snapshot_raced = combined["blockers"]["snapshot-raced"]
        retryable = combined["retryable"]
        if classification_untrusted or not _valid_compaction_result(combined):
            print("source compaction: snapshot classification could not be "
                  "trusted; no automatic remedy was attempted")
            return 1
        persistent = snapshot_raced - retryable
        if retryable:
            print(f"source compaction: {retryable} input snapshot(s) changed "
                  "while proofs were checked; retry `antiphon sources compact`")
        if persistent:
            print(f"source compaction: {persistent} proof failure(s) could not "
                  "be interpreted as a transient snapshot change; no automatic "
                  "remedy was attempted")
        return 1 if blocked or combined["pending"] else 0
    state = "degraded" if failed or refused else "complete"
    cleanup_pending = _catalog_cleanup_pending(cwd)
    cleanup = ("cleanup pending unknown" if cleanup_pending is None else
               f"{cleanup_pending} cleanup pending")
    print(f"source catalog: {state}; {processed} candidate(s) inspected; "
          f"{refused} refused; {gone} gone; {cleanup}")
    return 1 if state != "complete" else 0


def print_summary(side="claude"):
    cwd = project_dir()
    text, _, _ = build_summary(cwd, side, since=time.time() - LOOKBACK)
    print(text or "(nothing new)")
    return 0


COMMANDS = {
    "setup": setup, "status": status, "doctor": doctor, "catch-up": catch_up,
    "sources": sources, "retrieve": retrieve,
    "hook": hook, "summary": print_summary,
    "push": push, "reply": reply, "mcp": mcp, "register_peer": register_peer,
    "unregister_peer": unregister_peer, "retrieve_mcp": _retrieve_mcp_bridge,
    "claude_identity": _claude_identity_bridge,
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
    # Answered before the command table on purpose. As a `COMMANDS` entry
    # `help` would pick up the arity check below, and `antiphon help doctor`
    # would exit 2 with "takes no arguments" instead of printing the usage.
    # Asking how to use something is not an error, so this exits 0; an unknown
    # command still exits 1.
    if command in ("--help", "-h", "help"):
        print(__doc__)
        sys.exit(0)
    # Same shape as help, same reason. The number is the one in package.json
    # beside this copy — the one npm installed — so a release bumps one file.
    if command in ("--version", "-V", "version"):
        print(f"antiphon {_package_version(_package_root()) or 'unknown'}")
        sys.exit(0)
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
