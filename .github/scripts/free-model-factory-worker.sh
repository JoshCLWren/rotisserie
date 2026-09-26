#!/usr/bin/env bash
set -Eeuo pipefail

# Preserve the worker's public fail-fast contract before loading shared
# primitives. Guard tests intentionally exercise these checks in isolation.
: "${FACTORY_WORKER:?FACTORY_WORKER is required}"
: "${FACTORY_SOURCE:?FACTORY_SOURCE is required}"
: "${FACTORY_MODEL:?FACTORY_MODEL is required}"
: "${FACTORY_RUNTIME_MODEL:?FACTORY_RUNTIME_MODEL is required}"

# Factory selection and lease handoff must keep working even when GitHub's
# GraphQL installation bucket is exhausted. Route the small set of gh list/view
# reads used by the wrapper through REST while forwarding every other gh command
# to the real CLI unchanged.
install_factory_rest_gh() {
  local real_gh shim_dir
  real_gh="$(command -v gh)"
  shim_dir="$(mktemp -d /tmp/comic-pile-factory-gh.XXXXXX)"
  cp .github/scripts/factory-gh-rest-shim.sh "$shim_dir/gh"
  chmod +x "$shim_dir/gh"
  export FACTORY_REAL_GH="$real_gh"
  export PATH="$shim_dir:$PATH"
}
install_factory_rest_gh

# These four tiny bridges preserve the existing regression harness, which
# extracts named helper functions directly from this file. In the real worker
# they are immediately replaced when the tracked primitives are sourced below.
stage_trusted_guard() {
  local primitives definition
  primitives="$(dirname "${WORKER:-${BASH_SOURCE[0]}}")/free-model-factory-worker-primitives.sh"
  [[ -f "$primitives" ]] || primitives="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/free-model-factory-worker-primitives.sh"
  definition="$(sed -n '/^stage_trusted_guard() {/,/^}/p' "$primitives")"
  unset -f stage_trusted_guard
  eval "$definition"
  stage_trusted_guard "$@"
}

conflict_markers_present() {
  local primitives definition
  primitives="$(dirname "${WORKER:-${BASH_SOURCE[0]}}")/free-model-factory-worker-primitives.sh"
  [[ -f "$primitives" ]] || primitives="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/free-model-factory-worker-primitives.sh"
  definition="$(sed -n '/^conflict_markers_present() {/,/^}/p' "$primitives")"
  unset -f conflict_markers_present
  eval "$definition"
  conflict_markers_present "$@"
}

branch_folded_main_commits() {
  local primitives definition
  primitives="$(dirname "${WORKER:-${BASH_SOURCE[0]}}")/free-model-factory-worker-primitives.sh"
  [[ -f "$primitives" ]] || primitives="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/free-model-factory-worker-primitives.sh"
  definition="$(sed -n '/^branch_folded_main_commits() {/,/^}/p' "$primitives")"
  unset -f branch_folded_main_commits
  eval "$definition"
  branch_folded_main_commits "$@"
}

unclean_git_state_json() {
  local primitives definition
  primitives="$(dirname "${WORKER:-${BASH_SOURCE[0]}}")/free-model-factory-worker-primitives.sh"
  [[ -f "$primitives" ]] || primitives="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/free-model-factory-worker-primitives.sh"
  definition="$(sed -n '/^unclean_git_state_json() {/,/^}/p' "$primitives")"
  unset -f unclean_git_state_json
  eval "$definition"
  unclean_git_state_json "$@"
}

# Reuse the proven persistence/guard/provider/lease primitives as a tracked
# repository file, stopping before its legacy main loop.
# NOTE: This cut is intentional. The live wrapper imports declarations only and
# drops the primitives top-level loop starting at the exact ensure_owner_label
# sentinel. Shared definitions required here must stay above that sentinel;
# changes below it are not inherited by this worker.
source <(sed '/^ensure_owner_label$/,$d' .github/scripts/free-model-factory-worker-primitives.sh)
source .github/scripts/factory-semantic-verdict.sh

