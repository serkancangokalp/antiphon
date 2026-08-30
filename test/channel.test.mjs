import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { Socket, connect } from "node:net";
import { once } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// The socket name derives from the project directory. Using process.cwd() here
// would mean stealing the socket of a live session running in the repo dir: the
// server unlinks and takes it over on startup, breaking the bridge when the test
// finishes. The test's own temp directory gives it a unique key instead, so
// `npm test` never touches a live session.
const projectDir = await mkdtemp(join(tmpdir(), "antiphon-test-"));
const projectKey = createHash("sha256").update(projectDir).digest("hex").slice(0, 20);
const socketPath = join(process.env.TMPDIR || "/tmp", `antiphon-channel-${projectKey}.sock`);
const transport = new StdioClientTransport({
  command: "node",
  args: ["lib/channel.mjs"],
  env: { ...process.env, ANTIPHON_CWD: projectDir },
  stderr: "inherit",
});
const client = new Client({ name: "antiphon-test", version: "1.0.0" });
let resolveNotification;
const notificationReceived = new Promise((resolve) => {
  resolveNotification = resolve;
});

function sendToSocket(payload) {
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const socket = connect(socketPath);
      let reply = "";
      socket.setEncoding("utf8");
      socket.on("connect", () => socket.end(JSON.stringify(payload)));
      socket.on("data", (chunk) => { reply += chunk; });
      socket.on("end", () => resolve(JSON.parse(reply)));
      socket.on("error", (error) => {
        socket.destroy();
        if (error.code === "ENOENT" || error.code === "ECONNREFUSED") {
          setTimeout(attempt, 20);
        } else {
          reject(error);
        }
      });
    };
    attempt();
  });
}

// ---- one socket per peer -------------------------------------------------
// Two sessions in one project used to share a socket path. The second bound
// over the first, and then whichever exited first deleted the survivor's
// socket, leaving a live session that nothing could reach and no error anywhere.

function socketFor(dir, name) {
  const seed = name ? `${dir}\0${name}` : dir;
  const key = createHash("sha256").update(seed).digest("hex").slice(0, 20);
  return join(process.env.TMPDIR || "/tmp", `antiphon-channel-${key}.sock`);
}

