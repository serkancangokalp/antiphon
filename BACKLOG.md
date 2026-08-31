# Antiphon product backlog

Last reviewed: 2026-08-30

Priorities here describe product risk, not release promises. The bridge keeps
two invariants across every item: it preserves who said something, and it
refuses ambiguity rather than guessing or broadcasting.

## P0 — Lossless, paged context transfer

This item is now a phase ledger rather than one open problem: the delivery and
paging mechanics shipped, and what remains open is named below instead of
hiding under a general "lossless" claim.

### Shipped before the paging plan

Provenance-safe parsers (a user message beginning with `<` is no longer
mistaken for bridge metadata), the byte-offset reader and per-source cursor
with generation fingerprints, the delivery lock beside each peer cursor, and
the write-and-flush-before-advance transaction.

### Completed by the paging plan

- Oldest-first atomic pages of completed source records: an ordinary full page
  targets 8,000 UTF-8 bytes and at most 40 records, and a record is never
  split across pages.
- The non-tool 420-character cut and the 2,600-character summary trim are
  gone; whitespace, indentation and line structure are preserved exactly.
- `has_more` is visible on every page, explicitly scoped to the currently
  discovered sources.
- An oversized record is handed whole to the automatic hooks, whose hosts
  were measured (2026-08-30, Claude Code 2.1.251 and Codex CLI 0.151.0) to
  spill above 10,000 characters and expose a path; both 400,251-character
  probes matched their spill files by SHA-256. Codex's MCP tool result did
  **not** meet that assumption — the transport kept the bytes but the model
  could identify neither content nor a saved path — so `antiphon_read`
  refuses an oversized record without advancing, and the next automatic hook
  delivers it. That is a measured host behaviour, not an inference about its
  internal truncation.
- A rolling-upgrade-safe v3 page key: the legacy v2 value is preserved
  byte-for-byte for still-running old processes and is never trusted as a
  delivered frontier. Any present legacy key, and any malformed or unreadable
  existing cursor, conservatively replays the currently discovered sources
  from byte zero — measured at 69 Claude-source and 53 Codex-source pages on
  the reviewed snapshots — with a fixed replay reason visible on every page
  until the final persisted one clears it.

### Still open, by name

- Stable event ids and full tool-call retrieval: tool calls remain compressed
  one-line summaries with no `antiphon_read(id)` route.
- The durable source catalog and the degraded-discovery marker: discovery
  still reads the newest 3 transcripts per side, and `has_more: false` cannot
  distinguish complete discovery from that window.
- Backward paging into history an older version already marked seen.
- The last-record content anchor (an in-place rewrite that keeps inode,
  length and first line still resumes silently).
- Descriptor-safe reading of registry-supplied transcript paths.
- Direct-channel spill for the 128 KiB `antiphon_send` cap.
- Retirement of the preserved v2 sibling key once pre-v3 processes and
  rollback support are no longer needed.

## P1 — Source-aware multi-peer pull context

Live push is explicitly addressed and never broadcast. Passive pull context is
project-wide awareness, which is useful, but today it merges transcripts under
generic `Claude`/`Codex` labels. With several terminals that can look like one
agent said another agent's words.

- Record and validate the source peer for every transcript used by pull.
- Label each event with the source alias (or honestly `unnamed`) and stable
  session identity.
- Keep project-wide awareness separate from task dispatch: an event addressed
  to `api` may be visible to `ui`, but must remain visibly addressed to `api`.
- Add an explicit filtering policy only if users ask for it; do not silently
  infer Claude↔Codex pairs from matching aliases.

## P1 — Relayed human words are not the reader's own user (fixed)

`build_summary` labelled the other side's human as `YOU`. The block header said
which side it came from, but the line itself read `[11:04] YOU: rewrite the
migration`, and nothing told the reading agent that this was a person talking
to *somebody else*. An agent that treated it as its own user's instruction had
been handed authority nobody gave it — in a bridge whose whole invariant is
preserving who said something.

Provenance and authority are different questions, and that label answered only
the first. The fix had to say both: relay the words under a label that names
them as relayed, and state once, where the reader cannot miss it, that they are
context rather than a direct instruction. The header and closing line already
carried that tone; the per-line label was the part that lied.