TERMINAL_OUTCOME_FILE="${RUNNER_TEMP:-/tmp}/factory-discovery-outcome"

# Each GitHub runner may execute the same durable worker repeatedly. Clear the
# worker-scoped logs before leasing work so a prior model/provider failure
# cannot be misclassified as the outcome of this attempt.
worker_log="/tmp/opencode-factory-${WORKER}.log"
: > "$worker_log"
rm -f \
  "/tmp/opencode-factory-${WORKER}.sanitized.log" \
  "/tmp/opencode-factory-${WORKER}-verdict-recovery.log"

# INCIDENT 2026-09-06: OmniRoute stays dark unless FACTORY_OMNIROUTE_ENABLED is on.
# Honor the kill switch here so one-var re-enable works, and install the lease-release
# trap before exiting so a dispatcher-owned omniroute-free lease is not stranded.
if [[ "${FACTORY_SOURCE}" == 'omniroute-free' ]]; then
  case "$(printf '%s' "${FACTORY_OMNIROUTE_ENABLED:-off}" | tr '[:upper:]' '[:lower:]')" in
    1|on|true|yes) ;;
    *)
      printf 'OmniRoute Entry is disabled for this incident; refusing source %s\n' "${FACTORY_SOURCE}" >&2
      trap 'release_owned_targets omniroute-disabled-incident || true' EXIT
      exit 2
      ;;
  esac
fi

record_terminal_outcome() {
  local outcome="$1" detail="$2"
  case "$outcome" in
    success|no_work|work_failure|provider_failure|provider_throttle|model_unavailable|model_retired_410|model_policy_violation|environment_failure|control_plane_failure|unknown_failure) ;;
    *)
      echo "unsupported terminal factory outcome: ${outcome}" >&2
      return 2
      ;;
  esac
  detail="${detail//$'\n'/ }"
  detail="${detail//$'\t'/ }"
  printf '%s\t%s\n' "$outcome" "$detail" > "$TERMINAL_OUTCOME_FILE"
}

record_agent_failure_outcome() {
  local status="$1" log_file="/tmp/opencode-factory-${WORKER}.log"
  if [[ -f "$log_file" ]] && grep -Eqi '429|too many requests|rate.?limit|quota|throttl|capacity' "$log_file"; then
    record_terminal_outcome provider_throttle "pinned provider/model session was throttled (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'HTTP[^0-9]*410|410 Gone' "$log_file"; then
    record_terminal_outcome model_retired_410 "pinned model has been permanently retired by the provider (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'model[^[:alnum:]]+(not found|unavailable|does not exist)|unknown model|invalid model|HTTP[^0-9]*404|404 Not Found' "$log_file"; then
    record_terminal_outcome model_unavailable "pinned model became unavailable during execution (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'model[^\n]*(policy|guard)[^\n]*(blocked|rejected|denied)|model[^\n]*(blocked|rejected|denied)[^\n]*(policy|guard)' "$log_file"; then
    record_terminal_outcome model_policy_violation "pinned model was rejected by provider/model policy (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'checkout failed|dependency install failed|disk full|no space left|docker daemon|runner environment|tool installation failed' "$log_file"; then
    record_terminal_outcome environment_failure "worker environment failed during assigned execution (agent exit ${status})"
  elif (( status == 124 || status == 137 || status == 143 )); then
    record_terminal_outcome provider_failure "pinned provider/model session timed out or was interrupted after smoke succeeded (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'provider[^\n]*(error|unavailable|failed)|service unavailable|bad gateway|gateway timeout|stream[^\n]*(timeout|readiness)|STREAM_READINESS_TIMEOUT|HTTP[^0-9]*(502|503|504)|ECONNRESET|ETIMEDOUT|connection reset|upstream[^\n]*(error|failed)' "$log_file"; then
    record_terminal_outcome provider_failure "pinned provider/model execution failed after smoke succeeded (agent exit ${status})"
  else
    record_terminal_outcome unknown_failure "agent exited ${status} without enough evidence for a narrower failure class"
  fi
}


