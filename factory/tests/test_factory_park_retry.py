"""A parked PR closes as a failed attempt; the second failed attempt splits its issue."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import factory_review_policy as policy
import factory_self_heal as heal
import factory_self_heal_loops as loops
import factory_self_heal_park_retry as retry
import factory_self_heal_rules as rules
import factory_split_plan as plans
from factory_adopter import MARKER_PREFIX
import pytest
from tests.test_factory_item_loop import HEAD, heal_budget

from tests import turso_support as support

PR, ISSUE = 12, 40
BRANCH = f"factory/7-{ISSUE}-normalize"


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loops, "incident_pulls", lambda *_a: frozenset())
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


class Hub:
    def __init__(
        self,
        repo: str,
        issue_labels: tuple[str, ...] = ("factory", "factory:unowned", "factory:review"),
        issue_comments: tuple[str, ...] = (),
        issue_body: str = "Normalize the key.",
    ) -> None:
        self.repo = repo
        self.writes: list[tuple[str, tuple[str, ...], Any]] = []
        self.issue_labels = issue_labels
        self.issue_comments = issue_comments
        self.issue_body = issue_body

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if not args:
            return {
                "number": ISSUE,
                "state": "open",
                "body": self.issue_body,
                "labels": [{"name": name} for name in self.issue_labels],
            }
        self.writes.append((path, args, payload))
        return {}

    def comments(self, number: int) -> list[dict[str, Any]]:
        if number == ISSUE:
            return [{"body": body} for body in self.issue_comments]
        if number != PR:
            return []
        first = policy.review_marker(
            pr=PR, head=HEAD, reviewer="8", producer="7", verdict="repair"
        )
        verdict = f"{first}\nThe change misses the null case."
        return [{"body": verdict, "user": {"login": policy.TRUSTED_COMMENT_LOGIN}}]

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        raise AssertionError(query)

    def paths(self) -> list[str]:
        return [path for path, _args, _payload in self.writes]


def pull(*labels: str) -> dict[str, Any]:
    names = ["factory", "factory:unowned", "factory:review", "factory:parked", *labels]
    return {
        "number": PR,
        "title": "Normalize the API key",
        "body": f"Closes #{ISSUE}",
        "html_url": f"https://github.com/o/r/pull/{PR}",
        "head": {"sha": HEAD, "ref": BRANCH},
        "labels": [{"name": name} for name in names],
    }


def park(db: Any, repo: str, hours: float) -> None:
    heal.LedgerStore(repo).record(
        [
            heal.event(
                kind=rules.LOOP_PARKED,
                pr=PR,
                head=HEAD,
                ref="review-x",
                text="4 of 4 worker runs changed nothing.",
                stage="factory:review",
            )
        ]
    )
    age(db, repo, rules.LOOP_PARKED, hours)


def age(db: Any, repo: str, kind: str, hours: float) -> None:
    db.batch(
        [
            (
                "UPDATE factory_state_events SET created_at = created_at - :s"
                " WHERE repo = :repo AND detail ->> 'kind' = :kind",
                {"s": int(hours * 3600), "repo": repo, "kind": kind},
            )
        ]
    )


def sweep(repo: str, hub: Hub | None = None, *labels: str) -> tuple[Hub, list[dict[str, Any]]]:
    hub = hub or Hub(repo)
    found = retry.resolve_long_parks(hub, heal.LedgerStore(repo), [pull(*labels)], heal_budget())
    return hub, found




def written(hub: Hub, path: str) -> Any:
    return next(payload for at, _args, payload in hub.writes if at == path)


def earlier_closure(pr: int) -> str:
    return f"<!-- {MARKER_PREFIX}-{retry.NOTE_MARKER}:issue-{ISSUE}:pr-{pr}:park-1 -->"


def test_a_fresh_park_closes_the_pr_and_requeues_the_issue(env) -> None:
    db, repo = env
    park(db, repo, 0)
    hub, found = sweep(repo)
    assert found[0]["kind"] == rules.PARK_CLOSED and found[0]["issue_status"] == "requeued"
    assert hub.paths()[-1] == f"pulls/{PR}"
    assert hub.writes[-1][2] == {"state": "closed"}
    assert not any("git/refs" in path or "dispatches" in path for path in hub.paths())
    text = str(written(hub, f"issues/{PR}/comments")["body"])
    assert "failed attempt" in text and BRANCH in text
    assert "4 of 4 worker runs changed nothing." in text
    assert "repair at `dddddddddddd`: The change misses the null case." in text
    assert f"pull/{PR}" in str(written(hub, f"issues/{ISSUE}/comments")["body"])
    labels = written(hub, f"issues/{ISSUE}/labels")["labels"]
    assert "factory:review" not in labels and retry.SPLIT_LABEL not in labels


def test_a_reopened_pr_is_not_closed_again(env) -> None:
    db, repo = env
    park(db, repo, 0)
    sweep(repo)
    reopened, found = sweep(repo)
    assert f"pulls/{PR}" not in reopened.paths()
    assert found == [{"pr": PR, "status": "reopened after closure"}]


def test_the_second_failed_attempt_labels_the_issue_for_a_split(env) -> None:
    db, repo = env
    park(db, repo, 0)
    hub, found = sweep(repo, Hub(repo, issue_comments=(earlier_closure(7),)))
    assert found[0]["issue_status"] == "split"
    assert retry.SPLIT_LABEL in written(hub, f"issues/{ISSUE}/labels")["labels"]
    note = str(written(hub, f"issues/{ISSUE}/comments")["body"])
    assert "2 failed attempts" in note and "splits it" in note


def test_the_same_pr_closed_before_does_not_count_twice(env) -> None:
    db, repo = env
    park(db, repo, 0)
    hub, found = sweep(repo, Hub(repo, issue_comments=(earlier_closure(PR),)))
    assert found[0]["issue_status"] == "requeued"


def test_a_split_child_is_requeued_without_the_split_label(env) -> None:
    db, repo = env
    park(db, repo, 0)
    child = f"<!-- {MARKER_PREFIX}-{plans.CHILD_MARKER}:parent-3:index-1 -->\nPart of #3."
    hub, found = sweep(repo, Hub(repo, issue_comments=(earlier_closure(7),), issue_body=child))
    assert found[0]["issue_status"] == "requeued"
    assert retry.SPLIT_LABEL not in written(hub, f"issues/{ISSUE}/labels")["labels"]


def test_an_owner_override_keeps_the_pr_open(env) -> None:
    db, repo = env
    park(db, repo, 0)
    hub, found = sweep(repo, None, "factory:override")
    assert hub.writes == [] and found == [{"pr": PR, "status": "owner"}]


def test_a_pushed_head_is_no_longer_parked(env) -> None:
    db, repo = env
    park(db, repo, 0)
    hub = Hub(repo)
    moved = {**pull(), "head": {"sha": "e" * 40, "ref": BRANCH}}
    found = retry.resolve_long_parks(hub, heal.LedgerStore(repo), [moved], heal_budget())
    assert found == [] and hub.writes == []


def test_one_sweep_closes_at_most_three_parks(env) -> None:
    db, repo = env
    numbers = (12, 13, 14, 15)
    heal.LedgerStore(repo).record(
        [
            heal.event(kind=rules.LOOP_PARKED, pr=n, head=HEAD, ref="review-x", text="t",
                       stage="factory:review")
            for n in numbers
        ]
    )
    hub = Hub(repo)
    pulls = [{**pull(), "number": n} for n in numbers]
    found = retry.resolve_long_parks(hub, heal.LedgerStore(repo), pulls, heal_budget())
    assert [p for p in hub.paths() if p.startswith("pulls/")] == ["pulls/12", "pulls/13", "pulls/14"]
    assert {"pr": 15, "status": "would-close"} in found
