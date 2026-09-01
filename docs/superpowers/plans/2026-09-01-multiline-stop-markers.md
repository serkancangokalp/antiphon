# Multi-line Stop Markers Implementation Plan

> **For agentic workers:** Use test-driven development, systematic debugging,
> verification before completion and independent exact-SHA review. Every
> behavior change begins with an observed focused failure.

**Goal:** Carry explicit multi-line bodies through the Stop-marker road without
guessing continuation, partially sending malformed turns or changing the
established one-line contract.

**Spec:**
`docs/superpowers/specs/2026-09-01-multiline-stop-markers-design.md`

**Primary files:** `lib/antiphon.py`, `test/test_antiphon.py`,
`test/test_contracts.py`, `README.md`, `BACKLOG.md`, and `lib/channel.mjs`.

## Constraints

- Work only in `.worktrees/p0-claude-identity`; one writer owns it.
- Do not touch live cursor, source-catalog or peer-registry files.
- Keep direct MCP tools, attachment storage and routing unchanged.
- Preserve all non-`<<` one-line messages and transport bytes exactly.
- Stop at a reviewed local exact commit. No push, merge, version or publish.

### Task 1: Pin the parser contract red-before-green

Add focused failing tests before changing the parser:

- bare and named blocks for both targets;
- exact body substring preservation, including blank lines, leading/trailing
  spaces, marker-looking lines and `\n`/`\r\n` endings;
- mixed one-line/block source order;
- fence-unaware exact close and a non-exact indented/trailing-space line;
- no nesting and inert markers inside a body;
- empty body;
- invalid token grammar and missing closer as explicit parse failures;
- a target-other-side block that remains irrelevant.

Add a table-driven compatibility corpus around the existing punctuation,
alias, empty-name and one-line cases. Assert exact tuple/string equality and
observe the new block tests fail against the current line-only parser.

Implement one left-to-right physical-line parser. Factor the established
single-line alias/delimiter decoding into a helper rather than duplicating it.
Use one structured syntax exception/result carrying only a reason and, for an
unclosed block, the safe token. Do not put body or raw marker text in it.

### Task 2: Make Stop refusal atomic

Add push-level red tests using real throwaway Claude and Codex transcript
records and the real turn readers.

- A mixed body reaches the substituted final transport byte-for-byte.
- Re-running the same turn is deduplicated using the complete body.
- Recipient grouping and per-recipient source order remain unchanged.
- One invalid or unclosed block suppresses every transport call, including a
  valid marker before it.
- The stderr line names an unclosed safe token but contains none of the body;
  invalid syntax echoes no raw marker or alias.
- No delivery fingerprint is written on refusal; only an already-existing
  matching mid-turn park is retired through the established helper.
- An empty body takes the existing empty-marker diagnostic and leaves valid
  siblings deliverable.

Catch the structured parse failure in `push` before delivery. Hoist only the
existing side/key facts needed to retire a park, print the bounded diagnostic,
and return through `_retire_park`. Do not create a second dedupe or cursor
mutation path.

### Task 3: Preserve bounds and surface truth

Add focused tests proving that the fully composed block participates in the
existing marker size cap and is never parked as an attachment. Keep direct-tool
attachment tests unchanged.

Update README, `AGENTS_RULE`, `CLAUDE_RULE` and channel instructions together.
Add contract assertions for the exact opener/closer grammar, fence-unaware and
no-nesting rules, sender-chosen absent token, all-or-nothing refusal, and the
unchanged recommendation/asymmetry.

Move the BACKLOG multi-line entry from an open request to a shipped contract,
naming the reserved-`<<` compatibility boundary and the exact verification
evidence. Do not claim delivery acknowledgement or durable pending state.

### Task 4: Exact-SHA gate

1. Run each focused parser/push/contract test after its red observation and
   again after the smallest implementation step.
2. Run `npm test`, `git diff --check`, Python/Node compilation and shell syntax
   checks.
3. Inspect and commit the complete wave; require a clean worktree.
4. Run `test/e2e/fresh-user.sh` from that exact commit and record its tested
   SHA, assertion count and marker landing attempts.
5. Send the exact SHA and evidence to Claude for read-only contract review.
6. Reactivate the existing independent Codex reviewer on the same SHA.
7. Fix every finding red-before-green and repeat the complete exact-SHA gate.

Only after both reviewers explicitly close the exact SHA may the next backlog
dependency group begin.
