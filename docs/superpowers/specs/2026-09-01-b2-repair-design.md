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

Exactly one fact is missing, and it surfaces in two places rather than one.
The missing fact: nothing supersedes A's session half when the same owner's
current session becomes B. `_session_address` joins the halves on owner and
nothing else, so a stale session record stays authoritative forever.

Already correct: the join guard refuses B, and Python's Stop-signing identity
`claimed_alias` derives its name from the *current* session id and requires
name, owner, digest and session address to agree, so a B session cannot sign as
A on that path.

Stale, both reading state that nothing superseded: **routing**, which the
reproduction above measured, and **Node's reply signing**, where
`automaticIdentityJoined()` consults the endpoint and the stale session half
without reading the proof at all. The first was measured here and led me to
claim two of three mechanisms were already correct; review found the second and
that claim was wrong. One missing fact, two consequences.

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

### The verdict is classified, because one `false` hides two opposite answers

A boolean readiness answer cannot drive self-retirement. The same `false` covers
two situations that demand opposite handling:

- **UNREADY / UNKNOWN** — no proof yet (bootstrap, before the first hook ever
  runs) or the proof could not be read (a transient I/O error). Routing and
  signing fail closed, but destroying the listener would be wrong: the first
  hook is about to make it ready, and an `EIO` must not force a reconnect.
- **PROVED_STALE** — a valid current proof names a different session or digest.
  This is the only case where `no-peer`, flush and self-retire are correct.

So the single shared function returns a **verdict with a reason**, not a bool:
`READY`, `UNREADY`, `UNKNOWN`, `PROVED_STALE`, `STRUCTURAL_INVALID`. The
resolver, `status`, `doctor` and Node signing all consume the same verdict, and
**only `PROVED_STALE` retires anything destructively.** A bool here would have
made the listener either kill itself at bootstrap or never retire at all — the
original bug in the other direction.

Parity is therefore proved over the verdict and its reason, not over a boolean:
two readers agreeing on `false` for different reasons would still diverge on
what to do about it.

That refusal is **classified**, not a transport error. The class is literally
`no-peer` — the existing one, not a new spelling. It is the true statement: the
peer that alias named is no longer this session, so there is no such peer to
reach. The listener replies `ok:false` carrying `refusal_class="no-peer"` and an
actionable reconnect remedy, flushes that response, and only then self-retires. The delivery attempt
is therefore its own wakeup, and cleanup stops depending on a control that may
never arrive. Python preserves that class through `_ClassifiedRefusal` rather
than recasting every negative reply as `transport`, so a sender learns the
identity moved instead of that the socket failed.

**What triggers retirement is a validated action, never a closing socket.**
`doctor` probes the channel with a half-close and expects `{ok:...}`, and
`doctor` is bound by a byte-for-byte read-only contract. If the proof check and
self-retire hung off "socket end", a diagnostic run would retire a stale
listener, and the first symptom anyone met would be that running `doctor` kills
their channel.

There are therefore exactly two triggers, both verified before anything is
destroyed: the explicit retire control, content-free but not empty and
*recognised* as its own control action — shape-validated on magic, version,
action, alias and nonce, which is not authentication and must not be written as
if it were: there is no shared secret and no peer identity behind it. Its safety
comes from the listener re-reading the proof for itself, never from trusting who
sent the control; and a real delivery, where the proof is
checked against a valid delivery payload. `doctor`'s empty half-close probe is
neither, and answers without touching registry or socket state.

The retire control becomes an optimisation rather than a correctness mechanism:
routing is already safe from the proof, and cleanup is already guaranteed by the
first stale delivery attempt.

RED test, the race named exactly: sender resolves A; hook B commits the proof;
the wakeup is withheld; the sender connects to A's socket. Assert the listener
refuses with the classified reason, that zero channel notifications are emitted,
that the response is flushed before the socket closes, and that A has
self-retired afterwards.

## 2a. Startup lifecycle

The first automatic endpoint may register with **no proof at all**, as an
UNREADY candidate: at startup no hook has run yet, so no proof can exist. It is
a candidate, never a ready peer.

The check belongs **inside the same Python registry lock that writes the
endpoint**, never as a Node precheck. The real path is
`channel.mjs` → `register_peer` → `peers.register`, so a proof validated in Node
and a registration performed later in Python are two moments with a window
between them, and the proof can move inside that window. Checking where the
write happens closes it by construction.

