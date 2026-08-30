import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { connect } from "node:net";
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
    ui.child.kill("SIGKILL");
    api.child.kill("SIGKILL");
    await rm(dir, { recursive: true, force: true });
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
    first.child.kill("SIGKILL");
    second.child.kill("SIGKILL");
    await rm(dir, { recursive: true, force: true });
  }
}

await twoNamedPeersKeepSeparateSockets();
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
