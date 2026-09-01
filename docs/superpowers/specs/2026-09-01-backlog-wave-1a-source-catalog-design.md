# Backlog Wave 1A: Durable Source Truth and Safe Reads

**Status:** Proposed after the user's delegated plan approval; Claude approved
the 1A/1B split and required the cursor migration to move into its own later
review unit. This exact text still needs Claude's approval before implementation.

**Scope:** Build a durable, truthful inventory of Claude and Codex transcript
sources, make every catalog-backed transcript read descriptor-safe, and expose
when discovery is incomplete. This unit does not change cursor versions,
frontier ordering, event identifiers, tool retrieval, peer identity or routing.

## Goal

`has_more: false` must mean there is no more visible content in every source
Antiphon can currently prove belongs to the project. It must no longer silently
mean “none in the newest three files.” If that proof is incomplete, the page,
status and doctor say discovery is degraded instead of presenting a bounded
window as complete history.

The catalog is an index, never a second transcript store. Transcript bytes
remain owned by the hosts, and a path in catalog state is only a claim until
Antiphon opens and validates it through the expected host root.

## 1. Boundary and dependency order

Wave 1 is split into three exact-SHA review units:

1. **1A (this design):** durable catalog, bounded/resumable bootstrap,
   descriptor-safe source reads and the degraded-discovery contract;
2. **1B:** a v4 page cursor with a last-delivered-record content anchor, v3
   frontier adoption and three-class live/unknown/dead scheduling;
3. **1C:** stable event ids and full tool-call retrieval through the safe
   catalog.

The cut is load-bearing. Discovery changes which files are candidates. A cursor
migration changes which bytes are considered delivered. Combining those
changes would make a gap impossible to attribute in review. Event retrieval in
turn depends on the catalog but must not enlarge either earlier state machine.

The preserved `_seen` key is not deleted here. It remains explicit rollback
compatibility until a later release policy can prove old readers are gone; if
that proof remains impossible, the backlog will close it as deliberately
retained rather than deleting it on a guess.

## 2. Catalog ownership and shape

The project-local state is split so normal hooks never rewrite a document whose
size grows with the transcript corpus:

```text
.antiphon/sources/.lock
.antiphon/sources/state.json
.antiphon/sources/manifests/<generation>-<kind>.json
.antiphon/sources/records/<kind>/<digest-prefix>/<sha256-of-kind-and-relative-path>.json
```

`state.json` is a small schema-versioned document containing the absolute
project identity plus per-kind generation, manifest, pending-index and
reconciliation counters. A manifest is an immutable array of relative
candidate names captured by one enumeration. Each candidate has one small
atomic record containing:

- host kind (`claude` or `codex`);
- the existing path-independent `source_id`;
- the candidate's relative path beneath its expected host root;
- the last observation time, complete-file generation and safely measured
  complete-file size;
- enough host metadata to re-check that the source still names this project.

The record filename is the full SHA-256 of the host kind and relative path; it
is not a shortened source id and therefore does not choose a winner when two
paths claim one source id. Candidate records are retained because a session
file can move and because two different files claiming one source id is
evidence of ambiguity, not authority to choose the newest. On read, candidates
that resolve to the same device/inode are one file; two simultaneously valid
distinct files for one source are a degraded collision and neither is selected,
including through the recent-window fallback. Records are partitioned by the
leading digest bytes so listing one directory does not scale to the entire
catalog. The measured corpus creates about 563 small JSON records; this inode
and directory footprint is the accepted price of bounded normal-hook writes.

Every mutation holds `.antiphon/sources/.lock` and persists through an adjacent
temporary file plus `os.replace`. A normal hook rewrites at most its current
candidate record, the fixed batch of candidate records it inspects, and the
small `state.json`; total bytes written are therefore bounded independently of
catalog size. Manifests are immutable after publication. The lock is never held
while a transcript is scanned or while a page, cursor, socket or queue operation
runs. The operation is: read state and reserve a bounded work batch under the
catalog lock, release; inspect source descriptors; reacquire and merge only
observations whose bootstrap generation is still current.

Completeness is re-proved on every read rather than inferred from the word
`complete` in `state.json`: the nested state shape, safe manifest basename,
project/kind/generation/phase metadata, committed indices and terminal record
coverage must agree. A manifest candidate whose record is missing is still
opened through the descriptor-safe path so its content is not hidden, but the
page is degraded until a later bounded scan restores the missing proof.

