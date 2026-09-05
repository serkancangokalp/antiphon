# Antiphon

**Two agents in one project, an open-identity bridge.** Claude Code and Codex CLI work side by side — one terminal each, or several on each side — and each sees the other's context and can wake the other when it needs to, without ever faking who the message is from.

Antiphon does not own your workflow. It carries messages between the sides
while preserving whether they came from the human user, from Claude, or from
Codex; its optional managed workers stay isolated and never merge their work.

With one terminal per side there is nothing to configure beyond `antiphon setup`: Antiphon assigns an automatic alias when it can positively prove the host session, otherwise the peer stays honestly unnamed and the legacy single-peer road remains. With several sessions, address the automatic aliases shown by `status` or set explicit names — see [Many peers](#many-peers).

## How it works

No shared log is kept. Both CLIs already write their own transcripts; Antiphon reads and derives from them, recording which messages each peer has already seen. An unnamed peer keeps its cursor at `.antiphon/cursor.json`; a named one owns `.antiphon/peers/<side>-<name>/cursor.json`, so two sessions on the same side never advance each other's place. Every direct send leaves one small file on a delivery ledger at `.antiphon/deliveries/<id>.json` — who sent, to whom, over what, the words' digest — and, for a refused or transport-outcome-unknown line, its first 60 characters so the sender can recognise it; never the words otherwise — kept for a week, two for an entry with a notice its sender has not heard. If the transport acknowledgement disappears after bytes may have left, the ledger says `unknown`, the caller is warned not to retry automatically, and any parked attachment is retained. A later receipt resolves that attempt to `sent` and removes its bounded preview; if that receipt already won the race while the sender was awaiting the acknowledgement, the direct result says `received` instead of contradicting the transcript with `unknown`.

Receipts come from the peer's own transcript, read by the same reader that builds the pull page. A receiver can record a receipt before the sender returns from transport, so Antiphon first preserves that proof in a bounded sidecar under `.antiphon/deliveries/pending-receipts/`; the page cursor advances only after the proof or its ledger update is durable. Exact delivery-ID receipts are consumed when their immutable row appears. Attachment-read proofs remain for one ledger TTL so a late, older sender row can still reconcile, and apply only to matching attempts whose pre-transport `sent_at` is no later than the observed read — a later reuse of the same filename can never steal an earlier read. When one delayed page contains several opens around basename reuse, Antiphon applies every distinct read horizon in order and retains the latest, so each attempt receives its first qualifying proof. A shipped old sender timestamps after transport, so a mixed-version row whose time is later than its read remains conservatively unresolved; `status` and `doctor` show the retained proof instead of guessing. Invalid sidecars, a symlinked store, or an unreadable sidecar directory hold the page cursor and are diagnosed with the repair location; bounded expiry or capacity loss is counted. The exact Stop-outcome store uses the same fail-closed health reporting because unreadable suppression evidence cannot justify an automatic retry.

The pull page never looks behind its 24-hour horizon, so a receipt older than that is never seen there and `status` says so; an automatic Codex alias's receipt is read off its own rollout after the session has exited; each session's own hook also reads the tail of its own transcript for the receipts it owes, horizon or not. An automatic Claude identity also records which session its owner runs now, one small file per owner under `.antiphon/identity/claude/`, named from a digest rather than from anything about the session. `doctor` reports that a proof could not be read or could not be trusted without naming the file; this is the directory it means, and removing the unreadable one costs nothing — the next turn writes it again.

### Pull — shared context, no wake

| Direction | Mechanism |
|---|---|
| Codex → Claude | Claude `UserPromptSubmit` hook |
| Claude → Codex | Codex `UserPromptSubmit` hook |

The other side's recent messages enter your turn's context when you type
something. Nobody is woken up.

This path is project-wide awareness, not delivery. It is not addressed to
anyone. A host's own bookkeeping — an injected AGENTS.md, a goal continuation,
a compact summary, an interruption marker, an external agent's relayed tool
traffic — is never rendered as either agent's speech. When a page interleaves multiple sessions, blocks that can be joined to
a current named peer carry that peer's label; older, unnamed or unjoinable
blocks remain honestly unlabelled. Do not read pull context as a private line
between two particular peers.

### Push — addressed, live wake

| Direction | At the end of a turn | Mid-turn |
|---|---|---|
| Claude → Codex | `Stop` hook + `codex queue` | `reply_to_codex` tool |
| Codex → Claude | `Stop` hook + MCP Channel | `antiphon_send` tool |
| Claude → Claude | `Stop` hook + MCP Channel, always named | `reply_to_claude` tool |
| Codex → Codex | `Stop` hook + `codex queue`, always named | `antiphon_send(kind="codex")` tool |

A line starting with `@codex` or `@claude` in a reply reaches the other
agent immediately, even if nobody is typing.

A Stop marker can carry a block. Make its one-line message
`@claude[:name] <<TOKEN` or `@codex[:name] <<TOKEN`, where `TOKEN` matches
`[A-Z][A-Z0-9_]{0,31}`; put the body on following lines and use an exact
`TOKEN` line to close it. Blocks do not nest and the closer is not
Markdown-fence-aware, so choose a token absent from the body. Marker-looking
lines inside the body are content. A malformed or unclosed block sends nothing
from that turn. To send literal text beginning with `<<`, put it inside a block
body. Use the direct tools — `antiphon_send` or `reply_to_codex` — for long
content: an oversized direct-tool message can be parked as an attachment,
while an oversized Stop-marker block is refused and not parked.

Every push is addressed to exactly one peer and is never broadcast. When the
recipient cannot be shown to be the only candidate, the send is refused rather
than guessed.

Neither side has to wait for its turn to end. Either agent can hand work
over mid-turn and keep going, so the other starts on it in parallel; the
answer is collected later from the same turn with `antiphon_read` (Codex)
or the channel event (Claude). Nothing blocks, and a message delivered by
a tool is recorded, so ending the turn with the same `@claude` / `@codex`
line does not send it twice.

A tool result says what happened and no more. `reply_to_codex` answers
*queued* — `codex queue` accepted a row, and Codex reads it at its next turn
— and `antiphon_send` answers *delivered* to Claude's channel; neither is
the peer reading the words. The receipt is the peer's own transcript showing
the message — a delivery to a named peer is credited only from that peer's
transcript, a bare one from any transcript of its kind — and `antiphon status`
reports what is awaiting a receipt, what was received and what was refused. A
`@codex` or `@claude` line the Stop hook could not deliver is reported on the
sender's next page, with the reason (at most eight notices per page, the rest
on the following turns; a reason is kept to 400 characters), because the
hook's own refusal reaches a debug log and not the agent.

