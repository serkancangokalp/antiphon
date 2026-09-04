# Frictionless Bridge UX Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Antiphon's passive history temporally unambiguous, make correctly configured host sessions one command to launch, and make handed-task and census output honest at their persistence and discovery boundaries.

**Architecture:** Keep the bridge thin: one conditional record-clock formatter, one shell-free CLI exec wrapper that reuses doctor configuration checks, an ordered prepare/send/finalize handoff transaction, bounded receipt/outcome durability around the existing ledger, and a test-only census split that mirrors production file admission. No message store, supervisor, dashboard, or new task protocol is introduced.

**Tech Stack:** Python 3.9+ standard-library dispatcher/bridge, Node.js 20+ channel, `unittest`, Markdown.

**Spec:** `docs/superpowers/specs/2026-09-04-frictionless-bridge-ux-design.md`

## Global Constraints

- Preserve `PAGE_BUDGET = 8000`, whole-record atomicity, cursor/catalog/horizon semantics, and local-time rendering.
- Never invoke a host through a shell; preserve passthrough argv order exactly.
- Never hold a task or ledger lock while calling a transport.
- A successful transport with incomplete persistence must never be described as a trackable task.
- Census output contains aggregate counts only, never transcript paths or content.
- Do not merge, push, publish, tag, or modify the shared main checkout.

---

### Task 1: Date-qualified passive records

**Files:**
- Modify: `lib/antiphon.py` (`_render_record`, generated bridge rules)
- Modify: `test/test_antiphon.py` (`PageHorizonTest` and record rendering tests)
- Modify: `README.md` (passive-page contract)

**Interfaces:**
- Consumes: event epoch seconds and a page render's local calendar date.
- Produces: `_record_clock(epoch, today) -> str`; `[HH:MM]` today,
  `[YYYY-MM-DD HH:MM]` on another representable day, and `time unavailable`
  for a parser-accepted epoch the host clock cannot represent.

- [x] **Step 1: Write failing behavior tests.** Literal same-day, previous-day,
  future-day, unrepresentable-time, and byte-frontier assertions are present.
- [x] **Step 2: Observe the old `[HH:MM]` output fail the absolute-date tests.**
- [x] **Step 3: Implement the formatter and thread one captured page-local date
  through every render in the budget loop.** Manual numeric formatting avoids
  platform-specific year padding and clock conversion failure is bounded.
- [x] **Step 4: Run targeted page regressions.** The mutation control proves
  removing the eleven date bytes increases the selected record count.
- [x] **Step 5: Update README and both generated rules without changing cursor
  or horizon semantics.** Static-surface ceilings remain enforced.

### Task 2: Guided host launch

**Files:**
- Modify: `lib/antiphon.py` (usage, `launch`, command table, setup guidance)
- Create: `bin/antiphon` (installed Python dispatcher)
- Delete: `bin/antiphon.mjs` (old Node dispatcher)
- Modify: `package.json`, `package-lock.json` (installed executable mapping)
- Modify: `test/test_antiphon.py` (new `LaunchTest`)
- Modify: `test/test_contracts.py` (documented-command contract if required)
- Modify: `README.md` (quickstart and multi-peer launch)

**Interfaces:**
- Consumes: `launch(*args)`, `project_dir()`, `_which(host)`, `_config_state(cwd)`, `_doctor_config(report, cwd, states)`, `peers.valid_name`.
- Produces: `antiphon launch claude|codex [--name NAME] [-- HOST_ARGS...]`; on
  success `os.execve(resolved_executable, argv, env)`.

- [x] **Step 1: Write failing launch tests.** They cover parsing, exact argv and
  environment, both duplicate flag spellings, readiness-defeating host flags,
  attached short forms, host modes supplied through the environment, the
  host's own literal boundary, an inherited cross-project `ANTIPHON_CWD`,
  missing setup/host, exec failure, and a
  filesystem snapshot proving preflight is read-only.
- [x] **Step 2: Observe the missing command fail, then separately observe the
  `--flag=value` duplicate mutation reach the host before its guard was added.**
- [x] **Step 3: Implement parsing and preflight without a shell.** Reuse doctor,
  resolve once with `_which`, copy the environment, and execute that exact path
  with `os.execve`. The installed Python dispatcher owns the final `execve`, so
  the launched host replaces it as the top-level process; Node remains only on
  the channel road.
