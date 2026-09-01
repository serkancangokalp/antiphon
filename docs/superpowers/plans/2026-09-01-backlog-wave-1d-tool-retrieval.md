# Backlog Wave 1D: Stable Tool-Call Ids and Retrieval Implementation Plan

> **For agentic workers:** Use TDD, systematic debugging, verification before
> completion and the existing independent reviewer. Do not implement until the
> spec and this plan are hash-approved by Claude.

**Goal:** Add short content-bound ids to compact tool events and return one
exact invocation through symmetric, read-only MCP and CLI surfaces.

**Spec:**
`docs/superpowers/specs/2026-09-01-backlog-wave-1d-tool-retrieval-design.md`

**Primary files:** `lib/antiphon.py`, `lib/channel.mjs`, parser/page/MCP/setup
tests, README, BACKLOG and generated rules.

## Constraints

- E2E reliability and Codex tool visibility close first on their own SHAs.
- One writer owns `.worktrees/p0-claude-identity`.
- No live cursor, catalog or registry mutation in any test.
- Invocation only; never imply a result is returned.
- No persistent index or attachment spill.
- Stop at a reviewed local exact commit. No push, merge, version or publish.

### Task 1: Pin token canonicalisation red-before-green

Add focused tests for the exact 22-character grammar and deterministic digest
input:

- Claude object arguments retain nested JSON types;
- Codex custom input remains a free-form string;
- function arguments remain the exact host string rather than being re-parsed;
- path rename/copy with the same source identity keeps the token;
- same-generation, same-size earlier-prefix argument mutation changes it;
- different source or native id changes it;
- an id-less complete record uses start+ordinal deterministically;
- duplicate exact calls produce an ambiguity in retrieval.

Observe missing helpers and the silent-native-id mutation case red before
implementation. Implement one canonical invocation extractor shared by page
parsing and retrieval, strict finite JSON encoding, and the 96-bit token.

### Task 2: Carry ids through pages

Extend `Event` compatibly with an optional public id. Keep positional callers
working through a trailing default. Add ids to both real tool parsers and
render each compact tool entry with its own id.

Tests pin whole-record grouping, multi-call records, 40-record and byte budgets,
oversized atomic pages, failed delivery and no change to non-tool rendering or
human-message counts. Record the 22-character page-cost measurement in BACKLOG.

### Task 3: Implement read-only lookup

Write failures first for invalid, unavailable, ambiguous, untrusted and found.
Use throwaway catalog/current-discovery fixtures with:

- source hash/token collision injections;
- duplicate native and id-less calls;
- changed content under unchanged generation/native id;
- malformed/partial/symlink/replaced sources;
- building and degraded discovery;
- post-compaction absence.

Snapshot all project-local Antiphon metadata bytes and file names before every
outcome and assert exact equality after. Patch cursor/catalog/registry mutation
helpers to raise if reached.

Implement full-trust-set descriptor scanning with no early success return; a
second exact match must still turn the result ambiguous. Return only the
approved invocation fields as JSON.

### Task 4: Add CLI and Codex MCP surfaces

Add `antiphon retrieve <id>` and `antiphon_retrieve` to Python MCP.

- CLI prints full JSON-escaped invocation on found, nonzero with fixed
  aggregate-safe explanations otherwise.
- MCP returns the same full JSON only at or below `PAGE_BUDGET`.
- Above the budget it returns an error naming the CLI command; it writes and
  advances nothing.
- Existing `antiphon_read` schema, locking, cursor and delivery behavior remain
  unchanged. Pages with no tool events remain byte-identical; tool pages change
  only by the separately specified public-id suffix.

Pin malicious ids, control characters, 8,000/8,001 boundaries and 36-KB-like
arguments. Assert no truncation and no path/native/source leak on errors.

### Task 5: Add the Claude MCP surface and setup permission

Expose the same `antiphon_retrieve` schema from `channel.mjs`. Dispatch with
fixed `execFile` arguments to Python; never construct a shell command. Keep
`reply_to_codex` byte-identical.

Update setup's Claude permission allow-list and doctor/setup shape checks.
Test clean install, upgrade, malformed settings refusal and that the tool's
description says invocation-only, write-free and oversized-limited on both
servers.

### Task 6: Align all contracts

Update README, BACKLOG, CLAUDE_RULE, AGENTS_RULE and channel instructions
together. Contract tests require:

- invocation only, never result;
- opaque content-bound id and earlier-prefix protection;
- read-only/cursor-neutral behavior;
- coarse honest outcome classes;
- the accepted loss of a separate `changed` diagnosis and why doctor cannot
  recover it without the rejected prefix/index state;
- no persistent index/tombstone;
- the >8-K MCP limitation and CLI escape hatch;
- candidate compaction/host retention can make an id unavailable.
- a duplicate transcript inside a host discovery root degrades the source and
  makes its invocations untrusted, while backups outside those roots do not.

Close only stable invocation ids and invocation retrieval in the P0 ledger.
Leave tool-result retrieval, acknowledgement/retry and v2 retirement named.

### Task 7: Exact-SHA gate

1. Run focused token/parser/retrieval/MCP/setup/contract suites.
2. Run `npm test`, diff check and Python/Node/shell static checks.
3. Inspect the complete diff, commit and require a clean tree.
4. Run fresh-user E2E on that exact SHA and record SHA/assertions/attempts.
5. Send the SHA and evidence to Claude for read-only contract confirmation.
6. Reactivate the existing independent Codex reviewer for full review on the
   same SHA.
7. Fix every finding red-before-green and repeat the complete exact-SHA gate.
