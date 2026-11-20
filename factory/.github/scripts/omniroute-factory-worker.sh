#!/usr/bin/env bash
set -Eeuo pipefail

WORKER="${FACTORY_WORKER:-16}"
CALL_SIGN="${FACTORY_CALL_SIGN:-Multiple Man}"
MODEL="${OMNIROUTE_MODEL:?OMNIROUTE_MODEL is required}"
PROVIDER="${OMNIROUTE_PROVIDER:?OMNIROUTE_PROVIDER is required}"
OWNER="factory:${WORKER}"
WORKER_ID="opencode-omniroute-factory-${WORKER}"
BUDGET_SECONDS="${FACTORY_BUDGET_SECONDS:-6000}"
MAX_AGENT_ATTEMPTS="${OMNIROUTE_AGENT_MAX_ATTEMPTS:-2}"
TRANSIENT_BACKOFF_SECONDS="${OMNIROUTE_TRANSIENT_BACKOFF_SECONDS:-20}"
STARTED="$(date +%s)"
DEADLINE=$((STARTED + BUDGET_SECONDS))
OWNER_RE='^factory:(unowned|local|[1-9][0-9]*)$'
STAGE_RE='^factory:(building|review|changes-requested|ci|ready|blocked)$'
SKIP_PRS=()
MODE="run"

if [[ "${1:-}" == "--smoke" ]]; then
  MODE="smoke"
