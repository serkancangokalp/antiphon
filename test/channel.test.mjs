import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { Socket, connect, createServer } from "node:net";
import { once } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { materialiseLib } from "./fixtures/mixed_lib.mjs";

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
// Every transcript and catalog this integration run discovers stays inside its
// throwaway project. The real channel process receives this HOME too, so its
// Python retrieval child and this test name the same isolated source roots.
mainEnv.HOME = projectDir;

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
const AUTO_ALIAS = "auto-yzmcrss6whnnsjxthq2pclz3l4";
const AUTO_DIGEST = "c65828ca5eb1dad926f33c34f12f3b5fb031dca2f2e33d83dd70aa072a959928";
const UUID_V7 = "01890f3e-7b5a-7cc2-8f5d-123456789abc";
const AUTO_V7_ALIAS = "auto-h7n54mu5tb4wpf3g72gggmq5om";
const AUTO_V7_DIGEST = "3fdbde329d9879679766fe8c63321d733f8abaee51cf9dd450a4b5fc85471986";

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

function spawnChannel(dir, name, extraEnv = {}) {
  const env = { ...process.env, ...extraEnv, ANTIPHON_CWD: dir };
  if (name) env.ANTIPHON_NAME = name;
  else delete env.ANTIPHON_NAME;
  const child = spawn("node", ["lib/channel.mjs"], { env, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = "";
  let stdout = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  return {
    child,
    socketPath: socketFor(dir, name),
    stderr: () => stderr,
    stdout: () => stdout,
  };
}

async function makeDelayedRegistryPython() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-python-"));
  const armed = join(dir, "armed");
  const registerStarted = join(dir, "register-started");
  const registerFinished = join(dir, "register-finished");
  const releaseRegister = join(dir, "release-register");
  const unregisterArmed = join(dir, "unregister-armed");
  const unregisterStarted = join(dir, "unregister-started");
  const releaseUnregister = join(dir, "release-unregister");
  const realPython = execFileSync("python3", [
    "-c", "import sys; print(sys.executable)",
  ], { encoding: "utf8" }).trim();
  writeFileSync(join(dir, "python3"), `#!${realPython}
import os
import subprocess
import sys
import time

command = sys.argv[2] if len(sys.argv) > 2 else ""
armed = os.environ["ANTIPHON_TEST_REGISTER_ARMED"]
register_started = os.environ["ANTIPHON_TEST_REGISTER_STARTED"]
register_finished = os.environ["ANTIPHON_TEST_REGISTER_FINISHED"]
release_register = os.environ["ANTIPHON_TEST_REGISTER_RELEASE"]
unregister_started = os.environ["ANTIPHON_TEST_UNREGISTER_STARTED"]
unregister_armed = os.environ["ANTIPHON_TEST_UNREGISTER_ARMED"]
release_unregister = os.environ["ANTIPHON_TEST_UNREGISTER_RELEASE"]
real_python = os.environ["ANTIPHON_TEST_REAL_PYTHON"]

if command == "register_peer" and os.path.exists(armed):
    open(register_started, "w").close()
    while not os.path.exists(release_register):
        time.sleep(0.01)

if command == "unregister_peer":
    open(unregister_started, "w").close()
    while os.path.exists(unregister_armed) and not os.path.exists(release_unregister):
        time.sleep(0.01)
    result = subprocess.run([real_python, *sys.argv[1:]])
    raise SystemExit(result.returncode)

if command == "register_peer":
    result = subprocess.run([real_python, *sys.argv[1:]])
    open(register_finished, "w").close()
    raise SystemExit(result.returncode)

os.execv(real_python, [real_python, *sys.argv[1:]])
`, { mode: 0o755 });
  return {
    dir,
    armed,
    registerStarted,
    registerFinished,
    releaseRegister,
    unregisterArmed,
    unregisterStarted,
    releaseUnregister,
    env: {
      PATH: `${dir}:${process.env.PATH}`,
      ANTIPHON_TEST_REGISTER_ARMED: armed,
      ANTIPHON_TEST_REGISTER_STARTED: registerStarted,
      ANTIPHON_TEST_REGISTER_FINISHED: registerFinished,
      ANTIPHON_TEST_REGISTER_RELEASE: releaseRegister,
      ANTIPHON_TEST_UNREGISTER_ARMED: unregisterArmed,
      ANTIPHON_TEST_UNREGISTER_STARTED: unregisterStarted,
      ANTIPHON_TEST_UNREGISTER_RELEASE: releaseUnregister,
      ANTIPHON_TEST_REAL_PYTHON: realPython,
    },
  };
}

// One shim doing both jobs: the identity probe a channel needs to take an
// automatic alias, and a `unregister_peer` that blocks until released. Two
// separate stubs cannot be used together — each installs its own `python3` on
// PATH — and the retirement race needs both at once.
// A registry whose register succeeds and returns no fingerprint — what a
// platform where the process table cannot be read looks like from here.
async function makeBirthlessRegistryHarness(identity) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-birthless-"));
  const realPython = execFileSync("python3", [
    "-c", "import sys; print(sys.executable)",
  ], { encoding: "utf8" }).trim();
  writeFileSync(join(dir, "python3"), `#!${realPython}
import json
import os
import subprocess
import sys

command = sys.argv[2] if len(sys.argv) > 2 else ""
real_python = os.environ["ANTIPHON_TEST_REAL_PYTHON"]

if command == "claude_identity":
    print(os.environ["ANTIPHON_TEST_IDENTITY_RESULT"])
    raise SystemExit(0)

if command == "register_peer":
    payload = sys.stdin.read()
    result = subprocess.run([real_python, *sys.argv[1:]], input=payload,
                            text=True, capture_output=True)
    sys.stderr.write(result.stderr)
    if result.returncode == 0:
        # Registered, fingerprint unavailable: a current registry whose ps
        # failed, which acknowledges the field. Answering {} would read as
        # an older Python on disk, the downgrade case, not this one.
        print(json.dumps({"birth": None, "fingerprint_field": "process_birth"}))
    raise SystemExit(result.returncode)

os.execv(real_python, [real_python, *sys.argv[1:]])
`, { mode: 0o755 });
  return {
    dir,
    env: {
      PATH: `${dir}:${process.env.PATH}`,
      ANTIPHON_TEST_IDENTITY_RESULT: JSON.stringify(identity),
      ANTIPHON_TEST_REAL_PYTHON: realPython,
    },
  };
}

async function makeRetireRaceHarness(identity) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-retire-race-"));
  const unregisterStarted = join(dir, "unregister-started");
  const releaseUnregister = join(dir, "release-unregister");
  const realPython = execFileSync("python3", [
    "-c", "import sys; print(sys.executable)",
  ], { encoding: "utf8" }).trim();
  writeFileSync(join(dir, "python3"), `#!${realPython}
import os
import subprocess
import sys
import time

command = sys.argv[2] if len(sys.argv) > 2 else ""
real_python = os.environ["ANTIPHON_TEST_REAL_PYTHON"]

if command == "claude_identity":
    print(os.environ["ANTIPHON_TEST_IDENTITY_RESULT"])
    raise SystemExit(0)

if command == "unregister_peer":
    open(os.environ["ANTIPHON_TEST_UNREGISTER_STARTED"], "w").close()
    while not os.path.exists(os.environ["ANTIPHON_TEST_UNREGISTER_RELEASE"]):
        time.sleep(0.01)
    result = subprocess.run([real_python, *sys.argv[1:]])
    raise SystemExit(result.returncode)

os.execv(real_python, [real_python, *sys.argv[1:]])
`, { mode: 0o755 });
  return {
    dir,
    unregisterStarted,
    releaseUnregister,
    env: {
      PATH: `${dir}:${process.env.PATH}`,
      ANTIPHON_TEST_IDENTITY_RESULT: JSON.stringify(identity),
      ANTIPHON_TEST_UNREGISTER_STARTED: unregisterStarted,
      ANTIPHON_TEST_UNREGISTER_RELEASE: releaseUnregister,
      ANTIPHON_TEST_REAL_PYTHON: realPython,
    },
  };
}

async function makeAutomaticIdentityPython(identity) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-identity-python-"));
  const calls = join(dir, "calls.txt");
  const realPython = execFileSync("python3", [
    "-c", "import sys; print(sys.executable)",
  ], { encoding: "utf8" }).trim();
  writeFileSync(join(dir, "python3"), `#!${realPython}
import os
import sys

command = sys.argv[2] if len(sys.argv) > 2 else ""
if command == "claude_identity":
    with open(os.environ["ANTIPHON_TEST_IDENTITY_CALLS"], "a") as stream:
        stream.write(command + "\\n")
    print(os.environ["ANTIPHON_TEST_IDENTITY_RESULT"])
    raise SystemExit(0)

real_python = os.environ["ANTIPHON_TEST_REAL_PYTHON"]
os.execv(real_python, [real_python, *sys.argv[1:]])
`, { mode: 0o755 });
  return {
    dir,
    calls,
    env: {
      PATH: `${dir}:${process.env.PATH}`,
      ANTIPHON_TEST_IDENTITY_CALLS: calls,
      ANTIPHON_TEST_IDENTITY_RESULT: JSON.stringify(identity),
      ANTIPHON_TEST_REAL_PYTHON: realPython,
    },
  };
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

async function invalidConfiguredNamesAreNotEchoed() {
  const variants = [
    CODEX_SESSION.toUpperCase(),
    `{${CODEX_SESSION}}`,
    `urn:uuid:${CODEX_SESSION}`,
    `uuid:${CODEX_SESSION}`,
    `<${CODEX_SESSION}>`,
    `${CODEX_SESSION}.`,
  ];
  for (const supplied of variants) {
    const dir = await mkdtemp(join(tmpdir(), "antiphon-uuid-name-"));
    const session = spawnChannel(dir, supplied);
    try {
      assert.ok(await waitFor(() => /not a usable peer name/.test(session.stderr())),
        session.stderr());
      assert.match(session.stderr(), /ANTIPHON_NAME/,
        "the refusal names the invalid setting without reproducing its value");
      assert.ok(!session.stderr().toLowerCase().includes(CODEX_SESSION),
        `the configured host id must stay private: ${session.stderr()}`);
    } finally {
      await cleanUp(session, dir);
    }
  }
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

function endpointFor(dir, name) {
  return join(dir, ".antiphon", "peers", `claude-${name}`, "endpoint.json");
}

async function anAutomaticClaudeIdentityRequiresTheHookJoinBeforeSigning() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-auto-claude-"));
  const probe = await makeAutomaticIdentityPython({
    alias: AUTO_ALIAS,
    identity_digest: AUTO_DIGEST,
  });
  const stub = await makeCodexStub();
  const socket = socketFor(dir, AUTO_ALIAS);
  const env = { ...process.env, ...probe.env, ANTIPHON_CWD: dir };
  env.PATH = `${probe.dir}:${stub.dir}:${process.env.PATH}`;
  delete env.ANTIPHON_NAME;
  const transport = new StdioClientTransport({
    command: "node", args: ["lib/channel.mjs"], env, stderr: "pipe",
  });
  const client = new Client({ name: "antiphon-auto-identity", version: "1.0.0" });
  try {
    liveCodexPeer(dir, "build", "300:build", CODEX_SESSION);
    await client.connect(transport);
    assert.ok(await waitFor(() => existsSync(endpointFor(dir, AUTO_ALIAS))),
      "the verified probe did not create the automatic Claude endpoint");
    const endpoint = JSON.parse(readFileSync(endpointFor(dir, AUTO_ALIAS), "utf8"));
    assert.ok(await waitFor(() => existsSync(socket)),
      `the automatic alias must select its named socket; endpoint=${JSON.stringify(endpoint)}`);

    assert.equal(endpoint.name, AUTO_ALIAS);
    assert.equal(endpoint.automatic, true);
    assert.equal(endpoint.identity_digest, AUTO_DIGEST);
    assert.ok(!JSON.stringify(endpoint).includes(CODEX_SESSION),
      "the public registry record must not contain the host session id");
    assert.equal(readFileSync(probe.calls, "utf8").trim(), "claude_identity",
      "an unnamed channel asks the fixed Python identity probe exactly once");

    await client.callTool({
      name: "reply_to_codex", arguments: { text: "before hook", to: "build" },
    });
    let queued = readFileSync(stub.log, "utf8");
    assert.match(queued, /\[from=<unnamed> id=/,
      "a probe alone cannot authenticate an outgoing automatic identity");
    assert.ok(!queued.includes(AUTO_ALIAS),
      "the automatic alias stays private until the hook joins the same endpoint");

    // The real hook writes both halves: the session record and the
    // owner-current proof, in that one turn. A fixture that wrote only the
    // session half would be simulating a hook that no longer exists.
    writeFileSync(join(dir, ".antiphon", "peers", `claude-${AUTO_ALIAS}`, "session.json"),
      JSON.stringify({
        kind: "claude",
        name: AUTO_ALIAS,
        owner: endpoint.owner,
        session_id: CODEX_SESSION,
        automatic: true,
        identity_digest: AUTO_DIGEST,
      }));
    const currentOwnerDigest = createHash("sha256")
      .update(endpoint.owner).digest("hex");
    mkdirSync(join(dir, ".antiphon", "identity", "claude"), { recursive: true });
    writeFileSync(
      join(dir, ".antiphon", "identity", "claude", `${currentOwnerDigest}.json`),
      JSON.stringify({
        version: 1, kind: "claude", owner_key: endpoint.owner,
        owner_digest: currentOwnerDigest, session_id: CODEX_SESSION,
        identity_digest: AUTO_DIGEST,
      }));
    await client.callTool({
      name: "reply_to_codex", arguments: { text: "after hook", to: "build" },
    });
    queued = readFileSync(stub.log, "utf8");
    assert.match(queued, new RegExp(`\\[from=${AUTO_ALIAS} id=`),
      "the matching hook record authenticates the automatic sender alias");
    const labels = queued.match(/\[from=[^\]]+ id=[^\]]+\]/g)?.join("\n") || "";
    assert.ok(!labels.includes(CODEX_SESSION),
      "the host session id never crosses in the message label");

    // --- Task 6b: signing_identity ---------------------------------------
    // Routing already refuses a rotated alias. Signing must refuse it too, or
    // the old listener keeps announcing an identity that is no longer its own.
    const ownerDigest = currentOwnerDigest;
    const otherSession = "0199a1b2-2222-7000-8000-00000000000b";
    const otherDigest = createHash("sha256")
      .update(otherSession).digest("hex");
    mkdirSync(join(dir, ".antiphon", "identity", "claude"), { recursive: true });
    writeFileSync(
      join(dir, ".antiphon", "identity", "claude", `${ownerDigest}.json`),
      JSON.stringify({
        version: 1, kind: "claude", owner_key: endpoint.owner,
        owner_digest: ownerDigest, session_id: otherSession,
        identity_digest: otherDigest,
      }));
    const before = readFileSync(stub.log, "utf8").length;
    await client.callTool({
      name: "reply_to_codex",
      arguments: { text: "signing_identity after rotation", to: "build" },
    });
    const after = readFileSync(stub.log, "utf8").slice(before);
    assert.ok(!after.includes(AUTO_ALIAS),
      "signing_identity: a rotated proof must stop this listener signing as "
      + "the alias it no longer owns");
    assert.match(after, /\[from=<unnamed> id=/,
      "signing_identity: unreachable and unnamed, not silently still itself");
    assert.ok(!labels.includes(AUTO_DIGEST),
      "the private digest never crosses in the message label");
    console.log("automatic Claude identity waits for the hook join: ok");
  } finally {
    await client.close().catch(() => {});
    await rm(socket, { force: true }).catch(() => {});
    for (const path of [dir, probe.dir, stub.dir]) {
      await rm(path, { recursive: true, force: true }).catch(() => {});
    }
  }
}

