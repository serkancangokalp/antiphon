# Mixed-Version Endpoint Pruning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every behavioural red is written and observed red on the pre-fix tree before the first product edit; mutate each guard and watch the named test fail before calling it green.

**Goal:** A reader from the published 0.3.x line that is still running must leave a current endpoint record alone; the current reader keeps refusing a true pid/birth mismatch; and an already-running pre-fix Node listener is refused honestly rather than told it recovered.

**Architecture:** The canonical process fingerprint moves out of the field the old Python reader interprets (`birth`) into a sibling the old reader never selects (`process_birth`, generation in the value). Writers stop emitting `birth`/`birth_version`. One selector per language, driven by the same ASCII grammar, prefers the sibling by key presence, accepts the 0.4.0-on-main `birth` + `birth_version: 1` pair as strict migration input, and treats everything else as unverifiable, never dead. Because no single record can satisfy both the old Python reader (must not see `birth`) and an old in-memory Node verdict (requires `birth`), the Node→Python claim payload declares which fingerprint field the listener reads; an automatic claim without that declaration is refused with a reconnect remedy instead of publishing an endpoint the listener cannot govern.

**Tech Stack:** Python 3.9+ standard library and `unittest`; Node.js ESM and `node:assert`; the byte-exact 0.3.3 `lib/peers.py` and the pre-fix `lib/*.mjs` as test fixtures.

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

Measured on `f0c529f` (2026-09-02), in a scratch copy of the live registry, nothing live touched:

| Reader | Record | `_record_alive` | `read_peers` | endpoint.json after |
|---|---|---|---|---|
| `943da8a:lib/peers.py` (0.3.3; identical bytes to `a076723`, sha256 `4bb3ea14…a060b`), `TZ=Europe/Istanbul` | current (`birth` UTC, `birth_version: 1`) | False | `[]` | **deleted** |
| same old reader, same TZ | same record with `birth` removed and a sibling `process_birth` added | True | `[alias]` | present |
| same old reader | `owner` of the form `pid:v1:start` | n/a | `valid_owner_key` True | n/a |

The old `_record_alive` reads `birth` unconditionally and compares it against its own `ps lstart`, rendered in the reader's inherited timezone. The current writer (`lib/peers.py:1536-1541`) renders under `TZ=UTC`/`LC_ALL=C` and adds `birth_version: 1`, which the old reader never consults. A long-lived old MCP server therefore prunes every current endpoint it enumerates, repeatedly, until it is restarted. Codex measured the product shape live: `endpoint.json` vanished, status showed "current automatic identity with no channel yet", the stale MCP's `antiphon_send` refused the alias, and the current `send_to_claude` reassert restored the record and delivered once.

**The second half of the matrix (Codex, plan review 1).** `lib/channel.mjs` shells `python3 <here>/antiphon.py` on every registry call (`bridgeScript`, `channel.mjs:24`), so an upgrade on disk gives a running listener a *new* Python and keeps its *old* `identity.mjs` in memory. That in-memory `automaticProofVerdict` (`identity.mjs:359-360`) compares `endpoint.record.birth` with `claimedBirth`. After the writer change alone, a reassert against that listener would write a record with only `process_birth`, `endpointDescribesListener` would accept it, the sender would re-resolve and deliver, and the old verdict would answer UNREADY and refuse the words. "Recovered, then refused" is the one outcome this contract forbids, and it is why the claim payload has to say what the listener reads.

Runtime facts the design depends on, measured 2026-09-02:

- `spelledFractional(record, field)` reads `record.scan` (`identity.mjs:176-178`); `endpoint.record` carries no `scan`, the `readRecord` wrapper does. A selector handed the bare record cannot see how `1` was spelled.
- `str.strip()` and `String.prototype.trim()` disagree on U+0085 (Python strips, JS keeps) and U+FEFF (JS strips, Python keeps); `\s`/`\S` differ per runtime likewise.
- `/usr/bin/python3` is 3.9.6 and has no `int_max_str_digits`; newer Pythons raise on >4300 digits, JS rounds to Infinity. Version tokens must be bounded before conversion, or never converted.
- The old `read_peers` never consults `valid_owner_key` for an addressed Claude endpoint (Codex measured `owner='definitely-invalid'` listed). The join road is `_session_address`, reached only through an addressless Codex endpoint.
- A backgrounded test run has no CLI root above it, `owner_key()` is None and 31 tests fail for that reason alone. Run suites in the foreground.

## Design

### The record

Current writer output for a fingerprintable process:

```json
{"kind": "claude", "name": "…", "pid": 77348, "address": "…",
 "started_at": 1788366537.8, "owner": "77000:v1:Wed Sep 2 16:13:00 2026",
 "process_birth": "v1:Wed Sep 2 16:13:13 2026"}
```

- `process_birth` is `"v" + PROCESS_FINGERPRINT_VERSION + ":" + <canonical start>`. The generation is in the value, as in the owner key, because a marker in a sibling field protects no reader that ignores the sibling. It is rendered from `PROCESS_FINGERPRINT_VERSION` through a process-specific helper, not from `OWNER_KEY_VERSION`; the two are equal today and the process generation is the authority for this field.
- `birth` and `birth_version` are no longer written. The old reader finds no `birth`, takes its documented pid-only road, and keeps the record — the behaviour those readers always had.
- Writing a legacy `birth` for old readers' benefit is rejected: the old reader compares against its own timezone, which the writer cannot know.

### The grammar, shared by both readers

One ASCII grammar, spelled identically in Python and JS, so neither runtime's notion of blank or `\S` takes part:

```
CANONICAL_START  = [A-Z][a-z]{2} [A-Z][a-z]{2} [0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}
CURRENT_SIBLING  = v1:CANONICAL_START            # exactly what register writes
GENERATION_ONLY  = v[0-9]{1,9}:                  # a prefix this reader recognises as "some generation"
```