elif (($#)); then
  echo "usage: $0 [--smoke]" >&2
  exit 2
fi

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
# Canonical exact-head merge route (see factory-canonical-merge-route.sh).
# Worker-direct merges run the same trusted controller gates as the scheduled
# drain and close through the trusted post-merge policy, never a raw
# ``gh pr merge`` + ``gh issue close``.
# shellcheck source=factory-canonical-merge-route.sh
if [[ -f "$(dirname "${BASH_SOURCE[0]}")/factory-canonical-merge-route.sh" ]]; then
  source "$(dirname "${BASH_SOURCE[0]}")/factory-canonical-merge-route.sh"
else
  source .github/scripts/factory-canonical-merge-route.sh
fi

log() { printf '[factory:%s][omniroute:%s][model:%s] %s\n' "$WORKER" "$PROVIDER" "$MODEL" "$*"; }
remaining() { echo $((DEADLINE - $(date +%s))); }
contains_skip_pr() {
  local needle="$1" item
  for item in "${SKIP_PRS[@]:-}"; do [[ "$item" == "$needle" ]] && return 0; done
  return 1
}

replace_labels() {
  local number="$1" owner="$2" stage="$3"
  local labels target
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  target="$(jq -c --arg owner "$owner" --arg stage "$stage" --arg owner_re "$OWNER_RE" --arg stage_re "$STAGE_RE" '
    map(select((test($owner_re)|not) and (test($stage_re)|not) and . != "factory"))
    + ["factory", $owner, $stage] | unique' <<< "$labels")"
  gh api --method PUT "repos/${GITHUB_REPOSITORY}/issues/${number}/labels" --input - <<< "{\"labels\":${target}}" >/dev/null
}

record_no_diff_attempt() {
  local number="$1" epoch marker
  epoch="$(date +%s)"
  marker="<!-- comic-pile-factory-claim-released-v3:issue-${number}:${WORKER_ID}:${epoch}:no-persisted-change-handoff -->"
  gh issue comment "$number" --body "$marker" >/dev/null 2>&1
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

release_target() {
  local number="$1" fallback_stage="$2" reason="$3" target_kind="${4:-target}"
  local stage epoch marker
  stage="$(current_stage "$number" "$fallback_stage")"
  epoch="$(date +%s)"
  marker="<!-- comic-pile-factory-claim-released-v3:${target_kind}-${number}:${WORKER_ID}:${epoch}:${reason} -->"
  replace_labels "$number" 'factory:unowned' "$stage"
  gh issue comment "$number" --body "$marker" >/dev/null 2>&1 || true
  log "released ${target_kind} #${number} to factory:unowned at ${stage} (${reason})"
}

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
      -f name="$OWNER" -f color='5319e7' \
      -f description="Owned by OmniRoute Factory ${WORKER} · ${CALL_SIGN}" >/dev/null
    log "created ${OWNER}"
  fi
}

choose_existing_pr() {
  gh pr list --state open --limit 200 --json number,headRefName,updatedAt | jq -r --arg prefix "factory/${WORKER}-" '
    map(select(.headRefName | startswith($prefix))) | sort_by(.updatedAt) | .[].number'
}

choose_unowned_pr() {
  gh pr list --state open --limit 200 --label 'factory:unowned' --json number,isDraft,updatedAt | jq -r '
    map(select(.isDraft == false)) | sort_by(.updatedAt) | .[].number'
}

choose_issue() {
  local labels=("$@")
  local args=(issue list --state open --limit 300 --json number,labels,createdAt)
  local label
  for label in "${labels[@]}"; do args+=(--label "$label"); done
  gh "${args[@]}" | jq -r --arg owner_re "$OWNER_RE" '
    map(select(.number != 679 and .number != 1093 and .number != 1109))
    | map(select((([.labels[].name | select(test($owner_re) and . != "factory:unowned")] | length) == 0)))
    | sort_by(.createdAt) | reverse | .[].number'
}

choose_backlog_zero_issue() {
  gh issue view 679 --json state,labels 2>/dev/null | jq -r --arg owner_re "$OWNER_RE" '
    select(.state == "OPEN")
    | select((([.labels[].name | select(test($owner_re) and . != "factory:unowned")] | length) == 0))
    | "679"' || true
}

claim_issue() {
  local number="$1" labels target epoch marker
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  if jq -e --arg owner_re "$OWNER_RE" '[.[] | select(test($owner_re) and . != "factory:unowned")] | length > 0' >/dev/null <<< "$labels"; then
    return 1
  fi
  target="$(jq -c --arg owner "$OWNER" --arg owner_re "$OWNER_RE" --arg stage_re "$STAGE_RE" '
    map(select((test($owner_re)|not) and (test($stage_re)|not) and . != "factory"))
    + ["factory", $owner, "factory:building"] | unique' <<< "$labels")"
  gh api --method PUT "repos/${GITHUB_REPOSITORY}/issues/${number}/labels" --input - <<< "{\"labels\":${target}}" >/dev/null
  epoch="$(date +%s)"
  marker="<!-- comic-pile-factory-implement-claim-v3:issue-${number}:${WORKER_ID}:${epoch}:attempt-1 -->"
  gh issue comment "$number" --body "$marker" >/dev/null 2>&1 || true
}

claim_unowned_pr() {
  local number="$1" labels owner_count
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  jq -e 'index("factory:unowned") != null' >/dev/null <<< "$labels" || return 1
  owner_count="$(jq -r --arg owner_re "$OWNER_RE" '[.[] | select(test($owner_re) and . != "factory:unowned")] | length' <<< "$labels")"
  [[ "$owner_count" == "0" ]] || return 1
  replace_labels "$number" "$OWNER" 'factory:review'
  labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${number}/labels?per_page=100" --jq '[.[].name]')"
  jq -e --arg owner "$OWNER" 'index($owner) != null' >/dev/null <<< "$labels" || return 1
  gh issue comment "$number" --body "$(printf '<!-- omniroute-factory-owner:%s -->\nFactory %s · %s adopted this unowned PR using %s through OmniRoute.\n' "$WORKER" "$WORKER" "$CALL_SIGN" "$MODEL")" >/dev/null
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

checkout_target() {
  local mode="$1" number="$2" branch="$3"
  git fetch --prune origin
  git switch --detach origin/main >/dev/null 2>&1 || true
  git reset --hard origin/main >/dev/null
  git clean -fd >/dev/null
  if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
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
  changes="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews?per_page=100" | jq -s --arg head "$head" '[.[][] | select(.state == "CHANGES_REQUESTED" and .commit_id == $head)] | length')"
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
  grep -Eiq '429|Too Many Requests|rate.?limit|overloaded|temporar(il)?y unavailable|stream_early_eof|stream[^[:space:]]*[[:space:]]+timeout|stream_timeout|STREAM_READINESS_TIMEOUT|bad gateway|gateway timeout|service unavailable|HTTP[^0-9]*(502|503|504)|ECONNRESET|ETIMEDOUT|connection reset' "/tmp/opencode-factory-${WORKER}.log"
}

run_agent() {
  local mode="$1" number="$2" timeout_seconds="$3"
  local target mission prompt findings status=0
  if [[ "$mode" == "pr" ]]; then
    target="pull request #${number}"
    mission="Resume this PR. Inspect the exact current head, required CI, review submissions, and every inline review thread. Use 'gh pr view ${number} --json ...' and 'gh pr checks ${number} --required' for GitHub evidence; do not use 'gh pr status ${number}'. Greptile (greptile-apps[bot]) review findings are HIGH PRIORITY, must-address feedback: fix each one in code or post a concrete evidence-backed rebuttal reply on its thread, and never resolve a thread silently. Fix closure-critical defects and resolve or concretely rebut actionable threads. Treat a missing local .venv, pnpm, ruff, or other optional runner tool as an environment limitation, not a code blocker; rely on the repository's required CI results unless CI itself reports a failure. If no edits are required, decide whether the PR fully completes its declared scope and is safe to merge. End your final response with FACTORY_GATE_READY only when no semantic blocker remains; otherwise end with FACTORY_GATE_NOT_READY."
    findings="$(factory_review_findings_mission "$number" "${EXPECTED_HEAD:-$(git rev-parse HEAD)}")"
    mission+=" ${findings}"
  else
    target="issue #${number}"
    mission="Implement the full closure-critical acceptance contract for this issue with code and focused tests. Do not stop at planning or optional polish."
  fi
  prompt="Read AGENTS.md, docs/ISSUE_EXECUTION_PROTOCOL.md, docs/AUTONOMOUS_FACTORY_POLICY.md, docs/CHATGPT_FACTORY_PROMPT.md, and docs/FACTORY_GITHUB_VISIBILITY.md first. Follow normal ComicPile product-first factory policy and ownership rules.  Edit only the assigned target, run focused validation, and use GitHub for review context. Do not commit or push; the wrapper persists changes. OmniRoute may switch upstream models, providers, or routes within the configured policy; do not bypass the gateway or request unconfigured capacity. Do not enable auto-merge, push main, touch production databases, or alter automation schedules. Run context: Worker: ${WORKER}; route: ${MODEL}; gateway: ${PROVIDER}; target: ${target}. ${mission}"
  # OpenCode 2 rejects `opencode run --dir`; run from the workspace instead.
  ( cd "$GITHUB_WORKSPACE" && exec timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" \
    opencode run -m "omniroute/${MODEL}" --agent build --auto \
    --title "ComicPile OmniRoute Factory ${WORKER} · ${CALL_SIGN}" "$prompt" \
  ) 2>&1 | tee "/tmp/opencode-factory-${WORKER}.log"
  status=${PIPESTATUS[0]}
  return "$status"
}

smoke_agent() {
  local before after status
  before="$(git status --porcelain)"
  [[ -z "$before" ]] || { log 'smoke requires a clean worktree'; return 1; }
  set +e
  ( cd "$GITHUB_WORKSPACE" && exec timeout --signal=TERM --kill-after=10s 180s \
    opencode run -m "omniroute/${MODEL}" --agent build \
    --title "ComicPile OmniRoute Factory ${WORKER} smoke" \
    'Reply with exactly OMNIROUTE_OPENCODE_OK. Do not edit files, run commands, or use tools.' \
  ) 2>&1 | tee "/tmp/opencode-factory-${WORKER}.log"
  status=${PIPESTATUS[0]}
  set -e
  (( status == 0 )) || return "$status"
  grep -q 'OMNIROUTE_OPENCODE_OK' "/tmp/opencode-factory-${WORKER}.log"
  after="$(git status --porcelain)"
  [[ -z "$after" ]] || { log 'OpenCode smoke modified the worktree'; return 1; }
  log 'OpenCode smoke succeeded without modifying the worktree'
}

reject_unclean_git_state() {
  # Fail closed before persistence: a model-created merge, rebase, cherry-pick,
  # revert, or unresolved conflict must never be committed or pushed by the
  # wrapper. The model is instructed not to commit; any HEAD movement away from
  # the checked-out target means the model merged or rebased main.
  local marker
  marker="$(git rev-parse -q --verify MERGE_HEAD 2>/dev/null || true)"
  [[ -z "$marker" ]] || { log 'rejecting persistence: merge in progress (MERGE_HEAD)'; return 1; }
  marker="$(git rev-parse -q --verify CHERRY_PICK_HEAD 2>/dev/null || true)"
  [[ -z "$marker" ]] || { log 'rejecting persistence: cherry-pick in progress'; return 1; }
  marker="$(git rev-parse -q --verify REVERT_HEAD 2>/dev/null || true)"
  [[ -z "$marker" ]] || { log 'rejecting persistence: revert in progress'; return 1; }
  [[ -z "$(git ls-files -u)" ]] || { log 'rejecting persistence: unmerged index entries'; return 1; }
  if git diff --name-only --diff-filter=ACMRTUXB HEAD | xargs -r grep -lE '^(<<<<<<< |>>>>>>> |=======$)' 2>/dev/null | grep -q .; then
    log 'rejecting persistence: conflict markers in changed files'
    return 1
  fi
  if [[ "$(git rev-parse HEAD)" != "$EXPECTED_HEAD" ]]; then
    log 'rejecting persistence: HEAD moved away from the checked-out target (model merge/rebase/commit)'
    return 1
  fi
  return 0
}

persist_issue_pr() {
  local number="$1" branch="$2" pr title body
  [[ -n "$(git status --porcelain)" ]] || return 1
  if ! reject_unclean_git_state; then
    git reset --hard "$EXPECTED_HEAD" >/dev/null
    git clean -fd >/dev/null
    return 1
  fi
  git add -A
  git commit -m "factory: advance #${number} with OmniRoute"
  local pushed_head remote_head
  pushed_head="$(git rev-parse HEAD)"
  git push --set-upstream origin "HEAD:$branch"
  remote_head="$(git ls-remote origin "refs/heads/${branch}" | awk '{print $1}')"
  [[ "$remote_head" == "$pushed_head" ]] || return 1
  pr="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number // empty')"
  if [[ -z "$pr" ]]; then
    title="$(gh issue view "$number" --json title --jq .title)"
    body="$(printf 'Closes #%s.\n\nModel: %s\nProvider: %s\nWorker: %s\n\nProduced by Factory %s · %s through OmniRoute. Normal ComicPile exact-head factory merge gates apply.\n' "$number" "$MODEL" "$PROVIDER" "$WORKER_ID" "$WORKER" "$CALL_SIGN")"
    gh pr create --base main --head "$branch" --title "$title" \
      --body "$body" >/tmp/factory-pr-url
    pr="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number')"
  fi
  echo "$pr"
}

persist_pr_changes() {
  local pr="$1" branch="$2"
  [[ -n "$(git status --porcelain)" ]] || return 1
  if ! reject_unclean_git_state; then
    git reset --hard "$EXPECTED_HEAD" >/dev/null 2>&1
    git clean -fd >/dev/null
    return 1
  fi
  git add -A
  git commit -m "factory: advance PR #${pr} with OmniRoute"
  local pushed_head remote_head
  pushed_head="$(git rev-parse HEAD)"
  git push origin "HEAD:$branch"
  remote_head="$(git ls-remote origin "refs/heads/${branch}" | awk '{print $1}')"
  [[ "$remote_head" == "$pushed_head" ]] || return 1
  replace_labels "$pr" "$OWNER" 'factory:review'
}

# Review independence is only meaningful when the contributor record is written
# by trusted controller code, so stage the controller and its pure policy module
# before any checkout switch onto an adopted PR branch.
stage_trusted_review_controller() {
  if [[ -z "${TRUSTED_REVIEW_CONTROLLER:-}" ]]; then
    local trusted_dir
    trusted_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/comic-pile-review-controller.XXXXXX")"
    cp .github/scripts/factory-review-controller.py "$trusted_dir/factory-review-controller.py"
    cp .github/scripts/factory_post_merge_closure.py "$trusted_dir/factory_post_merge_closure.py"
    cp .github/scripts/factory_review_policy.py "$trusted_dir/factory_review_policy.py"
    # Bind model provenance to a trusted main checkout snapshot, not PR data.
    cp .github/scripts/factory_worker_slots.py "$trusted_dir/factory_worker_slots.py"
    cp .github/scripts/factory_provider_registry.py "$trusted_dir/factory_provider_registry.py"
    cp .github/scripts/factory_work_policy.py "$trusted_dir/factory_work_policy.py"
    chmod +x "$trusted_dir/factory-review-controller.py"
    TRUSTED_REVIEW_CONTROLLER="$trusted_dir/factory-review-controller.py"
    export TRUSTED_REVIEW_CONTROLLER
  fi
}

record_head_contribution() {
  # Every push that changes a PR head must bind this worker to that exact head,
  # including when the worker leaves no resume packet. Losing this record is not
  # fatal here: review independence then fails closed and needs two reviewers.
  local pr="$1" reason="$2" head
  stage_trusted_review_controller
  head="$(git rev-parse HEAD)"
  if ! python3 "$TRUSTED_REVIEW_CONTROLLER" record-contribution \
    --worker "$WORKER" --pr "$pr" --head "$head" >/dev/null; then
    log "unable to record trusted contributor provenance for PR #${pr} (${reason}); review will require two distinct reviewers" >&2
    return 1
  fi
}

if [[ "$MODE" == "smoke" ]]; then
  smoke_agent
  exit 0
fi

ensure_owner_label
stage_trusted_review_controller
log "starting normal factory session; budget ${BUDGET_SECONDS}s"

while (( $(remaining) > 480 )); do
  TARGET_MODE=''
  NUMBER=''
  BRANCH=''

  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    contains_skip_pr "$candidate" && continue
    NUMBER="$candidate"
    TARGET_MODE='pr'
    BRANCH="$(gh pr view "$NUMBER" --json headRefName --jq .headRefName)"
    break
  done < <(choose_existing_pr)

  if [[ -z "$NUMBER" ]]; then
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      contains_skip_pr "$candidate" && continue
      if claim_unowned_pr "$candidate"; then
        NUMBER="$candidate"
        TARGET_MODE='pr'
        BRANCH="$(gh pr view "$NUMBER" --json headRefName --jq .headRefName)"
        log "adopted unowned PR #${NUMBER} on ${BRANCH}"
        break
      fi
    done < <(choose_unowned_pr)
  fi

  if [[ -z "$NUMBER" ]]; then
    for selector in 'user-reported bug' 'bug' 'ralph-task'; do
      read -r -a labels <<< "$selector"
      while IFS= read -r candidate; do
        [[ -n "$candidate" ]] || continue
        if claim_issue "$candidate"; then
          NUMBER="$candidate"
          TARGET_MODE='issue'
          BRANCH="factory/${WORKER}-${NUMBER}-omniroute"
          break 2
        fi
      done < <(choose_issue "${labels[@]}")
    done
  fi

  if [[ -z "$NUMBER" ]]; then
    candidate="$(choose_backlog_zero_issue)"
    if [[ -n "$candidate" ]] && claim_issue "$candidate"; then
      NUMBER="$candidate"
      TARGET_MODE='issue'
      BRANCH="factory/${WORKER}-${NUMBER}-omniroute"
      log 'ordinary executable work is exhausted; entering backlog-zero Chromium work #679'
    fi
  fi

  if [[ -z "$NUMBER" ]]; then
    log 'no claimable normal work or backlog-zero work found; ending this bounded session'
    break
  fi

  checkout_target "$TARGET_MODE" "$NUMBER" "$BRANCH"
  available="$(remaining)"
  agent_timeout=$((available - 240))
  (( agent_timeout < 300 )) && break

  agent_attempt=1
  agent_status=0
  transient_failure=0
  while :; do
    set +e
    run_agent "$TARGET_MODE" "$NUMBER" "$agent_timeout"
    agent_status=$?
    set -e
    log "agent exit status ${agent_status} for ${TARGET_MODE} #${NUMBER}"
    if (( agent_status == 0 )); then
      transient_failure=0
      break
    fi
    if ! is_transient_agent_failure "$agent_status"; then
      transient_failure=0
      break
    fi
    transient_failure=1
    log 'transient OmniRoute/upstream interruption; allowing the gateway to adapt the route and retry if budget permits'
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

  if [[ "$TARGET_MODE" == 'issue' ]]; then
    if pr="$(persist_issue_pr "$NUMBER" "$BRANCH")"; then
      if ! record_pr_provenance "$pr" "$NUMBER"; then
        log "PR #${pr} could not prove the issue assignment survived through persistence; closing it fail-closed"
        gh pr close "$pr" --comment 'Closed because durable factory provenance could not be established before handoff.' >/dev/null 2>&1 || true
        SKIP_PRS+=("$pr")
        continue
      fi
      replace_labels "$pr" "$OWNER" 'factory:review'
      # A newly created PR is unlabeled and record_contribution refuses anything
      # that is not a factory pull request, so the producer's exact-head record
      # must follow the label write. Without it this producer is covered only by
      # the weaker branch/body fallback and the head fails closed to two reviews.
      record_head_contribution "$pr" 'pr-opened-handoff' || true
      log "opened/updated PR #${pr} for issue #${NUMBER}"
      SKIP_PRS+=("$pr")
    elif (( transient_failure == 1 )); then
      log "issue #${NUMBER} made no edits because the pinned route was temporarily unavailable; releasing without a false blocker"
      replace_labels "$NUMBER" 'factory:unowned' 'factory:building'
    else
      if record_no_diff_attempt "$NUMBER"; then
        log "issue #${NUMBER} produced no changes; recorded a bounded no-diff attempt and released for retry"
        replace_labels "$NUMBER" 'factory:unowned' 'factory:building'
      else
        log "issue #${NUMBER} produced no changes, but the durable retry marker failed; retaining the lease rather than creating an uncounted retry"
      fi
    fi
    continue
  fi

  if persist_pr_changes "$NUMBER" "$BRANCH"; then
    log "pushed repairs to PR #${NUMBER}; recording contributor provenance"
    record_head_contribution "$NUMBER" 'repairs-pushed-handoff' || true
    log "pushed repairs to PR #${NUMBER}; review and CI must refresh"
    SKIP_PRS+=("$NUMBER")
    continue
  fi

  current="$(git rev-parse HEAD)"
  if grep -q 'FACTORY_GATE_READY' "/tmp/opencode-factory-${WORKER}.log"; then
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
          release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'merge-failed'
          SKIP_PRS+=("$NUMBER")
          ;;
        *)
          log "trusted controller gates for PR #${NUMBER} decision=${FACTORY_MERGE_GATE_DECISION:-empty}; not merging (reason: ${FACTORY_MERGE_GATE_REASON:-no reason})"
          # Do not merge: release lease and select other work. A retry decision
          # (for example pending required CI) is not a reason to run this PR
          # again in the same session, and the released lease lets
          # choose_unowned_pr hand it straight back. Record it in SKIP_PRS so
          # the session spends its budget on other targets instead.
          release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'gate-denied'
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
