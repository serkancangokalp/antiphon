# Backlog Wave 1D: Stable Tool-Call Ids and Retrieval Design

**Date:** 2026-09-01

**Scope:** Give every visible tool invocation a short, content-bound public id
and a symmetric, read-only route that returns the complete invocation. Tool
results remain separate and are not returned.

## 1. Measured input contract

The reviewed corpus contained about 8,400 calls:

- Claude: 2,012 `tool_use` blocks; every input was a JSON object and every
  call had a unique native id.
- Codex: about 6,400 `custom_tool_call`/`function_call` records; every call had
  a unique native id. The dominant custom input is free-form text and is not
  JSON, while function arguments are JSON-object strings.
- Every completed call had one separately recorded linked result. One call was
  always in flight while a live census tool was executing.

Retrieval therefore returns the invocation only: side, call type, tool name,
optional namespace/caller metadata and the complete argument value. A result
is a later mutable fact. Binding it to the call's id would make the value behind
an already published id change. Every tool description and agent rule says
"invocation only" plainly.

## 2. Public id

The public form is `tc1.<kind>.<digest>`, where kind distinguishes Claude and
Codex and digest is the first 96 bits of SHA-256, base64url without padding.
The digest input is deterministic canonical JSON over:

- token version and source kind;
- source identity, not its path or inode generation;
- native call id, or the complete-record start plus block ordinal when the
  native id is absent;
- exactly the invocation fields retrieval returns, preserving JSON types and
  free-form strings.

The resulting id is 22 ASCII characters. It exposes no source id, native id,
path, offset or argument.

Measured justification:

- zero collisions across the current corpus;
- birthday upper bound about `6.31e-18` at one million events;
- existing Claude-tool pages grow 5.2% in bytes and 3.6% in page count;
- on a tool-bearing Claude page the id consumes median/p95 4.38%/9.06% of the
  8,000-byte budget;
- after the separately gated Codex parser correction, ids add 2.7% bytes and
  0.4% pages; a tool-bearing page spends median/p95 10.62%/11.88% on ids.

The rejected 94-character three-hash token spent median/p95 13.34%/29.1% of
existing Claude tool pages and added 17.4% pages. Full-width component hashes
did not buy a product guarantee proportional to that cost.

An earlier-prefix same-size rewrite was reproduced with generation, native id
and the later v4 frontier anchor unchanged. Because the invocation participates
in this token, the changed call gets a different id. Looking up the old id
returns unavailable; it can never return changed content under the old id.

## 3. Indexless, write-free lookup

No persistent id index is added. Retrieval obtains the same validated catalog
plus bounded current-discovery set used for project sources, descriptor-opens
every trusted candidate and scans complete records to EOF. It computes tokens
and succeeds only when exactly one invocation matches.

Scanning the complete measured trust set cost about 106 ms for Claude (27.8 MB)
and 579 ms for Codex (243 MB). That explicit-call latency is accepted in
exchange for short page ids and no new persistent proof surface. It never runs
inside ordinary paging.

Outcomes are deliberately coarse and honest:

- `invalid-id`: grammar/version/kind/digest is invalid;
- `unavailable`: zero exact matches, including changed content, incomplete
  discovery, host retention, catalog compaction and an id that never existed;
- `ambiguous`: more than one exact match, including a duplicate id-less
  fallback or digest collision;
- `untrusted`: source discovery/opening is degraded or unsafe;
- `found`: exactly one validated invocation.

Without a tombstone or a longer self-describing id, "changed", "expired" and
"never existed" cannot be distinguished. The product does not invent that
distinction.

Losing the separate `changed` verdict is an explicit diagnostic trade, not an
accidental side effect of shortening the id. The primary invariant is kept:
an old id never returns new invocation content. Teaching doctor to distinguish
the rewrite would require storing earlier delivered-event hashes or hashing
the whole delivered prefix, because the last-record v4 anchor contains no
evidence for an older record. That is the persistent index/prefix-cost surface
this design rejects. Doctor therefore does not claim knowledge it cannot have;
the old id returns the one honest `unavailable` outcome.

