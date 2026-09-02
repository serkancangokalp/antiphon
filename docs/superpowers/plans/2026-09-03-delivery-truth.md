# Delivery Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every behavioural red is observed before its product edit; every new guard is mutated once and its named test watched fail.

**Goal:** A sender is told what the bridge actually did — queued, delivered, refused — and later learns from the peer's own transcript whether the words were received and, for an attachment, read; a refused Stop-hook line is shown to the agent that wrote it; a resent oversized message reuses its parked file; a bare-reply refusal names the sender most likely meant; and a Codex session the host keeps no lock for is addressed as *unproven* rather than refused as dead.

**Architecture:** One ledger, `.antiphon/deliveries/<id>.json`, written by every direct send and updated with receipts by the page readers, which already walk the peer's transcript and already recognise the bridge's own injected messages. Liveness of a Codex thread gains a third answer (`None`: no lock file, unknown) beside proven-live and proven-dead, and a bare send goes to the newest unknown candidate with the word *unproven* on it. Surfaces (tool results, the sender's next page, `status`, `doctor`) report from the ledger. The ledger is written only from the hook, the tools and the Stop hook — never from `status` or `doctor`.

**Tech Stack:** Python 3.9 stdlib, `unittest`; Node ESM for the `reply_to_codex` result text and its test. A new module `lib/ledger.py` ships in `package.json` `files`.

**Spec:** `docs/superpowers/specs/2026-09-02-final-campaign-design.md` §3.

## Global Constraints

- Work in `.worktrees/delivery-truth` on branch `delivery-truth` from `main` after the token-discipline merge. One writer.
- Never touch the live `.antiphon/` of the repository; fixtures live under `tempfile`. The live queue database `~/.codex/queue_*.sqlite` is read only through `sqlite3 -readonly` as doctor already does.
- The ledger holds no message content, route, session id, digest of a session or socket path: id, aliases (public), kind, transport, proof class, timestamps, content SHA-256 and size, attachment basename, state, reason (redacted), and a 60-character preview of the sender's own marker line (their own words, in their own project).
- Both interpreters run the suite; the Node suite runs alone.
- No merge, push, version bump or publish inside this plan.

---

## Measured facts this plan relies on (2026-09-02/03, read-only)

- Codex CLI 0.152.1 `codex exec` creates `~/.codex/thread-writer-locks/<id>.lock` at start and removes it at exit; two open sessions hosted by the ChatGPT app's app-server (`/Applications/ChatGPT.app/…/codex app-server`, parent of both `antiphon mcp` servers) have **no** lock file, and their `session_meta` says `originator: codex-tui, source: cli` — indistinguishable from a closed CLI thread by anything on disk. `codex_thread_alive` today answers `False` for a missing file whenever the directory exists, so `reply_to_codex` refused with *"the Codex sessions recorded in this directory are not running"* while both were open.
- `codex queue --thread <app thread> --message …` was accepted (`queued_items` row written) and **not consumed within 120 s**; the rollout did not change. Two rows from 31 August (threads `01a0573e…`, `01a04f93…`) are still in the queue. The tool nevertheless says *delivered*.
- The sender's Stop-hook refusal prints on exit-0 stderr, which reaches a debug log and not the agent: the Codex peer said "Gönderdim" for a marker that was never delivered.
- A Codex rollout records a bridge delivery as a `response_item/message role=user` whose text starts `[Antiphon bridge] Claude: [from=<alias> id=<uuid>] …` or `[Antiphon channel] Claude: …` (332 such records on this machine). Claude's transcript records a channel delivery as a `user` record with `origin: {"kind": "channel", "server": "antiphon"}`, `isMeta` set, and content `<channel source="antiphon" sender="codex" sender_kind="agent" sender_alias="…" message_id="<uuid>">…</channel>`.
- `attachment_dir(cwd)` is `.antiphon/messages/`; a parked file is `{uuid4}.txt` (`ATTACHMENT_NAME`), header `[Antiphon attachment from=… id=… sha256=… bytes=…]`, and `sweep_attachments` deletes by mtime alone.

---

### Task 1: The ledger module

**Files:**
- Create: `lib/ledger.py`
- Modify: `package.json` (`files` gains `lib/ledger.py`)
- Test: `test/test_ledger.py` (new)

**Interfaces (produces):**
```python
LEDGER_VERSION = 1
DELIVERY_STATES = ("sent", "refused")
ledger_dir(cwd) -> str                                  # .antiphon/deliveries
record_sent(cwd, delivery_id, *, sender, to_kind, to_alias, transport,
            proof, sha256, size, attachment=None) -> bool
record_refused(cwd, delivery_id, *, sender, to_kind, to_alias, reason,
               preview) -> bool
mark_received(cwd, delivery_id, at) -> bool             # idempotent, keeps the earliest
mark_read(cwd, attachment_basename, at) -> bool         # every entry naming that file
mark_reported(cwd, delivery_ids, at) -> None
mark_expired_unread(cwd, attachment_basename, at) -> None
read_entry(cwd, delivery_id) -> dict | None             # validated or None
entries(cwd) -> list[dict]                              # validated, oldest first
pending_notices(cwd, side, alias) -> list[(delivery_id, text)]
awaiting_receipt(cwd, now) -> list[dict]                # sent, no receipt
last_unanswered_sender(cwd, to_kind, to_alias, now) -> (alias, age_seconds) | None
reusable_attachment(cwd, sha256, to_kind, to_alias, now) -> basename | None
```
`sender` and aliases are public strings (`<unnamed>` allowed); `proof` ∈ `("live", "unproven", "registered", "channel")`; `transport` ∈ `("queue", "channel")`; times are epoch floats. Files are written atomically (`tmp` + `os.replace`), mode 0600, under a 0700 directory created with the same `_sound_store`-style checks (no symlink following, leaf owned). A malformed entry is skipped, never raised. Entries older than `LEDGER_TTL = 7 * 24 * 3600` are removed by `prune(cwd, now)`, which the hook calls beside the attachment sweep.

- [ ] **Step 1: Failing tests** — `test_ledger.py`: write/read round trip and validation (missing keys, wrong types, a 65,000-digit integer → skipped); `mark_received` keeps the earliest time and is idempotent; `mark_read` marks every entry naming the file; `pending_notices` returns refused-unreported entries for this side and alias (and `<unnamed>` entries for an unnamed session), each text naming the marker preview, the time and the redacted reason, and nothing for a reported one; `awaiting_receipt` excludes received and refused; `last_unanswered_sender` returns the newest sender whose delivery to me has no later delivery from me to them, with age; `reusable_attachment` returns a basename only when sha256, recipient and an unexpired `sent` entry match and the file exists; `prune` removes entries older than the TTL and leaves the rest; no entry ever contains a socket path, a canonical UUID other than the delivery id, or the content (assert on the file bytes).
- [ ] **Step 2: Red** — `ModuleNotFoundError: ledger`.
- [ ] **Step 3: Implement** `lib/ledger.py` as specified; validation mirrors `peers._read_record` (bounded integers, duplicate keys refused, size ceiling 64 KiB).
- [ ] **Step 4: Green; mutate** (validation dropped → the malformed-entry test; `mark_received` overwrites the earlier time → its test; `reusable_attachment` ignores the recipient → its test).
- [ ] **Step 5: Commit** — `Ledger: what was sent, refused, received and read, in one directory`.

---

### Task 2: Codex liveness has three answers; a bare send goes to the newest unknown as unproven

**Files:**
- Modify: `lib/antiphon.py` — `codex_thread_alive` (~:4441), `codex_session_id` (~:4539), `_legacy_target` (~:4974), `ResolvedTarget`/`_resolve_target` (~:5018), `send_to_codex` (~:5162), `_queue_codex` unchanged
- Test: `test/test_antiphon.py` — `LiveCodexTargetTest`

**Interfaces:**
- `codex_thread_alive(session)` → `True` (lock held), `False` (lock file present, not held), `None` (no lock file, or no locks directory).
- `codex_session_id(cwd)` → `(session_id, proof)` with `proof ∈ ("live", "unproven")`, or `(None, None)`; `ResolvedTarget` gains `proof`; `send_to_codex` returns `(True, proof)` on success.

- [ ] **Step 1: Failing tests** in `LiveCodexTargetTest`:
```python
    def test_no_lock_file_is_unknown_not_dead(self):
        directory = self.locks()
        with patch.object(antiphon, "CODEX_THREAD_LOCKS", directory):
            self.assertIsNone(antiphon.codex_thread_alive("no-such-thread"),
                              "the host may keep no lock for an open thread")
            open(os.path.join(directory, self.DEAD + ".lock"), "w").close()
            self.assertIs(antiphon.codex_thread_alive(self.DEAD), False,
                          "a lock file nobody holds is a thread that is gone")

    def test_a_bare_send_reaches_the_newest_unknown_thread_as_unproven(self):
        directory = self.locks()
        with tempfile.TemporaryDirectory() as sessions, \
             patch.object(antiphon, "CODEX_SESSIONS", sessions), \
             patch.object(antiphon, "CODEX_THREAD_LOCKS", directory):
            self.rollouts(sessions, self.LIVE, self.DEAD)
            self.assertEqual(antiphon.codex_session_id(self.CWD), (self.DEAD, "unproven"))
            self.hold(directory, self.LIVE)
            self.assertEqual(antiphon.codex_session_id(self.CWD), (self.LIVE, "live"),
                             "a proven-live thread beats a newer unknown one")

    def test_every_candidate_proven_dead_is_still_refused(self):
        directory = self.locks()
        with tempfile.TemporaryDirectory() as sessions, \
             patch.object(antiphon, "CODEX_SESSIONS", sessions), \
             patch.object(antiphon, "CODEX_THREAD_LOCKS", directory):
            self.rollouts(sessions, self.DEAD)
            open(os.path.join(directory, self.DEAD + ".lock"), "w").close()
            address, detail = antiphon.resolve_target(self.CWD, "codex")
            self.assertIsNone(address)
            self.assertEqual(detail.refusal_class, "no-peer")
            self.assertIn("not running", detail)
```
Update `test_a_bare_target_is_the_newest_running_session_not_the_newest_file` and `test_no_running_session_is_refused_rather_than_queued_into_a_dead_one` to the new answers (the second now needs an unheld lock file to be refused).
- [ ] **Step 2: Red.**
- [ ] **Step 3: Implement.** `codex_thread_alive`: `except OSError: return None` on the open. `codex_session_id`: `live = [sid for sid in candidates if alive[sid] is True]`; if live → `(live[0], "live")`; `unknown = [sid for sid in candidates if alive[sid] is None]`; if unknown → `(unknown[0], "unproven")`; else `(None, None)`. `_legacy_target` returns `(session, "")` with the proof carried on `ResolvedTarget.proof`; the "not running" refusal now says *"every Codex session recorded in this directory is proved gone (its writer lock is unheld)"*. `send_to_codex` returns `(True, target.proof or "registered")`.
- [ ] **Step 4: Green; mutate** (`None` → `False` for a missing file → the first test; the live-beats-unknown preference dropped → the second).
- [ ] **Step 5: Commit** — `A Codex thread the host keeps no lock for is unknown, not dead; a bare send reaches it as unproven`.

---

### Task 3: Every direct send writes the ledger; the tools say what they did

**Files:**
- Modify: `lib/antiphon.py` — `reply()` (~:5974), `_send_tool` (~:6240), `push.deliver` (~:4700), `_spill`/`_spill_locked` (attachment basename into the ledger)
- Modify: `lib/channel.mjs` — the `reply_to_codex` result text (~:348-355) reads Python's stdout JSON
- Test: `test/test_antiphon.py` (the classes holding `reply`/`_send_tool`/`push` tests: grep `def test_.*reply\b`, `_send_tool(`, `antiphon.push(`), `test/channel.test.mjs` (`aRefusedTransportTellsTheAgentWhereTheWordsTravel` is the neighbour; add `theReplyToolSaysQueuedNotDelivered`)

**Interfaces:**
- `reply()` prints one JSON line on success: `{"queued": true, "id": <id>, "proof": <proof>, "to": <alias|null>, "attachment": <basename|null>}`.
- `reply_to_codex` result text: `Queued for Codex peer 'x' (id …): Codex reads its queue at its next turn; run antiphon status to see whether it was received.` (`peer 'x'` → `the newest Codex session` when bare; `(unproven: the host keeps no lock for that thread)` appended when proof is `unproven`).
- `antiphon_send` result text: `Delivered to the Claude Code channel (id …); antiphon status shows when Claude's transcript received it.`
- `push.deliver` records `sent` on success and `refused` (reason, 60-char preview) on failure; the line it prints is unchanged.

- [ ] **Step 1: Failing tests** — Python: after `reply()`/`_send_tool`/`push` with the transport mocked, `ledger.entries(project)` holds one entry with the expected fields (`transport`, `proof`, `to_alias`, `sha256` of the outgoing bare text, `attachment` basename when parked) and the stdout JSON carries the same id; a refused push writes a `refused` entry with the reason redacted and the preview; the existing "Channel reply delivered" assertion in `channel.test.mjs` becomes the queued wording and the unproven suffix is asserted with a codex stub whose queue accepts.
- [ ] **Step 2: Red.** **Step 3: Implement.** **Step 4: Green; mutate** (ledger write skipped on the push refusal → its test; the tool text says delivered → the Node test). **Step 5: Commit** — `Every direct send is on the ledger, and the tools say queued, delivered or refused`.

---

### Task 4: Receipts from the peer's own transcript; the sender's next page carries what it needs to know

**Files:**
- Modify: `lib/antiphon.py` — `codex_events`/`claude_events` (`receipts=None` collector), `hook()` (~:4104; write receipts after delivery, prepend notices, `ledger.prune`), `_mcp_serve` `antiphon_read` branch (receipts after the result is written), `_render_page` unchanged
- Test: `test/test_antiphon.py` — `AntiphonTest` (readers), `PagedDeliveryTest` or the class driving `hook()` end to end

**Interfaces:**
- Readers append `("received", delivery_id, epoch)` for a Codex user record whose text is self-injected and carries `id=<uuid>`, and for a Claude record with `origin.kind == "channel"` whose content carries `message_id="<uuid>"` (read before the `isMeta` filter; the record is still not an event); `("read", basename, epoch)` for a tool invocation whose arguments name `.antiphon/messages/<uuid>.txt` (Claude `file_path`/`command`/`pattern`, Codex argument string).
- `hook()`: `notices = ledger.pending_notices(cwd, side, claimed_alias(...))`; the injected context is `"\n".join(notices) + "\n" + text` when both exist, notices alone when the page is empty; after `_deliver` succeeds, `ledger.mark_reported(cwd, ids)` and `ledger.record_receipts(cwd, receipts)`; before the page, `ledger.prune(cwd)`.
- Notice text: `Antiphon: your @codex line at 21:44 ("run the suite…") was not delivered — <reason>` and `Antiphon: the attachment you sent to Codex at 21:44 expired unread after 7 days`.

- [ ] **Step 1: Failing tests** — reader fixtures with a self-injected Codex user record and a Claude channel record produce receipts and no events; a hook run against a fixture transcript marks the entry received and the `status` block shows it; a refused entry appears once at the top of the next hook page for the same side and never again; an empty page with a pending notice is still delivered; `status` never writes a receipt (snapshot the ledger directory bytes around `antiphon.status()`).
- [ ] **Step 2: Red.** **Step 3: Implement.** **Step 4: Green; mutate** (receipt collection skipped → the reader test; notice not marked reported → the "never again" test). **Step 5: Commit** — `Receipts come from the peer's own transcript; the sender's next page says what was refused`.

---

### Task 5: Attachments — read receipts, reuse on resend, honest expiry

**Files:**
- Modify: `lib/antiphon.py` — `_spill_locked` (reuse), `sweep_attachments` (ledger-aware), `attachment_report`/`status` (counts read/unread)
- Test: `test/test_antiphon.py` — the attachment classes (grep `write_attachment`, `sweep_attachments`)

- [ ] Reuse: a second `_spill` with the same content SHA-256 to the same recipient within the TTL returns an `_Attachment` naming the existing file (its header keeps the first id); the ledger entry for the new delivery names the same basename; the store holds one file.
- [ ] Sweep: an attachment past its TTL is deleted as today; if no ledger entry naming it has `read_at`, every such entry gets `expired_unread` and the sender's next page reports it (Task 4's notice); an attachment with a read receipt is eligible after `ATTACHMENT_READ_GRACE = 3600` seconds instead of the TTL, announced as `read and collected`.
- [ ] README §Limits: the acknowledgement/retry paragraph becomes what shipped (the contract test `test_attachment_limits_match_code` pins `acknowledgement … not` today — it changes in the same commit to pin `read receipt`, `reused`, `expired unread`). BACKLOG: the attachments entry closes its two open items.
- [ ] Commit — `Attachments: read receipts from the peer's transcript, one file per resend, an expiry the sender hears about`.

