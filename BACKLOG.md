# Antiphon product backlog

Last reviewed: 2026-08-30

Priorities here describe product risk, not release promises. The bridge keeps
two invariants across every item: it preserves who said something, and it
refuses ambiguity rather than guessing or broadcasting.

## P0 — Lossless, paged context transfer

This item is now a phase ledger rather than one open problem: the delivery and
paging mechanics shipped, and what remains open is named below instead of
hiding under a general "lossless" claim.

### Shipped before the paging plan

Provenance-safe parsers (a user message beginning with `<` is no longer
mistaken for bridge metadata), the byte-offset reader and per-source cursor
with generation fingerprints, the delivery lock beside each peer cursor, and
the write-and-flush-before-advance transaction.

### Completed by the paging plan

- Oldest-first atomic pages of completed source records: an ordinary full page
  targets 8,000 UTF-8 bytes and at most 40 records, and a record is never
  split across pages.
- The non-tool 420-character cut and the 2,600-character summary trim are
  gone; whitespace, indentation and line structure are preserved exactly.
- `has_more` is visible on every page, explicitly scoped to the currently
  discovered sources.
- An oversized record is handed whole to the automatic hooks, whose hosts
  were measured (2026-08-30, Claude Code 2.1.251 and Codex CLI 0.151.0) to
  spill above 10,000 characters and expose a path; both 400,251-character
  probes matched their spill files by SHA-256. Codex's MCP tool result did
  **not** meet that assumption — the transport kept the bytes but the model
  could identify neither content nor a saved path — so `antiphon_read`
  refuses an oversized record without advancing, and the next automatic hook
  delivers it. That is a measured host behaviour, not an inference about its
  internal truncation.
- A rolling-upgrade-safe v3 page key: the legacy v2 value is preserved
  byte-for-byte for still-running old processes and is never trusted as a
  delivered frontier. Any present legacy key, and any malformed or unreadable
  existing cursor, conservatively replays the currently discovered sources
  from byte zero — measured at 69 Claude-source and 53 Codex-source pages on
  the reviewed snapshots — with a fixed replay reason visible on every page
  until the final persisted one clears it.

### Still open, by name

- Stable event ids and full tool-call retrieval: tool calls remain compressed
  one-line summaries with no `antiphon_read(id)` route.
- The durable source catalog and the degraded-discovery marker: discovery
  still reads the newest 3 transcripts per side, and `has_more: false` cannot
  distinguish complete discovery from that window.
- Backward paging into history an older version already marked seen —
  **settled for the published path, ruled for the rest.** Its cost was
  measured on 2026-08-31: the byte-zero `legacy_upgrade` replay of two days
  of transcripts (16 MB one way, 44 MB the other) was still draining twenty
  hours after the upgrade, one page per turn, and every live message waited
  behind it. Three things shipped. (1) A numeric 0.1.0 `_seen` — the only
  legacy shape npm ever carried (0.1.0 → 0.3.x; the v2 map lived on dev
  machines only) — is taken as authoritative: the page starts at the first
  record at or after that time, `>=`, so the cohort sharing that second
  repeats per source (measured: up to 10 records share one second), and what
  0.1.0's own 2,600-character trim cut before then stays cut — a conscious
  contract decision, because 0.1.0 had already declared it delivered and
  resurrecting it cost the maintainer twenty deaf hours. (2) A v2 map still
  replays from byte zero: it records how far the old scanner *read*, not what
  it delivered — the old reader scanned the whole suffix and rendered the
  newest `EVENT_LIMIT` — so no offset in it is a safe start; the four
  rolling-upgrade tests that pin this stay. (3) The replay notices name
  `antiphon catch-up`; `status` prints `unread <reader>: N raw bytes …` per
  reader — raw bytes, never pages, since a page is a rendered envelope and
  most raw bytes are filtered before one — and `doctor` notes a replaying
  reader as `·`. The count runs through the reader's own start resolver
  (`_resolve_start`), so a numeric v1 time, an offset past EOF and a replaced
  generation are counted from where the reader will actually start (review of
  6089336 caught the re-derived rule saying 0 while the reader would read 126);
  only an unreadable cursor file is `unknown`. Malformed v3 keeps `cursor_recovery` from byte zero and
  never falls to the v2 sibling; generation mismatch and offset-past-EOF keep
  byte zero.
- The last-record content anchor (an in-place rewrite that keeps inode,
  length and first line still resumes silently).
- Descriptor-safe reading of registry-supplied transcript paths.
- Retirement of the preserved v2 sibling key once pre-v3 processes and
  rollback support are no longer needed.

## P1 — Source-aware multi-peer pull context (fixed)

Live push is explicitly addressed and never broadcast. Passive pull context is
project-wide awareness, which is useful, but it merged transcripts under
generic `Claude`/`Codex` labels. With several terminals that can look like one
agent said another agent's words.

The entry asked for the source alias on every event. What shipped is narrower
and, measured, more honest: a *source* is a **session**, not a peer. `Event.source`
is `source_id(path)` — the session UUID in the transcript's own filename, which
both hosts write and which survives a move or a rename — and `RECENT_FILES = 3`
means the sources on an ordinary page are usually one terminal's *consecutive*
sessions. Measured on this machine over a real drain: 5 of 60 Claude-read pages
and 1 of 60 Codex-read pages carry more than one source, and every one of those
is one person's Codex session restarted, not two terminals. Labelling all of
them, and advising their reader to "name each terminal", would have been advice
that is wrong wherever it fires most.

### What shipped — four decisions

**1. The Claude hook records its own session, mirroring the Codex one.** The
hook has always been handed `session_id` and always threw it away;
`record_claude_session` now writes the same `session.json` half through the same
`peers.write_session`, gated on `explicit_name()` and `owner_key()` exactly as
the Codex arm is. The alternative — a `session` field on `endpoint.json`, filled
from `CLAUDE_CODE_SESSION_ID` when the channel server registers — was measured
and dropped: `claimPeer()` runs exactly once (the live server on this machine
had been up 20h59m on one claim), so a resume or a fork would leave the record
naming the old session and put the live alias on a dead transcript; and an
env-settable identity is the one thing `owner_key` refuses, because a key anyone
can set lets one session claim another's. The hook route needs no `channel.mjs`
change, no endpoint-schema change, and refreshes every turn.

**2. A label needs both halves — two or more sessions sharing the page, and a
live claim on that source — joined the way the registry already joins.** One
source is not a "which of these": there is no ambiguity for a label to prevent,
and since naming terminals is the recommended practice, a claim-only rule would
put a permanent suffix on every page of every named session, the ordinary
single-pair install included. `peers._session_address(cwd, endpoint)` is the
join: liveness on the **endpoint**, session identity from the hook's half, and
the owner key between them. A missing record, one with no owner, one from a
different owner and one whose id is not a canonical UUID all read the same way
— no claim. Liveness on the session record itself would have joined nothing at
all: `write_session` deliberately writes no pid. A session two live endpoints
claim under different aliases is dropped rather than decided;
`sorted(os.listdir(...))` order is not an answer. The reserved `<unnamed>` key
is filtered by `valid_name`: it is a place in the registry, not a name anything
may print.

The join is built **once per `build_summary`** and threaded down. `_render_page`
runs once per prefix length inside the budget loop — up to `EVENT_LIMIT` times —
and the join walks the registry with a `ps` per record: measured, 343 ms per
turn if it were built there, against a 46 ms whole-page build.

**3. Labels are per record block, not per kind of line.** Every line a record
renders comes from one source, so the agent line, the relayed line and the tool
line take the suffix together: `Codex (build):`, `To Codex (build):`,
`  · (build) 3 tool calls: …`. The tool line matters most — measured, 26 of the
first 40 records a Codex reader sees render *only* a tool line, so it is the
only place their label can go. A per-kind rule would have labelled the agent
lines and left the relayed line of the *same rollout* bare: measured on real
page 52, where the per-kind source counts are `{'codex': 2, 'you': 1}`.

**4. The notice is two-tier and anchored on live claims; the relayed sentence
is additive.** A page says `This page interleaves {n} {Label} sessions;
unlabelled blocks are earlier or unnamed sessions.` only when at least one
selected source is live-claimed **and** two or more sources are selected. It
adds `A {Label} session is running now with no name; name each terminal
(ANTIPHON_NAME) to tell them apart.` only when a block is unlabelled **and** the
registry holds a live endpoint under the reserved key. That last condition is a
kind fact: `valid_key("codex", UNNAMED)` is False, so an unnamed Codex endpoint
cannot exist and the remedy can only ever appear on a page **Codex** reads.
A dead-only multi-source page gets neither line and stays byte-identical.

Today's relayed closing sentence keeps its bytes exactly. On a page where
labelling is active *and* a `you` event is selected, one sentence follows it:
`A parenthesised session label after the recipient names which live session's
line it is.` The trigger is conjunctive because the two conditions come apart —
measured, 4 of the 6 real labellable pages carry no `you` event at all, and a
sentence about what follows a recipient, on a page with no recipient line,
is the defect the relayed-words entry above exists to close. An earlier revision
made the reword unconditional and moved the closing bytes on 22% of real
Claude-read pages and 37% of Codex-read ones; that is why it is additive.

