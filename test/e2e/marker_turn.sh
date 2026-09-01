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

for attempt in 1 2 3; do
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
    1) ;;
    *)
      echo "the exact-assistant marker probe failed" >&2
      exit 2
      ;;
  esac
done

echo "exact assistant marker absent after 3 successful Claude turns" >&2
exit 1
