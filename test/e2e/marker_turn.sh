#!/usr/bin/env bash
# Land one exact Claude assistant marker. This helper never retries any later
# E2E stage and never prints the prompt or response content.

set -uo pipefail

if [ "$#" -ne 3 ] || [ -z "$3" ]; then
  echo "usage: marker_turn.sh PROJECT CLAUDE_DIR EXACT_MARKER" >&2
  exit 2
fi

PROJECT="$1"
CLAUDE_DIR="$2"
MARKER="$3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/marker_contract.sh" || exit 2

attempt=1
while [ "$attempt" -le "$MAX_MARKER_ATTEMPTS" ]; do
  if ! (cd "$PROJECT" && claude -p \
      "Respond with exactly one line and nothing else, no preamble: $MARKER" \
      >/dev/null 2>&1); then
    echo "claude -p exited non-zero on attempt $attempt" >&2
    exit 2
  fi

  transcript="$(python3 "$SCRIPT_DIR/marker_probe.py" "$CLAUDE_DIR" "$MARKER")"
  probe_code=$?
  case "$probe_code" in
    0)
      printf 'attempt=%s\ntranscript=%s\n' "$attempt" "$transcript"
      exit 0
      ;;
    3) ;;
    *)
      echo "the exact-assistant marker probe failed; not retrying Claude" >&2
      exit 2
      ;;
  esac
  attempt=$((attempt + 1))
done

echo "exact assistant marker absent after $MAX_MARKER_ATTEMPTS successful Claude turns" >&2
exit 1