# A PR branch may predate the Kilo integration entirely. Stage the backend
# runner from trusted main before any checkout so cross-worker takeover never
# executes a missing or stale branch copy, mirroring the trusted guard model.
stage_trusted_kilo_helper() {
  [[ "$SOURCE" == 'kilo-auto' ]] || return 0
  TRUSTED_KILO_HELPER="${TRUSTED_KILO_HELPER:-}"
  if [[ -z "$TRUSTED_KILO_HELPER" ]]; then
    TRUSTED_KILO_HELPER="$(mktemp /tmp/comic-pile-kilo-auto-run.XXXXXX.sh)"
  fi
  cp .github/scripts/kilo-auto-factory-run.sh "$TRUSTED_KILO_HELPER"
  chmod +x "$TRUSTED_KILO_HELPER"
}

# Semantic state transitions are more privileged than the reviewed branch.
# Copy the controller and its pure policy module from trusted main before any
# checkout_target switch so a stale or contaminated PR cannot replace the
# authority code that interprets its model verdict.
stage_trusted_review_controller() {
  local trusted_dir
  trusted_dir="$(mktemp -d /tmp/comic-pile-review-controller.XXXXXX)"
  cp .github/scripts/factory-review-controller.py "$trusted_dir/factory-review-controller.py"
  cp .github/scripts/factory_review_policy.py "$trusted_dir/factory_review_policy.py"
  chmod +x "$trusted_dir/factory-review-controller.py"
  TRUSTED_REVIEW_CONTROLLER="$trusted_dir/factory-review-controller.py"
  export TRUSTED_REVIEW_CONTROLLER
}

# Keep the established OpenCode/NVIDIA implementation untouched. Kilo needs a
# small override only so it invokes the trusted helper staged from main.
eval "$(declare -f run_agent | sed '1s/^run_agent /legacy_run_agent /')"
run_agent() {
  local mode="$1" number="$2" timeout_seconds="$3"
  local target mission prompt status=0
  if [[ "$SOURCE" != 'kilo-auto' ]]; then
    legacy_run_agent "$@"
    return $?
  fi

  if [[ "$mode" == 'pr' ]]; then
    target="pull request #${number}"
    mission="Resume this PR. Inspect the exact current head, required CI, review submissions, and every inline review thread. Fix closure-critical defects and resolve or concretely rebut actionable threads. If no edits are required, decide whether the PR fully completes its declared scope and is safe to merge. End your final response with FACTORY_GATE_READY only for semantic approval, FACTORY_GATE_REJECT only when the PR is clearly unsalvageable, contaminated, obsolete, duplicate, or fundamentally incomplete, otherwise end with FACTORY_GATE_NOT_READY."
  else
    target="issue #${number}"
    mission="Implement the full closure-critical acceptance contract for this issue with code and focused tests. Do not stop at planning or optional polish."
  fi

  prompt="You are external-model Factory ${WORKER} for JoshCLWren/comic-pile. Durable worker ID: ${WORKER_ID}. Source: ${SOURCE}. Requested model or route: ${MODEL}. Runtime selector: ${RUNTIME_MODEL}. Assigned target: ${target}. Read AGENTS.md, docs/ISSUE_EXECUTION_PROTOCOL.md, docs/AUTONOMOUS_FACTORY_POLICY.md, docs/CHATGPT_FACTORY_PROMPT.md, and docs/FACTORY_GITHUB_VISIBILITY.md first. Follow the canonical product-first factory policy. ${mission} Work only on the assigned target during this agent invocation. Edit the checked-out branch, run focused validation, and use gh/GitHub when needed for review context. Do not commit or push; the wrapper persists changes. Do not merge or close the assigned pull request; the trusted wrapper and review controller own the final lifecycle transition after your terminal verdict. Do not switch models, providers, or routes. A provider failure is a result for this lane, not permission to fall back to another paid or unrequested route. Do not enable auto-merge, push main, touch production databases, or alter automation schedules."

  bash "$TRUSTED_KILO_HELPER" \
    "$timeout_seconds" \
    "ComicPile Factory ${WORKER} · ${DISPLAY}" \
    "$prompt" \
    "/tmp/opencode-factory-${WORKER}.log" || status=$?
  return "$status"
}

