"""A PR stage label on an issue only mirrors an open linked PR in that stage."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

import factory_self_heal_issue_ready as issue_ready
from tests.test_factory_item_loop import heal_budget


class Hub:
    repo = "o/r"

    def __init__(self, issues: dict[str, list[dict[str, Any]]]) -> None:
        self.issues = issues
        self.deleted: list[str] = []

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if path.startswith("issues?labels="):
            return self.issues.get(unquote(path.split("=", 1)[1].split("&", 1)[0]), [])
        if "--method" in args and "DELETE" in args:
            self.deleted.append(unquote(path))
        return {}


def issue(number: int) -> dict[str, Any]:
    return {"number": number}


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
    hub = Hub({"factory:ready": [issue(5701), issue(5525), issue(5900)]})
    pulls = [pull(5990, 5900, "factory:ready"), pull(5991, 5701, "factory:review")]
    found = issue_ready.sync_issue_ready(hub, None, pulls, heal_budget())  # type: ignore[arg-type]
    assert sorted(row["issue"] for row in found) == [5525, 5701]
    assert hub.deleted == ["issues/5701/labels/factory:ready", "issues/5525/labels/factory:ready"]


def test_review_ci_and_changes_requested_need_an_open_linked_pr() -> None:
    """ComicPile #3132 kept factory:review and #4335 factory:changes-requested with no PR."""
    hub = Hub(
        {
            "factory:review": [issue(3132), issue(5876)],
            "factory:changes-requested": [issue(4335)],
            "factory:building": [issue(5701)],
        }
    )
    pulls = [pull(5883, 5876, "factory:review")]
    issue_ready.sync_issue_ready(hub, None, pulls, heal_budget())  # type: ignore[arg-type]
    assert hub.deleted == [
        "issues/3132/labels/factory:review",
        "issues/4335/labels/factory:changes-requested",
    ]


def test_pull_requests_in_the_label_listing_are_left_alone() -> None:
    hub = Hub({"factory:ready": [{**issue(5848), "pull_request": {"url": "x"}}]})
    assert issue_ready.sync_issue_ready(hub, None, [], heal_budget()) == []  # type: ignore[arg-type]
    assert hub.deleted == []
