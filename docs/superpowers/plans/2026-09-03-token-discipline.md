# Token Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every behavioural red is observed on the pre-fix tree before the product edit; every new guard is mutated once and its named test watched fail.

**Goal:** A passive page never spends a reader's tokens on a host's bookkeeping or on history older than a day of the source's own clock, and the three static agent surfaces say the contract in under 3,000 bytes each.

**Architecture:** Five host record shapes (measured) stop rendering as speech: a Codex tag joins the wrapper set, an AGENTS.md injection and a Claude compact summary are host records, the ChatGPT app's external-agent relays become name-only tool lines, and Claude's interruption literals are host records. A page horizon relative to each source's newest record bounds every reader's lag; the skip is counted, announced on the page and on `status`. `CLAUDE_RULE`, `AGENTS_RULE` and the channel `instructions` are rewritten to carry every contract-tested fact and none of the implementation narrative, with byte ceilings pinned. Doctor learns to see a stale rule section, which `setup` already repairs.

**Tech Stack:** Python 3.9+ stdlib, `unittest`; Node ESM for the channel instructions and its contract test. `/usr/bin/python3` (3.9.6) and `python3` (Anaconda 3.14) are both verification roads; the Node suite is never run concurrently with a Python suite.

**Spec:** `docs/superpowers/specs/2026-09-02-final-campaign-design.md` §2.

## Global Constraints

