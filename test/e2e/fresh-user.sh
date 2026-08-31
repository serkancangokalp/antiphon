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
# network, and it spends two small model calls per run. Run it before a release,
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
#     is produced by running the command `.codex/hooks.json` declares, with the
#     payload the host sends. The wiring itself is what `antiphon doctor` checks.
#
# Usage:  test/e2e/fresh-user.sh [--version <npm-version>] [--keep]
#   default: packs this working tree.  --version: installs that release instead.
#   --keep:   leaves the temp tree and the transcripts in place.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
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
lacks() { case "$2" in *"$3"*) fail "$1 — olmamalıydı: $3" ;; *) pass "$1" ;; esac; }

for tool in claude codex node npm python3 sqlite3; do
  command -v "$tool" >/dev/null || { echo "gerekli araç yok: $tool" >&2; exit 2; }
done

# `pwd -P`, because on macOS $TMPDIR is a symlink into /private and both hosts
# record the resolved path: an unresolved one names a transcript directory that
# never fills and a project Codex discovery cannot match.
TMP="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/antiphon-e2e.XXXXXX")" && pwd -P)"
PREFIX="$TMP/prefix"; PROJECT="$TMP/project"
SLUG="$(printf '%s' "$PROJECT" | sed 's|[^A-Za-z0-9]|-|g')"
CLAUDE_DIR="$HOME/.claude/projects/$SLUG"
QUEUE="$HOME/.codex/queue_1.sqlite"
CODEX_CONFIG_BEFORE="$(shasum -a 256 "$HOME/.codex/config.toml" 2>/dev/null | cut -d' ' -f1)"
queued() { sqlite3 "$QUEUE" 'SELECT count(*) FROM queued_items;' 2>/dev/null || echo 0; }

cleanup() {
  if [ "$KEEP" = "1" ]; then
    echo; echo "korunuyor: $TMP"; echo "korunuyor: $CLAUDE_DIR"
    return
  fi
  rm -rf "$TMP"
  # Only this run's own transcript directory, named after its own temp project.
  case "$CLAUDE_DIR" in *antiphon-e2e*) rm -rf "$CLAUDE_DIR" ;; esac
  [ -n "${ROLLOUT:-}" ] && rm -f "$ROLLOUT"
}
trap cleanup EXIT

echo "e2e: $TMP"

step "T0 — the published shape installs and names its version"
if [ -n "$VERSION" ]; then
  TARBALL="antiphon@$VERSION"
else
  TARBALL="$(cd "$TMP" && npm pack "$REPO" --silent 2>/dev/null | tail -1)"
  TARBALL="$TMP/$TARBALL"
fi
npm install -g --prefix "$PREFIX" "$TARBALL" --silent >/dev/null 2>&1
export PATH="$PREFIX/bin:$PATH"
EXPECTED_VERSION="$(python3 -c "import json,sys; print(json.load(open('$REPO/package.json'))['version'])")"
[ -n "$VERSION" ] && EXPECTED_VERSION="$VERSION"
check "antiphon --version" "$(antiphon --version 2>&1)" "antiphon $EXPECTED_VERSION"
HANDSHAKE="$(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}\n' | ANTIPHON_CWD="$TMP" antiphon mcp 2>/dev/null | python3 -c "import sys,json; print(json.loads(sys.stdin.readline())['result']['serverInfo']['version'])")"
check "mcp handshake version" "$HANDSHAKE" "$EXPECTED_VERSION"

step "T1 — setup, then a clean bill of health"
mkdir -p "$PROJECT"; (cd "$PROJECT" && git init -q)
export ANTIPHON_CWD="$PROJECT"
(cd "$PROJECT" && antiphon setup >/dev/null 2>&1)
for f in .claude/settings.json .claude/settings.local.json .codex/hooks.json .codex/config.toml .mcp.json CLAUDE.md AGENTS.md; do
  [ -f "$PROJECT/$f" ] && pass "setup wrote $f" || fail "setup did not write $f"
done
check "~/.codex/config.toml untouched" "$(shasum -a 256 "$HOME/.codex/config.toml" 2>/dev/null | cut -d' ' -f1)" "$CODEX_CONFIG_BEFORE"
DOCTOR="$(cd "$PROJECT" && antiphon doctor 2>&1)"; DOCTOR_CODE=$?
check "doctor exit on a fresh project" "$DOCTOR_CODE" "0"
lacks "doctor finds nothing broken" "$DOCTOR" "✗"

step "T2 — Claude writes to a Codex that does not exist yet"
BEFORE_QUEUE="$(queued)"
# Two turns, so T5 has two distinct moments to place a cursor between. One
# turn renders both of its lines in the same second, and the `>=` boundary
# rule repeats the whole cohort sharing it — nothing to bound.
(cd "$PROJECT" && claude -p 'Respond with exactly one line and nothing else, no preamble: @codex e2e-probe-one' >/dev/null 2>&1)
sleep 2
(cd "$PROJECT" && claude -p 'Respond with exactly one line and nothing else, no preamble: @codex e2e-probe-two' >/dev/null 2>&1)
TRANSCRIPT="$(ls -t "$CLAUDE_DIR"/*.jsonl 2>/dev/null | head -1)"
[ -n "$TRANSCRIPT" ] && pass "claude -p wrote a transcript" || fail "claude -p wrote no transcript"
grep -qr '@codex e2e-probe-two' "$CLAUDE_DIR" 2>/dev/null && pass "the marker is in the transcript" || fail "the marker never reached the transcript"
[ -d "$PROJECT/.antiphon" ] && pass "the hooks ran (.antiphon exists)" || fail "the hooks never ran"
PUSH="$(printf '{"cwd":"%s","hook_event_name":"Stop","transcript_path":"%s","session_id":"%s"}' \
        "$PROJECT" "$TRANSCRIPT" "$(basename "$TRANSCRIPT" .jsonl)" | (cd "$PROJECT" && antiphon push codex 2>&1))"
