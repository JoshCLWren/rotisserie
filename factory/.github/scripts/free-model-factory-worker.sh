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
  # Keep a real gh resolved by an outer step: PATH may already start with the
  # usage-meter shim, and re-resolving here would point the meter at itself.
  real_gh="${FACTORY_REAL_GH:-$(command -v gh)}"
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
# shellcheck disable=SC1090  # trimmed copy of free-model-factory-worker-primitives.sh
source <(sed '/^ensure_owner_label$/,$d' .github/scripts/free-model-factory-worker-primitives.sh)
source .github/scripts/factory-semantic-verdict.sh
source .github/scripts/factory-fork-worker.sh

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
    success|no_work|work_failure|agent_timeout|provider_failure|provider_throttle|provider_quota|model_unavailable|model_retired_410|model_policy_violation|environment_failure|control_plane_failure|unknown_failure) ;;
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
  # Preserve trusted infrastructure failures through the final handoff.
  if [[ -f "$TERMINAL_OUTCOME_FILE" ]] &&       grep -Eq '^control_plane_failure[[:space:]]' "$TERMINAL_OUTCOME_FILE"; then
    return 0
  fi
  # GNU timeout returns 124 when this worker's own deadline expires.
  # Reading source/docs mentioning quotas must not poison provider health.
  if (( status == 124 )); then
    record_terminal_outcome agent_timeout "trusted local execution deadline expired (agent exit 124)"
    return
  fi
  if [[ -f "$log_file" ]] && grep -Eqi 'budget[ _]exhausted|api key budget (limit )?exceeded|payment required|HTTP[^0-9]*402|(monthly|daily|weekly|free) usage limit|reached your [a-z ]*usage limit|insufficient[ _](credits|balance|quota)|exceeded your current quota|quota[ _](exceeded|exhausted)|free-models-per-day|rate limit exceeded: free-models-per-day|add usage credits or subscribe|account (credits|balance) (depleted|exhausted)|(does|do)(n.t| not) have enough credits' "$log_file"; then
    record_terminal_outcome provider_quota "pinned provider/model account quota or budget is exhausted (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi '(^|[^0-9A-Za-z_])(429|413)([^0-9]|$)|too many requests|request too large|rate.?limit|\bquota\b|throttl|capacity[^\n]*(exceeded|exhausted|unavailable)|(exceeded|exhausted|unavailable)[^\n]*capacity|tokens per minute|\btpm\b' "$log_file"; then
    record_terminal_outcome provider_throttle "pinned provider/model session was throttled (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'HTTP[^0-9]*410|410 Gone' "$log_file"; then
    if [[ "$SOURCE" == nvidia ]]; then
      printf 'NVIDIA permanently retired model: %s\n' "$MODEL"
    fi
    record_terminal_outcome model_retired_410 "pinned model has been permanently retired by the provider (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'model[^[:alnum:]]+(not found|unavailable|does not exist)|unknown model|invalid model|free tier users do not have access to this model|HTTP[^0-9]*404|404 Not Found|function id [^ ]+ version [^ ]+: specified function [^\n]*not found' "$log_file"; then
    record_terminal_outcome model_unavailable "pinned model became unavailable during execution (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'model[^\n]*(policy|guard)[^\n]*(blocked|rejected|denied)|model[^\n]*(blocked|rejected|denied)[^\n]*(policy|guard)' "$log_file"; then
    record_terminal_outcome model_policy_violation "pinned model was rejected by provider/model policy (agent exit ${status})"
  elif [[ -f "$log_file" ]] && grep -Eqi 'checkout failed|dependency install failed|disk full|no space left|docker daemon|runner environment|tool installation failed' "$log_file"; then
    record_terminal_outcome environment_failure "worker environment failed during assigned execution (agent exit ${status})"
  elif (( status == 137 || status == 143 )); then
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
  if [[ -z "${KILO_SERVED_MODELS_FILE:-}" ]]; then
    KILO_SERVED_MODELS_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/kilo-served-models.XXXXXX")"
    export KILO_SERVED_MODELS_FILE
  fi
  cp .github/scripts/kilo-auto-factory-run.sh "$TRUSTED_KILO_HELPER"
  chmod +x "$TRUSTED_KILO_HELPER"
}

# Semantic state transitions are more privileged than the reviewed branch.
# Copy the controller and its pure policy module from trusted main before any
# checkout_target switch so a stale or contaminated PR cannot replace the
# authority code that interprets its model verdict.
stage_trusted_review_controller() {
  # Stage exactly once. This definition overrides the shared primitive that
  # free-model-factory-worker.sh sources at startup, so without the guard every
  # later caller -- record_head_contribution runs after checkout_target has
  # switched onto the PR branch -- would re-copy the controller out of that
  # branch's working tree and the PR could supply the code that records its own
  # contributor provenance.
  [[ -z "${TRUSTED_REVIEW_CONTROLLER:-}" ]] || return 0
  local trusted_dir
  trusted_dir="$(mktemp -d /tmp/comic-pile-review-controller.XXXXXX)"
  cp .github/scripts/factory-review-controller.py "$trusted_dir/factory-review-controller.py"
  cp .github/scripts/factory_review_policy.py "$trusted_dir/factory_review_policy.py"
  cp .github/scripts/factory_issue_acceptance.py "$trusted_dir/factory_issue_acceptance.py"
  cp .github/scripts/factory_implementation_delivery.py "$trusted_dir/factory_implementation_delivery.py"
  cp .github/scripts/factory_post_merge_closure.py "$trusted_dir/factory_post_merge_closure.py"
  # Bind model provenance to a trusted main checkout snapshot, not PR data.
  cp .github/scripts/factory_worker_slots.py "$trusted_dir/factory_worker_slots.py"
  cp .github/scripts/factory_provider_registry.py "$trusted_dir/factory_provider_registry.py"
  cp .github/scripts/factory_work_policy.py "$trusted_dir/factory_work_policy.py"
  cp .github/scripts/factory_fork_control.py "$trusted_dir/factory_fork_control.py"
  # factory-fork-worker.sh reads this from the environment.
  export TRUSTED_FORK_CONTROL="$trusted_dir/factory_fork_control.py"
  # The staged policy imports canonical eligibility rules; pin this dependency
  # from trusted main too, never from an untrusted PR checkout or /scripts.
  cp scripts/factory_eligibility.py "$trusted_dir/factory_eligibility.py"
  chmod +x "$trusted_dir/factory-review-controller.py"
  TRUSTED_REVIEW_CONTROLLER="$trusted_dir/factory-review-controller.py"
  export TRUSTED_REVIEW_CONTROLLER
}

