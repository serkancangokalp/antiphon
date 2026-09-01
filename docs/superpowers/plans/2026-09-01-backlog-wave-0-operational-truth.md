# Backlog Wave 0: Operational Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make setup, status, doctor and the release census tell one truthful,
measured story without mutating runtime state or claiming candidate fixes were
already published.

**Architecture:** Keep `lib/antiphon.py` as the owner of setup and diagnostics.
Extract only pure shared shapes and target selection inside that module, reuse
the existing content-free socket probe, and make `doctor --fix` a composition
of the existing setup writer followed by the existing read-only doctor. Add a
repository-only census utility for repeatable host measurements.

**Tech Stack:** Python 3.9+ standard library and `unittest`; Bash fresh-user
E2E; existing Node/MCP integration tests.

**Spec:**
`docs/superpowers/specs/2026-09-01-backlog-wave-0-operational-truth-design.md`

## Global Constraints

- Work only in `.worktrees/p0-claude-identity`; never modify the live project
  `.antiphon/cursor.json` or peer registry.
- Default `doctor` remains read-only and `status` remains exit 0.
- `doctor --fix` writes project configuration only through `setup()`; it never
  deletes sockets, registry records, cursors, queues, transcripts or
  attachments and never starts/restarts a host process.
- Preserve thread-lock and read-only queue evidence; decline only active Codex
  reachability by executing `codex`.
- Do not add `TZ=UTC` to doctor's process table. `peers.py` compares rendered
  process fingerprints under UTC; doctor parses local `ps lstart` into local
  epoch time. Those are intentionally different operations.
- No push, `npm version`, or publish.

---

### Task 1: One configuration envelope for setup and doctor

**Files:**

- Modify: `lib/antiphon.py` near `HOOK_COMMAND`, `hook_installed`, `_add_hook`,
  `setup`, and `_doctor_config`
- Modify: `test/test_antiphon.py` in `SetupShapeCharacterizationTest`

**Interfaces:**

- Produces: `CONFIG_KEYS`, one immutable mapping/named tuple containing
  `hooks`, `hook_entries`, `hook_type`, `hook_command`, `hook_status`,
  `permissions`, `allow`, `mcp_servers`, and `enabled_mcp_servers`.
- Consumed by: `_add_hook`, `_dedupe_hooks`, `hook_installed`, setup mutation
  closures, and `_doctor_config`.
- Preserves: every on-disk key and the existing unknown-field behavior.

- [ ] **Step 1: Write a failing shared-shape characterization test**

Add a test that replaces the shared key object with alternate fixture names
and proves both the writer helper and reader helper follow it:

```python
def test_hook_writer_and_reader_consume_one_envelope(self):
    keys = antiphon.CONFIG_KEYS._replace(
        hooks="events", hook_entries="commands",
        hook_type="kind", hook_command="run", hook_status="label")
    data = {}
    shape = antiphon.HookShape("x.json", "Stop", "antiphon push codex", "Bridge")
    with patch.object(antiphon, "CONFIG_KEYS", keys):
        groups = data.setdefault(keys.hooks, {}).setdefault(shape.event, [])
        antiphon._add_hook(groups, shape.command, label=shape.label)
        self.assertTrue(antiphon.hook_installed(data, shape))
    self.assertEqual(data, {"events": {"Stop": [{"commands": [{
        "kind": "command", "run": "antiphon push codex", "label": "Bridge",
    }]}]}})
```

Extend the full-shape setup test to assert the real keys stay byte-compatible.

- [ ] **Step 2: Run the focused test and observe the missing interface**

Run:

```sh
python3 -m unittest \
  test.test_antiphon.SetupShapeCharacterizationTest.test_hook_writer_and_reader_consume_one_envelope
```

Expected: error because `CONFIG_KEYS` does not exist or the helpers still use
literal keys.

- [ ] **Step 3: Add the immutable key vocabulary and route both sides through it**

Define:

```python
ConfigKeys = collections.namedtuple(
    "ConfigKeys",
    "hooks hook_entries hook_type hook_command hook_status "
    "permissions allow mcp_servers enabled_mcp_servers")
CONFIG_KEYS = ConfigKeys(
    "hooks", "hooks", "type", "command", "statusMessage",
    "permissions", "allow", "mcpServers", "enabledMcpjsonServers")
```

Replace the corresponding literals in the named setup/doctor helpers. Do not
replace unrelated protocol keys such as `args`, `env`, or cursor fields.

- [ ] **Step 4: Run setup/doctor shape tests**

Run:

```sh
python3 -m unittest \
  test.test_antiphon.SetupShapeCharacterizationTest \
  test.test_antiphon.DoctorTest.test_doctor_passes_on_a_set_up_project \
  test.test_antiphon.DoctorTest.test_doctor_survives_a_malformed_config
```

Expected: all pass and existing real file shapes remain unchanged.

- [ ] **Step 5: Commit the shared envelope**

```sh
git add lib/antiphon.py test/test_antiphon.py
git commit -m "refactor: share setup and doctor config envelopes"
```

### Task 2: Make status mean an answering channel

**Files:**

- Modify: `lib/antiphon.py` near `_probe_channel`, `_doctor_channel`, and
  `status`
- Modify: `test/test_antiphon.py` in `StatusTest` and `DoctorTest`

**Interfaces:**

- Produces: `ChannelTarget(name, path, state, patient)` and
  `_channel_targets(cwd, live_records) -> list[ChannelTarget]`.
- Produces: `_channel_answering(cwd, live_records) -> bool`.
- Consumed by: `status` and `_doctor_channel`.
- Preserves: `_probe_channel(path, patient) -> Probe(error, answered)`.

- [ ] **Step 1: Rewrite the two old path/registry assertions as failing probe assertions**

Replace `test_a_registered_claude_peer_is_never_reported_as_down` and
`test_with_nothing_registered_the_legacy_socket_still_decides` with tests that
patch `_probe_channel` and assert the exact target/patience calls:

```python
def test_a_registered_peer_is_live_only_when_its_channel_answers(self):
    with tempfile.TemporaryDirectory() as project:
        antiphon.peers.register(project, "claude", "ui", "/tmp/ui.sock",
                                pid=os.getpid())
        with patch.object(antiphon, "_probe_channel",
                          return_value=antiphon.Probe(None, False)) as probe:
            _, down = self._status(project)
        probe.assert_called_once_with("/tmp/ui.sock", patient=True)
        self.assertIn("Claude channel:     down", down)

def test_an_idle_project_probes_legacy_once_without_patience(self):
    with tempfile.TemporaryDirectory() as project, \
         patch.object(antiphon, "_probe_channel",
                      return_value=antiphon.Probe(errno.ENOENT, False)) as probe:
        _, text = self._status(project)
    probe.assert_called_once_with(antiphon.claude_socket_path(project), patient=False)
    self.assertIn("Claude channel:     down", text)
```

Add separate cases for an answering registered peer, an answering
unregistered configured alias, a stale socket, and a binder returning bytes
without the Antiphon `ok` shape. Patch `_probe_channel` in pre-existing status
tests whose purpose is peer formatting so their expected `live` result remains
about that purpose.

- [ ] **Step 2: Run the focused status tests and observe registry/path false positives**

Run:

```sh
python3 -m unittest test.test_antiphon.StatusTest
```

Expected: the new tests fail because `status` never calls `_probe_channel`.

- [ ] **Step 3: Extract target selection and use the probe in status**

Implement the pure target selector with the same rules doctor currently owns:

```python
ChannelTarget = collections.namedtuple(
    "ChannelTarget", "name path state patient")

def _channel_targets(cwd, live):
    claude = [record for record in live if record.get("kind") == "claude"]
    requested = sender_alias(peers.explicit_name())
    registered = {record.get("name") for record in claude}
    targets = [ChannelTarget(record.get("name"), peers._address_of(record),
                             "registered", True)
               for record in claude]
    if requested and requested not in registered:
        targets.insert(0, ChannelTarget(
            requested, claude_socket_path(cwd, requested), "unregistered", False))
    if not claude and not requested:
        targets.append(ChannelTarget(
            None, claude_socket_path(cwd), "legacy", False))
    return targets

def _channel_answering(cwd, live):
    return any(_probe_channel(target.path, patient=target.patient).answered
               for target in _channel_targets(cwd, live))
```