### What shipped

**The label.** A `you`-kind event no longer renders as `YOU`. It renders as
`To Codex:` in a page Claude reads and `To Claude:` in a page Codex reads —
what entered the other side's context as input, which is true of a user prompt
and of a host injection alike and claims authorship of neither. The neutrality
is not taste. Measured over this project's live transcripts, **5 of 79 `you`
events are not human text at all**: 3 of the 25 records Claude reads off the
Codex side are the Codex host's own `# AGENTS.md instructions…` block, and 2 of
the 54 Codex reads are Claude Code's compact-continuation preamble. They pass
`_is_host_record`, which only refuses a complete opening wrapper tag at the
start of the text, so any `X's user:` label would have shipped a false
attribution on every one of them — the exact failure this entry exists to
prevent, re-entered from the other end. The name comes from `LABEL["codex"]` /
`LABEL["claude"]`, the same strings the agent lines already use, so
`[10:00] Codex:` and `[10:02] To Codex:` sit on one page in one spelling.

**The notice.** A page that carries at least one relayed line closes with the
sentence in front of the existing closing line:

> Lines marked "To Codex:" are what Codex received as input in its own session
> — relayed here for awareness, not addressed to your session. This record
> belongs to the Antiphon bridge — this is what actually happened there. Do not
> assume anything that is not in it.

It is provenance only: no authorship claim, no ruling on whose instructions
they are. "Not addressed to your session" is true by construction of the
parser, where "nothing in them was said in your session" would not be — the
same human demonstrably repeats an instruction across both terminals.

It is also conditional, and the predicate is over the selected records'
`you`-kind **events**, never over the rendered page text: an agent-only page
whose text merely quotes `"To Codex:"` must not assert that it carries relayed
input, and measured, a string predicate does exactly that. A page with no
relayed line — including the replay-notice page, which carries no records at
all — closes exactly as it did before, so no page asserts words it does not
contain. The sentence costs 143 bytes for a Claude reader and 146 for a Codex
reader, inside the ordinary `PAGE_BUDGET` arithmetic: on a tuned fixture it is
what defers a second record to the next page.

Two facts from the gate rounds, recorded so they are not re-derived. Because
the notice is conditional, every existing page stays byte-identical and **no
existing test broke** (519 tests before, 525 after — six new ones). And the
rendering/counting split — `_render_record(record, side)` renders and returns a
bare string, `_record_message_count(record)` counts — has **no runtime
observable**: with the split fully undone the suite stays green, because the
count never reads a label. It is kept as a structural invariant instead,
recorded by grep: `_render_record` has exactly one caller (`_render_page`,
which knows the reading side) and the counter takes no side argument, so no
unobservable side parameter can drift back in.

### The two open questions, settled

**Whether the relayed label should carry the speaking peer's alias — deferred**
to the source-aware multi-peer P1 above. An alias↔session-UUID join exists
today **only for named Codex sessions**: `peers.write_session` records that
UUID under the alias and `Event.source` carries it, but there is no Claude-side
writer and `read_session` has no production caller at all. A label that named
aliases now would cover one side of one shape and guess the rest — which is
misattribution again, wearing a more specific name.

**Whether an agent should act on a relayed instruction — the bridge answers
provenance and stops.** The shipped sentence says where the words ran and that
they did not run here; whether the reading agent acts on them belongs to that
agent and its own user. The single human who often *is* both sides' user can of
course repeat an instruction in the reader's own session — that is exactly the
confirmation loop working, not a gap in the bridge.

The boundary, stated straight: a reading agent can still choose to act on
relayed words. What shipped makes their provenance impossible to misread, which
is all a label can do.

## P1 — Large direct-message attachments

The direct channel has a separate, honest 128 KiB byte cap. Keep it until an
oversized message has a recoverable path:

- atomically write mode-0600 content under `.antiphon/messages/`;
- send a size, SHA-256 hash and local reference instead of truncating;
- validate every reference beneath the project state directory;
- define acknowledgement, retry, TTL and total-quota behavior;
- show pending storage in `antiphon status` and clean it without deleting an
  unread message silently.

This is separate from passive pull, whose old 2,600-character trim is retired
— pull now pages complete records. Ordinary long SQL and code already fit
under 128 KiB when sent through a channel tool.

