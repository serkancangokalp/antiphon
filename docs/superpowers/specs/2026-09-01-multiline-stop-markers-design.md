# Multi-line Stop Markers Design

**Date:** 2026-09-01

**Scope:** Add an explicit delimited block form to the existing `@claude` and
`@codex` Stop-marker road. Direct MCP tools, routing, transport limits,
attachments and delivery acknowledgement are unchanged.

## 1. Problem and boundary

Stop markers currently carry only the remainder of their physical line. That
is safe and explicit, but it forces an agent with a structured or multi-line
handoff either to flatten it or to use a direct MCP tool. Arbitrary continuation
is not acceptable: prose after a marker must never become a message merely
because it follows one.

This wave adds one opt-in delimiter syntax. It does not infer paragraphs,
Markdown fences or indentation, and it does not turn the Stop hook into a new
durable transport. The direct tools remain the recommended road for long
content.

## 2. Syntax

An ordinary marker whose parsed one-line message is exactly `<<TOKEN` opens a
block for that marker's existing recipient:

```text
@claude:review <<HANDOFF
First line.
Second line.
HANDOFF
```

`TOKEN` must match `[A-Z][A-Z0-9_]{0,31}`. Alias parsing and the existing
marker delimiter rules happen first, so bare and named markers work in both
directions without inventing another addressing grammar.

Any targeted marker message beginning with `<<` reserves the block syntax. If
the whole message is not exactly `<<` plus a valid token, the turn has an
invalid delimiter. This is the one deliberate exception to one-line
compatibility; every one-line marker that does not begin with `<<` retains its
current alias and message byte-for-byte.

The first later physical line whose content, excluding only its `\n` or
`\r\n` terminator, is exactly `TOKEN` closes the block. Indentation, trailing
spaces or additional text prevent a line from closing it. The opener and
closer are structural and are not delivered. The body is the exact substring
between them, including its whitespace, blank lines and original line endings.
Thus adjacent opener and closer lines produce the empty string; an ordinary
body line normally retains the line ending that precedes the closer.

After the closer, scanning resumes at the next physical line. One-line and
block markers may be mixed, and parsed records remain in source order.

## 3. Deliberately simple parsing rules

- **Fence-unaware closer.** An exact `TOKEN` line closes even inside a Markdown
  fence. The sender chooses a token absent from the content. Making the parser
  Markdown-aware would require defining malformed and unbalanced fences across
  languages before the syntax could be trusted.
- **No nesting.** A marker-looking opener, another `<<TOKEN2`, and an exact
  `TOKEN2` line inside a body are ordinary body text. Only the current token
  has structural meaning.
- **Body markers are inert.** Lines beginning `@claude` or `@codex` inside the
  body are delivered as content and never become independent sends.
- **Target-local.** A marker for the other side remains ignored by this push,
  exactly as today; its delimiter-like text cannot invalidate this target's
  extraction.

These rules make the parse a single left-to-right pass with no guessed
continuation and no hidden language parser.

## 4. All-or-nothing refusal

The parser validates the complete target extraction before any recipient is
sent. An invalid delimiter or a valid opener with no closer invalidates all
marker extraction for that turn, including valid markers before it. Nothing is
partially delivered and no delivery fingerprint is recorded.

The Stop hook writes one actionable stderr refusal:

- an unclosed block names its safe, grammar-limited token and says nothing was
  sent for the turn;
- an invalid delimiter is named only as an invalid multi-line marker and does
  not echo the line, alias, surrounding text or prospective body.

Neither refusal leaks body content. As with today's empty-marker refusal, a
successfully reported syntax refusal keeps the Stop hook at exit zero. The
Stop still retires any mid-turn delivery park it was responsible for; failure
to make that existing cursor update retains the existing nonzero bookkeeping
failure. Delivery fingerprints and unrelated cursor state are untouched.

An empty block reaches the existing visible empty-message path: it is reported
as carrying no message, sends nothing for that marker, and does not invalidate
valid siblings.

## 5. Existing delivery semantics remain authoritative

Each complete block body is one message in the existing recipient group.
Within a recipient, one-line and block messages retain source order. The
existing grouping across recipients, newline composition, turn-scoped
fingerprint, retry behavior and transport routing are unchanged.

The batch size check sees the fully composed body. A block over the marker
road's cap is refused exactly as an oversized one-line marker is; it is not
parked as an attachment. Its words remain in the visible agent reply and
therefore in passive pages. This wave does not alter the deliberate asymmetry
with direct MCP sends, which may spill to an attachment.

## 6. Agent-facing contract

README, `AGENTS_RULE`, `CLAUDE_RULE` and the channel instructions teach the
same compact contract:

- `@claude[:name] <<TOKEN` or `@codex[:name] <<TOKEN` opens a block;
- the exact later `TOKEN` line closes it;
- tokens use the uppercase bounded grammar, are not nestable and are not
  fence-aware;
- the sender must choose a token absent from the body;
- malformed or unclosed blocks send nothing for the turn.

The surfaces continue to recommend the direct tool for long content and keep
the existing attachment asymmetry explicit.

## 7. Verification gate

- Parser tests for bare/named blocks in both directions, exact body
  preservation, CRLF, source order, fenced and marker-looking body lines,
  non-nesting, empty blocks, early fence-unaware close, invalid delimiters and
  unclosed tokens.
- A compatibility corpus proving existing one-line parse results and outgoing
  transport strings byte-for-byte unchanged.
- Stop-push integration from real throwaway transcript records through the
  real turn reader, parser, grouping and dedupe boundary; only the final
  transport is substituted. Repeating the same turn sends once.
- All-or-nothing push tests proving syntax failures call no transport, record
  no delivery fingerprint, retire only the matching park and leak no body.
- Whole-body size-cap and attachment-asymmetry assertions.
- Contract tests over all four agent-facing surfaces.
- Full Python/Node suite, static checks and fresh-user E2E on one exact clean
  commit.
- Read-only Claude confirmation and the existing independent Codex reviewer on
  that same SHA.

No live cursor, catalog or registry mutation, push, merge, version bump or
publish belongs to this wave.
