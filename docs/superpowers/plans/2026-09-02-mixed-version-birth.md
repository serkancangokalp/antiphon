# Mixed-Version Endpoint Pruning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every behavioural red is written and observed red on the pre-fix tree before the first product edit; mutate each guard and watch the named test fail before calling it green.

**Goal:** A reader from the published 0.3.x line that is still running must leave a current endpoint record alone; the current reader keeps refusing a true pid/birth mismatch; a listener whose in-memory Node and on-disk Python disagree about the fingerprint field — in either direction — is refused honestly rather than told it recovered; and the Python resolver never routes an automatic endpoint the current listener would refuse.

**Architecture:** The canonical process fingerprint moves out of the field the old Python reader interprets (`birth`) into a sibling the old reader never selects (`process_birth`, generation in the value). Writers stop emitting `birth`/`birth_version`. One selector per language, driven by one grammar and one range check, prefers the sibling by key presence, accepts the 0.4.0-on-main `birth` + `birth_version: 1` pair as strict migration input, and treats everything else as unverifiable, never dead. The claim is a two-way capability: Node declares which fingerprint field its verdict reads, Python acknowledges it in the response, and a claim missing either half is refused (Python) or withdrawn (Node). `automatic_verdict` requires a current fingerprint before it can say READY, so the resolver and the listener agree on every fixture the parity suite can construct.

**Tech Stack:** Python 3.9+ standard library and `unittest`; Node.js ESM and `node:assert`; the byte-exact 0.3.3 `lib/peers.py` and per-commit `lib/` snapshots as test fixtures.

**Spec:** this document's *Design* section. Every claim in *Evidence* is measured, not inferred.

## Global Constraints

- Work only in `.worktrees/p0-mixed-version-birth` on branch `p0/mixed-version-birth`, from `f0c529f`. One writer owns it.
- Do not touch the live `.antiphon/` registry, cursors or sockets of the repository; every fixture lives under `tempfile.TemporaryDirectory()` or `mkdtemp`.
- Do not touch the user's untracked `docs/superpowers/{plans,specs}/2026-08-*` files.
- Python floor is 3.9 (`/usr/bin/python3` 3.9.6 is one of the verification roads).
- `package.json` `files` ships `bin/`, four `lib/` files, `README.md`, `BACKLOG.md`, `LICENSE` and nothing under `test/`; test fixtures are repo-only and never shipped.
- The registry is read by two languages: a rule added to one reader is a divergence until it is added to the other, and `test_readiness_parity_holds_across_every_fixture` drives both.
- The registration response's birth authority stays the operation's own return, never a read-back of the record (`test_the_birth_authority_never_comes_from_the_record_it_judges`).
- No production commit while any test is knowingly red. Reds are committed as reds, then one atomic product change turns them green.
- Stop at a locally reviewed exact commit. No merge, push, version bump or publish without the normal exact-SHA gates. The package on `main` is unpublished 0.4.0; this plan names no other version.

---

## Evidence

Measured on `f0c529f` (2026-09-02), in scratch copies, nothing live touched:

| Reader | Record | `_record_alive` | `read_peers` | endpoint.json after |
|---|---|---|---|---|
| `943da8a:lib/peers.py` (0.3.3; identical bytes to `a076723`, sha256 `4bb3ea14…a060b`), `TZ=Europe/Istanbul` | current (`birth` UTC, `birth_version: 1`) | False | `[]` | **deleted** |
| same old reader, same TZ | same record with `birth` removed and a sibling `process_birth` added | True | `[alias]` | present |
| same old reader | `owner` of the form `pid:v1:start` | n/a | `valid_owner_key` True | n/a |

The old `_record_alive` reads `birth` unconditionally and compares it against its own `ps lstart`, rendered in the reader's inherited timezone. The current writer (`lib/peers.py:1536-1541`) renders under `TZ=UTC`/`LC_ALL=C` and adds `birth_version: 1`, which the old reader never consults. A long-lived old MCP server therefore prunes every current endpoint it enumerates, repeatedly, until it is restarted. Codex measured the product shape live: `endpoint.json` vanished, status showed "current automatic identity with no channel yet", the stale MCP's `antiphon_send` refused the alias, and the current `send_to_claude` reassert restored the record and delivered once.

**Both halves of the mixed-process matrix.** `lib/channel.mjs` shells `python3 <here>/antiphon.py` on every registry call (`bridgeScript`, `channel.mjs:24`), so files replaced on disk give a running listener a *new* Python and keep its *old* `identity.mjs` in memory; a downgrade on disk gives the reverse.

- *Old Node, new Python.* The in-memory `automaticProofVerdict` (`identity.mjs:359-360`) compares `endpoint.record.birth` with `claimedBirth`. With the writer change alone, a reassert would write a record with only `process_birth`, `endpointDescribesListener` would accept it, the sender would re-resolve and deliver, and the old verdict would answer UNREADY and refuse the words. "Recovered, then refused" is the outcome this contract forbids. And because the refusal has to cover the initial claim as well as reassert (an old listener cannot govern the new record in either mode), a fixture that starts old Node against new Python never binds — the test chronology has to start old Node against old Python, prove it bound, and only then replace the Python files.
- *New Node, old Python.* Old `register_peer` ignores an unknown payload field, writes the `birth` + `birth_version` record, and answers an unversioned `birth`. Current Node's `runRegistryCall` (`channel.mjs:423-432`) returns true on any parseable answer, so the listener would bind, announce, and then refuse every delivery because `renderFingerprint(migration pair)` is `"v1:…"` and `claimedBirth` is not. The capability therefore has to be acknowledged in the response and required by Node.

**Runtime facts the design depends on, measured 2026-09-02:**

- The parity harness (`test_antiphon.py:17300-17335`) already passes the test process's pid and `_own_birth()` to Node as `listenerPid`/`listenerBirth`, and `_python` returns `NO-RECORD` when `read_peers` cannot enumerate the record. The existing fixture "endpoint records another process's birth" measures `('NO-RECORD', 'UNREADY')` and lives in the `ungovernable` bucket (`:17610-17740`), whose assertions are: Python says `NO-RECORD`, neither reader says `READY`, neither says `PROVED_STALE`. A strict current-birth mismatch stays there; every state Python can still enumerate belongs in `cases`, where the verdicts must be equal.
- `spelledFractional(record, field)` reads `record.scan` (`identity.mjs:176-178`); the `readRecord` wrapper carries `scan`, the bare record does not. `scan.duplicate` is a boolean and `readRecord` answers `invalid` when it is true (`identity.mjs:45-46`); Python `_read_record` parses with `_no_duplicate_keys` and returns `None` for any duplicate (`peers.py:1007-1037`). Duplicate keys are structural, decided before any selector runs, on both sides.
- `str.strip()` and `String.prototype.trim()` disagree on U+0085 (Python strips, JS keeps) and U+FEFF (JS strips, Python keeps); `\s`/`\S` differ per runtime likewise.
- `RECORD_CEILING` is 64 KiB on both sides. `json.loads` converts an integer token before any selector sees it; `/usr/bin/python3` 3.9.6 converts a 65,000-digit token (no `int_max_str_digits`), newer Pythons raise `ValueError` (which `_read_record` catches into `None`), and `JSON.parse` rounds it to a double. Without a bound the bucket a near-ceiling token lands in depends on the Python version.
- A purely lexical `[A-Z][a-z]{2} [A-Z][a-z]{2} [0-9]{1,2} …` accepts `Mon Jan 99 99:99:99 2026` and `Abc Xxx 0 00:00:00 0000`.
- The old `read_peers` never consults `valid_owner_key` for an addressed Claude endpoint (Codex measured `owner='definitely-invalid'` listed). The join road is `_session_address`, reached only through an addressless Codex endpoint.
- `_patch(path, drop=None, **over)` (`test_antiphon.py:17173`) pops one hashable `drop`; there is no `_raw_endpoint`/`_endpoint_json`. `CANONICAL_START` in `peers.py` will be two adjacent string literals, so a source grep for the whole pattern cannot find it.
- A backgrounded test run has no CLI root above it, `owner_key()` is None and 31 tests fail for that reason alone. Run suites in the foreground.

## Design

### The record

Current writer output for a fingerprintable process:

```json
{"kind": "claude", "name": "…", "pid": 77348, "address": "…",
 "started_at": 1788366537.8, "owner": "77000:v1:Wed Sep 2 16:13:00 2026",
 "process_birth": "v1:Wed Sep 2 16:13:13 2026"}
```

- `process_birth` is `"v" + PROCESS_FINGERPRINT_VERSION + ":" + <canonical start>`, rendered by `_render_fingerprint` from `PROCESS_FINGERPRINT_VERSION`, not from `OWNER_KEY_VERSION`: the two are equal today and the process generation is the authority for this field.
- `birth` and `birth_version` are no longer written. The old reader finds no `birth`, takes its documented pid-only road, and keeps the record.
- Writing a legacy `birth` for old readers' benefit is rejected: the old reader compares against its own timezone, which the writer cannot know.

