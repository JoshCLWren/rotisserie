"""After 6 hours with no eligible reviewer, the factory allows self-review on that one head."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import factory_review_exclusion as exclusion
import factory_review_policy as policy
import factory_self_heal as heal
import factory_self_heal_self_review as auto
import factory_self_review_allowance as allowance
import factory_turso_events as events
import pytest
from tests.test_factory_item_loop import heal_budget

from tests import turso_support as support

PR = 31


@dataclass(frozen=True)
class Verdict:
    eligible: bool
    reason: str


HEAD = "a" * 40
NEW = "b" * 40
REASON = "No reviewer model: 3 model(s) outside exclusions are eligible and all 3 authored"


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def no_reviewer(repo: str, run: int, head: str = HEAD, detail: str = REASON) -> None:
    events.record_runtime_outcome(
        repo,
        str(run),
        {
            "worker": support.uncapped_workers(1)[0],
            "outcome_class": "no_capacity",
            "detail": detail,
            "selected_kind": "pr",
            "selected_number": str(PR),
            "action_kind": "review",
            "assigned_head": head,
        },
    )


def age(db: Any, repo: str, run: int, hours: float) -> None:
    db.batch(
        [
            (
                "UPDATE factory_state_events SET created_at = created_at - :s"
                " WHERE repo = :repo AND event_id = :e",
                {"s": int(hours * 3600), "repo": repo, "e": f"runtime:{run}"},
            )
        ]
    )


class Hub:
    def __init__(self, repo: str, comments: list[dict[str, Any]] | None = None) -> None:
        self.repo = repo
        self.writes: list[tuple[str, Any]] = []
        self.records = comments or []

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        self.writes.append((path, payload))
        return {}

    def comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.records)


def pull(head: str = HEAD, *labels: str) -> dict[str, Any]:
    names = ["factory", "factory:unowned", "factory:review", *labels]
    return {
        "number": PR,
        "state": "open",
        "head": {"sha": head},
        "base": {"ref": "main"},
        "labels": [{"name": name} for name in names],
    }


@pytest.fixture
def authored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        exclusion,
        "for_pull",
        lambda _fetch, pr, *, pull, records: SimpleNamespace(
            head=pull["head"]["sha"], excluded=("lab/model",)
        ),
    )


def sweep(repo: str, hub: Hub, row: dict[str, Any]) -> list[dict[str, Any]]:
    return auto.allow_long_waits(hub, heal.LedgerStore(repo), [row], heal_budget())


def trusted(body: str) -> dict[str, Any]:
    return {"body": body, "user": {"login": policy.TRUSTED_COMMENT_LOGIN}}


def test_only_a_no_reviewer_outcome_dates_the_wait(env) -> None:
    db, repo = env
    no_reviewer(repo, 1)
    no_reviewer(repo, 2, detail="No capacity: every model is cooling")
    rows = db.reads([auto.statement(repo)])[0]
    assert [(row["pr"], row["head"]) for row in rows] == [(str(PR), HEAD)]


def test_a_head_that_waited_six_hours_gets_one_automatic_grant(env, authored) -> None:
    db, repo = env
    no_reviewer(repo, 1)
    age(db, repo, 1, 7)
    no_reviewer(repo, 2)
    hub = Hub(repo)
    found = sweep(repo, hub, pull())
    assert found[0]["status"] == "self-review allowed"
    path, payload = hub.writes[-1]
    assert path == f"issues/{PR}/comments"
    body = str(payload["body"])  # type: ignore[index]
    assert f"<!-- factory-self-review-auto pr={PR} head={HEAD} -->" in body
    assert "no eligible reviewer for 7h" in body
    stored = db.reads(
        [
            (
                "SELECT detail ->> 'automatic' AS auto, detail ->> 'reason' AS reason"
                " FROM factory_state_events WHERE repo = :repo AND event_type = 'self_review_allowed'",
                {"repo": repo},
            )
        ]
    )[0]
    assert stored[0]["auto"] == 1 and "lab/model" in stored[0]["reason"]
    again = Hub(repo, [trusted(body)])
    assert sweep(repo, again, pull()) == [{"pr": PR, "status": "already granted"}]
    assert again.writes == []


@pytest.mark.parametrize(
    ("first", "last", "head"),
    [(5, 0, HEAD), (9, 4, HEAD), (7, 0, NEW)],
    ids=["under six hours", "no longer waiting", "a newer head"],
)
def test_no_grant_without_a_long_current_wait(env, authored, first, last, head) -> None:
    db, repo = env
    no_reviewer(repo, 1)
    age(db, repo, 1, first)
    no_reviewer(repo, 2)
    age(db, repo, 2, last)
    hub = Hub(repo)
    assert sweep(repo, hub, pull(head)) == []
    assert hub.writes == []


def test_an_owner_override_skips_the_grant(env, authored) -> None:
    db, repo = env
    no_reviewer(repo, 1)
    age(db, repo, 1, 7)
    no_reviewer(repo, 2)
    hub = Hub(repo)
    assert sweep(repo, hub, pull(HEAD, "factory:override"))[0]["status"].startswith("owner")
    assert hub.writes == []


def test_the_automatic_grant_lifts_the_exclusion_for_its_head_only() -> None:
    marker = f"<!-- factory-self-review-auto pr={PR} head={HEAD} -->"
    found = allowance.check(lambda _p: None, PR, pull=pull(), head=HEAD, comments=[marker])
    assert found.allowed and found.actor == allowance.AUTO_ACTOR
    assert not allowance.check(
        lambda _p: None, PR, pull=pull(NEW), head=NEW, comments=[marker]
    ).allowed
    closed = {**pull(), "state": "closed"}
    assert not allowance.check(
        lambda _p: None, PR, pull=closed, head=HEAD, comments=[marker]
    ).allowed
    verdict = allowance.noted(Verdict(eligible=True, reason="Approved."), allowance.AUTO_ACTOR)
    assert verdict.reason == (
        "Approved. Self-review allowed by the factory: no eligible reviewer for 6 hours."
    )


def test_the_entry_persister_hands_the_assigned_head_to_the_store() -> None:
    entry = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    text = (entry / "free-model-factory-entry.yml").read_text()
    assert ".assigned_head = $head" in text
    assert "select(.kind == \"pr\") | .head" in text