Initial registration and reassert also carry **different rules and must be
distinguishable**, which today they are not: both send the same payload, so once
an endpoint has been pruned Python cannot tell them apart. The bridge contract
takes a strict allowlisted `mode`. `initial` opens an UNREADY candidate claim
only when the classified read says the proof is genuinely `absent`; when a valid
proof exists it must match exactly; `invalid` or `unreadable` refuses. `reassert`
requires a valid exactly-matching proof in every case. An unknown mode fails
closed.

So a stale A can never republish after proof B lands — which is the whole point of putting the check
on register and reassert rather than on delivery alone.

The whole rotation is **one transaction under one lock acquisition**, exposed
as a single production call — capture the prior proof, validate and write the
new one, and withdraw every same-owner stale automatic session half relative to the
new current proof. The bounded sweep is part of that same transaction once it
exists. It cannot be assembled from
helpers that each take the lock, because the registry lock is not reentrant and
because two concurrent hooks would interleave between acquisitions and corrupt
both the prior-proof answer and the judgement of which half is stale.

Within that transaction the hook captures the prior proof first, then withdraws
every same-owner stale automatic session half. It sends at
most **one** wakeup, to the previous current alias, and that one is bounded at
250 ms. Waking every stale half would make hook latency grow with history; the
previous current alias is the only one with a live listener worth waking, and
the others are already inert because routing consults the proof.

## 2b. What this contract does NOT touch

`peers.register` is shared: by Claude and Codex, and by automatic and explicit
records alike. Everything in this document is scoped to **automatic Claude
endpoints only**, and that scoping is structural rather than inferred from which
caller happens to reach it today.

- The owner-current proof carries kind exactly `claude`. A proof whose kind is
  `codex` is **invalid**, not merely irrelevant.
- The composite verdict and the `initial`/`reassert` mode gate of §2a apply only
  when `kind == "claude"` **and** the endpoint is automatic — that is, it
  carries the automatic identity digest.
- Explicit and legacy Claude registration, reassert and routing keep their
  present semantics exactly. They require no proof and no mode.
- Every Codex path — registration, observation, projection, routing — is
  untouched by this repair.

This is written down because the failure it prevents is a faithful
implementation, not a careless one: a reader told "automatic endpoint" about a
shared function could reasonably require the proof on an explicit Claude peer or
a Codex registration and silently break working behaviour that this repair was
never about.

## 3. The proof record

- One file per owner under the project's registry area, named from a digest of
  the owner key, never from the raw key.
- Fields: a strict integer version, kind, the validated canonical owner key,
  the owner-key digest that names the path, the current canonical session id
  and its identity digest. Nothing else — in particular no written time, which
  nothing reads: retention is by proved death, not by age, and an unread field
  that must still be validated is pure cost. The owner key is stored because liveness is a
  fact about a pid and a start time, and a digest is one-way: without the key,
  an owner's death can never be proved, only assumed. It is private and is
  never rendered on any surface.
- Written in full then atomically replaced, under the existing registry lock.
  Never rendered on any surface.
- **The read is classified, not boolean-by-absence.** A `dict | None` reader
  would collapse three different facts into one answer: the file is absent
  (ENOENT), the file cannot be read (EIO), and the file is present but does not
  parse or does not validate. The verdict layer must tell those apart —
  absent is `UNREADY`, unreadable is `UNKNOWN`, and present-but-wrong is
  `STRUCTURAL_INVALID` — and none of that is recoverable once they are all
  `None`.
  It is also a safety hole, not only a modelling one: §2a lets a first
  automatic endpoint register as a candidate **only** when the proof is
  genuinely absent. A corrupt proof collapsing to `None` would read as absent
  and open a claim it must have refused.
  So the reader returns a state — `valid`, `absent`, `unreadable`, `invalid` —
  with the proof attached only in the `valid` case. There is no second lossy
  convenience reader: one that returned `dict | None` would be the path every
  safety decision eventually drifted back onto. Parity is over that state and
  its reason, in both languages.
- Read validation is strict and total. Every one of these is `invalid`, never
  `absent`: a version that is not exactly the current integer (a bool is not an
  int); wrong kind; a missing or non-canonical session id; a malformed digest;
  an owner key that is not canonical under the versioned fingerprint; a
  filename that is not the digest of the stored owner key; an identity digest
  that is not the one derived from the stored session id; a torn record; an
  empty file.
