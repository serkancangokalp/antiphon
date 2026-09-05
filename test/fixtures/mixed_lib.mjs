import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const NODE_FILES = ["channel.mjs", "identity.mjs"];

// A linked worktree normally has no node_modules of its own. Resolve the
// dependency through this running fixture, then give a materialised lib the
// same package root instead of assuming `${repoRoot}/node_modules` exists.
function installedNodeModules() {
  let candidate = dirname(fileURLToPath(
    import.meta.resolve("@modelcontextprotocol/sdk/client/index.js")));
  while (basename(candidate) !== "node_modules") {
    const parent = dirname(candidate);
    if (parent === candidate) throw new Error("the MCP SDK has no node_modules ancestor");
    candidate = parent;
  }
  return candidate;
}

function git(repoRoot, args) {
  return execFileSync("git", args, { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"] })
    .toString().trim();
}

// What history this checkout carries: "complete", "shallow" (a `--depth`
// clone), or "none" (an extracted tarball, no repository at all). Only a
// complete history can be expected to hold a pinned commit.
export function checkoutHistory(repoRoot = process.cwd()) {
  try {
    return git(repoRoot, ["rev-parse", "--is-shallow-repository"]) === "true" ? "shallow" : "complete";
  } catch (error) {
    if (error?.code === "ENOENT") return "none";           // no git binary
    return "none";                                          // not a repository
  }
}

// Whether a pinned commit can be materialised from this checkout, and why
// not when it cannot. A shallow clone and a tarball cannot hold it and say
// so — the tests that need it skip, by name, and the rest of the suite runs.
// A checkout with complete history that lacks it has a rewritten history,
// which throws: after a squash the mixed-version contract must not pass by
// skipping.
const availability = new Map();
export function pinnedAvailability(source, repoRoot = process.cwd()) {
  if (source === "worktree") return { available: true };
  const key = `${repoRoot}\0${source}`;
  if (availability.has(key)) return availability.get(key);
  let verdict;
  try {
    git(repoRoot, ["cat-file", "-e", `${source}^{commit}`]);
    verdict = { available: true };
  } catch (error) {
    if (error?.code === "ENOENT") {
      verdict = { available: false, skip: "no git" };
    } else {
      const history = checkoutHistory(repoRoot);
      if (history === "complete") {
        throw new Error(`commit ${source} is not in this checkout, whose history is complete: `
          + String(error?.stderr || error?.message || error).trim());
      }
      verdict = { available: false,
                  skip: `commit ${source} unavailable: ${history === "shallow" ? "shallow clone" : "not a git checkout"}` };
    }
  }
  availability.set(key, verdict);
  return verdict;
}

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
// running listener lives through. Returns `{ skipped }` naming the reason
// when a source cannot be materialised here — no git binary, or a pinned
// commit this checkout cannot hold (`pinnedAvailability`); a commit that a
// complete history cannot show throws.
export async function materialiseLib({ node, python }, repoRoot = process.cwd()) {
  for (const source of [node, python]) {
    const pin = pinnedAvailability(source, repoRoot);
    if (!pin.available) return { skipped: pin.skip };
  }
  const dir = await mkdtemp(join(tmpdir(), "antiphon-mixed-lib-"));
  const lib = join(dir, "lib");
  mkdirSync(lib);
  if (!place(repoRoot, lib, node, NODE_FILES)) return { skipped: "no git" };
  if (!placePython(repoRoot, lib, python)) return { skipped: "no git" };
  symlinkSync(installedNodeModules(), join(dir, "node_modules"), "dir");
  return {
    dir, lib,
    swapPython: (source) => placePython(repoRoot, lib, source),
  };
}
