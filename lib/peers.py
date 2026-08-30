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

import hashlib
import os
import re

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
