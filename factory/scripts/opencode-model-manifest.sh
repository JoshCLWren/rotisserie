#!/usr/bin/env bash
# OpenCode model manifest helper.
#
# Manifest layout ($STATE_DIR/model_manifest.tsv):
#   model_id<TAB>status<TAB>tool_support<TAB>availability<TAB>last_probed<TAB>use_count

set -Eeuo pipefail

default_state_dir() {
  local source_repo="${COMIC_PILE_REPO:-}"
  if [[ -n "$source_repo" ]]; then
    printf '%s-factory-state\n' "${source_repo%/}"
  else
    printf '%s\n' "${COMIC_PILE_FACTORY_STATE_DIR:-/tmp/comic-pile-factory-state}"
  fi
}

state_dir_for() {
  if [[ $# -ge 1 && -n "$1" ]]; then
    printf '%s\n' "$1"
  else
    printf '%s\n' "${COMIC_PILE_FACTORY_STATE_DIR:-$(default_state_dir)}"
  fi
}

manifest_path() {
  printf '%s/model_manifest.tsv\n' "$1"
}

manifest_init() {
  local dir="$1"
  mkdir -p "$dir"
  local file
  file="$(manifest_path "$dir")"
  if [[ ! -f "$file" ]]; then
    printf 'model_id\tstatus\ttool_support\tavailability\tlast_probed\tuse_count\n' >"$file"
  elif ! head -1 "$file" | grep -Fq $'\tavailability\t'; then
    local migrated
    migrated="$(mktemp "$dir/.manifest-migrate.XXXXXX")"
    awk -F'\t' 'BEGIN{OFS="\t"}
      NR==1{print "model_id", "status", "tool_support", "availability", "last_probed", "use_count"; next}
      {print $1, $2, ($3==""?"unknown":$3), "yes", ($4==""?0:$4), ($5==""?0:$5)}' \
      "$file" >"$migrated"
    mv "$migrated" "$file"
  fi
}

manifest_lock() {
  local dir="$1"
  exec 9>"$dir/model_manifest.lock"
  flock -x 9
}

manifest_unlock() {
  flock -u 9 2>/dev/null || true
}

manifest_set() {
  local model="$1" status="$2" tool="$3" dir="$4"
  manifest_init "$dir"
  local file tmp
  file="$(manifest_path "$dir")"
  manifest_lock "$dir"
  tmp="$(mktemp "$dir/.manifest.XXXXXX")"
  awk -F'\t' -v model="$model" -v status="$status" -v tool="$tool" -v now="$(date +%s)" \
    'BEGIN{OFS="\t"}
     NR==1{print; next}
     $1==model{print model, status, tool, ($4==""?"yes":$4), now, ($6==""?0:$6); found=1; next}
     {print}
     END{if(!found) print model, status, tool, "yes", now, 0}' \
    "$file" >"$tmp"
  mv "$tmp" "$file"
  manifest_unlock
}

manifest_record() {
  local model="$1" dir="$2"
  manifest_init "$dir"
  local file tmp
  file="$(manifest_path "$dir")"
  manifest_lock "$dir"
  tmp="$(mktemp "$dir/.manifest.XXXXXX")"
  awk -F'\t' -v model="$model" -v now="$(date +%s)" \
    'BEGIN{OFS="\t"}
     NR==1{print; next}
     $1==model{print model, "confirmed", ($3==""?"unknown":$3), ($4==""?"yes":$4), now, ($6==""?1:$6+1); found=1; next}
     {print}
     END{if(!found) print model, "confirmed", "unknown", "yes", now, 1}' \
    "$file" >"$tmp"
  mv "$tmp" "$file"
  manifest_unlock
}

manifest_failure() {
  local model="$1" dir="$2"
  manifest_init "$dir"
  local file tmp
  file="$(manifest_path "$dir")"
  manifest_lock "$dir"
  tmp="$(mktemp "$dir/.manifest.XXXXXX")"
  awk -F'\t' -v model="$model" -v now="$(date +%s)" \
    'BEGIN{OFS="\t"}
     NR==1{print; next}
     $1==model{print model, "failed", ($3==""?"unknown":$3), ($4==""?"yes":$4), now, ($6==""?0:$6); found=1; next}
     {print}
     END{if(!found) print model, "failed", "unknown", "yes", now, 0}' \
    "$file" >"$tmp"
  mv "$tmp" "$file"
  manifest_unlock
}

manifest_availability() {
  local model="$1" availability="$2" dir="$3"
  [[ "$availability" == "yes" || "$availability" == "no" ]] || return 2
  manifest_init "$dir"
  local file tmp
  file="$(manifest_path "$dir")"
  manifest_lock "$dir"
  tmp="$(mktemp "$dir/.manifest.XXXXXX")"
  awk -F'\t' -v model="$model" -v availability="$availability" \
    'BEGIN{OFS="\t"}
     NR==1{print; next}
     $1==model{$4=availability; print; found=1; next}
     {print}
     END{if(!found) print model, "untested", "unknown", availability, 0, 0}' \
    "$file" >"$tmp"
  mv "$tmp" "$file"
  manifest_unlock
}

manifest_confirmed() {
  local dir="$1"
  manifest_init "$dir"
  awk -F'\t' 'NR>1 && $2=="confirmed" && $1!="" {print $1}' "$(manifest_path "$dir")"
}

manifest_pending() {
  local dir="$1" cooldown="${2:-0}"
  manifest_init "$dir"
  awk -F'\t' -v now="$(date +%s)" -v cooldown="$cooldown" \
    'NR>1 && $1!="" && ($2=="untested" || ($2=="failed" && (cooldown==0 || now-$5>=cooldown))) {print $1}' \
    "$(manifest_path "$dir")"
}

# Return confirmed models in least-used order while rotating ties/order between calls.
# The cursor read and update are protected by the same lock as manifest writes so
# concurrent factory processes cannot all select the same position.
manifest_next() {
  local default="$1" dir="$2"
  manifest_init "$dir"
  local file cursor_file rows cursor count n selected
  file="$(manifest_path "$dir")"
  cursor_file="$dir/model_cursor"

  manifest_lock "$dir"
  rows="$(awk -F'\t' 'NR>1 && $2=="confirmed" && $4!="no" && $1!="" {print}' "$file" \
    | sort -t$'\t' -k6,6n -k1,1)"
  if [[ -z "$rows" ]]; then
    manifest_unlock
    printf '%s\n' "$default"
    return 0
  fi

  cursor=0
  [[ -f "$cursor_file" ]] && cursor="$(cat "$cursor_file" 2>/dev/null || printf 0)"
  [[ "$cursor" =~ ^[0-9]+$ ]] || cursor=0
  count="$(printf '%s\n' "$rows" | wc -l | tr -d ' ')"
  n=$((cursor % count + 1))
  selected="$(printf '%s\n' "$rows" | sed -n "${n}p" | cut -f1)"
  printf '%s\n' "$(((cursor + 1) % count))" >"$cursor_file"
  manifest_unlock
  printf '%s\n' "$selected"
}

manifest_summary() {
  local dir="$1"
  manifest_init "$dir"
  local file
  file="$(manifest_path "$dir")"
  printf '%-45s %-10s %-8s %-12s %-12s %s\n' MODEL STATUS TOOLS AVAILABILITY PROBED USES
  if command -v column >/dev/null 2>&1; then
    tail -n +2 "$file" | column -t -s $'\t'
  else
    tail -n +2 "$file"
  fi
}

main() {
  local cmd="${1:-}"
  [[ -n "$cmd" ]] || { printf 'usage: %s <init|set|record|confirmed|next|pending|summary> [...]\n' "$0" >&2; exit 2; }
  shift
  case "$cmd" in
    init)
      manifest_init "$(state_dir_for "${1:-}")"
      ;;
    set)
      (($# >= 3)) || { printf 'usage: %s set MODEL STATUS TOOL [STATE_DIR]\n' "$0" >&2; exit 2; }
      local set_model="$1" set_status="$2" set_tool="$3" set_dir
      shift 3
      set_dir="$(state_dir_for "${1:-}")"
      manifest_set "$set_model" "$set_status" "$set_tool" "$set_dir"
      ;;
    fail)
      (($# >= 1)) || { printf 'usage: %s fail MODEL [STATE_DIR]\n' "$0" >&2; exit 2; }
      local fail_model="$1" fail_dir
      shift
      fail_dir="$(state_dir_for "${1:-}")"
      manifest_failure "$fail_model" "$fail_dir"
      ;;
    availability)
      (($# >= 2)) || { printf 'usage: %s availability MODEL yes|no [STATE_DIR]\n' "$0" >&2; exit 2; }
      local availability_model="$1" availability_value="$2" availability_dir
      shift 2
      availability_dir="$(state_dir_for "${1:-}")"
      manifest_availability "$availability_model" "$availability_value" "$availability_dir"
      ;;
    record)
      (($# >= 1)) || { printf 'usage: %s record MODEL [STATE_DIR]\n' "$0" >&2; exit 2; }
      local record_model="$1" record_dir
      shift
      record_dir="$(state_dir_for "${1:-}")"
      manifest_record "$record_model" "$record_dir"
      ;;
    confirmed)
      manifest_confirmed "$(state_dir_for "${1:-}")"
      ;;
    pending)
      pending_dir="$(state_dir_for "${1:-}")"
      pending_cooldown="${2:-0}"
      manifest_pending "$pending_dir" "$pending_cooldown"
      ;;
    next)
      (($# >= 1)) || { printf 'usage: %s next DEFAULT_MODEL [STATE_DIR]\n' "$0" >&2; exit 2; }
      local next_default="$1" next_dir
      shift
      next_dir="$(state_dir_for "${1:-}")"
      manifest_next "$next_default" "$next_dir"
      ;;
    summary)
      manifest_summary "$(state_dir_for "${1:-}")"
      ;;
    *)
      printf 'unknown command: %s\n' "$cmd" >&2
      exit 2
      ;;
  esac
}

main "$@"