`CANONICAL_START` is the exact shape `_process_info` produces under `LC_ALL=C`/`TZ=UTC` (fields split on whitespace and rejoined with single spaces, `peers.py:1690-1696`). Only writer-producible current-generation evidence may authorise "dead". Nothing is trimmed, stripped or normalised before matching; a value with a leading BOM or a trailing NEL simply does not match.

### Reading a fingerprint

`_fingerprint_of(record)` in Python and `fingerprintOf(wrapper)` in Node — the Node form takes the `readRecord` wrapper `{state, record, scan}` so lexical spelling is visible — return `("current", start)`, `("other", None)` or `None`:

1. If the key `process_birth` is present (Python `"process_birth" in record`; Node `Object.hasOwn(record, "process_birth")`), the answer comes from it alone and `birth` is never consulted:
   - a string fully matching `CURRENT_SIBLING` → `("current", start)`;
   - a string fully matching `GENERATION_ONLY` at its start but not `CURRENT_SIBLING` → `("other", None)`: a generation this reader does not produce, unverifiable;
   - anything else (non-string, null, empty, malformed) → `None`.
2. Else if `birth` is a string fully matching `CANONICAL_START` and `birth_version` is the integer `1` spelled lexically as an integer (Python `type(v) is int and v == 1`; Node `record.birth_version === 1 && !spelledFractional(wrapper, "birth_version")` — and a duplicate `birth_version` key, which `scan.duplicate` reports, disqualifies) → `("current", birth)`. Migration input: 0.4.0-on-main wrote it under the canon, so it stays strictly comparable.
3. Else `None`.

Liveness (`_record_alive`): no fingerprint or `("other", …)` → pid only; `("current", start)` and `ps` unreadable → live; `("current", start)` and a different observed start → dead. `_record_liveness` (scheduling) maps `None`/`other` to `unknown` and `current` to `live`/`dead` as today; its cache key becomes `(pid, fingerprint)`.

An absent sibling beside `birth_version: 1` is never death. A present-but-malformed sibling beside a valid migration pair is `None`: pid only, and the pair is not consulted.

### The claim declares what its listener reads

`channel.mjs` adds `"fingerprint_field": "process_birth"` to every `register_peer` payload (`channel.mjs:412-421`). In `register_peer` (`antiphon.py:6080-6122`), an automatic Claude claim (`identity_digest` present) whose payload lacks `fingerprint_field == "process_birth"` is refused before `peers.register` runs:

```
register_peer: this listener predates the registry's fingerprint field and cannot govern the endpoint it would publish; reconnect the Claude session (`/mcp` → reconnect antiphon) so a current listener claims it
```

- Scope: automatic Claude claims only. Explicit-name (`ANTIPHON_NAME`) and legacy listeners are ungoverned by the verdict (`automaticProofVerdict` returns `null` without a digest), so they keep working. Codex registers from inside the Python process and never sends this payload.
- Effect on an old in-memory listener: its `claimPeer("reassert")` returns false, the listener answers `{ok:false, error:"listener could not reassert its endpoint"}` (`channel.mjs:830-835`, unchanged), the sender's existing fail-closed keeps the refusal and the bridge-authored notice, no endpoint is written, no delivery is attempted. Nothing claims recovery.
- Where the remedy is read: the refusal text reaches the listener's stderr (the host's MCP log). Doctor's existing process-start-vs-code-mtime diagnostic is the grounded place a person sees "restart/reconnect this session"; Task 4 verifies it names Claude channel processes as well as Codex MCP processes and extends it if it does not.
- Rejected alternative: writing the 0.4.0 pair for undeclared callers would keep the old listener governed but keep publishing a record the old Python reader prunes, and would need a caller-conditional response spelling. The contract prefers an honest refusal with a remedy over a working-until-pruned record.

### The registration response

`register_peer` keeps printing `{"birth": …}` — the key is an internal Node↔Python contract and the contract test pins it — but the value becomes the versioned spelling from a fresh observation, `peers.process_fingerprint(pid)`. `channel.mjs` keeps `claimedBirth` as is; `identity.mjs` compares `renderFingerprint(fingerprintOf(endpoint))` against it. The fail-closed on a missing fingerprint (`channel.mjs:531`) is untouched.

### What does not change

- Owner keys, `owner_key_version`, `owner_generations_mixed`, the session↔endpoint join and `_owner_liveness`: already versioned in the value. Task 2 pins that the old reader validates a `v1` owner key and joins an addressless Codex endpoint to its session on it.
- The reassert control path's shape, the identity-retire path, shutdown ordering.

### Rolling window this fix leaves open, by name

Records already on disk in the `birth` + `birth_version: 1` spelling stay prunable by an old Python reader until their owner rewrites them (a Claude listener on its next reassert, a Codex MCP on restart). Doctor names that record as a risk (Task 4). An old in-memory automatic Claude listener is refused on its next claim and must be reconnected; until then it is unreachable, not misdescribed. Old readers judge current records by pid alone, as they did before 0.4.0; only current readers keep the recycled-pid check.

## File map

