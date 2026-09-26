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
  local number="$1" stage="$2"
  local sha="" conflicted=0 meta='{}' mergeable='' merge_state=''
  sha="$(git rev-parse HEAD 2>/dev/null || true)"
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
  local stage epoch marker generation=""
  case "${target_kind}:${reason}" in
    pr:repair-no-change-ready-handoff|pr:repair-no-persisted-change-handoff)
      stage="$fallback_stage"
      ;;
    *)
      stage="$(current_stage "$number" "$fallback_stage")"
      ;;
  esac
  epoch="$(date +%s)"
  if [[ "$reason" == 'no-persisted-change-handoff' || "$reason" == 'repair-no-persisted-change-handoff' ]]; then
    if [[ "$target_kind" == 'pr' ]]; then
      if ! generation="$(pr_no_diff_generation_fields "$number" "$stage")"; then
        FACTORY_RETAIN_LEASE_ON_EXIT=1
        log "unable to pin no-diff generation to head SHA for pr #${number}; retaining the lease"
        return 1
      fi
    fi
  fi
  marker="<!-- comic-pile-factory-claim-released-v3:${target_kind}-${number}:${WORKER_ID}:${epoch}:${reason}${generation} -->"
  if [[ "$reason" == 'no-persisted-change-handoff' || "$reason" == 'repair-no-persisted-change-handoff' ]]; then
    if ! gh issue comment "$number" --body "$marker" >/dev/null 2>&1; then
      FACTORY_RETAIN_LEASE_ON_EXIT=1
      log "unable to persist bounded no-diff marker for ${target_kind} #${number}; retaining the lease"
      return 1
    fi
    # Retry exhaustion is an execution-control concern. Preserve the truthful
    # workflow stage and let the controller suppress re-selection from the
    # durable marker; never rewrite no-diff failures as factory:blocked.
    replace_labels "$number" 'factory:unowned' "$stage"
  else
    replace_labels "$number" 'factory:unowned' "$stage"
    gh issue comment "$number" --body "$marker" >/dev/null 2>&1 || true
  fi
  log "released ${target_kind} #${number} to factory:unowned at ${stage} (${reason})"
}

release_owned_targets() {
  local reason="$1" number stage
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
  issue="$(linked_issue_from_branch "$branch")"
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
  changes="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews?per_page=100" \
    | jq -s --arg head "$head" '[.[][] | select(.state == "CHANGES_REQUESTED" and .commit_id == $head)] | length')"
  [[ "$changes" == "0" ]] || return 1
  unresolved="$(gh api graphql -F owner='JoshCLWren' -F name='comic-pile' -F number="$pr" \
    -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved}}}}}' \
    --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length')"
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

