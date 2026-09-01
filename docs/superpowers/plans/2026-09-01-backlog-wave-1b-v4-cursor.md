# Backlog Wave 1B: Anchored Cursor and Fair Backlog Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans`,
> `superpowers:test-driven-development`, `superpowers:systematic-debugging`,
> `superpowers:verification-before-completion` and
> `superpowers:requesting-code-review`. Every behavior change starts with a
> focused failing test whose failure is observed before production code.

**Goal:** Ship the approved v4 anchored cursor, bounded v3 adoption,
live/unknown/dead scheduling, unreferenced-manifest reclamation and explicit
cursor-proved candidate compaction without gaps, guessed liveness or rolling
upgrade breakage.

**Spec:**
`docs/superpowers/specs/2026-09-01-backlog-wave-1b-v4-cursor-design.md`

**Primary files:** `lib/antiphon.py`, `lib/peers.py`,
`test/test_antiphon.py`, `test/test_peers.py`, `test/e2e/fresh-user.sh`,
`README.md`, `BACKLOG.md` and generated agent-rule constants/tests.

## Global constraints

- Work only in `.worktrees/p0-claude-identity`; one writer owns the checkout.
- Do not touch the live project's `.antiphon/cursor.json`, source catalog or
  peer registry. Real-host checks are read-only; mutations use temp projects.
- Do not nest catalog and cursor locks. Transcript/process work happens outside
  both.
- Keep every parsed v3 and `_seen` sibling value deeply equal, including JSON
  types and unknown fields, unless an existing writer changes its own key. A
  canonical whole-file rewrite may change whitespace or key order; v4 never
  advances the sibling values.
- Stop after a reviewed local exact commit. No push, merge, version or publish.

### Task 1: Pin the v4 schema and rolling reader selection

**Tests first**

- Add shape tests for anchored zero/positive positions, 64-hex digests,
  `adopting_v3`, `next_lane` and opaque status rendering.
- Add precedence tests: valid v4 over v3; valid v3 over `_seen`; malformed v4
  forces recovery and never falls back; unknown sibling keys remain unchanged.
- Assert operator/compaction format selection follows that precedence: a valid
  v4 cursor remains v4 when preserved v3 and `_seen` siblings coexist.
- Add a failed-cursor-write test proving no v4 key or adoption snapshot lands.

Run those tests and record the expected failures against `PAGE_CURSOR_VERSION =
3` and the missing v4 key.

**Implementation**

- Introduce explicit v3/v4 key helpers and strict validators.
- Return a typed runtime cursor view instead of overloading the persisted dict.
- Extend page advancement to write only the v4 sibling and preserve every
  unrelated cursor field.
- Keep status output aggregate/opaque.

Run the focused cursor/malformed-state tests and commit the schema slice.

### Task 2: Add descriptor-backed raw anchors

**Tests first**

- Pin anchor creation at zero, after a normal record, after filtered records,
  before a partial tail and after a multi-megabyte record.
- Rewrite the frontier record in place while preserving inode, total size and
  first line; prove v3 skips it and the new resolver starts at its record start.
- Pin hash mismatch versus broken boundary/generation/shrink classifications.
- Assert bounded read chunks rather than one allocation of the record span.

Watch the in-place rewrite test fail by resuming silently at the old offset.

**Implementation**

- Add streaming hash and previous-record-boundary methods to `SafeSource`.
- Thread anchored boundaries through both parsers, scanned fronts and
  `_page_frontier`.
- Resolve match/mismatch/untrusted outcomes exactly as the spec states.
- Make `catch-up` construct anchored complete-prefix positions.

Run source/parser/page/catch-up suites and commit the anchor slice.

### Task 3: Adopt v3 with at most one boundary-record repeat

**Tests first**

- Cover v3 zero, valid positive boundary, invalid boundary, replaced generation,
  offset past complete prefix and a partial last record.
