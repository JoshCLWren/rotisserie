#!/usr/bin/env bash
# Dispatch Deploy Production after a factory bot merge.
#
# A merge made with the built-in GITHUB_TOKEN (github-actions[bot]) does not
# fire push or workflow_run events, so Deploy Production never starts for it,
# and its */5 cron fires only every few hours in practice. workflow_dispatch is
# the one event GITHUB_TOKEN may raise, so every bot-merge path calls this
# helper after merging. Drains call it once per run, after the last merge.
#
# The dispatch passes source=factory-merge, which makes Deploy Production
# deduplicate against a READY deployment of the same main SHA (a manual
# dispatch still forces a redeploy). The vercel-production concurrency group
# (cancel-in-progress: false) runs at most one deploy at a time and keeps only
# the newest pending run. Every run deploys current main, so overlapping
# dispatches cannot deploy an older SHA after a newer one.
#
# Because only the newest pending run survives, a factory-merge dispatch would
# replace (cancel) a queued manual force-redeploy and then skip as already
# READY. So when a manual dispatch is already waiting, this helper does not
# dispatch: that manual run checks out main when it starts, so it deploys the
# merge anyway. If the queue lookup fails, it dispatches as before.
#
# A failed `gh workflow run` is retried (3 attempts, 3s then 6s apart, the
# same pattern as dispatch_worker). Failure is still non-fatal: the merge
# already landed, and the next push, dispatch, or cron run deploys the same
# main. Needs `actions: write`.
# FACTORY_DEPLOY_DISPATCH=off disables it for an incident.
# FACTORY_DEPLOY_DISPATCH_RETRY_SLEEP scales the retry backoff (tests use 0).

# Prints the number of manual Deploy Production dispatches that are waiting to
# run (queued, pending in the concurrency group, waiting, or requested). The
# run-name of a dispatched run starts with "Deploy Production [source=<src>]".
factory_pending_manual_deploy_count() {
  local repo="$1"
  gh run list --repo "$repo" --workflow deploy-production.yml \
    --event workflow_dispatch --limit 20 --json status,displayTitle \
    --jq '[.[] | select((.status == "queued" or .status == "pending" or .status == "waiting" or .status == "requested") and (.displayTitle | startswith("Deploy Production [source=manual]")))] | length'
}

factory_dispatch_production_deploy() {
  local reason="${1:-factory bot merge}"
  local repo="${GITHUB_REPOSITORY:-JoshCLWren/comic-pile}"
  local backoff="${FACTORY_DEPLOY_DISPATCH_RETRY_SLEEP:-3}"
  local attempt=1 pending_manual
  if [[ "${FACTORY_DEPLOY_DISPATCH:-on}" == 'off' ]]; then
    echo "Deploy Production dispatch disabled (FACTORY_DEPLOY_DISPATCH=off); skipping: ${reason}" >&2
    return 0
  fi
  if pending_manual="$(factory_pending_manual_deploy_count "$repo" 2>/dev/null)" && \
    [[ "$pending_manual" =~ ^[0-9]+$ ]] && (( pending_manual > 0 )); then
    echo "A manual Deploy Production run is already queued and will deploy current main; not dispatching (it would replace that run): ${reason}"
    return 0
  fi
  while (( attempt <= 3 )); do
    if gh workflow run deploy-production.yml --repo "$repo" --ref main \
      -f source=factory-merge -f reason="$reason"; then
      echo "Dispatched Deploy Production on main (attempt ${attempt}/3): ${reason}"
      return 0
    fi
    if (( attempt < 3 )); then
      sleep $((attempt * backoff))
    fi
    attempt=$((attempt + 1))
  done
  echo "::warning::Unable to dispatch Deploy Production after ${reason} (3 attempts); the next push, dispatch, or cron run deploys main." >&2
  return 0
}
