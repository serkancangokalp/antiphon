# Process Identity and Claude Channel Recovery Implementation Plan

> Implement on `codex/p0-claude-identity` from design commit `99fcb04`.
> Keep all fixtures under temporary projects and socket directories. Do not
> touch the repository's live `.antiphon/cursor.json` or peer registry.

**Goal:** Make process identity independent of reader timezone/locale and let a
current Claude listener safely restore its own missing endpoint record.

**Architecture:** `lib/peers.py` owns one canonical, versioned process identity
format used by endpoint liveness and Codex owner joins. `lib/channel.mjs` owns a
content-free control request through which the process actually serving a named
socket re-registers itself. `lib/antiphon.py` may request that recovery once for
an explicit alias, then must re-resolve through the registry before delivering
the user's message.

**Tech stack:** Python 3 standard library and `unittest`; Node.js ESM, Unix
sockets, MCP SDK, and `node:assert`; shell E2E harness.

---

## Task 1: Canonical and versioned process birth

**Files:**

- Modify: `test/test_peers.py` (`OwnerKeyTest`, `RecycledPidTest`)
- Modify: `lib/peers.py` (process constants, `_process_info`, `_birth_of`,
  `_record_alive`, `register`, `owner_key`)

### 1.1 Write failing tests

Add cases that prove:

- `_process_info` passes `LC_ALL=C` and `TZ=UTC` while preserving other env;
- canonical C `lstart` is parsed by fields rather than an inherited 24-byte
  slice;
- a newly registered endpoint records a process-fingerprint version;
- a legacy record with a different rendered `birth` stays live while its pid is
  live;
- a versioned record with a different canonical birth is pruned;
- a new owner key carries the version marker.

Run:

```sh
python3 -m unittest \
  test.test_peers.OwnerKeyTest \
  test.test_peers.RecycledPidTest
```

Confirm the new assertions fail for the expected missing environment, schema,
and migration behavior.

### 1.2 Implement the smallest process-identity change

- Introduce a single fingerprint-version constant and helpers for legacy versus
  current records/owner keys.
- Run `ps` with a copied environment plus `LC_ALL=C`, `TZ=UTC`.
- Parse canonical C `lstart` structurally; return `None` on malformed output.
- Write the version marker with every readable new endpoint birth.
- Compare `birth` strictly only when the record marks the current version.
- Return a versioned owner key from `owner_key`.

### 1.3 Verify and commit

Run the focused tests above and then:

```sh
python3 -m unittest test.test_peers
git diff --check
```

Commit:

```sh
git add lib/peers.py test/test_peers.py
git commit -m "fix: canonicalize process identity"
```

## Task 2: Fail-closed owner migration and diagnostic contract

**Files:**

- Modify: `test/test_peers.py` (session/endpoint join cases)
- Modify: `test/test_antiphon.py` (doctor cases)
- Modify: `lib/peers.py` (owner classification and join)
- Modify: `lib/antiphon.py` (`_doctor_peers` text/state only)

### 2.1 Write failing tests

Add cases that prove:

- two current owner keys still join endpoint and session;
- two equal legacy owner keys retain rolling compatibility;
- a legacy/current pair with one pid does not join by pid alone;
- the mixed pair remains visible as live and unroutable;
- doctor describes the mixed owner format without pruning or rewriting either
  file;
- doctor and the registry observe process birth through the same helper/canon.

Run the exact new test names and observe the join/diagnostic failures.

### 2.2 Implement owner classification

- Keep validation compatible with already-written owner strings.
- Add a pure classifier for owner-key schema and equality.
- Keep `_session_address` exact/fail-closed across formats.
- Surface a read-only diagnostic for records that are individually valid but
  cannot join during rolling upgrade.
- Do not introduce a PID-only mixed-format join.

### 2.3 Verify and commit

Run:

```sh
python3 -m unittest test.test_peers
python3 -m unittest test.test_antiphon.DoctorTest
git diff --check
```

Use the actual doctor test class name discovered in the file if it differs.

Commit:

```sh
git add lib/peers.py lib/antiphon.py test/test_peers.py test/test_antiphon.py
git commit -m "fix: migrate owner identities without guessing"
```

## Task 3: Listener-owned reassert control protocol

**Files:**

- Modify: `test/channel.test.mjs`
- Modify: `lib/channel.mjs`

### 3.1 Write failing real-process tests

With real `channel.mjs` processes and throwaway paths, prove:

- deleting a live listener's endpoint and sending a versioned reassert request
  recreates it with the listener's pid and address;
