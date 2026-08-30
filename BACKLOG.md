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

## P1 — Relayed human words are not the reader's own user

`build_summary` labels the other side's human as `YOU`. The block header says
which side it came from, but the line itself reads `[11:04] YOU: rewrite the
migration`, and nothing tells the reading agent that this is a person talking
to *somebody else*. An agent that treats it as its own user's instruction has
been handed authority nobody gave it — in a bridge whose whole invariant is
preserving who said something.

Provenance and authority are different questions, and this label answers only
the first. The fix is to say both: relay the words under a label that names them
as relayed, and state once, where the reader cannot miss it, that they are
context rather than a direct instruction. The existing header and footer already
carry that tone; the per-line label is the part that lies.

Worth settling with it: whether the relayed label should also carry the speaking
peer's alias when one is set, and whether an agent should ever act on a relayed
instruction without its own user confirming.

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
exactly what a census measured (2026-08-30), and nothing else. They will go
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
