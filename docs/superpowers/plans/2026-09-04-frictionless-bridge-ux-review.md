# Frictionless Bridge UX Foundation Review Record

**Date:** 2026-09-04

**Branch:** `codex/ux-foundation`

**Base:** `8ae369230365c856ee48a9628321c64cf71b54d2`

**Release boundary:** Review candidate only. It has not been merged, pushed,
tagged, or published. `package.json` deliberately still says `0.5.0`; the
already-published version must be bumped in an explicitly authorised release
change before these bytes can be published.

## Product decision

The comparison set included Claude Code cross-session messaging and agent
teams, Conductor, Claude Squad, Crystal, Vibe Kanban, and MCP Agent Mail. The
shared lesson adopted here is explicit session identity, visible delivery
state, a guided first-run path, isolated autonomous work, and review before
integration. Antiphon remains a thin cross-vendor bridge. It does not become an
IDE, task board, worktree manager, workflow DSL, second identity system, or
shared message-content database.

## Review process

- Claude Opus reviewed the initial design over the live Antiphon channel
  (`3bd031bf-4460-4b80-9a3a-b5a8c8da4740`). Its objections tightened the
  launch boundary, honest handoff states, and census scope before implementation.
- Claude Opus separately reviewed the receipt-before-ledger race
  (`029ae553-d7b2-48eb-9eac-f37dd102e1d1`). The accepted design is a bounded,
  time-qualified proof sidecar under the existing delivery lock rather than a
  second message store.
- Independent agents audited competitive UX, reliability, CI/package gates,
  census portability, Unicode boundaries, task-reader hardening, early receipt
  schedules, and installed CLI packaging. Their concrete findings were either
  reproduced by a failing test and fixed or retained as an explicit release
  boundary below.
- A final read-only code reviewer is auditing the integrated working tree. Its
  resolved findings so far are recorded below; exact-commit review remains a
  completion gate.

## Resolved findings

### Delivery and persistence truth

- Every current send road captures one attempt timestamp immediately before
  transport and uses it for the eventual sent or unknown row.
- A receipt that wins before the sender row is retained and reconciled. Multiple
  attachment-read horizons are applied chronologically; a later filename reuse
  cannot steal an earlier read.
- Exact Stop outcomes survive cursor-write failure and suppress only the exact
  retry scope. Misbound valid JSON is not accepted as evidence.
- Definite refusal, post-write unknown, receipt-won, and tracking-incomplete
  results stay distinct. A peer-visible handoff ID is never discarded after
  bytes may have left.
- Task, delivery, receipt, and Stop-outcome durable strings reject invalid
  UTF-8; ordinary transcript text containing a lone surrogate renders as an
  ASCII escape instead of crashing a page.
- Pending-receipt and Stop-outcome health distinguishes normal absence from a
  symlinked, non-directory, or unlistable store. `status` exposes the condition;
  `doctor` fails with an actionable repair directory.

### Process and protocol boundaries

- `antiphon launch` is shell-free, resolves the executable once, replaces the
  dispatcher process, preserves host argv order, and refuses host flags or
  environment modes that disable the project just checked.
- A stale inherited `ANTIPHON_CWD` pointing at another real directory is
  refused before host lookup or preflight.
- The installed dispatcher is Python and execs Node only for the MCP channel;
  ordinary CLI commands no longer pay for an intermediary Node process.
- Node validates exact Python reply/delegate result shapes. It rejects
  contradictory fields, false receipt persistence, malformed IDs, and any
  attachment value other than a canonical UUID basename.
- Claude channel success requires literal `ok: true` and the exact attempt ID.
  Missing, stale, or mismatched acknowledgements are post-write unknown, never
  success or a retry invitation.
- A same-kind handoff accepted by an old Claude listener retains its successful
  delivery semantics but carries the existing reconnect warning because that
  listener displayed the sender under the wrong origin label.

### Discovery, diagnosis, and portability

- Passive records use `[HH:MM]` today and `[YYYY-MM-DD HH:MM]` across a local
  date boundary; the longer label participates in the existing byte budget.
- The host-wrapper census measures production-admitted and excluded corpora
  separately, rejects invalid UTF-8 path names, merges repeated path refusals,
  and behaves consistently across Python 3.9 and 3.14 JSON recursion limits.
- Cross-root server diagnosis compares bounded complete code signatures rather
  than trusting matching versions or mtimes.
- Hostile task/ledger JSON, lock acquisition/unlock failures, and deep decoded
  structures are controlled refusals instead of tracebacks or invented success.

## Verification evidence

Evidence below is from the uncommitted integrated tree and will be repeated for
the exact commit before this record is final.

- Python 3.9.6: `1404` tests, `3` skipped, all passing.
- Python 3.14.7: `1404` tests, `1` skipped, all passing.
- Node channel/MCP integration suite: all scenarios passing.
- CI verifier unit suite: `18` tests passing under both Python runtimes.
- Installed tarball smoke: version/help, parse refusals, both MCP initialises,
  exact installed-byte comparison, and `11` first-party files passing from an
  empty workspace outside the checkout.
- `npm pack --dry-run --json`: exactly `11` intended package files; no project
  `.antiphon` state, tests, research notes, or CI files.
- Static Python compilation, Node syntax checks, shell syntax checks, and
  `git diff --check`: passing.

## Release-engineering follow-up

The CI matrix and exact installed-artifact verifier are intentionally a
separate commit from product behavior. The matrix covers the Python floor and
current release, Node floor/current/LTS, Linux, and macOS ARM. The package job
archives the workflow SHA, hashes the artifact, and the install-only job has no
checkout and verifies every installed byte before exercising the CLI and MCP
servers.

No publication is authorised by this review. Before a release, choose and bump
a new version, rerun exact-SHA review and all gates on those final bytes, then
verify the registry directly after any publish attempt.
