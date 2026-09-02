# Mixed-Version Endpoint Pruning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Red first on every task; mutate the guard and watch the named test fail before calling it green.

**Goal:** A 0.3.1-era reader that is still running must leave a current endpoint record alone, while the current reader keeps refusing a true pid/birth mismatch.

**Architecture:** The canonical process fingerprint moves out of the field the old reader interprets (`birth`) into a sibling field the old reader never selects (`process_birth`, versioned in its value exactly like the owner key). Writers stop emitting `birth`/`birth_version`; readers in both languages select the new field first, accept the transitional `birth` + `birth_version: 1` spelling as migration input, and treat an absent or future-generation fingerprint as unverifiable rather than dead. The registration response keeps returning an independently observed fingerprint, now in the same versioned spelling, so the Node listener's fail-closed comparison is unchanged in shape.

**Tech Stack:** Python 3.9+ standard library and `unittest`; Node.js ESM and `node:assert`; the frozen 0.3.1 `lib/peers.py` as a test fixture.

**Spec:** this document's *Design* section. The defect is reproduced, not inferred; see *Evidence*.

## Global Constraints

- Work only in `.worktrees/p0-mixed-version-birth` on branch `p0/mixed-version-birth`, from `f0c529f`. One writer owns it.
- Do not touch the live `.antiphon/` registry, cursors or sockets of the repository; every fixture lives under a `tempfile.TemporaryDirectory()` or `mkdtemp`.
- Do not touch the user's untracked `docs/superpowers/{plans,specs}/2026-08-*` files.
- Python floor is 3.9 (`/usr/bin/python3` 3.9.6 is one of the three verification roads); no `match`, no `str.removeprefix` assumptions beyond 3.9.
- The registry is read by two languages: a rule added to one reader is a divergence until it is added to the other, and `test_readiness_parity_holds_across_every_fixture` drives both.
- The registration response's birth authority stays the operation's own return, never a read-back of the record (`test_the_birth_authority_never_comes_from_the_record_it_judges`).
- Stop at a locally reviewed exact commit. No merge, push, version bump or publish without the normal exact-SHA gates.

---

## Evidence

Measured on `f0c529f` (2026-09-02), in a scratch copy of the live registry, nothing live touched:

| Reader | Record | `_record_alive` | `read_peers` | endpoint.json after |
|---|---|---|---|---|
| `a076723:lib/peers.py` (0.3.1), `TZ=Europe/Istanbul` | current (`birth` UTC, `birth_version: 1`) | False | `[]` | **deleted** |
| same old reader, same TZ | same record with `birth` removed and a sibling `process_birth` added | True | `[alias]` | present |
| same old reader | `owner` of the form `pid:v1:start` | n/a | accepted by `valid_owner_key` | n/a |

The old `_record_alive` (a076723 lines 315-345) reads `birth` unconditionally and compares it against its own `ps lstart`, rendered in the reader's inherited timezone. The current writer (`lib/peers.py:1536-1541`) renders under `TZ=UTC`/`LC_ALL=C` and adds `birth_version: 1`, which the old reader never consults. A long-lived old MCP server therefore prunes every current endpoint it enumerates, repeatedly, until it is restarted. This is the failure Codex measured live: `endpoint.json` vanished, status showed "current automatic identity with no channel yet", the stale MCP's `antiphon_send` refused the alias, and the current `send_to_claude` reassert restored the record and delivered once.

## Design

### The record

Current writer output for a fingerprintable process:

```json
{"kind": "claude", "name": "…", "pid": 77348, "address": "…",
 "started_at": 1788366537.8, "owner": "77000:v1:Wed Sep 2 16:13:00 2026",
 "process_birth": "v1:Wed Sep 2 16:13:13 2026"}
```

- `process_birth` is `"v<N>:<canonical lstart>"`, `N = PROCESS_FINGERPRINT_VERSION`. The version lives in the value, as `owner_key` already does, because a marker in a sibling field protects nobody who ignores the sibling: that is exactly this defect.
- `birth` and `birth_version` are no longer written. An old reader finds no `birth`, takes the documented pid-only road (`recorded is None → True`), and keeps the record. That is the pre-0.4.0 behaviour those readers always had; it is not a regression for them.
- Writing a legacy `birth` for old readers' benefit is rejected: the old reader compares against its own timezone, which the writer cannot know.

### Reading a fingerprint

One selector in each language, `_fingerprint_of(record)` in Python and `fingerprintOf(record)` in Node, returning `(version, start)` or nothing:

1. `process_birth` is a string matching `^v([1-9][0-9]*):(\S(?:.*\S)?)$` → `(N, start)`.
2. else `birth` is a non-blank string and `birth_version` is the integer `1` (not `1.0`, not `"1"`; the parity suite already pins spelled versions) → `(1, birth)`. Migration input: those records were written by 0.4.0 code under the UTC canon and remain strictly comparable.
3. else → `None`. Covers records that predate the field, unversioned `birth`, a `birth_version` that is not `1`, and malformed `process_birth`.

