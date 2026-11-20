#!/usr/bin/env bash
set -Eeuo pipefail

WORKER="${FACTORY_WORKER:?FACTORY_WORKER is required}"
SOURCE="${FACTORY_SOURCE:?FACTORY_SOURCE is required}"
MODEL="${FACTORY_MODEL:?FACTORY_MODEL is required}"
RUNTIME_MODEL="${FACTORY_RUNTIME_MODEL:?FACTORY_RUNTIME_MODEL is required}"
DISPLAY="${FACTORY_DISPLAY:-$MODEL}"
BRANCH_SUFFIX="${FACTORY_BRANCH_SUFFIX:-free-model}"
OWNER="factory:${WORKER}"
WORKER_ID="opencode-free-model-factory-${WORKER}"
BUDGET_SECONDS="${FACTORY_BUDGET_SECONDS:-6000}"
MAX_AGENT_ATTEMPTS="${FREE_MODEL_AGENT_MAX_ATTEMPTS:-2}"
TRANSIENT_BACKOFF_SECONDS="${FREE_MODEL_TRANSIENT_BACKOFF_SECONDS:-20}"
STARTED="$(date +%s)"
DEADLINE=$((STARTED + BUDGET_SECONDS))
OWNER_RE='^factory:(unowned|local|[1-9][0-9]*)$'
STAGE_RE='^factory:(building|review|changes-requested|ci|ready|blocked)$'
SKIP_PRS=()

# Stage the trusted review-findings helper from main before any PR checkout.
# shellcheck source=factory-review-findings.sh
if [[ -f "$(dirname "${BASH_SOURCE[0]}")/factory-review-findings.sh" ]]; then
  source "$(dirname "${BASH_SOURCE[0]}")/factory-review-findings.sh"
else
  source .github/scripts/factory-review-findings.sh
fi
# Deploy dispatch after a GITHUB_TOKEN merge (see factory-dispatch-production-deploy.sh).
# shellcheck source=factory-dispatch-production-deploy.sh
if [[ -f "$(dirname "${BASH_SOURCE[0]}")/factory-dispatch-production-deploy.sh" ]]; then
  source "$(dirname "${BASH_SOURCE[0]}")/factory-dispatch-production-deploy.sh"
else
  source .github/scripts/factory-dispatch-production-deploy.sh
fi
# Trusted pre-push fast checks, staged the same way (see factory-fast-checks.sh).
# shellcheck source=factory-fast-checks.sh
if [[ -f "$(dirname "${BASH_SOURCE[0]}")/factory-fast-checks.sh" ]]; then
  source "$(dirname "${BASH_SOURCE[0]}")/factory-fast-checks.sh"
else
  source .github/scripts/factory-fast-checks.sh
fi
# Canonical exact-head merge route (see factory-canonical-merge-route.sh).
# Loaded before any PR checkout so worker-direct merges only ever execute the
# trusted controller gate and the trusted post-merge closure policy.
# shellcheck source=factory-canonical-merge-route.sh
if [[ -f "$(dirname "${BASH_SOURCE[0]}")/factory-canonical-merge-route.sh" ]]; then
  source "$(dirname "${BASH_SOURCE[0]}")/factory-canonical-merge-route.sh"
else
  source .github/scripts/factory-canonical-merge-route.sh
fi
FACTORY_PUSH_BLOCKED_REASON=''

