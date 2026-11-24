"""A claim on a named item is never blocked by factory intake (issue #212)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import external, external_target, frontier

REPO = "acme/widgets"
ALL = external.ACTIONS


class Store:
    def __init__(self, leases: list[dict[str, Any]] | None = None) -> None:
        self.leases = leases or []
        self.reserved: list[dict[str, Any]] = []
        self.released: list[str] = []

    def reserve_external(
        self, repo: str, actor: str, client: str, numbers: list[int], **kwargs: Any
    ) -> dict[str, object]:
        if any(set(row["targets"]) & set(numbers) for row in self.leases):
            return {"status": "conflict", "reason": "existing_or_unreconciled_lease"}
        self.reserved.append({"actor": actor, "numbers": numbers, **kwargs})
        return {
            "status": "reserved",
            "claim_id": "11111111-2222-3333-4444-555555555555",
            "expires_at": "2026-11-24T05:00:00+00:00",
        }

    def list_leases(self, repo: str) -> list[dict[str, Any]]:
        return self.leases

    def external_lease_action(self, repo: str, *args: Any, **kwargs: Any) -> dict[str, object]:
        self.released.append(args[2])
        return {"status": "released"}


class Fake:
    repo = REPO

    def __init__(self, items: dict[str, dict[str, Any]]) -> None:
        self.items = items
        self.after_first_read: dict[str, dict[str, Any]] = {}
        self.reads: dict[str, int] = {}

    def repo_api(self, route: str) -> Any:
        self.reads[route] = self.reads.get(route, 0) + 1
        if self.reads[route] > 1 and route in self.after_first_read:
            return self.after_first_read[route]
        return self.items[route]


def issue(number: int, state: str = "open", labels: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "title": f"Issue {number}",
        "body": "Acceptance: it works",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "labels": [{"name": name} for name in labels],
    }


def pull(number: int, labels: tuple[str, ...], head: str = "a" * 40) -> dict[str, Any]:
    return {
        **issue(number, labels=labels),
        "body": "Closes #9",
        "head": {"sha": head, "ref": "fix-9"},
        "user": {"login": "bob"},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
    }


@pytest.fixture(autouse=True)
def no_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a targeted claim must not consult the factory frontier")

    monkeypatch.setattr(frontier, "capture", refuse)


def claim(github: Fake, store: Store, target: str) -> dict[str, object]:
    return external_target.reserve_target(
        cast(GitHub, github),
        store=cast(Any, store),
        actor="alice",
        client="label",
        target=target,
        abilities=ALL,
    )


def test_an_open_issue_is_reserved_whatever_the_factory_is_doing() -> None:
    store = Store()
    github = Fake({"issues/5695": issue(5695, labels=("factory", "factory:unowned"))})
    result = claim(github, store, "issue:5695")
    assert result["status"] == "reserved"
    assert store.reserved[0]["numbers"] == [5695]
    assert store.reserved[0]["wip_exempt"] is True
    assert store.reserved[0]["action"] == "implement"
    assert cast(dict[str, Any], result["handoff"])["action"] == "implement"


def test_an_active_lease_refuses_and_names_the_holder_and_the_next_step() -> None:
    store = Store(
        [
            {
                "targets": [5695],
                "operation": "external:bob/label",
                "claim_id": "c-1",
                "expires_at": "2026-11-24T04:00:00+00:00",
                "active": True,
            }
        ]
    )
    result = claim(Fake({"issues/5695": issue(5695)}), store, "issue:5695")
    assert result["status"] == "conflict"
    assert result["reason"] == "#5695 is held by @bob until 2026-11-24T04:00:00+00:00"
    assert "baste revoke c-1" in str(result["next"])


def test_an_expired_factory_lease_says_how_to_clear_it() -> None:
    store = Store(
        [
            {
                "targets": [5695],
                "operation": "",
                "worker": "1004230852003",
                "expires_at": "2026-11-23T20:00:00+00:00",
                "active": False,
            }
        ]
    )
    result = claim(Fake({"issues/5695": issue(5695)}), store, "issue:5695")
    assert "expired lease by factory worker 1004230852003" in str(result["reason"])
    assert "baste cancel issue:5695" in str(result["next"])


def test_a_closed_issue_says_to_reopen_it() -> None:
    result = claim(Fake({"issues/7": issue(7, state="closed")}), Store(), "issue:7")
    assert result == {
        "status": "conflict",
        "reason": "issue:7 is closed",
        "next": "Reopen issue:7, then claim it again.",
    }


def test_a_pull_request_claim_takes_its_stage_action_and_fences_its_issue() -> None:
    store = Store()
    github = Fake(
        {
            "pulls/12": pull(12, ("factory", "factory:review", "factory:merge-conflict")),
            "issues/9": issue(9),
        }
    )
    result = claim(github, store, "pr:12")
    assert result["status"] == "reserved"
    assert store.reserved[0]["action"] == "merge-conflict"
    assert store.reserved[0]["head"] == "a" * 40
    assert store.reserved[0]["numbers"] == [9, 12]


def test_new_commits_during_the_claim_undo_it() -> None:
    store = Store()
    github = Fake({"pulls/12": pull(12, ("factory:review",)), "issues/9": issue(9)})
    github.after_first_read["pulls/12"] = pull(12, ("factory:review",), head="b" * 40)
    result = claim(github, store, "pr:12")
    assert result["status"] == "conflict"
    assert result["reason"] == "new commits arrived while it was being claimed"
    assert store.released == ["11111111-2222-3333-4444-555555555555"]


def test_a_review_stage_pr_by_the_claimant_is_worked_not_reviewed() -> None:
    item = pull(12, ("factory:review",))
    github = Fake({})
    assert external_target.action_for(cast(GitHub, github), "pr", 12, item, "bob") == (
        "review-findings"
    )
    assert external_target.action_for(cast(GitHub, github), "pr", 12, item, "alice") == "review"
