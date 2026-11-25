# Source after `set -Eeuo pipefail`. A command that fails the script prints a
# GitHub ::error annotation (command, line, script) and stores one shell crash
# through factory_crash.py when this job holds the store. The exit status is kept.
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
  python3 "${FACTORY_CRASH_SCRIPTS:-$(dirname "${BASH_SOURCE[0]}")}/factory_crash.py" record-shell \
    --script "$script" --line "$line" --function "$function" --command "$command" \
    --status "$status" --trace "\`$command\` exited $status"$'\n'"$trace" >&2 || true
  return "$status"
}
trap factory_crash_trap ERR
