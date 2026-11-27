"""factory:ready on an issue only mirrors an open linked PR that is ready."""

from __future__ import annotations

from typing import Any

import factory_self_heal_issue_ready as issue_ready
from tests.test_factory_item_loop import heal_budget


class Hub:
    repo = "o/r"

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.deleted: list[str] = []

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if path.startswith("issues?labels="):
            return self.issues
        if "--method" in args and "DELETE" in args:
            self.deleted.append(path)
        return {}


def issue(number: int) -> dict[str, Any]:
    return {"number": number, "labels": [{"name": "factory"}, {"name": "factory:ready"}]}


def pull(number: int, issue_number: int, *labels: str) -> dict[str, Any]:
    branch = f"factory/work-checkpoints/issue-{issue_number}/model/1"
    return {
        "number": number,
        "head": {"ref": branch, "sha": "a" * 40},
        "body": f"Part of #{issue_number}.",
        "title": "slice",
        "labels": [{"name": name} for name in ("factory", *labels)],
    }


def test_an_issue_keeps_ready_only_while_its_ready_pr_is_open() -> None:
    """ComicPile #5701 to #5723 and #5525 sat at factory:ready with no ready PR and never ran."""
    hub = Hub([issue(5701), issue(5525), issue(5900)])
    pulls = [pull(5990, 5900, "factory:ready"), pull(5991, 5701, "factory:review")]
    found = issue_ready.sync_issue_ready(hub, None, pulls, heal_budget())  # type: ignore[arg-type]
    assert sorted(row["issue"] for row in found) == [5525, 5701]
    assert hub.deleted == ["issues/5701/labels/factory%3Aready", "issues/5525/labels/factory%3Aready"]


def test_pull_requests_in_the_label_listing_are_left_alone() -> None:
    hub = Hub([{**issue(5848), "pull_request": {"url": "x"}}])
    assert issue_ready.sync_issue_ready(hub, None, [], heal_budget()) == []  # type: ignore[arg-type]
    assert hub.deleted == []