Make `_doctor_channel` iterate the same `ChannelTarget` values and preserve its
existing verdict text. Make `status` print `live` only from
`_channel_answering`.

- [ ] **Step 4: Prove target sharing and zero idle retry**

Run:

```sh
python3 -m unittest \
  test.test_antiphon.StatusTest \
  test.test_antiphon.DoctorTest.test_doctor_probes_the_socket_not_the_file \
  test.test_antiphon.DoctorTest.test_doctor_reports_a_named_channel_with_no_live_endpoint_record
```

Expected: all pass. Add an assertion that `_probe_channel` receives
`patient=False` for the normal missing legacy path; do not test elapsed wall
time where a call assertion proves the rule directly.

- [ ] **Step 5: Commit truthful status reachability**

```sh
git add lib/antiphon.py test/test_antiphon.py
git commit -m "fix: make status require an answering channel"
```

### Task 3: Add configuration-only `doctor --fix`

**Files:**

- Modify: `lib/antiphon.py` usage text, doctor entry point, and CLI argument
  behavior
- Modify: `test/test_antiphon.py` in `DoctorTest`
- Modify: `test/e2e/fresh-user.sh` T1

**Interfaces:**

- Produces: `_doctor_readonly() -> int`, the present doctor body.
- Produces: `doctor(mode=None) -> int`, accepting only `None` or `"--fix"`.
- Consumes: existing `setup() -> int` without intercepting its output.

- [ ] **Step 1: Write failing unit tests for mode, refusal, and remaining faults**

Add these behaviors:

```python
def test_doctor_fix_runs_setup_then_a_read_only_recheck(self):
    project = self.project()
    with patch.object(antiphon, "project_dir", return_value=project), \
         patch.object(antiphon, "setup", return_value=0) as setup, \
         patch.object(antiphon, "_doctor_readonly", return_value=0) as recheck:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = antiphon.doctor("--fix")
    self.assertEqual(code, 0)
    setup.assert_called_once_with()
    recheck.assert_called_once_with()
    self.assertIn("configuration only", out.getvalue())

def test_doctor_fix_keeps_setup_refusal_nonzero(self):
    with patch.object(antiphon, "setup", return_value=1), \
         patch.object(antiphon, "_doctor_readonly", return_value=0):
        self.assertEqual(antiphon.doctor("--fix"), 1)

def test_doctor_fix_keeps_remaining_runtime_fault_nonzero(self):
    with patch.object(antiphon, "setup", return_value=0), \
         patch.object(antiphon, "_doctor_readonly", return_value=1):
        self.assertEqual(antiphon.doctor("--fix"), 1)
```

Add a subprocess test that `doctor nonsense` exits 2 and help names the exact
write boundary.

- [ ] **Step 2: Run the focused tests and observe the arity/mode failure**

Run:

```sh
python3 -m unittest test.test_antiphon.DoctorTest
```

Expected: the new calls fail because `doctor` accepts no argument and there is
no `_doctor_readonly` seam.

- [ ] **Step 3: Split the read-only body and compose the repair mode**

Move the existing doctor implementation byte-for-byte into
`_doctor_readonly()`. Implement:

```python
def doctor(mode=None):
    if mode is None:
        return _doctor_readonly()
    if mode != "--fix":
        print(f"antiphon: doctor accepts only --fix, got {mode}", file=sys.stderr)
        return 2
    print("repair: project configuration only; runtime state is not changed")
    setup_status = setup()
    print("\n— doctor re-check (read-only) —")
    doctor_status = _doctor_readonly()
    return 1 if setup_status or doctor_status else 0
```

Update the module usage text. Keep default doctor snapshot tests pointed at
`doctor()` so they continue proving the public default writes nothing.