- [x] **Step 4: Run `LaunchTest`, setup, and contract tests green.**
- [x] **Step 5: Make `antiphon launch` the default setup/README path and retain
  the manual equivalent.** Interactive passthrough policy is documented.

### Task 3: Honest handed-task persistence

**Files:**
- Modify: `lib/antiphon.py` (`_delegate` handed branch)
- Modify: `test/test_antiphon.py` (`ManagedWorkerToolTest` handed cases)

**Interfaces:**
- Consumes: `workers.new_task`, `workers.update_task`, `workers._discard_record`, `ledger.record_sent`, existing queue/channel transports.
- Produces: ordinary `state: "handed"` only after both durable records; otherwise
  a retained `handing`/`tracking_incomplete` record, `tracking: "incomplete"`,
  and one shared attempt returned as both `task_id` and `delivery_id`.

- [x] **Step 1: Write failing mutation-oriented tests.** They independently
  remove preparation, ledger persistence, task finalisation, recovery-state
  persistence, and duplicate-launch guards.
- [x] **Step 2: Observe transport-before-prepare, ignored-false, capacity,
  start-sweep, disappearing-ID, and returned-state failures.**
- [x] **Step 3: Prepare as `handing`, send with no store lock held, validate the
  ledger before `handed`, and retain the peer-visible ID on every post-send
  persistence failure.** Pre-send refusal still cleans the private preparation.
- [x] **Step 4: Run managed-worker and delivery/tool regressions.** `handing`
  consumes no worker slot, survives start patience, and both incomplete states
  refuse local result/cancel.
- [x] **Step 5: Close independent-review mutations.** Validate and exact-read
  the preparation before transport; match every supplied task/ledger field;
  preserve IDs and attachments on unknown post-write outcomes; make all direct
  send surfaces return an explicit no-retry unknown result and disclose a
  failed dedupe write; prevent Stop-marker retry while making a lost unknown
  ledger entry exit nonzero; record a receipt-resolvable ledger `unknown`; model failed definite-
  refusal cleanup as `delivery_refused`; and bump task/ledger schemas to v2
  while retaining every shipped v1 state. Exact readback includes the
  pre-transport `sent_at`, and task-store syscall failures after transport
  retain and return the peer-visible attempt id.

### Task 4: Production-scoped wrapper census

**Files:**
- Modify: `test/host_wrapper_census.py`
- Modify: `test/test_host_wrapper_census.py`

**Interfaces:**
- Consumes: Claude's projects root and Codex's sessions root.
- Produces: each side as `{all_files, refused_paths, unsupported_platform,
  root_error, production: stats, excluded: stats}`; each stats object has
  `files`, `refused_files`,
  `malformed_lines`, `user_messages`, `prompt_sources`, `tags`, and `shapes`.
  `refused_paths` is the side-wide unsafe inventory total; scoped
  `refused_files` includes the candidate-shaped subset plus open races, so the
  values overlap rather than forming additive buckets.
  Missing/unreadable roots and unsupported descriptor primitives are printed
  explicitly and make the census CLI exit non-zero; existing empty roots do
  not.

- [x] **Step 1: Add nested Claude and non-rollout Codex fixtures and assert they
  appear only under `excluded`.**
- [x] **Step 2: Observe the old flat recursive schema fail the scoped assertions.**
- [x] **Step 3: Refactor counting into `_stats(root, paths, ...)`; inventory and
  open with component-by-component no-follow descriptors, then use each Claude
  project's immediate transcripts plus Codex's recursive rollout files;
  filesystem names outside strict UTF-8 are refused before content read.**
- [x] **Step 3a: Bind Claude's candidate boundary across all three readers.**
  Direct fallback, durable enumeration, and the census use the same immediate
  `.jsonl` suffix rule, including dot-prefixed and zero-length candidates.
  Filesystem-safe, visible, non-empty regular files retain fallback priority, and a single
  fixture compares the three admitted sets.
- [x] **Step 4: Run the real aggregate census and record scoped counts.** The
  2026-09-04 release-corpus refresh found Claude 98 production / 448 excluded
  files, with all five `fork-boilerplate` tags excluded, and Codex 281
  then 282 production / 0 excluded files as another rollout appeared. Both
  sides reported zero refused paths/files. Fixture tests prove no path or secret is
  emitted; historical flat counts are not compared numerically to either new
  scope.

