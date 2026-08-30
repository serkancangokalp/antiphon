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
import errno
import hashlib
import json
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
SUMMARY_BUDGET = 2600     # character budget for the injected summary
EVENT_LIMIT = 40          # max events that go into the summary
LOOKBACK = 6 * 3600       # anything older than this doesn't count as part of "this session"

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


def deliver_batches(batches, sent, deliver):
    """Calls `deliver(recipient, messages)` for each batch that has not gone yet.

    A recipient's fingerprint advances only if its own delivery succeeded, so one
    failure does not suppress its retry while another recipient's success is
    kept. The key is `""` for unaddressed and `"@alias"` otherwise, so a peer
    named the empty string cannot collide with the unaddressed slot.
    """
    for recipient, messages in batches.items():
        key = "" if recipient is None else f"@{recipient}"
        fingerprint = batch_fingerprint(messages)
        if sent.get(key) == fingerprint:
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


def read_cursor(cwd, kind):
    new_path = state_path(cwd, kind)
    try:
        with open(new_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    translated = _translate_cursor_keys(data)
    if translated != data:
        write_cursor(cwd, translated, kind)
    return translated


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


def claude_events(cwd, start=0.0):
    """(time, type, text) — type: you | claude | tool"""
    events = []
    for path in claude_transcripts(cwd)[:3]:
        for line in tail_lines(path):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("isMeta"):
                continue
            ts = iso_epoch(d.get("timestamp"))
            if ts <= start:
                continue
            kind = d.get("type")
            msg = d.get("message") or {}
            content = msg.get("content")
            if kind == "user":
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(c.get("text", "") for c in content
                                    if isinstance(c, dict) and c.get("type") == "text")
                text = text.strip()
                # tool results and system injections are not the user's own words
                if text and not text.startswith("<") and not _is_self_injected(text):
                    events.append((ts, "you", text))
            elif kind == "assistant":
                for c in content if isinstance(content, list) else []:
                    if c.get("type") == "text" and c.get("text", "").strip():
                        events.append((ts, "claude", c["text"].strip()))
                    elif c.get("type") == "tool_use":
                        i = c.get("input") or {}
                        detail = i.get("file_path") or i.get("command") or i.get("pattern") or ""
                        events.append((ts, "tool", f"{c.get('name', '?')} {detail}".strip()))
    return sorted(events)


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


def codex_events(cwd, start=0.0):
    """(time, type, text) — type: you | codex | tool"""
    events = []
    for path in codex_rollout_files(cwd)[:3]:
        for line in tail_lines(path):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = iso_epoch(d.get("timestamp"))
            if ts <= start:
                continue
            kind, p = d.get("type"), d.get("payload") or {}
            if kind == "response_item" and p.get("type") == "message":
                role = p.get("role")
                text = " ".join(
                    c.get("text") or c.get("input_text") or ""
                    for c in p.get("content") or [] if isinstance(c, dict)
                ).strip()
                if not text or role == "developer":
                    continue
                if role == "user":
                    if text.startswith("<") or _is_self_injected(text):
                        continue
                    events.append((ts, "you", text))
                elif role == "assistant":
                    events.append((ts, "codex", text))
            elif kind == "event_msg" and p.get("type") == "exec_command_begin":
                cmd = p.get("command")
                if isinstance(cmd, list):
                    cmd = " ".join(cmd)
                if cmd:
                    events.append((ts, "tool", f"shell {cmd}"))
    return sorted(events)


# ---------- summary ----------

LABEL = {"you": "YOU", "claude": "Claude", "codex": "Codex", "tool": "·"}


# side -> (the other side's key, its display name for headings, its phrasing in notices)
OTHER_SIDE = {
    "claude": ("codex", "Codex", "from Codex"),
    "codex": ("claude", "Claude Code", "from Claude Code"),
}


def build_summary(cwd, side, start=0.0):
    """`side` is the side that will READ the summary ('claude' | 'codex').
    Turns what happened on the other side, and what the user said, into
    compact text.

    Returns: (text, last_event_time, message_count). `message_count` doesn't
    count tool calls — the notice shown in the terminal uses it."""
    if side == "claude":
        events = codex_events(cwd, start)
    else:
        events = claude_events(cwd, start)
    other = OTHER_SIDE[side][1]

    if not events:
        return "", 0.0, 0

    events = events[-EVENT_LIMIT:]
    last_time = events[-1][0]
    count = sum(1 for _, kind, _ in events if kind != "tool")

    lines = []
    tools = []
    for ts, kind, text in events:
        if kind == "tool":
            tools.append(truncate(text, 70))
            continue
        if tools:
            lines.append(f"  · {len(tools)} tool calls: " + truncate(" | ".join(tools[-3:]), 130))
            tools = []
        clock = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"[{clock}] {LABEL.get(kind, kind)}: {truncate(text, 420)}")
    if tools:
        lines.append(f"  · {len(tools)} tool calls: " + truncate(" | ".join(tools[-3:]), 130))

    body = "\n".join(lines)
    truncated = False
    if len(body) > SUMMARY_BUDGET:
        # keep the newest; trim from the front
        while len(body) > SUMMARY_BUDGET and len(lines) > 1:
            lines.pop(0)
            body = "\n".join(lines)
        truncated = True

    head = f"## What happened on the {other} side (since your last turn)"
    foot = ("\nThis record belongs to the Antiphon bridge — this is what actually happened "
            "there. Do not assume anything that is not in it.")
    if truncated:
        foot = "\n(older lines were cut for budget)" + foot
    return f"{head}\n{body}{foot}", last_time, count


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

    cursor = read_cursor(cwd, side)
    key = f"{side}_seen"
    start = float(cursor.get(key) or (time.time() - LOOKBACK))
    text, last, _ = build_summary(cwd, side, start)
    if text and last:
        cursor[key] = last
        write_cursor(cwd, cursor, side)

    if not text:
        return 0

    # The hook prints nothing to the terminal. The counter used to say
    # "message" but it was counting the other side's transcript events;
    # incoming channel messages already show up via their own notices.
    # Context is injected silently.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }, ensure_ascii=False))
    return 0