- `test/fixtures/peers_0_3_3.py` — byte-exact `943da8a:lib/peers.py` (31,994 bytes, sha256 `4bb3ea14ab9415f84a734b16472638c09d0acfd56eba83ee96d11d3ea29a060b`; `a076723` carries the same bytes and both are pinned). Repo-only; `package.json` `files` excludes `test/`.
- `test/fixtures/README.md`, `test/fixtures/__init__.py`.
- `test/fixtures/prefix_lib.mjs` (new helper, not a fixture file): materialises `git show f0c529f:lib/{channel,identity}.mjs` beside *current* `lib/{antiphon,peers}.py` under a temp dir with a `node_modules` symlink — the exact live upgrade: old Node in memory, new Python on disk. Skips with a named reason when `git` is unavailable.
- `test/test_peers.py` — `RecycledPidTest` cross-version, precedence, grammar and owner-join cases; `FrozenReaderFixtureTest`.
- `test/test_antiphon.py` — parity fixtures; scheduling `_claim` helper; doctor note test.
- `test/test_contracts.py` — selector-shape contract.
- `test/channel.test.mjs` — old-reader-vs-live-listener E2E; mixed-Node E2E.
- `lib/peers.py` — grammar constants, `process_fingerprint`, `_fingerprint_of`, `_record_alive`, `_record_liveness`, `register`.
- `lib/antiphon.py` — `register_peer` (declaration gate, response value); doctor note.
- `lib/identity.mjs` — `fingerprintOf`, `renderFingerprint`, `automaticProofVerdict`.
- `lib/channel.mjs` — one payload field.
- `README.md`, `BACKLOG.md` — the contract in words.

---

### Task 1: Fixtures — the shipped 0.3.3 reader, and the pre-fix Node beside a current Python

**Files:**
- Create: `test/fixtures/peers_0_3_3.py`, `test/fixtures/README.md`, `test/fixtures/__init__.py`
- Create: `test/fixtures/prefix_lib.mjs`
- Modify: `test/test_peers.py` (new class at the end; `import hashlib`, `import subprocess` if absent)

**Interfaces:**
- Produces: `test.fixtures.peers_0_3_3` importable; `OLD_READER_SHA256`, `OLD_READER_COMMITS = ("943da8a", "a076723")` in `test/test_peers.py`; `materialisePrefixLib(): Promise<{dir, lib} | null>` exported from `test/fixtures/prefix_lib.mjs`.

- [ ] **Step 1: Write the failing tests**

```python
OLD_READER_SHA256 = "4bb3ea14ab9415f84a734b16472638c09d0acfd56eba83ee96d11d3ea29a060b"
OLD_READER_COMMITS = ("943da8a", "a076723")   # 0.3.3 as published; same bytes at 0.3.1's successor


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

- [ ] **Step 3: Create the fixture byte-exact and the prefix-lib helper**

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

`prefix_lib.mjs` is not a fixture file but a helper: it materialises the
pre-fix `lib/channel.mjs` and `lib/identity.mjs` (`git show f0c529f:…`)
beside the *current* `lib/antiphon.py` and `lib/peers.py`, which is exactly
what a running listener sees after an upgrade on disk — old Node in memory,
new Python spawned per registry call.
```

`test/fixtures/prefix_lib.mjs`:

```js
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

export const PRE_FIX_COMMIT = "f0c529f";

// Old Node in memory, new Python on disk: the live upgrade. Returns null when
// git history is unavailable so the caller can skip by name.
export async function materialisePrefixLib(repoRoot = process.cwd()) {
  const dir = await mkdtemp(join(tmpdir(), "antiphon-prefix-lib-"));
  const lib = join(dir, "lib");
  mkdirSync(lib);
  for (const name of ["channel.mjs", "identity.mjs"]) {
    let blob;
    try {
      blob = execFileSync("git", ["show", `${PRE_FIX_COMMIT}:lib/${name}`],
        { cwd: repoRoot });
    } catch {
      return null;
    }
    writeFileSync(join(lib, name), blob);
  }
  for (const name of ["antiphon.py", "peers.py"]) {
    copyFileSync(resolve(repoRoot, "lib", name), join(lib, name));
  }
  symlinkSync(resolve(repoRoot, "node_modules"), join(dir, "node_modules"), "dir");
  return { dir, lib };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `/usr/bin/python3 -m unittest test.test_peers.FrozenReaderFixtureTest -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```sh
git add test/fixtures test/test_peers.py
git commit -m "Test fixtures: the 0.3.3 registry reader byte-exact, and the pre-fix Node beside a current Python"
```

---

### Task 2: Every red, observed red on the pre-fix tree, before any product edit

**Files:**
- Modify: `test/test_peers.py` (`RecycledPidTest`)
- Modify: `test/test_antiphon.py` (`ReadinessParityTest._patch_endpoint` callers ~`:17690-17705`; the source-activity `_claim` helper `:12433-12448`)
- Modify: `test/test_contracts.py` (beside `test_the_birth_authority_never_comes_from_the_record_it_judges`, `:1045`)
- Modify: `test/channel.test.mjs` (after `aLiveListenerReassertsItsOwnMissingEndpoint`, `:629`; import `materialisePrefixLib`)

