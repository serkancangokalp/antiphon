# Backlog Wave 1C: Real Codex Tool Visibility Design

**Date:** 2026-09-01

**Scope:** Correct the Codex transcript parser so real completed tool-call
records become compact passive-page events. Stable public ids and full
invocation retrieval are deliberately a later, separately gated wave.

## 1. Root cause and corpus evidence

The current parser emits a tool event only for
`event_msg/exec_command_begin`. Across 22 real project rollouts (about 58,000
complete records and 243 MB), that shape occurred zero times. The actual host
records were approximately 5,970 `response_item/custom_tool_call` and 468
`response_item/function_call` entries, each with a unique native call id and
one call per raw record. Matching output records were separate
`custom_tool_call_output` and `function_call_output` entries.

The test suite has three id-less Claude tool fixtures and no real Codex
call/output fixture. That coverage gap is why a parser blind to more than
6,400 calls remained green.

## 2. Why this is its own wave

A real-corpus page simulation isolated the parser change before public ids:

- rendered bytes increased 3.2%;
- pages increased from 800 to 899, or 12.4%, because the 40-completed-record
  cap binds before bytes in tool-heavy runs;
- 6,234 human records moved by median/p95/max 53/59/98 pages in a fully drained
  historical replay;
- 210 of 899 pages contained tools, with median/p95 34/38 calls on those pages.

This materially changes passive-page composition on the bridge's fallback
road. It therefore gets its own tests, documentation, exact-SHA E2E and review
gate instead of entering as an incidental retrieval parser edit.

The historical drain is not the steady state. A second exact-render simulation
grouped the real records by source and measured each run of tool calls followed
by the next human-visible record, with the later wave's 22-character id cost
included. Across 987 such runs, calls before the next human record were
median/p95/max 4/20/97. The human record stayed on the same page at both median
and p95; only 4 of 987 runs needed a later page, only one needed more than one,
and the maximum delay was two pages. Of the 53 runs with at least 20 calls, p95
delay was one page. Only two runs reached 40 calls.

That steady-state result does not justify a second conversation-versus-tool
lane. The existing active/dead scheduler already protects live work from
proved-dead history; adding another persisted alternation rule for a 4/987
case would enlarge cursor state without measured starvation. The replay cost
remains named and accepted, and the steady-state distribution is re-measured
when host record shapes or page limits change.

## 3. Accepted record shapes

For a completed Codex `response_item` record:

- `custom_tool_call`: emit one tool event when `name` is a non-empty string.
  Its `input` is measured to be free-form text for the dominant `exec` tool;
  never guess that it is JSON and never place the full input on the page.
- `function_call`: emit one tool event when `name` is a non-empty string.
  `arguments` remains an opaque string in this wave. A namespace may qualify
  the compact name when it is a safe non-empty string.
- `custom_tool_call_output` and `function_call_output`: emit no visible event.
  They remain completed filtered records and advance the parser's safe scanned
  frontier exactly as other filtered records do.
- Malformed objects, names and payloads are filtered without raising. A future
  unknown response item is not guessed into a tool call.

The compact text is the tool name, optionally namespace-qualified. Arguments
stay out of the passive page. The next wave provides explicit full invocation
retrieval.

The obsolete `exec_command_begin` reader remains only if fixtures or a
measured supported host still require rolling compatibility; otherwise its
retirement is explicit in tests and documentation rather than accidental.

## 4. Paging and cursor invariants

- One raw call record is one visible completed record, matching the existing
  atomic-record and source-prefix rules.
- Tool-only records do not increment the human-message count, but they do count
  toward `EVENT_LIMIT` and `PAGE_BUDGET`.
- Oldest-first order within a source and active/dead scheduling are unchanged.
- Output records and malformed records can move the safe scanned frontier but
  cannot move it past a first undelivered visible call.
- Failed hook delivery and oversized MCP refusal persist no frontier or lane.
- No tool arguments, results, native ids, source ids or paths enter status or
  doctor.

The 12.4% measured page increase and historical-delay cost are recorded as
the accepted price of making previously invisible work visible. This wave does
not weaken the 40-record limit to hide that cost and does not add an unearned
tool/conversation scheduler.

## 5. Surface contract

README, BACKLOG and both generated agent rules say that Codex tool calls now
appear as compact name-only events; arguments are still unavailable until the
retrieval wave lands. They do not claim tool results are visible.

## 6. Verification gate

- Faithful fixtures for both real call shapes and both output shapes.
- Malformed, filtered-frontier, page-budget, 40-record, delivery-failure and
  ordering tests.
- A before/after page-composition fixture pinned to the direction and reason,
  not machine-global corpus counts.
- Full Python/Node suite and static checks.
- Fresh-user E2E from the exact clean commit, after the marker-reliability wave.
- Claude contract confirmation and independent Codex review on the same SHA.

No persistent index, retrieval surface, push, merge, version or publish belongs
to this wave.