- Freeze unreadable/missing v3 entries in `adopting_v3`; later make one readable
  and prove it migrates from the frozen rather than subsequently advanced v3.
- Fail first delivery/cursor persistence and prove the same adoption page is
  offered again.
- Prove `anchor_upgrade` appears only for a readable adopting source and clears
  without a permanent notice from a gone entry.

Observe the old v3 resume skip the boundary record before implementation.

**Implementation**

- Build adopting runtime starts from v3 and persist the frozen map only after
  successful progress.
- Convert scanned sources to anchors and retain unresolved entries.
- Keep v3 and `_seen` siblings untouched.
- Align backlog counts and recovery notices with the actual resolver.

Run migration, paging, hook and MCP tests and commit the adoption slice.

### Task 4: Classify source activity without guessing

**Tests first**

- Pin live endpoint/session joins, proved-dead current owners, no claim,
  unnamed claim, legacy/mixed owner generation, pid reuse, process-read failure
  and conflicting claims.
- Assert only proved-dead enters the dead class and registry files remain
  byte-identical.
- Assert process observation is cached per owner key.

Watch the focused tests fail because no activity snapshot exists.

**Implementation**

- Add a read-only peer-session census with strict directory/record validation.
- Add current-generation owner liveness classification using canonical
  pid/birth evidence; failures return unknown.
- Thread one snapshot into page building and status without pruning.

Run peer/session-join tests and commit the evidence slice.

### Task 5: Alternate active and dead pages fairly

**Tests first**

- Reproduce the reported shape with old backlog from two proved-dead sources
  and a new live record; page one must contain the active record.
- Under continuous active arrivals, prove a dead page lands every second mixed
  successful page and an active record waits behind at most one dead page.
- Cover unknown sharing the active lane, oldest-first order inside each lane,
  filtered-only advancement, oversized atomic records, lane drain, failed hook
  delivery and oversized MCP refusal.
- Pin takeover semantics: when one lane is empty and the other serves, the
  empty lane keeps `next_lane`; only a successfully delivered mixed scheduled
  page toggles it.
- Assert `has_more` accounts for both lanes and lane state changes atomically
  with frontiers.

Observe global chronological ordering delay the live record before changing it.

**Implementation**

- Partition ordered records by the read-only activity snapshot.
- Select one lane per mixed page and carry the next-lane decision in
  `PageAdvance`/v4 state.
- Preserve existing budgets, atomism and retry semantics.
- Add aggregate activity/lane status without identifiers.

Run page, hook, MCP, status and delivery-lock tests and commit the scheduler.

### Task 6: Make manifest reclamation race-safe

**Tests first**

- Hold a shared reader snapshot while an exclusive cleanup waits; prove the
  referenced manifest remains readable.
- Pin cleanup after state switch, crash orphan recovery, failed unlink,
  malformed names, symlinks and files outside the manifest directory.
- Repeated unchanged explicit scans must not leave an ever-growing set of
  unreferenced valid manifests after successful cleanup.
- Assert no transcript read, cursor read or process probe occurs under the
  catalog lock.

Observe stale manifests accumulate before implementation.

**Implementation**

- Split validated catalog snapshot loading from transcript discovery and guard
  it with bounded shared flock; retain exclusive mutation flock.
- Compute referenced basenames from validated current state and delete only
  unreferenced, grammar-valid, non-link manifest files under the owned
  directory.
- Retry crash leftovers on later catalog mutations; surface aggregate pending
  cleanup without making an otherwise deliverable hook nonzero.

Run catalog/lock/discovery tests and commit the manifest-GC slice.

### Task 7: Add explicit cursor-proved candidate compaction

**Tests first**

- Refuse a readable source, recent gone source, collision/refusal, changed
  generation, relevant reader whose selected format is legacy/v3,
  malformed/unreadable/newer relevant cursor, unconsumed v4 position and a
  cursor/owner-classification race.