**Interfaces:**
- Consumes: `test.fixtures.peers_0_3_3`; `materialisePrefixLib`; existing helpers `RecycledPidTest._register/_read/_ps/LIVE/RECYCLED`, `ReadinessParityTest._proof/_withdrawn/_patch_endpoint(project, alias, drop=None, **over)`, `spawnChannel`, `makeAutomaticIdentityPython`, `registeredPeers`, `endpointFor`, `runPeers`, `sendTo`, `boundSocketOf`, `waitFor`, `waitForExit`.
- Produces: the contract Task 3 makes green. Names used by Task 3: `peers.process_fingerprint(pid)`, `peers._fingerprint_of(record)`, `peers.CURRENT_SIBLING`, `peers.CANONICAL_START`, `identity.fingerprintOf(wrapper)`, payload key `fingerprint_field`.

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
            endpoint = json.load(open(peers._peer_file(project, "codex", "x"),
                                      encoding="utf-8"))
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
        """Precedence is by key presence, not by type. A malformed sibling
        beside a valid, conflicting migration pair yields no fingerprint at
        all: pid only, and the pair is never read."""
        for bad in ("", "v1:", "1:Sat Aug 30 01:00:00 2026", "v0:x", 7, None,
                    [], {}, "v1:" + self.LIVE + "\n", "\ufeffv1:" + self.LIVE,
                    "v1:" + self.LIVE + "\x85"):
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

    def test_version_parsing_is_bounded(self):
        """A 64 KB digit run must neither raise nor cost a conversion: it is
        simply not a generation this reader names."""
        huge = "v" + "9" * 65536 + ":" + self.LIVE
        self.assertIsNone(peers._fingerprint_of({"process_birth": huge}))
        self.assertIsNone(peers._fingerprint_of(
            {"birth": self.LIVE, "birth_version": int("9" * 20)}))

    def test_the_grammar_is_the_writers_own_shape_and_nothing_wider(self):
        for start in (self.LIVE, "Wed Sep 2 16:13:13 2026"):
            self.assertEqual(peers._fingerprint_of({"process_birth": "v1:" + start}),
                             ("current", start))
            self.assertEqual(peers._fingerprint_of(
                {"birth": start, "birth_version": 1}), ("current", start))
        for start in ("Wed Sep  2 16:13:13 2026",       # padded day: the old 24-column slice
                      "Çar Eyl 2 16:13:13 2026",        # a locale
                      self.LIVE + " ", " " + self.LIVE,
                      self.LIVE + "\u0085", "\ufeff" + self.LIVE):
            with self.subTest(start=start):
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

`RecycledPidTest.UUID` already exists (`test_peers.py:570`). If `peers.register` refuses `address=None` for Codex without a mode, read `register`'s `address` handling (`peers.py:1440-1465`) and use the call the existing addressless-Codex tests use.

- [ ] **Step 2: Parity reds in `test_readiness_parity_holds_across_every_fixture`**

Change the two existing birth cases and add these; `_patch_endpoint(project, alias, drop=None, **over)` already deletes `drop` keys and sets the rest. `_own_birth()` returns `antiphon.peers._process_birth(<the pid the fixture's endpoint names>)`, measured, and the case is skipped when it is None. `_raw_endpoint(project, alias, text)` writes literal bytes to `endpoint.json` for the lexical cases (model it on `_raw`, which does this for the proof file).

```python
            "endpoint records another process's birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v1:Thu Jan 1 00:00:00 1970")),
            "rotated, endpoint records another birth": lambda p, a: (
                self._proof(p, self.B), self._withdrawn(p, a),
                self._patch_endpoint(p, a, process_birth="v1:Thu Jan 1 00:00:00 1970")),
            "migration spelling, same birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop=["process_birth"],
                                     birth=self._own_birth(), birth_version=1)),
            "migration spelling, other birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop=["process_birth"],
                                     birth="Thu Jan 1 00:00:00 1970", birth_version=1)),
            "migration spelling, version spelled 1.0": lambda p, a: (
                self._proof(p, self.A),
                self._raw_endpoint(p, a, self._endpoint_json(p, a, drop=["process_birth"],
                    birth="Thu Jan 1 00:00:00 1970").replace("}", ', "birth_version": 1.0}', 1))),
            "migration spelling, version spelled 1e0": lambda p, a: (
                self._proof(p, self.A),
                self._raw_endpoint(p, a, self._endpoint_json(p, a, drop=["process_birth"],
                    birth="Thu Jan 1 00:00:00 1970").replace("}", ', "birth_version": 1e0}', 1))),
            "migration spelling, version key escaped": lambda p, a: (
                self._proof(p, self.A),
                self._raw_endpoint(p, a, self._endpoint_json(p, a, drop=["process_birth"],
                    birth="Thu Jan 1 00:00:00 1970").replace("}", ', "birth\\u005fversion": 1}', 1))),
            "migration spelling, version key duplicated": lambda p, a: (
                self._proof(p, self.A),
                self._raw_endpoint(p, a, self._endpoint_json(p, a, drop=["process_birth"],
                    birth="Thu Jan 1 00:00:00 1970").replace("}", ', "birth_version": 2, "birth_version": 1}', 1))),
            "malformed sibling beside a conflicting valid pair": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth=7,
                                     birth="Thu Jan 1 00:00:00 1970", birth_version=1)),
            "sibling of a future generation": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v2:whatever")),
            "sibling with a trailing NEL": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v1:Thu Jan 1 00:00:00 1970\u0085")),
            "sibling with a leading BOM": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="\ufeffv1:Thu Jan 1 00:00:00 1970")),
            "migration birth with a trailing NEL": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop=["process_birth"],
                                     birth="Thu Jan 1 00:00:00 1970\u0085", birth_version=1)),
            "migration birth with a leading BOM": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop=["process_birth"],
                                     birth="\ufeffThu Jan 1 00:00:00 1970", birth_version=1)),
```

Where the harness passes a `listenerBirth` to Node, pass `peers.process_fingerprint(pid)`. The parity assertion is unchanged: both readers return the same verdict for every case.

- [ ] **Step 3: Contract red**

```python
    def test_the_two_readers_select_the_fingerprint_the_same_way(self):
        """One selector per language, one ASCII grammar, precedence by key
        presence, and the Node form sees the lexical scan."""
        node = read("lib", "identity.mjs")
        self.assertIn("export function fingerprintOf(wrapper)", node)
        self.assertIn('Object.hasOwn(record, "process_birth")', node)
        self.assertIn('spelledFractional(wrapper, "birth_version")', node)
        python = read("lib", "peers.py")
        self.assertIn("def _fingerprint_of(record)", python)
        self.assertIn('"process_birth" in record', python)
        self.assertNotIn("def _birth_of(", python)
        grammar = r"[A-Z][a-z]{2} [A-Z][a-z]{2} [0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}"
        self.assertIn(grammar, python)
        self.assertIn(grammar, node)
        self.assertIn('"fingerprint_field": "process_birth"',
                      read("lib", "channel.mjs").replace("fingerprint_field:", '"fingerprint_field":'))
```