### The grammar and the range check, shared by both readers

One ASCII grammar and one set of C-locale names, spelled identically in Python and JS and compared at runtime by the contract test (never grepped from source):

```
CANONICAL_START  = ([A-Z][a-z]{2}) ([A-Z][a-z]{2}) ([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4})
WEEKDAYS         = Sun Mon Tue Wed Thu Fri Sat
MONTHS           = Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec
day 1..31, hour 0..23, minute 0..59, second 0..59, year 1970..9999
GENERATION       = v([0-9]{1,9}):            # anchored at the start; the token is captured lexically
```

`canonical_start(text)` in Python and `canonicalStart(text)` in Node return the start when the full match holds *and* every group is in range; otherwise nothing. Nothing is trimmed, stripped or normalised before matching. This is the shape `_process_info` produces (`peers.py:1690-1696`), and it is what "writer-producible" means here. **Residual, stated:** a value that is shaped and in range but not this process's birth — corruption inside the space the writer could have produced, such as `Feb 31` — is indistinguishable from a recycled pid and authorises `dead`, exactly as an in-range but wrong owner key does today. Calendar validation is deliberately not mirrored: it widens the surface two runtimes must agree on for no reduction in that residual.

### Reading a fingerprint

`_fingerprint_of(record)` in Python and `fingerprintOf(wrapper)` in Node — the Node form takes the `readRecord` wrapper `{state, record, scan}` so lexical spelling is visible — return `("current", start)`, `("other", None)` or `None`:

1. If the key `process_birth` is present (`"process_birth" in record`; `Object.hasOwn(record, "process_birth")`), the answer comes from it alone and `birth` is never consulted:
   - not a string → `None`;
   - no `GENERATION` prefix → `None`;
   - generation token, converted from at most nine digits, equal to `PROCESS_FINGERPRINT_VERSION`: the remainder must pass `canonical_start`; if it does → `("current", start)`, else → `None` (malformed *current* input, never "another generation");
   - generation token different from the current one → `("other", None)`: unverifiable.
2. Else if `birth` is a string passing `canonical_start` and `birth_version` is the integer `1` spelled lexically as an integer (Python `type(v) is int and v == 1` after the bounded parse below; Node `record.birth_version === 1 && !spelledFractional(wrapper, "birth_version")`) → `("current", birth)`. Migration input: 0.4.0-on-main wrote it under the canon, so it stays strictly comparable and live-capable.
3. Else `None`.

Duplicate keys never reach the selector: both readers call the record invalid first.

**Bounded integers.** `_read_record` parses with `parse_int=_bounded_int`, which raises `ValueError` for a token longer than 20 digits, so the record is `None` on every Python and no conversion of a long token ever happens. Node keeps `JSON.parse` (a rounded double is never `=== 1`) and `readRecord` stays as is; the parity bucket for a near-ceiling token is then deterministic: Python `NO-RECORD`, Node non-READY.

**Liveness** (`_record_alive`): no fingerprint or `("other", …)` → pid only; `("current", start)` and `ps` unreadable → live; `("current", start)` and a different observed start → dead. `_record_liveness` (scheduling) maps `None`/`other` to `unknown` and `current` to `live`/`dead` as today; its cache key becomes `(pid, fingerprint)`.

### The composite readiness contract

`automatic_verdict` (`antiphon.py:516-561`) gains one rule, placed after the governance check and before the proof is consulted, mirroring where Node's structural block sits (`identity.mjs:349-363`, which runs after Node's own ungoverned short-circuit at line 334):

```
an automatic Claude record whose _fingerprint_of is not ("current", …) → UNREADY
```

A current listener always writes a current sibling when `ps` answers, and when `ps` does not answer its `claimedBirth` is null and it already fails closed as UNGOVERNED (`channel.mjs:531`). So a governed record without a current fingerprint is one no current listener can be serving as its own, and routing to it is exactly the "recovered, then refused" the contract forbids. UNREADY is non-destructive: nothing is pruned or retired over it, and a later reassert by a current listener rewrites the record.

Verdict classes stay as they are (`READY`, `UNREADY`, `UNKNOWN`, `PROVED_STALE`, `STRUCTURAL_INVALID`, `NO-RECORD` in the harness). The surface names the cause: doctor's per-peer note for an UNREADY record without a current fingerprint says so (Task 4).

Bucket assignment for every new parity fixture, with the verdict pair each must measure:

| Fixture | Bucket | Python | Node |
|---|---|---|---|
| sibling `v1:` other process's birth (+ rotated) | ungovernable | NO-RECORD (pruned) | UNREADY |
| migration pair, same birth | cases | READY | READY |
| migration pair, other birth | ungovernable | NO-RECORD | UNREADY |
| migration pair, `birth_version` spelled `1.0` / `1e0` | cases | UNREADY | UNREADY |
| migration pair, key spelled `birth_version` | cases | READY | READY (both decode the key; Node's scan maps by decoded name) |
| migration pair, `birth_version` duplicated | ungovernable | NO-RECORD (invalid record) | UNREADY (invalid record) |
| migration pair, `birth_version` a 65,000-digit token | ungovernable | NO-RECORD (bounded parse) | UNREADY |
| malformed sibling (`7`, `null`, `""`, `v1:`, `v1:garbage`) beside a valid conflicting pair | cases | UNREADY | UNREADY |
| sibling of a future generation (`v2:…`, `v999999999:…`) | cases | UNREADY | UNREADY |
| sibling shaped but out of range (`v1:Mon Jan 99 99:99:99 2026`) | cases | UNREADY | UNREADY |
| sibling / migration birth with a trailing NEL or leading BOM | cases | UNREADY | UNREADY |
| no fingerprint at all (sibling dropped, no pair) | cases | UNREADY | UNREADY |
| sibling `v1:` matching the listener's own birth (control) | cases | READY | READY |

If a measured pair differs from this table, the table is wrong or the code is; the disagreement is reported, not bent.

### The claim is a two-way capability

`channel.mjs` adds `"fingerprint_field": "process_birth"` to every `register_peer` payload (`channel.mjs:412-421`). `register_peer` (`antiphon.py:6080-6122`):

- refuses an automatic Claude claim (`identity_digest` present), in **both** modes, whose payload lacks `fingerprint_field == "process_birth"`, before `peers.register` runs:

  ```
  register_peer: this listener predates the registry's fingerprint field and cannot govern the endpoint it would publish; reconnect the Claude session (`/mcp` → reconnect antiphon) so a current listener claims it
  ```

- answers `{"birth": "<v1:start or null>", "fingerprint_field": "process_birth"}` on success.

`runRegistryCall` for `register_peer` with an automatic digest requires `answer.fingerprint_field === "process_birth"`; when it is missing the listener has just been registered by a Python that wrote the old record and cannot be governed, so Node immediately runs `unregister_peer` inside the same serialised `registryMutations` step, logs

```
antiphon: the registry on disk predates this listener's fingerprint field; the endpoint it wrote was withdrawn. Reinstall antiphon so both sides match, then reconnect the Claude session
```

and returns false. Startup then takes the existing "did not get the channel" road (`channel.mjs:993`), and a reassert answers the existing `{ok:false}`.

Scope: automatic Claude claims only. Explicit-name (`ANTIPHON_NAME`) and legacy listeners are ungoverned by the verdict (`automaticProofVerdict` returns `null` without a digest) and keep working in both directions. Codex registers from inside the Python process and never sends this payload.

Rejected alternative: writing the 0.4.0 pair for undeclared callers would keep an old listener governed but keep publishing a record the old Python reader prunes, and would need a caller-conditional response spelling. The contract prefers an honest refusal with a remedy over a working-until-pruned record.

### What does not change

- Owner keys, `owner_key_version`, `owner_generations_mixed`, the session↔endpoint join and `_owner_liveness`: already versioned in the value. Task 2 pins that the old reader validates a `v1` owner key and joins an addressless Codex endpoint to its session on it.
- The reassert control path's shape, the identity-retire path, shutdown ordering, `claimedBirth`'s fail-closed.

### Rolling window this fix leaves open, by name

Records already on disk in the `birth` + `birth_version: 1` spelling stay prunable by an old Python reader until their owner rewrites them (a Claude listener on its next reassert, a Codex MCP on restart). Doctor names that record as a risk (Task 4). An old in-memory automatic Claude listener is refused on its next claim and must be reconnected; a current listener over a downgraded Python withdraws its own endpoint and says why; until then each is unreachable, not misdescribed. Old readers judge current records by pid alone, as they did before 0.4.0; only current readers keep the recycled-pid check.

## File map

- `test/fixtures/peers_0_3_3.py` — byte-exact `943da8a:lib/peers.py` (31,994 bytes, sha256 `4bb3ea14ab9415f84a734b16472638c09d0acfd56eba83ee96d11d3ea29a060b`; `a076723` carries the same bytes and both are pinned). Repo-only.
- `test/fixtures/README.md`, `test/fixtures/__init__.py`.
- `test/fixtures/mixed_lib.mjs` — helper: materialises a `lib/` whose Node files and Python files come from independently chosen sources (a commit, or the working tree), with a `node_modules` symlink, and can swap the Python files in place afterwards. Skips by name when `git` is unavailable.
- `test/test_peers.py` — `RecycledPidTest` cross-version, precedence, grammar, bounded-integer and owner-join cases; `FrozenReaderFixtureTest`.
- `test/test_antiphon.py` — parity fixtures and the two raw-write helpers; scheduling `_claim` helper; doctor note tests.
- `test/test_contracts.py` — selector-shape and runtime-grammar contract.
- `test/channel.test.mjs` — old-reader-vs-live-listener E2E; old-Node/new-Python E2E; new-Node/old-Python E2E.
- `lib/peers.py` — grammar constants, `canonical_start`, `_bounded_int`, `_read_record`, `process_fingerprint`, `_render_fingerprint`, `_fingerprint_of`, `_record_alive`, `_record_liveness`, `register`.
- `lib/antiphon.py` — `automatic_verdict` fingerprint rule; `register_peer` gate and acknowledged response; doctor note.
- `lib/identity.mjs` — `CANONICAL_START`, `WEEKDAYS`, `MONTHS`, `PROCESS_FINGERPRINT_VERSION`, `canonicalStart`, `fingerprintOf`, `renderFingerprint`, `automaticProofVerdict`.
- `lib/channel.mjs` — payload field; acknowledgement check and withdrawal in `runRegistryCall`.
- `README.md`, `BACKLOG.md` — the contract in words.

---

### Task 1: Fixtures — the shipped 0.3.3 reader, and a `lib/` assembled from two sources

**Files:**
- Create: `test/fixtures/peers_0_3_3.py`, `test/fixtures/README.md`, `test/fixtures/__init__.py`, `test/fixtures/mixed_lib.mjs`
- Modify: `test/test_peers.py` (new class at the end; `import hashlib`, `import subprocess` if absent)

**Interfaces:**
- Produces: `test.fixtures.peers_0_3_3`; `OLD_READER_SHA256`, `OLD_READER_COMMITS = ("943da8a", "a076723")` in `test/test_peers.py`; from `mixed_lib.mjs`: `materialiseLib({node, python}) -> Promise<{dir, lib, swapPython(source)} | null>` where each source is a commit string or `"worktree"`.

- [ ] **Step 1: Write the failing tests**

```python
OLD_READER_SHA256 = "4bb3ea14ab9415f84a734b16472638c09d0acfd56eba83ee96d11d3ea29a060b"
OLD_READER_COMMITS = ("943da8a", "a076723")   # 0.3.3 as published; the same bytes one release earlier


class FrozenReaderFixtureTest(unittest.TestCase):
    """The cross-version tests drive the reader 0.3.3 actually shipped, not a
    model of it. The fixture is byte-exact, and when git history is at hand the
    blobs are compared too, so a hand edit to the fixture cannot quietly turn
    the old reader into a kinder one."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "peers_0_3_3.py")
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_the_fixture_is_the_shipped_reader(self):
        with open(self.FIXTURE, "rb") as stream:
            self.assertEqual(hashlib.sha256(stream.read()).hexdigest(),
                             OLD_READER_SHA256)

    def test_the_fixture_matches_both_pinned_blobs(self):
        for commit in OLD_READER_COMMITS:
            with self.subTest(commit=commit):
                try:
                    blob = subprocess.run(
                        ["git", "show", f"{commit}:lib/peers.py"],
                        capture_output=True, check=True, timeout=10,
                        cwd=self.REPO).stdout
                except (OSError, subprocess.SubprocessError):
                    self.skipTest("no git history in this checkout")
                self.assertEqual(hashlib.sha256(blob).hexdigest(),
                                 OLD_READER_SHA256)

    def test_the_fixture_imports_and_is_the_old_reader(self):
        from test.fixtures import peers_0_3_3 as old
        self.assertFalse(hasattr(old, "PROCESS_FINGERPRINT_VERSION"),
                         "0.3.3 had no fingerprint generation at all")
        self.assertTrue(callable(old._record_alive))
```

- [ ] **Step 2: Run to verify it fails**

Run: `/usr/bin/python3 -m unittest test.test_peers.FrozenReaderFixtureTest -v`
Expected: FAIL/ERROR — `FileNotFoundError` and `ModuleNotFoundError: test.fixtures`.

- [ ] **Step 3: Create the fixture byte-exact and the mixed-lib helper**

```sh
mkdir -p test/fixtures
git show 943da8a:lib/peers.py > test/fixtures/peers_0_3_3.py
: > test/fixtures/__init__.py
shasum -a 256 test/fixtures/peers_0_3_3.py   # 4bb3ea14…a060b
```

`test/fixtures/README.md`:

```markdown
# Frozen readers

`peers_0_3_3.py` is `lib/peers.py` exactly as commit `943da8a` (npm 0.3.3)
shipped it — the same bytes as `a076723` — and the last reader that
interpreted an endpoint's `birth` with no generation marker. It is imported
only by tests, never by product code, and `FrozenReaderFixtureTest` refuses
any byte that differs from those blobs. It exists because a rolling upgrade
leaves that reader running for hours inside a live MCP server, and a test
that models it instead of running it proved nothing. `package.json` `files`
excludes `test/`, so none of this ships.

`mixed_lib.mjs` is a helper, not a fixture file: it assembles a `lib/` whose
Node files and Python files come from independently chosen sources (a commit
or the working tree) and can swap the Python files afterwards. That is what a
running listener sees across an upgrade or a downgrade on disk: the Node it
loaded stays in memory, the Python it shells is whatever is on disk now.
```

`test/fixtures/mixed_lib.mjs`:

```js
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `/usr/bin/python3 -m unittest test.test_peers.FrozenReaderFixtureTest -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```sh
git add test/fixtures test/test_peers.py
git commit -m "Test fixtures: the 0.3.3 registry reader byte-exact, and a lib assembled from two sources"
```

---

### Task 2: Every red, observed red on the pre-fix tree, before any product edit

**Files:**
- Modify: `test/test_peers.py` (`RecycledPidTest`)
- Modify: `test/test_antiphon.py` (`ReadinessParityTest`: two helpers beside `_patch_endpoint` at `:17259`; `cases` and `ungovernable` dicts; the source-activity `_claim` helper `:12433-12448`)
- Modify: `test/test_contracts.py` (beside `test_the_birth_authority_never_comes_from_the_record_it_judges`, `:1045`)
- Modify: `test/channel.test.mjs` (after `aLiveListenerReassertsItsOwnMissingEndpoint`, `:629`; import `materialiseLib`)

**Interfaces:**
- Consumes: `test.fixtures.peers_0_3_3`; `materialiseLib`; existing helpers `RecycledPidTest._register/_read/_ps/LIVE/RECYCLED/UUID`, `ReadinessParityTest._proof/_withdrawn/_patch_endpoint/_write/_read/_own_birth/_both`, `spawnChannel`, `makeAutomaticIdentityPython`, `registeredPeers`, `endpointFor`, `runPeers`, `sendTo`, `waitFor`, `waitForExit`, `STALE_A*`.
- Produces: the contract Task 3 makes green. Names Task 3 must define: `peers.CANONICAL_START`, `peers.WEEKDAYS`, `peers.MONTHS`, `peers.canonical_start(text)`, `peers._bounded_int(token)`, `peers.INTEGER_TOKEN_CEILING`, `peers.process_fingerprint(pid)`, `peers._render_fingerprint(start)`, `peers._fingerprint_of(record)`; `identity.CANONICAL_START`, `identity.WEEKDAYS`, `identity.MONTHS`, `identity.PROCESS_FINGERPRINT_VERSION`, `identity.canonicalStart(text)`, `identity.fingerprintOf(wrapper)`, `identity.renderFingerprint(fp)`; payload key `fingerprint_field`; response key `fingerprint_field`.

- [ ] **Step 1: Python unit reds in `RecycledPidTest`**

Replace the two fingerprint assertions of `test_a_record_carries_the_fingerprint_of_the_process_it_names` with `self.assertEqual(record["process_birth"], f"v1:{self.LIVE}")`, keep its `started_at` assertion, and add:

```python
    # ---- a reader from the published 0.3.x line is still running ----

    LIVE_LOCAL = "Sat Aug 30 04:00:00 2026"   # LIVE rendered three hours east

    @staticmethod
    def _old_ps(old, start):
        return patch.object(old, "_process_info",
                            return_value=("1", start, "node server.js"))

    def _rewrite(self, project, mutate):
        path = peers._peer_file(project, "claude", "ui")
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
        mutate(record)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(record, stream)

    def _raw(self, project, text):
        with open(peers._peer_file(project, "claude", "ui"), "w",
                  encoding="utf-8") as stream:
            stream.write(text)

    def _listed(self, project, start):
        with self._ps(start):
            return [p["name"] for p in peers.read_peers(project, "claude")]

    def test_the_published_reader_leaves_a_current_record_alone(self):
        """The reproduced P0. A 0.3.x MCP server that started before the
        upgrade keeps reading the registry with the reader it loaded, which
        compares `birth` against its own timezone's `ps`. It must find nothing
        to compare — and so keep the record on its pid alone — rather than
        prune a live listener every time it enumerates."""
        from test.fixtures import peers_0_3_3 as old
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            with self._old_ps(old, self.LIVE_LOCAL):
                listed = old.read_peers(project, "claude")
            self.assertEqual([p["name"] for p in listed], ["ui"])
            self.assertTrue(os.path.exists(
                peers._peer_file(project, "claude", "ui")),
                "the old reader must not prune what it cannot judge")

    def test_a_record_carries_its_fingerprint_where_the_old_reader_never_looks(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            record = self._read(project)
        self.assertEqual(record["process_birth"], f"v1:{self.LIVE}")
        self.assertNotIn("birth", record)
        self.assertNotIn("birth_version", record)

    def test_the_published_reader_validates_a_versioned_owner_and_joins_on_it(self):
        """The join field already carried its generation in the value. Pin
        both halves of that on the old reader directly: the validator accepts
        the spelling, and an addressless Codex endpoint joins its session on
        it. (`read_peers` never consults the validator for an addressed Claude
        endpoint, so listing one proves nothing about owners.)"""
        from test.fixtures import peers_0_3_3 as old
        owner = f"300:v1:{self.LIVE}"
        self.assertTrue(old.valid_owner_key(owner))
        with tempfile.TemporaryDirectory() as project:
            with self._ps(self.LIVE):
                ok, detail = peers.register(project, "codex", "x", None,
                                            pid=os.getpid(), owner_key=owner)
            self.assertTrue(ok, detail)
            peers.write_session(project, "codex", "x", self.UUID,
                                "/t/x.jsonl", owner)
            with open(peers._peer_file(project, "codex", "x"),
                      encoding="utf-8") as stream:
                endpoint = json.load(stream)
            self.assertEqual(old._session_address(project, endpoint), self.UUID)

    # ---- the current reader keeps the strict check ----

    def test_the_current_reader_still_prunes_a_recycled_pid(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            self.assertEqual(self._listed(project, self.RECYCLED), [])
            self.assertFalse(os.path.exists(
                peers._peer_file(project, "claude", "ui")))

    def test_a_migration_spelling_is_read_strictly_and_never_as_absent(self):
        """What 0.4.0-on-main wrote: `birth` under the UTC canon beside the
        integer `birth_version: 1`, no sibling. Migration input: a matching
        birth is live, a different one is dead, and the missing sibling is
        evidence of nothing."""
        def to_migration(record):
            del record["process_birth"]
            record["birth"] = self.LIVE
            record["birth_version"] = 1
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            self._rewrite(project, to_migration)
            self.assertEqual(self._listed(project, self.LIVE), ["ui"])
            self.assertEqual(self._listed(project, self.RECYCLED), [])

    def test_a_present_sibling_is_the_only_thing_consulted(self):
        """Precedence is by key presence, not by type. A malformed sibling —
        including a malformed *current* one — beside a valid, conflicting
        migration pair yields no fingerprint at all: pid only, and the pair is
        never read."""
        for bad in ("", "v1:", "v1:garbage", "1:Sat Aug 30 01:00:00 2026",
                    "v0:x", 7, None, [], {},
                    "v1:" + self.LIVE + "\u0085", "\ufeffv1:" + self.LIVE):
            def conflict(record, bad=bad):
                record["process_birth"] = bad
                record["birth"] = self.LIVE          # valid pair, would be strict
                record["birth_version"] = 1
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as project:
                self._register(project, self.LIVE)
                self._rewrite(project, conflict)
                self.assertEqual(self._listed(project, self.RECYCLED), ["ui"])
                self.assertIsNone(peers._fingerprint_of(self._read(project)))

    def test_a_future_generation_is_unverifiable_not_dead(self):
        for sibling in ("v2:2026-08-30T01:00:00Z", "v999999999:" + self.LIVE):
            with self.subTest(sibling=sibling), \
                    tempfile.TemporaryDirectory() as project:
                self._register(project, self.LIVE)
                self._rewrite(project, lambda r: r.update(process_birth=sibling))
                self.assertEqual(self._listed(project, self.RECYCLED), ["ui"])
                self.assertEqual(peers._fingerprint_of(self._read(project)),
                                 ("other", None))

    def test_a_long_integer_token_never_reaches_a_conversion(self):
        """A near-ceiling digit run in the raw record: the bounded parser
        refuses it before `int()` sees it, on 3.9 as on any later Python, and
        the record is not one. Timed, because 3.9.6 would otherwise convert
        it — slowly, and to a value the selector must then refuse."""
        digits = "9" * (peers.RECORD_CEILING - 200)
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            self._raw(project, '{"kind": "claude", "name": "ui", "pid": %d, '
                      '"address": "/tmp/ui.sock", "started_at": 1.0, '
                      '"birth": "%s", "birth_version": %s}'
                      % (os.getpid(), self.LIVE, digits))
            started = time.monotonic()
            record = peers._read_record(peers._peer_file(project, "claude", "ui"))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertIsNone(record, "a record with an unbounded integer is not a record")
            self.assertEqual(self._listed(project, self.RECYCLED), [])
        self.assertEqual(peers._bounded_int("1"), 1)
        self.assertEqual(peers._bounded_int("9" * 20), 10 ** 20 - 1)
        with self.assertRaises(ValueError):
            peers._bounded_int("9" * 21)

    def test_the_grammar_is_the_writers_own_shape_and_in_range(self):
        for start in (self.LIVE, "Wed Sep 2 16:13:13 2026", "Thu Jan 1 00:00:00 1970"):
            with self.subTest(start=start):
                self.assertEqual(peers.canonical_start(start), start)
                self.assertEqual(peers._fingerprint_of({"process_birth": "v1:" + start}),
                                 ("current", start))
                self.assertEqual(peers._fingerprint_of(
                    {"birth": start, "birth_version": 1}), ("current", start))
        for start in ("Wed Sep  2 16:13:13 2026",       # padded day: the old 24-column slice
                      "Çar Eyl 2 16:13:13 2026",        # a locale
                      "Mon Jan 99 99:99:99 2026",       # shaped, out of range
                      "Abc Xxx 0 00:00:00 0000",        # shaped, no such names
                      "Sat Aug 30 01:00:00 1969",       # before the epoch
                      "Sat Aug 30 24:00:00 2026", "Sat Aug 30 01:60:00 2026",
                      "Sat Aug 30 01:00:60 2026", "Sat Aug 0 01:00:00 2026",
                      "Sat Aug 32 01:00:00 2026",
                      self.LIVE + " ", " " + self.LIVE,
                      self.LIVE + "\u0085", "\ufeff" + self.LIVE, ""):
            with self.subTest(start=start):
                self.assertIsNone(peers.canonical_start(start))
                self.assertIsNone(peers._fingerprint_of({"process_birth": "v1:" + start}))
                self.assertIsNone(peers._fingerprint_of(
                    {"birth": start, "birth_version": 1}))
        for version in (True, 1.0, "1", 2, None):
            with self.subTest(version=version):
                self.assertIsNone(peers._fingerprint_of(
                    {"birth": self.LIVE, "birth_version": version}))

    def test_the_response_spelling_is_the_records_spelling(self):
        with self._ps(self.LIVE):
            self.assertEqual(peers.process_fingerprint(os.getpid()),
                             f"v1:{self.LIVE}")
        with patch.object(peers, "_process_info", return_value=None):
            self.assertIsNone(peers.process_fingerprint(os.getpid()))
```

`RecycledPidTest.UUID` exists (`test_peers.py:570`). Add `import time` if absent. If `peers.register` refuses `address=None` for Codex, read `register`'s address handling (`peers.py:1440-1465`) and use the call the existing addressless-Codex tests use.

- [ ] **Step 2: Parity reds**

Two helpers beside `_patch_endpoint` (`test_antiphon.py:17259`):

```python
    def _endpoint_path(self, project, alias):
        return os.path.join(antiphon.peers.peer_dir(project, "claude", alias),
                            "endpoint.json")

    def _rewrite_endpoint_text(self, project, alias, edit):
        """Rewrite the endpoint as text, for spellings `json.dump` cannot
        produce: `1.0`, `1e0`, an escaped key, a duplicated key, a 65,000-digit
        token. `edit` maps the current text to the new text."""
        path = self._endpoint_path(project, alias)
        with open(path, encoding="utf-8") as stream:
            text = stream.read()
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(edit(text))

    def _migration_text(self, birth):
        """Turn a current record's text into the 0.4.0 spelling with the given
        birth, leaving the closing brace off for a caller to append a spelling
        of `birth_version` after."""
        def edit(text):
            record = json.loads(text)
            record.pop("process_birth", None)
            record["birth"] = birth
            return json.dumps(record)[:-1]
        return edit
```

Move the two existing birth fixtures ("endpoint records another process's birth", "rotated, endpoint records another birth") out of wherever they sit into `ungovernable` under the sibling spelling (they measure `NO-RECORD`/`UNREADY` today and keep doing so), and add to `cases`:

```python
            # --- the fingerprint contract: both readers, one selector ---
            "sibling is the listener's own birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v1:" + self._own_birth())),
            "migration pair, same birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop="process_birth",
                                     birth=self._own_birth(), birth_version=1)),
            "migration pair, version spelled 1.0": lambda p, a: (
                self._proof(p, self.A),
                self._rewrite_endpoint_text(p, a, lambda t:
                    self._migration_text(self._own_birth())(t) + ', "birth_version": 1.0}')),
            "migration pair, version spelled 1e0": lambda p, a: (
                self._proof(p, self.A),
                self._rewrite_endpoint_text(p, a, lambda t:
                    self._migration_text(self._own_birth())(t) + ', "birth_version": 1e0}')),
            "migration pair, version key escaped": lambda p, a: (
                self._proof(p, self.A),
                self._rewrite_endpoint_text(p, a, lambda t:
                    self._migration_text(self._own_birth())(t) + ', "birth\\u005fversion": 1}')),
            "malformed sibling beside a conflicting valid pair (7)": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth=7,
                                     birth=self._own_birth(), birth_version=1)),
            "malformed sibling beside a conflicting valid pair (null)": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth=None,
                                     birth=self._own_birth(), birth_version=1)),
            "malformed current sibling beside a valid pair (v1:garbage)": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v1:garbage",
                                     birth=self._own_birth(), birth_version=1)),
            "sibling of a future generation": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v2:whatever")),
            "sibling of a nine-digit generation": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v999999999:" + self._own_birth())),
            "sibling shaped but out of range": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v1:Mon Jan 99 99:99:99 2026")),
            "sibling with a trailing NEL": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v1:" + self._own_birth() + "\u0085")),
            "sibling with a leading BOM": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="\ufeffv1:" + self._own_birth())),
            "migration birth with a trailing NEL": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop="process_birth",
                                     birth=self._own_birth() + "\u0085", birth_version=1)),
            "migration birth with a leading BOM": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop="process_birth",
                                     birth="\ufeff" + self._own_birth(), birth_version=1)),
            "no fingerprint at all": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop="process_birth")),
```

In `ungovernable`, the moved pair plus:

```python
            "sibling names another process's birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v1:Thu Jan 1 00:00:00 1970")),
            "rotated, sibling names another birth": lambda p, a: (
                self._proof(p, self.B), self._withdrawn(p, a),
                self._patch_endpoint(p, a, process_birth="v1:Thu Jan 1 00:00:00 1970")),
            "migration pair, other birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop="process_birth",
                                     birth="Thu Jan 1 00:00:00 1970", birth_version=1)),
            "migration pair, version key duplicated": lambda p, a: (
                self._proof(p, self.A),
                self._rewrite_endpoint_text(p, a, lambda t:
                    self._migration_text(self._own_birth())(t)
                    + ', "birth_version": 2, "birth_version": 1}')),
            "migration pair, version is a 65,000-digit token": lambda p, a: (
                self._proof(p, self.A),
                self._rewrite_endpoint_text(p, a, lambda t:
                    self._migration_text(self._own_birth())(t)
                    + ', "birth_version": ' + "9" * 65_000 + "}")),
```

Skip the `_own_birth()`-dependent cases with `self.skipTest` when it returns `""` (no process table), as the harness does elsewhere.

Where the harness's Node script passes `birth` (`:17326-17333`), pass `antiphon.peers.process_fingerprint(os.getpid())` instead of `self._own_birth()`, since `listenerBirth` is now the rendered spelling; add `_own_fingerprint()` beside `_own_birth()` for it.

- [ ] **Step 3: Contract red**

```python
    def test_the_two_readers_select_the_fingerprint_the_same_way(self):
        """One selector per language, one grammar compared at runtime — never
        grepped from source, where Python's constant spans two adjacent
        literals — precedence by key presence, and the Node form sees the
        lexical scan."""
        node = read("lib", "identity.mjs")
        self.assertIn("export function fingerprintOf(wrapper)", node)
        self.assertIn('Object.hasOwn(record, "process_birth")', node)
        self.assertIn('spelledFractional(wrapper, "birth_version")', node)
        python = read("lib", "peers.py")
        self.assertIn("def _fingerprint_of(record)", python)
        self.assertIn('"process_birth" in record', python)
        self.assertNotIn("def _birth_of(", python)
        exported = json.loads(subprocess.run(
            ["node", "--input-type=module", "-e",
             'import * as m from "./lib/identity.mjs";'
             'process.stdout.write(JSON.stringify({'
             'grammar: m.CANONICAL_START, weekdays: m.WEEKDAYS, months: m.MONTHS,'
             'version: m.PROCESS_FINGERPRINT_VERSION}));'],
            capture_output=True, text=True, cwd=ROOT, check=True).stdout)
        self.assertEqual(exported["grammar"], antiphon.peers.CANONICAL_START)
        self.assertEqual(exported["weekdays"], list(antiphon.peers.WEEKDAYS))
        self.assertEqual(exported["months"], list(antiphon.peers.MONTHS))
        self.assertEqual(exported["version"],
                         antiphon.peers.PROCESS_FINGERPRINT_VERSION)
        channel = read("lib", "channel.mjs")
        self.assertIn('fingerprint_field: "process_birth"', channel)
        self.assertIn('answer?.fingerprint_field !== "process_birth"', channel)
        register = python_of_antiphon = read("lib", "antiphon.py")
        register = register[register.index("def register_peer("):]
        register = register[:register.index("\ndef ")]
        self.assertIn('"fingerprint_field": "process_birth"', register)
```

`ROOT`, `read(...)` and the `register_peer` slice: exactly what `test_the_birth_authority_never_comes_from_the_record_it_judges` already uses in this file; `import json`/`import subprocess` if absent.

- [ ] **Step 4: Node E2E reds**

Beside `runPeers` (`channel.test.mjs:1928`):

```js
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
```

Then three cases after `aLiveListenerReassertsItsOwnMissingEndpoint`:

```js
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
    assert.match(session.stderr(), /registry on disk predates this listener's fingerprint field[\s\S]*withdrawn[\s\S]*Reinstall antiphon/,
      `the listener names the downgrade: ${session.stderr()}`);
    assert.ok(!existsSync(endpointFor(dir, STALE_A_ALIAS)), "and leaves no endpoint behind");
    assert.doesNotMatch(session.stderr(), /channel ready/, "and does not announce a channel it cannot govern");
  } finally {
    session.child.kill("SIGKILL");
    await waitForExit(session.child, 2_000);
    for (const p of [dir, stub.dir, mixed.dir]) await rm(p, { recursive: true, force: true }).catch(() => {});
  }
}

await thePublishedReaderLeavesALiveListenerRegistered();
await anOldListenerOverAnUpgradedPythonIsRefusedNotToldItRecovered();
await aCurrentListenerOverADowngradedPythonWithdrawsItsOwnEndpoint();
```

Add `import { materialiseLib } from "./fixtures/mixed_lib.mjs";` at the top. If `sendTo`'s signature differs (`grep -n "^async function sendTo" test/channel.test.mjs`), adapt the calls only. If the current channel prints "channel ready" *before* its claim resolves, read `channel.mjs:960-1010` and assert on whichever line follows the claim instead.

- [ ] **Step 5: Observe every red on the pre-fix tree, for the reasons named**

Run, in the foreground:

```sh
/usr/bin/python3 -m unittest test.test_peers.RecycledPidTest -v 2>&1 | tail -60
/usr/bin/python3 -m unittest test.test_antiphon.ReadinessParityTest -v 2>&1 | tail -40
/usr/bin/python3 -m unittest test.test_contracts -k fingerprint -v
node test/channel.test.mjs 2>&1 | grep -B2 -A8 "old reader\|old listener\|downgraded\|AssertionError" | head -80
```

Expected, each for the reason that names the defect:

- `test_the_published_reader_leaves_a_current_record_alone`: `[] != ['ui']` — the shipped reader pruned. **The P0 under test.**
- `..._where_the_old_reader_never_looks`: `KeyError: 'process_birth'`.
- `..._validates_a_versioned_owner_and_joins_on_it`: PASS (pins today's join; Task 3's mutation proves it protects).
- `test_the_current_reader_still_prunes_a_recycled_pid`: PASS (pins today's strictness).
- migration / precedence / future / bounded / grammar / response cases: `AttributeError` on the new names, or `KeyError`.
- parity `cases`: disagreements listed by name — record the table of `python=… node=…` pairs; it goes into the Task 3 commit message. `ungovernable`: the 65,000-digit token fails its `NO-RECORD` assertion on 3.9 (Python enumerates it) — the bounded-parse defect under test.
- contract: missing exports and the missing payload/ack strings.
- `thePublishedReaderLeavesALiveListenerRegistered`: `[] != ["ui"]`.
- `anOldListenerOverAnUpgradedPythonIsRefusedNotToldItRecovered`: `reply.ok` is `true`, `action === "reasserted"` — the pre-fix Python accepts the undeclared claim. **The old-Node half of the matrix.**
- `aCurrentListenerOverADowngradedPythonWithdrawsItsOwnEndpoint`: on the pre-fix tree "worktree" Node *is* f0c529f Node, so this case measures old-vs-old and its assertion about the withdrawal message fails for the trivial reason that no such message exists yet. Its meaningful red — a *current* Node binding over an old Python — is observed in Task 3 Step 4 by removing only the acknowledgement check. **The new-Node half is proven by mutation, not by the pre-fix run**, and the commit says so.

- [ ] **Step 6: Commit the reds**

```sh
git add test/test_peers.py test/test_antiphon.py test/test_contracts.py test/channel.test.mjs
git commit -m "Red: the shipped reader prunes a current endpoint; a mixed-version listener is told it recovered"
```

---

### Task 3: One atomic product change across both readers, the verdict and the claim

**Files:**
- Modify: `lib/peers.py:69-70` (constants), `:1004-1037` (`_bounded_int`, `_read_record`), `:1139-1161` (`_birth_of`, `_birth_is_current` → `canonical_start`, `_fingerprint_of`), `:1197-1258` (`_record_alive`, `_record_liveness`), `:1536-1541` (`register`), after `_process_birth` (~`:1700`)
- Modify: `lib/antiphon.py:516-561` (`automatic_verdict`), `:6080-6122` (`register_peer`)
- Modify: `lib/identity.mjs` (constants, `canonicalStart`, `fingerprintOf`, `renderFingerprint`, `automaticProofVerdict:359-360`)
- Modify: `lib/channel.mjs:412-432` (payload field; acknowledgement and withdrawal)
- Modify: `test/test_antiphon.py:12433-12448` (`_claim` writes the new spelling by default; `endpoint_birth_version=1` writes the legacy pair, `None` writes unversioned `birth`)

- [ ] **Step 1: Python — grammar, selector, bounded parse, liveness, writer**

Constants beside `OWNER_KEY_VERSION`:

```python
# The exact shape `_process_info` renders under LC_ALL=C and TZ=UTC: C-locale
# weekday and month, an unpadded day, a 24-hour clock, a four-digit year,
# single spaces. Spelled identically in identity.mjs and compared at runtime
# by a contract test. Only this shape, with every field in range, may
# authorise "dead": anything wider lets a reader's own notion of blank or word
# take part, and the two runtimes disagree on U+0085 and U+FEFF.
CANONICAL_START = (r"([A-Z][a-z]{2}) ([A-Z][a-z]{2}) ([0-9]{1,2}) "
                   r"([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4})")
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_CANONICAL_START = re.compile(CANONICAL_START)
# Some generation, captured lexically and bounded: a reader that names v1
# must neither convert nor choke on a long digit run, only tell whether the
# token is its own.
_GENERATION = re.compile(r"v([0-9]{1,9}):")
INTEGER_TOKEN_CEILING = 20      # digits; a pid, a version, a clock all fit


def canonical_start(text):
    """`text` when it is a start the writer could have produced, else None.

    Shape and range, not calendar: `Feb 31` passes, and a value that is shaped
    and in range but not this process's birth is indistinguishable from a
    recycled pid — the same residual an in-range wrong owner key carries.
    """
    if not isinstance(text, str):
        return None
    matched = _CANONICAL_START.fullmatch(text)
    if not matched:
        return None
    weekday, month, day, hour, minute, second, year = matched.groups()
    if (weekday in WEEKDAYS and month in MONTHS and 1 <= int(day) <= 31
            and int(hour) <= 23 and int(minute) <= 59 and int(second) <= 59
            and 1970 <= int(year) <= 9999):
        return text
    return None
```

`_read_record`, with the bounded parser:

```python
def _bounded_int(token):
    """`int(token)` for a token of at most INTEGER_TOKEN_CEILING digits.

    `json.loads` converts an integer before any reader sees it; a 65,000-digit
    token fits under RECORD_CEILING, converts slowly on 3.9, raises on later
    Pythons, and rounds to a double in Node. Refusing it here makes the record
    invalid on every Python, before any conversion."""
    if len(token.lstrip("-")) > INTEGER_TOKEN_CEILING:
        raise ValueError("integer token too long")
    return int(token)
```

and in `_read_record`: `json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_int=_bounded_int)`.

Replace `_birth_of`/`_birth_is_current`:

```python
def _fingerprint_of(record):
    """`("current", start)`, `("other", None)` or None for a record.

    Precedence is by key presence. A present `process_birth` is the only
    thing consulted, however it is spelled: the 0.3.x reader interpreted
    `birth` against its own timezone and pruned live listeners while
    `birth_version` sat unread beside it, so the canonical value lives where
    that reader never looks, and nothing here falls back past it. A generation
    token that is not this reader's is unverifiable; this reader's token with
    anything but the writer's shape after it is malformed, not "other". The
    0.4.0 pair — canonical `birth` beside a lexical integer `birth_version`
    1 — is migration input, read strictly. Everything else is evidence of
    nothing.
    """
    if not hasattr(record, "get"):
        return None
    if "process_birth" in record:
        sibling = record.get("process_birth")
        if not isinstance(sibling, str):
            return None
        head = _GENERATION.match(sibling)
        if head is None:
            return None
        if int(head.group(1)) != PROCESS_FINGERPRINT_VERSION:
            return ("other", None)
        start = canonical_start(sibling[head.end():])
        return ("current", start) if start is not None else None
    version = record.get("birth_version")
    start = canonical_start(record.get("birth"))
    if start is not None and type(version) is int and version == 1:
        return ("current", start)
    return None
```

`_record_alive` tail:

```python
    pid = _pid_of(record)
    if pid is None or not alive(pid):
        return False
    fingerprint = _fingerprint_of(record)
    if fingerprint is None or fingerprint[0] != "current":
        return True
    observed = _process_birth(pid)
    return observed is None or observed == fingerprint[1]
```

`_record_liveness`:

```python
    pid = _pid_of(record)
    fingerprint = _fingerprint_of(record)
    key = (pid, fingerprint)
    if cache is not None and key in cache:
        return cache[key]
    result = "unknown"
    if pid is not None and fingerprint is not None and fingerprint[0] == "current":
        if not alive(pid):
            result = "dead"
        else:
            observed = _process_birth(pid)
            if observed is not None:
                result = "live" if observed == fingerprint[1] else "dead"
    if cache is not None:
        cache[key] = result
    return result
```

`register`, the `if birth:` block:

```python
        if birth:
            # When the process was born, the half of its identity the number
            # does not carry — written where the 0.3.x reader never looks, so
            # a server still running that reader keeps this record on its pid
            # rather than pruning it against its own timezone.
            record["process_birth"] = _render_fingerprint(birth)
```

Beside `_process_birth`:

```python
def _render_fingerprint(start):
    return f"v{PROCESS_FINGERPRINT_VERSION}:{start}"


def process_fingerprint(pid):
    """The spelling `register` writes, from a fresh observation, so a caller
    can learn what its own record says without reading that record."""
    birth = _process_birth(pid)
    return _render_fingerprint(birth) if birth else None
```

- [ ] **Step 2: Python — the verdict and the claim**

`automatic_verdict`, after the `digest is None or automatic is not True` check and before `state, record = proof`:

```python
    fingerprint = peers._fingerprint_of(peer)
    if fingerprint is None or fingerprint[0] != "current":
        # A current listener writes a current sibling whenever `ps` answers,
        # and fails closed on its own side when it does not. A governed
        # record without one is not a record any current listener is serving
        # as its own; routing to it is "recovered, then refused". Non-
        # destructive: nothing is pruned or retired over it.
        return "UNREADY"
```

`register_peer`, before the `mode` check (`antiphon.py:6100`):

```python
    if data.get("identity_digest") is not None and kind == "claude" \
            and data.get("fingerprint_field") != "process_birth":
        print("register_peer: this listener predates the registry's "
              "fingerprint field and cannot govern the endpoint it would "
              "publish; reconnect the Claude session (`/mcp` → reconnect "
              "antiphon) so a current listener claims it", file=sys.stderr)
        return 1
```

and the response:

```python
    print(json.dumps({"birth": peers.process_fingerprint(data.get("pid")),
                      "fingerprint_field": "process_birth"}))
```

- [ ] **Step 3: Node — grammar, selector, verdict, claim**

`lib/identity.mjs`, exported beside the other constants:

```js
export const PROCESS_FINGERPRINT_VERSION = 1;
export const CANONICAL_START =
  "([A-Z][a-z]{2}) ([A-Z][a-z]{2}) ([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) ([0-9]{4})";
export const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const CANONICAL = new RegExp(`^${CANONICAL_START}$`);
const GENERATION = /^v([0-9]{1,9}):/;

// The same shape-and-range check Python's `canonical_start` makes.
export function canonicalStart(text) {
  if (typeof text !== "string") return null;
  const m = CANONICAL.exec(text);
  if (!m) return null;
  const [, weekday, month, day, hour, minute, second, year] = m;
  const ok = WEEKDAYS.includes(weekday) && MONTHS.includes(month)
    && Number(day) >= 1 && Number(day) <= 31
    && Number(hour) <= 23 && Number(minute) <= 59 && Number(second) <= 59
    && Number(year) >= 1970 && Number(year) <= 9999;
  return ok ? text : null;
}

// The same selection Python's `_fingerprint_of` makes, in the same order and
// on the same grammar. Takes the readRecord wrapper, not the bare record,
// because how `1` was spelled is only visible in the scan. Duplicate keys
// never reach here: readRecord already called the record invalid.
export function fingerprintOf(wrapper) {
  const record = wrapper?.record;
  if (!record || typeof record !== "object") return null;
  if (Object.hasOwn(record, "process_birth")) {
    const sibling = record.process_birth;
    if (typeof sibling !== "string") return null;
    const head = GENERATION.exec(sibling);
    if (!head) return null;
    if (Number(head[1]) !== PROCESS_FINGERPRINT_VERSION) return { kind: "other" };
    const start = canonicalStart(sibling.slice(head[0].length));
    return start === null ? null : { kind: "current", start };
  }
  const start = canonicalStart(record.birth);
  if (start !== null && record.birth_version === 1
      && !spelledFractional(wrapper, "birth_version")) {
    return { kind: "current", start };
  }
  return null;
}

export function renderFingerprint(fingerprint) {
  return fingerprint?.kind === "current"
    ? `v${PROCESS_FINGERPRINT_VERSION}:${fingerprint.start}` : null;
}
```

In `automaticProofVerdict`:

```js
      || (typeof listenerBirth === "string"
          && renderFingerprint(fingerprintOf(endpoint)) !== listenerBirth)
```

`lib/channel.mjs:412-432`, the payload and the acknowledgement:

```js
  const payload = {
    kind: "claude", name: peerId, address: socketPath, pid: process.pid,
    // What this listener's in-memory verdict reads. A registry that predates
    // the field refuses this claim; one that honours it says so below, and a
    // registry that says nothing has written a record this listener cannot
    // govern.
    fingerprint_field: "process_birth",
  };
  …
    if (subcommand === "register_peer") {
      let answer = null;
      try { answer = JSON.parse(String(stdout).trim()); } catch { answer = null; }
      claimedBirth = typeof answer?.birth === "string" ? answer.birth : null;
      if (automaticIdentityDigest && answer?.fingerprint_field !== "process_birth") {
        // The Python on disk is older than this listener: it wrote the old
        // record and cannot be governed by the verdict this process runs.
        // Withdraw what it wrote, inside this same serialised step, and say why.
        claimedBirth = null;
        await runRegistryCall("unregister_peer", true);
        console.error("antiphon: the registry on disk predates this listener's "
          + "fingerprint field; the endpoint it wrote was withdrawn. Reinstall "
          + "antiphon so both sides match, then reconnect the Claude session");
        return false;
      }
    }
    return true;
```

`runRegistryCall` is what `registryCall` awaits inside `registryMutations`; calling it directly here keeps the withdrawal inside the same chain step. Read `channel.mjs:434-450` before editing and keep the shutdown-ordering comment true.

- [ ] **Step 4: Green, then mutate each guard**

Run the four commands from Task 2 Step 5; expected all PASS and the parity `cases` list of disagreements empty; then compare the measured pairs against the bucket table in *Design* and report any row that differs. Then, one at a time, restore each by reverse edit (never `git checkout` on `lib/`):

| Mutation | Named test that must fail |
|---|---|
| `register` writes `record["birth"] = birth; record["birth_version"] = 1` | `test_the_published_reader_leaves_a_current_record_alone`, `thePublishedReaderLeavesALiveListenerRegistered` |
| `_fingerprint_of` uses `isinstance(sibling, str)` as the precedence test instead of key presence | `test_a_present_sibling_is_the_only_thing_consulted` (`7`/`None` subtests), parity "malformed sibling beside a conflicting valid pair" |
| `_fingerprint_of` returns `("other", None)` for a current-generation token with a malformed remainder | `test_a_present_sibling_is_the_only_thing_consulted` (`v1:`, `v1:garbage`), parity "malformed current sibling" |
| `canonical_start` drops the range check | `test_the_grammar_is_the_writers_own_shape_and_in_range` ("Mon Jan 99 …"), parity "shaped but out of range" |
| `_bounded_int` ceiling removed | `test_a_long_integer_token_never_reaches_a_conversion`, parity ungovernable "65,000-digit token" |
| `_fingerprint_of` accepts `version == 1` without `type(version) is int` | parity "version spelled 1.0" |
| `automatic_verdict` fingerprint rule removed | parity "no fingerprint at all", "sibling of a future generation", NEL/BOM cases (Python READY vs Node UNREADY) |
| `register_peer` gate removed | `anOldListenerOverAnUpgradedPythonIsRefusedNotToldItRecovered` |
| `runRegistryCall` acknowledgement check removed | `aCurrentListenerOverADowngradedPythonWithdrawsItsOwnEndpoint` — **this is that case's red**, observed here |
| `identity.mjs` `canonicalStart` range check dropped | parity "shaped but out of range" (Node READY vs Python UNREADY) |
| fixture `valid_owner_key` made to refuse `v1` keys (edit the copy, restore with `git checkout -- test/fixtures/peers_0_3_3.py`) | `test_the_published_reader_validates_a_versioned_owner_and_joins_on_it` |

- [ ] **Step 5: The whole tree, foreground**

```sh
/usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3
node test/channel.test.mjs 2>&1 | tail -3
git diff f0c529f --stat -- lib/channel.mjs
```

Expected: `OK (skipped=2)` (the fixture-blob test skips only without git); Node suite ok; the channel diff is the payload field and the acknowledgement block — `claimedBirth`'s declaration and the `:531` fail-closed byte-identical.

- [ ] **Step 6: Commit**

```sh
git add lib/peers.py lib/identity.mjs lib/antiphon.py lib/channel.mjs test/test_antiphon.py
git commit -m "Registry: the fingerprint moves where the 0.3.x reader never looks; the claim is a two-way capability; the verdict requires a current fingerprint"
```

The commit body carries the parity table recorded in Task 2 Step 5 and the measured bucket pairs.

---

### Task 4: Doctor names the migration record as a risk and the fingerprint-less record by cause

**Files:**
- Modify: `lib/antiphon.py` (~`:8700-8720`, the doctor per-record loop; the process-start-vs-code-mtime diagnostic)
- Modify: `test/test_antiphon.py` (the class holding the existing "restart that Codex session" test; `grep -n "restart that" test/test_antiphon.py`)

*Bounded on purpose: two notes and one verification, read-only, no recovery. Nothing above depends on it.*

- [ ] **Step 1: Write the failing tests**

```python
    def test_doctor_names_a_record_an_old_reader_may_still_prune(self):
        """A record in the 0.4.0 migration spelling stays prunable by a 0.3.x
        reader until its owner rewrites it. Risk, not diagnosis: doctor cannot
        see whether such a reader is running. The remedy is by kind."""
        with tempfile.TemporaryDirectory() as project:
            self._register_migration_spelling(project, "claude", "ui")
            self._register_migration_spelling(project, "codex", "x")
            report = self._doctor(project)
        self.assertIn("peer claude/ui: fingerprint in the 0.4.0 spelling; a 0.3.x "
                      "reader, if one is still running, prunes it until this "
                      "listener reasserts or reconnects", report)
        self.assertIn("peer codex/x: fingerprint in the 0.4.0 spelling; a 0.3.x "
                      "reader, if one is still running, prunes it until that "
                      "Codex session restarts", report)

    def test_doctor_names_an_unready_record_that_carries_no_current_fingerprint(self):
        """UNREADY has a bootstrap meaning — waiting for its first turn — and
        this record is not that. Say what it is."""
        with tempfile.TemporaryDirectory() as project:
            self._register_automatic(project, "ui")          # as the class's other tests do
            self._patch_endpoint_here(project, "ui", process_birth="v1:garbage")
            report = self._doctor(project)
        self.assertIn("peer claude/ui: endpoint carries no current fingerprint, so no "
                      "current listener can be serving it as its own; reconnect the "
                      "Claude session", report)

    def test_doctor_judges_a_claude_channel_by_its_start_against_its_code(self):
        """The grounded stale-reader diagnostic already exists for Codex MCP
        processes. It has to cover a Claude channel process the same way, since
        that is the listener the registry now refuses. Modelled on the existing
        Codex-MCP case in this class: the same process-table seam, a `node
        …/lib/channel.mjs` line started before lib/channel.mjs's mtime, and
        the same 'its code changed' sentence with 'reconnect the Claude
        session' as the remedy."""
```

Write `_register_migration_spelling` and `_patch_endpoint_here` with `peers.register` plus a direct JSON rewrite (pattern in Task 2), reuse `_register_automatic`/`_doctor` as the class defines them (read the class first), and finish the third test's body by copying the Codex-MCP test's seam.

- [ ] **Step 2: Run to verify they fail**

Run: `/usr/bin/python3 -m unittest test.test_antiphon -k doctor_names -k doctor_judges -v`
Expected: FAIL — the notes are absent; the channel case either fails or already passes (if it passes, keep it: it pins the coverage the design relies on, and say so in the commit).

- [ ] **Step 3: Implement**

In the doctor loop after the `mixed_owner_generation` branch:

```python
        fingerprint = peers._fingerprint_of(record)
        if fingerprint is not None and "process_birth" not in record:
            remedy = ("this listener reasserts or reconnects"
                      if record.get("kind") == "claude"
                      else "that Codex session restarts")
            report.note(f"peer {who}: fingerprint in the 0.4.0 spelling; a "
                        f"0.3.x reader, if one is still running, prunes it "
                        f"until {remedy}")
        if (record.get("automatic") is True
                and (fingerprint is None or fingerprint[0] != "current")):
            report.note(f"peer {who}: endpoint carries no current fingerprint, "
                        "so no current listener can be serving it as its own; "
                        "reconnect the Claude session")
```

Extend the process-start-vs-code-mtime diagnostic to `node …/lib/channel.mjs` processes if it does not already cover them, with the same sentence shape and "reconnect the Claude session" as the remedy.

- [ ] **Step 4: Green, then the doctor classes whole**

Run: `/usr/bin/python3 -m unittest test.test_antiphon -k doctor -v 2>&1 | tail -3`
Expected: OK.

- [ ] **Step 5: Commit**

```sh
git add lib/antiphon.py test/test_antiphon.py
git commit -m "Doctor names the record an old reader may still prune, and the record no current listener can serve"
```

---

### Task 5: The contract in words

**Files:**
- Modify: `README.md` (grep `birth` and `rolling` first; add one paragraph to the upgrade/identity section only if it discusses process identity)
- Modify: `BACKLOG.md` (*What is on `main` at 0.4.0* paragraph beginning "New process observations now run `ps` under `LC_ALL=C`…", ~`:1428-1436`; one *Start here* line)

- [ ] **Step 1: Write the words**

BACKLOG, replacing "A legacy endpoint birth retains PID-only liveness until its owner refreshes it: its old rendered value is evidence of nothing under the new canon." with:

```markdown
The canonical fingerprint is written as `process_birth: "v1:<start>"`, a field
the 0.3.x reader never selects, because that reader interpreted `birth`
against its own timezone and pruned live listeners while `birth_version` sat
unread beside it (reproduced 2026-09-02 with the byte-exact 0.3.3 reader,
`test/fixtures/peers_0_3_3.py`). Both readers select on one grammar and one
range check, by key presence: a present sibling is the only thing consulted;
this generation's token with anything but the writer's shape after it is
malformed, not another generation; the 0.4.0 pair (`birth` + lexical integer
`birth_version: 1`) is migration input read strictly and never dead for
lacking the sibling; anything else is pid-only for liveness and UNREADY for
routing, because a current listener always writes a current sibling and a
governed record without one is one no current listener serves. Integer
tokens are bounded at parse time so a near-ceiling digit run is not a record
on any Python. The claim is a two-way capability: Node declares
`fingerprint_field`, Python acknowledges it, an undeclared automatic claim is
refused with a reconnect remedy, and a listener whose registry does not
acknowledge withdraws its own endpoint and says to reinstall — the
alternative in each direction told the sender it had recovered and then
refused the words. Records still in the 0.4.0 spelling stay prunable by a
0.3.x reader until their owner rewrites them; doctor names that as a risk.
Old readers judge current records by pid alone, as they always did; only
current readers keep the recycled-pid check.
```

*Start here*, one line, by mechanism: "Mixed-version pruning (a 0.3.x reader deleting current endpoints; a listener whose in-memory Node and on-disk Python disagree being told it recovered) is closed by the `process_birth` sibling, the two-way `fingerprint_field` claim and the verdict's current-fingerprint rule — see the 0.4.0 paragraph."

- [ ] **Step 2: Commit**

```sh
git add README.md BACKLOG.md
git commit -m "The fingerprint move, in words: what old readers do, what stays prunable, who is refused"
```

---

### Task 6: Verification on the exact SHA, then two read-only reviews

**Files:** none modified. Everything below runs against the commit Task 5 produced, named once and never moved.

- [ ] **Step 1: Name the commit**

```sh
SHA=$(git rev-parse HEAD); echo "$SHA"
git status --short          # must be empty: clean tracked tree
```

- [ ] **Step 2: Focused tests, full suites, statics — foreground, in the worktree at `$SHA`**

```sh
/usr/bin/python3 -m unittest test.test_peers.RecycledPidTest test.test_peers.FrozenReaderFixtureTest \
  test.test_antiphon.ReadinessParityTest test.test_contracts -v 2>&1 | tail -5
/usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3
ANTIPHON_NAME=ui /usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3
npm test 2>&1 | tail -5
git diff --check "$SHA~6" "$SHA"
/usr/bin/python3 -m py_compile lib/antiphon.py lib/peers.py test/fixtures/peers_0_3_3.py
node --check lib/channel.mjs && node --check lib/identity.mjs && node --check test/fixtures/mixed_lib.mjs
git status --short          # still empty (py_compile writes only __pycache__, which is ignored)
```

Expected: every line green; `npm test` ends in the Node suite's `MCP channel integration: ok`.

- [ ] **Step 3: Fresh-user E2E tied to `$SHA`**

`fresh-user.sh` needs a clean tree, logged-in `claude` and `codex`, and the network; it spends a few small model calls. Run it from a temporary worktree at the exact commit, never from this one:

```sh
TMP=$(mktemp -d)
git worktree add "$TMP/at-sha" "$SHA"
(cd "$TMP/at-sha" && npm install --silent && bash test/e2e/fresh-user.sh 2>&1 | tail -20)
git worktree remove --force "$TMP/at-sha"
```

Expected: the script's own final verdict line green. If the CLIs are not logged in, stop and report that this step is pending on the user; do not mark it done.

- [ ] **Step 4: Two read-only reviews on `$SHA`**

1. Codex, via `@codex:<alias>` / `reply_to_codex`: exact `$SHA`, the bucket table with measured pairs, the mutation table with observed failures, the fresh-user verdict.
2. An independent read-only review (opus subagent, `superpowers:requesting-code-review`) of `git diff f0c529f..$SHA`, with the same three artefacts.

Merge, push and publish do not follow from green. They wait for both reviews to close on `$SHA` and for the user's separate approval.

---

## Self-review against plan review 2

- **C1 chronology:** `materialiseLib({node:"f0c529f", python:"f0c529f"})` → bound → `swapPython("worktree")` → prune → reassert. Both modes refused, stated in *Design*.
- **I2 malformed current:** the generation token is captured lexically; `v1:` + malformed → `None`; only a *different* bounded token is `other`. Unit subtests `v1:`, `v1:garbage`; parity "malformed current sibling"; mutation row.
- **I3 composite parity:** *Design → The composite readiness contract* adds the `automatic_verdict` rule, keeps verdict classes, names the surface (doctor note, Task 4), and assigns every new fixture to `cases` or `ungovernable` with the pair it must measure; mutation rows for the Python rule and the Node range check.
- **I4 reverse direction:** two-way capability (declare / acknowledge / withdraw), E2E `aCurrentListenerOverADowngradedPythonWithdrawsItsOwnEndpoint`, and the honest note that its meaningful red is observed by mutation because the pre-fix tree cannot host a "new" Node.
- **I5 duplicates:** structural on both sides before the selector, stated in *Evidence* and *Design*; "version key duplicated" placed in `ungovernable`; no `.has` branch.
- **I6 bounded integers:** `_bounded_int` as `parse_int`, unit test with a near-ceiling raw token timed on 3.9, parity ungovernable case; the "never converted" claim is now true because the token is refused by length before `int()`.
- **I7 grammar:** `canonical_start` with names and ranges mirrored; out-of-range unit and parity cases; the residual (shaped, in-range, wrong) stated explicitly rather than claimed away.
- **I8 executability:** `_rewrite_endpoint_text`/`_migration_text` defined; `drop=` single-valued everywhere; grammar, names and version compared at runtime through a `node -e` export, not grepped.
- **I9 gate:** Task 6 runs after the final commit on the named SHA — focused, full, `ANTIPHON_NAME=ui`, `npm test`, `git diff --check`, `py_compile`, `node --check`, clean status, `fresh-user.sh` from a temporary worktree at the SHA, then two read-only reviews; nothing follows from green.
- **Open for critique:** (1) whether `automatic_verdict`'s UNREADY for a fingerprint-less governed record should instead be a new class, given UNREADY's bootstrap meaning — the plan keeps UNREADY and names the cause in doctor; (2) `INTEGER_TOKEN_CEILING = 20` digits — wide enough for every integer the registry writes, narrow enough that nothing near the record ceiling converts.