### Task 4a: Receipt-first, retry, and durable-input hardening

**Files:**
- Modify: `lib/antiphon.py`, `lib/ledger.py`, `lib/workers.py`
- Modify: `test/test_antiphon.py`, `test/test_ledger.py`, `test/test_workers.py`

**Interfaces:**
- Consumes: transcript receipts, the delivery flock, all transport call sites,
  Stop cursor promotion, and durable JSON task/ledger records.
- Produces: bounded pending-receipt and exact Stop-outcome sidecars; a
  pre-transport attempt timestamp on every current send road; retained,
  time-qualified read proof reconciliation; strict UTF-8 durable metadata.

- [x] **Step 1: Reproduce receipt-before-row loss and cursor-before-receipt
  advancement.** Add success/unknown, empty-page, sidecar failure, bounds,
  symlink, malformed-record, old-writer, and concurrent schedule tests.
- [x] **Step 2: Reconcile received proofs by immutable delivery ID and retain
  read proofs for every prior matching attempt.** Pin the future-basename-reuse
  counterexample, multiple chronological horizons, partial-write replay, no-op
  sidecar replay, and the conservative mixed-v1 timestamp limit.
- [x] **Step 3: Capture attempt time before transport on Stop, reply, direct
  tool, and handed-task families.** The transport spy test proves every family
  records an earlier or equal `sent_at`.
- [x] **Step 3a: Make attachment reuse and collection obey retained reads.** A
  pending read vetoes reuse; read grace requires all attempts read, starts at
  the latest proof, and is disabled by a file mtime at or after that proof so a
  transport crash before row persistence waits out the full TTL.
- [x] **Step 4: Add exact Stop-outcome suppression around cursor promotion.** A
  cursor failure retains suppression; a successful cursor makes a temporary
  sidecar failure irrelevant; valid JSON bound to another hashed scope is not
  accepted as exact suppression evidence.
- [x] **Step 5: Reject non-UTF-8 durable strings and hostile worker-task JSON.**
  Cover legacy worker metadata through the status path as well as v2 handoffs.
- [x] **Step 6: Make sidecar-store health explicit.** Normal absence remains
  empty; symlinked, non-directory, or unlistable pending-receipt and exact
  Stop-outcome stores make status visible and doctor fail with the repair path.

### Task 5: Integrated review and exact artifact verification

**Files:**
- Modify if findings require it: only files already named above
- Create: `docs/superpowers/plans/2026-09-04-frictionless-bridge-ux-review.md`

**Interfaces:**
- Consumes: the complete feature diff and independent Claude/subagent findings.
- Produces: a clean feature commit with recorded review dispositions and exact test evidence.

- [x] **Step 1: Run focused Python tests under `/usr/bin/python3` and the current `python3`, then the entire Python suite under both.** Record exact counts and skips.
- [x] **Step 2: Run `node test/channel.test.mjs`, `git diff --check`, and `npm pack --dry-run --json`.** Confirm the package contains every runtime module and no `.antiphon` state or research-only files.
- [x] **Step 3: Install the generated tarball into a fresh temporary npm prefix and run `antiphon --version`, `antiphon --help`, launch parse refusal smoke tests, and MCP initialization from outside the repository.** Verify resolution uses installed bytes.
- [x] **Step 4: Ask Claude Opus for an independent correctness/security/UX review of the exact diff.** Resolve every concrete finding or document the technical reason it is not accepted, then request a re-review if code changes.
- [x] **Step 5: Run final verification again, inspect `git diff --stat`, `git diff --check`, and `git status --short`, then commit on `codex/ux-foundation`.** Do not merge, push, publish, or tag.

## Self-review

- Spec coverage: Tasks 1-4 map one-to-one to all in-scope behavior; Task 5 covers review and release boundaries.
- Placeholder scan: no deferred implementation placeholder is used; explicit non-goals remain in the spec, not as incomplete steps.
- Type consistency: `launch(*args)`, `_record_clock(epoch, today)`, handoff task
  states, and the scoped census schema are named once and used consistently.
