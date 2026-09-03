# 0.5.0 — melting the backlog, and the token cost of a bridge

**Date:** 2026-09-02, evening. **Mandate:** the user asked, before leaving the
machine, for every open backlog item to be closed, tested, and for the bridge to
stop growing the token bill of the sessions it serves. Approval for the queue
was given up front; only pushing, publishing and irreversible state changes
wait for a person. Nothing here is pushed to `origin` or published to npm.

**Reviewer situation.** The Codex peer that reviewed the P0 plan four times has
not answered the fifth revision (`e8625bf3`); its rollouts hold no such review.
Both Codex sessions on this machine run inside the ChatGPT app's app-server,
which keeps no thread-writer locks, so the bridge's automatic Codex census is
`at least 0`. Every phase below therefore closes with an independent read-only
review by a fresh agent, and a Codex review is *attempted* over the bridge and
recorded either way.

## What was measured before deciding anything

Read-only, on the live project, 2026-09-02 21:50–22:40:

| Fact | Value |
|---|---|
| Claude page reader backlog, drained in memory from the live cursor | **>400 pages** (cap hit), 2.6 MB rendered, 2,436 messages; every page carries 31 August content |
| The same backlog bounded to the last 24 h / 6 h | 21 pages / **6 pages** |
| Codex reader, last 24 h / 6 h | 15 / 6 pages |
| `[external_agent_tool_call: …]` / `[external_agent_tool_result]` assistant records in project rollouts (the ChatGPT app relaying an external agent's tool traffic) | 8,936 records, 11.0 MB — rendered today as `Codex:` speech |
| `<codex_internal_context source="goal">` user records (the app's goal continuation) | 133 records, 931 KB — not in `CODEX_HOST_WRAPPERS`, so they reach Claude's page as `To Codex:` |
| `# AGENTS.md instructions for …` user records (Codex injecting the rule at session start) | 18 records, 32 KB — relayed to Claude verbatim, and the rule is 8 KB now |
| Claude `isCompactSummary` user records | 6 records, 104 KB — 17 KB each, always an oversized record |
| `CLAUDE_RULE` / `AGENTS_RULE` / channel MCP `instructions` | 7,354 / 8,120 / ~6,000 bytes, in every turn or every session; the host truncates the instructions |
| The live `CLAUDE.md` on this project | 2,370 bytes — a 0.3.2-era section; `doctor` does not notice rule drift |

The first row is the whole token argument: a reader that is behind by days
spends ~2,000 tokens per turn on history nobody asked for and never reaches the
live conversation. The existing contract says "repeat rather than skip" for
every untrusted cursor; measured, that contract is what produced the twenty
deaf hours recorded in BACKLOG and the 400-page backlog above.

## Phases and decisions

### 1. P0 — mixed-version endpoint pruning

Plan `docs/superpowers/plans/2026-09-02-mixed-version-birth.md` at `e8625bf3`,
Tasks 1–6 as written: the fingerprint moves to `process_birth: "v1:<start>"`,
one grammar and one selector in both languages, the two-way
`fingerprint_field` capability, the verdict's current-fingerprint rule, doctor's
two notes, the words. Deviation from the plan's Task 6: the Codex review is
attempted, not awaited; the independent review is a read-only agent.

### 2. Token discipline (new backlog item, P1)

**2a. Host records are not speech.** Five shapes, each measured above, each a
host's own bookkeeping:

- `codex_internal_context` joins `CODEX_HOST_WRAPPERS` (tag rule, Codex only).
- A Codex user record beginning `# AGENTS.md instructions for ` whose text
  carries a complete `<INSTRUCTIONS>` fence is a host record.
- A Codex assistant record beginning `[external_agent_tool_call: NAME]` renders
  as a name-only tool line (`external agent: NAME`), never as `Codex:` speech
  (outcome, 2026-09-03: the relay is filtered whole — the measured shape
  carried nothing a reader could act on, and BACKLOG keeps the measurement;
  the name-only line was not built);
  `[external_agent_tool_result]` is a tool output and stays filtered, exactly as
  every other output does. No arguments, no results, no id (nothing retrievable
  backs it).
- A Claude user record with `isCompactSummary: true` is a host record.
- A Claude user record whose whole text is one of the host's interruption
  literals (`[Request interrupted by user]`, `[Request interrupted by user for
  tool use]`) is a host record. Exact equality, so no pasted text can match.

The census utility learns the prefix shapes beside the tag shapes so the release
ritual can see them.

**2b. The page has a horizon.** A page reader never delivers a record older
than `LOOKBACK` (six hours, the window a fresh reader already gets). A
positioned source whose next undelivered record is older than that skips
forward to its first record at or after the horizon; the skipped span is
counted and the page says so on one line: `skipped: N raw bytes of Codex
activity older than 6 hours in K source(s); the transcripts keep it`. Never
silent, never a broken record, never applied to what is inside the window.
`status`'s `unread` line names the part the horizon will skip. `antiphon
catch-up` stays for "skip to the live edge now". This reverses one documented
sentence in BACKLOG ("skipping is the error this bridge does not accept") for a
measured reason, and the entry says so.

**2c. Shorter agent surfaces.** `CLAUDE_RULE`, `AGENTS_RULE` and the channel
`instructions` keep every fact the contract tests pin and lose the
implementation narrative (v4 adoption, lanes, compaction journal, manifest
retention) that belongs in README. Target: each under 3,000 bytes. (Outcome:
not reached — the pinned contract facts alone fill about 4.5 KB; the measured
sizes and the ceilings are in BACKLOG's token-cost entry.)

**2d. Doctor sees rule drift.** `CLAUDE.md`/`AGENTS.md` whose Antiphon section
differs from the generated rule is `✗ … run antiphon setup`, like the missing
permission it already reports; `setup` and `doctor --fix` rewrite it in place.

### 3. Delivery truth

One ledger, `.antiphon/deliveries/<id>.json`, written by every direct send
(Stop-hook push, `reply_to_codex`, `antiphon_send`): id, sender alias, target
kind and alias, transport, content SHA-256 and size, attachment path if parked,
`sent_at`. No content, no route, no session id.

- **Receipts come from the peer's own transcript.** The page readers already
  walk it. A Codex user record `[Antiphon bridge|channel] Claude: [from=… id=X]`
  proves Codex consumed X; a Claude channel record (`origin.kind == "channel"`,
  `message_id="X"`) proves Claude's host accepted X. The reader marks
  `received_at`; nothing is guessed.
- **The tool says what it did.** `reply_to_codex` answers `queued for Codex
  peer 'x' (id …); Codex reads its queue at its next turn — antiphon status
  shows what is still unread`, never `delivered`. `antiphon_send` keeps
  `delivered to the channel` because the host acknowledged the notification,
  and adds the id.
- **A refused push is shown to the sender.** A Stop-hook refusal writes a
  ledger entry in state `refused` with the reason; the sender's next page opens
  with `your @codex line at HH:MM was not delivered: <reason>` once, then the
  entry is marked reported. This is what would have stopped the "Gönderdim"
  claim measured today.
- **Attachments.** An envelope's receipt is the message's receipt. A read
  receipt is a tool invocation in the peer's transcript naming the attachment
  file (Claude `Read`/`Bash` detail, Codex shell arguments). A read attachment
  is eligible for removal at the next sweep; an unread one keeps its TTL and
  its expiry is reported to the sender's next page. Retry: a send whose content
  digest and recipient match an unexpired parked file reuses that file — one
  attachment, one path, however many envelopes.
- **Reply correlation is advice, not routing.** The bare-reply refusal names
  the most recent unanswered sender (`the last unanswered sender was 'build',
  3 min ago; pass to="build"`). The bridge still never chooses.
- `status` lists deliveries awaiting receipt with age; `doctor` notes them as
  `·` (only Codex can drain its queue).

### 4. Same-vendor bridging

- Markers: `@claude:name` in a Claude reply and `@codex:name` in a Codex reply
  push to the named same-kind peer. Same-kind sends are **always addressed**: a
  bare same-kind marker is refused ("name the peer; a bare same-kind line has
  no meaning").
- Tools: the Claude channel gains `reply_to_claude(text, to)`; Codex's
  `antiphon_send` gains `kind` (`claude` default, `codex`).
- Labels keep kind and alias: `[Antiphon bridge] Codex: [from=… id=…]` into a
  Codex peer, `sender="claude"` in the channel notification into a Claude peer.
- The passive page does **not** carry same-kind activity: it stays the other
  side's transcripts, so page size and the discovery window are unchanged.
- Loops: the bridge forwards nothing automatically, so no bridge-level loop
  exists; the hop budget belongs to managed workers (phase 5).

### 5. Cross-vendor managed workers

The five open decisions are taken in the backlog entry; the lifecycle is
specified. Implementation is the last phase and runs only after phases 1–4 are
merged and reviewed; if it is not built in this campaign the entry says
"designed, not built" and why.

### 6. Release ritual

Host wrapper census re-run (the new prefix shapes included); full suites in the
foreground under both interpreters; `fresh-user.sh` from a temporary worktree at
the exact commit; an independent read-only review per phase and one on the
final SHA; a Codex review attempted over the bridge; `npm version minor` →
0.5.0 with lockfile; **stop before push and publish**.