## P1 — A marker in anything but the turn's last message is dropped (fixed)

`push` used to read the other side's newest assistant text through
`last_claude_reply` or `last_codex_reply`, and both kept only the most recent
assistant record — `chunks = texts` overwrote on each one. One turn is not one
record, so an agent that wrote a progress message containing `@claude do this`
and then a closing message without markers had its instruction silently
dropped. The obvious repair — join every assistant record in the tail window —
was wrong on its own: it would sweep up markers from previous turns and resend
them, since the dedupe fingerprint compares the joined text and a window that
grows by one record each turn produces a different fingerprint every time. The
fix needed a boundary for "this turn", not a wider join, and both readers now
have one.

### What shipped

- Codex (`last_codex_reply`): the hook payload's own `turn_id` names the turn.
  A matched id returns the span from its `task_started` to its own
  `task_complete`, or to EOF — a live measurement (Task 1, one non-ephemeral
  local run) confirmed the CLI writes `task_complete` only *after* the Stop
  hook has already fired, so waiting on it would have returned the previous
  turn forever. An id present but unmatched (its start already scrolled out
  of the tail window) fails open to the whole visible window rather than
  guessing at a different turn's span. With no id at all — a CLI older than
  the `turn_id` field — the newest `task_started` alone decides, cut by
  nothing. The reader also reports *whether* it bound the turn: the matched
  id where a span was actually cut to it, nothing in either fail-open branch
  or in any no-id branch.
- Claude (`last_claude_reply`): a `user` record is a turn boundary unless it
  is a tool result, an `isMeta` record carrying `sourceToolUseID` or
  `turnCompanion` (a Skill load or turn companion), or an `isMeta` record
  whose top-level `origin.kind` is in the measured mid-turn allowlist
  `{"coordinator", "task-notification"}`. `origin.kind="channel"` — the
  bridge's own injection — and any unmeasured kind stay boundaries.
- The `promptId` field this entry originally proposed as the boundary was
  measured and set aside: absent on all 5,067 sampled assistant records,
  present on only 554 of 556 sampled user records — corroboration for a
  boundary found another way, not something reliable enough to key on
  directly.
- The dedupe fingerprint itself is turn-scoped, not content-only.
  Content alone is not identity: the exact same `@claude`/`@codex` line
  repeated in a later turn hashed identically to the one an earlier turn
  already sent, so it was silently swallowed — measured, one send where two
  turns each said it once. `push_fingerprint` now folds the turn's own
  identity (the matched Codex `turn_id`, or the `uuid` of the Claude
  boundary record that opened the turn) into the fingerprint before
  hashing, as a structured pair that cannot collide with the flat
  content-only shape; an empty key falls back to that original shape
  unchanged, both for continuity with cursors already on disk and because
  a repeat with no nameable turn is exactly what content-only dedupe was
  always meant for. The key is always the *reader's* — never the hook
  payload's on its own. Keying an unbound window on the id the hook happened
  to report changed the fingerprint every turn while the window's marker text
  did not: measured, four sends of one instruction across four turns where
  content-only dedupe sends it once.
- One read decides both halves, each direction. `push` calls the internal
  `(text, turn key)` reader once — `_claude_turn` or `_codex_turn` — rather
  than parsing the transcript a second time to re-derive the key.
  `last_claude_reply` / `last_codex_reply` remain the public single-value
  names over those pairs. Two reads were not equivalent: the transcript can
  grow between them, so the second can name a turn the first never saw, and
  the send from turn A is then recorded under turn B's key — which silently
  suppresses turn B's own later identical marker. Measured: one send where
  two were expected.
- A cursor slot still holding the flat, pre-scoping digest of the batch about
  to be pushed migrates in place, without sending. Comparing it against the
  new scoped shape alone would call already-delivered content new and resend
  it once per slot; recognised in its own old form instead and rewritten to
  the scoped digest, the same way the pre-digest string cursor migrates.
  Every fingerprint `push` writes goes through the one scoped helper — the
  migration write included — so the two shapes cannot drift apart. This
  migration is not free of consequence; see the follow-up entry below.
