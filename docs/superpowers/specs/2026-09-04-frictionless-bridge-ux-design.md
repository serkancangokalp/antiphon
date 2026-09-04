# Frictionless Bridge UX Foundation

**Date:** 2026-09-04

**Status:** approved for autonomous implementation by the maintainer's overnight instruction

## Product position

Antiphon is not a task board, IDE, or replacement runtime for Claude Code and
Codex. It is the thin, identity-preserving bridge between their native
sessions. Its advantage is that it derives content from the transcripts the
hosts already own, keeps routing metadata separate from message content, and
refuses to guess when identity or delivery is ambiguous.

That position matters more now that Claude Code has native named cross-session
messaging with `Delivered`, `Held`, and `Refused` outcomes, one-shot idle
notifications, and a session view. Antiphon should complement that native
Claude-to-Claude surface by making Claude-to-Codex exchange exceptionally
truthful and easy, not duplicate it with a second team manager.

## Evidence

### Live Antiphon failure

A passive page rendered on 4 September contained records from 3 September and
4 September, but every record had only `[HH:MM]`. Claude Opus interpreted old,
completed release work as current work. The 24-hour page horizon bounded the
amount of history but did not tell the reader which day each record belonged
to. This is a user-visible truth defect, not a cosmetic preference.

The install flow has a second avoidable cliff: after `antiphon setup`, a person
must remember a preview-only Claude flag, hook approval, `ANTIPHON_NAME`, and
when to reconnect. The setup command prints shell fragments rather than giving
the person one safe command that owns Antiphon's launch requirements.

The handed-task path has a third truth defect. It transports a task before it
creates the task and delivery records, ignores failed final writes, and still
promises that `status` will show a receipt. A transport success must never be
reported as a fully tracked task when persistence failed.

Finally, the host-wrapper census recursively scans nested Claude subagent
transcripts even though production Claude discovery admits only immediate
project transcript files. Five observed `<fork-boilerplate>` records were all
outside production's admitted set. A release decision based on the current
census could therefore add a destructive production filter for text production
never sees.

### Comparable products

- [Claude Code cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)
  provides named direct messages, `Delivered`/`Held`/`Refused`, one-shot idle
  notifications, and compact previews. It is the strongest model for explicit
  delivery states and no-poll notification.
- [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams) adds a
  shared dependency-aware task list and teammate mailboxes, with explicit
  overhead and shared-file conflict cautions. Antiphon does not need to own a
  second task universe to bridge two existing sessions.
