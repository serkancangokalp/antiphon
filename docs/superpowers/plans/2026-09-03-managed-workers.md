# Managed workers (MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `antiphon_delegate` / `antiphon task delegate` starts a one-task worker of the other kind (`claude -p` or `codex exec`) in its own directory, records it under a task id, and `antiphon_task` reports its state, result and evidence; nothing is merged, guessed or forwarded automatically.

**Architecture:** One new module, `lib/workers.py`, owns the task records (`.antiphon/tasks/<id>.json`, validated like the ledger), the worker directory (`.antiphon/workers/<id>/`), the two subprocess adapters, the hop budget and the sweep. `lib/antiphon.py` gains the `task` CLI command, the two MCP tools on the Codex server and a hook-time sweep call; `lib/channel.mjs` gains the two tools on the Claude server, each a thin call to `antiphon.py task …` with JSON on stdin/stdout, like `reply`. A worker's output is labelled `[Antiphon worker <kind>:<id>]` wherever it is shown.

**Tech Stack:** Python 3.9+, Node, `git worktree` for write tasks, unittest + `node --test`; the CLIs stubbed in the suite by PATH, one real run in `fresh-user.sh`.

**Spec:** `docs/superpowers/specs/2026-09-03-managed-workers-design.md`.

## Global Constraints

- A task is `read` (default) or `write`. A write task needs a git checkout and runs in a fresh worktree under `.antiphon/workers/<id>/`; a read task runs in the project directory with a read-only host sandbox.
- Never `--dangerously-skip-permissions`, never `--full-auto`; `claude -p` runs with its default permission mode and `codex exec` with `-s read-only` (read) or `-s workspace-write` (write, bound to the worktree by cwd).
- Hop budget: `ANTIPHON_HOP_BUDGET` (default 1); a worker is started with `ANTIPHON_HOP=<parent+1>`; at the budget, `delegate` is refused.
- At most 4 running workers per project; timeout default 900 s, max 3600 s.
- A task record carries the task's SHA-256 and size, never the text; the label the worker's words carry is `[Antiphon worker <kind>:<id>]`.
- Every new test is born with a mutation gate; suites in the foreground; never `git push`, never `npm publish`.

---

### Task 1: The task record and its store (`lib/workers.py`)

- [ ] `TASK_VERSION = 1`, `TASK_TTL = 7*24*3600`, `MAX_WORKERS = 4`, `DEFAULT_TIMEOUT = 900`, `MAX_TIMEOUT = 3600`, `STATES = ("accepted", "running", "completed", "failed", "cancelled", "timed_out", "blocked")`, `KINDS`, `CLASSES = ("read", "write")`.
- [ ] `tasks_dir(cwd)`, `_sound_dir` (no symlink, 0700), `new_task(cwd, *, kind, task_class, sha256, size, parent, timeout, hop) -> record`, `read_task(cwd, id)`, `tasks(cwd)`, `update_task(cwd, id, mutate)` under a flock, `prune(cwd, now)`; validation refuses unknown keys, non-hex digests, times past `MAX_TIME`.
- [ ] Tests (`test/test_workers.py`): round trip; malformed skipped; prune by TTL; symlinked dir refused. Mutations: validator dropped; prune keeps everything.

### Task 2: Adapters — one subprocess per kind

- [ ] `adapter(kind, task_class)` → argv and env: claude → `["claude", "-p", <prompt>]` (+ `--permission-mode default` is the host default; nothing added); codex → `["codex", "exec", "-s", "read-only" | "workspace-write", "--color", "never", <prompt>]`. The prompt is the task text prefixed with one line naming the task id and the label the worker must not remove.
- [ ] `start(cwd, record, text)`: creates the worker dir; for `write`, `git worktree add .antiphon/workers/<id> HEAD` (refused with a reason when `cwd` is not a git checkout); spawns the adapter detached (`start_new_session=True`), stdout/stderr to `.antiphon/workers/<id>/log`, env with `ANTIPHON_HOP`, `ANTIPHON_CWD=<worker dir>`, `ANTIPHON_NAME=worker-<id[:8]>`; records `pid`, `started_at`, state `running`.
- [ ] Tests: argv per kind and class (never the two dangerous flags); a write task without git is refused; the worker env carries the hop and the name; the stubbed CLI (a PATH script) is started and its pid recorded. Mutations: dangerous flag added; hop not passed.

### Task 3: Lifecycle — status, result, cancel, timeout

- [ ] `status(cwd, id)`: reads the record and reconciles: pid gone with exit code 0 → `completed`; non-zero → `failed`; past `timeout` → SIGTERM, 10 s, SIGKILL → `timed_out`; a log line matching the host's permission-prompt shape → `blocked`.
- [ ] `result(cwd, id, wait=0)`: polls `status` up to `wait` (≤ 300 s); on `completed` for a write task computes `git -C <worktree> diff` (≤ 256 KiB inline, else a path) and reads the worker's `tests` report file if it wrote one; returns `{state, log_path, diff?, tests?, worker}`.
- [ ] `cancel(cwd, id)`: SIGTERM/SIGKILL, state `cancelled`, worktree removed.
- [ ] Sweep: `sweep(cwd, now)` removes worker dirs of `completed`/`cancelled` tasks whose result was collected, keeps `failed` until the record expires, never touches a running worker; called from `hook()` beside the attachment sweep.
- [ ] Tests with a stub CLI that sleeps/exits as told: each state transition; timeout kills; cancel removes the worktree; sweep rules. Mutations: timeout ignored; failed dir removed early.

### Task 4: The CLI and the two MCP tools

- [ ] `antiphon task delegate|status|result|cancel` (JSON on stdin for delegate: `{text, kind, to?, task, timeout}`; JSON on stdout) in `COMMANDS`; `to=` hands the task to a running named peer through `send_to_*` with the task id in the label and records the task as `handed`.
- [ ] Codex server: `antiphon_delegate(text, kind?, to?, task?, timeout?)`, `antiphon_task(id, action, wait?)`; Claude server: the same two, calling `antiphon.py task …`; both refuse with the spec's sentences (no kind that can be known; a worker whose server cannot see its hop; the hop budget; four running).
- [ ] Tests: `_mcp_serve` dispatch; Node tools listed and forwarded; refusals verbatim. Mutations: hop check dropped; both-modes check dropped.

### Task 5: Labels, receipts and the passive page

- [ ] A worker's own transcript is discovered as any session's; `SessionJoin` labels a worker source `[Antiphon worker <kind>:<id>]` (the name `worker-<id[:8]>` registered by its hook is the join key).
- [ ] The ledger records the handed task under the task id (`to_kind`, `to_alias`, `sender_kind`), so receipts and status apply.
- [ ] Tests: a page built from a worker's rollout carries the label; a handed task appears on the ledger. Mutation: label dropped.

### Task 6: Words and verification

- [ ] README (§Push "Managed workers" subsection, §Commands `antiphon task`, §Limits: never merges, hop budget, subprocess adapters only); rules and instructions: one sentence each, ceilings checked; BACKLOG entry `(shipped 2026-09-03, MVP)` with the five decisions as taken; contract tests.
- [ ] `fresh-user.sh`: one real `delegate` of a read task to `codex exec` with the stub-free CLI, `result(wait=120)` returns `completed` and a log.
- [ ] Both suites, statics, `npm pack`, review, merge.