log() { printf '[factory:%s][source:%s][model:%s] %s\n' "$WORKER" "$SOURCE" "$MODEL" "$*"; }
remaining() { echo $((DEADLINE - $(date +%s))); }
contains_skip_pr() {
  local needle="$1" item
  for item in "${SKIP_PRS[@]:-}"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

stage_trusted_guard() {
  # The worker starts on the trusted main checkout. Copy the guard to a
  # stable location BEFORE any checkout_target switches onto an adopted PR
  # branch, then invoke only this copy for pre-push decisions. A stale PR
  # branch copy of fixed-model-guard.py can never downgrade the decision.
  TRUSTED_GUARD="${TRUSTED_GUARD:-}"
  if [[ -z "$TRUSTED_GUARD" ]]; then
    TRUSTED_GUARD="$(mktemp /tmp/comic-pile-fixed-model-guard.XXXXXX.py)"
  fi
  cp .github/scripts/fixed-model-guard.py "$TRUSTED_GUARD"
  chmod +x "$TRUSTED_GUARD"
  python3 "$TRUSTED_GUARD" --self-test >/dev/null 2>&1
}

replace_labels() {
  local number="$1" owner="$2" stage="$3"
  local labels target
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  target="$(jq -c --arg owner "$owner" --arg stage "$stage" --arg owner_re "$OWNER_RE" --arg stage_re "$STAGE_RE" '
    map(select((test($owner_re)|not) and (test($stage_re)|not) and . != "factory"))
    + ["factory", $owner, $stage] | unique' <<< "$labels")"
  gh api --method PUT "repos/${GITHUB_REPOSITORY}/issues/${number}/labels" \
    --input - <<< "{\"labels\":${target}}" >/dev/null
}

current_stage() {
  local number="$1" fallback="$2" labels stage
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  stage="$(jq -r --arg stage_re "$STAGE_RE" '[.[] | select(test($stage_re))][0] // empty' <<< "$labels")"
  if [[ -n "$stage" ]]; then printf '%s\n' "$stage"; else printf '%s\n' "$fallback"; fi
}

current_owner_is_self() {
  local number="$1" labels
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  jq -e --arg owner "$OWNER" 'index($owner) != null' >/dev/null <<< "$labels"
}

pr_no_diff_generation_fields() {
  local number="$1" stage="$2" prefer_live="${3:-0}"
  local sha="" conflicted=0 meta='{}' mergeable='' merge_state=''
  if [[ "$prefer_live" != 1 ]]; then
    if [[ -n "${PR_HEAD_REPOSITORY:-}" && "$PR_HEAD_REPOSITORY" != "${GITHUB_REPOSITORY:-}" ]]; then
      sha="${EXPECTED_HEAD:-}"
    else
      sha="$(git rev-parse HEAD 2>/dev/null || true)"
    fi
  fi
  meta="$(gh pr view "$number" --json headRefOid,mergeable,mergeStateStatus 2>/dev/null || true)"
  [[ -n "$meta" ]] || meta='{}'
  if [[ ! "$sha" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    sha="$(jq -r '.headRefOid // empty' <<<"$meta" 2>/dev/null || true)"
  fi
  mergeable="$(jq -r '.mergeable // empty' <<<"$meta" 2>/dev/null || true)"
  merge_state="$(jq -r '.mergeStateStatus // empty' <<<"$meta" 2>/dev/null || true)"
  mergeable="${mergeable^^}"
  merge_state="${merge_state^^}"
  if [[ "$mergeable" == 'CONFLICTING' || "$merge_state" == 'DIRTY' ]]; then
    conflicted=1
  fi
  if [[ ! "$sha" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    return 1
  fi
  printf ':sha=%s:stage=%s:conflicted=%s' "${sha,,}" "$stage" "$conflicted"
}

release_target() {
  local number="$1" fallback_stage="$2" reason="$3" target_kind="${4:-target}"
  local stage epoch marker generation="" bounded=0
  case "$reason" in
    no-persisted-change-handoff|repair-no-persisted-change-handoff) bounded=1 ;;
    transient-model-interruption|review-agent-failed|controller-assignment-read-failed|semantic-review-head-read-failed|missing-semantic-verdict|conflicting-semantic-verdict|authoritative-diff-read-failed|fork-repair-execution-failed|fork-repair-result-invalid|fork-dispatch-failed)
      [[ "$target_kind" != pr ]] || bounded=1
      ;;
  esac
  case "${target_kind}:${reason}" in
    pr:repair-no-change-ready-handoff|pr:repair-no-persisted-change-handoff)
      stage="$fallback_stage"
      ;;
    *)
      if [[ "$fallback_stage" == 'factory:blocked' ]]; then
        # A gate denial (deny decision) is not a stage-preservation case: the
        # caller truthfully downgraded the PR to factory:blocked so it does
        # not get repeated work. current_stage would preserve the stale live
        # stage label, silently discarding the denial.
        stage="$fallback_stage"
      else
        stage="$(current_stage "$number" "$fallback_stage")"
      fi
      ;;
  esac
  epoch="$(date +%s)"
  if (( bounded )); then
    if [[ "$target_kind" == 'pr' ]]; then
      local prefer_live=0
      [[ "$reason" != controller-assignment-read-failed ]] || prefer_live=1
      if ! generation="$(pr_no_diff_generation_fields "$number" "$stage" "$prefer_live")"; then
        FACTORY_RETAIN_LEASE_ON_EXIT=1
        log "unable to pin no-diff generation to head SHA for pr #${number}; retaining the lease"
        return 1
      fi
    fi
  fi
  marker="<!-- comic-pile-factory-claim-released-v3:${target_kind}-${number}:${WORKER_ID}:${epoch}:${reason}${generation} -->"
  if (( bounded )); then
    if ! gh issue comment "$number" --body "$marker" >/dev/null 2>&1; then
      FACTORY_RETAIN_LEASE_ON_EXIT=1
      log "unable to persist bounded no-diff marker for ${target_kind} #${number}; retaining the lease"
      return 1
    fi
    # Retry exhaustion is an execution-control concern. Preserve the truthful
    # workflow stage and let the controller suppress re-selection from the
    # durable marker; never rewrite no-diff failures as factory:blocked.
    replace_labels "$number" 'factory:unowned' "$stage"
    if [[ "$reason" != *no-persisted-change-handoff ]]; then
      log "review retry recorded for PR #${number}${generation}; after three failures this generation cools down for 30 minutes"
    fi
  else
    replace_labels "$number" 'factory:unowned' "$stage"
    gh issue comment "$number" --body "$marker" >/dev/null 2>&1 || true
  fi
  log "released ${target_kind} #${number} to factory:unowned at ${stage} (${reason})"
}

release_owned_targets() {
  local reason="$1" number stage
  if [[ -n "${FACTORY_ASSIGNMENT:-}" ]]; then
    # The separate trusted cleanup job holds the original claim UUID. Never
    # discover or release another assignment through a worker-wide label scan.
    log 'deferring general lease release to token-bound trusted cleanup'
    return 0
  fi
  if [[ "${FACTORY_RETAIN_LEASE_ON_EXIT:-0}" == '1' && "$reason" == 'session-end-handoff' ]]; then
    log 'retaining current lease because the bounded no-diff marker could not be persisted'
    return 0
  fi
  while IFS= read -r number; do
    [[ -n "$number" ]] || continue
    stage="$(current_stage "$number" 'factory:building')"
    release_target "$number" "$stage" "$reason" 'issue'
  done < <(gh issue list --state open --limit 300 --label "$OWNER" --json number --jq '.[].number')

  while IFS= read -r number; do
    [[ -n "$number" ]] || continue
    stage="$(current_stage "$number" 'factory:review')"
    release_target "$number" "$stage" "$reason" 'pr'
  done < <(gh pr list --state open --limit 200 --label "$OWNER" --json number --jq '.[].number')
}

linked_issue_from_branch() {
  sed -nE 's#^factory/[0-9]+-([0-9]+)-.*$#\1#p' <<< "$1"
}

# Use the same trusted linkage policy as the controller, including local PRs.
# The policy is staged from main before switching to the assigned branch.
linked_issue_from_pr() {
  local pr_json
  pr_json="$(gh pr view "$1" --json headRefName,body,title)" || return 3
  printf '%s' "$pr_json" | python3 -c '
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from factory_work_policy import linked_issue_from_pr
issue = linked_issue_from_pr(json.load(sys.stdin))
print(issue if issue is not None else "")
' "$TRUSTED_REVIEW_CONTROLLER"
}

issue_has_open_factory_pr() {
  local issue="$1"
  gh pr list --state open --limit 300 --json headRefName,body | jq -e --arg issue "$issue" '
    any(.[];
      (.headRefName | test("^factory/[0-9]+-" + $issue + "-"))
      or (.headRefName | test("^factory/" + $issue + "-"))
      or ((.body // "") | test("(?im)(closes|fixes|resolves|implements|part of)[[:space:]]+#" + $issue + "([^0-9]|$)"))
    )' >/dev/null
}

release_pr_and_issue() {
  local pr="$1" branch="$2" stage="$3" reason="$4" issue state
  release_target "$pr" "$stage" "$reason" 'pr'
  issue="$(linked_issue_from_pr "$pr")" || return 3
  if [[ -n "$issue" ]]; then
    state="$(gh issue view "$issue" --json state --jq .state 2>/dev/null || true)"
    if [[ "$state" == 'OPEN' ]] && current_owner_is_self "$issue"; then
      release_target "$issue" "$stage" "$reason" 'issue'
    fi
  fi
}

ensure_owner_label() {
  if ! gh api "repos/${GITHUB_REPOSITORY}/labels/factory%3A${WORKER}" >/dev/null 2>&1; then
    gh api --method POST "repos/${GITHUB_REPOSITORY}/labels" \
      -f name="$OWNER" \
      -f color='5319e7' \
      -f description="Owned by fixed-model Factory ${WORKER}: ${DISPLAY}" >/dev/null
    log "created ${OWNER}"
  fi
}

choose_existing_pr() {
  gh pr list --state open --limit 200 --json number,labels,updatedAt | jq -r \
    --arg owner "$OWNER" '
      map(select(([.labels[].name] | index($owner)) != null))
      | sort_by(.updatedAt)
      | .[].number'
}

choose_unowned_pr() {
  gh pr list --state open --limit 200 --label 'factory:unowned' --json number,isDraft,updatedAt | jq -r '
    map(select(.isDraft == false)) | sort_by(.updatedAt) | .[].number'
}

choose_issue() {
  local labels=("$@") candidate
  local args=(issue list --state open --limit 300 --json number,labels,createdAt)
  local label
  for label in "${labels[@]}"; do args+=(--label "$label"); done
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    issue_has_open_factory_pr "$candidate" && continue
    printf '%s\n' "$candidate"
  done < <(gh "${args[@]}" | jq -r --arg owner_re "$OWNER_RE" '
    map(select(.number != 679 and .number != 1093 and .number != 1109))
    | map(select((([.labels[].name | select(test($owner_re) and . != "factory:unowned")] | length) == 0)))
    | sort_by(.createdAt) | reverse | .[].number')
}

choose_backlog_zero_child() {
  gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/679/sub_issues?per_page=100" 2>/dev/null \
    | jq -s -r --arg owner_re "$OWNER_RE" '
      add
      | map(select(.state == "open"))
      | map(select((([.labels[].name | select(test($owner_re) and . != "factory:unowned")] | length) == 0)))
      | sort_by(.number)
      | .[].number' || true
}

claim_issue() {
  local number="$1" labels target epoch marker
  issue_has_open_factory_pr "$number" && return 1
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  if jq -e --arg owner_re "$OWNER_RE" \
    '[.[] | select(test($owner_re) and . != "factory:unowned")] | length > 0' \
    >/dev/null <<< "$labels"; then
    return 1
  fi
  target="$(jq -c --arg owner "$OWNER" --arg owner_re "$OWNER_RE" --arg stage_re "$STAGE_RE" '
    map(select((test($owner_re)|not) and (test($stage_re)|not) and . != "factory"))
    + ["factory", $owner, "factory:building"] | unique' <<< "$labels")"
  gh api --method PUT "repos/${GITHUB_REPOSITORY}/issues/${number}/labels" \
    --input - <<< "{\"labels\":${target}}" >/dev/null
  epoch="$(date +%s)"
  marker="<!-- comic-pile-factory-implement-claim-v3:issue-${number}:${WORKER_ID}:${epoch}:attempt-1 -->"
  gh issue comment "$number" --body "$marker" >/dev/null 2>&1 || true
}

claim_unowned_pr() {
  local number="$1" labels owner_count
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  jq -e 'index("factory:unowned") != null' >/dev/null <<< "$labels" || return 1
  owner_count="$(jq -r --arg owner_re "$OWNER_RE" \
    '[.[] | select(test($owner_re) and . != "factory:unowned")] | length' <<< "$labels")"
  [[ "$owner_count" == "0" ]] || return 1

  replace_labels "$number" "$OWNER" 'factory:review'
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  jq -e --arg owner "$OWNER" 'index($owner) != null' >/dev/null <<< "$labels" || return 1

  gh issue comment "$number" --body "$(printf '<!-- free-model-factory-owner:%s -->\nFactory %s adopted this unowned PR with fixed model %s via %s.\n' \
    "$WORKER" "$WORKER" "$MODEL" "$SOURCE")" >/dev/null
}

record_pr_provenance() {
  local pr="$1" issue="$2" labels head epoch marker
  if ! labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${issue}/labels?per_page=100" --jq '[.[].name]' 2>/dev/null)"; then
    log "unable to read issue #${issue} ownership before PR provenance; refusing handoff" >&2
    return 1
  fi
  if ! jq -e --arg owner "$OWNER" 'index($owner) != null' >/dev/null <<< "$labels"; then
    log "${OWNER} no longer owns issue #${issue}; refusing stale PR provenance" >&2
    return 1
  fi
  head="$(git rev-parse HEAD)"
  epoch="$(date +%s)"
  marker="<!-- comic-pile-factory-pr-provenance-v1:pr-${pr}:issue-${issue}:${WORKER_ID}:${epoch}:${head} -->"
  if ! gh issue comment "$pr" --body "$marker" >/dev/null 2>&1; then
    log "unable to persist provenance for PR #${pr}; refusing handoff" >&2
    return 1
  fi
}

stage_trusted_review_controller() {
  # Review independence is only meaningful when the contributor record is written
  # by trusted controller code, so copy the controller and its policy module to a
  # stable location BEFORE any checkout_target switches onto an adopted PR
  # branch. A stale PR-branch copy must never be able to record provenance.
  if [[ -z "${TRUSTED_REVIEW_CONTROLLER:-}" ]]; then
    local trusted_dir
    trusted_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/comic-pile-review-controller.XXXXXX")"
    cp .github/scripts/factory-review-controller.py "$trusted_dir/factory-review-controller.py"
    cp .github/scripts/factory_review_policy.py "$trusted_dir/factory_review_policy.py"
  cp .github/scripts/factory_issue_acceptance.py "$trusted_dir/factory_issue_acceptance.py"
  cp .github/scripts/factory_implementation_delivery.py "$trusted_dir/factory_implementation_delivery.py"
    cp .github/scripts/factory_post_merge_closure.py "$trusted_dir/factory_post_merge_closure.py"
    cp scripts/factory_eligibility.py "$trusted_dir/factory_eligibility.py"
    # Bind model provenance to a trusted main checkout snapshot, not PR data.
    cp .github/scripts/factory_worker_slots.py "$trusted_dir/factory_worker_slots.py"
    cp .github/scripts/factory_provider_registry.py "$trusted_dir/factory_provider_registry.py"
    chmod +x "$trusted_dir/factory-review-controller.py"
    TRUSTED_REVIEW_CONTROLLER="$trusted_dir/factory-review-controller.py"
    export TRUSTED_REVIEW_CONTROLLER
  fi
}

record_head_contribution() {
  # Every push that changes a PR head must bind this worker to that exact head,
  # including when the worker leaves no resume packet or other comment. Losing
  # this record is not fatal here: review independence then fails closed and the
  # head needs two distinct reviewers instead.
  local pr="$1" reason="$2" head
  stage_trusted_review_controller
  head="$(git rev-parse HEAD)"
  if ! python3 "$TRUSTED_REVIEW_CONTROLLER" record-contribution \
    --worker "$WORKER" --pr "$pr" --head "$head" >/dev/null; then
    log "unable to record trusted contributor provenance for PR #${pr} (${reason}); review will require two distinct reviewers" >&2
    return 1
  fi
}

checkout_target() {
  local mode="$1" number="$2" branch="$3" pr_json head
  PR_HEAD_REPOSITORY=''
  git fetch --prune origin
  git switch --detach origin/main >/dev/null 2>&1 || true
  git reset --hard origin/main >/dev/null
  git clean -fd >/dev/null
  if [[ "$mode" == pr ]]; then
    # PR refs exist in the base repository even when the head is on a fork.
    # Never fall back to main (or an unrelated same-named base branch).
    pr_json="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${number}")" || return 1
    PR_HEAD_REPOSITORY="$(jq -er '.head.repo.full_name' <<< "$pr_json")" || return 1
    [[ "$PR_HEAD_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
    [[ "$(jq -r '.base.repo.full_name' <<< "$pr_json")" == "$GITHUB_REPOSITORY" ]] || return 1
    if [[ "$PR_HEAD_REPOSITORY" != "$GITHUB_REPOSITORY" ]]; then
      log 'refusing fork checkout in a credentialed worker; use the secretless execution workflow' >&2
      return 1
    fi
    head="$(jq -er '.head.sha' <<< "$pr_json")" || return 1
    [[ "$head" =~ ^[a-f0-9]{40}$ ]] || return 1
    git fetch origin "refs/pull/${number}/head" || return 1
    [[ "$(git rev-parse FETCH_HEAD)" == "$head" ]] || return 1
    git switch -C "$branch" "$head" || return 1
  elif git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    git fetch origin "$branch:$branch" --force
    git switch "$branch"
    git reset --hard "origin/$branch" >/dev/null
  else
    git switch -C "$branch" origin/main
  fi
  EXPECTED_HEAD="$(git rev-parse HEAD)"
  log "checked out ${mode} #${number} on ${branch} at ${EXPECTED_HEAD}"
}

current_head_review_blockers() {
  local pr="$1" head="$2"
  local changes unresolved
  changes="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews?per_page=100" \
    | jq -s --arg head "$head" '[.[][] | select(.state == "CHANGES_REQUESTED" and .commit_id == $head)] | length')"
  [[ "$changes" == "0" ]] || return 1
  # Pages through every review thread; fails closed when a page is missing
  # or pagination is incomplete.
  unresolved="$(factory_count_unresolved_review_threads "$pr")" || return 1
  [[ "$unresolved" == "0" ]]
}

machine_merge_gates_pass() {
  local pr="$1" expected_head="$2" info head
  info="$(gh pr view "$pr" --json state,isDraft,mergeable,headRefOid)"
  [[ "$(jq -r .state <<< "$info")" == "OPEN" ]] || return 1
  [[ "$(jq -r .isDraft <<< "$info")" == "false" ]] || return 1
  [[ "$(jq -r .mergeable <<< "$info")" == "MERGEABLE" ]] || return 1
  head="$(jq -r .headRefOid <<< "$info")"
  [[ "$head" == "$expected_head" ]] || return 1
  gh pr checks "$pr" --required >/tmp/factory-required-checks 2>&1 || return 1
  current_head_review_blockers "$pr" "$head" || return 1
}

is_transient_agent_failure() {
  local status="$1"
  [[ "$status" == "124" ]] && return 0
  grep -Eiq '429|Too Many Requests|rate.?limit|overloaded|temporar(il)?y unavailable|cooling down|model_cooldown|stream_early_eof|stream[^[:space:]]*[[:space:]]+timeout|stream_timeout|STREAM_READINESS_TIMEOUT|bad gateway|gateway timeout|service unavailable|HTTP[^0-9]*(502|503|504)|ECONNRESET|ETIMEDOUT|connection reset' \
    "/tmp/opencode-factory-${WORKER}.log"
}

is_model_cooldown_failure() {
  grep -Eiq 'cooling down|model_cooldown' "/tmp/opencode-factory-${WORKER}.log"
}

completion_action_for_stage() {
  case "${1:-}" in
    factory:conflict) printf '%s' 'merge-conflict' ;;
    factory:ci) printf '%s' 'ci-repair' ;;
    factory:changes-requested) printf '%s' 'review-findings' ;;
    factory:review) printf '%s' 'review' ;;
    *) printf '' ;;
  esac
}

