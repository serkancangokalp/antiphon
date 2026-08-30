#!/usr/bin/env node
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const [subcommand, ...rest] = process.argv.slice(2);

// `channel` is a long-lived stdio MCP server; it runs directly, never through Python.
const target = subcommand === "channel"
  ? { cmd: process.execPath, args: [join(here, "..", "lib", "channel.mjs")] }
  : { cmd: "python3", args: [join(here, "..", "lib", "antiphon.py"), ...(subcommand ? [subcommand] : []), ...rest] };

const child = spawn(target.cmd, target.args, { stdio: "inherit" });

// Forward signals rather than dying under them. Without this the wrapper exits
// first and leaves the channel server orphaned under PPID 1, still holding its
// socket — and the server never learns it was supposed to stop. Exit is left to
// the child's own exit handler below, so it gets to clean up first.
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => {
    if (!child.killed) child.kill(signal);
  });
}

child.on("exit", (code, signal) => process.exit(signal ? 1 : code ?? 0));
child.on("error", (error) => {
  process.stderr.write(`antiphon: ${error.message}\n`);
  process.exit(1);
});
