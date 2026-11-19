#!/usr/bin/env bash
set -Eeuo pipefail
# FACTORY_GH_SHIM_MARKER: factory gh shims must never be resolved as the real gh.

REAL_GH="${FACTORY_REAL_GH:-/usr/bin/gh}"
[[ -x "$REAL_GH" ]] || {
  echo "factory gh REST shim: real gh executable not found at ${REAL_GH}" >&2
  exit 127
}

# Attribute every invocation to this job's shared-quota usage report.
# Reads may use the optional read-only App token (separate quota bucket).
METER="${GITHUB_WORKSPACE:-.}/.github/scripts/factory-gh-meter.sh"
if [[ -f "$METER" ]]; then
  # shellcheck source=/dev/null
  source "$METER"
  factory_gh_meter_record "$@"
else
  # shellcheck disable=SC2329  # invoked below and copied via declare -f
  factory_gh_real() { "$REAL_GH" "$@"; }
  factory_gh_is_read() { return 1; }
fi

# Run-local read cache (dispatcher jobs only; FACTORY_DISCOVERY_DIR is the
# job's scheduling memory). One dispatcher pass re-ran the same reads from
# many controller processes: 66x `gh pr checks` and 26+26 paginated Entry/
# fork run listings in a single push-triggered pass (run 44097292205). Only
# these repetitive status reads are cached, for a short TTL, and ANY write
# made through this shim (labels, comments, merges, workflow dispatches)
# drops the whole cache so later reads in the job are live again.
eval "$(declare -f factory_gh_real | sed '1s/factory_gh_real/factory_gh_live/')"
FACTORY_GH_READ_CACHE="${FACTORY_DISCOVERY_DIR:+$FACTORY_DISCOVERY_DIR/gh-read-cache}"
factory_gh_cacheable() {
  [[ -n "$FACTORY_GH_READ_CACHE" ]] || return 1
  factory_gh_is_read "$@" || return 1
  [[ "${1:-} ${2:-}" == "pr checks" ]] && return 0
  [[ "${1:-}" == api && "$*" =~ actions/(workflows/[^/?\ ]+/runs|runs/[0-9]+/jobs) ]]
}
factory_gh_real() {
  local key file now age status=0
  if ! factory_gh_cacheable "$@"; then
    if [[ -n "$FACTORY_GH_READ_CACHE" ]] && ! factory_gh_is_read "$@"; then
      rm -rf "$FACTORY_GH_READ_CACHE"
    fi
    factory_gh_live "$@"
    return
  fi
  key="$(printf '%s\0' "$@" | sha256sum | cut -c1-40)"
  file="$FACTORY_GH_READ_CACHE/$key"
  now="$(date +%s)"
  if [[ -f "$file" ]]; then
    age=$(( now - $(stat -c %Y "$file") ))
    if (( age >= 0 && age < ${FACTORY_GH_READ_CACHE_TTL:-45} )); then
      # Usage reports subtract hits: the meter logged this invocation already.
      [[ -z "${FACTORY_GH_CALL_LOG:-}" ]] || printf '%s\tcache-hit\n' "$now" >> "$FACTORY_GH_CALL_LOG"
      cat "$file"
      return 0
    fi
  fi
  mkdir -p "$FACTORY_GH_READ_CACHE"
  factory_gh_live "$@" > "$file.$$" || status=$?
  cat "$file.$$"
  if (( status == 0 )); then
    mv "$file.$$" "$file"
  else
    rm -f "$file.$$"
  fi
  return "$status"
}

original=("$@")
command_group="${1:-}"
command_name="${2:-}"

# Run-local inventories are advisory scheduling memory. Generic REST reads,
# exact targets, ownership, checks and merges always reach GitHub directly.
FACTORY_DISCOVERY_HELPER="${GITHUB_WORKSPACE:-.}/.github/scripts/factory_discovery.py"

factory_api() {
  factory_gh_real api "$@"
}

factory_snapshot() {
  local repo="$1" kind="$2"
  [[ -n "${FACTORY_DISCOVERY_DIR:-}" && -f "$FACTORY_DISCOVERY_HELPER" ]] || return 1
  python3 "$FACTORY_DISCOVERY_HELPER" "$repo" "$kind" 2>/dev/null
}

if [[ "$command_group" == api ]]; then
  shift
  factory_api "$@"
  exit $?
fi


emit_with_optional_jq() {
  local payload="$1" jq_expr="${2:-}"
  if [[ -n "$jq_expr" ]]; then
    jq -r "$jq_expr" <<< "$payload"
  else
    printf '%s\n' "$payload"
  fi
}

