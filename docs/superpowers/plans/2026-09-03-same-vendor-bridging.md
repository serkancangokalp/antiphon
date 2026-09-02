# Same-vendor bridging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude session can address another Claude session (`@claude:name`, `reply_to_claude`) and a Codex session another Codex session (`@codex:name`, `antiphon_send(kind="codex")`), always by name, over the transports that already exist — and every surface says so.

**Architecture:** The registry, both transports and the resolver are already kind-aware; what is missing is the surfaces and every sentence that assumes "the other side". Each side's Stop hook gains a same-kind pass that parses the same-kind marker out of its own transcript and refuses the bare form; the two direct tools gain a same-kind road; the Claude channel server passes `sender_kind` through so a Claude peer's words arrive labelled `sender="claude"`; a Codex peer's words arrive under `[Antiphon bridge] Codex:` / `[Antiphon channel] Codex:`, which the self-injection guard and the receipt collector already understand once the labels exist. The passive page stays the other kind's transcripts. Same-kind receipts come from the receiver's own hook reading the tail of its own transcript, so a Claude-only or Codex-only project still gets them. The bridge forwards nothing automatically, so no bridge-level loop exists.

**Tech Stack:** Python 3.9+ (`lib/antiphon.py`, `lib/ledger.py`), Node (`lib/channel.mjs`), unittest + `node --test`.

**Spec:** `docs/superpowers/specs/2026-09-02-final-campaign-design.md` §4.

## Global Constraints

- Same-kind sends are always addressed. A bare same-kind marker or tool call is refused with `not delivered: a bare @claude line from a Claude session has no meaning — name the peer (@claude:name)` (and the Codex mirror), recorded on the ledger as refused so the sender's next page says it.
- A session never addresses its own alias: `not delivered: '<alias>' is this session's own alias`.
- The passive page does not carry same-kind activity; `EVENT_LIMIT`, `PAGE_BUDGET`, `PAGE_HORIZON` untouched.
- Labels: `PUSH_LABEL_CODEX = "[Antiphon bridge] Codex:"`, `CHANNEL_LABEL_CODEX = "[Antiphon channel] Codex:"`; all four labels in `_SELF_INJECTION_PREFIXES`.
- Surfaces stay inside their ceilings (`test_the_agent_surfaces_stay_small`: CLAUDE_RULE 5,000, AGENTS_RULE 5,500). If a sentence cannot fit, the ceiling moves by the measured amount in the same commit and the token entry says so.
- Every new test is born with a mutation gate: apply the mutation, watch the named test fail, restore byte-exact.
- Never `git push`, never `npm publish`.

---

### Task 1: Labels for a Codex sender; the guard and the receipt collector know them

**Files:** Modify `lib/antiphon.py` (beside `PUSH_LABEL`/`CHANNEL_LABEL`); Test `test/test_antiphon.py` (`DeliveryReceiptTest`, a new `SameVendorTest`).

- [ ] Constants `PUSH_LABEL_CODEX`, `CHANNEL_LABEL_CODEX`; `_SELF_INJECTION_PREFIXES` built from all four; `SIDE_LABELS = {"claude": (PUSH_LABEL, CHANNEL_LABEL), "codex": (PUSH_LABEL_CODEX, CHANNEL_LABEL_CODEX)}`.
- [ ] Tests: `_is_self_injected` true for `[Antiphon bridge] Codex: …` and `[Antiphon channel] Codex: …`; `codex_events` does not render a `[Antiphon bridge] Codex: [from=build id=X] ship it` user record and reports `("received", X)`; `claude_events` unchanged.
- [ ] Mutation: drop the Codex labels from the prefixes → the filter test red.

### Task 2: `sender_kind` travels to a Claude peer

**Files:** `lib/antiphon.py` (`send_to_claude(cwd, text, alias=None, sender_alias=None, message_id=None, sender_kind=None)`), `lib/channel.mjs` (socket handler ~line 895), `test/channel.test.mjs`, `test/test_antiphon.py`.

