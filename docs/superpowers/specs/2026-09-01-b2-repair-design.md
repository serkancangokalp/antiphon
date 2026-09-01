# B2 Repair Design — automatic identity that cannot outlive its session

**Date:** 2026-09-01
**Base:** `d89d983`
**Scope:** the one Critical and three Important findings from the independent
exact-SHA review of B2. Nothing wider.

## 1. The defect, reproduced

Measured at `d89d983` with a correct fixture — `peers.register` plus
`write_session`, a real owner key, a real bound socket:

1. endpoint and session registered for `auto-A`; `@claude:auto-A` resolves to
   an address. Correct while the session is A.
2. the session rotates to B and hook B runs; it correctly cannot join —
   "automatic peer `auto-B…` has no matching live endpoint".
3. `@claude:auto-A` still resolves to an address: the socket now served by the
   process running session B.

Two of the three mechanisms were already correct. `sender_alias` derives the
name from the *current* session id and requires name, owner, digest and session
address to agree, so a B session can never sign as A. The join guard already
refuses B. Exactly one fact is missing: nothing supersedes A's session half when
the same owner's current session becomes B. `_session_address` joins the halves
on owner and nothing else, so a stale session record stays authoritative.

The owner-current proof is therefore the missing fact, not a compensating
cleanup pass.

## 2. Delivery linearizes at the listener

The check cannot live only on the resolve side. The retire control is best
effort, so a sender may resolve A while A is current, the hook may then commit
the proof, and the sender may connect afterwards.

Node validates the owner-current proof on **every inbound delivery**, before
emitting `notifications/claude/channel`. The linearization point is that
validation. With it the invariant holds even if cleanup never runs and the
wakeup never arrives: a connection arriving after the proof moved is refused at
the moment of delivery and emits no notification.

## 3. The proof record

- One file per owner under the project's registry area, named from a digest of
  the owner key, never from the raw key.
- Fields: version, kind, owner-key digest, current canonical session id,
  identity digest, written time.
- Written in full then atomically replaced, under the existing registry lock.
  Never rendered on any surface.
- Read validation is strict and total. Wrong version, wrong kind, missing or
  non-canonical session id, malformed digest, and a torn or empty record all
  read as **no proof**.
- **No proof is not agreement.** An automatic peer without a valid current
  proof is neither routable nor deliverable. Pre-upgrade records fail closed
  rather than inheriting trust, and old writers are never rewritten or guessed.
- Explicit-name and configured-invalid hooks never write it. The proof is a
  fact about automatic identity alone.
- Per owner and replaced in place, so it does not grow; removed when that
  owner's last automatic peer is unregistered.

## 4. Withdrawal

- The hook may remove **only** an automatic session record whose owner matches
  its own and whose identity digest is no longer current. Never an endpoint,
  never an explicit peer, never another owner's record.
- The retire control is content-free, single-shot, bounded and non-patient. Any
  error, timeout or refusal is swallowed: it cannot fail the hook and cannot
  cost it. The Stop hook is the hottest path on the bridge, and the proof has
  already made routing safe without the control.
- The listener alone unregisters its own pid-owned endpoint and closes only its
  channel socket.

## 5. Two readers, one format, parity proved

Node cannot call a Python function, so "one predicate" would be a claim this
design could not cash. Shelling out to Python is this codebase's pattern for
shared contracts, but §2 puts the check on every inbound delivery, and a
subprocess per delivered message is both a cost and a new failure mode on the
hot path.

Node therefore reads the proof file directly with mirrored validation, and the
two readers are bound by parity tests rather than by assertion. The parity suite
drives both over identical fixtures and requires identical verdicts for: a valid
current proof; a proof naming a different session; a missing file; a wrong
version; a wrong kind; a malformed digest; a non-canonical session id; a torn
record; and an empty file.

## 6. The accepted cost, named

There is no dynamic rename of a live listener: a process serving under one
identity must not silently become another. The consequence is a real cost to a
person, and it is accepted deliberately.

After a session rotates A→B, that terminal is **unreachable by automatic
identity until a fresh endpoint exists** — in practice an MCP reconnect. The
hook, `status`, `doctor` and the written surfaces must say so with the public
alias and an actionable remedy, and must never show a UUID, an identity digest
or a route while saying it.

## 7. Configured names

Presence and usability are different facts. A normalized non-empty unusable
value is a configured-invalid override: no observation, no automatic
projection, no route, no status peer, and Node does not probe. Absence alone
enables automatic identity.

Empty and whitespace remain **absent**: `explicit_name()` documents "the name
set for this session, or `""` when none was set", and `fresh-user.sh` uses bare
`ANTIPHON_NAME=` in ten places as the way to run unnamed. `UPPER` is not a
counter-example — it normalizes to the valid name `upper` and was never the
defect.

## 8. The probe is bounded at the read

Fixed argv; streaming reads under a 2s deadline; exact byte ceilings on stdout
and on stderr, so total retained memory is bounded by their sum; strict decode;
JSON parsed only after bounded completion. On timeout or either overflow:
terminate, then kill and reap, and return unnamed. None of the captured bytes
are emitted.

The child starts in its own session before any group-directed signal. If that
isolation cannot be established, only the child pid is signalled — never widen a
signal past a boundary you failed to create.

## 9. Privacy

One central redactor per language, applied **before** truncation and preserving
`refusal_class`. It removes unanchored UUIDs case-insensitively, full identity
digests, and automatic routes, while preserving the public `auto-` alias and the
remedy.

Surfaces: `README.md`, `BACKLOG.md`, `CLAUDE_RULE`, `AGENTS_RULE` and the
channel instructions. Behavioural fixtures for reply, Stop, startup, status,
doctor and queue.

Two decisions written down rather than left to fall out of a pattern: a
truncated session-id prefix counts as session identity, so it leaves doctor
queue output in favour of an aggregate; and explicit or legacy peers may show a
path because it is actionable for them, while automatic peers may not.

## 10. Verification

The A→B red crosses the real Codex/Claude hook, the real Python resolver and a
real Node channel — not registry units. T2 uses the real hook with a positively
held writer lock. T3 uses real children that keep writing on stdout and on
stderr. Full Python and Node suites, static checks, a clean commit, and
`fresh-user.sh` on that exact SHA, then independent review.

No push, merge, version, publish, or live cursor, registry, config or socket
state belongs to this repair.
