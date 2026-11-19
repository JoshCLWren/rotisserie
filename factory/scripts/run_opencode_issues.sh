#!/usr/bin/env bash
# Run opencode on each open issue that doesn't already have an open PR.
# Issues with open PRs (as of 2026-03-24): #323 #324 #326 #328
# Epics closed and split: #193 → #376 #377 #378 | #374 → #379 #380
#
# Full list (22 issues, priority order):
#   Bugs:    #357 #361 #362
#   UX:      #358 #366 #367 #368 #370 #373 #379 #359 #360 #363 #365 #369 #371 #372
#   API:     #376 #377 #378
#   Complex: #364 #380
#
# Usage:
#   ./scripts/run_opencode_issues.sh
#   TIMEOUT=60m ./scripts/run_opencode_issues.sh

set -uo pipefail

TIMEOUT="${TIMEOUT:-45m}"
LOG_DIR="$(dirname "$0")/../.opencode_logs"
mkdir -p "$LOG_DIR"

# Default model: use environment variable if set, otherwise use a known working model
# Skip specific problematic model only: mistralai/mistral-small-3.1-24b-instruct
OPcode_MODEL="${OPcode_MODEL:-opencode/nemotron-3-super-free}"

# If the selected model is known to cause ProviderModelNotFoundError, fall back to safe default
# Perform case-insensitive check to catch all variants (e.g., MistralAI/..., with :free suffix)
lower_model=$(echo "$OPcode_MODEL" | tr '[:upper:]' '[:lower:]')
if [[ "$lower_model" =~ mistral-small-3\.1-24b-instruct ]]; then
  echo "Warning: Model $OPcode_MODEL is known to cause ProviderModelNotFoundError. Falling back to opencode/nemotron-3-super-free."
  OPcode_MODEL="opencode/nemotron-3-super-free" # safe fallback for problematic models
fi

# Ordered: bugs first, then simpler frontend-only UX, then complex UX, then API/onboarding
ISSUES=(357 361 362 358 366 367 368 370 373 379 359 360 363 365 369 371 372 376 377 378 364 380)

echo "Issues to process: ${ISSUES[*]}"
echo "Timeout per issue: $TIMEOUT"
echo "Logs: $LOG_DIR"
echo ""

for issue in "${ISSUES[@]}"; do
    log="$LOG_DIR/issue_${issue}.log"

    # Skip if an open PR already references this issue
    if gh pr list --state open --json body --jq '.[].body' 2>/dev/null | grep -qi "#${issue}"; then
        echo "[SKIP] #$issue — open PR already exists"
        continue
    fi

    title=$(gh issue view "$issue" --json title --jq '.title' 2>/dev/null || echo "unknown")
    echo "[START] #$issue — $title"

    prompt="Fix GitHub issue #${issue} in the comic-pile repo.

WORKFLOW:
1. Read the issue: gh issue view ${issue}
2. Spawn a sub-agent to implement the fix
3. Spawn a second sub-agent to harshly review the implementation (run git diff main, lint, tests)
4. Spawn a third sub-agent to address all valid criticisms from the review
5. Stop after 2 review rounds — do not loop indefinitely
6. Open a PR with: gh pr create --title '...' --body 'Closes #${issue}\n\n...'

STANDARDS:
- Run make lint (ruff + pyright must pass)
- Run make pytest (all tests must pass, 96%+ coverage)
- No Any types
- Mobile-first, touch targets 44px+ for UI changes
- Update docs/changelog.md

Do not ask clarifying questions. Start immediately."

    if timeout "$TIMEOUT" opencode run -m "$OPcode_MODEL" "$prompt" >"$log" 2>&1; then
        echo "[DONE]  #$issue — see $log"
    else
        exit_code=$?
        if [[ $exit_code -eq 124 ]]; then
            echo "[TIMEOUT] #$issue after $TIMEOUT — see $log"
        else
            echo "[FAILED] #$issue (exit $exit_code) — see $log"
        fi
    fi

    echo ""
done

echo "All issues processed."
