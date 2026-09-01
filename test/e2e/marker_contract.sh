#!/usr/bin/env bash
# Guards used by the real fresh-user flow and its deterministic shell fixture.
# Bash 3.2 has no associative arrays, so the two approved stage names keep
# explicit state and an unknown name fails closed.

: "${E2E_PUSH_RAN:=0}"
: "${E2E_PAGE_RAN:=0}"

e2e_once() {
  case "${1:-}" in
    push)
      [ "$E2E_PUSH_RAN" = "0" ] || return 1
      E2E_PUSH_RAN=1
      ;;
    page)
      [ "$E2E_PAGE_RAN" = "0" ] || return 1
      E2E_PAGE_RAN=1
      ;;
    *) return 2 ;;
  esac
  return 0
}

preserve_marker_evidence() {
  local label="$1" temp_root="$2" transcript_root="$3"
  KEEP=1
  echo "$label: exact assistant marker absent after three exit-zero turns" >&2
  echo "preserving evidence: $temp_root and $transcript_root" >&2
  return 1
}