There is a fourth, deliberately non-binary result: `outcome: "unknown"`. It
means the bytes may have left but the transport acknowledgement was lost.
Antiphon returns the immutable attempt id, preserves any parked content,
and says not to retry manually because the peer may already have received it.
A direct mid-turn tool separately reports `dedupe_recorded: false` when its
Stop-marker fingerprint could not be parked. A Stop send records an exact,
content-bound outcome before promoting the live cursor; if that cursor write
fails, the retained outcome suppresses the same turn automatically and both
`status` and `doctor` expose it. If neither the cursor nor that exact outcome
can be persisted, the hook exits nonzero and names the duplicate risk. A
temporary outcome-sidecar failure is harmless after a successful cursor write
and is not reported as a failed send. `status` keeps an unknown ledger state
visible until the peer's transcript proves receipt; an unknown Stop outcome
whose ledger entry itself cannot be written also makes the hook nonzero so the
loss is not hidden on exit-0 stderr.

### Managed workers — one task, one worker of the other kind

`antiphon_delegate(text, kind?, to?, task?, timeout?)`, on both servers, starts
a fresh `claude -p` or `codex exec` session for one task and returns at once
with a task id; `antiphon_task(id, action, wait?)` reports its `status`,
collects its `result` after a bounded wait (the log tail, and for a completed
write task the diff against the base it started from and its test summary), or
`cancel`s it. Whenever the project is a git checkout with a commit the worker —
a read task as much as a write task — works in a detached worktree of its own
at HEAD under `.antiphon/workers/<id>/work/`, so nothing it does touches your
tree and nothing uncommitted in your tree is visible to it: a worker asked to
review "the current changes" reviews HEAD. The log and a compatibility copy
of the exit code sit beside that worktree; the current reader trusts neither
for lifecycle authority. A supervisor-only `.antiphon/tasks-v2/<id>.live`
marker binds the final exit code beside the task record, where a too-large
diff also sits as `.antiphon/tasks-v2/<id>.diff`; the one file the
worker writes for the bridge, its test summary, sits inside it at
`.antiphon/tests.txt`, where a write task's sandbox can reach it and where git
ignores it. A read task (the default) runs under the host's read-only class
(`--permission-mode plan` for Claude, `-s read-only` for Codex); in a project
that is not a checkout it runs in the project under that class, and so in a
checkout without a commit, where a write task is refused. A write task runs
under the host's default class in its worktree and returns a diff the
bridge never applies — the parent agent or the human does, after `git apply
--check`. No worker is ever started with `--dangerously-skip-permissions`, `--full-auto`
or a sandbox value that widens its class; it carries `ANTIPHON_HOP` one deeper
than its parent and is refused a delegation of its own at the hop budget
(`ANTIPHON_HOP_BUDGET`, default 1) — on both roads: a worker whose server
cannot see its hop is refused on its name alone, and the command line reads
that name from the worker's own environment. The budget bounds a cooperating
chain, not a worker that runs the other CLI by hand from a shell. On the Codex
side the two tools are the only ones that ask first: `setup` writes a per-tool
`approval_mode = "prompt"` for them beneath the server's blanket approval of
the read tools. With `to`, a read task is handed to a running named peer of
that kind over the ordinary addressed send instead (parked as an attachment
when too large, like any direct send), marked `[Antiphon task <id>]` and
recorded on the ledger; a handed task has no worker here, so `result` and
`cancel` are refused for it and only the peer can be told to stop, and a write
task cannot be handed. The recipient must be a valid public peer name before
any record is written. The task marker and delivery ledger share one attempt
ID, returned under both names. The task is prepared as `handing` and read back
exactly before transport, without consuming a worker slot. Only exact read-back
matches for every supplied task and ledger field make it `handed`. A definite
transport refusal removes the preparation; if removal itself fails, the record
becomes `delivery_refused` instead of looking in-flight. If bytes may have left
but the acknowledgement is lost, or if either post-send final write is
incomplete, the local ID remains as `tracking_incomplete` (or `handing` if even
that recovery write fails), the result says tracking is incomplete, and it
warns not to retry automatically because the peer may already act. An unknown
transport attempt is also kept on the delivery ledger so a later transcript
receipt can resolve it. Those states refuse local `result` and `cancel`. If an
older Claude channel accepts a same-kind handoff but cannot preserve its Claude
origin, the result keeps the successful handoff state and tells you to reconnect
that recipient before relying on its displayed sender label;
receipt still comes only from the peer's transcript. At most four workers run per project, admitted and
recorded under one lock. A Python supervisor holds a protected per-worker
marker from before admission until it binds and publishes the exit code;
`delegate` returns only
after an `admit` → `ready` → `commit` handshake, so a refused start cannot run
its adapter later. The trusted wrapper gets a five-second scheduling window to
publish `ready`; a refusal reports the measured wait and its bounded
pre-adapter log, while the separate commit gate still prevents late work. A
worker whose exit is proved gives its slot back the
moment another is asked for; one whose exact process identity is proved is
stopped at its timeout by the next `status`, `result`, hook sweep or new
delegation. A final protected-marker read after identity verification prevents
a stop from acting on an already-published outcome. The stop intent is durable
before any signal, and the supervisor's authenticated publication says whether
it exited naturally or observed that stop. The supervisor blocks SIGTERM
before sampling that bit and keeps it blocked through the marker fsync: a stop
observed before that publication fence is signed, while one arriving after the
fence loses to the publication already in progress. A natural publication still wins
when no signal reached the supervisor; a stopped publication resolves to the
first durable timeout or cancel intent. If the whole process group is
positively dead but no authenticated publication survived, the task is
`outcome_unknown`; a later authenticated publication, or the caller that
proved its own stop completed, can refine that conservative result. Process-
table absence proves death only from a successful, completely parsed snapshot;
birth and state for one pid come from the same canonical C-locale row, so a
recycled pid cannot splice two owners into signal authority. Any malformed
nonblank row makes the observation unknown. The supervisor gives a transient
unreadable group snapshot one bounded retry window; status/admission use the
same rule when releasing a finished slot, and persistent uncertainty still
withholds publication or terminal state. If
liveness or ownership cannot be proved after the deadline, the current record
deliberately remains `running`: Antiphon invents no terminal
outcome and sends no unverified signal, retaining its work directory and one
of the four slots indefinitely. A later trustworthy published exit or restored
observation reconciles it automatically; otherwise operator intervention
outside Antiphon is required, with no unsafe force-reclaim command. An
ordinary terminal task record lives a week under
`.antiphon/tasks-v2/`, and a worker's directory is swept once its evidence was
collected; a durable `cancelled` row needs no result evidence and authorizes a
later sweep to retry directory cleanup after a crash. Reading
`outcome_unknown` never collects a possible future result; refinement exposes
its evidence first. Explicit cancel also refuses while the outcome remains
unknown, and refuses a refinement first observed during cleanup, so the caller
can inspect the newly proved result before making a fresh cleanup decision.
Expiry revalidates the exact row under the task lock. Git-backed work writes
its durable cleanup witness before
`git worktree add`, covering a crash before `base` reaches the accepted row,
and fsyncs the containing worker-directory entry before Git is touched; it
revalidates the added worktree's base before admitting the adapter. A visible
witness is freshly fsynced again before it can authorize retirement. Git alone
removes the exact worktree registration; Antiphon only verifies the bounded,
regular, one-line `gitdir` namespace and retains the witness when any entry is
malformed or unreadable. It also fsyncs Git's common directory when removal of
the last worktree removes the admin directory itself. External work is deleted
only afterwards, so a crash can
leave only an orphan directory that a later prune retries — its worktree
removed by exact path, never by deleting an admin pathname or pruning your
repository's other worktrees. An explicit `cancel` reports success only after
both the worker directory and
its own Git worktree entry are proved absent; a partial cleanup keeps the
terminal row and refuses with a safe retry instead. Task-id creation and the
whole Git/filesystem cleanup transaction share the task-store lock; a new row
cannot reuse an id while its current or legacy row, lifecycle marker, retained
diff or worker directory remains. Record-first cleanup requires those state
markers to stay absent, while terminal cleanup retains its row. `cancel` reads
that same generation before deleting anything and again before releasing the
cleanup lock. A concurrent authenticated refinement of `outcome_unknown`
therefore refuses without erasing its evidence, and a later same-id task cannot
become the answer.
The worktree-add lease is held by a small Python guardian whose environment,
user-site and `sitecustomize` startup paths are disabled. Git cannot cross its
one-byte gate until the exact Git pid/birth recovery marker is durable, and
neither Git nor its hook descendants inherit the lease. The guardian drains
stdout and stderr into bounded memory while waiting for the direct Git child,
then closes those pipes without waiting for detached hook copies, so neither
pipe EOF nor post-return output can retain resources. Its durable marker,
written only after it directly observes that return (success or failure), is
the sole completion receipt. A timeout, signal, guardian loss or later process-death
observation never substitutes for it: the accepted row, cleanup witness, work
and worker slot remain, `status`, `task list` and `doctor` name the uncertainty,
and automatic retry is withheld until operator intervention outside Antiphon.
A deliberately detached same-UID Git or hook process that keeps mutating after
the direct Git command returns is outside this cooperative boundary.