- Verified end to end: `push`, run against real transcript fixtures with only
  `send_to_claude`/`send_to_codex` mocked, delivers a marker from a non-final
  message, stays quiet on an identical re-read, delivers again — with the
  old turn's text absent — once a new turn carries its own marker, and
  delivers again when a later turn repeats the identical instruction
  verbatim.

### Named limitations

- A turn larger than `TAIL_BYTES` still clips at the tail window; both readers
  keep reading through `tail_lines` unchanged.
- An ephemeral Codex run reports `transcript_path: null`, and `push` no-ops
  before either reader runs — a marker written there never reaches the
  bridge.
- Codex's two fail-open paths — an id present but unmatched, and no
  `task_started` visible while an orphan `task_complete` is — return the
  whole visible window and can duplicate an old turn's tail into a fresh
  send: at-least-once by design, the same trade the delivery layer already
  makes elsewhere. (The third no-id sub-branch, no task marker visible at
  all, does not fail open; it falls back to today's newest-message
  behaviour instead.) Neither carries a turn key — the reader cannot name a
  turn it just failed to bind — so these windows dedupe on content alone: an
  unchanged window stays fingerprint-stable turn after turn, however many
  turns run over it. What still duplicates is a window whose *marker set*
  changes, because the whole window is one batch: measured, a fail-open
  window holding `do X` (already delivered) that gains a second marker
  delivers `do X\ndo Y` — the new instruction and the old one again.
  At-least-once, once per new marker written into the same unbound window,
  not once per turn.
- Where no turn key exists at all — a pre-`turn_id` Codex hook, or a Claude
  window whose boundary record has scrolled out of the tail or carries no
  `uuid` — the dedupe fingerprint stays content-only, so an identical
  instruction repeated in a genuinely new turn is still silently deduped
  away in that case.
- On a CLI whose hook payload predates `turn_id`, the no-id case has two
  residual gaps: a closed nested span sitting inside the window still drops
  text written before that nested start, and the reader cannot distinguish a
  rollout with no markers at all (3/127 measured) from one whose markers all
  sit beyond the tail (1/127 measured) — the two windows look identical from
  inside.

## P1 — A mid-turn tool reply's digest swallows a later turn's identical marker (fixed)

`_record_delivery` — the mid-turn `reply_to_codex` / `_send_tool` path —
wrote `batch_fingerprint([text])`, the **flat**, content-only shape, into
exactly the slot `push`'s dedupe reads. It had to: without that record the
same text arrives twice, once from the tool and once from the Stop hook that
ends the turn. But `deliver_batches`'s flat→scoped migration branch reads a
flat value in that slot as "this batch already went out", and that premise is
false when the flat digest came from a *different, earlier* turn.

Measured before the fix (real transcript, real cursor, only `send_to_codex`
mocked), kept here as the historical record:

- turn A: Claude answers Codex through the reply tool with `run the suite`;
  its visible reply carries no marker, so the Stop hook finds no batch and
  the slot keeps the flat digest `8a2e661d…`;
- turn B: Claude writes `@codex run the suite` — a genuinely new instruction
  that happens to repeat the earlier wording. **0 sends where 1 is expected**,
  silently; the slot upgrades to the scoped digest `c573f74c…`;
- turn C: the same words again → 1 send, as expected.

### What shipped

The record no longer lives in the live slot. `_record_delivery` merges
`{slot: digest}` under `MID_TURN_SLOT = "\0midturn"` — out of recipient space
the way `LEGACY_SLOT` is, and nested so the per-recipient separation the live
slots pay for survives the move. An *unaddressed* park write still drops the
pre-digest string beside it: the rule `forget_superseded` encodes is about the
delivery, not about where the record sits, and keeping both would leave a
record nothing clears.

`push` retires the park in one phase:

1. **Read** — the existing unlocked cursor read notes the park's exact
   `{slot: digest}` pairs.
2. **Consume** — inline in `push`, before `deliver_batches` and without
   touching its signature: a batch whose *content-only* digest equals its
   slot's parked digest is pre-seeded into `raw_sent` with that batch's own
   current-shape fingerprint (scoped where a turn key exists, flat otherwise),
   so `deliver_batches` recognises it and records it without sending. The
   pre-seed goes after the `before_send` snapshot; seeded earlier the consumed
   slot is equal on both sides of the delta and never reaches the cursor at
   all. It is deliberately not guarded by `turn_key` — a keyless Stop is
   exactly the case that echoes its own mid-turn delivery.
