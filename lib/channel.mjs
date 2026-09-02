#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { access, chmod, mkdir, unlink, writeFile } from "node:fs/promises";
import { connect, createServer } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { automaticProofVerdict as sharedProofVerdict, redactPrivate,
         automaticNameFromDigest, classifyEndpoint } from "./identity.mjs";

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
// An explicit or legacy peer's path is actionable: its operator chose the name,
// so `remove it` is a thing they can do and the path says what. An automatic
// peer's path derives from a host session id nobody typed, and its remedy was
// always "restart that session" — printing the route buys nothing and publishes
// a private shape. Errors on this side use this; the readiness line does not,
// because it is neither a refusal nor an error.
const routeIsPrivate = !configuredPeerName && Boolean(automaticIdentity);
const visibleRoute = routeIsPrivate ? "<route>" : socketPath;

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
      "sender_kind=\"agent\" sender_alias=\"...\" message_id=\"...\">: the Codex " +
      "agent's words, never the human user's. Handle the request, then answer " +
      "with reply_to_codex, passing sender_alias back as `to` when it is a name " +
      "rather than the literal `<unnamed>`; a bare reply works only while no " +
      "Codex peer is registered or one positively live automatic peer is the " +
      "only candidate, and is refused otherwise. Its result says queued, never " +
      "delivered — Codex reads its queue at its next turn — and `antiphon " +
      "status` shows the receipt. `<unnamed>` means that peer " +
      "has no name; passing it as `to` is the same as leaving it out. A valid " +
      "Claude ANTIPHON_NAME is this session's configured identity, not proof " +
      "that its return channel is reachable: if startup warned that the channel " +
      "was not acquired, run antiphon doctor and restart the session. " +
      "An `Antiphon delivery notice:` event is a bridge-authored diagnostic with " +
      "no original message content; it does not turn the sender's refusal into " +
      "delivery. That refusal follows one content-free recovery request to the " +
      "named socket — a current listener can restore its own endpoint, so " +
      "the words go only if the registry resolves again; an old or unverified " +
      "listener stays refused; doctor only reports it. If an outgoing label " +
      "carries `reply_to=<unavailable>`, do not reply to that `from` alias: it " +
      "belongs to a different session. " +
      "Without `ANTIPHON_NAME` a session may get an automatic `auto-` peer alias " +
      "— Codex once its first hook proves it live, Claude from a fixed " +
      "Claude probe (the host display name is ignored) — and a census is " +
      "`at least N` since a session before its first hook may be invisible. " +
      "`ANTIPHON_NAME` overrides automatic identity; older peers are never " +
      "guessed into one. One positively live automatic peer is the only " +
      "bare-send case; two or more candidates refuse. The session id, identity " +
      "digest, owner key and socket route stay private — status, doctor, " +
      "labels, refusals and errors expose only the public alias. After a " +
      "rotation the old alias stops resolving and the new one is unreachable " +
      "until a fresh endpoint exists (an MCP reconnect); an owner not proved " +
      "live is counted, never addressed. " +
      "In a visible reply, a Stop marker starting `@codex[:name]` can carry a " +
      "block: make its one-line message exactly `<<TOKEN` (TOKEN matches " +
      "`[A-Z][A-Z0-9_]{0,31}`), put the body on the next lines and close it " +
      "with an exact `TOKEN` line. Blocks do not nest and the closer is not " +
      "fence-aware, so choose a token absent from the body; marker-looking " +
      "lines inside are content; a malformed or unclosed block sends nothing " +
      "from that turn; literal text beginning with `<<` goes in a block body. " +
      "Use `reply_to_codex` for long content: an oversized direct-tool message " +
      "is parked as an attachment, while an oversized Stop-marker block is " +
      "refused and not parked. " +
      "Every compact tool-call line carries a content-bound `tc1` id: the " +
      "`antiphon_retrieve` tool with `id=\"<id>\"` returns that invocation " +
      "only, never the tool result, read-only and cursor-neutral; above 8,000 " +
      "bytes it refuses without truncation and `antiphon retrieve <id>` prints " +
      "it whole. Host retention or `antiphon sources compact` can make an id " +
      "`unavailable`; two copies of one transcript make retrieval `untrusted`.",
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
        "a bare reply refused. The result says queued, never delivered.",
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
      name: "reply_to_claude",
      description:
        "Send a message to another Claude Code session working in this project, " +
        "named with `to` — always: a same-kind message to nobody in particular " +
        "has no meaning. The result says delivered to that session's channel; " +
        "antiphon status shows when its transcript received it.",
      inputSchema: {
        type: "object",
        properties: {
          text: {
            type: "string",
            description: "Message for that Claude session",
          },
          to: {
            type: "string",
            description: "Alias of the Claude peer to send to. Required.",
          },
        },
        required: ["text", "to"],
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
      // Redacted before the cut, like every other refusal on this bridge: a
      // truncation taken first can leave half a session id behind and a
      // whole-shape check would pass over the fragment. This is the Python
      // bridge's own stderr, which names the keys it refused.
      throw new Error("Failed to retrieve invocation: " + redactPrivate(
        String(error?.stderr || error?.message || error).trim(), 500));
    }
  }
  const toolName = request.params.name;
  if (toolName !== "reply_to_codex" && toolName !== "reply_to_claude") {
    throw new Error(`unknown tool: ${toolName}`);
  }
  // The same-kind road: this Claude session to another, always named.
  const toClaude = toolName === "reply_to_claude";
  const peerKind = toClaude ? "Claude" : "Codex";
  const text = request.params.arguments?.text;
  if (typeof text !== "string" || !text.trim()) {
    throw new Error("text must be a non-empty string");
  }
  let to = request.params.arguments?.to;
  if (to !== undefined && to !== null && typeof to !== "string") {
    throw new Error(`to must be a string naming one live ${peerKind} peer`);
  }
  // The reserved key is what an agent reads in `sender_alias` when the peer
  // has no name. Handed back as `to`, it means "nobody in particular" — the
  // bare reply — never a peer called `<unnamed>`, which the alias grammar
  // refuses and the registry can never hold.
  if (to === UNNAMED_KEY) to = undefined;
  if (toClaude && (typeof to !== "string" || !to.trim())) {
    throw new Error("to is required: a reply to another Claude session names its peer");
  }
  // Wait for startup to have decided who this session is and whether its named
  // return route belongs to it, so an early call carries both facts correctly.
  await identitySettled;
  let stdout = "";
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
    // Routing consults the proof on every delivery, and signing must consult
    // the same verdict: otherwise a rotated listener keeps announcing an
    // identity it no longer owns while refusing every message sent to it.
    //
    // `deliveryVerdict`, not the shared function under it — the two must be
    // one predicate or they are two. Reading the raw verdict here left the
    // fail-closed on one side only: a listener whose claim came back without a
    // fingerprint refused everything sent to it and went on signing its
    // replies with the alias it could no longer prove was its own.
    const automaticJoined = !automaticIdentityDigest
      || (automaticIdentityJoined() && deliveryVerdict() === "READY");
    const publishedAlias = automaticJoined ? senderAlias : null;
    const payload = {
      text: text.trim(),
      kind: toClaude ? "claude" : "codex",
      sender_alias: publishedAlias,
      sender_reachable: Boolean(publishedAlias) && senderReachable,
    };
    if (typeof to === "string") payload.to = to;
    execution.child.stdin.end(JSON.stringify(payload));
    ({ stdout } = await execution);
  } catch (error) {
    throw new Error(`Failed to deliver reply to ${peerKind}: ` + redactPrivate(
      String(error?.stderr || error?.message || error).trim(), 500));
  }
  // The words are Python's: it knows whether the row was queued for a
  // registered peer, a proven-live thread or an unproven one, and under
  // which id the receipt will later show. A queue accepting a row is not
  // the peer reading it, so the result never says "delivered".
  let answer = null;
  try { answer = JSON.parse(String(stdout).trim().split("\n").pop()); } catch { answer = null; }
  const outcome = typeof answer?.text === "string" && answer.text
    ? answer.text
    : toClaude
      ? `Delivered to the Claude Code peer '${to}'; run antiphon status to see whether it was received.`
      : (typeof to === "string"
        ? `Queued for Codex peer '${to}'; run antiphon status to see whether it was received.`
        : "Queued for the newest Codex session; run antiphon status to see whether it was received.");
  return { content: [{ type: "text", text: outcome }] };
});

