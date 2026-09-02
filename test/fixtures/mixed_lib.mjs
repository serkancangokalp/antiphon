import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

const NODE_FILES = ["channel.mjs", "identity.mjs"];
const PYTHON_FILES = ["antiphon.py", "peers.py"];

function bytesOf(repoRoot, source, name) {
  if (source === "worktree") return null;            // copy from disk instead
  try {
    return execFileSync("git", ["show", `${source}:lib/${name}`], { cwd: repoRoot });
  } catch {
    return undefined;                                // no git: caller skips by name
  }
}

function place(repoRoot, lib, source, names) {
  for (const name of names) {
    const blob = bytesOf(repoRoot, source, name);
    if (blob === undefined) return false;
    if (blob === null) copyFileSync(resolve(repoRoot, "lib", name), join(lib, name));
    else writeFileSync(join(lib, name), blob);
  }
  return true;
}

// A lib/ whose Node and Python halves come from two sources. `swapPython`
// replaces only the Python files in place — the upgrade or downgrade a
// running listener lives through. Returns null when git history is absent.
export async function materialiseLib({ node, python }, repoRoot = process.cwd()) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-mixed-lib-"));
  const lib = join(dir, "lib");
  mkdirSync(lib);
  if (!place(repoRoot, lib, node, NODE_FILES)) return null;
  if (!place(repoRoot, lib, python, PYTHON_FILES)) return null;
  symlinkSync(resolve(repoRoot, "node_modules"), join(dir, "node_modules"), "dir");
  return {
    dir, lib,
    swapPython: (source) => place(repoRoot, lib, source, PYTHON_FILES),
  };
}
