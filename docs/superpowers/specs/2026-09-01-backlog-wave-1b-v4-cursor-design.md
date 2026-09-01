# Backlog Wave 1B: Anchored Cursor, Fair Backlog, and Catalog Reclamation

**Status:** Proposed under the user's delegated plan-approval rule. Claude must
approve this exact design and its implementation plan before production code is
changed.

**Scope:** Introduce a rolling-upgrade-safe v4 page cursor with a content anchor
at every delivered source frontier, adopt valid v3 frontiers without replaying
whole transcripts, prevent provably dead sessions from hiding live/unknown
activity for many turns, and reclaim catalog state with proofs appropriate to
each artifact. Stable event ids and full tool-call retrieval remain Wave 1C.

## Goal

The cursor must detect the concrete gap still open after v3: a transcript may
be rewritten in place while retaining its device, inode, total length and first
record, so v3 resumes at an offset whose preceding bytes are no longer the bytes
it delivered. Recovery accepts a bounded duplicate and never accepts a gap.

At the same time, old backlog from a session whose owning process is provably
gone must not keep a new live reply 15–20 prompt turns away. Unknown is not
dead, and no absence, stale-looking mtime or missing registry join is enough to
deprioritise it.

Wave 1A's monotone catalog is also no longer allowed to grow forever merely
because an explicit unchanged scan publishes another generation. Reclaiming an
unreferenced manifest and retiring a gone candidate are separate operations
with separate evidence.

## 1. Cursor key and rolling compatibility

v4 uses a new sibling key, `<side>_pages_v4`; it does not overwrite
`<side>_pages` (v3) or `<side>_seen` (v1/v2). An older still-running process may
continue to advance its own key, but it can never advance v4 past bytes the v4
reader did not deliver. Duplicate delivery during an overlap is acceptable;
cross-version skipping is not.

The value is:

```json
{
  "v": 4,
  "sources": {
    "<source-id>": {
      "gen": "<source-generation>",
      "offset": 123,
      "anchor": {"start": 17, "sha256": "<64 lowercase hex>"}
    }
  },
  "adopting_v3": {
    "<source-id>": {"gen": "<v3 generation>", "offset": 123}
  },
  "next_lane": "active"
}
```

An anchored position at byte zero has `anchor: null`; every positive offset
requires an anchor. `anchor.start` is the start of the one complete raw JSONL
record ending exactly at `offset`, and `sha256` covers that record's raw bytes,
including its terminating newline. The hash and source identifiers remain
private cursor state and are never printed by status or doctor.

`adopting_v3` is a frozen snapshot, not a live view of the v3 sibling. It is
written only as part of the first successful v4 advancement and carries v3
positions for sources that could not yet be opened and anchored. A later v3
writer cannot move that adoption frontier. Each source leaves the map only
after v4 has safely scanned it and persisted an anchored position.

The reader chooses formats in this order:

1. a structurally valid v4 key;
2. otherwise a structurally valid v3 key, prepared for bounded adoption;
3. otherwise the existing v1/v2 migration rule;
4. malformed/unreadable state enters explicit `cursor_recovery` from byte zero.

If a v4 key exists but is malformed, the reader never falls back to a sibling
that may be older and farther ahead. It recovers from byte zero. Old sibling
keys retain deeply equal parsed values in this wave, including JSON types and
unknown fields. Canonical whole-file serialization may change whitespace or
key order. Retiring the keys requires a minimum supported-reader/rollback
policy that cannot be inferred from local process state.

The **selected format** is the first valid format in the precedence list above,
not the set of every sibling key present. A cursor with a valid v4 value is a
v4 reader even when the deliberately preserved v3 and `_seen` siblings remain
beside it. Operator checks and candidate compaction use this selected format;
otherwise every cursor that has ever upgraded would be called legacy forever.

## 2. Anchor creation and validation

