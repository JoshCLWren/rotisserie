#!/usr/bin/env bash
# Discover and probe OpenCode models for coding-tool support.
#
# Each probe runs in its own process group. A watchdog kills only probes whose
# output heartbeat has gone stale, so a long but active probe may continue.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO="${COMIC_PILE_REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
MANIFEST_HELPER="$SCRIPT_DIR/opencode-model-manifest.sh"
STATE_DIR="${COMIC_PILE_FACTORY_STATE_DIR:-${SOURCE_REPO%/}-factory-state}"
PARALLEL="${MODEL_SCOUT_PARALLEL:-4}"
TIMEOUT="${MODEL_SCOUT_TIMEOUT:-900}"
WATCHDOG_POLL_SECONDS="${MODEL_SCOUT_WATCHDOG_POLL_SECONDS:-15}"
FAILURE_COOLDOWN_SECONDS="${MODEL_SCOUT_FAILURE_COOLDOWN_SECONDS:-3600}"
ALLOWED_PROVIDERS="${COMIC_PILE_FACTORY_ALLOWED_PROVIDERS:-opencode nvidia fcm-nvidia openrouter google}"
# Only these Google models are verified free on the free tier; probing the whole
# google/ prefix would otherwise probe paid pro models.
GOOGLE_FREE_MODELS="${COMIC_PILE_GOOGLE_FREE_MODELS:-google/gemini-3.1-flash-lite google/gemini-2.5-flash-lite}"
# OpenCode's Google provider reads GOOGLE_GENERATIVE_AI_API_KEY, but the
# credentials file exports GOOGLE_API_KEY. Alias it so free-tier Gemini models
# can be probed and confirmed.
if [[ -z "${GOOGLE_GENERATIVE_AI_API_KEY:-}" && -n "${GOOGLE_API_KEY:-}" ]]; then
  export GOOGLE_GENERATIVE_AI_API_KEY="$GOOGLE_API_KEY"
fi
WATCH=0
RECHECK_SECONDS=600
FORCE=0
LIMIT=0
EXPLICIT_MODELS=()
CANDIDATES_FILE=""

