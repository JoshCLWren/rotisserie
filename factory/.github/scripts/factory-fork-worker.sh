#!/usr/bin/env bash
# Loaded only from trusted main, before any normal PR checkout.

factory_route_fork_assignment() {
  local info repository head branch
  FACTORY_FORK_STATIC_REVIEW=0
  [[ "$MODE" == pr ]] || return 1
  info="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${NUMBER}")" || return 2
  repository="$(jq -er '.head.repo.full_name' <<< "$info")" || return 2
  [[ "$repository" != "$GITHUB_REPOSITORY" ]] || return 1
  [[ "$(jq -r '.base.repo.full_name' <<< "$info")" == "$GITHUB_REPOSITORY" ]] || return 2
  head="$(jq -er '.head.sha' <<< "$info")" || return 2
  branch="$(jq -er '.head.ref' <<< "$info")" || return 2
  [[ "$head" =~ ^[a-f0-9]{40}$ ]] || return 2
  if [[ -n "${FACTORY_ASSIGNMENT:-}" && "$head" != "$(jq -r '.head' <<< "$FACTORY_ASSIGNMENT")" ]]; then
    log 'fork PR head moved after assignment; refusing stale execution'
    return 2
  fi
  EXPECTED_HEAD="$head"
  BRANCH="$branch"
  PR_HEAD_REPOSITORY="$repository"
  if [[ "$ASSIGNED_PR_STAGE" == factory:review && "$SOURCE" != kilo-auto ]]; then
    # Fetch objects as data only, for the existing outcome ancestry gate.
    git fetch origin "refs/pull/${NUMBER}/head" || return 2
    [[ "$(git rev-parse FETCH_HEAD)" == "$head" ]] || return 2
    FACTORY_REVIEW_ANCESTOR="$head"
    factory_prepare_greptile_outcomes_file
    FACTORY_FORK_STATIC_REVIEW=1
    log "fork PR #${NUMBER} will be reviewed as data; keeping the credentialed checkout on trusted main"
    return 0
  fi
  if [[ "$ASSIGNED_PR_STAGE" != factory:review && "$SOURCE" == kilo-auto ]]; then
    if ! python3 "$TRUSTED_FORK_CONTROL" dispatch --pr "$NUMBER" --head "$head" --worker "$WORKER"; then
      release_pr_and_issue "$NUMBER" "$BRANCH" "$ASSIGNED_PR_STAGE" 'fork-dispatch-failed'
      record_terminal_outcome control_plane_failure 'secretless fork dispatch failed'
      return 10
    fi
    FACTORY_RETAIN_LEASE_ON_EXIT=1
    record_terminal_outcome success "fork repair dispatched to the separate secretless execution workflow"
    return 10
  fi
  release_pr_and_issue "$NUMBER" "$BRANCH" "$ASSIGNED_PR_STAGE" 'fork-capability-mismatch'
  record_terminal_outcome no_work 'fork stage requires a different safe execution capability'
  return 10
}

factory_run_fork_static_review() {
  python3 "$TRUSTED_FORK_CONTROL" review --pr "$NUMBER" --head "$EXPECTED_HEAD" \
    --worker "$WORKER" --runtime "$RUNTIME_MODEL" --timeout "$1" \
    --log "/tmp/opencode-factory-${WORKER}.log"
}