The descriptor-safe source primitive gains a streaming raw-record anchor
operation. It never reopens the source by path and never buffers an unbounded
record merely to hash it. For a requested positive frontier it proves:

- the frontier is no later than the complete-record prefix;
- it is immediately after a newline-terminated record;
- `anchor.start` is byte zero or immediately follows a newline;
- exactly one record occupies `[anchor.start, offset)`;
- the streaming SHA-256 of that span matches the stored digest.

Every parser frontier it may return carries the anchor for the raw record just
before that frontier, including filtered host records that produce no visible
event. `_page_frontier` selects the anchor at the same boundary as the selected
offset. A failed delivery persists neither offset, anchor nor scheduler state.
`catch-up` pins the complete-prefix end together with its anchor in one cursor
write; an empty source uses offset zero and a null anchor.

Resume outcomes are deliberately asymmetric:

- matching generation, size, boundary and anchor: resume at `offset`;
- a valid boundary span whose hash changed: resume at `anchor.start`, visibly
  repeat that one boundary record, then replace the anchor after delivery;
- generation replacement, shrink, malformed anchor, broken boundary or an
  unreadable measurement: resume at byte zero with the existing conservative
  recovery wording.

This is a last-record anchor, not a rolling prefix digest. It detects a rewrite
of the record that certifies the delivered frontier. A rewrite wholly before
that record can remain undetected; hashing the complete prefix every turn would
make ordinary read cost grow with all delivered history and is outside this
unit. The limitation is stated in README and BACKLOG rather than hidden behind
the word “anchored.”

## 3. Bounded v3 adoption

A valid v3 `gen`/`offset` remains authoritative evidence of the frontier the
published reader declared delivered, but it has no content anchor. Adoption
does not bless the current bytes silently and does not replay the source from
byte zero.

For every readable v3 source:

- offset zero becomes an anchored v4 zero position without a duplicate;
- a positive offset that is a proved record boundary starts at the preceding
  complete record's start, so at most that one raw record repeats;
- an offset that is not a record boundary, lies past the complete prefix, or
  belongs to another generation starts at byte zero.

The first successful v4 cursor write freezes every unresolved v3 entry in
`adopting_v3`. A source that is absent or refused is not dropped from the map;
when it later becomes readable it follows the same one-record-or-byte-zero
rule. The page emits an `anchor_upgrade` notice only while it is actually
delivering or advancing a readable adopting source. A forever-gone unresolved
entry therefore does not put an unrelated page into permanent replay mode.

This closes the v4 adoption blind spot at the same boundary the ongoing anchor
can protect. It cannot reconstruct content from before the v3 boundary, and it
does not claim to.

## 4. Live, unknown, and dead source evidence

One read-only registry snapshot classifies source session ids for the kind a
page consumes:

- **live:** a live endpoint and its matching current-generation session record
  join to this exact source id through the existing owner-key rule;
- **dead:** at least one current-generation session claim names this exact
  source id, no live claim names it, and every such owner's pid/birth can be
  proved no longer to identify that CLI process;
- **unknown:** everything else — no claim, unnamed session, legacy/mixed owner
  generation, live process without a valid join, unreadable process evidence,
  duplicate/conflicting claim, or registry parse failure.

Classification uses no mtime heuristic, never calls a pruning registry reader,
and writes nothing. Process observations are cached by owner key so cost scales
with claimed sessions rather than catalogued transcripts. A failed `ps` is
unknown, not dead.

Live and unknown sources form the **active lane**. Only proved-dead sources form
the **dead lane**. Within each lane, the existing oldest-first ordering by
timestamp/source/generation/offset remains unchanged.

## 5. Fair page scheduling

When both lanes have visible work, v4 alternates one whole page per lane using
`next_lane`, persisted with the frontier only after successful delivery. The
first mixed page is active. The following mixed page is dead, then active, and
so on. If the chosen lane is empty after filtered records are advanced, the
other lane may serve the turn and the cursor records all safe filtered
frontiers.