function spawnChannel(dir, name) {
  const env = { ...process.env, ANTIPHON_CWD: dir };
  if (name) env.ANTIPHON_NAME = name;
  else delete env.ANTIPHON_NAME;
  const child = spawn("node", ["lib/channel.mjs"], { env, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  return { child, socketPath: socketFor(dir, name), stderr: () => stderr };
}

// Every test removes exactly the socket it created and nothing else. Killing
// the child and deleting only the project directory leaves the socket in the
// shared temp directory: 163 of them had piled up before this was noticed.
async function cleanUp(session, dir) {
  session.child.kill("SIGTERM");
  await Promise.race([
    once(session.child, "exit"),
    new Promise((resolve) => setTimeout(resolve, 2_000)),
  ]);
  if (session.child.exitCode === null) {
    session.child.kill("SIGKILL");
    await Promise.race([
      once(session.child, "exit"),
      new Promise((resolve) => setTimeout(resolve, 2_000)),
    ]);
  }
  await rm(session.socketPath, { force: true });
  await rm(dir, { recursive: true, force: true });
}

async function waitFor(predicate, timeoutMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return false;
}

async function twoNamedPeersKeepSeparateSockets() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-peers-"));
  const ui = spawnChannel(dir, "ui");
  const api = spawnChannel(dir, "api");
  try {
    assert.notEqual(ui.socketPath, api.socketPath, "named peers must not share a path");
    assert.ok(await waitFor(() => existsSync(ui.socketPath)), `ui socket: ${ui.stderr()}`);
    assert.ok(await waitFor(() => existsSync(api.socketPath)), `api socket: ${api.stderr()}`);

    ui.child.kill("SIGTERM");
    await once(ui.child, "exit");
    assert.ok(existsSync(api.socketPath),
      "closing one peer must not remove another peer's socket");
  } finally {
    await cleanUp(ui, dir);
    await cleanUp(api, dir);
  }
}

function registeredPeers(dir) {
  const root = join(dir, ".antiphon", "peers");
  if (!existsSync(root)) return [];
  return readdirSync(root)
    .map((entry) => join(root, entry, "endpoint.json"))
    .filter((path) => existsSync(path))
    .map((path) => JSON.parse(readFileSync(path, "utf8")));
}

async function onlyOneUnnamedSessionGetsTheChannel(startTogether) {
  // Started together, both sessions can see the socket path free at the same
  // moment. Nothing after that point may let the loser bind: it would unlink
  // the winner's live socket, and both would register under different automatic
  // names carrying the same address, so a message addressed to either would
  // reach whichever actually held it. The registry claim is atomic, so exactly
  // one gets through however the two interleave.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-unnamed-"));
  const first = spawnChannel(dir, "");
  if (!startTogether) {
    assert.ok(await waitFor(() => existsSync(first.socketPath)), first.stderr());
  }
  const second = spawnChannel(dir, "");
  try {
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1
      && /cannot be reached|already serves/.test(first.stderr() + second.stderr())),
      `expected exactly one peer and one refusal:\n${first.stderr()}\n${second.stderr()}`);

    const peers = registeredPeers(dir);
    assert.equal(peers.length, 1, "two sessions must not both hold the address");
    assert.ok(existsSync(peers[0].address), "the registered address must be served");
    assert.match(first.stderr() + second.stderr(), /ANTIPHON_NAME/,
      "the refusal must say how to run both");

    // The refused session must leave the socket alone on the way out too:
    // unlinking the path unconditionally in shutdown is what let a session that
    // never owned the socket delete it for the one that did.
    const loser = /cannot be reached|already serves/.test(second.stderr())
      ? second : first;
    loser.child.kill("SIGTERM");
    await once(loser.child, "exit");
    assert.ok(existsSync(peers[0].address),
      "a session that never bound the socket must not remove it when it exits");
    assert.equal(registeredPeers(dir).length, 1,
      "the refused session must not have left a claim behind");
  } finally {
    await cleanUp(first, dir);
    await cleanUp(second, dir);
  }
}

await twoNamedPeersKeepSeparateSockets();
async function aSocketPathItCannotClearDoesNotKillTheSession() {
  // A directory sitting on the socket path makes `unlink` fail with EPERM. That
  // rejection used to reach the top level of the module, where an uncaught error
  // exits the process — taking `reply_to_codex` down with it over a socket that
  // was only ever the other half of the bridge, and leaving the claim behind.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-blocked-"));
  const blocked = socketFor(dir, "");
  mkdirSync(blocked, { recursive: true });
  const session = spawnChannel(dir, "");
  try {
    assert.ok(await waitFor(() => /could not clear|could not serve/.test(session.stderr())),
      `expected a refusal, got: ${session.stderr()}`);
    assert.match(session.stderr(), /can still reply to Codex/,
      "the session must say the reply direction survives");
    assert.equal(session.child.exitCode, null, "the session must stay alive");
    // The warning is printed before the claim is handed back, so wait for the
    // release rather than assuming it happened by the time the message appeared.
    assert.ok(await waitFor(() => registeredPeers(dir).length === 0),
      "a claim that could not be honoured must be given back");
  } finally {
    await rm(blocked, { recursive: true, force: true });
    await cleanUp(session, dir);
  }
}

function sendTo(path, payload) {
  // Resolves with whatever came back, including nothing. A server that drops the
  // connection instead of answering gives the client an EPIPE or ECONNRESET, and
  // rejecting on that would fail the test with a transport error rather than
  // with the assertion that explains what actually broke.
  return new Promise((resolve) => {
    const socket = connect(path);
    let reply = "";
    // Bounded, so a server that answers nothing fails the assertion instead of
    // hanging the whole suite.
    const giveUp = setTimeout(() => { socket.destroy(); resolve(reply); }, 5_000);
    const settle = () => { clearTimeout(giveUp); resolve(reply); };
    socket.setEncoding("utf8");
    socket.on("connect", () => socket.end(payload));
    socket.on("data", (chunk) => { reply += chunk; });
    socket.on("close", settle);
    socket.on("error", settle);
  });
}

