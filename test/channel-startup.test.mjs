import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const channel = fileURLToPath(new URL("../lib/channel.mjs", import.meta.url));
const preload = fileURLToPath(new URL("./fixtures/stdio_startup_gate.mjs", import.meta.url));

async function until(predicate, timeout = 10_000) {
  const deadline = Date.now() + timeout;
  while (!predicate() && Date.now() < deadline) await delay(10);
  return predicate();
}

// Late EOF handlers fail during-mkdir; missing readableEnded/destroyed checking
// fails already-ended. Both run the real MCP server, filesystem and registry.
for (const mode of ["during-mkdir", "already-ended"]) {
  const dir = mkdtempSync(join(tmpdir(), "antiphon-early-eof-"));
  const name = "early-eof";
  const key = createHash("sha256").update(`${dir}\0${name}`).digest("hex").slice(0, 20);
  const socket = join(process.env.TMPDIR || "/tmp", `antiphon-channel-${key}.sock`);
  const child = spawn(process.execPath, ["--import", preload, channel], {
    env: { ...process.env, HOME: dir, ANTIPHON_CWD: dir, ANTIPHON_NAME: name,
      ANTIPHON_STDIO_TEST_GATE: dir, ANTIPHON_STDIO_TEST_MODE: mode },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  let stdout = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  const exited = () => child.exitCode !== null || child.signalCode !== null;
  try {
    if (mode === "during-mkdir") {
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize",
        params: { protocolVersion: "2024-11-05", capabilities: {},
          clientInfo: { name: "early-eof-test", version: "1.0" } } }) + "\n");
      assert.ok(await until(() => existsSync(join(dir, "entered"))), stderr);
    }
    child.stdin.end();
    assert.ok(await until(() => existsSync(join(dir, "close"))),
      `the fixture must observe actual stdin EOF before releasing startup: ${stderr}`);
    writeFileSync(join(dir, "release"), "");
    assert.ok(await until(exited), `${mode}: EOF must end the session: ${stderr}`);
    assert.equal(child.exitCode, 0, `${mode}: clean exit required: ${stderr}`);
    assert.equal(child.signalCode, null);
    assert.equal(existsSync(socket), false, "early EOF must leave no socket");
    const peers = join(dir, ".antiphon", "peers");
    const endpoints = existsSync(peers)
      ? readdirSync(peers).filter((peer) => existsSync(join(peers, peer, "endpoint.json")))
      : [];
    assert.deepEqual(endpoints, [], "early EOF must leave no endpoint claim");
    assert.doesNotMatch(stderr, /antiphon channel ready:/,
      "a session closed before bind must never publish a socket");
    if (mode === "during-mkdir") assert.match(stdout, /"id":1/);
    console.log(`channel EOF ${mode}: ok`);
  } finally {
    writeFileSync(join(dir, "release"), "");
    if (!exited()) child.kill("SIGKILL");
    await until(exited);
    await rm(socket, { force: true });
    await rm(dir, { recursive: true, force: true });
  }
}