completion_action_matches_stage() {
  case "${1:-}:${2:-}" in
    merge-conflict:factory:conflict|merge-conflict:factory:changes-requested) return 0 ;;
    ci-repair:factory:ci|ci-repair:factory:changes-requested) return 0 ;;
    review-findings:factory:changes-requested) return 0 ;;
    review:factory:review) return 0 ;;
    *) return 1 ;;
  esac
}

trusted_completion_action_line() {
  [[ -n "${JEV_ACTION:-}" ]] || return 1
  [[ -n "${JEV_ACTION_HEAD:-}" ]] || return 1
  [[ -n "${EXPECTED_HEAD:-}" && "${JEV_ACTION_HEAD}" == "${EXPECTED_HEAD}" ]] || return 1
  completion_action_matches_stage "${JEV_ACTION}" "${ASSIGNED_PR_STAGE:-}" || return 1
  printf 'The trusted factory decision for this lease selected the %s action at exact head %s. Focus this session on that action and do not re-plan unrelated PR work.' "$JEV_ACTION" "$JEV_ACTION_HEAD"
}

run_agent() {
  local mode="$1" number="$2" timeout_seconds="$3"
  local target mission prompt findings status=0
  if [[ "$mode" == "pr" ]]; then
    target="pull request #${number}"
    mission="Resume this PR. Inspect the exact current head, required CI, review submissions, and every inline review thread. Greptile (greptile-apps[bot]) review findings are HIGH PRIORITY, must-address feedback: fix each open one in code or rebut it with concrete evidence, and record every outcome in the outcome file described below; the wrapper posts those outcomes as the thread replies and blocks FACTORY_GATE_READY while any open Greptile finding lacks one. Never resolve a Greptile thread yourself. Fix closure-critical defects and resolve or concretely rebut actionable threads. If no edits are required, decide whether the PR fully completes its declared scope and is safe to merge. End your final response with FACTORY_GATE_READY only when no semantic blocker remains; otherwise end with FACTORY_GATE_NOT_READY."
    findings="$(factory_review_findings_mission "$number" "${EXPECTED_HEAD:-$(git rev-parse HEAD)}")"
    mission+=" ${findings}"
    if completion_line="$(trusted_completion_action_line)"; then
      mission+=" ${completion_line}"
    else
      log "no trusted Jev completion action for PR #${number} (action=${JEV_ACTION:-none}, head=${JEV_ACTION_HEAD:-none}, stage=${ASSIGNED_PR_STAGE:-none}); using generic PR mission"
    fi
  else
    target="issue #${number}"
    mission="Implement the full closure-critical acceptance contract for this issue with code and focused tests. Do not stop at planning or optional polish."
  fi

  if [[ "$mode" == 'pr' && -n "${FACTORY_FAST_CHECK_FEEDBACK_FILE:-}" ]]; then
    mission+=" This is a same-lease fast-check repair, not a fresh review. The prior unpushed edits are still in this worktree. Read the local trusted fast-check log at ${FACTORY_FAST_CHECK_FEEDBACK_FILE}, correct the concrete failures while retaining valid changes, and run focused validation. The wrapper will independently rerun scope and pre-push checks. Do not commit or push."
  fi

  prompt="Read AGENTS.md, docs/ISSUE_EXECUTION_PROTOCOL.md, docs/AUTONOMOUS_FACTORY_POLICY.md, docs/CHATGPT_FACTORY_PROMPT.md, and docs/FACTORY_GITHUB_VISIBILITY.md first. Follow the canonical product-first factory policy.  Edit only the assigned target, run focused validation, and use GitHub for review context. Do not commit or push; the wrapper persists changes. OmniRoute may switch upstream models, providers, or routes within the configured policy; do not bypass OmniRoute or request paid or unconfigured capacity. Do not enable auto-merge, push main, touch production databases, or alter automation schedules. Run context: Worker: ${WORKER_ID}; source: ${SOURCE}; model: ${MODEL}; runtime: ${RUNTIME_MODEL}; target: ${target}. ${mission}"

  if [[ "$SOURCE" == 'kilo-auto' ]]; then
    # Do not toggle errexit: callers capture this status under `set +e`.
    bash .github/scripts/kilo-auto-factory-run.sh \
      "$timeout_seconds" \
      "ComicPile Factory ${WORKER} · ${DISPLAY}" \
      "$prompt" \
      "/tmp/opencode-factory-${WORKER}.log" || status=$?
    return "${status:-0}"
  fi

  timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" \
    opencode run --standalone -m "$RUNTIME_MODEL" --agent build \
    "$prompt" \
    2>&1 | tee "/tmp/opencode-factory-${WORKER}.log"
  status=${PIPESTATUS[0]}
  return "$status"
}