#### Recovering a missing Git completion receipt

This state is deliberately not auto-recoverable: Antiphon cannot tell whether
Git or one of its hooks still has unpublished work. Use the task id printed by
`antiphon task list` and `antiphon doctor`, then:

1. Confirm the state with `antiphon task status <id>`. Do not retry, cancel or
   delete the task while its mutation boundary is uncertain, and stop starting
   new Antiphon workers in this checkout during the investigation.
2. Before changing anything, copy `.antiphon/tasks-v2/<id>.json`,
   `.antiphon/tasks-v2/<id>.live`, any `<id>.diff`, the complete
   `.antiphon/workers/<id>/` directory and the output of
   `git -C <project> worktree list --porcelain` to a location outside the
   project. These files are the recovery evidence.
3. Establish quiescence with operating-system process and open-file tools.
   The stored pid, its birth time or the death of one process-group leader is
   not sufficient: stop if any Git, hook or descendant process might still
   mutate the project or worker worktree.
4. If the exact worker worktree exists, inspect and preserve its status and
   diff first. Remove its registration only through Git, using its exact
   absolute path (`git -C <project> worktree remove --force
   <absolute-worker-worktree>`), then verify that path is absent from
   `git worktree list --porcelain`. Never delete an entry under
   `.git/worktrees` by hand.
5. Removing the retained task state is an informed operator decision, not an
   Antiphon recovery claim. Only after quiescence and evidence preservation,
   remove paths for that exact id, with its JSON row last; never use a glob or
   remove the whole `.antiphon` or `.git` tree. Finish by checking
   `antiphon task list` and `antiphon doctor`. If quiescence cannot be proved,
   leave the evidence in place; Antiphon intentionally has no force-reclaim
   command.