- [Conductor parallel agents](https://www.conductor.build/docs/concepts/parallel-agents)
  makes the isolation decision explicit: separate workspaces for independently
  shippable work, one shared workspace for tightly coupled review/fix loops.
- [Claude Squad](https://github.com/smtg-ai/claude-squad) and
  [Crystal](https://github.com/stravu/crystal/blob/main/CLAUDE.md) show the value
  of one visible session surface, persisted state, isolated worktrees, and
  review-before-apply. They do not document a truth-preserving cross-vendor
  message bus.
- [Vibe Kanban](https://www.vibekanban.com/docs/workspaces/interface) and its
  [review flow](https://www.vibekanban.com/docs/reviewing-code) show useful
  `Running`/`Idle`/`Needs Attention` grouping and feedback on diffs, but its
  broader IDE/Kanban scope is not Antiphon's job.
- [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) shows
  durable inbox/outbox receipts, threads, and recovery diagnostics. Its second
  identity system, database-backed content store, and file leases conflict with
  Antiphon's native-identity and no-shared-message-log boundaries.

## This milestone

The milestone contains independently testable slices. They share one goal:
the first five minutes and every passive page must tell the truth without the
user memorising host-specific incantations.

### 1. Date-qualified passive records

- A record whose local calendar date matches the page render date remains
  byte-for-byte `[HH:MM]`.
- A record on any other local date renders `[YYYY-MM-DD HH:MM]`.
- Future-dated and timestamp-zero records use the same absolute form when the
  local platform can represent them. A parser-accepted epoch outside the host
  clock's range renders `time unavailable` rather than dropping the page.
  Antiphon does not invent a relative age or say a clock is wrong.
- Rendering continues to use one local-time interpretation, as it does today.
- The longer label is charged to the existing 8,000-byte page budget. Whole
  records remain atomic, including the existing single-oversized-record escape.
- Cursor, catalog, ordering, horizon, and replay semantics do not change.
- The generated `AGENTS.md`/`CLAUDE.md` bridge rules and README state this
  visible contract.

This chooses a conditional absolute timestamp over day separators. It is eleven
bytes larger per dated non-tool run, so stale history can fit fewer records in a
page, but each record remains self-contained when copied, replayed, or rendered
alone and the existing page-budget loop accounts for the tradeoff exactly.

### 2. Guided, shell-free host launch

Add:

```text
antiphon launch claude [--name NAME] [-- HOST_ARGS...]
antiphon launch codex  [--name NAME] [-- HOST_ARGS...]
```

Contract:

- Only `claude` and `codex` are accepted. Syntax failures exit 2 without
  starting a process.
- `--name` is optional, accepted once, normalised to lower case, and validated
  with the public Antiphon alias grammar. If omitted, a valid existing
  `ANTIPHON_NAME` is preserved; an invalid one is refused rather than silently
  dropped.
- Tokens after the first exact `--` are passed to the host byte-for-byte as
  argument strings and in the same order, except for the explicit readiness
  guard below. A second `--` is the host's own option boundary, after which
  even a flag-looking token is literal content. Before the first boundary,
  only Antiphon's `--name` option is accepted.
- Claude receives exactly one
  `--dangerously-load-development-channels server:antiphon` pair. A caller who
  repeats that Antiphon-owned option, including its `--flag=value` spelling, is
  refused with a direct remedy.
- Codex receives no extra host option.
- Refuse host options that invalidate the project just preflighted: Claude
  `--bare`, `--restricted`, `--safe-mode`, `--strict-mcp-config`,
  `--no-session-persistence`, `--setting-sources`, and `--worktree`/`-w`;
  Codex `--cd`/`-C`. Long options are guarded in split and `--flag=value`
  spellings; the short forms include attached values (`-wNAME`, `-CDIR`) and
  their `=...` variants. The remedy is to remove the option or start the
  intentionally incompatible host manually.
- Refuse truthy (`1`, `true`, `yes`, `on`, case- and surrounding-space-
  insensitive) `CLAUDE_CODE_SAFE_MODE`, `CLAUDE_CODE_SIMPLE`, and
  `CLAUDE_CODE_RESTRICTED`. False values remain valid. The manual path is the
  deliberate escape hatch for these modes too.
- The host executable must resolve on `PATH`.
- An inherited `ANTIPHON_CWD` must resolve to the same real directory as the
  launcher's process cwd. A stale cross-project value is refused before host
  lookup or configuration preflight; an accepted explicit value is normalised
  to an absolute path before exec.
- Project configuration is inspected read-only through the same checks as
  `doctor`. Incomplete or stale configuration refuses launch with one action:
  `antiphon setup`, then retry. Launch never rewrites configuration.
- The host is resolved once and that exact path is started with `os.execve`, an
  argv array, and a copied environment: no second `PATH` lookup, shell, quoting
  ambiguity, intermediary host process, or swallowed signal.
- Arguments after `--` are a person's direct interactive host request. Beyond
  the readiness guard above, launch deliberately does not apply the managed-
  worker permission filter to them; managed workers remain restricted because
  they are autonomous subprocesses.
- Setup output and README use these two commands as the default path. The raw
  host commands remain documented as an advanced/manual alternative.

### 3. Honest handed-task tracking

- Validate `to` with the public peer-name grammar before writing anything.
- Create and exact-read-back a `handing` task record before any transport call.
  If that preparation fails, transport is not attempted. `handing` consumes no managed
  worker slot and is not swept by the 60-second worker-start patience rule.
- Never hold a task-store or ledger lock across transport.
- A definite transport refusal removes the prepared task. A newly parked
  attachment is removed as today. If task removal itself fails, retain the
  explicit `delivery_refused` state and name its ID instead of leaving a
  misleading `handing` record.
- One UUID is the shared attempt ID printed to the peer and returned as both
  `task_id` and `delivery_id`; these names identify two views of one attempt,
  not two unrelated identifiers.
- After transport success, write and read back the delivery ledger entry, then
  move the task from `handing` to `handed` and read it back. Return ordinary
  `state: "handed"` only when the task record exactly matches its preparation
  plus the final state and every supplied delivery field matches: shared ID,
  sender and sender kind, recipient and recipient kind, proof, transport,
  digest, size, attachment, and the exact pre-transport `sent_at`.
- A lost acknowledgement after bytes may have left is not a refusal. Preserve
  the task ID and any parked attachment, record delivery state `unknown`, return
  `tracking: "incomplete"`, and suppress automatic retry. A later transcript
  receipt resolves the delivery entry to `sent`.
- Direct sends make their separate Stop-marker fingerprint write observable as
  `dedupe_recorded`. If it fails, the result warns the caller to remove the same
  addressed marker because automatic suppression is not durable. An unknown
  Stop-marker whose delivery-ledger entry cannot be persisted still records its
  dedupe fingerprint but returns nonzero so that loss is not hidden on hook
  stderr.
- If the ledger or task finalisation is incomplete after transport succeeded,
  never delete the peer-visible local ID. Move it to `tracking_incomplete` when
  possible; if even that write fails, the original `handing` record remains.
  Return the retained record's state, `tracking: "incomplete"`, both ID names,
  and text saying the message was delivered or queued and must not be retried
  automatically because the peer may already act. `result` and `cancel` refuse
  both incomplete states; neither promises a receipt or a result by ID.
- A ledger-write failure leaves no claimed ledger entry and a retained local
  incomplete record. A task-transition failure leaves the accurate ledger entry
  and a retained local incomplete record. Both shapes describe only what was
  actually persisted.
- A same-kind Claude handoff accepted by a listener from before sender-kind
  support remains a successful handoff, but both complete and tracking-
  incomplete results carry the existing reconnect warning: the recipient saw
  the words under the wrong Codex origin label.
- Task and delivery records carrying the new states use schema v2. New readers
  remain compatible with all shipped v1 states, including v1 `handed`. An old
  long-lived server cannot read v2 and must be restarted; the existing
  code-mtime diagnostic names that restart after an on-disk update.
- A v2 delivery always carries an explicit `sender_kind`; only a v1 delivery
  may omit it and use the historical opposite-kind inference.
- Receipt still means transcript observation, never task acceptance or
  completion.

This is deliberately not a peer-reported task protocol. Acceptance, progress,
and completion need a separate authenticated sidecar and report tool if repeated
use proves the need; transcript inference is out of scope.

### 3a. Receipt-first and exact-retry durability

- A transcript receipt may become durable inside the transport call, before
  the sender can write its delivery row. Receipt processing therefore shares
  the delivery flock and writes a bounded proof sidecar before its page cursor
  advances when no usable row exists.
- Received proofs are keyed by immutable delivery ID and are consumed after the
  row appears or proves an immutable recipient mismatch. Read proofs are keyed
  by attachment basename plus reader kind and alias; they remain for one ledger
  TTL because an older lock-unaware sender may publish another row later.
- A read proof updates every matching recipient row whose `sent_at <= read_at`.
  It never updates a row that began after the observation, so reusing an
  attachment basename cannot make a future envelope appear read. If one page
  contains multiple opens around one or more reuses, every distinct horizon is
  applied chronologically: each row keeps its earliest qualifying proof while
  the retained sidecar keeps the latest horizon for late writers.
- A durable pending read vetoes reuse even if its delivery-row update failed.
  Read-grace collection requires every ledger attempt naming the basename to
  be read and begins at the latest qualifying read. The file's mtime must be
  strictly older than that proof: a reuse refreshes mtime before transport, so
  even a sender that dies before writing its row leaves the file on full-TTL
  treatment. Equality is conservative for coarse filesystems.
- Every current Stop, reply, send-tool, and peer-handoff road captures
  `sent_at` immediately before entering transport and passes that same value to
  either `record_sent` or `record_unknown`.
- A shipped v1 sender records time after transport. If such a mixed-version row
  appears to begin after the read, it is indistinguishable from a genuine later
  reuse: the proof remains retained and diagnosed, and the row stays unread.
  Restarting long-lived clients after upgrade closes this conservative limit.
- Pending proof files are exact-schema, UTF-8, no-follow, size bounded, count
  bounded, and TTL bounded. Invalid exact files are never overwritten; they
  hold cursor progress and `doctor` names the repair directory. Capacity and
  TTL retirement are accounted for in bounded diagnostics.
- A missing sidecar directory is healthy and empty; a present symlink,
  non-directory, or unlistable pending-receipt or Stop-outcome store is an
  explicit broken state in both `status` and `doctor`. Diagnosis never reads a
  diagnostics child through a store already proved unsound.
- Replaying a proof that is equal to or weaker than retained evidence performs
  no sidecar rewrite, so replay cannot extend its lifetime indefinitely. If a
  page carries several read horizons and only part of their row updates can be
  persisted, receipt recording reports the page unresolved and the cursor
  remains behind it; replay reapplies every horizon idempotently.
- A successful or outcome-unknown Stop send also writes an exact
  `(side, cursor key, recipient slot, turn fingerprint) -> attempt ID` outcome
  before cursor promotion. A failed cursor write retains that evidence and
  suppresses the exact automatic retry. Once the cursor is durable, the
  temporary sidecar is cleared; failure to create it alone is not a failed send.
- Durable task, delivery, receipt, and Stop-outcome strings must be strict UTF-8
  so malformed JSON escape sequences remain inert data instead of crashing a
  later status, sweep, hashing, or serialization path.

### 4. Production-scoped host census

- The census reports all files discovered beneath each supplied root, but
  splits statistics into `production` and `excluded` scopes.
- The Claude input is the host's projects root. Its `production` scope is the
  union of immediate names ending in `.jsonl` inside each immediate project
  directory. This is a suffix rule, not a shell-glob dotfile rule: immediate
  dot-prefixed and zero-length JSONL files stay candidates, matching the
  durable catalog's monotone authority. Recent fallback uses the same set but
  gives filesystem-safe, visible, non-empty regular transcripts priority. Deeper files such as
  `project/subagents/*.jsonl` are `excluded`, matching production's per-project
  discovery.
- Codex `production` contains recursive `rollout-*.jsonl` files only. Other
  JSONL files are `excluded`, matching production discovery.
- Each scope independently reports file count, malformed lines, user messages,
  prompt sources, tags, and shapes. No path or message content is emitted.
- Each side also reports `root_error` (`null`, `missing`, `unreadable`,
  `not-directory`, or `io-error`) and `unsupported_platform`. An existing empty
  root is a truthful zero; a missing, unreadable, or unsupported root makes the
  CLI exit non-zero after printing the aggregate JSON.
- Inventory and reads walk beneath the resolved host root without following
  symlink leaves or directories, matching production's descriptor-based
  admission. Unsafe entries never contribute content or tags. The side-level
  `refused_paths` is the total unsafe inventory count; each candidate-shaped
  unsafe entry also contributes to the `refused_files` count of the scope it
  would have occupied, alongside later open races. The two counts therefore
  overlap and must not be summed.
- A filesystem name that cannot be represented as strict UTF-8 is an unsafe
  path refusal and is never opened for transcript content, matching the
  production reader's boundary.
- One filesystem path contributes at most one inventory refusal. If a
  candidate-shaped directory is first counted as a refused leaf and then races
  while being opened for recursion, its candidate and may-hide-children facts
  merge; it is never counted once per observation.
- Counts from the former flat recursive schema are not directly comparable to
  either new scope; release notes must name `production` or `excluded`.
- A nested Claude `<fork-boilerplate>` fixture must appear only under
  `excluded`. Symlink-leaf and symlink-directory fixtures must contribute no
  tag and must increment `refused_paths`. Removing either boundary must make a
  test fail.

## Explicit non-goals

- No Kanban board, IDE, terminal multiplexer, automatic merge/push, or new
  worktree manager.
- No shared message-content database, broadcast, guessed route, or replacement
  identity.
- No peer-reported acceptance/progress/completion protocol in this milestone.
- No autonomous worker supervisor or completion notification yet. That changes
  process ownership, crash recovery, PID-birth proofs, and task schema and needs
  its own design.
- No workflow DSL.
- No dependency refresh or CI policy change in this feature commit. The current
  moderate transitive advisory and a deterministic packed-artifact CI matrix
  are recorded as a separate release-engineering follow-up rather than mixed
  into user-facing bridge behavior.

## Verification and release boundary

Every production behavior starts with a failing test and an observed reason for
failure. Targeted tests cover each slice and its realistic mutations. The final
feature commit must pass both supported Python runtimes available on the
maintainer machine, the Node channel suite, `git diff --check`, package-content
inspection, and an installed-package smoke test. The feature remains on its
isolated branch: no merge, push, npm publication, or release tag is implied.
