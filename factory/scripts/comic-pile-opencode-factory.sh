#!/usr/bin/env bash
# Rotate models between independent factory heartbeats.
#
# The full single-heartbeat implementation lives in
# comic-pile-opencode-factory-heartbeat.sh. Keeping orchestration here makes
# model selection happen for every heartbeat instead of once per long-lived run.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HEARTBEAT_RUNNER="$SCRIPT_DIR/comic-pile-opencode-factory-heartbeat.sh"
MANIFEST_HELPER="$SCRIPT_DIR/opencode-model-manifest.sh"
MODEL_SCOUT="$SCRIPT_DIR/opencode-model-scout.sh"
SOURCE_REPO="${COMIC_PILE_REPO:-/mnt/extra/josh/code/comic-pile}"
STATE_DIR="${COMIC_PILE_FACTORY_STATE_DIR:-}"
DEFAULT_MODEL="${COMIC_PILE_DEFAULT_MODEL:-deepseek/deepseek-v4-flash}"
PINNED_MODEL="${OPENCODE_MODEL:-}"
IDLE_SECONDS="${FACTORY_IDLE_SECONDS:-60}"
MODE="drain"
RUN_ONCE=0
STATE_DIR_EXPLICIT=0
FORWARD_ARGS=()
MAX_FAILURES="${FACTORY_MAX_FAILURES:-2}"
WAIT_FOR_SCOUT="${COMIC_PILE_FACTORY_WAIT_FOR_SCOUT:-0}"
SCOUT_READY_FILE="${COMIC_PILE_FACTORY_SCOUT_READY_FILE:-}"
SCOUT_PID_FILE="${COMIC_PILE_FACTORY_SCOUT_PID_FILE:-}"
AUTO_SCOUT="${COMIC_PILE_FACTORY_AUTO_SCOUT:-1}"
SCOUT_PARALLEL="${SCOUT_PARALLEL:-4}"
SCOUT_TIMEOUT="${MODEL_SCOUT_TIMEOUT:-${FACTORY_HEARTBEAT_TIMEOUT:-60}}"
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
FAILURE_THRESHOLD="${FACTORY_FAILURE_THRESHOLD:-2}"