target_scope_text() {
  local mode="$1" number="$2" issue title body
  if [[ "$mode" == 'issue' ]]; then
    gh issue view "$number" --json title,body --jq '{title,body}'
    return 0
  fi
  title="$(gh pr view "$number" --json title --jq .title)"
  body="$(gh pr view "$number" --json body --jq .body)"
  issue="$(linked_issue_from_pr "$number")" || return 3
  if [[ -n "$issue" ]]; then
    title="$(gh issue view "$issue" --json title --jq .title)"
    body="$(gh issue view "$issue" --json body --jq .body)"
  fi
  jq -nc --arg title "$title" --arg body "$body" '{title:$title,body:$body}'
}

changed_paths_json() {
  local base_ref="$1"
  {
    git diff --name-only --relative "$base_ref"
    git ls-files --others --exclude-standard
  } | jq -R -s 'split("\n") | map(select(length > 0)) | unique'
}

conflict_markers_present() {
  local file
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    [[ -f "$file" ]] || continue
    if grep -qE '^(<<<<<<< |>>>>>>> |=======$)' "$file" 2>/dev/null; then
      return 0
    fi
  done < <(git diff --name-only --diff-filter=ACMRTUXB HEAD; git ls-files --others --exclude-standard)
  return 1
}

branch_folded_main_commits() {
  local ours theirs shared
  ours="$(git rev-list "$EXPECTED_HEAD"..HEAD 2>/dev/null || true)"
  theirs="$(git rev-list "$EXPECTED_HEAD"..origin/main 2>/dev/null || true)"
  [[ -n "$ours" && -n "$theirs" ]] || return 1
  shared="$(comm -12 <(printf '%s\n' "$ours" | sort) <(printf '%s\n' "$theirs" | sort))"
  [[ -n "$shared" ]]
}

