# Backlog Wave 1C: Real Codex Tool Visibility Implementation Plan

> **For agentic workers:** Use TDD, systematic debugging, exact-SHA
> verification and the existing independent reviewer. Every behavior change
> begins with an observed focused failure.

**Goal:** Make real Codex tool invocations visible as compact, atomic passive
events without exposing their arguments or changing cursor safety.

**Spec:**
`docs/superpowers/specs/2026-09-01-backlog-wave-1c-codex-tool-visibility-design.md`

**Primary files:** `lib/antiphon.py`, `test/test_antiphon.py`, README,
BACKLOG and generated rule contract tests.

## Constraints

- The E2E marker-reliability exact gate closes first.
- One writer in `.worktrees/p0-claude-identity`.
- No live cursor, catalog or registry mutation in tests or measurements.
- No arguments/results/ids in the passive summary in this wave.
- Stop at a reviewed local exact commit. No push, merge, version or publish.

### Task 1: Pin real host shapes red-before-green

Add faithful JSONL fixtures for:

- `response_item/custom_tool_call` with free-form input;
- `response_item/function_call` with JSON text in `arguments` and optional
  namespace;
- both linked output record types;
- malformed payload/name/input variants.

Assert the current parser emits zero tools for valid call fixtures, then add
the expected compact events and watch the test turn green. Assert outputs stay
filtered.

### Task 2: Preserve frontier and atomicity

Add red tests where filtered output follows a visible call, a call sits before
an oversized record, a delivery fails, and 41 tool-only records cross the
lookahead/page boundary. Pin source offsets, anchors, `has_more`, human message
count and retry identity.

Implement the two real response-item branches through the existing Event and
whole-record pipeline. Do not parse custom input or function arguments for page
text. Keep or retire `exec_command_begin` only according to a named
compatibility test.

### Task 3: Pin composition and surface truth

Add a deterministic mixed message/tool fixture showing that tool records
consume the 40-record cap without becoming human messages. Record the real
corpus measurement in BACKLOG: zero of roughly 6.4K calls visible before;
12.4% more pages and median 53-page historical displacement in the measured
fully drained simulation after.

Update README and both generated rules together. They promise compact tool
names only and keep full arguments/results explicitly unavailable.

Record both regimes instead of presenting replay as ordinary latency: the
fully drained corpus adds 12.4% pages and moves historical human records by a
median 53 pages, while 983 of 987 measured steady-state tool runs keep the next
human record on the same page and the worst run delays it by two. Add a
contract sentence explaining why those numbers do not earn another persisted
fairness lane.

### Task 4: Exact-SHA gate

1. Run focused Codex parser/page/frontier suites.
2. Run `npm test`, `git diff --check`, Python/Node and shell syntax checks.
3. Inspect and commit the complete wave; require a clean tree.
4. Run fresh-user E2E on that exact SHA and record its attempt/assertion output.
5. Send exact SHA and evidence to Claude.
6. Reactivate the existing independent Codex reviewer on that SHA.
7. Fix findings red-before-green and repeat the complete gate.