`next_lane` toggles only after a successful page from the scheduled lane when
both lanes had visible work at selection. If one lane is empty and the other
takes the turn, the preference is retained rather than spent; when mixed work
returns, the lane that was owed service still goes first. A no-text filtered
advance likewise does not manufacture a turn for either lane.

This gives two bounded properties even for an oversized atomic record:

- a new live/unknown record waits behind at most one dead-lane page;
- dead backlog receives at least every second successfully delivered mixed
  page, so continuous live traffic cannot starve it.

`has_more` is true when either lane retains a visible record. `EVENT_LIMIT`,
the UTF-8 page budget, whole-record delivery and per-source contiguous frontier
remain unchanged. A failed hook write or oversized MCP refusal changes neither
lane turn nor frontier, so the identical page is offered again.

Unknown backlog can still precede a live reply inside the active lane. That is
the cost of refusing to call missing evidence death; status reports aggregate
live/unknown/dead readable counts so the boundary is visible without exposing
session ids or paths.

## 6. Manifest reclamation

Manifest reclamation does not need cursor evidence. It needs only proof that no
active catalog state references the file.

Catalog readers take a short shared catalog lock while loading and validating
`state.json` plus its base/delta manifests into an in-memory candidate snapshot.
They release it before opening a transcript, reading a cursor, consulting the
registry or rendering a page. Catalog mutations keep the existing exclusive
lock. The existing lock-order rule therefore remains: catalog work finishes
before any cursor lock, and the locks are never nested.

After an atomic state write makes a generation current, the exclusive holder
may remove valid manifest filenames not referenced by either kind's current
state. A crash before cleanup leaves an orphan; the next catalog mutation
retries. A crash before the state switch leaves the old manifest referenced and
untouched. Unrecognised names, symlinks, unreadable files and paths outside the
manifest directory are never deleted. Cleanup failure does not suppress a
deliverable page, but status/doctor and the explicit scanner report an aggregate
cleanup-pending count.

This bounds retained manifest storage by active state plus crash leftovers,
while each generation publication remains O(retained candidates).

## 7. Candidate retirement

Candidate retirement is explicit, conservative and separate from ordinary
hooks: `antiphon sources compact`. It first completes the catalog scan, then
considers a whole source-id group only when:

- every recorded path is currently proved missing (not unreadable, replaced,
  colliding or otherwise refused);
- its last complete observation is strictly older than the lookback, so a new
  reader would not select it;
- every **relevant reader** selects a readable, structurally valid v4 format
  and either proves the matching generation consumed through the last complete
  size or has no entry for the already-aged-out source;
- no relevant reader selects legacy/v3, malformed, unreadable or newer state.

The shared cursor is always relevant because an unnamed reader has no durable
owner identity. A named cursor is relevant when its session owner is live or
cannot be proved dead with the same current-generation pid/birth rule used by
source activity. A named cursor whose recorded current-generation owner is
proved dead is **dormant**: its file is preserved byte-for-byte but does not
block retirement of a source that is already gone and older than lookback. If
that alias later returns, its preserved cursor still adopts from its old
frontier; a source that reappears is enumerated and catalogued again before it
can be read. Missing, legacy or unreadable owner evidence is unknown and stays
relevant — never silently dormant.

Compaction reports blocker classes separately and without paths or ids:
`selected-legacy`, `invalid-or-unreadable`, `unconsumed-v4`,
`unknown-owner`, `source-not-gone` and `snapshot-raced`, plus a non-blocking
count of dormant readers ignored. The remedy for a selected legacy reader is
to let that reader run v4 adoption or explicitly `catch-up` in its own terminal;
invalid/unknown evidence is never converted or deleted by compaction.

