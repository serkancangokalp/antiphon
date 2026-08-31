import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, symlinkSync, writeFileSync } from "node:fs";
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
await onlyTheSessionThatWonTheNameSignsItsMessages();
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
