"""The console counts parked PRs as the dispatcher does: outside the review backlog.

The 2026-11-25 stall: six parked PRs filled a review backlog limit of six, and
`baste why` said issues "wait for the review backlog to drain" though nothing
could drain it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from test_factory_console import (  # type: ignore[import-not-found]
    REPO,
    FakeGitHub,
    factory_pr,
    fake,
    invoke,
    open_issue,
    why_routes,
)

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import turning

__all__ = ["fake"]

HEAD = "d" * 40
NOW = datetime(2026, 11, 25, 8, 30, tzinfo=UTC)


def park_comment(number: int) -> dict[str, Any]:
    rules = runtime.module("factory_self_heal_rules")
    body = rules.comment_body(
        kind="loop-parked", pr=number, head=HEAD, ref=f"review-{HEAD[:12]}", text="Parked it."
    )
    return {"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}


def parked_board(fake: FakeGitHub, count: int, issue: Any) -> None:
    numbers = [5700 + n for n in range(count)]
    why_routes(fake, [issue], [factory_pr(n, 900 + n, "factory:review") for n in numbers])
    fake.routes["search/issues"] = {"items": [{"number": n} for n in numbers]}
    for number in numbers:
        fake.routes[rf"issues/{number}/comments"] = [park_comment(number)]


@pytest.fixture(autouse=True)
def default_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_PARKED_PR_LIMIT", raising=False)


def test_why_lets_an_issue_start_when_the_backlog_is_all_parked(fake: FakeGitHub) -> None:
    parked_board(fake, 6, open_issue(41, "factory:unowned"))
    code, out = invoke(["--json", "why", "issue:41"])
    data = json.loads(out)
    assert code == 0
    assert data["status"] == "ready"
    assert data["explanation"]["review_backlog"] == 0
    assert data["explanation"]["parked"] == 6


def test_why_names_the_parked_cap_when_it_is_reached(fake: FakeGitHub) -> None:
    parked_board(fake, 12, open_issue(41, "factory:unowned"))
    code, out = invoke(["why", "issue:41"])
    assert code == 0
    assert "12 parked PRs waiting on the owner (limit 12, FACTORY_PARKED_PR_LIMIT)" in out
    assert "review backlog to drain" not in out


def test_turning_says_the_backlog_is_full_of_parked_prs(fake: FakeGitHub) -> None:
    parked_board(fake, 12, open_issue(41, "factory:unowned"))
    found = turning.backlog_state(GitHub(REPO, executable="sh"), NOW)
    assert found["limit"] == "parked-limit"
    assert "full of parked PRs: 12 parked PRs are waiting on the owner" in found["limit_text"]
    assert turning.NEXT[found["limit"]] == "baste recap"


def test_turning_sees_no_limit_when_six_parked_prs_meet_a_limit_of_six(fake: FakeGitHub) -> None:
    parked_board(fake, 6, open_issue(41, "factory:unowned"))
    found = turning.backlog_state(GitHub(REPO, executable="sh"), NOW)
    assert found["limit"] == ""
    assert found["open_prs"] == 0
    assert found["eligible_issues"] == 1


def test_turning_counts_looping_prs_outside_the_backlog(fake: FakeGitHub) -> None:
    numbers = [5700 + n for n in range(6)]
    prs = [factory_pr(n, 900 + n, "factory:review") for n in numbers]
    why_routes(fake, [open_issue(41, "factory:unowned")], prs)
    github = GitHub(REPO, executable="sh")
    assert turning.backlog_state(github, NOW)["limit"] == "review-limit"
    found = turning.backlog_state(github, NOW, frozenset(numbers))
    assert (found["limit"], found["eligible_issues"]) == ("", 1)
