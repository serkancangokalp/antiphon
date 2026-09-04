# Cross-vendor managed workers — design

Campaign phase 5 (`2026-09-02-final-campaign-design.md` §5). The BACKLOG entry
*P2 — Cross-vendor managed workers* states the shape and lists five open
decisions. This document takes them and specifies the lifecycle. Whether it is
built in this campaign is decided at the end of phase 4; if it is not, the
BACKLOG entry says "designed, not built" and points here.

## The five decisions

1. **`delegate` exposes both modes, explicitly, and never guesses.**
   `kind` names the worker's kind — the other side by default on the two
   servers, required on the command line. With `to`, the task is handed to
   that already-running named peer of `kind` over the addressed send that
   exists today (parked when too large), marked `[Antiphon task <id>]` and
   recorded on the ledger under the task id; without `to`, a fresh managed
   worker of `kind` starts: a subprocess of that kind's CLI in a worktree of
   its own. No kind that can be known → refused ("name a kind (claude or
   codex)"). (An earlier draft refused `to` and `kind` together; the shipped
   rule needs `kind` to pick the transport, so both is the handed case.)
2. **Managed workers are one-task, ephemeral sessions.** A worker runs one task
   and exits. There is no resume: a follow-up is a new task that names the
   previous task id as `parent`, and the new worker starts from the previous
   worker's worktree if it is still there. A current task record lives at
   `.antiphon/tasks-v2/<task-id>.json` for seven days (the ledger's own TTL, one
   sweep); a worker's worktree under `.antiphon/workers/<task-id>/` is removed
   when its result is collected or the task is cancelled, kept for inspection
   after `failed` until the record expires, and never removed while the worker
   runs. At most four workers run at once per project; a fifth `delegate` is
   refused with the four task ids.
3. **CLI subprocess adapters only, first.** `claude -p` and `codex exec` are the
   two adapters, the same two `fresh-user.sh` proves on every release. Neither
   host's native cross-vendor worker API is depended on (Claude Code subagents
   and Codex `multi_agent`/`spawnAgent` are same-vendor stories, one of them
   experimental); when one becomes a stable cross-vendor contract, it is a
   second adapter behind version detection, with the subprocess adapter as the
   documented fallback.
4. **Read-only tasks run without another confirmation; a write task never
   merges itself.** A task is `read` (review, explain, search) or `write`
   (edit, generate). A read task runs the worker with its host's default
   permission class and no write access outside its worktree. A write task
   runs in its own worktree with the host's default sandbox (`claude -p` with
   the default permission mode; `codex exec --sandbox workspace-write` bound to
   the worktree) and returns a patch and its test output as evidence; the
   bridge never applies a patch. The parent agent, or the human, applies it
   with `git apply --check` first. A worker never receives a broader permission
   class than the delegating session (`--dangerously-skip-permissions` and
   `--full-auto` are never passed), never approves the parent's requests, and
   never widens its sandbox.
5. **No synchronous `delegate`; a bounded wait on `result`.** `delegate` returns
   at once with the task id. `result(id, wait=<seconds>)` blocks up to
   `wait` (at most 300 s, default 0) and returns whatever state the task is in
   when it returns. The parent's turn stays free; the bound is explicit and
   the caller's.

## Lifecycle

States: `accepted → running → completed | failed | cancelled | timed_out |
blocked`. `blocked` is a worker that asked for a permission its class denies;
it is reported, never granted from here.

Commands and tools:

| Surface | Claude side | Codex side |
|---|---|---|
| CLI | `antiphon task delegate\|status\|result\|cancel` | same |
| MCP | `antiphon_delegate`, `antiphon_task` on the channel server | `antiphon_delegate`, `antiphon_task` on the `mcp` server |