The lock-order invariant is explicit: any operation that needs catalog and
cursor work acquires and releases the catalog lock before it acquires the cursor
lock; the locks are never nested. A deterministic two-hook test must fail on
inversion rather than relying on timing. A slow file therefore cannot block
another hook from recording its own active source or create a catalog/cursor
deadlock.

Catalog lock acquisition has the same bounded two-second patience as the
cursor lock. If the deadline expires, the hook skips all catalog mutation for
that turn, marks discovery degraded with a lock-contention reason and continues
the existing fallback page path. It neither blocks indefinitely nor makes an
otherwise deliverable hook nonzero. The explicit scanner returns nonzero so a
person invoking it learns that no progress was reserved.

Malformed, unreadable or newer-version catalog state is never overwritten.
Its detached candidate-record inventory is not an authority: readers ignore it,
fall back to the existing current-window discovery and return a
degraded reason; hooks still deliver the page they can build. The explicit
scanner refuses that state too and tells the person to repair or move it;
setup and `doctor --fix` remain configuration-only and do not take ownership
of this runtime state.

## 3. The hook records the current source first

Both hook arms receive `cwd`, `session_id` and `transcript_path`. Immediately
after resolving the stated project and before building a page, the hook offers
its own side's transcript to the catalog whether the peer is named or unnamed.
This source write does not depend on `ANTIPHON_NAME`, `owner_key` or a peer
registry join. Those facts decide addressing and labels; they do not decide
whether a real host transcript exists.

The offered path is validated as described below. A missing or still-partial
file is not invented into the catalog. Failure is visible on stderr and makes
discovery degraded, but it cannot make the hook nonzero or suppress an
otherwise deliverable page. The current source is attempted before bootstrap
work, so a new live reply never waits for historical enumeration to finish.
For Claude, the expected parent is derived independently from the one host
project directory selected for `cwd`; an offered or stored relative path never
supplies its own project prefix.

`record_claude_session` and `record_codex_session` keep their existing named
peer records and owner-key rules. The catalog is not read through
`session.json`, and a registry writer cannot redirect transcript reads.

## 4. Bounded, resumable complete-scan bootstrap

A forward-only catalog would be truthful only after the lookback window aged
out, while the existing reader can already have undelivered positions in older
sources. The catalog therefore bootstraps from a complete host snapshot.

The first bootstrap step enumerates candidate names into one immutable manifest
under a new bootstrap generation and records the next index in `state.json`.
Enumeration reads directory entries only; it does not open every transcript or
parse head records. Each hook then claims and inspects at most a small fixed
batch. The exact batch is chosen from a checked-in timing measurement, and a
test asserts both the inspection cap and the bounded number of bytes/files
rewritten. Bootstrap progress is persistent and resumable across processes and
restarts.

When the pending snapshot reaches zero, one reconciliation enumeration runs.
If it finds unseen names, it publishes a new immutable manifest containing
only those names and processes that generation before completeness can be
claimed. It does not append to or rewrite the completed manifest. The hook's
direct current-source write closes the ordinary new-session race;
reconciliation closes the snapshot race.

Reconciliation has a finite stop condition: exactly one reconciliation
snapshot and its one delta manifest belong to a bootstrap generation. After
that delta is processed, the generation becomes complete; names created after
the reconciliation cutoff cannot recursively extend it. A later observed root
directory change starts a new finite refresh generation, while the direct
current-source record makes the ordinary newly active transcript available
immediately. Thus continuous transcript creation can schedule later bounded
refresh work but cannot keep one generation permanently `building`.

An explicit command, `antiphon sources scan`, performs the same resumable work
outside the latency-sensitive hook and may run it to completion. It is safe to
interrupt and idempotent. It prints aggregate progress and reasons only, never
transcript paths, source ids or content. It does not read or move a cursor.

For Claude, project identity is the host-assigned project slug directory already
selected for this project. The same Claude transcript legitimately mixes
records whose `cwd` is the repository root and records from multiple worktrees;
per-record `cwd` is context, not source authority, and does not exclude that
transcript. This is measured in three real sources: one held 2,091 root records
and 2,215 records across 12 worktree paths; another 1,412 root and 586 across 12
worktrees; a third 2,219 root and 1,372 across two paths. For Codex, candidates
come from the session tree and are admitted only after their session metadata
`cwd` equals the project exactly. An unrelated Codex rollout is a successfully
excluded candidate, not a degraded error. An unreadable candidate is unproven
and keeps the bootstrap degraded until a later scan can settle it.

