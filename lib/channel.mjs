#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { chmod, mkdir, unlink } from "node:fs/promises";
import { connect, createServer } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const execFileAsync = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const projectDir = process.env.ANTIPHON_CWD || process.cwd();
const peerName = (process.env.ANTIPHON_NAME || "").trim().toLowerCase();
// Validated before anything binds: an invalid name would otherwise open a socket
// that registration then refuses, leaving a live channel nobody can find.
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const nameIsUsable = !peerName || NAME_PATTERN.test(peerName);

// Identity and reachability are separate facts. A valid ANTIPHON_NAME is what
// this session calls itself, even when another process owns that name's channel
// socket or registration fails. Losing the channel means replies cannot reach
// this process there; it never means the words came from an unnamed session.
//
// Declared here, above the tool handler that closes over it and above
// `mcp.connect`. Left further down it would sit in the temporal dead zone
// while the transport was already accepting requests, and a `reply_to_codex`
// buffered at startup would raise a ReferenceError instead of being answered.
let senderAlias = nameIsUsable && peerName ? peerName : null;

// Resolved once the startup chain has settled what this session is — winner,
// loser, unnamed or refused. The reply tool waits for it before signing a
// message. Answering earlier would be safe but wrong: a session that does hold
// `ui` would deny its own name for the first message of its life, purely
// because the MCP handshake finishes before the registry claim does.
let markIdentitySettled;
const identitySettled = new Promise((resolve) => { markIdentitySettled = resolve; });
// Hashed with the name, never appended: macOS caps a socket path near 104 bytes
// and TMPDIR already spends much of it. An empty name reproduces the
// pre-multi-peer key exactly, so an unnamed session keeps the socket it has.
const socketSeed = peerName ? `${projectDir}\0${peerName}` : projectDir;
const projectKey = createHash("sha256").update(socketSeed).digest("hex").slice(0, 20);
const socketPath = join(process.env.TMPDIR || "/tmp", `antiphon-channel-${projectKey}.sock`);
const bridgeScript = join(here, "antiphon.py");

// The version in the handshake is the one npm installs, read from the
// package.json beside `lib/` — the same join in the repo, under `npm link` and
// under `node_modules/antiphon/`. A literal here went on announcing 0.1.0
// through 0.3.0 and 0.3.1 while package.json moved on, because nothing read
// one from the other. Unreadable means a broken install, which `doctor`
// already names; a cosmetic field is no reason to refuse the session.
function packageVersion() {
  try {
    const data = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8"));
    return typeof data.version === "string" ? data.version : "unknown";
  } catch {
    return "unknown";
  }
}