# Pin the checkpoint writer to trusted main before loading arbitrary PR code.
# The model may edit repository scripts but cannot replace this running helper.
stage_trusted_checkpoint_helper() {
  TRUSTED_WORK_CHECKPOINT="$(mktemp "${RUNNER_TEMP:-/tmp}/factory-work-checkpoint.XXXXXXXX")"
  cp .github/scripts/factory-work-checkpoint.sh "$TRUSTED_WORK_CHECKPOINT"
  chmod +x "$TRUSTED_WORK_CHECKPOINT"
  TRUSTED_CHECKPOINT_HANDOFF="$(mktemp "${RUNNER_TEMP:-/tmp}/factory-checkpoint-handoff.XXXXXXXX.py")"
  cp .github/scripts/factory_checkpoint_handoff.py "$TRUSTED_CHECKPOINT_HANDOFF"
  export TRUSTED_CHECKPOINT_HANDOFF
}

# Keep the established OpenCode/NVIDIA implementation untouched. Kilo needs a
# small override only so it invokes the trusted helper staged from main.
eval "$(declare -f run_agent | sed '1s/^run_agent /legacy_run_agent /')"
run_agent() {
  local mode="$1" number="$2" timeout_seconds="$3"
  local target mission prompt findings status=0
  if [[ "${FACTORY_FORK_STATIC_REVIEW:-0}" == 1 ]]; then
    factory_run_fork_static_review "$timeout_seconds"
    return $?
  fi
  if [[ "$SOURCE" != 'kilo-auto' ]]; then
    legacy_run_agent "$@"
    return $?
  fi

  if [[ "$mode" == 'pr' ]]; then
    target="pull request #${number}"
    mission="Resume this PR. Inspect the exact current head, required CI, review submissions, and every inline review thread. Greptile (greptile-apps[bot]) review findings are HIGH PRIORITY, must-address feedback: fix each open one in code or rebut it with concrete evidence, and record every outcome in the outcome file described below; the wrapper posts those outcomes as the thread replies and blocks FACTORY_GATE_READY while any open Greptile finding lacks one. Never resolve a Greptile thread yourself. Fix closure-critical defects and resolve or concretely rebut actionable threads. If no edits are required, decide whether the PR fully completes its declared scope and is safe to merge. End your final response with FACTORY_GATE_READY only for semantic approval, FACTORY_GATE_REJECT only when the PR is clearly unsalvageable, contaminated, obsolete, duplicate, or fundamentally incomplete, otherwise end with FACTORY_GATE_NOT_READY."
    findings="$(factory_review_findings_mission "$number" "${EXPECTED_HEAD:-$(git rev-parse HEAD)}")"
    mission+=" ${findings}"
    if completion_line="$(trusted_completion_action_line)"; then
      mission+=" ${completion_line}"
    fi
  else
    target="issue #${number}"
    mission="Implement the full closure-critical acceptance contract for this issue with code and focused tests. Do not stop at planning or optional polish."
  fi

  if [[ "$mode" == 'pr' && -n "${FACTORY_FAST_CHECK_FEEDBACK_FILE:-}" ]]; then
    mission+=" This is a same-lease fast-check repair, not a fresh review. The prior unpushed edits are still in this worktree. Read the local trusted fast-check log at ${FACTORY_FAST_CHECK_FEEDBACK_FILE}, correct the concrete failures while retaining valid changes, and run focused validation. The wrapper will independently rerun scope and pre-push checks. Do not commit or push."
  fi

  prompt="Read AGENTS.md, docs/ISSUE_EXECUTION_PROTOCOL.md, docs/AUTONOMOUS_FACTORY_POLICY.md, docs/CHATGPT_FACTORY_PROMPT.md, and docs/FACTORY_GITHUB_VISIBILITY.md first. Follow the canonical product-first factory policy.  Edit only the assigned target, run focused validation, and use GitHub for review context. Do not commit or push; the wrapper persists changes. Do not merge or close the assigned pull request; the trusted wrapper and review controller own the final lifecycle transition after your terminal verdict. Do not switch models, providers, or routes. A provider failure is a result for this lane, not permission to fall back to another paid or unrequested route. Do not enable auto-merge, push main, touch production databases, or alter automation schedules. Run context: Worker: ${WORKER_ID}; source: ${SOURCE}; model: ${MODEL}; runtime: ${RUNTIME_MODEL}; target: ${target}. ${mission}"

  bash "$TRUSTED_KILO_HELPER" \
    "$timeout_seconds" \
    "ComicPile Factory ${WORKER} · ${DISPLAY}" \
    "$prompt" \
    "/tmp/opencode-factory-${WORKER}.log" || status=$?
  return "$status"
}

