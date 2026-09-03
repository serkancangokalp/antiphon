# Antiphon product backlog

Last reviewed: 2026-09-02

## Start here

**Where this stands (2026-09-03).** `main` carries **0.5.0**: the final
campaign of 2026-09-02/03 melted every open item — the mixed-version endpoint
pruning (P0), the token cost of the passive page and the static surfaces, the
delivery truth (a ledger, receipts from the peer's own transcript, "queued"
where a queue is all there is), same-vendor bridging (Claude ↔ Claude, Codex ↔
Codex, always by name) and the managed-worker MVP — each in its own worktree,
each merged after an independent read-only review closed on it, each certified
by both Python suites, the Node suite, the statics and `fresh-user.sh` from a
temporary worktree at the exact commit. **npm still serves 0.3.3**: nothing
here was pushed or published; that is the person's own step, after the Codex
review over the bridge — which was attempted and is still queued in a Codex
thread that has not taken a turn (the queue measurement of the delivery-truth
entry, live).

**Mixed-version pruning** (a 0.3.x reader deleting current endpoints; a
listener whose in-memory Node and on-disk Python disagree being told it
recovered) is closed by the `process_birth` sibling, the two-way
`fingerprint_field` claim and the verdict's current-fingerprint rule — see the
0.4.0 paragraph under *P0 — A named Claude session can identify itself as
`<unnamed>`*.

**Token cost** (2026-09-03): host records are no longer rendered as speech,
the page has a 24-hour horizon relative to the other side's newest record,
the agent surfaces are about a quarter smaller, and doctor sees a stale rule section
— see *P1 — Token cost of the passive page and the static surfaces*.

**Delivery truth** (2026-09-03): every direct send is on a ledger under
`.antiphon/deliveries/`, `reply_to_codex` says queued and never delivered,
receipts come from the named recipient's own transcript (a bare delivery's
from any transcript of its kind), a refused Stop marker and an
attachment that expired unread are reported on the sender's next page, a
bare-reply refusal names the last unanswered sender, and `status`/`doctor`
read the ledger — see *P1 — `reply_to_codex` can report success while the
peer receives nothing (fixed)*, the attachments entry's closing, and *P2 —
Reply correlation*.

**Picking the work up.** The open items are the P0/P1/P2 sections that follow,
in that order. The two rules this project runs on, learned the expensive way
and worth reading before touching anything:

- A test that is green is not a test that is protecting something. Mutate the
  guard, watch the named test fail, put the guard back. Several fixtures in
  this repository have passed for the wrong reason — landing in an assertion
  group that could not see them, short-circuited by a guard added beside them,
  or asserting a tautology.
- The registry is read by two languages, and a rule added to one reader is a
  divergence until it is added to the other. `test_readiness_parity_holds_across_every_fixture`
  drives both over the same state; a change to either reader belongs there.

**One thing about the working tree.** The tracked tree is clean, and about
forty `docs/superpowers/{plans,specs}/2026-08-*` files are untracked — historical
design documents, visible since the `.gitignore` negation, left alone
deliberately. `fresh-user.sh` requires a clean tree, so run it from a temporary
`git worktree` at the commit under test rather than moving those files aside.

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
- `has_more` is visible on every page. Wave 1A later replaced its temporary
  newest-file scope with the durable catalog contract below.
- An oversized record is handed whole to the automatic hooks, whose hosts
  were measured (2026-08-30, Claude Code 2.1.251 and Codex CLI 0.151.0) to
  spill above 10,000 characters and expose a path; both 400,251-character
  probes matched their spill files by SHA-256. Codex's MCP tool result did
  **not** meet that assumption — the transport kept the bytes but the model
  could identify neither content nor a saved path — so `antiphon_read`
  refuses an oversized record without advancing, and the next automatic hook
  delivers it. That is a measured host behaviour, not an inference about its
  internal truncation.
- A rolling-upgrade-safe v3 page key: the legacy v2 parsed value is preserved
  with deep type equality for still-running old processes and is never trusted
  as a delivered frontier. Any present legacy key, and any malformed or unreadable
  existing cursor, conservatively replays the currently discovered sources
  from byte zero — measured at 69 Claude-source and 53 Codex-source pages on
  the reviewed snapshots — with a fixed replay reason visible on every page
  until the final persisted one clears it.

### Completed by Wave 1A durable discovery

- The durable source catalog and degraded-discovery marker replace the newest
  3 correctness boundary. A `has_more: false` page is scoped to catalogued
  project sources; `discovery: building` and `discovery: degraded` make an
  incomplete boundary explicit. Wave 1A selected a fixed
  batch of 8 from a read-only 25-sample descriptor benchmark on 2026-09-01:
  the main project exposed 3 Claude and 156 Codex candidates; batch 8 measured
  Claude median/p95 0.171/0.175 ms and Codex 1.192/3.896 ms. The resumed-hook
  write harness then compared 8 versus 563 synthetic candidates: both inspected
  8 and made exactly 11 atomic writes, totalling 5,164 versus 5,155 bytes (the
  largest single write was 1,284 bytes in both). Resumed batch turns use a
  fixed number of writes and bytes independent of catalog size. Starting or
  refreshing a generation still publishes one full immutable manifest and is
  O(retained candidates); monotone retention makes that transition grow until
  cursor-aware v4 retirement. Superseded manifests are also retained in 1A,
  so their on-disk total is O(retained candidates × refresh generations) and
  is not bounded by candidate count alone. Every completed explicit
  `antiphon sources scan` forces a refresh even when membership is unchanged,
  and retry-driven refreshes can add generations too; if candidates and
  generations grow together, the special case is quadratic. V4 therefore has
  two reclamation jobs with different proof: retiring a candidate record needs
  per-reader cursor evidence, while removing a superseded manifest needs only
  proof that no active catalog state references it.
  Hooks record their current source first, then inspect one bounded batch
  through finite base/reconcile/delta generations;
  `antiphon sources scan` completes or refreshes the same catalog without
  moving a page cursor. Status and doctor report aggregate complete/building/
  degraded truth separately for each reader, without paths or source ids.
- Descriptor-safe reading now covers catalog, fallback, event parsing,
  backlog and catch-up. The Task 2 pre-commit narrowing gate ran read-only on
  2026-09-01 against both real host roots for the main project and its
  implementation worktree. On the main project, old/new admitted identity
  counts were Claude 3/3 and Codex 3/3, with zero missing, zero extra and no
  refusal class; the worktree had 0/0 on both hosts, also with no refusal. A
  transcript is opened beneath its host root without following symlinks, then
  identity, metadata, generation, prefix and records come from that one
  descriptor. Whole-union source-id collisions refuse every claimant. Gone
  means every recorded path is `ENOENT`, and its relevance is read-only and
  reader-specific. Ordinary advancement and catch-up merge safe fronts onto
  the prior v3 map, preserving every unresolved subtree.
- The exact-SHA review caught two false authorities before Wave 1A closed:
  `state.json` could say `complete` without proving its immutable manifests and
  terminal record coverage, and a Claude catalog path could supply the same
  project prefix later used to validate itself. Readers now validate the whole
  state/manifest join, keep a manifest source readable-but-degraded when its
  index record is missing, ignore detached records when state is untrusted,
  and bind every Claude candidate to the independently selected host project
  directory. The same review made reservation/write contention visible in the
  hook page and scanner result, and made a partial first record retryable rather
  than durable `ready` state. The follow-up exact-SHA review closed the crash
  edge after delta-manifest publication, retained a committed candidate when
  both its record and transcript disappear, separated structural corruption
  from a reader-specific retained refusal, and rejected non-filesystem paths
  plus boolean/non-finite observation times before either can manufacture a
  false `complete` result.

### Completed by Wave 1B anchored paging and bounded retention

- Paging writes `<side>_pages_v4` beside the preserved v3 sibling and legacy
  key. A v4 frontier includes a content anchor for the last complete record;
  during v3 adoption that last record repeats at most once while its anchor is
  established. In-place rewrites that preserve inode, length and the first
  line therefore repeat rather than skip. Later v3 writes cannot move the
  frozen v4 adoption frontier, and failed delivery changes no anchor or lane.
- Live and unknown sources share the active lane. Only a current process
  fingerprint can prove a source dead; missing, legacy or unreadable evidence
  remains unknown. A mixed backlog alternates whole pages between active and
  dead after successful delivery, so live replies are bounded without starving
  history. The owed lane is persisted atomically with the page frontier.
- Catalog readers copy state plus immutable manifests under a short shared
  lock. After a state switch, mutation holders remove only grammar-valid,
  unreferenced regular manifests inside the owned directory; crash leftovers
  retry on later catalog mutations. Cleanup failure never suppresses a page and
  is reported only as an aggregate pending count.
- `antiphon sources compact` is the explicit candidate-retirement boundary.
  It retires only whole aged, gone source groups every relevant v4 reader
  proves consumed or has no entry for after lookback. The shared cursor is
  always relevant; a named cursor is dormant only when the recorded owner is
  proved dead by a current process fingerprint. Every decision input —
  including cursor, owner, source and the deeply typed values of only the
  candidate records being retired — is revalidated under the catalog lock
  around the atomic state switch. Unrelated hook updates do not starve the
  command. A durable prepared/committed journal exposes the old catalog through
  a crash or failed rollback and authorizes cleanup only after post-switch
  proof; an unjournaled detached record is never guessed safe. Output exposes
  only aggregate blocker classes and reclaimed files/bytes across stdout and
  stderr. It tells the operator to retry only the subset of `snapshot-raced`
  blockers caused by a revalidated input change; persistent proof failures say
  that no automatic remedy was attempted. The internal retryable count is a
  narrowing classification and can never exceed `snapshot-raced`.
  Hooks never retire candidates.

### Completed by Wave 1C real Codex tool visibility

- Codex tool calls now use the host's real completed record shapes:
  `response_item/custom_tool_call` and `response_item/function_call`. Each is
  one atomic, compact name-only event; a safe namespace may qualify the name.
  Inputs, arguments, results, call ids, source ids and paths remain unavailable.
  Separate output records stay filtered but advance the safe scanned frontier.
  The obsolete `event_msg/exec_command_begin` reader is explicitly retired: it
  occurred zero times in the measured supported corpus and exposed commands.
- The pre-change corpus measurement found roughly 5,970 custom calls and 468
  function calls across 22 project rollouts, while the parser rendered zero of
  them. Making those calls visible increased a fully drained replay from 800 to
  899 pages (+12.4%) and displaced 6,234 human records by median/p95/max
  53/59/98 pages. That is historical replay cost, not ordinary latency: in 987
  steady-state tool runs the next human record stayed on the same page 983
  times, and the worst delay was two pages. This does not justify another
  persisted fairness lane; the existing active/dead scheduler remains the
  measured starvation boundary.
- Tool-only records consume the same 40-record and UTF-8 byte budgets without
  incrementing the human-message count. Failed delivery persists neither their
  frontier nor scheduler lane, and no output can move the frontier past a first
  undelivered visible call.
- A doctor diagnostic, on `main` at 0.4.0, protects the same failure boundary
  that let roughly 6,400 real calls disappear while the old suite stayed green.
  On explicit `antiphon doctor` only, it scans the trusted complete Codex
  discovery set and counts aggregate call-like records the production
  `_codex_tool_fields` validator rejects. A positive count is broken because
  those records are omitted from passive pages; incomplete discovery or reads
  report an unknown amount rather than a false green zero. No type, name,
  argument, result, path, source or native id is printed or persisted. The
  green claim is deliberately narrower than "no tools were missed": a future
  host shape that abandons the `_call` suffix can evade both parser and counter.
  The full scan remains out of hooks, paging, setup and status — the measured
  indexless scan over 243 MB cost about 579 ms, acceptable for an explicit
  diagnostic and not for every turn.

### Completed by Wave 1D stable tool invocation retrieval (on `main` at 0.4.0)

- Every recognized tool invocation now carries a 22-character opaque,
  content-bound `tc1.<kind>.<digest>` id on its compact page entry. The digest
  binds source identity, native call id (or record start plus block ordinal),
  and exactly the invocation fields retrieval returns. Claude objects preserve
  nested JSON types; Codex custom and function arguments remain exact strings.
  Source/native ids, paths, offsets and generations never enter public output.
- Both MCP servers expose the same read-only, cursor-neutral
  `antiphon_retrieve(id="<id>")`; `antiphon retrieve <id>` is the lossless CLI
  escape hatch. Retrieval returns the invocation only, never the tool result.
  It scans the complete trusted candidate set without an early success return
  and reports `found`, `invalid-id`, `unavailable`, `ambiguous` or `untrusted`.
  The MCP road accepts at most 8,000 UTF-8 bytes and refuses 8,001 without
  truncation; the CLI is pinned with a 36,963-byte-like argument.
- The id closes the earlier-prefix rewrite hole for invocation content. A
  same-size mutation with unchanged source generation and native id receives a
  different id; the old id never returns the new bytes. It returns
  `unavailable`, because there is no persistent invocation index or tombstone.
  `changed`, `expired` and `never existed` cannot be distinguished without the
  rejected delivered-prefix/index state, and doctor does not invent one.