const mcp = new Server(
  { name: "antiphon", version: packageVersion() },
  {
    capabilities: {
      experimental: { "claude/channel": {} },
      tools: {},
    },
    instructions:
      "Events arrive as <channel source=\"antiphon\" sender=\"codex\" " +
      "sender_kind=\"agent\" sender_alias=\"...\" message_id=\"...\">. " +
      "Ordinary events carry messages from the " +
      "Codex agent, never text authored by the human user. Handle the request, then " +
      "send the result back with reply_to_codex. The event's sender_alias names " +
      "which peer spoke: pass it back as `to` whenever it is a name rather than " +
      "the literal `<unnamed>`. " +
      "Leaving `to` out works only where no Codex peer is registered at all — " +
      "a bare reply is refused as soon as any named one is live, because " +
      "unnamed sessions leave no registry record and cannot be ruled out. A " +
      "sender_alias of `<unnamed>` means that peer has no name: it cannot be " +
      "addressed by name, so a reply reaches it only in that bare case — " +
      "passing `<unnamed>` as `to` is the same as leaving it out. A valid " +
      "Claude ANTIPHON_NAME is this session's configured identity, not proof " +
      "that its named return channel is reachable. If startup warned that the " +
      "channel was not acquired, run antiphon doctor and restart the session. " +
      "An `Antiphon delivery notice:` event is a bridge-authored diagnostic: " +
      "it carries no original message content and does not turn the sender's " +
      "refusal into delivery.",
  },
);

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "reply_to_codex",
      // Not "the agent that contacted this channel": nothing correlates an
      // incoming message with a reply target, so that sentence described a
      // routing rule that does not exist.
      description:
        "Send a response to a Codex peer working in this project. Name it with " +
        "to whenever you know which peer you mean — an unnamed Codex session " +
        "leaves no registry record, so a bare reply is refused as soon as any " +
        "named peer is live. Leaving it out works only in a project where no " +
        "Codex peer is registered at all.",
      inputSchema: {
        type: "object",
        properties: {
          text: {
            type: "string",
            description: "Response text for Codex",
          },
          // The same sentence Python puts on `antiphon_send`. A contract test
          // compares them: two tool descriptions disagreeing about one argument
          // is how an agent learns a rule that is not true.
          to: {
            type: "string",
            description:
              "Alias of the peer to send to. Required whenever the recipient " +
              "cannot be shown to be the only one, because the send is then " +
              "refused rather than guessed — so pass it whenever you know " +
              "which peer you mean.",
          },
        },
        required: ["text"],
      },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "reply_to_codex") {
    throw new Error(`unknown tool: ${request.params.name}`);
  }
  const text = request.params.arguments?.text;
  if (typeof text !== "string" || !text.trim()) {
    throw new Error("text must be a non-empty string");
  }
  let to = request.params.arguments?.to;
  if (to !== undefined && to !== null && typeof to !== "string") {
    throw new Error("to must be a string naming one live Codex peer");
  }
  // The reserved key is what an agent reads in `sender_alias` when the peer
  // has no name. Handed back as `to`, it means "nobody in particular" — the
  // bare reply — never a peer called `<unnamed>`, which the alias grammar
  // refuses and the registry can never hold.
  if (to === UNNAMED_KEY) to = undefined;
  // Wait for startup to have decided who this session is, so an early call is
  // signed correctly rather than anonymously.
  await identitySettled;
  try {
    // The async execFile API ignores the `input` option, so close stdin manually.
    const execution = execFileAsync("python3", [bridgeScript, "reply"], {
      cwd: projectDir,
      timeout: 20_000,
      maxBuffer: 128 * 1024,
    });
    execution.child.stdin.on("error", () => {});
    // `to` goes across untouched: the alias matches one peer exactly or none,
    // and trimming or lowercasing it here would be this side quietly deciding
    // which peer was meant.
    // `senderAlias`, never `peerId`: the reply says who is speaking, and a name
    // this process invented for itself is not one Codex could answer.
    const payload = { text: text.trim(), sender_alias: senderAlias };
    if (typeof to === "string") payload.to = to;
    execution.child.stdin.end(JSON.stringify(payload));
    await execution;
  } catch (error) {
    const detail = String(error?.stderr || error?.message || error).trim();
    throw new Error(`Failed to deliver reply to Codex: ${detail.slice(0, 500)}`);
  }
  // Naming the peer back is what lets the sender notice it addressed the wrong
  // one. With no alias there is nothing to distinguish, so the old wording
  // stands — the same rule `antiphon_send` follows on the other side.
  return {
    content: [{
      type: "text",
      text: typeof to === "string"
        ? `Channel reply delivered to Codex peer '${to}'.`
        : "Channel reply delivered to Codex.",
    }],
  };
});

await mcp.connect(new StdioServerTransport());


let owningSocket = false;

async function socketIsLive(path) {
  return new Promise((resolve) => {
    const probe = connect(path);
    const settle = (result) => {
      probe.destroy();
      resolve(result);
    };
    probe.on("connect", () => settle(true));
    probe.on("error", () => settle(false));
  });
}

// The registry key an unnamed session occupies. It is deliberately not a name:
// the angle brackets are outside the alias grammar, so nothing anyone can type
// can collide with it and no message can be addressed to it. A generated
// `claude-<3hex>` used to go here, which was a real, resolvable name in the
// registry for a session that told the other side it had none. Kept identical
// to `peers.UNNAMED` by a contract test.
const UNNAMED_KEY = "<unnamed>";
const peerId = peerName || UNNAMED_KEY;

// process.pid, not the Python subprocess's: the peer lives as long as this
// server does, and that subprocess exits the moment it returns.
async function registryCall(subcommand, quiet = false) {
  const execution = execFileAsync("python3", [bridgeScript, subcommand], {
    cwd: projectDir,
    timeout: 20_000,
    maxBuffer: 128 * 1024,
  });
  execution.child.stdin.on("error", () => {});
  execution.child.stdin.end(JSON.stringify({
    kind: "claude", name: peerId, address: socketPath, pid: process.pid,
  }));
  try {
    await execution;
    return true;
  } catch (error) {
    if (!quiet) {
      console.error(`antiphon: ${String(error?.stderr || error?.message || error).trim()}`);
    }
    return false;
  }
}