# Trusted Greptile outcome gate (docs/FACTORY_GITHUB_VISIBILITY.md,
# "Greptile outcome gate"). Runs after the agent, right before any ready or
# review handoff:
#   1. rebuilds the findings file and the open Greptile findings from a fresh,
#      complete fetch (fail closed when the fetch is incomplete);
#   2. validates the agent's per-finding outcome file against them and posts
#      each valid outcome as a reply on its thread (idempotent; never resolves);
#   3. compares against the snapshot taken when the mission was built, so
#      findings that arrived during the run are never declared ready.
# $1 is the SHA this run pushed ("" when nothing was pushed). Sets
# GREPTILE_GATE_STATUS (clear|blocked|unknown|new-findings) and
# GREPTILE_GATE_DETAIL. Returns 0 only for clear.
greptile_outcome_gate() {
  local pushed="${1:-}" head findings validation valid new_ids reply_status=0
  findings="/tmp/factory-review-findings-${WORKER}-pr${NUMBER}.md"
  head="$(git rev-parse HEAD)"
  # Fork reviews run data-only from trusted main, so HEAD is main, not the
  # reviewed contributor head. Gate findings and outcome replies against the
  # exact reviewed head instead.
  if [[ "${FACTORY_FORK_STATIC_REVIEW:-0}" == 1 && -n "${EXPECTED_HEAD:-}" ]]; then
    head="$EXPECTED_HEAD"
  fi
  GREPTILE_GATE_STATUS='unknown'
  GREPTILE_GATE_DETAIL='open Greptile findings could not be fetched completely (fail closed)'
  factory_collect_review_findings "$NUMBER" "$head" "$findings" || true
  jq -e 'type == "array"' "${findings}.open.json" >/dev/null 2>&1 || return 1
  validation="$(factory_validate_greptile_outcomes "${findings}.open.json" "$(factory_greptile_outcomes_path)" "$pushed")" || true
  if [[ -z "$validation" ]]; then
    GREPTILE_GATE_DETAIL='Greptile outcome file could not be validated'
    return 1
  fi
  valid="$(jq -c '.valid' <<< "$validation")"
  if [[ "$valid" != '[]' ]] && ! factory_post_greptile_outcome_replies "$valid" "$head" "${findings}.threads.json"; then
    reply_status=1
  fi
  new_ids=''
  if [[ -f "${findings}.open-before.json" ]]; then
    new_ids="$(jq -rn --slurpfile now "${findings}.open.json" --slurpfile before "${findings}.open-before.json" \
      '([$now[0][].id] - [$before[0][].id]) | join(", ")' 2>/dev/null || true)"
  fi
  if [[ -n "$new_ids" ]]; then
    GREPTILE_GATE_STATUS='new-findings'
    GREPTILE_GATE_DETAIL="new Greptile findings arrived during this run: ${new_ids}"
    return 1
  fi
  GREPTILE_GATE_STATUS='blocked'
  if ! jq -e '(.missing | length) == 0 and (.invalid | length) == 0' <<< "$validation" >/dev/null; then
    GREPTILE_GATE_DETAIL="open Greptile findings without a valid outcome: $(jq -r '
      ([.missing[] | "\(.) (missing)"] + [.invalid[] | "\(.thread_id) (\(.reason))"]) | join("; ")' <<< "$validation")"
    return 1
  fi
  if (( reply_status != 0 )); then
    GREPTILE_GATE_DETAIL='posting a Greptile outcome reply failed'
    return 1
  fi
  GREPTILE_GATE_STATUS='clear'
  GREPTILE_GATE_DETAIL="$(jq -r '"\(.valid | length) Greptile outcome(s) recorded"' <<< "$validation")"
  return 0
}

review_snapshot_failure_reason() {
  local observed="$1" expected="$2" pr="$3"
  if [[ -z "$observed" ]]; then
    printf '%s' 'semantic-review-head-read-failed'
  elif [[ "$observed" != "$expected" ]]; then
    printf '%s' 'semantic-review-head-changed'
  elif ! current_owner_is_self "$pr"; then
    printf '%s' 'semantic-review-lease-lost'
  fi
}

# Record a wrapper-gate downgrade in the review log the controller excerpts.
append_wrapper_gate_reason() {
  printf '\n# comic-pile-factory-wrapper-gate\nThe trusted wrapper downgraded this approval to repair: %s\n' "$1" >> "$sanitized_review_log"
}

read_trusted_completion_decision() {
  local pr="$1" worker="$2" payload parsed action='' head=''
  JEV_ACTION=''
  JEV_ACTION_HEAD=''
  if ! payload="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${pr}/comments?per_page=100" 2>/dev/null)"; then
    log "unable to read lease provenance for PR #${pr}; using generic PR mission"
    return 0
  fi
  parsed="$(jq -sr --arg worker "$worker" '
    [ .[][]?
      | select(.performed_via_github_app.slug == "github-actions")
      | .body
      | capture("comic-pile-factory-controller-claim-v1:pr-(?<pr>[0-9]+):(?<worker>[0-9]+):(?<epoch>[0-9]+)(?::source=(?<source>[a-z-]+))?(?::action=(?<action>[a-z-]+))?(?::head=(?<head>[0-9a-f]{7,40}))?")
      | select(.worker == $worker)
    ] | last // empty | [.action // "", .head // ""] | @tsv
  ' <<< "$payload" 2>/dev/null)" || true
  IFS=$'\t' read -r action head <<< "$parsed" || true
  JEV_ACTION="${action:-}"
  JEV_ACTION_HEAD="${head:-}"
  log "lease decision provenance for PR #${pr}: action=${JEV_ACTION:-none} head=${JEV_ACTION_HEAD:-none}"
}

select_controller_assignment() {
  local assignment="${FACTORY_ASSIGNMENT:-}" target
  [[ -n "$assignment" ]] || { log 'dispatcher assignment input is required'; return 3; }
  if ! jq -e --arg worker "$WORKER" '
    .worker == $worker and (.kind == "issue" or .kind == "pr")
    and (.number | type == "number" and . > 0 and floor == .)
  ' <<< "$assignment" >/dev/null; then
    log 'invalid dispatcher assignment'; return 3
  fi
  MODE="$(jq -r '.kind' <<< "$assignment")"
  NUMBER="$(jq -r '.number' <<< "$assignment")"
  target="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${NUMBER}")" || return 3
  if ! jq -e --arg owner "$OWNER" '.state == "open" and ([.labels[].name] | index($owner) != null)' <<< "$target" >/dev/null; then
    log 'assigned target ownership changed'; return 3
  fi
  if [[ "$MODE" == pr ]]; then
    target="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${NUMBER}")" || return 3
    if ! jq -e --arg head "$(jq -r '.head' <<< "$assignment")" '.head.sha == $head' <<< "$target" >/dev/null; then
      log 'assigned PR head moved'; return 3
    fi
    BRANCH="$(jq -r '.head.ref' <<< "$target")"
    # The controller exports stage_of(live labels), which is null for a PR
    # the reconciler left stageless. Compare the same projection: a null
    # stage must still be stageless, never a label literally named "null".
    ASSIGNED_PR_STAGE="$(jq -r '.stage // empty' <<< "$assignment")"
    if ! jq -e --arg stage "$ASSIGNED_PR_STAGE" '
      [.labels[].name] as $labels
      | if $stage == "" then
          ($labels | map(select(IN("factory:building", "factory:review",
            "factory:changes-requested", "factory:ci", "factory:ready",
            "factory:blocked"))) | length) == 0
        else ($labels | index($stage)) != null end
    ' <<< "$target" >/dev/null; then
      log 'assigned PR stage changed'; return 3
    fi
    read_trusted_completion_decision "$NUMBER" "$WORKER"
  else
    BRANCH="factory/${WORKER}-${NUMBER}-${BRANCH_SUFFIX}"
  fi
  return 0
}

