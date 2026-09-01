# Backlog Wave 1A: Durable Source Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` and `superpowers:test-driven-development`.
> Watch every new test fail for the intended reason before production code.

**Goal:** Replace the newest-three correctness boundary with a durable,
bounded source catalog; read transcript bytes from one validated descriptor;
and make incomplete discovery explicit without moving or losing an unresolved
v3 cursor position.

**Architecture:** Keep the packaged implementation in `lib/antiphon.py`.
Introduce a descriptor-backed source handle, a small catalog state machine
whose large membership lives in immutable manifests and per-candidate records,
and a `Discovery` result consumed by pages, status, doctor, backlog and
catch-up. Bootstrap and refresh are finite generations. The hook records its
current transcript first, reserves only a fixed catalog batch, releases the
catalog lock, scans descriptors, merges observations, and only afterwards may
take the cursor lock. Existing recent-window discovery remains a degraded
fallback.

**Tech Stack:** Python 3.9+ standard library (`fcntl`, `os.open`, `dir_fd`,
`O_NOFOLLOW`, `fstat`, JSON, `unittest`); Bash fresh-user E2E; existing Node/MCP
integration suite.

**Spec:**
`docs/superpowers/specs/2026-09-01-backlog-wave-1a-source-catalog-design.md`

## Global constraints

- Work only in `.worktrees/p0-claude-identity`; never touch the live project's
  `.antiphon/cursor.json`, source catalog or peer registry.
- Catalog and cursor locks are never nested. When one hook needs both, the
  catalog phase finishes before the cursor phase begins.
- Catalog lock patience is two seconds. Hook contention degrades discovery but
  preserves exit 0 and fallback delivery; `antiphon sources scan` returns
  nonzero when it cannot reserve work.
- Claude source authority is the already-selected host project slug. A Claude
  transcript may mix repository-root and worktree `cwd` values. Codex
  admission requires exact session-metadata `cwd` equality.
- A source is not gone unless every recorded path is `ENOENT`; other failures
  are unproven. Gone relevance is computed read-only and separately for each
  reader's own cursor.
- Cursor version stays 3. Normal advancement and catch-up begin with the
  existing position map and merge safe fronts; unresolved entries remain
  byte-for-byte equal.
- No path, source id, session id or message content appears in page/status/
  doctor catalog diagnostics.
- No push, merge, version bump or publish.

---

### Task 1: Prove lock ordering and bounded contention before catalog code

**Files:**

- Modify: `lib/antiphon.py` near `cursor_lock` and lock constants
- Modify: `test/test_antiphon.py` with `SourceCatalogLockTest`

**Interfaces:**

- Produces: `catalog_lock(cwd)` with the cursor lock's two-second deadline.
- Produces: `_catalog_phase(cwd, operation)` as the only hook/scanner mutation
  entry point; it returns a classified lock-contention result rather than
  raising or waiting forever.
- Preserves: `cursor_lock` behavior and on-disk cursor bytes.

- [ ] **Step 1: Write the deterministic red lock-order tests first**

Use instrumented context managers, not elapsed-time races. Run two complete
hook invocations against one temporary project and record every catalog/cursor
enter and exit. Assert each hook's catalog exit precedes its cursor enter and
that depth is zero at every second-lock entry. Add an intentionally inverted
helper fixture and prove the assertion rejects it. Separately hold the real
catalog `flock`, shorten the injected monotonic deadline, and assert hook mode
returns a `lock-contention` result without taking the cursor lock, while scan
mode reports failure.

- [ ] **Step 2: Watch the new lock tests fail for missing behavior**

```sh
python3 -m unittest test.test_antiphon.SourceCatalogLockTest -v
```

Expected: missing `catalog_lock`/catalog phase or a hook trace with no catalog
phase; the inversion fixture must not be the only failure.

- [ ] **Step 3: Implement the bounded lock and explicit phase boundary**

Reuse the cursor lock's polling/deadline semantics and adjacent lock-file
creation, but return a structured reason to callers. Add a test-only-neutral
phase wrapper so production call order is inspectable without sleeps. Never
hold the catalog lock while opening a transcript, writing a cursor, probing a
socket or reading a queue.

- [ ] **Step 4: Run lock and cursor regression tests**

```sh
python3 -m unittest \
  test.test_antiphon.SourceCatalogLockTest \
  test.test_antiphon.PositionCursorTest -v
```

- [ ] **Step 5: Commit the lock contract**

```sh
git add lib/antiphon.py test/test_antiphon.py
git commit -m "refactor: enforce source catalog lock order"
```

### Task 2: Read every source through one validated descriptor

**Files:**

- Modify: `lib/antiphon.py` near `source_id`, `source_generation`,
  `read_records`, `head_lines`, `_complete_prefix_end`, and host discovery
