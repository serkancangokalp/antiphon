# Backlog Wave 0: Operational Truth Design

**Status:** Proposed after delegated plan approval from the user to Claude;
Claude approved the dependency-wave approach and this wave subject to the
constraints recorded below.

**Scope:** This is the first independently reviewable wave of the remaining
BACKLOG campaign. It repairs operational statements and diagnostic behavior;
it does not introduce delivery state, peer identity, same-vendor routing, or
managed workers.

## Goal

Make the commands and maintenance records a truthful description of the copy
that is actually running: `status` must call a channel live only when an
Antiphon listener answers, setup and doctor must consume the same configuration
shape, an explicit repair command must use setup without pretending to repair
runtime faults, and the BACKLOG must distinguish shipped 0.3.3 behavior from
the fixes present only on the candidate branch.

“Clean” does not mean deleting inconvenient backlog entries. An item leaves the
open ledger only when it is either delivered with evidence or explicitly
declined with the condition that would make it safe to revisit.

## Campaign choice

Three approaches were considered:

1. A single big-bang change for every open item. Rejected because paging,
   pending delivery, identity, routing and managed workers are independent
   state machines that cannot receive an honest exact-commit review together.
2. Dependency-ordered waves, each with its own design, tests, commits, real-host
   gate where relevant and independent exact-SHA review. **Chosen.**
3. Finish only P0/P1 and close every P2 item as declined. Rejected because it
   would relabel unfinished requested work instead of cleaning it.

The remaining campaign order is:

1. **Wave 0:** operational truth;
2. **Wave 1:** lossless pull and source integrity;
3. **Wave 2:** turn and marker fidelity;
4. **Wave 3:** one pending-delivery ledger for acknowledgement, retry and reply
   correlation;
5. **Wave 4:** peer visibility/identity, then same-vendor routing and loop
   bounds;
6. **Wave 5:** managed foreign workers;
7. **Wave 6:** release compatibility measurements and exact-commit gates.

Waves 3 and 5 require their own design approvals. In particular, making
an unnamed peer addressable changes a user-facing default and remains the
user's decision; visibility can be designed separately. No wave authorizes
push, `npm version`, or publish.

## 1. Shared configuration shape

`lib/antiphon.py` already shares commands, paths, hook rows, MCP table
assignments and server entries between setup and doctor. The remaining drift
risk is the container vocabulary around those facts: `hooks`, `permissions`,
`allow`, `mcpServers`, `enabledMcpjsonServers`, and the hook entry keys are
still independently spelled in mutation and inspection code.

Wave 0 will define those envelope keys once alongside the existing setup
constants and make both `_add_hook`/`setup` and `hook_installed`/doctor consume
them. It will not move setup into a new module or replace the existing
preserve-unknown-fields behavior. Hand-edited configuration remains supported:
every reader must type-check each level and a malformed file must still be
refused rather than overwritten.

Tests will deliberately change or omit each shared envelope field through a
fixture and prove that setup writes exactly what doctor expects. The existing
full setup/doctor agreement test remains the end-to-end contract.

## 2. Status channel truth

The public status line remains:

```text
Claude channel:     live|down
```

Only its evidence changes. `live` means at least one relevant socket answered
the existing content-free Antiphon probe with a JSON object containing an `ok`
key. A registry record or socket pathname alone is not sufficient.

Status and doctor will share target selection and the patience rule:

- a live registered Claude endpoint is probed at its recorded address with the
  bounded startup retry, because registration intentionally precedes bind;
- a configured valid Claude alias absent from the live registry is probed once
  at its deterministic named path, without startup retry;
- with no registered Claude peer and no configured alias, the legacy project
  path is probed once, without startup retry;
- addresses and session identifiers never enter status output;
- `status` remains an informational exit-0 command and does not diagnose why a
  target is down; `doctor` retains the detailed verdict.

This preserves the load-bearing “patience only after registration” split. A
normal project with no Claude session must not spend roughly 1.5 seconds proving
that an absent socket is absent.

Tests cover a stale socket file, a non-Antiphon binder, an answering registered
peer, an answering unregistered named listener, and the zero-retry idle case.

## 3. `doctor --fix`

The default `antiphon doctor` remains byte-for-byte read-only by construction
and by the existing filesystem snapshot tests.

`antiphon doctor --fix` is an explicit configuration repair workflow:

1. print that the mode writes project configuration only and cannot repair or
   restart live processes, sockets, peer records, cursors, queues, transcripts
   or attachments;
2. invoke the existing idempotent `setup()` implementation without suppressing
   or rewriting any of its stdout/stderr lines;