ensure_owner_label
stage_trusted_guard
stage_trusted_checkpoint_helper
stage_trusted_kilo_helper

# Keep all selection and configuration code on the trusted main snapshot.
# The agent may check out/edit a PR branch before a fallback is needed.
stage_trusted_model_failover() {
  FACTORY_TRUSTED_FAILOVER_DIR="$(mktemp -d "$RUNNER_TEMP/factory-model-failover.XXXXXXXX")"
  local module
  # Stage every trusted module, not a hand-kept list: a missing transitive
  # import (factory_model_catalog) crashed every same-run failover with
  # ModuleNotFoundError and ended the worker after its first model failed.
  for module in .github/scripts/*.py; do
    case "${module##*/}" in test_*|*_test.py) continue ;; esac
    cp "$module" "$FACTORY_TRUSTED_FAILOVER_DIR/"
  done
  cp scripts/factory_eligibility.py "$FACTORY_TRUSTED_FAILOVER_DIR/factory_eligibility.py"
  cp .github/scripts/wait-opencode-model.sh "$FACTORY_TRUSTED_FAILOVER_DIR/wait-opencode-model.sh"
  export FACTORY_TRUSTED_FAILOVER_DIR
}

stage_trusted_review_controller
stage_trusted_worker_app_helper
stage_trusted_model_failover

# Issue snapshots are the normal work PR; repairs advance the assigned PR atomically.
# A separate Git index avoids interfering with an agent editing files.
CHECKPOINT_PID=''
CHECKPOINT_LOG=''
CHECKPOINT_REF_FILE=''
start_work_checkpoint() {
  [[ "${FACTORY_FORK_STATIC_REVIEW:-0}" != 1 ]] || return 0
  [[ -n "${GITHUB_RUN_ID:-}" ]] || return 0
  local checkpoint_issue
  if [[ "$MODE" == issue ]]; then
    checkpoint_issue="$NUMBER"
  else
    checkpoint_issue="$(linked_issue_from_pr "$NUMBER")" || checkpoint_issue=''
    if [[ -z "$checkpoint_issue" ]]; then
      log "checkpoint issue linkage unavailable for PR #${NUMBER}; preserving explicit unknown-issue identity"
    fi
  fi
  CHECKPOINT_REF_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/factory-persisted-ref.XXXXXXXX")"
  export FACTORY_CHECKPOINT_REF_FILE="$CHECKPOINT_REF_FILE"
  CHECKPOINT_LOG="${RUNNER_TEMP:-/tmp}/factory-work-checkpoint-${GITHUB_RUN_ID}.log"
  FACTORY_CHECKPOINT_ISSUE="$checkpoint_issue" FACTORY_CHECKPOINT_MODEL="$MODEL" \
    FACTORY_CHECKPOINT_RUNTIME_MODEL="$RUNTIME_MODEL" \
    bash "$TRUSTED_WORK_CHECKPOINT" "$MODE" "$NUMBER" "$WORKER" "$GITHUB_RUN_ID" >"$CHECKPOINT_LOG" 2>&1 &
  CHECKPOINT_PID="$!"
  log "automatic persisted work enabled for run $GITHUB_RUN_ID (every 45s)"
}

stop_work_checkpoint() {
  [[ -n "$CHECKPOINT_PID" ]] || return 0
  kill -USR1 "$CHECKPOINT_PID" 2>/dev/null || true
  wait "$CHECKPOINT_PID" || log 'persisted work writer did not exit cleanly'
  CHECKPOINT_PID=''
  [[ ! -f "$CHECKPOINT_LOG" ]] || tail -n 8 "$CHECKPOINT_LOG" || true
}

discard_delivered_checkpoint() {
  [[ -n "${GITHUB_RUN_ID:-}" ]] || return 0
  stop_work_checkpoint
  # Record exact-head validation; canonical open work branches are retained.
  python3 "$TRUSTED_CHECKPOINT_HANDOFF" --repo "$GITHUB_REPOSITORY" \
    --run-id "$GITHUB_RUN_ID" --delivered-pr "$1" --cleanup || \
    log 'checkpoint cleanup deferred to independent reconciliation'
}

finish_persisted_work() {
  stop_work_checkpoint
  release_owned_targets session-end-handoff || true
  # Release the saved PR as well as its source issue, after execution really ends.
  [[ -z "${GITHUB_RUN_ID:-}" ]] || python3 "$TRUSTED_CHECKPOINT_HANDOFF" \
    --repo "$GITHUB_REPOSITORY" --run-id "$GITHUB_RUN_ID" --source-ended || true
}
trap 'finish_persisted_work' EXIT

MODE=''
NUMBER=''
BRANCH=''
ASSIGNED_PR_STAGE=''
JEV_ACTION=''
JEV_ACTION_HEAD=''

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

# Dispatch and worker execution each check the authoritative leased stage.
# If the lease was repurposed while Entry started, the composite review model
# must never run implementation, review-findings repairs, or CI repairs.
if [[ "$RUNTIME_MODEL" == 'openrouter/free' ]] \
  && [[ "$MODE" != pr || "$ASSIGNED_PR_STAGE" != 'factory:review' ]]; then
  log "control_plane_failure: dedicated review model cannot execute ${MODE} #${NUMBER} stage ${ASSIGNED_PR_STAGE:-none}"
  record_terminal_outcome control_plane_failure 'composite semantic review route lost its review-stage assignment'
  release_owned_targets 'composite-review-stage-changed' || true
  exit 2
fi

log "executing control-plane assignment: ${MODE} #${NUMBER}; runtime ${RUNTIME_MODEL}; budget ${BUDGET_SECONDS}s"
fork_route=1
if factory_route_fork_assignment; then fork_route=0; else fork_route=$?; fi
if (( fork_route == 10 )); then
  exit 0
elif (( fork_route != 0 && fork_route != 1 )); then
  record_terminal_outcome control_plane_failure 'safe fork routing failed before source execution'
  exit 2