- Modify: `test/test_antiphon.py` with `SafeSourceTest`

**Interfaces:**

- Produces: immutable `SourceCandidate(kind, relative_path, expected_source)`.
- Produces: context-managed descriptor-backed `SafeSource` containing kind,
  source id, generation, device/inode, complete size and a seekable file
  descriptor that is closed on every success/refusal/exception route.
- Produces: `_open_safe_source(root, candidate, cwd) -> SafeSource | Refusal`.
- Consumed by: event parsing, generation/head reads, complete-prefix sizing,
  backlog and catch-up.

- [ ] **Step 1: Write red descriptor-authority tests**

Cover leaf symlink, symlinked parent escape, `..`/absolute escape,
FIFO/directory/non-regular leaf, source-id mismatch, Codex session cwd mismatch,
and unreadable/missing classification. Add a deterministic pathname-swap
fixture: open the valid file, replace its pathname before reading, then prove
generation, metadata and events all come from the originally opened inode.
Add a Claude fixture whose records contain the repository root plus several
worktree cwd values and prove it is admitted solely by the selected slug.
Patch `os.supports_dir_fd` to the unsupported case and require a classified
refusal, never an exception escaping the hook.

- [ ] **Step 2: Watch the safe-source tests fail against path reopenings**

```sh
python3 -m unittest test.test_antiphon.SafeSourceTest -v
```

Expected: missing safe-source interface and/or the swap fixture reads the
replacement through existing `stat` + `open(path)` helpers.

- [ ] **Step 3: Implement descriptor traversal and descriptor readers**

Resolve the expected host root once, open it as a directory, walk each relative
component with `dir_fd`, `O_DIRECTORY` and `O_NOFOLLOW`, open the final leaf
with `O_NOFOLLOW | O_NONBLOCK` so a FIFO cannot hang before `fstat`, and require
regular `fstat`. Derive first-record hash,
generation, head metadata, complete prefix and JSONL records from duplicated or
seek-reset views of that descriptor only. Do not silently fall back from a
catalog refusal to reopening its path. Guard `dir_fd`/flag support explicitly;
unsupported platforms return a classified refusal and degraded fallback rather
than raising through a hook.

- [ ] **Step 4: Route current-window production reads through the primitive**

Convert host glob results into candidates beneath the expected root, then use
the same safe opener. Preserve public/path helper wrappers only where old unit
tests call them directly. Keep Claude slug authority asymmetric with Codex
metadata-cwd authority. Until Task 4 adds the page marker, every classified
refusal is reported on stderr with aggregate reason only so this intermediate
commit cannot narrow discovery silently.

Run a read-only narrowing census against the real host roots before committing:
compare the exact candidate identity sets admitted by old discovery and the new
primitive for both hosts. Record old/new counts and each aggregate refusal
class in `BACKLOG.md`; any unexplained missing identity blocks this commit.

- [ ] **Step 5: Run source/parser regressions and commit**

```sh
python3 -m unittest \
  test.test_antiphon.SafeSourceTest \
  test.test_antiphon.OffsetReadingTest \
  test.test_antiphon.PositionCursorTest \
  test.test_antiphon.SourceAwarePullTest -v
git add lib/antiphon.py test/test_antiphon.py
git commit -m "fix: validate transcript reads on one descriptor"
```

### Task 3: Add the bounded durable catalog state machine

**Files:**

- Modify: `lib/antiphon.py` after source primitives
- Modify: `test/test_antiphon.py` with `SourceCatalogStateTest`

**Interfaces:**

- Produces: catalog schema v1 under `.antiphon/sources/`:
  `state.json`, immutable base/delta manifests, and digest-prefix-partitioned
  candidate records.
- Produces: `_read_catalog(cwd)`, `_reserve_catalog_batch(cwd, kind, limit)`,
  `_merge_catalog_batch(cwd, reservation, observations)`, and
  `_record_current_source(cwd, kind, transcript_path)`.
- Produces: finite phases `base -> reconcile -> delta -> complete`; a later
  root-directory change starts a new refresh generation rather than extending
  the completed generation.
- Stores enumerated-directory stamps for diagnostics. The flat Claude project
  directory can use `(device, inode, mtime_ns)` as a cheap change fast path.
  Codex is a recursive year/month/day tree, so its top-level root stamp is not
  authoritative: direct-current recording and the existing current-window name
  enumeration detect an unindexed recent candidate and schedule the next finite
  refresh. The explicit scanner always performs a fresh enumeration.

- [ ] **Step 1: Write red state, preservation and write-bound tests**