No hook performs an unbounded set of transcript head reads. In particular, the
563-path corpus measured while approving this design is processed over bounded
turns rather than on somebody's first prompt.

## 5. Descriptor-safe source authority

One primitive opens a catalog or discovery path and returns a source descriptor
or a classified refusal. It applies all of these checks:

1. choose the expected root from the host kind (`CLAUDE_PROJECTS` or
   `CODEX_SESSIONS`) and resolve that root once;
2. reject an absolute or relative candidate whose resolved components leave
   that root;
3. walk from the root directory descriptor with `dir_fd`, `O_DIRECTORY` and
   `O_NOFOLLOW`, then open the final component with `O_NOFOLLOW`;
4. require `fstat` to describe a regular file;
5. derive generation, complete prefix, head metadata and record bytes from that
   same descriptor, never by reopening the path;
6. require the host-specific filename/source identity to agree with the catalog
   entry; for Codex, also require the session metadata `cwd` to equal the exact
   project root, while for Claude the already-selected host slug directory is
   the project authority and record-level `cwd` is deliberately not an
   admission predicate.

All production page reads, bootstrap inspection, backlog accounting and
catch-up source measurements use that primitive. Existing path-based helpers
may remain as compatibility wrappers for isolated tests, but production must
not `stat` one object and later read another by path.

A symlink at the leaf, a symlinked escape in a parent, a FIFO/device/directory,
an outside-root path, a Codex cwd mismatch, a source-id mismatch and a file
exchanged after validation are all refused. A rename after open cannot redirect
the descriptor; the read either completes from the object already opened or
fails as that object, never from its replacement.

No path, source id or session id is printed in a page or ordinary status. Doctor
may report aggregate counts and a safe reason class; it does not reveal the
catalog's absolute paths.

## 6. Discovery and page semantics

Discovery returns a value, not a bare list:

```text
Discovery(sources, state, pending, refusals, gone)
```

`sources` is the union of safely validated catalog sources and the existing
current-window scan, deduplicated by `(kind, source_id, device, inode)`. The
current scan remains during migration so a source created before its direct
catalog write is not lost. A complete catalog removes `RECENT_FILES` as a
correctness boundary; the constant remains only as a migration fallback while
bootstrap is incomplete or state is unreadable. Source-id collisions are
checked across the whole union before selection; a recent-window candidate
cannot bypass a collision already known to the catalog.

`state` is one of `complete`, `building` or `degraded`:

- `complete`: mechanically, each relevant host kind has finished the pending
  index of its base manifest, captured its one reconciliation snapshot, finished
  the pending index of that snapshot's delta manifest, and every candidate in
  those manifests or directly recorded for the generation is either safely
  readable, successfully classified unrelated, or proven gone and irrelevant
  by the rule below;
- `building`: the bounded bootstrap has pending candidates but no structural
  failure;
- `degraded`: catalog state is unreadable/newer, enumeration failed, a candidate
  could not be proved, or a source identity collision exists.

“Gone” is not inferred from one failed open. A candidate is proven gone only
when all of its recorded paths fail with `ENOENT`; permission errors, symlink or
type refusals, identity mismatches and transient I/O are unproven. Even proven
gone remains relevant unless either (a) this reader has a trusted v3 position
for the same recorded generation at or beyond the catalog's last safely
measured complete-file size, or (b) the catalog's last complete observation
timestamp is strictly before the ordinary lookback boundary. A proven-gone but
potentially unread source keeps discovery degraded. Catalog records are never
deleted merely because a path disappeared.

Completeness is scoped to the exact project root. A repository root and each
worktree cwd are distinct Antiphon bridge projects/Claude slugs; completeness
for one makes no claim about the others or about all repository history.

The page header keeps `has_more` but makes its proof boundary explicit:

```text
has_more_scope: catalogued project sources
discovery: building — source catalog bootstrap is incomplete
```

or:

```text
discovery: degraded — some project sources could not be proved
```

The second line is omitted only for `complete`. `has_more: false` under
building/degraded therefore remains useful but cannot be mistaken for complete
project history. Wording is fixed and aggregate; file paths and source ids stay
out of context.

