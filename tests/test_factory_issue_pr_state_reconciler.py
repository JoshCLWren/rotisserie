"""Regression coverage for scheduled factory issue-state reconciliation."""

from pathlib import Path


WORKFLOW = Path(".github/workflows/factory-issue-pr-state-reconciler.yml")


def test_factory_issue_state_reconciler_has_periodic_sweep() -> None:
    """Old orphaned issue labels must be repaired without a new PR event."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "  schedule:" in workflow
    assert "    - cron: '*/15 * * * *'" in workflow
