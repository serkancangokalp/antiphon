#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { access, chmod, mkdir, unlink, writeFile } from "node:fs/promises";
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
const bridgeScript = join(here, "antiphon.py");
const configuredPeerName = (process.env.ANTIPHON_NAME || "").trim().toLowerCase();
// Validated before anything binds: an invalid name would otherwise open a socket
// that registration then refuses, leaving a live channel nobody can find.
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const IDENTITY_DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const configuredNameIsUsable = !configuredPeerName || NAME_PATTERN.test(configuredPeerName);

function automaticNameFromDigest(digest) {
  const alphabet = "abcdefghijklmnopqrstuvwxyz234567";
  const bytes = Buffer.from(digest.slice(0, 32), "hex");
  let bits = 0;
  let value = 0;
  let encoded = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      encoded += alphabet[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
    value &= bits ? (1 << bits) - 1 : 0;
  }
  if (bits) encoded += alphabet[(value << (5 - bits)) & 31];
  return `auto-${encoded}`;
}

async function probeAutomaticIdentity() {
  if (configuredPeerName || !configuredNameIsUsable) return null;
  try {
    const { stdout } = await execFileAsync("python3", [bridgeScript, "claude_identity"], {
      cwd: projectDir,
      timeout: 2_000,
      maxBuffer: 32 * 1024,
    });
    const identity = JSON.parse(stdout);
    if (typeof identity?.alias !== "string"
        || !NAME_PATTERN.test(identity.alias)
        || typeof identity?.identity_digest !== "string"
        || !IDENTITY_DIGEST_PATTERN.test(identity.identity_digest)
        || identity.alias !== automaticNameFromDigest(identity.identity_digest)) {
      return null;
    }
    return identity;
  } catch {
    return null;
  }
}

const automaticIdentity = await probeAutomaticIdentity();
const automaticIdentityDigest = automaticIdentity?.identity_digest || null;
const peerName = configuredPeerName || automaticIdentity?.alias || "";
const nameIsUsable = configuredNameIsUsable && (!peerName || NAME_PATTERN.test(peerName));

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
// The name says who spoke. This separate bit says whether a reply addressed to
// that name returns to this process rather than to another process that owns
// the channel. It is published only after the claim and socket both succeed.
let senderReachable = false;