async function explicitAndUnverifiedClaudeIdentitiesStayConservative() {
  const explicitDir = await mkdtemp(join(tmpdir(), "antiphon-explicit-claude-"));
  const explicitProbe = await makeAutomaticIdentityPython({
    alias: AUTO_ALIAS,
    identity_digest: AUTO_DIGEST,
  });
  const explicit = spawnChannel(explicitDir, "ui", explicitProbe.env);
  try {
    assert.ok(await waitFor(() => existsSync(endpointFor(explicitDir, "ui"))),
      `the explicit peer never registered: ${explicit.stderr()}`);
    assert.ok(!existsSync(explicitProbe.calls),
      "ANTIPHON_NAME is an override, so an explicit peer never runs the auto probe");
  } finally {
    await cleanUp(explicit, explicitDir);
    await rm(explicitProbe.dir, { recursive: true, force: true });
  }

  const rejectedDir = await mkdtemp(join(tmpdir(), "antiphon-rejected-auto-"));
  const rejectedProbe = await makeAutomaticIdentityPython({
    alias: "auto-aaaaaaaaaaaaaaaaaaaaaaaaaa",
    identity_digest: AUTO_DIGEST,
  });
  const rejected = spawnChannel(rejectedDir, "", rejectedProbe.env);
  try {
    assert.ok(await waitFor(() => registeredPeers(rejectedDir).length === 1),
      `the conservative unnamed peer never registered: ${rejected.stderr()}`);
    assert.deepEqual(registeredPeers(rejectedDir).map((peer) => peer.name), ["<unnamed>"],
      "an alias that does not derive from its digest is never published");
    assert.ok(await waitFor(() => existsSync(socketFor(rejectedDir, ""))),
      "a rejected probe keeps the legacy unnamed socket");
    assert.ok(!rejected.stderr().includes(AUTO_DIGEST),
      "a rejected private digest is not echoed in diagnostics");
  } finally {
    await cleanUp(rejected, rejectedDir);
    await rm(rejectedProbe.dir, { recursive: true, force: true });
  }
  console.log("explicit and unverified Claude identities stay conservative: ok");
}

async function automaticClaudeIdentityAcceptsTheCanonicalUuidGrammar() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-auto-v7-"));
  const probe = await makeAutomaticIdentityPython({
    alias: AUTO_V7_ALIAS,
    identity_digest: AUTO_V7_DIGEST,
  });
  const stub = await makeCodexStub();
  const socket = socketFor(dir, AUTO_V7_ALIAS);
  const env = { ...process.env, ...probe.env, ANTIPHON_CWD: dir };
  env.PATH = `${probe.dir}:${stub.dir}:${process.env.PATH}`;
  delete env.ANTIPHON_NAME;
  const transport = new StdioClientTransport({
    command: "node", args: ["lib/channel.mjs"], env, stderr: "pipe",
  });
  const client = new Client({ name: "antiphon-auto-v7", version: "1.0.0" });
  try {
    liveCodexPeer(dir, "build", "300:build", CODEX_SESSION);
    await client.connect(transport);
    assert.ok(await waitFor(() => existsSync(endpointFor(dir, AUTO_V7_ALIAS))),
      "the canonical v7 identity did not create an endpoint");
    const endpoint = JSON.parse(readFileSync(endpointFor(dir, AUTO_V7_ALIAS), "utf8"));
    writeFileSync(join(dir, ".antiphon", "peers", `claude-${AUTO_V7_ALIAS}`, "session.json"),
      JSON.stringify({
        kind: "claude", name: AUTO_V7_ALIAS, owner: endpoint.owner,
        session_id: UUID_V7, automatic: true, identity_digest: AUTO_V7_DIGEST,
      }));
    // Both halves, as the real hook writes them in one turn.
    const v7OwnerDigest = createHash("sha256")
      .update(endpoint.owner).digest("hex");
    mkdirSync(join(dir, ".antiphon", "identity", "claude"), { recursive: true });
    writeFileSync(
      join(dir, ".antiphon", "identity", "claude", `${v7OwnerDigest}.json`),
      JSON.stringify({
        version: 1, kind: "claude", owner_key: endpoint.owner,
        owner_digest: v7OwnerDigest, session_id: UUID_V7,
        identity_digest: AUTO_V7_DIGEST,
      }));
    await client.callTool({
      name: "reply_to_codex", arguments: { text: "from v7", to: "build" },
    });
    assert.match(readFileSync(stub.log, "utf8"),
      new RegExp(`\\[from=${AUTO_V7_ALIAS} id=`),
      "Node must accept every canonical UUID shape accepted by the registry");
    console.log("automatic Claude identity accepts the canonical UUID grammar: ok");
  } finally {
    await client.close().catch(() => {});
    await rm(socket, { force: true }).catch(() => {});
    for (const path of [dir, probe.dir, stub.dir]) {
      await rm(path, { recursive: true, force: true }).catch(() => {});
    }
  }
}

async function aLiveListenerReassertsItsOwnMissingEndpoint() {
  // The durable outage: the process and named socket are both alive, but a
  // reader rendered its process birth in another timezone and pruned the
  // endpoint. Only the process actually serving the socket may restore it.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-reassert-"));
  const session = spawnChannel(dir, "ui");
  try {
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1),
      `listener never registered: ${session.stderr()}`);
    assert.ok(await waitFor(() => existsSync(session.socketPath)),
      `listener registered before its socket was ready: ${session.stderr()}`);
    const endpoint = endpointFor(dir, "ui");
    await rm(endpoint, { force: true });
    assert.deepEqual(registeredPeers(dir), [], "fixture must reproduce no endpoint");

    const wrong = JSON.parse(await sendTo(session.socketPath, JSON.stringify({
      control: "antiphon.channel",
      version: 1,
      action: "reassert",
      alias: "api",
      nonce: "wrong-alias",
    })));
    assert.equal(wrong.ok, false);
    assert.deepEqual(registeredPeers(dir), [],
      "a request for another alias must write nothing");

    const nonce = "reassert-own-endpoint";
    const reply = JSON.parse(await sendTo(session.socketPath, JSON.stringify({
      control: "antiphon.channel",
      version: 1,
      action: "reassert",
      alias: "ui",
      nonce,
    })));
    assert.deepEqual(reply, {
      ok: true,
      control: "antiphon.channel",
      version: 1,
      action: "reasserted",
      alias: "ui",
      nonce,
      pid: session.child.pid,
    });
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1),
      `listener did not restore its endpoint: ${session.stderr()}`);
    const [restored] = registeredPeers(dir);
    assert.equal(restored.pid, session.child.pid,
      "the caller must never register another process on its behalf");
    assert.equal(restored.address, session.socketPath);
    assert.ok(!session.stdout().includes("notifications/claude/channel"),
      "a control request must never become a Claude channel notification");
    console.log("a live listener reasserts its own endpoint: ok");
  } finally {
    await cleanUp(session, dir);
  }
}

async function aStartupClaimCannotOutliveSignalShutdown() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-claim-signal-"));
  const delayed = await makeDelayedRegistryPython();
  writeFileSync(delayed.armed, "");
  const session = spawnChannel(dir, "ui", delayed.env);
  let stale = false;
  try {
    assert.ok(await waitFor(() => existsSync(delayed.registerStarted)),
      `startup never entered register_peer: ${session.stderr()}`);

    session.child.kill("SIGTERM");
    // The broken implementation starts unregister_peer beside the blocked
    // registration. The corrected implementation deliberately does not, so
    // this bounded wait is only a deterministic observation window before the
    // test lets the registration finish.
    await waitFor(() => existsSync(delayed.unregisterStarted), 2_000);
    writeFileSync(delayed.releaseRegister, "");

    assert.ok(await waitFor(() => existsSync(delayed.registerFinished)),
      "the delayed startup registration never finished");
    assert.ok(await waitForExit(session.child, 5_000),
      "shutdown did not finish after startup registration was released");
    stale = registeredPeers(dir).length !== 0;
    return stale;
  } finally {
    writeFileSync(delayed.releaseRegister, "");
    await cleanUp(session, dir);
    await rm(delayed.dir, { recursive: true, force: true });
  }
}

async function aCompletedClaimCannotBindAfterSignalShutdown() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-bind-signal-"));
  const delayed = await makeDelayedRegistryPython();
  const gateEntered = join(delayed.dir, "socket-gate-entered");
  const releaseGate = join(delayed.dir, "release-socket-gate");
  writeFileSync(delayed.unregisterArmed, "");
  const session = spawnChannel(dir, "ui", {
    ...delayed.env,
    NODE_ENV: "test",
    ANTIPHON_TEST_SOCKET_GATE: "after-claim",
    ANTIPHON_TEST_SOCKET_GATE_ENTERED: gateEntered,
    ANTIPHON_TEST_SOCKET_GATE_RELEASE: releaseGate,
  });
  let stale = false;
  try {
    assert.ok(await waitFor(() => existsSync(gateEntered)
      && registeredPeers(dir).length === 1),
    `claim did not finish before the socket gate: ${session.stderr()}`);
    assert.ok(!existsSync(session.socketPath),
      "the fixture must stop after claim and before bind");

    session.child.kill("SIGTERM");
    // In the broken order shutdown reaches unregister while startup is still
    // held before bind. In the corrected order it waits for startup first.
    const unregisterStartedBeforeGate = await waitFor(
      () => existsSync(delayed.unregisterStarted), 2_000);
    writeFileSync(releaseGate, "");
    if (unregisterStartedBeforeGate) {
      assert.ok(await waitFor(() => existsSync(session.socketPath)),
        "the regression fixture did not expose the post-cleanup bind");
    } else {
      assert.ok(await waitFor(() => existsSync(delayed.unregisterStarted)),
        "shutdown did not reach its final unregister after startup settled");
      assert.ok(!existsSync(session.socketPath),
        "startup must not bind after shutdown begins");
    }
    writeFileSync(delayed.releaseUnregister, "");

    assert.ok(await waitForExit(session.child, 5_000),
      "shutdown did not finish after unregister was released");
    stale = existsSync(session.socketPath) || registeredPeers(dir).length !== 0;
    return stale;
  } finally {
    writeFileSync(releaseGate, "");
    writeFileSync(delayed.releaseUnregister, "");
    await cleanUp(session, dir);
    await rm(delayed.dir, { recursive: true, force: true });
  }
}