- Lookup writes no cursor, catalog, peer, cleanup or attachment state. Every
  result class is tested against a byte-identical project metadata tree and
  mutation seams that raise if reached. Malformed and partial records are not
  invented into calls; unsafe, replaced or degraded sources fail untrusted.
  Host retention and explicit candidate compaction may make an old id
  unavailable. Duplicate transcript identities inside a host discovery root
  degrade discovery and make retrieval untrusted; backups outside those roots
  are irrelevant.
- The pre-change corpus measured about 8,400 calls. Indexless full-set scans
  cost about 106 ms over 27.8 MB of Claude transcripts and 579 ms over 243 MB
  of Codex rollouts. The 22-byte id added 5.2% bytes / 3.6% pages to Claude tool
  traffic and, after Wave 1C made the real Codex shapes visible, 2.7% bytes /
  0.4% pages there. That explicit-call latency and page cost are accepted in
  exchange for no new persistent proof surface.

### Still open, by name

- Backward paging into history an older version already marked seen —
  **settled for the published path, ruled for the rest.** Its cost was
  measured on 2026-08-31: the byte-zero `legacy_upgrade` replay of two days
  of transcripts (16 MB one way, 44 MB the other) was still draining twenty
  hours after the upgrade, one page per turn, and every live message waited
  behind it. Three things shipped. (1) A numeric 0.1.0 `_seen` — the only
  legacy shape npm ever carried (0.1.0 → 0.3.x; the v2 map lived on dev
  machines only) — is taken as authoritative: the page starts at the first
  record at or after that time, `>=`, so the cohort sharing that second
  repeats per source (measured: up to 10 records share one second), and what
  0.1.0's own 2,600-character trim cut before then stays cut — a conscious
  contract decision, because 0.1.0 had already declared it delivered and
  resurrecting it cost the maintainer twenty deaf hours. (2) A v2 map still
  replays from byte zero: it records how far the old scanner *read*, not what
  it delivered — the old reader scanned the whole suffix and rendered the
  newest `EVENT_LIMIT` — so no offset in it is a safe start; the four
  rolling-upgrade tests that pin this stay. (3) The replay notices name
  `antiphon catch-up`; `status` prints `unread <reader>: N raw bytes …` per
  reader — raw bytes, never pages, since a page is a rendered envelope and
  most raw bytes are filtered before one — and `doctor` notes a replaying
  reader as `·`. The count runs through the reader's own start resolver
  (`_resolve_start`), so a numeric v1 time, an offset past EOF and a replaced
  generation are counted from where the reader will actually start (review of
  6089336 caught the re-derived rule saying 0 while the reader would read 126);
  only an unreadable cursor file is `unknown`. Malformed v3 keeps `cursor_recovery` from byte zero and
  never falls to the v2 sibling; generation mismatch and offset-past-EOF keep
  byte zero.
- Retirement of the preserved v2 sibling key once pre-v3 processes and
  rollback support are no longer needed.

## P1 — Source-aware multi-peer pull context (fixed)

Live push is explicitly addressed and never broadcast. Passive pull context is
project-wide awareness, which is useful, but it merged transcripts under
generic `Claude`/`Codex` labels. With several terminals that can look like one
agent said another agent's words.

The entry asked for the source alias on every event. What shipped is narrower
and, measured, more honest: a *source* is a **session**, not a peer. `Event.source`
is `source_id(path)` — the session UUID in the transcript's own filename, which
both hosts write and which survives a move or a rename. The original
`RECENT_FILES = 3` sample meant the sources on an ordinary page were usually
one terminal's *consecutive* sessions; the durable catalog changes completeness,
not that identity model. Measured on this machine over a real drain: 5 of 60 Claude-read pages
and 1 of 60 Codex-read pages carry more than one source, and every one of those
is one person's Codex session restarted, not two terminals. Labelling all of
them, and advising their reader to "name each terminal", would have been advice
that is wrong wherever it fires most.

### What shipped — four decisions

**1. The Claude hook records its own session, mirroring the Codex one.** The
hook has always been handed `session_id` and always threw it away;
`record_claude_session` now writes the same `session.json` half through the same
`peers.write_session`, gated on `explicit_name()` and `owner_key()` exactly as
the Codex arm is. The alternative — a `session` field on `endpoint.json`, filled
from `CLAUDE_CODE_SESSION_ID` when the channel server registers — was measured
and dropped: `claimPeer()` runs exactly once (the live server on this machine
had been up 20h59m on one claim), so a resume or a fork would leave the record
naming the old session and put the live alias on a dead transcript; and an
env-settable identity is the one thing `owner_key` refuses, because a key anyone
can set lets one session claim another's. The hook route needs no `channel.mjs`
change, no endpoint-schema change, and refreshes every turn.

**2. A label needs both halves — two or more sessions sharing the page, and a
live claim on that source — joined the way the registry already joins.** One
source is not a "which of these": there is no ambiguity for a label to prevent,
and since naming terminals is the recommended practice, a claim-only rule would
put a permanent suffix on every page of every named session, the ordinary
single-pair install included. `peers._session_address(cwd, endpoint)` is the
join: liveness on the **endpoint**, session identity from the hook's half, and
the owner key between them. A missing record, one with no owner, one from a
different owner and one whose id is not a canonical UUID all read the same way
— no claim. Liveness on the session record itself would have joined nothing at
all: `write_session` deliberately writes no pid. A session two live endpoints
claim under different aliases is dropped rather than decided;
`sorted(os.listdir(...))` order is not an answer. The reserved `<unnamed>` key
is filtered by `valid_name`: it is a place in the registry, not a name anything
may print.

The join is built **once per `build_summary`** and threaded down. `_render_page`
runs once per prefix length inside the budget loop — up to `EVENT_LIMIT` times —
and the join walks the registry with a `ps` per record: measured, 343 ms per
turn if it were built there, against a 46 ms whole-page build.

**3. Labels are per record block, not per kind of line.** Every line a record
renders comes from one source, so the agent line, the relayed line and the tool
line take the suffix together: `Codex (build):`, `To Codex (build):`,
`  · (build) 3 tool calls: …`. The tool line matters most — measured, 26 of the
first 40 records a Codex reader sees render *only* a tool line, so it is the
only place their label can go. A per-kind rule would have labelled the agent
lines and left the relayed line of the *same rollout* bare: measured on real
page 52, where the per-kind source counts are `{'codex': 2, 'you': 1}`.

**4. The notice is two-tier and anchored on live claims; the relayed sentence
is additive.** A page says `This page interleaves {n} {Label} sessions;
unlabelled blocks are earlier or unnamed sessions.` only when at least one
selected source is live-claimed **and** two or more sources are selected. It
adds `A {Label} session is running now with no name; name each terminal
(ANTIPHON_NAME) to tell them apart.` only when a block is unlabelled **and** the
registry holds a live endpoint under the reserved key. That last condition is a
kind fact: `valid_key("codex", UNNAMED)` is False, so an unnamed Codex endpoint
cannot exist and the remedy can only ever appear on a page **Codex** reads.
A dead-only multi-source page gets neither line and stays byte-identical.

Today's relayed closing sentence keeps its bytes exactly. On a page where
labelling is active *and* a `you` event is selected, one sentence follows it:
`A parenthesised session label after the recipient names which live session's
line it is.` The trigger is conjunctive because the two conditions come apart —
measured, 4 of the 6 real labellable pages carry no `you` event at all, and a
sentence about what follows a recipient, on a page with no recipient line,
is the defect the relayed-words entry above exists to close. An earlier revision
made the reword unconditional and moved the closing bytes on 22% of real
Claude-read pages and 37% of Codex-read ones; that is why it is additive.

### Measured, so it is not re-derived

**The real-data gate.** Every file of both corpora — 85 Claude transcripts and
130 Codex rollouts, 215 files, 1,656 pages drained from a zero cursor through
the real budget loop — rendered **byte-identical** old against new, with no
crash on either side. It could not have been otherwise today, and the reason is
recorded rather than assumed: across the 31 distinct project directories those
corpora name, the registries hold exactly **one** endpoint record, and it
carries no owner key, so `_session_address` refuses it. The one-file sweep itself can
never label under the two-source rule, so reachability was proved the way the
rule requires: two real rollouts of one project drained together produce 199
pages of which exactly one differs — four labelled lines and the informational
sentence, no remedy (correctly: no unnamed endpoint was live). The differing
bucket is reachable, its measured frequency on real data is 1 in 199, and the
sweep's zero is the rule working, not the feature idle.

**Capacity.** A label can only be added as a prefix grows, never removed, so
candidate size stays monotone and the budget loop cannot oscillate — `selected`
is still the largest length that fits. It is not free: on a near-budget page
(200-byte bodies, 30 records from one source and 5 from another, interleaved,
the first claimed) the same corpus that was delivered complete at 35 records now
delivers 34 and reports `has_more: true`. That flip holds at 20-20 and does not
at 5-30, because the split decides how many blocks carry a suffix; the fixture
parameters are part of the test's assertion.

**`status` shows labels on purpose.** `build_summary` has four call sites, and
the what-X-would-see preview is the page — so it shows exactly what is
delivered. The `Peers:` block is a different thing and is untouched: names and
readiness, never an address, never a session id.

### The bounds, by name

- **Unnamed Codex concurrency is undetectable.** A Codex session that was given
  no alias registers nothing at all, so two of them interleaved on one page
  leave no evidence anywhere. The page says nothing rather than guessing; this
  is why the remedy tier can only appear on a page Codex reads.
- **A hook that carries no `session_id` stays unjoinable.** Nothing is inferred
  from a transcript's contents or its mtime.
- **A started-but-unprompted Claude session is an honestly unlabelled source.**
  `hook_shapes()` installs the Codex pull hook on both `SessionStart` and
  `UserPromptSubmit`; the Claude side has only `UserPromptSubmit`, so
  `record_claude_session` cannot run before that session's first prompt. The
  extra `HookShape` row is declined as out of scope here — both `setup` and
  `doctor` read that table — and nothing false ships: the label is absent, not
  wrong.
- **The proof sweep degrades to its first window if its cursor cannot be
  written.** Reclamation runs inside the rotation the Claude hook already
  performs, examines at most eight proof records per write, and resumes where
  the last write stopped — that cursor is what turns a latency bound into a
  guarantee that the whole inventory is covered in a finite number of writes.
  A failed cursor write is swallowed, and must be: the rotation has already
  committed, and a hook that made the routing decision correct must not then
  report failure over housekeeping. If that cursor can never be written —
  a read-only `.antiphon/identity`, a full disk, a permission fault — the sweep
  silently falls back to examining the same first eight records on every write,
  and a dead owner's proof sorted beyond them is never collected. Nothing is
  lost and nothing is misrouted; the leak is one small file per dead session.
  The degradation is bounded and named here rather than detected, because
  detecting it would mean a surface that reports a housekeeping fault the
  operator cannot act on.
- **A withdrawn session half leaves a tombstone, and that is why cleanup
  happens at all.** The rotation withdraws every same-owner stale automatic
  half so nothing can route through it. Deleting the half outright made the
  outgrown endpoint read `UNREADY` — the same answer a listener gives before
  its first hook — and those two call for opposite actions: one must wait, the
  other must retire. So the guaranteed cleanup never happened and an outgrown
  socket lived until its process exited. Withdrawal now writes a small
  `retired.json` beside the halves: not joinable by anything, and validated in
  full before it may authorise the one destructive action in this contract.
  Two properties make it positive evidence rather than a mark. It names the
  session it withdrew, and the owner's current session must be a different one
  — a host that resumes a session id is not a rotation, and a listener
  reconnecting under an identity its owner is on again must not read stale
  about itself. And it is consulted only when the half is *genuinely absent*:
  `_session_address` answers "no" for six different reasons, and a read that
  failed or a record that is torn is evidence of nothing, which must never
  destroy a listener. The evidence is written before the half is unlinked, so
  a failure there keeps the half rather than leaving a deletion nothing
  explains. Writing a half again clears the tombstone; one whose endpoint is
  gone has no reader left and is collected on the same locked pass that writes
  them.
- **A rotation costs a reconnect, and that price is accepted.** There is no
  dynamic rename of a live listener: a process serving under one identity must
  not silently become another, because a sender that addressed the old name
  would reach the new session without either side having said so. The
  consequence is real and falls on a person. After a session rotates to a new
  host session, its old automatic alias stops resolving at once and its new one
  is unreachable until a fresh endpoint exists — in practice an MCP reconnect.
  The proof outlives endpoints precisely so that window has a name to show:
  `status` and `doctor` read the read-only inventory and print the current
  alias beside the remedy, while an identity whose owner cannot be proved live
  — a legacy or future owner-key generation has no reproducible fingerprint
  here — is counted, never addressed, and never rewritten to make it
  renderable. The window before the new session's first hook is separate and
  narrower: until that hook commits, Antiphon holds no project-scoped evidence
  the new session exists at all, so nothing here should be read as closing it.