- The **filename-to-owner binding** is not decoration. Without it a record
  could be planted or renamed under another owner's digest and read as that
  owner's proof.
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
- **The inventory carries completeness, not just contents.** A bare list would
  make a genuinely empty directory and an `EIO` or `EACCES` during enumeration
  both return `[]`, and `status` and `doctor` would report a confident zero
  from a state they could not read. The result carries its own state: when
  every entry was read the count is exact; when enumeration or any single read
  failed, the aliases are a **lower bound** and the diagnostic is unknown or
  degraded, never "none". Valid entries still render — one unreadable neighbour
  does not hide a live peer — but the total is never claimed as complete, and
  with no valid entry at all the answer is unknown rather than zero. Paths and
  ids stay private throughout, and the read mutates nothing.
- Discovery is by inventory, not by lookup. `status` and `doctor` cannot start
  from an owner they do not know, and in the reconnect window B has no peer
  record to enumerate — so a read-only validated inventory is what makes §6
  implementable at all.
- **Liveness governs rendering.** Only a proof whose owner is positively live
  renders as an alias with a reconnect remedy. Unknown and dead owners never
  render as live; at most they are counted. The same rule as everywhere else on
  this bridge: positive proof or nothing.
- It is removed only when its owner is **positively proved dead**, and only on
  a mutation path that is already writing the registry. That path is named, not
  implied: `rotate_identity_proof` — the call production actually makes —
  sweeps under the lock it already holds
  and reclaims only those whose owner death is proved. A reclamation function
  with no real caller would let a unit test pass while production never
  reclaimed anything.
- **The sweep must make progress, not merely be bounded.** Scanning the first
  eight of a sorted inventory on every write is a latency bound with no
  progress guarantee: eight live or unknown records at the front would starve
  every dead record behind them forever. A persistent sweep cursor is stored
  beside the proofs and advanced atomically under the same lock, so each
  mutation examines the next window of at most eight and the whole inventory is
  covered in a finite number of writes.
- **The sweep uses only cheap positive-death evidence.** The ordinary liveness
  path shells out to `ps` with a five-second timeout, so eight of those under
  the global registry lock could hold a hook for forty seconds — measured in
  `lib/peers.py`, not supposed. Garbage collection therefore asks one question
  only, with a syscall rather than a subprocess: does any process hold that
  pid? `ProcessLookupError` proves the owner is gone. Every other outcome —
  the pid exists, permission denied, anything unexpected — is *not proved
  dead*, and nothing is reclaimed.
  Pid reuse can only make a dead owner look alive, which costs one lingering
  file. It can never make a live owner look dead, which would cost a session
  its identity. The cheap check is sound in the direction that matters.
- The sweep carries a **cooperative budget of 50 ms**, and the word cooperative
  is load-bearing. Python cannot preempt a syscall running on the same thread:
  once `os.scandir`, a read, or `os.kill` has been entered it runs to
  completion whatever the clock says. So the budget is checked between records,
  not enforced across them, and it is not an absolute wall-clock cap. Calling
  it one would promise something only a separate process or an interruptible
  mechanism could deliver, and neither is warranted here.
  The rule is therefore: check the budget before and after each record, stop
  when it is spent, and always make at least one record of progress per sweep
  so the cursor cannot stall. The position is kept for the next mutation.
- **A failing sweep never costs the rotation.** Once the new proof has been
  committed atomically, any garbage-collection or cursor read/write error is
  swallowed: the hook still succeeds, the current proof stands, the correct
  session withdrawal stands, the delivery path is not lost, and no private path
  is printed. Cleanup is best effort and the next mutation retries it.
- A malformed or unreadable cursor **resets to the start** rather than
  refusing. Rescanning is harmless here — every reclamation still requires
  positive death proof for the record it touches — so the safe failure is to
  repeat work, not to stop doing it. A read-only surface —
  `status`, `doctor`, any resolver — never prunes it. Unproved or unknown
  liveness leaves it alone: an unreclaimed record costs a file, and a wrongly
  reclaimed one costs a live session its identity.

## 4. Withdrawal

- The hook may remove **only** an automatic session record whose owner matches
  its own and whose identity digest is no longer current. Never an endpoint,
  never an explicit peer, never another owner's record.
