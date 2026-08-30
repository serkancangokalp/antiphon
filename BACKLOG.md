# Antiphon product backlog

Last reviewed: 2026-08-30

Priorities here describe product risk, not release promises. The bridge keeps
two invariants across every item: it preserves who said something, and it
refuses ambiguity rather than guessing or broadcasting.

## P0 — Lossless, paged context transfer

### Problem

The current pull path does more than keep one prompt small:

- `SUMMARY_BUDGET` keeps roughly 2,600 characters;
- each non-tool event is cut to 420 characters;
- whitespace, including code and SQL line breaks, is collapsed;
- only 40 events, the transcript tail, and a small set of recent transcript
  files are considered;
- the timestamp cursor advances past content omitted by those cuts.

The result is permanent, invisible loss. During development of named peers, a
real Claude task report was visibly cut by this path and its omitted remainder
was nevertheless marked seen. Raising only `SUMMARY_BUDGET` would not fix the
420-character cut, flattened formatting, tail window, ordering, or cursor.

### Decision

Keep each model injection bounded, but remove any lifetime limit on what can be
transferred:

- start with an inline page target near 8,000 characters, measured against both
  real hosts rather than treated as a permanent magic number;
- select completed source records oldest-first and stop only at record
  boundaries;
- preserve text blocks, order, whitespace, code and SQL exactly;
- advance a per-peer cursor only through records written and flushed to that
  peer; delivery is at-least-once, so a crash may repeat but never skip;
- include `has_more` and let the hook and `antiphon_read` drain later pages;
- let a single record larger than the inline page pass whole so the host can
  spill it to a file and expose its path;
- give compressed tool events a stable id and a one-call full-content retrieval
  path;
- have each side's hook maintain a durable source catalog keyed by side and host
  session id, independent of peer aliases and endpoint liveness; pruning a dead
  peer must not make its unread transcript disappear, and the newest-three scan
  may prioritize migration discovery but never define completeness;
- replace timestamp-only cursors with source generation, byte offset and record
  hash anchors that detect truncation, replacement and rotation;
- use a lock beside each peer cursor, not the project-wide registry lock;
- read a registry-supplied transcript only through a descriptor-safe walk under
  trusted transcript roots; never follow a symlink or reopen a checked name;
- migrate old cursors conservatively, preferring a duplicate over a gap, and
  expose backward paging for history an older version already marked seen.

### Acceptance

- A multi-line SQL statement and a multi-block code message arrive byte-for-byte
  in order.
- More than one page drains across repeated reads with no missing record.
- A failed or interrupted injection leaves the cursor before that page.
- One record larger than the inline target remains fully retrievable through
  the host spill path.
- An event larger than the current transcript tail and a transcript outside the
  newest three are still found when their source is known.
- A catalogued source remains pageable after its endpoint closes, and a
  pre-catalog migration inventories older matching sources without treating the
  newest three as the complete set.
- A user message beginning with `<` is not mistaken for bridge metadata.
- Rotation, same-timestamp events, concurrent hook/tool reads, migration, and
  path traversal all have regression tests on Python 3.9 and the current Python.
- Real Claude Code and Codex hook tests confirm inline and spill behavior.

This item replaces the README's current “roughly 2600 characters; newest
messages are kept” contract. It does not set hook output to unlimited: unbounded
context in one model call is unsafe and unnecessary when paging is lossless.

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

## P1 — Large direct-message attachments

The direct channel has a separate, honest 128 KiB byte cap. Keep it until an
oversized message has a recoverable path:

- atomically write mode-0600 content under `.antiphon/messages/`;
- send a size, SHA-256 hash and local reference instead of truncating;
- validate every reference beneath the project state directory;
- define acknowledgement, retry, TTL and total-quota behavior;
- show pending storage in `antiphon status` and clean it without deleting an
  unread message silently.

This is separate from the 2,600-character pull bug. Ordinary long SQL and code
already fit under 128 KiB when sent through a channel tool.

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
