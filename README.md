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

The tag is followed by `[from=<alias> id=<uuid>]`, naming which Claude peer spoke so a reply can be addressed back to it. A session started without `ANTIPHON_NAME` shows `from=<unnamed>`: it has no name to be addressed by, and the angle brackets keep that apart from a peer actually called `unnamed`. The id names one delivery attempt — it is not a correlation id, and nothing routes replies by it.

A Codex → Claude message never pastes text into the terminal and never impersonates user input. The local MCP server sends Claude Code a `notifications/claude/channel` event. Its metadata looks like:

```xml
<channel source="antiphon" sender="codex" sender_kind="agent" sender_alias="build" message_id="...">
```

Claude Code's interface shows this as an incoming channel event, and Claude treats the message as the words of the Codex agent, not of the human user. It sends its reply back with the `reply_to_codex` MCP tool, passing `sender_alias` as `to` whenever it is non-null. A bare reply is refused as soon as any named Codex peer is registered: an unnamed Codex session leaves no registry record, so one visible peer cannot be shown to be the only one running. A `null` `sender_alias` is a peer with no name — it cannot be addressed by name, and a bare reply reaches it only in a project where nothing is registered.

Nothing pairs peers up. There is no automatic Claude↔Codex partnership, and no reply correlation: a message is routed only by the name written on it.

## Many peers

A name is an environment variable read at startup, so it goes in front of the
command:

    ANTIPHON_NAME=ui claude --dangerously-load-development-channels server:antiphon
    ANTIPHON_NAME=api claude --dangerously-load-development-channels server:antiphon
    ANTIPHON_NAME=build codex
    ANTIPHON_NAME=review codex

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
antiphon summary [side]    # show the context that would be injected
antiphon setup             # (re)install the project setup
npm test                   # Python unit tests + real MCP protocol test
```

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

### Passive pull loses content, permanently

The pull path is lossy today, and the loss is silent: the cursor advances past
what was dropped, so nothing re-delivers it and nothing reports it. Against one
turn it cuts in this order:

- discovery reads only the newest 3 transcript files per side and only the last
  300,000 bytes of each;
- selection keeps only the newest 40 events from what discovery found;
- rendering collapses each non-tool event's whitespace to single spaces — code
  and SQL line breaks with it — and cuts that event to 420 characters;
- the whole rendered summary is cut to 2,600 characters, keeping its newest
  lines.

A message longer than any of those does not arrive truncated with a warning; the
remainder is marked seen and is gone. Lossless oldest-first paging, with a
per-peer cursor that only advances over records actually delivered, is the P0
item in [BACKLOG.md](BACKLOG.md) — it is designed, not yet built. Until then, do
not use the pull path to move anything you cannot afford to lose; send it
directly instead.

### The Codex-to-Claude channel refuses rather than truncates

A message sent from Codex to Claude with `antiphon_send` or `@claude` uses the
Unix-socket channel. Its serialized payload has a separate 128 KiB byte cap,
checked by the sender before transport and by the server on arrival. Over that,
the send fails with an error you can see; it is never silently shortened, so
ordinary long code and SQL within the cap travel intact.

The reverse Claude-to-Codex path uses `codex queue` and does not share that
explicit Antiphon byte cap. It also has no oversized-message attachment
protocol yet, so extremely large direct transfers in either direction remain a
P1 item in [BACKLOG.md](BACKLOG.md).

MIT.