await mcp.connect(new StdioServerTransport());


let owningSocket = false;
let shuttingDown = false;
let registryMutations = Promise.resolve();
// Set once a claim came back unacknowledged: the Python on disk predates
// this listener's fingerprint field. Every later claim would write the old
// record and withdraw it again — a window in which another reader enumerates
// a record nothing governs, and a remedy printed on every reassert — so the
// answer is remembered and later claims refuse before shelling out. Only a
// restart (a new process, a fresh flag) can try again, which is the remedy.
let registryUngoverned = false;

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
  if (subcommand === "register_peer" && registryUngoverned) {
    console.error("antiphon: the registry on disk predates this listener's "
      + "fingerprint field; refusing further claims until it is restarted");
    return false;
  }
  const execution = execFileAsync("python3", [bridgeScript, subcommand], {
    cwd: projectDir,
    timeout: 20_000,
    maxBuffer: 128 * 1024,
  });
  execution.child.stdin.on("error", () => {});
  const payload = {
    kind: "claude", name: peerId, address: socketPath, pid: process.pid,
    // What this listener's in-memory verdict reads. A registry that predates
    // the field refuses this claim; one that honours it says so below, and a
    // registry that says nothing has written a record this listener cannot
    // govern.
    fingerprint_field: "process_birth",
  };
  if (automaticIdentityDigest) {
    payload.identity_digest = automaticIdentityDigest;
    // Initial and reassert carry different rules, and an identical payload
    // leaves Python nothing to tell them apart once an endpoint is pruned.
    payload.mode = mode;
  }
  execution.child.stdin.end(JSON.stringify(payload));
  try {
    const { stdout } = await execution;
    if (subcommand === "register_peer") {
      let answer = null;
      try { answer = JSON.parse(String(stdout).trim()); } catch { answer = null; }
      claimedBirth = typeof answer?.birth === "string" ? answer.birth : null;
      if (automaticIdentityDigest && answer?.fingerprint_field !== "process_birth") {
        // The Python on disk is older than this listener: it wrote the old
        // record and cannot be governed by the verdict this process runs.
        // No authority from this claim, then; withdraw what it wrote inside
        // this same serialised step, and say which of three things happened.
        claimedBirth = null;
        registryUngoverned = true;
        await withdrawUngovernedClaim();
        return false;
      }
    }
    return true;
  } catch (error) {
    if (!quiet) {
      // The registry's own words, and it names the key it refused.
      console.error("antiphon: " + redactPrivate(
        String(error?.stderr || error?.message || error).trim()));
    }
    return false;
  }
}