run_agent() {
  local mode="$1" number="$2" timeout_seconds="$3"
  local target mission prompt status=0
  if [[ "$mode" == "pr" ]]; then
    target="pull request #${number}"
    mission="Resume this PR. Inspect the exact current head, required CI, review submissions, and every inline review thread. Fix closure-critical defects and resolve or concretely rebut actionable threads. If no edits are required, decide whether the PR fully completes its declared scope and is safe to merge. End your final response with FACTORY_GATE_READY only when no semantic blocker remains; otherwise end with FACTORY_GATE_NOT_READY."
  else
    target="issue #${number}"
    mission="Implement the full closure-critical acceptance contract for this issue with code and focused tests. Do not stop at planning or optional polish."
  fi

  prompt="You are external-model Factory ${WORKER} for JoshCLWren/comic-pile. Durable worker ID: ${WORKER_ID}. Source: ${SOURCE}. Requested capability route: ${MODEL}. Runtime selector: ${RUNTIME_MODEL}. Assigned target: ${target}. Read AGENTS.md, docs/ISSUE_EXECUTION_PROTOCOL.md, docs/AUTONOMOUS_FACTORY_POLICY.md, docs/CHATGPT_FACTORY_PROMPT.md, and docs/FACTORY_GITHUB_VISIBILITY.md first. Follow the canonical product-first factory policy. ${mission} Work only on the assigned target during this agent invocation. Edit the checked-out branch, run focused validation, and use gh/GitHub when needed for review context. Do not commit or push; the wrapper persists changes. OmniRoute may switch upstream models, providers, or routes within the configured policy; do not bypass OmniRoute or request paid or unconfigured capacity. Do not enable auto-merge, push main, touch production databases, or alter automation schedules."

  if [[ "$SOURCE" == 'kilo-auto' ]]; then
    set +e
    bash .github/scripts/kilo-auto-factory-run.sh \
      "$timeout_seconds" \
      "ComicPile Factory ${WORKER} · ${DISPLAY}" \
      "$prompt" \
      "/tmp/opencode-factory-${WORKER}.log"
    status=$?
    set -e
    return "$status"
  fi

  timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" \
    opencode run -m "$RUNTIME_MODEL" --agent build --auto --dir "$GITHUB_WORKSPACE" \
    --title "ComicPile Factory ${WORKER} · ${DISPLAY}" "$prompt" \
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
  issue="$(linked_issue_from_branch "$(gh pr view "$number" --json headRefName --jq .headRefName)")"
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

persist_issue_pr() {
  local number="$1" branch="$2" pr title body base_ref
  [[ -n "$(git status --porcelain)" ]] || return 1
  base_ref="$EXPECTED_HEAD"
  if ! reject_out_of_scope_diff 'issue' "$number" "$base_ref"; then
    return 1
  fi
  git add -A >&2
  git commit -m "factory: advance #${number} with ${DISPLAY}" >&2
  # The agent may be running on a local worktree branch whose name differs from
  # the target branch. Push the exact commit we just created, then verify that
  # GitHub's branch ref is actually at that commit before reporting success.
  local pushed_head remote_head
  pushed_head="$(git rev-parse HEAD)"
  git push --set-upstream origin "HEAD:$branch" >&2
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
    gh pr create --base main --head "$branch" --title "$title" --body "$body" >/tmp/factory-pr-url
    pr="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number')"
  fi
  echo "$pr"
}

persist_pr_changes() {
  local pr="$1" branch="$2" base_ref
  [[ -n "$(git status --porcelain)" ]] || return 1
  base_ref="$EXPECTED_HEAD"
  if ! reject_out_of_scope_diff 'pr' "$pr" "$base_ref"; then
    return 1
  fi
  git add -A
  git commit -m "factory: advance PR #${pr} with ${DISPLAY}"
  local pushed_head remote_head
  pushed_head="$(git rev-parse HEAD)"
  git push origin "HEAD:$branch"
  remote_head="$(git ls-remote origin "refs/heads/${branch}" | awk '{print $1}')"
  if [[ "$remote_head" != "$pushed_head" ]]; then
    log "push verification failed for ${branch}: expected ${pushed_head}, found ${remote_head:-missing}" >&2
    return 1
  fi
  replace_labels "$pr" "$OWNER" 'factory:review'
}

ensure_owner_label
stage_trusted_guard
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
  (( agent_timeout > 3000 )) && agent_timeout=3000
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
    (( agent_timeout > 3000 )) && agent_timeout=3000
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
    release_pr_and_issue "$NUMBER" "$BRANCH" 'factory:review' 'repairs-pushed-handoff'
    SKIP_PRS+=("$NUMBER")
    continue
  fi

  current="$(git rev-parse HEAD)"
  if [[ "$SOURCE" != 'kilo-auto' ]] && \
    grep -q 'FACTORY_GATE_READY' "/tmp/opencode-factory-${WORKER}.log" && \
    machine_merge_gates_pass "$NUMBER" "$current"; then
    log "all exact-head gates passed for PR #${NUMBER}; merging ${current}"
    gh pr merge "$NUMBER" --merge --match-head-commit "$current" --delete-branch
    issue_number="$(linked_issue_from_branch "$BRANCH")"
    if [[ -n "$issue_number" ]]; then
      state="$(gh issue view "$issue_number" --json state --jq .state 2>/dev/null || true)"
      if [[ "$state" == 'OPEN' ]]; then
        gh issue close "$issue_number" --reason completed \
          --comment "Closed after Factory ${WORKER} merged PR #${NUMBER} through the exact-head gates."
      fi
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