- **Redaction is scoped, and its two exceptions are deliberate.** One central
  redactor per language removes unanchored UUIDs, full identity digests, raw
  owner keys and automatic socket routes from every status, doctor, label,
  refusal and error, before any truncation. Two shapes stay visible on purpose.
  An explicitly named peer keeps its socket path, because the operator chose
  that name and `remove it` needs to say what to remove; only an automatic
  peer's route — derived from a host session id nobody typed, and whose remedy
  was always a restart — is withheld. And the channel's own `antiphon channel
  ready:` line keeps its path: it is a readiness line in the session's own
  terminal, neither a refusal nor an error, and the tests that find a bound
  socket read it. Neither exception is a gap to close later; widening the
  promise would cost an operator the one path they can act on.
- **An endpoint that records no owner key can never be joined.** Two causes and
  no way to tell them apart from a record: one written before the field existed
  (which is what this machine's one live record is), or an `owner_key()` that
  returned nothing at registration. `doctor` names the observable and offers the
  common cause as a cause; the hook stays silent, because saying it once per
  prompt forever is not a diagnosis.
- **Discovery later became durable without changing labels.** The catalog is
  the project inventory; `RECENT_FILES = 3` is now only current-window/degraded
  fallback. Labels still name only live claims among sources actually selected
  for that page, so completeness never becomes dispatch.

### The guardrails, kept verbatim

Awareness never becomes dispatch: a label names a session on a page and changes
nothing about who a message can be sent to. There is no filtering — a page
carries every source it carried before. And no Claude↔Codex pair is inferred
from matching aliases: the join is built for one kind, the kind the reading side
does not own.

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
contain. The sentence costs 140 bytes for a Claude reader and 142 for a Codex
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

**Whether the relayed label should carry the speaking peer's alias — resolved
by the source-aware multi-peer P1 above.** The original work stopped because
only named Codex sessions wrote the alias↔session-UUID half and no page-building
path consumed it. The later source-aware work added `record_claude_session`,
uses `read_session` through the owner-key-checked join, and labels every line of
an eligible record block together. Its measured limits remain in that entry;
this older question is historical, not a second open item.

**Whether an agent should act on a relayed instruction — the bridge answers
provenance and stops.** The shipped sentence says where the words ran and that
they did not run here; whether the reading agent acts on them belongs to that
agent and its own user. The single human who often *is* both sides' user can of
course repeat an instruction in the reader's own session — that is exactly the
confirmation loop working, not a gap in the bridge.

The boundary, stated straight: a reading agent can still choose to act on
relayed words. What shipped makes their provenance impossible to misread, which
is all a label can do.

## P1 — Large direct-message attachments (shipped; acknowledgement and retry closed 2026-09-03)

The direct channel has a separate, honest 128 KiB byte cap, and it stays. What
changed is what happens above it: an oversized direct message is no longer a
dead end. The sender parks the full text and delivers a small envelope naming
where it went.

The entry asked for five things. Three shipped, one shipped in a form the entry
did not ask for and this close names as a deviation, and two — acknowledgement
and retry — were open until 2026-09-03 and are closed at the end of this entry.

### What shipped — the decisions

**The spill lives in the tools, not in the transports.** `reply` and
`_send_tool` decide; `send_to_claude`, `send_to_codex` and `_queue_codex`'s
transport body are untouched. Measured: a spill inside a transport strips the
`[Antiphon bridge] Claude:` / `[Antiphon channel] Claude:` prefix that anchors
the echo guard, so the bridge's own delivery is read back as new traffic and
delivered again — `queue_label`'s docstring names that failure exactly — and it
leaves the mid-turn park holding the original. At the caller layer the envelope
replaces only the outgoing text, and the prefixes, the `[from= id=]` reply
address, the park and the dedupe all see an ordinary message.

**Each direction's predicate mirrors what actually refuses.** The composition
comes first — the full outgoing message, prefix and label included — and that
is what is measured.

- `_oversized_for_claude` reproduces `send_to_claude`'s own JSON
  serialization. `len(text)` and the payload length are different numbers and
  not by a constant: measured, `"` costs two bytes and every control character
  six, so 22,000 control characters serialize to 132,091 against a 131,072-byte
  cap while their raw length is a sixth of it, and the whole 130,982–131,072
  ASCII band is over the cap while reading as under it. A raw-length trigger
  would have left exactly that band refusing.
- `_oversized_for_queue` computes its bound at call time:
  `SC_ARG_MAX` − a 502-byte per-exec overhead − the live environment − the
  fixed argv (the session id included) − a one-page margin. No constant could
  be correct: measured on one machine, one binary, only the environment block
  grown, the largest message that execs fell **1,044,820 → 844,759 → 444,759**,
  byte for byte with the environment. `ARG_MAX` is one budget argv and environ
  share. The single-argument limit is not separately binding on Darwin — a
  1,047,587-byte argument execs against an `ARG_MAX` of 1,048,576 — so the
  formula carries no term for it.

**The file carries its own provenance.** One header line —
`[Antiphon attachment from=… id=… sha256=… bytes=…]` — then a blank line, then
the exact content. Megabytes of the other agent's words must not enter a
context as anonymous `Read` output, and the header says whose they are in the
same read. The hash covers the **content only**, so the rule is one a person
can perform: everything after the first blank line, `tail -n +3 <path> |
shasum -a 256`. The envelope repeats the author, redundantly on purpose.

**The envelope carries an absolute path.** The receiving agent's `Read` tool
requires one, and a session started in a subdirectory cannot rebuild it from a
relative form. Measured on the live install: `setup` writes the same absolute
`ANTIPHON_CWD` into `.mcp.json` and `.codex/config.toml`, so both sides agree
on the root, and `project_dir()` returns it from any working directory. The
path is local, and the envelope and both RULEs say so: same machine, same user,
same project. That bound was previously stated nowhere.

**No orphan ever charges the store.** The order is write → send → on any
non-delivery unlink at once, with one line on stderr. Refusals are the common
case, not crashes: an agent retrying an oversized send against a channel that
is down would otherwise write one full-size orphan per attempt and turn a
transport outage into a seven-day storage refusal.

**Above `ATTACHMENT_MAX` the old refusal returns, unchanged.** No store will
take those words, and the guidance naming the visible-reply road is still the
right message. It falls through to the existing `oversize` wrap rather than
refusing in the tool, which is why no new refusal class was born there.

**The quota refusal is unclassed.** `_ClassifiedRefusal`'s own invariant is
that a class means "the sender needs telling where its words still travel", and
its absence means "leave this message alone, it already names its fix". The
quota refusal names its fix — wait for the TTL, or clear the directory — so it
joins the `addressing` family. Nothing is ever evicted to make room: an
unexpired attachment is somebody's undelivered words.

**The sweep runs on the bridge's own heartbeat.** In `hook`, immediately after
`cwd` is resolved and before the page is built. Both halves matter: before the
resolution there is no store path, and after the page build the ordinary quiet
turn — `hook` has five exits and `if not text` is the common one — would never
sweep. Outside every lock (a hold across `cursor_lock` was measured at 5,008 ms
against a concurrent reader's 2,038 ms of patience), in its own `try/except` (a
non-zero exit suppresses the page), tolerating a concurrent unlink, and never
on the `project_dir()` fallback root this code distrusts everywhere else. The
cost was measured at 1.32 µs against a missing store — the overwhelming case —
and 93.9 µs across 50 files.

**The store is a directory this bridge owns outright, or it is not used.**
Measured before the check existed: with `.antiphon/messages` symlinked at a
directory outside the project, the words landed there and were counted as
though they were here. Every write, count and unlink now runs against a store
whose parent and leaf are checked without following a link, with the leaf
opened `O_NOFOLLOW` so a symlink fails the open rather than being examined and
then followed. A pre-existing loose mode is tightened to 0700 rather than
trusted — `makedirs(..., exist_ok=True)` leaves a 0755 directory exactly as it
found it — and a mode that cannot be tightened fails closed. `drop_attachment`
runs on a failure path against a path a caller supplied, so it goes through the
same validated helper the sweep uses: a uuid-shaped name in somebody else's
directory is not this bridge's file to delete, and before the fix it was
deleting it.

Named limitation, ruled at the review gate: a hair of TOCTOU remains — the
store is proved sound with `O_NOFOLLOW` and then re-opened by path for the
write, so a same-user process racing its OWN store between the two steps could
redirect it. Under this project's stated threat model (one person, one
machine; the envelope itself teaches "same machine, same user") that race is
self-sabotage, not an attack surface, and the reviewer's verdict was PASS on
exactly that condition. The absolute closure is known and named: dir-fd-based
creation (`open(..., O_NOFOLLOW, dir_fd=…)` + `os.replace(..., dst_dir_fd=…)`),
which would replace the `mkstemp` idiom — future work if the threat model ever
widens, not a silent gap.

**The quota is one transaction.** A usage read and the write it authorizes are
not two operations. Measured before the lock: two processes released from one
barrier, a 1,000-byte quota and two 700-byte messages — both passed the check,
five rounds out of five, and the store held 1,400. A flock beside the store
(never inside it, where only `{uuid4}.txt` names may appear) covers the read,
the decision and the write, and is released before the caller sends. `push`
already records what a lock held across a transport costs: a 5,008 ms hold
against a concurrent reader's own 2,038 ms of patience.

**Only `{uuid4}.txt` is ever counted, swept or unlinked**, checked without
following symlinks. The one foreign entry this feature can create is a
`mkstemp` leftover from a write that died mid-flight, and the naming rule
refuses it, so it is reported and left rather than swept as an attachment.

### The bounds

`ATTACHMENT_MAX` 8 MiB per message, `ATTACHMENT_QUOTA` 64 MiB for the store,
`ATTACHMENT_TTL` 7 days. All three count **content** bytes, header excluded —
the cap, the quota and the status line alike — and all three are pinned in
README §Limits by the contract technique this project already uses for
`PAGE_BUDGET` and `MAX_CHANNEL_BYTES`, so drift is loud. A contract test reads
this file too, but only for the names of open gaps — never for a number, which
is why the numbers live in the README.

The TTL makes a file *eligible* for removal; it does not remove it. There is no
timer. The next hook either side runs does the deleting, so a project where
neither side takes a turn keeps its files until one does. The envelope, both
RULE sentences, the quota refusal and README §Limits all say that, rather than
promising a deletion nothing schedules.

The queue's own bound is deliberately unpinnable. It is not a constant, and a
number written down here would be a promise about somebody else's shell.

### The two roads are not the same, and both surfaces say so

The direct tools spill. The `@claude` / `@codex` marker road does not: a marker
line over the cap is still refused, and that refusal prints on an exit-0 Stop
hook, which this file already records as reaching a debug log and not the
agent. That is a decision, not an omission — a marker line's words are already
in the visible reply, and the passive pull pages carry that reply whole, so an
attachment there would duplicate for nobody what pull already delivers. An
agent that learned "oversized direct sends work now" and then lost a `@claude`
line would have been told something untrue, so `AGENTS_RULE`, `CLAUDE_RULE` and
README §Limits each state the asymmetry, with contract tests on all three.

### The deviation, named

The entry asked to clean the store "without deleting an unread message
silently". Unread is not tracked and this does not track it. What ships is
time-based and announced: a file older than the TTL is deleted with a line
naming it, on the next hook either side runs. The party that announcement
reaches is whoever is looking at that terminal — **not** the reader who never
read the message. That is a real gap against the words of the bullet, and
closing it needs the acknowledgement protocol below.

### Closed 2026-09-03, with the delivery ledger

- **Two limits, measured and left.** A receipt that sits in the reader-side
  skipped span (a transcript more than a day behind the other side's newest
  record) is never seen on that road; the sender's own-transcript road is
  horizon-independent, and `status`/`doctor` say "sent before the page
  horizon without a receipt". The ledger is scanned linearly on every hook —
  about 0.9 s at 10,000 entries, measured by the release-gate review — and is
  bounded by the seven-day TTL rather than by a cache; a project that sends
  hundreds of direct messages a day will feel it before anything else does.
- **Acknowledgement** is a read receipt from the named recipient's own
  transcript — a bare delivery's from any transcript of its kind; the reader's
  name comes from its session record, live or not, and an unknown reader
  credits bare deliveries only — the reader that already walks it reports a
  tool call naming
  `.antiphon/messages/<uuid>.txt`, and the ledger entry for that delivery
  gets `read_at`. The sweep collects a read file `ATTACHMENT_READ_GRACE`
  (3,600 s) after the receipt; a file with no receipt waits out the TTL as
  before, and when it goes the entry is marked `expired_unread` and the
  sender's next page says so — the party the stderr announcement never
  reached. `status` counts the parked files with and without a receipt.
- **Retry** reuses: the same words, from the same sender, to the same peer,
  within the ledger's TTL, name the file that already exists (its header
  keeps the first id) under a fresh envelope, and the resend restarts the
  file's clock. Different words, a different peer or a different sender park
  their own file.

The pending-delivery state both needed is the ledger under
`.antiphon/deliveries/`, the same state the reply-correlation entry uses.

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

## Shipped — a bare push goes to a running Codex, never to the newest file

Measured on Codex 0.151.0: a thread opened at 13:03 got its rollout file at
15:13, on the user's first turn, 7,832 s later. Discovery reads rollouts, so
until then the running session did not exist for the bridge, and a push at
15:04 was queued — `codex queue --thread` accepted it, Claude's Stop hook saw
success — into the newest file's thread: an empty session from 12:55 that
nothing would ever drain. A second message had been waiting the same way in a
thread closed at 11:42. Nothing anywhere said so.