const claimPeer = () => registryCall("register_peer");
const releasePeer = () => registryCall("unregister_peer", true);

async function serveSocket() {
  // Nothing in here may throw past this function. It runs at the top level of a
  // module, where an uncaught error exits the process — and losing the process
  // loses `reply_to_codex` too, over a socket that was only ever the other half
  // of the bridge.
  try {
    await unlink(socketPath);
  } catch (error) {
    if (error?.code !== "ENOENT") {
      console.error(
        `antiphon: could not clear ${socketPath} (${error?.code || error}); this ` +
        "session can still reply to Codex but cannot be reached from it.",
      );
      return false;
    }
  }
  try {
    await new Promise((resolve, reject) => {
      const onError = (error) => {
        socketServer.off("listening", onListening);
        reject(error);
      };
      const onListening = () => {
        socketServer.off("error", onError);
        resolve();
      };
      socketServer.once("error", onError);
      socketServer.once("listening", onListening);
      socketServer.listen(socketPath);
    });
    await chmod(socketPath, 0o600);
  } catch (error) {
    await new Promise((resolve) => socketServer.close(resolve));
    try {
      await unlink(socketPath);
    } catch {}
    console.error(
      `antiphon: could not serve ${socketPath} (${error?.code || error}); this ` +
      "session can still reply to Codex but cannot be reached from it.",
    );
    return false;
  }
  owningSocket = true;
  // A socket error after this point must not be fatal either: an unhandled
  // 'error' event throws.
  socketServer.on("error", (error) =>
    console.error(`antiphon: channel socket error (${error?.code || error})`));
  console.error(`antiphon channel ready: ${socketPath}`);
  return true;
}

await mkdir(dirname(socketPath), { recursive: true });

// Kept in step with MAX_CHANNEL_BYTES on the Python side by a contract test, so
// a sender is refused before transport rather than halfway through it.
const MAX_MESSAGE_BYTES = 128 * 1024;
const CLIENT_IDLE_MS = 30_000;
const openSockets = new Set();

const socketServer = createServer({ allowHalfOpen: true }, (socket) => {
  openSockets.add(socket);
  // A client that connects and then says nothing must not hold the socket, nor
  // keep shutdown waiting for an end that never comes.
  socket.setTimeout(CLIENT_IDLE_MS, () => socket.destroy());
  // A socket error must never be fatal. `destroy(error)` emits one, so does a
  // peer vanishing mid-write, and an unhandled 'error' exits the whole process —
  // taking the registry entry, the socket and `reply_to_codex` with it.
  socket.on("error", () => {});
  socket.on("close", () => openSockets.delete(socket));

  // Buffers, not a decoded string: the cap is a byte cap, and `String.length`
  // counts UTF-16 units, so a multi-byte message measured that way is let
  // through well over the limit.
  const chunks = [];
  let bytes = 0;
  let refused = false;
  socket.on("data", (chunk) => {
    if (refused) return;
    bytes += chunk.length;
    if (bytes > MAX_MESSAGE_BYTES) {
      refused = true;
      chunks.length = 0;
      // Answer, then close both directions once the answer is out. Ending alone
      // closes only this side's writes: `allowHalfOpen` keeps the read half open,
      // and a client that opts into half-open can go on streaming for as long as
      // it likes — measured at 2 MiB past a refusal, with every write pushing the
      // idle timeout back. The cap has to be a ceiling on what a connection can
      // cost, not just on what gets parsed. `destroy` is idempotent, and the
      // callback still runs if the write never flushes.
      socket.end(
        JSON.stringify({
          ok: false,
          error: `message too large: over ${MAX_MESSAGE_BYTES} bytes`,
        }),
        () => socket.destroy(),
      );
      return;
    }
    chunks.push(chunk);
  });
  socket.on("end", async () => {
    if (refused) return;
    try {
      const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      if (typeof payload.content !== "string" || !payload.content.trim()) {
        throw new Error("content must be a non-empty string");
      }
      const messageId = typeof payload.message_id === "string"
        ? payload.message_id
        : randomUUID();
      // Validated again on arrival. The field crossed a socket, so what it
      // holds is a claim rather than a fact, and an alias reaches the agent as
      // the name it is told to reply to.
      const inboundAlias =
        typeof payload.sender_alias === "string" && NAME_PATTERN.test(payload.sender_alias)
          ? payload.sender_alias
          : null;
      await mcp.notification({
        method: "notifications/claude/channel",
        params: {
          content: payload.content.trim(),
          meta: {
            sender: "codex",
            sender_kind: "agent",
            // Always a string. Measured on Claude Code 2.1.251: the host
            // validates this field as a string and drops the whole
            // notification on null — after this server has already told the
            // sender `{ok:true}`. So a peer with no usable name arrives as the
            // same reserved key it occupies in the registry, which the alias
            // grammar refuses and the reply tool reads as "nobody in
            // particular".
            sender_alias: inboundAlias ?? UNNAMED_KEY,
            message_id: messageId,
          },
        },
      });
      socket.end(JSON.stringify({ ok: true, message_id: messageId }));
    } catch (error) {
      socket.end(JSON.stringify({ ok: false, error: String(error?.message || error) }));
    }
  });
});