Retrieval performs no cursor read-modify-write, no source recording, no
catalog or peer mutation, no cleanup and no attachment parking. It does not
repair discovery. Tests compare the whole project-local Antiphon metadata tree
byte-for-byte and forbid mutation helpers.

## 4. Surfaces and output

- Both MCP servers expose `antiphon_retrieve` with one required string `id`.
- The CLI exposes `antiphon retrieve <id>`.
- Each compact tool line includes its opaque id. No-argument paging and its MCP
  schema remain byte-for-byte unchanged because retrieval is a separate tool.
- A found invocation is JSON with the public id, source kind, call type, tool
  name, optional namespace/caller and `arguments`. Claude objects keep their
  JSON types. Codex free-form input remains a string and is never guessed into
  JSON. JSON escaping neutralises terminal control bytes without truncation.
- Native ids, source ids, paths, offsets, generations and internal host
  metadata are not returned.

## 5. Oversized limitation

An MCP retrieval whose rendered JSON exceeds `PAGE_BUDGET` is refused without
writing anything and names `antiphon retrieve <id>`. The CLI writes the full
JSON-escaped invocation to stdout.

This is a real limitation: 34 measured Codex calls already exceed 8,000 bytes
and the largest argument was 36,963 bytes. The bridge does not promise that an
agent can access those invocations; a shell-capable agent may run the CLI, but
the MCP route itself cannot. README and both agent rules name this explicitly.
Nothing is truncated and no unverified MCP spill behavior is assumed.

## 6. Missing and duplicate native ids

Real calls all carried native ids, but the compatibility path is still
specified. An id-less call uses source identity, raw complete-record start and
block ordinal in the token input. Faithful fixtures prove it is stable for
unchanged append-only bytes. Two exact matches are ambiguous and return no
content. The fixtures are deliberately constructed because the real corpus
provides no missing-id evidence.

## 7. Paging, compaction and retention

- Tool ids are presentation metadata derived during parsing; they are not
  stored in cursors or the source catalog.
- Retrieval never advances page progress, whether found, oversized, invalid,
  unavailable or ambiguous.
- Candidate compaction can make an old id unavailable. No tombstone is kept.
- A valid catalog in a degraded state fails untrusted rather than falling
  through to a guessed path. Building discovery may search its bounded current
  fallback and otherwise says unavailable/incomplete.
- The last-record anchor's earlier-prefix limitation remains, but content-bound
  ids prevent that limitation from serving silently changed invocation bytes.

Duplicate sources inherit the catalog's existing fail-closed collision rule.
A throwaway reproduction copied one Claude transcript beside itself under a
second `*.jsonl` backup name and copied one Codex rollout inside the host
session store. Each ordinary discovery enumerated both copies, selected zero,
counted two refusals and became degraded. The current measured corpus had zero
duplicate source groups across 3 Claude and 22 Codex sources, so the cliff is
reachable but not presently active. A backup outside the host discovery roots
is irrelevant; a copy left inside either host store can make every invocation
from that source untrusted until the duplicate is moved away. README names
this existing source-identity limitation beside retrieval rather than calling
it a token collision.

## 8. Verification gate

Tests cover both real host shapes, canonical type preservation, free-form Codex
arguments, path/copy stability, earlier-prefix mutation, missing native ids,
duplicates, digest collisions, malformed/partial records, unsafe discovery,
all result classes, metadata-tree byte equality, forbidden mutation seams,
page budgets and oversized refusal/CLI success.

Contracts span README, BACKLOG, both generated agent rules, both MCP tool
descriptions and setup permissions. The final gate is full Python/Node tests,
static checks, fresh-user E2E from the exact clean SHA, Claude confirmation and
independent Codex review.

No acknowledgement, result retrieval, persistent index, push, merge, version
or publish belongs to this wave.
