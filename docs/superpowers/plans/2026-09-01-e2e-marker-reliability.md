# Fresh-User Exact-Marker Reliability Implementation Plan

> **For agentic workers:** Use test-driven development, systematic debugging,
> verification before completion and independent exact-SHA review. One writer
> owns the worktree.

**Goal:** Stabilize only the live-model marker precondition in the fresh-user
gate while preserving every product assertion and the gate's exact-commit
provenance.

**Spec:**
`docs/superpowers/specs/2026-09-01-e2e-marker-reliability-design.md`

**Files:** `test/e2e/fresh-user.sh`, new harness-only marker probe and focused
unit tests. `BACKLOG.md` records the observed flake and its closed contract.

## Constraints

- Work only in `.worktrees/p0-claude-identity`.
- Do not touch live cursor, source-catalog or peer-registry files.
- Never retry push, queue, page delivery, rollout discovery or all of T2/T3.
- Do not synthesize a host transcript.
- Stop at a reviewed local exact commit. No push, merge, version or publish.

### Task 1: Pin the exact-assistant predicate

Write focused failing tests around a harness-only JSONL probe:

- prompt-only marker is rejected;
- exact assistant text is accepted;
- preamble, suffix and fence are rejected;
- malformed/partial records are ignored;
- newest exact match is returned, content is never printed.

Observe the prompt-only case fail against the current grep-based predicate.
Implement the smallest read-only probe and run the focused tests.

### Task 2: Bound only marker generation

Add a fixed three-attempt helper to `fresh-user.sh`.

- Retry only after exit zero plus no exact assistant match.
- Fail immediately on a nonzero `claude -p` exit.
- Print the accepted attempt number.
- Carry the exact second-marker transcript into push.
- On exhaustion, set the explicit preservation flag, name both retained
  temporary roots and exit nonzero before downstream assertions can cascade.
- Remove the old prompt-satisfiable `grep -qr` check.

Use deterministic shell-level fixtures or a stubbed Claude command to watch
the first-attempt omission take exactly one retry and to prove push/page stages
are invoked once. Do not use the live model as the red test.

### Task 3: Record the boundary

Update the E2E header and BACKLOG with the measured 91/93 then 93/93 history,
the one-cause/two-assertion relationship, the bounded exact-marker retry and
the fact that the rest of T2/T3 remains single-shot.

Add contract assertions that forbid a blanket stage retry and require final
failure preservation.

### Task 4: Exact-SHA gate

1. Run focused probe/harness tests and observe them green.
2. Run `npm test`, `git diff --check`, Python/Node compilation and `bash -n`.
3. Inspect the complete diff and commit only this wave.
4. Require a clean worktree and run `test/e2e/fresh-user.sh` from that exact
   commit. Record assertion count, tested SHA and marker landing attempts.
5. Send the SHA and evidence to Claude for read-only confirmation.
6. Reactivate the existing separate Codex reviewer for that exact SHA.
7. Fix findings red-before-green and repeat the whole exact-SHA gate.
