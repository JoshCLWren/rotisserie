#!/usr/bin/env bash
# Sourced by free-model-factory-worker.sh after a split run's agent exits.
# A split run edits nothing: the wrapper validates the model's plan, logs it
# once for the trusted Entry persister, and releases the issue.

factory_finish_split() {
  local plan
  if (( agent_status == 0 )) && plan="$(python3 .github/scripts/factory_split_plan.py extract \
      --log "/tmp/opencode-factory-${WORKER}.log" --parent "$NUMBER")"; then
    log "FACTORY_SPLIT_PLAN: ${plan}"
    release_target "$NUMBER" 'factory:building' 'split-plan-handoff' 'issue' || true
    record_terminal_outcome success "issue #${NUMBER} split plan validated and handed to the trusted persister"
    return 0
  fi
  release_target "$NUMBER" 'factory:building' 'split-plan-rejected' 'issue' || true
  if (( agent_status != 0 )); then
    record_agent_failure_outcome "$agent_status"
  else
    record_terminal_outcome work_failure "issue #${NUMBER} split plan missing or invalid"
  fi
}