async function anOversizedMessageIsRefusedWithoutKillingTheSession() {
  // `socket.destroy(new Error(...))` emits 'error' on the client socket, and
  // with no listener that took the whole process down: one 200 KiB message
  // cost the registry entry, the socket and `reply_to_codex` together.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-oversize-"));
  const session = spawnChannel(dir, "");
  try {
    assert.ok(await waitFor(() => existsSync(session.socketPath)), session.stderr());

    const refusal = await sendTo(session.socketPath,
      JSON.stringify({ content: "x".repeat(200 * 1024) }));
    assert.match(refusal, /too large/, `expected a refusal, got: ${refusal}`);
    assert.equal(session.child.exitCode, null, "the session must stay alive");
    assert.ok(existsSync(session.socketPath), "the socket must survive");
    assert.equal(registeredPeers(dir).length, 1, "the registry entry must survive");

    // And the channel still works afterwards, which is the point of surviving.
    const ack = await sendTo(session.socketPath,
      JSON.stringify({ content: "after the refusal", message_id: "m-after" }));
    assert.deepEqual(JSON.parse(ack), { ok: true, message_id: "m-after" });
  } finally {
    await cleanUp(session, dir);
  }
}

async function aStalledClientDoesNotBlockShutdown() {
  // `server.close()` waits for open connections to end. A client that connects
  // and never speaks would hold the process past its own termination, leaving
  // its socket and its registry entry behind.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-stalled-"));
  const session = spawnChannel(dir, "");
  assert.ok(await waitFor(() => existsSync(session.socketPath)), session.stderr());
  const idle = connect(session.socketPath);
  idle.on("error", () => {});
  try {
    await once(idle, "connect");

    session.child.kill("SIGTERM");
    const exited = await Promise.race([
      once(session.child, "exit").then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), 5_000)),
    ]);
    assert.ok(exited, "SIGTERM must not hang on a client that sent nothing");
    assert.ok(!existsSync(session.socketPath), "it must remove its own socket");
    assert.deepEqual(registeredPeers(dir), [], "it must release its own claim");
  } finally {
    idle.destroy();
    await cleanUp(session, dir);
  }
}

async function aRefusedClientCannotKeepStreaming() {
  // Ending the response closes only the server's write half. With
  // `allowHalfOpen` the read half stays open, so a client that opts into
  // half-open can go on writing after it has been refused — measured at 2 MiB,
  // with every write pushing the idle timeout back. The byte cap has to bound
  // what a connection can cost, not only what gets parsed.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-stream-"));
  const session = spawnChannel(dir, "");
  const client = new Socket({ allowHalfOpen: true });
  let written = 0;
  try {
    assert.ok(await waitFor(() => existsSync(session.socketPath)), session.stderr());
    client.on("error", () => {});
    client.connect(session.socketPath);
    await once(client, "connect");
    client.write(JSON.stringify({ content: "x".repeat(200 * 1024) }));

    // Never calls end(): the server has to be the one that closes.
    const blob = Buffer.alloc(64 * 1024, 0x61);
    const closed = await waitFor(() => {
      if (client.destroyed) return true;
      if (written < 32 * blob.length) {
        client.write(blob);
        written += blob.length;
      }
      return false;
    }, 8_000);
    assert.ok(closed,
      `the server must close a refused connection; wrote ${written} bytes after the refusal`);

    // And the channel still serves everyone else.
    const ack = await sendTo(session.socketPath,
      JSON.stringify({ content: "still here", message_id: "m-stream" }));
    assert.deepEqual(JSON.parse(ack), { ok: true, message_id: "m-stream" });
  } finally {
    client.destroy();
    await cleanUp(session, dir);
  }
}