- [ ] **Step 4: Add a real fresh-user repair gate**

In E2E T1, after initial clean doctor:

1. use a fixed Python argv to remove only `mcpServers.antiphon` from the
   throwaway project's `.mcp.json`;
2. assert ordinary doctor exits 1 and names the missing server;
3. run `antiphon doctor --fix`, capture stdout/stderr and exit;
4. assert it exits 0, includes a setup repair line and the read-only re-check
   boundary;
5. assert a following ordinary doctor is clean;
6. re-check the global Codex config hash.

Do not execute a command read from the file under test.

- [ ] **Step 5: Run unit tests and the shell syntax check**

```sh
python3 -m unittest test.test_antiphon.DoctorTest
bash -n test/e2e/fresh-user.sh
```

Expected: both pass.

- [ ] **Step 6: Commit the explicit repair workflow**

```sh
git add lib/antiphon.py test/test_antiphon.py test/e2e/fresh-user.sh
git commit -m "feat: add configuration-only doctor repair"
```

### Task 4: Make the host-wrapper census repeatable

**Files:**

- Create: `test/host_wrapper_census.py`
- Create: `test/test_host_wrapper_census.py`
- Modify: `BACKLOG.md` release-census entry after the real run

**Interfaces:**

- Produces: `opening_tag(text) -> str | None`.
- Produces: `claude_user_blocks(record) -> list[(text, prompt_source)]`.
- Produces: `codex_user_blocks(record) -> list[(text, None)]`.
- Produces: `census(claude_root, codex_root) -> dict` containing aggregate file,
  block, tag and prompt-source counts only.
- CLI: `python3 test/host_wrapper_census.py --claude-root PATH --codex-root PATH`.

- [ ] **Step 1: Write failing fixture tests**

Create temporary Claude and Codex JSONL trees containing:

- a Claude `type=user` string block with `promptSource=system`;
- a Claude list content block with `promptSource=sdk`;
- a Codex `response_item/payload.type=message/role=user` block using
  `input_text`;
- one person's `<html>` text;
- malformed JSON and non-user records;
- a secret sentence after each opening tag.

Assert exact aggregate keys such as:

```python
self.assertEqual(result["claude"]["tags"]["channel"]["system"], 1)
self.assertEqual(result["claude"]["tags"]["task-notification"]["sdk"], 1)
self.assertEqual(result["codex"]["tags"]["environment_context"]["<absent>"], 1)
```

Invoke the CLI and assert none of the secret sentences or individual JSONL
paths appears in output.

- [ ] **Step 2: Run the new tests and observe the missing module**

```sh
python3 -m unittest test.test_host_wrapper_census
```

Expected: import failure because `host_wrapper_census.py` does not exist.

- [ ] **Step 3: Implement side-specific parsing and aggregate-only output**

Use recursive `glob("**/*.jsonl", recursive=True)`, parse one line at a time,
and match only a complete opening name with:

```python
OPENING_TAG = re.compile(r"^\s*<([A-Za-z][A-Za-z0-9_-]*)(?=[\s>/])")
```

For Claude, accept `message.content` as either a string or a list of `text`
blocks and retain `promptSource` as `"<absent>"` when missing. For Codex,
accept only `response_item` message payloads with `role=user`, join `text` and
`input_text` blocks, and use `"<absent>"` for the unavailable source field.
Malformed lines increment a malformed counter and do not abort the census.
Print sorted JSON or stable sorted text containing counts only.

- [ ] **Step 4: Run fixture tests and the real read-only census**

```sh
python3 -m unittest test.test_host_wrapper_census
python3 test/host_wrapper_census.py \
  --claude-root "$HOME/.claude/projects" \
  --codex-root "$HOME/.codex/sessions"
```

Compare every observed tag on each side to `CLAUDE_HOST_WRAPPERS` and
`CODEX_HOST_WRAPPERS`. Do not add a tag by symmetry. If a new tag appears,
inspect only the minimum records needed to decide host bookkeeping versus user
content and record the evidence; otherwise record “no set change.”