fi
if [[ "${FACTORY_FORK_STATIC_REVIEW:-0}" != 1 ]]; then
  checkout_target "$MODE" "$NUMBER" "$BRANCH"
  if [[ "$MODE" == pr && "$(git rev-parse HEAD)" != "$(jq -r '.head' <<< "$FACTORY_ASSIGNMENT")" ]]; then
    log 'assigned PR moved during checkout; refusing stale execution'
    record_terminal_outcome control_plane_failure 'assigned PR head moved during checkout'
    exit 2
  fi
  start_work_checkpoint
fi

available="$(remaining)"
# Reserve post-agent time for the pre-push fast checks (toolchain install,
# lint/type, targeted tests) on top of the existing handoff reserve.
FAST_CHECK_RESERVE_SECONDS="${FACTORY_FAST_CHECK_RESERVE_SECONDS:-240}"
agent_timeout=$((available - 240 - FAST_CHECK_RESERVE_SECONDS))
if (( agent_timeout < 300 )); then
  log 'insufficient remaining budget for assigned work'
  record_terminal_outcome environment_failure 'insufficient remaining runner budget for assigned work'
  exit 1
fi

# Iterate eligible provider/model pairs within this lease.
ORIGINAL_SOURCE="$SOURCE"
attempted_pairs=()
blocked_providers=()
ACCOUNT_REFUSAL_RE='insufficient balance or no resource package|please recharge|add usage credits or subscribe|api key budget (limit )?exceeded|account (credits|balance) (depleted|exhausted)|billing_api_budget_exhausted|payment required|no resource package|(does|do)(n.t| not) have enough credits'
account_wide_failure() {
  grep -Eqi "$ACCOUNT_REFUSAL_RE" "/tmp/opencode-factory-$WORKER.log"
}
# OpenCode prints some provider account refusals (Pollinations "doesn't have
# enough credits") and still exits 0. A fast, edit-free zero exit whose log
# carries such a refusal never reached the model: treat it as the provider
# failure it is so the lease fails over instead of burning a review attempt.
zero_exit_provider_refusal() {
  local elapsed="$1"
  (( elapsed < FAST_FAILOVER_SECONDS )) || return 1
  [[ -z "$(git status --porcelain)" ]] || return 1
  account_wide_failure
}
record_model_failover_failure() {
  local failed_source="$1" failed_model="$2" classification="$3" scope="$4"
  # The timestamped log marker is the attested record the trusted Entry
  # persister writes to Neon (refine_trusted_failover_models). Issue #1093
  # is retired as a registry and is never written.
  log "FACTORY_TRUSTED_FAILOVER: source=$failed_source model=$failed_model outcome=$classification scope=$scope"
}
select_next_eligible_model() {
  local next_json next_source next_model next_runtime candidate_status pair provider
  local -a select_args=(--source "$ORIGINAL_SOURCE" --worker "$WORKER" \
    --rotation "$GITHUB_RUN_NUMBER" \
    --exclusions-file "$RUNNER_TEMP/factory-neon-model-exclusions.json" \
    --eligible-sources-file "$RUNNER_TEMP/factory-fleet-sources.json" \
    --fleet-catalog-cache "$RUNNER_TEMP/factory-fleet-catalogs.json")
  for pair in "${attempted_pairs[@]}"; do select_args+=(--attempted-pair "$pair"); done
  for provider in "${blocked_providers[@]}"; do select_args+=(--blocked-provider "$provider"); done
  while :; do
    (( $(remaining) > 540 )) || return 1
    if ! next_json="$(python3 "$FACTORY_TRUSTED_FAILOVER_DIR/factory_model_failover.py" "${select_args[@]}")"; then
      log "No further currently eligible fleet provider/model candidates"
      return 1
    fi
    next_source="$(jq -er '.source' <<< "$next_json")" || return 1
    next_model="$(jq -er '.model' <<< "$next_json")" || return 1
    next_runtime="$(jq -er '.runtime_model' <<< "$next_json")" || return 1
    pair="$(jq -nc --arg source "$next_source" --arg model "$next_model" '[$source,$model]')"
    attempted_pairs+=("$pair")
    select_args+=(--attempted-pair "$pair")
    if [[ "$next_source" != opencode-free && "$next_source" != openrouter-free && "$next_source" != kilo-auto ]]; then
      local config_file="$HOME/.config/opencode/opencode.json"
      local candidate_config="$RUNNER_TEMP/factory-failover-config.json"
      if ! python3 "$FACTORY_TRUSTED_FAILOVER_DIR/factory_provider_registry.py" config --source "$next_source" --model "$next_model" > "$candidate_config"; then
        record_terminal_outcome control_plane_failure "canonical provider configuration failed during failover"
        return 1
      fi
      jq -s '.[0] * .[1]' "$config_file" "$candidate_config" > "$candidate_config.merged" || return 1
      mv "$candidate_config.merged" "$config_file"
      chmod 600 "$config_file"
    fi
    if [[ "$next_source" != kilo-auto ]]; then
      candidate_status=0
      bash "$FACTORY_TRUSTED_FAILOVER_DIR/wait-opencode-model.sh" "$next_runtime" || candidate_status=$?
      if (( candidate_status == 3 )); then
        record_model_failover_failure "$next_source" "$next_model" model_unavailable model || return 1
        continue
      fi
      if (( candidate_status != 0 )); then
        record_terminal_outcome control_plane_failure "executable catalog unreadable during failover"
        return 1
      fi
    fi
    # The trusted FACTORY_FLEET_TRANSITION log marker below (attested from
    # the completed job log into Neon) is the only transition record.
    stop_work_checkpoint
    SOURCE="$next_source"
    MODEL="$next_model"
    RUNTIME_MODEL="$next_runtime"
    DISPLAY="$SOURCE · $MODEL"
    start_work_checkpoint
    log "FACTORY_FLEET_TRANSITION: source=$SOURCE model=$MODEL runtime=$RUNTIME_MODEL target=$MODE/$NUMBER"
    return 0
  done
}