3. **Retire** — exactly the pairs observed in step 1 are deleted, by
   compare-and-clear so a pair written while the send was in flight keeps its
   own Stop, and the `MID_TURN_SLOT` key goes with its last pair (an empty
   park is *absent*, never `{}`). On the delivered path the deletion rides
   inside the `mutate` that already runs under the cursor lock: `cursor_lock`
   is not reentrant, so a helper taking its own lock there would burn the full
   patience on every delivered push and retire nothing. The two returns that
   hold no lock use a small lock-taking helper instead.

Exit paths, exhaustively:

| exit | what happens to a park |
|---|---|
| `stop_hook_active` | nothing to do — the invocation that set the flag already retired what existed when it ran. A `_record_delivery` made *after* that retire leaves a park no push in that turn retires: the same one-turn window as the crash case below. |
| missing / `null` `transcript_path` | the documented ephemeral-Codex case. A park written by an ephemeral turn survives until the next non-ephemeral push. |
| `if not batches` (markerless Stop) | retired |
| `if not delivered` (nothing sent, or the send refused) | retired |
| delivered path | retired inside the same `mutate` as the delivery record |

A write that was carrying a retire and failed prints
`antiphon: a mid-turn record was left behind; an identical marker may be
suppressed one extra turn`, and the next push retries. The delivered path's
own failure line no longer claims "not a drop" — with a park aboard, that
failure really can suppress one next-turn marker — and prints the left-behind
notice beside it, so the two costs are reported separately rather than under
one blanket sentence.

**The trade, stated straight.** In the ordinary flow the park lives exactly
one Stop. In the crash window (a turn whose Stop never runs — or a push that dies
between its unlocked read and its retire) and the lost-lock window, a stale park survives into the next push, whose consume can
then suppress **one** next-turn marker repeating the parked wording — a
bounded, one-turn loss; the turn after sends. It is diagnosed on stderr in the
lost-lock case and **silent in the crash case**, where no push ran to say
anything. Measured: 0 sends where 1 was expected on the next turn, 1 on the
one after. That is not "never a loss"; it is today's behaviour for exactly one
turn instead of forever, where today's is unbounded until something overwrites
the slot.

**Cost.** The markerless Stop did zero cursor I/O before (measured). It now
does one unlocked read per markerless Stop, and takes the lock only when that
read actually found a park. One visible side effect of that read: a corrupt
cursor file is now diagnosed ("could not be read safely") on a path that
never printed before — a diagnosis, not a behaviour change.

### Named limitations

- Two mid-turn replies to the **same recipient in one turn** still echo —
  and the echo is the **whole batch** (measured: `line one\nline two`, so the
  first line is redelivered too). The park holds one digest per slot, the
  Stop batch hashes both lines together, so nothing matches — unchanged from
  before this work, not a regression, but it bounds "the mid-turn duplicate
  stays prevented" to the single-line case.
- The legacy supersede stays where it was moved to: at park-write time, on an
  unaddressed delivery.
- The flat→scoped migration branch inside `deliver_batches` stays live. It is
  **not** a one-time upgrade path and must not be described as one: any push
  that resolves **no turn key** — a pre-`turn_id` CLI, a boundary clipped out
  of the tail window — still writes a flat, content-only digest into the live
  slot, and a later push that *does* resolve a key and repeats the wording is
  silently suppressed once by that branch. Measured with no tool call anywhere
  in the scenario, identical before and after this work: 1 send, then 0, then
  1 — on `8a2e661d…`, the same digest this entry names above. What this work
  removes is exactly the *mid-turn tool path* as a feeder of flat digests.
  The cause is already filed under the turn-scoped entry's content-only
  fallback limitation; the manifestation is narrower than that entry's
  wording, which describes keyless→keyless suppression, while this residual is
  **keyless-write → keyed-identical-push** through the migration branch.

## P2 — A refused active send does not say the message will still arrive