- [ ] **Step 5: Commit the repeatable census and measured ledger update**

```sh
git add test/host_wrapper_census.py test/test_host_wrapper_census.py BACKLOG.md
git commit -m "test: make the host wrapper census repeatable"
```

### Task 5: Reconcile the operational backlog

**Files:**

- Modify: `BACKLOG.md` at the source-label deferral, doctor future, named-Claude
  follow-up and wrapper-census sections
- Modify: `test/test_contracts.py` only where it pins stale open-item wording

**Interfaces:**

- Consumes: behavior delivered by Tasks 1–4 and process-fingerprint commits
  `a4533d1`/`6902546` already on this branch.
- Produces: no vague future bullet for a behavior already implemented or
  deliberately declined.

- [ ] **Step 1: Write/update lexical contract assertions before prose**

Pin these facts in `test/test_contracts.py`:

- default doctor is read-only and `--fix` says configuration-only;
- status liveness requires an answering Antiphon listener;
- active Codex reachability is declined until a non-spawning bounded API
  exists, while thread-lock/queue evidence remains supported;
- published 0.3.3 is distinguished from the candidate fingerprint fix;
- recurring wrapper census names the checked utility.

Run the exact new tests and observe the stale BACKLOG text fail.

- [ ] **Step 2: Correct stale and contradictory entries**

Update the four stale statements identified in the spec. Move delivered
`--fix` and measured alias forwarding out of “Still future.” Replace the two
active Codex probe bullets with the explicit decline/revisit condition. State
that ordinary EOF/signal orphaning is fixed and abrupt process death remains a
recovery case. Preserve the candidate-versus-published wording until release.

- [ ] **Step 3: Run contract and operational tests**

```sh
python3 -m unittest \
  test.test_contracts \
  test.test_antiphon.SetupShapeCharacterizationTest \
  test.test_antiphon.DoctorTest \
  test.test_antiphon.StatusTest \
  test.test_host_wrapper_census
git diff --check
```

Expected: all pass.

- [ ] **Step 4: Commit the reconciled ledger**

```sh
git add BACKLOG.md test/test_contracts.py
git commit -m "docs: reconcile operational backlog evidence"
```

### Task 6: Verify Wave 0 on one exact commit

**Files:**

- No planned product edits
- Read-only verification of the committed tree

**Interfaces:**

- Produces: one exact candidate SHA with complete test, E2E, census and review
  evidence.

- [ ] **Step 1: Run the full local suite**

```sh
npm test
```

Expected: all Python and Node tests pass; the two intentional Python skips may
remain.

- [ ] **Step 2: Inspect the complete wave diff and cleanliness**

```sh
git diff --check 2b3ac64..HEAD
git diff --stat 2b3ac64..HEAD
git status --porcelain=v1 --untracked-files=all
git rev-parse HEAD
```

Expected: no diff-check output, no status output, and one recorded SHA.

- [ ] **Step 3: Run fresh-user E2E from that exact clean commit**

```sh
test/e2e/fresh-user.sh
```

Expected: every assertion passes and the summary prints the same SHA from Step
2. Record any intentionally retained Codex rollout path.

- [ ] **Step 4: Re-run the wrapper census from the exact tree**

```sh
python3 test/host_wrapper_census.py \
  --claude-root "$HOME/.claude/projects" \
  --codex-root "$HOME/.codex/sessions"
```

Expected: stable aggregate output matching the evidence committed in BACKLOG.

- [ ] **Step 5: Request independent exact-SHA review**

Ask the existing read-only Codex reviewer to inspect the exact SHA, with focus
on default-doctor zero writes, `--fix` refusal propagation, target/patience
sharing, secret-free census output, test honesty and the full Wave 0 diff.

- [ ] **Step 6: Stop at the local reviewed candidate**

If review finds anything, receive it through
`superpowers:receiving-code-review`, reproduce it, add a red test, fix it and
repeat Steps 1–5 on the new SHA. When review closes, report Wave 0 complete and
begin brainstorming Wave 1. Do not merge/push/version/publish.
