#!/usr/bin/env bash
# Canonical exact-head merge route for worker-direct factory merges.
#
# Worker wrappers that merge without the scheduled drain must run the same
# trusted controller gate as the drain: exact-head mechanical gates plus the
# controller's issue-level acceptance, exact-commit review/CI and substantive
# delivery safeguards, including a fresh declared/native dependency check. After
# the merge they must use the canonical trusted post-merge closure policy instead
# of a raw ``gh issue close``.
#
# The controller and the closure module are staged from trusted main before any
# checkout_target switches onto a PR branch. This route only ever executes those
# staged copies; it never runs a PR-branch-controlled gate.
#
# Outcomes are exported through the FACTORY_* variables below so callers can log
# a truthful result and tests can execute this file directly:
#
#   FACTORY_MERGE_STATE         denied | merge-failed | merged
#   FACTORY_MERGE_GATE_DECISION pass | retry | deny | empty
#   FACTORY_MERGE_GATE_REASON   controller reason, or a fail-closed reason
#   FACTORY_CLOSURE_OUTCOME     canonical closure decision, or error text
#   FACTORY_CLOSURE_STATUS      closed | not-applicable | blocked | error
#
# Return codes for factory_merge_with_canonical_gates:
#   0  merged, closure completed or not applicable
#   1  gates did not pass; nothing merged (retry/deny/fail-closed)
#   2  merge command failed; nothing closed
#   3  merged, but post-merge closure blocked or could not be verified

factory_canonical_merge_json_field() {
  local field="$1"
  python3 -c '
import json
import sys

field = sys.argv[1]
try:
    data = json.loads(sys.stdin.read())
except Exception:
    print("")
    raise SystemExit(0)
value = data.get(field, "") if isinstance(data, dict) else ""
print(value if isinstance(value, str) else "")
' "$field"
}

factory_trusted_post_merge_closure_script() {
  # The closure module is staged beside the trusted controller from main before
  # any PR checkout; a branch copy could be rewritten by the PR itself.
  local controller="${TRUSTED_REVIEW_CONTROLLER:-}"
  [[ -n "$controller" ]] || return 1
  [[ -f "$(dirname "$controller")/factory_post_merge_closure.py" ]] || return 1
  printf '%s/factory_post_merge_closure.py\n' "$(dirname "$controller")"
}

factory_run_canonical_merge_gates() {
  # Run the trusted controller and require an explicit JSON decision of "pass".
  # Exit status alone is insufficient: retry and deny decisions exit zero.
  local pr="$1" expected_head="$2" controller="${TRUSTED_REVIEW_CONTROLLER:-}"
  local output='' status=0
  FACTORY_MERGE_GATE_DECISION=''
  FACTORY_MERGE_GATE_REASON=''
  if [[ -z "$controller" ]]; then
    FACTORY_MERGE_GATE_REASON='trusted review controller is not staged'
    return 1
  fi
  if [[ ! -f "$controller" ]]; then
    FACTORY_MERGE_GATE_REASON='trusted review controller file is missing'
    return 1
  fi
  if [[ ! "$pr" =~ ^[0-9]+$ ]]; then
    FACTORY_MERGE_GATE_REASON='invalid pull request number'
    return 1
  fi
  expected_head="${expected_head,,}"  # normalize to lowercase for validation
  if [[ ! "$expected_head" =~ ^[0-9a-f]{40}$ ]]; then
    FACTORY_MERGE_GATE_REASON='expected head is not a full lowercase Git SHA'
    return 1
  fi
  output="$(python3 "$controller" gates \
    --pr "$pr" --expected-head "$expected_head" 2>&1)" || status=$?
  FACTORY_MERGE_GATE_DECISION="$(
    printf '%s' "$output" | factory_canonical_merge_json_field decision
  )"
  FACTORY_MERGE_GATE_REASON="$(
    printf '%s' "$output" | factory_canonical_merge_json_field reason
  )"
  if (( status != 0 )); then
    [[ -n "$FACTORY_MERGE_GATE_REASON" ]] || \
      FACTORY_MERGE_GATE_REASON="trusted controller gates failed with status ${status}"
    return 1
  fi
  if [[ -z "$FACTORY_MERGE_GATE_DECISION" ]]; then
    FACTORY_MERGE_GATE_REASON="${FACTORY_MERGE_GATE_REASON:-trusted controller returned no decision}"
    return 1
  fi
  if [[ "$FACTORY_MERGE_GATE_DECISION" != 'pass' ]]; then
    FACTORY_MERGE_GATE_REASON="${FACTORY_MERGE_GATE_REASON:-trusted controller decision=${FACTORY_MERGE_GATE_DECISION:-empty}; required pass}"
    return 1
  fi
}

