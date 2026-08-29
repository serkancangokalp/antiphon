import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { connect } from "node:net";
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