- The retire control is content-free, single-shot, bounded and non-patient. Any
  error, timeout or refusal is swallowed: it cannot fail the hook, and can
  delay it by at most the stated control patience of 250 ms — a bounded and
  known cost, but a cost. Saying it "cannot cost" the hook would be false. The Stop hook is the hottest path on the bridge, and the proof has
  already made routing safe without the control.
- The listener alone unregisters its own pid-owned endpoint and closes only its
  channel socket.
- **Retirement is destructive only for an automatic endpoint that a valid
  current proof shows `PROVED_STALE`.** The control travels to the deterministic
  socket of the previous auto alias, and `auto-…` fits the alias grammar — 31
  characters of `[a-z0-9][a-z0-9_-]{0,31}` — so a person may legitimately set
  `ANTIPHON_NAME` to exactly that string. If the stale automatic listener is
  gone and an explicit peer now holds that alias and socket, a control that
  acted on the address alone would destroy a peer it has no claim over.
  So the listener checks what it *is* before it destroys anything: the endpoint
  must carry the automatic identity digest, and the proof must classify it
  stale. An explicit or legacy endpoint, and an automatic one that is merely
  `UNREADY` or `UNKNOWN`, answer the control with a non-destructive refusal.
  Reserving the `auto-` prefix from explicit names would also close this, and is
  deliberately not done: it would restrict a namespace to compensate for a
  destructive action that should be guarded on its own terms.

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

## 6a. Where the guarantee starts, and what it does not cover

The owner-current proof changes only when the new session's first unnamed hook
commits it. Between the host rotating A→B and that commit, the listener and the
proof still agree on A, and Antiphon holds no project-scoped evidence that B
exists at all — the same blind window branch U named for observations, arriving
here for the same reason.

So the guarantee is stated with its start point rather than as "immediately":
**from the first unnamed hook of the new session onward**, a message addressed
to the stale alias is refused rather than delivered. Before that commit the
window is unmeasured and, with today's host observables, unavoidable. The
critical red begins after hook B commits, and nothing in this repair should be
read as closing the pre-hook interval.

If a host ever exposes a project-scoped signal for a session before its first
hook, this is the limitation to revisit.

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
| stdout ceiling | 32,768 bytes | a fail-closed policy limit. The earlier rationale here was wrong and is corrected: `channel.mjs`'s `maxBuffer: 32 * 1024` bounds the **Python bridge's** JSON output, not raw `claude agents`, which Node never sees. The two numbers coincide by choice, not because they bound the same stream |
| stderr ceiling | 8,192 bytes | room for a real diagnostic and no more |
| total retained | captured pipe bytes <= 40,960 | their sum. Decoding and JSON parsing allocate beyond it, so the honest claim is that captured bytes are bounded and every allocation stays O(bound) — not that 40,960 is all the memory used |
| overflow boundary | the byte that would exceed a ceiling | a limit on what is retained triggers at the first byte past it, not after the read completes |
| terminate to kill | 250 ms, then `SIGKILL` | bounded |
| reap wait | 250 ms | a child that cannot be reaped is reported, never awaited forever |
| retire control | 250 ms total, no reply awaited | connect, send, close; any error swallowed |

On timeout or either overflow: terminate, then kill and reap, and return
unnamed. None of the captured bytes are emitted.

The child starts in its own session before any group-directed signal. If that
isolation cannot be established, only the child pid is signalled — never widen a
signal past a boundary you failed to create.

That distinction carries a named limitation, and the two cases must not be
written as one. When isolation succeeds the group signal reaches the child and
its descendants, and their death is asserted. When isolation cannot be
established only the child is signalled, and **descendant death is neither
guaranteed nor claimed**: a grandchild may outlive the probe. Promising
descendant death in both cases would be a promise the second case cannot keep.

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

The A→B red crosses the real Claude hook, the real Python resolver and a real
Node channel — not registry units. The configured-name work (plan Task 7) uses
the real Codex hook with a positively held writer lock. The probe work (plan
Task 8) uses real children that keep writing on stdout and on stderr. Full Python and Node suites, static checks, a clean commit, and
`fresh-user.sh` on that exact SHA, then independent review.

No push, merge, version, publish, or live cursor, registry, config or socket
state belongs to this repair.