async function losingTheStdioClientEndsTheSession() {
  // The Unix server keeps the event loop alive, so a stdio client going away is
  // not enough on its own to end the process. Observed in the wild: a channel
  // server from a session that ended hours earlier, orphaned under PPID 1, still
  // holding its socket, with its stdio descriptors pointing at nothing. Only a
  // signal cleared it — and nothing sends one when a session merely closes.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-eof-"));
  const session = spawnChannel(dir, "");
  try {
    assert.ok(await waitFor(() => existsSync(session.socketPath)), session.stderr());

    session.child.stdin.end();                    // EOF, no signal
    const exited = await Promise.race([
      once(session.child, "exit").then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), 5_000)),
    ]);
    assert.ok(exited, "the session must exit when its stdio client goes away");
    assert.equal(session.child.exitCode, 0, "and exit cleanly");
    assert.ok(!existsSync(session.socketPath), "it must remove its own socket");
    assert.deepEqual(registeredPeers(dir), [], "it must release its own claim");
  } finally {
    await cleanUp(session, dir);
  }
}

async function theWrapperTakesItsChannelDownWithIt() {
  // The installed command is `antiphon channel`, a wrapper that spawns
  // channel.mjs. It used to exit under a signal without passing it on, leaving
  // the server orphaned under PPID 1 — which is how the real leak happened,
  // not by anyone running lib/channel.mjs directly.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-wrapper-"));
  const env = { ...process.env, ANTIPHON_CWD: dir };
  delete env.ANTIPHON_NAME;
  const wrapper = spawn("node", ["bin/antiphon.mjs", "channel"],
    { env, stdio: ["pipe", "pipe", "pipe"] });
  const socketPath = socketFor(dir, "");
  try {
    assert.ok(await waitFor(() => existsSync(socketPath)), "wrapper never served");

    wrapper.kill("SIGTERM");
    const exited = await Promise.race([
      once(wrapper, "exit").then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), 5_000)),
    ]);
    assert.ok(exited, "the wrapper must exit on a signal");
    assert.ok(await waitFor(() => !existsSync(socketPath), 3_000),
      "the channel it started must clean up its socket, not outlive it");
    assert.deepEqual(registeredPeers(dir), [],
      "and release its registry claim");
  } finally {
    wrapper.kill("SIGKILL");
    await rm(socketPath, { force: true });
    await rm(dir, { recursive: true, force: true });
  }
}

await losingTheStdioClientEndsTheSession();
await theWrapperTakesItsChannelDownWithIt();
await anOversizedMessageIsRefusedWithoutKillingTheSession();
await aRefusedClientCannotKeepStreaming();
await aStalledClientDoesNotBlockShutdown();
await aSocketPathItCannotClearDoesNotKillTheSession();
await onlyOneUnnamedSessionGetsTheChannel(false);   // a second terminal, later
await onlyOneUnnamedSessionGetsTheChannel(true);    // both started together
console.log("per-peer sockets: ok");

try {
  await client.connect(transport);
  assert.equal(client.getServerVersion().name, "antiphon");
  assert.deepEqual(client.getServerCapabilities().experimental, { "claude/channel": {} });
  const onMessage = transport.onmessage;
  transport.onmessage = (message) => {
    if (message.method === "notifications/claude/channel") {
      resolveNotification(message);
    }
    onMessage(message);
  };

  const tools = await client.listTools();
  assert.equal(tools.tools[0].name, "reply_to_codex");

  const ack = await sendToSocket({ content: "identity test", message_id: "m-test" });
  assert.deepEqual(ack, { ok: true, message_id: "m-test" });
  const received = await Promise.race([
    notificationReceived,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error("channel notification timeout")),
      1_000,
    )),
  ]);
  assert.equal(received.params.content, "identity test");
  assert.deepEqual(received.params.meta, {
    sender: "codex",
    sender_kind: "agent",
    message_id: "m-test",
  });
  console.log("MCP channel integration: ok");
} finally {
  await client.close();
  await rm(projectDir, { recursive: true, force: true });
}