3. print a re-check boundary;
4. run a fresh ordinary read-only doctor pass;
5. return non-zero if setup refused any file or the re-check still contains a
   broken verdict.

No second repair implementation is introduced. Setup remains the sole owner of
configuration mutation. An unreadable/malformed file is left untouched and its
setup refusal remains visible even if every other file was repaired. Runtime
faults such as stale sockets and old processes remain doctor findings with
manual restart guidance.

The help text will state: `doctor` is read-only; `doctor --fix` writes only the
same project configuration as `setup`, then re-checks. Any other doctor argument
is an exit-2 usage error.

Unit tests cover clean, repairable, partially refused and runtime-only-broken
projects. The fresh-user E2E will remove one generated configuration fact, run
`doctor --fix`, and prove both the setup repair and the clean re-check without
touching global Codex configuration.

## 4. Codex operational boundary

Wave 0 does not remove existing Codex liveness evidence. Antiphon already reads
Codex's queue database in read-only mode and uses the thread-writer lock to
distinguish a stopped thread without spawning a process; both remain.

What is explicitly declined is an active “can this peer receive now?” probe
implemented by executing `codex`. Such a command may block on authentication or
start a session and therefore violates the diagnostic's non-invasive contract.
Per-peer active reachability remains declined until Codex exposes a bounded,
non-spawning, version-detectable API. The BACKLOG will name that revisit
condition rather than leave a vague future bullet.

## 5. Backlog truth and candidate-versus-release wording

The ledger will correct four stale statements:

- Claude session records and source labels are implemented and exercised by
  production callers;
- Codex forwarding of `ANTIPHON_NAME` has been measured; setup and tests pin the
  forwarding declaration;
- doctor already reports a dead-pid endpoint without pruning it;
- ordinary `channel.mjs` accumulation through stdin close and wrapper-forwarded
  signals is fixed, while abrupt host death/SIGKILL still relies on stale-state
  diagnosis and recovery.

The process-fingerprint defect must be named without rewriting release history.
Published 0.3.3 still rendered `ps lstart` under the caller's timezone/locale.
The candidate branch beginning at `a4533d1` canonicalizes observations under
`LC_ALL=C` and `TZ=UTC`, versions endpoint births and owner keys, and treats
unversioned records as unverifiable rather than dead. Wave 0 will preserve that
distinction until a later release actually contains the candidate.

The doctor future section will then contain decisions rather than contradictions:

- configuration repair is delivered as the explicit `--fix` mode;
- thread-lock and read-only queue evidence are the supported Codex diagnostics;
- active Codex reachability is declined until a safe host API exists;
- `ANTIPHON_NAME` forwarding is measured and closed.

## 6. Repeatable wrapper census

The host-wrapper census is a recurring release obligation, not a feature that
can be permanently marked fixed. Wave 0 will make it repeatable with a
repository test utility that:

- reads Claude and Codex JSONL roots without modifying them;
- counts only `role: user` text records beginning with an opening tag;
- groups by side, opening tag and `promptSource` presence/value;
- prints aggregate corpus counts and tag/source groups, never per-record text
  or per-transcript paths;
- performs no automatic classification or constant rewrite.

Fixture tests will prove parsing, grouping, malformed-line tolerance and that
no content is printed. The utility is not part of `npm test` against the live
home directory and is not included in the npm package. Before the final
candidate review it will be run read-only against the currently available host
corpora; any new tag requires human classification under the existing
asymmetric rule before constants change.

Interactive Claude notification-schema validation remains a named manual
release gate because `claude -p` does not load the channel. The fresh-user E2E
still cannot reproduce the live-Codex-before-first-rollout window; neither
limitation will be disguised as automated coverage.

## Error handling and safety

- No command in this wave deletes a socket, peer record, cursor, queue row,
  transcript or attachment.
- Default doctor remains a reader and status remains exit 0.
- Probe failures are values, not exceptions; status collapses them to `down`
  and doctor explains them.
- `doctor --fix` never catches setup output merely to make the command look
  successful.
- All tests use throwaway projects and socket paths. The live project cursor
  and peer registry are out of scope.
- No release operation is authorized.

## Verification and review

Implementation follows red-before-green TDD with focused commits. The final
wave gate is:

1. focused setup/doctor/status and census tests;
2. full `npm test`;
3. `git diff --check` and a clean worktree;
4. `test/e2e/fresh-user.sh` packed from the exact candidate commit;
5. read-only wrapper census on current host data;
6. an independent Codex reviewer on that exact SHA;
7. stop without push, version bump or publish.