Status reports catalog state, aggregate source counts and pending, refusal and
gone counts. Doctor is read-only and explains the safe action: let hooks
continue, run `antiphon sources scan`, or repair/move malformed catalog state.
Neither command advances a page cursor. `doctor --fix` does not repair the
catalog, because its Wave 0 contract is project configuration only.

## 7. Compatibility and deliberately unchanged behavior

- Page cursor version remains 3; existing positions and replay reasons are
  byte-compatible.
- Normal page advancement starts from the existing position map and merges only
  the safe scanned fronts it proved in this read. It never rebuilds the map from
  the currently readable subset: every unproven, missing or refused source keeps
  its existing cursor entry byte-for-byte.
- `catch-up` follows the same preservation rule. It moves only safely opened
  sources, retains every unresolved source position byte-for-byte, and reports
  the aggregate number it could not move.
- Within the discovered source set, record ordering, page budget, event limit,
  record atomicity and frontier advancement are unchanged.
- No live/unknown/dead priority is introduced. Unknown is not dead; Wave 1B
  will keep unknown with the live lane and separately prove dead-source
  fairness.
- No event id or retrieval argument appears yet.
- No catalog operation deletes a transcript, peer record, cursor, queue,
  attachment or socket.
- Catalog source records are not automatically retired in this unit. Safe
  retirement needs cursor-aware evidence and belongs with the v4 design.

## 8. Failure handling

Catalog availability can never be a precondition for the bridge's existing
recent-window delivery. If catalog locking, parsing, writing, enumeration or
source validation fails, the reader uses only sources it independently proved,
marks discovery degraded, and leaves cursor advancement scoped to those
sources. It never advances a missing source by inference.

If a source vanishes after being catalogued, it stays known. A proven-gone
source already consumed by this reader, or safely outside the ordinary lookback,
can be classified irrelevant without degrading discovery; a gone source that
may still contain unread bytes remains degraded. If two paths claim one source
id, neither distinct object wins by mtime. If the catalog belongs to another
absolute project root, it is unreadable for this project and is not rewritten.

The explicit scanner returns nonzero while pending/refused work remains or the
catalog cannot be trusted. Hook delivery remains exit 0 when its page was
delivered, even if bootstrap work failed; the stderr line and page marker carry
the operational truth without suppressing content.

## 9. Test and review gate

Red-before-green tests must cover:

- a fourth/older source outside `RECENT_FILES` becoming discoverable and
  deliverable from the complete catalog;
- current-source recording for named and unnamed hooks before bounded
  bootstrap work;
- a fixed per-hook bootstrap batch, persistent resume and final reconciliation
  of a file created during the first snapshot;
- a reconciliation generation completing after exactly one delta even while
  later names keep appearing, with those later names entering a later finite
  refresh generation rather than recursively extending the first;
- bounded catalog write amplification independent of corpus size, immutable
  manifests, and deterministic proof that catalog and cursor locks are never
  nested or acquired in the wrong order;
- catalog-lock timeout after bounded patience producing degraded fallback and
  hook exit 0, while the explicit scanner returns nonzero without mutation;
- Claude admission through the selected host slug despite mixed root/worktree
  record cwd values, plus exact Codex session-metadata cwd filtering and
  successful exclusion of unrelated Codex rollouts;
- malformed/newer/unreadable catalog state preserved byte-for-byte with
  fallback delivery and a degraded marker;
- leaf and parent symlink escapes, outside-root paths, non-regular files,
  source/cwd mismatches, duplicate-source collisions and path replacement
  after descriptor open;
- one-descriptor generation/read behavior, including a deterministic fixture
  that swaps the pathname after open;
- page, status and doctor wording without paths or session ids;
- no cursor change from scanning, status or doctor;
- a gone source consumed at its recorded complete size, a gone source older
  than lookback, a gone source that may contain unread bytes, and unproven
  permission/symlink/type/identity failures;
- normal page advancement and `catch-up` preserving every existing unresolved
  source position byte-for-byte, with `catch-up` reporting the unresolved count;
- existing v3 replay, catch-up, page budget and source-aware label tests
  unchanged.

The exact-commit gate is focused source/catalog tests, full `npm test`,
`git diff --check`, a clean worktree, `test/e2e/fresh-user.sh` packed from that
SHA, a measured bootstrap timing/corpus report, and independent read-only review
by a separate Codex agent. There is no push, version bump or publish.
