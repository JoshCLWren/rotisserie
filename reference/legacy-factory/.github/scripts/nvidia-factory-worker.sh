#!/usr/bin/env bash
set -Eeuo pipefail

WORKER="${1:?worker number required}"
MODEL="${2:?model required}"
OWNER="factory:${WORKER}"
WORKER_ID="opencode-nvidia-factory-${WORKER}"
BUDGET_SECONDS="${FACTORY_BUDGET_SECONDS:-6000}"
MAX_AGENT_ATTEMPTS="${NVIDIA_AGENT_MAX_ATTEMPTS:-2}"
TRANSIENT_BACKOFF_SECONDS="${NVIDIA_TRANSIENT_BACKOFF_SECONDS:-20}"
STARTED="$(date +%s)"
DEADLINE=$((STARTED + BUDGET_SECONDS))
OWNER_RE='^factory:(unowned|local|[1-9]|[1-3][0-9]|[4-7][0-9])$'
STAGE_RE='^factory:(building|review|changes-requested|ci|ready|blocked)$'
SKIP_PRS=()
COOLED_MODELS=()

log() { printf '[factory:%s] %s\n' "$WORKER" "$*"; }
remaining() { echo $((DEADLINE - $(date +%s))); }
contains_skip_pr() {
  local needle="$1" item
  for item in "${SKIP_PRS[@]:-}"; do [[ "$item" == "$needle" ]] && return 0; done
  return 1
}
contains_cooled_model() {
  local needle="$1" item
  for item in "${COOLED_MODELS[@]:-}"; do [[ "$item" == "$needle" ]] && return 0; done
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

ensure_fleet_labels() {
  local worker label
  for worker in {6..15}; do
    label="factory:${worker}"
    if ! gh api "repos/${GITHUB_REPOSITORY}/labels/factory%3A${worker}" >/dev/null 2>&1; then
      gh api --method POST "repos/${GITHUB_REPOSITORY}/labels" \
        -f name="$label" -f color='5319e7' -f description="Owned by OpenCode NVIDIA Factory ${worker}" >/dev/null
      log "created ${label}"
    fi
  done
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

  gh issue comment "$number" --body "<!-- nvidia-factory-owner:${WORKER} -->\nFactory ${WORKER} adopted this previously unowned PR for the next actionable step." >/dev/null
  return 0
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
  unresolved="$(gh api graphql -F owner='JoshCLWren' -F name='comic-pile' -F number="$pr" -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved}}}}}' --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length')"
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
  grep -Eiq '429|Too Many Requests|rate.?limit|overloaded|temporar(il)?y unavailable|cooling down|model_cooldown|stream_early_eof|stream[^[:space:]]*[[:space:]]+timeout|stream_timeout|STREAM_READINESS_TIMEOUT|bad gateway|gateway timeout|service unavailable|HTTP[^0-9]*(502|503|504)|ECONNRESET|ETIMEDOUT|connection reset' "/tmp/opencode-factory-${WORKER}.log"
}

probe_nvidia_model() {
  local candidate="$1" provider_model request response http_code
  provider_model="${candidate#nvidia/}"
  request="$(jq -nc --arg model "$provider_model" '{model:$model,messages:[{role:"user",content:"Reply with exactly: FCM_NVIDIA_MODEL_OK"}],max_tokens:32}')"
  response="$(mktemp)"
  http_code="$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
    --connect-timeout 5 --max-time 20 \
    --header "Authorization: Bearer $NVIDIA_API_KEY" \
    --header 'Content-Type: application/json' \
    --data "$request" https://integrate.api.nvidia.com/v1/chat/completions || true)"
  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]] && jq -e '.choices[0].message.content | strings | contains("FCM_NVIDIA_MODEL_OK")' >/dev/null 2>&1 <"$response"; then
    rm -f "$response"
    return 0
  fi
  rm -f "$response"
  return 1
}

