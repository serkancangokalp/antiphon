#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { chmod, mkdir, unlink } from "node:fs/promises";
import { createServer } from "node:net";
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
const projectKey = createHash("sha256").update(projectDir).digest("hex").slice(0, 20);
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

await mkdir(dirname(socketPath), { recursive: true });
try {
  await unlink(socketPath);
} catch (error) {
  if (error?.code !== "ENOENT") throw error;
}

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

socketServer.listen(socketPath, async () => {
  await chmod(socketPath, 0o600);
  console.error(`antiphon channel ready: ${socketPath}`);
});

async function shutdown() {
  await new Promise((resolve) => socketServer.close(resolve));
  try {
    await unlink(socketPath);
  } catch {}
  process.exit(0);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