Liveness then reads as today (`_record_alive`): no fingerprint → pid only; fingerprint of a generation this reader does not produce → pid only (a future writer's value is not a corpse); current generation and `ps` unreadable → live; current generation and a different observed start → dead. `_record_liveness` (scheduling) maps the same cases to `unknown` / `live` / `dead` and its cache key becomes `(pid, fingerprint)`.

An absent `process_birth` beside `birth_version: 1` is never death. A record whose `process_birth` and `birth` disagree is read by rule 1 alone; the legacy pair is ignored, not reconciled.

### The registration response

`register_peer` in `lib/antiphon.py:6121` keeps printing `{"birth": …}` — the key is an internal Node↔Python contract and the contract test pins it — but the value becomes the versioned spelling produced by a new `peers.process_fingerprint(pid)`, observed independently of the record exactly as now. `channel.mjs` keeps `claimedBirth` as is. `identity.mjs:359-360` compares `fingerprintOf(endpoint.record)` rendered back to `"v<N>:<start>"` against `claimedBirth`; a listener never reads its own record to learn its fingerprint. The fail-closed on a missing fingerprint (`channel.mjs:531`) is untouched.

### What does not change

- Owner keys, `owner_key_version`, `owner_generations_mixed`, the session↔endpoint join and `_owner_liveness`: already versioned in the value, already accepted structurally by the old reader (measured above). A test pins that the old reader still enumerates a record carrying a `v1` owner.
- The reassert control path: it rewrites the record through `register`, so every current listener heals its own record into the new spelling on the first refusal it recovers from.
- Doctor's existing mixed-generation note.

### Rolling window this fix leaves open, by name

Records already on disk in the `birth` + `birth_version: 1` spelling stay prunable by an old reader until their owner refreshes them (restart, or the reassert path). Doctor names that state (Task 6). A truly recycled pid is judged by old readers with pid-only liveness, as before 0.4.0; only current readers keep the strict check. Both are stated in README and BACKLOG in Task 7.

## File map

- `test/fixtures/peers_0_3_1.py` — byte-exact copy of `a076723:lib/peers.py` (31,994 bytes, sha256 `4bb3ea14ab9415f84a734b16472638c09d0acfd56eba83ee96d11d3ea29a060b`), header comment added *only* as a separate `README` beside it so the bytes stay comparable. The real old reader, importable on every verification road including the npm tarball, where `git show` is not available.
- `test/fixtures/README.md` — what the fixture is, its origin commit and hash, and that it is never imported by product code.
- `test/test_peers.py` — `RecycledPidTest` grows the cross-version and migration cases; a new `FrozenReaderFixtureTest` pins the fixture's hash against the blob when git is present.
- `lib/peers.py` — `PROCESS_BIRTH_PATTERN`, `process_fingerprint`, `_fingerprint_of`, `_record_alive`, `_record_liveness`, `register`.
- `lib/antiphon.py` — `register_peer` response; doctor note.
- `lib/identity.mjs` — `fingerprintOf`, `automaticProofVerdict`.
- `lib/channel.mjs` — none expected; verify the `claimedBirth` block is byte-identical after Task 4.
- `test/test_antiphon.py` — parity fixtures for the migration spelling; scheduling `_claim` helper writes the new spelling; doctor note test.
- `test/channel.test.mjs` — the rewritten-endpoint case writes `process_birth`.
- `README.md`, `BACKLOG.md` — the contract in words.

---

### Task 1: The frozen 0.3.1 reader is a fixture, and it is the real one

**Files:**
- Create: `test/fixtures/peers_0_3_1.py`
- Create: `test/fixtures/README.md`
- Create: `test/fixtures/__init__.py` (empty; `unittest discover -s test` imports by package)
- Modify: `test/test_peers.py` (new class at the end)

**Interfaces:**
- Produces: `test.fixtures.peers_0_3_1` importable as a module; `OLD_READER_SHA256` constant in `test/test_peers.py`.

- [ ] **Step 1: Write the failing test**

```python
OLD_READER_SHA256 = "4bb3ea14ab9415f84a734b16472638c09d0acfd56eba83ee96d11d3ea29a060b"
OLD_READER_COMMIT = "a076723"


class FrozenReaderFixtureTest(unittest.TestCase):
    """The cross-version tests drive the reader 0.3.1 actually shipped, not a
    model of it. The fixture is byte-exact, and when git history is at hand
    the blob is compared too, so a hand edit to the fixture cannot quietly
    turn the old reader into a kinder one."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "peers_0_3_1.py")

    def test_the_fixture_is_the_shipped_reader(self):
        with open(self.FIXTURE, "rb") as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        self.assertEqual(digest, OLD_READER_SHA256)

    def test_the_fixture_matches_the_blob_when_history_is_present(self):
        try:
            blob = subprocess.run(
                ["git", "show", f"{OLD_READER_COMMIT}:lib/peers.py"],
                capture_output=True, check=True, timeout=10,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("no git history here (npm tarball road)")
        self.assertEqual(hashlib.sha256(blob).hexdigest(), OLD_READER_SHA256)

    def test_the_fixture_imports_and_is_the_old_reader(self):
        from test.fixtures import peers_0_3_1 as old
        self.assertFalse(hasattr(old, "PROCESS_FINGERPRINT_VERSION"),
                         "0.3.1 had no fingerprint generation at all")
        self.assertTrue(callable(old._record_alive))
```

Add `import hashlib` and `import subprocess` at the top of `test/test_peers.py` if absent.

- [ ] **Step 2: Run it to verify it fails**

Run: `/usr/bin/python3 -m unittest test.test_peers.FrozenReaderFixtureTest -v`
Expected: FAIL/ERROR — `FileNotFoundError` on the fixture and `ModuleNotFoundError: test.fixtures`.

- [ ] **Step 3: Create the fixture byte-exact**

```sh
mkdir -p test/fixtures
git show a076723:lib/peers.py > test/fixtures/peers_0_3_1.py
: > test/fixtures/__init__.py
shasum -a 256 test/fixtures/peers_0_3_1.py   # must print 4bb3ea14…
```

Write `test/fixtures/README.md`:

```markdown
# Frozen readers

`peers_0_3_1.py` is `lib/peers.py` exactly as commit `a076723` (package
version 0.3.1) shipped it — the last reader that interpreted an endpoint's
`birth` without a generation marker. It is imported only by tests, never by
product code, and `FrozenReaderFixtureTest` refuses any byte that differs
from that blob. It exists because a rolling upgrade leaves that reader
running for hours inside a live MCP server, and a test that models it
instead of running it proved nothing.
```

- [ ] **Step 4: Run it to verify it passes**

Run: `/usr/bin/python3 -m unittest test.test_peers.FrozenReaderFixtureTest -v`
Expected: 3 passed (the blob test may skip on the tarball road only).

- [ ] **Step 5: Commit**

```sh
git add test/fixtures test/test_peers.py
git commit -m "Test fixture: the 0.3.1 registry reader, byte-exact"
```

---

### Task 2: Red — the old reader prunes a current record, and the fix shape keeps it

**Files:**
- Modify: `test/test_peers.py` (`RecycledPidTest`)

**Interfaces:**
- Consumes: `test.fixtures.peers_0_3_1`; `RecycledPidTest._register`, `_read`, `_ps`, `LIVE`, `RECYCLED`.
- Produces: the behavioural contract Tasks 3-4 make green: a record written by `register` carries `process_birth == "v1:<LIVE>"` and no `birth`/`birth_version`.

- [ ] **Step 1: Write the failing tests**

Add to `RecycledPidTest`, after `test_a_record_carries_the_fingerprint_of_the_process_it_names`:

```python
    # ---- a reader from the last release is still running somewhere ----

    LIVE_LOCAL = "Sat Aug 30 04:00:00 2026"   # LIVE rendered three hours east

    @staticmethod
    def _old_ps(old, start):
        return patch.object(old, "_process_info",
                            return_value=("1", start, "node server.js"))

    def test_the_last_releases_reader_leaves_a_current_record_alone(self):
        """The reproduced P0. A 0.3.1 MCP server that started before the
        upgrade keeps reading the registry with the reader it loaded, which
        compares `birth` against its own timezone's `ps`. It must find nothing
        to compare — and so keep the record on its pid alone — rather than
        prune a live listener every time it enumerates."""
        from test.fixtures import peers_0_3_1 as old
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
        self.assertNotIn("birth", record,
                         "the field the old reader interprets is absent")
        self.assertNotIn("birth_version", record)

    def test_the_old_reader_still_enumerates_a_current_owner_key(self):
        """The join field already carried its generation in the value; pin
        that the old reader accepts it structurally, so the fix for `birth`
        does not hide a second pruning road."""
        from test.fixtures import peers_0_3_1 as old
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE,
                           owner_key=f"300:v1:{self.LIVE}")
            with self._old_ps(old, self.LIVE_LOCAL):
                self.assertEqual(
                    [p["name"] for p in old.read_peers(project, "claude")],
                    ["ui"])

    # ---- the current reader keeps the strict check ----

    def test_the_current_reader_still_prunes_a_recycled_pid(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            with self._ps(self.RECYCLED):
                self.assertEqual(peers.read_peers(project, "claude"), [])
            self.assertFalse(os.path.exists(
                peers._peer_file(project, "claude", "ui")))

    def test_a_migration_spelling_is_read_strictly_and_never_as_absent(self):
        """Records 0.4.0-on-main already wrote: `birth` under the UTC canon
        beside `birth_version: 1`, and no sibling. They are migration input.
        A matching birth is live, a different one is dead, and the missing
        sibling is evidence of nothing."""
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            path = peers._peer_file(project, "claude", "ui")
            with open(path, encoding="utf-8") as stream:
                record = json.load(stream)
            del record["process_birth"]
            record["birth"] = self.LIVE
            record["birth_version"] = 1
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
            with self._ps(self.LIVE):
                self.assertEqual(
                    [p["name"] for p in peers.read_peers(project, "claude")],
                    ["ui"])
            with self._ps(self.RECYCLED):
                self.assertEqual(peers.read_peers(project, "claude"), [])

    def test_a_future_generation_is_unverifiable_not_dead(self):
        with tempfile.TemporaryDirectory() as project:
            self._register(project, self.LIVE)
            path = peers._peer_file(project, "claude", "ui")
            with open(path, encoding="utf-8") as stream:
                record = json.load(stream)
            record["process_birth"] = "v2:2026-08-30T01:00:00Z"
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
            with self._ps(self.RECYCLED):
                self.assertEqual(
                    [p["name"] for p in peers.read_peers(project, "claude")],
                    ["ui"], "a spelling this reader does not produce is not a corpse")

    def test_a_malformed_sibling_falls_back_to_the_pid(self):
        for bad in ("", "v1:", "1:Sat Aug 30 01:00:00 2026", "v0:x", 7, None):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as project:
                self._register(project, self.LIVE)
                path = peers._peer_file(project, "claude", "ui")
                with open(path, encoding="utf-8") as stream:
                    record = json.load(stream)
                record["process_birth"] = bad
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(record, stream)
                with self._ps(self.RECYCLED):
                    self.assertEqual(
                        [p["name"] for p in peers.read_peers(project, "claude")],
                        ["ui"])
```

Also update the existing `test_a_record_carries_the_fingerprint_of_the_process_it_names` to assert `record["process_birth"] == f"v1:{self.LIVE}"` instead of `record["birth"]`/`record["birth_version"]`, keeping its `started_at` assertion.

- [ ] **Step 2: Run them to verify they fail for the right reason**

Run: `/usr/bin/python3 -m unittest test.test_peers.RecycledPidTest -v`
Expected: `test_the_last_releases_reader_leaves_a_current_record_alone` FAILS with `[] != ['ui']` (the old reader pruned — the P0 reproduced under test); `..._where_the_old_reader_never_looks` FAILS with `KeyError: 'process_birth'`; the migration/future/malformed cases FAIL with `KeyError: 'process_birth'`; `test_the_current_reader_still_prunes_a_recycled_pid` PASSES (it pins today's strictness so Task 3 cannot lose it).

- [ ] **Step 3: Commit the red**

```sh
git add test/test_peers.py
git commit -m "Red: the 0.3.1 reader prunes a current endpoint it cannot judge"
```

---

### Task 3: Python — the fingerprint moves to a sibling field, versioned in the value

**Files:**
- Modify: `lib/peers.py:69-70` (constants), `:1139-1161` (`_birth_of`, `_birth_is_current`), `:1197-1258` (`_record_alive`, `_record_liveness`), `:1536-1541` (`register`), after `_process_birth` (~`:1700`)

**Interfaces:**
- Produces: `peers.PROCESS_BIRTH_PATTERN`, `peers.process_fingerprint(pid) -> str | None`, `peers._fingerprint_of(record) -> tuple[int, str] | None`. `_birth_of` and `_birth_is_current` are removed; grep confirms no other caller (`grep -n "_birth_of\|_birth_is_current" lib test`).

- [ ] **Step 1: Implement**

Constants, beside `OWNER_KEY_VERSION`:

```python
# `"v<N>:<canonical lstart>"`. The generation is in the value, as it is in the
# owner key, because a marker in a sibling field protects no reader that never
# consults the sibling: a 0.3.1 reader compared `birth` against its own
# timezone and pruned live listeners while `birth_version` sat beside it.
PROCESS_BIRTH_PATTERN = re.compile(r"v([1-9][0-9]*):(\S(?:.*\S)?)", re.DOTALL)
```

Replace `_birth_of` and `_birth_is_current` with:

```python
def _fingerprint_of(record):
    """`(generation, canonical start)` for the process the record names, or None.

    Selected in this order and never reconciled:

    - `process_birth`, the field only readers from 0.4.1 on select. The last
      release's reader interprets `birth` and nothing else, so the canonical
      value has to live where that reader does not look.
    - `birth` beside the integer `birth_version` 1: what 0.4.0-on-main wrote
      under the UTC canon before the sibling existed. Migration input, still
      strictly comparable — and never a corpse merely because the sibling is
      absent.
    - otherwise None. A record that predates the field, an unversioned
      `birth` of unknown rendering, a malformed sibling: none of these is
      evidence that the pid was recycled, so all fall back to pid liveness.
    """
    if not hasattr(record, "get"):
        return None
    sibling = record.get("process_birth")
    if isinstance(sibling, str):
        matched = PROCESS_BIRTH_PATTERN.fullmatch(sibling)
        return (int(matched.group(1)), matched.group(2)) if matched else None
    birth = record.get("birth")
    version = record.get("birth_version")
    if (isinstance(birth, str) and birth.strip()
            and isinstance(version, int) and not isinstance(version, bool)
            and version == 1):
        return (1, birth)
    return None
```

`_record_alive`, the tail:

```python
    pid = _pid_of(record)
    if pid is None or not alive(pid):
        return False
    fingerprint = _fingerprint_of(record)
    if fingerprint is None or fingerprint[0] != PROCESS_FINGERPRINT_VERSION:
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
    if (pid is not None and fingerprint is not None
            and fingerprint[0] == PROCESS_FINGERPRINT_VERSION):
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

`register`, replacing the `if birth:` block (keep the pre-lock `birth = _process_birth(owner_pid)` observation):

```python
        if birth:
            # Kept apart from `started_at`, which is when the claim was made
            # and is what the listing sorts on. This is when the process was
            # born, the half of its identity the number does not carry — and
            # it is written where the last release's reader never looks, so a
            # server still running that reader keeps this record on its pid
            # rather than pruning it against its own timezone.
            record["process_birth"] = f"{OWNER_KEY_VERSION}:{birth}"
```

New public helper beside `_process_birth`:

```python
def process_fingerprint(pid):
    """`"v<N>:<canonical start>"` for a live pid, or None when it has none.

    The spelling `register` writes, produced from a fresh observation so a
    caller can learn what its own record says without reading that record."""
    birth = _process_birth(pid)
    return f"{OWNER_KEY_VERSION}:{birth}" if birth else None
```

Update the three docstrings that still say "`birth`" in `_record_alive` (three readings paragraph) to say "fingerprint".

- [ ] **Step 2: Run the task-2 tests**

Run: `/usr/bin/python3 -m unittest test.test_peers.RecycledPidTest -v`
Expected: all PASS, including the frozen-reader case.

- [ ] **Step 3: Mutation check — the guard is the guard**

Temporarily change `record["process_birth"] = …` back to `record["birth"] = birth` in `register`; run `test.test_peers.RecycledPidTest.test_the_last_releases_reader_leaves_a_current_record_alone`; expect FAIL. Restore by reverse edit (not `git checkout`, which would discard Step 1). Then temporarily make `_fingerprint_of` return `None` for the migration spelling; run `test_a_migration_spelling_is_read_strictly_and_never_as_absent`; expect FAIL on the `RECYCLED` half. Restore.

- [ ] **Step 4: Repair the fixtures that wrote the old spelling on purpose**

`test/test_antiphon.py:12433-12448` (`_claim` in the source-activity tests) writes `birth` + `birth_version`. Change the helper to write `"process_birth": f"v1:{self.ENDPOINT_START}"` by default, and keep an `endpoint_birth_version=None` path that writes the legacy pair (`birth` alone when `None`, `birth` + `birth_version: 1` when `1`) so the existing legacy-generation cases still mean what they say. Run: `/usr/bin/python3 -m unittest test.test_antiphon -k source_activity -v` and expect the same pass/fail set as before the change (record the count).

- [ ] **Step 5: Full Python suite**

Run: `/usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3`
Expected: `OK (skipped=2)` or the parity test failing on Node — which Task 4 fixes; anything else is a regression to investigate before moving on.

- [ ] **Step 6: Commit**

```sh
git add lib/peers.py test/test_peers.py test/test_antiphon.py
git commit -m "Registry: the process fingerprint moves where the last release's reader never looks"
```

---

### Task 4: Node — the same selector, the same response contract

**Files:**
- Modify: `lib/identity.mjs:332-361` (`automaticProofVerdict`), plus a new exported `fingerprintOf`
- Modify: `lib/antiphon.py:6121` (`register_peer` response value)
- Modify: `test/channel.test.mjs:2634-2638` (the rewritten-endpoint case)
- Modify: `test/test_antiphon.py:17697-17702` and the `_patch_endpoint` helper (parity fixtures)
- Verify unchanged: `lib/channel.mjs:420-431, 461-465, 531`

**Interfaces:**
- Consumes: `peers.process_fingerprint(pid)` from Task 3.
- Produces: `fingerprintOf(record) -> {version:number, start:string} | null` exported from `identity.mjs`; the register response `{"birth": "v1:<start>"}`.

- [ ] **Step 1: Write the failing tests**

Parity, in `test_readiness_parity_holds_across_every_fixture` (`test/test_antiphon.py` ~`:17697`), change the two existing birth cases to the new spelling and add the migration and future cases:

```python
            "endpoint records another process's birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a,
                                     process_birth="v1:Thu Jan  1 00:00:00 1970")),
            "rotated, endpoint records another birth": lambda p, a: (
                self._proof(p, self.B),
                self._withdrawn(p, a),
                self._patch_endpoint(p, a,
                                     process_birth="v1:Thu Jan  1 00:00:00 1970")),
            # What 0.4.0-on-main wrote. Both readers must call a matching
            # legacy-spelled birth this listener's own, and a different one
            # not — and neither may read the missing sibling as death.
            "endpoint carries the migration spelling, same birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop=["process_birth"],
                                     birth=self._own_birth(), birth_version=1)),
            "endpoint carries the migration spelling, other birth": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, drop=["process_birth"],
                                     birth="Thu Jan  1 00:00:00 1970",
                                     birth_version=1)),
            "endpoint carries a future fingerprint generation": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="v2:whatever")),
            "endpoint carries a malformed sibling": lambda p, a: (
                self._proof(p, self.A),
                self._patch_endpoint(p, a, process_birth="1:no-generation")),