### Measured, so it is not re-derived

**The real-data gate.** Every file of both corpora — 85 Claude transcripts and
130 Codex rollouts, 215 files, 1,656 pages drained from a zero cursor through
the real budget loop — rendered **byte-identical** old against new, with no
crash on either side. It could not have been otherwise today, and the reason is
recorded rather than assumed: across the 31 distinct project directories those
corpora name, the registries hold exactly **one** endpoint record, and it
carries no owner key, so `_session_address` refuses it. The one-file sweep itself can
never label under the two-source rule, so reachability was proved the way the
rule requires: two real rollouts of one project drained together produce 199
pages of which exactly one differs — four labelled lines and the informational
sentence, no remedy (correctly: no unnamed endpoint was live). The differing
bucket is reachable, its measured frequency on real data is 1 in 199, and the
sweep's zero is the rule working, not the feature idle.

**Capacity.** A label can only be added as a prefix grows, never removed, so
candidate size stays monotone and the budget loop cannot oscillate — `selected`
is still the largest length that fits. It is not free: on a near-budget page
(200-byte bodies, 30 records from one source and 5 from another, interleaved,
the first claimed) the same corpus that was delivered complete at 35 records now
delivers 34 and reports `has_more: true`. That flip holds at 20-20 and does not
at 5-30, because the split decides how many blocks carry a suffix; the fixture
parameters are part of the test's assertion.

**`status` shows labels on purpose.** `build_summary` has four call sites, and
the what-X-would-see preview is the page — so it shows exactly what is
delivered. The `Peers:` block is a different thing and is untouched: names and
readiness, never an address, never a session id.

### The bounds, by name

- **Unnamed Codex concurrency is undetectable.** A Codex session that was given
  no alias registers nothing at all, so two of them interleaved on one page
  leave no evidence anywhere. The page says nothing rather than guessing; this
  is why the remedy tier can only appear on a page Codex reads.
- **A hook that carries no `session_id` stays unjoinable.** Nothing is inferred
  from a transcript's contents or its mtime.
- **A started-but-unprompted Claude session is an honestly unlabelled source.**
  `hook_shapes()` installs the Codex pull hook on both `SessionStart` and
  `UserPromptSubmit`; the Claude side has only `UserPromptSubmit`, so
  `record_claude_session` cannot run before that session's first prompt. The
  extra `HookShape` row is declined as out of scope here — both `setup` and
  `doctor` read that table — and nothing false ships: the label is absent, not
  wrong.
- **An endpoint that records no owner key can never be joined.** Two causes and
  no way to tell them apart from a record: one written before the field existed
  (which is what this machine's one live record is), or an `owner_key()` that
  returned nothing at registration. `doctor` names the observable and offers the
  common cause as a cause; the hook stays silent, because saying it once per
  prompt forever is not a diagnosis.
- **Discovery is unchanged.** `RECENT_FILES = 3` still bounds which transcripts
  pull reads at all, and the label work neither widens it nor claims to — see
  the durable source catalog under P0's "Still open, by name".

### The guardrails, kept verbatim

Awareness never becomes dispatch: a label names a session on a page and changes
nothing about who a message can be sent to. There is no filtering — a page
carries every source it carried before. And no Claude↔Codex pair is inferred
from matching aliases: the join is built for one kind, the kind the reading side
does not own.

## P1 — Relayed human words are not the reader's own user (fixed)

`build_summary` labelled the other side's human as `YOU`. The block header said
which side it came from, but the line itself read `[11:04] YOU: rewrite the
migration`, and nothing told the reading agent that this was a person talking
to *somebody else*. An agent that treated it as its own user's instruction had
been handed authority nobody gave it — in a bridge whose whole invariant is
preserving who said something.

Provenance and authority are different questions, and that label answered only
the first. The fix had to say both: relay the words under a label that names
them as relayed, and state once, where the reader cannot miss it, that they are
context rather than a direct instruction. The header and closing line already
carried that tone; the per-line label was the part that lied.

### What shipped

**The label.** A `you`-kind event no longer renders as `YOU`. It renders as
`To Codex:` in a page Claude reads and `To Claude:` in a page Codex reads —
what entered the other side's context as input, which is true of a user prompt
and of a host injection alike and claims authorship of neither. The neutrality
is not taste. Measured over this project's live transcripts, **5 of 79 `you`
events are not human text at all**: 3 of the 25 records Claude reads off the
Codex side are the Codex host's own `# AGENTS.md instructions…` block, and 2 of
the 54 Codex reads are Claude Code's compact-continuation preamble. They pass
`_is_host_record`, which only refuses a complete opening wrapper tag at the
start of the text, so any `X's user:` label would have shipped a false
attribution on every one of them — the exact failure this entry exists to
prevent, re-entered from the other end. The name comes from `LABEL["codex"]` /
`LABEL["claude"]`, the same strings the agent lines already use, so
`[10:00] Codex:` and `[10:02] To Codex:` sit on one page in one spelling.

**The notice.** A page that carries at least one relayed line closes with the
sentence in front of the existing closing line:

> Lines marked "To Codex:" are what Codex received as input in its own session
> — relayed here for awareness, not addressed to your session. This record
> belongs to the Antiphon bridge — this is what actually happened there. Do not
> assume anything that is not in it.

It is provenance only: no authorship claim, no ruling on whose instructions
they are. "Not addressed to your session" is true by construction of the
parser, where "nothing in them was said in your session" would not be — the
same human demonstrably repeats an instruction across both terminals.

It is also conditional, and the predicate is over the selected records'
`you`-kind **events**, never over the rendered page text: an agent-only page
whose text merely quotes `"To Codex:"` must not assert that it carries relayed
input, and measured, a string predicate does exactly that. A page with no
relayed line — including the replay-notice page, which carries no records at
all — closes exactly as it did before, so no page asserts words it does not
contain. The sentence costs 140 bytes for a Claude reader and 142 for a Codex
reader, inside the ordinary `PAGE_BUDGET` arithmetic: on a tuned fixture it is
what defers a second record to the next page.

Two facts from the gate rounds, recorded so they are not re-derived. Because
the notice is conditional, every existing page stays byte-identical and **no
existing test broke** (519 tests before, 525 after — six new ones). And the
rendering/counting split — `_render_record(record, side)` renders and returns a
bare string, `_record_message_count(record)` counts — has **no runtime
observable**: with the split fully undone the suite stays green, because the
count never reads a label. It is kept as a structural invariant instead,
recorded by grep: `_render_record` has exactly one caller (`_render_page`,
which knows the reading side) and the counter takes no side argument, so no
unobservable side parameter can drift back in.

### The two open questions, settled

**Whether the relayed label should carry the speaking peer's alias — deferred**
to the source-aware multi-peer P1 above. An alias↔session-UUID join exists
today **only for named Codex sessions**: `peers.write_session` records that
UUID under the alias and `Event.source` carries it, but there is no Claude-side
writer and `read_session` has no production caller at all. A label that named
aliases now would cover one side of one shape and guess the rest — which is
misattribution again, wearing a more specific name.

**Whether an agent should act on a relayed instruction — the bridge answers
provenance and stops.** The shipped sentence says where the words ran and that
they did not run here; whether the reading agent acts on them belongs to that
agent and its own user. The single human who often *is* both sides' user can of
course repeat an instruction in the reader's own session — that is exactly the
confirmation loop working, not a gap in the bridge.

The boundary, stated straight: a reading agent can still choose to act on
relayed words. What shipped makes their provenance impossible to misread, which
is all a label can do.

## P1 — Large direct-message attachments (shipped, minus acknowledgement and retry)

The direct channel has a separate, honest 128 KiB byte cap, and it stays. What
changed is what happens above it: an oversized direct message is no longer a
dead end. The sender parks the full text and delivers a small envelope naming
where it went.

The entry asked for five things. Three shipped, one shipped in a form the entry
did not ask for and this close names as a deviation, and two — acknowledgement
and retry — are **not delivered and remain open below**.

### What shipped — the decisions

**The spill lives in the tools, not in the transports.** `reply` and
`_send_tool` decide; `send_to_claude`, `send_to_codex` and `_queue_codex`'s
transport body are untouched. Measured: a spill inside a transport strips the
`[Antiphon bridge] Claude:` / `[Antiphon channel] Claude:` prefix that anchors
the echo guard, so the bridge's own delivery is read back as new traffic and
delivered again — `queue_label`'s docstring names that failure exactly — and it
leaves the mid-turn park holding the original. At the caller layer the envelope
replaces only the outgoing text, and the prefixes, the `[from= id=]` reply
address, the park and the dedupe all see an ordinary message.

**Each direction's predicate mirrors what actually refuses.** The composition
comes first — the full outgoing message, prefix and label included — and that
is what is measured.

- `_oversized_for_claude` reproduces `send_to_claude`'s own JSON
  serialization. `len(text)` and the payload length are different numbers and
  not by a constant: measured, `"` costs two bytes and every control character
  six, so 22,000 control characters serialize to 132,091 against a 131,072-byte
  cap while their raw length is a sixth of it, and the whole 130,982–131,072
  ASCII band is over the cap while reading as under it. A raw-length trigger
  would have left exactly that band refusing.
- `_oversized_for_queue` computes its bound at call time:
  `SC_ARG_MAX` − a 502-byte per-exec overhead − the live environment − the
  fixed argv (the session id included) − a one-page margin. No constant could
  be correct: measured on one machine, one binary, only the environment block
  grown, the largest message that execs fell **1,044,820 → 844,759 → 444,759**,
  byte for byte with the environment. `ARG_MAX` is one budget argv and environ
  share. The single-argument limit is not separately binding on Darwin — a
  1,047,587-byte argument execs against an `ARG_MAX` of 1,048,576 — so the
  formula carries no term for it.

**The file carries its own provenance.** One header line —
`[Antiphon attachment from=… id=… sha256=… bytes=…]` — then a blank line, then
the exact content. Megabytes of the other agent's words must not enter a
context as anonymous `Read` output, and the header says whose they are in the
same read. The hash covers the **content only**, so the rule is one a person
can perform: everything after the first blank line, `tail -n +3 <path> |
shasum -a 256`. The envelope repeats the author, redundantly on purpose.

**The envelope carries an absolute path.** The receiving agent's `Read` tool
requires one, and a session started in a subdirectory cannot rebuild it from a
relative form. Measured on the live install: `setup` writes the same absolute
`ANTIPHON_CWD` into `.mcp.json` and `.codex/config.toml`, so both sides agree
on the root, and `project_dir()` returns it from any working directory. The
path is local, and the envelope and both RULEs say so: same machine, same user,
same project. That bound was previously stated nowhere.

**No orphan ever charges the store.** The order is write → send → on any
non-delivery unlink at once, with one line on stderr. Refusals are the common
case, not crashes: an agent retrying an oversized send against a channel that
is down would otherwise write one full-size orphan per attempt and turn a
transport outage into a seven-day storage refusal.

**Above `ATTACHMENT_MAX` the old refusal returns, unchanged.** No store will
take those words, and the guidance naming the visible-reply road is still the
right message. It falls through to the existing `oversize` wrap rather than
refusing in the tool, which is why no new refusal class was born there.

**The quota refusal is unclassed.** `_ClassifiedRefusal`'s own invariant is
that a class means "the sender needs telling where its words still travel", and
its absence means "leave this message alone, it already names its fix". The
quota refusal names its fix — wait for the TTL, or clear the directory — so it
joins the `addressing` family. Nothing is ever evicted to make room: an
unexpired attachment is somebody's undelivered words.

**The sweep runs on the bridge's own heartbeat.** In `hook`, immediately after
`cwd` is resolved and before the page is built. Both halves matter: before the
resolution there is no store path, and after the page build the ordinary quiet
turn — `hook` has five exits and `if not text` is the common one — would never
sweep. Outside every lock (a hold across `cursor_lock` was measured at 5,008 ms
against a concurrent reader's 2,038 ms of patience), in its own `try/except` (a
non-zero exit suppresses the page), tolerating a concurrent unlink, and never
on the `project_dir()` fallback root this code distrusts everywhere else. The
cost was measured at 1.32 µs against a missing store — the overwhelming case —
and 93.9 µs across 50 files.

**The store is a directory this bridge owns outright, or it is not used.**
Measured before the check existed: with `.antiphon/messages` symlinked at a
directory outside the project, the words landed there and were counted as
though they were here. Every write, count and unlink now runs against a store
whose parent and leaf are checked without following a link, with the leaf
opened `O_NOFOLLOW` so a symlink fails the open rather than being examined and
then followed. A pre-existing loose mode is tightened to 0700 rather than
trusted — `makedirs(..., exist_ok=True)` leaves a 0755 directory exactly as it
found it — and a mode that cannot be tightened fails closed. `drop_attachment`
runs on a failure path against a path a caller supplied, so it goes through the
same validated helper the sweep uses: a uuid-shaped name in somebody else's
directory is not this bridge's file to delete, and before the fix it was
deleting it.

Named limitation, ruled at the review gate: a hair of TOCTOU remains — the
store is proved sound with `O_NOFOLLOW` and then re-opened by path for the
write, so a same-user process racing its OWN store between the two steps could
redirect it. Under this project's stated threat model (one person, one
machine; the envelope itself teaches "same machine, same user") that race is
self-sabotage, not an attack surface, and the reviewer's verdict was PASS on
exactly that condition. The absolute closure is known and named: dir-fd-based
creation (`open(..., O_NOFOLLOW, dir_fd=…)` + `os.replace(..., dst_dir_fd=…)`),
which would replace the `mkstemp` idiom — future work if the threat model ever
widens, not a silent gap.