`antiphon_delegate(text, kind?, to?, task=read|write)` → `{task_id, worker: {kind, session, worktree?}, state: accepted}`.
`antiphon_task(id, action=status|result|cancel, wait?)` → the record; `result` on a completed write task carries `diff` (bounded to 256 KiB, then a path), `tests` (the worker's own report) and `log_path`.

Every event and artifact names the worker: the passive page and the ledger
label it `[Antiphon worker codex:<task-id>]` / `[Antiphon worker
claude:<task-id>]`, never as the parent's own words or work. The worker's own
transcript is discovered by the ordinary readers (it runs in this project's
directory or its worktree, which is catalogued as a worker source and labelled).

*Outcome (2026-09-03, at the release gate):* the paragraph above was not built,
and the MVP does not claim it. A worker's worktree holds no hooks and no MCP
server when the bridge's files are generated and not committed (the usual
case), so there a managed worker registers nothing, is not a peer, and appears
on no page; it is followed by its task id through `status`, `result`, `list`
and its log. Where its working directory carries the bridge's configuration —
a task run in place, or a checkout that commits the generated files — the
project's hooks and servers are its own and it is a live named peer
`worker-<id8>` for its duration (round 2 of the gate). Seeding a worktree with
the bridge's configuration on purpose is the follow-up, named in BACKLOG.