On the first current-protocol worker admission,
Antiphon inspects the previous `.antiphon/tasks/` store under its old lock. It
refuses while an old worker, unresolved old process group or uncollected old
write result remains. A newly prepared lock-free legacy hand-off receives a
60-second cooperative settlement window before it is treated as crash-stale
incomplete evidence; this is a reclamation policy, not proof about an
arbitrarily wedged old process. Once quiescent, Antiphon makes that directory
read-only (`0500`) and records the completed fence in its lock file before
admitting current work.
Old history remains visible as `legacy:<state>` in `antiphon task list`, but
current commands never mutate it and a pinned old client can no longer publish
there. Finish and collect old work with the client that created it before the
transition, then retry. This local fence relies on non-root POSIX owner-mode
enforcement; root or arbitrary same-user code that deliberately restores write
permission is outside the file protocol's threat boundary. Every worker is
asked to keep `[Antiphon worker
<kind>:<id>]` at the start of its final message, so nothing it says reads as
the parent's own. A worker is followed by its task id — `status`, `result`,
the log. Whether it is also a peer depends on where it runs. In a worktree
that carries no bridge configuration — the usual case, since the files
`setup` writes are generated and not committed — it registers nothing,
appears on no page and makes no bare send refused. Where its working
directory does carry that configuration — a task run in place because the
project is not a checkout or has no commit, or a checkout that commits
`.codex/config.toml`, `.codex/hooks.json` and `.claude/settings.json` — the
project's hooks and
servers are its own for the duration: it is a live named peer
`worker-<id8>` (it is started as `ANTIPHON_NAME=worker-<id8>` with
`ANTIPHON_CWD` pointing at the project, so everything it records lands in
the project's own store under that name), its words are on the page under
that name, and a bare send of its kind is refused while it runs. Seeding a
worktree with the bridge's configuration on purpose is the follow-up, not
built. One side effect is
Codex's own, measured with Codex CLI 0.152.1: `codex exec -s workspace-write`
records the repository root as trusted in `~/.codex/config.toml` (a read
worker does not, and a transient `-c` trust override does not prevent it), so
a Codex write worker leaves that entry behind, which the bridge neither asks
for nor undoes. The same lifecycle is on the command line as `antiphon task`,
and `antiphon task list` names every task on record.

### How identity is preserved

A Claude → Codex message reaches Codex tagged either `[Antiphon bridge] Claude:` (pushed from Claude's Stop hook) or `[Antiphon channel] Claude:` (a direct reply sent through the channel, via the `reply_to_codex` tool) — either way, Codex sees these as Claude's words, not the human user's. A Codex → Codex message reaches the other Codex session tagged `[Antiphon bridge] Codex:` or `[Antiphon channel] Codex:`, and a Claude → Claude message arrives as a channel event with `sender="claude"` — the other session's words, never the human user's.

The tag is followed by `[from=<alias> id=<uuid>]`, naming which Claude peer spoke. A session whose configured identity does not own its return channel instead carries `[from=<alias> reply_to=<unavailable> id=<uuid>]`; that exception is explained below. A session that cannot establish either an explicit or automatic identity shows `from=<unnamed>`: it has no name to be addressed by, and the angle brackets keep that apart from a peer actually called `unnamed`. The id names one delivery attempt — it is not a correlation id, and nothing routes replies by it.

A valid Claude `ANTIPHON_NAME` is the session's configured identity, not proof that its named return channel is reachable. If startup says the channel was not acquired, the name still identifies that session's outgoing words, but the label alone does not establish that a reply will reach the same process. The startup warning exposes a duplicate-name loss; `antiphon doctor` exposes a named listener whose endpoint registration is missing. Restart the Claude session after correcting either fault.

Without `ANTIPHON_NAME`, Antiphon may derive an automatic `auto-` peer alias from a canonical host session UUID. Codex publishes one only after its first hook records that UUID and a writer lock positively proves the session live. Every census remains `at least N` because sessions before their first hook may be invisible. Claude accepts one only from a fixed Claude probe that finds exactly one interactive record with this session's CLI-root pid and exact project cwd; the host display name is ignored, and the Claude hook must join the same endpoint, owner and identity. Probe or hook failure stays `<unnamed>`. `ANTIPHON_NAME` overrides automatic identity. One positively live automatic peer can be addressed by alias and is the only automatic case a bare send may choose; two or more positively live candidates make a bare send refused. Older or mixed-version peers are never guessed into automatic identity. The full host session id, identity digest, owner key and socket route stay private; status, doctor, labels, refusals and errors expose only the public alias and the remedy beside it. After a session rotates to a new host session, its old automatic alias stops resolving at once and its new one is unreachable until a fresh endpoint exists — in practice an MCP reconnect. `status` and `doctor` name the current alias beside that remedy; an identity whose owner cannot be proved live is counted, never addressed.

If a label carries `reply_to=<unavailable>`, do not reply to its `from` alias: that channel belongs to a different session. The sender remains identified, but there is deliberately no routable return alias until its Claude channel is restarted under a unique name.

An `Antiphon delivery notice:` event is a bridge-authored diagnostic: it carries no original message content and does not turn the sender's refusal into delivery. Before that refusal, Antiphon makes one content-free recovery request to the explicitly requested alias's deterministic socket. A current listener can restore its own endpoint; the original words are sent exactly once, and only if the registry resolves again. An old or unverified listener stays refused and may need a restart. Doctor only reports this state; it never performs the recovery. When recovery fails but the socket still answers, the notice names the alias and attempt time while the original send remains refused.

A Codex → Claude message never pastes text into the terminal and never impersonates user input. The local MCP server sends Claude Code a `notifications/claude/channel` event. Its metadata looks like:

```xml
<channel source="antiphon" sender="codex" sender_kind="agent" sender_alias="build" message_id="...">
```

Claude Code's interface shows this as an incoming channel event, and Claude treats the message as the words of the Codex agent, not of the human user. It sends its reply back with the `reply_to_codex` MCP tool, passing `sender_alias` as `to` whenever it is a name rather than the literal `<unnamed>`. A bare reply works where no Codex peer is registered, or where one positively live automatic peer is the only candidate. It is refused when an explicit named peer or multiple positive candidates are live, because an unnamed Codex session before its first hook cannot be ruled out. A `sender_alias` of `<unnamed>` is a peer with no name — it cannot be addressed by name, and a bare reply reaches it only in the no-registered-peer case; passing `<unnamed>` as `to` is the same as leaving it out.

Automatic aliases identify individual peers; they do not pair a Claude peer with a Codex peer. There is no automatic Claude↔Codex partnership and no reply correlation: a message is routed only by the alias written on it, except for the explicitly bounded bare-send cases above.

## Many peers

A name is an environment variable read at startup. The guided launcher sets it
alongside each host's Antiphon requirements:

    antiphon launch claude --name ui
    antiphon launch claude --name api
    antiphon launch codex --name build
    antiphon launch codex --name review

Put host arguments after an exact `--`, for example
`antiphon launch claude --name ui -- --model opus`. The arguments retain their
order and stay argv entries; no shell interprets them. Options that disable the
configured bridge or move the host to another project are refused here: Claude
`--bare`, `--restricted`, `--safe-mode`, `--strict-mcp-config`,
`--no-session-persistence`, `--setting-sources`, and `--worktree`/`-w`; Codex
`--cd`/`-C`. Truthy `CLAUDE_CODE_SAFE_MODE`, `CLAUDE_CODE_SIMPLE`, and
`CLAUDE_CODE_RESTRICTED` environment modes are refused too. Remove the option
or unset the mode to get the ready Antiphon session, or start the host manually
when that incompatible behavior is intentional. For advanced/manual launches,
the equivalent raw commands are
`ANTIPHON_NAME=ui claude --dangerously-load-development-channels server:antiphon`
and `ANTIPHON_NAME=build codex`.

Launch also requires an inherited `ANTIPHON_CWD` to name the same real
directory the process is currently in. A stale value pointing at another
project is refused before host lookup or setup preflight, so one configured
checkout cannot accidentally authorize a host started in another.

Names must be unique per side within a project. Two Claude sessions configured
with the same name both identify their own outgoing words by that name, but
only the one that owns the named channel can receive a reply there.

Once an alias is visible, a peer is addressed explicitly — by marker at the start of a line,
or by the `to` argument of the tool that sends without ending the turn:

| From | Marker | Tool |
|---|---|---|
| Codex → Claude | `@claude:ui` | `antiphon_send(to="ui", text=…)` |
| Claude → Codex | `@codex:build` | `reply_to_codex(to="build", text=…)` |
| Claude → Claude | `@claude:api` | `reply_to_claude(to="api", text=…)` |
| Codex → Codex | `@codex:review` | `antiphon_send(kind="codex", to="review", text=…)` |

There is no way to reach several peers at once. A send is delivered to the one
peer named on it, and to nobody else.

**Same-vendor.** A Claude session reaches another Claude session, and a Codex
session another Codex session, by the same machinery — always by name. A bare
same-kind line (`@claude` from Claude, `@codex` from Codex) has no meaning and
is refused, and so is a session's own alias; both refusals are reported on the
sender's next page. The passive pull page gains no same-kind lane: it stays
the other kind's transcripts, so its size and its discovery window are
unchanged. That is not confidentiality: a `@claude:name` line is part of the
sender's own visible reply, which the other kind's page shows like any reply,
and a same-kind tool call's arguments stay retrievable by their public id — a
same-kind message is addressed, not confidential. A same-kind receipt comes
from the receiving session's own hook
reading the tail of its own transcript, which is why a Claude-only or
Codex-only project gets receipts too. The bridge forwards nothing
automatically, so no message ever comes back to its author through it.

### When a bare message is refused

The two sides fail closed on different rules, because they leave different
traces:

- **To Claude.** A bare `@claude` works while exactly one Claude peer is live.
  From the second one on, it is refused and you must name one.
- **To Codex.** A bare `@codex` works when no peer is registered (the legacy
  single-peer road), or when exactly one positively live automatic peer is the
  only candidate. It is refused when any explicit named Codex peer is live or
  when two or more positive candidates exist. A session before its first hook
  may still be invisible, so an explicit peer is never guessed to be alone.

Automatic aliases make multi-peer addressing work without configuration after
the host proofs arrive. Distinct `ANTIPHON_NAME` values remain the explicit
override and avoid the first-hook/probe window; a peer whose proof fails stays
unnamed and cannot be addressed while other candidates are live.

### Seeing who is live

    antiphon status

Beyond transcripts and cursors, `status` lists every registered peer with the
side it runs on, the name it took, and its state — `ready` once it has an
address to receive on, or `waiting for first turn` before that. Under the list
it prints the addressing rule that currently applies:

```
Codex session census: at least 2 live observed; additional sessions before their first hook may be invisible

Peers:
  Claude ui — ready
  Claude api — ready
  Codex build — ready
  Codex auto-cwymp7bdr2do3ymgr5kxwv74tq — ready
  → a bare @claude line is refused; address one: @claude:ui, @claude:api
  → a bare @codex line is refused; address a named peer: @codex:auto-cwymp7bdr2do3ymgr5kxwv74tq, @codex:build
```

A peer that is `waiting for first turn` is still a candidate: readiness never
decides who a message goes to, so it cannot silently hand routing to whichever
session happened to start first. A positively live Codex observation is
projected read-only as its stable automatic alias; the host UUID and full digest
remain internal. Every census says `at least N` because additional sessions
before their first hook may be invisible. One automatic peer is both explicitly
addressable and reachable by a bare send while it is the only positive
candidate. Two or more positive candidates make a bare send refused. With no
peer or live observation, the `Peers:` block is empty while the census still
says `at least 0`; zero observations never proves that zero sessions exist.
Stored observations whose writer lock no longer gives positive liveness are
retained and shown only as a count: a missing or unlocked lock is insufficient
evidence that the host session is dead.

## Install

Requires Node 20+ and Python 3.9+. The Claude Code channel is a research
preview and needs Claude Code 2.1.80 or newer; recent Claude Code releases
set their own, higher, Node floor, so check theirs as well.

Install the command, either straight from the repository:

    npm i -g github:serkancangokalp/antiphon

or from npm:

    npm i -g antiphon

Either way the command is `antiphon` — the package name only decides
where it comes from. Then, in the project the two agents share:

    cd /your/project
    antiphon setup
    antiphon launch claude        # terminal one
    antiphon launch codex         # terminal two

`launch` checks the complete project setup and resolves the selected host
without changing project files. It then executes that host directly, adding
Claude's required development channel flag. Use `--name NAME` when several sessions of one kind will run;
put any host arguments after `--`. If setup is incomplete, or the host is not
on `PATH`, launch refuses before starting anything and prints one remedy.
Arguments after `--` are your direct interactive host request and are not
filtered by the stricter managed-worker permission policy; autonomous workers
remain read-only by default and keep their existing forbidden-flag checks. The
only launcher guard is readiness: it refuses host flags that disable the
configured hooks/MCP/transcript or move the session to another project, because
otherwise a successful launch would not be the session it just preflighted.
The guard applies only before the host's own exact `--`; later tokens are
literal host content. It also refuses Claude's truthy safe, simple, and
restricted environment modes, and an inherited `ANTIPHON_CWD` that resolves to
a different directory than the launcher's current project. The raw host commands above remain the explicit
escape hatch for an intentionally different configuration.

`setup` writes `.claude/settings.json`, `.codex/hooks.json`,
`.codex/config.toml`, `.mcp.json`, `.claude/settings.local.json`,
`AGENTS.md`, `CLAUDE.md` and `.antiphon/.gitignore` for that project. The hook commands resolve
`antiphon` through your `PATH` rather than hardcoding an install path, so
they keep working after a reinstall or a move; `.mcp.json` and
`.codex/config.toml` still record this project's own absolute directory,
so each side can find the right channel socket. None of the eight files
live in this repository — they are generated per project. Approve the
Codex hooks once when Codex first shows them.

## Update

    npm i -g github:serkancangokalp/antiphon      # from the repository
    npm i -g antiphon@latest                      # from npm

    cd /your/project && antiphon setup

Re-running `setup` migrates hooks and instruction blocks written by older
versions in place; it never creates duplicates. An Antiphon section written
before 0.5.0 has no end marker: when it is, word for word, one of the wordings
that shipped — npm 0.1.0 to 0.3.3, or the unpublished 0.4.0 at its version
commit — `setup` rewrites it up to the end of that wording and nothing after,
so notes and headings of your own below it stay; a section with any other
wording, a heading of your own inside it, or a second Antiphon heading is
left alone, and `setup` and `doctor` print the same remedy. Until `setup` is re-run, a `.codex/config.toml` from before 0.5.0
still pre-approves every tool, the worker tools included; `doctor` names it.

A long-lived bridge server keeps the code it loaded, so an upgrade on disk
runs beside sessions still using the old reader for a while. That is safe in
both directions: a 0.3.x reader keeps a current endpoint record on its pid
alone (the current fingerprint lives in a field it never selects), and a
listener whose Node and Python halves disagree about that field is refused
with a remedy — reconnect the Claude session, or reinstall so both sides
match — rather than told it recovered. A record written by 0.4.0 before this
change stays prunable by a 0.3.x reader until its owner rewrites it; `doctor`
names such a record and the remedy by kind (reconnect Claude, restart Codex).
Task and delivery records introduced by this release use schema v2; the new
reader continues to read every shipped v1 worker, handed-task, sent and refused
record. A pre-upgrade long-lived server does not understand v2 and must be
restarted after installation. `doctor` compares each in-scope server's start
time with the code it loaded and names that restart when on-disk code changed;
`antiphon launch` always starts a fresh host process.

## Commands

```bash
antiphon status            # transcripts, cursors, live peers and channel status
antiphon doctor            # read-only checkup: why is the bridge quiet?
antiphon summary [side]    # show the context that would be injected
antiphon setup             # (re)install the project setup
antiphon launch claude [--name NAME] [-- HOST_ARGS...]  # start a ready Claude session
antiphon launch codex [--name NAME] [-- HOST_ARGS...]   # start a ready Codex session
antiphon catch-up [side]   # skip undelivered history: page cursors jump to the live edge
antiphon sources scan      # finish or refresh the durable source catalog
antiphon sources compact   # retire aged gone sources proved consumed by every relevant reader
antiphon retrieve <id>     # print one complete tool invocation (never its result)
antiphon task delegate     # start a managed worker for one task (JSON on stdin: text, kind, to, task, timeout)
antiphon task status|result|cancel <id> [wait]   # follow a delegated task by id; status, result and a new delegate's admission stop a worker past its timeout
antiphon task list         # every task on record: id, kind, class, state, started
antiphon --version         # the installed version
npm test                   # Python unit tests + real MCP protocol test
test/e2e/fresh-user.sh     # what a new user gets, with the real CLIs (not in npm test)
```

`catch-up` is for the other quiet: pages that keep arriving but are days old.
After an upgrade the bridge may re-deliver history from the start of every
transcript it can see, one page per turn, and a new message waits behind all
of it. `catch-up` pins each side's page cursor at the live edge — the end of
the last complete record in every safely proved catalog source — under the
same lock the readers take, and says how many bytes it abandoned. Sources that
cannot be proved are counted and left alone, as are their existing cursor
entries. What it skips is not
delivered later; run it when both terminals have already been read by the
person sitting at them. Unnamed, it moves both sides; a named terminal has its
own cursor and is told which side to move. `status` shows how far behind each
reader is as `unread <reader>: N raw bytes …` — raw transcript bytes, never a
page count, which cannot be derived from them — and a replaying reader's
page, `status` line and `doctor` note all name `catch-up`. Upgrading from
0.1.0 no longer replays from the start of every transcript: the page resumes
at the first record at or after the old cursor's time, and whatever 0.1.0 had
already left behind before that time is not replayed.

`doctor` answers "why is nothing arriving?" and edits nothing: which copy
of the package `PATH` resolves and whether the hooks run it, which bridge servers are running and whether any of
them started before its own code last changed (a server loads its code once;
the hooks reload every turn), the Node and
Python the bridge actually gets, every file `setup` writes read back
through the shapes `setup` wrote, the current alias, the registered peers,
whether the durable source catalog is complete for each reader, and whether
the Claude channel *answers* — a connect and a one-line reply,
not a file that exists. `✓` fine, `·` nothing to do here, `✗` broken; only
a `✗` makes it exit non-zero, so a set-up project with no session running
exits 0. It never takes a lock, never writes, and never removes the stale
record it is explaining; `doctor --fix` is the one exception, and it is
`setup` followed by the same read-only check.

`setup` registers Codex's MCP tools — `antiphon_read`, `antiphon_send`,
`antiphon_retrieve`, `antiphon_delegate` and `antiphon_task`
— in this project's `.codex/config.toml`, so there is nothing to add by
hand. Note the entry
names `args = ["mcp"]`: the `channel` server is Claude's side and hands out
`reply_to_codex` plus its own `antiphon_retrieve`. Aiming Codex at it would let
Codex publish messages
labelled as Claude's — exactly what this bridge exists to prevent — so
`setup` rewrites that table whenever it is wrong, leaving the rest of the
file alone. The same table forwards `ANTIPHON_NAME`, `ANTIPHON_HOP` and
`ANTIPHON_HOP_BUDGET` into the tool process, because Codex does not pass the
parent environment through on its own.

Without this entry the pull hook still delivers Claude's context at the start
of each Codex turn, but Codex loses all five tools: it can no longer check the
bridge by hand, retrieve a complete invocation, nor reach Claude before its
turn ends.

## Limits

- A live push needs an open session on the target side. If the target is closed, the message still shows up on the next pull.
- The Claude MCP Channel is only live while Claude Code is started with the right development-channel flag.
- Channels is currently a research preview; it requires a claude.ai login or a Console API key. It isn't supported on the Bedrock, Vertex, or Foundry providers. A Team/Enterprise admin may need to enable the feature.
- The Codex hook asks for re-approval the first time it's used and whenever the hook file changes.
- Matching is done on the same project's absolute directory.
- Claude discovery treats every immediate basename ending in `.jsonl` as a
  candidate, including dot-prefixed and zero-length entries, consistently in
  fallback, the durable catalog, and the release census. Filesystem-safe,
  visible, non-empty regular transcripts stay first in the three-file degraded fallback.
- Unix sockets only — there is no Windows support.
- A same-vendor message (`@claude:name` from Claude, `@codex:name` from Codex, `reply_to_claude`, `antiphon_send(kind="codex")`) is always addressed, and the passive page gains no same-kind lane — two same-kind sessions need names or automatic aliases, and a session of one kind is told nothing of another's work unless it is sent. It is addressed, not confidential: a Stop-marker line is part of the sender's visible reply, which the other kind's page shows, and a same-kind tool call's arguments stay retrievable by their public id.
- A managed worker is a subprocess of the other CLI (`claude -p` / `codex exec`), never a host-native subagent: it needs that CLI installed and logged in, is stopped at its timeout (at most an hour) by the next `status`, `result`, hook sweep or a new delegation's admission rather than by a clock of its own, appears in neither host's own agent UI, and its patch is evidence, never a merge. A worker cannot be resumed; a follow-up is a new task. It inherits the environment of the session that started it, as any subprocess of that session would. It is followed by its task id, and it is a peer only where its working directory carries the bridge's configuration — a task run in place, or a checkout that commits the generated files — and then a live named one, `worker-<id8>`, for its duration; in a worktree without that configuration it registers nothing and appears on no page. Measured on Codex CLI 0.152.1: `codex exec -s workspace-write` (a write task) records the project's root as trusted in `~/.codex/config.toml`, the way an interactive `codex` would after its own prompt; `-s read-only` does not. The bridge does not undo that in your projects — it is the host's own record of your project — and says so here.
- A tool result is a statement about the transport, never about the peer. `reply_to_codex` says queued and `antiphon_send` says delivered to the channel; if the acknowledgement disappears after bytes may have left, either returns the attempt id, says not to retry, and reports `outcome: "unknown"` unless the peer's transcript already proves receipt — then it truthfully reports `outcome: "received"`. On a direct tool result, `dedupe_recorded: false` means the mid-turn Stop-marker fingerprint could not be persisted, so remove the same addressed marker from the turn or it may repeat. A Stop send instead retains an exact outcome if its cursor promotion fails and suppresses that exact turn; `status` and `doctor` show the retained protection. A queued row in a thread that never takes a turn is not read, and only the peer's transcript proves receipt — `antiphon status` shows what still waits, `antiphon doctor` notes what has waited more than ten minutes. A bare reply refused among several peers names the last unanswered sender as advice; the bridge itself still never chooses.

### Passive pull pages, and what it still cannot promise

The pull path delivers the other side's transcript as pages of completed
records, oldest first. An ordinary full page targets 8,000 UTF-8 bytes and at
most 40 completed source records — the byte number is measured against the
installed hosts' injection limits, not a permanent host guarantee. Records
from the page's current local date use `[HH:MM]`; records from any other date
use `[YYYY-MM-DD HH:MM]`, so overnight history and replay cannot look current.
If the host timestamp cannot be rendered, the label is `[time unavailable]`.
The longer label is part of the same 8,000-byte budget. Non-tool records are no
longer cut or flattened: line structure, indentation, code and
SQL formatting travel intact, and a record is never split across pages.
Tool calls appear as compact events with ids. Claude tool entries retain the
pre-existing selected `file_path`, `command` or `pattern` value as a compact
detail; the rest of the argument object is absent. Codex tool entries remain
name-only. Complete invocations require retrieval, and tool results remain
unavailable. Tool outputs remain filtered while still advancing the safe
scanned frontier.

#### Tool invocation ids and retrieval

Every compact tool-call entry carries a 22-character opaque, content-bound
`tc1.<kind>.<digest>` id. Both agents can call
`antiphon_retrieve(id="<id>")`; the CLI equivalent is `antiphon retrieve <id>`.
Retrieval returns the complete invocation only, never the tool result: side,
call type, safe tool name, optional namespace/caller and the complete argument
value. Claude argument objects keep their JSON types; Codex free-form and
function arguments remain exact strings rather than being guessed into JSON.
Source ids, native ids, paths, offsets and generations are not returned.

Retrieval is read-only, write-free and cursor-neutral. It scans every safely
discovered candidate to avoid accepting the first of two matches, and returns
one of five honest outcomes: `found`, `invalid-id`, `unavailable`, `ambiguous`
or `untrusted`. A content-bound id protects an earlier-prefix rewrite: changed
invocation bytes receive a new id, and the old id never returns the new bytes.
There is no persistent invocation index or tombstone. Consequently changed,
expired and never-existed ids cannot be distinguished and all honestly collapse
to `unavailable`; doctor cannot recover that distinction without the rejected
persistent prefix/index state.

An MCP retrieval above 8,000 UTF-8 bytes is refused without truncation and
names `antiphon retrieve <id>`, which prints the full invocation. Host retention
or `antiphon sources compact` can make an old id unavailable. Two copies of one
transcript identity inside a host discovery root make retrieval `untrusted`;
a backup outside those roots does not affect discovery.

A page that leaves work behind says so with a visible `has_more: true` line;
calling `antiphon_read` again (or simply letting later turns run) drains the
rest. `has_more_scope: catalogued project sources` names the boundary. A
`has_more: false` page proves the current durable project catalog is drained
only when it has no `discovery: building` or `discovery: degraded` line. Either
marker makes the incomplete discovery boundary explicit instead of allowing a
newest-file fallback to masquerade as project completeness.

A page never carries a record older than 24 hours before the newest complete
record the other side wrote in any of its sources — its last dated record
within the last megabyte; a source undated for longer has none — one moment
for the whole reader, never later than the wall clock, so a source that stopped more than a
day before that newest record is skipped whole — unless its records carry no
readable time, which are read rather than skipped on the strength of records
before them — and a record stamped in the future cannot move the horizon. A reader that fell further behind skips to
that point: the page says `skipped: N raw bytes of … activity older than 24
hours …` once, where it happened, `status` counts what the next page will
skip, and the transcripts keep what was skipped. Measured before the horizon existed, a
reader more than 400 pages behind delivered a day-old page on every turn;
bounded to a day it delivers 21. A brand-new reader still starts 6 hours
back, and `antiphon catch-up` remains the way to skip to the live edge at
once.

The catalog lives under `.antiphon/sources/` as small state, immutable
generation manifests and partitioned per-candidate records; it stores paths and
fingerprints, never transcript content. Each hook records its own current
source first and inspects at most 8 candidate records per hook. A generation is
finite (`base`, one reconciliation pass, one delta, then `complete`); later
changes start a new refresh. `antiphon sources scan` runs the same bounded
steps explicitly until the catalog is complete, returns nonzero for pending or
refused work, and never moves a page cursor. State and immutable manifests are
loaded under a short shared lock; successful state switches reclaim only
grammar-valid, unreferenced regular manifests. Failed cleanup is retried by the
next catalog mutation and appears as an aggregate count in `status`, `doctor`
and the scanner, never as paths or source ids.

Every admitted transcript is opened beneath its host root without following
symlinks, checked as a regular file, and read for identity, metadata, generation
and records through that one descriptor. Claude membership comes from the
selected host project slug; Codex membership requires an exact project path in
session metadata. The newest 3 files remain only a bounded current-window and
degraded fallback, not the correctness inventory. A missing path becomes
`gone` only when every recorded path for that source is missing; whether that
gap still matters is calculated separately from each reader's own cursor and
the six-hour lookback. Permissions, unsafe paths, type changes, identity
collisions and transient I/O stay refused and keep discovery degraded.

One record larger than an ordinary page is handled asymmetrically, from
measurement rather than preference. Both hosts' automatic prompt hooks save an
oversized injection to a host-managed file and show the model a preview and the
path, so the hook hands such a record over whole — which means host-written
spill files may contain verbatim transcript text, under the host's own
lifecycle. Codex's MCP tool-result surface showed no such verified path, so
`antiphon_read` refuses that one record instead: nothing is read or marked
seen, and the next automatic prompt hook delivers it.

Page positions now live under `<side>_pages_v4`, beside the preserved v3 sibling
`<side>_pages` and legacy `<side>_seen` key. Every sibling's parsed value,
including unknown fields and JSON types, remains deeply equal for rolling old
processes and rollback; canonical reserialization may change whitespace or key
order. V4 never lets a later sibling write move its frozen adoption frontier.
Each v4 position anchors
the last complete source record by content. During adoption from a valid v3
frontier, that last record repeats at most once while the anchor is established;
an in-place rewrite that keeps inode, length and the first line no longer skips
silently. A malformed or unreadable existing cursor restarts every discovered
source from byte zero; only a genuinely missing cursor means a new side and
keeps the normal six-hour lookback. The measured full recovery was 69
Claude-source pages and 53 Codex-source pages on the reviewed snapshots. Every
replay page carries one of exactly three fixed explanation lines — legacy
upgrade, cursor recovery or anchor adoption — until its corresponding recovery
finishes. A failed delivery changes neither frontier nor scheduler lane.

Live and unknown sources share the active lane. Only a current process
fingerprint can prove a source dead; missing, legacy or unreadable identity
evidence stays unknown. When both active and dead backlog exist, delivery
alternates whole pages after each successful delivery, so live replies are not
stranded behind dead history and dead history still drains. `status` and
`doctor` report aggregate live/unknown/dead counts, adoption and the next lane,
without identities or anchors.

Candidate retirement is separate and explicit. `antiphon sources compact`
first completes discovery, then retires only a whole aged, gone source that
every relevant v4 reader proves consumed (or has no entry for after lookback).
The shared cursor is always relevant; a named cursor is dormant only when its
current process fingerprint proves its recorded owner dead. Unknown ownership
stays relevant. The command revalidates every value its decision read —
including the deeply typed values of the candidate records it would retire —
under the catalog lock around its atomic state switch. Unrelated record updates
do not block it. Output reports only aggregate blocker classes and reclaimed
files/bytes, tells the operator to retry only when a revalidated input snapshot
changed, and preserves all cursor files. A proof failure that cannot be
interpreted as a transient change reports that no automatic remedy was
attempted. A durable prepared/committed journal keeps the old catalog visible
until post-switch proof succeeds; a crash or failed rollback therefore retries
or rolls back instead of turning an unproved detached record into deletion proof.
Hooks never retire candidates; they may only recover a prepared safe view.
Committed record cleanup remains an explicit `sources compact` operation.

What still loses, by name: tool results remain unavailable, and there is no
backward paging into history an older version already marked seen. Those are
tracked in
[BACKLOG.md](BACKLOG.md).

### An oversized direct message is parked, never truncated

A message sent from Codex to Claude with `antiphon_send` or `@claude` uses the
Unix-socket channel. Its serialized payload has a separate 128 KiB byte cap,
checked by the sender before transport and by the server on arrival — the
serialized size, not the character count, because JSON escaping is unbounded: a
message of control characters crosses the cap at a sixth of its raw length.
Nothing is ever silently shortened, so ordinary long code and SQL within the cap
travel intact.

Past that cap the two roads behave differently, and which one you are on
matters.

The direct tools park the words. Over the cap, `antiphon_send` and
`reply_to_codex` write the full text to `.antiphon/messages/<uuid>.txt` — mode
0600, inside a 0700 directory — and deliver a small envelope naming the absolute
path, the content's size and its SHA-256. The file opens with one line of
provenance saying whose words follow; the content is everything after the first
blank line, and the hash covers only that, so `tail -n +3 <path> | shasum -a
256` verifies it. The path is local, and that is the bound: both agents run on
this machine, as this user, against this project. One attachment may hold 8 MiB
of content, the whole store 64 MiB, and a parked file becomes eligible for
removal 7 days after it was written. There is no timer: it goes on the next
hook either side runs, announced on stderr and named, so a project where
neither side takes a turn keeps its files until one does. `antiphon status`
reports how many are pending, how many content bytes they hold and how old the
oldest is, and never deletes anything itself. Above the per-attachment
cap, or with the store full, the send is refused with the reason and nothing is
written; a send that fails for any other reason removes its parked file at once.

The `@claude` and `@codex` marker road does not park. A marker line over the cap
is still refused, and that refusal prints on an exit-0 Stop hook, which this
project measured as reaching a debug log rather than the agent. It is a
deliberate asymmetry rather than an unfinished half: a marker line's words are
already in the visible reply, and the passive pull pages carry that reply
whole, so an attachment there would duplicate for nobody what pull already
delivers.

The reverse Claude-to-Codex path uses `codex queue` and has no Antiphon byte cap
of its own. What bounds it is the kernel's `ARG_MAX` — one budget argv shares
with the environment, so the largest message that can be handed over shrinks
byte for byte as the environment grows, measured here from 1,044,820 down to
444,759. `reply_to_codex` computes that bound at call time and parks above it,
and an exec the kernel refuses for any other reason comes back as a named
refusal instead of a traceback.

A parked file has a read receipt: the named recipient's own transcript shows
the tool call that read it — another peer of the same kind opening the file is
not the receipt — and the reader that already walks that transcript records
the receipt on the delivery ledger under `.antiphon/deliveries/`. A file with
a read receipt is collected one hour after the read; a file without one waits
out the 7 days and then expires unread, and its sender hears that on its next
page. A resent message with the same words, from the same sender to the same
peer, is reused: the envelope names the file that already exists and the
store holds one copy. The read applies to every matching attempt already begun
at the receipt time, never to a later reuse of that basename. Read-grace
collection starts from the latest read only when every ledger attempt naming
the file is read and the file was not touched at or after that proof; an unread
reuse, or a reuse whose process died before writing its row, waits for the full
file TTL. `antiphon status` uses the same conservative rule when it counts the
parked files with and without a read receipt.

MIT.