**The quota is one transaction.** A usage read and the write it authorizes are
not two operations. Measured before the lock: two processes released from one
barrier, a 1,000-byte quota and two 700-byte messages — both passed the check,
five rounds out of five, and the store held 1,400. A flock beside the store
(never inside it, where only `{uuid4}.txt` names may appear) covers the read,
the decision and the write, and is released before the caller sends. `push`
already records what a lock held across a transport costs: a 5,008 ms hold
against a concurrent reader's own 2,038 ms of patience.

**Only `{uuid4}.txt` is ever counted, swept or unlinked**, checked without
following symlinks. The one foreign entry this feature can create is a
`mkstemp` leftover from a write that died mid-flight, and the naming rule
refuses it, so it is reported and left rather than swept as an attachment.

### The bounds

`ATTACHMENT_MAX` 8 MiB per message, `ATTACHMENT_QUOTA` 64 MiB for the store,
`ATTACHMENT_TTL` 7 days. All three count **content** bytes, header excluded —
the cap, the quota and the status line alike — and all three are pinned in
README §Limits by the contract technique this project already uses for
`PAGE_BUDGET` and `MAX_CHANNEL_BYTES`, so drift is loud. A contract test reads
this file too, but only for the names of open gaps — never for a number, which
is why the numbers live in the README.

The TTL makes a file *eligible* for removal; it does not remove it. There is no
timer. The next hook either side runs does the deleting, so a project where
neither side takes a turn keeps its files until one does. The envelope, both
RULE sentences, the quota refusal and README §Limits all say that, rather than
promising a deletion nothing schedules.

The queue's own bound is deliberately unpinnable. It is not a constant, and a
number written down here would be a promise about somebody else's shell.

### The two roads are not the same, and both surfaces say so

The direct tools spill. The `@claude` / `@codex` marker road does not: a marker
line over the cap is still refused, and that refusal prints on an exit-0 Stop
hook, which this file already records as reaching a debug log and not the
agent. That is a decision, not an omission — a marker line's words are already
in the visible reply, and the passive pull pages carry that reply whole, so an
attachment there would duplicate for nobody what pull already delivers. An
agent that learned "oversized direct sends work now" and then lost a `@claude`
line would have been told something untrue, so `AGENTS_RULE`, `CLAUDE_RULE` and
README §Limits each state the asymmetry, with contract tests on all three.

### The deviation, named

The entry asked to clean the store "without deleting an unread message
silently". Unread is not tracked and this does not track it. What ships is
time-based and announced: a file older than the TTL is deleted with a line
naming it, on the next hook either side runs. The party that announcement
reaches is whoever is looking at that terminal — **not** the reader who never
read the message. That is a real gap against the words of the bullet, and
closing it needs the acknowledgement protocol below.

### Still open, by name

- **Acknowledgement.** Nothing signals that the parked file was read. The
  envelope rides the same at-least-once delivery every message does, and its
  ack story is every message's: none beyond transport success. This is also
  what would make the TTL's deletion provably safe rather than merely
  announced.
- **Retry.** A resent message parks a second file under a second uuid; nothing
  reuses or supersedes the first. The first is then an ordinary attachment with
  an ordinary TTL, so nothing leaks, but a retry is not a retry of anything —
  it is a new attachment.

Both need pending-delivery state this release does not have, which is the same
state the `reply correlation` entry below wants. They belong together.

## P1 — A marker in anything but the turn's last message is dropped (fixed)

`push` used to read the other side's newest assistant text through
`last_claude_reply` or `last_codex_reply`, and both kept only the most recent
assistant record — `chunks = texts` overwrote on each one. One turn is not one
record, so an agent that wrote a progress message containing `@claude do this`
and then a closing message without markers had its instruction silently
dropped. The obvious repair — join every assistant record in the tail window —
was wrong on its own: it would sweep up markers from previous turns and resend
them, since the dedupe fingerprint compares the joined text and a window that
grows by one record each turn produces a different fingerprint every time. The
fix needed a boundary for "this turn", not a wider join, and both readers now
have one.

### What shipped

- Codex (`last_codex_reply`): the hook payload's own `turn_id` names the turn.
  A matched id returns the span from its `task_started` to its own
  `task_complete`, or to EOF — a live measurement (Task 1, one non-ephemeral
  local run) confirmed the CLI writes `task_complete` only *after* the Stop
  hook has already fired, so waiting on it would have returned the previous
  turn forever. An id present but unmatched (its start already scrolled out
  of the tail window) fails open to the whole visible window rather than
  guessing at a different turn's span. With no id at all — a CLI older than
  the `turn_id` field — the newest `task_started` alone decides, cut by
  nothing. The reader also reports *whether* it bound the turn: the matched
  id where a span was actually cut to it, nothing in either fail-open branch
  or in any no-id branch.
