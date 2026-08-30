import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
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
// `projectKey` above is the unnamed seed, so this server has to start unnamed
// too. Inheriting the host's `ANTIPHON_NAME` would move its socket somewhere
// else and leave the test waiting on a path nobody serves — and running
// `ANTIPHON_NAME=ui npm test` is a perfectly reasonable thing to do now.
const mainEnv = { ...process.env, ANTIPHON_CWD: projectDir };
delete mainEnv.ANTIPHON_NAME;

// A `codex` that records instead of queueing. Without it the reply tool can
// only ever be tested on its failure paths, and the sentence it hands back on
// success — the one that has to name the peer — is never exercised at all.
async function makeCodexStub() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-stub-"));
  const log = join(dir, "queued.txt");
  writeFileSync(join(dir, "codex"),
    `#!/bin/sh\nprintf '%s\\n' "$*" >> ${JSON.stringify(log)}\nexit 0\n`,
    { mode: 0o755 });
  return { dir, log };
}

const stub = await makeCodexStub();
const stubDir = stub.dir;
const queueLog = stub.log;
mainEnv.PATH = `${stubDir}:${process.env.PATH}`;

const CODEX_SESSION = "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7";

function liveCodexPeer(dir, alias, owner, session) {
  const peer = join(dir, ".antiphon", "peers", `codex-${alias}`);
  mkdirSync(peer, { recursive: true });
  writeFileSync(join(peer, "endpoint.json"), JSON.stringify({
    kind: "codex", name: alias, pid: process.pid,
    address: null, owner, started_at: Date.now() / 1000,
  }));
  writeFileSync(join(peer, "session.json"), JSON.stringify({
    kind: "codex", name: alias, owner, session_id: session,
  }));
}
const transport = new StdioClientTransport({
  command: "node",
  args: ["lib/channel.mjs"],
  env: mainEnv,
  stderr: "inherit",
});
const client = new Client({ name: "antiphon-test", version: "1.0.0" });
const notifications = [];
function nextNotification() {
  const seen = notifications.length;
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = setInterval(() => {
      if (notifications.length > seen) {
        clearInterval(poll);
        resolve(notifications[seen]);
      } else if (Date.now() - started > 2_000) {
        clearInterval(poll);
        reject(new Error("channel notification timeout"));
      }
    }, 10);
  });
}

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
// `once(child, "exit")` never fires for a child that has already gone: the event
// is in the past. Waiting on it therefore costs the full timeout every time,
// which is how a suite of fast tests came to take fourteen seconds. Check the
// recorded outcome first — and check `signalCode` too, since a child killed by a
// signal leaves `exitCode` null and looks alive to a naive test.
async function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return Promise.race([
    once(child, "exit").then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs)),
  ]);
}

