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
# An absence proves nothing about a text that is not there: an empty page
# lacks every word, and a check reading "the page does not replay" would pass
# for a page that was never produced.
lacks() {
  if [ -z "$2" ]; then fail "$1 — kanıt yok: ölçülen metin boş"; return; fi
  case "$2" in *"$3"*) fail "$1 — olmamalıydı: $3" ;; *) pass "$1" ;; esac
}

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
# A queue that cannot be read must not answer 0 twice and call that proof.
queued() { sqlite3 "$QUEUE" 'SELECT count(*) FROM queued_items;' 2>/dev/null || echo unreadable; }

cleanup() {
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
else
  TARBALL="$(cd "$TMP" && npm pack "$REPO" --silent 2>/dev/null | tail -1)"
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
for f in .claude/settings.json .claude/settings.local.json .codex/hooks.json .codex/config.toml .mcp.json CLAUDE.md AGENTS.md; do
  [ -f "$PROJECT/$f" ] && pass "setup wrote $f" || fail "setup did not write $f"
done
check "~/.codex/config.toml untouched" "$(shasum -a 256 "$HOME/.codex/config.toml" 2>/dev/null | cut -d' ' -f1)" "$CODEX_CONFIG_BEFORE"
DOCTOR="$(cd "$PROJECT" && antiphon doctor 2>&1)"; DOCTOR_CODE=$?
check "doctor exit on a fresh project" "$DOCTOR_CODE" "0"
lacks "doctor finds nothing broken" "$DOCTOR" "✗"

step "T2 — Claude writes to a Codex that does not exist yet"
BEFORE_QUEUE="$(queued)"
case "$BEFORE_QUEUE" in ''|*[!0-9]*) fail "Codex's queue could not be read; the stranding check would prove nothing" ;; esac
# Two turns, so T5 has two distinct moments to place a cursor between. One
# turn renders both of its lines in the same second, and the `>=` boundary
# rule repeats the whole cohort sharing it — nothing to bound.
(cd "$PROJECT" && claude -p 'Respond with exactly one line and nothing else, no preamble: @codex e2e-probe-one' >/dev/null 2>&1) \
  && pass "the first claude -p turn exits 0" || fail "the first claude -p turn failed"
sleep 2
(cd "$PROJECT" && claude -p 'Respond with exactly one line and nothing else, no preamble: @codex e2e-probe-two' >/dev/null 2>&1) \
  && pass "the second claude -p turn exits 0" || fail "the second claude -p turn failed"
TRANSCRIPT="$(ls -t "$CLAUDE_DIR"/*.jsonl 2>/dev/null | head -1)"
[ -n "$TRANSCRIPT" ] && pass "claude -p wrote a transcript" || fail "claude -p wrote no transcript"
grep -qr '@codex e2e-probe-two' "$CLAUDE_DIR" 2>/dev/null && pass "the marker is in the transcript" || fail "the marker never reached the transcript"
[ -d "$PROJECT/.antiphon" ] && pass "the hooks ran (.antiphon exists)" || fail "the hooks never ran"
PUSH="$(printf '{"cwd":"%s","hook_event_name":"Stop","transcript_path":"%s","session_id":"%s"}' \
        "$PROJECT" "$TRANSCRIPT" "$(basename "$TRANSCRIPT" .jsonl)" | (cd "$PROJECT" && antiphon push codex 2>&1))"
contains "the push refuses instead of guessing" "$PUSH" "no Codex session found"
AFTER_QUEUE="$(queued)"
case "$AFTER_QUEUE" in ''|*[!0-9]*) fail "Codex's queue could not be read after the push" ;; esac
STRANDED="$(sqlite3 -readonly "$QUEUE" "SELECT count(*) FROM queued_items WHERE payload_json LIKE '%e2e-probe%';" 2>&1)"
check "this run stranded nothing of its own" "$STRANDED" "0"
check "and left the queue as it found it" "$AFTER_QUEUE" "$BEFORE_QUEUE"

step "T3 — Codex's first turn reads what the refused push could not carry"
BEFORE_ROLLOUTS="$(mktemp)"; find "$HOME/.codex/sessions" -name 'rollout-*.jsonl' 2>/dev/null | sort > "$BEFORE_ROLLOUTS"
CODEX_OUT="$(cd "$PROJECT" && codex exec -s read-only --color never 'Reply with exactly: E2E-OK' 2>&1)" \
  && pass "codex exec exits 0" || fail "codex exec failed"
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
HOOK_CMD="$(python3 -c "import json; print(json.load(open('$PROJECT/.codex/hooks.json'))['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'])")"
check "hooks.json declares the hook command" "$HOOK_CMD" "antiphon hook codex"
PAGE="$(printf '{"cwd":"%s","hook_event_name":"UserPromptSubmit","transcript_path":"%s","session_id":"%s"}' \
        "$PROJECT" "$ROLLOUT" "$(basename "$ROLLOUT" .jsonl | grep -o '[0-9a-f-]\{36\}')" \
        | (cd "$PROJECT" && eval "$HOOK_CMD") \
        | python3 -c "import sys,json; raw=sys.stdin.read().strip(); print(json.loads(raw)['hookSpecificOutput']['additionalContext'] if raw else '')")"
# The relayed prompt carries the same words, so the label decides: this must
# be Claude's own line, which is what the refused push could not carry.
SAID="$(printf '%s' "$PAGE" | grep -A1 '^\[[0-9][0-9]:[0-9][0-9]\] Claude:$' | grep '@codex e2e-probe-two' || true)"
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
    | (cd "$PROJECT" && eval "$HOOK_CMD"))" || { echo "HOOK-FAILED"; return 1; }
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
contains "the v2 map replays the first turn" "$FULL_PAGE" "e2e-probe-one"
contains "and the second" "$FULL_PAGE" "e2e-probe-two"
lacks "the 0.1.0 time leaves the first turn where it was" "$UPGRADE_PAGE" "e2e-probe-one"
contains "and replays the turn it recorded" "$UPGRADE_PAGE" "e2e-probe-two"
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