- the control request never becomes an MCP channel notification;
- the response repeats the nonce and protocol version;
- a wrong alias or malformed request changes no registry file;
- a second same-name process cannot overwrite or unlink the listener;
- all startup routes still settle `identitySettled`.

Observe the current server reject the control request and leave the registry
empty.

### 3.2 Implement the control frame

- Add constants for the internal protocol marker and version.
- Detect and validate the control shape before normal content validation.
- Have the listener call `claimPeer()` with its own existing registry payload.
- Return a bounded control reply; never call `mcp.notification` for it.
- Refactor startup so an answering socket gets one verified reassert attempt
  before a new process claims or unlinks anything.
- Preserve the existing bind-race cleanup and every identity-settlement path.

### 3.3 Verify and commit

Run:

```sh
node test/channel.test.mjs
git diff --check
```

Commit:

```sh
git add lib/channel.mjs test/channel.test.mjs
git commit -m "fix: let a Claude listener reassert its endpoint"
```

## Task 4: Recover one explicit named send

**Files:**

- Modify: `test/test_antiphon.py` (channel transport and routing tests)
- Modify: `lib/antiphon.py` (control client and `send_to_claude`)

### 4.1 Write failing transport tests

Prove:

- only a valid explicit Claude alias is eligible for recovery;
- the recovery request uses the deterministic named socket and contains no
  original message content;
- generic `{ok:true}` or a mismatched nonce is not accepted;
- a valid control response without a fresh matching registry record is not
  accepted;
- a valid listener reassertion is re-resolved and the original message is sent
  once;
- failed recovery retains the `no-peer` refusal and existing content-free
  attempt notice;
- a bare send never scans or probes named socket guesses.

Observe the tests fail because no recovery client exists.

### 4.2 Implement bounded recovery

- Add a private helper that sends one versioned reassert frame, reads a bounded
  response, checks marker/version/nonce, and returns only a boolean.
- On a valid explicit-alias miss in `send_to_claude`, invoke it at most once,
  then call `_resolve_target` again.
- Deliver the existing serialized user payload only after registry resolution.
- Preserve the no-retry rule after user bytes may have been sent.
- Keep the refused-attempt notice best-effort and content-free.

### 4.3 Verify and commit

Run focused tests, then:

```sh
python3 -m unittest test.test_antiphon
git diff --check
```

Commit:

```sh
git add lib/antiphon.py test/test_antiphon.py
git commit -m "fix: recover an unregistered named Claude channel"
```

## Task 5: Align public contracts and BACKLOG

**Files:**

- Modify: `README.md`
- Modify: `BACKLOG.md`
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`
- Modify tests that pin the shared channel wording

### 5.1 Write or update contract assertions first

Pin the same facts on all public surfaces:

- configured identity is independent of channel ownership;
- a current listener can restore its own missing endpoint;
- recovery is content-free and bounded;
- an old or unverified listener remains fail-closed and may require restart;
- doctor reports but does not repair.

Run the named contract tests and observe any stale wording fail.

### 5.2 Update docs and backlog evidence

- Record the measured TZ/locale root cause and the durable socket chain.
- Mark only the work actually implemented as fixed.
- Preserve the separate pending-delivery and follow-on backlog boundaries.
- Do not claim the external reporter's environment was measured; identify the
  product reproduction as deterministic and the reporter attribution as
  unproven.

### 5.3 Verify and commit

Run contract tests and:

```sh
git diff --check
```

Commit:

```sh
git add README.md BACKLOG.md CLAUDE.md AGENTS.md test
git commit -m "docs: explain channel identity recovery"
```

## Task 6: Whole-tree verification on the exact commit

### 6.1 Run complete local tests

```sh
npm test
git status --short
git diff --check HEAD
```

Fix regressions with red-before-green tests and separate truthful commits.

### 6.2 Create the candidate commit if verification fixes were needed

Record:

```sh
git rev-parse HEAD
git status --porcelain --untracked-files=all
```

The tree must be clean.

### 6.3 Run the release E2E against that SHA

```sh
test/e2e/fresh-user.sh
```

Require its summary to name the same `git rev-parse HEAD` and pass every
assertion.

### 6.4 Independent review

Give the exact candidate SHA and approved design to the separate reviewer
agent. The reviewer is read-only and must report findings by severity with
file/line evidence. Address findings through TDD and repeat the whole-tree and
exact-commit gates on the new SHA.

### 6.5 Stop before external publication

Report commits, test counts, E2E SHA, review result, and remaining backlog.
Do not push, run `npm version`, or publish until the user separately authorizes
that external action.