When the direct channel refuses a send, the tools report the host's error and
stop there. Observed twice in one session: the Codex host answered
`direct app-server input is not allowed for unloaded spawned sub-agents`, so
`reply_to_codex` failed. `antiphon status` then showed the same text waiting in
the other side's pull queue — the passive path had it, and it was delivered on
the peer's next prompt.

The sender could not know that from the error. The reasonable reading of a
failed send is that the message was lost, which invites repeating it, or
proceeding as though the peer was never told. Both are worse than waiting.

The bridge already knows the answer: passive pull carries everything either
side wrote, and a refused *active* send changes only the timing. Say so in the
failure — name the fallback and what it costs, which is a delay until the peer's
next turn rather than a loss. Worth checking first whether any refusal exists
that the pull path genuinely cannot cover; if one does, it needs a different
message from the ones that merely arrive late.

## P1 — `antiphon doctor`

Add one read-only command that explains the common “bridge is quiet” cases:

- command/package version and which executable `PATH` resolves;
- Node/Python compatibility;
- hook, MCP and environment-forwarding configuration;
- current alias validity, live peers, readiness and stale records;
- channel socket reachability and Codex queue availability;
- actionable repair text. A future `--fix` may call the existing idempotent
  setup path, but the default command must not edit anything.

## P2 — Reply correlation

Explicit `to` remains the safe default when several peers are live. Automatic
reply routing needs a durable design before implementation:

- correlate only after a successful delivery acknowledgement;
- scope pending messages to the receiving peer;
- validate the original sender is still the same live session;
- let explicit `to` override correlation;
- fail closed when several unanswered senders remain;
- define expiry and cleanup without losing a late reply.

## P2 — Automatic peer identity

Aliases are intentionally explicit in the first multi-peer release. A later
release may make unnamed peers visible, but only after both writers derive the
same identity on every supported host. There must be no user-settable owner-key
override, no short-id collision, and no “newest session” fallback once more than
one candidate is known.

There is a concrete Claude-side lead, not yet a contract. On Claude Code
2.1.251, `claude agents --json --cwd <project>` locally returned the active
interactive session with `pid`, exact `cwd`, `sessionId` and a generated `name`,
and the channel server's ancestor chain reached that pid. Before using it:

- feature-detect the command and schema; help text calls this background-agent
  management even though JSON currently includes interactive sessions;
- measure the MCP startup race and use a bounded wait, never “newest” as a
  fallback while the session has not appeared yet;
- fail anonymous when the server is orphaned or its ancestry cannot be joined
  to exactly one entry;
- treat `sessionId` as the identity candidate and the generated `name` only as
  untrusted display metadata until uniqueness and lifetime are documented;
- prove the equivalent Codex MCP/hook join before changing the product rule —
  a Claude-only automatic name would restore the asymmetry this release removed;
- keep an explicit `ANTIPHON_NAME` as the deliberate override and test upgrade,
  collision and mixed-version behaviour.

## P2 — Cross-vendor managed workers

A user should be able to tell a live Claude session “have Codex do this”, or a
live Codex session “have Claude review this”, without manually opening another
terminal and without making the foreign agent look like a native subagent. The
right abstraction is an **Antiphon-managed foreign worker**: the parent agent
can delegate to it and follow its lifecycle, but every event and result still
names the actual Claude or Codex session that produced it. This preserves the
bridge's identity invariant; an absent or ambiguous worker is refused rather
than guessed.

The first safe shape is:

- expose a small `delegate`, `status`, `result` and `cancel` lifecycle, with a
  stable task id and explicit worker session id;
- return immediately after acceptance by default, so the parent can continue
  working and collect the result later;
- label every update and artifact as coming from the foreign worker, never as
  the parent agent's own reasoning or work;
- give every write-capable task its own Git worktree; a worker must not edit in
  the parent session's checkout or race another worker over the same files;
- never give the worker a broader permission class than the delegating session
  or an explicit human grant, and never let a worker approve the parent's
  permission requests, merge its own work, or silently widen its sandbox;
- default the cross-agent hop budget to one. Nested delegation is refused unless
  the user explicitly opts into a higher bounded value, so
  Claude → Codex → Claude cannot become an invisible recursive loop;
