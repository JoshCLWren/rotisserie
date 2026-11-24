#!/usr/bin/env bash
# Sourced by free-model-factory-worker.sh from trusted main before any checkout.

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
  if [[ -f "$log_file" ]] && grep -Eqi 'budget[ _]exhausted|api key budget (limit )?exceeded|payment required|HTTP[^0-9]*402|(monthly|daily|weekly|free) usage limit|reached your [a-z ]*usage limit|insufficient[ _](credits|balance|quota)|out of credits|add credits to keep going|please recharge|balance is too low|exceeded your current quota|quota[ _](exceeded|exhausted)|free-models-per-day|rate limit exceeded: free-models-per-day|add usage credits or subscribe|account (credits|balance) (depleted|exhausted)|(does|do)(n.t| not) have enough credits' "$log_file"; then
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

# Only a provider-attested empty balance on a model whose catalog row
# publishes a positive price trips the pool. Any other evidence, a slow
# attempt or an edited worktree keeps the failure on the exact pair.
trip_exhausted_credit_pool() {
  local failed_source="$1" failed_model="$2" elapsed="$3" pool
  (( elapsed < FAST_FAILOVER_SECONDS )) || return 1
  [[ -z "$(git status --porcelain)" ]] || return 1
  pool="$(python3 "$FACTORY_TRUSTED_FAILOVER_DIR/factory_billing_pool.py" trip \
    --source "$failed_source" --model "$failed_model" \
    --fleet-catalog-cache "$RUNNER_TEMP/factory-fleet-catalogs.json" \
    --log "/tmp/opencode-factory-${WORKER}.log")" || return 1
  exhausted_pools+=("$failed_source")
  log "FACTORY_CREDIT_POOL_EXHAUSTED: source=$failed_source pool=$pool model=$failed_model"
}

record_exhausted_capacity() {
  (( ${#exhausted_pools[@]} > 0 )) || return 0
  [[ "$(cut -f1 "$TERMINAL_OUTCOME_FILE" 2>/dev/null)" == provider_quota ]] || return 0
  local retry_at
  retry_at="$(python3 "$FACTORY_TRUSTED_FAILOVER_DIR/factory_billing_pool.py" retry-at)" || retry_at=unknown
  record_terminal_outcome provider_quota "paid credit balance exhausted at ${exhausted_pools[*]}; failover ended in this run; earliest canonical Fibonacci retry ${retry_at}"
  log "FACTORY_CAPACITY_EXHAUSTED: pools=${exhausted_pools[*]} earliest_retry_at=${retry_at}"
}