agent_attempt=1
agent_status=0
transient_failure=0
FAST_FAILOVER_SECONDS="${FACTORY_FAST_FAILOVER_SECONDS:-180}"
while :; do
  log "FACTORY_EXECUTION_CANDIDATE: source=$SOURCE model=$MODEL runtime=$RUNTIME_MODEL"
  : > "/tmp/opencode-factory-${WORKER}.log"
  attempt_started_at="$SECONDS"
  set +e
  run_agent "$MODE" "$NUMBER" "$agent_timeout"
  agent_status=$?
  set -e
  attempt_elapsed=$((SECONDS - attempt_started_at))
  log "agent exit status ${agent_status} for ${MODE} #${NUMBER}"
  if (( agent_status == 0 )) && zero_exit_provider_refusal "$attempt_elapsed"; then
    log "agent exited 0 after a provider account refusal; treating it as a provider failure"
    agent_status=1
  fi

  if (( agent_status == 0 )); then
    # Earlier failed candidates must never poison the terminal outcome.
    rm -f "$TERMINAL_OUTCOME_FILE"
    transient_failure=0
    break
  fi
  # The actual inference failed. For model-scoped provider failures, advance
  # through *all* discovered eligible models, not the same pinned model twice.
  # Never reassign a claimed issue/PR or discard changes already checkpointed.
  record_agent_failure_outcome "$agent_status"
  failure_class="$(cut -f1 "$TERMINAL_OUTCOME_FILE")"
  # An agent that exits non-zero within the first minutes, edits nothing and
  # leaves no narrower evidence never started real work: the candidate's
  # inference call failed. Treat it as a model-scoped provider failure so the
  # same lease advances to the next eligible pair instead of being released.
  if [[ "$failure_class" == unknown_failure ]] && (( attempt_elapsed < FAST_FAILOVER_SECONDS )) \
    && [[ -z "$(git status --porcelain)" ]] && (( agent_status != 124 )); then
    record_terminal_outcome provider_failure "candidate exited ${agent_status} after ${attempt_elapsed}s without edits or narrower evidence"
    failure_class=provider_failure
  fi
  if [[ "$SOURCE" != omniroute-free && "${FACTORY_FORK_STATIC_REVIEW:-0}" != 1 ]]; then
    case "$failure_class" in
      provider_quota|provider_throttle|provider_failure|model_unavailable|model_retired_410|model_policy_violation)
        if [[ -z "$(git status --porcelain)" ]] && (( $(remaining) > 540 )); then
          attempted_pairs+=("$(jq -nc --arg source "$SOURCE" --arg model "$MODEL" '[$source,$model]')")
          # A paid-model credit refusal backs off only the attempted model.
          failure_scope=model
          if ! record_model_failover_failure "$SOURCE" "$MODEL" "$failure_class" "$failure_scope"; then
            record_terminal_outcome control_plane_failure "failed to persist exact attempt before failover"
            break
          fi
          if select_next_eligible_model; then
            agent_attempt=1
            available="$(remaining)"
            agent_timeout=$((available - 240 - FAST_CHECK_RESERVE_SECONDS))
            (( agent_timeout >= 300 )) || break
            continue
          fi
        fi
        break
        ;;
    esac
  fi
  if ! is_transient_agent_failure "$agent_status"; then
    transient_failure=0
    break
  fi

  transient_failure=1
  log 'Non-attributable transient interruption; bounded retry of this model only'
  [[ -z "$(git status --porcelain)" ]] || break
  (( agent_attempt < MAX_AGENT_ATTEMPTS )) || break
  (( $(remaining) > 540 )) || break

  # Exponential retry delay, bounded by both the session and a ten-minute
  # maximum. Never retry after dirty worktree changes or exhausted budget.
  backoff_factor=$((1 << (agent_attempt - 1)))
  sleep_for=$(( TRANSIENT_BACKOFF_SECONDS * backoff_factor ))
  (( sleep_for > 600 )) && sleep_for=600
  max_sleep=$(( $(remaining) - 540 ))
  (( sleep_for > max_sleep )) && sleep_for="$max_sleep"
  (( sleep_for > 0 )) && sleep "$sleep_for"
  agent_attempt=$((agent_attempt + 1))
  available="$(remaining)"
  agent_timeout=$((available - 240 - FAST_CHECK_RESERVE_SECONDS))
  (( agent_timeout >= 300 )) || break
done

# Flush pending model edits before checks while retaining periodic protection
# for the optional correction model run inside the persistence helpers. EXIT
# performs the final synchronous snapshot on all other handoff/failure paths.
[[ -z "$CHECKPOINT_PID" ]] || kill -USR2 "$CHECKPOINT_PID" 2>/dev/null || true

# Flush synchronously and adopt the saved parent chain before overlap detection.
# EXPECTED_HEAD stays the initial base so validation covers the entire attempt.
stop_work_checkpoint
if ! adopt_persisted_work; then
  record_terminal_outcome control_plane_failure 'persisted ancestry moved; work retained'
  exit 2
fi

if [[ "$MODE" == issue ]] && ! redirect_issue_to_overlapping_pr "$NUMBER"; then
  record_terminal_outcome control_plane_failure 'overlapping PR is owned, unreadable, or needs conflict recovery; no duplicate opened'
  exit 2
fi

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
      log "PR #${pr} provenance needs reconciliation; preserving the work PR"
      release_target "$pr" factory:changes-requested provenance-incomplete pr || true
      record_terminal_outcome control_plane_failure "PR #${pr} could not prove durable issue assignment provenance"
      log "assignment complete; remaining budget $(remaining)s"
      exit 0
    fi
    replace_labels "$pr" "$OWNER" 'factory:review'
    # The controller records this producer against the exact head it just
    # created so a later repair cannot make it an eligible reviewer. A newly
    # created PR is unlabeled, and record_contribution refuses anything that is
    # not a factory pull request, so this must follow the label write.
    record_head_contribution "$pr" 'pr-opened-handoff' || true
    log "opened/updated PR #${pr} for issue #${NUMBER}"
    release_target "$NUMBER" 'factory:review' 'pr-opened-handoff' 'issue'
    release_target "$pr" 'factory:review' 'pr-opened-handoff' 'pr'
    # Trusted markers above stay on github-actions[bot]. Worker 48 may add a
    # separate plain-prose handoff as its App (#3135); others post nothing.
    post_readable_handoff implementation "$pr" "$(git rev-parse HEAD)" "Opened from issue #${NUMBER}."
    record_terminal_outcome success "issue #${NUMBER} produced PR #${pr} and handed it to review"
    discard_delivered_checkpoint "$pr"
  elif [[ -n "${CHECKPOINT_REF_FILE:-}" && -s "$CHECKPOINT_REF_FILE" ]]; then
    record_terminal_outcome work_failure "issue #${NUMBER} work is preserved; final validation/delivery failed"
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
  log "pushed repairs to PR #${NUMBER}; recording contributor provenance"
  record_head_contribution "$NUMBER" 'repairs-pushed-handoff' || true
  pushed_stage='factory:review'
  pushed_reason='repairs-pushed-handoff'
  if greptile_outcome_gate "$(git rev-parse HEAD)"; then
    log "greptile outcome gate clear for PR #${NUMBER}: ${GREPTILE_GATE_DETAIL}"
  else
    # Pushed, but an open Greptile finding has no recorded outcome (or new
    # findings arrived): re-dispatch for repair instead of review.
    log "greptile outcome gate ${GREPTILE_GATE_STATUS} for PR #${NUMBER}: ${GREPTILE_GATE_DETAIL}; releasing as factory:changes-requested"
    pushed_stage='factory:changes-requested'
    pushed_reason='repairs-pushed-greptile-findings-open'
  fi
  log "pushed repairs to PR #${NUMBER}; handing it to the merge controller as ${pushed_stage}"
  release_pr_and_issue "$NUMBER" "$BRANCH" "$pushed_stage" "$pushed_reason"
  post_readable_handoff repair "$NUMBER" "$(git rev-parse HEAD)" "Repairs were pushed and the lease was released for independent review."
  record_terminal_outcome success "PR #${NUMBER} repairs were persisted and handed to review"
  discard_delivered_checkpoint "$NUMBER"
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