// Resolved once the startup chain has settled both identity and return-channel
// reachability — winner, loser, unnamed or refused. The reply tool waits before
// signing a message. Answering earlier would label the first message's return
// route unavailable merely because the MCP handshake finishes before the
// registry claim does.
let markIdentitySettled;
const identitySettled = new Promise((resolve) => { markIdentitySettled = resolve; });
// Hashed with the name, never appended: macOS caps a socket path near 104 bytes
// and TMPDIR already spends much of it. An empty name reproduces the
// pre-multi-peer key exactly, so an unnamed session keeps the socket it has.
const socketSeed = peerName ? `${projectDir}\0${peerName}` : projectDir;
const projectKey = createHash("sha256").update(socketSeed).digest("hex").slice(0, 20);
const socketPath = join(process.env.TMPDIR || "/tmp", `antiphon-channel-${projectKey}.sock`);

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
      "Leaving `to` out works where no Codex peer is registered, or where exactly " +
      "one positively live automatic peer is the only candidate. A bare reply is " +
      "refused when an explicit named peer or multiple positive candidates are live, " +
      "because unnamed sessions before their first hook cannot be ruled out. A " +
      "sender_alias of `<unnamed>` means that peer has no name: it cannot be " +
      "addressed by name, so a reply reaches it only in that bare case — " +
      "passing `<unnamed>` as `to` is the same as leaving it out. " +
      "Without `ANTIPHON_NAME`, Antiphon may derive an automatic `auto-` peer alias " +
      "from a canonical host session UUID. Codex publishes one only after its first " +
      "hook records that UUID and a writer lock positively proves the session live. " +
      "Every census remains `at least N` because sessions before their first hook " +
      "may be invisible. " +
      "Claude accepts one only from a fixed Claude probe that finds exactly one " +
      "interactive record with this session's CLI-root pid and exact project cwd; " +
      "the host display name is ignored, and the Claude hook must join the same " +
      "endpoint, owner and identity. Probe or hook failure stays `<unnamed>`. " +
      "`ANTIPHON_NAME` overrides automatic identity. One positively live automatic " +
      "peer can be addressed by alias and is the only automatic case a bare send may " +
      "choose; two or more positively live candidates make a bare send refused. " +
      "Older or mixed-version peers are never guessed into automatic identity. The " +
      "full host session id and identity digest stay private; status, doctor, labels " +
      "and refusals expose only the public alias. A valid " +
      "Claude ANTIPHON_NAME is this session's configured identity, not proof " +
      "that its named return channel is reachable. If startup warned that the " +
      "channel was not acquired, run antiphon doctor and restart the session. " +
      "If an outgoing label carries `reply_to=<unavailable>`, do not reply to " +
      "its `from` alias: that channel belongs to a different session. " +
      "An `Antiphon delivery notice:` event is a bridge-authored diagnostic: " +
      "it carries no original message content and does not turn the sender's " +
      "refusal into delivery. Before that refusal, Antiphon makes one " +
      "content-free recovery request to this session's exact named socket. A " +
      "current listener can restore its own endpoint; the original words arrive " +
      "only if the registry resolves again. An old or unverified listener stays " +
      "refused and may need a restart. Doctor only reports this state; it never " +
      "performs the recovery. In a visible reply, a Stop marker starting " +
      "`@codex[:name]` can carry a block: make its one-line message exactly " +
      "`<<TOKEN`, where TOKEN matches `[A-Z][A-Z0-9_]{0,31}`, put the body on " +
      "following lines, and use an exact `TOKEN` line to close it. Blocks do not " +
      "nest and the closer is not Markdown-fence-aware, so choose a token absent " +
      "from the body. Marker-looking lines inside the body are content. A " +
      "malformed or unclosed block sends nothing from that turn. To send literal " +
      "text beginning with `<<`, put it inside a block body. Use " +
      "`reply_to_codex` for long content: an oversized direct-tool message can " +
      "be parked as an attachment, while an oversized Stop-marker block is " +
      "refused and not parked. Paging writes " +
      "`<side>_pages_v4` beside the " +
      "preserved v3 sibling. During adoption, at most the v3 frontier's last " +
      "record repeats while a content anchor is established. Live and unknown " +
      "sources stay in the active lane; only a current process fingerprint can " +
      "prove a source dead, and mixed backlog alternates whole pages between " +
      "active and dead after successful delivery. Candidate retirement is never " +
      "a hook side effect: `antiphon sources compact` explicitly retires only " +
      "aged, gone sources every relevant v4 reader proves consumed. Hooks never " +
      "retire candidates. Every compact tool-call entry carries a 22-character " +
      "opaque, content-bound `tc1` id. Call the `antiphon_retrieve` tool with " +
      "`id=\"<id>\"` for " +
      "the complete invocation only, never the tool result. Retrieval is read-only " +
      "and cursor-neutral; it reports `invalid-id`, `unavailable`, `ambiguous` or " +
      "`untrusted` without inventing content. An MCP value above 8,000 UTF-8 bytes " +
      "is refused without truncation; run `antiphon retrieve <id>` for the full " +
      "invocation. Host retention or `antiphon sources compact` can make an old id " +
      "unavailable. Two copies of one transcript identity inside a host discovery " +
      "root make retrieval untrusted; backups outside those roots do not affect it. " +
      "There is no persistent invocation index or tombstone, so changed, expired " +
      "and never-existed ids all honestly collapse to `unavailable`; binding " +
      "invocation content into the id prevents changed bytes from being returned " +
      "under the old id.",
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
        "to whenever you know which peer you mean. Leaving it out works only " +
        "where no Codex peer is registered or one positively live automatic peer is " +
        "the only candidate; an explicit named peer or multiple candidates make " +
        "a bare reply refused.",
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
    {
      name: "antiphon_retrieve",
      description:
        "Read-only, write-free retrieval of the complete tool invocation named by " +
        "a tc1 id from the original project transcript. It returns the invocation only, " +
        "never the tool result, and does not move a cursor or write bridge state. " +
        "MCP results larger than 8000 UTF-8 bytes are refused without truncation; " +
        "use `antiphon retrieve <id>` for the full value.",
      inputSchema: {
        type: "object",
        properties: {
          id: {
            type: "string",
            description: "The 22-character tc1 invocation id shown on a tool line.",
          },
        },
        required: ["id"],
      },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name === "antiphon_retrieve") {
    const publicId = request.params.arguments?.id;
    try {
      // Fixed argv, no shell and no transcript content on stdin. Python owns
      // discovery and the byte bound, so both MCP surfaces share one result
      // contract instead of implementing subtly different scans.
      const { stdout } = await execFileAsync(
        "python3",
        [bridgeScript, "retrieve_mcp", typeof publicId === "string" ? publicId : ""],
        {
          cwd: projectDir,
          timeout: 20_000,
          maxBuffer: 32 * 1024,
        },
      );
      const result = JSON.parse(stdout);
      if (!result || !Array.isArray(result.content)) {
        throw new Error("retrieval bridge returned an invalid result");
      }
      return result;
    } catch (error) {
      const detail = String(error?.stderr || error?.message || error).trim();
      throw new Error(`Failed to retrieve invocation: ${detail.slice(0, 500)}`);
    }
  }
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
  // Wait for startup to have decided who this session is and whether its named
  // return route belongs to it, so an early call carries both facts correctly.
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
    const automaticJoined = !automaticIdentityDigest || automaticIdentityJoined();
    const publishedAlias = automaticJoined ? senderAlias : null;
    const payload = {
      text: text.trim(),
      sender_alias: publishedAlias,
      sender_reachable: Boolean(publishedAlias) && senderReachable,
    };
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
let shuttingDown = false;
let registryMutations = Promise.resolve();

