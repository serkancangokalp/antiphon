# Unnamed Codex Peer Visibility Implementation Plan

> **For agentic workers:** Obtain Claude approval on the committed measurement
> contract before probing. Obtain a second approval on the measured branch
> before product code. Use TDD, exact-SHA verification and independent review.

**Goal:** Make proven unnamed-Codex ambiguity visible and safely refuse it,
without making session ids aliases or overstating a necessarily incomplete
census.

**Spec:**
`docs/superpowers/specs/2026-09-01-unnamed-peer-visibility-design.md`

**Expected product files after measurement:** `lib/antiphon.py`,
`lib/peers.py`, `test/test_antiphon.py`, `test/test_peers.py`,
`test/test_contracts.py`, `README.md`, `BACKLOG.md`, and `lib/channel.mjs` only
if its shared instructions change.

## Constraints

- Work only in `.worktrees/p0-claude-identity`; one writer owns it.
- Do not touch live Antiphon cursor, source-catalog or peer-registry files.
- Explicit `ANTIPHON_NAME` remains the only routable human alias in B1.
- Never derive identity or uniqueness from newest rollout, process recency,
  filename order, age alone or a short-id prefix.
- Preserve the legacy single-unnamed-session road unless ambiguity is proved.
- Stop at a locally reviewed exact commit. No push, merge, version or publish.

## Recorded measurement outcome

The exact pre-measurement contract was approved at `7562e3b`. The real
interactive probe then stopped at the directory-trust screen within 1.2
seconds. `No, quit` was selected; no hook, lock or probe rollout appeared and
the four guarded global files remained byte-identical. Reusing an absent path
with stale persisted trust was rejected rather than used.

The selected product branch is `U — unmeasured, blocked by trust`: assume the
pre-turn blind window exists, write observations only when hooks actually
supply ids, use exact writer locks only as positive liveness proof, always use
lower-bound wording, and refuse only ambiguity positively observed. A later
user-present probe may tighten this contract in a separate wave.

### Task 1: Commit and approve the measurement contract

1. Commit only this design and plan.
2. Record both file hashes and the exact commit SHA.
3. Send the commit, hashes, branch matrix and safety boundary to Claude.
4. Do not launch an interactive probe until Claude explicitly approves that
   exact documentation commit.

### Task 2: Attempt the real interactive pre-turn window — complete, blocked

After approval, inspect only the installed CLI's version/help needed to choose
non-mutating launch flags. Create a fresh temporary Git project. Add a
project-local capture hook that records only the bounded fields named in the
design; do not run setup in the live Antiphon project.

Take baseline, ready-before-turn, after-one-inert-turn, after-exit and
process-gone snapshots. At every boundary collect:

- project-attributable Codex process presence/cwd;
- thread-writer lock filename and held/acquirable state;
- project-attributable rollout existence plus bounded `session_meta` fields;
- captured SessionStart event/id/cwd presence.

Snapshot global Codex configuration/state bytes before and after. Abort before
accepting trust or mutating global state. Preserve an unexpected-failure temp
root for diagnosis; otherwise remove only the exact temp project created for
the probe after recording the evidence. Do not delete host rollout history.

The trust boundary prevented the ready state. The design records this as U,
not as an invented A/B/C/D result, and excludes each measured branch by the
evidence that was unavailable. Commit this measurement-only change, record
hashes, and obtain Claude approval again.

### Task 3: Pin observation semantics red-before-green

Only after the second approval, add focused failing tests for selected branch
U. They must prove:

- an unnamed hook event with a canonical UUID creates one versioned,
  transcript-free observation atomically;
- replaying the hook is idempotent and refreshes only that session's record;
- malformed/missing ids create nothing;
- a held exact writer lock makes the observation live; released, missing or
  unavailable lock evidence is unknown and cannot make a live claim;
- explicit named endpoint/session ownership suppresses the corresponding
  unnamed presentation without deleting evidence owned by another writer;
- malformed or unreadable observation files cannot make a live claim and do
  not crash diagnostics.

Implement the smallest observation writer/reader and one pure liveness
classifier. Keep observation ids outside `valid_name`, `valid_key` and the
peer endpoint/session join. Do not store transcript paths or content.

Do not claim that `SessionStart` ran before the first turn. Do not improvise a
new identity source without a fresh spec approval.

### Task 4: Refuse only proved ambiguity

Add routing failures before implementation:

- two live unnamed observations and no named peers refuse before
  `_legacy_target` is called;
- one named plus one live unnamed observation refuses a bare send;
- an exact named send remains deliverable and ignores observations;
- a canonical session UUID supplied as `alias` remains invalid;
- one unnamed observation and no named peer preserves legacy delivery/refusal;
- stale, malformed and absent observations preserve current behavior;
- every refusal keeps the existing `no-peer` honest-fallback class and contains
  counts/guidance but no session id, path or content.

Thread one immutable observation snapshot into target resolution. Render
`at least N` wherever the measurement cannot prove completeness. Do not choose
an observation by order or make it a transport address.

### Task 5: Make local diagnostics tell the same truth

Start with failing status/doctor tests for:

- two live unnamed Codex observations shown in deterministic full-id order as
  `unnamed` and `not addressable`;
- lower-bound wording and the possibility of additional pre-turn sessions;
- the restart-with-distinct-`ANTIPHON_NAME` remedy;
- no transcript path, socket address or content leakage;
- no ids from sibling projects;
- no claim of zero when the census is unknown;
- one registry/observation snapshot per command, preventing split views.

Narrow the old blanket session-id secrecy test to one rule and one carve-out: a
session id may appear only in its labelled unnamed-observation diagnostic row;
its appearance anywhere else — any other status or doctor line, any refusal,
or any error path — is a failure. Update README, `AGENTS_RULE`, `CLAUDE_RULE`, channel
instructions and BACKLOG together, with contract tests binding their shared
rules. Mark B1 shipped but leave B2/B3 open and record why the order is a
dependency.

### Task 6: Exact-SHA gate

1. Run every focused test after observing its red failure and after the
   smallest implementation step.
2. Run `npm test`, `git diff --check`, Python/Node compilation and shell syntax
   checks.
3. Inspect and commit the complete wave; require a clean worktree.
4. Run `test/e2e/fresh-user.sh` from that exact commit and record its tested
   SHA, assertion count and marker landing attempts.
5. Send the exact SHA and evidence to Claude for read-only contract review.
6. Reactivate the existing independent Codex reviewer on the same SHA.
7. Fix every finding red-before-green and repeat the complete exact-SHA gate.

Only after both reviewers explicitly close the exact SHA may B2 begin.