if [[ -n "$FACTORY_PUSH_BLOCKED_REASON" ]]; then
  release_pr_and_issue "$NUMBER" "$BRANCH" factory:changes-requested failed-validation
  record_terminal_outcome work_failure "PR #${NUMBER} failed trusted checks; work retained"
  exit 0
fi

if [[ "$ASSIGNED_PR_STAGE" != 'factory:review' ]]; then
  repair_log="/tmp/opencode-factory-${WORKER}.log"
  repair_verdict="$(factory_extract_semantic_verdict "$repair_log" || true)"
  if [[ "$repair_verdict" == 'approve' && -n "$FACTORY_PUSH_BLOCKED_REASON" ]]; then
    log "repair attempt for PR #${NUMBER} reported ready but its edits were not pushed (${FACTORY_PUSH_BLOCKED_REASON}); preserving ${ASSIGNED_PR_STAGE}"
    release_pr_and_issue "$NUMBER" "$BRANCH" "$ASSIGNED_PR_STAGE" "repair-${FACTORY_PUSH_BLOCKED_REASON}"
    record_terminal_outcome work_failure "repair attempt for PR #${NUMBER} failed pre-push fast checks"
  elif [[ "$repair_verdict" == 'approve' ]] && ! greptile_outcome_gate ''; then
    log "repair attempt for PR #${NUMBER} reported ready but the greptile outcome gate is ${GREPTILE_GATE_STATUS}: ${GREPTILE_GATE_DETAIL}; preserving ${ASSIGNED_PR_STAGE}"
    release_pr_and_issue "$NUMBER" "$BRANCH" "$ASSIGNED_PR_STAGE" "repair-greptile-gate-${GREPTILE_GATE_STATUS}"
    record_terminal_outcome no_work "repair attempt for PR #${NUMBER} left Greptile findings without outcomes"
  elif [[ "$repair_verdict" == 'approve' ]]; then
    log "repair attempt for PR #${NUMBER} found no persisted change and reports ready; handing off for independent review"
    release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'repair-no-change-ready-handoff'
    record_terminal_outcome no_work "repair attempt for PR #${NUMBER} required no persisted change and handed off for review"
  else
    # Still post any outcomes (e.g. rebuttals) the agent recorded.
    greptile_outcome_gate '' || true
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
# Read the authoritative diff and record a compact proof, never its body.
# Appended source previously evicted semantic findings from the controller's
# bounded excerpt, turning legitimate REPAIR verdicts into useless handoffs.
# A failed diff read must not masquerade as inspection evidence.
if ! gh pr diff "$NUMBER" >/dev/null; then
  log "control_plane_failure: authoritative diff read failed for PR #${NUMBER}"
  release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'authoritative-diff-read-failed'
  record_terminal_outcome control_plane_failure "PR #${NUMBER} diff inspection unavailable"
  exit 2
fi
{
  printf '\n# comic-pile-factory-authoritative-diff-evidence\n'
  printf 'gh pr diff %s\n' "$NUMBER"
} >> "$sanitized_review_log"
# Retain the terminal token for diagnostics; the merge controller remains the
# only component authorized to decide whether a PR can merge.
last_token="$(factory_terminal_marker "$review_log" || true)"
log "agent terminal token for PR #${NUMBER}: ${last_token:-none}"

current_head="$(gh pr view "$NUMBER" --json headRefOid --jq .headRefOid 2>/dev/null || true)"
snapshot_failure="$(review_snapshot_failure_reason "$current_head" "$EXPECTED_HEAD" "$NUMBER")"
if [[ -n "$snapshot_failure" ]]; then
  log "${FACTORY_SEMANTIC_STATUS_HEAD_CHANGED}: PR #${NUMBER} review invalidated (expected ${EXPECTED_HEAD}, observed ${current_head:-unavailable}, or lease revoked)"
  # A revoked lease belongs to its new owner; never release that worker.
  if [[ "$snapshot_failure" != semantic-review-lease-lost ]]; then
    release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' "$snapshot_failure"
  fi
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
acceptance_policy="$(dirname "$TRUSTED_REVIEW_CONTROLLER")/factory_issue_acceptance.py"
# Obtain the trusted linked-issue rubric before checking primary evidence.
# A parseable report with only A1 is not a complete acceptance report.
acceptance_mission="$(factory_issue_acceptance_mission "$NUMBER" "$EXPECTED_HEAD")"
if [[ "$verdict" == 'approve' ]] \
  && ! factory_has_acceptance_report "$sanitized_review_log" "$acceptance_policy" "$acceptance_mission"; then
  log "review returned READY without a parseable acceptance report; requesting bounded evidence recovery"
  # A bare READY cannot be submitted to the controller if recovery times out.
  verdict=''
fi
recovery_attempted=0
recovery_status=0

