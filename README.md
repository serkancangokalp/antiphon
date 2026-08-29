# Antiphon

**Two terminals, two separate agents, an open-identity bridge.** While Claude Code and Codex CLI work in the same project, each sees the other's context and can wake the other when it needs to, without ever faking who the message is from.

Antiphon doesn't dispatch work. It only carries messages between the two sides while preserving whether they came from the human user, from Claude, or from Codex.

## How it works

No shared log is kept. Both CLIs already write their own transcripts; Antiphon reads and derives from them, marking in `.antiphon/cursor.json` which messages each side has already seen.

### Pull — context, no wake

| Direction | Mechanism |
|---|---|
| Codex → Claude | Claude `UserPromptSubmit` hook |
| Claude → Codex | Codex `UserPromptSubmit` hook |

The other side's recent messages enter your turn's context when you type
something. Nobody is woken up.

### Push — live wake

| Direction | Mechanism |
|---|---|
| Claude → Codex | Claude `Stop` hook + `codex queue` |
| Codex → Claude | Codex `Stop` hook + MCP Channel |

A line starting with `@codex` or `@claude` in a reply reaches the other
agent immediately, even if nobody is typing.

### How identity is preserved

A Claude → Codex message reaches Codex tagged either `[Antiphon bridge] Claude:` (pushed from Claude's Stop hook) or `[Antiphon channel] Claude:` (a direct reply sent through the channel, via the `reply_to_codex` tool) — either way, Codex sees these as Claude's words, not the human user's.

A Codex → Claude message never pastes text into the terminal and never impersonates user input. The local MCP server sends Claude Code a `notifications/claude/channel` event. Its metadata looks like:

```xml
<channel source="antiphon" sender="codex" sender_kind="agent" message_id="...">
```

Claude Code's interface shows this as an incoming channel event, and Claude treats the message as the words of the Codex agent, not of the human user. It sends its reply back with the `reply_to_codex` MCP tool.

## Install

Requires Node 18+ and Python 3. The Claude Code channel is a research
preview and needs Claude Code 2.1.80 or newer.

    npm i -g antiphon
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

    npm i -g antiphon@latest
    cd /your/project && antiphon setup

Re-running `setup` migrates hooks and instruction blocks written by older
versions in place; it never creates duplicates.

## Commands

```bash
antiphon status            # transcript, cursor and channel status
antiphon summary [side]    # show the context that would be injected
antiphon setup             # (re)install the project setup
npm test                   # Python unit tests + real MCP protocol test
```

`setup` registers the `antiphon_read` MCP tool for Codex in this project's
`.codex/config.toml`, so there is nothing to add by hand. Note the entry
names `args = ["mcp"]`: the `channel` server is Claude's side and hands out
`reply_to_codex`. Aiming Codex at it would let Codex publish messages
labelled as Claude's — exactly what this bridge exists to prevent — so
`setup` rewrites that table whenever it is wrong, leaving the rest of the
file alone.

The bridge works without this entry; it only lets Codex query the bridge by hand when it suspects the pull hook has gone quiet.

## Limits

- A live push needs an open session on the target side. If the target is closed, the message still shows up on the next pull.
- The Claude MCP Channel is only live while Claude Code is started with the right development-channel flag.
- Channels is currently a research preview; it requires a claude.ai login or a Console API key. It isn't supported on the Bedrock, Vertex, or Foundry providers. A Team/Enterprise admin may need to enable the feature.
- The Codex hook asks for re-approval the first time it's used and whenever the hook file changes.
- Matching is done on the same project's absolute directory.
- Once a message has been seen, the cursor advances and the same content is never injected twice.
- Context transfer has a budget of roughly 2600 characters; the newest messages are kept.
- Unix sockets only — there is no Windows support.

MIT.
