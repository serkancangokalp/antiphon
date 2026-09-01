# Codex Tool-Shape Diagnostic Implementation Plan

> **For agentic workers:** Use TDD and keep this a diagnostic-only commit.
> Observe the focused failure before changing production code.

**Goal:** Make rejected tool-like Codex records visible as one aggregate doctor
finding without weakening the fail-closed parser.

**Spec:**
`docs/superpowers/specs/2026-09-01-codex-tool-shape-diagnostic-design.md`

**Primary files:** `lib/antiphon.py`, `test/test_antiphon.py`, `BACKLOG.md`.

## Constraints

- One writer in `.worktrees/p0-claude-identity`.
- Reuse `_codex_tool_fields`; do not create a second accepted-shape parser.
- Aggregate only. No payload/type/name/path/source output.
- Read-only and catalog/cursor neutral.
- Stop at a reviewed local exact commit. No push, merge, version or publish.

### Task 1: Pin the silent regression red-before-green

In one focused doctor test, write a complete trusted Codex fixture containing:

- one supported call;
- its output record;
- one `function_call` whose `arguments` changed from string to object;
- one future `*_call` kind;
- unrelated response items.

Assert exactly two rejected tool-like records, one `codex tool shapes:` line,
no payload words or identities in that line, a broken doctor result, and no
project metadata mutation. Observe the current absence of the line fail.

Add the same test's safe variants: known calls/outputs produce the green zero;
building/degraded discovery and an injected strict-reader fault produce
`amount unknown`, never zero.

### Task 2: Implement one shared counter and one doctor line

Add a small predicate around `_codex_tool_fields` for call-like payloads. Scan
the Claude reader's durable Codex discovery through the strict complete-prefix
reader. Return an integer only after every admitted source finishes; otherwise
return unknown. Do not retain invocations or print rejected fields.

Call the counter once from read-only doctor and render the three-state contract
from the design. Leave page parsing and retrieval unchanged.

### Task 3: Record and verify

Move the Wave 1C observation from an informal review note into BACKLOG as a
completed candidate diagnostic, including the privacy/fail-closed reason.

Run the focused doctor/counter tests, full `npm test`, static and diff checks.
Commit a clean candidate, run `test/e2e/fresh-user.sh` on that exact SHA because
doctor output changes, then obtain Claude and independent-agent review on the
same SHA. Fix findings red-before-green and repeat the exact gate. Do not touch
live cursor, catalog or peer-registry state.