async function cleanUp(session, dir) {
  session.child.kill("SIGTERM");
  if (!(await waitForExit(session.child, 2_000))) {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
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

async function onlyTheSessionThatWonTheNameSignsItsMessages() {
  // Two sessions started as `ui`; exactly one wins the registry. The winner
  // must sign its messages `ui` and the loser must not, because a reply
  // addressed to `ui` reaches the winner — and a message the loser signed `ui`
  // would send that reply to a session that never spoke.
  //
  // Both branches, in one real race, through two live MCP clients. With only
  // the loser exercised, deleting the success assignment outright changed no
  // test at all; with only the winner, the refusal branch went unwatched.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-race-"));
  const sockets = [socketFor(dir, "ui")];
  const stubs = [await makeCodexStub(), await makeCodexStub()];
  const clients = [];
  try {
    liveCodexPeer(dir, "build", "300:build", CODEX_SESSION);
    const start = async (stub) => {
      const env = { ...process.env, ANTIPHON_CWD: dir, ANTIPHON_NAME: "ui" };
      env.PATH = `${stub.dir}:${process.env.PATH}`;
      const transport = new StdioClientTransport({
        command: "node", args: ["lib/channel.mjs"], env, stderr: "pipe",
      });
      const client = new Client({ name: "antiphon-race", version: "1.0.0" });
      await client.connect(transport);
      let stderr = "";
      transport.stderr?.setEncoding("utf8");
      transport.stderr?.on("data", (chunk) => { stderr += chunk; });
      clients.push(client);
      return { client, stderr: () => stderr };
    };

    // `connect` returns once the MCP handshake is done, which is well before
    // the claim has been decided. Waiting for each session to say how the race
    // went is what makes this a test of the finished state rather than of
    // whichever moment the call happened to land in.
    const winner = await start(stubs[0]);
    // Fired the instant the handshake is done, before the claim can possibly
    // have been decided. A session that goes on to hold `ui` must still sign
    // this message `ui`: safe-but-anonymous is still the wrong answer.
    const earliest = winner.client.callTool({
      name: "reply_to_codex",
      arguments: { text: "from the winner", to: "build" },
    });
    assert.ok(await waitFor(() => /channel ready/.test(winner.stderr())),
      `the first session never took the channel: ${winner.stderr()}`);
    await earliest;
    const loser = await start(stubs[1]);
    assert.ok(await waitFor(() => /did not get the channel/.test(loser.stderr())),
      `the second session never reported losing: ${loser.stderr()}`);

    await loser.client.callTool({
      name: "reply_to_codex",
      arguments: { text: "from the loser", to: "build" },
    });

    assert.match(readFileSync(stubs[0].log, "utf8"), /\[from=ui id=/,
      "the session that holds `ui` must sign itself `ui`");
    const loserQueue = readFileSync(stubs[1].log, "utf8");
    assert.match(loserQueue, /\[from=<unnamed> id=/,
      "and the session that does not hold it must not");
    assert.ok(!loserQueue.includes("[from=ui "),
      `the loser signed itself with a name it does not hold: ${loserQueue}`);

    await clients.pop().close();      // the loser leaves
    const held = registeredPeers(dir).filter((peer) => peer.name === "ui");
    assert.equal(held.length, 1, "the winner's record survives the loser's exit");
    assert.equal(held[0].address, sockets[0]);
    assert.ok(existsSync(sockets[0]), "and its socket still serves");
    console.log("only the winner signs its name: ok");
  } finally {
    for (const client of clients) await client.close().catch(() => {});
    for (const path of sockets) await rm(path, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    for (const stub of stubs) {
      await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
    }
  }
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
    assert.ok(await waitForExit(session.child, 5_000),
      "SIGTERM must not hang on a client that sent nothing");
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
    assert.ok(await waitForExit(session.child, 5_000),
      "the session must exit when its stdio client goes away");
    assert.equal(session.child.exitCode, 0, "and exit cleanly");
    assert.ok(!existsSync(session.socketPath), "it must remove its own socket");
    assert.deepEqual(registeredPeers(dir), [], "it must release its own claim");
  } finally {
    await cleanUp(session, dir);
  }
}

async function theWrapperTakesItsChannelDownWithIt(signal) {
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

    wrapper.kill(signal);
    assert.ok(await waitForExit(wrapper, 5_000), `the wrapper must exit on ${signal}`);
    assert.equal(wrapper.exitCode, 0, `${signal} must be a clean exit, not a kill`);
    assert.ok(await waitFor(() => !existsSync(socketPath), 3_000),
      `the channel started by the wrapper must clean up its socket on ${signal}`);
    assert.deepEqual(registeredPeers(dir), [],
      "and release its registry claim");
  } finally {
    wrapper.kill("SIGKILL");
    await rm(socketPath, { force: true });
    await rm(dir, { recursive: true, force: true });
  }
}

async function aSignalTheInstantTheSocketAppearsIsStillClean() {
  // The socket becomes externally visible partway through startup. Registering
  // the lifecycle handlers after that — at the end of the module, once the claim
  // and the bind had finished — left a window where a signal met the default
  // disposition instead: measured at 30 runs out of 30, each exiting under
  // SIGTERM with its socket and its registry claim left behind. A session closed
  // at the wrong moment hit exactly this.
  //
  // Signalled the instant the socket exists, repeatedly, because a window this
  // wide only stays shut if something keeps checking.
  for (let attempt = 0; attempt < 5; attempt++) {
    const dir = await mkdtemp(join(tmpdir(), "antiphon-startrace-"));
    const session = spawnChannel(dir, "");
    try {
      const deadline = Date.now() + 8_000;
      while (!existsSync(session.socketPath) && Date.now() < deadline
             && session.child.exitCode === null) {
        await new Promise((resolve) => setImmediate(resolve));
      }
      assert.ok(existsSync(session.socketPath), `never served: ${session.stderr()}`);

      session.child.kill("SIGTERM");
      assert.ok(await waitForExit(session.child, 5_000), `attempt ${attempt}: no exit`);
      assert.equal(session.child.exitCode, 0,
        `attempt ${attempt}: signalled during startup must still exit cleanly`);
      assert.ok(!existsSync(session.socketPath),
        `attempt ${attempt}: socket left behind`);
      assert.deepEqual(registeredPeers(dir), [],
        `attempt ${attempt}: registry claim left behind`);
    } finally {
      await cleanUp(session, dir);
    }
  }
}

await aSignalTheInstantTheSocketAppearsIsStillClean();
await losingTheStdioClientEndsTheSession();
for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
  await theWrapperTakesItsChannelDownWithIt(signal);
}
await anOversizedMessageIsRefusedWithoutKillingTheSession();
await aRefusedClientCannotKeepStreaming();
await aStalledClientDoesNotBlockShutdown();
await aSocketPathItCannotClearDoesNotKillTheSession();
await onlyTheSessionThatWonTheNameSignsItsMessages();
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
      notifications.push(message);
    }
    onMessage(message);
  };

  const tools = await client.listTools();
  assert.equal(tools.tools[0].name, "reply_to_codex");
  assert.doesNotMatch(tools.tools[0].description, /you can leave it out/,
    "there is no single-peer shortcut on the Codex side to promise");
  const schema = tools.tools[0].inputSchema;
  assert.equal(schema.properties.to.type, "string");
  assert.deepEqual(schema.required, ["text"],
    "`to` is optional here only to preserve the project with no registered " +
    "Codex peer at all; requiring it would break every unnamed single pair");

  // `to` has to survive the hop into the Python process. Only the resolver
  // there can produce this sentence, so seeing the alias in the error proves
  // the argument arrived rather than being dropped on the way.
  await assert.rejects(
    () => client.callTool({ name: "reply_to_codex",
                            arguments: { text: "hi", to: "nobody-here" } }),
    /nobody-here/,
    "the alias must reach the Python resolver",
  );
  await assert.rejects(
    () => client.callTool({ name: "reply_to_codex",
                            arguments: { text: "hi", to: 42 } }),
    /to must be a string/,
    "a malformed argument is refused before the process is started",
  );

  // End to end, with a `codex` that answers: the alias resolves, the message is
  // queued against that peer's session, and the acknowledgement says which peer
  // it reached. A sender that addressed the wrong one can only notice here.
  liveCodexPeer(projectDir, "review", "301:review", CODEX_SESSION);
  const named = await client.callTool({
    name: "reply_to_codex", arguments: { text: "ship it", to: "review" },
  });
  assert.match(named.content[0].text, /review/,
    "an explicit recipient must be named back");
  assert.match(readFileSync(queueLog, "utf8"), new RegExp(CODEX_SESSION),
    "and the message must be queued against that peer's session");

  // A bare reply is refused as soon as a named Codex peer is registered: an
  // unnamed session leaves no record, so `review` cannot be shown to be the
  // only one there. Nothing is queued for it.
  await assert.rejects(
    () => client.callTool({ name: "reply_to_codex", arguments: { text: "bare" } }),
    /not discoverable/,
    "one registered peer is not proof of one session",
  );
  assert.ok(!readFileSync(queueLog, "utf8").includes("bare"),
    "and nothing was queued for the refused message");

  // This server started unnamed, so it holds the reserved key rather than a
  // name. The project also holds the Codex peer planted above, so the filter
  // has to pick out this server's own record or the assertion would pass while
  // proving nothing.
  const claudeRecords = registeredPeers(projectDir)
    .filter((peer) => peer.kind === "claude");
  assert.deepEqual(claudeRecords.map((peer) => peer.name), ["<unnamed>"],
    "an unnamed channel server occupies the reserved key and invents no name");
  const queued = readFileSync(queueLog, "utf8");
  assert.match(queued, /\[from=<unnamed> id=/,
    "and says so rather than signing with anything addressable");
  assert.doesNotMatch(queued, /\[from=claude-/,
    "no generated name may reach the other side as a sender");

  // Claimed before sending, so this reads its own event rather than whichever
  // notification happened to arrive first.
  const pendingIdentity = nextNotification();
  const ack = await sendToSocket({ content: "identity test", message_id: "m-test",
                                   sender_alias: "build" });
  assert.deepEqual(ack, { ok: true, message_id: "m-test" });
  const received = await pendingIdentity;
  assert.equal(received.params.content, "identity test");
  assert.deepEqual(received.params.meta, {
    sender: "codex",
    sender_kind: "agent",
    sender_alias: "build",
    message_id: "m-test",
  });

  // The alias crossed a socket, so it is a claim rather than a fact. Anything
  // that is not a name the registry would accept reaches the agent as null —
  // never as text it might read as a name to reply to.
  for (const claimed of ["Not A Name", "a b", "a]b", 42, null, ["ui"], "",
                         "a".repeat(40)]) {
    const pending = nextNotification();
    await sendToSocket({ content: `claim ${JSON.stringify(claimed)}`,
                         sender_alias: claimed });
    const seen = await pending;
    assert.equal(seen.params.meta.sender_alias, null,
      `an unusable alias must not reach the agent: ${JSON.stringify(claimed)}`);
  }

  console.log("MCP channel integration: ok");
} finally {
  // Each step runs even if an earlier one throws. A close that fails would
  // otherwise leave a project directory and a stub `codex` behind on every run.
  await client.close().catch(() => {});
  await rm(projectDir, { recursive: true, force: true }).catch(() => {});
  await rm(stubDir, { recursive: true, force: true }).catch(() => {});
}
