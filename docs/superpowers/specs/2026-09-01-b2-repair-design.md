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

That refusal is **classified**, not a transport error. The listener replies
`ok:false` carrying a stable non-transport `refusal_class` and an actionable
remedy, flushes that response, and only then self-retires. The delivery attempt
is therefore its own wakeup, and cleanup stops depending on a control that may
never arrive. Python preserves that class through `_ClassifiedRefusal` rather
than recasting every negative reply as `transport`, so a sender learns the
identity moved instead of that the socket failed.

The retire control becomes an optimisation rather than a correctness mechanism:
routing is already safe from the proof, and cleanup is already guaranteed by the
first stale delivery attempt.

RED test, the race named exactly: sender resolves A; hook B commits the proof;
the wakeup is withheld; the sender connects to A's socket. Assert the listener
refuses with the classified reason, that zero channel notifications are emitted,
that the response is flushed before the socket closes, and that A has
self-retired afterwards.

## 3. The proof record

- One file per owner under the project's registry area, named from a digest of
  the owner key, never from the raw key.
- Fields: version, kind, the validated canonical owner key, the owner-key
  digest that names the path, the current canonical session id, the identity
  digest, and the written time. The owner key is stored because liveness is a
  fact about a pid and a start time, and a digest is one-way: without the key,
  an owner's death can never be proved, only assumed. It is private and is
  never rendered on any surface.
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
- Per owner and replaced in place, so it does not grow.
- **The proof deliberately outlives endpoints.** An owner with zero automatic
  peers is not a reason to delete it: that is precisely the reconnect window of
  §6, where B is the current identity and has no endpoint yet. Deleting the
  proof there would erase the only evidence that B exists, and `status` and
  `doctor` would have nothing to name.
- It is removed only when its owner is **positively proved dead**, and only on
  a mutation path that is already writing the registry. A read-only surface —
  `status`, `doctor`, any resolver — never prunes it. Unproved or unknown
  liveness leaves it alone: an unreclaimed record costs a file, and a wrongly
  reclaimed one costs a live session its identity.

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
two readers are bound by parity tests rather than by assertion. Parity is proved over the **whole readiness predicate** — endpoint, session
half and owner-current proof together — not over proof parsing alone. Parsing
agreement would still let the resolver and the listener disagree about a
composite, and the composite is the thing that decides delivery.

The parity suite drives both readers over identical fixtures and requires
identical verdicts for: a valid current proof; a proof that is missing,
wrong-version, wrong-kind, malformed-digest, non-canonical-id, torn, empty, or
naming a different session; a missing or torn session half; a missing or torn
endpoint; and owner, digest, pid and address mismatches between the three
halves.

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
projection, no route, no status peer, and Node does not probe.

Ignoring a legacy observation by predicate is impossible and the design does
not pretend otherwise. An observation record carries version, kind, session id
and time — nothing that says which environment wrote it — so a later resolver
cannot tell a bug-created one from a legitimate one, and a stale record with a
positively held writer lock would keep projecting. The configured-invalid hook
therefore **withdraws durably**: it idempotently retires the observation for its
own canonical session id, and only that id. It never touches another session's
record, and it renders no path or id while doing it. Absence alone
enables automatic identity.

Empty and whitespace remain **absent**: `explicit_name()` documents "the name
set for this session, or `""` when none was set", and `fresh-user.sh` uses bare
`ANTIPHON_NAME=` in ten places as the way to run unnamed. `UPPER` is not a
counter-example — it normalizes to the valid name `upper` and was never the
defect.

## 8. The probe is bounded at the read

Fixed argv; streaming reads; strict decode; JSON parsed only after bounded
completion. The bounds are literal, not adjectival:

| bound | value | why |
| --- | --- | --- |
| deadline | 2.0 s | the existing `CLAUDE_IDENTITY_TIMEOUT`, unchanged |
| stdout ceiling | 32,768 bytes | the same number `channel.mjs` already uses as `maxBuffer` on this probe, so the two sides cannot disagree about what is too large |
| stderr ceiling | 8,192 bytes | room for a real diagnostic and no more |
| total retained | 40,960 bytes | their sum, and nothing else is held |
| overflow boundary | the byte that would exceed a ceiling | a limit on what is retained triggers at the first byte past it, not after the read completes |
| terminate to kill | 250 ms, then `SIGKILL` | bounded |
| reap wait | 250 ms | a child that cannot be reaped is reported, never awaited forever |
| retire control | 250 ms total, no reply awaited | connect, send, close; any error swallowed |

On timeout or either overflow: terminate, then kill and reap, and return
unnamed. None of the captured bytes are emitted.

The child starts in its own session before any group-directed signal. If that
isolation cannot be established, only the child pid is signalled — never widen a
signal past a boundary you failed to create.

## 9. Privacy

One central redactor per language, applied **before** truncation and preserving
`refusal_class`. It removes unanchored UUIDs case-insensitively, full identity
digests, automatic routes, and raw owner keys, while preserving the public
`auto-` alias and the remedy.

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