What shipped: Codex holds an exclusive flock on
`~/.codex/thread-writer-locks/<id>.lock` from the moment a thread opens and
removes the file when it closes (measured: the live terminal and one desktop
thread held theirs; every closed thread's file was gone). `codex_session_id`
now takes the newest *running* session among the rollouts recording this
directory; where a Codex keeps no such locks the old newest-file rule stands
unchanged. When rollouts exist and none is running, the push is refused with
the reason — a running session gets a transcript only on its first turn and
cannot be addressed until then — instead of stranding the words; the refusal
is classified `no-peer`, so the honest-refusal sentence about the passive page
still follows it. `doctor` reads Codex's own queue read-only and notes, as `·`,
messages waiting in a thread that is not running; only Codex can drain that
queue, so it is never ✗.

Named limitations: the lock is Codex's internal behaviour, feature-detected
and measured on one version, not a contract. A running thread that has not
taken its first turn is still unaddressable from here — the Codex hook does
receive its `session_id` and `cwd` at `SessionStart`, and recording that as a
delivery hint would close the gap, but it changes the rule that an unnamed
Codex leaves no routable peer record. The later visibility fix records only a
diagnostic observation; automatic addressability still belongs with the
automatic-peer-identity entry.

**2026-09-03, the third answer.** `codex_thread_alive` answers True, False
or None: a held lock is proof of life, an unheld lock file is proof of death,
and no lock file at all is *unknown* — measured, the Codex desktop app keeps no
writer locks, so the refuse-unless-proved-running rule had silently closed the
app road. A bare push now goes to a proved-live thread first, else to the
newest thread not proved dead, and the delivery is recorded with that proof
class (`live` or `unproven`) so the tool result and `status` say it; only when
every recorded thread is proved gone is the push refused. Doctor's queue note
counts threads *not proved running*. Measured after: a `codex queue` row to an
open app-hosted thread sat unconsumed for over an hour — the unproven road
delivers to a queue, not to a reader, which is exactly what the ledger and the
word "queued" now say.

## Shipped — the channel never hands Claude Code a null sender

Measured on Claude Code 2.1.251, in the host's own MCP log: every
`notifications/claude/channel` carrying `meta.sender_alias: null` was refused
with `ProtocolError: Invalid params … expected string, received null`, and the
event never reached the agent — while `channel.mjs` had already answered the
sender `{ok:true}` and Codex read "Delivered". An unnamed Codex is the default
install, so the whole Codex→Claude direct road was dark for it; the passive
pull masked this until the upgrade replay (below) starved that road too. The
same day's log shows string aliases going through untouched, and the previous
day's log shows no refusal at all: every live send until then had come from a
named session, so the null branch had never been exercised against the host —
the Node suite drives the server with the SDK client, which does not enforce
the host's schema.

What shipped: `meta.sender_alias` is always a string. A peer with no usable
name arrives as the reserved registry key `<unnamed>` — the one both writers
already share and the alias grammar already refuses — and `reply_to_codex`
reads that key handed back as `to` as "nobody in particular", the bare reply,
never as a peer called `<unnamed>`. The three Claude-side surfaces (channel
instructions, CLAUDE.md rule, README) say "a name rather than the literal
`<unnamed>`" where they said "non-null", and the contract test now rejects the
word "null" on any of them. The Node test pins the wire: nine unusable claims,
including `null` and `"<unnamed>"` itself, all reach the agent as the string
sentinel; the sentinel as `to` produces word-for-word the bare outcome.

Named limitation: this is the host's schema as measured on one version. If a
later Claude Code accepts null, nothing breaks; if it ever rejects the
sentinel string, the same measurement (the MCP log under
`~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-antiphon/`) finds it.
The fresh-user end-to-end run that would have caught this before release is
filed under the release checklist entry below.

## Shipped — a refused send says what the peer will and will not see

The problem was real and the recorded premise was false. Observed twice in one
session: the Codex host answered `direct app-server input is not allowed for
unloaded spawned sub-agents`, `reply_to_codex` failed, and the tool reported
that error and stopped there. The reasonable reading of a failed send is that
the message was lost — which invites repeating it, or proceeding as though the
peer was never told. But the fix this entry prescribed was to promise a delay:
*"passive pull carries everything either side wrote, and a refused active send
changes only the timing."* Measured, what a refused send leaves on the peer's
page is neither everything nor the same thing in both directions.

- **Claude → Codex: a tool-name line, and nothing of the message.**
  `claude_events` (`lib/antiphon.py:1077`) draws a tool event's detail from
  `file_path`, `command` or `pattern`. `reply_to_codex` carries none of them,
  so across 123 real `tool_use` records in this project's own transcripts the
  whole page entry is `· 1 tool calls: mcp__antiphon__reply_to_codex`, and the
  `text` argument is unreachable by any parser path. That sample covers refused
  calls by mechanism: a `tool_use` block is part of the assistant message and
  is written when the call is *emitted*, before any result exists.
- **Codex → Claude: a tool-name line, and nothing of the message.** Before
  Wave 1C, `codex_events` recognised only `exec_command_begin`, a shape absent
  from every measured supported rollout, so thousands of real
  `custom_tool_call` and `function_call` records were invisible. The parser now
  emits the safe compact name from those real call records and deliberately
  excludes their inputs, arguments, outputs and ids. A refused
  `antiphon_send` therefore leaves the call name on Claude's page, never its
  message text.
- **What does survive is the visible reply.** `build_summary` carries assistant
  text verbatim, marker line included, and `_build_page`
  (`lib/antiphon.py:1415`) never splits a record. So the guidance points there
  and claims order and completeness only — never timing. A page is bounded by
  `PAGE_BUDGET` and `EVENT_LIMIT` and filled oldest-first, so under a backlog
  the words land on the peer's third or fourth turn; "on its next turn" would
  have been the same species of false promise as the one being removed.
- **The Stop hook is not a second transport.** `push()` → `deliver()`
  (`lib/antiphon.py:1806`, `1879`) calls the same `send_to_codex` /
  `send_to_claude` the tool just called, so a refusal repeats byte-identically
  after the prefix. Advising a sender to re-address the line would be advising
  it to reproduce the failure. (A failed push does not record its fingerprint,
  so the line is genuinely re-offered every turn until it succeeds — that is
  the true guarantee, and it is not one about the refused send.)
- **The no-session refusal is about addressing, not about readership.**
  `not delivered: no Codex session found in this directory` means
  `codex_rollout_files(cwd)[:1]` was empty (`codex_session_id`,
  `lib/antiphon.py:1762`). The Codex-side page is built from **Claude's**
  transcripts and never consults a Codex rollout, so it is fully readable
  exactly where that refusal fires — measured in the same fixture that produces
  it. This is the class where naming the passive page matters most, not least.

### What the surfaces say now

`TOOL_GUIDANCE` with a `{seen}` slot the reading surface fills from its own
measurement: `only a tool-name line` from both `reply()` and `_send_tool`. It
is appended only when the detail was born
carrying a class — a `str` subclass with `refusal_class`
(`_ClassifiedRefusal`, `lib/antiphon.py:2000`), wrapped at the birth sites
below. Widening the `(ok, detail)` pair to carry the class instead was measured
at 72 red tests; the subclass costs zero and survives every existing unpack.

| class | born at | guidance |
|---|---|---|
| `transport` | `_queue_codex` ×4 (one of them the crash-belt: an exec the kernel refuses, measured at 1.1 MB of message), `send_to_claude`'s five socket/response failures | yes — the words are nowhere on the page |
| `no-peer` | `_legacy_target`'s no-session message | yes — the page carries them regardless |
| `oversize` | `send_to_claude`'s 128 KiB pre-transport refusal | yes — an oversized record still travels whole through the automatic hook |
| `addressing` | `resolve_target`'s six sites | **no**, byte-identical: they already name the fix, and those sites are never wrapped, so there is nothing to append |
| **push**, every class | — | **no**, byte-identical: its failure print is exit-0 hook stderr, which reaches a debug log and not the agent |

`lib/channel.mjs` is untouched. It hands the calling agent
`detail.slice(0, 500)` of Python's stderr line, which `reply()` writes as
`reply: <detail>`; the longest guidance-carrying detail is a host refusal cut
at 200 characters (`no-peer` is 54 and `oversize` shorter still, so neither
moves the number), giving 396 of 500 with 104 characters of headroom. A
contract test measures that line end to end rather than recomputing it.

### The entry's open question, answered

It asked whether any refusal exists that the pull path genuinely cannot cover.
Pull covers every *refusal*. What it cannot carry is text that exists only
inside a tool call's arguments — in both directions, and that is precisely the
text a refused send consists of. Which is why every guidance points at the
visible reply rather than at the send being retried. The route that would
change that answer is the tool-call retrieval item under P0's *Still open, by
name*; until it exists, a tool argument is unreadable to the other side whether
its call succeeded or not.

### What "in full" is bounded by

- `antiphon_read` refuses a record over `PAGE_BUDGET` without advancing (the
  measured Codex-MCP-result caveat above). It cannot bite the `oversize` class,
  whose reader is always Claude and whose channel exposes no `antiphon_read`,
  and the automatic hook delivers the record whole either way.
- Complete discovery reads every safely proved project source in the durable
  catalog. While the catalog is building or degraded, the newest
  `RECENT_FILES = 3` transcripts remain a bounded fallback and the page says
  explicitly that its boundary is incomplete.
- A peer with no cursor positions starts its window at `LOOKBACK` —
  **six hours** (`lib/antiphon.py:61`, applied in `positions_for` at `708`). A
  Codex session that starts more than six hours after the words were written
  begins past them, which bounds the `no-peer` case's future reader.

## Shipped — `antiphon doctor`

The default read-only command explains the common “bridge is quiet” cases;
explicit configuration repair is `antiphon doctor --fix`.
Seven checks, in print order:

1. **Install** — which `antiphon` `PATH` resolves, against the package root of
   the copy running the check, `realpath` on both sides. Same install is `✓`
   with the version; a different copy of the same version is `·` (a
   maintainer's clone beside a global install is not broken, and the hooks use
   `PATH`'s copy either way); an **older** copy on `PATH` is `✗`, because the
   hooks that actually run are the old ones; a newer one is `·`. Versions are
   compared as tuples of ints — measured, plain string comparison inverts on
   three of four realistic pairs, `0.9.0` reading as newer than `0.10.0` — and
   anything that will not parse that way is `·` “cannot compare”, never an
   ordering guess. No `antiphon` on `PATH` while hooks call it is `✗`.
1b. **Running servers** — `ps -eo pid=,lstart=,args=` behind a third seam
   (`_process_table`, beside `_which` and `_tool_version`), filtered to the two
   long-lived servers by the script in their argv; the wrapper only spawns and
   is not one. A server whose package root's code files (`lib/antiphon.py`,
   `lib/peers.py`, `lib/channel.mjs`, `package.json`) changed after it started
   is `✗` with both times and "restart that session" — measured 2026-08-31,
   four servers were answering with pre-merge code while doctor said 13/13 ✓.
   A server whose root is gone is `✗` as an orphan (measured: a two-day-old
   channel under `launchd` from a renamed directory). The verdict is scoped to
   the copy this project's hooks run plus pids registered in this project;
   other installs are one path-free `·` count, not this project's failures.
   The registry lends an in-scope pid its alias, scanned without pruning.
   Nothing running is `·`.
2. **Interpreters** — this run's Python against a `PYTHON_FLOOR` constant bound
   to the README by contract test; what the wrapper's bare `python3` actually
   resolves to on `PATH`, which is a different question (measured on the
   maintainer's machine: Anaconda 3.14, not the 3.9 the suite runs under); and
   `node --version` against `engines.node`.
3. **Configuration** — `.claude/settings.json` (both hooks and the
   `mcp__antiphon__reply_to_codex` permission), `.claude/settings.local.json`
   (the `enabledMcpjsonServers` entry that gates the channel server at all),
   `.codex/hooks.json` (three events and the approval-prompt label),
   `.codex/config.toml` (the table, its `env_vars` line, and the `ANTIPHON_CWD`
   **value** — a table pointing at a renamed directory reads another project's
   registry with every key present), `.mcp.json` (the channel entry). Missing
   pieces are named; the repair is `antiphon setup` or the explicit
   configuration-only `antiphon doctor --fix` mode described below.
4. **Alias** — through `peers.explicit_name()`, the exact function production
   routing uses, never the raw environment. Measured: it lower-cases, so
   `ANTIPHON_NAME=UI` is a working named session a raw read calls invalid.
   Doctor prints the name it returns, because `@claude:UI` addresses nobody.
5. **Peers** — `_scan` plus `_record_alive`, listed by kind and alias.
6. **Channel reachability** — a probe, not a `stat`.
7. **Codex delivery** — `codex` on `PATH`, presence only.

**Vocabulary and exit contract.** `✓` fine, `·` nothing to do here, `✗`
broken; only `✗` makes the command exit 1. A set-up project with no session
running prints only `✓` and `·` and exits 0 — pinned by test. A diagnostic
that warns about the normal resting state is one people learn to ignore.

**The default is zero writes, enforced.** The default `antiphon doctor` remains
read-only: it never opens a file for writing, never takes
the registry lock, and calls none of the three readers that prune —
`peers.read_peers` (`peers.py:425`), `_live_by_kind` (`antiphon.py`, what
`status` uses) and `resolve_target`. All three delete a dead peer's record on
the way past, which would remove exactly the stale record somebody ran the
command to ask about. A test snapshots bytes, sizes and mtimes under two roots
— the project fixture and the external socket directory — before and after, on
a broken fixture and a healthy one, each with a corpse armed.

**Configuration repair is explicit.** `antiphon doctor --fix` writes project
configuration only, through the same idempotent `setup()` path, and then marks
and runs a fresh read-only doctor pass. It does not delete or repair sockets,
peer records, cursors, queues, transcripts or attachments and does not start a
host process. Setup's own stdout/stderr is not hidden; a file it refuses keeps
the combined command nonzero even if the re-check can read everything else.

**The socket `✓` is falsifiable.** Connect → `shutdown(SHUT_WR)` → read one
reply → close; nothing is ever sent. The half-close is load-bearing: the
channel server answers from its `end` handler, so measured against the real
server the reply arrives in 0 ms with it and never without — the obvious
order reports every healthy bridge as broken and tells the user to restart.
The reply must parse as a JSON object carrying an `ok` key; the *presence* is
the signal and the value is ignored deliberately, because the healthy answer
to a bare connection is `{"ok":false,"error":"Unexpected end of JSON input"}`.
Any process can bind that path, so without reading the answer the check would
pass for all of them. Retry patience is spent only where a registered live
peer claims the address: `NOT_LISTENING_YET` includes `ENOENT`, so a blind
retry at the unregistered project default spends 1,545 ms over 28 attempts on
the perfectly normal no-socket state. That split cannot reintroduce the race
the patience exists for — the channel server claims the registry before it
binds, and a contract test pins that ordering.

**`status` uses the same content-free channel probe.** Both commands select the
same relevant registered, configured-name or legacy target. `Claude channel: live`
now means at least one listener answered with the Antiphon JSON shape;
neither a registry record nor a socket pathname earns it. Registered targets
receive bounded startup patience. An unregistered configured alias and the
ordinary no-peer legacy path are tried once, so an idle `status` does not spend
roughly 1.5 seconds retrying an absent socket. `status` stays informational and
exit 0; doctor alone explains the failure class.

**Privacy.** No session ids, no cursor contents, no addresses in the peer
list. One deliberate exception: the stale-socket and not-a-socket repair lines
print the socket path, because that is the file the person may need to remove
(five repair lines in total print it — every not-listening/not-a-socket/
cannot-connect arm, per probed address).

The configuration envelope is shared too: immutable `CONFIG_KEYS` supplies
`permissions`/`allow`, `mcpServers`, `enabledMcpjsonServers` and the hook entry
fields to both setup's writer helpers and doctor's readers. A fixture replaces
that vocabulary and proves both halves move together; real on-disk keys remain
byte-compatible.

**Also fixed:** `antiphon --help`, `-h` and `help` print the usage and exit 0.
They exited 1 on all three spellings, and the check runs before the command
table so `antiphon help doctor` is not an arity error.

### Supported and explicitly declined

The thread-writer lock and the queue's read-only inspection remain supported
Codex evidence: the former distinguishes a stopped known thread without
executing it, and the latter names this project's stranded queue rows without
creating or changing Codex's database. `codex` on `PATH` remains a presence
check.

Active Codex reachability is declined until Codex exposes a bounded,
non-spawning, version-detectable API. Executing `codex` from a diagnostic can
block on authentication or start a session, so it cannot truthfully be a
read-only health probe. `ANTIPHON_NAME` forwarding is no longer an open
question: a live `ANTIPHON_NAME=probe codex exec` measurement found the same
value in the MCP child's environment, recorded under the unnamed-peer entry.

## P2 — Reply correlation (closed 2026-09-03: advice inside the refusal)

Explicit `to` remains the safe default when several peers are live. Automatic
reply routing needs a durable design before implementation:

- correlate only after a successful delivery acknowledgement;
- scope pending messages to the receiving peer;
- validate the original sender is still the same live session;
- let explicit `to` override correlation;
- fail closed when several unanswered senders remain;
- define expiry and cleanup without losing a late reply.

**Decision, 2026-09-03.** Automatic routing is not built. The delivery
ledger answers the first, second, fourth and sixth bullets — a correlation
reads sent entries scoped to the receiving alias (or to nobody), an explicit
`to` never consults it, and entries expire with the ledger's TTL — and the
answer is *advice inside the refusal*: a bare reply refused among several
peers ends `; the last unanswered sender was 'build' (5 min ago): pass
to="build"`. The bridge still never chooses; when several senders are
unanswered the newest is named as advice and the refusal stands, which is the
fifth bullet's fail-closed. The third bullet (the same live session) is what
the alias grammar and the registry already guarantee for a named peer, and an
unnamed sender is never advised.

## P0 — A named Claude session can identify itself as `<unnamed>` (fixed)

From a user report on 0.3.3 (macOS 26.3, Node 22.16, Python 3.12.7; one Claude
and two Codex peers, all named): for 38 minutes every Codex → Claude send
failed with `Claude MCP Channel is down: No such file or directory` while
Claude → Codex succeeded throughout. The Claude peer had a `session.json` and
no `endpoint.json`, so `resolve_target` could not move the address to the
named socket and returned the project-wide path, where nothing listens — while
the named socket was bound and serving the whole time. The reporter's own
words for the shape of it: it presents as "the other agent is ignoring me"
rather than as a transport fault, because the sender falls back to the passive
page and the receiver is told nothing at all.

**The report's stated root cause is a false inference, and must not be acted
on as written.** It observes `grep -c endpoint lib/channel.mjs` → 0 and
concludes the Node arm never publishes an endpoint record. It does:
`channel.mjs:235` is `const claimPeer = () => registryCall("register_peer")`,
which runs `python3 lib/antiphon.py register_peer` with kind, name, address
and pid on stdin. The word never appears because the file's name belongs to
the Python half. Adding a second writer there would double-register.

**What was measured here.** Two `channel.mjs` servers started for one project
under the same `ANTIPHON_NAME`: the second was refused —
`register_peer: peer name 'mainclaude' is already held by pid 18763` — and
`endpoint.json` survived intact, still naming the serving process. So the
atomic claim protects the record in the ordinary duplicate case, and the
hypothesis that a second server releases the first's registration is
**disproved**. Why the reporter's first server (pid 6519) held the socket
without ever registering is **not root-caused from here**; `registryCall`
writes its failure to stderr, which for an MCP server reaches the host's log
and never the person. Their `mcp-logs-antiphon/` is where that evidence is.

**A product path to exactly that state is now root-caused and reproduced.**
`peers._process_info` inherited both timezone and locale from its caller and
then assumed the rendered `ps lstart` occupied 24 characters. `birth` and
`owner_key` both embedded that rendering. In a throwaway project with the real
0.3.3 channel, one live pid recorded under `TZ=UTC` read three hours later under
`TZ=Europe/Istanbul`; `_record_alive` called it recycled, `read_peers` pruned
its endpoint, and the same Node process remained bound to the named socket.
The `owner_key` for one real CLI root changed across the same two reads, so a
Codex endpoint and session could also remain live while becoming permanently
unjoinable. A non-C `LC_TIME` adds a second failure to the fixed-width slice.

The socket failure is durable rather than a transient race. Once a live named
listener has no endpoint, every new 0.3.3 channel process claims the alias,
finds that listener's socket, releases its own claim and leaves the listener
unadvertised. This exact chain was reproduced with a plain live listener and a
real 0.3.3 `channel.mjs`. It explains a product route to the report's shape; it
does **not** prove which environment the reporter's host supplied, because the
artifact contains neither that process environment nor its MCP log.

**What is a real defect, independent of that.** `channel.mjs:462-466` assigns
`senderAlias = peerName` only in the branch that both won the claim and serves
the socket. Every other route through startup — claim lost, socket already
served by another process, `serveSocket()` failed — leaves it null, and the
session then signs its messages `[from=<unnamed>]` with `ANTIPHON_NAME` set
and valid. The report saw exactly this: a named peer silently downgraded to
the one condition the documentation says cannot be routed, which then made the
Codex side refuse delivery by name. Identity is not the same question as
channel ownership, and today one answers the other.

**What to change, in the order the evidence supports.**

1. Separate identity from channel ownership: a valid `ANTIPHON_NAME` makes
   this session that peer for the purpose of signing, whether or not it won
   the socket. Losing the channel means it cannot be *reached*; it never meant
   its words come from nobody.
2. Make a registration failure visible where a person looks. `doctor` can see
   the shape directly: a live channel socket for this project with no endpoint
   record naming a live pid is exactly the reported state, and it is
   diagnosable read-only.
3. Refuse by the true reason. When a caller asks for `mainclaude` and no live
   endpoint holds that name, say so instead of falling through to the
   project-wide path and reporting `ENOENT` on an address the caller never
   asked for.
4. Tell the receiver that somebody tried. Nothing on the Claude side learns
   that a peer attempted a send and was refused; both agents concluded the
   other was idle. Even a line on the next page would have collapsed a
   40-minute investigation.
5. Canonicalise process identity at observation time, under `LC_ALL=C` and
   `TZ=UTC`, and version both endpoint births and owner keys. An already-written
   unversioned value has unknown rendering provenance and must not be read as a
   corpse merely because the new canonical string differs.
6. Let a current listener restore only its own missing endpoint through a
   versioned, content-free control request. A bare connect or generic JSON reply
   is not proof; routing must see the listener's matching pid and socket in the
   registry before the original message can be sent.

**Release boundary.** Published 0.3.3 still contains the inherited-timezone,
inherited-locale `ps lstart` fingerprint defect reproduced above. The candidate
branch beginning at `a4533d1` and completed for migration by `6902546`
canonicalises new observations under `LC_ALL=C` and `TZ=UTC`, versions both
endpoint births and owner keys, and treats an unversioned fingerprint as
unverifiable rather than dead. Those commits are on `main` at 0.4.0 and are
not published: 0.3.3 is what npm serves and it still carries the defect, which
is what an operator on an installed copy is meeting.

**What is on `main` at 0.4.0.** A valid Claude `ANTIPHON_NAME` now
signs both of its outgoing roads — the channel's `reply_to_codex` subprocess
and the Stop-hook push —
without treating ownership of the return socket as identity. Codex keeps the
stricter owner-key rule because its MCP server is not the session it names.
The duplicate-name loser is still refused the channel and the startup warning
now asks for a *unique* name; `identitySettled` still resolves after every
startup route. Its label keeps `from=ui` but adds
`reply_to=<unavailable>`; that deliberately invalid return token prevents a
literal reply from reaching the different process that owns `ui`. The channel
owner's ordinary label stays byte-compatible and addressable.

`doctor` derives the current named socket and reports a live listener with no
live endpoint holding that alias as broken, without writing the registry or
socket. A bare send with neither a registry record nor a live legacy socket is
now a classified `no-peer` refusal that says no Claude peer is registered and
suggests addressing a named channel, rather than exposing `ENOENT` for the
implementation path. `_resolve_target` carries that legacy origin internally,
so an ENOENT from one registered peer's missing socket remains the distinct
channel-outage refusal. The already-honest explicit-name refusal remains
byte-identical.

On that explicit-name refusal only, Antiphon makes one best-effort connection
to `sha256(project\0alias)`'s socket. It first sends a versioned, content-free
reassert request. A current listener validates its own alias and writes its own
pid/address through the existing atomic `register_peer` path; the caller checks
the nonce and protocol response, re-reads the registry, resolves the alias
again and only then sends the original payload, once. Generic `{ok:true}`, a
mismatched nonce, a matching reply without the registry record, an old listener
and an arbitrary socket binder all fail closed. If recovery fails but the
socket answers, the existing channel payload carries a bridge-authored notice
with the attempt time and requested alias, no original message content and no
new metadata field. The sender's refusal remains the result; an absent socket
receives no bytes; no pending delivery state is created.

Registry claims, releases and shutdown now share one order. Shutdown refuses
new reassert work, waits for an in-flight claim, waits for startup to finish
probing or acquiring its socket, then performs one final PID-guarded
unregister. Socket ownership starts in the `listening` callback, before the
following `chmod` await. Deterministic delayed-process tests cover SIGTERM
during the first claim, EOF during a control reassert, and SIGTERM after a
claim has completed but before bind; none may leave an endpoint or socket.

New process observations now run `ps` under `LC_ALL=C` and `TZ=UTC`, parse its
fields rather than slicing inherited output, mark endpoint births with their
fingerprint generation and generate owner keys as `pid:vN:start`. The canonical fingerprint is written as `process_birth: "v1:<start>"`, a field
the 0.3.x reader never selects, because that reader interpreted `birth`
against its own timezone and pruned live listeners while `birth_version` sat
unread beside it (reproduced 2026-09-02 with the byte-exact 0.3.3 reader,
`test/fixtures/peers_0_3_3.py`). Both readers select on one grammar and one
range check, by key presence: a present sibling is the only thing consulted;
this generation's token with anything but the writer's shape after it is
malformed, not another generation; the 0.4.0 pair (`birth` + lexical integer
`birth_version: 1`) is migration input read strictly and never dead for
lacking the sibling; anything else is pid-only for liveness and UNREADY for
routing, because a current listener always writes a current sibling and a
governed record without one is one no current listener serves. Integer
tokens are bounded at parse time so a near-ceiling digit run is not a record
on any Python. The claim is a two-way capability: Node declares
`fingerprint_field`, Python acknowledges it, an undeclared automatic claim is
refused with a reconnect remedy, and a listener whose registry does not
acknowledge withdraws its own endpoint and says to reinstall — the
alternative in each direction told the sender it had recovered and then
refused the words. The claim answers the fingerprint it wrote, from its
one observation, never a second `ps` and never the file. Records still in
the 0.4.0 spelling stay prunable by a 0.3.x reader until their owner
rewrites them — a Claude session by reconnecting (the old listener's
reassert is refused without writing), a Codex session by restarting; doctor
names that as a risk. Old readers judge current records by pid alone, as
they always did; only current readers keep the recycled-pid check. Equal
legacy owner keys still join, equal current keys join, and a mixed generation never joins by
pid alone; `doctor` reports that rolling-upgrade state without writing either
record. A reconnect that finds a live current named listener asks that listener
to reassert itself before claiming or touching its socket, so the persistent
no-record loop repairs itself. An old or unverified listener remains a visible
restart requirement. Doctor only reports; it never performs recovery.

README, both generated agent rules and the live channel instructions state the
same identity, reachability, recovery and notice contract. Why the reporter's
original serving process had no registration remains unproven from their
artifact, exactly as above; the deterministic product reproduction is the
evidence for this fix, not an attribution to logs we do not have.

**Operational follow-up.** Read-only doctor now reports a dead-pid endpoint as
a stale record without pruning it; that is separate from `running:`, which
judges in-scope server processes against the code they loaded. Ordinary
channel shutdown through stdin close and wrapper-forwarded signals is fixed:
the wrapper forwards SIGINT/SIGTERM, and the server's idempotent shutdown waits
for startup/reassert work, unregisters its own claim and closes only its own
socket. Abrupt host death or SIGKILL can still leave a stale record or socket
and remain a doctor-guided recovery case; Antiphon does not broadly reap paths
it cannot prove it owns.

The passive fallback arithmetic is now closed by the Wave 1B scheduler above.
The triggering report measured ~940 KB across four sources, two already exited,
with a reply 15-20 turns away. Live and unknown work now takes the active lane;
only current-generation owner proof demotes a source to dead, and mixed backlog
alternates whole pages so neither lane starves.

## P1 — Same-vendor bridging: Codex ↔ Codex and Claude ↔ Claude (shipped 2026-09-03)

Asked for on 2026-08-31 after running two Codex terminals and one Claude on a
second machine. Today the bridge is defined by its two sides: a Claude session
reaches Codex and a Codex session reaches Claude, and neither can reach a peer
of its own kind. That is a property of the surfaces, not of the machinery —
which is why this is worth doing and why it is not free.

**What already exists.** The registry is kind-aware end to end: `peer_dir` and
every record carry `kind`, `read_peers(cwd, kind)` filters on it, and
`resolve_target(cwd, kind, alias)` takes the kind as an argument rather than
assuming one. Both transports are per-kind and already written: a Claude peer
is reached by writing its Unix socket (`send_to_claude`), a Codex peer by
`codex queue --thread` (`send_to_codex`). So `claude → claude` and
`codex → codex` are, mechanically, calls that already compile.

**What is missing.** The surfaces, and every sentence that assumes the other
side. `@codex:name` is parsed only out of a Claude transcript and `@claude:name`
only out of a Codex one; `reply_to_codex` names its recipient kind in the tool
itself; the CLAUDE.md and AGENTS.md rules, the channel instructions and the
README all describe a two-sided bridge. The passive pull is the same shape: a
side's page is built from *the other* kind's transcripts, so a Claude session
would not see another Claude session's work at all.

**The questions this opens, none of them settled.**

- *Identity.* An event arriving from a peer of the same kind must still say
  which session spoke, and a Claude reader must not read another Claude's words
  as its own or as its user's. The `sender_kind` field exists; the labelling
  rules were written when kind and side were the same thing.
- *Loops.* Two peers of one kind, each pushing markers the other renders, is
  the first shape in this project where a message can come back to its author.
  The relayed-words work keeps provenance readable, but nothing today bounds a
  hop count, and the cross-vendor entry below wants the same budget.
- *The unnamed default.* Two same-kind peers are exactly the case where names
  stop being optional. See the fixed visibility entry below: an unnamed Codex
  session leaves no routable peer record. Its hook-owned observation can prove
  ambiguity, but cannot tell two terminals apart or make either addressable.
- *Scope.* Whether the passive page should carry same-kind activity too, or
  whether same-vendor stays a direct-message-only road. Carrying it doubles
  what a page can hold and re-opens the discovery window question.

The honest order is: unnamed addressability first (below), because same-vendor
routing is unusable without it, then the surfaces, then loop bounds.

### What shipped (2026-09-03)

The surfaces, on the machinery that already existed. `@claude:name` in a
Claude reply and `@codex:name` in a Codex reply push to the named same-kind
peer from the same Stop hook, under a cursor key of their own
(`last_pushed_<kind>_same`); the Claude channel offers `reply_to_claude(text,
to)`; Codex's `antiphon_send` takes `kind="codex"`. The four questions above,
answered in code:

- *Identity.* A Codex sender's words carry `[Antiphon bridge] Codex:` /
  `[Antiphon channel] Codex:` and the same `[from=… id=…]` label; a Claude
  sender's channel payload carries `sender_kind`, and the notification arrives
  with `sender="claude"`. The self-injection guard knows all four labels, so
  another Codex session's words are never rendered as this one's user. The
  ledger records the sender's kind, so a same-kind refusal is reported to its
  own side and correlation advice never names a peer of the other kind.
- *Loops.* The bridge forwards nothing automatically; a message reaches the
  one peer named on it and stops. There is no bridge-level loop to bound.
- *The unnamed default.* A same-kind send is always addressed: a bare
  `@claude` from Claude, a bare `@codex` from Codex, `reply_to_claude` without
  `to`, `antiphon_send(kind="codex")` without `to` — each is refused with the
  reason, on the ledger, on the sender's next page. A session never addresses
  its own alias.
- *Scope.* Direct-message road only. The passive page gains no same-kind
  lane: it stays the other kind's transcripts, and a same-kind message in
  them is a receipt for the ledger, never speech. Addressed, not confidential
  — a Stop-marker line is part of the sender's visible reply, which the other
  kind's page shows, and a same-kind tool call's arguments stay retrievable by
  their public id (the review of 2026-09-03 measured both). A same-kind
  receipt comes from the receiving session's own hook reading the tail of its
  own transcript, scoped to its own kind and, for a same-kind delivery, to its
  own alias as the named receiver — the sender's verification of a file it
  parked is never the receiver's read (the same review's critical finding).

README, both rules and the channel instructions say it; the rule ceilings moved
200 bytes each for one sentence (CLAUDE_RULE 5,300, AGENTS_RULE 5,800).

## P1 — An unnamed peer is invisible, and two of them are indistinguishable (fixed)

**Current-status note.** The observation writer, positive-liveness rule and
lower-bound census from this entry remain the storage foundation. Its interim
public UUID row and “not addressable” boundary were deliberately superseded by
the Automatic peer identity entry immediately below: a positively live
observation is now projected only as a public `auto-…` alias, while the UUID,
route and digest stay private. Unknown observations remain count-only. The
paragraphs below describe the B1 checkpoint and its evidence, not the final B2
presentation contract.

Measured on 2026-08-31, from a real session on a second machine: with two
unnamed Codex terminals and one Claude, Claude could only answer whichever
Codex it had last exchanged with, and could not reach the other at all. A Codex
terminal that had not been typed into could not be reached either.

**The shipped defect, read out of the old code.** `record_codex_session`
(`lib/antiphon.py`) returned False before writing anything unless
`peers.valid_name(peers.explicit_name())`, so an unnamed Codex session wrote no
registry or diagnostic record. `resolve_target` could not see it and fell back
to `_legacy_target`, which picked the newest *running* rollout — one session,
chosen by recency, with no way to ask for the other. Refusing to invent a name
was right; being unable to prove that two unnamed sessions were live was the
gap.

**What was measured today, and what was not.**

- `ANTIPHON_NAME` *is* forwarded by Codex to the `antiphon mcp` server it
  spawns: a session started as `ANTIPHON_NAME=probe codex exec` had a live MCP
  child carrying `ANTIPHON_NAME=probe` in its environment. This closes the
  open question in the doctor entry above, which could only verify that the
  `env_vars` line asks for the forward.
- In that same `codex exec` probe no registry record appeared at all, named or
  not. Not root-caused: `register_codex_peer` gives up silently when
  `peers.owner_key()` cannot identify the owning Codex process, and `codex
  exec` is not an interactive session. Whether an interactive
  `ANTIPHON_NAME=x codex` registers before its first turn is **unmeasured**,
  and it is the measurement this entry needs first.

**The interactive measurement was stopped at the trust boundary on
2026-09-01.** Codex CLI 0.151.0 showed its directory-trust prompt before the
fresh temporary project's `SessionStart` hook ran. The probe chose `No, quit`;
no hook capture, writer lock or project rollout appeared, and the guarded Codex
global files remained byte-identical. This is not evidence that the pre-turn
observable is absent. B1 therefore assumes the blind window may exist and uses
lower-bound wording until a user-present measurement can settle it.

That safe stop exposed a separate machine-local security finding. The user's
Codex config still explicitly trusts four deleted probe directories:

- `/private/tmp/antiphon-name-probe.4eaFUC`
- `/Users/serkancangokalp/Documents/antiphon/.antiphon-hook-probe/project`
- `/private/tmp/antiphon-delivery-measure.N5smiV/hook-probe`
- `/private/tmp/antiphon-hook-order-probe-20260830`

Recreating any of them would load new project content under an old trust
decision, so this work will not use them. This is an operator-visible cleanup
item, not an Antiphon product migration: tell the user and remove or re-evaluate
the stale entries only with their direct participation; do not silently edit
their global Codex configuration.

**The B1 checkpoint contract.** Every unnamed Codex hook event with a canonical host
UUID atomically refreshes one project-local observation file under
`.antiphon/observations/codex/`. It contains only a schema version, the side,
the full host session id and the observation time — never a name, transcript or
route. A named peer joined to that same id suppresses the older unnamed
observation in one read snapshot without deleting another writer's evidence.

The exact writer lock is the only positive liveness proof. A held lock makes an
observation live; an unlocked, missing or unsupported lock makes it unknown,
never dead. Unknown records are retained because their absence of a lock is not
proof of death, and `status`/`doctor` show only their count. Positively live
observations appear only on a labelled local diagnostic row carrying the full
UUID. Refusals, errors and every other line expose neither that id nor paths.
The UUID is explicitly not an alias and cannot be addressed. Invalid routing
and malformed hook-session values are never echoed, so wrapping or decorating a
UUID cannot bypass the rule; invalid configured-name values are described
without ever repeating their value. A failed observation write reports only
its exception class and numeric errno.

Observation reads are fail-closed: the schema version is an exact JSON integer,
the time is finite and representable, and malformed or overflow-sized records
are ignored rather than breaking routing or diagnostics. `status` and `doctor`
apply the same owner-key-validated endpoint/session join on one snapshot, so a
named session's older observation is neither probed nor counted a second time.
Doctor performs that join on a copy and remains read-only.

Every Codex census is a lower bound and says that sessions before their first
hook may be invisible. Two or more positively live candidates — registered
peers plus unjoined unnamed observations — make a bare Codex send refuse before
rollout discovery. An exact valid alias bypasses the observation census. Zero
or one live observation preserves the pre-existing legacy single-peer route;
unknown observations cannot manufacture an ambiguity. This deliberately keeps
the default one-pair workflow compatible while preventing delivery once the
bridge has positive evidence that it would be guessing.

The four agent-facing surfaces agree on the remedy: restart each intended
terminal with a distinct `ANTIPHON_NAME`, then address it by name. This closes
visibility and honest ambiguity, not automatic identity or unnamed
addressability; those remain the next entry's work.

## P2 — Automatic peer identity (on `main` at 0.4.0)

The design is approved after measuring both hosts on 2026-09-01. Claude Code
2.1.251 returned exactly one interactive record from `claude agents --json
--cwd <project>`: its pid was the same canonical CLI root independently found
by `owner_key`, its cwd matched exactly, and its `sessionId` was canonical.
Twelve local runs took 141–183 ms. Codex 0.151.0 exposed no session or thread id
in the Antiphon MCP process environment, and the Codex App can multiplex work
through a shared app-server, so neither an MCP ancestor nor an owner key may be
promoted into Codex public identity.

**The approved contract.** A canonical host session UUID maps to `auto-` plus
the lower-case unpadded base32 of the first 128 SHA-256 bits: 31 characters,
inside the existing alias grammar. Records also carry and compare the complete
256-bit digest. The short alias is therefore useful, never authoritative: a
different full digest or an explicit alias at the same name is a collision and
delivery refuses. `owner_key` remains only process ownership and endpoint/hook
join evidence. It is never a public identity, never user-settable and never a
fallback seed. An explicit valid `ANTIPHON_NAME` deliberately overrides the
automatic road; an invalid configured value does not fall through to automatic
identity.

Codex gains an automatic peer only after its existing B1 observation has a
positively held exact writer lock. The observation is projected read-only into
an addressable peer whose route is the canonical host session UUID; no endpoint
record is invented, and the shared MCP server makes no identity guess. Unknown
observations remain retained counts and create neither an alias nor ambiguity.
One automatic candidate preserves the legacy bare single-pair route; two or
more positive candidates refuse. One explicit named Codex peer retains the
conservative refusal because a pre-hook unnamed session still cannot be ruled
out.

An unnamed Claude channel performs one bounded fixed-argv Python probe of
`claude agents --json --cwd <exact project>`. It accepts only one schema-valid
interactive record whose pid equals the canonical Claude CLI root and whose cwd
matches exactly; otherwise it remains unnamed. The record's generated `name`
is ignored deliberately — it is host display metadata, not Antiphon identity.
The Claude hook derives the expected alias and full digest independently from
its supplied session UUID, and writes its session half only beside a live
automatic endpoint with that alias, digest and owner key. A mismatch stays
unjoined and visible; it never repoints a peer. Automatic outgoing identity is
published only after that authenticated join. Explicit Claude identity keeps
the existing identity-versus-reachability rule.

Status, doctor, refusals, labels and errors may show the public `auto-…` alias
but never the underlying session UUID, route or digest. This re-verifies B1's
privacy rule for the new internal address shape. README, both generated agent
rules and the live channel instructions say that automatic names appear after
the first trustworthy observation/probe, the Claude host's generated display
name is ignored, and `ANTIPHON_NAME` is the explicit override. Older peers that
cannot supply the new proof remain on the existing unnamed/explicit paths; no
mixed-version guess or automatic migration rewrites their records.

**What is on `main` at 0.4.0.** The registry derives the
pinned 31-character alias, stores and validates the complete digest, rejects
explicit/automatic and same-prefix/full-digest collisions, and refuses an
automatic session whose digest does not belong to its canonical UUID. Codex
routing, Stop labels, passive-page labels, status and doctor share the same
positive writer-lock projection; exact unrelated explicit aliases still bypass
the private observation census. One automatic Codex candidate preserves the
bare single-pair road, while multiple positives or a collision refuse without
printing an internal route.

The Claude channel runs the fixed probe only when no `ANTIPHON_NAME` was
configured, validates the alias/digest relation independently in Node, stores
the automatic metadata on its endpoint and publishes the alias only after the
hook's session record matches endpoint, owner, UUID-derived digest and current
channel pid. Probe failure, a mismatched join and malformed probe output remain
unnamed; an explicit valid name skips the probe. Node and Python now accept the
same canonical UUID grammar, including a pinned UUIDv7 regression case.

README, setup guidance, both generated rules and live channel instructions now
state the first-hook/probe windows, ignored Claude display name, explicit
override, one-vs-many bare-send rule, mixed-version refusal and privacy
boundary. This work is merged to `main` and carries version 0.4.0, certified
on an exact commit by `fresh-user.sh` and reviewed independently. **It is not
published to npm**: `npm i -g antiphon` still installs 0.3.3, which does not
contain it; installing from the repository does. See the release-gate note
under P1 for why publication is held. The test count moves with the suite and
is not restated here — a number written into prose is stale the next time
anyone adds a test, and this one was stale by a hundred.

## P2 — Cross-vendor managed workers (shipped 2026-09-03, MVP)

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

### Decisions taken (2026-09-03) — `docs/superpowers/specs/2026-09-03-managed-workers-design.md`

- **Both modes, explicitly.** `kind` names the worker's kind (the other side by
  default); with `to` the task is handed to that running named peer of `kind`
  over the ordinary addressed send, marked `[Antiphon task <id>]` and on the
  ledger; without it a fresh worker starts. Neither a kind nor a peer is ever
  guessed.