- Work in a fresh worktree `.worktrees/token-discipline` on branch `token-discipline` from `main` after the P0 merge. One writer.
- Never touch the live `.antiphon/` of the repository; fixtures live under `tempfile`.
- Python floor 3.9; both interpreters run the suite; the Node suite runs alone.
- `package.json` `files` is unchanged; nothing under `test/` ships.
- A tag/shape joins a host set only on measured evidence; exact literals only for the Claude interruption markers; a doubtful shape stays out (a leaked line is visible, a deleted person's message is not).
- Every phrase a contract test pins today stays on its surface unless the test is changed in the same commit for a stated reason.
- No merge, push, version bump or publish inside this plan.

---

## Measured facts this plan relies on (2026-09-02, read-only)

| Shape | Count | Bytes | Renders today as |
|---|---|---|---|
| Codex assistant `[external_agent_tool_call: NAME]…` / `[external_agent_tool_result]…` | 8,936 | 11.0 MB | `Codex:` speech |
| Codex user `<codex_internal_context source="goal">…` | 133 | 931 KB | `To Codex:` |
| Codex user `# AGENTS.md instructions for <cwd>\n\n<INSTRUCTIONS>…` | 18 | 32 KB | `To Codex:` |
| Claude user `isCompactSummary: true` | 6 | 104 KB | `To Claude:` (always an oversized record) |
| Claude user exactly `[Request interrupted by user]` / `[Request interrupted by user for tool use]` | 7 | — | `To Claude:` |
| Claude page reader backlog from the live cursor | >400 pages | 2.6 MB rendered | 31 August content on every page |
| Same backlog bounded to 24 h / 6 h | 21 / 6 pages | | |
| `CLAUDE_RULE` / `AGENTS_RULE` / channel `instructions` | 7,354 / 8,120 / ~6,000 bytes | | every turn / every session; the host truncates the instructions |

Fixture timestamps in `test/test_antiphon.py` are fixed dates (58 records on `2026-08-30` / `2026-09-01`, three in 2020, one in 2030), so a wall-clock horizon would break the suite and, worse, would hide an overnight run from the next morning's reader. The horizon is therefore **relative to the source's newest complete record**.

---

### Task 1: Codex host records — the goal tag, the AGENTS.md injection, and external-agent relays

**Files:**
- Modify: `lib/antiphon.py` — `CODEX_HOST_WRAPPERS` (~:462), new `_is_codex_host_block` and `_external_agent_relay` beside `_is_host_record` (~:486), `codex_events` user/assistant branches (~:3480-3500), `_codex_turn.message_texts` (~:4356)
- Test: `test/test_antiphon.py` — class `AntiphonTest` (the class holding the `CODEX_HOST_WRAPPERS` pin at ~:2381 and the `codex_events` tests at ~:2457)

**Interfaces:**
- Produces: `antiphon.CODEX_HOST_WRAPPERS` (tuple, now with `"codex_internal_context"`); `antiphon._is_codex_host_block(text) -> bool`; `antiphon._external_agent_relay(text) -> ("call", name) | ("result", None) | None`; a tool `Event` whose text is `external agent: <NAME>` and whose `public_id` is `None`.

- [ ] **Step 1: Write the failing tests** (in `AntiphonTest`, after the existing `CODEX_HOST_WRAPPERS` pin; the pinned list in that test gains `"codex_internal_context"` in the same edit)

```python
    def _codex_rollout(self, project, records):
        """One rollout under a fake CODEX_SESSIONS naming `project` as cwd."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = os.path.join(root, "rollout-2026-08-30T10-00-00-"
                            "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7.jsonl")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": "2026-08-30T10:00:00.000Z",
                                     "type": "session_meta",
                                     "payload": {"cwd": project}}) + "\n")
            for record in records:
                stream.write(json.dumps(record) + "\n")
        return root

    @staticmethod
    def _codex_message(role, text, second=1):
        return {"timestamp": f"2026-08-30T10:00:{second:02d}.000Z",
                "type": "response_item",
                "payload": {"type": "message", "role": role,
                            "content": [{"type": "input_text" if role == "user"
                                         else "output_text", "text": text}]}}

    def test_codex_goal_continuation_is_a_host_record(self):
        self.assertIn("codex_internal_context", antiphon.CODEX_HOST_WRAPPERS)
        self.assertTrue(antiphon._is_host_record(
            '<codex_internal_context source="goal">\nContinue working toward '
            "the active thread goal.", antiphon.CODEX_WRAPPER_OPENING))
        self.assertFalse(antiphon._is_host_record(
            "<codex_internal_contexts> is a word", antiphon.CODEX_WRAPPER_OPENING),
            "a whole tag name, as every other wrapper")

    def test_codex_agents_md_injection_is_a_host_record(self):
        block = ("# AGENTS.md instructions for /Users/x/project\n\n"
                 "<INSTRUCTIONS>\n\n## The Antiphon bridge\n...\n</INSTRUCTIONS>\n"
                 "<environment_context>\n  <cwd>/Users/x/project</cwd>\n"
                 "</environment_context>")
        self.assertTrue(antiphon._is_codex_host_block(block))
        # Both halves, or it is a person's text that happens to start that way.
        self.assertFalse(antiphon._is_codex_host_block(
            "# AGENTS.md instructions for my talk\n\nslides"))
        self.assertFalse(antiphon._is_codex_host_block(
            "please read <INSTRUCTIONS> in the doc"))
        self.assertFalse(antiphon._is_codex_host_block(""))
        self.assertFalse(antiphon._is_codex_host_block(None))

    def test_codex_external_agent_relays_are_tool_lines_not_speech(self):
        with tempfile.TemporaryDirectory() as project:
            root = self._codex_rollout(project, [
                self._codex_message("assistant",
                    "[external_agent_tool_call: Bash]\ndescription: list\n"
                    "command: ls -la /secret\n[/external_agent_tool_call]", 1),
                self._codex_message("assistant",
                    "[external_agent_tool_result]\ntotal 0\n"
                    "SECRET-OUTPUT\n[/external_agent_tool_result]", 2),
                self._codex_message("assistant", "Plain words from Codex.", 3),
                self._codex_message("user",
                    "# AGENTS.md instructions for " + project
                    + "\n\n<INSTRUCTIONS>\nAGENTS-SECRET\n</INSTRUCTIONS>", 4),
                self._codex_message("user",
                    '<codex_internal_context source="goal">\nGOAL-SECRET', 5),
                self._codex_message("user", "a person typed this", 6),
            ])
            with patch.object(antiphon, "CODEX_SESSIONS", root):
                events, _ = antiphon.codex_events(project)
        rendered = [(event.kind, event.text) for event in events]
        self.assertEqual(rendered, [
            ("tool", "external agent: Bash"),
            ("codex", "Plain words from Codex."),
            ("you", "a person typed this"),
        ])
        self.assertIsNone(events[0].public_id, "nothing retrievable backs a relay")
        joined = "\n".join(text for _kind, text in rendered)
        for secret in ("ls -la", "SECRET-OUTPUT", "AGENTS-SECRET", "GOAL-SECRET"):
            self.assertNotIn(secret, joined)

    def test_the_relay_predicate_is_exact_about_its_shape(self):
        self.assertEqual(antiphon._external_agent_relay(
            "[external_agent_tool_call: mcp__antiphon__reply_to_codex]\ninput: {}"),
            ("call", "mcp__antiphon__reply_to_codex"))
        self.assertEqual(antiphon._external_agent_relay(
            "[external_agent_tool_result]\nok"), ("result", None))
        for text in ("[external_agent_tool_call: ]\nx", "[external_agent_tool_call:Bash]",
                     "external_agent_tool_call: Bash", "[external_agent_tool_call: Bad Name]",
                     " [external_agent_tool_call: Bash]", "", None):
            self.assertIsNone(antiphon._external_agent_relay(text), repr(text))

    def test_a_codex_stop_hook_never_pushes_a_marker_out_of_a_relay(self):
        """A relayed external-agent call can carry `@claude` at a line start
        — it is the other agent's own command text. The Stop reader must not
        read it as Codex addressing anyone."""
        with tempfile.TemporaryDirectory() as project:
            path = os.path.join(project, "rollout.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "task_started", "turn_id": "t1"}}) + "\n")
                stream.write(json.dumps(self._codex_message("assistant",
                    "[external_agent_tool_call: Bash]\ncommand: echo '@claude hi'\n"
                    "[/external_agent_tool_call]", 1)) + "\n")
                stream.write(json.dumps(self._codex_message("assistant",
                    "@claude real marker", 2)) + "\n")
            text, key = antiphon._codex_turn(path, "t1")
        self.assertEqual(text, "@claude real marker")
        self.assertEqual(key, "t1")
```

`import shutil` if absent in the test module (grep first).

- [ ] **Step 2: Run to verify they fail**

Run: `/usr/bin/python3 -m unittest test.test_antiphon.AntiphonTest -k codex_goal -k agents_md -k external_agent -k relay -v`
Expected: the pin test fails on the constant, `_is_codex_host_block`/`_external_agent_relay` raise `AttributeError`, the events test renders the relays as `codex` speech, the Stop-hook test returns both lines.

- [ ] **Step 3: Implement**

`CODEX_HOST_WRAPPERS` gains `"codex_internal_context"` (update the measurement comment above it: 2026-09-02, 133 records in 206 files, the ChatGPT app's goal continuation, Codex only). Beside `_is_host_record`:

```python
# The Codex host injects the project's AGENTS.md as the first user message
# of a session — `# AGENTS.md instructions for <cwd>` then an
# `<INSTRUCTIONS>` fence. Measured 2026-09-02: 18 records, 32 KB, every one
# host-written, and the rule inside is this bridge's own text relayed back
# to the other side. Both halves are required: a heading alone could be a
# person's document, and a fence alone could be quoted.
AGENTS_INJECTION_HEAD = "# AGENTS.md instructions for "


def _is_codex_host_block(text):
    return (isinstance(text, str) and text.startswith(AGENTS_INJECTION_HEAD)
            and "\n<INSTRUCTIONS>" in text)


# The ChatGPT app records an external agent's tool traffic — Claude Code's
# own Bash and Read calls, when Claude runs inside the app — as assistant
# messages in the Codex rollout. Measured 2026-09-02: 8,936 records, 11 MB,
# rendered as Codex speech. A call is one name-only tool line, as every
# Codex tool call is; a result is a tool output and stays filtered. Exact at
# the first line: the bracket, the fixed prefix, a tool-name token, nothing
# leading.
EXTERNAL_AGENT_CALL = re.compile(
    r"\[external_agent_tool_call: (" + TOOL_COMPONENT.pattern + r")\](?:\n|$)")
EXTERNAL_AGENT_RESULT_HEAD = "[external_agent_tool_result]"


def _external_agent_relay(text):
    if not isinstance(text, str):
        return None
    matched = EXTERNAL_AGENT_CALL.match(text)
    if matched:
        return "call", matched.group(1)
    if text == EXTERNAL_AGENT_RESULT_HEAD or text.startswith(
            EXTERNAL_AGENT_RESULT_HEAD + "\n"):
        return "result", None
    return None
```

`TOOL_COMPONENT` is defined later in the file (~:2786); move the relay definitions below it or reference the pattern string literally — keep one spelling. In `codex_events`:

```python
                        if text != "" and role != "developer":
                            if role == "user":
                                if (not _is_host_record(text, CODEX_WRAPPER_OPENING)
                                        and not _is_codex_host_block(text)
                                        and not _is_self_injected(text)):
                                    events.append(...)          # unchanged
                            elif role == "assistant":
                                relay = _external_agent_relay(text)
                                if relay is None:
                                    events.append(... Event(ts, "codex", text, ...))   # unchanged
                                elif relay[0] == "call":
                                    events.append((ts, path, next(position),
                                                  Event(ts, "tool",
                                                        f"external agent: {relay[1]}",
                                                        sid, gen, start, end,
                                                        previous_anchor, anchor)))
                                # a result is a tool output: filtered, frontier advances
```

In `_codex_turn.message_texts`: after computing `texts`, `if _external_agent_relay("\n".join(texts)) is not None: return None`.

- [ ] **Step 4: Run to verify they pass, then mutate**

Run the Step 2 command; expected PASS. Mutations, each reverted by writing the saved original back: drop `"codex_internal_context"` from the set → the pin test and the events test fail; make `_is_codex_host_block` ignore the fence (`and True`) → its test fails on the "my talk" case; make `_external_agent_relay` return `None` always → the events test renders speech; drop the `message_texts` guard → the Stop-hook test returns both lines.

- [ ] **Step 5: Commit**

```bash
git add lib/antiphon.py test/test_antiphon.py
git commit -m "Codex host records are not speech: the goal tag, the AGENTS.md injection, external-agent relays"
```

---

### Task 2: Claude host records — compact summaries and interruption literals

**Files:**
- Modify: `lib/antiphon.py` — `CLAUDE_HOST_LITERALS` beside `CLAUDE_HOST_WRAPPERS`, `claude_events` user branch (~:3220)
- Test: `test/test_antiphon.py` — `AntiphonTest`, beside the `CLAUDE_HOST_WRAPPERS` pin (~:2263)

**Interfaces:**
- Produces: `antiphon.CLAUDE_HOST_LITERALS = ("[Request interrupted by user]", "[Request interrupted by user for tool use]")`.

- [ ] **Step 1: Write the failing tests**

```python
    def _claude_transcript(self, project, records):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        slug = antiphon._claude_slug(project)
        os.makedirs(os.path.join(root, slug))
        path = os.path.join(root, slug,
                            "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7.jsonl")
        with open(path, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(dict(record, cwd=project)) + "\n")
        return root

    @staticmethod
    def _claude_user(text, second=1, **extra):
        return dict({"timestamp": f"2026-08-30T10:00:{second:02d}.000Z",
                     "type": "user", "message": {"role": "user", "content": text}},
                    **extra)

    def test_claude_compact_summaries_and_interruptions_are_host_records(self):
        self.assertEqual(antiphon.CLAUDE_HOST_LITERALS,
                         ("[Request interrupted by user]",
                          "[Request interrupted by user for tool use]"))
        with tempfile.TemporaryDirectory() as project:
            root = self._claude_transcript(project, [
                self._claude_user("This session is being continued from a "
                                  "previous conversation. SUMMARY-SECRET", 1,
                                  isCompactSummary=True),
                self._claude_user("[Request interrupted by user]", 2),
                self._claude_user("[Request interrupted by user for tool use]", 3),
                self._claude_user("[Request interrupted by user] and more", 4),
                self._claude_user("a person typed this", 5),
            ])
            with patch.object(antiphon, "CLAUDE_PROJECTS", root):
                events, _ = antiphon.claude_events(project)
        self.assertEqual([(e.kind, e.text) for e in events], [
            ("you", "[Request interrupted by user] and more"),
            ("you", "a person typed this"),
        ], "exact literals only; a person's line that starts the same way stays")
```

Read `claude_transcripts`/`_find_claude_project_dir` (~:3116-3181) first: the fixture must be discoverable the way the existing `claude_events` tests in this class build theirs — copy their helper if one exists rather than the sketch above.

- [ ] **Step 2: Run to verify it fails**

Run: `/usr/bin/python3 -m unittest test.test_antiphon.AntiphonTest -k compact_summaries -v`
Expected: FAIL — `CLAUDE_HOST_LITERALS` missing; three extra `you` events.

- [ ] **Step 3: Implement**

```python
# Two host literals, exact. Claude Code writes them as the user record that
# ends an interrupted turn; nobody typed them. Equality, never a prefix: a
# person's line that begins the same way stays a person's line.
CLAUDE_HOST_LITERALS = ("[Request interrupted by user]",
                        "[Request interrupted by user for tool use]")
```

In `claude_events`, the `kind == "user"` branch: `if isinstance(d, dict) and not d.get("isMeta")` stays; add to the condition that appends the event:

```python
                        if (text != ""
                                and not d.get("isCompactSummary")
                                and text not in CLAUDE_HOST_LITERALS
                                and not _is_host_record(...)
                                and not _is_self_injected(text)):
```

with a comment: a compact summary is the host's own restatement of context (measured 17 KB each, always an oversized record) and is host-set, not text-shaped.

- [ ] **Step 4: Verify, mutate, commit**

PASS; mutate `not d.get("isCompactSummary")` → `True` (summary leaks), and the literal check → `text not in ()` (interruptions leak); each red; restore.

```bash
git commit -am "Claude host records are not speech: compact summaries and interruption literals"
```

---

### Task 3: The census sees the prefix shapes

**Files:**
- Modify: `test/host_wrapper_census.py`, `test/test_host_wrapper_census.py`, `BACKLOG.md` (census entry)

**Interfaces:**
- Produces: `census()` result gains `"shapes"` per side: Codex `{"agents_md_block": n, "external_agent_call": n, "external_agent_result": n}` (the first from user messages, the other two from assistant messages), Claude `{"compact_summary": n, "interruption_literal": n}`.

- [ ] **Step 1: Failing test** — extend `fixtures()` with one AGENTS.md block user message, one `[external_agent_tool_call: Bash]` and one `[external_agent_tool_result]` assistant message on the Codex side; one `isCompactSummary` and one `[Request interrupted by user]` user record on the Claude side; assert the counts and that the CLI output carries none of the secret strings.
- [ ] **Step 2: Red** — `KeyError: 'shapes'`.
- [ ] **Step 3: Implement** — `codex_assistant_blocks(record)` beside `codex_user_blocks`; `_side_census` takes an extra `shapes_for(record)` callable returning shape names; keep aggregate-only output.
- [ ] **Step 4: Green; run `python3 test/host_wrapper_census.py --claude-root "$HOME/.claude/projects" --codex-root "$HOME/.codex/sessions"` read-only and record the aggregate numbers in the BACKLOG census entry (date 2026-09-03, the new shapes, the `codex_internal_context` decision).**
- [ ] **Step 5: Commit** — `Census: prefix shapes beside tag shapes`.

---

### Task 4: The page horizon

**Files:**
- Modify: `lib/antiphon.py` — constant beside `LOOKBACK` (~:82); `SafeSource.first_record_at` and `_PathSource.first_record_at`; new `_record_time`, `_source_newest_time`, `_first_offset_at_or_after`, `_apply_horizon` beside `_source_offset_at_or_after` (~:2913); `claude_events`/`codex_events` (`skipped` parameter); `build_summary`/`_build_page`/`_render_page` (the notice); `reader_backlog` (fifth value) and `_backlog_line`; `README.md` §Limits; `test/test_contracts.py` (`test_paged_context_limits_match_code` pins the number and the README words)
- Test: `test/test_antiphon.py` — new class `PageHorizonTest` beside `CatchUpTest` (~:4018)

**Interfaces:**
- Produces: `antiphon.PAGE_HORIZON = 24 * 3600`; `source.first_record_at(offset) -> (start, end, line) | None`; `antiphon._apply_horizon(source, start) -> (start, skipped_bytes)`; `claude_events(..., skipped=None)` / `codex_events(..., skipped=None)` fill `skipped[sid] = bytes` when they skip; `reader_backlog` returns `(unread, positioned, unpositioned, replay, skipped)`; the page line `skipped: {n:,} raw bytes of {Label} activity older than 24 hours in {k} source(s) — not delivered; the transcripts keep it`.

- [ ] **Step 1: Write the failing tests**

```python
class PageHorizonTest(unittest.TestCase):
    """A reader never lags a source by more than a day of that source's own
    clock. Measured on the live project: a Claude reader more than 400 pages
    behind, every page 31 August; bounded to 24 hours, 21 pages. Relative to
    the source's newest record, not to the wall clock: an overnight run is
    still there in the morning, and a source that stopped days ago yields its
    last day rather than nothing or everything.
    """

    SID = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7"

    def _rollout(self, project, times):
        """One Codex rollout with an assistant record at each ISO time."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = os.path.join(root, f"rollout-2026-08-30T10-00-00-{self.SID}.jsonl")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": times[0], "type": "session_meta",
                                     "payload": {"cwd": project}}) + "\n")
            for index, when in enumerate(times):
                stream.write(json.dumps({
                    "timestamp": when, "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": f"message {index}"}]}}) + "\n")
        return root, path

    def _drain(self, project, root, positions=None):
        pages, texts = 0, []
        positions = positions or antiphon.RuntimePositions()
        with patch.object(antiphon, "CODEX_SESSIONS", root):
            while pages < 50:
                text, advance, _ = antiphon.build_summary(
                    project, "claude", positions, None, None)
                if not text:
                    break
                pages += 1
                texts.append(text)
                sources = dict(positions)
                for sid, raw in advance.sources.items():
                    sources[sid] = dict(raw)
                positions = antiphon.RuntimePositions(
                    sources, adopting={}, next_lane=advance.next_lane)
                if not advance.has_more:
                    break
        return texts

    def test_a_positioned_reader_skips_what_is_older_than_a_day_of_the_source(self):
        # Three days of records, one per hour; the reader is positioned at the
        # very start. Only the last 24 hours of the source's own clock arrive.
        times = [f"2026-08-{28 + hour // 24:02d}T{hour % 24:02d}:00:00.000Z"
                 for hour in range(72)]
        with tempfile.TemporaryDirectory() as project:
            root, path = self._rollout(project, times)
            gen = antiphon.source_generation(path)
            positions = antiphon.RuntimePositions(
                {self.SID: {"gen": gen, "offset": 0, "anchor": None}})
            pages = self._drain(project, root, positions)
        joined = "\n".join(pages)
        self.assertNotIn("message 46", joined, "older than a day of the newest")
        self.assertIn("message 47", joined, "exactly 24 hours before the newest")
        self.assertIn("message 71", joined)
        self.assertRegex(pages[0], r"skipped: [\d,]+ raw bytes of Codex activity "
                                   r"older than 24 hours in 1 source\(s\) — not "
                                   r"delivered; the transcripts keep it")
        self.assertNotIn("skipped:", pages[-1], "announced once, where it happened")

    def test_a_source_within_the_horizon_is_never_skipped(self):
        times = [f"2026-08-30T{hour:02d}:00:00.000Z" for hour in range(23)]
        with tempfile.TemporaryDirectory() as project:
            root, path = self._rollout(project, times)
            gen = antiphon.source_generation(path)
            positions = antiphon.RuntimePositions(
                {self.SID: {"gen": gen, "offset": 0, "anchor": None}})
            pages = self._drain(project, root, positions)
        joined = "\n".join(pages)
        self.assertIn("message 0", joined)
        self.assertNotIn("skipped:", joined)

    def test_the_skip_lands_on_a_record_boundary_the_anchor_can_prove(self):
        times = [f"2026-08-{28 + hour // 24:02d}T{hour % 24:02d}:00:00.000Z"
                 for hour in range(72)]
        with tempfile.TemporaryDirectory() as project:
            root, path = self._rollout(project, times)
            with antiphon._PathSource(path, "codex") as source:
                start, skipped = antiphon._apply_horizon(source, 0)
                self.assertGreater(skipped, 0)
                record = source.first_record_at(start)
                self.assertEqual(record[0], start, "a boundary")
                self.assertIn('"message 47"', record[2].replace("message 47", '"message 47"'))
                self.assertIsNotNone(source.anchor_at(start))
                again, more = antiphon._apply_horizon(source, start)
                self.assertEqual((again, more), (start, 0), "idempotent")

    def test_bisection_agrees_with_the_linear_scan_on_a_large_source(self):
        # Enough bytes that the bisecting road is taken; the answer must be
        # the linear scan's answer, record for record.
        times = [f"2026-08-{28 + hour // 24:02d}T{hour % 24:02d}:{minute:02d}:00.000Z"
                 for hour in range(72) for minute in range(0, 60, 2)]
        with tempfile.TemporaryDirectory() as project:
            root, path = self._rollout(project, times)
            with antiphon._PathSource(path, "codex") as source:
                newest = antiphon._source_newest_time(source)
                horizon = newest - antiphon.PAGE_HORIZON
                linear = next(start for start, _end, line in source.read_records(0)
                              if antiphon._record_time(line) >= horizon)
                self.assertGreater(source.size(), antiphon.HORIZON_BISECT_ABOVE)
                self.assertEqual(antiphon._first_offset_at_or_after(source, horizon, 0),
                                 linear)

    def test_an_unparseable_newest_record_disables_the_horizon(self):
        times = [f"2026-08-{28 + hour // 24:02d}T{hour % 24:02d}:00:00.000Z"
                 for hour in range(72)]
        with tempfile.TemporaryDirectory() as project:
            root, path = self._rollout(project, times)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write('{"type": "event_msg", "payload": {}}\n')
            with antiphon._PathSource(path, "codex") as source:
                self.assertIsNone(antiphon._source_newest_time(source))
                self.assertEqual(antiphon._apply_horizon(source, 0), (0, 0))

    def test_status_counts_what_the_horizon_will_skip(self):
        times = [f"2026-08-{28 + hour // 24:02d}T{hour % 24:02d}:00:00.000Z"
                 for hour in range(72)]
        with tempfile.TemporaryDirectory() as project:
            root, path = self._rollout(project, times)
            gen = antiphon.source_generation(path)
            cursor = {"claude_pages_v4": {"v": 4, "sources": {
                self.SID: {"gen": gen, "offset": 0, "anchor": None}},
                "adopting_v3": {}, "next_lane": "active"}}
            with patch.object(antiphon, "CODEX_SESSIONS", root):
                unread, positioned, unpositioned, replay, skipped = \
                    antiphon.reader_backlog(project, "claude", cursor)
        self.assertGreater(skipped, 0)
        self.assertGreater(unread, 0)
        line = antiphon._backlog_line("claude_pages",
                                      (unread, positioned, unpositioned, replay, skipped))
        self.assertIn(f"{skipped:,} raw bytes older than the 24-hour horizon will be skipped", line)
```

`reader_backlog` builds discovery through the catalog; if the fixture above is not discovered without a catalog, use the newest-file fallback the way `CatchUpTest` builds its fixtures (read that class first and copy its arrangement).

- [ ] **Step 2: Run to verify they fail**

Run: `/usr/bin/python3 -m unittest test.test_antiphon.PageHorizonTest -v`
Expected: `AttributeError` on `PAGE_HORIZON`, `_apply_horizon`, `first_record_at`; the drain test delivers message 0; the status test unpacks four values.

- [ ] **Step 3: Implement**

Constants:

```python
LOOKBACK = 6 * 3600       # a new reader starts this far back
# A positioned reader never lags a source by more than this much of the
# source's own clock: undelivered records older than the source's newest
# complete record minus this are skipped, counted and announced. Relative to
# the source, not to the wall: an overnight run is still there in the
# morning, and a source that stopped days ago yields its last day. Measured
# before this existed: a reader more than 400 pages behind, every page a
# day old, 2,000 tokens per turn spent on history nobody asked for.
PAGE_HORIZON = 24 * 3600
HORIZON_BISECT_ABOVE = 1024 * 1024     # bytes of unread span before bisecting
HORIZON_BISECT_SLACK = 256 * 1024      # linear scan resumes this far before the probe
```

Source method (both classes; `_PathSource` opens the path, `SafeSource` uses `_reader`):

```python
    def first_record_at(self, offset):
        """The first complete record starting at or after `offset`, as
        `(start, end, line)`, or None. An offset inside a line skips to the
        next line; an offset that is a boundary — byte zero or one just after
        a newline — starts the record there."""
        try:
            with self._reader(max(0, offset - 1)) as stream:
                position = offset
                if offset > 0:
                    if stream.read(1) != b"\n":
                        rest = stream.readline()
                        if not rest.endswith(b"\n"):
                            return None
                        position = offset + len(rest)
                raw = stream.readline()
        except OSError:
            return None
        if not raw.endswith(b"\n"):
            return None
        return position, position + len(raw), raw[:-1].decode("utf-8", "replace")
```

Helpers beside `_source_offset_at_or_after`:

```python
def _record_time(line):
    """The record's own timestamp as an epoch float, or None."""
    try:
        return iso_epoch(json.loads(line).get("timestamp"))
    except (ValueError, AttributeError, TypeError):
        return None


def _source_newest_time(source):
    """The timestamp of the source's last complete record, or None when it
    has none or it does not carry one — and then there is no horizon."""
    end = source.complete_prefix_end()
    if not end:
        return None
    anchor = source.anchor_at(end)
    if anchor is None:
        return None
    record = source.first_record_at(anchor["start"])
    return _record_time(record[2]) if record else None


def _first_offset_at_or_after(source, timestamp, start):
    """The start of the first record at or after `timestamp`, searching from
    `start` (a record boundary); the complete-prefix end when none is.

    Bisects over record boundaries when the span is large, then scans the
    last slack linearly, so a local misorder of timestamps costs a few
    repeated records rather than skipped ones."""
    size = source.complete_prefix_end()
    lo, hi = start, size
    if hi - lo > HORIZON_BISECT_ABOVE:
        while hi - lo > HORIZON_BISECT_SLACK:
            mid = (lo + hi) // 2
            record = source.first_record_at(mid)
            if record is None or record[0] >= hi:
                hi = mid
                continue
            when = _record_time(record[2])
            if when is not None and when >= timestamp:
                hi = record[0]
            else:
                lo = record[1]
        lo = max(start, lo - HORIZON_BISECT_SLACK)
        boundary = source.first_record_at(lo)
        lo = boundary[0] if boundary else lo
    for record_start, _end, line in source.read_records(lo):
        when = _record_time(line)
        if when is not None and when >= timestamp:
            return record_start
    return size


def _apply_horizon(source, start):
    """`(start, skipped)`: a trusted start moved forward to the first record
    within PAGE_HORIZON of the source's newest complete record."""
    newest = _source_newest_time(source)
    if newest is None:
        return start, 0
    horizon = newest - PAGE_HORIZON
    first = source.first_record_at(start)
    if first is None:
        return start, 0
    when = _record_time(first[2])
    if when is None or when >= horizon:
        return start, 0
    landing = _first_offset_at_or_after(source, horizon, start)
    return landing, max(0, landing - start)
```

Note `iso_epoch` may return `None` for a missing timestamp — read it (~:2768) and make `_record_time` return None in that case.

In both event readers, after `offset = _start_source_offset(source, positions, since)`:

```python
            offset, cut = _apply_horizon(source, offset)
            if cut and skipped is not None:
                skipped[sid] = skipped.get(sid, 0) + cut
```

with `skipped=None` added to both signatures. `build_summary` creates `skipped = {}`, passes it to the reader, and hands it to `_build_page(..., skipped=skipped)` → `_render_page(..., skipped=skipped)`, which after the `discovery` line and before the replay notice appends, when `skipped` is non-empty:

```python
    if skipped:
        text = _append_page_section(text, (
            f"skipped: {sum(skipped.values()):,} raw bytes of {name} activity "
            f"older than {PAGE_HORIZON // 3600} hours in {len(skipped)} "
            "source(s) — not delivered; the transcripts keep it"))
```

The budget loop re-renders every prefix; `skipped` is constant across it, so the line is on every candidate and the budget arithmetic already covers it.

`reader_backlog`: after `start, reason = _resolve_source_start(...)`, `start, cut = _apply_horizon(source, start)`; accumulate `skipped += cut`; return the five-tuple. `_backlog_line` unpacks five and appends `; {skipped:,} raw bytes older than the {PAGE_HORIZON // 3600}-hour horizon will be skipped` when non-zero. Update the two existing four-value unpacks in `test/test_antiphon.py` (~:4283, ~:7507) and doctor's `_doctor_replay` (~:8984) to the new shape.

README §Limits, after the paragraph beginning "A page that leaves work behind": one paragraph — "A page never carries a record older than 24 hours of its source's newest complete record. A reader that fell further behind skips to that point, the page says `skipped: N raw bytes … older than 24 hours` once where it happened, `status` counts what the next page will skip, and the transcripts keep what was skipped. A brand-new reader still starts six hours back. `antiphon catch-up` remains the way to skip to the live edge at once." Pin in `test_paged_context_limits_match_code`: `antiphon.PAGE_HORIZON == 24 * 3600`, and the words `24 hours` and `skipped:` in §Limits.

- [ ] **Step 4: Verify, mutate, commit**

PASS. Mutations, each red then restored: `_apply_horizon` returns `(start, 0)` always → drain test delivers message 0; bisect without the slack (`lo = lo` instead of `lo - SLACK`) with a fixture whose timestamps are misordered by one record near the boundary → add that fixture to the bisection test first; `_source_newest_time` returns the *first* record's time → drain test skips nothing; `_render_page` drops the line → the regex assertion fails; `reader_backlog` returns four values → the status test fails.

```bash
git commit -am "The page has a horizon: a reader never lags a source by more than a day of its own clock"
```

---

### Task 5: The three agent surfaces, in under 3,000 bytes each

**Files:**
- Modify: `lib/antiphon.py` — `TOOL_RETRIEVAL_RULE`, `MULTILINE_MARKER_RULE`, `LONG_MARKER_RULE`, `AUTOMATIC_PEER_IDENTITY_RULE`, `AGENTS_RULE`, `CLAUDE_RULE` (~:7044-7245); `TOOLS[0]["description"]` (antiphon_read, ~:6166)
- Modify: `lib/channel.mjs` — the `instructions` string (~:118-198)
- Modify: `test/test_contracts.py` — new `test_the_agent_surfaces_stay_small`; existing pins unchanged unless a phrase moved (state each in the commit)
- Test: run `test.test_contracts` and every `test_antiphon.py` class that reads the rules (`grep -n "CLAUDE_RULE\|AGENTS_RULE" test/test_antiphon.py`)

**Interfaces:**
- Produces: the same constant names, new bytes; `SURFACE_BYTE_CEILING = 3_000` in `test/test_contracts.py` applied to `CLAUDE_RULE`, `AGENTS_RULE` and the collapsed `instructions` string.

- [ ] **Step 1: Write the failing test**

```python
    def test_the_agent_surfaces_stay_small(self):
        """Every byte here is paid on every turn (the rules) or every session
        (the instructions), and the host truncates a long instructions
        string. Measured before this ceiling: 7,354 / 8,120 / ~6,000 bytes."""
        node = read("lib", "channel.mjs")
        start = node.index("    instructions:")
        end = node.index("\n  },\n);", start)
        channel = json.loads("[" + re.sub(r'"\s*\+\s*\n\s*"', "", node[start:end])
                             .split(":", 1)[1].strip().rstrip(",") + "]")[0]
        for where, text in (("CLAUDE_RULE", antiphon.CLAUDE_RULE),
                            ("AGENTS_RULE", antiphon.AGENTS_RULE),
                            ("channel instructions", channel)):
            self.assertLessEqual(len(text.encode("utf-8")), 3_000, where)
```

(If the JSON decode of the collapsed instructions string is fragile, measure the collapsed source slice's byte length instead and say so in the test.)

- [ ] **Step 2: Red** — three sizes over the ceiling.

- [ ] **Step 3: Write the surfaces.** Every pinned phrase listed here stays: `<channel source="antiphon" sender="codex" sender_kind="agent" sender_alias="...">`; one sentence containing `sender_alias`, ``as `to` `` and `<unnamed>` and neither "always" nor "null"; `never broadcast`; `is refused`/`are refused`; `project-wide`; `ANTIPHON_NAME`; `automatic `auto-` peer alias`; `at least … first hook` inside one sentence; `fixed Claude probe`; `host display name is ignored`; `identity digest, owner key and socket route stay private`; `expose only the public alias`; `refusals … errors`; `two or more … refus`; `until a fresh endpoint exists`; `reconnect`; `counted, never addressed`; `<<TOKEN`; `[A-Z][A-Z0-9_]{0,31}`; `exact … TOKEN … line … close`; `do not nest`; `not … fence-aware`; `token absent from the body`; `malformed or unclosed … nothing … turn`; ``literal text beginning with `<<` … block body``; `oversized … Stop-marker block … refused … not parked`; ``reply_to_codex … long content`` (Claude, channel) / ``antiphon_send … long content`` (Agents); `antiphon_retrieve`; `content-bound`; `invocation only`; `never the tool result`; `read-only`; `8,000`; `antiphon retrieve`; `retention`/`compact`; `unavailable`; `two copies`; `untrusted`; `[Antiphon attachment]`; `.antiphon/messages/`; `first blank line`; `shasum`; `own words`; `this same machine`; `7 days`; `eligible for removal`; `no timer`; `is not parked`; `passive pages`; ``@codex` line is not parked`` (Claude) / ``@claude` line is not parked`` (Agents); `has_more`; `one page`; `has_more_scope`; `catalogued project sources`; `building`; `degraded`; `incomplete`; `drain`; Agents also: `again`, `upgrade`, `cursor recovery`, `duplicate`, `disappear`, `nothing is read or marked seen`, `next automatic prompt`, `refus`, `truncat`, `whole`, `[from=`, `<unnamed>`, `antiphon_send(to=`, `@claude:<alias>`, and every `TOOLS` name in backticks. New on both rules: one sentence on the horizon (`older than 24 hours` … `skipped:`).

The shared texts:

```python
TOOL_RETRIEVAL_RULE = (
    "Every tool line carries a content-bound `tc1` id: `antiphon_retrieve(id=...)` "
    "returns that invocation only, never the tool result, read-only and "
    "cursor-neutral; above 8,000 bytes it refuses and `antiphon retrieve <id>` "
    "prints it whole. Host retention or `antiphon sources compact` can make an "
    "id `unavailable`; two copies of one transcript make retrieval `untrusted`.")

MULTILINE_MARKER_RULE = (
    "A marker can carry a block: make its one-line message exactly `<<TOKEN` "
    "(TOKEN matches `[A-Z][A-Z0-9_]{0,31}`), put the body on the following "
    "lines and close it with an exact `TOKEN` line. Blocks do not nest and the "
    "closer is not fence-aware, so choose a token absent from the body; "
    "marker-looking lines inside are content; a malformed or unclosed block "
    "sends nothing from that turn; literal text beginning with `<<` goes inside "
    "a block body.")

LONG_MARKER_RULE = (
    "Use `{tool}` for long content: an oversized direct-tool message is parked "
    "as an attachment, while an oversized Stop-marker block is refused and not "
    "parked.")

AUTOMATIC_PEER_IDENTITY_RULE = (
    "Without `ANTIPHON_NAME` a session may get an automatic `auto-` peer alias "
    "— Codex once its first hook proves the session live, Claude from a fixed "
    "Claude probe of its own session (the host display name is ignored) — and "
    "every census is `at least N` because a session before its first hook may "
    "be invisible. `ANTIPHON_NAME` overrides it. One positively live automatic "
    "peer is the only case a bare send may choose; two or more candidates make "
    "a bare send refused. The session id, identity digest, owner key and socket "
    "route stay private: status, doctor, labels, refusals and errors expose "
    "only the public alias. After a session rotates its old alias stops "
    "resolving and the new one is unreachable until a fresh endpoint exists (an "
    "MCP reconnect); an identity whose owner cannot be proved live is counted, "
    "never addressed.")
```

`CLAUDE_RULE` (four paragraphs: the page; the event and the reply; addressing; attachments) and `AGENTS_RULE` (five: the page and `antiphon_read`; Claude's messages and the label; markers and `antiphon_send`; addressing; attachments) are written to the pin list above; the channel `instructions` carry the event tag, the reply rule, `reply_to=<unavailable>`, the delivery notice, the identity rule, the marker rules and the retrieval rule. The `antiphon_read` description loses its second half (the oversized-record story stays: pinned).

- [ ] **Step 4: Green** — `/usr/bin/python3 -m unittest test.test_contracts` and every class from the grep; also `node --check lib/channel.mjs`. Every pin failure is a phrase the new text must carry; put it back rather than loosening the test, unless the sentence was describing the implementation (say so in the commit).

- [ ] **Step 5: Commit** — `The agent surfaces say the contract in under 3,000 bytes each`.

---

### Task 6: Doctor sees a stale rule section

**Files:**
- Modify: `lib/antiphon.py` — `_rule_section(text)` beside `_update_instructions` (~:7478), used by both; `_config_state` (~:8148) reads `AGENTS.md`/`CLAUDE.md`; `_doctor_config` (~:8606) verdicts
- Test: `test/test_antiphon.py` — `DoctorTest` (~:4367), using `self.project()`, `self.set_up()`, `self.run_doctor()`, `self.line_for()`

- [ ] **Step 1: Failing tests**

```python
    def test_doctor_names_a_stale_rule_section(self):
        project = self.project()
        self.set_up(project)
        with open(os.path.join(project, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write("# Mine\n\n## The Antiphon bridge\n\nold words\n\n## After\n\nkept\n")
        code, printed = self.run_doctor(project)
        self.assertEqual(code, 1)
        line = self.line_for(printed, "CLAUDE.md")
        self.assertTrue(line.startswith("✗ CLAUDE.md: the Antiphon section is stale"), line)
        self.assertIn("run `antiphon setup`", line)
        self.assertTrue(self.line_for(printed, "AGENTS.md").startswith("✓"), printed)

    def test_doctor_names_a_missing_rule_section(self):
        project = self.project()
        self.set_up(project)
        os.unlink(os.path.join(project, "AGENTS.md"))
        code, printed = self.run_doctor(project)
        self.assertEqual(code, 1)
        line = self.line_for(printed, "AGENTS.md")
        self.assertTrue(line.startswith("✗ AGENTS.md: the Antiphon section is missing"), line)

    def test_doctor_fix_repairs_a_stale_rule_section(self):
        project = self.project()
        self.set_up(project)
        with open(os.path.join(project, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write("## The Antiphon bridge\n\nold words\n")
        with patch.object(antiphon, "project_dir", return_value=project), \
             self.hermetic(project), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(antiphon.doctor("--fix"), 0)
        with open(os.path.join(project, "CLAUDE.md"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), antiphon.CLAUDE_RULE.strip())
```

Read `run_doctor`/`hermetic` first: the healthy fixture must keep exiting 0 after `set_up`, so the new verdict is `✓` on a freshly set-up project (the existing "prints only ✓ and ·" test proves it).

- [ ] **Step 2: Red** — no such lines; `--fix` test passes already if setup rewrites the section (keep it: it pins the repair).
- [ ] **Step 3: Implement** — `_rule_section(text)` returns the `## The Antiphon bridge` section (to the next `\n## ` or the end) or None; `_update_instructions` uses it; `_config_state` adds `"AGENTS.md"` and `"CLAUDE.md"` as `ConfigState(path, text or "", None)` (unreadable → reason); `_doctor_config` ends with, for each `(name, rule)` in `(("AGENTS.md", AGENTS_RULE), ("CLAUDE.md", CLAUDE_RULE))`: unreadable → `bad`; section None → `bad(f"{name}: the Antiphon section is missing — run `antiphon setup`")`; `section.strip() != rule.strip()` → `bad(... is stale ...)`; else `ok(f"{name}: the Antiphon section is current")`.
- [ ] **Step 4: Green; mutate** (compare with `==` on the raw text instead of `.strip()` → the stale test still red? no — mutate the verdict to `ok` always → both tests red); the `DoctorTest` class whole; commit `Doctor sees a stale rule section; setup and doctor --fix repair it`.

---

### Task 7: Words and the live project

- [ ] BACKLOG: new entry `## P1 — Token cost of the passive page and the static surfaces (fixed)` carrying the measured table, the five shapes, the horizon decision (and the sentence it reverses: "repeat rather than skip" for untrusted cursors stays for *what is inside the horizon*; beyond it, skipping is announced), the surface sizes before/after, doctor's new check; the census entry re-dated. README §Limits already carries the horizon (Task 4); README §How it works gets one sentence about host records not being speech.
- [ ] Contract tests for the BACKLOG words as this repository does elsewhere (`section(...)` on the new entry: `skipped:`, `24 hours`, `external agent`, `isCompactSummary`, `codex_internal_context`).
- [ ] Commit `Token discipline, in words`.
- [ ] After the merge to `main`: run `antiphon setup` in the live project (its `CLAUDE.md`/`AGENTS.md` are 0.3.2-era and untracked) and `antiphon doctor`; expect `✓` on both rule sections and on the `antiphon_retrieve` permission that was `✗` at the start of the campaign.

---

### Task 8: Verification on the exact SHA

- [ ] `git status --short` empty; `SHA=$(git rev-parse HEAD)`.
- [ ] `/usr/bin/python3 -m unittest discover -s test`; `ANTIPHON_NAME=ui /usr/bin/python3 -m unittest discover -s test`; then `npm test` (Anaconda 3.14 + Node), never concurrently.
- [ ] `git diff --check main "$SHA"`; `py_compile`; `node --check`.
- [ ] Read-only measurement on the live project: `antiphon status` — the `unread claude_pages` line names the horizon skip; an in-memory drain from the live cursor (the scratch measurement used before this plan) is ≤ 25 pages.
- [ ] `fresh-user.sh` from a temporary worktree at `$SHA`.
- [ ] An independent read-only review of `git diff main..$SHA`; a Codex review attempted over the bridge.

## Self-review

- **Spec coverage:** §2a → Tasks 1–3; §2b → Task 4; §2c → Task 5; §2d → Task 6; words → Task 7; ritual → Task 8.
- **Placeholders:** none; every step shows code or the exact command.
- **Types:** `_apply_horizon(source, start) -> (int, int)`; `first_record_at(offset) -> (int, int, str) | None`; `reader_backlog` five-tuple everywhere it is unpacked (`_backlog_line`, `_doctor_replay`, two tests); `skipped` is `dict[str, int]` from the readers to `_render_page`.
- **Open for critique:** the horizon's residual — a source whose timestamps are misordered by more than `HORIZON_BISECT_SLACK` bytes can lose the misordered records; both hosts append records in time order, and the slack is stated as the bound rather than claimed away.