Relevant cursor files and the owner evidence deciding dormant/relevant are
snapshotted one at a time under their own locks, then released before the
catalog lock is acquired. Under the catalog lock the command re-proves that the
source is missing, the catalog generation has not changed and the set plus
classification inputs of cursor files are unchanged. It then writes a durable
prepared journal naming the old and proposed states plus hashes of the exact
candidate records. Readers continue to expose the old state after the atomic
switch until source/cursor/owner evidence is revalidated and the journal is
marked committed. Only a committed journal authorizes deletion of its matching,
still-detached regular records. A crash, failed rollback or failed unlink is
therefore retryable; an unjournaled detached record is evidence of nothing and
is retained. Hooks may roll a prepared transaction back but never finalize or
delete a retired candidate. Manifest cleanup waits until the journal no longer
needs either generation.

Because the source is already gone and older than every new reader's lookback,
a cursor created immediately after the census cannot make its bytes readable;
the command nevertheless refuses if the rechecked cursor-file set changed.
Output is aggregate only: candidates considered/retired/refused and bytes/files
reclaimed, never paths, source ids, cursor hashes or transcript content.

Ordinary `sources scan` does not retire candidates. It may reclaim unreferenced
manifests because that proof is catalog-local; it cannot infer that all readers
are done with a source.

## 8. Status, doctor, and compatibility surfaces

- Status recognises v4 without printing anchors and reports v3 adoption,
  cursor recovery, scheduler lane, aggregate activity classes and catalog
  cleanup pending.
- Doctor is read-only. Invalid v4 is bad; pending bounded adoption and backlog
  fairness are notes; failed manifest cleanup or a blocked compaction is named
  without attempting repair.
- `catch-up` writes anchors and clears `adopting_v3` only for sources it safely
  pins. It preserves unresolved parsed entries with deep type equality.
- README, BACKLOG, `CLAUDE_RULE`, `AGENTS_RULE` and channel instructions agree
  on the new key, one-record adoption repeat, dead-proof rule, alternation and
  compaction boundary. Agent rules contain no private cursor values.
- A v3 process remains usable during rollout because its sibling key is left
  untouched. The v4 reader ignores later v3 movement after freezing adoption.

## 9. Failure and safety invariants

- Transcript bytes are still read only through the descriptor-safe primitive.
- No cursor advance, anchor update or lane toggle occurs before successful
  delivery/flush.
- Catalog and cursor locks are never nested; all lock waits remain bounded.
- Missing or unreadable evidence causes replay, unknown classification or a
  refused compaction — never a skipped record or a guessed death.
- No hook performs candidate retirement or unbounded cursor-file census.
- No live project cursor, registry or catalog is touched by tests; all mutation
  fixtures use throwaway projects.
- No push, merge, version bump or publish belongs to this wave.

## 10. Verification gate

Red-before-green tests cover:

- same inode/size/first record with a rewritten frontier record;
- anchor match, mismatch, malformed span, shrink, replacement, partial tail and
  a multi-megabyte record hashed with bounded memory;
- v3 offset zero, one-record repeat, invalid boundary, unresolved frozen entry,
  overlapping old-writer movement and failed first v4 write;
- live/dead/unknown classification, mixed/legacy owner evidence and no-prune
  registry reads;
- active-first alternation, dead progress under continuous active traffic,
  at-most-one-dead-page active latency, filtered records, oversized records and
  failed-delivery retry identity;
- shared/exclusive manifest snapshot races, crash-orphan cleanup, symlink/name
  refusal and repeated unchanged scans not accumulating referenced manifests;
- compaction refusal for every unsafe relevant cursor/catalog state, dormant
  reader handling, selected-format precedence, blocker-class output and
  successful aggregate-only retirement when every proof agrees;
- status/doctor/catch-up and rolling v3/v4 sibling contracts;
- fresh-user real-CLI E2E from the exact clean commit.

The final gate is the full Python/Node suite, `git diff --check`, shell/Node
syntax checks, fresh-user E2E naming the exact SHA, and an independent read-only
review by a separate Codex agent on that same SHA.