```

`_patch_endpoint(self, project, alias, drop=None, **over)` already deletes the keys in `drop` and sets the rest; no change to it. Add `_own_birth()` returning `antiphon.peers._process_birth(<the fixture's listener pid>)` — the same pid the fixture's endpoint names, so "same birth" is measured, not typed. If the parity harness drives Node with a `listenerBirth`, pass `peers.process_fingerprint(pid)` there.

`test/channel.test.mjs:2634-2638`, the rewritten-endpoint case, becomes:

```python
record.pop("birth", None); record.pop("birth_version", None)
record["process_birth"] = "v1:Thu Jan  1 00:00:00 1970"
```

Contract, in `test/test_contracts.py` beside `test_the_birth_authority_never_comes_from_the_record_it_judges`:

```python
    def test_the_two_readers_select_the_fingerprint_the_same_way(self):
        """One selector per language, and both prefer the sibling, accept the
        migration pair, and refuse to read an absent sibling as death."""
        node = read("lib", "identity.mjs")
        self.assertIn("export function fingerprintOf(record)", node)
        self.assertIn('record?.process_birth', node)
        self.assertIn('record?.birth_version === 1', node)
        python = read("lib", "peers.py")
        self.assertIn("def _fingerprint_of(record)", python)
        self.assertNotIn("def _birth_of(", python, "the old selector is gone")
```

- [ ] **Step 2: Run to verify they fail**

Run: `/usr/bin/python3 -m unittest test.test_antiphon.ReadinessParityTest test.test_contracts -k fingerprint -k parity -v` and `node test/channel.test.mjs 2>&1 | grep -i "rewritten\|birth"`.
Expected: parity FAILS on the migration cases (Node says UNREADY where Python says READY, or vice versa); the contract test FAILS on the missing export; the Node case FAILS because the listener no longer compares the field the test rewrote.

- [ ] **Step 3: Implement**

`lib/identity.mjs`, exported beside `automaticProofVerdict`:

```js
const PROCESS_BIRTH = /^v([1-9][0-9]*):(\S(?:[\s\S]*\S)?)$/;

// The same selection Python's `_fingerprint_of` makes, in the same order:
// the sibling first, the 0.4.0 migration pair second, nothing otherwise.
// Never reconciled, never read as death when absent.
export function fingerprintOf(record) {
  const sibling = record?.process_birth;
  if (typeof sibling === "string") {
    const matched = PROCESS_BIRTH.exec(sibling);
    return matched ? { version: Number(matched[1]), start: matched[2] } : null;
  }
  const birth = record?.birth;
  if (typeof birth === "string" && birth.trim()
      && record?.birth_version === 1 && !spelledFractional(record, "birth_version")) {
    return { version: 1, start: birth };
  }
  return null;
}

function renderFingerprint(fingerprint) {
  return fingerprint ? `v${fingerprint.version}:${fingerprint.start}` : null;
}
```

(`spelledFractional` already exists in this module for `pid`; reuse it so `1.0` is refused as it is in Python.)

In `automaticProofVerdict`, replace the `birth` comparison:

```js
      || (typeof listenerBirth === "string"
          && renderFingerprint(fingerprintOf(endpoint.record)) !== listenerBirth)
```

`lib/antiphon.py:6121`:

```python
    print(json.dumps({"birth": peers.process_fingerprint(data.get("pid"))}))
```

- [ ] **Step 4: Run to verify they pass, then the parity suite whole**

Run: `/usr/bin/python3 -m unittest test.test_antiphon -k parity -v && node test/channel.test.mjs 2>&1 | tail -3`
Expected: PASS; `MCP channel integration: ok`.

- [ ] **Step 5: Verify the fail-closed gate is untouched**

Run: `git diff f0c529f -- lib/channel.mjs`
Expected: empty. `test_the_birth_authority_never_comes_from_the_record_it_judges` and the Node case `no authority, no delivery` still pass.

- [ ] **Step 6: Commit**

```sh
git add lib/identity.mjs lib/antiphon.py test/channel.test.mjs test/test_antiphon.py test/test_contracts.py
git commit -m "Both readers select the fingerprint the same way; the claim answers in the same spelling"
```

---

### Task 5: End to end — a real old reader against a real current listener

**Files:**
- Modify: `test/channel.test.mjs` (new case after `aLiveListenerReassertsItsOwnMissingEndpoint`)

**Interfaces:**
- Consumes: `spawnChannel`, `registeredPeers`, `endpointFor`, `runPeers` helpers already in the file; `test/fixtures/peers_0_3_1.py`.

- [ ] **Step 1: Write the failing test**

```js
async function theLastReleasesReaderLeavesALiveListenerRegistered() {
  // The reproduced P0, end to end: a real current listener has registered,
  // and the reader 0.3.1 shipped — still running inside an MCP server that
  // started before the upgrade — enumerates the registry from a timezone
  // three hours east. Before this fix it pruned the endpoint every pass.
  const dir = await mkdtemp(join(tmpdir(), "antiphon-old-reader-"));
  const session = spawnChannel(dir, "ui");
  try {
    assert.ok(await waitFor(() => registeredPeers(dir).length === 1),
      `listener never registered: ${session.stderr()}`);
    const endpoint = endpointFor(dir, "ui");
    const listed = runOldReader(dir, `
print(json.dumps([p["name"] for p in old.read_peers(${JSON.stringify(dir)}, "claude")]))
`);
    assert.deepEqual(JSON.parse(listed.trim()), ["ui"],
      "the old reader lists the live listener");
    assert.ok(existsSync(endpoint), "and prunes nothing");
    assert.equal(registeredPeers(dir).length, 1, "the current reader agrees");
  } finally {
    session.child.kill("SIGTERM");
    await waitForExit(session.child, 2_000);
    await rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

await theLastReleasesReaderLeavesALiveListenerRegistered();
```

`runPeers(dir, code)` (`test/channel.test.mjs:1928`) discards stdout and takes no env, so add a sibling beside it rather than changing it:

```js
// The reader 0.3.1 shipped, loaded from the byte-exact fixture and run from
// a timezone three hours east of the canon, exactly as a live pre-upgrade
// MCP server would run it. Returns what the child printed.
function runOldReader(dir, code) {
  return String(execFileSync(pythonBridge(), ["-c",
    `import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("old", os.path.join("test", "fixtures", "peers_0_3_1.py"))
old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
${code}`],
    { cwd: process.cwd(),
      env: { ...process.env, ANTIPHON_CWD: dir, TZ: "Europe/Istanbul", LC_ALL: "C" } }));
}
```

- [ ] **Step 2: Verify it fails against the pre-fix writer**

Temporarily revert `lib/peers.py`'s `register` to write `record["birth"] = birth; record["birth_version"] = 1` (reverse edit, then restore by reverse edit); run `node test/channel.test.mjs 2>&1 | grep -A3 "old reader"`; expect the assertion `[] != ["ui"]` — the real old reader, real listener, real timezone. Restore.

- [ ] **Step 3: Run it green**

Run: `node test/channel.test.mjs 2>&1 | tail -3`
Expected: ok.

- [ ] **Step 4: Commit**

```sh
git add test/channel.test.mjs
git commit -m "E2E: the 0.3.1 reader leaves a live current listener registered"
```

---

### Task 6: Doctor names the rolling window instead of leaving it to be rediscovered

**Files:**
- Modify: `lib/antiphon.py` (~`:8700-8720`, the doctor per-record loop)
- Modify: `test/test_antiphon.py` (`DoctorRemedyMatchesTheVerdictTest` or the nearest doctor-note class)

*Bounded on purpose: two notes, read-only, no recovery. Codex may strike this task; nothing above depends on it.*

- [ ] **Step 1: Write the failing tests**

```python
    def test_doctor_names_a_record_an_old_reader_can_still_prune(self):
        """A record in the 0.4.0 migration spelling stays prunable by a 0.3.1
        reader until its owner refreshes it. Doctor says so, once, by name."""
        with tempfile.TemporaryDirectory() as project:
            self._register_migration_spelling(project, "ui")   # writes birth + birth_version: 1
            report = self._doctor(project)
        self.assertIn("peer claude/ui: fingerprint in the 0.4.0 spelling; a reader "
                      "from 0.3.x may still prune it until this session restarts "
                      "or reasserts", report)

    def test_doctor_names_a_live_reader_from_the_last_release(self):
        """A live endpoint whose owner key has no generation was written by
        pre-0.4.0 code, and the process that wrote it is still reading the
        registry with that code."""
        with tempfile.TemporaryDirectory() as project:
            self._register_legacy_owner(project, "codex", "x")  # owner "300:Sat Aug 30 01:00:00 2026"
            report = self._doctor(project)
        self.assertIn("peer codex/x: registered by a pre-0.4.0 server that is still "
                      "running; restart it, it prunes current endpoints it cannot "
                      "judge", report)
```

Write the two `_register_*` helpers in the test class using `peers.register` plus a direct JSON rewrite of the record (pattern as in Task 2), and `_doctor` as the class already does for its other notes (read the class first; reuse its capture helper).

- [ ] **Step 2: Run to verify they fail**

Run: `/usr/bin/python3 -m unittest test.test_antiphon -k doctor_names -v`
Expected: FAIL, the notes are absent.

- [ ] **Step 3: Implement**

In the doctor loop, after the `mixed_owner_generation` branch:

```python
        fingerprint = peers._fingerprint_of(record)
        if fingerprint is not None and not isinstance(
                record.get("process_birth"), str):
            report.note(f"peer {who}: fingerprint in the 0.4.0 spelling; a "
                        "reader from 0.3.x may still prune it until this "
                        "session restarts or reasserts")
        if owner is not None and peers.owner_key_version(owner) is None:
            report.note(f"peer {who}: registered by a pre-0.4.0 server that is "
                        "still running; restart it, it prunes current "
                        "endpoints it cannot judge")
```

- [ ] **Step 4: Run green, then the doctor classes whole**

Run: `/usr/bin/python3 -m unittest test.test_antiphon -k doctor -v 2>&1 | tail -3`
Expected: OK.

- [ ] **Step 5: Commit**

```sh
git add lib/antiphon.py test/test_antiphon.py
git commit -m "Doctor names the rolling window the fingerprint move leaves open"
```

---

### Task 7: The contract in words, and the whole tree verified at one SHA

**Files:**
- Modify: `README.md` (the process-identity / rolling-upgrade paragraph, if it names `birth`; grep first — the 2026-09-02 grep found no `birth` in README, so this may be a one-sentence addition to the upgrade note)
- Modify: `BACKLOG.md` (*What is on `main` at 0.4.0* paragraph that begins "New process observations now run `ps` under `LC_ALL=C`…", ~`:1428-1436`; and the *Start here* status line)

- [ ] **Step 1: Write the words**

BACKLOG, replacing the sentence "A legacy endpoint birth retains PID-only liveness until its owner refreshes it: its old rendered value is evidence of nothing under the new canon." with:

```markdown
The canonical fingerprint is written as `process_birth: "v1:<start>"`, a field
the 0.3.x reader never selects, because that reader interpreted `birth`
against its own timezone and pruned live listeners while `birth_version` sat
unread beside it (reproduced 2026-09-02 with the byte-exact 0.3.1 reader,
`test/fixtures/peers_0_3_1.py`). A record 0.4.0-on-main wrote in the `birth` +
`birth_version: 1` spelling is migration input — read strictly by current
readers, never dead for lacking the sibling — and stays prunable by a 0.3.x
reader until its owner restarts or reasserts; doctor names both that record
and any live pre-0.4.0 server. Old readers judge current records by pid alone,
as they always did; only current readers keep the recycled-pid check.
```

Add one line to *Start here*: "The mixed-version pruning P0 (2026-09-02) is fixed on `p0/mixed-version-birth`; see the 0.4.0 paragraph."

- [ ] **Step 2: The three roads, at the exact SHA**

```sh
/usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3      # OK (skipped=2 or 3)
node test/channel.test.mjs 2>&1 | tail -3
ANTIPHON_NAME=ui /usr/bin/python3 -m unittest discover -s test 2>&1 | tail -3
git status --short   # only intended files
git rev-parse HEAD
```

Run the Python suite in the foreground: a backgrounded run has no CLI root above it, `owner_key()` returns None, and thirty-one tests fail for that reason alone (measured 2026-09-02 on `f0c529f`).

- [ ] **Step 3: Commit and hand the SHA to Codex for review**

```sh
git add README.md BACKLOG.md
git commit -m "The fingerprint move, in words: what old readers do, what stays prunable"
git rev-parse HEAD
```

Stop here. Merge, push and publish wait for Codex's review on that exact SHA and the user's separate approval.

---

## Self-review

- **Coverage of Codex's asks:** red cross-version mutation (Task 2, Task 5 with the real reader); current reader still rejects a true mismatch (Task 2 `test_the_current_reader_still_prunes_a_recycled_pid`); Node/Python schema parity (Task 4 parity cases + contract test); session↔endpoint owner join (Task 2 `..._still_enumerates_a_current_owner_key`, and the join code is untouched); registration response authority intact (Task 4 Step 5, `git diff` on `channel.mjs` empty); `birth_version: 1` records as migration input, never death by absent sibling (Task 2 migration test, Task 3 selector, Task 4 parity cases).
- **Open for critique:** (1) vendoring the whole 0.3.1 `peers.py` as a fixture rather than only its liveness functions — chosen so the test runs the shipped reader on every road, at the cost of 32 KB in the test tree; (2) Task 6's second note infers "runs old code" from an unversioned owner key on a live record, which is sound only while nothing current writes unversioned keys — true today; (3) whether `_record_liveness` should keep treating the migration spelling as `live`-capable evidence or demote it to `unknown` — this plan keeps it strict, since 0.4.0 wrote it canonically.
- **Type consistency:** `_fingerprint_of` returns `(int, str) | None` everywhere; `process_fingerprint` returns the rendered string; Node `fingerprintOf` returns `{version, start} | null` and `renderFingerprint` produces the string compared against `claimedBirth`.