Assert exact absolute-project/schema validation; malformed, unreadable and
newer-version bytes remain unchanged. Assert manifests are immutable. Count
`os.replace` calls and bytes written for catalogs with 3 and 563 candidates;
one hook may rewrite only current record, fixed batch records and small state.
Assert full SHA-256 filenames and digest-prefix partitioning.

- [ ] **Step 2: Write red finite-generation tests**

Create a base snapshot, add a file before reconciliation, add another while
the delta is processed, and keep adding later names. Assert the first generation
finishes after exactly one delta; the later root change creates a distinct
refresh generation. Interrupt after each phase and prove resume is idempotent.
Assert direct current-source recording is immediately readable and independent
of `ANTIPHON_NAME`, `owner_key` and peer registry state.

- [ ] **Step 3: Watch the catalog tests fail**

```sh
python3 -m unittest test.test_antiphon.SourceCatalogStateTest -v
```

- [ ] **Step 4: Implement atomic, bounded reservations and merges**

Write adjacent temporary JSON then `os.replace`; never modify a manifest after
publication. Reserve indices/generation under the catalog lock, scan after
release, and merge only if the reservation's generation/phase is still current.
Store only relative host-root paths. Refuse malformed/newer/wrong-project state
without overwriting it. Catalog failures return reason classes rather than
raising through the hook.

Candidate records themselves are the durable discovery index, so a direct
current-source record is visible without growing `state.json`. Enumerating and
parsing those small partitioned records is measured separately from opening
transcripts; transcript inspection remains bounded during bootstrap.

- [ ] **Step 5: Add bounded bootstrap and explicit scanner**

Choose the per-hook batch from a checked-in measurement against the real local
candidate counts, record command/corpus/median in `BACKLOG.md`, and add
`sources()` plus `COMMANDS["sources"]` accepting only `scan`. The explicit scan
uses the same reserve/inspect/merge loop until complete, is interruptible, never
touches a cursor, prints aggregate counts only, and returns nonzero for pending,
refused, malformed or lock-contended work.

- [ ] **Step 6: Run catalog/CLI tests and commit**

```sh
python3 -m unittest \
  test.test_antiphon.SourceCatalogStateTest \
  test.test_antiphon.AntiphonTest \
  test.test_contracts -v
git add lib/antiphon.py test/test_antiphon.py test/test_contracts.py
git commit -m "feat: add a bounded durable source catalog"
```

### Task 4: Make discovery complete, degraded and cursor-preserving

**Files:**

- Modify: `lib/antiphon.py` near host event readers, `_build_page`,
  `build_summary`, `reader_backlog`, and `catch_up`
- Modify: `test/test_antiphon.py` with `CatalogDiscoveryTest`

**Interfaces:**

- Produces: `Discovery(sources, state, pending, refusals, gone, reason)`.
- Produces: `_discover_sources(cwd, kind, reader_side, positions, since)`.
- Consumed by: `claude_events`, `codex_events`, page rendering, backlog and
  catch-up.
- Preserves: v3 `PageAdvance` and replay values.

- [ ] **Step 1: Write red discovery/completeness tests**

Put four sources in a complete catalog and make the fourth older than
`RECENT_FILES`; prove its content is delivered and `has_more: false` has
`has_more_scope: catalogued project sources`. For building/degraded catalogs,
prove the fixed aggregate discovery marker appears even when fallback content
is delivered. Build one catalog/recent union with a duplicate source id on two
distinct inodes and prove neither path is selected.

- [ ] **Step 2: Write red gone and per-reader tests**

Cover all-paths-`ENOENT` plus: reader A positioned at recorded complete size,
reader B behind it, timestamp strictly before lookback, and equal-to-lookback
boundary. Assert A and B receive different relevance/degraded results without
any cursor write or lock. Permission, symlink, type, identity and transient I/O
remain unproven. `status` must calculate each reader from its own cursor.

- [ ] **Step 3: Write red position-preservation tests**

Seed a v3 map with readable, missing, refused and malformed-source entries.
After an ordinary page advance, assert only the readable source frontier
changes and every unresolved JSON subtree is equal. Run catch-up and assert the
same preservation, safe-source-only movement and aggregate unresolved count.

- [ ] **Step 4: Watch the focused tests fail on the newest-three boundary**

```sh
python3 -m unittest test.test_antiphon.CatalogDiscoveryTest -v
```

- [ ] **Step 5: Implement union discovery and mechanical state**

Load candidate records plus safe current-window candidates, validate through
`SafeSource`, collision-check the whole union before selection, and derive
`complete` only from finished base/delta indices and terminal candidate
classifications. Fall back to recent sources on catalog failure, always with a
degraded reason. Keep reason strings aggregate and stable.

- [ ] **Step 6: Merge, never replace, v3 positions**