---

### Task 6: Reply correlation is advice inside the refusal

**Files:**
- Modify: `lib/antiphon.py` — `_resolve_target` (the `len(live) > 1` and the Codex single-named refusals) gains an optional `sender` argument threaded from `reply()`/`_send_tool()`
- Test: `test/test_antiphon.py` — the resolver tests (grep `address one by name`)

- [ ] Failing test: with two live Codex peers and a ledger entry from `build` to this Claude alias five minutes ago with no later reply, the bare refusal ends `; the last unanswered sender was 'build' (5 min ago): pass to="build"`; with no such entry the refusal is byte-identical to today; an explicit `to` never consults the ledger (mutation seam: `last_unanswered_sender` raising).
- [ ] Implement; green; mutate; commit — `A bare-reply refusal names the sender most likely meant; the bridge still never chooses`.

---

### Task 7: status and doctor read the ledger

- [ ] `status` prints `Deliveries: N awaiting receipt (oldest 12 min), M refused (reported)` after the attachments line; `doctor` notes `·` deliveries awaiting receipt older than 10 minutes with the remedy (`the peer has not read it yet; if its session is closed, address another`), keeps the existing queue note, and stays read-only (snapshot test).
- [ ] Commit — `status and doctor read the ledger`.

---

### Task 8: Words, then verification on the exact SHA