// The withdrawal after an unacknowledged claim, and then a look — because
// `unregister` swallows an unlink error and treats an owner mismatch as
// nothing to do, and folding absent, torn and unreadable into one `false`
// would let a torn file read as "withdrawn". A withdrawal is announced only
// when the record no longer describes this listener; the other two answers
// name the file the person has to look at. Called from inside the claim's own
// registry step, so nothing else can mutate the registry in between.
async function withdrawUngovernedClaim() {
  await runRegistryCall("unregister_peer", true);
  const file = `.antiphon/peers/claude-${peerId}/endpoint.json`;
  const held = classifyEndpoint(
    join(projectDir, file),
    { pid: process.pid, address: socketPath, name: peerId,
      identityDigest: automaticIdentityDigest });
  const lead = "antiphon: the registry on disk predates this listener's fingerprint field";
  const tail = "reinstall antiphon so both sides match, then reconnect the Claude session";
  console.error(
    held === "absent" || held === "other"
      ? `${lead}; the endpoint it wrote was withdrawn. R${tail.slice(1)}`
      : held === "self"
        ? `${lead}, and the endpoint it wrote could not be withdrawn; remove ${file} by hand, ${tail}`
        : `${lead}, and whether the endpoint it wrote was withdrawn could not be verified; check ${file} by hand, ${tail}`);
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

// The process fingerprint the registry wrote for this listener, remembered
// from the record it published. A pid is half of a process identity — the
// birth is what tells a live process from a recycled number — and this side
// cannot compute a birth, so it keeps the one it caused to be written and
// compares against that. An endpoint whose birth is not this one was not
// published by this listener, whatever pid it names.
let claimedBirth = null;

const claimPeer = (mode) => registryCall("register_peer", false, mode);
const releasePeer = () => registryCall("unregister_peer", true);

const CHANNEL_CONTROL = "antiphon.channel";
const CHANNEL_CONTROL_VERSION = 1;
const CONTROL_NONCE_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

// Whether the endpoint on disk is the one the replying listener published:
// the same four-way reader the withdrawal check uses, asked one question.
// `reply.pid` rather than this process's pid, because the listener being
// asked about is the one that answered the reassert.
function endpointDescribesListener(reply) {
  return classifyEndpoint(
    join(projectDir, ".antiphon", "peers", `claude-${peerId}`, "endpoint.json"),
    { pid: reply.pid, address: socketPath, name: peerId,
      identityDigest: automaticIdentityDigest }) === "self";
}

// The delivery-time check lives in its own module so the parity suite can
// drive the same code the listener runs, rather than a second copy of it.
function automaticProofVerdict() {
  // This process's own pid: the endpoint the verdict reads must be the record
  // this listener published, not one left by a process that is gone.
  return sharedProofVerdict(projectDir, peerId, automaticIdentityDigest,
                            process.pid, claimedBirth);
}

// `null` from the shared verdict means "this contract does not govern that
// record", and what that implies depends on who is asking. A listener with no
// automatic identity is an explicit peer: the answer is the truth about it and
// delivery proceeds. A listener that *holds* an automatic digest and gets
// `null` is being told something else — the endpoint on disk has stopped
// describing this process, because it is explicit-shaped now or carries
// another identity. Reading that as permission emitted a message from a record
// that no longer names the emitter, which is the misdelivery this whole repair
// exists to end. It refuses, and it destroys nothing: `null` is not
// PROVED_STALE, and only PROVED_STALE may retire anything.
// Interpolating a code with a fallback to the error itself reads as "the short
// name, or the whole thing" — and the whole thing is a message that can carry a
// path, a session id or an owner key. A code is a fixed short token and needs
// no redaction; everything else goes through the redactor first.
function errorCode(error) {
  const code = error?.code;
  if (typeof code === "string" && /^[A-Z][A-Z0-9_]*$/.test(code)) return code;
  return redactPrivate(String(error?.message || error), 200);
}

function deliveryVerdict() {
  const verdict = automaticProofVerdict();
  if (!automaticIdentityDigest) return verdict;
  // No authority, no delivery. If the claim did not come back with the
  // fingerprint of the process this endpoint names, this listener cannot tell
  // its own record from one an earlier process left behind — and answering
  // anyway is the fail-open the whole contract is written against.
  if (typeof claimedBirth !== "string") return "UNGOVERNED";
  return verdict === null ? "UNGOVERNED" : verdict;
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
        `antiphon: could not clear ${visibleRoute} (${errorCode(error)}); this ` +
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
    // Close and stop there. Reaching for the path afterwards is the same race
    // retirement had: this branch runs when `listen` or `chmod` failed, so the
    // path is either not ours — another binder took it between the preflight
    // and the listen, which is exactly how `EADDRINUSE` arrives here — or it
    // is ours and `close()` removed it. Unlinking unconditionally deleted the
    // winner's live socket.
    await new Promise((resolve) => socketServer.close(resolve));
    owningSocket = false;
    console.error(
      `antiphon: could not serve ${visibleRoute} (${errorCode(error)}); this ` +
      "session can still reply to Codex but cannot be reached from it.",
    );
    return false;
  }
  // A socket error after this point must not be fatal either: an unhandled
  // 'error' event throws.
  socketServer.on("error", (error) =>
    console.error(`antiphon: channel socket error (${errorCode(error)})`));
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
  // Set before the first await, so a reassert already queued behind this turn
  // of the loop sees it and refuses rather than racing the release below.
  retiring = true;
  try {
    // Stop accepting first, then withdraw. A connection accepted after the
    // release could still ask for a reassert, and the flag above is what keeps
    // that answer honest; closing first keeps the window as small as it can be.
    // Closing the server removes the socket file it bound, and that is the
    // only removal this path may make. An explicit `unlink` afterwards ran
    // once the release had already yielded — long enough for a successor to
    // bind the same path — and deleted the live listener's socket, which is
    // the exact failure `owningSocket` exists to prevent. Ownership is given
    // up here too, so the exit does not reach for the path a second time.
    // Open connections go first, exactly as shutdown does it. Without that,
    // `close()` waits for every client to end and a peer that connected and
    // said nothing holds retirement open for the full idle timeout — a stale
    // listener keeping its endpoint for thirty seconds after it learned it was
    // stale. It also means no reassert can still be in flight to be answered.
    for (const open of openSockets) open.destroy();
    openSockets.clear();
    await new Promise((resolve) => socketServer.close(resolve));
    owningSocket = false;
    await releasePeer();
    console.error(`antiphon: ${peerName} is no longer this session's automatic `
      + "identity; the channel withdrew its endpoint. Reconnect that Claude "
      + "session to be reachable.");
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
        // Two actions share one envelope and one validation. `reassert` asks
        // this listener to republish its endpoint; `identity-retire` tells it
        // the owner's current identity moved. Neither is authenticated — there
        // is no shared secret and no peer identity behind a Unix socket — so
        // the shape is what makes a message legible as its own action, and the
        // decision that follows is taken by re-reading the proof.
        const action = payload.action === "reassert"
          || payload.action === "identity-retire"
          ? payload.action
          : null;
        const valid = payload.version === CHANNEL_CONTROL_VERSION
          && action
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
        // `retiring` as well as `shuttingDown`: retirement releases the
        // endpoint and only then closes the socket, so a reassert that arrived
        // in between would re-create the very record the retirement just
        // withdrew — and it would name a listener that is on its way out.
        // Withdrawal has to be the last registry mutation this process makes.
        if (shuttingDown || retiring) {
          socket.end(JSON.stringify({
            ok: false,
            error: "listener is shutting down",
          }));
          return;
        }
        if (action === "identity-retire") {
          // The control is an optimisation, never the authority. Routing is
          // already safe from the proof, and only the listener's own reading
          // of that proof may destroy anything: a control that retired on
          // request would let anyone who can reach this socket unregister a
          // healthy peer. The answer is flushed before anything is withdrawn.
          // `ok` reports whether this listener acted, not whether the
          // envelope parsed. Answering `ok:true` while changing nothing told
          // the sender its wakeup had been honoured; the contract calls every
          // other verdict a non-destructive *refusal*, and it has to read like
          // one or a caller cannot tell the two apart.
          const verdict = deliveryVerdict();
          const retiring = verdict === "PROVED_STALE";
          socket.end(JSON.stringify({
            ok: retiring,
            control: CHANNEL_CONTROL,
            version: CHANNEL_CONTROL_VERSION,
            action: "identity-retire-ack",
            alias: peerName,
            nonce: payload.nonce,
            verdict,
            pid: process.pid,
          }), () => {
            if (retiring) retireSelf();
          });
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
      const verdict = deliveryVerdict();
      if (verdict !== null && verdict !== "READY") {
        // Classified, never a transport error: the peer that alias named is no
        // longer this session, so there is no such peer to reach. The response
        // is flushed before anything is torn down, and only PROVED_STALE
        // authorises tearing anything down at all.
        socket.end(JSON.stringify({
          ok: false,
          refusal_class: "no-peer",
          // The alias is the public half and the only part of this identity
          // a sender can act on: it is the name that stopped resolving, and
          // without it "this alias" names nothing the sender can look up.
          error: verdict === "PROVED_STALE"
            ? `${peerName} is no longer this session; reconnect that Claude `
              + "session to be reachable"
            : verdict === "UNGOVERNED"
              ? `${peerName}'s endpoint record no longer describes this `
                + "session; restart that Claude session to be reachable"
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
            // Another Claude session says so with `sender_kind: "claude"`;
            // anything else, including the key's absence from every Codex
            // sender and every older Python, is Codex.
            sender: payload.sender_kind === "claude" ? "claude" : "codex",
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
      // Whatever went wrong on the way to an emission, the sender reads this.
      socket.end(JSON.stringify({
        ok: false,
        error: redactPrivate(String(error?.message || error)),
      }));
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
  // Closing removes the socket file this server bound, and an explicit unlink
  // after the await is a second removal with a gap in front of it — long
  // enough for a successor to bind the same path, which is the failure
  // `owningSocket` was added to prevent, reached through the guard itself.
  // Ownership ends where the server does.
  await new Promise((resolve) => socketServer.close(resolve));
  owningSocket = false;
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
        `serves ${visibleRoute} and reasserted its endpoint. Give each session a ` +
        "unique ANTIPHON_NAME to run both."
      : `antiphon: this session did not get the channel; something already ` +
        `serves ${visibleRoute} but did not prove it is a current Antiphon listener. ` +
        "Restart the older Claude session, or give each session a unique " +
        "ANTIPHON_NAME to run both.",
  );
} else if (!(await claimPeer("initial"))) {
  // Claim the name and the address before binding. Probe, unlink and listen
  // cannot be one atomic step: two servers that both found the path free would
  // bind it in turn, the second unlinking the first's live socket, and the
  // registry would end up describing a server that is not the one answering.
  // The registry claim is atomic across processes, so exactly one gets here.
  // One remedy at a time: when the claim was refused over the registry on
  // disk, the withdrawal notice above already named the repair, and sending
  // the operator after a unique name would contradict it.
  console.error(
    "antiphon: this session did not get the channel; it can still reply to " +
    "Codex but cannot be reached from it." + (registryUngoverned ? ""
      : " Give each session a unique ANTIPHON_NAME to run more than one."),
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
        `serves ${visibleRoute} and reasserted its endpoint. Give each session a ` +
        "unique ANTIPHON_NAME to run both."
      : `antiphon: another session already serves ${visibleRoute}; this session ` +
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