- [ ] Python: the socket request carries `"sender_kind": "claude"` only when `sender_kind == "claude"`; absent otherwise (an old listener ignores unknown keys; a new listener treats absence as codex).
- [ ] Node: `sender: payload.sender_kind === "claude" ? "claude" : "codex"` in the notification meta; the instructions say a `sender="claude"` event is another Claude session's words.
- [ ] Tests: Python — a fake socket captures the JSON; with `sender_kind="claude"` the key is present, without it absent. Node — a socket payload with `sender_kind: "claude"` yields a notification whose meta.sender is `claude`; without it `codex`; `sender_kind: "human"` is `codex` (only the exact word counts).
- [ ] Mutations: Python always omits the key → red; Node ignores the field → red.

### Task 3: Same-kind Stop markers

**Files:** `lib/antiphon.py` (`push`, new `_push_same_kind`, `_record_delivery(..., key=None)`), `test/test_antiphon.py` (`SameVendorTest` using `ToolRecipientTest._stop`-style harness).

- [ ] `SAME_KIND_KEY = "last_pushed_{kind}_same"` — the sender's own cursor, a key of its own so the unnamed shared-cursor install cannot collide with the other side's `last_pushed_*`.
- [ ] In `push(target)`, after the cross-kind work (whatever it returned), run `_push_same_kind(cwd, side, reply_text, turn_key, input_data)` where `side = sender_side(target)`; it parses `group_by_recipient(side, reply_text)` (the same-kind marker out of the same transcript text), refuses `None` recipients and the sender's own alias (ledger `record_refused`, stderr line), and delivers the rest through `deliver_batches` under `SAME_KIND_KEY` with `deliver` choosing `send_to_claude(..., sender_kind="claude")` for a Claude sender and `send_to_codex(cwd, f"{PUSH_LABEL_CODEX} {queue_label(who, attempt, can_reply)} {outgoing}", recipient, sender=who)` for a Codex sender; ledger `record_sent`/`record_refused` as the cross-kind path does. The cross-kind path's exit code stands; a same-kind failure prints and returns 0 like every Stop refusal.
- [ ] Tests: Claude→Claude (`@claude:api hello` in a Claude transcript, `push("codex")`, `send_to_claude` mocked → called with alias `api`, `sender_kind="claude"`, ledger to_kind claude sender ui); Codex→Codex (`@codex:review ship it` in a Codex transcript, `push("claude")`, `send_to_codex` mocked → message starts with `[Antiphon bridge] Codex: [from=build id=`); a bare `@claude hello` from Claude → refused entry with the "no meaning" reason and a notice on the next page; `@claude:ui` from ui → refused as own alias; a second Stop with the same turn key does not resend; a reply with both `@codex fix` and `@claude:api look` delivers both.
- [ ] Mutations: same-kind pass skipped → red; bare accepted → red; own alias accepted → red; dedupe key shared with the cross-kind key → the mixed test red.

### Task 4: `reply_to_claude` on the channel; `reply(kind="claude")` in Python

**Files:** `lib/channel.mjs` (tools list, CallTool handler), `lib/antiphon.py` (`reply`), `test/channel.test.mjs`, `test/test_antiphon.py`.

- [ ] Python `reply()`: `kind = input_data.get("kind") or "codex"`; not in `("codex", "claude")` → `reply: kind must be codex or claude`, exit 1; for claude: `to` required (`reply: to is required — a reply to another Claude session names its peer`), own alias refused, `send_to_claude(cwd, outgoing, to, sender_alias=who, message_id=message_id, sender_kind="claude")`, oversized → `_spill(..., recipient=("claude", to))`, `_record_delivery` under `SAME_KIND_KEY`, ledger (channel/channel), stdout JSON `{"queued": false, "delivered": true, "id", "to", "attachment", "text": "Delivered to the Claude Code peer '<to>' (id …); antiphon status shows when its transcript received it."}`.
- [ ] Node: tool `reply_to_claude` (`text`, `to` required; description says another Claude session, always named); handler validates `to` non-empty, passes `{text, to, kind: "claude", sender_alias, sender_reachable}` to `antiphon.py reply`, returns Python's `text`; missing `to` → error `to is required: a reply to another Claude session names its peer`.
- [ ] Tests: Python — kind claude happy path (mock `send_to_claude`), missing `to`, bad kind, own alias; Node — `listTools` has both reply tools; `reply_to_claude` without `to` errors before spawning; with `to` the stub's stdout text is returned verbatim.
- [ ] Mutations: kind ignored (always codex) → red; `to` not required → red; Node tool missing → red.