usage() {
  cat <<'USAGE'
Usage: opencode-model-scout.sh [options]

Options:
  --models "id1 id2 ..."      Explicit model list (overrides discovery).
  --candidates-file PATH      Newline-separated candidate list.
  --state-dir DIR             Manifest state directory.
  --parallel N                Concurrent probes (default 4).
  --timeout SECONDS           Maximum silence before a probe is killed (default 900).
  --watch                     Re-scan on an interval.
  --recheck-seconds N         Watch-mode interval (default 600).
  --force                     Re-probe confirmed models.
  --limit N                   Probe at most N candidates per pass.
  --once                      Run one pass and exit (default).
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

is_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

while (($#)); do
  case "$1" in
    --models)
      (($# >= 2)) || die "--models requires a value"
      read -r -a EXPLICIT_MODELS <<<"$2"
      shift 2
      ;;
    --candidates-file)
      (($# >= 2)) || die "--candidates-file requires a path"
      CANDIDATES_FILE="$2"
      shift 2
      ;;
    --state-dir)
      (($# >= 2)) || die "--state-dir requires a path"
      STATE_DIR="$2"
      shift 2
      ;;
    --parallel)
      (($# >= 2)) || die "--parallel requires a number"
      PARALLEL="$2"
      shift 2
      ;;
    --timeout)
      (($# >= 2)) || die "--timeout requires a number"
      TIMEOUT="$2"
      shift 2
      ;;
    --watch)
      WATCH=1
      shift
      ;;
    --recheck-seconds)
      (($# >= 2)) || die "--recheck-seconds requires a number"
      RECHECK_SECONDS="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --limit)
      (($# >= 2)) || die "--limit requires a number"
      LIMIT="$2"
      shift 2
      ;;
    --once)
      WATCH=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

is_nonnegative_integer "$PARALLEL" || die "--parallel must be an integer"
((PARALLEL >= 1)) || die "--parallel must be at least 1"
is_nonnegative_integer "$TIMEOUT" || die "--timeout must be an integer"
((TIMEOUT >= 1)) || die "--timeout must be at least 1"
is_nonnegative_integer "$WATCHDOG_POLL_SECONDS" || die "MODEL_SCOUT_WATCHDOG_POLL_SECONDS must be an integer"
((WATCHDOG_POLL_SECONDS >= 1)) || die "MODEL_SCOUT_WATCHDOG_POLL_SECONDS must be at least 1"
is_nonnegative_integer "$RECHECK_SECONDS" || die "--recheck-seconds must be an integer"
is_nonnegative_integer "$LIMIT" || die "--limit must be an integer"
is_nonnegative_integer "$FAILURE_COOLDOWN_SECONDS" || die "MODEL_SCOUT_FAILURE_COOLDOWN_SECONDS must be an integer"

for command in opencode jq date kill grep flock setsid stat sort sed wc tr awk head touch; do
  command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
done
[[ -x "$MANIFEST_HELPER" ]] || die "manifest helper is not executable: $MANIFEST_HELPER"

# Scout heartbeats are deliberately isolated from factory-run heartbeats.
HB_DIR="$STATE_DIR/scout-heartbeats"
LOG_DIR="$STATE_DIR/scout"
PROBE_PGID_FILE="$STATE_DIR/scout-probe-pgids"
INITIAL_PASS_FILE="$STATE_DIR/scout-initial-pass.done"
mkdir -p "$HB_DIR" "$LOG_DIR"
: >"$PROBE_PGID_FILE"
rm -f "$INITIAL_PASS_FILE"
"$MANIFEST_HELPER" init "$STATE_DIR"

safe_name() {
  printf '%s' "$1" | tr '/:@._ ' '______'
}

non_chat() {
  [[ "$1" == *embedding* || "$1" == *rerank* || "$1" == *whisper* || "$1" == *tts* \
    || "$1" == *-image* || "$1" == *image-* || "$1" == *safeguard* \
    || "$1" == *content-safety* || "$1" == *cosmos* || "$1" == *riva* \
    || "$1" == *nemotron-mini* || "$1" == *prompt-guard* || "$1" == *esm* ]]
}

discover_candidates() {
  local provider candidate
  if ((${#EXPLICIT_MODELS[@]} > 0)); then
    printf '%s\n' "${EXPLICIT_MODELS[@]}"
    return
  fi
  if [[ -n "$CANDIDATES_FILE" ]]; then
    [[ -f "$CANDIDATES_FILE" ]] || die "candidates file not found: $CANDIDATES_FILE"
    grep -v '^[[:space:]]*#' "$CANDIDATES_FILE" | grep -v '^[[:space:]]*$'
    return
  fi
  opencode models 2>/dev/null | while IFS= read -r candidate; do
    non_chat "$candidate" && continue
    for provider in $ALLOWED_PROVIDERS; do
      [[ "$candidate" == "$provider/"* ]] || continue
      # OpenRouter exposes paid models alongside :free ones; only free routes
      # and the free router belong in the curated pool.
      if [[ "$provider" == "openrouter" ]]; then
        [[ "$candidate" == *:free || "$candidate" == "openrouter/openrouter/free" ]] || continue
      fi
      # Google free-tier models are curated per-model so the prefix filter does
      # not probe paid pro models.
      if [[ "$provider" == "google" ]]; then
        [[ " $GOOGLE_FREE_MODELS " == *" $candidate "* ]] || continue
      fi
      printf '%s\n' "$candidate"
      break
    done
  done
}

probe_model() {
  set -o pipefail
  local model="$1" safe hb log prompt out status tool_state tool_out tool
  safe="$(safe_name "$model")"
  hb="$HB_DIR/$safe.hb"
  log="$LOG_DIR/${safe}.log"
  prompt="Use the bash tool to run this exact command: printf 'TOOL_OK_1234'. After the tool returns, reply with the word DONE."

  # Store both process-group id and the original model id. The filename is only
  # for filesystem safety and must never be written back to the manifest.
  printf '%s\t%s\n' "$$" "$model" >"$hb"
  touch "$hb"
  # The discovery list is the authoritative availability snapshot for this
  # pass; tool support is recorded separately by the probe result.
  "$MANIFEST_HELPER" availability "$model" yes "$STATE_DIR"
  # Record the probe's process group so the shutdown trap can terminate it.
  printf '%s\n' "$$" >>"$PROBE_PGID_FILE"

  set +e
  out="$(opencode run --model "$model" --auto --format json --print-logs=false "$prompt" 2>&1 \
    | while IFS= read -r line; do
        touch "$hb"
        printf '%s\n' "$line"
      done)"
  status=$?
  set -e

  tool_state="$(printf '%s' "$out" | jq -r 'select(.type=="tool_use" and .part.tool=="bash") | .part.state.status' 2>/dev/null | head -1)"
  tool_out="$(printf '%s' "$out" | jq -r 'select(.type=="tool_use" and .part.tool=="bash") | .part.state.output' 2>/dev/null | head -1)"
  tool="no"
  if [[ "$tool_state" == "completed" && "$tool_out" == *TOOL_OK_1234* ]]; then
    tool="yes"
  fi

  printf '%s\n' "$out" >"$log"
  if [[ "$tool" == "yes" ]]; then
    "$MANIFEST_HELPER" set "$model" confirmed yes "$STATE_DIR"
    printf 'PASS  %-45s tool-call ok\n' "$model"
  else
    "$MANIFEST_HELPER" set "$model" failed no "$STATE_DIR"
    printf 'FAIL  %-45s no tool-call (exit %s)\n' "$model" "$status"
  fi
  rm -f "$hb"
}

export -f safe_name probe_model
export HB_DIR LOG_DIR MANIFEST_HELPER STATE_DIR PROBE_PGID_FILE
export INITIAL_PASS_FILE

file_mtime() {
  # GNU stat prints seconds via -c %Y; BSD/macOS needs -f %m. Prefer GNU and
  # fall back to BSD, defaulting to "now" so a missing value never kills a
  # healthy probe.
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || date +%s
}

watchdog() {
  while :; do
    local now hb age pgid model
    now="$(date +%s)"
    for hb in "$HB_DIR"/*.hb; do
      [[ -f "$hb" ]] || continue
      age=$((now - $(file_mtime "$hb")))
      ((age > TIMEOUT)) || continue
      IFS=$'\t' read -r pgid model <"$hb" || true
      [[ "$pgid" =~ ^[0-9]+$ && -n "$model" ]] || { rm -f "$hb"; continue; }
      if kill -0 -- "-$pgid" 2>/dev/null; then
        printf 'WATCHDOG: killing %s (pgid %s), no output for %ss\n' "$model" "$pgid" "$age" >&2
        # Persist the original model id before killing the probe. The parent can
        # reap the killed job immediately, so recording after the signal races
        # with scout shutdown.
        "$MANIFEST_HELPER" set "$model" failed no "$STATE_DIR" >/dev/null 2>&1 || true
        kill -KILL -- "-$pgid" 2>/dev/null || true
      fi
      rm -f "$hb"
    done
    sleep "$WATCHDOG_POLL_SECONDS"
  done
}

prune_finished_jobs() {
  local -n job_refs=$1
  local -a alive=()
  local pid
  for pid in "${job_refs[@]}"; do
    kill -0 "$pid" 2>/dev/null && alive+=("$pid")
  done
  job_refs=("${alive[@]}")
}

run_pass() {
  local candidates total model pending
  candidates="$(discover_candidates | sed '/^$/d')"
  if [[ "$FORCE" != "1" ]]; then
    pending="$("$MANIFEST_HELPER" pending "$STATE_DIR" "$FAILURE_COOLDOWN_SECONDS" 2>/dev/null || true)"
    candidates="$(printf '%s\n' "$candidates" | while IFS= read -r model; do
      [[ -n "$model" ]] || continue
      # Models not present in the manifest are untested and must enter the
      # initial pass. Previously the cooldown filter accidentally removed
      # those candidates before probe_model could create their manifest rows.
      if ! grep -Fq -- "$model"$'\t' "$STATE_DIR/model_manifest.tsv" \
        || grep -Fxq -- "$model" <<<"$pending"; then
        printf '%s\n' "$model"
      fi
    done)"
  fi
  candidates="$(printf '%s\n' "$candidates" | sed '/^$/d' | sort -u)"
  total="$(printf '%s\n' "$candidates" | sed '/^$/d' | wc -l | tr -d ' ')"
  if ((LIMIT > 0 && total > LIMIT)); then
    candidates="$(printf '%s\n' "$candidates" | head -n "$LIMIT")"
    total="$LIMIT"
  fi

  if ((total == 0)); then
    printf 'Scout: no pending candidates%s.\n' "$([[ "$FORCE" == "1" ]] && printf ' (--force)' || true)"
    return 0
  fi

  printf 'Scout: probing %d candidate model(s) (parallel=%d, silence timeout=%ss)\n' "$total" "$PARALLEL" "$TIMEOUT"
  local -a jobs=()
  while IFS= read -r model; do
    [[ -n "$model" ]] || continue
    while ((${#jobs[@]} >= PARALLEL)); do
      wait -n "${jobs[@]}" 2>/dev/null || true
      prune_finished_jobs jobs
    done
    setsid bash -c 'probe_model "$1"' _ "$model" &
    jobs+=("$!")
  done <<<"$candidates"

  while ((${#jobs[@]} > 0)); do
    wait -n "${jobs[@]}" 2>/dev/null || true
    prune_finished_jobs jobs
  done
  rm -f "$HB_DIR"/*.hb 2>/dev/null || true
}

watchdog &
WATCHDOG_PID=$!

stop_probes() {
  local pgid
  while IFS= read -r pgid; do
    [[ "$pgid" =~ ^[0-9]+$ ]] || continue
    kill -TERM -- "-$pgid" 2>/dev/null || true
  done <"$PROBE_PGID_FILE"
  rm -f "$PROBE_PGID_FILE"
}

trap 'kill "$WATCHDOG_PID" 2>/dev/null || true; stop_probes' EXIT

printf 'OpenCode model scout (state=%s, parallel=%s, silence timeout=%ss)\n' "$STATE_DIR" "$PARALLEL" "$TIMEOUT"
if ((WATCH == 1)); then
  while true; do
    run_pass
    touch "$INITIAL_PASS_FILE"
    printf 'Scout: sleeping %ss before re-scan.\n' "$RECHECK_SECONDS"
    sleep "$RECHECK_SECONDS"
  done
else
  run_pass
  touch "$INITIAL_PASS_FILE"
fi

printf '\n=== Confirmed models ===\n'
"$MANIFEST_HELPER" summary "$STATE_DIR"