- [ ] **Step 4: Node E2E reds**

Add beside `runPeers` (`channel.test.mjs:1928`):

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
```

Then the two cases, after `aLiveListenerReassertsItsOwnMissingEndpoint`:

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

async function aPreFixListenerIsRefusedRatherThanToldItRecovered() {
  // Old Node in memory, new Python on disk — the live upgrade. The listener's
  // in-memory verdict reads `birth`; the Python it now shells writes no such
  // field. A reassert must be refused with a remedy, write nothing, and never
  // answer `reasserted` for a record the listener cannot govern.
  const prefix = await materialisePrefixLib();
  if (!prefix) { console.log("pre-fix listener vs current python: skipped (no git)"); return; }
  const dir = await mkdtemp(join(tmpdir(), "antiphon-mixed-node-"));
  const stub = await makeAutomaticIdentityPython({
    alias: STALE_A_ALIAS, identity_digest: STALE_A_DIGEST, session_id: STALE_A,
  });
  const env = { ...process.env, ...stub.env, ANTIPHON_CWD: dir };
  delete env.ANTIPHON_NAME;
  const child = spawn("node", [join(prefix.lib, "channel.mjs")], { env, stdio: ["pipe", "pipe", "pipe"] });
  let stderr = ""; child.stderr.setEncoding("utf8"); child.stderr.on("data", (c) => { stderr += c; });
  let socket = null;
  try {
    await waitFor(() => /channel ready: (\S+)/.test(stderr));
    socket = /channel ready: (\S+)/.exec(stderr)?.[1] ?? null;
    assert.ok(socket && existsSync(socket), `pre-fix listener never bound: ${stderr}`);
    // The hook half and a current proof, so governance is the only open question.
    runPeers(dir, `
import json, os
path = os.path.join(${JSON.stringify(dir)}, ".antiphon", "peers", "claude-${STALE_A_ALIAS}", "endpoint.json")
owner = json.load(open(path))["owner"]
peers.write_session(${JSON.stringify(dir)}, "claude", "${STALE_A_ALIAS}", ${JSON.stringify(STALE_A)},
                    "/t/a.jsonl", owner, ${JSON.stringify(STALE_A_DIGEST)}, True)
peers.write_identity_proof(${JSON.stringify(dir)}, owner, ${JSON.stringify(STALE_A)}, ${JSON.stringify(STALE_A_DIGEST)})
`);
    const endpoint = endpointFor(dir, STALE_A_ALIAS);
    await rm(endpoint, { force: true });
    const reply = JSON.parse(await sendTo(socket, JSON.stringify({
      control: "antiphon.channel", version: 1, action: "reassert",
      alias: STALE_A_ALIAS, nonce: "mixed-node",
    })));
    assert.equal(reply.ok, false, `a pre-fix listener must not claim recovery: ${JSON.stringify(reply)}`);
    assert.notEqual(reply.action, "reasserted");
    assert.ok(!existsSync(endpoint), "and publishes no endpoint it cannot govern");
    assert.match(stderr, /predates the registry's fingerprint field[\s\S]*reconnect the Claude session/,
      `the remedy reaches the listener's log: ${stderr}`);
    const words = await sendTo(socket, JSON.stringify({ content: "hi" }));
    assert.equal(JSON.parse(words)?.ok, false, "and the words are refused, not delivered");
  } finally {
    child.kill("SIGKILL");
    await waitForExit(child, 2_000);
    if (socket) await rm(socket, { force: true }).catch(() => {});
    for (const p of [dir, stub.dir, prefix.dir]) await rm(p, { recursive: true, force: true }).catch(() => {});
  }
}

