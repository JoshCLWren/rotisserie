#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO="${COMIC_PILE_REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
RUNNER="$SCRIPT_DIR/comic-pile-opencode-factory.sh"
SCOUT="$SCRIPT_DIR/opencode-model-scout.sh"
STATE_DIR="${COMIC_PILE_FACTORY_STATE_DIR:-${SOURCE_REPO%/}-factory-state}"
PID_FILE="$STATE_DIR/overnight.pid"
SCOUT_PID_FILE="$STATE_DIR/overnight-scout.pid"
SUPERVISOR_LOG="$STATE_DIR/overnight.log"
DEFAULT_MODEL="deepseek/deepseek-v4-flash"
SCOUT_PARALLEL="${SCOUT_PARALLEL:-4}"

usage() {
  cat <<'USAGE'
Usage: bash scripts/comic-pile-opencode-factory-overnight.sh <start|stop|status|run> [factory options]

Commands:
  start    Launch the continuous OpenCode factory in the background.
  stop     Stop the background factory and all of its child processes.
  status   Report whether the supervised factory is running.
  run      Run continuously in the foreground.

Environment defaults:
  OPENCODE_MODEL=deepseek/deepseek-v4-flash
  FACTORY_IDLE_SECONDS=60
  FACTORY_HEARTBEAT_TIMEOUT=60
  FACTORY_FAILURE_BACKOFF_SECONDS=0
  FACTORY_MAX_FAILURES=2

Additional factory options are passed to comic-pile-opencode-factory.sh after the selected command.
Examples:
  bash scripts/comic-pile-opencode-factory-overnight.sh start
  bash scripts/comic-pile-opencode-factory-overnight.sh start --idle-seconds 30
  bash scripts/comic-pile-opencode-factory-overnight.sh run --idle-seconds 30
  bash scripts/comic-pile-opencode-factory-overnight.sh status
  bash scripts/comic-pile-opencode-factory-overnight.sh stop
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

read_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

is_running() {
  local pid
  pid="$(read_pid)" || return 1
  kill -0 -- "-$pid" 2>/dev/null
}

cleanup_stale_pid() {
  if [[ -f "$PID_FILE" ]] && ! is_running; then
    rm -f "$PID_FILE"
  fi
}

run_factory() {
  # Preserve an explicit OPENCODE_MODEL override; otherwise let the runner rotate
  # among confirmed models, falling back to COMIC_PILE_DEFAULT_MODEL.
  if [[ -n "${OPENCODE_MODEL:-}" ]]; then
    export OPENCODE_MODEL
  else
    unset OPENCODE_MODEL 2>/dev/null || true
  fi
  export COMIC_PILE_DEFAULT_MODEL="${COMIC_PILE_DEFAULT_MODEL:-$DEFAULT_MODEL}"
  export FACTORY_IDLE_SECONDS="${FACTORY_IDLE_SECONDS:-60}"
  export FACTORY_HEARTBEAT_TIMEOUT="${FACTORY_HEARTBEAT_TIMEOUT:-60}"
  export FACTORY_FAILURE_BACKOFF_SECONDS="${FACTORY_FAILURE_BACKOFF_SECONDS:-0}"
  export FACTORY_MAX_FAILURES="${FACTORY_MAX_FAILURES:-2}"
  "$RUNNER" --watch "$@"
}

read_scout_pid() {
  [[ -f "$SCOUT_PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$SCOUT_PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

is_scout_running() {
  local pid
  pid="$(read_scout_pid)" || return 1
  kill -0 -- "-$pid" 2>/dev/null
}

cleanup_stale_scout_pid() {
  if [[ -f "$SCOUT_PID_FILE" ]] && ! is_scout_running; then
    rm -f "$SCOUT_PID_FILE"
  fi
}

start_scout() {
  if is_scout_running; then
    printf 'Model scout is already running (process group %s).\n' "$(read_scout_pid)"
    return 0
  fi
  [[ -x "$SCOUT" ]] || die "model scout is not executable: $SCOUT"
  rm -f "$STATE_DIR/scout-initial-pass.done"
  printf 'Starting model scout (parallel=%s, heartbeat timeout=%ss). Scout log: %s\n' \
    "$SCOUT_PARALLEL" "${FACTORY_HEARTBEAT_TIMEOUT:-60}" "$SUPERVISOR_LOG"
  nohup setsid env \
    COMIC_PILE_FACTORY_STATE_DIR="$STATE_DIR" \
    "$SCOUT" --watch --state-dir "$STATE_DIR" \
    --parallel "$SCOUT_PARALLEL" \
    --timeout "${FACTORY_HEARTBEAT_TIMEOUT:-60}" \
    >>"$SUPERVISOR_LOG" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$SCOUT_PID_FILE"
  sleep 1
  if ! kill -0 -- "-$pid" 2>/dev/null; then
    rm -f "$SCOUT_PID_FILE"
    printf 'Model scout exited during startup; inspect %s\n' "$SUPERVISOR_LOG" >&2
  fi
}

stop_scout() {
  if ! is_scout_running; then
    cleanup_stale_scout_pid
    return 0
  fi
  local pid
  pid="$(read_scout_pid)"
  printf 'Stopping model scout process group %s...\n' "$pid"
  kill -TERM -- "-$pid"
  for _ in {1..10}; do
    if ! kill -0 -- "-$pid" 2>/dev/null; then
      rm -f "$SCOUT_PID_FILE"
      return 0
    fi
    sleep 1
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
  rm -f "$SCOUT_PID_FILE"
}

command="${1:-}"
[[ -n "$command" ]] || { usage; exit 2; }
shift

[[ -x "$RUNNER" ]] || die "factory runner is not executable: $RUNNER"
command -v setsid >/dev/null 2>&1 || die "required command not found: setsid"
mkdir -p "$STATE_DIR"
cleanup_stale_pid
cleanup_stale_scout_pid

case "$command" in
  start)
    if is_running; then
      printf 'ComicPile overnight factory is already running (process group %s).\n' "$(read_pid)"
      exit 0
    fi

    start_scout
    printf 'Starting ComicPile overnight factory with model %s. Supervisor log: %s\n' "${OPENCODE_MODEL:-$DEFAULT_MODEL}" "$SUPERVISOR_LOG"
    nohup setsid env \
      ${OPENCODE_MODEL:+OPENCODE_MODEL="$OPENCODE_MODEL"} \
      COMIC_PILE_DEFAULT_MODEL="${COMIC_PILE_DEFAULT_MODEL:-$DEFAULT_MODEL}" \
      FACTORY_IDLE_SECONDS="${FACTORY_IDLE_SECONDS:-60}" \
      FACTORY_FAILURE_BACKOFF_SECONDS="${FACTORY_FAILURE_BACKOFF_SECONDS:-0}" \
      FACTORY_MAX_FAILURES="${FACTORY_MAX_FAILURES:-1}" \
      COMIC_PILE_FACTORY_WAIT_FOR_SCOUT=1 \
      COMIC_PILE_FACTORY_SCOUT_READY_FILE="$STATE_DIR/scout-initial-pass.done" \
      COMIC_PILE_FACTORY_SCOUT_PID_FILE="$SCOUT_PID_FILE" \
      "$RUNNER" --watch "$@" >>"$SUPERVISOR_LOG" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" >"$PID_FILE"
    sleep 1

    if ! kill -0 -- "-$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      die "factory exited during startup; inspect $SUPERVISOR_LOG"
    fi

    printf 'ComicPile overnight factory started (process group %s).\n' "$pid"
    ;;

  stop)
    stop_scout
    if ! is_running; then
      cleanup_stale_pid
      printf 'ComicPile overnight factory is not running.\n'
      exit 0
    fi

    pid="$(read_pid)"
    printf 'Stopping ComicPile overnight factory process group %s...\n' "$pid"
    kill -TERM -- "-$pid"

    for _ in {1..20}; do
      if ! kill -0 -- "-$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        printf 'ComicPile overnight factory stopped.\n'
        exit 0
      fi
      sleep 1
    done

    printf 'Factory did not stop after 20 seconds; sending SIGKILL to the process group.\n' >&2
    kill -KILL -- "-$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    ;;

  status)
    if is_running; then
      printf 'ComicPile overnight factory is running (process group %s).\n' "$(read_pid)"
      printf 'Supervisor log: %s\n' "$SUPERVISOR_LOG"
      if is_scout_running; then
        printf 'Model scout is running (process group %s).\n' "$(read_scout_pid)"
      else
        printf 'Model scout is not running.\n'
      fi
      printf '\nConfirmed models:\n'
      bash "$SCRIPT_DIR/opencode-model-manifest.sh" summary "$STATE_DIR" 2>/dev/null || true
      exit 0
    fi

    cleanup_stale_pid
    printf 'ComicPile overnight factory is not running.\n'
    exit 1
    ;;

  run)
    start_scout
    trap 'stop_scout' EXIT
    export COMIC_PILE_FACTORY_WAIT_FOR_SCOUT=1
    export COMIC_PILE_FACTORY_SCOUT_READY_FILE="$STATE_DIR/scout-initial-pass.done"
    export COMIC_PILE_FACTORY_SCOUT_PID_FILE="$SCOUT_PID_FILE"
    run_factory "$@"
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    usage >&2
    die "unknown command: $command"
    ;;
esac
