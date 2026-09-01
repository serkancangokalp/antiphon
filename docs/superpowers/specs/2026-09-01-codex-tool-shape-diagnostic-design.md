# Codex Tool-Shape Diagnostic Design

**Date:** 2026-09-01

**Scope:** Make a future Codex tool-call schema drift visible in `antiphon
doctor` without exposing invocation content or changing passive delivery.

## 1. Failure being diagnosed

Wave 1C corrected a parser that silently rendered zero events for roughly
6,400 real Codex calls because it recognized a host shape no supported rollout
wrote. The repaired parser deliberately fails closed when a call's fields do
not match the measured schema. That privacy choice is correct, but today a
future drift is indistinguishable from a session that used no tools.

This diagnostic closes only that observability gap. It does not guess a new
schema into a public event and does not make malformed bytes retrievable.

## 2. Counted record

A record is counted as an unrecognized Codex tool-call shape only when all of
these are true:

- it is a complete `response_item` with an object payload;
- the payload type is one of the two supported call kinds
  (`custom_tool_call`, `function_call`) or is a future string kind ending in
  `_call`;
- the shared production validator `_codex_tool_fields` rejects it.

Output kinds are not calls and are not counted. Ordinary messages, lifecycle
events, malformed JSON and unrelated response items are not guessed into tool
calls. The counter reports only an aggregate integer; it never prints type,
name, argument, source id, path or native call id.

## 3. Trust and discovery boundary

Doctor scans the same durable Codex source discovery used by the Claude page
reader. It reads complete records through the descriptor-safe, strict raw
reader added for retrieval and never writes catalog or cursor state.

- A complete trusted catalog and complete reads can prove the aggregate.
- Building or degraded discovery cannot prove zero; the line says the amount
  is unknown and relies on the existing source-catalog line for the remedy.
- A source refusal or I/O-short traversal also makes the amount unknown. It
  must not render a green zero.
- An unterminated final record is outside the captured complete prefix, exactly
  as in retrieval and passive paging.

The scan may count recognized calls internally for tests, but the product
surface carries only the unrecognized count Claude requested.

## 4. Doctor contract

Exactly one `codex tool shapes:` line is emitted:

- `✓ ... 0 unrecognized tool-call records` when the trusted complete set was
  scanned and the count is zero;
- `✗ ... N unrecognized tool-call record(s) are omitted from passive pages`
  when `N > 0`, with an update/host-schema explanation;
- `· ... amount unknown because source discovery or reading is incomplete`
  when the aggregate cannot be proved. Existing catalog diagnostics retain
  responsibility for the actionable catalog remedy.

A positive count is broken, not merely informational: Antiphon's promised tool
visibility is known to be incomplete. An unknown count is a note because the
existing catalog diagnostic already decides whether the underlying boundary is
broken or still building.

The green line proves only that no call-like record matching this deliberately
narrow grammar was rejected. It does **not** prove that no tool call was
missed: a future host could abandon the `_call` suffix entirely (for example,
use `tool_use`) and remain invisible to both parser and counter. Broadening the
heuristic to arbitrary response items would trade this named blind spot for
false alarms over ordinary records, so the line states its exact claim rather
than overstating it.

## 5. Boundaries

No page event, retrieval result, cursor, catalog, registry, attachment,
configuration, setup surface or protocol schema changes. No persistent
counter or index is added. No transcript content enters doctor output.

The complete-set scan belongs only to the explicit `antiphon doctor` command.
The measured indexless scan over 243 MB of Codex rollouts cost about 579 ms,
which is acceptable for an operator-requested diagnostic and unacceptable on
every hook turn. This counter must not be moved into setup, status, paging or a
hook path without a new measurement and design gate.