usage() {
  "$HEARTBEAT_RUNNER" --help
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
    --watch)
      MODE="watch"
      shift
      ;;
    --once)
      RUN_ONCE=1
      shift
      ;;
    --model)
      (($# >= 2)) || die "--model requires an id"
      PINNED_MODEL="$2"
      shift 2
      ;;
    --repo)
      (($# >= 2)) || die "--repo requires a path"
      SOURCE_REPO="$2"
      FORWARD_ARGS+=("$1" "$2")
      shift 2
      ;;
    --state-dir)
      (($# >= 2)) || die "--state-dir requires a path"
      STATE_DIR="$2"
      STATE_DIR_EXPLICIT=1
      shift 2
      ;;
    --idle-seconds)
      (($# >= 2)) || die "--idle-seconds requires a number"
      IDLE_SECONDS="$2"
      FORWARD_ARGS+=("$1" "$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$STATE_DIR" || "$STATE_DIR_EXPLICIT" == "0" && -z "${COMIC_PILE_FACTORY_STATE_DIR:-}" ]]; then
  STATE_DIR="${SOURCE_REPO%/}-factory-state"
fi
[[ -n "$SCOUT_READY_FILE" ]] || SCOUT_READY_FILE="$STATE_DIR/scout-initial-pass.done"

is_nonnegative_integer "$IDLE_SECONDS" || die "FACTORY_IDLE_SECONDS must be an integer"
is_nonnegative_integer "$MAX_FAILURES" || die "FACTORY_MAX_FAILURES must be an integer"
((MAX_FAILURES >= 1)) || die "FACTORY_MAX_FAILURES must be at least 1"
is_nonnegative_integer "$SCOUT_PARALLEL" || die "SCOUT_PARALLEL must be an integer"
((SCOUT_PARALLEL >= 1)) || die "SCOUT_PARALLEL must be at least 1"
[[ "$SCOUT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die "MODEL_SCOUT_TIMEOUT must be a positive integer"
is_nonnegative_integer "$FAILURE_THRESHOLD" || die "FACTORY_FAILURE_THRESHOLD must be an integer"
((FAILURE_THRESHOLD >= 1)) || die "FACTORY_FAILURE_THRESHOLD must be at least 1"
[[ "$AUTO_SCOUT" == "0" || "$AUTO_SCOUT" == "1" ]] || die "COMIC_PILE_FACTORY_AUTO_SCOUT must be 0 or 1"
[[ -x "$HEARTBEAT_RUNNER" ]] || die "heartbeat runner is not executable: $HEARTBEAT_RUNNER"
[[ -x "$MANIFEST_HELPER" ]] || die "manifest helper is not executable: $MANIFEST_HELPER"
mkdir -p "$STATE_DIR"
"$MANIFEST_HELPER" init "$STATE_DIR"

non_chat_model() {
  [[ "$1" == *embedding* || "$1" == *rerank* || "$1" == *whisper* || "$1" == *tts* \
    || "$1" == *-image* || "$1" == *image-* || "$1" == *safeguard* \
    || "$1" == *content-safety* || "$1" == *cosmos* || "$1" == *riva* \
    || "$1" == *nemotron-mini* || "$1" == *prompt-guard* || "$1" == *esm* ]]
}

wait_for_initial_scout() {
  [[ "$WAIT_FOR_SCOUT" == "1" ]] || return 0
  printf 'Waiting for the model scout to complete its initial pass...\n'
  while [[ ! -f "$SCOUT_READY_FILE" ]]; do
    if [[ -n "$SCOUT_PID_FILE" && -f "$SCOUT_PID_FILE" ]]; then
      local scout_pid
      scout_pid="$(cat "$SCOUT_PID_FILE" 2>/dev/null || true)"
      if [[ "$scout_pid" =~ ^[0-9]+$ ]] && ! kill -0 -- "-$scout_pid" 2>/dev/null; then
        die "model scout exited before completing its initial pass"
      fi
    fi
    sleep 2
  done
  printf 'Model scout initial pass complete; refreshing the complete provider list.\n'
}

refresh_model_manifest() {
  local cooldown="${1:-3600}"
  local candidates_file model

  [[ -z "$PINNED_MODEL" && "$AUTO_SCOUT" == "1" ]] || return 0
  command -v opencode >/dev/null 2>&1 || return 0
  [[ -x "$MODEL_SCOUT" ]] || {
    printf 'WARNING: model scout is not executable: %s\n' "$MODEL_SCOUT" >&2
    return 1
  }

  candidates_file="$(mktemp "$STATE_DIR/.model-candidates.XXXXXX")"
  while IFS= read -r model; do
    [[ -n "$model" ]] || continue
    non_chat_model "$model" && continue
    for provider in $ALLOWED_PROVIDERS; do
      [[ "$model" == "$provider/"* ]] || continue
      # OpenRouter exposes paid models alongside :free ones; only free routes
      # and the free router belong in the curated pool.
      if [[ "$provider" == "openrouter" ]]; then
        [[ "$model" == *:free || "$model" == "openrouter/openrouter/free" ]] || continue
      fi
      # Google free-tier models are curated per-model so the prefix filter does
      # not probe paid pro models.
      if [[ "$provider" == "google" ]]; then
        [[ " $GOOGLE_FREE_MODELS " == *" $model "* ]] || continue
      fi
      printf '%s\n' "$model"
      break
    done
  done < <(opencode models 2>/dev/null) >"$candidates_file"

  if [[ ! -s "$candidates_file" ]]; then
    rm -f "$candidates_file"
    printf 'WARNING: opencode models returned no usable chat models; keeping the existing manifest.\n' >&2
    return 1
  fi

  printf 'Refreshing OpenCode model manifest from all available providers...\n'
  set +e
  MODEL_SCOUT_FAILURE_COOLDOWN_SECONDS="$cooldown" \
    "$MODEL_SCOUT" --once \
    --state-dir "$STATE_DIR" \
    --parallel "$SCOUT_PARALLEL" \
    --timeout "$SCOUT_TIMEOUT" \
    --candidates-file "$candidates_file"
  local status=$?
  set -e
  rm -f "$candidates_file"
  return "$status"
}

select_model() {
  if [[ -n "$PINNED_MODEL" ]]; then
    printf '%s\n' "$PINNED_MODEL"
  else
    "$MANIFEST_HELPER" next "$DEFAULT_MODEL" "$STATE_DIR"
  fi
}

failure_count_file() {
  printf '%s\n' "$STATE_DIR/model-failures/$(printf '%s' "$1" | tr '/:@._ ' '______')"
}

wait_for_initial_scout
refresh_model_manifest 3600 || true

heartbeat=0
recovery_scout_attempted=0
while true; do
  heartbeat=$((heartbeat + 1))
  model="$(select_model)"
  result_file="$(mktemp "$STATE_DIR/.factory-result.XXXXXX")"
  printf '\nFactory wrapper heartbeat %d using model %s\n' "$heartbeat" "$model"

  set +e
  "$HEARTBEAT_RUNNER" --once --state-dir "$STATE_DIR" --model "$model" \
    "${FORWARD_ARGS[@]}" 2>&1 | tee "$result_file"
  status=${PIPESTATUS[0]}
  set -e

  # The heartbeat implementation owns its watchdog files, but the wrapper owns
  # process lifetime. Remove completed files so they cannot accumulate forever.
  rm -f "$STATE_DIR"/heartbeats/factory_heartbeat_*.hb 2>/dev/null || true

  if ((status == 0)); then
    rm -f -- "$(failure_count_file "$model")"
  fi

  if ((status != 0)); then
    # A watchdog kill (token-per-minute limit or heartbeat silence timeout) is a
    # transient interruption, not evidence the model is broken. Productive runs
    # are frequently silent for long stretches (long test suites, git push
    # hooks), so rotate away without retiring the model.
    if grep -Eiq 'WATCHDOG:|tokens per minute' "$result_file"; then
      printf 'Heartbeat for %s was interrupted by the watchdog; rotating without retiring.\n' "$model" >&2
      rm -f "$result_file"
      continue
    fi
    # Genuine failures retire a model only after FACTORY_FAILURE_THRESHOLD
    # consecutive wrapper heartbeats, so a single bad run cannot drain the
    # manifest of verified models.
    mkdir -p "$STATE_DIR/model-failures"
    fail_file="$(failure_count_file "$model")"
    fail_count=$(( $(cat "$fail_file" 2>/dev/null || printf 0) + 1 ))
    printf '%s\n' "$fail_count" >"$fail_file"
    if ((fail_count < FAILURE_THRESHOLD)); then
      printf 'Heartbeat failed for %s (%d/%d); rotating without retiring.\n' "$model" "$fail_count" "$FAILURE_THRESHOLD" >&2
      rm -f "$result_file"
      continue
    fi
    "$MANIFEST_HELPER" fail "$model" "$STATE_DIR" >/dev/null 2>&1 || true
    rm -f -- "$fail_file"
    rm -f "$result_file"
    if [[ -n "$PINNED_MODEL" ]]; then
      printf 'Factory stopped after failure of pinned model %s.\n' "$model" >&2
      exit "$status"
    fi
    if ! "$MANIFEST_HELPER" confirmed "$STATE_DIR" | grep -q .; then
      if ((recovery_scout_attempted == 0)) && [[ "$AUTO_SCOUT" == "1" ]]; then
        recovery_scout_attempted=1
        printf 'No confirmed models remain; immediately re-probing every available provider.\n' >&2
        refresh_model_manifest 0 || true
      fi
      if ! "$MANIFEST_HELPER" confirmed "$STATE_DIR" | grep -q .; then
        printf 'Factory stopped: no confirmed models remain after exhausting all available providers.\n' >&2
        exit "$status"
      fi
    fi
    printf 'Heartbeat failed for %s; immediately rotating to the next model (no cooldown).\n' \
      "$model" >&2
    continue
  fi

  if grep -Fq 'FACTORY_RESULT: changed' "$result_file"; then
    rm -f "$result_file"
    if ((RUN_ONCE == 1)); then
      exit 0
    fi
    continue
  fi

  if grep -Fq 'FACTORY_RESULT: idle' "$result_file"; then
    rm -f "$result_file"
    if ((RUN_ONCE == 1)) || [[ "$MODE" == "drain" ]]; then
      exit 0
    fi
    sleep "$IDLE_SECONDS"
    continue
  fi

  rm -f "$result_file"
  die "heartbeat runner returned success without a terminal FACTORY_RESULT marker"
done