await thePublishedReaderLeavesALiveListenerRegistered();
await aPreFixListenerIsRefusedRatherThanToldItRecovered();
```

Add `import { materialisePrefixLib } from "./fixtures/prefix_lib.mjs";` at the top. If `sendTo` in this file takes a socket path and a string, the calls match; otherwise read its signature (`grep -n "^async function sendTo" test/channel.test.mjs`) and adapt the two calls only.

- [ ] **Step 5: Observe every red on the pre-fix tree, for the reasons named**

Run, in the foreground:

```sh
/usr/bin/python3 -m unittest test.test_peers.RecycledPidTest -v 2>&1 | tail -40
/usr/bin/python3 -m unittest test.test_antiphon.ReadinessParityTest -v 2>&1 | tail -30
/usr/bin/python3 -m unittest test.test_contracts -k fingerprint -v
node test/channel.test.mjs 2>&1 | grep -B2 -A8 "old reader\|pre-fix\|AssertionError" | head -60
```

Expected, each for the reason that names the defect:

- `test_the_published_reader_leaves_a_current_record_alone`: `[] != ['ui']` — the shipped reader pruned. **This is the P0 under test.**
- `test_a_record_carries_its_fingerprint_where_the_old_reader_never_looks`: `KeyError: 'process_birth'`.
- `test_the_published_reader_validates_a_versioned_owner_and_joins_on_it`: PASS (pins today's join; the mutation in Task 3 Step 4 proves it protects).
- `test_the_current_reader_still_prunes_a_recycled_pid`: PASS (pins today's strictness).
- migration / precedence / future / bounded / grammar / response cases: `AttributeError: _fingerprint_of` or `KeyError`.
- parity: Node and Python disagree on the migration-spelling and lexical cases (record which side says what; that table goes into the Task 3 commit message).
- contract: missing export and grammar.
- `thePublishedReaderLeavesALiveListenerRegistered`: `[] != ["ui"]` — real reader, real listener, real timezone.
- `aPreFixListenerIsRefusedRatherThanToldItRecovered`: `reply.ok` is `true` and `action === "reasserted"` — the pre-fix Python accepts the undeclared claim. **This is the Node half of the matrix under test.**

- [ ] **Step 6: Commit the reds**

```sh
git add test/test_peers.py test/test_antiphon.py test/test_contracts.py test/channel.test.mjs
git commit -m "Red: the shipped reader prunes a current endpoint, and a pre-fix listener is told it recovered"
```

---

### Task 3: One atomic product change across both readers and the claim

**Files:**
- Modify: `lib/peers.py:69-70` (constants), `:1139-1161` (`_birth_of`, `_birth_is_current` → `_fingerprint_of`), `:1197-1258` (`_record_alive`, `_record_liveness`), `:1536-1541` (`register`), after `_process_birth` (~`:1700`, `process_fingerprint`)
- Modify: `lib/identity.mjs` (`fingerprintOf`, `renderFingerprint`, `automaticProofVerdict:359-360`)
- Modify: `lib/antiphon.py:6080-6122` (`register_peer`: declaration gate; response value)
- Modify: `lib/channel.mjs:412-421` (payload field)
- Modify: `test/test_antiphon.py:12433-12448` (`_claim` writes the new spelling by default; `endpoint_birth_version=1` writes the legacy pair, `None` writes unversioned `birth`)

**Interfaces:**
- Produces: `peers.CANONICAL_START` (str), `peers.CURRENT_SIBLING` (compiled), `peers.GENERATION_ONLY` (compiled), `peers.process_fingerprint(pid) -> str | None`, `peers._fingerprint_of(record) -> ("current", str) | ("other", None) | None`; `identity.fingerprintOf(wrapper) -> {kind:"current", start} | {kind:"other"} | null`; `identity.renderFingerprint(fp) -> string | null`.

- [ ] **Step 1: Python**

Constants beside `OWNER_KEY_VERSION`:

```python
# The exact shape `_process_info` renders under LC_ALL=C and TZ=UTC: C-locale
# weekday and month, an unpadded day, a 24-hour clock, a four-digit year, single
# spaces. Spelled identically in identity.mjs. Only this shape may authorise
# "dead": anything wider would let a reader's own notion of blank or word take
# part, and the two runtimes disagree on U+0085 and U+FEFF.
CANONICAL_START = (r"[A-Z][a-z]{2} [A-Z][a-z]{2} [0-9]{1,2} "
                   r"[0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}")
CURRENT_SIBLING = re.compile(rf"v{PROCESS_FINGERPRINT_VERSION}:({CANONICAL_START})")
# Some generation, bounded: a reader that names v1 must neither convert nor
# choke on a 64 KB digit run, only recognise that this is not its own.
GENERATION_ONLY = re.compile(r"v[0-9]{1,9}:.*", re.DOTALL)
```

Replace `_birth_of`/`_birth_is_current`:

```python
def _fingerprint_of(record):
    """`("current", start)`, `("other", None)` or None for a record.

    Precedence is by key presence. A present `process_birth` is the only
    thing consulted, however it is spelled: the 0.3.x reader interpreted
    `birth` against its own timezone and pruned live listeners while
    `birth_version` sat unread beside it, so the canonical value lives where
    that reader never looks, and nothing here falls back past it. The 0.4.0
    pair — canonical `birth` beside a lexical integer `birth_version` 1 — is
    migration input, read strictly. Everything else is evidence of nothing.
    """
    if not hasattr(record, "get"):
        return None
    if "process_birth" in record:
        sibling = record.get("process_birth")
        if not isinstance(sibling, str):
            return None
        matched = CURRENT_SIBLING.fullmatch(sibling)
        if matched:
            return ("current", matched.group(1))
        return ("other", None) if GENERATION_ONLY.fullmatch(sibling) else None
    birth = record.get("birth")
    version = record.get("birth_version")
    if (isinstance(birth, str) and re.fullmatch(CANONICAL_START, birth)
            and type(version) is int and version == 1):
        return ("current", birth)
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

Python JSON parsing: `json.load` keeps the *last* duplicate key and turns `1.0`/`1e0` into `float`, which `type(version) is int` refuses; a duplicated `birth_version` whose last value is `1` is accepted by Python. Task 2's parity fixture "version key duplicated" therefore pins whatever both readers agree on — read `readRecord`'s duplicate handling (`identity.mjs:150-175`) and make Python match it through `_read_record`'s existing scan if one exists, else document the agreed verdict in the fixture's comment.

- [ ] **Step 2: Node**

`lib/identity.mjs`, exported:

```js
const CANONICAL_START = "[A-Z][a-z]{2} [A-Z][a-z]{2} [0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}";
const CURRENT_SIBLING = new RegExp(`^v${PROCESS_FINGERPRINT_VERSION}:(${CANONICAL_START})$`);
const GENERATION_ONLY = /^v[0-9]{1,9}:[\s\S]*$/;

// The same selection Python's `_fingerprint_of` makes, in the same order and
// on the same grammar. Takes the readRecord wrapper, not the bare record,
// because how `1` was spelled is only visible in the scan.
export function fingerprintOf(wrapper) {
  const record = wrapper?.record;
  if (!record || typeof record !== "object") return null;
  if (Object.hasOwn(record, "process_birth")) {
    const sibling = record.process_birth;
    if (typeof sibling !== "string") return null;
    const matched = CURRENT_SIBLING.exec(sibling);
    if (matched) return { kind: "current", start: matched[1] };
    return GENERATION_ONLY.test(sibling) ? { kind: "other" } : null;
  }
  const birth = record.birth;
  if (typeof birth === "string" && new RegExp(`^${CANONICAL_START}$`).test(birth)
      && record.birth_version === 1
      && !spelledFractional(wrapper, "birth_version")
      && !wrapper.scan?.duplicate?.has?.("birth_version")) {
    return { kind: "current", start: birth };
  }
  return null;
}

export function renderFingerprint(fingerprint) {
  return fingerprint?.kind === "current"
    ? `v${PROCESS_FINGERPRINT_VERSION}:${fingerprint.start}` : null;
}
```