- [ ] BACKLOG: `## P1 — reply_to_codex can report success while the peer receives nothing (fixed)`, the attachments entry's open items closed, `## P2 — Reply correlation (advice)` closed with the decision, the liveness change under the "bare push goes to a running Codex" entry, the measured facts above. README: the Push table's wording (`queued`), the ledger directory in §How it works, §Limits. The generated rules and channel instructions gain one sentence each (`reply_to_codex` says queued; `antiphon status` shows receipt) within their byte ceilings.
- [ ] Contract tests for the new words.
- [ ] Both Python suites, `npm test`, statics, `fresh-user.sh` from a temporary worktree at the SHA, an independent read-only review; a Codex review attempted over the bridge (now as an *unproven* queue, which is itself the measurement).

## Self-review

- **Spec coverage:** §3 receipts → Task 4; tool truth → Task 3; refused push visible → Tasks 3–4; attachments → Task 5; correlation advice → Task 6; status/doctor → Task 7; the liveness finding (not in the spec, measured after it) → Task 2.
- **Placeholders:** none. **Types:** `codex_session_id` returns a pair everywhere it is called (`_legacy_target`, its tests, `test_e2e_marker_probe` if it calls it — grep); `send_to_codex`'s success detail is a proof string and no caller compares it to `""`.
- **Open for critique:** whether a `None` liveness should deliver at all (the alternative — refuse — is what shipped and what silently killed the app road); whether a 60-character preview of the sender's own marker belongs in the ledger.
