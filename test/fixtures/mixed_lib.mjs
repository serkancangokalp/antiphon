import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

const NODE_FILES = ["channel.mjs", "identity.mjs"];

// The Python half is every `lib/*.py` the source has — a fixed list would
// leave a module a newer commit added (`ledger.py`) behind, and the upgraded
// `antiphon.py` would then fail on its own import under the old listener.
function pythonFilesOf(repoRoot, source) {
  if (source === "worktree") {
    return readdirSync(resolve(repoRoot, "lib")).filter((name) => name.endsWith(".py")).sort();
  }
  try {
    return execFileSync("git", ["ls-tree", "--name-only", `${source}:lib`],
      { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"] })
      .toString().split("\n").filter((name) => name.endsWith(".py")).sort();
  } catch (error) {
    if (error?.code === "ENOENT") return undefined;
    throw new Error(`git ls-tree ${source}:lib failed: `
      + String(error?.stderr || error?.message || error).trim());
  }
}

// Replace the Python half in place: what the source has is written, and a
// module the source does not have is removed, so the tree is the source's.
function placePython(repoRoot, lib, source) {
  const names = pythonFilesOf(repoRoot, source);
  if (names === undefined) return false;
  for (const stale of readdirSync(lib)) {
    if (stale.endsWith(".py") && !names.includes(stale)) rmSync(join(lib, stale), { force: true });
  }
  return place(repoRoot, lib, source, names);
}

function bytesOf(repoRoot, source, name) {
  if (source === "worktree") return null;            // copy from disk instead
  try {
    return execFileSync("git", ["show", `${source}:lib/${name}`],
      { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"] });
  } catch (error) {
    // Only a missing git binary is a skip. A missing object — a rewritten
    // history, a squash that dropped the pinned commit — must fail the test
    // that depends on it, not turn it into a silent no-op.
    if (error?.code === "ENOENT") return undefined;
    throw new Error(`git show ${source}:lib/${name} failed: `
      + String(error?.stderr || error?.message || error).trim());
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
// running listener lives through. Returns null only when there is no git
// binary; a commit that cannot be shown throws.
export async function materialiseLib({ node, python }, repoRoot = process.cwd()) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-mixed-lib-"));
  const lib = join(dir, "lib");
  mkdirSync(lib);
  if (!place(repoRoot, lib, node, NODE_FILES)) return null;
  if (!placePython(repoRoot, lib, python)) return null;
  symlinkSync(resolve(repoRoot, "node_modules"), join(dir, "node_modules"), "dir");
  return {
    dir, lib,
    swapPython: (source) => placePython(repoRoot, lib, source),
  };
}