rotate_model() {
  local fcm_output candidate start idx offset
  local candidates=()
  if ! fcm_output="$(free-coding-models --json --origin nvidia --hide-unconfigured --best --no-telemetry 2>/dev/null)"; then
    return 1
  fi
  mapfile -t candidates < <(printf '%s\n' "$fcm_output" | python scripts/select_fcm_nvidia_model.py | sed '/^$/d')
  ((${#candidates[@]} > 0)) || return 1
  start=$(( (WORKER - 6 + 1) % ${#candidates[@]} ))
  for ((offset=0; offset<${#candidates[@]}; offset++)); do
    idx=$(( (start + offset) % ${#candidates[@]} ))
    candidate="${candidates[$idx]}"
    [[ "$candidate" == "$MODEL" ]] && continue
    contains_cooled_model "$candidate" && continue
    log "probing alternate model ${candidate} after transient failure"
    if probe_nvidia_model "$candidate"; then
      MODEL="$candidate"
      log "rotated to healthy model ${MODEL}"
      return 0
    fi
  done
  return 1
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
  prompt="You are OpenCode NVIDIA Factory ${WORKER} for JoshCLWren/comic-pile. Durable worker ID: ${WORKER_ID}. Model: ${MODEL}. Assigned target: ${target}. Read AGENTS.md, docs/ISSUE_EXECUTION_PROTOCOL.md, docs/AUTONOMOUS_FACTORY_POLICY.md, docs/CHATGPT_FACTORY_PROMPT.md, and docs/FACTORY_GITHUB_VISIBILITY.md first. Josh directly requires product-first V22 behavior: user-reported product bugs outrank unrelated CI/E2E/test plumbing, and a run is a work session rather than a one-ticket punch. ${mission} Work only on the assigned target during this agent invocation. Edit the checked-out branch, run focused validation, and use gh/GitHub when needed for review context. Do not commit or push; the wrapper persists changes. Do not enable auto-merge, push main, touch production databases, or alter automation schedules."
  timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" opencode run -m "$MODEL" --agent build --auto --dir "$GITHUB_WORKSPACE" --title "ComicPile NVIDIA Factory ${WORKER}" "$prompt" 2>&1 | tee "/tmp/opencode-factory-${WORKER}.log"
  status=${PIPESTATUS[0]}
  return "$status"
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
  if [[ -z "$(git status --porcelain)" ]]; then return 1; fi
  if ! reject_unclean_git_state; then
    git reset --hard "$EXPECTED_HEAD" >/dev/null
    git clean -fd >/dev/null
    return 1
  fi
  git add -A
  git commit -m "factory: advance #${number} with NVIDIA OpenCode"
  local pushed_head remote_head
  pushed_head="$(git rev-parse HEAD)"
  git push --set-upstream origin "HEAD:$branch"
  remote_head="$(git ls-remote origin "refs/heads/${branch}" | awk '{print $1}')"
  [[ "$remote_head" == "$pushed_head" ]] || return 1
  pr="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number // empty')"
  if [[ -z "$pr" ]]; then
    title="$(gh issue view "$number" --json title --jq .title)"
    body="$(printf 'Closes #%s.\n\nModel: %s\nWorker: %s\n\nProduced by OpenCode NVIDIA Factory %s. Normal exact-head factory merge gates apply.\n' \
      "$number" "$MODEL" "$WORKER_ID" "$WORKER")"
    gh pr create --base main --head "$branch" --title "$title" --body "$body" >/tmp/factory-pr-url
    pr="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number')"
  fi
  echo "$pr"
}

persist_pr_changes() {
  local pr="$1" branch="$2"
  if [[ -z "$(git status --porcelain)" ]]; then return 1; fi
  if ! reject_unclean_git_state; then
    git reset --hard "$EXPECTED_HEAD" >/dev/null
    git clean -fd >/dev/null
    return 1
  fi
  git add -A
  git commit -m "factory: advance PR #${pr} with NVIDIA OpenCode"
  local pushed_head remote_head
  pushed_head="$(git rev-parse HEAD)"
  git push origin "HEAD:$branch"
  remote_head="$(git ls-remote origin "refs/heads/${branch}" | awk '{print $1}')"
  [[ "$remote_head" == "$pushed_head" ]] || return 1
  replace_labels "$pr" "$OWNER" 'factory:review'
  return 0
}

# Bootstrap all ten durable owner labels immediately. Visibility should not wait
# for future schedule slots to happen to execute successfully.
ensure_fleet_labels
log "starting with model ${MODEL}; budget ${BUDGET_SECONDS}s"

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
    for selector in 'user-reported bug' 'bug' 'ralph-task'; do
      read -r -a labels <<< "$selector"
      while IFS= read -r candidate; do
        [[ -n "$candidate" ]] || continue
        if claim_issue "$candidate"; then
          NUMBER="$candidate"
          MODE='issue'
          BRANCH="factory/${WORKER}-${NUMBER}-nvidia"
          break 2
        fi
      done < <(choose_issue "${labels[@]}")
    done
  fi

  if [[ -z "$NUMBER" ]]; then
    log 'no unclaimed executable product work found; ending this session cleanly'
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
    log "agent exit status ${agent_status} for ${MODE} #${NUMBER} on ${MODEL}"

    if (( agent_status == 0 )); then
      transient_failure=0
      break
    fi
    if ! is_transient_agent_failure "$agent_status"; then
      transient_failure=0
      break
    fi

    transient_failure=1
    log "transient NVIDIA/OpenCode interruption on ${MODEL}; preserving this target instead of failing the heartbeat"

    if [[ -n "$(git status --porcelain)" ]]; then
      log 'transient interruption left edits in the worktree; checkpointing them before selecting more work'
      break
    fi

    (( agent_attempt < MAX_AGENT_ATTEMPTS )) || break
    (( $(remaining) > 540 )) || break

    COOLED_MODELS+=("$MODEL")
    sleep_for="$TRANSIENT_BACKOFF_SECONDS"
    max_sleep=$(( $(remaining) - 540 ))
    (( sleep_for > max_sleep )) && sleep_for="$max_sleep"
    (( sleep_for > 0 )) && sleep "$sleep_for"

    if ! rotate_model; then
      log 'no alternate NVIDIA model became healthy during this recovery window'
      break
    fi

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
      SKIP_PRS+=("$pr")
    elif (( transient_failure == 1 )); then
      log "issue #${NUMBER} produced no changes because of a transient provider/runtime interruption; releasing without marking the issue blocked"
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
    log "pushed repairs to PR #${NUMBER}; review/CI must refresh"
    SKIP_PRS+=("$NUMBER")
    continue
  fi

  current="$(git rev-parse HEAD)"
  if grep -q 'FACTORY_GATE_READY' "/tmp/opencode-factory-${WORKER}.log" && machine_merge_gates_pass "$NUMBER" "$current"; then
    log "all exact-head gates passed for PR #${NUMBER}; merging ${current}"
    gh pr merge "$NUMBER" --merge --match-head-commit "$current" --delete-branch
    issue_number="$(sed -nE "s#^factory/${WORKER}-([0-9]+)-nvidia$#\\1#p" <<< "$BRANCH")"
    if [[ -n "$issue_number" ]]; then
      state="$(gh issue view "$issue_number" --json state --jq .state 2>/dev/null || true)"
      if [[ "$state" == 'OPEN' ]]; then gh issue close "$issue_number" --reason completed --comment "Closed after Factory ${WORKER} merged PR #${NUMBER} through the exact-head gates."; fi
    fi
    continue
  fi

  if (( transient_failure == 1 )); then
    log "PR #${NUMBER} was interrupted transiently with no persisted edits; preserving ownership state and selecting other work"
  else
    log "PR #${NUMBER} is not merge-eligible now; releasing this cycle and selecting other work"
  fi
  SKIP_PRS+=("$NUMBER")
done

log "session complete; remaining budget $(remaining)s"