- Claude (`last_claude_reply`): a `user` record is a turn boundary unless it
  is a tool result, an `isMeta` record carrying `sourceToolUseID` or
  `turnCompanion` (a Skill load or turn companion), or an `isMeta` record
  whose top-level `origin.kind` is in the measured mid-turn allowlist
  `{"coordinator", "task-notification"}`. `origin.kind="channel"` — the
  bridge's own injection — and any unmeasured kind stay boundaries.
- The `promptId` field this entry originally proposed as the boundary was
  measured and set aside: absent on all 5,067 sampled assistant records,
  present on only 554 of 556 sampled user records — corroboration for a
  boundary found another way, not something reliable enough to key on
  directly.
- The dedupe fingerprint itself is turn-scoped, not content-only.
  Content alone is not identity: the exact same `@claude`/`@codex` line
  repeated in a later turn hashed identically to the one an earlier turn
  already sent, so it was silently swallowed — measured, one send where two
  turns each said it once. `push_fingerprint` now folds the turn's own
  identity (the matched Codex `turn_id`, or the `uuid` of the Claude
  boundary record that opened the turn) into the fingerprint before
  hashing, as a structured pair that cannot collide with the flat
  content-only shape; an empty key falls back to that original shape
  unchanged, both for continuity with cursors already on disk and because
  a repeat with no nameable turn is exactly what content-only dedupe was
  always meant for. The key is always the *reader's* — never the hook
  payload's on its own. Keying an unbound window on the id the hook happened
  to report changed the fingerprint every turn while the window's marker text
  did not: measured, four sends of one instruction across four turns where
  content-only dedupe sends it once.
- One read decides both halves, each direction. `push` calls the internal
  `(text, turn key)` reader once — `_claude_turn` or `_codex_turn` — rather
  than parsing the transcript a second time to re-derive the key.
  `last_claude_reply` / `last_codex_reply` remain the public single-value
  names over those pairs. Two reads were not equivalent: the transcript can
  grow between them, so the second can name a turn the first never saw, and
  the send from turn A is then recorded under turn B's key — which silently
  suppresses turn B's own later identical marker. Measured: one send where
  two were expected.
- A cursor slot still holding the flat, pre-scoping digest of the batch about
  to be pushed migrates in place, without sending. Comparing it against the
  new scoped shape alone would call already-delivered content new and resend
  it once per slot; recognised in its own old form instead and rewritten to
  the scoped digest, the same way the pre-digest string cursor migrates.
  Every fingerprint `push` writes goes through the one scoped helper — the
  migration write included — so the two shapes cannot drift apart. This
  migration is not free of consequence; see the follow-up entry below.
- Verified end to end: `push`, run against real transcript fixtures with only
  `send_to_claude`/`send_to_codex` mocked, delivers a marker from a non-final
  message, stays quiet on an identical re-read, delivers again — with the
  old turn's text absent — once a new turn carries its own marker, and
  delivers again when a later turn repeats the identical instruction
  verbatim.

### Named limitations

- A turn larger than `TAIL_BYTES` still clips at the tail window; both readers
  keep reading through `tail_lines` unchanged.
- An ephemeral Codex run reports `transcript_path: null`, and `push` no-ops
  before either reader runs — a marker written there never reaches the
  bridge.
- Codex's two fail-open paths — an id present but unmatched, and no
  `task_started` visible while an orphan `task_complete` is — return the
  whole visible window and can duplicate an old turn's tail into a fresh
  send: at-least-once by design, the same trade the delivery layer already
  makes elsewhere. (The third no-id sub-branch, no task marker visible at
  all, does not fail open; it falls back to today's newest-message
  behaviour instead.) Neither carries a turn key — the reader cannot name a
  turn it just failed to bind — so these windows dedupe on content alone: an
  unchanged window stays fingerprint-stable turn after turn, however many
  turns run over it. What still duplicates is a window whose *marker set*
  changes, because the whole window is one batch: measured, a fail-open
  window holding `do X` (already delivered) that gains a second marker
  delivers `do X\ndo Y` — the new instruction and the old one again.
  At-least-once, once per new marker written into the same unbound window,
  not once per turn.
- Where no turn key exists at all — a pre-`turn_id` Codex hook, or a Claude
  window whose boundary record has scrolled out of the tail or carries no
  `uuid` — the dedupe fingerprint stays content-only, so an identical
  instruction repeated in a genuinely new turn is still silently deduped
  away in that case.
- On a CLI whose hook payload predates `turn_id`, the no-id case has two
  residual gaps: a closed nested span sitting inside the window still drops
  text written before that nested start, and the reader cannot distinguish a
  rollout with no markers at all (3/127 measured) from one whose markers all
  sit beyond the tail (1/127 measured) — the two windows look identical from
  inside.

## P1 — A mid-turn tool reply's digest swallows a later turn's identical marker (fixed)

`_record_delivery` — the mid-turn `reply_to_codex` / `_send_tool` path —
wrote `batch_fingerprint([text])`, the **flat**, content-only shape, into
exactly the slot `push`'s dedupe reads. It had to: without that record the
same text arrives twice, once from the tool and once from the Stop hook that
ends the turn. But `deliver_batches`'s flat→scoped migration branch reads a
flat value in that slot as "this batch already went out", and that premise is
false when the flat digest came from a *different, earlier* turn.

Measured before the fix (real transcript, real cursor, only `send_to_codex`
mocked), kept here as the historical record:

- turn A: Claude answers Codex through the reply tool with `run the suite`;
  its visible reply carries no marker, so the Stop hook finds no batch and
  the slot keeps the flat digest `8a2e661d…`;
- turn B: Claude writes `@codex run the suite` — a genuinely new instruction
  that happens to repeat the earlier wording. **0 sends where 1 is expected**,
  silently; the slot upgrades to the scoped digest `c573f74c…`;
- turn C: the same words again → 1 send, as expected.

### What shipped

The record no longer lives in the live slot. `_record_delivery` merges
`{slot: digest}` under `MID_TURN_SLOT = "\0midturn"` — out of recipient space
the way `LEGACY_SLOT` is, and nested so the per-recipient separation the live
slots pay for survives the move. An *unaddressed* park write still drops the
pre-digest string beside it: the rule `forget_superseded` encodes is about the
delivery, not about where the record sits, and keeping both would leave a
record nothing clears.

`push` retires the park in one phase:

1. **Read** — the existing unlocked cursor read notes the park's exact
   `{slot: digest}` pairs.
2. **Consume** — inline in `push`, before `deliver_batches` and without
   touching its signature: a batch whose *content-only* digest equals its
   slot's parked digest is pre-seeded into `raw_sent` with that batch's own
   current-shape fingerprint (scoped where a turn key exists, flat otherwise),
   so `deliver_batches` recognises it and records it without sending. The
   pre-seed goes after the `before_send` snapshot; seeded earlier the consumed
   slot is equal on both sides of the delta and never reaches the cursor at
   all. It is deliberately not guarded by `turn_key` — a keyless Stop is
   exactly the case that echoes its own mid-turn delivery.
3. **Retire** — exactly the pairs observed in step 1 are deleted, by
   compare-and-clear so a pair written while the send was in flight keeps its
   own Stop, and the `MID_TURN_SLOT` key goes with its last pair (an empty
   park is *absent*, never `{}`). On the delivered path the deletion rides
   inside the `mutate` that already runs under the cursor lock: `cursor_lock`
   is not reentrant, so a helper taking its own lock there would burn the full
   patience on every delivered push and retire nothing. The two returns that
   hold no lock use a small lock-taking helper instead.

Exit paths, exhaustively:

| exit | what happens to a park |
|---|---|
| `stop_hook_active` | nothing to do — the invocation that set the flag already retired what existed when it ran. A `_record_delivery` made *after* that retire leaves a park no push in that turn retires: the same one-turn window as the crash case below. |
| missing / `null` `transcript_path` | the documented ephemeral-Codex case. A park written by an ephemeral turn survives until the next non-ephemeral push. |
| `if not batches` (markerless Stop) | retired |
| `if not delivered` (nothing sent, or the send refused) | retired |
| delivered path | retired inside the same `mutate` as the delivery record |

A write that was carrying a retire and failed prints
`antiphon: a mid-turn record was left behind; an identical marker may be
suppressed one extra turn`, and the next push retries. The delivered path's
own failure line no longer claims "not a drop" — with a park aboard, that
failure really can suppress one next-turn marker — and prints the left-behind
notice beside it, so the two costs are reported separately rather than under
one blanket sentence.

**The trade, stated straight.** In the ordinary flow the park lives exactly
one Stop. In the crash window (a turn whose Stop never runs — or a push that dies
between its unlocked read and its retire) and the lost-lock window, a stale park survives into the next push, whose consume can
then suppress **one** next-turn marker repeating the parked wording — a
bounded, one-turn loss; the turn after sends. It is diagnosed on stderr in the
lost-lock case and **silent in the crash case**, where no push ran to say
anything. Measured: 0 sends where 1 was expected on the next turn, 1 on the
one after. That is not "never a loss"; it is today's behaviour for exactly one
turn instead of forever, where today's is unbounded until something overwrites
the slot.