`PROCESS_FINGERPRINT_VERSION` in `identity.mjs`: define it as `1` beside the other constants if absent, and add to the contract test in Task 2 Step 3 an assertion that `identity.mjs` and `peers.py` name the same number (read both with a regex). Adapt the `duplicate` check to what `readRecord`'s scan actually exposes (`identity.mjs:150-175`, `return { duplicate, integral }`).

In `automaticProofVerdict`:

```js
      || (typeof listenerBirth === "string"
          && renderFingerprint(fingerprintOf(endpoint)) !== listenerBirth)
```

`lib/channel.mjs:412-421`, the payload:

```js
  const payload = {
    kind: "claude", name: peerId, address: socketPath, pid: process.pid,
    // What this listener's in-memory verdict reads. A listener that predates
    // the field is refused by the registry rather than handed a record it
    // would then refuse to govern.
    fingerprint_field: "process_birth",
  };
```

- [ ] **Step 3: The gate and the response in `register_peer`**

Before the `mode` check (`antiphon.py:6100`):

```python
    if data.get("identity_digest") is not None and kind == "claude" \
            and data.get("fingerprint_field") != "process_birth":
        print("register_peer: this listener predates the registry's "
              "fingerprint field and cannot govern the endpoint it would "
              "publish; reconnect the Claude session (`/mcp` → reconnect "
              "antiphon) so a current listener claims it", file=sys.stderr)
        return 1
```

And the response:

```python
    print(json.dumps({"birth": peers.process_fingerprint(data.get("pid"))}))
```

- [ ] **Step 4: Green, then mutate each guard**

Run the four commands from Task 2 Step 5; expected all PASS. Then, one at a time, restore each by reverse edit (never `git checkout`, which discards Step 1-3):

| Mutation | Named test that must fail |
|---|---|
| `register` writes `record["birth"] = birth; record["birth_version"] = 1` instead of the sibling | `test_the_published_reader_leaves_a_current_record_alone`, `thePublishedReaderLeavesALiveListenerRegistered` |
| `_fingerprint_of` uses `isinstance(sibling, str)` as the precedence test instead of key presence | `test_a_present_sibling_is_the_only_thing_consulted` (the `7`/`None` subtests) |
| `_fingerprint_of` accepts `version == 1` without `type(version) is int` | parity "version spelled 1.0" |
| `CANONICAL_START` widened to `.+` | `test_the_grammar_is_the_writers_own_shape_and_nothing_wider`, parity NEL/BOM cases |
| `register_peer` gate removed | `aPreFixListenerIsRefusedRatherThanToldItRecovered` |
| `valid_owner_key` in the fixture made to refuse `v1` keys (edit the fixture copy, then restore the exact bytes: `git checkout -- test/fixtures/peers_0_3_3.py` is safe here, it is a committed fixture) | `test_the_published_reader_validates_a_versioned_owner_and_joins_on_it` |

- [ ] **Step 5: The whole tree, foreground**

```sh
/usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3
node test/channel.test.mjs 2>&1 | tail -3
git diff f0c529f --stat -- lib/channel.mjs      # one hunk: the payload field
```

Expected: `OK (skipped=2)` (the fixture-blob test skips only without git); Node suite ok; the channel diff is the payload field alone — `claimedBirth`'s block (`channel.mjs:420-431, 461-465, 531`) byte-identical.

- [ ] **Step 6: Commit**

```sh
git add lib/peers.py lib/identity.mjs lib/antiphon.py lib/channel.mjs test/test_antiphon.py
git commit -m "Registry: the fingerprint moves where the 0.3.x reader never looks, and a claim says what its listener reads"
```

The commit body carries the parity table recorded in Task 2 Step 5.

---

### Task 4: Doctor names the migration record as a risk, and names the listener it cannot reach

**Files:**
- Modify: `lib/antiphon.py` (~`:8700-8720`, the doctor per-record loop; the process-start-vs-code-mtime diagnostic)
- Modify: `test/test_antiphon.py` (the class holding the existing "restart that Codex session" test; find it with `grep -n "restart that" test/test_antiphon.py`)

*Bounded on purpose: one note and one verification, read-only, no recovery. Nothing above depends on it.*

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

    def test_doctor_judges_a_claude_channel_by_its_start_against_its_code(self):
        """The grounded stale-reader diagnostic already exists for Codex MCP
        processes. It has to cover a Claude channel process the same way, since
        that is the listener the registry now refuses."""
        # Model on the existing Codex-MCP case in this class: a fake process
        # table naming `node …/channel.mjs` started before lib/channel.mjs's
        # mtime; assert the same "its code changed … reconnect" line names it.
```

Write `_register_migration_spelling` with `peers.register` plus a direct JSON rewrite (pattern in Task 2), and `_doctor` as the class already does. Read the existing Codex-MCP test first and copy its process-table seam for the Claude case.

- [ ] **Step 2: Run to verify they fail**

Run: `/usr/bin/python3 -m unittest test.test_antiphon -k doctor_names -k doctor_judges -v`
Expected: FAIL — the note is absent; the channel case either fails or already passes (if it passes, keep it: it pins the coverage the design relies on, and say so in the commit).

- [ ] **Step 3: Implement**

In the doctor loop after the `mixed_owner_generation` branch:

```python
        if (peers._fingerprint_of(record) is not None
                and "process_birth" not in record):
            remedy = ("this listener reasserts or reconnects"
                      if record.get("kind") == "claude"
                      else "that Codex session restarts")
            report.note(f"peer {who}: fingerprint in the 0.4.0 spelling; a "
                        f"0.3.x reader, if one is still running, prunes it "
                        f"until {remedy}")
