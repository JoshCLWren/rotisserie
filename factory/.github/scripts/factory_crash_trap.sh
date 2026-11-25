# shellcheck shell=bash
# Source after `set -Eeuo pipefail`. A command that fails the script prints a
# GitHub ::error annotation (command, line, script); the exit status is kept.
# Only a job that holds the factory store also stores the crash, through the
# factory_crash.py beside this file. Model jobs never hold the store, so the
# trap never runs a checkout copy that a model could have edited.
FACTORY_CRASH_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

factory_crash_trap() {
  local status=$?
  ((BASH_SUBSHELL == 0)) || return "$status"
  local line="${BASH_LINENO[0]}" script="${BASH_SOURCE[1]:-$0}" function="${FUNCNAME[1]:-main}"
  local command="${BASH_COMMAND:0:300}" trace="" depth
  for ((depth = 1; depth < ${#FUNCNAME[@]}; depth++)); do
    trace+="  at ${FUNCNAME[$depth]} (${BASH_SOURCE[$depth]:-?}:${BASH_LINENO[$((depth - 1))]})"$'\n'
  done
  local escaped="${command//'%'/%25}"
  escaped="${escaped//$'\n'/%0A}"
  printf '::error file=%s,line=%s,title=Factory shell crash::%s exited %s in %s:%s %s\n' \
    "$script" "$line" "$escaped" "$status" "${script##*/}" "$line" "$function" >&2
  local recorder="$FACTORY_CRASH_SCRIPTS/factory_crash.py"
  if [[ -n "${FACTORY_TURSO_URL:-}${FACTORY_STATE_SQLITE:-}" && -f "$recorder" ]]; then
    python3 "$recorder" record-shell \
      --script "$script" --line "$line" --function "$function" --command "$command" \
      --status "$status" --trace "\`$command\` exited $status"$'\n'"$trace" >&2 || true
  fi
  return "$status"
}
trap factory_crash_trap ERR