# shellcheck disable=SC2034  # namerefs write the caller's variables
parse_list_args() {
  local -n out_repo=$1 out_state=$2 out_limit=$3 out_jq=$4 out_labels=$5
  shift 5
  out_repo="${GITHUB_REPOSITORY:-}"
  out_state='open'
  out_limit=100
  out_jq=''
  out_labels=()
  while (( $# )); do
    case "$1" in
      --repo)
        out_repo="$2"; shift 2 ;;
      --state)
        out_state="$2"; shift 2 ;;
      --limit)
        out_limit="$2"; shift 2 ;;
      --label)
        out_labels+=("$2"); shift 2 ;;
      --json)
        shift 2 ;;
      --jq)
        out_jq="$2"; shift 2 ;;
      *)
        return 2 ;;
    esac
  done
  [[ -n "$out_repo" && "$out_limit" =~ ^[0-9]+$ ]] || return 2
}

if [[ "$command_group" == issue && "$command_name" == list ]]; then
  shift 2
  repo='' state='' limit='' jq_expr=''
  labels=()
  if ! parse_list_args repo state limit jq_expr labels "$@"; then
    factory_gh_real "${original[@]}"; exit $?
  fi
  # Let GitHub narrow worker-owned issue queries server-side. Otherwise each
  # worker re-fetches every page of the entire issue backlog to find one lease.
  endpoint="repos/${repo}/issues?state=${state}&per_page=100"
  if (( ${#labels[@]} > 0 )); then
    label_list="$(IFS=,; printf '%s' "${labels[*]}")"
    endpoint+="&labels=$(jq -rn --arg labels "$label_list" '$labels | @uri')"
  fi
  snapshot=''
  if [[ "$state" == open ]] && ! [[ " ${original[*]} " == *"author"* ]]; then
    snapshot="$(factory_snapshot "$repo" issue)" || snapshot=''
  fi
  if [[ -n "$snapshot" ]]; then
    pages="$(jq -c '[.]' <<< "$snapshot")"
  else
    pages="$(factory_api --paginate --slurp "$endpoint")"
  fi
  labels_json="$(printf '%s\n' "${labels[@]:-}" | jq -Rsc 'split("\n") | map(select(length > 0))')"
  payload="$(jq -c --argjson labels "$labels_json" --argjson limit "$limit" '
    [
      .[][]
      | select(.pull_request == null)
      | select([.labels[]?.name] as $item_labels
          | ($labels | all(. as $wanted | ($item_labels | index($wanted)) != null)))
      | {
          number,
          title,
          body,
          labels,
          createdAt: (.createdAt // .created_at),
          updatedAt: (.updatedAt // .updated_at),
          state: (if .state == "open" then "OPEN" else "CLOSED" end)
        }
    ][: $limit]
  ' <<< "$pages")"
  emit_with_optional_jq "$payload" "$jq_expr"
  exit 0
fi

if [[ "$command_group" == pr && "$command_name" == list ]]; then
  shift 2
  repo='' state='' limit='' jq_expr=''
  labels=()
  if ! parse_list_args repo state limit jq_expr labels "$@"; then
    factory_gh_real "${original[@]}"; exit $?
  fi
  snapshot=''
  if [[ "$state" == open ]] && ! [[ " ${original[*]} " == *"author"* ]]; then
    snapshot="$(factory_snapshot "$repo" pr)" || snapshot=''
  fi
  if [[ -n "$snapshot" ]]; then
    pages="$(jq -c '[.]' <<< "$snapshot")"
  else
    pages="$(factory_api --paginate --slurp "repos/${repo}/pulls?state=${state}&per_page=100")"
  fi
  labels_json="$(printf '%s\n' "${labels[@]:-}" | jq -Rsc 'split("\n") | map(select(length > 0))')"
  payload="$(jq -c --argjson labels "$labels_json" --argjson limit "$limit" '
    [
      .[][]
      | select([.labels[]?.name] as $item_labels
          | ($labels | all(. as $wanted | ($item_labels | index($wanted)) != null)))
      | {
          number,
          title,
          body,
          labels,
          headRefName: (.headRefName // .head.ref),
          headRefOid: (.headRefOid // .head.sha),
          # Preserve reconciliation fields already present in projected
          # canonical PRs, or available from the live REST fallback. The
          # omission previously turned real MERGEABLE/CLEAN into null/UNKNOWN
          # every time the CLI adapter fed a full-inventory refresh.
          mergeable: (if .mergeable == true then "MERGEABLE" elif .mergeable == false then "CONFLICTING" else .mergeable end),
          mergeStateStatus: ((.mergeStateStatus // .mergeable_state // "UNKNOWN") | ascii_upcase),
          author: {login: (.author.login // .user.login)},
          isCrossRepository: (.isCrossRepository // (.head.repo.full_name != .base.repo.full_name)),
          createdAt: (.createdAt // .created_at),
          updatedAt: (.updatedAt // .updated_at),
          isDraft: (.isDraft // .draft // false),
          state: (if .state == "open" then "OPEN" else "CLOSED" end)
        }
    ][: $limit]
  ' <<< "$pages")"
  emit_with_optional_jq "$payload" "$jq_expr"
  exit 0
fi

# shellcheck disable=SC2034  # namerefs write the caller's variables
parse_view_args() {
  local -n out_number=$1 out_repo=$2 out_jq=$3
  shift 3
  out_number="${1:-}"
  shift || true
  out_repo="${GITHUB_REPOSITORY:-}"
  out_jq=''
  [[ "$out_number" =~ ^[0-9]+$ ]] || return 2
  while (( $# )); do
    case "$1" in
      --repo)
        out_repo="$2"; shift 2 ;;
      --json)
        shift 2 ;;
      --jq)
        out_jq="$2"; shift 2 ;;
      *)
        return 2 ;;
    esac
  done
  [[ -n "$out_repo" ]] || return 2
}

if [[ "$command_group" == pr && "$command_name" == view ]]; then
  shift 2
  number='' repo='' jq_expr=''
  if ! parse_view_args number repo jq_expr "$@"; then
    factory_gh_real "${original[@]}"; exit $?
  fi
  raw="$(factory_gh_real api "repos/${repo}/pulls/${number}")"
  payload="$(jq -c '
    {
      number,
      title,
      body,
      labels,
      state: (if .merged_at != null then "MERGED" elif .state == "open" then "OPEN" else "CLOSED" end),
      isDraft: (.draft // false),
      mergeable: (if .mergeable == true then "MERGEABLE" elif .mergeable == false then "CONFLICTING" else "UNKNOWN" end),
      headRefOid: .head.sha,
      author: {login: .user.login},
      isCrossRepository: (.head.repo.full_name != .base.repo.full_name),
      headRefName: .head.ref
    }
  ' <<< "$raw")"
  emit_with_optional_jq "$payload" "$jq_expr"
  exit 0
fi

if [[ "$command_group" == issue && "$command_name" == view ]]; then
  shift 2
  number='' repo='' jq_expr=''
  if ! parse_view_args number repo jq_expr "$@"; then
    factory_gh_real "${original[@]}"; exit $?
  fi
  raw="$(factory_gh_real api "repos/${repo}/issues/${number}")"
  payload="$(jq -c '
    {
      number,
      title,
      body,
      labels,
      state: (if .state == "open" then "OPEN" else "CLOSED" end)
    }
  ' <<< "$raw")"
  emit_with_optional_jq "$payload" "$jq_expr"
  exit 0
fi

# `gh pr checks --required` exits 1 both when required checks fail and when
# GitHub reports that a PR simply has no checks. The latter is not a failed
# gate when the base branch is explicitly unprotected. Normalize only that
# narrow case so the controller and dispatcher share the same fail-closed
# interpretation without weakening protected-branch behavior.
if [[ "$command_group" == pr && "$command_name" == checks ]]; then
  set +e
  checks_output="$(factory_gh_real "${original[@]}" 2>&1)"
  checks_status=$?
  set -e

  if (( checks_status == 0 )); then
    [[ -z "$checks_output" ]] || printf '%s\n' "$checks_output"
    exit 0
  fi

  is_required=0
  repo="${GITHUB_REPOSITORY:-}"
  number=''
  for (( index=2; index < ${#original[@]}; index++ )); do
    arg="${original[$index]}"
    case "$arg" in
      --required)
        is_required=1 ;;
      --repo)
        if (( index + 1 < ${#original[@]} )); then
          repo="${original[$((index + 1))]}"
          index=$((index + 1))
        fi
        ;;
      *)
        if [[ -z "$number" && "$arg" =~ ^[0-9]+$ ]]; then
          number="$arg"
        fi
        ;;
    esac
  done

  no_checks=0
  if grep -Eq '^no (required )?checks reported on the .+ branch$' <<< "$checks_output"; then
    no_checks=1
  fi

  if (( is_required == 1 && no_checks == 1 )) && [[ -n "$repo" && -n "$number" ]]; then
    if pr_raw="$(factory_gh_real api "repos/${repo}/pulls/${number}" 2>/dev/null)"; then
      base_ref="$(jq -r '.base.ref // empty' <<< "$pr_raw")"
      if [[ -n "$base_ref" ]]; then
        encoded_base="$(jq -rn --arg value "$base_ref" '$value | @uri')"
        if branch_raw="$(factory_gh_real api "repos/${repo}/branches/${encoded_base}" 2>/dev/null)"; then
          protected="$(jq -r 'if has("protected") then .protected else true end' <<< "$branch_raw")"
          if [[ "$protected" == false ]]; then
            echo "factory gh REST shim: no required checks configured on unprotected base ${base_ref}; treating required-check gate as satisfied" >&2
            exit 0
          fi
        fi
      fi
    fi
  fi

  [[ -z "$checks_output" ]] || printf '%s\n' "$checks_output" >&2
  exit "$checks_status"
fi

factory_gh_real "${original[@]}"; exit $?