- **One task, ephemeral.** A worker runs one task and exits; no resume — a
  follow-up is a new task. Records live a week under `.antiphon/tasks/`,
  worker directories under `.antiphon/workers/<id>/` go when the result was
  collected or the task cancelled, stay for inspection after a failure until
  the record expires, and are never touched while the worker runs. At most
  four workers per project.
- **CLI subprocess adapters only**: `claude -p` and `codex exec`, the two the
  release's own E2E proves. No host-native cross-vendor API is depended on.
- **Read tasks run without another confirmation; a write task never merges
  itself.** Read: the host's default class, read-only. Write: its own worktree
  and the host's default sandbox, a diff as evidence, applied only by the parent
  or the human after `git apply --check`. Never
  `--dangerously-skip-permissions`, never `--full-auto`.
- **No synchronous delegate; a bounded wait on result** (`wait` ≤ 300 s).

### What shipped (MVP)

`lib/workers.py` (the store, the adapters, start/status/result/cancel/sweep,
the hop budget), `antiphon task`, `antiphon_delegate` and `antiphon_task` on
both servers, the label `[Antiphon worker <kind>:<id>]` in every worker's prompt
and its registry name `worker-<id8>` on the page, the sweep on the hook. Exit
codes come from the worker's own exit file (a shell wrapper writes it; the
asking process never started the worker), liveness from the pid and its start
time, timeouts by SIGTERM then SIGKILL on the worker's session. Measured on
macOS: a zombie session leader makes `killpg(pgid, 0)` answer EPERM, read as
gone. Not in the MVP: native subagent UI on either host, resume, native worker
APIs — the portable contract is the task id, the lifecycle and the evidence.