select_controller_assignment() {
  local -a prs=() issues=()
  local pr_json issue_json pr_numbers issue_numbers canonical_pr_json canonical_pr
  local pr branch linked_issue issue pr_stage

  if ! pr_json="$(gh pr list --state open --limit 200 --label "$OWNER" --json number,labels)"; then
    log "unable to query controller-leased PRs for ${OWNER}"
    return 3
  fi
  if ! pr_numbers="$(jq -r '.[] | select(([.labels[].name] | index("factory:ready")) == null) | .number' <<< "$pr_json")"; then
    log "unable to parse controller-leased PRs for ${OWNER}"
    return 3
  fi
  mapfile -t prs < <(printf '%s' "$pr_numbers")

  if (( ${#prs[@]} > 1 )); then
    log "controller invariant failed: ${OWNER} owns multiple open PRs (${prs[*]})"
    return 2
  fi

  if ! issue_json="$(gh issue list --state open --limit 300 --label "$OWNER" --json number)"; then
    log "unable to query controller-leased issues for ${OWNER}"
    return 3
  fi
  if ! issue_numbers="$(jq -r '.[].number' <<< "$issue_json")"; then
    log "unable to parse controller-leased issues for ${OWNER}"
    return 3
  fi
  mapfile -t issues < <(printf '%s' "$issue_numbers")

  if (( ${#prs[@]} == 1 )); then
    pr="${prs[0]}"
    if ! branch="$(gh pr view "$pr" --json headRefName --jq .headRefName)"; then
      log "unable to resolve branch for controller-leased PR #${pr}"
      return 3
    fi
    if [[ -z "$branch" ]]; then
      log "controller-leased PR #${pr} returned an empty branch"
      return 3
    fi
    linked_issue="$(linked_issue_from_branch "$branch")"
    pr_stage="$(jq -r --argjson pr "$pr" '.[] | select(.number == $pr) | [.labels[].name | select(. == "factory:building" or . == "factory:review" or . == "factory:changes-requested" or . == "factory:conflict" or . == "factory:ci")] | first // empty' <<< "$pr_json")"
    if [[ -z "$pr_stage" ]]; then
      log "controller-leased PR #${pr} has no supported completion stage"
      return 3
    fi

    for issue in "${issues[@]:-}"; do
      [[ -n "$issue" ]] || continue
      if [[ -z "$linked_issue" || "$issue" != "$linked_issue" ]]; then
        log "controller invariant failed: ${OWNER} owns PR #${pr} plus unrelated issue #${issue}"
        return 2
      fi
    done

    MODE='pr'
    NUMBER="$pr"
    BRANCH="$branch"
    ASSIGNED_PR_STAGE="$pr_stage"
    return 0
  fi

  if (( ${#issues[@]} == 1 )); then
    issue="${issues[0]}"
    if ! canonical_pr_json="$(gh pr list --state open --limit 500 --json number,headRefName)"; then
      log "unable to verify canonical PR identity for controller-leased issue #${issue}"
      return 3
    fi
    if ! canonical_pr="$(jq -r --arg issue "$issue" '[.[] | select((.headRefName // "") | test("^factory/[0-9]+-" + $issue + "-")) | .number] | first // empty' <<< "$canonical_pr_json")"; then
      log "unable to parse canonical PR identity for controller-leased issue #${issue}"
      return 3
    fi
    if [[ -n "$canonical_pr" ]]; then
      log "controller invariant failed: ${OWNER} owns orphaned issue lease #${issue} while canonical open PR #${canonical_pr} exists"
      return 2
    fi

    MODE='issue'
    NUMBER="$issue"
    BRANCH="factory/${WORKER}-${NUMBER}-${BRANCH_SUFFIX}"
    return 0
  fi

  if (( ${#issues[@]} > 1 )); then
    log "controller invariant failed: ${OWNER} owns multiple open issues (${issues[*]})"
    return 2
  fi

  return 1
}

ensure_owner_label
stage_trusted_guard
stage_trusted_kilo_helper
stage_trusted_review_controller
trap 'release_owned_targets session-end-handoff || true' EXIT

MODE=''
NUMBER=''
BRANCH=''
ASSIGNED_PR_STAGE=''

set +e
select_controller_assignment
assignment_status=$?
set -e

if (( assignment_status == 1 )); then
  log 'no control-plane assignment is leased to this worker; exiting without repo-wide selection'
  record_terminal_outcome no_work 'no control-plane assignment was leased to this worker'
  exit 0
fi

if (( assignment_status != 0 )); then
  # Exit 2 and 3 are reserved for controller invariant/read failures. The
  # trusted workflow wrapper persists this source result after the process exits.
  release_owned_targets 'controller-assignment-read-failed' || true
  exit "$assignment_status"
fi

log "executing control-plane assignment: ${MODE} #${NUMBER}; runtime ${RUNTIME_MODEL}; budget ${BUDGET_SECONDS}s"
checkout_target "$MODE" "$NUMBER" "$BRANCH"

available="$(remaining)"
agent_timeout=$((available - 240))
(( agent_timeout > 3000 )) && agent_timeout=3000
if (( agent_timeout < 300 )); then
  log 'insufficient remaining budget for assigned work'
  record_terminal_outcome environment_failure 'insufficient remaining runner budget for assigned work'
  exit 1
fi

agent_attempt=1
agent_status=0
transient_failure=0
while :; do
  set +e
  run_agent "$MODE" "$NUMBER" "$agent_timeout"
  agent_status=$?
  set -e
  log "agent exit status ${agent_status} for ${MODE} #${NUMBER}"

  if (( agent_status == 0 )); then
    transient_failure=0
    break
  fi
  if ! is_transient_agent_failure "$agent_status"; then
    transient_failure=0
    break
  fi

  transient_failure=1
  log 'transient provider/runtime interruption on the pinned model; refusing to switch models'
  [[ -z "$(git status --porcelain)" ]] || break
  (( agent_attempt < MAX_AGENT_ATTEMPTS )) || break
  (( $(remaining) > 540 )) || break

  sleep_for="$TRANSIENT_BACKOFF_SECONDS"
  max_sleep=$(( $(remaining) - 540 ))
  (( sleep_for > max_sleep )) && sleep_for="$max_sleep"
  (( sleep_for > 0 )) && sleep "$sleep_for"
  agent_attempt=$((agent_attempt + 1))
  available="$(remaining)"
  agent_timeout=$((available - 240))
  (( agent_timeout > 3000 )) && agent_timeout=3000
  (( agent_timeout >= 300 )) || break
done

if [[ "$MODE" == 'pr' ]]; then
  pr_lifecycle="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${NUMBER}" 2>/dev/null || true)"
  if [[ -n "$pr_lifecycle" ]]; then
    pr_state="$(jq -r '.state // empty' <<< "$pr_lifecycle")"
    merged_at="$(jq -r '.merged_at // empty' <<< "$pr_lifecycle")"
    observed_head="$(jq -r '.head.sha // empty' <<< "$pr_lifecycle")"
    if [[ -n "$merged_at" ]]; then
      if [[ "$observed_head" == "$EXPECTED_HEAD" ]]; then
        log "factory_work_result_merged: PR #${NUMBER} merged at reviewed head ${EXPECTED_HEAD} during worker execution; skipping review controller"
        record_terminal_outcome success "PR #${NUMBER} merged at the reviewed head during worker execution"
        exit 0
      fi
      log "control_plane_failure: PR #${NUMBER} merged after its reviewed head changed from ${EXPECTED_HEAD} to ${observed_head}"
      record_terminal_outcome control_plane_failure "PR #${NUMBER} merged after reviewed head changed"
      exit 2
    fi
    if [[ "$pr_state" != 'open' ]]; then
      log "control_plane_failure: PR #${NUMBER} closed before trusted review-controller handoff"
      record_terminal_outcome control_plane_failure "PR #${NUMBER} closed before trusted review-controller handoff"
      exit 2
    fi
  fi
fi

if [[ "$MODE" == 'issue' ]]; then
  if pr="$(persist_issue_pr "$NUMBER" "$BRANCH")"; then
    if ! record_pr_provenance "$pr" "$NUMBER"; then
      log "PR #${pr} could not prove the issue assignment survived through persistence; closing it fail-closed"
      gh pr close "$pr" --comment 'Closed because durable factory provenance could not be established before handoff.' >/dev/null 2>&1 || true
      record_terminal_outcome control_plane_failure "PR #${pr} could not prove durable issue assignment provenance"
      log "assignment complete; remaining budget $(remaining)s"
      exit 0
    fi
    replace_labels "$pr" "$OWNER" 'factory:review'
    log "opened/updated PR #${pr} for issue #${NUMBER}"
    release_target "$NUMBER" 'factory:review' 'pr-opened-handoff' 'issue'
    release_target "$pr" 'factory:review' 'pr-opened-handoff' 'pr'
    record_terminal_outcome success "issue #${NUMBER} produced PR #${pr} and handed it to review"
  elif (( transient_failure == 1 )); then
    log "issue #${NUMBER} produced no changes because the model was interrupted; releasing the lease"
    release_target "$NUMBER" 'factory:building' 'transient-model-interruption' 'issue'
    record_agent_failure_outcome "$agent_status"
  elif (( agent_status != 0 )); then
    log "issue #${NUMBER} agent failed without persisted changes; releasing the lease"
    release_target "$NUMBER" 'factory:building' 'agent-failed-handoff' 'issue'
    record_agent_failure_outcome "$agent_status"
  else
    log "issue #${NUMBER} produced no persisted change; releasing the lease for another worker/model attempt"
    release_target "$NUMBER" 'factory:building' 'no-persisted-change-handoff' 'issue'
    record_terminal_outcome no_work "issue #${NUMBER} produced no persisted change"
  fi
  log "assignment complete; remaining budget $(remaining)s"
  exit 0
fi

if persist_pr_changes "$NUMBER" "$BRANCH"; then
  log "pushed repairs to PR #${NUMBER}; handing it to the merge controller for exact-head review"
  release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'repairs-pushed-handoff'
  record_terminal_outcome success "PR #${NUMBER} repairs were persisted and handed to review"
  log "assignment complete; remaining budget $(remaining)s"
  exit 0
fi

if (( transient_failure == 1 )); then
  release_pr_and_issue "$NUMBER" "$BRANCH" "$ASSIGNED_PR_STAGE" 'transient-model-interruption'
  record_agent_failure_outcome "$agent_status"
  log "assignment complete; remaining budget $(remaining)s"
  exit 0
fi

if (( agent_status != 0 )); then
  release_pr_and_issue "$NUMBER" "$BRANCH" "$ASSIGNED_PR_STAGE" 'review-agent-failed'
  record_agent_failure_outcome "$agent_status"
  log "assignment complete; remaining budget $(remaining)s"
  exit 0
fi

if [[ "$ASSIGNED_PR_STAGE" != 'factory:review' ]]; then
  repair_log="/tmp/opencode-factory-${WORKER}.log"
  repair_verdict="$(factory_extract_semantic_verdict "$repair_log" || true)"
  if [[ "$repair_verdict" == 'approve' ]]; then
    log "repair attempt for PR #${NUMBER} found no persisted change and reports ready; handing off for independent review"
    release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'repair-no-change-ready-handoff'
    record_terminal_outcome no_work "repair attempt for PR #${NUMBER} required no persisted change and handed off for review"
  else
    log "repair attempt for PR #${NUMBER} produced no persisted change; preserving ${ASSIGNED_PR_STAGE} without invoking the review controller"
    release_pr_and_issue "$NUMBER" "$BRANCH" "$ASSIGNED_PR_STAGE" 'repair-no-persisted-change-handoff'
    record_terminal_outcome no_work "repair attempt for PR #${NUMBER} produced no persisted change"
  fi
  log "assignment complete; remaining budget $(remaining)s"
  exit 0
fi

review_log="/tmp/opencode-factory-${WORKER}.log"
sanitized_review_log="/tmp/opencode-factory-${WORKER}.sanitized.log"
factory_sanitize_review_log "$review_log" "$sanitized_review_log"
# Append authoritative PR diff evidence after sanitize so the review controller
# can attest inspection even when the model used file reads instead of `gh pr
# diff`. review_excerpt keeps only the last 7000 chars, so the marker and the
# `gh pr diff` command line MUST come after the dumped hunks — putting them
# first caused every honest approve to soft-fail as diff-inspection-required
# once the dump exceeded the excerpt window (incident #2309 follow-up).
{
  printf '\n# comic-pile-factory-authoritative-diff-evidence\n'
  printf 'gh pr diff %s\n' "$NUMBER"
  gh pr diff "$NUMBER" 2>/dev/null | head -n 4000 || true
  printf '\n# comic-pile-factory-authoritative-diff-evidence\n'
  printf 'gh pr diff %s\n' "$NUMBER"
} >> "$sanitized_review_log"
# Retain the terminal token for diagnostics; the merge controller remains the
# only component authorized to decide whether a PR can merge.
last_token="$(factory_terminal_marker "$review_log" || true)"

current_head="$(gh pr view "$NUMBER" --json headRefOid --jq .headRefOid 2>/dev/null || true)"
if [[ "$current_head" != "$EXPECTED_HEAD" ]] || ! current_owner_is_self "$NUMBER"; then
  log "${FACTORY_SEMANTIC_STATUS_HEAD_CHANGED}: PR #${NUMBER} changed or lease was revoked during review"
  release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'semantic-review-head-changed'
  record_terminal_outcome control_plane_failure "PR #${NUMBER} head or lease changed during semantic review"
  exit 0
fi

if factory_review_has_conflicting_terminal_markers "$review_log"; then
  log "${FACTORY_SEMANTIC_STATUS_CONFLICTING}: PR #${NUMBER} emitted contradictory semantic markers"
  release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'conflicting-semantic-verdict'
  record_terminal_outcome work_failure "PR #${NUMBER} semantic review emitted contradictory terminal markers"
  exit 0
fi

verdict="$(factory_extract_semantic_verdict "$review_log" || true)"
recovery_attempted=0
recovery_status=0

if [[ -z "$verdict" ]] && [[ "$SOURCE" != 'kilo-auto' ]] && factory_review_is_substantive "$review_log"; then
  recovery_attempted=1
  recovery_log="/tmp/opencode-factory-${WORKER}-verdict-recovery.log"
  recovery_timeout=90
  available="$(remaining)"
  (( recovery_timeout > available - 45 )) && recovery_timeout=$((available - 45))

  if (( recovery_timeout >= 20 )); then
    log "semantic review completed without a terminal marker; attempting one bounded same-session verdict recovery"
    set +e
    factory_recover_semantic_verdict "$RUNTIME_MODEL" "$GITHUB_WORKSPACE" "$recovery_timeout" "$recovery_log"
    recovery_status=$?
    set -e

    current_head="$(gh pr view "$NUMBER" --json headRefOid --jq .headRefOid 2>/dev/null || true)"
    if [[ "$current_head" != "$EXPECTED_HEAD" ]] || ! current_owner_is_self "$NUMBER"; then
      log "${FACTORY_SEMANTIC_STATUS_HEAD_CHANGED}: PR #${NUMBER} changed or lease was revoked during recovery"
      release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'semantic-review-head-changed'
      record_terminal_outcome control_plane_failure "PR #${NUMBER} head or lease changed during semantic verdict recovery"
      exit 0
    fi

    if (( recovery_status == 0 )); then
      recovered_verdict="$(factory_extract_semantic_verdict "$recovery_log" || true)"
      if [[ "$recovered_verdict" == 'repair' || "$recovered_verdict" == 'reject' ]]; then
        verdict="$recovered_verdict"
        log "${FACTORY_SEMANTIC_STATUS_RECOVERED}: recovered conservative verdict for PR #${NUMBER}"
      elif [[ "$recovered_verdict" == 'approve' ]] && ! factory_primary_review_denies_ready_recovery "$review_log"; then
        verdict='approve'
        log "${FACTORY_SEMANTIC_STATUS_RECOVERED}: recovered approval verdict for PR #${NUMBER}"
      else
        log "${FACTORY_SEMANTIC_STATUS_RECOVERY_FAILED}: recovery remained ambiguous or contradicted the primary review"
      fi
    else
      log "${FACTORY_SEMANTIC_STATUS_RECOVERY_FAILED}: same-session recovery exited ${recovery_status}"
    fi
  fi
fi

if [[ -z "$verdict" ]]; then
  if (( recovery_attempted == 1 )); then
    semantic_status="$FACTORY_SEMANTIC_STATUS_RECOVERY_FAILED"
    release_reason='semantic-review-recovery-failed'
  else
    semantic_status="$FACTORY_SEMANTIC_STATUS_MISSING"
    release_reason='missing-semantic-verdict'
  fi
  log "${semantic_status}: review model did not produce an authoritative terminal verdict for PR #${NUMBER}"

  diagnostic="$(mktemp /tmp/factory-semantic-diagnostic.XXXXXX.md)"
  {
    printf '<!-- comic-pile-factory-semantic-diagnostic:v1 -->\n'
    printf '### Factory semantic review diagnostic\n\n'
    printf 'Status: `%s`  \n' "$semantic_status"
    printf 'Factory: `%s`  \n' "$WORKER"
    printf 'Run: `%s`  \n' "${GITHUB_RUN_ID:-unknown}"
    printf 'Reviewed head: `%s`\n\n' "$EXPECTED_HEAD"
    printf '<details><summary>Sanitized review tail</summary>\n\n```text\n'
    tail -n 120 "$sanitized_review_log"
    printf '\n```\n</details>\n'
  } > "$diagnostic"
  gh issue comment "$NUMBER" --body-file "$diagnostic" >/dev/null 2>&1 || true
  release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' "$release_reason"
  record_terminal_outcome work_failure "PR #${NUMBER} semantic review did not produce an authoritative terminal verdict"
  log "assignment complete; remaining budget $(remaining)s"
  exit 0
fi

case "$verdict" in
  approve) semantic_status="$FACTORY_SEMANTIC_STATUS_APPROVED" ;;
  repair|reject) semantic_status="$FACTORY_SEMANTIC_STATUS_BLOCKED" ;;
  *) semantic_status="$FACTORY_SEMANTIC_STATUS_MISSING" ;;
esac
log "${semantic_status}: submitting ${verdict} semantic verdict for PR #${NUMBER} at reviewed head ${EXPECTED_HEAD}"
set +e
python3 "$TRUSTED_REVIEW_CONTROLLER" review \
  --worker "$WORKER" \
  --pr "$NUMBER" \
  --reviewed-head "$EXPECTED_HEAD" \
  --verdict "$verdict" \
  --review-log "$sanitized_review_log"
controller_status=$?
set -e
if (( controller_status != 0 )); then
  record_terminal_outcome control_plane_failure "trusted review controller failed for PR #${NUMBER} with exit status ${controller_status}"
  exit "$controller_status"
fi
record_terminal_outcome success "PR #${NUMBER} semantic verdict completed through the trusted review controller"

log "assignment complete; remaining budget $(remaining)s"