async function aControlClaimCannotOutliveEofShutdown() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-claim-eof-"));
  const delayed = await makeDelayedRegistryPython();
  const session = spawnChannel(dir, "ui", delayed.env);
  let stale = false;
  try {
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1
      && existsSync(delayed.registerFinished)
      && existsSync(session.socketPath)),
    `listener never completed startup: ${session.stderr()}`);
    await rm(endpointFor(dir, "ui"), { force: true });
    await rm(delayed.registerStarted, { force: true });
    await rm(delayed.registerFinished, { force: true });
    writeFileSync(delayed.armed, "");

    const reply = sendTo(session.socketPath, JSON.stringify({
      control: "antiphon.channel",
      version: 1,
      action: "reassert",
      alias: "ui",
      nonce: "shutdown-during-reassert",
    }));
    assert.ok(await waitFor(() => existsSync(delayed.registerStarted)),
      `control request never entered register_peer; stderr=${session.stderr()} ` +
      `exit=${session.child.exitCode}/${session.child.signalCode} ` +
      `socket=${existsSync(session.socketPath)}`);

    session.child.stdin.end();
    await waitFor(() => existsSync(delayed.unregisterStarted), 2_000);
    writeFileSync(delayed.releaseRegister, "");

    assert.ok(await waitFor(() => existsSync(delayed.registerFinished)),
      "the delayed control registration never finished");
    assert.ok(await waitForExit(session.child, 5_000),
      "EOF shutdown did not finish after control registration was released");
    await reply;
    stale = registeredPeers(dir).length !== 0;
    return stale;
  } finally {
    writeFileSync(delayed.releaseRegister, "");
    await cleanUp(session, dir);
    await rm(delayed.dir, { recursive: true, force: true });
  }
}

async function anArbitrarySocketBinderIsNotAdopted() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-binder-"));
  const path = socketFor(dir, "ui");
  let received = "";
  const binder = createServer({ allowHalfOpen: true }, (socket) => {
    socket.on("error", () => {});
    socket.setEncoding("utf8");
    socket.on("data", (chunk) => { received += chunk; });
    socket.on("end", () => socket.end(JSON.stringify({ ok: true })));
  });
  await new Promise((resolve, reject) => {
    binder.once("error", reject);
    binder.listen(path, resolve);
  });
  const session = spawnChannel(dir, "ui");
  try {
    assert.ok(await waitFor(() => /did not prove/.test(session.stderr())),
      `the occupied path was not diagnosed: ${session.stderr()}`);
    assert.deepEqual(registeredPeers(dir), [],
      "generic JSON from a socket holder is not an endpoint claim");
    const request = JSON.parse(received);
    assert.equal(request.control, "antiphon.channel");
    assert.equal(request.action, "reassert");
    assert.equal(request.alias, "ui");
    assert.ok(!Object.hasOwn(request, "content"),
      "even an unverified listener learns no attempted message content");
    console.log("an arbitrary socket binder is not adopted: ok");
  } finally {
    await new Promise((resolve) => binder.close(resolve));
    await cleanUp(session, dir);
  }
}

async function aReconnectRepairsTheLiveListenersMissingRecord() {
  // This is the persistent production chain, not only the control primitive:
  // a second MCP server starts while the first still owns the named socket but
  // its endpoint has been pruned. The second must ask the first to advertise
  // itself, never leave the project in the same invisible state again.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-reconnect-"));
  const first = spawnChannel(dir, "ui");
  let second;
  try {
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1
      && existsSync(first.socketPath)),
      `first listener never registered: ${first.stderr()}`);
    await rm(endpointFor(dir, "ui"), { force: true });
    assert.deepEqual(registeredPeers(dir), []);

    second = spawnChannel(dir, "ui");
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1
      && /reasserted its endpoint/.test(second.stderr())),
      `reconnect did not repair the first listener:\n${first.stderr()}\n${second.stderr()}`);
    const [restored] = registeredPeers(dir);
    assert.equal(restored.pid, first.child.pid,
      "the process holding the socket must remain the registered peer");
    assert.notEqual(restored.pid, second.child.pid,
      "the reconnect must not advertise itself over somebody else's socket");
    assert.ok(existsSync(first.socketPath));

    second.child.kill("SIGTERM");
    await once(second.child, "exit");
    assert.equal(registeredPeers(dir)[0].pid, first.child.pid,
      "the reconnect's shutdown must not release the listener's restored claim");
    assert.ok(existsSync(first.socketPath));
    console.log("a reconnect repairs the live listener's missing record: ok");
  } finally {
    await cleanUp(first, dir);
    if (second) await cleanUp(second, dir);
  }
}