Change page cursor mutation and catch-up to start from a deep copy of existing
`sources`, then update only frontiers proven from safe descriptors. Never build
the persisted map from `Discovery.sources`. Backlog/status gone relevance reads
the appropriate side's cursor without locking or mutation.

- [ ] **Step 7: Run pagination/catch-up regressions and commit**

```sh
python3 -m unittest \
  test.test_antiphon.CatalogDiscoveryTest \
  test.test_antiphon.SourceAwarePullTest \
  test.test_antiphon.PagedSummaryModelTest \
  test.test_antiphon.CatchUpTest \
  test.test_antiphon.PositionCursorTest -v
git add lib/antiphon.py test/test_antiphon.py
git commit -m "fix: discover all proved transcript sources"
```

### Task 5: Expose catalog truth and reconcile documentation

**Files:**

- Modify: `lib/antiphon.py` usage, `status`, `doctor`, page wording
- Modify: `lib/antiphon.py` embedded `CLAUDE_RULE` and `AGENTS_RULE`
- Modify: `test/test_antiphon.py` status/doctor/page tests
- Modify: `test/test_contracts.py`
- Modify: `test/e2e/fresh-user.sh`
- Modify: `README.md`
- Modify: `BACKLOG.md`

- [ ] **Step 1: Write red operator-surface tests**

Pin exact complete/building/degraded page lines; status aggregate sources,
pending/refusal/gone counts per reader; doctor read-only advice; scanner usage
and exit classes. Capture cursor/catalog bytes before status/doctor and assert
they are unchanged. Assert no fixture path, source id or session id leaks.
Update the contract fixtures that pin `CLAUDE_RULE` and `AGENTS_RULE`: agents
must drain while `has_more: true`, treat `has_more: false` as complete project
source proof only when discovery is `complete`, and treat building/degraded as
an explicitly incomplete boundary. Channel instructions remain unchanged
because they do not mention `has_more`.

- [ ] **Step 2: Add a fresh-user E2E catalog gate**

Extend the exact-commit script to create a fourth older source, drive
`antiphon sources scan`, prove its nonce reaches the passive page, prove
building/degraded wording cannot masquerade as complete, and prove scan/status/
doctor do not move the page cursor. Keep all synthetic host roots inside the
E2E temp tree.

- [ ] **Step 3: Implement wording and command documentation**

Update CLI usage and README with `antiphon sources scan`, bounded hooks, finite
refresh, catalog footprint, descriptor authority, discovery scope, safe
fallback, gone rules and unresolved catch-up reporting. Remove claims that
`RECENT_FILES = 3` is the correctness boundary. Reconcile the durable-catalog
BACKLOG entry to exact code/tests/evidence, including the real-host old/new
narrowing census; leave 1B/1C items open and `_seen` explicitly retained.

- [ ] **Step 4: Run focused operational tests and commit**

```sh
python3 -m unittest \
  test.test_antiphon.StatusTest \
  test.test_antiphon.DoctorTest \
  test.test_antiphon.PagedSummaryModelTest \
  test.test_contracts -v
git add lib/antiphon.py test/test_antiphon.py test/test_contracts.py \
  test/e2e/fresh-user.sh README.md BACKLOG.md
git commit -m "docs: expose durable discovery truth"
```

### Task 6: Exact-SHA verification and independent review

- [ ] **Step 1: Run focused source/catalog suites**

```sh
python3 -m unittest \
  test.test_antiphon.SourceCatalogLockTest \
  test.test_antiphon.SafeSourceTest \
  test.test_antiphon.SourceCatalogStateTest \
  test.test_antiphon.CatalogDiscoveryTest -v
```

- [ ] **Step 2: Run all local verification**

```sh
npm test
git diff --check
git status --short
```

- [ ] **Step 3: Commit any verification-only correction, then freeze SHA**

The worktree must be clean. Record `git rev-parse HEAD`; do not amend or change
files after this point without restarting the exact-SHA gate.

- [ ] **Step 4: Run fresh-user E2E from that exact commit**

```sh
test/e2e/fresh-user.sh
```

Require its output to name the frozen SHA and pass every assertion. Record the
measured candidate corpus, batch cap, hook timing and write count/bytes.

- [ ] **Step 5: Request independent read-only review on the frozen SHA**

The separate Codex reviewer checks spec compliance, descriptor authority,
collision handling, finite generations, lock order/contention, per-reader gone
logic, position preservation, diagnostics, tests and E2E provenance. Resolve
findings with TDD, commit, and restart Steps 2-5 on the new exact SHA.

- [ ] **Step 6: Stop at a reviewed local commit**

Report exact SHA, commits, test counts, E2E result, timing/write measurements,
review findings and remaining Wave 1B/1C backlog. Do not push, merge, version or
publish.
