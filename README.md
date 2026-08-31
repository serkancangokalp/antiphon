# Antiphon

**Two agents in one project, an open-identity bridge.** Claude Code and Codex CLI work side by side — one terminal each, or several on each side — and each sees the other's context and can wake the other when it needs to, without ever faking who the message is from.

Antiphon doesn't dispatch work. It only carries messages between the sides while preserving whether they came from the human user, from Claude, or from Codex.

With one terminal per side there is nothing to configure beyond `antiphon setup`: peers go unnamed, messages have only one place to go, and the rest of this page is background. Naming becomes necessary the moment a second session opens on either side — see [Many peers](#many-peers).

## How it works

No shared log is kept. Both CLIs already write their own transcripts; Antiphon reads and derives from them, recording which messages each peer has already seen. An unnamed peer keeps its cursor at `.antiphon/cursor.json`; a named one owns `.antiphon/peers/<side>-<name>/cursor.json`, so two sessions on the same side never advance each other's place.

### Pull — shared context, no wake

| Direction | Mechanism |
|---|---|
| Codex → Claude | Claude `UserPromptSubmit` hook |
| Claude → Codex | Codex `UserPromptSubmit` hook |

The other side's recent messages enter your turn's context when you type
something. Nobody is woken up.

This path is project-wide awareness, not delivery. It is not addressed to
anyone, and today it can merge activity from several project transcripts under
one generic `Claude` or `Codex` label — so with several terminals open, one
agent's words can arrive looking like another's. Source-aware labelling is a
tracked P1 item in [BACKLOG.md](BACKLOG.md). Until it lands, do not read pull
context as a private line between two particular peers.

### Push — addressed, live wake

| Direction | At the end of a turn | Mid-turn |
|---|---|---|
| Claude → Codex | `Stop` hook + `codex queue` | `reply_to_codex` tool |
| Codex → Claude | `Stop` hook + MCP Channel | `antiphon_send` tool |

A line starting with `@codex` or `@claude` in a reply reaches the other
agent immediately, even if nobody is typing.

Every push is addressed to exactly one peer and is never broadcast. When the
recipient cannot be shown to be the only candidate, the send is refused rather
than guessed.

Neither side has to wait for its turn to end. Either agent can hand work
over mid-turn and keep going, so the other starts on it in parallel; the
answer is collected later from the same turn with `antiphon_read` (Codex)
or the channel event (Claude). Nothing blocks, and a message delivered by
a tool is recorded, so ending the turn with the same `@claude` / `@codex`
line does not send it twice.

### How identity is preserved

A Claude → Codex message reaches Codex tagged either `[Antiphon bridge] Claude:` (pushed from Claude's Stop hook) or `[Antiphon channel] Claude:` (a direct reply sent through the channel, via the `reply_to_codex` tool) — either way, Codex sees these as Claude's words, not the human user's.

The tag is followed by `[from=<alias> id=<uuid>]`, naming which Claude peer spoke. A session whose configured identity does not own its return channel instead carries `[from=<alias> reply_to=<unavailable> id=<uuid>]`; that exception is explained below. A session started without `ANTIPHON_NAME` shows `from=<unnamed>`: it has no name to be addressed by, and the angle brackets keep that apart from a peer actually called `unnamed`. The id names one delivery attempt — it is not a correlation id, and nothing routes replies by it.

A valid Claude `ANTIPHON_NAME` is the session's configured identity, not proof that its named return channel is reachable. If startup says the channel was not acquired, the name still identifies that session's outgoing words, but the label alone does not establish that a reply will reach the same process. The startup warning exposes a duplicate-name loss; `antiphon doctor` exposes a named listener whose endpoint registration is missing. Restart the Claude session after correcting either fault.

If a label carries `reply_to=<unavailable>`, do not reply to its `from` alias: that channel belongs to a different session. The sender remains identified, but there is deliberately no routable return alias until its Claude channel is restarted under a unique name.

An `Antiphon delivery notice:` event is a bridge-authored diagnostic: it carries no original message content and does not turn the sender's refusal into delivery. Antiphon sends one only when an explicitly requested alias has no live endpoint record but that alias's deterministic socket still answers; the notice names the alias and attempt time, while the original send remains refused.

A Codex → Claude message never pastes text into the terminal and never impersonates user input. The local MCP server sends Claude Code a `notifications/claude/channel` event. Its metadata looks like:

```xml
<channel source="antiphon" sender="codex" sender_kind="agent" sender_alias="build" message_id="...">
```

Claude Code's interface shows this as an incoming channel event, and Claude treats the message as the words of the Codex agent, not of the human user. It sends its reply back with the `reply_to_codex` MCP tool, passing `sender_alias` as `to` whenever it is a name rather than the literal `<unnamed>`. A bare reply is refused as soon as any named Codex peer is registered: an unnamed Codex session leaves no registry record, so one visible peer cannot be shown to be the only one running. A `sender_alias` of `<unnamed>` is a peer with no name — it cannot be addressed by name, and a bare reply reaches it only in a project where nothing is registered; passing `<unnamed>` as `to` is the same as leaving it out.

Nothing pairs peers up. There is no automatic Claude↔Codex partnership, and no reply correlation: a message is routed only by the name written on it.

## Many peers

A name is an environment variable read at startup, so it goes in front of the
command:

    ANTIPHON_NAME=ui claude --dangerously-load-development-channels server:antiphon
    ANTIPHON_NAME=api claude --dangerously-load-development-channels server:antiphon
    ANTIPHON_NAME=build codex
    ANTIPHON_NAME=review codex

Names must be unique per side within a project. Two Claude sessions configured
with the same name both identify their own outgoing words by that name, but
only the one that owns the named channel can receive a reply there.

Once named, a peer is addressed explicitly — by marker at the start of a line,
or by the `to` argument of the tool that sends without ending the turn:

| From | Marker | Tool |
|---|---|---|
| Codex → Claude | `@claude:ui` | `antiphon_send(to="ui", text=…)` |
| Claude → Codex | `@codex:build` | `reply_to_codex(to="build", text=…)` |

There is no way to reach several peers at once. A send is delivered to the one
peer named on it, and to nobody else.

### When a bare message is refused

The two sides fail closed on different rules, because they leave different
traces:

- **To Claude.** A bare `@claude` works while exactly one Claude peer is live.
  From the second one on, it is refused and you must name one.
- **To Codex.** A bare `@codex` is refused as soon as *any* named Codex peer is
  registered — even if it is the only one you can see. A Codex session started
  without a name leaves no registry record at all, so a second, unnamed one
  cannot be ruled out, and the bridge will not guess between a peer it can see
  and one it cannot.

That asymmetry is why **every terminal in a multi-peer project must be named,
Codex terminals above all**. Mixing named and unnamed sessions is the one
configuration that can leave a message impossible to answer: the unnamed peer
is live, it can send, and there is no name to send a reply back to.

### Seeing who is live

    antiphon status

Beyond transcripts and cursors, `status` lists every registered peer with the
side it runs on, the name it took, and its state — `ready` once it has an
address to receive on, or `waiting for first turn` before that. Under the list
it prints the addressing rule that currently applies:

```
Peers:
  Claude ui — ready
  Claude api — ready
  Codex build — ready
  Codex review — waiting for first turn
  → a bare @claude line is refused; address one: @claude:ui, @claude:api
  → a bare @codex line is refused, because unnamed Codex sessions leave no record; address one: @codex:build, @codex:review
```

A peer that is `waiting for first turn` is still a candidate: readiness never
decides who a message goes to, so it cannot silently hand routing to whichever
session happened to start first. With nothing registered — the unnamed single
pair — the block is empty, because there is nobody to choose between.

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
    claude --dangerously-load-development-channels server:antiphon

`setup` writes `.claude/settings.json`, `.codex/hooks.json`,
`.codex/config.toml`, `.mcp.json`, `.claude/settings.local.json`,
`AGENTS.md` and `CLAUDE.md` for that project. The hook commands resolve
`antiphon` through your `PATH` rather than hardcoding an install path, so
they keep working after a reinstall or a move; `.mcp.json` and
`.codex/config.toml` still record this project's own absolute directory,
so each side can find the right channel socket. None of the seven files
live in this repository — they are generated per project. Approve the
Codex hooks once when Codex first shows them.

## Update

    npm i -g github:serkancangokalp/antiphon      # from the repository
    npm i -g antiphon@latest                      # from npm

    cd /your/project && antiphon setup

Re-running `setup` migrates hooks and instruction blocks written by older
versions in place; it never creates duplicates.

## Commands

```bash
antiphon status            # transcripts, cursors, live peers and channel status
antiphon doctor            # read-only checkup: why is the bridge quiet?
antiphon summary [side]    # show the context that would be injected
antiphon setup             # (re)install the project setup
antiphon catch-up [side]   # skip undelivered history: page cursors jump to the live edge
antiphon --version         # the installed version
npm test                   # Python unit tests + real MCP protocol test
test/e2e/fresh-user.sh     # what a new user gets, with the real CLIs (not in npm test)
```

`catch-up` is for the other quiet: pages that keep arriving but are days old.
After an upgrade the bridge may re-deliver history from the start of every
transcript it can see, one page per turn, and a new message waits behind all
of it. `catch-up` pins each side's page cursor at the live edge — the end of
the last complete record in every discovered transcript — under the same lock
the readers take, and says how many bytes it abandoned. What it skips is not
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
and whether the Claude channel *answers* — a connect and a one-line reply,
not a file that exists. `✓` fine, `·` nothing to do here, `✗` broken; only
a `✗` makes it exit non-zero, so a set-up project with no session running
exits 0. It never takes a lock, never writes, and never removes the stale
record it is explaining.

`setup` registers Codex's MCP tools — `antiphon_read` and `antiphon_send`
— in this project's `.codex/config.toml`, so there is nothing to add by
hand. Note the entry
names `args = ["mcp"]`: the `channel` server is Claude's side and hands out
`reply_to_codex`. Aiming Codex at it would let Codex publish messages
labelled as Claude's — exactly what this bridge exists to prevent — so
`setup` rewrites that table whenever it is wrong, leaving the rest of the
file alone. The same table forwards `ANTIPHON_NAME` into the tool process,
because Codex does not pass the parent environment through on its own.

Without this entry the pull hook still delivers Claude's context at the start of each Codex turn, but Codex loses both tools: it can no longer check the bridge by hand, nor reach Claude before its turn ends.

## Limits

- A live push needs an open session on the target side. If the target is closed, the message still shows up on the next pull.
- The Claude MCP Channel is only live while Claude Code is started with the right development-channel flag.
- Channels is currently a research preview; it requires a claude.ai login or a Console API key. It isn't supported on the Bedrock, Vertex, or Foundry providers. A Team/Enterprise admin may need to enable the feature.
- The Codex hook asks for re-approval the first time it's used and whenever the hook file changes.
- Matching is done on the same project's absolute directory.
- Unix sockets only — there is no Windows support.

### Passive pull pages, and what it still cannot promise

The pull path delivers the other side's transcript as pages of completed
records, oldest first. An ordinary full page targets 8,000 UTF-8 bytes and at
most 40 completed source records — the byte number is measured against the
installed hosts' injection limits, not a permanent host guarantee. Non-tool
records are no longer cut or flattened: line structure, indentation, code and
SQL formatting travel intact, and a record is never split across pages.

A page that leaves work behind says so with a visible `has_more: true` line;
calling `antiphon_read` again (or simply letting later turns run) drains the
rest. Either `has_more` value describes only the transcripts discovery can
currently see — discovery still reads only the newest 3 transcript files per
side, so `has_more: false` is not an inventory of all project history.

One record larger than an ordinary page is handled asymmetrically, from
measurement rather than preference. Both hosts' automatic prompt hooks save an
oversized injection to a host-managed file and show the model a preview and the
path, so the hook hands such a record over whole — which means host-written
spill files may contain verbatim transcript text, under the host's own
lifecycle. Codex's MCP tool-result surface showed no such verified path, so
`antiphon_read` refuses that one record instead: nothing is read or marked
seen, and the next automatic prompt hook delivers it.

Page positions live under an isolated v3 cursor key, `<side>_pages`
(`claude_pages`, `codex_pages`). The old `<side>_seen` value is
preserved untouched beside it for still-running pre-upgrade processes and
rollback — it is never trusted or overwritten by paging code, and it is
scheduled for
retirement once pre-v3 processes no longer need it, not a template for
accumulating keys. Any present legacy value, and equally a malformed or
unreadable existing cursor file, starts a conservative replay of the currently
discovered sources from byte zero; only a genuinely missing cursor means a new
side and keeps the normal six-hour lookback. The old promise that a timestamp
cursor migrates at its exact boundary is gone: that boundary cannot be trusted
while an old process may still move it. The replay is bounded but it is not
small — on the reviewed snapshots it took 69 Claude-source pages and 53
Codex-source pages, up to that many automatic prompt turns — and every replay
page carries one of exactly two fixed explanation lines, one for the legacy
upgrade and one for cursor recovery, until the final successfully persisted
page clears it, so duplicated history is visible as recovery rather than
mistaken for a malfunction. A failed delivery leaves the cursor bytes exactly
as they were.

What still loses, by name: tool calls remain compressed one-line summaries
with no stable-id retrieval yet, discovery has no catalog (the newest-3 window
above), and there is no backward paging into history an older version already
marked seen. Those are tracked in [BACKLOG.md](BACKLOG.md).

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

Acknowledgement and retry are not part of this: an envelope rides the same
at-least-once delivery every message does, nothing ever learns the file was
read, and a resent message parks a second file rather than reusing the first.
Those two remain a P1 item in [BACKLOG.md](BACKLOG.md).

MIT.
