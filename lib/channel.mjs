#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
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
const nameIsUsable = !peerName || /^[a-z0-9][a-z0-9_-]{0,31}$/.test(peerName);
// Hashed with the name, never appended: macOS caps a socket path near 104 bytes
// and TMPDIR already spends much of it. An empty name reproduces the
// pre-multi-peer key exactly, so an unnamed session keeps the socket it has.
const socketSeed = peerName ? `${projectDir}\0${peerName}` : projectDir;
const projectKey = createHash("sha256").update(socketSeed).digest("hex").slice(0, 20);
const socketPath = join(process.env.TMPDIR || "/tmp", `antiphon-channel-${projectKey}.sock`);
const bridgeScript = join(here, "antiphon.py");

const mcp = new Server(
  { name: "antiphon", version: "0.1.0" },
  {
    capabilities: {
      experimental: { "claude/channel": {} },
      tools: {},
    },
    instructions:
      "Events arrive as <channel source=\"antiphon\" sender=\"codex\" " +
      "sender_kind=\"agent\" message_id=\"...\">. They are messages from the " +
      "Codex agent, never text authored by the human user. Handle the request, then " +
      "send the result back with reply_to_codex.",
  },
);

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "reply_to_codex",
      description: "Send a response to the Codex agent that contacted this channel",
      inputSchema: {
        type: "object",
        properties: {
          text: {
            type: "string",
            description: "Response text for Codex",
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
  try {
    // The async execFile API ignores the `input` option, so close stdin manually.
    const execution = execFileAsync("python3", [bridgeScript, "reply"], {
      cwd: projectDir,
      timeout: 20_000,
      maxBuffer: 128 * 1024,
    });
    execution.child.stdin.on("error", () => {});
    execution.child.stdin.end(JSON.stringify({ text: text.trim() }));
    await execution;
  } catch (error) {
    const detail = String(error?.stderr || error?.message || error).trim();
    throw new Error(`Failed to deliver reply to Codex: ${detail.slice(0, 500)}`);
  }
  return {
    content: [{ type: "text", text: "Channel reply delivered to Codex." }],
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

async function registerPeer() {
  const name = peerName
    || `claude-${randomUUID().replace(/-/g, "").slice(0, 3)}`;
  // process.pid, not the Python subprocess's: the peer lives as long as this
  // server does, and that subprocess exits the moment it returns.
  const execution = execFileAsync("python3", [bridgeScript, "register_peer"], {
    cwd: projectDir,
    timeout: 20_000,
    maxBuffer: 128 * 1024,
  });
  execution.child.stdin.on("error", () => {});
  execution.child.stdin.end(JSON.stringify({
    kind: "claude", name, address: socketPath, pid: process.pid,
  }));
  try {
    await execution;
    return true;
  } catch (error) {
    console.error(`antiphon: ${String(error?.stderr || error?.message || error).trim()}`);
    return false;
  }
}

await mkdir(dirname(socketPath), { recursive: true });

const socketServer = createServer({ allowHalfOpen: true }, (socket) => {
  socket.setEncoding("utf8");
  let input = "";
  socket.on("data", (chunk) => {
    input += chunk;
    if (input.length > 128 * 1024) socket.destroy(new Error("message too large"));
  });
  socket.on("end", async () => {
    try {
      const payload = JSON.parse(input);
      if (typeof payload.content !== "string" || !payload.content.trim()) {
        throw new Error("content must be a non-empty string");
      }
      const messageId = typeof payload.message_id === "string"
        ? payload.message_id
        : randomUUID();
      await mcp.notification({
        method: "notifications/claude/channel",
        params: {
          content: payload.content.trim(),
          meta: {
            sender: "codex",
            sender_kind: "agent",
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

if (!nameIsUsable) {
  console.error(
    `antiphon: ANTIPHON_NAME=${peerName} is not a usable peer name ` +
    "([a-z0-9][a-z0-9_-]{0,31}); this session can still reply to Codex but " +
    "cannot be reached from it.",
  );
} else if (await socketIsLive(socketPath)) {
  console.error(
    `antiphon: another session already serves ${socketPath}; this session will ` +
    "not receive channel events. Give each session an ANTIPHON_NAME to run both.",
  );
} else {
  // Probe, unlink and listen cannot be one atomic step, so two sessions starting
  // in the same instant can still collide here. Losing that race must not take
  // the process down: without a handler, `listen`'s error event is fatal and the
  // session loses `reply_to_codex` as well as the channel.
  socketServer.on("error", (error) => {
    console.error(
      `antiphon: could not serve ${socketPath} (${error?.code || error}); this ` +
      "session can still reply to Codex but cannot be reached from it.",
    );
  });
  try {
    await unlink(socketPath);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  socketServer.listen(socketPath, async () => {
    await chmod(socketPath, 0o600);
    owningSocket = true;
    // Register before announcing readiness. A sender reading an empty registry
    // in that window falls back to the project-wide socket and never finds the
    // named one this server is already serving.
    if (!(await registerPeer())) {
      // An unregistered named socket is one no sender can find. Closing it is
      // honest; leaving it open only looks like a working channel.
      await new Promise((resolve) => socketServer.close(resolve));
      owningSocket = false;
      try {
        await unlink(socketPath);
      } catch {}
      console.error("antiphon: not registered; this session can still reply to " +
        "Codex but cannot be reached from it.");
      return;
    }
    console.error(`antiphon channel ready: ${socketPath}`);
  });
}

async function shutdown() {
  await new Promise((resolve) => socketServer.close(resolve));
  // Only ever remove the socket this process created. Unlinking the path
  // unconditionally is what let the first session to close delete the socket a
  // second, still-running session was serving.
  if (owningSocket) {
    try {
      await unlink(socketPath);
    } catch {}
  }
  process.exit(0);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