contains "the push refuses instead of guessing" "$PUSH" "no Codex session found"
check "nothing is stranded in another thread" "$(queued)" "$BEFORE_QUEUE"

step "T3 — Codex's first turn reads what the refused push could not carry"
CODEX_OUT="$(cd "$PROJECT" && codex exec -s read-only --color never 'Reply with exactly: E2E-OK' 2>&1)"
contains "codex exec ran the SessionStart hook" "$CODEX_OUT" "hook: SessionStart"
# Through the package's own discovery, so a change to it fails here too.
ROLLOUT="$(PYTHONPATH="$PREFIX/lib/node_modules/antiphon/lib" python3 -c "
import antiphon
found = antiphon.codex_rollout_files('$PROJECT')
print(found[0] if found else '')")"
[ -n "$ROLLOUT" ] && pass "discovery finds this project's rollout" || fail "discovery found no rollout for this project"
HOOK_CMD="$(python3 -c "import json; print(json.load(open('$PROJECT/.codex/hooks.json'))['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'])")"
check "hooks.json declares the hook command" "$HOOK_CMD" "antiphon hook codex"
PAGE="$(printf '{"cwd":"%s","hook_event_name":"UserPromptSubmit","transcript_path":"%s","session_id":"%s"}' \
        "$PROJECT" "$ROLLOUT" "$(basename "$ROLLOUT" .jsonl | grep -o '[0-9a-f-]\{36\}')" \
        | (cd "$PROJECT" && eval "$HOOK_CMD") \
        | python3 -c "import sys,json; raw=sys.stdin.read().strip(); print(json.loads(raw)['hookSpecificOutput']['additionalContext'] if raw else '')")"
contains "the refused words arrive through the page" "$PAGE" "@codex e2e-probe-two"
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
# One helper, two legacy shapes. A v2 map is a scan high-water mark, so it
# replays from byte zero by design; a 0.1.0 float is a delivered boundary and
# must not. Comparing the two pages on the same transcripts measures the rule
# without guessing which timestamps happen to render.
seed() { python3 -c "import json,sys; json.dump(json.loads(sys.argv[1]), open('$PROJECT/.antiphon/cursor.json','w'))" "$1"; }
page_now() {
  printf '{"cwd":"%s","hook_event_name":"UserPromptSubmit","transcript_path":"%s","session_id":"%s"}' \
    "$PROJECT" "$ROLLOUT" "$(basename "$ROLLOUT" .jsonl | grep -o '[0-9a-f-]\{36\}')" \
    | (cd "$PROJECT" && eval "$HOOK_CMD") \
    | python3 -c "import sys,json; raw=sys.stdin.read().strip(); print(json.loads(raw)['hookSpecificOutput']['additionalContext'] if raw else '')"
}
events_in() { printf '%s' "$1" | grep -cE '^\[[0-9][0-9]:[0-9][0-9]\]'; }

seed '{"codex_seen": {"v": 2, "sources": {}}}'
FULL_PAGE="$(page_now)"; FULL="$(events_in "$FULL_PAGE")"

seed "{\"codex_seen\": $BOUNDARY}"
STATUS="$(cd "$PROJECT" && antiphon status 2>&1 | grep '^unread codex_pages')"
contains "status names the backlog in raw bytes" "$STATUS" "raw bytes"
contains "status names the way out" "$STATUS" "antiphon catch-up"
if printf '%s' "$STATUS" | grep -qE '[0-9]+ ~?pages?\b'; then
  fail "status guesses a page count: $STATUS"
else
  pass "status never guesses a page count"
fi
UPGRADE_PAGE="$(page_now)"; BOUNDED="$(events_in "$UPGRADE_PAGE")"
contains "the upgrade page says it is replaying" "$UPGRADE_PAGE" "replay:"
contains "and how to skip it" "$UPGRADE_PAGE" "antiphon catch-up"
if [ "$FULL" -lt 2 ]; then
  echo "  ---- too few visible events ($FULL) to compare the two legacy shapes"
elif [ "$BOUNDED" -lt "$FULL" ]; then
  pass "a 0.1.0 time bounds the replay ($BOUNDED of $FULL events, v2 map replays all $FULL)"
else
  fail "a 0.1.0 time did not bound the replay ($BOUNDED of $FULL events)"
fi
CATCHUP="$(cd "$PROJECT" && antiphon catch-up 2>&1)"
contains "catch-up says what it abandons" "$CATCHUP" "will not be delivered"
AFTER_PAGE="$(page_now)"
check "nothing is left to deliver after catch-up" "$(printf '%s' "$AFTER_PAGE" | tr -d '[:space:]')" ""

step "the machine is as it was"
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

printf '\n%s\n' "----------------------------------------"
printf 'passed %d, failed %d\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
