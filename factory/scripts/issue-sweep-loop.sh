#!/usr/bin/env bash
# Issue Sweep Loop — runs each step of archive/historical/ISSUE_SWEEP_PROMPT.md through Codex CLI
# Usage: bash scripts/issue-sweep-loop.sh
# Requires: codex CLI installed, authenticated (codex login)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Create the branch
git checkout -b issue-sweep/complete-issue-management 2>/dev/null || git checkout issue-sweep/complete-issue-management

STEPS=(
  "step1|Fix #261: Change order_by in Thread.issues relationship from Issue.issue_number to Issue.position at app/models/thread.py line 111. Add a test in tests/test_issue_model.py that creates issues with mixed issue_numbers (including an 'Annual') and verifies thread.issues returns them in position order. Run: make lint && pytest tests/test_issue_model.py -v. Commit with message 'Fix #261: Thread.issues relationship orders by position'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 1 for full details."

  "step2|Fix #263: Add UniqueConstraint('thread_id', 'issue_number', name='uq_issue_thread_number') to Issue.__table_args__ in app/models/issue.py. Create an alembic migration that first cleans up any existing duplicates (keeping lowest id), then adds the constraint. Add a test in tests/test_issue_api.py that verifies duplicate issue_numbers in the same thread are rejected. Run: make lint && pytest -v. Commit with message 'Fix #263: Add unique constraint on (thread_id, issue_number)'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 2 for full details."

  "step3|Fix #257: Make position canonical for in-thread ordering. Add validate_position_dependency_consistency() in comic_pile/dependencies.py that finds intra-thread issue dependencies where position order disagrees with dependency order and returns warnings. Add GET /api/v1/threads/{thread_id}/issues:validateOrder endpoint in app/api/issue.py. Add a doc comment on Dependency model noting position is canonical for in-thread order. Add tests. This also resolves #262 and #265. Run: make lint && pytest -v. Commit with message 'Fix #257: Make position canonical, add order validation (also fixes #262, #265)'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 3 for full details."

  "step4|Fix #258: Add insert_after_issue_id optional field to IssueCreateRange schema in app/schemas/issue.py. Modify POST /api/v1/threads/{thread_id}/issues in app/api/issue.py to support inserting at a specific position by shifting subsequent issues. Add issuesApi.create() support in frontend/src/services/api-issues.ts. Add tests for insert-middle, insert-after-last, invalid ID, and next_unread_issue_id update. Run: make lint && pytest tests/test_issue_api.py -v. Commit with message 'Fix #258: Add insert-at-position support for issue creation'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 4 for full details."

  "step5|Fix #259: Add POST /api/v1/issues/{issue_id}:move (single issue move with after_issue_id param) and POST /api/v1/threads/{thread_id}/issues:reorder (bulk reorder with ordered issue_ids list) endpoints in app/api/issue.py. Add IssueMoveRequest and IssueReorderRequest schemas in app/schemas/issue.py. Add issuesApi.move() and issuesApi.reorder() in frontend/src/services/api-issues.ts. Use SELECT FOR UPDATE. Add tests for move-to-top/middle/end, bulk reorder, invalid IDs, next_unread recalc. Run: make lint && pytest tests/test_issue_api.py -v. Commit with message 'Fix #259: Add issue move and reorder APIs'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 5 for full details."

  "step6|Fix #260: Add DELETE /api/v1/issues/{issue_id} endpoint in app/api/issue.py. Delete the issue, shift subsequent positions down, update total_issues/issues_remaining/next_unread_issue_id/reading_progress on the thread. Log an issue_deleted event. Add issuesApi.delete() in frontend/src/services/api-issues.ts. Add tests for delete-middle, delete-last, delete-next-unread, delete-all-issues, delete-nonexistent. Run: make lint && pytest tests/test_issue_api.py -v. Commit with message 'Fix #260: Add individual issue deletion API'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 6 for full details."

  "step7|Fix #266: Add drag-and-drop issue reordering to IssueToggleList in frontend/src/pages/QueuePage.tsx. Follow the existing thread drag-and-drop pattern (lines 240-370) using HTML5 drag events. On drop, call issuesApi.move() or issuesApi.reorder(). Add optimistic UI update with revert on error. Add a delete button (small x) on each issue pill with confirmation. Add vitest unit tests. Run: cd frontend && pnpm test && pnpm run build. Commit with message 'Fix #266: Add frontend issue reorder and delete UI'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 7 for full details."

  "step8|Fix #264: Add deprecation header comments to these scripts: archive/scripts-oneoff/add_xmen_annuals.py, archive/scripts-oneoff/fix_thread_positions.py, archive/scripts-oneoff/audit_thread_positions.py, archive/scripts-oneoff/complete_annual_dependencies.py, archive/scripts-oneoff/create_xmen_dependencies.py, archive/scripts-oneoff/check_xmen_dependencies.py. Comment should say DEPRECATED and reference the replacement API endpoints. Do NOT delete the scripts. Run: make lint. Commit with message 'Fix #264: Deprecate compensating scripts'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 8 for full details."

  "step9|Fix #248: Add regression tests to tests/test_session_api.py verifying session hydration preserves issue metadata (next_issue_id, next_issue_number) after refetch. Test: session with issues has populated fields, non-issue-tracked thread has null fields, completed thread has null next_issue. Run: make lint && pytest tests/test_session_api.py -v. Commit with message 'Fix #248: Add session issue metadata regression tests'. Read AGENTS.md and archive/historical/ISSUE_SWEEP_PROMPT.md Step 9 for full details."
)

echo "=== Issue Sweep Loop ==="
echo "Steps: ${#STEPS[@]}"
echo ""

for entry in "${STEPS[@]}"; do
  name="${entry%%|*}"
  prompt="${entry#*|}"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "▶ Running: $name"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  if codex exec \
    --full-auto \
    -o "/tmp/issue-sweep-${name}.md" \
    "$prompt"
  then
    echo "✅ $name completed"
    echo ""
  else
    exit_code=$?
    echo "❌ $name FAILED (exit code $exit_code)"
    echo "   Check /tmp/issue-sweep-${name}.md for details"
    echo "   Fix the issue manually, then re-run this script"
    echo "   (completed steps will be skipped via git branch state)"
    exit 1
  fi
done

echo ""
echo "=== All steps complete ==="
echo "Run final verification:"
echo "  make lint && pytest && cd frontend && pnpm test && pnpm run build"