def notice_text(side, count):
    """The one-line notice in `status` output (the hook no longer uses this)."""
    noun = "message" if count == 1 else "messages"
    return f"💬 {count} new {noun} {OTHER_SIDE[side][2]}"


# ---------- push (both directions) ----------

def last_claude_reply(transcript_path):
    """Returns the most recent assistant text in the Claude transcript."""
    chunks = []
    for line in tail_lines(transcript_path):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant" or d.get("isMeta"):
            continue
        content = (d.get("message") or {}).get("content")
        texts = [c.get("text", "") for c in content or []
                if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            chunks = texts            # each new assistant message supersedes the last
    return "\n".join(chunks).strip()


def last_codex_reply(transcript_path):
    """Returns the most recent assistant text in the Codex rollout."""
    chunks = []
    for line in tail_lines(transcript_path):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = d.get("payload") or {}
        if (d.get("type") != "response_item" or p.get("type") != "message"
                or p.get("role") != "assistant"):
            continue
        texts = [c.get("text") or c.get("output_text") or c.get("input_text") or ""
                for c in p.get("content") or [] if isinstance(c, dict)]
        if any(texts):
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

    reply_reader = last_claude_reply if target == "codex" else last_codex_reply
    reply_text = reply_reader(transcript)
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
    cursor = read_cursor(cwd, side)
    key = f"last_pushed_{target}"
    sent, already = migrate_pushed(cursor.get(key), batches.get(None) or [])
    if already:
        sent[""] = batch_fingerprint(batches[None])
    before = dict(sent)

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

    updated = forget_superseded(deliver_batches(batches, sent, deliver))
    # Written only when something actually moved: a delivery landed, or the old
    # string format was recognised and needs recording in the new one. A turn
    # that delivered nothing leaves the cursor file alone.
    if updated != before or already:
        cursor[key] = updated
        write_cursor(cwd, cursor, side)
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
            return False, detail      # waiting cannot make two peers into one
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
    cursor = read_cursor(cwd, side)
    key = f"last_pushed_{target}"
    held = cursor.get(key)
    sent = dict(held) if isinstance(held, dict) else {}
    if isinstance(held, str):
        sent[LEGACY_SLOT] = held
    sent["" if alias is None else f"@{alias}"] = batch_fingerprint([text])
    cursor[key] = forget_superseded(sent)
    write_cursor(cwd, cursor, side)


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
    "description": ("Returns what happened on the Claude Code side since your last turn. "
                    "This normally arrives automatically via the hook, no extra effort "
                    "required; this tool is the fallback — call it by hand if you suspect "
                    "the bridge has gone quiet."),
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


def _mcp_result(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


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
                "serverInfo": {"name": "antiphon", "version": "0.1.0"},
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
                cursor = read_cursor(cwd, "codex")
                start = float(cursor.get("codex_seen") or (time.time() - LOOKBACK))
                text, last, _ = build_summary(cwd, "codex", start)
                if text and last:
                    cursor["codex_seen"] = last
                    write_cursor(cwd, cursor, "codex")
                output = text or "Nothing new on the Claude Code side since your last turn."
                _mcp_result(mid, {"content": [{"type": "text", "text": output}]})
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
               "turn — you don't need to do anything else. If you suspect the bridge has gone "
               "quiet, you can call the `antiphon_read` tool by hand.\n\n"
               "When Claude wants to tell you something directly, you'll see it as a user "
               "message starting with `[Antiphon bridge] Claude:` (pushed from Claude's Stop "
               "hook) or `[Antiphon channel] Claude:` (a direct reply through the channel) — "
               "either way, these are Claude's words, not the user's. After that prefix comes "
               "`[from=<alias> id=<uuid>]`, naming which Claude peer spoke: reply to that one "
               "with `antiphon_send(to=<alias>)` or `@claude:<alias>`. A literal "
               "`from=<unnamed>` means that peer has no name and cannot be addressed back — "
               "with only one Claude peer live you can leave the recipient out entirely. The "
               "id names one delivery attempt; nothing routes replies by it. When you want to hand "
               "Claude a task directly, put `@claude` at the start of a line in your reply; "
               "only that line is sent to the Claude session as an MCP Channel "
               "event. Write `@claude:name` when several Claude peers are live — "
               "an unaddressed line is refused rather than sent to one of them. To reach Claude without ending your turn, call the `antiphon_send` "
               "tool instead: it delivers immediately, so Claude can start working while "
               "you carry on, and `antiphon_read` picks up the answer later in the same "
               "turn.\n")

CLAUDE_RULE = ("\n## The Antiphon bridge\n\n"
               "You are working alongside another agent on this project. What happens on the "
               "other side is injected into your context at the start of each turn. Events "
               "that come directly from that agent are marked "
               "`<channel source=\"antiphon\" sender=\"codex\" sender_kind=\"agent\" "
               "sender_alias=\"...\">`; they "
               "are the words of the Codex agent, not of the human user. Use the "
               "`reply_to_codex` tool to answer them, passing `sender_alias` back "
               "as `to` whenever it is non-null: a bare reply is refused as soon "
               "as any named Codex peer is live, because unnamed sessions leave "
               "no registry record and cannot be ruled out. A null `sender_alias` "
               "means that peer has no name: it cannot be answered by name, and "
               "a bare reply reaches it only where nothing is registered.\n")


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
    if failures:
        listed = "\n  ".join(failures)
        print(f"\n✗ setup did not finish. {len(failures)} file(s) were left untouched "
              f"because they could not be read:\n  {listed}\n"
              "  Fix or move them, then run `antiphon setup` again.", file=sys.stderr)
        return 1
    return 0


# ---------- status, for humans ----------

def _peer_report(cwd):
    """The `Peers:` block and the addressing hints under it, as lines.

    Empty when nothing is registered, which is the unnamed single pair: there
    is nobody to choose between, so there is nothing to say.

    Sorted by side and then name. `read_peers` orders by start time, which is
    right for resolution and wrong here — a list that reshuffles whenever a
    session restarts is a list nobody can read twice.
    """
    live = {kind: sorted(peers.read_peers(cwd, kind),
                         key=lambda peer: peer.get("name") or "")
            for kind in ("claude", "codex")}
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


def status():
    cwd = project_dir()
    print(f"project: {cwd}\n")
    c = claude_transcripts(cwd)
    x = codex_rollout_files(cwd)
    print(f"Claude transcripts: {len(c)} files" + ("" if c else " — none"))
    print(f"Codex rollouts:     {len(x)} files" + ("" if x else " — none"))
    # Derived from the registry when anything is registered: a named session
    # serves its own socket, so probing the project-wide path would report a
    # working channel as down. The path itself is never printed either way.
    registered = peers.read_peers(cwd, "claude")
    channel = ("live" if registered
               else "live" if os.path.exists(claude_socket_path(cwd)) else "down")
    print(f"Claude channel:     {channel}")
    for line in _peer_report(cwd):
        print(line)
    cursor = read_cursor(cwd, "claude")
    for k, v in (cursor or {}).items():
        if k.endswith("_seen") and isinstance(v, (int, float)):
            shown = datetime.fromtimestamp(v).strftime('%H:%M:%S') if v else '—'
        else:
            shown = truncate(str(v), 80) if v else '—'
        print(f"cursor {k}: {shown}")
    for side in ("claude", "codex"):
        start = float((cursor or {}).get(f"{side}_seen") or (time.time() - LOOKBACK))
        text, _, count = build_summary(cwd, side, start)
        print(f"\n=== what {side} would see ===")
        if count:
            print(notice_text(side, count))
        print(text or "(nothing new)")
    return 0


def print_summary(side="claude"):
    cwd = project_dir()
    text, _, _ = build_summary(cwd, side, time.time() - LOOKBACK)
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