Hop budget: `ANTIPHON_HOP_BUDGET` (default 1). A worker is started with
`ANTIPHON_HOP=<parent hop + 1>`; a session whose hop is at the budget has its
`antiphon_delegate` refused ("hop budget 1 reached; set ANTIPHON_HOP_BUDGET to
allow a bounded deeper chain"). The bridge never forwards a task automatically,
so the budget is the only recursion there can be, and it is stated.

Timeouts: a task has a `timeout` (default 900 s, at most 3600 s). A Python
supervisor owns a task-store live lock and binds its exit code into the marker
before unlocking; its adapter is
admitted only after the task record is durable and an `admit` → `ready` →
`commit` handshake completes. Past the timeout, an exactly identified worker
is sent SIGTERM, then SIGKILL after 10 s, and is `timed_out` only when a signal
was actually sent and death was proved. Unprovable liveness or ownership never
authorizes a signal or a terminal guess: the v1 `running` record, directory and
worker slot remain indefinitely for rolling-reader safety, until trustworthy
evidence arrives or an operator intervenes outside Antiphon. A finished shell
wrapper awaiting reaping as a zombie is stopped, not live; a non-zombie member
of its process group still keeps the task running. After exact identity is
verified, one final protected-marker read prevents acting on an already
published result, and the stop intent is made durable before any signal. The
supervisor authenticates both the exit code and whether it observed the stop:
a SIGTERM mask is the publication linearization point, held from the stop-bit
snapshot through the marker fsync, so a stop cannot interleave those facts;
a natural publication wins when no signal reached it, while a stopped
publication resolves to the first durable timeout or cancel intent. Positive
group death without authenticated publication becomes `outcome_unknown`, which
can later be refined only by an authenticated natural publication or by the
caller that proves its already-durable stop completed. Process-table absence
proves death only from a successful, completely parsed snapshot; any malformed
nonblank row makes the observation unknown. One canonical C-locale process row
supplies both birth and state, preventing pid reuse between two observations
from creating signal authority. Process-group reconciliation and adapter-
descendant draining retry unreadable complete snapshots only inside a fixed
250 ms window; persistent uncertainty withholds publication or terminal state.

Storage: `.antiphon/tasks-v2/` and `.antiphon/workers/` are swept on the hook like
the attachments and the ledger; a task record never carries the task text
(its SHA-256 and size). The worker's log and an old-reader exit mirror are
worker-visible, while the current reader's lifecycle marker stays beside the
task record. An unlocked orphan marker left by an older reader is pruned only
after the same seven-day retention window. `outcome_unknown` is not marked
collected because later authenticated evidence may refine it. Expiry and
terminal cleanup revalidate the exact observed row under the task lock;
completed work still requires collection, while a durable `cancelled` row
authorizes an idempotent retry after a crash. Record
retirement is durably acknowledged before external work is removed, and a
later prune completes any orphan cleanup left by a crash. Explicit cancel
reports success only when the worker directory and its own Git worktree entry
are proved absent; partial cleanup leaves the terminal row and returns a
retryable refusal. Task-id creation and the full Git/filesystem cleanup
transaction share the task-store lock. Creation refuses every current or
legacy row, lifecycle marker, retained diff and worker directory with that id;
record-first cleanup requires the row and marker to remain physically absent.
After successful terminal cleanup, `cancel` observes the same lifecycle
generation again before releasing that lock, so a concurrent authenticated
refinement of `outcome_unknown` is returned rather than a stale snapshot and a
later same-id task can never be adopted.
Before `git worktree add` is invoked, a durable marker in the worker directory
preserves the exact Git cleanup key, and the containing task-directory entry is
fsynced before Git can publish its own admin entry; a missing post-add base is
a refusal, not a base-less running task. A visible marker receives a fresh
directory acknowledgement before row retirement. Git alone removes a
registration by the exact physical worktree path. Antiphon never deletes a Git
admin pathname: it verifies every `gitdir` through a bounded, no-follow,
regular-file read and treats any malformed or unreadable entry as uncertainty.
When the last registration removes the admin directory, its common Git parent
is fsynced before absence consumes the marker. Thus
even a crash before the accepted row gains its base, after record removal, or
during a Git-admin fsync after the work directory vanished leaves a
discoverable retry witness for the next orphan sweep.
The start lease itself crosses only into a customization-disabled Python
guardian. Its gated Git child receives no lease and cannot exec until the
child's exact pid/birth marker is durable. The guardian drains Git's stdout and
stderr into bounded memory until that direct child returns, then closes the
pipes without waiting for detached descendants. The guardian's durable marker,
written only after it directly observes the Git child return (success or
failure), is the sole completion receipt. Timeout, signal, guardian loss and
later process death cannot replace it because a descendant may have detached
from the recorded process group. A missing receipt therefore retains the
accepted row, cleanup witness, work and worker slot without automatic retry;
the reporting surfaces require operator intervention outside Antiphon. A
deliberately detached same-UID Git or hook mutator that continues after the
direct command returns is outside this cooperative boundary. The same disabled
environment/user-site startup applies to the worker supervisor, so Python
bootstrap code cannot fork with lifecycle, gate, commit or publication-proof
descriptors.

The first current-protocol admission fences the prior `.antiphon/tasks/`
epoch under its old lock. It refuses while an accepted/running old worker, an
unresolved old process group or an uncollected old write result remains. A
fresh lock-free legacy `handing` row defers the fence for a 60-second
cooperative settlement window; an older one remains crash-stale incomplete
evidence and no longer blocks the epoch. The window is an availability policy,
not proof that an arbitrarily wedged old process is gone. Once quiescent, the
fence changes that directory to owner-read/execute (`0500`), proves
writes are denied, rechecks the rows, fsyncs the directory and commits a token
to the lock file. Old rows remain read-only and are listed as legacy; the
current reader never imports them. This protects cooperative non-root POSIX
clients, including an old call that opened the lock before the fence and the
old hand-off path that did not lock. Root or arbitrary same-UID code that
deliberately restores write permission is outside this file protocol's threat
boundary.

## What this does not claim

No worker appears in either host's native subagent UI. The portable contract is
Antiphon's task id, lifecycle, labels and evidence trail.

## Implementation gate

Built in this campaign only if, at the end of phase 4, both suites, the E2E run
and the independent reviews are green on `main` and a bounded MVP — `delegate`
by `kind` with the subprocess adapters, `status`, `result`, `cancel`, the hop
budget, the labels and the sweep — can be delivered with the same test
discipline (every behaviour under a named test with a mutation gate, the two
CLIs stubbed in the suite, and one real run in `fresh-user.sh`). Otherwise the
BACKLOG says "designed, not built" and why.