- make `blocked`, `completed`, `failed`, `cancelled` and timeout outcomes
  explicit, and return reviewable evidence such as the diff and test results
  with a completed write task.

This must be implemented as an Antiphon lifecycle over host adapters, not by
pretending that either host natively spawned the other vendor's model. Each host
already has its own same-vendor nesting story, and neither is a cross-vendor
contract: Claude Code documents that a subagent inherits the main conversation's
MCP tools and may itself spawn subagents up to a configurable depth
(`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, set to `1` to turn nesting off), and on
this machine Codex CLI 0.151.0 advertises `multi_agent` as a stable feature while
the generated App Server schema mentioning `spawnAgent` is still experimental.
Neither establishes a stable way for one vendor to spawn the other's model, and
this feature must not depend on one without version detection and a tested
fallback. The bounded-depth precedent is worth copying rather than reinventing:
a documented, configurable limit is exactly the shape the hop budget above takes.

### Decisions still required

- Whether `delegate` may target an already-running named peer, always creates a
  fresh managed worker, or exposes both modes explicitly.
- Whether managed workers are one-task ephemeral sessions or can be resumed,
  and what expiry, cleanup and storage quotas apply.
- Which host adapters are supported first, and whether an unavailable native
  worker API may fall back to a documented CLI/SDK subprocess.
- Which task classes may run without another user confirmation, and who may
  accept a worker's patch or merge it after deterministic checks pass.
- Whether a synchronous wait mode is worth exposing in addition to the safer
  asynchronous default.

No claim is made yet that a Claude worker can appear in Codex's native agent UI,
or that a Codex worker can appear in Claude Code's native subagent UI. That UX
would be optional integration work; the portable contract is Antiphon's own
named worker, task lifecycle and evidence trail.

## Observed, not adopted — Claude Code's per-session messaging socket

Measured on one macOS machine, 2026-08-30: a Claude Code session exports
`CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN` and
`CLAUDE_CODE_SESSION_ID` into the processes it starts, including hooks and
stdio MCP servers. The socket path pointed at a real Unix domain socket under a
temporary directory, the token was a 32-character string, and the session id was
a UUID. Nothing was connected to and no token value was read.

This is recorded because it is easy to find and tempting to use, and because the
temptation should be answered once rather than every time somebody notices it.
It is not documented, its path is named after a process id, and it exists on one
side of a bridge whose entire purpose is the asymmetry between two hosts —
Codex CLI has no equivalent. A published package that made an undocumented
internal interface its transport would break silently on the first release that
moved it, and the failure would look like the bridge going quiet.

If a first-party, documented agent-to-agent transport ever ships on both sides,
this is the entry to revisit. Until then Antiphon owns its own sockets.

## P2 — Multi-line Stop markers

Stop markers currently carry one line. Channel tools preserve multi-line text
and are the recommended route for long content. If users need natural-language
Stop blocks, add an explicit delimited syntax with tests for fenced code,
embedded marker text, empty blocks and deduplication; never guess continuation
from arbitrary following prose.

## P1 — Re-run the host wrapper census before release

`CLAUDE_HOST_WRAPPERS` and `CODEX_HOST_WRAPPERS` in `lib/antiphon.py` hold
exactly what a census measured (2026-08-30; re-run 2026-08-31 before 0.3.1,
which moved `ide_opened_file` into the Codex set on 4 directly inspected
records), and nothing else. They will go
stale as each host adds, renames or drops its own wrapper tags, and the
obligation to re-measure must not live only in a planning document that ships
nowhere. Re-run it before every release:

- count every `role: user` text record whose text opens with `<`, split by
  side, each one carrying its `promptSource` value (or its absence);
- for every opening tag that turns up, decide host bookkeeping or a person's
  own words before touching either set — a tag seen on only one side stays out
  of the other's, the way `local-command-caveat` did until it was measured;
- update the sets, the measurement comment above `CLAUDE_HOST_WRAPPERS`, and
  this entry's date together, so none of the three can drift from the other
  two.

The asymmetry that governs a doubtful case: a tag missing from a set lets one
stray host line leak into a summary — visible, and cheap to fix by adding it.
A tag wrongly present deletes a person's message — silently, with nothing left
behind to notice it happened. When the evidence is thin, leave the tag out.