Reviewed 2026-09-03 by an independent read-only pass (five Critical, seven
Important), each verified and fixed: the hop budget did not reach a Codex
worker's server (Codex forwards only the variables its config names) — the
config now forwards `ANTIPHON_HOP`/`ANTIPHON_HOP_BUDGET` and a worker whose
server cannot see its hop is refused on its name alone; a claude read task ran
under the parent's own permission mode in the parent's tree — read tasks run
under `--permission-mode plan` and, in a checkout, in a worktree of their own;
the diff was taken against the index (blind to staged and committed work,
and shipping the bridge's own files for loose work) — it is taken against the
recorded base, and the bridge's files sit beside the worktree, never inside
it; the Codex server's blanket tool approval covered the two process tools —
they ask first; the hook's sweep could spend ten seconds per stuck worker —
it sends the signals and moves on; a result without its evidence is not
collected; the test summary path is named to the worker; a refusal on any
road leaves no record; an oversized handed task is parked; the Codex
server's result wait is capped and a tool fault is an error result, never
the server's end; cancel and timeout are pinned by the worker's real death.

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

## Shipped — explicit multi-line Stop markers

Stop markers still never guess continuation from following prose. A sender who
needs a natural multi-line handoff opts in with a message exactly `<<TOKEN`,
where `TOKEN` matches `[A-Z][A-Z0-9_]{0,31}`, and closes it with the first later
exact `TOKEN` line. The body between those structural lines is preserved,
including whitespace and line endings.

The parser is deliberately fence-unaware and non-nesting. A marker-looking
line inside the body is content; the first exact current token closes even in a
Markdown fence, so the sender chooses a token absent from the body. A message
beginning `<<` but not matching the grammar is reserved and refused rather than
silently sent as a one-line message. Literal text beginning with `<<` remains
possible inside a block body.

Invalid or unclosed syntax rejects every marker for that target in the turn
before transport. The unclosed refusal names only its bounded token; the
invalid refusal echoes no sender text, and neither leaks the body. An empty
block uses the existing empty-message refusal without invalidating valid
siblings. Syntax refusal records no delivery fingerprint and consumes only the
mid-turn park that this Stop already owned.

Recipient grouping, source order within a recipient, whole-body size checks,
turn-scoped dedupe and the marker/direct-tool attachment asymmetry are
unchanged. Compatibility tests pin existing one-line parse tuples and both
directions' outgoing transport strings exactly. Real throwaway Claude and
Codex transcripts drive block parsing through the Stop readers and cursor
dedupe; malformed turns, CRLF, fences, nested-looking text, empty bodies and
oversize refusal have focused coverage. README, both generated rules and the
channel instructions state the same syntax and literal-`<<` escape.

## Shipped — a fresh-user end-to-end run, and doctor scoped to this project

`npm test` drives the servers with an SDK client and fixtures. It was green on
2026-08-31 while three faults were live at once — a null `sender_alias` the
host rejected, a push queued into a thread nobody would read, an upgrade
replaying from byte zero — because each needed a real host, a real CLI or real
transcripts to see. `test/e2e/fresh-user.sh` is the run that sees them: it
packs this tree, installs it into a throwaway prefix, sets up a throwaway
project, drives `claude -p` and `codex exec`, and asserts on what the person
would actually get. It is not part of `npm test` (it needs both CLIs logged
in, the network, and 2 to 6 small model calls) and belongs in the release ritual
between the wrapper census and `npm version`.

What the first run measured, in order of what it changed:

- **doctor judged other projects.** On a correctly set up fresh project it
  printed four ✗ about another project's servers and exited 1, so a healthy
  project could not be told from a broken one. `running:` is now scoped to the
  copy this project's hooks run (PATH's `antiphon`, `realpath`) plus any pid
  registered here; the rest are one `·` count with no paths and no verdict.
  The `codex queue:` note is scoped the same way — only threads one of this
  project's own rollouts records — because Codex's queue is one database for
  every project on the machine. A thread whose rollout has aged out of
  discovery drops out of the note with it.
