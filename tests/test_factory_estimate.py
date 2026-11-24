"""factory estimate: attempts, time and unattended chance from similar past factory items."""

from __future__ import annotations

import io
import json
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from rotisserie import web
from rotisserie.factory_console.commands.estimate import render
from rotisserie.factory_console.context import Context
from rotisserie.factory_console.github import GitHub, GitHubNotFoundError
from rotisserie.factory_console.service import estimate, estimate_history
from rotisserie.factory_console.service import estimate_model as model
from rotisserie.factory_console.tracker import Clock
from rotisserie.factory_console.ui import Console, Prompt

REPO = "acme/widgets"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
BOT = {"__typename": "Bot", "login": "github-actions"}
PERSON = {"__typename": "User", "login": "owner"}


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def claim(kind: str, number: int, worker: str, moment: datetime) -> dict[str, Any]:
    epoch = int(moment.timestamp())
    body = f"<!-- widgets-factory-controller-claim-v1:{kind}-{number}:{worker}:{epoch} -->"
    return {"createdAt": stamp(moment), "author": BOT, "body": body}


def node(
    number: int,
    *,
    pull: bool,
    labels: list[str],
    body: str = "",
    comments: list[dict[str, Any]] | None = None,
    state: str = "CLOSED",
    created: datetime | None = None,
    merged: datetime | None = None,
    merged_by: dict[str, str] | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    rows = comments or []
    found: dict[str, Any] = {
        "number": number,
        "state": "MERGED" if merged else state,
        "createdAt": stamp(created or NOW - timedelta(days=3)),
        "closedAt": stamp(merged) if merged else None,
        "body": body,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "comments": {"totalCount": len(rows) if total is None else total, "nodes": rows},
        "timelineItems": {"nodes": [{"label": {"name": name}} for name in labels]},
    }
    if pull:
        found.update(
            mergedAt=stamp(merged) if merged else None,
            mergedBy=merged_by if merged else None,
            closingIssuesReferences={"nodes": []},
        )
    return found


def finished_issue(
    number: int,
    *,
    attempts: int,
    hours: float,
    labels: list[str],
    size: int = 2000,
    merged_by: dict[str, str] = BOT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    start = NOW - timedelta(days=2)
    issue_claims = [
        claim("issue", number, f"w{i}", start + timedelta(minutes=10 * i)) for i in range(attempts)
    ]
    mirrored = claim("pr", number + 1000, "w0", start + timedelta(seconds=5))
    pull = node(
        number + 1000,
        pull=True,
        labels=["factory", "via:nvidia/kimi"],
        body=f"Closes #{number}.",
        comments=[mirrored],
        created=start + timedelta(hours=1),
        merged=start + timedelta(hours=hours),
        merged_by=merged_by,
    )
    issue = node(number, pull=False, labels=labels, body="x" * size, comments=issue_claims)
    return pull, issue


class FakeGitHub:
    repo = REPO

    def __init__(self, pulls: list[dict[str, Any]], others: dict[int, dict[str, Any]]) -> None:
        self.pulls = pulls
        self.others = others
        self.rest: list[str] = []

    def gh(self, *args: str, **kwargs: Any) -> str:
        query = next(a for a in args if a.startswith("query="))
        limit = {"cost": 1, "remaining": 4000, "resetAt": "2026-11-24T13:00:00Z"}
        if "search(" in query:
            search = {"pageInfo": {"hasNextPage": False, "endCursor": ""}, "nodes": self.pulls}
            return json.dumps({"data": {"rateLimit": limit, "search": search}})
        found = {f"n{n}": self.others[n] for n in self.others if f"n{n}:" in query}
        return json.dumps({"data": {"rateLimit": limit, "repository": found}})

    def repo_api(self, route: str, **kwargs: Any) -> Any:
        self.rest.append(route)
        if route.startswith("contents/factory-stats.json"):
            raise GitHubNotFoundError("none")
        if route.startswith("issues/"):
            epoch = int((NOW - timedelta(days=1)).timestamp())
            body = f"<!-- widgets-factory-controller-claim-v1:issue-7:old:{epoch} -->"
            user = {"login": "github-actions[bot]", "type": "Bot"}
            return (
                [{"user": user, "created_at": stamp(NOW), "body": body}]
                if "page=1" in route
                else []
            )
        return {}


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_MARKER_PREFIX", "widgets")
    estimate_history.clear_cache()


def history(count: int, labels: list[str]) -> FakeGitHub:
    pulls: list[dict[str, Any]] = []
    others: dict[int, dict[str, Any]] = {}
    for index in range(count):
        number = 10 + index
        merged_by = PERSON if index < 2 else BOT
        pull, issue = finished_issue(
            number, attempts=index + 1, hours=float(index + 2), labels=labels, merged_by=merged_by
        )
        pulls.append(pull)
        others[number] = issue
    others[100] = node(100, pull=False, labels=labels, body="y" * 1800, state="OPEN")
    return FakeGitHub(pulls, others)


def test_enough_history_gives_ranges_counts_and_the_unattended_chance() -> None:
    github = history(8, ["bug", "factory"])
    data = estimate.estimate(cast(GitHub, github), "issue:100", now=NOW)
    similar = cast(dict[str, Any], data["similar"])
    assert similar["count"] == 8
    assert similar["tier"] == "same start, category, labels and size"
    attempts = cast(dict[str, Any], data["attempts"])
    assert attempts["median"] == 4.5
    assert attempts["n"] == 8
    assert attempts["low"] <= attempts["median"] <= attempts["high"]
    hours = cast(dict[str, Any], data["hours"])
    assert hours["median"] == pytest.approx(statistics.median(range(2, 10)))
    chance = cast(dict[str, Any], data["unattended"])
    assert (chance["unattended"], chance["n"]) == (6, 8)
    assert chance["low"] < 0.75 < chance["high"]
    assert chance["human_signals"] == {"merged_by_person": 2}
    assert cast(dict[str, Any], data["sample"])["kept_items"] == 8
    assert cast(dict[str, Any], data["features"])["origin"] == "issue"


def test_too_little_history_says_so_and_gives_no_number() -> None:
    github = history(3, ["bug", "factory"])
    data = estimate.estimate(cast(GitHub, github), "issue:100", now=NOW)
    assert data["attempts"] is None and data["hours"] is None and data["unattended"] is None
    assert "Too few similar finished items" in str(data["notes"])
    stream = io.StringIO()
    console = Console(stream, env={})
    context = Context(cast(GitHub, github), console, Prompt(console), Clock(), False)
    render(context, data)
    text = stream.getvalue()
    assert "too few for an estimate" in text
    assert "median" not in text


def test_the_target_is_left_out_of_its_own_sample() -> None:
    github = history(8, ["bug", "factory"])
    github.others[100] = github.others[10]
    data = estimate.estimate(cast(GitHub, github), "issue:10", now=NOW)
    assert "issue:10" not in cast(dict[str, Any], data["similar"])["items"]


def test_range_math_uses_the_middle_80_percent_and_a_wilson_interval() -> None:
    values = [float(n) for n in range(1, 11)]
    spread = model.spread(values)
    assert spread == {"median": 5.5, "low": pytest.approx(1.9), "high": pytest.approx(9.1), "n": 10}
    assert model.spread(values[: model.MIN_SIMILAR - 1]) is None
    low, high = model.wilson(5, 10)
    assert low == pytest.approx(1 - high)
    assert low == pytest.approx(0.3122, abs=1e-3)
    assert model.wilson(0, 10)[0] == pytest.approx(0.0)
    assert model.wilson(10, 10)[1] == pytest.approx(1.0)


def features(origin: str, category: str, labels: tuple[str, ...], size: str) -> model.Features:
    return model.Features(origin, category, labels, size, 0)


def item(key: str, found: model.Features) -> model.Item:
    return model.Item(key, (1,), found, "merged", 1, None, None, NOW, NOW, ())


def test_matching_drops_size_then_labels_but_never_start_or_category() -> None:
    target = features("issue", "normal", ("bug",), "small")
    near = [item(f"a{i}", features("issue", "normal", ("bug",), "large")) for i in range(6)]
    loose = [item(f"b{i}", features("issue", "normal", (), "small")) for i in range(6)]
    other = [item(f"c{i}", features("pr", "normal", ("bug",), "small")) for i in range(9)]
    tier, used, found, tried = model.match(target, near + loose + other)
    assert tier == "same start, category and labels"
    assert used == ["origin", "category", "labels"]
    assert {i.key for i in found} == {f"a{i}" for i in range(6)}
    assert [row["count"] for row in tried] == [0, 6]
    tier, _, found, _ = model.match(target, loose + other)
    assert tier == "same start and category" and len(found) == 6
    assert model.match(features("issue", "user-bug", (), "small"), near + loose)[0] == ""


def test_via_and_stage_labels_do_not_change_the_features() -> None:
    github = history(1, ["bug", "factory", "factory:ready", "via:x/y"])
    thread = estimate_history.parse_thread(github.others[10])
    assert thread is not None
    found = model.features(thread, lambda labels: "normal")
    assert found.labels == ("bug",)
    assert found.size == "medium"


def test_mirrored_issue_and_pr_claims_count_once() -> None:
    start = NOW - timedelta(hours=5)
    issue = node(
        7,
        pull=False,
        labels=[],
        comments=[
            claim("issue", 7, "w1", start),
            claim("issue", 7, "w1", start + timedelta(minutes=2)),
            {
                "createdAt": stamp(start),
                "author": PERSON,
                "body": claim("issue", 7, "x", start)["body"],
            },
        ],
    )
    pull = node(
        8, pull=True, labels=[], comments=[claim("pr", 8, "w1", start + timedelta(seconds=3))]
    )
    threads = [estimate_history.parse_thread(n) for n in (issue, pull)]
    starts = model.claims([t for t in threads if t], model.claim_pattern("widgets"))
    assert len(starts) == 2


def test_cut_off_comments_are_read_in_full_over_rest() -> None:
    github = history(0, [])
    github.others[7] = node(7, pull=False, labels=[], comments=[], total=150)
    found = estimate_history.threads(cast(GitHub, github), estimate_history.Budget(), [7])
    assert any(route.startswith("issues/7/comments") for route in github.rest)
    assert len(found[7].comments) == 1 and not found[7].truncated


def test_sample_size_is_bounded() -> None:
    with pytest.raises(ValueError, match="--sample"):
        estimate.estimate(cast(GitHub, history(0, [])), "issue:100", sample=5)


def test_the_web_ui_reads_estimates() -> None:
    assert web.read_command("estimate", {"target": "issue:5"}) == ["estimate", "issue:5"]
    assert web.read_command("estimate", {"target": "pr:9", "sample": "50"}) == [
        "estimate",
        "pr:9",
        "--sample",
        "50",
    ]
    for bad in ({"target": "issue:5 --sample 9"}, {"target": "pr:9", "sample": "9; x"}, {}):
        with pytest.raises(web.InvalidRequest):
            web.read_command("estimate", bad)
    with pytest.raises(web.InvalidRequest):
        web.read_command("estimate", {"target": "issue:5", "limit": "3"})