# Fork reviews run tool-free from a data snapshot. The recovery pass starts a
# normal tool-enabled agent in this credentialed checkout, so it must never run
# for a fork: a prompt-injected contributor could otherwise fetch and execute
# its own branch here. A missing fork verdict is released for re-dispatch.
if [[ -z "$verdict" ]] && [[ "$SOURCE" != 'kilo-auto' ]] \
  && [[ "${FACTORY_FORK_STATIC_REVIEW:-0}" != 1 ]] \
  && factory_review_is_substantive "$review_log"; then
  recovery_attempted=1
  recovery_log="/tmp/opencode-factory-${WORKER}-verdict-recovery.log"
  recovery_timeout=90
  # The composite router may spend more time selecting an upstream model and
  # a full issue rubric is longer than the former one-item example. Keep a
  # single bounded recovery, but give this review-only route sufficient budget.
  if [[ "$RUNTIME_MODEL" == 'openrouter/free' ]]; then
    recovery_timeout=180
  fi
  available="$(remaining)"
  (( recovery_timeout > available - 45 )) && recovery_timeout=$((available - 45))

  if (( recovery_timeout >= 20 )); then
    log "semantic review needs a verdict or acceptance report; attempting one bounded same-session evidence recovery"
    recovery_status=0
    factory_recover_semantic_verdict "$RUNTIME_MODEL" "$GITHUB_WORKSPACE" "$recovery_timeout" "$recovery_log" \
      "$NUMBER" "$EXPECTED_HEAD" "$acceptance_mission" || recovery_status=$?

    current_head="$(gh pr view "$NUMBER" --json headRefOid --jq .headRefOid 2>/dev/null || true)"
    snapshot_failure="$(review_snapshot_failure_reason "$current_head" "$EXPECTED_HEAD" "$NUMBER")"
    if [[ -n "$snapshot_failure" ]]; then
      log "${FACTORY_SEMANTIC_STATUS_HEAD_CHANGED}: PR #${NUMBER} recovery invalidated (expected ${EXPECTED_HEAD}, observed ${current_head:-unavailable}, or lease revoked)"
      # A revoked lease belongs to its new owner; never release that worker.
      if [[ "$snapshot_failure" != semantic-review-lease-lost ]]; then
        release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' "$snapshot_failure"
      fi
      record_terminal_outcome control_plane_failure "PR #${NUMBER} head or lease changed during semantic verdict recovery"
      exit 0
    fi

    if (( recovery_status == 0 )); then
      sanitized_recovery_log="/tmp/opencode-factory-${WORKER}-verdict-recovery.sanitized.log"
      factory_sanitize_review_log "$recovery_log" "$sanitized_recovery_log"
      if factory_review_has_conflicting_terminal_markers "$sanitized_recovery_log"; then
        recovered_verdict=''
        log "${FACTORY_SEMANTIC_STATUS_RECOVERY_FAILED}: recovery returned contradictory terminal markers"
      else
        recovered_verdict="$(factory_extract_semantic_verdict "$sanitized_recovery_log" || true)"
      fi
      if [[ "$recovered_verdict" == 'repair' || "$recovered_verdict" == 'reject' ]]; then
        verdict="$recovered_verdict"
        log "${FACTORY_SEMANTIC_STATUS_RECOVERED}: recovered conservative verdict for PR #${NUMBER}"
      elif [[ "$recovered_verdict" == 'approve' ]] && ! factory_primary_review_denies_ready_recovery "$review_log"; then
        if factory_append_recovered_acceptance_report \
          "$sanitized_review_log" "$sanitized_recovery_log" "$acceptance_policy" "$EXPECTED_HEAD" "$acceptance_mission"; then
          verdict='approve'
          log "${FACTORY_SEMANTIC_STATUS_RECOVERED}: recovered approval with acceptance evidence for PR #${NUMBER}"
        else
          log "${FACTORY_SEMANTIC_STATUS_RECOVERY_FAILED}: recovery approval lacks a unique current-head acceptance report"
        fi
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

# Wrapper gates on approval. A push blocked by the fast checks, or an open
# Greptile finding without a recorded outcome, can never be declared ready.
if [[ "$verdict" == 'approve' && -n "$FACTORY_PUSH_BLOCKED_REASON" ]]; then
  log "downgrading approval for PR #${NUMBER}: the agent's edits were not pushed (${FACTORY_PUSH_BLOCKED_REASON})"
  append_wrapper_gate_reason "the agent edited the branch but its diff failed the pre-push fast checks and was not pushed (see the fast-check comment)."
  verdict='repair'
elif [[ "$verdict" == 'approve' ]] && ! greptile_outcome_gate ''; then
  case "$GREPTILE_GATE_STATUS" in
    new-findings)
      log "greptile outcome gate: ${GREPTILE_GATE_DETAIL}; releasing PR #${NUMBER} for re-dispatch instead of declaring ready"
      release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:changes-requested' 'new-greptile-findings-during-run'
      record_terminal_outcome no_work "PR #${NUMBER} received new Greptile findings during the review run"
      log "assignment complete; remaining budget $(remaining)s"
      exit 0
      ;;
    unknown)
      log "greptile outcome gate: ${GREPTILE_GATE_DETAIL}; releasing PR #${NUMBER} for another review attempt"
      release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'greptile-findings-fetch-failed'
      record_terminal_outcome control_plane_failure "PR #${NUMBER} Greptile findings could not be fetched before the gate"
      log "assignment complete; remaining budget $(remaining)s"
      exit 0
      ;;
  esac
  log "downgrading approval for PR #${NUMBER}: ${GREPTILE_GATE_DETAIL}"
  append_wrapper_gate_reason "${GREPTILE_GATE_DETAIL}"
  verdict='repair'
elif [[ "$verdict" != 'approve' ]]; then
  # Post any outcomes the agent recorded even when it did not approve.
  greptile_outcome_gate '' || true
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
# Trusted review marker/comment stayed on github-actions[bot] above. Worker 48
# may additionally post a plain-prose review handoff as its App. Never widen
# TRUSTED_FACTORY_APP_SLUGS and never pass the App token into the controller.
post_readable_handoff review "$NUMBER" "$EXPECTED_HEAD" "Semantic verdict: ${verdict}."
record_terminal_outcome success "PR #${NUMBER} semantic verdict completed through the trusted review controller"

log "assignment complete; remaining budget $(remaining)s"