### Task 5: `antiphon_send(kind="codex")`

**Files:** `lib/antiphon.py` (`TOOLS`, `_send_tool(cwd, text, to=None, sender=None, kind="claude")`, `_mcp_serve`), `test/test_antiphon.py`.

- [ ] Schema: `kind` enum `["claude", "codex"]`, default claude, description "which kind of peer; codex reaches another Codex session and then `to` is required".
- [ ] kind codex: `to` required (`_tool_error("to is required: a message to another Codex session names its peer")`), own alias refused, message `f"{CHANNEL_LABEL_CODEX} {queue_label(who, message_id, True)} {outgoing}"`, `send_to_codex(cwd, message, to, sender=who)`, oversized → `_spill(recipient=("codex", to))`, `_record_delivery` under `SAME_KIND_KEY`, ledger (queue, proof), result `queued_words(to, message_id, proof, attachment)`.
- [ ] Tests: happy path (mock `send_to_codex` → `(True, "live")`; result starts `Queued for Codex peer 'review'`; ledger to_kind codex transport queue proof live sender build); missing `to`; bad kind; own alias; `_mcp_serve` passes `kind`.
- [ ] Mutations: kind ignored → red; `to` not required → red; label missing → the self-injection round-trip test red.

### Task 6: Same-kind receipts from the receiver's own transcript

**Files:** `lib/antiphon.py` (`hook`, new `_own_transcript_receipts(side, transcript_path)`), `test/test_antiphon.py` (`DeliveryReceiptTest`).

- [ ] On every `UserPromptSubmit` hook with a `transcript_path` that exists: read `tail_lines(transcript_path)` (TAIL_BYTES), parse each JSON record, run the side's own collector (`_collect_claude_receipts` on the Claude side, `_collect_codex_receipts` on the Codex side), `ledger.record_receipts`. Idempotent; the earliest sighting wins; costs one bounded read.
- [ ] Tests: hook("claude") whose transcript tail holds a channel record with `message_id=X` where X is a ledger entry `to_kind="claude"` from a Claude sender → received; hook("codex") whose rollout tail holds `[Antiphon bridge] Codex: [from=build id=X]` → received; a missing transcript path is silent.
- [ ] Mutation: the scan removed → both red.

### Task 7: Words

**Files:** `README.md`, `BACKLOG.md`, `lib/antiphon.py` (rules, tool descriptions), `lib/channel.mjs` (instructions, tool descriptions), `test/test_contracts.py`.

- [ ] README: Push table gains `Claude → Claude | @claude:name | reply_to_claude(to=…)` and `Codex → Codex | @codex:name | antiphon_send(kind="codex", to=…)`; a "Same-vendor" paragraph under *Many peers* (always addressed; labels; the passive page does not carry it; receipts from the receiver's own hook; no automatic forwarding, so no loop); §Limits bullet.
- [ ] Rules: AGENTS_RULE — "Another Codex session's words reach you as `[Antiphon bridge] Codex:` / `[Antiphon channel] Codex:`; reach one with `@codex:name` or `antiphon_send(kind=\"codex\", to=name)`, always named." CLAUDE_RULE — "An event with `sender=\"claude\"` is another Claude session; answer it with `reply_to_claude(to=…)` or `@claude:name`, always named." Channel instructions: the same two sentences. Tool descriptions: `reply_to_claude`, `antiphon_send.kind`.
- [ ] BACKLOG: the same-vendor entry becomes `(shipped 2026-09-03)` with the decisions (always addressed; labels; page unchanged; receipts; loops), and the four open questions answered in place.
- [ ] Contract tests pin the new words on every surface and the ceilings.

### Task 8: Verification on the exact SHA

- [ ] Both Python interpreters, the Node suite alone, statics, `npm pack --dry-run`, `fresh-user.sh` from a temporary worktree, an independent read-only review; commit.