async function waitAtTestSocketGate(point) {
  if (process.env.NODE_ENV !== "test"
      || process.env.ANTIPHON_TEST_SOCKET_GATE !== point) return;
  const entered = process.env.ANTIPHON_TEST_SOCKET_GATE_ENTERED;
  const release = process.env.ANTIPHON_TEST_SOCKET_GATE_RELEASE;
  if (!entered || !release) return;
  await writeFile(entered, "");
  while (true) {
    try {
      await access(release);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  }
}

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
async function runRegistryCall(subcommand, quiet = false, mode = null) {
  const execution = execFileAsync("python3", [bridgeScript, subcommand], {
    cwd: projectDir,
    timeout: 20_000,
    maxBuffer: 128 * 1024,
  });
  execution.child.stdin.on("error", () => {});
  const payload = {
    kind: "claude", name: peerId, address: socketPath, pid: process.pid,
  };
  if (automaticIdentityDigest) {
    payload.identity_digest = automaticIdentityDigest;
    // Initial and reassert carry different rules, and an identical payload
    // leaves Python nothing to tell them apart once an endpoint is pruned.
    payload.mode = mode;
  }
  execution.child.stdin.end(JSON.stringify(payload));
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

function registryCall(subcommand, quiet = false, mode = null) {
  // Registration and release describe one process lifetime and must therefore
  // have one order too. In particular, shutdown's unregister must run after
  // every claim that was already in flight; otherwise a delayed Python child
  // can recreate this process's endpoint after Node has exited.
  registryMutations = registryMutations.then(async () => {
    if (subcommand === "register_peer" && shuttingDown) return false;
    const result = await runRegistryCall(subcommand, quiet, mode);
    // A claim that crossed the shutdown boundary is real registry state, but
    // it is no longer usable by startup or a control response. The final
    // unregister already queued by shutdown will remove it.
    return subcommand === "register_peer" && shuttingDown ? false : result;
  });
  return registryMutations;
}

const claimPeer = (mode) => registryCall("register_peer", false, mode);
const releasePeer = () => registryCall("unregister_peer", true);

const CHANNEL_CONTROL = "antiphon.channel";
const CHANNEL_CONTROL_VERSION = 1;
const CONTROL_NONCE_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

function endpointDescribesListener(reply) {
  try {
    const record = JSON.parse(readFileSync(
      join(projectDir, ".antiphon", "peers", `claude-${peerId}`, "endpoint.json"),
      "utf8",
    ));
    const identityMatches = automaticIdentityDigest
      ? record?.automatic === true
        && record?.identity_digest === automaticIdentityDigest
      : record?.automatic !== true
        && !Object.hasOwn(record || {}, "identity_digest");
    return record?.kind === "claude"
      && record?.name === peerId
      && record?.address === socketPath
      && record?.pid === reply.pid
      && identityMatches;
  } catch {
    return false;
  }
}

// The delivery-time check. The retire control is best effort, so a sender can
// resolve this alias while it is current, the hook can then move the proof, and
// the sender can connect afterwards. The listener's own read is the only point
// that closes that window, so it is taken on every inbound delivery before
// anything is emitted.
//
// Narrow on purpose: only what delivery needs. Task 6 extends it to the whole
// readiness predicate and proves both languages agree.
function automaticProofVerdict() {
  if (!automaticIdentityDigest) return null;      // ungoverned: explicit peer
  const root = join(projectDir, ".antiphon", "peers", `claude-${peerId}`);
  let owner;
  try {
    const endpoint = JSON.parse(readFileSync(join(root, "endpoint.json"), "utf8"));
    owner = endpoint?.owner;
    if (typeof owner !== "string" || !owner) return "UNREADY";
  } catch {
    return "UNREADY";
  }
  const digest = createHash("sha256").update(owner).digest("hex");
  const path = join(projectDir, ".antiphon", "identity", "claude", `${digest}.json`);
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch (error) {
    // Absent and unreadable are different facts and only one of them is a
    // reason to do nothing but wait.
    return error?.code === "ENOENT" ? "UNREADY" : "UNKNOWN";
  }
  let proof;
  try {
    proof = JSON.parse(raw);
  } catch {
    return "STRUCTURAL_INVALID";
  }
  if (proof?.kind !== "claude" || proof?.owner_digest !== digest
      || typeof proof?.identity_digest !== "string") {
    return "STRUCTURAL_INVALID";
  }
  return proof.identity_digest === automaticIdentityDigest
    ? "READY"
    : "PROVED_STALE";
}

function automaticIdentityJoined() {
  if (!automaticIdentityDigest || !peerName) return false;
  const root = join(projectDir, ".antiphon", "peers", `claude-${peerId}`);
  try {
    const endpoint = JSON.parse(readFileSync(join(root, "endpoint.json"), "utf8"));
    const session = JSON.parse(readFileSync(join(root, "session.json"), "utf8"));
    const sessionId = session?.session_id;
    const canonicalSession = typeof sessionId === "string"
      && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(sessionId);
    return endpoint?.kind === "claude"
      && endpoint?.name === peerId
      && endpoint?.address === socketPath
      && endpoint?.pid === process.pid
      && endpoint?.automatic === true
      && endpoint?.identity_digest === automaticIdentityDigest
      && typeof endpoint?.owner === "string"
      && endpoint.owner
      && session?.kind === "claude"
      && session?.name === peerId
      && session?.owner === endpoint.owner
      && session?.automatic === true
      && session?.identity_digest === automaticIdentityDigest
      && canonicalSession
      && createHash("sha256").update(sessionId).digest("hex") === automaticIdentityDigest;
  } catch {
    return false;
  }
}

async function requestListenerReassert(path, alias) {
  const nonce = randomUUID();
  const request = JSON.stringify({
    control: CHANNEL_CONTROL,
    version: CHANNEL_CONTROL_VERSION,
    action: "reassert",
    alias,
    nonce,
  });
  const reply = await new Promise((resolve) => {
    const client = connect(path);
    let response = "";
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      client.destroy();
      resolve(value);
    };
    client.setEncoding("utf8");
    client.setTimeout(1_000, () => finish(null));
    client.on("connect", () => client.end(request));
    client.on("data", (chunk) => {
      response += chunk;
      if (Buffer.byteLength(response) > 64 * 1024) finish(null);
    });
    client.on("end", () => {
      try {
        finish(JSON.parse(response));
      } catch {
        finish(null);
      }
    });
    client.on("error", () => finish(null));
  });
  if (!(reply?.ok === true
        && reply.control === CHANNEL_CONTROL
        && reply.version === CHANNEL_CONTROL_VERSION
        && reply.action === "reasserted"
        && reply.alias === alias
        && reply.nonce === nonce
        && Number.isSafeInteger(reply.pid)
        && reply.pid > 0)) {
    return false;
  }
  return endpointDescribesListener(reply);
}

async function serveSocket() {
  // Nothing in here may throw past this function. It runs at the top level of a
  // module, where an uncaught error exits the process — and losing the process
  // loses `reply_to_codex` too, over a socket that was only ever the other half
  // of the bridge.
  if (shuttingDown) return false;
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
  if (shuttingDown) return false;
  try {
    await new Promise((resolve, reject) => {
      const onError = (error) => {
        socketServer.off("listening", onListening);
        reject(error);
      };
      const onListening = () => {
        socketServer.off("error", onError);
        // Ownership starts when the path becomes externally visible, before
        // chmod or any other await gives shutdown a chance to inspect it.
        owningSocket = true;
        resolve();
      };
      socketServer.once("error", onError);
      socketServer.once("listening", onListening);
      socketServer.listen(socketPath);
    });
    if (shuttingDown) return false;
    await chmod(socketPath, 0o600);
    if (shuttingDown) return false;
  } catch (error) {
    await new Promise((resolve) => socketServer.close(resolve));
    try {
      await unlink(socketPath);
      owningSocket = false;
    } catch {}
    console.error(
      `antiphon: could not serve ${socketPath} (${error?.code || error}); this ` +
      "session can still reply to Codex but cannot be reached from it.",
    );
    return false;
  }
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

let retiring = false;

// Only the process serving this endpoint may withdraw it, and only when a valid
// current proof shows the identity moved. An explicit peer holding the same
// alias, or an automatic one that is merely unready, is never touched: the
// alias grammar allows `auto-...` as a configured name, so acting on the
// address alone could destroy a peer this listener has no claim over.
async function retireSelf() {
  if (retiring) return;
  retiring = true;
  try {
    await releasePeer();
    socketServer.close();
    await unlink(socketPath).catch(() => {});
    console.error("antiphon: this session's automatic identity moved; "
      + "the channel withdrew its endpoint. Reconnect to be reachable.");
  } catch {
    // Routing is already safe from the proof; cleanup is best effort.
  }
}

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
      if (payload?.control === CHANNEL_CONTROL) {
        const valid = payload.version === CHANNEL_CONTROL_VERSION
          && payload.action === "reassert"
          && peerName
          && payload.alias === peerName
          && typeof payload.nonce === "string"
          && CONTROL_NONCE_PATTERN.test(payload.nonce)
          && !Object.hasOwn(payload, "content");
        if (!valid) {
          socket.end(JSON.stringify({
            ok: false,
            error: "invalid Antiphon channel control request",
          }));
          return;
        }
        if (shuttingDown) {
          socket.end(JSON.stringify({
            ok: false,
            error: "listener is shutting down",
          }));
          return;
        }
        if (!(await claimPeer("reassert"))) {
          socket.end(JSON.stringify({
            ok: false,
            error: "listener could not reassert its endpoint",
          }));
          return;
        }
        socket.end(JSON.stringify({
          ok: true,
          control: CHANNEL_CONTROL,
          version: CHANNEL_CONTROL_VERSION,
          action: "reasserted",
          alias: peerName,
          nonce: payload.nonce,
          pid: process.pid,
        }));
        return;
      }
      if (typeof payload.content !== "string" || !payload.content.trim()) {
        throw new Error("content must be a non-empty string");
      }
      const verdict = automaticProofVerdict();
      if (verdict !== null && verdict !== "READY") {
        // Classified, never a transport error: the peer that alias named is no
        // longer this session, so there is no such peer to reach. The response
        // is flushed before anything is torn down, and only PROVED_STALE
        // authorises tearing anything down at all.
        socket.end(JSON.stringify({
          ok: false,
          refusal_class: "no-peer",
          error: verdict === "PROVED_STALE"
            ? "this alias is no longer this session; reconnect to be reachable"
            : "this session's automatic identity is not established yet",
        }), () => {
          if (verdict === "PROVED_STALE") retireSelf();
        });
        return;
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
  // Startup may already have claimed the registry while it is still probing,
  // unlinking or binding the socket. Let it observe `shuttingDown` and settle
  // before deciding whether a socket exists for this process to close. Without
  // this barrier startup can bind after cleanup has skipped an unowned path.
  await identitySettled;
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
  // This final PID-guarded unregister is queued behind every claim that began
  // before shutdown. New claims are refused above, so no registry mutation can
  // recreate the endpoint after this one completes.
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

const namedSocketAlreadyLive = Boolean(
  nameIsUsable && peerName && await socketIsLive(socketPath));

if (!nameIsUsable) {
  console.error(
    "antiphon: ANTIPHON_NAME is not a usable peer name " +
    "([a-z0-9][a-z0-9_-]{0,31}); this session can still reply to Codex but " +
    "cannot be reached from it.",
  );
} else if (namedSocketAlreadyLive) {
  const recovered = await requestListenerReassert(socketPath, peerName);
  console.error(
    recovered
      ? `antiphon: this session did not get the channel; another session already ` +
        `serves ${socketPath} and reasserted its endpoint. Give each session a ` +
        "unique ANTIPHON_NAME to run both."
      : `antiphon: this session did not get the channel; something already ` +
        `serves ${socketPath} but did not prove it is a current Antiphon listener. ` +
        "Restart the older Claude session, or give each session a unique " +
        "ANTIPHON_NAME to run both.",
  );
} else if (!(await claimPeer("initial"))) {
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
} else if (await waitAtTestSocketGate("after-claim") || shuttingDown) {
  // Shutdown's final unregister is still the last registry mutation. This
  // earlier release lets the startup lifecycle settle without publishing a
  // channel it will never serve.
  await releasePeer();
} else if (await socketIsLive(socketPath)) {
  // Another listener won the bind race after our preflight. Give our claim
  // back before asking a named listener to publish its own pid: it cannot take
  // the record while ours occupies it, and we must never write one for it.
  await releasePeer();
  const recovered = Boolean(peerName)
    && await requestListenerReassert(socketPath, peerName);
  console.error(
    recovered
      ? `antiphon: this session did not get the channel; another session already ` +
        `serves ${socketPath} and reasserted its endpoint. Give each session a ` +
        "unique ANTIPHON_NAME to run both."
      : `antiphon: another session already serves ${socketPath}; this session ` +
        "will not receive channel events. Give each session a unique " +
        "ANTIPHON_NAME to run both.",
  );
} else if (!(await serveSocket())) {
  // Every failure on the way to a working socket ends here, and every one of
  // them gives the claim back: a record whose socket never came up hands senders
  // an address nothing serves. The MCP direction is untouched throughout, so the
  // session keeps `reply_to_codex` whatever went wrong.
  await releasePeer();
} else {
  senderReachable = true;
}

// After the chain, not inside a branch: every route through it ends here, so a
// tool call waiting on this cannot be left waiting whichever way startup went.
markIdentitySettled();
