#!/usr/bin/env bash
set -Eeuo pipefail

FACTORY_SEMANTIC_STATUS_APPROVED='semantic_review_approved'
FACTORY_SEMANTIC_STATUS_BLOCKED='semantic_review_blocked'
FACTORY_SEMANTIC_STATUS_MISSING='semantic_review_missing_verdict'
FACTORY_SEMANTIC_STATUS_CONFLICTING='semantic_review_conflicting_verdict'
FACTORY_SEMANTIC_STATUS_TIMEOUT='semantic_review_timeout'
FACTORY_SEMANTIC_STATUS_MODEL_ERROR='semantic_review_model_error'
FACTORY_SEMANTIC_STATUS_HEAD_CHANGED='semantic_review_head_changed'

factory_strip_ansi() {
  sed -E $'s/\x1B\[[0-9;?]*[ -\/]*[@-~]//g'
}

factory_normalize_terminal_line() {
  local line="$1"
  line="$(printf '%s' "$line" | factory_strip_ansi)"
  line="$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<< "$line")"
  line="$(sed -E 's/^(\*\*|__|`)//; s/(\*\*|__|`)$//' <<< "$line")"
  printf '%s\n' "$line"
}

factory_terminal_marker() {
  local log_file="$1"
  [[ -f "$log_file" ]] || return 1
  factory_strip_ansi < "$log_file" \
    | awk 'BEGIN { fenced=0 } /^[[:space:]]*(```|~~~)/ { fenced = !fenced; next } !fenced { print }' \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
    | sed -E 's/^(\*\*|__|`)//; s/(\*\*|__|`)$//' \
    | grep -E '^FACTORY_GATE_(READY|BLOCKED|NOT_READY|REJECT)$' \
    | tail -n 1
}

factory_verdict_from_marker() {
  local marker="$1"
  case "$marker" in
    FACTORY_GATE_READY) printf '%s\n' approve ;;
    FACTORY_GATE_BLOCKED|FACTORY_GATE_NOT_READY) printf '%s\n' repair ;;
    FACTORY_GATE_REJECT) printf '%s\n' reject ;;
    *) return 1 ;;
  esac
}

factory_extract_semantic_verdict() {
  local log_file="$1" marker
  marker="$(factory_terminal_marker "$log_file" || true)"
  [[ -n "$marker" ]] || return 1
  factory_verdict_from_marker "$marker"
}

factory_review_has_conflicting_terminal_markers() {
  local log_file="$1" markers
  [[ -f "$log_file" ]] || return 1
  markers="$(factory_strip_ansi < "$log_file" \
    | awk 'BEGIN { fenced=0 } /^[[:space:]]*(```|~~~)/ { fenced = !fenced; next } !fenced { print }' \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
    | sed -E 's/^(\*\*|__|`)//; s/(\*\*|__|`)$//' \
    | grep -E '^FACTORY_GATE_(READY|BLOCKED|NOT_READY|REJECT)$' \
    | sort -u || true)"
  [[ "$(wc -l <<< "$markers" | tr -d ' ')" -gt 1 ]]
}

factory_review_is_substantive() {
  local log_file="$1" bytes
  [[ -f "$log_file" ]] || return 1
  bytes="$(tr -d '[:space:]' < "$log_file" | wc -c | tr -d ' ')"
  (( bytes >= 80 ))
}

factory_sanitize_review_log() {
  local input="$1" output="$2"
  factory_strip_ansi < "$input" \
    | sed -E 's#https://x-access-token:[^@[:space:]]+@github\.com/#https://x-access-token:[REDACTED]@github.com/#g' \
    | sed -E 's/(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)/[REDACTED_GITHUB_TOKEN]/g' \
    | sed -E 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
    > "$output"
}

# The review's verdict, decided by its line-anchored findings record against the
# exact head diff (factory_review_findings.py). Prints approve, repair or reject.
# Fails (prints nothing) when the record is missing or malformed, or the diff
# cannot be read: a failed review run, never an approval.
factory_line_review_verdict() {
  local log_file="$1" pr="$2" head="$3" files result rejects=()
  local policy
  policy="$(dirname "${TRUSTED_REVIEW_CONTROLLER:?}")/factory_review_findings.py"
  [[ -f "$policy" && -f "$log_file" ]] || return 1
  [[ "$(factory_terminal_marker "$log_file" || true)" == FACTORY_GATE_REJECT ]] && rejects=(--rejects)
  files="$(mktemp)"
  if ! gh api --paginate --slurp "repos/${GITHUB_REPOSITORY:?}/pulls/${pr}/files?per_page=100" > "$files" 2>/dev/null; then
    rm -f "$files"
    return 1
  fi
  result="$(python3 "$policy" check --log "$log_file" --head "$head" --files "$files" "${rejects[@]}")" || {
    rm -f "$files"
    return 1
  }
  rm -f "$files"
  jq -er '.verdict | select(. == "approve" or . == "repair" or . == "reject")' <<< "$result"
}