- Prove preserved legacy siblings do not block a selected valid v4 format.
- Classify the shared cursor as always relevant; classify a named cursor as
  dormant only with current-generation pid/birth proof that its recorded owner
  is dead. Unknown/legacy/missing owner evidence stays relevant.
- Retire one aged, gone source only when every relevant v4 reader proves it
  consumed or has no relevant entry; preserve dormant and unrelated cursors
  byte-for-byte.
- Assert aggregate blocker classes (`selected-legacy`,
  `invalid-or-unreadable`, `unconsumed-v4`, `unknown-owner`,
  `source-not-gone`, `snapshot-raced`) and the dormant-reader count without
  exposing aliases, ids or paths.
- Crash between compact manifest publication, state switch, record deletion
  and manifest cleanup; every retry must converge without hiding a candidate.
- Output tests reject paths, ids, hashes and content while checking aggregate
  files/bytes reclaimed.

Observe the command/interface missing before implementation.

**Implementation**

- Add `antiphon sources compact` as an explicit, non-hook operation.
- Snapshot cursor files and owner-classification evidence one lock at a time,
  release, then revalidate every decision input under the catalog lock:
  catalog state/manifests, deeply typed values of only the eligible candidate
  records, source absence and the cursor/owner input set. Unrelated record
  updates must not prevent convergence.
- Publish a durable prepared journal before the compact state, revalidate after
  the switch while readers still see the old state, then mark the journal
  committed before removing only its receipt-named records. A crash or failed
  rollback must preserve the safe old view; invoke safe manifest cleanup only
  after the journal no longer needs either generation.
- Refuse on any uncertainty and preserve the old complete catalog.
- Keep `snapshot-raced` as the frozen aggregate blocker while tracking a
  retryable subset that can never exceed it. Tell the operator to retry only
  when a revalidated input snapshot changed. For persistent proof failures,
  say that the evidence could not be interpreted as a transient change and no
  automatic remedy was attempted. Neither route exposes a path, identity, hash
  or content on stdout or stderr; mixed results print both independent lines.

Run command, cursor, catalog and interruption tests and commit compaction.

### Task 8: Align operator and agent contracts

**Tests first**

- Extend contract tests across README, BACKLOG, `CLAUDE_RULE`, `AGENTS_RULE`
  and channel instructions for the v4 sibling, one-record adoption repeat,
  dead-proof threshold, fairness alternation and compaction boundary.
- Add status/doctor tests for v4, adoption, activity counts and cleanup pending,
  with no private identifiers.
- Extend fresh-user E2E with an anchored v3 adoption and in-place rewrite
  fixture; assert cursor siblings and exact content, not only counts.

Observe stale v3-only wording fail the lexical contracts.

**Implementation**

- Update all surfaces together and close the corresponding BACKLOG bullets with
  exact limitations: last-record rather than prefix anchor, unknown-in-active,
  explicit compaction and preserved rolling siblings.
- Keep generated rule constants and setup output derived from one wording
  source where the existing architecture supports it.

Run contract/status/doctor/E2E shell syntax tests and commit documentation/E2E.

### Task 9: Exact-SHA verification and independent review

1. Run focused source, cursor, page, catalog, peer, status and lock suites.
2. Run `npm test`, `git diff --check`, `python3 -m py_compile lib/*.py`,
   `node --check lib/channel.mjs` and `bash -n test/e2e/fresh-user.sh`.
3. Inspect the complete diff, commit any correction, and require a clean tree.
4. Run `test/e2e/fresh-user.sh` from that exact commit; record its assertion
   count and certified SHA.
5. Send the exact SHA and evidence to Claude for read-only confirmation.
6. Ask the existing separate Codex reviewer agent for a full read-only review
   of that exact SHA. If it finds anything, return to red-before-green and
   repeat the complete exact-SHA gate.
7. Stop at the reviewed local commit and report remaining Wave 1C/backlog work.

No step pushes, merges, versions or publishes.
