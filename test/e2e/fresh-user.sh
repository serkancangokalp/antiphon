#!/usr/bin/env bash
# What a person who has never run Antiphon gets, measured with the real CLIs.
#
# `npm test` drives the servers with an SDK client and fixtures. That is why it
# was green on 2026-08-31 while three separate faults were live: the channel
# handed Claude Code a null `sender_alias` the host rejected, a push went to
# the newest rollout file rather than a running Codex thread, and an upgrade
# replayed every transcript from byte zero. Each needed a real host, a real
# CLI, or real transcripts to see. This script installs the packed tarball into
# a throwaway prefix, sets up a throwaway project, and drives both CLIs.
#
# It is NOT part of `npm test`: it needs a logged-in `claude` and `codex`, the
# network, and it spends 2 to 6 small model calls per run. Run it before a release,
# after the wrapper census and before `npm version`.
#
# What it does not cover, and how to check those by hand:
#   * That Claude Code accepts the channel notification. `-p` mode never loads
#     the channel. Reconnect an interactive session (`/mcp` → antiphon), have
#     Codex send one message, and read
#     ~/Library/Caches/claude-cli-nodejs/<project-slug>/mcp-logs-antiphon/ —
#     a `ProtocolError` line there is the fault this misses.
#   * The window where a Codex thread is open but has written no rollout yet.
#     `codex exec` writes one immediately; only an interactive terminal waits.
#   * `codex exec` fires SessionStart but never UserPromptSubmit, so the page
#     is produced by running `antiphon hook codex` with the payload the host
#     sends, after asserting that is exactly what `.codex/hooks.json` declares.
#     The declared command is compared, never executed: running whatever the
#     code under test wrote would hand it a shell. The wiring itself is what
#     `antiphon doctor` checks.
#   * T2 proves a push against a real Claude transcript, not that Claude's own
#     Stop hook invoked it — `.antiphon/` appears from the UserPromptSubmit
#     hook too, so the automatic Stop wiring could regress with all of this
#     green. Only doctor's reading of `.claude/settings.json` covers that.
#   * The Codex rollout this run creates is left in place and named, never
#     deleted: choosing a file to delete from a person's session store on the
#     word of the code under test is a risk no test script may take.
#   * A successful live model call can omit the exact marker it was asked for.
#     Only that marker-producing turn is retried up to the shared attempt limit,
#     after exit zero. Push, queue, page delivery and the rest of T2/T3 stay
#     single-shot. Final marker exhaustion preserves both evidence roots.
#
# Usage:  test/e2e/fresh-user.sh [--version <npm-version>] [--keep]
#   default:  packs an immutable copy of HEAD (refuses a dirty tree) and prints
#             the commit its result describes.
#   --version: installs that published release instead; no commit involved.
#   --keep:   leaves the temp tree and the transcripts in place.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
source "$REPO/test/e2e/marker_contract.sh" || exit 2
VERSION=""
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PASSED=0; FAILED=0
pass() { PASSED=$((PASSED + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { FAILED=$((FAILED + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
step() { printf '\n== %s\n' "$1"; }
check() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (beklenen: $3, ölçülen: $2)"; fi; }
contains() { case "$2" in *"$3"*) pass "$1" ;; *) fail "$1 — bulunamadı: $3" ;; esac; }
# A worker that did not complete is explained before it is cancelled: its
# state and the tail of its log, so a reader can tell a transient Codex
# failure from a regression. Measured once without this: three FAILs and no
# way to say which.
explain_worker() {  # $1 = the result JSON, $2 = the worker's log path
  printf '  evidence: %s\n' "$(printf '%s' "$1" | python3 -c 'import sys, json
try:
    r = json.load(sys.stdin); print("state=%s exit=%s stopped=%s" % (r.get("state"), r.get("exit_code"), r.get("stopped")))
except Exception as e:
    print("unparseable result: %r" % (sys.stdin.read()[:200],))' 2>&1)"
  if [ -s "$2" ]; then printf '  evidence: log tail —\n'; tail -n 12 "$2" | sed 's/^/    | /'; else printf '  evidence: no log at %s\n' "$2"; fi
}
# An absence proves nothing about a text that is not there: an empty page
# lacks every word, and a check reading "the page does not replay" would pass
# for a page that was never produced.
lacks() {
  if [ -z "$2" ]; then fail "$1 — kanıt yok: ölçülen metin boş"; return; fi
  case "$2" in *"$3"*) fail "$1 — olmamalıydı: $3" ;; *) pass "$1" ;; esac
}

# One helper, two legacy shapes. A v2 map is a scan high-water mark, so it
# replays from byte zero by design; a 0.1.0 float is a delivered boundary and
# must not. Comparing the two pages on the same transcripts measures the rule
# without guessing which timestamps happen to render.
seed() { python3 -c "import json,sys; json.dump(json.loads(sys.argv[1]), open('$PROJECT/.antiphon/cursor.json','w'))" "$1"; }
# A hook that crashes prints nothing, and a check reading "nothing is left to
# deliver" would call that success. The status of every stage is kept, and a
# failure answers with a word no assertion accepts.
page_now() {
  local raw decoded
  raw="$(printf '{"cwd":"%s","hook_event_name":"UserPromptSubmit","transcript_path":"%s","session_id":"%s"}' \
    "$PROJECT" "$ROLLOUT" "$(basename "$ROLLOUT" .jsonl | grep -o '[0-9a-f-]\{36\}')" \
    | (cd "$PROJECT" && antiphon hook codex))" || { echo "HOOK-FAILED"; return 1; }
  [ -z "$raw" ] && return 0                      # nothing to deliver is empty, legitimately
  decoded="$(printf '%s' "$raw" | python3 -c "
import sys, json
try:
    print(json.loads(sys.stdin.read())['hookSpecificOutput']['additionalContext'])
except Exception as error:
    print('HOOK-UNDECODABLE: %s' % error); raise SystemExit(1)")" || { echo "$decoded"; return 1; }
  printf '%s' "$decoded"
}
events_in() { printf '%s' "$1" | grep -cE '^\[[0-9][0-9]:[0-9][0-9]\]'; }

# This is the only retry boundary in the E2E. The helper prints a path and an
# attempt number, never transcript content. A final exact-marker omission keeps
# the evidence automatically; a CLI or probe failure remains a distinct error.
land_exact_marker() {
  local marker="$1" label="$2" result code attempt transcript
  result="$(bash "$REPO/test/e2e/marker_turn.sh" \
    "$PROJECT" "$CLAUDE_DIR" "$marker")"
  code=$?
  case "$code" in
    0)
      attempt="$(printf '%s\n' "$result" | sed -n 's/^attempt=//p')"
      transcript="$(printf '%s\n' "$result" | sed -n 's/^transcript=//p')"
      case "$attempt" in
        ''|*[!0-9]*) fail "$label returned an invalid attempt"; exit 1 ;;
      esac
      if [ "$attempt" -lt 1 ] || [ "$attempt" -gt "$MAX_MARKER_ATTEMPTS" ]; then
        fail "$label returned an out-of-range attempt"
        exit 1
      fi
      case "$transcript" in
        "$CLAUDE_DIR"/*.jsonl) ;;
        *) fail "$label returned a transcript outside this run"; exit 1 ;;
      esac
      [ -f "$transcript" ] && [ ! -L "$transcript" ] \
        || { fail "$label returned no regular transcript"; exit 1; }
      MARKER_ATTEMPT="$attempt"
      MARKER_TRANSCRIPT="$transcript"
      ;;
    1)
      preserve_marker_evidence "$label" "$TMP" "$CLAUDE_DIR"
      exit 1
      ;;
    *)
      fail "$label failed before an exact assistant marker landed"
      exit 1
      ;;
  esac
}

for tool in claude codex node npm python3 sqlite3; do
  command -v "$tool" >/dev/null || { echo "gerekli araç yok: $tool" >&2; exit 2; }
done

# `pwd -P`, because on macOS $TMPDIR is a symlink into /private and both hosts
# record the resolved path: an unresolved one names a transcript directory that
# never fills and a project Codex discovery cannot match.
TMP="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/antiphon-e2e.XXXXXX")" && pwd -P)"
# Unique per run: a constant marker meets an earlier run's leftovers in a
# store neither run owns, and an assertion about "this run's rows" would be
# answering about someone else's.
NONCE="e2e-$(basename "$TMP" | tr -dc 'A-Za-z0-9')"
PREFIX="$TMP/prefix"; PROJECT="$TMP/project"
SLUG="$(printf '%s' "$PROJECT" | sed 's|[^A-Za-z0-9]|-|g')"
CLAUDE_DIR="$HOME/.claude/projects/$SLUG"
QUEUE="$HOME/.codex/queue_1.sqlite"
CODEX_CONFIG_BEFORE="$(shasum -a 256 "$HOME/.codex/config.toml" 2>/dev/null | cut -d' ' -f1)"
# A byte copy beside the digest: the write worker's `codex exec` records this
# run's project as trusted in that file (Codex's own doing), and the run undoes
# exactly that against this copy.
CODEX_CONFIG_COPY="$TMP/codex-config-before.toml"; cp "$HOME/.codex/config.toml" "$CODEX_CONFIG_COPY" 2>/dev/null || :
# Read-only, and absent is its own answer: a plain `sqlite3` call creates the
# file it was asked to read, so a first-ever run would leave a database behind
# and call the machine unchanged.
queued() {
  [ -f "$QUEUE" ] || { echo absent; return; }
  sqlite3 -readonly "$QUEUE" 'SELECT count(*) FROM queued_items;' 2>/dev/null || echo unreadable
}
cursor_digest() {
  local path="$1/.antiphon/cursor.json"
  [ -f "$path" ] || { echo absent; return; }
  shasum -a 256 "$path" | cut -d' ' -f1
}

# Undo the one trust entry Codex writes for this run's project, byte-exact
# against the copy taken at the start; anything else in that file is the
# person's and is never touched. Called from the write step and again from the
# EXIT trap before the copy goes with $TMP, so an interrupted run leaves no
# entry behind (review 2026-09-03). Prints unchanged | undone | other | unreadable.
undo_trust() {
  if [ -z "${PROJECT:-}" ] || [ ! -f "${CODEX_CONFIG_COPY:-}" ]; then echo "unchanged"; return 0; fi
  python3 - "$CODEX_CONFIG_COPY" "$HOME/.codex/config.toml" "$PROJECT" <<'PY'
import os, sys
copy, live, project = sys.argv[1:4]
try:
    with open(copy, encoding="utf-8") as f: before = f.read()
    with open(live, encoding="utf-8") as f: after = f.read()
except OSError:
    print("unreadable"); raise SystemExit
if after == before:
    print("unchanged"); raise SystemExit
table = '[projects."%s"]\ntrust_level = "trusted"\n' % os.path.realpath(project)
for candidate in (table + "\n", "\n" + table, table):
    if after.count(candidate) == 1 and after.replace(candidate, "", 1) == before:
        with open(live, "w", encoding="utf-8") as f: f.write(before)
        print("undone"); raise SystemExit
print("other")
PY
}

cleanup() {
  # First, whatever else happens: the copy lives under $TMP.
  case "$(undo_trust 2>/dev/null)" in
    undone) echo "undone at exit: the trust entry Codex wrote for $PROJECT" ;;
  esac
  if [ "$KEEP" = "1" ]; then
    echo; echo "korunuyor: $TMP"; echo "korunuyor: $CLAUDE_DIR"
    return
  fi
  rm -rf "$TMP"
  # This run's own transcript directory and nothing else: the path is derived
  # from its own temp project, so the name carries the proof. Checked again
  # here rather than trusted, because the variable is built by substitution and
  # an empty PROJECT would name the whole store.
  case "$CLAUDE_DIR" in
    "$HOME/.claude/projects/"*antiphon-e2e*-project)
      [ -d "$CLAUDE_DIR" ] && [ ! -L "$CLAUDE_DIR" ] && rm -rf "$CLAUDE_DIR" ;;
  esac
  # Nothing under ~/.codex is ever deleted. `$ROLLOUT` comes from discovery
  # over the person's whole session store, and one wrong match would destroy a
  # real Codex session's history — a price no test script may risk. It is
  # named instead, for whoever wants to prune it.
  [ -n "${ROLLOUT:-}" ] && echo "left in place: $ROLLOUT"
}
trap cleanup EXIT

echo "e2e: $TMP"

step "T0 — the published shape installs and names its version"
if [ -n "$VERSION" ]; then
  TARBALL="antiphon@$VERSION"
  echo "  ---- testing the published $VERSION; no commit of this tree is involved"
else
  # A commit, not a working tree. Packing $REPO directly proves whatever bytes
  # happened to be on disk — uncommitted edits, untracked files npm would
  # include — and the run is long enough for the tree to move under it. So the
  # gate names a SHA and packs an immutable copy of exactly that: `git archive`
  # writes the commit's tree, and nothing that happens in the repo afterwards
  # can reach it. A dirty tree is refused, because a gate that certifies bytes
  # nobody can name is not a gate.
  TESTED_HEAD="$(git -C "$REPO" rev-parse HEAD)"
  DIRT="$(git -C "$REPO" status --porcelain --untracked-files=all)"
  if [ -n "$DIRT" ]; then
    echo "the working tree is not clean, so no commit describes what would be tested:" >&2
    printf '%s\n' "$DIRT" >&2
    echo "commit or stash first, or use --version <release> to test a published one." >&2
    exit 2
  fi
  echo "  ---- testing commit $TESTED_HEAD (packed from git archive, not the working tree)"
  mkdir -p "$TMP/src"
  git -C "$REPO" archive --format=tar "$TESTED_HEAD" | tar -x -C "$TMP/src"
  TARBALL="$(cd "$TMP" && npm pack "$TMP/src" --silent 2>/dev/null | tail -1)"
  TARBALL="$TMP/$TARBALL"
fi
npm install -g --prefix "$PREFIX" "$TARBALL" --silent >/dev/null 2>&1 \
  && pass "the tarball installs" || fail "npm install of the tarball failed"
export PATH="$PREFIX/bin:$PATH"
case "$(command -v antiphon)" in
  "$PREFIX/bin/antiphon") pass "PATH resolves to the copy under test" ;;
  *) fail "PATH resolves to $(command -v antiphon), not $PREFIX/bin/antiphon" ;;
esac
EXPECTED_VERSION="$(python3 -c "import json,sys; print(json.load(open('$REPO/package.json'))['version'])")"
[ -n "$VERSION" ] && EXPECTED_VERSION="$VERSION"
check "antiphon --version" "$(antiphon --version 2>&1)" "antiphon $EXPECTED_VERSION"
HANDSHAKE="$(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}\n' | ANTIPHON_CWD="$TMP" antiphon mcp 2>/dev/null | python3 -c "import sys,json; print(json.loads(sys.stdin.readline())['result']['serverInfo']['version'])")"
check "mcp handshake version" "$HANDSHAKE" "$EXPECTED_VERSION"

step "T1 — setup, then a clean bill of health"
mkdir -p "$PROJECT"; (cd "$PROJECT" && git init -q)
export ANTIPHON_CWD="$PROJECT"
(cd "$PROJECT" && antiphon setup >/dev/null 2>&1) \
  && pass "setup exits 0" || fail "setup exited non-zero"
for f in .claude/settings.json .claude/settings.local.json .codex/hooks.json .codex/config.toml .mcp.json CLAUDE.md AGENTS.md .antiphon/.gitignore; do
  [ -f "$PROJECT/$f" ] && pass "setup wrote $f" || fail "setup did not write $f"
done
check "~/.codex/config.toml untouched" "$(shasum -a 256 "$HOME/.codex/config.toml" 2>/dev/null | cut -d' ' -f1)" "$CODEX_CONFIG_BEFORE"
DOCTOR="$(cd "$PROJECT" && antiphon doctor 2>&1)"; DOCTOR_CODE=$?
check "doctor exit on a fresh project" "$DOCTOR_CODE" "0"
lacks "doctor finds nothing broken" "$DOCTOR" "✗"

# Break one project-local entry with a fixed Python program. No command read
# from the configuration is executed, and no runtime state is involved.
python3 - "$PROJECT/.mcp.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as source:
    data = json.load(source)
servers = data.get("mcpServers")
if not isinstance(servers, dict) or "antiphon" not in servers:
    raise SystemExit(1)
del servers["antiphon"]
with open(path, "w", encoding="utf-8") as target:
    json.dump(data, target, indent=2)
    target.write("\n")
PY
[ "$?" -eq 0 ] && pass "the fixture removed only the project MCP entry" \
                 || fail "the fixture could not remove the project MCP entry"
BROKEN_DOCTOR="$(cd "$PROJECT" && antiphon doctor 2>&1)"; BROKEN_DOCTOR_CODE=$?
check "ordinary doctor rejects the broken project" "$BROKEN_DOCTOR_CODE" "1"
contains "ordinary doctor names the missing MCP server" "$BROKEN_DOCTOR" ".mcp.json: missing"
REPAIR="$(cd "$PROJECT" && antiphon doctor --fix 2>&1)"; REPAIR_CODE=$?
check "doctor --fix repairs project configuration" "$REPAIR_CODE" "0"
contains "the repair says what setup changed" "$REPAIR" "Claude MCP Channel registered"
contains "the repair marks its read-only re-check" "$REPAIR" "doctor re-check (read-only)"
AFTER_REPAIR="$(cd "$PROJECT" && antiphon doctor 2>&1)"; AFTER_REPAIR_CODE=$?
check "ordinary doctor is clean after repair" "$AFTER_REPAIR_CODE" "0"
lacks "the repaired project has no broken finding" "$AFTER_REPAIR" "✗"
check "doctor --fix left global Codex config untouched" \
  "$(shasum -a 256 "$HOME/.codex/config.toml" 2>/dev/null | cut -d' ' -f1)" \
  "$CODEX_CONFIG_BEFORE"

step "T1C — a durable catalog proves the whole project or names its boundary"
CATALOG_HOME="$TMP/catalog-home"; CATALOG_PROJECT="$TMP/catalog-project"
mkdir -p "$CATALOG_HOME" "$CATALOG_PROJECT"
(cd "$CATALOG_PROJECT" && git init -q)
CATALOG_SLUG="$(printf '%s' "$CATALOG_PROJECT" | sed 's|[^A-Za-z0-9]|-|g')"
CATALOG_CLAUDE="$CATALOG_HOME/.claude/projects/$CATALOG_SLUG"
mkdir -p "$CATALOG_CLAUDE"
python3 - "$CATALOG_CLAUDE" "$NONCE" <<'PY'
import datetime, json, os, sys
root, nonce = sys.argv[1:]
now = datetime.datetime.now(datetime.timezone.utc)
for number in range(4):
    sid = f"{number:08x}-4444-4444-8444-{number:012x}"
    text = f"{nonce}-catalog-{'oldest' if number == 0 else number}"
    record = {
        "type": "assistant",
        "timestamp": (now + datetime.timedelta(seconds=number)).isoformat(),
        "message": {"content": [{"type": "text", "text": text}]},
    }
    path = os.path.join(root, sid + ".jsonl")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    os.utime(path, (100 + number, 100 + number))
PY
HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= \
  antiphon setup >/dev/null 2>&1 \
  && pass "catalog fixture setup exits 0" || fail "catalog fixture setup failed"
BUILDING="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon summary codex 2>&1)"
contains "an incomplete bootstrap says building" "$BUILDING" "discovery: building"
contains "the incomplete page still names its scope" "$BUILDING" "has_more_scope: catalogued project sources"
lacks "the newest-three fallback cannot claim the fourth source" "$BUILDING" "$NONCE-catalog-oldest"
CURSOR_BEFORE="$(cursor_digest "$CATALOG_PROJECT")"
CATALOG_SCAN="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon sources scan 2>&1)"; CATALOG_SCAN_CODE=$?
check "sources scan completes" "$CATALOG_SCAN_CODE" "0"
contains "sources scan reports completion" "$CATALOG_SCAN" "source catalog: complete"
check "sources scan does not move a cursor" "$(cursor_digest "$CATALOG_PROJECT")" "$CURSOR_BEFORE"
COMPLETE="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon summary codex 2>&1)"
contains "complete discovery sees the fourth older source" "$COMPLETE" "$NONCE-catalog-oldest"
lacks "a complete page has no building marker" "$COMPLETE" "discovery: building"
lacks "a complete page has no degraded marker" "$COMPLETE" "discovery: degraded"
CATALOG_STATE="$CATALOG_PROJECT/.antiphon/sources/state.json"
cp "$CATALOG_STATE" "$TMP/catalog-state.saved"
printf '{malformed\n' > "$CATALOG_STATE"
DEGRADED="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon summary codex 2>&1)"
contains "an untrusted catalog says degraded" "$DEGRADED" "discovery: degraded"
lacks "an untrusted catalog uses only the bounded recent fallback" "$DEGRADED" "$NONCE-catalog-oldest"
cp "$TMP/catalog-state.saved" "$CATALOG_STATE"
OLDEST_REL="$CATALOG_SLUG/00000000-4444-4444-8444-000000000000.jsonl"
OLDEST_RECORD="$(python3 - "$CATALOG_PROJECT" "$OLDEST_REL" <<'PY'
import hashlib, os, sys
project, relative = sys.argv[1:]
digest = hashlib.sha256(("claude\0" + relative).encode()).hexdigest()
print(os.path.join(project, ".antiphon", "sources", "records", "claude",
                   digest[:2], digest + ".json"))
PY
)"
rm "$OLDEST_RECORD"
MISSING_RECORD="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon summary codex 2>&1)"
contains "a missing index record cannot hide its manifest source" "$MISSING_RECORD" "$NONCE-catalog-oldest"
contains "a missing index record degrades completeness" "$MISSING_RECORD" "discovery: degraded"
CATALOG_REPAIR="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon sources scan 2>&1)"; CATALOG_REPAIR_CODE=$?
check "sources scan repairs missing record coverage" "$CATALOG_REPAIR_CODE" "0"
contains "repaired catalog is complete" "$CATALOG_REPAIR" "source catalog: complete"
CATALOG_STATUS="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon status 2>&1)"
contains "status reports the catalog complete" "$CATALOG_STATUS" "source catalog codex_pages: complete"
check "status does not move a cursor" "$(cursor_digest "$CATALOG_PROJECT")" "$CURSOR_BEFORE"
CATALOG_DOCTOR="$(HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon doctor 2>&1)"; CATALOG_DOCTOR_CODE=$?
check "doctor accepts the complete catalog fixture" "$CATALOG_DOCTOR_CODE" "0"
contains "doctor reports the catalog complete" "$CATALOG_DOCTOR" "source catalog codex_pages: complete"
check "doctor does not move a cursor" "$(cursor_digest "$CATALOG_PROJECT")" "$CURSOR_BEFORE"
CATALOG_PAGE="$(printf '{"cwd":"%s","hook_event_name":"UserPromptSubmit"}' "$CATALOG_PROJECT" \
  | HOME="$CATALOG_HOME" ANTIPHON_CWD="$CATALOG_PROJECT" ANTIPHON_NAME= antiphon hook codex 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])")"
contains "the passive hook delivers the fourth older source" "$CATALOG_PAGE" "$NONCE-catalog-oldest"

step "T2 — Claude writes to a Codex that does not exist yet"
BEFORE_QUEUE="$(queued)"
case "$BEFORE_QUEUE" in
  absent) echo "  ---- Codex has no queue database yet; the stranding check compares against none" ;;
  ''|*[!0-9]*) fail "Codex's queue could not be read; the stranding check would prove nothing" ;;
esac
# Two accepted marker moments give T5 two distinct cursor boundaries. Omitted
# attempts may add records, but they cannot remove either accepted boundary and
# only increase T5's lower-bound event count. One accepted turn renders both of
# its lines in the same second, and the `>=` rule repeats that whole cohort.
land_exact_marker "@codex $NONCE-one" "the first claude -p turn"
pass "the first claude -p turn exits 0"
pass "the first exact assistant marker landed on attempt $MARKER_ATTEMPT"
sleep 2
land_exact_marker "@codex $NONCE-two" "the second claude -p turn"
pass "the second claude -p turn exits 0"
pass "the second exact assistant marker landed on attempt $MARKER_ATTEMPT"
# Stronger than "newest": this is the exact transcript whose assistant block
# passed the second-marker predicate, and it is the only transcript T2 pushes.
TRANSCRIPT="$MARKER_TRANSCRIPT"
# `.antiphon/` itself is setup's now (its .gitignore); the hooks' own files say
# they ran — measured on a quiet project: the cursor lock, the peers and the
# identity directories, and no cursor.json until there is a page to record.
{ [ -e "$PROJECT/.antiphon/cursor.json.lock" ] || [ -d "$PROJECT/.antiphon/peers" ]; } \
  && pass "the hooks ran (.antiphon/cursor.json.lock or peers/ exists)" || fail "the hooks never ran"
e2e_once push || { fail "the T2 push stage attempted to run twice"; exit 1; }
PUSH="$(printf '{"cwd":"%s","hook_event_name":"Stop","transcript_path":"%s","session_id":"%s"}' \
        "$PROJECT" "$TRANSCRIPT" "$(basename "$TRANSCRIPT" .jsonl)" | (cd "$PROJECT" && antiphon push codex 2>&1))"
contains "the push refuses instead of guessing" "$PUSH" "no Codex session found"
AFTER_QUEUE="$(queued)"
if [ "$AFTER_QUEUE" = "absent" ]; then
  pass "this run stranded nothing of its own (Codex has no queue)"
else
  case "$AFTER_QUEUE" in ''|*[!0-9]*) fail "Codex's queue could not be read after the push" ;; esac
  # This run's own rows, by its own nonce: a constant marker would collide
  # with an earlier run's residue, and a machine-wide total moves under any
  # other session enqueueing or draining while this one runs.
  STRANDED="$(sqlite3 -readonly "$QUEUE" "SELECT count(*) FROM queued_items WHERE payload_json LIKE '%$NONCE%';" 2>&1)"
  check "this run stranded nothing of its own" "$STRANDED" "0"
fi

step "T3a — a managed worker: delegate one read task to a fresh codex exec, collect it by id"
# A commit, so the worker gets the worktree road — the one a real checkout
# takes — rather than the run-in-place road of a bare `git init`.
(cd "$PROJECT" && git -c user.email=e2e@antiphon -c user.name=e2e commit -q --allow-empty -m root) \
  && pass "the project has a commit for a worker's worktree" || fail "the empty root commit failed"
DELEGATED="$(cd "$PROJECT" && printf '%s' '{"text":"Reply with exactly: WORKER-OK","kind":"codex","timeout":300}' | antiphon task delegate 2>/dev/null)" \
  && pass "antiphon task delegate exits 0" || fail "antiphon task delegate failed: $DELEGATED"
contains "the answer names a fresh codex worker" "$DELEGATED" "to a fresh codex worker"
TASK_ID="$(printf '%s' "$DELEGATED" | python3 -c 'import sys, json; print(json.load(sys.stdin)["task_id"])' 2>/dev/null)"
RESULT="$(cd "$PROJECT" && antiphon task result "$TASK_ID" 240 2>&1)"
contains "the worker completed within the wait" "$RESULT" '"state": "completed"'
case "$RESULT" in *'"state": "completed"'*) : ;; *) explain_worker "$RESULT" "$PROJECT/.antiphon/workers/$TASK_ID/log"; (cd "$PROJECT" && antiphon task cancel "$TASK_ID" >/dev/null 2>&1) ;; esac
contains "the result names the worker" "$RESULT" "worker-"
contains "the read worker ran in a worktree of its own" "$RESULT" "workers/$TASK_ID/work\""
WORKER_LOG="$PROJECT/.antiphon/workers/$TASK_ID/log"
if [ -s "$WORKER_LOG" ]; then pass "the worker's log exists and is not empty"; else fail "no worker log at $WORKER_LOG"; fi
contains "the worker answered in its own log" "$(cat "$WORKER_LOG" 2>/dev/null)" "WORKER-OK"
# One real write task: the worker edits in its worktree, never the project,
# and its diff is the evidence — bounded by the same wait as the read task.
WRITE_DELEGATED="$(cd "$PROJECT" && printf '%s' '{"text":"Create a file named WORKER-WROTE.txt in the current directory containing the single line OK (nothing else), do not commit, then reply with exactly: WORKER-WROTE","kind":"codex","task":"write","timeout":300}' | antiphon task delegate 2>/dev/null)" \
  && pass "a write task is delegated" || fail "the write delegation failed: $WRITE_DELEGATED"
WRITE_ID="$(printf '%s' "$WRITE_DELEGATED" | python3 -c 'import sys, json; print(json.load(sys.stdin)["task_id"])' 2>/dev/null)"
WRITE_RESULT="$(cd "$PROJECT" && antiphon task result "$WRITE_ID" 240 2>&1)"
contains "the write worker completed within the wait" "$WRITE_RESULT" '"state": "completed"'
case "$WRITE_RESULT" in *'"state": "completed"'*) : ;; *) explain_worker "$WRITE_RESULT" "$PROJECT/.antiphon/workers/$WRITE_ID/log"; (cd "$PROJECT" && antiphon task cancel "$WRITE_ID" >/dev/null 2>&1) ;; esac
contains "its diff carries the file it made" "$WRITE_RESULT" "WORKER-WROTE.txt"
contains "and the line it wrote" "$WRITE_RESULT" "+OK"
if [ -e "$PROJECT/WORKER-WROTE.txt" ]; then fail "the write worker edited the project's own tree"; else pass "the write worker never touched the project's tree"; fi
# Codex's own side effect, measured on 0.152.1: `codex exec -s workspace-write`
# records the repository root as trusted in ~/.codex/config.toml (a read
# worker does not, and a transient `-c` trust override does not prevent it).
# This run undoes exactly its own entry, here and again from the EXIT trap.
UNDONE="$(undo_trust)"
case "$UNDONE" in
  undone) pass "the trust entry Codex wrote for this run's project was undone byte-exact" ;;
  unchanged) pass "Codex wrote no trust entry for this run's project" ;;
  *) fail "~/.codex/config.toml changed beyond this run's own trust entry ($UNDONE); remove [projects.\"$PROJECT\"] by hand" ;;
esac
LISTED="$(cd "$PROJECT" && antiphon task list 2>&1)"
contains "task list names the read task" "$LISTED" "$TASK_ID"
contains "task list names the write task" "$LISTED" "$WRITE_ID"
SWEPT="$(cd "$PROJECT" && ANTIPHON_NAME= antiphon status 2>&1)"; contains "status still runs with a task on file" "$SWEPT" "Deliveries:"
contains "status counts the workers on record" "$SWEPT" "Workers:"

step "T3 — Codex's first turn reads what the refused push could not carry"
BEFORE_ROLLOUTS="$(mktemp)"; find "$HOME/.codex/sessions" -name 'rollout-*.jsonl' 2>/dev/null | sort > "$BEFORE_ROLLOUTS"
# Bounded: twice in seven runs this `codex exec` never returned (the worker
# step before it had passed; not reproduced under a process watchdog in five
# further runs). A hang here must be a FAIL with a name, not a stalled run.
CODEX_OUT="$(cd "$PROJECT" && perl -e 'alarm 240; exec @ARGV' codex exec -s read-only --color never 'Reply with exactly: E2E-OK' 2>&1)"; CODEX_CODE=$?
if [ "$CODEX_CODE" -eq 0 ]; then pass "codex exec exits 0"; elif [ "$CODEX_CODE" -ge 128 ]; then fail "codex exec did not return within 240 s (killed by the alarm)"; else fail "codex exec failed ($CODEX_CODE)"; fi
contains "codex exec ran the SessionStart hook" "$CODEX_OUT" "hook: SessionStart"
# Two answers, deliberately: what the package's own discovery returns, and
# what this run provably created. They must agree — that is the assertion —
# and only the proven-new one is ever a deletion target. Trusting the code
# under test to choose a file to delete is how a review finds a harness that
# can destroy a real session's history.
FOUND="$(PYTHONPATH="$PREFIX/lib/node_modules/antiphon/lib" python3 -c "
import antiphon
found = antiphon.codex_rollout_files('$PROJECT')
print(found[0] if found else '')")"
NEW_ROLLOUT="$(find "$HOME/.codex/sessions" -name 'rollout-*.jsonl' 2>/dev/null | sort | comm -13 "$BEFORE_ROLLOUTS" - | head -1)"
rm -f "$BEFORE_ROLLOUTS"
if [ -n "$NEW_ROLLOUT" ] && python3 -c "
import json, sys
head = open('$NEW_ROLLOUT', encoding='utf-8', errors='replace').readline()
sys.exit(0 if json.loads(head).get('payload', {}).get('cwd') == '$PROJECT' else 1)" 2>/dev/null; then
  pass "codex wrote one new rollout, recorded for this project"
else
  fail "no new rollout recording this project"
fi
check "discovery returns the rollout this run created" "$FOUND" "$NEW_ROLLOUT"
ROLLOUT="$FOUND"
# Read, compared, and never executed. Running whatever `setup` happened to
# write would hand the code under test a shell — the assertion would record
# the surprise and the script would run it anyway.
HOOK_CMD="$(python3 -c "import json; print(json.load(open('$PROJECT/.codex/hooks.json'))['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'])")"
check "hooks.json declares the hook command" "$HOOK_CMD" "antiphon hook codex"
e2e_once page || { fail "the T3 page stage attempted to run twice"; exit 1; }
PAGE="$(page_now)" && pass "the first page was produced" || fail "the first page — hook failed"
# The relayed prompt carries the same words, so the label decides: this must
# be Claude's own line, which is what the refused push could not carry.
SAID="$(printf '%s' "$PAGE" | grep -A1 '^\[[0-9][0-9]:[0-9][0-9]\] Claude:$' | grep -- "@codex $NONCE-two" || true)"
[ -n "$SAID" ] && pass "the refused words arrive as Claude's own line" \
                || fail "the page has no Claude: line carrying the marker"
contains "and the prompt that asked for them is relayed, not claimed" "$PAGE" "To Claude:"
lacks "a new project does not replay history" "$PAGE" "replay:"

step "T5 — upgrading from the published 0.1.0 cursor"
# The moment Claude last spoke. A cursor there must replay that turn and
# nothing before it — which is the whole 0.1.0 rule.
BOUNDARY="$(python3 - "$CLAUDE_DIR" <<'PY'
import datetime, glob, json, os, sys
stamps = []
for path in glob.glob(os.path.join(sys.argv[1], "*.jsonl")):
    for line in open(path, encoding="utf-8", errors="replace"):
        try: record = json.loads(line)
        except Exception: continue
        when = record.get("timestamp")
        if when and record.get("type") == "assistant":
            stamps.append(datetime.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
print("%.3f" % (max(stamps) if stamps else 0))
PY
)"
seed '{"codex_seen": {"v": 2, "sources": {}}}'
FULL_PAGE="$(page_now)" && pass "the byte-zero page was produced" || fail "the byte-zero page was produced — hook failed"; FULL="$(events_in "$FULL_PAGE")"

seed "{\"codex_seen\": $BOUNDARY}"
STATUS="$(cd "$PROJECT" && antiphon status 2>&1 | grep '^unread codex_pages')"
contains "status names the backlog in raw bytes" "$STATUS" "raw bytes"
contains "status names the way out" "$STATUS" "antiphon catch-up"
if printf '%s' "$STATUS" | grep -qE '[0-9]+ ~?pages?\b'; then
  fail "status guesses a page count: $STATUS"
else
  pass "status never guesses a page count"
fi
UPGRADE_PAGE="$(page_now)" && pass "the upgrade page was produced" || fail "the upgrade page was produced — hook failed"; BOUNDED="$(events_in "$UPGRADE_PAGE")"
contains "the upgrade page says it is replaying" "$UPGRADE_PAGE" "replay:"
contains "and how to skip it" "$UPGRADE_PAGE" "antiphon catch-up"
# Both turns come back from byte zero, and only the second from the 0.1.0
# time: named, not counted, so a boundary that wrongly skipped everything
# could not pass as "fewer".
contains "the v2 map replays the first turn" "$FULL_PAGE" "$NONCE-one"
contains "and the second" "$FULL_PAGE" "$NONCE-two"
lacks "the 0.1.0 time leaves the first turn where it was" "$UPGRADE_PAGE" "$NONCE-one"
contains "and replays the turn it recorded" "$UPGRADE_PAGE" "$NONCE-two"
if [ "$FULL" -lt 2 ]; then
  # Never a silent skip: a summary reading "failed 0" while the central
  # assertion never ran is the wrong-reason pass this script exists to avoid.
  fail "the fixture holds $FULL visible event(s); the two legacy shapes cannot be compared"
elif [ "$BOUNDED" -ge 1 ] && [ "$BOUNDED" -lt "$FULL" ]; then
  pass "a 0.1.0 time bounds the replay ($BOUNDED of $FULL events, v2 map replays all $FULL)"
else
  fail "a 0.1.0 time did not bound the replay ($BOUNDED of $FULL events)"
fi
CATCHUP="$(cd "$PROJECT" && antiphon catch-up 2>&1)"
contains "catch-up says what it abandons" "$CATCHUP" "will not be delivered"
AFTER_PAGE="$(page_now)" && pass "the post-catch-up page was produced" || fail "the post-catch-up page was produced — hook failed"
check "nothing is left to deliver after catch-up" "$(printf '%s' "$AFTER_PAGE" | tr -d '[:space:]')" ""

step "T6 — v3 adoption anchors the boundary and catches an in-place rewrite"
ANCHOR_HOME="$TMP/anchor-home"; ANCHOR_PROJECT="$TMP/anchor-project"
mkdir -p "$ANCHOR_HOME" "$ANCHOR_PROJECT"
(cd "$ANCHOR_PROJECT" && git init -q)
ANCHOR_SLUG="$(printf '%s' "$ANCHOR_PROJECT" | sed 's|[^A-Za-z0-9]|-|g')"
ANCHOR_CLAUDE="$ANCHOR_HOME/.claude/projects/$ANCHOR_SLUG"
mkdir -p "$ANCHOR_CLAUDE"
ANCHOR_SID="aaaaaaaa-5555-4555-8555-aaaaaaaaaaaa"
ANCHOR_SOURCE="$ANCHOR_CLAUDE/$ANCHOR_SID.jsonl"
python3 - "$ANCHOR_SOURCE" "$NONCE" <<'PY'
import datetime, json, sys
path, nonce = sys.argv[1:]
now = datetime.datetime.now(datetime.timezone.utc)
records = []
for offset, text in enumerate((f"{nonce}-anchor-first", f"{nonce}-anchor-old")):
    records.append({
        "type": "assistant",
        "timestamp": (now + datetime.timedelta(seconds=offset)).isoformat(),
        "message": {"content": [{"type": "text", "text": text}]},
    })
with open(path, "w", encoding="utf-8") as stream:
    for record in records:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
PY
HOME="$ANCHOR_HOME" ANTIPHON_CWD="$ANCHOR_PROJECT" ANTIPHON_NAME= \
  antiphon sources scan >/dev/null 2>&1 \
  && pass "the anchor fixture catalog completes" || fail "the anchor fixture catalog failed"
PYTHONPATH="$PREFIX/lib/node_modules/antiphon/lib" python3 - \
  "$ANCHOR_PROJECT" "$ANCHOR_SOURCE" "$ANCHOR_SID" <<'PY'
import json, os, sys
import antiphon
project, source, sid = sys.argv[1:]
cursor = {"codex_pages": {"v": 3, "sources": {sid: {
    "gen": antiphon.source_generation(source),
    "offset": os.path.getsize(source),
}}, "future": {"integer": 1, "float": 1.0,
                 "flag": True, "nested": [None, "kept"]}}}
path = os.path.join(project, ".antiphon", "cursor.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as stream:
    json.dump(cursor, stream, sort_keys=True)
PY
ANCHOR_CURSOR="$ANCHOR_PROJECT/.antiphon/cursor.json"
V3_BEFORE="$(python3 - "$ANCHOR_CURSOR" <<'PY'
import hashlib, json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))["codex_pages"]
print(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
PY
)"
anchor_page() {
  local raw decoded
  raw="$(printf '{"cwd":"%s","hook_event_name":"UserPromptSubmit"}' "$ANCHOR_PROJECT" \
    | HOME="$ANCHOR_HOME" ANTIPHON_CWD="$ANCHOR_PROJECT" ANTIPHON_NAME= \
      antiphon hook codex 2>/dev/null)" || { echo "HOOK-FAILED"; return 1; }
  [ -z "$raw" ] && return 0
  decoded="$(printf '%s' "$raw" | python3 -c "
import sys, json
try: print(json.loads(sys.stdin.read())['hookSpecificOutput']['additionalContext'])
except Exception as error:
    print('HOOK-UNDECODABLE: %s' % error); raise SystemExit(1)")" \
    || { echo "$decoded"; return 1; }
  printf '%s' "$decoded"
}
ADOPTION_PAGE="$(anchor_page)" && pass "the v3 adoption page was produced" \
  || fail "the v3 adoption page failed"
contains "v3 adoption repeats its last record" "$ADOPTION_PAGE" "$NONCE-anchor-old"
lacks "v3 adoption does not replay the earlier record" "$ADOPTION_PAGE" "$NONCE-anchor-first"
contains "v3 adoption names the bounded repeat" "$ADOPTION_PAGE" "adopting a delivered frontier"
V3_AFTER="$(python3 - "$ANCHOR_CURSOR" <<'PY'
import hashlib, json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))["codex_pages"]
print(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
PY
)"
check "the preserved v3 sibling remains deeply value-equivalent" "$V3_AFTER" "$V3_BEFORE"
V4_STATE="$(python3 - "$ANCHOR_CURSOR" "$ANCHOR_SID" <<'PY'
import json, sys
cursor = json.load(open(sys.argv[1], encoding="utf-8"))
value = cursor.get("codex_pages_v4") or {}
position = (value.get("sources") or {}).get(sys.argv[2]) or {}
print("%s|%s|%s" % (value.get("v"), bool(position.get("anchor")),
                     len(value.get("adopting_v3") or {})))
PY
)"
check "the adopted cursor is anchored v4 with no pending v3 source" "$V4_STATE" "4|True|0"
ANCHOR_STAT_BEFORE="$(stat -f '%i:%z' "$ANCHOR_SOURCE")"
python3 - "$ANCHOR_SOURCE" "$NONCE" <<'PY'
import sys
path, nonce = sys.argv[1:]
old = (nonce + "-anchor-old").encode()
new = (nonce + "-anchor-new").encode()
assert len(old) == len(new)
with open(path, "r+b") as stream:
    raw = stream.read()
    assert raw.count(old) == 1
    stream.seek(0)
    stream.write(raw.replace(old, new))
    stream.truncate()
PY
check "the rewrite preserves inode and length" "$(stat -f '%i:%z' "$ANCHOR_SOURCE")" "$ANCHOR_STAT_BEFORE"
REWRITE_PAGE="$(anchor_page)" && pass "the rewritten-anchor page was produced" \
  || fail "the rewritten-anchor page failed"
contains "an in-place rewrite repeats the changed anchored record" "$REWRITE_PAGE" "$NONCE-anchor-new"
lacks "the rewrite does not replay the earlier record" "$REWRITE_PAGE" "$NONCE-anchor-first"
lacks "the superseded anchored bytes are not delivered" "$REWRITE_PAGE" "$NONCE-anchor-old"

step "what the run changed outside its own temp tree"
# Not "the machine is as it was": this run leaves a Codex rollout behind on
# purpose — deleting from that store on a discovery result is a risk no test
# may take — and Claude Code records every directory it runs in. What must
# not change is configuration and trust.
check "~/.codex/config.toml untouched" "$(shasum -a 256 "$HOME/.codex/config.toml" 2>/dev/null | cut -d' ' -f1)" "$CODEX_CONFIG_BEFORE"
# Claude Code records every directory it runs in, so this file legitimately
# changes. What must not change is trust: the script never grants it, and a
# `-p` run in an untrusted workspace still runs hooks (measured 2026-08-31 —
# only `permissions.allow` is ignored there).
TRUSTED="$(python3 -c "
import json, os
try: data = json.load(open(os.path.expanduser('~/.claude.json')))
except Exception: print('unreadable'); raise SystemExit
entry = (data.get('projects') or {}).get('$PROJECT') or {}
print('granted' if entry.get('hasTrustDialogAccepted') else 'not granted')")"
check "the script never granted workspace trust" "$TRUSTED" "not granted"

# The evidence is only worth the commit it names: if HEAD moved or the tree
# went dirty while the run was in flight, this result describes a state that
# no longer exists.
if [ -n "${TESTED_HEAD:-}" ]; then
  step "the result names a commit"
  check "HEAD is where the run started" "$(git -C "$REPO" rev-parse HEAD)" "$TESTED_HEAD"
  check "and the tree is still clean" "$(git -C "$REPO" status --porcelain --untracked-files=all | wc -l | tr -d ' ')" "0"
fi

printf '\n%s\n' "----------------------------------------"
printf 'passed %d, failed %d\n' "$PASSED" "$FAILED"
[ -n "${TESTED_HEAD:-}" ] && printf 'tested commit %s\n' "$TESTED_HEAD"
[ "$FAILED" -eq 0 ] || exit 1