// Four sessions, four real process trees, nothing planted. Every earlier test
// here builds a piece of this by hand — a registry record written directly, a
// peer whose owner key came from a patch — which is the right way to test a
// piece and the wrong way to believe the whole. This one starts two Claude
// channel servers and two Codex MCP servers for real and makes them talk.
//
// `owner_key` finds the nearest ancestor whose `ps` command is `claude` or
// `codex`, so each session needs a real process of that name above it. A
// symlink to `/bin/sh` gives one: the kernel runs the real shell while argv[0],
// and therefore `ps`, says `claude`. A copied binary is killed on macOS for
// having lost its signature, and a `#!/bin/sh` script shows up as `sh`.
//
// The danger in a harness like this is that it silently proves nothing: if the
// fake roots did not work, every session would walk past them to the real
// `claude` running the suite and they would all share one owner key — and the
// assertions would still pass. So the identities are checked directly.
async function fourLiveSessionsRouteByName() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-four-"));
  const roots = await mkdtemp(join(tmpdir(), "antiphon-roots-"));
  const stub = await makeCodexStub();
  symlinkSync("/bin/sh", join(roots, "claude"));
  symlinkSync("/bin/sh", join(roots, "codex"));
  const SESSIONS = {
    build: "1d5a03e0-0548-4339-87c3-45c5dbf7e9d7",
    review: "2e6b14f1-1659-544a-98d4-56d6eca8fa48",
  };
  const open = [];
  const sockets = ["ui", "api"].map((name) => socketFor(dir, name));

  const connect = async (root, name, script) => {
    const env = { ...process.env, ANTIPHON_CWD: dir, ANTIPHON_NAME: name };
    env.PATH = `${stub.dir}:${process.env.PATH}`;
    const transport = new StdioClientTransport({
      command: join(roots, root), args: ["-c", script], env, stderr: "pipe",
    });
    const seen = [];
    const client = new Client({ name: `antiphon-${name}`, version: "1.0.0" });
    await client.connect(transport);
    const passThrough = transport.onmessage;
    transport.onmessage = (message) => {
      if (message.method === "notifications/claude/channel") seen.push(message);
      passThrough(message);
    };
    let stderr = "";
    transport.stderr?.setEncoding("utf8");
    transport.stderr?.on("data", (chunk) => { stderr += chunk; });
    open.push(client);
    return { name, client, seen, stderr: () => stderr };
  };

  // Two commands, never one: `sh -c` with a single command replaces itself with
  // it, and the root this whole test depends on would vanish before the child
  // it is supposed to parent.
  const claudeSession = (name) =>
    connect("claude", name, "node lib/channel.mjs; exit");

  const codexSession = async (name) => {
    // The hook runs first and as a child of the same root, which is how the
    // two halves of a Codex peer find each other: same walk, same owner key,
    // no shared state and no coordination. Its stdout goes nowhere — the MCP
    // stream begins with the second command.
    const payload = join(dir, `${name}-session.json`);
    writeFileSync(payload, JSON.stringify({
      cwd: dir, hook_event_name: "SessionStart",
      session_id: SESSIONS[name], transcript_path: `/tmp/${name}.jsonl`,
    }));
    return connect("codex", name,
      `python3 lib/antiphon.py hook codex < ${payload} > /dev/null; ` +
      "python3 lib/antiphon.py mcp");
  };

  const named = (kind, name) => registeredPeers(dir)
    .find((peer) => peer.kind === kind && peer.name === name);

  try {
    const ui = await claudeSession("ui");
    const api = await claudeSession("api");
    const build = await codexSession("build");
    const review = await codexSession("review");
    const everyone = [ui, api, build, review];

    assert.ok(await waitFor(() =>
      ["ui", "api"].every((n) => named("claude", n)) &&
      ["build", "review"].every((n) => named("codex", n))),
      `not all four registered: ${everyone.map((s) => s.stderr()).join("\n")}`);

    // The check that keeps this test honest. Four sessions under four roots
    // means four owner keys; one key would mean the roots were never used and
    // everything below proves nothing.
    const owners = [named("claude", "ui"), named("claude", "api"),
                    named("codex", "build"), named("codex", "review")]
      .map((peer) => peer.owner);
    assert.ok(owners.every(Boolean), `a session identified nobody: ${owners}`);
    assert.equal(new Set(owners).size, 4,
      `four sessions must have four identities, got ${JSON.stringify(owners)}`);

    // ---- Codex → Claude, by name ----
    await build.client.callTool({
      name: "antiphon_send", arguments: { text: "for api only", to: "api" },
    });
    assert.ok(await waitFor(() => api.seen.length === 1), "api never heard build");
    assert.equal(api.seen[0].params.content, "for api only");
    assert.equal(api.seen[0].params.meta.sender_alias, "build");
    // Each delivery is identified, and separately. The channel server mints an
    // id when the sender sends none, so this cannot show which side produced
    // it — that belongs to the Python tests. What it does show is that two
    // deliveries are never one.
    assert.ok(api.seen[0].params.meta.message_id, "the delivery must be identified");
    assert.equal(ui.seen.length, 0, "and nobody else may hear it");

    await review.client.callTool({
      name: "antiphon_send", arguments: { text: "for ui only", to: "ui" },
    });
    assert.ok(await waitFor(() => ui.seen.length === 1), "ui never heard review");
    assert.equal(ui.seen[0].params.meta.sender_alias, "review");
    assert.equal(api.seen.length, 1, "api must not hear it a second time");
    assert.notEqual(ui.seen[0].params.meta.message_id,
                    api.seen[0].params.meta.message_id,
                    "two deliveries are two attempts");

    // ---- Codex → Claude, unaddressed ----
    const refused = await build.client.callTool({
      name: "antiphon_send", arguments: { text: "to whom?" },
    });
    assert.ok(refused.isError, "two live Claude peers cannot be chosen between");
    const said = JSON.stringify(refused.content);
    assert.match(said, /ui/);
    assert.match(said, /api/);
    assert.equal(ui.seen.length + api.seen.length, 2, "and nothing was delivered");

    // ---- Claude → Codex, by name ----
    await ui.client.callTool({
      name: "reply_to_codex", arguments: { text: "for review", to: "review" },
    });
    const queued = readFileSync(stub.log, "utf8");
    assert.match(queued, new RegExp(`--thread ${SESSIONS.review}`),
      `the message must go to review's own session: ${queued}`);
    assert.match(queued, /\[from=ui id=/, "and say which Claude peer sent it");
    assert.ok(!queued.includes(SESSIONS.build), "never to the other one");

    // ---- Claude → Codex, unaddressed ----
    await assert.rejects(
      () => api.client.callTool({
        name: "reply_to_codex", arguments: { text: "to whom?" },
      }),
      (error) => {
        const refusal = String(error?.message || error);
        assert.match(refusal, /build/, "the refusal must name build");
        assert.match(refusal, /review/, "the refusal must name review");
        return true;
      },
      "two live Codex peers cannot be chosen between either",
    );
    assert.equal(readFileSync(stub.log, "utf8").split("\n").filter(Boolean).length,
      1, "and still only the one addressed message was queued");

    // ---- two leave, two carry on ----
    await ui.client.close();
    await build.client.close();
    open.splice(open.indexOf(ui.client), 1);
    open.splice(open.indexOf(build.client), 1);
    assert.ok(await waitFor(() =>
      !named("claude", "ui") && !named("codex", "build") &&
      !existsSync(socketFor(dir, "ui"))),
      "a session that leaves must take its claim and its socket with it");

    const survivors = registeredPeers(dir)
      .map((peer) => `${peer.kind} ${peer.name}`).sort();
    assert.deepEqual(survivors, ["claude api", "codex review"]);

    await review.client.callTool({
      name: "antiphon_send", arguments: { text: "still here", to: "api" },
    });
    assert.ok(await waitFor(() => api.seen.length === 2), "api stopped hearing");
    assert.equal(api.seen[1].params.meta.sender_alias, "review");

    await api.client.callTool({
      name: "reply_to_codex", arguments: { text: "so am i", to: "review" },
    });
    assert.match(readFileSync(stub.log, "utf8"), /\[from=api id=/,
      "and the survivor still signs its own name");
    console.log("four sessions route by name: ok");
  } finally {
    for (const client of open) await client.close().catch(() => {});
    for (const path of sockets) await rm(path, { force: true }).catch(() => {});
    for (const path of [dir, roots, stub.dir]) {
      await rm(path, { recursive: true, force: true }).catch(() => {});
    }
  }
}

async function everySessionSignsTheValidNameItWasGiven() {
  // Two sessions started as `ui`; exactly one wins the registry and owns the
  // channel. That decides reachability, not identity: both sessions were
  // explicitly named `ui`, so both must sign their own words `ui`. The losing
  // session is warned that it cannot receive there; silently renaming its words
  // `<unnamed>` would describe a configuration it does not have.
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

    const winnerQueue = readFileSync(stubs[0].log, "utf8");
    assert.match(winnerQueue, /\[from=ui id=/,
      "the session that holds `ui` signs itself `ui`");
    assert.ok(!winnerQueue.includes("reply_to=<unavailable>"),
      `the channel owner must remain addressable: ${winnerQueue}`);
    const loserQueue = readFileSync(stubs[1].log, "utf8");
    assert.match(loserQueue, /\[from=ui reply_to=<unavailable> id=/,
      "the loser keeps its identity but exposes no route to the winner's channel");
    assert.ok(!loserQueue.includes("[from=<unnamed> "),
      `a named session silently denied its own identity: ${loserQueue}`);

    await clients.pop().close();      // the loser leaves
    const held = registeredPeers(dir).filter((peer) => peer.name === "ui");
    assert.equal(held.length, 1, "the winner's record survives the loser's exit");
    assert.equal(held[0].address, sockets[0]);
    assert.ok(existsSync(sockets[0]), "and its socket still serves");
    console.log("identity is independent of channel ownership: ok");
  } finally {
    for (const client of clients) await client.close().catch(() => {});
    for (const path of sockets) await rm(path, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    for (const stub of stubs) {
      await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
    }
  }
}

// A `codex` that refuses, with more to say than the transport keeps. The
// success stub above cannot reach this road: what an agent is told when the
// host itself says no crosses a Python process, a stderr pipe and a
// 500-character slice before it becomes a tool error, and only the far end of
// that trip decides whether the sentence arrives whole.
async function makeRefusingCodexStub(said) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-refuse-"));
  writeFileSync(join(dir, "codex"),
    `#!/bin/sh\nprintf '%s' ${JSON.stringify(said)} >&2\nexit 1\n`,
    { mode: 0o755 });
  return dir;
}

async function aRefusedTransportTellsTheAgentWhereTheWordsTravel() {
  // The refusal this work was opened on, padded past the 200-character cut
  // `_queue_codex` applies: the worst case is a host with plenty to say, and it
  // is the case where the appended sentence would be the first thing lost.
  const observed = "direct app-server input is not allowed for unloaded spawned sub-agents";
  const noisy = `${observed} ${"detail ".repeat(70)}`;
  // Read out of the module rather than copied: a second hand-maintained literal
  // in a second language is exactly the drift `TO_DESCRIPTION` needs a contract
  // test for, and this arm is about the trip, not the wording.
  const guidance = execFileSync("python3", ["-c",
    "import sys; sys.path.insert(0, 'lib'); import antiphon; " +
    "sys.stdout.write(antiphon.TOOL_GUIDANCE.format(seen='only a tool-name line'))",
  ], { encoding: "utf8" });

  const dir = await mkdtemp(join(tmpdir(), "antiphon-refusal-"));
  const stubDir = await makeRefusingCodexStub(noisy);
  const sockets = [socketFor(dir, "")];
  const clients = [];
  try {
    liveCodexPeer(dir, "build", "300:build", CODEX_SESSION);
    const env = { ...process.env, ANTIPHON_CWD: dir, PATH: `${stubDir}:${process.env.PATH}` };
    delete env.ANTIPHON_NAME;
    const transport = new StdioClientTransport({
      command: "node", args: ["lib/channel.mjs"], env, stderr: "pipe",
    });
    const client = new Client({ name: "antiphon-refusal", version: "1.0.0" });
    await client.connect(transport);
    clients.push(client);

    await assert.rejects(
      () => client.callTool({
        name: "reply_to_codex", arguments: { text: "hi", to: "build" },
      }),
      (error) => {
        const refusal = String(error?.message || error);
        assert.match(refusal, /Failed to deliver reply to Codex: /);
        assert.ok(refusal.includes(observed),
          `the host's own reason must survive: ${refusal}`);
        assert.ok(refusal.endsWith(guidance),
          `the guidance must arrive whole, not sliced: ${refusal}`);
        return true;
      },
      "a transport refusal must tell the agent where its words still travel",
    );
    console.log("a refused transport names the passive page: ok");
  } finally {
    for (const client of clients) await client.close().catch(() => {});
    for (const path of sockets) await rm(path, { force: true }).catch(() => {});
    for (const path of [dir, stubDir]) {
      await rm(path, { recursive: true, force: true }).catch(() => {});
    }
  }
}

async function onlyOneUnnamedSessionGetsTheChannel(startTogether) {
  // Started together, both sessions can see the socket path free at the same
  // moment. Nothing after that point may let the loser bind: it would unlink
  // the winner's live socket, and the one reserved registry key would no longer
  // describe a reliably reachable server. The registry claim is atomic, so
  // exactly one gets through however the two interleave.
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
  let reply = "";
  try {
    assert.ok(await waitFor(() => existsSync(session.socketPath)), session.stderr());
    client.setEncoding("utf8");
    client.on("data", (chunk) => { reply += chunk; });
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
    const refusal = JSON.parse(reply);
    assert.equal(refusal.ok, false,
      `the half-open client must receive the refusal before close: ${reply}`);
    assert.match(refusal.error, /message too large: over 131072 bytes/,
      "the refusal must arrive as one complete JSON response, not a truncated write");

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
const startupClaimOutlivedShutdown = await aStartupClaimCannotOutliveSignalShutdown();
const postClaimBindOutlivedShutdown = await aCompletedClaimCannotBindAfterSignalShutdown();
const controlClaimOutlivedShutdown = await aControlClaimCannotOutliveEofShutdown();
assert.deepEqual(
  {
    startupClaimOutlivedShutdown,
    postClaimBindOutlivedShutdown,
    controlClaimOutlivedShutdown,
  },
  {
    startupClaimOutlivedShutdown: false,
    postClaimBindOutlivedShutdown: false,
    controlClaimOutlivedShutdown: false,
  },
  "shutdown must outlive both registry claims and socket acquisition",
);
await aLiveListenerReassertsItsOwnMissingEndpoint();
await anArbitrarySocketBinderIsNotAdopted();
await aReconnectRepairsTheLiveListenersMissingRecord();
await everySessionSignsTheValidNameItWasGiven();
await invalidConfiguredNamesAreNotEchoed();
await anAutomaticClaudeIdentityRequiresTheHookJoinBeforeSigning();
await explicitAndUnverifiedClaudeIdentitiesStayConservative();
await automaticClaudeIdentityAcceptsTheCanonicalUuidGrammar();
await fourLiveSessionsRouteByName();
await onlyOneUnnamedSessionGetsTheChannel(false);   // a second terminal, later
await onlyOneUnnamedSessionGetsTheChannel(true);    // both started together
console.log("per-peer sockets: ok");
await aRefusedTransportTellsTheAgentWhereTheWordsTravel();

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
  assert.deepEqual(tools.tools.map((tool) => tool.name),
    ["reply_to_codex", "antiphon_retrieve"]);
  const replyTool = tools.tools.find((tool) => tool.name === "reply_to_codex");
  const retrieveTool = tools.tools.find((tool) => tool.name === "antiphon_retrieve");
  assert.doesNotMatch(replyTool.description, /you can leave it out/,
    "there is no single-peer shortcut on the Codex side to promise");
  const schema = replyTool.inputSchema;
  assert.equal(schema.properties.to.type, "string");
  assert.deepEqual(schema.required, ["text"],
    "`to` is optional here only to preserve the project with no registered " +
    "Codex peer at all; requiring it would break every unnamed single pair");
  assert.deepEqual(retrieveTool.inputSchema.required, ["id"]);
  assert.match(retrieveTool.description, /invocation only/);
  assert.match(retrieveTool.description, /read-only/i);
  assert.match(retrieveTool.description, /antiphon retrieve/);

  // The Claude-facing retrieval tool crosses the real Node -> Python boundary.
  // Build its source catalog in an isolated HOME, then prove both a small exact
  // invocation and the bounded refusal for a value that cannot fit in MCP.
  const retrievalSource = "4eecac24-1c21-47ad-ab11-a650708f3098";
  const retrievalDir = join(
    projectDir, ".claude", "projects", projectDir.replace(/[^A-Za-z0-9]/g, "-"));
  mkdirSync(retrievalDir, { recursive: true });
  const smallBlock = {
    type: "tool_use", id: "toolu_node_small", name: "Read",
    input: { argument: "line\nsmall" }, caller: { type: "direct" },
  };
  const largeBlock = {
    type: "tool_use", id: "toolu_node_large", name: "Read",
    input: { argument: "z".repeat(9_000) }, caller: { type: "direct" },
  };
  writeFileSync(join(retrievalDir, `${retrievalSource}.jsonl`), `${JSON.stringify({
    type: "assistant", cwd: projectDir, timestamp: "2026-09-01T00:00:00Z",
    message: { content: [smallBlock, largeBlock] },
  })}\n`);
  execFileSync("python3", [join(process.cwd(), "lib", "antiphon.py"),
                           "sources", "scan"],
    { cwd: projectDir, env: mainEnv, encoding: "utf8" });
  const invocationId = (block) => execFileSync("python3", [
    "-c",
    "import json, sys\n" +
    "sys.path.insert(0, sys.argv[1])\n" +
    "import antiphon\n" +
    "b = json.load(sys.stdin)\n" +
    "print(antiphon._claude_invocation(b, sys.argv[2], 0, int(sys.argv[3])).public_id)\n",
    join(process.cwd(), "lib"), retrievalSource, String(block === largeBlock ? 1 : 0),
  ], { input: JSON.stringify(block), env: mainEnv, encoding: "utf8" }).trim();

  const smallId = invocationId(smallBlock);
  const smallRetrieved = await client.callTool({
    name: "antiphon_retrieve", arguments: { id: smallId },
  });
  assert.equal(smallRetrieved.isError, undefined);
  assert.deepEqual(JSON.parse(smallRetrieved.content[0].text).arguments,
    { argument: "line\nsmall" });
  const largeId = invocationId(largeBlock);
  const largeRetrieved = await client.callTool({
    name: "antiphon_retrieve", arguments: { id: largeId },
  });
  assert.equal(largeRetrieved.isError, true);
  assert.match(largeRetrieved.content[0].text,
    new RegExp(`antiphon retrieve ${largeId}`));
  assert.doesNotMatch(largeRetrieved.content[0].text, /zzzzzzzzzz/,
    "an oversized refusal carries no invocation content");

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
  // unnamed session leaves no routable peer record, so `review` cannot be shown
  // to be the only one there. Nothing is queued for it.
  await assert.rejects(
    () => client.callTool({ name: "reply_to_codex", arguments: { text: "bare" } }),
    /not all observable/,
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
  // that is not a name the registry would accept reaches the agent as the
  // reserved `<unnamed>` key — never as text it might read as a name to reply
  // to, and never as null: measured on Claude Code 2.1.251, the host validates
  // `meta.sender_alias` as a string and drops the whole notification on null
  // ("ProtocolError: Invalid params … expected string, received null") while
  // this server has already answered the sender `{ok:true}`. Every message
  // from an unnamed Codex — the default install — was lost that way.
  for (const claimed of ["Not A Name", "a b", "a]b", 42, null, ["ui"], "",
                         "a".repeat(40), "<unnamed>"]) {
    const pending = nextNotification();
    await sendToSocket({ content: `claim ${JSON.stringify(claimed)}`,
                         sender_alias: claimed });
    const seen = await pending;
    assert.equal(seen.params.meta.sender_alias, "<unnamed>",
      `an unusable alias must reach the agent as the sentinel: ${JSON.stringify(claimed)}`);
    assert.equal(typeof seen.params.meta.sender_alias, "string",
      "the host schema accepts only a string here");
  }

  // The sentinel is what the agent now reads in `sender_alias`, and the
  // instructions tell it to pass a *name* back as `to`. An agent that passes
  // the sentinel anyway must get the bare-reply outcome, not "no peer named
  // <unnamed>": the two refusals (or two deliveries) have to be word-for-word
  // the same, and both have to differ from a genuinely unknown name.
  const outcome = async (args) => {
    try {
      const result = await client.callTool({ name: "reply_to_codex", arguments: args });
      return result.content[0].text;
    } catch (error) {
      // A refusal surfaces as a tool error; its wording is the outcome.
      return String(error?.message || error);
    }
  };
  const bare = await outcome({ text: "sentinel probe" });
  const viaSentinel = await outcome({ text: "sentinel probe", to: "<unnamed>" });
  const unknown = await outcome({ text: "sentinel probe", to: "nobody" });
  assert.equal(viaSentinel, bare,
    "`to: \"<unnamed>\"` must be handled exactly like leaving `to` out");
  assert.notEqual(unknown, bare,
    "the premise: an unknown name is answered differently from a bare reply");
  console.log("the sentinel as a recipient means no recipient: ok");

  // ---- large attachments, end to end -------------------------------------
  // The one place the whole road is real at once: the real Python tool, the
  // real Unix socket, the real channel server, a real MCP notification — and a
  // file on disk this test verifies exactly as the envelope tells its reader
  // to, by hashing everything after the first blank line.
  const libDir = join(process.cwd(), "lib");

  function sendTool(text) {
    return JSON.parse(execFileSync("python3", [
      "-c",
      "import json, os, sys\n" +
      "sys.path.insert(0, sys.argv[1])\n" +
      "import antiphon\n" +
      "print(json.dumps(antiphon._send_tool(os.environ['ANTIPHON_CWD'],\n" +
      "                                     sys.stdin.read())))\n",
      libDir,
    ], { env: mainEnv, input: text, encoding: "utf8",
         maxBuffer: 64 * 1024 * 1024 }));
  }

  function parkedContent(path) {
    const raw = readFileSync(path);
    const blank = raw.indexOf("\n\n");
    assert.ok(blank > 0, "one header line, then a blank line");
    return { header: raw.subarray(0, blank).toString(),
             content: raw.subarray(blank + 2) };
  }

  const words = "x".repeat(400_000);          // over the cap, far under 8 MiB
  const pendingEnvelope = nextNotification();
  const parkedSend = sendTool(words);
  assert.match(parkedSend.content[0].text, /parked at/,
    "the sender is told its words were parked");
  const envelope = (await pendingEnvelope).params.content;
  assert.match(envelope, /^\[Antiphon attachment\] 400000 bytes from /,
    "the envelope names the size and the author");
  assert.ok(envelope.length < 2_000,
    "the transport carried the envelope, not the words");
  assert.ok(!envelope.includes("xxxxxxxxxx"), "and none of the words");

  const parkedPath = /parked at (\S+) —/.exec(envelope)[1];
  const parked = parkedContent(parkedPath);
  assert.equal(parked.content.toString(), words, "the file holds them exactly");
  assert.match(parked.header, /^\[Antiphon attachment from=<unnamed> id=/,
    "and says whose they are, in the file itself");
  const digest = createHash("sha256").update(parked.content).digest("hex");
  assert.ok(envelope.includes(`sha256 ${digest}`),
    "the envelope's hash verifies against the content, as its own rule says");
  assert.equal(statSync(parkedPath).mode & 0o777, 0o600, "0600");
  assert.equal(statSync(join(projectDir, ".antiphon", "messages")).mode & 0o777,
    0o700, "0700");

  // And the bridge keeps working: a small message straight after flows the way
  // it always did, through the same tool and the same socket.
  const pendingSmall = nextNotification();
  const smallSend = sendTool("and here is the short version");
  assert.match(smallSend.content[0].text, /^Delivered to the Claude Code/);
  assert.doesNotMatch(smallSend.content[0].text, /parked/);
  assert.equal((await pendingSmall).params.content,
    "and here is the short version");

  // The queue direction, through the real `reply_to_codex` tool and the stub
  // recorder. The size comes from the live bound rather than a constant: it is
  // `ARG_MAX` minus this shell's own environment, so a number written here
  // would be a claim about somebody else's machine.
  const queueLimit = Number(execFileSync("python3", [
    "-c",
    "import sys\n" +
    "sys.path.insert(0, sys.argv[1])\n" +
    "import antiphon\n" +
    "print(antiphon._queue_message_limit())\n",
    libDir,
  ], { env: mainEnv, encoding: "utf8" }).trim());
  assert.ok(queueLimit > 0 && queueLimit < 8 * 1024 * 1024,
    `a usable queue bound: ${queueLimit}`);

  const tooLongToExec = "y".repeat(queueLimit + 4_096);
  const replied = await client.callTool({
    name: "reply_to_codex", arguments: { text: tooLongToExec, to: "review" },
  });
  assert.match(replied.content[0].text, /review/);
  const queuedNow = readFileSync(queueLog, "utf8");
  assert.match(queuedNow,
    /\[Antiphon channel\] Claude: \[from=<unnamed> id=[0-9a-f-]{36}\] \[Antiphon attachment\]/,
    "the envelope keeps the prefix and the reply address in front of it");
  assert.ok(!queuedNow.includes("yyyyyyyyyy"),
    "and the words themselves never reached the argv");
  const queuedEnvelope =
    /(\[Antiphon attachment\][^\n]*)/.exec(queuedNow)[1];
  const queuedPath = /parked at (\S+) —/.exec(queuedEnvelope)[1];
  const queuedFile = parkedContent(queuedPath);
  assert.equal(queuedFile.content.length, tooLongToExec.length);
  assert.ok(queuedEnvelope.includes(
    `sha256 ${createHash("sha256").update(queuedFile.content).digest("hex")}`),
    "this direction's hash verifies the same way");

  // Two parked files, one per direction, and `status` sees exactly those.
  const store = readdirSync(join(projectDir, ".antiphon", "messages"));
  assert.equal(store.length, 2, `one per direction: ${store.join(", ")}`);
  const reported = execFileSync("python3", [
    join(libDir, "antiphon.py"), "status",
  ], { env: mainEnv, encoding: "utf8" });
  assert.match(reported, /Attachments:\s+2 parked, [\d,]+ bytes, oldest today/);
  for (const name of store) {
    assert.ok(!reported.includes(name), "status names no parked file");
  }
  assert.equal(readdirSync(join(projectDir, ".antiphon", "messages")).length, 2,
    "and status deleted nothing");
  console.log("large attachments end to end: ok");

  console.log("MCP channel integration: ok");
} finally {
  // Each step runs even if an earlier one throws. A close that fails would
  // otherwise leave a project directory and a stub `codex` behind on every run.
  await client.close().catch(() => {});
  // The server unlinks its own socket on a clean exit, so this only matters
  // when it did not get one — a killed process leaves the path behind, and a
  // hundred and eighty of them piled up in the shared temp directory before
  // anybody looked.
  await rm(socketPath, { force: true }).catch(() => {});
  await rm(projectDir, { recursive: true, force: true }).catch(() => {});
  await rm(stubDir, { recursive: true, force: true }).catch(() => {});
}

// --- Task 4: the bridge payload must say which claim this is -------------
// Initial and reassert carry different rules and today send an identical
// payload, so once an endpoint is pruned Python has nothing left to tell them
// apart. The mode travels in the payload, and an unknown one fails closed.
async function makeModeRecordingPython(identity) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-mode-python-"));
  const payloads = join(dir, "payloads.txt");
  const realPython = execFileSync("python3", [
    "-c", "import sys; print(sys.executable)",
  ], { encoding: "utf8" }).trim();
  writeFileSync(join(dir, "python3"), `#!${realPython}
import os
import sys

command = sys.argv[2] if len(sys.argv) > 2 else ""
if command == "claude_identity":
    print(os.environ["ANTIPHON_TEST_IDENTITY_RESULT"])
    raise SystemExit(0)
if command == "register_peer":
    body = sys.stdin.read()
    with open(os.environ["ANTIPHON_TEST_MODE_PAYLOADS"], "a") as stream:
        stream.write(body + "\\n")
    raise SystemExit(0)

real_python = os.environ["ANTIPHON_TEST_REAL_PYTHON"]
os.execv(real_python, [real_python, *sys.argv[1:]])
`, { mode: 0o755 });
  return {
    dir,
    payloads,
    env: {
      PATH: `${dir}:${process.env.PATH}`,
      ANTIPHON_TEST_MODE_PAYLOADS: payloads,
      ANTIPHON_TEST_IDENTITY_RESULT: JSON.stringify(identity),
      ANTIPHON_TEST_REAL_PYTHON: realPython,
    },
  };
}

async function automaticRegistrationDeclaresItsMode() {
  // The real values, taken from peers.auto_identity rather than recomputed:
  // the alias is lowercase base32 of the first 128 bits, and an alias that
  // does not validate would silently leave the channel on the explicit path
  // where no mode is sent at all.
  const session_id = "8261c119-2c20-4bf4-87ab-f152ac87dbda";
  const digest =
    "9aa9141f2a5c704b91ef1d2122ad75e67a1ca8be84b7fe119a6edeca9f0b6937";
  const alias = "auto-tkurihzklryexeppduqsfllv4y";
  const stub = await makeModeRecordingPython({
    alias, identity_digest: digest, session_id,
  });
  const dir = await mkdtemp(join(tmpdir(), "antiphon-mode-"));
  const session = spawnChannel(dir, undefined, stub.env);
  try {
    assert.ok(await waitFor(() => existsSync(stub.payloads)),
      `the channel never reached register_peer; stderr=${session.stderr()}`);
    const first = JSON.parse(readFileSync(stub.payloads, "utf8")
      .trim().split("\n")[0]);
    assert.equal(first.mode, "initial",
      "a startup claim is an initial claim, and must say so");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await automaticRegistrationDeclaresItsMode();

// --- Task 5: delivery linearizes at the listener --------------------------
// The retire control is best effort, so a sender can resolve A while A is
// current, the hook can then move the proof, and the sender can connect
// afterwards. The only point that closes that window is the listener's own
// check, taken on every inbound delivery before anything is emitted.
const STALE_A = "8261c119-2c20-4bf4-87ab-f152ac87dbda";
const STALE_A_ALIAS = "auto-tkurihzklryexeppduqsfllv4y";
const STALE_A_DIGEST =
  "9aa9141f2a5c704b91ef1d2122ad75e67a1ca8be84b7fe119a6edeca9f0b6937";
const STALE_B = "0199a1b2-2222-7000-8000-00000000000b";

function pythonBridge() {
  return execFileSync("python3", ["-c", "import sys; print(sys.executable)"],
    { encoding: "utf8" }).trim();
}

function runPeers(dir, code) {
  execFileSync(pythonBridge(), ["-c",
    `import sys; sys.path.insert(0, "lib"); import peers\n${code}`],
    { cwd: process.cwd(), env: { ...process.env, ANTIPHON_CWD: dir } });
}

async function boundSocketOf(session) {
  // The channel derives its own name, so the path is whatever it announces.
  await waitFor(() => /channel ready: (\S+)/.test(session.stderr()));
  const found = /channel ready: (\S+)/.exec(session.stderr());
  return found ? found[1] : null;
}

async function staleInboundSession(dir) {
  const stub = await makeAutomaticIdentityPython({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST,
    session_id: STALE_A,
  });
  const session = spawnChannel(dir, undefined, stub.env);
  return { session, stub };
}

async function aStaleInboundIsRefusedAsNoPeerAndEmitsNothing() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-stale-"));
  const { session, stub } = await staleInboundSession(dir);
  try {
    const bound = await boundSocketOf(session);
    assert.ok(bound && existsSync(bound),
      `channel never bound; stderr=${session.stderr()}`);
    // No hook has written this listener's session half, so this is UNREADY
    // rather than PROVED_STALE — the comment here used to claim the latter and
    // the fixture never built it. Both refuse with the same class, which is
    // what this test is about; the wording below is what tells them apart, and
    // `aRetiringListenerNamesItsAliasAndTheRemedy` builds the stale one.
    runPeers(dir, `
owner = peers.owner_key() or "1:v1:x"
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_B)},
                           peers.auto_identity(${JSON.stringify(STALE_B)})[1])
`);
    const before = session.stdout();
    const reply = await new Promise((resolve, reject) => {
      const socket = connect(bound);
      let out = "";
      socket.setEncoding("utf8");
      socket.on("connect", () => socket.end(JSON.stringify({
        content: "hello", sender_alias: "build",
      })));
      socket.on("data", (chunk) => { out += chunk; });
      socket.on("end", () => resolve(out ? JSON.parse(out) : null));
      socket.on("error", reject);
    });
    assert.equal(reply?.ok, false, "a stale listener must refuse");
    assert.equal(reply?.refusal_class, "no-peer",
      "classified, not a transport error: the peer that alias named is gone");
    assert.match(String(reply?.error), /not established yet/,
      `an unready listener says so in its own words: ${reply?.error}`);
    assert.ok(!session.stdout().slice(before.length)
      .includes("notifications/claude/channel"),
      "a stale inbound emits zero channel notifications");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

async function anUnreadyInboundRefusesWithoutRetiringAnything() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-unready-"));
  const { session, stub } = await staleInboundSession(dir);
  let socket = null;
  try {
    socket = await boundSocketOf(session);
    assert.ok(socket && existsSync(socket),
      `channel never bound; stderr=${session.stderr()}`);
    // No hook has run, so there is no proof at all: UNREADY, never destructive.
    const reply = await sendToSocketAt(socket, { content: "hi" });
    assert.equal(reply?.ok, false, "unready refuses");
    assert.ok(existsSync(socket), "an unready listener keeps its socket");
    assert.ok(existsSync(join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json")),
      "an unready listener keeps its endpoint: the next hook makes it ready");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

function sendToSocketAt(path, payload) {
  return new Promise((resolve, reject) => {
    const socket = connect(path);
    let out = "";
    socket.setEncoding("utf8");
    socket.on("connect", () => socket.end(JSON.stringify(payload)));
    socket.on("data", (chunk) => { out += chunk; });
    socket.on("end", () => resolve(out ? JSON.parse(out) : null));
    socket.on("error", reject);
  });
}

await aStaleInboundIsRefusedAsNoPeerAndEmitsNothing();
await anUnreadyInboundRefusesWithoutRetiringAnything();

// --- Task 9: an automatic route is private on this side too ----------------
// The channel prints its own refusals. Nothing here crosses back into Python to
// have them cleaned, so a shape only the Python redactor removes still reaches
// the terminal from here.
async function anAutomaticSessionNeverPrintsItsOwnRoute() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-private-"));
  const stub = await makeAutomaticIdentityPython({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST,
    session_id: STALE_A,
  });
  // A directory on the socket path: `unlink` fails with EPERM and the refusal
  // that follows is the one that used to name the route.
  const blocked = socketFor(dir, STALE_A_ALIAS);
  mkdirSync(blocked, { recursive: true });
  const session = spawnChannel(dir, undefined, stub.env);
  try {
    assert.ok(await waitFor(() => /could not clear|could not serve/.test(session.stderr())),
      `expected a refusal, got: ${session.stderr()}`);
    const words = session.stderr();
    assert.doesNotMatch(words, /antiphon-channel-[0-9a-f]+\.sock/,
      `an automatic peer's route is not a remedy: ${words}`);
    assert.match(words, /can still reply to Codex/,
      "the remedy beside it survives the redaction");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    await rm(blocked, { recursive: true, force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

async function anExplicitSessionKeepsTheRouteItCanActOn() {
  // The mirror case, and the reason redaction is not blanket: an operator who
  // typed the name can act on the path, and `remove it` needs to name what.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-explicit-private-"));
  const blocked = socketFor(dir, "ui");
  mkdirSync(blocked, { recursive: true });
  const session = spawnChannel(dir, "ui");
  try {
    assert.ok(await waitFor(() => /could not clear|could not serve/.test(session.stderr())),
      `expected a refusal, got: ${session.stderr()}`);
    assert.match(session.stderr(), /antiphon-channel-[0-9a-f]+\.sock/,
      "an explicit peer's path is actionable for it");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    await rm(blocked, { recursive: true, force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

await anAutomaticSessionNeverPrintsItsOwnRoute();
await anExplicitSessionKeepsTheRouteItCanActOn();

// --- Task 10: the retiring listener says which alias stopped answering ------
// "The identity moved" is true and unusable. Whoever reads this terminal, and
// whoever addressed the peer, both need the name that stopped resolving and the
// one remedy that fixes it — and the alias is the public half, so naming it
// costs nothing the privacy contract protects.
async function aRetiringListenerNamesItsAliasAndTheRemedy() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-retire-notice-"));
  const { session, stub } = await staleInboundSession(dir);
  let socket = null;
  try {
    socket = await boundSocketOf(session);
    assert.ok(socket && existsSync(socket),
      `channel never bound; stderr=${session.stderr()}`);
    // PROVED_STALE, not UNREADY: the hook half has to exist and agree with the
    // endpoint before a proof naming another session can outgrow it. The owner
    // comes from the endpoint the channel itself wrote, because this test
    // process and the channel need not walk to the same CLI root.
    runPeers(dir, `
import json, os
root = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${STALE_A_ALIAS}")
owner = json.load(open(os.path.join(root, "endpoint.json")))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}",
                    ${JSON.stringify(STALE_A)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_A)},
                           ${JSON.stringify(STALE_A_DIGEST)})
# The production rotation, not a hand-built state: it withdraws this peer's
# half and leaves the tombstone behind. Writing the proof directly would have
# skipped the withdrawal and measured a state no hook can produce.
peers.rotate_identity_proof(${JSON.stringify(dir)}, owner,
                            ${JSON.stringify(STALE_B)},
                            peers.auto_identity(${JSON.stringify(STALE_B)})[1])
`);
    const reply = await sendToSocketAt(socket, { content: "hi" });
    assert.equal(reply?.ok, false, "a proved-stale identity refuses");
    assert.match(String(reply?.error), new RegExp(STALE_A_ALIAS),
      `the sender is told which alias stopped answering: ${reply?.error}`);
    assert.match(String(reply?.error), /reconnect/i,
      "and the remedy travels with it");
    assert.ok(await waitFor(() => new RegExp(STALE_A_ALIAS).test(session.stderr())),
      `the terminal is told too: ${session.stderr()}`);
    assert.match(session.stderr(), /reconnect/i,
      "the terminal gets the same remedy");
    // The alias is public; nothing else about the identity is.
    assert.doesNotMatch(session.stderr(), new RegExp(STALE_A_DIGEST),
      "the digest is not public");
    assert.doesNotMatch(String(reply?.error), new RegExp(STALE_A),
      "nor is the host session id");
    // The spec's guarantee, end to end: the delivery attempt is its own
    // wakeup, so cleanup does not depend on a control that may never arrive.
    // The response is flushed first — a listener that withdrew before
    // answering would leave the sender with a closed socket and no reason.
    const endpoint = join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json");
    assert.ok(await waitFor(() => !existsSync(endpoint)),
      "a proved-stale listener withdraws its own endpoint");
    assert.ok(await waitFor(() => !existsSync(socket)),
      "and unlinks the socket nothing should reach any more");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await aRetiringListenerNamesItsAliasAndTheRemedy();

// --- the retire control, over real bytes ------------------------------------
// Every existing test of this path patches `_retire_control` away, so the wire
// shape was never exercised: Python sent an envelope the listener does not
// branch on, and the wakeup was answered as a malformed message instead. Its
// safety is not authentication — anyone who can reach a Unix socket can send
// this — it is that the listener decides by re-reading the proof itself.
async function theRetireControlIsRecognisedAndNonDestructive() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-retire-control-"));
  const { session, stub } = await staleInboundSession(dir);
  let socket = null;
  try {
    socket = await boundSocketOf(session);
    assert.ok(socket && existsSync(socket),
      `channel never bound; stderr=${session.stderr()}`);
    const endpoint = join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json");
    assert.ok(existsSync(endpoint), "the listener registered before the test");

    // No proof at all: UNREADY. A control must not retire this — the next hook
    // is about to make it ready, and that is the bootstrap case the verdict
    // exists to keep apart from a stale one.
    const ack = await sendToSocketAt(socket, {
      control: "antiphon.channel",
      version: 1,
      action: "identity-retire",
      alias: STALE_A_ALIAS,
      nonce: "n0nce_test-1",
    });
    // Recognised as a control — the ack shape and the echoed nonce say so —
    // but `ok` reports whether this listener acted, and it did not. Asserting
    // `ok:true` here pinned a contradiction: a success that changed nothing,
    // which a sender cannot tell from a wakeup that was honoured.
    assert.equal(ack?.action, "identity-retire-ack",
      `recognised as a control: ${JSON.stringify(ack)}`);
    assert.equal(ack?.nonce, "n0nce_test-1", "the nonce is echoed");
    assert.equal(ack?.verdict, "UNREADY", "and says why it did nothing");
    assert.equal(ack?.ok, false, "an UNREADY listener retires nothing");
    assert.notEqual(ack?.error, "content must be a non-empty string");
    assert.ok(existsSync(endpoint),
      "an UNREADY listener keeps its endpoint through a retire control");

    // A malformed control is refused as a control, not treated as content.
    const bad = await sendToSocketAt(socket, {
      control: "antiphon.channel",
      version: 1,
      action: "identity-retire",
      alias: STALE_A_ALIAS,
    });
    assert.equal(bad?.ok, false, "a missing nonce fails the shape check");
    assert.match(String(bad?.error), /control request/,
      `refused as a control: ${JSON.stringify(bad)}`);
    assert.ok(existsSync(endpoint), "and nothing was withdrawn");

    // Another peer's alias is not this listener's business.
    const other = await sendToSocketAt(socket, {
      control: "antiphon.channel",
      version: 1,
      action: "identity-retire",
      alias: "auto-yzmcrss6whnnsjxthq2pclz3l4",
      nonce: "n0nce_test-2",
    });
    assert.equal(other?.ok, false, "an alias this listener does not hold is refused");
    assert.ok(existsSync(endpoint), "and nothing was withdrawn");

    // Now the state a real rotation leaves. The control is an optimisation —
    // the first stale delivery would do this too — but it is the one that
    // stops an outgrown socket lingering until its process exits.
    runPeers(dir, `
import json, os
root = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${STALE_A_ALIAS}")
owner = json.load(open(os.path.join(root, "endpoint.json")))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}",
                    ${JSON.stringify(STALE_A)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_A)},
                           ${JSON.stringify(STALE_A_DIGEST)})
peers.rotate_identity_proof(${JSON.stringify(dir)}, owner,
                            ${JSON.stringify(STALE_B)},
                            peers.auto_identity(${JSON.stringify(STALE_B)})[1])
`);
    const stale = await sendToSocketAt(socket, {
      control: "antiphon.channel",
      version: 1,
      action: "identity-retire",
      alias: STALE_A_ALIAS,
      nonce: "n0nce_test-3",
    });
    assert.equal(stale?.ok, true, `answered: ${JSON.stringify(stale)}`);
    assert.equal(stale?.verdict, "PROVED_STALE",
      "the listener reached the verdict by reading the proof itself");
    assert.ok(await waitFor(() => !existsSync(endpoint)),
      "and only then withdrew, after its answer was flushed");
    // Withdrawal must be the last registry mutation this process makes. The
    // release happens before the socket is fully torn down, so a reassert that
    // lands in that window would re-create the record naming a listener on its
    // way out. Nothing may bring the endpoint back.
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await sendToSocketAt(socket, {
        control: "antiphon.channel",
        version: 1,
        action: "reassert",
        alias: STALE_A_ALIAS,
        nonce: `n0nce_revive-${attempt}`,
      }).catch(() => {});
      assert.ok(!existsSync(endpoint),
        `attempt ${attempt}: a retired listener must not republish itself`);
    }
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await theRetireControlIsRecognisedAndNonDestructive();

// --- an automatic listener must not read `null` as permission ---------------
// `null` from the shared verdict means "this contract does not govern that
// record". For a listener that has no automatic identity at all — an explicit
// peer — that is right, and delivery proceeds. For a listener that *does* hold
// an automatic digest it means something else entirely: the endpoint on disk
// no longer describes this process. The delivery gate treated both the same
// and emitted, which is the wrong-recipient delivery this whole repair exists
// to end. The prior test measured only that the string "null" is not "READY".
async function anAutomaticListenerRefusesWhenItsEndpointStopsDescribingIt() {
  for (const [name, mutation] of [
    ["endpoint is explicit-shaped", "record.pop('automatic', None)"],
    ["endpoint digest moved", "record['identity_digest'] = '0' * 64"],
  ]) {
    const dir = await mkdtemp(join(tmpdir(), "antiphon-null-verdict-"));
    const { session, stub } = await staleInboundSession(dir);
    let socket = null;
    try {
      socket = await boundSocketOf(session);
      assert.ok(socket && existsSync(socket),
        `${name}: channel never bound; stderr=${session.stderr()}`);
      const endpoint = join(dir, ".antiphon", "peers",
        `claude-${STALE_A_ALIAS}`, "endpoint.json");
      runPeers(dir, `
import json
path = ${JSON.stringify(endpoint)}
record = json.load(open(path))
${mutation}
json.dump(record, open(path, "w"))
`);
      const before = session.stdout();
      const reply = await sendToSocketAt(socket, { content: "hello" });
      assert.equal(reply?.ok, false,
        `${name}: an endpoint that does not describe this listener must refuse`);
      assert.equal(reply?.refusal_class, "no-peer", name);
      assert.ok(!session.stdout().slice(before.length)
        .includes("notifications/claude/channel"),
        `${name}: zero notifications are emitted`);
      // `null` is not PROVED_STALE and authorises nothing destructive.
      assert.ok(existsSync(endpoint), `${name}: nothing is withdrawn`);
      assert.ok(existsSync(socket), `${name}: the socket stays`);
    } finally {
      session.child.kill("SIGKILL");
      await waitForExit(session.child, 2_000);
      if (socket) await rm(socket, { force: true }).catch(() => {});
      await rm(dir, { recursive: true, force: true }).catch(() => {});
      await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
    }
  }
}

await anAutomaticListenerRefusesWhenItsEndpointStopsDescribingIt();

// --- two guarantees the plan names and nothing measured --------------------
// Both hold by construction — a configured name leaves `automaticIdentityDigest`
// null, and doctor's probe half-closes with an empty payload, which fails the
// content check long before any verdict. Construction changes; these do not.
async function neitherTheControlNorADoctorProbeTouchesAnExplicitPeer() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-explicit-auto-"));
  // The exact string a prior automatic identity would have used, configured by
  // hand. §4 devotes a paragraph to this: the alias grammar allows it, so the
  // control must not act on the address alone.
  const session = spawnChannel(dir, STALE_A_ALIAS);
  try {
    assert.ok(await waitFor(() => /channel ready/.test(session.stderr())),
      `channel never bound; stderr=${session.stderr()}`);
    const socket = socketFor(dir, STALE_A_ALIAS);
    const endpoint = join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json");
    assert.ok(existsSync(endpoint), "the explicit peer registered");

    const ack = await sendToSocketAt(socket, {
      control: "antiphon.channel",
      version: 1,
      action: "identity-retire",
      alias: STALE_A_ALIAS,
      nonce: "n0nce_explicit",
    });
    assert.equal(ack?.ok, false, "an explicit peer retires nothing");
    assert.notEqual(ack?.verdict, "PROVED_STALE",
      `an explicit peer has no automatic verdict: ${JSON.stringify(ack)}`);
    assert.ok(existsSync(endpoint), "and keeps its endpoint");

    // Doctor's probe: connect, half-close, expect an object with `ok`.
    const probe = await new Promise((resolve, reject) => {
      const client = connect(socket);
      let out = "";
      client.setEncoding("utf8");
      client.on("connect", () => client.end());
      client.on("data", (chunk) => { out += chunk; });
      client.on("end", () => resolve(out ? JSON.parse(out) : null));
      client.on("error", reject);
    });
    assert.equal(probe?.ok, false, "the probe is answered, not acted on");
    assert.ok(existsSync(endpoint),
      "a read-only diagnostic never retires a listener");
    assert.ok(existsSync(socket), "nor removes its socket");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    await rm(socketFor(dir, STALE_A_ALIAS), { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

await neitherTheControlNorADoctorProbeTouchesAnExplicitPeer();

// --- a retired listener stops claiming the socket it gave up ----------------
// `shutdown()` unlinks the path when this process owns it. Retirement gives the
// socket up outside that path, so leaving the flag set means the exit unlinks a
// path a successor may by then have bound — deleting a live listener's socket,
// which is what the flag exists to prevent.
async function retirementGivesUpSocketOwnership() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-own-"));
  const { session, stub } = await staleInboundSession(dir);
  let socket = null;
  try {
    socket = await boundSocketOf(session);
    runPeers(dir, `
import json, os
root = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${STALE_A_ALIAS}")
owner = json.load(open(os.path.join(root, "endpoint.json")))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}",
                    ${JSON.stringify(STALE_A)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_A)},
                           ${JSON.stringify(STALE_A_DIGEST)})
peers.rotate_identity_proof(${JSON.stringify(dir)}, owner,
                            ${JSON.stringify(STALE_B)},
                            peers.auto_identity(${JSON.stringify(STALE_B)})[1])
`);
    const reply = await sendToSocketAt(socket, { content: "hi" });
    assert.equal(reply?.ok, false, "the stale listener refuses");
    assert.ok(await waitFor(() => !existsSync(socket)),
      "and unlinks its own socket on the way out");

    // A successor binds the same path while the retired process is still up.
    const successor = createServer(() => {});
    await new Promise((resolve) => successor.listen(socket, resolve));
    assert.ok(existsSync(socket), "the successor bound the path");
    try {
      // Now end the retired process the ordinary way. Its shutdown must not
      // unlink a socket it no longer owns.
      session.child.stdin.end();
      assert.ok(await waitForExit(session.child, 5_000),
        `the retired session must exit; stderr=${session.stderr()}`);
      assert.ok(existsSync(socket),
        "the successor's socket survives the retired process's exit");
    } finally {
      await new Promise((resolve) => successor.close(resolve));
    }
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await retirementGivesUpSocketOwnership();

// --- retirement is the last registry mutation, measured in its own window ---
// The window is real and narrow: `retiring` is set, the server closes, and then
// the release runs as a subprocess. A structural pin on the flag would measure
// today's spelling; this holds the release open through the production registry
// seam and asks what a caller can actually do inside it.
async function nothingResurrectsTheEndpointWhileRetirementRuns() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-retire-window-"));
  const harness = await makeRetireRaceHarness({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST,
    session_id: STALE_A,
  });
  const session = spawnChannel(dir, undefined, harness.env);
  let socket = null;
  try {
    socket = await boundSocketOf(session);
    const endpoint = join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json");
    runPeers(dir, `
import json, os
root = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${STALE_A_ALIAS}")
owner = json.load(open(os.path.join(root, "endpoint.json")))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}",
                    ${JSON.stringify(STALE_A)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_A)},
                           ${JSON.stringify(STALE_A_DIGEST)})
peers.rotate_identity_proof(${JSON.stringify(dir)}, owner,
                            ${JSON.stringify(STALE_B)},
                            peers.auto_identity(${JSON.stringify(STALE_B)})[1])
`);
    const refusal = await sendToSocketAt(socket, { content: "hi" });
    assert.equal(refusal?.ok, false, "the stale listener refuses");

    // Provably inside the window: the release has started and is blocked.
    assert.ok(await waitFor(() => existsSync(harness.unregisterStarted)),
      `retirement never reached the release; stderr=${session.stderr()}`);
    assert.ok(existsSync(endpoint),
      "the endpoint is still there — the release has not completed");

    // The listener stopped accepting before it started releasing, so nothing
    // can even reach it inside the window. Asserting that the connect is
    // refused — rather than tolerating a failure — is what makes the ordering
    // load-bearing: released first and closed after, this connects, and the
    // reassert is answered by a process that is giving its endpoint away.
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const reply = await sendToSocketAt(socket, {
        control: "antiphon.channel",
        version: 1,
        action: "reassert",
        alias: STALE_A_ALIAS,
        nonce: `n0nce_window-${attempt}`,
      }).catch(() => "refused");
      assert.equal(reply, "refused",
        `attempt ${attempt}: a retiring listener accepts nothing; got `
        + JSON.stringify(reply));
    }

    writeFileSync(harness.releaseUnregister, "");
    assert.ok(await waitFor(() => !existsSync(endpoint)),
      "the release completes and the endpoint is withdrawn");
    // And stays withdrawn: nothing queued behind the release brings it back.
    for (let attempt = 0; attempt < 5; attempt += 1) {
      await sendToSocketAt(socket, {
        control: "antiphon.channel",
        version: 1,
        action: "reassert",
        alias: STALE_A_ALIAS,
        nonce: `n0nce_after-${attempt}`,
      }).catch(() => null);
      assert.ok(!existsSync(endpoint),
        `attempt ${attempt}: the endpoint must not come back`);
    }
  } finally {
    writeFileSync(harness.releaseUnregister, "");
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(harness.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await nothingResurrectsTheEndpointWhileRetirementRuns();

// --- a silent client must not hold retirement open -------------------------
// `close()` waits for every accepted connection to end. A peer that connects
// and says nothing therefore keeps a listener that has just learned it is
// stale holding its endpoint for the full idle timeout — measured at 30,124 ms
// without the destroy, against 79 ms with it. Nothing detected its removal.
async function aSilentClientDoesNotHoldRetirementOpen() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-silent-hold-"));
  const { session, stub } = await staleInboundSession(dir);
  let socket = null;
  let idle = null;
  try {
    socket = await boundSocketOf(session);
    const endpoint = join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json");
    runPeers(dir, `
import json, os
root = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${STALE_A_ALIAS}")
owner = json.load(open(os.path.join(root, "endpoint.json")))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}",
                    ${JSON.stringify(STALE_A)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_A)},
                           ${JSON.stringify(STALE_A_DIGEST)})
peers.rotate_identity_proof(${JSON.stringify(dir)}, owner,
                            ${JSON.stringify(STALE_B)},
                            peers.auto_identity(${JSON.stringify(STALE_B)})[1])
`);
    // A client that connects and then says nothing, accepted before the stale
    // delivery arrives.
    idle = connect(socket);
    await once(idle, "connect");

    const started = Date.now();
    const refusal = await sendToSocketAt(socket, { content: "hi" });
    assert.equal(refusal?.ok, false, "the stale listener refuses");
    assert.ok(await waitFor(() => !existsSync(endpoint)),
      `the endpoint must be withdrawn; stderr=${session.stderr()}`);
    const took = Date.now() - started;
    // Well inside CLIENT_IDLE_MS (30 s). Generous enough not to be a timing
    // test, tight enough that waiting for the idle timeout cannot pass it.
    assert.ok(took < 10_000,
      `retirement waited ${took} ms for a silent client to go away`);
  } finally {
    if (idle) idle.destroy();
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await aSilentClientDoesNotHoldRetirementOpen();

// --- an ordinary exit takes its own socket and nothing else ---------------
async function anOrdinaryShutdownRemovesItsOwnSocket() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-shutdown-own-"));
  const session = spawnChannel(dir, "ui");
  const socket = socketFor(dir, "ui");
  try {
    assert.ok(await waitFor(() => existsSync(socket)),
      `channel never bound; stderr=${session.stderr()}`);
    session.child.stdin.end();
    assert.ok(await waitForExit(session.child, 5_000), "the session exits");
    assert.ok(!existsSync(socket), "and takes its own socket with it");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

await anOrdinaryShutdownRemovesItsOwnSocket();

// --- the birth authority comes from the claim, not from the record ---------
// A listener that re-reads its own endpoint to learn what it published has no
// authority: the same bytes anyone could have changed answer both questions,
// and the comparison always agrees with itself. Driven through the real
// `claimPeer` and the real channel, with the record rewritten after the claim.
async function aRewrittenEndpointIsNotThisListenersOwn() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-birth-authority-"));
  const { session, stub } = await staleInboundSession(dir);
  let socket = null;
  try {
    socket = await boundSocketOf(session);
    const endpoint = join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json");
    // The hook half and a current proof, so the only thing left to decide the
    // verdict is whether this endpoint is the one the listener published.
    runPeers(dir, `
import json, os
root = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${STALE_A_ALIAS}")
path = os.path.join(root, "endpoint.json")
owner = json.load(open(path))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}",
                    ${JSON.stringify(STALE_A)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_A)},
                           ${JSON.stringify(STALE_A_DIGEST)})
# The record now names another process's birth. The pid still matches, which
# is exactly the recycled-number case the pairing exists for.
record = json.load(open(path))
record["process_birth"] = "v1:Thu Jan 1 00:00:00 1970"
json.dump(record, open(path, "w"))
`);
    const reply = await sendToSocketAt(socket, { content: "hi" });
    assert.equal(reply?.ok, false,
      `a rewritten endpoint is not this listener's own: ${JSON.stringify(reply)}`);
    assert.equal(reply?.refusal_class, "no-peer");
    // Refused, and not retired: a record this listener cannot claim is not a
    // record it may act destructively on either.
    assert.ok(existsSync(endpoint), "nothing is withdrawn");
    assert.ok(existsSync(socket), "and the socket stays");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(stub.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await aRewrittenEndpointIsNotThisListenersOwn();

// --- no authority, no delivery ---------------------------------------------
// The claim can succeed while the fingerprint of the process it named is
// unavailable — a platform where the process table cannot be read. Without an
// authority this listener cannot tell its own endpoint from one an earlier
// process left behind, and answering anyway is the fail-open the contract is
// written against.
async function aListenerWithoutItsFingerprintRefuses() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-no-authority-"));
  const harness = await makeBirthlessRegistryHarness({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST,
    session_id: STALE_A,
  });
  const session = spawnChannel(dir, undefined, harness.env);
  let socket = null;
  try {
    socket = await boundSocketOf(session);
    const endpoint = join(dir, ".antiphon", "peers",
      `claude-${STALE_A_ALIAS}`, "endpoint.json");
    runPeers(dir, `
import json, os
root = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${STALE_A_ALIAS}")
owner = json.load(open(os.path.join(root, "endpoint.json")))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}",
                    ${JSON.stringify(STALE_A)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(STALE_A)},
                           ${JSON.stringify(STALE_A_DIGEST)})
`);
    const before = session.stdout();
    const reply = await sendToSocketAt(socket, { content: "hi" });
    assert.equal(reply?.ok, false,
      `everything else is in order; only the authority is missing: ${JSON.stringify(reply)}`);
    assert.ok(!session.stdout().slice(before.length)
      .includes("notifications/claude/channel"),
      "and nothing is emitted");
    assert.ok(existsSync(endpoint), "nor withdrawn");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(harness.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await aListenerWithoutItsFingerprintRefuses();

// --- one predicate, both directions ----------------------------------------
// The fail-closed lived in the inbound gate alone, so a listener whose claim
// came back without a fingerprint refused everything sent to it and went on
// signing its replies with the alias it could no longer prove was its own.
// Driven through the real `reply_to_codex`, which is where signing is visible.
async function aListenerWithoutItsFingerprintDoesNotSignEither() {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-no-authority-sign-"));
  const codex = await makeCodexStub();
  const harness = await makeBirthlessRegistryHarness({
    alias: AUTO_ALIAS, identity_digest: AUTO_DIGEST, session_id: CODEX_SESSION,
  });
  const env = {
    ...process.env, ...harness.env, ANTIPHON_CWD: dir, HOME: dir,
    PATH: `${codex.dir}:${harness.env.PATH}`,
  };
  delete env.ANTIPHON_NAME;
  const transport = new StdioClientTransport({
    command: "node", args: ["lib/channel.mjs"], env, stderr: "pipe",
  });
  const client = new Client({ name: "antiphon-no-authority", version: "1.0.0" });
  try {
    liveCodexPeer(dir, "build", "300:build", CODEX_SESSION);
    await client.connect(transport);
    assert.ok(await waitFor(() => existsSync(endpointFor(dir, AUTO_ALIAS))),
      "the claim itself succeeds; only its fingerprint is missing");
    // Both halves and a current proof, written the way a real hook writes
    // them: everything a READY verdict needs except the one thing this
    // listener cannot obtain. Without the proof the raw verdict is UNREADY
    // anyway, and the test would pass whether or not signing consults the
    // gate — which is the shape it had first.
    runPeers(dir, `
import json, os
path = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers",
                    "claude-${AUTO_ALIAS}", "endpoint.json")
owner = json.load(open(path))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${AUTO_ALIAS}",
                    ${JSON.stringify(CODEX_SESSION)}, "/t/a.jsonl", owner,
                    ${JSON.stringify(AUTO_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner,
                           ${JSON.stringify(CODEX_SESSION)},
                           ${JSON.stringify(AUTO_DIGEST)})
`);
    await client.callTool({
      name: "reply_to_codex", arguments: { text: "unsigned", to: "build" },
    });
    const queued = readFileSync(codex.log, "utf8");
    assert.match(queued, /\[from=<unnamed> id=/,
      `a listener that cannot prove its endpoint is its own signs nothing: ${queued}`);
    assert.ok(!queued.includes(AUTO_ALIAS),
      "the alias stays private while the authority is missing");
  } finally {
    await client.close().catch(() => {});
    await rm(socketFor(dir, AUTO_ALIAS), { force: true }).catch(() => {});
    await rm(dir, { recursive: true, force: true }).catch(() => {});
    await rm(harness.dir, { recursive: true, force: true }).catch(() => {});
    await rm(codex.dir, { recursive: true, force: true }).catch(() => {});
  }
}

await aListenerWithoutItsFingerprintDoesNotSignEither();

// --- the fingerprint moves where the 0.3.x reader never looks ---------------
// A rolling upgrade leaves the published 0.3.x reader running for hours inside
// a live MCP server, and `channel.mjs` shells whatever Python is on disk on
// every registry call: a listener's in-memory Node and on-disk Python can
// disagree in either direction. These drive the real old reader and real
// mixed listeners rather than a model of either.

// The reader 0.3.3 shipped, loaded from the byte-exact fixture and run from a
// timezone three hours east of the canon, as a live pre-upgrade MCP server
// would run it. Returns what the child printed.
function runOldReader(dir, code) {
  return String(execFileSync(pythonBridge(), ["-c",
    `import importlib.util, json, os
spec = importlib.util.spec_from_file_location("old", os.path.join("test", "fixtures", "peers_0_3_3.py"))
old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
${code}`],
    { cwd: process.cwd(),
      env: { ...process.env, ANTIPHON_CWD: dir, TZ: "Europe/Istanbul", LC_ALL: "C" } }));
}

// A listener from an assembled lib/, with the automatic-identity stub.
function spawnMixedListener(lib, dir, stubEnv) {
  const env = { ...process.env, ...stubEnv, ANTIPHON_CWD: dir };
  delete env.ANTIPHON_NAME;
  const child = spawn("node", [join(lib, "channel.mjs")], { env, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  return { child, stderr: () => stderr };
}

async function boundSocketOfMixed(session) {
  await waitFor(() => /channel ready: (\S+)/.test(session.stderr()));
  return /channel ready: (\S+)/.exec(session.stderr())?.[1] ?? null;
}

async function thePublishedReaderLeavesALiveListenerRegistered() {
  // The reproduced P0, end to end: a real current listener has registered,
  // and the reader 0.3.3 shipped enumerates the registry from three hours
  // east. Before the fix it pruned the endpoint on every pass.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-old-reader-"));
  const session = spawnChannel(dir, "ui");
  try {
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1),
      `listener never registered: ${session.stderr()}`);
    const listed = runOldReader(dir,
      `print(json.dumps([p["name"] for p in old.read_peers(${JSON.stringify(dir)}, "claude")]))`);
    assert.deepEqual(JSON.parse(listed.trim()), ["ui"], "the old reader lists the live listener");
    assert.ok(existsSync(endpointFor(dir, "ui")), "and prunes nothing");
    assert.equal(registeredPeers(dir).length, 1, "the current reader agrees");
    console.log("the published reader leaves a live listener registered: ok");
  } finally {
    session.child.kill("SIGTERM");
    await waitForExit(session.child, 2_000);
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

async function anOldListenerOverAnUpgradedPythonIsRefusedNotToldItRecovered() {
  // Old Node in memory, new Python on disk — the upgrade. Chronology matters:
  // the old listener must bind against its own Python first (the new gate
  // refuses its initial claim too), then the Python files are replaced under
  // it, the endpoint is pruned, and a reassert is requested.
  const mixed = await materialiseLib({ node: "f0c529f", python: "f0c529f" });
  if (!mixed) { console.log("old listener over upgraded python: skipped (no git)"); return; }
  const dir = await mkdtemp(join(tmpdir(), "antiphon-old-node-"));
  const stub = await makeAutomaticIdentityPython({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST, session_id: STALE_A,
  });
  const session = spawnMixedListener(mixed.lib, dir, stub.env);
  let socket = null;
  try {
    socket = await boundSocketOfMixed(session);
    assert.ok(socket && existsSync(socket), `old listener never bound: ${session.stderr()}`);
    const endpoint = endpointFor(dir, STALE_A_ALIAS);
    assert.ok(existsSync(endpoint), "and registered under its own Python");
    // The hook half and a current proof, so governance is the only open question.
    runPeers(dir, `
import json, os
owner = json.load(open(${JSON.stringify(endpoint)}))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}", ${JSON.stringify(STALE_A)},
                    "/t/a.jsonl", owner, ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner, ${JSON.stringify(STALE_A)}, ${JSON.stringify(STALE_A_DIGEST)})
`);
    assert.ok(mixed.swapPython("worktree"), "the upgrade on disk");
    await rm(endpoint, { force: true });                  // what the old reader did
    const reply = JSON.parse(await sendTo(socket, JSON.stringify({
      control: "antiphon.channel", version: 1, action: "reassert",
      alias: STALE_A_ALIAS, nonce: "old-node-new-python",
    })));
    assert.equal(reply.ok, false, `an old listener must not claim recovery: ${JSON.stringify(reply)}`);
    assert.notEqual(reply.action, "reasserted");
    assert.ok(!existsSync(endpoint), "and publishes no endpoint it cannot govern");
    assert.match(session.stderr(), /predates the registry's fingerprint field[\s\S]*reconnect the Claude session/,
      `the remedy reaches the listener's log: ${session.stderr()}`);
    const words = JSON.parse(await sendTo(socket, JSON.stringify({ content: "hi" })));
    assert.equal(words?.ok, false, "and the words are refused, not delivered");
    console.log("an old listener over an upgraded python is refused, not told it recovered: ok");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    for (const p of [dir, stub.dir, mixed.dir]) await rm(p, { recursive: true, force: true }).catch(() => {});
  }
}

async function aCurrentListenerOverADowngradedPythonWithdrawsItsOwnEndpoint() {
  // New Node in memory, old Python on disk — the downgrade. The old registry
  // ignores the declaration, writes the old record and answers without the
  // acknowledgement; the listener must withdraw what was written and say why,
  // rather than bind and then refuse every delivery.
  const mixed = await materialiseLib({ node: "worktree", python: "f0c529f" });
  if (!mixed) { console.log("current listener over downgraded python: skipped (no git)"); return; }
  const dir = await mkdtemp(join(tmpdir(), "antiphon-new-node-"));
  const stub = await makeAutomaticIdentityPython({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST, session_id: STALE_A,
  });
  const session = spawnMixedListener(mixed.lib, dir, stub.env);
  try {
    await waitFor(() => /did not get the channel|channel ready/.test(session.stderr()));
    assert.match(session.stderr(), /registry on disk predates this listener's fingerprint field; the endpoint it wrote was withdrawn[\s\S]*Reinstall antiphon/,
      `the listener names the downgrade: ${session.stderr()}`);
    assert.ok(!existsSync(endpointFor(dir, STALE_A_ALIAS)), "and leaves no endpoint behind");
    assert.doesNotMatch(session.stderr(), /channel ready/, "and does not announce a channel it cannot govern");
    console.log("a current listener over a downgraded python withdraws its own endpoint: ok");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    for (const p of [dir, stub.dir, mixed.dir]) await rm(p, { recursive: true, force: true }).catch(() => {});
  }
}

// The four answers, on real files, without a listener.
async function anEndpointIsClassifiedNotCollapsed() {
  const { classifyEndpoint } = await import("../lib/identity.mjs");
  const dir = await mkdtemp(join(tmpdir(), "antiphon-classify-"));
  const path = join(dir, "endpoint.json");
  const me = { pid: process.pid, address: "/t/me.sock", name: "ui", identityDigest: null };
  const record = (over) => JSON.stringify({ kind: "claude", name: "ui", pid: process.pid,
    address: "/t/me.sock", started_at: 1, ...over });
  try {
    assert.equal(classifyEndpoint(path, me), "absent");
    writeFileSync(path, record({}));
    assert.equal(classifyEndpoint(path, me), "self");
    writeFileSync(path, record({ pid: process.pid + 1 }));
    assert.equal(classifyEndpoint(path, me), "other");
    writeFileSync(path, record({ address: "/t/other.sock" }));
    assert.equal(classifyEndpoint(path, me), "other");
    writeFileSync(path, record({ automatic: true, identity_digest: "0".repeat(64) }));
    assert.equal(classifyEndpoint(path, me), "other", "an automatic record is not an explicit listener's");
    writeFileSync(path, "{");
    assert.equal(classifyEndpoint(path, me), "unknown", "torn is not withdrawn");
    writeFileSync(path, record({}).slice(0, -1) + ', "pid": ' + process.pid + "}");
    assert.equal(classifyEndpoint(path, me), "unknown", "a duplicate key is not a record");
    writeFileSync(path, "[]");
    assert.equal(classifyEndpoint(path, me), "unknown");
    writeFileSync(path, record({}));
    chmodSync(path, 0o000);
    assert.equal(process.getuid?.() === 0 ? "unknown" : classifyEndpoint(path, me), "unknown",
      "unreadable is not withdrawn");
    chmodSync(path, 0o600);
    console.log("an endpoint is classified, not collapsed: ok");
  } finally {
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

// The same stub as makeAutomaticIdentityPython, plus one lie: `unregister_peer`
// exits 0 having removed nothing — the shape of a swallowed unlink error or a
// silent owner mismatch. The listener must not announce a withdrawal it did
// not verify.
async function makeSwallowingUnregisterPython(identity) {
  const stub = await makeAutomaticIdentityPython(identity);
  const wrapper = readFileSync(join(stub.dir, "python3"), "utf8").replace(
    'if command == "claude_identity":',
    'if command == "unregister_peer":\n    raise SystemExit(0)\nif command == "claude_identity":');
  writeFileSync(join(stub.dir, "python3"), wrapper, { mode: 0o755 });
  return stub;
}

async function aWithdrawalThatDidNotHappenIsNotAnnounced() {
  const mixed = await materialiseLib({ node: "worktree", python: "f0c529f" });
  if (!mixed) { console.log("unverified withdrawal: skipped (no git)"); return; }
  const dir = await mkdtemp(join(tmpdir(), "antiphon-no-withdraw-"));
  const stub = await makeSwallowingUnregisterPython({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST, session_id: STALE_A,
  });
  const session = spawnMixedListener(mixed.lib, dir, stub.env);
  try {
    await waitFor(() => /did not get the channel|channel ready/.test(session.stderr()));
    assert.match(session.stderr(), /could not be withdrawn; remove [^\n]*endpoint\.json by hand/,
      `the listener says the withdrawal failed: ${session.stderr()}`);
    assert.doesNotMatch(session.stderr(), /was withdrawn/, "and never claims it succeeded");
    assert.ok(existsSync(endpointFor(dir, STALE_A_ALIAS)), "the record the old registry wrote is still there — which is the point");
    assert.doesNotMatch(session.stderr(), /channel ready/);
    console.log("a withdrawal that did not happen is not announced: ok");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    for (const p of [dir, stub.dir, mixed.dir]) await rm(p, { recursive: true, force: true }).catch(() => {});
  }
}

await thePublishedReaderLeavesALiveListenerRegistered();
await anOldListenerOverAnUpgradedPythonIsRefusedNotToldItRecovered();
await aCurrentListenerOverADowngradedPythonWithdrawsItsOwnEndpoint();
await anEndpointIsClassifiedNotCollapsed();
await aWithdrawalThatDidNotHappenIsNotAnnounced();