unclean_git_state_json() {
  local merge_head cherry_pick_head revert_head
  local merge_in_progress=false cherry_pick_in_progress=false revert_in_progress=false
  local unmerged_entries=false conflict_markers=false head_changed=false foreign_main_commits=false
  merge_head="$(git rev-parse -q --verify MERGE_HEAD 2>/dev/null || true)"
  [[ -z "$merge_head" ]] || merge_in_progress=true
  cherry_pick_head="$(git rev-parse -q --verify CHERRY_PICK_HEAD 2>/dev/null || true)"
  [[ -z "$cherry_pick_head" ]] || cherry_pick_in_progress=true
  revert_head="$(git rev-parse -q --verify REVERT_HEAD 2>/dev/null || true)"
  [[ -z "$revert_head" ]] || revert_in_progress=true
  [[ -z "$(git ls-files -u)" ]] || unmerged_entries=true
  if conflict_markers_present; then conflict_markers=true; fi
  [[ "$(git rev-parse HEAD)" == "$EXPECTED_HEAD" ]] || head_changed=true
  if branch_folded_main_commits; then foreign_main_commits=true; fi
  jq -nc \
    --argjson merge_in_progress "$merge_in_progress" \
    --argjson cherry_pick_in_progress "$cherry_pick_in_progress" \
    --argjson revert_in_progress "$revert_in_progress" \
    --argjson unmerged_entries "$unmerged_entries" \
    --argjson conflict_markers "$conflict_markers" \
    --argjson head_changed "$head_changed" \
    --argjson foreign_main_commits "$foreign_main_commits" \
    '{merge_in_progress:$merge_in_progress,cherry_pick_in_progress:$cherry_pick_in_progress,revert_in_progress:$revert_in_progress,unmerged_entries:$unmerged_entries,conflict_markers:$conflict_markers,head_changed:$head_changed,foreign_main_commits:$foreign_main_commits}'
}

reject_out_of_scope_diff() {
  local mode="$1" number="$2" base_ref="$3" scope previous latest git_state decision reason
  scope="$(target_scope_text "$mode" "$number")"
  if [[ "$mode" == 'pr' ]]; then
    previous="$(gh api "repos/${GITHUB_REPOSITORY}/compare/$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${number}" --jq .base.sha)...${base_ref}" \
      --jq '[.files[]?.filename] | unique')"
  else
    previous='[]'
  fi
  latest="$(changed_paths_json "$base_ref")"
  git_state="$(unclean_git_state_json)"
  decision="$(python3 "$TRUSTED_GUARD" \
    --previous-json "$previous" \
    --latest-json "$latest" \
    --git-state "$git_state" \
    --title "$(jq -r .title <<< "$scope")" \
    --body "$(jq -r .body <<< "$scope")")"
  if [[ "$(jq -r .reject <<< "$decision")" != true ]]; then
    return 0
  fi
  reason="$(jq -r .reason <<< "$decision")"
  log "rejecting out-of-scope ${mode} #${number} diff before push (${reason}): git_state=$(jq -c .git_state <<< "$decision") factory_control_files=$(jq -c .factory_control_files <<< "$decision")"
  git reset --hard "$base_ref" >/dev/null
  git clean -fd >/dev/null
  return 1
}

factory_push_credential() {
  # Print "<source>\t<token>" for push and PR creation. Worker 48 gets its
  # installation token when the mapping and secrets exist; every other worker
  # keeps PR_REBASE_TOKEN. The shared producer is not removed.
  # The minted installation token is not a repository secret, so Actions will
  # not mask it on its own: register ::add-mask:: before any other use. Xtrace
  # is suspended so `set -x` can never echo it, and nothing here writes it to
  # git config or logs.
  local had_xtrace=0 resolved source token
  [[ $- == *x* ]] && had_xtrace=1
  { set +x; } 2>/dev/null
  if ! resolved="$(bash .github/scripts/factory-worker-push-token.sh resolve)"; then
    echo 'factory push credential could not be resolved' >&2
    (( had_xtrace )) && set -x
    return 1
  fi
  source="${resolved%%$'\t'*}"
  token="${resolved#*$'\t'}"
  if [[ -z "$token" || "$token" == "$resolved" ]]; then
    echo 'factory push credential was empty' >&2
    (( had_xtrace )) && set -x
    return 1
  fi
  if [[ "$source" == installation ]]; then
    # Workflow commands are read from stderr as well as stdout; stdout here is
    # captured by the caller.
    printf '::add-mask::%s\n' "$token" >&2
    if [[ -z "${GITHUB_REPOSITORY:-}" ]]; then
      echo 'GITHUB_REPOSITORY is required to push with a worker installation token' >&2
      (( had_xtrace )) && set -x
      return 1
    fi
  fi
  printf '%s\t%s' "$source" "$token"
  (( had_xtrace )) && set -x
  return 0
}

factory_git_push() {
  # Usage: factory_git_push SOURCE TOKEN <git push args...>
  # PR_REBASE_TOKEN pushes use the remote the workflow already configured.
  # An installation-token push swaps the origin URL only for this one push and
  # always restores the previous URL, so the token is never left in git config.
  local source="$1" token="$2" had_xtrace=0 previous_url status=0
  shift 2
  if [[ "$source" != installation && -z "${FACTORY_PUSH_REPOSITORY:-}" ]]; then
    git push "$@"
    return $?
  fi
  [[ $- == *x* ]] && had_xtrace=1
  { set +x; } 2>/dev/null
  previous_url="$(git remote get-url origin 2>/dev/null || true)"
  git remote set-url origin "https://x-access-token:${token}@github.com/${FACTORY_PUSH_REPOSITORY:-$GITHUB_REPOSITORY}.git" || status=$?
  if (( status == 0 )); then
    git push "$@" || status=$?
  fi
  if [[ -n "$previous_url" ]]; then
    git remote set-url origin "$previous_url" || true
  else
    git remote set-url origin "https://github.com/${GITHUB_REPOSITORY}.git" || true
  fi
  (( had_xtrace )) && set -x
  return "$status"
}

stage_trusted_worker_app_helper() {
  # Copy the worker App helper and mapping from the trusted checkout before any
  # checkout_target switch, so a PR branch cannot supply the code that picks
  # the readable-handoff author. Missing files simply disable readable handoffs.
  [[ -z "${TRUSTED_WORKER_APP_HELPER:-}" ]] || return 0
  [[ -f .github/scripts/factory_worker_github_app.py ]] || return 0
  local trusted_dir
  trusted_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/comic-pile-worker-app.XXXXXX")"
  cp .github/scripts/factory_worker_github_app.py "$trusted_dir/factory_worker_github_app.py"
  if [[ -f .github/factory-worker-github-apps.json ]]; then
    cp .github/factory-worker-github-apps.json "$trusted_dir/factory-worker-github-apps.json"
    TRUSTED_WORKER_APP_MAPPING="$trusted_dir/factory-worker-github-apps.json"
    export TRUSTED_WORKER_APP_MAPPING
  fi
  TRUSTED_WORKER_APP_HELPER="$trusted_dir/factory_worker_github_app.py"
  export TRUSTED_WORKER_APP_HELPER
}