**Cost.** The markerless Stop did zero cursor I/O before (measured). It now
does one unlocked read per markerless Stop, and takes the lock only when that
read actually found a park. One visible side effect of that read: a corrupt
cursor file is now diagnosed ("could not be read safely") on a path that
never printed before — a diagnosis, not a behaviour change.

### Named limitations

- Two mid-turn replies to the **same recipient in one turn** still echo —
  and the echo is the **whole batch** (measured: `line one\nline two`, so the
  first line is redelivered too). The park holds one digest per slot, the
  Stop batch hashes both lines together, so nothing matches — unchanged from
  before this work, not a regression, but it bounds "the mid-turn duplicate
  stays prevented" to the single-line case.
- The legacy supersede stays where it was moved to: at park-write time, on an
  unaddressed delivery.
- The flat→scoped migration branch inside `deliver_batches` stays live. It is
  **not** a one-time upgrade path and must not be described as one: any push
  that resolves **no turn key** — a pre-`turn_id` CLI, a boundary clipped out
  of the tail window — still writes a flat, content-only digest into the live
  slot, and a later push that *does* resolve a key and repeats the wording is
  silently suppressed once by that branch. Measured with no tool call anywhere
  in the scenario, identical before and after this work: 1 send, then 0, then
  1 — on `8a2e661d…`, the same digest this entry names above. What this work
  removes is exactly the *mid-turn tool path* as a feeder of flat digests.
  The cause is already filed under the turn-scoped entry's content-only
  fallback limitation; the manifestation is narrower than that entry's
  wording, which describes keyless→keyless suppression, while this residual is
  **keyless-write → keyed-identical-push** through the migration branch.

## Shipped — a bare push goes to a running Codex, never to the newest file

Measured on Codex 0.151.0: a thread opened at 13:03 got its rollout file at
15:13, on the user's first turn, 7,832 s later. Discovery reads rollouts, so
until then the running session did not exist for the bridge, and a push at
15:04 was queued — `codex queue --thread` accepted it, Claude's Stop hook saw
success — into the newest file's thread: an empty session from 12:55 that
nothing would ever drain. A second message had been waiting the same way in a
thread closed at 11:42. Nothing anywhere said so.

