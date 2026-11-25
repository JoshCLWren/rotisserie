"""Review and comment runs of the issue-PR state reconciler queue per PR, not globally."""

from __future__ import annotations

from pathlib import Path

CALLER = (
    Path(__file__).resolve().parents[1]
    / "factory/install/callers/factory-issue-pr-state-reconciler.yml"
)


def test_comment_and_review_events_use_a_per_pr_group() -> None:
    group = next(
        line for line in CALLER.read_text().splitlines() if line.strip().startswith("group:")
    )
    for event in ("pull_request_review", "pull_request_review_comment", "issue_comment"):
        assert f"github.event_name == '{event}'" in group, event
    assert "github.event.pull_request.number || github.event.issue.number" in group