factory_classify_closure_outcome() {
  # Only the canonical success and no-op decisions finish feature delivery.
  # Everything else leaves the issue open under the closure policy's own labels
  # and durable reason; the merge is never treated as acceptance.
  case "$1" in
    closed | already-closed)
      printf 'closed\n'
      ;;
    no-linked-issue)
      printf 'not-applicable\n'
      ;;
    '')
      printf 'error\n'
      ;;
    *)
      printf 'blocked\n'
      ;;
  esac
}

factory_merge_with_canonical_gates() {
  local pr="$1" expected_head="$2"
  local closure_script='' closure_output='' closure_status=0 outcome=''
  export FACTORY_MERGE_STATE FACTORY_CLOSURE_OUTCOME FACTORY_CLOSURE_STATUS
  FACTORY_MERGE_STATE='denied'
  FACTORY_CLOSURE_OUTCOME=''
  FACTORY_CLOSURE_STATUS=''

  factory_run_canonical_merge_gates "$pr" "$expected_head" || return 1
  if ! closure_script="$(factory_trusted_post_merge_closure_script)"; then
    FACTORY_MERGE_GATE_REASON='trusted post-merge closure module is not staged'
    return 1
  fi
  if ! declare -F factory_dispatch_production_deploy >/dev/null; then
    FACTORY_MERGE_GATE_REASON='deploy dispatch helper is unavailable'
    return 1
  fi

  # --match-head-commit makes GitHub reject the merge when the reviewed head
  # moved after the gates were evaluated.
  if gh pr merge "$pr" --merge --match-head-commit "$expected_head" --delete-branch; then
    factory_dispatch_production_deploy "Factory ${WORKER:-worker} merged PR #${pr}"
  else
    FACTORY_MERGE_STATE='merge-failed'
    FACTORY_MERGE_GATE_REASON="merge command failed for head ${expected_head}"
    return 2
  fi
  FACTORY_MERGE_STATE='merged'

  printf '[factory-merge] Executing post-merge closure for PR #%s using %s\n' "$pr" "$closure_script" >&2
  closure_output="$(python3 "$closure_script" --pr "$pr" 2>&1)" || closure_status=$?
  # The closure module prints its decision on the last non-empty line; error
  # output can be multi-line.
  # Prefer explicit canonical outcomes; otherwise take last non-empty line.
  # Any non-canonical line conservatively maps to blocked, never to success.
  outcome="$(printf '%s\n' "$closure_output" | awk 'NF { last=$0; if ($0=="closed"||$0=="already-closed"||$0=="no-linked-issue") ok=$0 } END { print (ok?ok:last) }')"
  FACTORY_CLOSURE_OUTCOME="$outcome"
  if (( closure_status != 0 )); then
    FACTORY_CLOSURE_STATUS='error'
    [[ -n "$outcome" ]] || FACTORY_CLOSURE_OUTCOME="post-merge closure exited ${closure_status}"
    return 3
  fi
  FACTORY_CLOSURE_STATUS="$(factory_classify_closure_outcome "$outcome")"
  case "$FACTORY_CLOSURE_STATUS" in
    closed | not-applicable) return 0 ;;
    *) return 3 ;;
  esac
}