// Every way this process can be asked to stop is wired up here — before the
// claim, before the bind, before anything is externally visible. Registering
// them at the end of the file left a window in which the socket already existed
// and a signal still hit the default disposition: measured at 30 runs out of 30,
// each one exiting under SIGTERM with its socket and its registry claim left
// behind. A session closing at the wrong moment did the same.
let shuttingDown = false;

async function shutdown() {
  // EOF and a signal can arrive together; the second caller must not repeat the
  // work or race the first one's unlink.
  if (shuttingDown) return;
  shuttingDown = true;
  // Close waits for open connections to end. A client holding the socket without
  // sending anything would keep the process alive past its own termination, so
  // they are dropped first.
  for (const socket of openSockets) socket.destroy();
  openSockets.clear();
  await new Promise((resolve) => socketServer.close(resolve));
  // Only ever remove the socket this process created. Unlinking the path
  // unconditionally is what let the first session to close delete the socket a
  // second, still-running session was serving.
  if (owningSocket) {
    try {
      await unlink(socketPath);
    } catch {}
  }
  await releasePeer();
  process.exit(0);
}

// Every signal the wrapper forwards has to land on the same idempotent
// shutdown. SIGHUP was forwarded but not handled here, so the default
// disposition killed the server outright: socket left bound, registry entry
// left claiming a live pid, wrapper exiting 1.
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, shutdown);
}

// Losing the stdio client has to end the process. The Unix server keeps the
// event loop alive on its own, so without this a session that simply closed left
// a server orphaned under PPID 1, still holding its socket and its registry
// entry, with its stdio descriptors pointing at nothing — observed on a real
// machine hours after the session it belonged to had gone.
//
// Bound to stdin rather than to the server: measured, `end` and `close` both
// fire here on EOF while the SDK's `onclose` does not.
process.stdin.on("end", () => { void shutdown(); });
process.stdin.on("close", () => { void shutdown(); });

if (!nameIsUsable) {
  console.error(
    `antiphon: ANTIPHON_NAME=${peerName} is not a usable peer name ` +
    "([a-z0-9][a-z0-9_-]{0,31}); this session can still reply to Codex but " +
    "cannot be reached from it.",
  );
} else if (!(await claimPeer())) {
  // Claim the name and the address before binding. Probe, unlink and listen
  // cannot be one atomic step: two servers that both found the path free would
  // bind it in turn, the second unlinking the first's live socket, and the
  // registry would end up describing a server that is not the one answering.
  // The registry claim is atomic across processes, so exactly one gets here.
  console.error(
    "antiphon: this session did not get the channel; it can still reply to " +
    "Codex but cannot be reached from it. Give each session a unique " +
    "ANTIPHON_NAME to run more than one.",
  );
} else if (await socketIsLive(socketPath)) {
  // An older server from before the registry existed is still serving this
  // path. It is a working peer, so leave it alone and give the claim back.
  await releasePeer();
  console.error(
    `antiphon: another session already serves ${socketPath}; this session will ` +
    "not receive channel events. Give each session a unique ANTIPHON_NAME to " +
    "run both.",
  );
} else if (!(await serveSocket())) {
  // Every failure on the way to a working socket ends here, and every one of
  // them gives the claim back: a record whose socket never came up hands senders
  // an address nothing serves. The MCP direction is untouched throughout, so the
  // session keeps `reply_to_codex` whatever went wrong.
  await releasePeer();
}

// After the chain, not inside a branch: every route through it ends here, so a
// tool call waiting on this cannot be left waiting whichever way startup went.
markIdentitySettled();