What shipped: Codex holds an exclusive flock on
`~/.codex/thread-writer-locks/<id>.lock` from the moment a thread opens and
removes the file when it closes (measured: the live terminal and one desktop
thread held theirs; every closed thread's file was gone). `codex_session_id`
now takes the newest *running* session among the rollouts recording this
directory; where a Codex keeps no such locks the old newest-file rule stands
unchanged. When rollouts exist and none is running, the push is refused with
the reason — a running session gets a transcript only on its first turn and
cannot be addressed until then — instead of stranding the words; the refusal
is classified `no-peer`, so the honest-refusal sentence about the passive page
still follows it. `doctor` reads Codex's own queue read-only and notes, as `·`,
messages waiting in a thread that is not running; only Codex can drain that
queue, so it is never ✗.

Named limitations: the lock is Codex's internal behaviour, feature-detected
and measured on one version, not a contract. A running thread that has not
taken its first turn is still unaddressable from here — the Codex hook does
receive its `session_id` and `cwd` at `SessionStart`, and recording that as a
delivery hint would close the gap, but it changes the rule that an unnamed
Codex leaves no record, and belongs with the automatic-peer-identity entry.

## Shipped — the channel never hands Claude Code a null sender

Measured on Claude Code 2.1.251, in the host's own MCP log: every
`notifications/claude/channel` carrying `meta.sender_alias: null` was refused
with `ProtocolError: Invalid params … expected string, received null`, and the
event never reached the agent — while `channel.mjs` had already answered the
sender `{ok:true}` and Codex read "Delivered". An unnamed Codex is the default
install, so the whole Codex→Claude direct road was dark for it; the passive
pull masked this until the upgrade replay (below) starved that road too. The
same day's log shows string aliases going through untouched, and the previous
day's log shows no refusal at all: every live send until then had come from a
named session, so the null branch had never been exercised against the host —
the Node suite drives the server with the SDK client, which does not enforce
the host's schema.

What shipped: `meta.sender_alias` is always a string. A peer with no usable
name arrives as the reserved registry key `<unnamed>` — the one both writers
already share and the alias grammar already refuses — and `reply_to_codex`
reads that key handed back as `to` as "nobody in particular", the bare reply,
never as a peer called `<unnamed>`. The three Claude-side surfaces (channel
instructions, CLAUDE.md rule, README) say "a name rather than the literal
`<unnamed>`" where they said "non-null", and the contract test now rejects the
word "null" on any of them. The Node test pins the wire: nine unusable claims,
including `null` and `"<unnamed>"` itself, all reach the agent as the string
sentinel; the sentinel as `to` produces word-for-word the bare outcome.

Named limitation: this is the host's schema as measured on one version. If a
later Claude Code accepts null, nothing breaks; if it ever rejects the
sentinel string, the same measurement (the MCP log under
`~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-antiphon/`) finds it.
The fresh-user end-to-end run that would have caught this before release is
filed under the release checklist entry below.

## Shipped — a refused send says what the peer will and will not see

The problem was real and the recorded premise was false. Observed twice in one
session: the Codex host answered `direct app-server input is not allowed for
unloaded spawned sub-agents`, `reply_to_codex` failed, and the tool reported
that error and stopped there. The reasonable reading of a failed send is that
the message was lost — which invites repeating it, or proceeding as though the
peer was never told. But the fix this entry prescribed was to promise a delay:
*"passive pull carries everything either side wrote, and a refused active send
changes only the timing."* Measured, what a refused send leaves on the peer's
page is neither everything nor the same thing in both directions.

- **Claude → Codex: a tool-name line, and nothing of the message.**
  `claude_events` (`lib/antiphon.py:1077`) draws a tool event's detail from
  `file_path`, `command` or `pattern`. `reply_to_codex` carries none of them,
  so across 123 real `tool_use` records in this project's own transcripts the
  whole page entry is `· 1 tool calls: mcp__antiphon__reply_to_codex`, and the
  `text` argument is unreachable by any parser path. That sample covers refused
  calls by mechanism: a `tool_use` block is part of the assistant message and
  is written when the call is *emitted*, before any result exists.
- **Codex → Claude: nothing at all.** `codex_events`
  (`lib/antiphon.py:1205`) emits a tool event from exactly one record shape,
  `payload.type == "exec_command_begin"`. Across 21 discovered rollouts there
  were 0 tool events, and a census of all 129 rollout files on the maintainer's
  machine found no `exec_command_begin` record of any kind — this Codex writes
  tool calls as `custom_tool_call` (3,879) and `function_call` (282). A refused
  `antiphon_send` leaves no trace on Claude's page whatsoever.
- **What does survive is the visible reply.** `build_summary` carries assistant
  text verbatim, marker line included, and `_build_page`
  (`lib/antiphon.py:1415`) never splits a record. So the guidance points there
  and claims order and completeness only — never timing. A page is bounded by
  `PAGE_BUDGET` and `EVENT_LIMIT` and filled oldest-first, so under a backlog
  the words land on the peer's third or fourth turn; "on its next turn" would
  have been the same species of false promise as the one being removed.
- **The Stop hook is not a second transport.** `push()` → `deliver()`
  (`lib/antiphon.py:1806`, `1879`) calls the same `send_to_codex` /
  `send_to_claude` the tool just called, so a refusal repeats byte-identically
  after the prefix. Advising a sender to re-address the line would be advising
  it to reproduce the failure. (A failed push does not record its fingerprint,
  so the line is genuinely re-offered every turn until it succeeds — that is
  the true guarantee, and it is not one about the refused send.)
- **The no-session refusal is about addressing, not about readership.**
  `not delivered: no Codex session found in this directory` means
  `codex_rollout_files(cwd)[:1]` was empty (`codex_session_id`,
  `lib/antiphon.py:1762`). The Codex-side page is built from **Claude's**
  transcripts and never consults a Codex rollout, so it is fully readable
  exactly where that refusal fires — measured in the same fixture that produces
  it. This is the class where naming the passive page matters most, not least.

### What the surfaces say now

`TOOL_GUIDANCE` (`lib/antiphon.py:2033`) with a `{seen}` slot the reading
surface fills from its own measurement: `only a tool-name line` from `reply()`,
`nothing` from `_send_tool`. It is appended only when the detail was born
carrying a class — a `str` subclass with `refusal_class`
(`_ClassifiedRefusal`, `lib/antiphon.py:2000`), wrapped at the birth sites
below. Widening the `(ok, detail)` pair to carry the class instead was measured
at 72 red tests; the subclass costs zero and survives every existing unpack.

| class | born at | guidance |
|---|---|---|
| `transport` | `_queue_codex` ×4 (one of them the crash-belt: an exec the kernel refuses, measured at 1.1 MB of message), `send_to_claude`'s five socket/response failures | yes — the words are nowhere on the page |
| `no-peer` | `_legacy_target`'s no-session message | yes — the page carries them regardless |
| `oversize` | `send_to_claude`'s 128 KiB pre-transport refusal | yes — an oversized record still travels whole through the automatic hook |
| `addressing` | `resolve_target`'s six sites | **no**, byte-identical: they already name the fix, and those sites are never wrapped, so there is nothing to append |
| **push**, every class | — | **no**, byte-identical: its failure print is exit-0 hook stderr, which reaches a debug log and not the agent |

`lib/channel.mjs` is untouched. It hands the calling agent
`detail.slice(0, 500)` of Python's stderr line, which `reply()` writes as
`reply: <detail>`; the longest guidance-carrying detail is a host refusal cut
at 200 characters (`no-peer` is 54 and `oversize` shorter still, so neither
moves the number), giving 396 of 500 with 104 characters of headroom. A
contract test measures that line end to end rather than recomputing it.

### The entry's open question, answered

It asked whether any refusal exists that the pull path genuinely cannot cover.
Pull covers every *refusal*. What it cannot carry is text that exists only
inside a tool call's arguments — in both directions, and that is precisely the
text a refused send consists of. Which is why every guidance points at the
visible reply rather than at the send being retried. The route that would
change that answer is the tool-call retrieval item under P0's *Still open, by
name*; until it exists, a tool argument is unreadable to the other side whether
its call succeeded or not.

### What "in full" is bounded by

- `antiphon_read` refuses a record over `PAGE_BUDGET` without advancing (the
  measured Codex-MCP-result caveat above). It cannot bite the `oversize` class,
  whose reader is always Claude and whose channel exposes no `antiphon_read`,
  and the automatic hook delivers the record whole either way.
- Discovery reads the newest `RECENT_FILES = 3` transcripts per side
  (`lib/antiphon.py:60`): a sender whose session is not among them contributes
  nothing to the peer's page. Filed under the durable source catalog in *Still
  open, by name*.
- A peer with no cursor positions starts its window at `LOOKBACK` —
  **six hours** (`lib/antiphon.py:61`, applied in `positions_for` at `708`). A
  Codex session that starts more than six hours after the words were written
  begins past them, which bounds the `no-peer` case's future reader.

## Shipped — `antiphon doctor`

One read-only command that explains the common “bridge is quiet” cases.
Seven checks, in print order:

1. **Install** — which `antiphon` `PATH` resolves, against the package root of
   the copy running the check, `realpath` on both sides. Same install is `✓`
   with the version; a different copy of the same version is `·` (a
   maintainer's clone beside a global install is not broken, and the hooks use
   `PATH`'s copy either way); an **older** copy on `PATH` is `✗`, because the
   hooks that actually run are the old ones; a newer one is `·`. Versions are
   compared as tuples of ints — measured, plain string comparison inverts on
   three of four realistic pairs, `0.9.0` reading as newer than `0.10.0` — and
   anything that will not parse that way is `·` “cannot compare”, never an
   ordering guess. No `antiphon` on `PATH` while hooks call it is `✗`.
1b. **Running servers** — `ps -eo pid=,lstart=,args=` behind a third seam
   (`_process_table`, beside `_which` and `_tool_version`), filtered to the two
   long-lived servers by the script in their argv; the wrapper only spawns and
   is not one. A server whose package root's code files (`lib/antiphon.py`,
   `lib/peers.py`, `lib/channel.mjs`, `package.json`) changed after it started
   is `✗` with both times and "restart that session" — measured 2026-08-31,
   four servers were answering with pre-merge code while doctor said 13/13 ✓.
   A server whose root is gone is `✗` as an orphan (measured: a two-day-old
   channel under `launchd` from a renamed directory). Machine-wide like the
   install check; another copy's root is named. The registry lends a pid its
   alias, scanned without pruning. Nothing running is `·`.
2. **Interpreters** — this run's Python against a `PYTHON_FLOOR` constant bound
   to the README by contract test; what the wrapper's bare `python3` actually
   resolves to on `PATH`, which is a different question (measured on the
   maintainer's machine: Anaconda 3.14, not the 3.9 the suite runs under); and
   `node --version` against `engines.node`.
3. **Configuration** — `.claude/settings.json` (both hooks and the
   `mcp__antiphon__reply_to_codex` permission), `.claude/settings.local.json`
   (the `enabledMcpjsonServers` entry that gates the channel server at all),
   `.codex/hooks.json` (three events and the approval-prompt label),
   `.codex/config.toml` (the table, its `env_vars` line, and the `ANTIPHON_CWD`
   **value** — a table pointing at a renamed directory reads another project's
   registry with every key present), `.mcp.json` (the channel entry). Missing
   pieces are named; the repair is `antiphon setup`.
4. **Alias** — through `peers.explicit_name()`, the exact function production
   routing uses, never the raw environment. Measured: it lower-cases, so
   `ANTIPHON_NAME=UI` is a working named session a raw read calls invalid.
   Doctor prints the name it returns, because `@claude:UI` addresses nobody.
5. **Peers** — `_scan` plus `_record_alive`, listed by kind and alias.
6. **Channel reachability** — a probe, not a `stat`.
7. **Codex delivery** — `codex` on `PATH`, presence only.

**Vocabulary and exit contract.** `✓` fine, `·` nothing to do here, `✗`
broken; only `✗` makes the command exit 1. A set-up project with no session
running prints only `✓` and `·` and exits 0 — pinned by test. A diagnostic
that warns about the normal resting state is one people learn to ignore.

**Zero writes, enforced.** Doctor never opens a file for writing, never takes
the registry lock, and calls none of the three readers that prune —
`peers.read_peers` (`peers.py:425`), `_live_by_kind` (`antiphon.py`, what
`status` uses) and `resolve_target`. All three delete a dead peer's record on
the way past, which would remove exactly the stale record somebody ran the
command to ask about. A test snapshots bytes, sizes and mtimes under two roots
— the project fixture and the external socket directory — before and after, on
a broken fixture and a healthy one, each with a corpse armed.

**The socket `✓` is falsifiable.** Connect → `shutdown(SHUT_WR)` → read one
reply → close; nothing is ever sent. The half-close is load-bearing: the
channel server answers from its `end` handler, so measured against the real
server the reply arrives in 0 ms with it and never without — the obvious
order reports every healthy bridge as broken and tells the user to restart.
The reply must parse as a JSON object carrying an `ok` key; the *presence* is
the signal and the value is ignored deliberately, because the healthy answer
to a bare connection is `{"ok":false,"error":"Unexpected end of JSON input"}`.
Any process can bind that path, so without reading the answer the check would
pass for all of them. Retry patience is spent only where a registered live
peer claims the address: `NOT_LISTENING_YET` includes `ENOENT`, so a blind
retry at the unregistered project default spends 1,545 ms over 28 attempts on
the perfectly normal no-socket state. That split cannot reintroduce the race
the patience exists for — the channel server claims the registry before it
binds, and a contract test pins that ordering.

**Doctor is authoritative over `status` on reachability.** `status` prints
`Claude channel: live` when the socket *file* exists; doctor means somebody
answered. On a stale socket the two disagree, and doctor is right. `status` is
unchanged here on purpose; aligning it is a separate change.

**Privacy.** No session ids, no cursor contents, no addresses in the peer
list. One deliberate exception: the stale-socket and not-a-socket repair lines
print the socket path, because that is the file the person may need to remove
(five repair lines in total print it — every not-listening/not-a-socket/
cannot-connect arm, per probed address).

Known incompleteness, deliberately parked: the config *envelope keys*
(`permissions`/`allow`, `mcpServers`, `enabledMcpjsonServers` as a key, the
hook envelope fields) are still spelled once in setup and once in doctor.
Latent, not live — those keys are owned by Claude Code and Codex and never
change unilaterally — but a future extraction pass could move them into the
shared shapes too. Note also that `status` may say `live` purely from the
registry, not only from the socket file; doctor's answered/unanswered remains
the authoritative reachability verdict either way.

**Also fixed:** `antiphon --help`, `-h` and `help` print the usage and exit 0.
They exited 1 on all three spellings, and the check runs before the command
table so `antiphon help doctor` is not an arity error.

### Still future

- `--fix`. The default command must keep editing nothing; a repair mode would
  call the existing idempotent `setup` path.
- Executing `codex` to prove queue liveness. Check 7 is presence on `PATH`
  only: running it from a diagnostic can block on authentication or spawn a
  session, and a command run because things are broken must do neither.
- Per-peer Codex reachability, for the same reason.
- Whether Codex actually *forwards* `ANTIPHON_NAME`. Doctor verifies the
  `env_vars` line that asks for the forward; only a live Codex process can
  show that it happened.

## P2 — Reply correlation

Explicit `to` remains the safe default when several peers are live. Automatic
reply routing needs a durable design before implementation:

- correlate only after a successful delivery acknowledgement;
- scope pending messages to the receiving peer;
- validate the original sender is still the same live session;
- let explicit `to` override correlation;
- fail closed when several unanswered senders remain;
- define expiry and cleanup without losing a late reply.

## P0 — A named Claude session can identify itself as `<unnamed>`

From a user report on 0.3.3 (macOS 26.3, Node 22.16, Python 3.12.7; one Claude
and two Codex peers, all named): for 38 minutes every Codex → Claude send
failed with `Claude MCP Channel is down: No such file or directory` while
Claude → Codex succeeded throughout. The Claude peer had a `session.json` and
no `endpoint.json`, so `resolve_target` could not move the address to the
named socket and returned the project-wide path, where nothing listens — while
the named socket was bound and serving the whole time. The reporter's own
words for the shape of it: it presents as "the other agent is ignoring me"
rather than as a transport fault, because the sender falls back to the passive
page and the receiver is told nothing at all.

**The report's stated root cause is a false inference, and must not be acted
on as written.** It observes `grep -c endpoint lib/channel.mjs` → 0 and
concludes the Node arm never publishes an endpoint record. It does:
`channel.mjs:235` is `const claimPeer = () => registryCall("register_peer")`,
which runs `python3 lib/antiphon.py register_peer` with kind, name, address
and pid on stdin. The word never appears because the file's name belongs to
the Python half. Adding a second writer there would double-register.

**What was measured here.** Two `channel.mjs` servers started for one project
under the same `ANTIPHON_NAME`: the second was refused —
`register_peer: peer name 'mainclaude' is already held by pid 18763` — and
`endpoint.json` survived intact, still naming the serving process. So the
atomic claim protects the record in the ordinary duplicate case, and the
hypothesis that a second server releases the first's registration is
**disproved**. Why the reporter's first server (pid 6519) held the socket
without ever registering is **not root-caused from here**; `registryCall`
writes its failure to stderr, which for an MCP server reaches the host's log
and never the person. Their `mcp-logs-antiphon/` is where that evidence is.

**What is a real defect, independent of that.** `channel.mjs:462-466` assigns
`senderAlias = peerName` only in the branch that both won the claim and serves
the socket. Every other route through startup — claim lost, socket already
served by another process, `serveSocket()` failed — leaves it null, and the
session then signs its messages `[from=<unnamed>]` with `ANTIPHON_NAME` set
and valid. The report saw exactly this: a named peer silently downgraded to
the one condition the documentation says cannot be routed, which then made the
Codex side refuse delivery by name. Identity is not the same question as
channel ownership, and today one answers the other.

**What to change, in the order the evidence supports.**

1. Separate identity from channel ownership: a valid `ANTIPHON_NAME` makes
   this session that peer for the purpose of signing, whether or not it won
   the socket. Losing the channel means it cannot be *reached*; it never meant
   its words come from nobody.
2. Make a registration failure visible where a person looks. `doctor` can see
   the shape directly: a live channel socket for this project with no endpoint
   record naming a live pid is exactly the reported state, and it is
   diagnosable read-only.
3. Refuse by the true reason. When a caller asks for `mainclaude` and no live
   endpoint holds that name, say so instead of falling through to the
   project-wide path and reporting `ENOENT` on an address the caller never
   asked for.
4. Tell the receiver that somebody tried. Nothing on the Claude side learns
   that a peer attempted a send and was refused; both agents concluded the
   other was idle. Even a line on the next page would have collapsed a
   40-minute investigation.

**Also reported, filed rather than fixed here.** A stale pid left in
`endpoint.json` by a writer that exited without unregistering, which works
only because the socket path is derived from a hash rather than read from the
record — `doctor`'s `running:` check now names servers whose code is older
than the install, but not endpoints whose pid is gone. Orphaned `channel.mjs`
processes accumulating across reconnects (seen on the maintainer's machine
too). And the passive fallback's own arithmetic: with a real backlog the
reader advances roughly one page per turn and iterates dead sources ahead of
live ones — measured by the reporter at ~940 KB across four sources, two of
them sessions that had already exited, putting a reply 15-20 turns away.
`antiphon catch-up` is the blunt escape; deprioritising sources whose session
is gone is the real answer, and belongs with the source-catalog entry.

## P1 — Same-vendor bridging: Codex ↔ Codex and Claude ↔ Claude

Asked for on 2026-08-31 after running two Codex terminals and one Claude on a
second machine. Today the bridge is defined by its two sides: a Claude session
reaches Codex and a Codex session reaches Claude, and neither can reach a peer
of its own kind. That is a property of the surfaces, not of the machinery —
which is why this is worth doing and why it is not free.

**What already exists.** The registry is kind-aware end to end: `peer_dir` and
every record carry `kind`, `read_peers(cwd, kind)` filters on it, and
`resolve_target(cwd, kind, alias)` takes the kind as an argument rather than
assuming one. Both transports are per-kind and already written: a Claude peer
is reached by writing its Unix socket (`send_to_claude`), a Codex peer by
`codex queue --thread` (`send_to_codex`). So `claude → claude` and
`codex → codex` are, mechanically, calls that already compile.

**What is missing.** The surfaces, and every sentence that assumes the other
side. `@codex:name` is parsed only out of a Claude transcript and `@claude:name`
only out of a Codex one; `reply_to_codex` names its recipient kind in the tool
itself; the CLAUDE.md and AGENTS.md rules, the channel instructions and the
README all describe a two-sided bridge. The passive pull is the same shape: a
side's page is built from *the other* kind's transcripts, so a Claude session
would not see another Claude session's work at all.

**The questions this opens, none of them settled.**

- *Identity.* An event arriving from a peer of the same kind must still say
  which session spoke, and a Claude reader must not read another Claude's words
  as its own or as its user's. The `sender_kind` field exists; the labelling
  rules were written when kind and side were the same thing.
- *Loops.* Two peers of one kind, each pushing markers the other renders, is
  the first shape in this project where a message can come back to its author.
  The relayed-words work keeps provenance readable, but nothing today bounds a
  hop count, and the cross-vendor entry below wants the same budget.
- *The unnamed default.* Two same-kind peers are exactly the case where names
  stop being optional. See the entry below on unnamed addressability: on the
  Codex side an unnamed session leaves no record at all, so two unnamed Codex
  terminals cannot be told apart, let alone addressed.
- *Scope.* Whether the passive page should carry same-kind activity too, or
  whether same-vendor stays a direct-message-only road. Carrying it doubles
  what a page can hold and re-opens the discovery window question.

The honest order is: unnamed addressability first (below), because same-vendor
routing is unusable without it, then the surfaces, then loop bounds.

## P1 — An unnamed peer is invisible, and two of them are indistinguishable

Measured on 2026-08-31, from a real session on a second machine: with two
unnamed Codex terminals and one Claude, Claude could only answer whichever
Codex it had last exchanged with, and could not reach the other at all. A Codex
terminal that had not been typed into could not be reached either.

**The mechanism, read out of the code.** `record_codex_session`
(`lib/antiphon.py`) returns False before writing anything unless
`peers.valid_name(peers.explicit_name())` — so an unnamed Codex session writes
*no registry record whatsoever*. `register_codex_peer` returns None on the same
condition. `resolve_target` therefore cannot see it, cannot name it in an
ambiguity refusal, and falls back to `_legacy_target`, which picks the newest
*running* rollout — one session, chosen by recency, with no way to ask for the
other. This is deliberate: the project refuses to guess a recipient. But the
cost lands on the default install, where naming is the thing nobody did yet.

**What was measured today, and what was not.**

- `ANTIPHON_NAME` *is* forwarded by Codex to the `antiphon mcp` server it
  spawns: a session started as `ANTIPHON_NAME=probe codex exec` had a live MCP
  child carrying `ANTIPHON_NAME=probe` in its environment. This closes the
  open question in the doctor entry above, which could only verify that the
  `env_vars` line asks for the forward.
- In that same `codex exec` probe no registry record appeared at all, named or
  not. Not root-caused: `register_codex_peer` gives up silently when
  `peers.owner_key()` cannot identify the owning Codex process, and `codex
  exec` is not an interactive session. Whether an interactive
  `ANTIPHON_NAME=x codex` registers before its first turn is **unmeasured**,
  and it is the measurement this entry needs first.

**The option, and why it is not the guess the project refuses.** The Codex hook
receives `session_id` at `SessionStart`, before any turn — the same id
`codex queue --thread` needs. Registering an unnamed session under that id
(displayed as a short prefix) would make two unnamed terminals distinguishable
and addressable, and would make a terminal addressable the moment it opens.
That is the host's own identity arriving in the host's own payload, not a
choice the bridge invents. The reason it was not done is recorded in the
automatic-peer-identity entry below, and most of that caution is about the
*Claude* side, where identity needs `claude agents --json`.

**The decisions this needs, and they are product decisions.** Whether an
id-shaped peer should be addressable at all or only *visible* (so a refusal can
say "two Codex sessions are live, name them"); whether the display is the id, a
short prefix, or a generated word; whether an unnamed peer may receive a bare
message when it is provably the only one; and whether the Claude side stays
asymmetric until its own identity source is proven. A visible-but-unaddressable
peer is the smallest step that fixes the worst symptom — a refusal that says
*why* rather than a silent delivery to the wrong terminal.

## P2 — Automatic peer identity

Aliases are intentionally explicit in the first multi-peer release. A later
release may make unnamed peers visible, but only after both writers derive the
same identity on every supported host. There must be no user-settable owner-key
override, no short-id collision, and no “newest session” fallback once more than
one candidate is known.

There is a concrete Claude-side lead, not yet a contract. On Claude Code
2.1.251, `claude agents --json --cwd <project>` locally returned the active
interactive session with `pid`, exact `cwd`, `sessionId` and a generated `name`,
and the channel server's ancestor chain reached that pid. Before using it:

- feature-detect the command and schema; help text calls this background-agent
  management even though JSON currently includes interactive sessions;
- measure the MCP startup race and use a bounded wait, never “newest” as a
  fallback while the session has not appeared yet;
- fail anonymous when the server is orphaned or its ancestry cannot be joined
  to exactly one entry;
- treat `sessionId` as the identity candidate and the generated `name` only as
  untrusted display metadata until uniqueness and lifetime are documented;
- prove the equivalent Codex MCP/hook join before changing the product rule —
  a Claude-only automatic name would restore the asymmetry this release removed;
- keep an explicit `ANTIPHON_NAME` as the deliberate override and test upgrade,
  collision and mixed-version behaviour.

## P2 — Cross-vendor managed workers

A user should be able to tell a live Claude session “have Codex do this”, or a
live Codex session “have Claude review this”, without manually opening another
terminal and without making the foreign agent look like a native subagent. The
right abstraction is an **Antiphon-managed foreign worker**: the parent agent
can delegate to it and follow its lifecycle, but every event and result still
names the actual Claude or Codex session that produced it. This preserves the
bridge's identity invariant; an absent or ambiguous worker is refused rather
than guessed.

The first safe shape is:

- expose a small `delegate`, `status`, `result` and `cancel` lifecycle, with a
  stable task id and explicit worker session id;
- return immediately after acceptance by default, so the parent can continue
  working and collect the result later;
- label every update and artifact as coming from the foreign worker, never as
  the parent agent's own reasoning or work;
- give every write-capable task its own Git worktree; a worker must not edit in
  the parent session's checkout or race another worker over the same files;
- never give the worker a broader permission class than the delegating session
  or an explicit human grant, and never let a worker approve the parent's
  permission requests, merge its own work, or silently widen its sandbox;
- default the cross-agent hop budget to one. Nested delegation is refused unless
  the user explicitly opts into a higher bounded value, so
  Claude → Codex → Claude cannot become an invisible recursive loop;
- make `blocked`, `completed`, `failed`, `cancelled` and timeout outcomes
  explicit, and return reviewable evidence such as the diff and test results
  with a completed write task.

This must be implemented as an Antiphon lifecycle over host adapters, not by
pretending that either host natively spawned the other vendor's model. Each host
already has its own same-vendor nesting story, and neither is a cross-vendor
contract: Claude Code documents that a subagent inherits the main conversation's
MCP tools and may itself spawn subagents up to a configurable depth
(`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, set to `1` to turn nesting off), and on
this machine Codex CLI 0.151.0 advertises `multi_agent` as a stable feature while
the generated App Server schema mentioning `spawnAgent` is still experimental.
Neither establishes a stable way for one vendor to spawn the other's model, and
this feature must not depend on one without version detection and a tested
fallback. The bounded-depth precedent is worth copying rather than reinventing:
a documented, configurable limit is exactly the shape the hop budget above takes.

### Decisions still required

- Whether `delegate` may target an already-running named peer, always creates a
  fresh managed worker, or exposes both modes explicitly.
- Whether managed workers are one-task ephemeral sessions or can be resumed,
  and what expiry, cleanup and storage quotas apply.
- Which host adapters are supported first, and whether an unavailable native
  worker API may fall back to a documented CLI/SDK subprocess.
- Which task classes may run without another user confirmation, and who may
  accept a worker's patch or merge it after deterministic checks pass.
- Whether a synchronous wait mode is worth exposing in addition to the safer
  asynchronous default.

No claim is made yet that a Claude worker can appear in Codex's native agent UI,
or that a Codex worker can appear in Claude Code's native subagent UI. That UX
would be optional integration work; the portable contract is Antiphon's own
named worker, task lifecycle and evidence trail.

## Observed, not adopted — Claude Code's per-session messaging socket

Measured on one macOS machine, 2026-08-30: a Claude Code session exports
`CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN` and
`CLAUDE_CODE_SESSION_ID` into the processes it starts, including hooks and
stdio MCP servers. The socket path pointed at a real Unix domain socket under a
temporary directory, the token was a 32-character string, and the session id was
a UUID. Nothing was connected to and no token value was read.

This is recorded because it is easy to find and tempting to use, and because the
temptation should be answered once rather than every time somebody notices it.
It is not documented, its path is named after a process id, and it exists on one
side of a bridge whose entire purpose is the asymmetry between two hosts —
Codex CLI has no equivalent. A published package that made an undocumented
internal interface its transport would break silently on the first release that
moved it, and the failure would look like the bridge going quiet.

If a first-party, documented agent-to-agent transport ever ships on both sides,
this is the entry to revisit. Until then Antiphon owns its own sockets.

## P2 — Multi-line Stop markers

Stop markers currently carry one line. Channel tools preserve multi-line text
and are the recommended route for long content. If users need natural-language
Stop blocks, add an explicit delimited syntax with tests for fenced code,
embedded marker text, empty blocks and deduplication; never guess continuation
from arbitrary following prose.

## Shipped — a fresh-user end-to-end run, and doctor scoped to this project

`npm test` drives the servers with an SDK client and fixtures. It was green on
2026-08-31 while three faults were live at once — a null `sender_alias` the
host rejected, a push queued into a thread nobody would read, an upgrade
replaying from byte zero — because each needed a real host, a real CLI or real
transcripts to see. `test/e2e/fresh-user.sh` is the run that sees them: it
packs this tree, installs it into a throwaway prefix, sets up a throwaway
project, drives `claude -p` and `codex exec`, and asserts on what the person
would actually get. It is not part of `npm test` (it needs both CLIs logged
in, the network, and two small model calls) and belongs in the release ritual
between the wrapper census and `npm version`.

What the first run measured, in order of what it changed:

- **doctor judged other projects.** On a correctly set up fresh project it
  printed four ✗ about another project's servers and exited 1, so a healthy
  project could not be told from a broken one. `running:` is now scoped to the
  copy this project's hooks run (PATH's `antiphon`, `realpath`) plus any pid
  registered here; the rest are one `·` count with no paths and no verdict.
  The `codex queue:` note is scoped the same way — only threads one of this
  project's own rollouts records — because Codex's queue is one database for
  every project on the machine. A thread whose rollout has aged out of
  discovery drops out of the note with it.
- **`claude -p` runs hooks in an untrusted workspace; only `permissions.allow`
  is ignored.** Measured: `.antiphon/` appears, the peer registers, `push`
  runs. The first reading of the warning ("this workspace has not been
  trusted") suggested otherwise and was wrong. `--dangerously-skip-permissions`
  does not change it, and `CLAUDE_CONFIG_DIR` isolates the login too, so the
  script grants no trust and asserts it granted none.
- **`codex exec` fires `SessionStart` and never `UserPromptSubmit`**, so a
  non-interactive Codex is never injected with a page. The script therefore
  runs the command `.codex/hooks.json` declares, with the payload the host
  sends; the wiring itself is what `doctor` checks. It also runs the project's
  hooks with no trust prompt and writes nothing to `~/.codex/config.toml`.
- **macOS `$TMPDIR` is a symlink** and both hosts record the resolved path, so
  a harness that does not `pwd -P` watches a transcript directory that never
  fills. Two of the first run's failures were this, not the product.

Named limitations, checked by hand instead: `-p` mode never loads the channel,
so the host's notification schema — the fault that cost the most today — is
still only visible in an interactive session's MCP log; and `codex exec` writes
its rollout at once, so the window where a live thread has no transcript yet
does not reproduce.

## P1 — Re-run the host wrapper census before release

`CLAUDE_HOST_WRAPPERS` and `CODEX_HOST_WRAPPERS` in `lib/antiphon.py` hold
exactly what a census measured (2026-08-30; re-run 2026-08-31 before 0.3.1,
which moved `ide_opened_file` into the Codex set on 4 directly inspected
records; re-run 2026-08-31 before 0.3.2 — 991 Claude text blocks in 86 files,
1,060 Codex in 134, nothing outside either set, no change), and nothing else. They will go
stale as each host adds, renames or drops its own wrapper tags, and the
obligation to re-measure must not live only in a planning document that ships
nowhere. Re-run it before every release:

- count every `role: user` text record whose text opens with `<`, split by
  side, each one carrying its `promptSource` value (or its absence);
- for every opening tag that turns up, decide host bookkeeping or a person's
  own words before touching either set — a tag seen on only one side stays out
  of the other's, the way `local-command-caveat` did until it was measured;
- update the sets, the measurement comment above `CLAUDE_HOST_WRAPPERS`, and
  this entry's date together, so none of the three can drift from the other
  two.

The asymmetry that governs a doubtful case: a tag missing from a set lets one
stray host line leak into a summary — visible, and cheap to fix by adding it.
A tag wrongly present deletes a person's message — silently, with nothing left
behind to notice it happened. When the evidence is thin, leave the tag out.