```

Extend the process-start-vs-code-mtime diagnostic to `node …/lib/channel.mjs` processes if it does not already cover them, with the same sentence shape and "reconnect the Claude session" as the remedy.

- [ ] **Step 4: Green, then the doctor classes whole**

Run: `/usr/bin/python3 -m unittest test.test_antiphon -k doctor -v 2>&1 | tail -3`
Expected: OK.

- [ ] **Step 5: Commit**

```sh
git add lib/antiphon.py test/test_antiphon.py
git commit -m "Doctor names the record an old reader may still prune, and the listener it cannot reach"
```

---

### Task 5: The contract in words, and the whole tree verified at one SHA

**Files:**
- Modify: `README.md` (grep `birth` and `rolling` first; add one paragraph to the upgrade/identity section only if it discusses process identity)
- Modify: `BACKLOG.md` (*What is on `main` at 0.4.0* paragraph beginning "New process observations now run `ps` under `LC_ALL=C`…", ~`:1428-1436`; and one *Start here* line)

- [ ] **Step 1: Write the words**

BACKLOG, replacing "A legacy endpoint birth retains PID-only liveness until its owner refreshes it: its old rendered value is evidence of nothing under the new canon." with:

```markdown
The canonical fingerprint is written as `process_birth: "v1:<start>"`, a field
the 0.3.x reader never selects, because that reader interpreted `birth`
against its own timezone and pruned live listeners while `birth_version` sat
unread beside it (reproduced 2026-09-02 with the byte-exact 0.3.3 reader,
`test/fixtures/peers_0_3_3.py`). Both readers select on one ASCII grammar, by
key presence: a present sibling is the only thing consulted; the 0.4.0 pair
(`birth` + lexical integer `birth_version: 1`) is migration input read
strictly and never dead for lacking the sibling; anything else is pid-only.
A claim now declares `fingerprint_field`, and an automatic Claude listener
that predates it is refused with a reconnect remedy rather than handed a
record its in-memory verdict would then refuse — the alternative told the
sender it had recovered and then refused the words. Records still in the
0.4.0 spelling stay prunable by a 0.3.x reader until their owner rewrites
them; doctor names that as a risk. Old readers judge current records by pid
alone, as they always did; only current readers keep the recycled-pid check.
```

*Start here*, one line, by mechanism and not by branch: "Mixed-version pruning (a 0.3.x reader deleting current endpoints; an upgraded-on-disk listener told it recovered) is closed by the `process_birth` sibling and the `fingerprint_field` claim declaration — see the 0.4.0 paragraph."

- [ ] **Step 2: The three roads, at the exact SHA, in the foreground**

```sh
/usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3
node test/channel.test.mjs 2>&1 | tail -3
ANTIPHON_NAME=ui /usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3
git status --short        # only intended files
git rev-parse HEAD
```

- [ ] **Step 3: Commit and hand the SHA to Codex for review**

```sh
git add README.md BACKLOG.md
git commit -m "The fingerprint move, in words: what old readers do, what stays prunable, who is refused"
git rev-parse HEAD
```

Stop here. Merge, push and publish wait for Codex's review on that exact SHA and the user's separate approval.

---

## Self-review against plan review 1

- **C1 mixed-Node matrix:** *Evidence* names the mechanism (`bridgeScript` per call, in-memory verdict); *Design → The claim declares what its listener reads* fixes the declaration and the refusal; Task 2 Step 4's `aPreFixListenerIsRefusedRatherThanToldItRecovered` runs the real pre-fix Node against the current Python and asserts no `reasserted`, no endpoint, remedy in the log, words refused; observed red on the pre-fix tree (Task 2 Step 5). The rejected alternative is stated.
- **I2 lexical spelling:** `fingerprintOf(wrapper)`; parity fixtures for `1.0`, `1e0`, escaped key, duplicate key.
- **I3 precedence by presence:** rule 1 keyed on `in`/`Object.hasOwn`; "malformed sibling beside a conflicting valid pair" in both unit and parity suites; the named mutation in Task 3 Step 4.
- **I4 shared grammar:** `CANONICAL_START` identical in both files (contract test reads it back from both); no strip/trim; NEL/BOM cases in unit and parity suites, sibling and migration alike.
- **I5 bounded versions, right generation:** `GENERATION_ONLY` is `[0-9]{1,9}`, no conversion; 64 KB digit test; rendering through `_render_fingerprint` from `PROCESS_FINGERPRINT_VERSION`, and the contract test pins the number across both files.
- **I6 owner test:** asserts `old.valid_owner_key` directly and `old._session_address` on an addressless Codex endpoint; mutation of the validator listed.
- **I7 sequencing:** Task 2 is every red including both E2Es, committed red; Task 3 is one atomic product commit; no production commit while knowingly red.
- **I8 doctor:** the "pre-0.4.0 server still running" inference is struck; the migration note is risk-worded with a remedy by kind; the grounded process-start-vs-code-mtime diagnostic is verified to cover Claude channels.
- **Corrections:** fixture anchored to `943da8a` (0.3.3) with `a076723` also pinned; nothing ships under `test/`; no version other than the unpublished 0.4.0 is named; *Start here* states the fix by mechanism.
- **Open for critique:** (1) whether the `fingerprint_field` refusal should also apply to `mode == "initial"` from a process that is somehow old at startup — the plan refuses both modes, on the reasoning that an undeclared automatic claim cannot govern its record whichever mode it names; (2) Python's `json.load` duplicate-key semantics versus Node's scan, resolved in Task 3 Step 1 by whichever `_read_record` already does — if Python has no scan, the parity fixture documents the agreed verdict rather than inventing one.