post_readable_handoff() {
  # Human-readable implementation/repair/review handoff (#3135). Only worker 48
  # with its App id, installation id, and private key posts one, through its
  # installation token. Everyone else, and worker 48 until those secrets exist,
  # posts nothing new and keeps every existing comment on its current identity.
  # Trusted marker comments never come through here: they keep using the
  # ambient workflow GH_TOKEN (github-actions[bot]). GH_TOKEN is only overridden
  # for this one gh call and the git remote is never touched.
  local kind="$1" number="$2" head="${3:-}" detail="${4:-}"
  local helper="${TRUSTED_WORKER_APP_HELPER:-.github/scripts/factory_worker_github_app.py}"
  local mapping="${TRUSTED_WORKER_APP_MAPPING:-${FACTORY_WORKER_APP_MAPPING:-}}"
  local resolved source token body_file had_xtrace=0
  # The minted installation token is not a repository secret, so Actions will
  # not mask it on its own. Suspend xtrace for the whole handoff so `set -x`
  # can never echo it, and register ::add-mask:: before any other use.
  [[ $- == *x* ]] && had_xtrace=1
  { set +x; } 2>/dev/null
  [[ -f "$helper" ]] || { (( had_xtrace )) && set -x; return 0; }
  if [[ -n "$mapping" ]]; then
    resolved="$(FACTORY_WORKER_APP_MAPPING="$mapping" python3 "$helper" resolve-handoff 2>/dev/null)" || { (( had_xtrace )) && set -x; return 0; }
  else
    resolved="$(python3 "$helper" resolve-handoff 2>/dev/null)" || { (( had_xtrace )) && set -x; return 0; }
  fi
  source="${resolved%%$'\t'*}"
  token="${resolved#*$'\t'}"
  [[ "$source" == installation && -n "$token" ]] || { (( had_xtrace )) && set -x; return 0; }
  # Workflow commands are read from stderr as well as stdout.
  printf '::add-mask::%s\n' "$token" >&2
  body_file="$(mktemp "${RUNNER_TEMP:-/tmp}/factory-readable-handoff.XXXXXX.md")"
  if ! python3 "$helper" render-handoff \
    --kind "$kind" --worker "$WORKER" --display "$DISPLAY" --model "$MODEL" \
    --head "$head" --detail "$detail" > "$body_file"; then
    rm -f "$body_file"
    (( had_xtrace )) && set -x
    return 0
  fi
  if ! GH_TOKEN="$token" gh issue comment "$number" --body-file "$body_file" >/dev/null 2>&1; then
    log "readable ${kind} handoff for #${number} could not be posted by the worker App; trusted state is unaffected" >&2
  fi
  rm -f "$body_file"
  (( had_xtrace )) && set -x
  return 0
}

factory_advance_display() {
  local served=''
  if [[ "$SOURCE" == kilo-auto && -n "${KILO_SERVED_MODELS_FILE:-}" && -f "$KILO_SERVED_MODELS_FILE" ]]; then
    served="$(grep -E '^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$' "$KILO_SERVED_MODELS_FILE" \
      | sort -u | paste -sd ',' - || true)"
  fi
  if [[ -n "$served" ]]; then
    printf '%s (served: %s)' "$DISPLAY" "$served"
  else
    printf '%s' "$DISPLAY"
  fi
}

