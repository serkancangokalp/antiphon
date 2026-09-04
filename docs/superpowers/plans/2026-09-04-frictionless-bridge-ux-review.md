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
- Claude Opus reviewed the successive exact candidates through six gates. Its
  final request was delivered under `7520cd54-8de6-4d79-8c75-31b740cc9a11`
  with the corrected full SHA under
  `365677b4-6288-43c6-8ceb-3447a0c95aa4`; it returned an explicit PASS for
  `0865227e79adf6053ca6d66fffd86753eb27d829` after reproducing both final
  mutation controls in an isolated archive copy.
- A separate read-only agent also returned PASS for that exact SHA and tree
  after four discriminating Node mutations, the complete test suite, syntax,
  contract, repository-connectivity, and clean-tree checks. Neither reviewer
  edited the feature worktree.

## Exact candidate

The reviewed runtime-and-test candidate is
`0865227e79adf6053ca6d66fffd86753eb27d829`, tree
`a14d69c7f284a935782c4ef64aa1122a3e0b62d8`, based on
`8ae369230365c856ee48a9628321c64cf71b54d2`. Its five commits are:

1. `e501a1cea43350cf21d176e4163ec79665590969` — UX and delivery foundation.
2. `39cad45660ee2d83aa60fd57a58c39a5a3c7caaa` — CI matrix and installed artifact proof.
3. `59076649f03d66f99507379c2e21d865a775f934` — delegation and census boundary fixes.
4. `acf4793f0c8e301265828434476de13f7d6ced1d` — mutation-gap fixes.
5. `0865227e79adf6053ca6d66fffd86753eb27d829` — discriminating Node boundary tests.

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
- Delegation defaults apply only when `kind` or `task` is absent. Explicit
  null/empty values are rejected before Python is spawned; a PATH stub proves
  that Python's later validation cannot mask a Node regression.
- `handing`, `missing`, and `tracking_incomplete` are all exercised through the
  real Node MCP boundary. Removing any one accepted state makes the suite fail,
  preventing a successful peer handoff from becoming a false refusal/retry.
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
- Census prompt sources use a closed, bounded vocabulary while retaining the
  observed `queued` and `suggestion_accepted` categories. Tag names have
  separate length and cardinality ceilings: currently observed names such as
  `subagent_notification` remain visible, while excess distinct names are
  explicitly accumulated under `<additional-tags>`.
- Cross-root server diagnosis compares bounded complete code signatures rather
  than trusting matching versions or mtimes.
- Hostile task/ledger JSON, lock acquisition/unlock failures, and deep decoded
  structures are controlled refusals instead of tracebacks or invented success.

## Verification evidence

Evidence below is from a clean detached clone of exact candidate
`0865227e79adf6053ca6d66fffd86753eb27d829`, except where the parent is named
explicitly.

- Python 3.9.6: `1408` tests, `3` skipped, all passing.
- Python 3.14.7: `1408` tests, `1` skipped, all passing.
- Node 26.7.0 channel/MCP integration suite: all scenarios passing, including
  both new process-boundary mutation gates.
- CI verifier unit suite: `18` tests passing under both Python runtimes.
- Installed tarball smoke: version/help, parse refusals, both MCP initialises,
  exact installed-byte comparison, and `11` first-party files passing from an
  empty workspace outside the checkout.
- The exact-SHA tarball SHA-256 is
  `9831a45469937f276968dc9ec6eebf531a4748513c216dd3c70d789decb70780`.
  `npm pack --dry-run --json` reports exactly `11` intended package files; no
  project `.antiphon` state, tests, research notes, or CI files.
- The real fresh-user flow passed `112/112` on exact parent
  `acf4793f0c8e301265828434476de13f7d6ced1d`. The child changes only
  `test/channel.test.mjs`; an exact diff proves all production and package bytes
  are identical, so model-backed E2E was not repeated merely to spend calls.
- The final live wrapper census measured Claude `98` production and `450`
  excluded files, and Codex `299` production files. Both sides reported zero
  refused paths, zero refused files, and zero malformed lines.
- Static Python compilation, Node syntax checks, shell syntax checks, and
  full-range `git diff --check`: passing. The exact checkout stayed clean.
- Claude's earlier unexplained, output-discarded Node exit `1` did not recur in
  `26` subsequent output-preserving runs, including eight isolated serial runs
  and three Python-then-Node chains. There is no reproduced product failure;
  retaining unexpected-rejection output in the harness remains a useful
  diagnostics follow-up.

## Release-engineering follow-up

The CI matrix and exact installed-artifact verifier are intentionally a
separate commit from product behavior. The matrix covers the Python floor and
current release, Node floor/current/LTS, Linux, and macOS ARM. The package job
archives the workflow SHA, hashes the artifact, and the install-only job has no
checkout and verifies every installed byte before exercising the CLI and MCP
servers.

No publication is authorised by this review. Before a release, choose and bump
a new version (`npm view antiphon version` still reports the already-published
`0.5.0`), rerun exact-SHA review and all gates on those final bytes, then verify
the registry directly after any publish attempt.