- **`claude -p` runs hooks in an untrusted workspace; only `permissions.allow`
  is ignored.** Measured: `.antiphon/` appears, the peer registers, `push`
  runs. The first reading of the warning ("this workspace has not been
  trusted") suggested otherwise and was wrong. `--dangerously-skip-permissions`
  does not change it, and `CLAUDE_CONFIG_DIR` isolates the login too, so the
  script grants no trust and asserts it granted none.
- **`codex exec` fires `SessionStart` and never `UserPromptSubmit`**, so a
  non-interactive Codex is never injected with a page. The script therefore
  runs the command `.codex/hooks.json` declares, with the payload the host
  sends; the wiring itself is what `doctor` checks. It also runs the project's
  hooks with no trust prompt and writes nothing to `~/.codex/config.toml`.
- **macOS `$TMPDIR` is a symlink** and both hosts record the resolved path, so
  a harness that does not `pwd -P` watches a transcript directory that never
  fills. Two of the first run's failures were this, not the product.
- **An exit-zero model turn is not proof of the exact marker it was asked to
  write.** On exact candidate commit `802fc8a`, one fresh temporary run passed
  91 of 93 assertions because the second Claude answer omitted its exact
  marker; the two failures were that one missing fact and its downstream page
  assertion. Two later fresh runs on the same product bytes passed 93 of 93.
  The varying component was the live `claude -p` answer, while the compaction-
  only diff could not reach T2/T3. The candidate harness now accepts only an
  exact trimmed assistant text block — the prompt itself cannot satisfy it —
  and retries only that marker-producing turn after exit zero, up to 3 attempts.
  It prints the landing attempt and carries the exact second transcript
  into the one push. Push, queue, page delivery and all later T2/T3 behavior
  remain single-shot. Final marker exhaustion fails before those assertions
  can cascade and preserves both temporary evidence roots automatically;
  nonzero CLI exit remains a distinct, non-retried failure.
  A deterministic shell fixture executes the same once/preserve guards the
  harness sources, while wiring checks bind the real push and page sites to
  them; it does not stub the entire host-heavy script. The clean exact-SHA run
  therefore remains the separate proof that those guarded sites execute in the
  real flow. This split is an explicit harness boundary, not a claim that the
  focused fixture drove Claude, Codex, setup and delivery together.

Named limitations, checked by hand instead: `-p` mode never loads the channel,
so the host's notification schema — the fault that cost the most today — is
still only visible in an interactive session's MCP log; and `codex exec` writes
its rollout at once, so the window where a live thread has no transcript yet
does not reproduce.

## P1 — Token cost of the passive page and the static surfaces (fixed)

Asked for on 2026-09-02: the bridge must stop growing the token bill of the
sessions it serves. Measured before anything changed, read-only on the
maintainer's own project:

| What | Measured |
|---|---|
| Claude page reader backlog from the live cursor, drained in memory | **more than 400 pages** (cap hit), 2.6 MB rendered, 2,436 messages, every page 31 August — 2,000 tokens a turn on history nobody asked for |
| The same backlog bounded to the last 24 hours / 6 hours | 21 / 6 pages |
| `[external_agent_tool_call: …]` / `[external_agent_tool_result]` assistant records in project rollouts | 8,936 records, 11 MB, rendered as `Codex:` speech with commands and output in full |
| `<codex_internal_context source="goal">` user records | 133, 931 KB, reaching the Claude page as `To Codex:` |
| `# AGENTS.md instructions for …` user records | 18, 32 KB — the bridge's own rule relayed back |
| Claude `isCompactSummary` user records | 6, 104 KB, 17 KB each, always an oversized record |
| Claude interruption literals | 14 records |
| `CLAUDE_RULE` / `AGENTS_RULE` / channel `instructions` | 7,354 / 8,120 / ~6,000 bytes, every turn or every session; the host truncates the instructions |

### What shipped

**Host records are not speech.** Five shapes, each on measured evidence:
`codex_internal_context` joined `CODEX_HOST_WRAPPERS`; a Codex user record
beginning `# AGENTS.md instructions for ` with a complete `<INSTRUCTIONS>` fence
is a host record; a Codex assistant record beginning
`[external_agent_tool_call: NAME]` or `[external_agent_tool_result]` — the
ChatGPT app relaying an external agent's tool traffic, which is that agent's own
activity and already in its own transcript — is filtered whole (rendered as
name-only tool lines instead, 1,022 of them sat inside one day's horizon on the
live project); a Claude user record with `isCompactSummary: true` is a host
record; a Claude user record that is exactly `[Request interrupted by user]`
or `[Request interrupted by user for tool use]` is a host record, by equality
and never by prefix. The Codex Stop reader skips relays too, so an `@claude`
inside a relayed command never pushes. The census utility counts the prefix
shapes beside the tag shapes and a test compares its predicates with
production's.

**The page has a horizon.** A reader never delivers a record older than
`PAGE_HORIZON` (24 hours) before the newest complete record the other side
wrote in any of its sources. A positioned start older than that is moved to
the first record at or after the horizon — bisected over record boundaries
when the span is large, converged to 4 KB, then rescanned one 256 KB slack
back so a local misorder repeats rather than skips — and the skip is counted
and said once on the page where it happened (`skipped: N raw bytes of Codex
activity older than 24 hours in K source(s) — not delivered; the transcripts
keep it`), also when nothing newer is left, never as a silent advance.
`status` counts what the next page will skip. The horizon is relative to the
other side's clock, never later than the wall clock: an overnight run is
still there in the morning, a side that stopped days ago yields nothing
older than a day before its newest record, the suite's fixed-date fixtures
stand, and one record stamped in the future cannot move the horizon past
everything real (0 such records in 203,890 live ones; bounded anyway). Measured per source first, every one of thirty old
rollouts still yielded its own last day — a hundred pages — which is why it is
one moment for the whole reader. Measured after: the Claude reader is 20 pages
behind (88 KB) and the Codex reader 5, with the first page naming 150,137,892
raw bytes skipped in 5 sources. This reverses, for records beyond the horizon,
the sentence above that "skipping is the error this bridge does not accept":
inside the horizon every untrusted start still repeats; beyond it the twenty
deaf hours recorded in this file are the measured cost of never skipping.
`antiphon catch-up` remains the way to skip to the live edge at once.

Reviewed 2026-09-03 by an independent read-only pass (unit runs, targeted
mutations, a 240-case fuzz of the bisection, a census of the live host roots):
the README described the abandoned per-source horizon and now describes the
shipped one; the horizon is bounded by the wall clock; a first record without a
timestamp (101 of 526 Claude transcripts end on one) no longer switches it off;
the skip-only page, the degraded page and the widest envelope are pinned by
named tests; `setup` refuses a rules file that is not UTF-8 the way `doctor`
names it, instead of dying on it after the hooks were written; the generated
section now closes with `SECTION_END`, so a rewrite keeps the notes a person
appended after it, and `doctor` says what a rewrite of an older, unmarked
section replaces.

**The surfaces shrank by about a quarter.** `CLAUDE_RULE`, `AGENTS_RULE` and
the channel instructions keep every fact the contract tests pin and lose the
implementation narrative (v4 cursor keys, the v3 sibling, lanes, compaction),
which now lives in README and this file only. The two rules went from 7,354 /
8,120 bytes to 4,740 / 5,338 in this phase's first cut; the delivery-truth
review then asked for one clause the recipient acts on ("or 1 hour after the
bridge sees it read"), the same-vendor and managed-worker phases added a
sentence each, and the release-gate review found the channel instructions
naming neither worker tool. At 0.5.0 they measure 5,335 / 5,936 / 3,627
bytes, with ceilings pinned at 5,450 / 5,950 / 3,700 in `test_contracts`, so
they cannot regrow unnoticed. The 3,000
the campaign aimed for was not reachable without dropping pinned contract
facts, which fill about 4.5 KB on their own.

**Doctor sees rule drift.** `CLAUDE.md` and `AGENTS.md` whose Antiphon
section is missing or differs from the generated rule are `✗ … run
`antiphon setup``, like the missing permission doctor already reported;
`setup` and `doctor --fix` rewrite the section in place.

## P1 — Re-run the host wrapper census before release

`CLAUDE_HOST_WRAPPERS` and `CODEX_HOST_WRAPPERS` in `lib/antiphon.py` hold
exactly what a census measured (2026-08-30; re-run 2026-08-31 before 0.3.1,
which moved `ide_opened_file` into the Codex set on 4 directly inspected
records; re-run 2026-08-31 before 0.3.2 — 991 Claude text blocks in 86 files,
1,060 Codex in 134, nothing outside either set, no change; re-run 2026-09-01
with the checked-in aggregate-only utility and the production eligibility
rules — 1,181 Claude user messages in 508 files and 1,156 Codex user messages
in 154 files. The seven production-eligible Claude tags and all eleven Codex
tags matched the constants. `<channel>` and `<local-command-caveat>` appeared
only on Claude records already excluded by `isMeta`, so both were removed:
keeping them could silently discard a person's pasted text. The accepted cost
is that a future non-meta host record using either tag would leak one visible
line; `_is_self_injected` is not a second guard for either tag; re-run
2026-09-03 with the prefix shapes beside the tags — 1,245 Claude user
messages in 525 files and 1,515 Codex user messages in 209 files;
`codex_internal_context` (133, the ChatGPT app's goal continuation) joined
the Codex set on that evidence, and the census now also counts 18 AGENTS.md
injections, 4,463 external-agent calls and 4,336 results on the Codex side,
6 compact summaries and 14 interruption literals on the Claude side), and
nothing else. They will go
stale as each host adds, renames or drops its own wrapper tags. Re-run before
every release with:

```sh
python3 test/host_wrapper_census.py \
  --claude-root "$HOME/.claude/projects" \
  --codex-root "$HOME/.codex/sessions"
```

The utility prints aggregate counts only — never transcript text or individual
paths. Review its tag keys, then:

- count every production-eligible, non-meta, non-empty Claude `type=user`
  message and Codex `response_item/message/role=user` message whose joined text
  opens with `<`, split by side, each one carrying its `promptSource` value (or
  its absence);
- for every opening tag that turns up, decide host bookkeeping or a person's
  own words before touching either set — a tag seen on only one side stays out
  of the other's;
- update the sets, the measurement comment above `CLAUDE_HOST_WRAPPERS`, and
  this entry's date together, so none of the three can drift from the other
  two.

The asymmetry that governs a doubtful case: a tag missing from a set lets one
stray host line leak into a summary — visible, and cheap to fix by adding it.
A tag wrongly present deletes a person's message — silently, with nothing left
behind to notice it happened. When the evidence is thin, leave the tag out.

## P1 — 0.4.0 is on `main` and held back from npm

`main` carries version 0.4.0 with the automatic-identity repair merged, and
npm still serves 0.3.3. The hold is deliberate and this is the reason.

Eight independent review rounds ran against the exact release commit — some by
the Codex peer, some by read-only agents — and **every one of them found
something**. Several were destructive: a listener retiring itself on a record
the other reader refuses outright, a process deleting a successor's socket, a
delivery emitted from an endpoint that no longer described the emitter. None of
those survive; each was reproduced, fixed red-first, and pinned.

What stopped the release was the shape of the sequence rather than any single
finding. Late rounds kept finding defects **introduced by the previous round's
fix**: binding the endpoint's pid to the listener left the birth unbound;
binding the birth took its authority from the very record it was judging, and
made the check fail open when that read failed; adding the fail-closed put it
on the inbound gate and left signing calling the raw verdict. Each fix was
correct and each opened the next surface.

The tests told the same story from the other side. Four fixtures written during
these rounds passed for the wrong reason: one landed in an assertion group that
cannot see what it was named for, two never reached the race they described,
one was green because an unrelated precondition was missing. All four are
fixed, and the parity suite now audits its own bucket membership — but a
campaign that produces that many is a campaign whose remaining unknowns are not
estimable from the outside.

So: no open finding blocks 0.4.0, and that is not the same as ready. Publishing
is a decision to stop looking, and the evidence does not yet support making it.
Resuming means another exact-SHA round, not a fresh audit — the state is
certified (full suite, statics, `fresh-user.sh`) at the commit `main` points to.

What is worth doing before it: run a round that finds nothing in product
behaviour, and treat *that* as the signal rather than fatigue.

## P1 — `reply_to_codex` can report success while the peer receives nothing (fixed)

Fixed 2026-09-03 — see *What shipped* at the end of this entry. The record
of the measurement and the ruled-out guesses stays as written.

Measured 2026-09-01 across one long working session: the active channel tool
returns `Channel reply delivered to Codex.` and the peer, asked directly,
reports its `antiphon_read` empty. It happened repeatedly and the peer had to
ask for the same content again. Separately and more visibly, the same tool
sometimes fails loudly with `thread/queue/add failed: direct app-server input
is not allowed for unloaded spawned sub-agents (code -32600)`.

The loud failure is tolerable: it tells the sender to use the other road, and
the passive pull page then carries the words. Silent success is not. A sender
that is told "delivered" has no reason to repeat itself, and the only thing
that surfaced the loss here was the peer volunteering that it had received
nothing — which will not happen between an agent and a person.

### What was ruled out, so nobody re-derives it

- **Attachment parking.** The first guess was that oversized replies were being
  parked and the envelope lost. Measured against: `.antiphon/messages/` did not
  exist at all, so nothing had ever been parked, and `MAX_CHANNEL_BYTES` is
  131,072 bytes while the lost replies were roughly 3,000. Parking cannot be
  the mechanism. This guess was stated before it was measured, which is the
  error the entry below is written to prevent repeating.
- **`_legacy_target` picking the first live thread.** That selection governs
  `antiphon push codex`, not the MCP reply road, so it cannot explain this.
  Worth its own scrutiny anyway: it returns the first live candidate without
  counting how many are live, which is the rule the identity work forbids
  elsewhere — never choose among several positive candidates.

### What is not established

That short replies arrive and long ones do not. It is an impression from a
handful of cases and no mechanism supports it. It is recorded because it is
what the sender noticed, not because it is evidence.

### Conditions measured at the time, for whoever picks this up

Five live Codex thread-writer locks; no Codex peer registered in this project's
`.antiphon/peers/` at all; five candidate rollouts for the main checkout and
none for the worktree the peer was working in; three distinct Claude sessions
committing to this repository. Multiple unnamed peers on both sides is exactly
the condition under which a target-selection defect would be invisible, and it
is the condition this bridge is currently being rebuilt to remove.

### What a fix has to provide

Delivery has to be reported from the thing that proves it, not from the queue
accepting a write. Either the tool returns success only once the message is in
the peer's readable position, or it stops saying "delivered" and says what it
actually did — queued, to which peer, with what left to prove. A refusal that
names its reason is worth more than a success that does not mean anything.

### What shipped (2026-09-03)

The second half of that sentence, then the first. `reply_to_codex` now says
what it did — `Queued for Codex peer 'build' (id …): Codex reads its queue at
its next turn; run antiphon status to see whether it was received` — with the
proof class the address was chosen on (`registered`, `live`, or `unproven`
with its caveat) and the parked file when there is one; it never says
delivered. `antiphon_send` says delivered to the channel and names the id.
Every direct send, and every Stop-marker attempt, is one small file on a
delivery ledger (`lib/ledger.py`, `.antiphon/deliveries/<id>.json`): sender,
peer, transport, proof, the words' SHA-256 and size, never the words. A
refused Stop marker is on it with its reason and the sender's first sixty
characters.

The receipt is the peer's own transcript. The readers that already walk it
report what those records prove — Codex's user record carrying our
`[from=… id=…]` label (180 measured), Claude's channel injection carrying
`message_id` (245 measured), a tool call naming an attachment file — and the
hook and `antiphon_read` write them wherever the cursor advances, the empty
turn included. The sender's next page carries, once, what it was never told:
`Antiphon: your @codex line at 21:44 ("run the suite") was not delivered —
no Codex session found` and `Antiphon: the attachment you sent to Codex at
21:44 expired unread after 7 days`. `status` prints a Deliveries line;
`doctor` notes a delivery without a receipt after ten minutes, read-only.

Measured while building it: a `codex queue` row to an open app-hosted thread
sat unconsumed for over an hour, which is why the word is "queued"; the
Codex desktop app keeps no thread-writer locks, so liveness has three answers
(see *Shipped — a bare push goes to a running Codex*); two rows queued on
2026-08-31 to closed threads are still in Codex's queue, where only Codex can
drain them. A queued-and-unread message is still a loss this bridge can name
but not prevent: that is Codex's queue, and the receipt is the thing that
says whether it was ever read.