find_overlapping_factory_pr() {
  local issue="$1" branch="$2" changed prs pr linked files matched
  changed="$(git diff --cached --name-only --no-renames -z | jq -Rsc 'split("\u0000") | map(select(length > 0))')" || return 2
  prs="$(gh pr list --state open --label factory --limit 1000 --json number,headRefName)" || return 2
  # A saturated list is incomplete evidence; never assume no overlap.
  [[ "$(jq 'length' <<< "$prs")" -lt 1000 ]] || return 2
  while IFS= read -r pr; do
    [[ -n "$pr" ]] || continue
    linked="$(linked_issue_from_pr "$pr")" || return 2
    [[ "$linked" == "$issue" ]] || continue
    files="$(gh api --paginate --slurp "repos/${GITHUB_REPOSITORY}/pulls/${pr}/files?per_page=100")" || return 2
    matched="$(jq -r --argjson changed "$changed" '
      [.[][] | .filename, .previous_filename // empty]
      | any(. as $path | $changed | index($path) != null)
    ' <<< "$files")" || return 2
    if [[ "$matched" == true ]]; then
      jq -c --argjson pr "$pr" '.[] | select(.number == $pr)' <<< "$prs"
      return 0
    fi
  done < <(jq -r --arg branch "$branch" 'sort_by(.number)[] | select(.headRefName != $branch) | .number' <<< "$prs")
  printf '{}\n'
}

adopt_persisted_work() {
  local head saved_branch
  [[ -n "${CHECKPOINT_REF_FILE:-}" && -s "$CHECKPOINT_REF_FILE" ]] || return 0
  head="$(sed -n '1p' "$CHECKPOINT_REF_FILE")"
  saved_branch="$(sed -n '2p' "$CHECKPOINT_REF_FILE")"
  [[ "$head" =~ ^[a-f0-9]{40}$ && "$saved_branch" == factory/work-checkpoints/* ]] || return 1
  git fetch origin "refs/heads/$saved_branch" >&2 || return 1
  [[ "$(git rev-parse FETCH_HEAD)" == "$head" ]] || return 1
  git merge-base --is-ancestor "$EXPECTED_HEAD" "$head" || return 1
  # Never overwrite ordinary commits made since the last snapshot.
  if git merge-base --is-ancestor "$head" HEAD; then
    :
  elif git merge-base --is-ancestor HEAD "$head"; then
    git reset --mixed "$head" >&2 || return 1
  else
    return 1
  fi
  [[ "$MODE" != issue ]] || BRANCH="$saved_branch"
}

redirect_issue_to_overlapping_pr() {
  local issue="$1" overlap pr branch patch timeout_seconds
  [[ -n "$(git status --porcelain)" ]] || return 0
  git add -A || return 2
  overlap="$(find_overlapping_factory_pr "$issue" "$BRANCH")" || return 2
  pr="$(jq -r '.number // empty' <<< "$overlap")"
  [[ -n "$pr" ]] || return 0
  branch="$(jq -er '.headRefName' <<< "$overlap")" || return 2
  # Reuse only an unowned or already-self-owned PR. Never steal a live lease.
  if ! current_owner_is_self "$pr" && ! claim_unowned_pr "$pr"; then
    log "same-issue file overlap belongs to active PR #${pr}; refusing another PR" >&2
    return 2
  fi
  patch="$(mktemp "${RUNNER_TEMP:-/tmp}/factory-overlap.XXXXXX.patch")" || return 2
  git diff --no-ext-diff --no-textconv --no-color --cached --binary > "$patch" || return 2
  log "prepared overlap patch retained at ${patch} until successful application" >&2
  # checkout_target rejects fork repositories and verifies the exact remote head.
  checkout_target pr "$pr" "$branch" >&2 || return 2
  MODE=pr NUMBER="$pr" BRANCH="$branch"
  replace_labels "$pr" "$OWNER" factory:building || return 2
  log "advancing overlapping PR #${pr} for issue #${issue} instead of opening a duplicate" >&2
  if ! git apply --3way --index "$patch" >&2; then
    timeout_seconds=$(( $(remaining) - 480 ))
    (( timeout_seconds >= 300 )) || return 2
    # Reconcile the prepared change with existing work on the actual PR branch;
    # normal PR fast checks still run before any push.
    run_agent pr "$pr" "$timeout_seconds" >&2 || return 2
    [[ -z "$(git ls-files -u)" ]] || return 2
  fi
  rm -f "$patch"
}

persist_issue_pr() {
  # The push credential (possibly a minted installation token) lives in locals
  # below. Suspend xtrace for the whole persistence step so `set -x` cannot
  # echo it, then restore the caller's tracing state.
  local had_xtrace=0 status=0
  [[ $- == *x* ]] && had_xtrace=1
  { set +x; } 2>/dev/null
  _persist_issue_pr_untraced "$@" || status=$?
  (( had_xtrace )) && set -x
  return "$status"
}

_persist_issue_pr_untraced() {
  local number="$1" branch="$2" pr title body base_ref
  base_ref="$EXPECTED_HEAD"
  [[ -n "$(git status --porcelain)" ]] || ! git diff --quiet "$base_ref" HEAD || return 1
  if ! reject_out_of_scope_diff 'issue' "$number" "$base_ref"; then
    return 1
  fi
  if ! factory_run_fast_checks "$base_ref" "/tmp/factory-fast-checks-${WORKER}.log" >&2; then
    log "fast checks failed for issue #${number}; not pushing (log: /tmp/factory-fast-checks-${WORKER}.log)" >&2
    factory_report_fast_check_failure 'issue' "$number" "/tmp/factory-fast-checks-${WORKER}.log"
    FACTORY_PUSH_BLOCKED_REASON='fast-checks-failed'
    return 1
  fi
  git add -A >&2
  # Revalidate immediately before creating a new branch/PR. A late overlap or
  # unreadable GitHub state must not turn into another conflicting PR.
  local overlap
  overlap="$(find_overlapping_factory_pr "$number" "$branch")" || return 1
  if [[ -n "$(jq -r '.number // empty' <<< "$overlap")" ]]; then
    log 'same-issue overlap appeared during validation; refusing duplicate PR' >&2
    return 1
  fi
  if [[ -n "${TRUSTED_CHECKPOINT_HANDOFF:-}" ]]; then
    python3 "$TRUSTED_CHECKPOINT_HANDOFF" --repo "$GITHUB_REPOSITORY" \
      --run-id "$GITHUB_RUN_ID" --check-write --mode issue --number "$number" \
      --worker "$WORKER" --work-branch "$branch" || return 1
  fi
  git commit --allow-empty -m "factory: advance #${number} with $(factory_advance_display)" >&2
  # The agent may be running on a local worktree branch whose name differs from
  # the target branch. Push the exact commit we just created, then verify that
  # GitHub's branch ref is actually at that commit before reporting success.
  local pushed_head remote_head push_credential push_source push_token
  pushed_head="$(git rev-parse HEAD)"
  push_credential="$(factory_push_credential)" || return 1
  push_source="${push_credential%%$'\t'*}"
  push_token="${push_credential#*$'\t'}"
  factory_git_push "$push_source" "$push_token" --set-upstream origin "HEAD:$branch" >&2
  remote_head="$(git ls-remote origin "refs/heads/${branch}" | awk '{print $1}')"
  if [[ "$remote_head" != "$pushed_head" ]]; then
    log "push verification failed for ${branch}: expected ${pushed_head}, found ${remote_head:-missing}" >&2
    return 1
  fi
  pr="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number // empty')"
  if [[ -z "$pr" ]]; then
    title="$(gh issue view "$number" --json title --jq .title)"
    body="$(printf 'Closes #%s.\n\nModel: %s\nSource: %s\nWorker: %s\n\nProduced by fixed-model Factory %s (%s). Normal ComicPile exact-head factory merge gates apply.\n' \
      "$number" "$MODEL" "$SOURCE" "$WORKER_ID" "$WORKER" "$DISPLAY")"
    # PR creation must use the same actor as the push. Worker 48 uses its
    # installation token when configured; every other worker uses
    # PR_REBASE_TOKEN. The workflow token stays github-actions[bot] for
    # controller calls and is not a worker App.
    GH_TOKEN="$push_token" \
      gh pr create --base main --head "$branch" --title "$title" --body "$body" >/tmp/factory-pr-url
    pr="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number')"
  fi
  echo "$pr"
}

persist_pr_changes() {
  # The push credential (possibly a minted installation token) lives in locals
  # below. Suspend xtrace for the whole persistence step so `set -x` cannot
  # echo it, then restore the caller's tracing state.
  local had_xtrace=0 status=0
  [[ $- == *x* ]] && had_xtrace=1
  { set +x; } 2>/dev/null
  _persist_pr_changes_untraced "$@" || status=$?
  (( had_xtrace )) && set -x
  return "$status"
}

_persist_pr_changes_untraced() {
  local pr="$1" branch="$2" base_ref
  local FACTORY_PUSH_REPOSITORY="${PR_HEAD_REPOSITORY:-}"
  if [[ "$FACTORY_PUSH_REPOSITORY" == "${GITHUB_REPOSITORY:-}" ]]; then
    FACTORY_PUSH_REPOSITORY=''
  fi
  base_ref="$EXPECTED_HEAD"
  [[ -n "$(git status --porcelain)" ]] || ! git diff --quiet "$base_ref" HEAD || return 1
  if ! reject_out_of_scope_diff 'pr' "$pr" "$base_ref"; then
    return 1
  fi
  local fast_check_log="/tmp/factory-fast-checks-${WORKER}.log"
  if ! factory_run_fast_checks "$base_ref" "$fast_check_log" >&2; then
    local retry_timeout=0 retry_passed=0
    # Keep useful, unpushed repairs in this ephemeral worktree for one bounded
    # correction. Previously we hard-reset immediately, forcing the next
    # worker to rediscover the same check failure on an unchanged PR head.
    # Review-only work and all other persistence paths retain their behavior.
    if [[ "${ASSIGNED_PR_STAGE:-}" == 'factory:changes-requested' ]] \
      && declare -F remaining >/dev/null \
      && declare -F run_agent >/dev/null; then
      retry_timeout=$(( $(remaining) - 300 ))
      (( retry_timeout > 900 )) && retry_timeout=900
      if (( retry_timeout >= 300 )); then
        log "PR #${pr} failed trusted pre-push checks; offering one same-lease correction (no push yet)"
        if declare -F start_work_checkpoint >/dev/null; then start_work_checkpoint; fi
        if FACTORY_FAST_CHECK_FEEDBACK_FILE="$fast_check_log" \
          run_agent pr "$pr" "$retry_timeout"; then
          # The correction can add files or rewrite the diff. Recheck BOTH
          # independent guards; a model's own success message proves nothing.
          if reject_out_of_scope_diff 'pr' "$pr" "$base_ref" \
            && factory_run_fast_checks "$base_ref" "$fast_check_log" >&2; then
            retry_passed=1
          fi
        fi
      fi
    fi
    if declare -F stop_work_checkpoint >/dev/null; then
      stop_work_checkpoint
      adopt_persisted_work || return 1
    fi
    if (( ! retry_passed )); then
      log "fast checks still failed for pr #${pr}; retaining persisted edits (log: ${fast_check_log})" >&2
      factory_report_fast_check_failure 'pr' "$pr" "$fast_check_log"
      FACTORY_PUSH_BLOCKED_REASON='fast-checks-failed'
      return 1
    fi
    log "PR #${pr} corrected fast-check failures in the same lease; safe persistence gates passed"
  fi
  git add -A
  if [[ -n "${TRUSTED_CHECKPOINT_HANDOFF:-}" ]]; then
    python3 "$TRUSTED_CHECKPOINT_HANDOFF" --repo "$GITHUB_REPOSITORY" \
      --run-id "$GITHUB_RUN_ID" --check-write --mode pr --number "$pr" \
      --worker "$WORKER" --work-branch "$branch" || return 1
  fi
  git commit --allow-empty -m "factory: advance PR #${pr} with $(factory_advance_display)"
  local pushed_head remote_head push_credential
  pushed_head="$(git rev-parse HEAD)"
  if [[ -n "$FACTORY_PUSH_REPOSITORY" ]]; then
    [[ -n "${PR_REBASE_TOKEN:-}" ]] || return 1
    push_credential="$(printf 'pat\t%s' "$PR_REBASE_TOKEN")"
  else
    push_credential="$(factory_push_credential)" || return 1
  fi
  factory_git_push "${push_credential%%$'\t'*}" "${push_credential#*$'\t'}" origin "HEAD:$branch"
  if [[ -n "$FACTORY_PUSH_REPOSITORY" ]]; then
    remote_head="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${pr}" --jq '.head.sha')"
  else
    remote_head="$(git ls-remote origin "refs/heads/${branch}" | awk '{print $1}')"
  fi
  if [[ "$remote_head" != "$pushed_head" ]]; then
    log "push verification failed for ${branch}: expected ${pushed_head}, found ${remote_head:-missing}" >&2
    return 1
  fi
  replace_labels "$pr" "$OWNER" 'factory:review'
}

ensure_owner_label
stage_trusted_guard
stage_trusted_review_controller
release_owned_targets 'previous-run-stale-lease'
trap 'release_owned_targets session-end-handoff || true' EXIT
log "starting fixed-model session with runtime ${RUNTIME_MODEL}; budget ${BUDGET_SECONDS}s"

while (( $(remaining) > 480 )); do
  MODE=''
  NUMBER=''
  BRANCH=''

  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    contains_skip_pr "$candidate" && continue
    NUMBER="$candidate"
    MODE='pr'
    BRANCH="$(gh pr view "$NUMBER" --json headRefName --jq .headRefName)"
    break
  done < <(choose_existing_pr)

  if [[ -z "$NUMBER" ]]; then
    for selector in 'user-reported bug' 'bug' 'ralph-task'; do
      read -r -a labels <<< "$selector"
      while IFS= read -r candidate; do
        [[ -n "$candidate" ]] || continue
        if claim_issue "$candidate"; then
          NUMBER="$candidate"
          MODE='issue'
          BRANCH="factory/${WORKER}-${NUMBER}-${BRANCH_SUFFIX}"
          break 2
        fi
      done < <(choose_issue "${labels[@]}")
    done
  fi

  if [[ -z "$NUMBER" ]]; then
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      contains_skip_pr "$candidate" && continue
      if claim_unowned_pr "$candidate"; then
        NUMBER="$candidate"
        MODE='pr'
        BRANCH="$(gh pr view "$NUMBER" --json headRefName --jq .headRefName)"
        log "adopted unowned PR #${NUMBER} on ${BRANCH}"
        break
      fi
    done < <(choose_unowned_pr)
  fi

  if [[ -z "$NUMBER" ]]; then
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      if claim_issue "$candidate"; then
        NUMBER="$candidate"
        MODE='issue'
        BRANCH="factory/${WORKER}-${NUMBER}-${BRANCH_SUFFIX}"
        log "ordinary executable backlog unavailable; entering required #679 child #${NUMBER}"
        break
      fi
    done < <(choose_backlog_zero_child)
  fi

  if [[ -z "$NUMBER" ]]; then
    log 'no selectable ordinary target or unowned #679 child is currently available; refusing to report this as productive work'
    break
  fi

  checkout_target "$MODE" "$NUMBER" "$BRANCH"
  available="$(remaining)"
  agent_timeout=$((available - 240))
  (( agent_timeout < 300 )) && break

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
  if is_model_cooldown_failure; then
    log 'OmniRoute reported model cooldown; ending this assignment without another retry'
    break
  fi
  log 'transient provider/runtime interruption; allowing OmniRoute to adapt the upstream route'
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
      (( agent_timeout >= 300 )) || break
  done

  if [[ "$MODE" == 'issue' ]]; then
    if pr="$(persist_issue_pr "$NUMBER" "$BRANCH")"; then
      if ! record_pr_provenance "$pr" "$NUMBER"; then
        log "PR #${pr} could not prove the issue assignment survived through persistence; closing it fail-closed"
        gh pr close "$pr" --comment 'Closed because durable factory provenance could not be established before handoff.' >/dev/null 2>&1 || true
        SKIP_PRS+=("$pr")
        continue
      fi
      replace_labels "$pr" "$OWNER" 'factory:review'
      # record_contribution only accepts a factory pull request, and a freshly
      # created PR has no labels yet. Record after the label write so the
      # producer's own head is covered by trusted provenance.
      record_head_contribution "$pr" 'pr-opened-handoff' || true
      log "opened/updated PR #${pr} for issue #${NUMBER}"
      release_target "$NUMBER" 'factory:review' 'pr-opened-handoff' 'issue'
      release_target "$pr" 'factory:review' 'pr-opened-handoff' 'pr'
      SKIP_PRS+=("$pr")
    elif (( transient_failure == 1 )); then
      log "issue #${NUMBER} produced no changes because the OmniRoute upstream was interrupted; releasing without marking the issue blocked"
      release_target "$NUMBER" 'factory:building' 'transient-model-interruption' 'issue'
    else
      log "issue #${NUMBER} produced no persisted change; recording a bounded retry attempt"
      release_target "$NUMBER" 'factory:building' 'no-persisted-change-handoff' 'issue'
    fi
    continue
  fi

  if persist_pr_changes "$NUMBER" "$BRANCH"; then
    log "pushed repairs to PR #${NUMBER}; review/CI must refresh"
    record_head_contribution "$NUMBER" 'repairs-pushed-handoff' || true
    release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'repairs-pushed-handoff'
    SKIP_PRS+=("$NUMBER")
    continue
  fi

  current="$(git rev-parse HEAD)"
  if [[ "$SOURCE" != 'kilo-auto' ]] && \
    grep -q 'FACTORY_GATE_READY' "/tmp/opencode-factory-${WORKER}.log"; then
    # Merge only through the canonical exact-head route: the trusted controller
    # gates (issue acceptance, exact-commit review/CI, substantive delivery and a
    # fresh declared/native dependency check) must return JSON decision "pass",
    # the head is pinned with --match-head-commit, and closure must use the
    # trusted staged post-merge policy. Never run a PR-branch-controlled gate and
    # never close the issue with a raw issue-close command.
    if factory_merge_with_canonical_gates "$NUMBER" "$current"; then
      log "trusted controller gates passed for PR #${NUMBER}; merged ${current}; post-merge closure ${FACTORY_CLOSURE_STATUS}: ${FACTORY_CLOSURE_OUTCOME}"
    else
      merge_route_status=$?
      case "$merge_route_status" in
        3)
          # The merge landed but the canonical closure policy did not complete.
          # Leave the issue open with the durable closure reason instead of
          # treating the merge as feature acceptance.
          log "merged PR #${NUMBER}, but post-merge closure did not complete (${FACTORY_CLOSURE_STATUS}: ${FACTORY_CLOSURE_OUTCOME}); issue left open"
          # The merge landed, so the PR is closed; skip it anyway so this
          # session never re-enters the merge route for the same target.
          SKIP_PRS+=("$NUMBER")
          ;;
        2)
          log "merge command failed for PR #${NUMBER} at ${current}; leaving it open for another route"
          # The PR is still open and still leased to this worker. Release the
          # lease first so a peer can retry without waiting for this session to
          # end, then skip it so choose_existing_pr does not hand it straight
          # back for another full agent run.
          if [[ "$SOURCE" != 'kilo-auto' ]]; then
            release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'merge-failed'
          fi
          SKIP_PRS+=("$NUMBER")
          ;;
        *)
          log "trusted controller gates for PR #${NUMBER} decision=${FACTORY_MERGE_GATE_DECISION:-empty}; not merging (reason: ${FACTORY_MERGE_GATE_REASON:-no reason})"
          # Do not merge: release lease and select other work. A retry decision
          # (for example pending required CI) is not a reason to run this PR
          # again in the same session, and the released lease lets
          # choose_unowned_pr hand it straight back. Record it in SKIP_PRS so
          # the session spends its budget on other targets instead.
          reason="gate-denied: ${FACTORY_MERGE_GATE_REASON:-no reason}"
          stage='factory:review'
          if [[ "$FACTORY_MERGE_GATE_DECISION" == 'deny' ]]; then
            stage='factory:blocked'
          fi
          if [[ "$SOURCE" != 'kilo-auto' ]]; then
            release_pr_and_issue "$NUMBER" "$BRANCH" "$stage" "$reason"
          fi
          SKIP_PRS+=("$NUMBER")
          ;;
      esac
    fi
    continue
  fi

  if (( transient_failure == 1 )); then
    log "PR #${NUMBER} was interrupted transiently with no persisted edits; releasing the lease and selecting other work"
    release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'transient-model-interruption'
  else
    log "PR #${NUMBER} is not merge-eligible now; releasing the lease for cross-worker takeover"
    release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'not-merge-eligible-handoff'
  fi
  SKIP_PRS+=("$NUMBER")
done

log "session complete; remaining budget $(remaining)s"
