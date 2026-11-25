"""A forced or targeted run ends a park in the store; incident-work and closed-item labels."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import factory_dispatch_holds as holds
import factory_self_heal as heal
import factory_self_heal_incident as incident_pass
import factory_self_heal_rules as rules
import factory_state_labels as state
import factory_turso_loops as turso_loops
import pytest

from tests import turso_support as support
from tests.test_factory_item_loop import HEAD, Hub, Store, failures, heal_budget

operations = importlib.import_module("factory_turso_operations")


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def park(repo: str, number: int) -> None:
    heal.LedgerStore(repo).record(
        [
            heal.event(
                kind=rules.LOOP_PARKED, pr=number, head=HEAD, ref="review-x", text="t",
                stage="factory:review",
            )
        ]
    )


def test_a_release_ends_the_park_and_old_runs_stop_counting(env) -> None:
    _, repo = env
    failures(repo, 12, 5)
    park(repo, 12)
    assert operations.cooling_prs(repo, {12: (HEAD, "factory:review")}) == {12}
    assert turso_loops.release(repo, 12, HEAD, "900") is True
    assert operations.cooling_prs(repo, {12: (HEAD, "factory:review")}) == set()
    assert turso_loops.release(repo, 12, HEAD, "901") is False


def test_a_release_without_a_park_records_nothing(env) -> None:
    _, repo = env
    assert turso_loops.release(repo, 13, HEAD, "900") is False


def test_the_parks_reader_orders_a_release_after_its_park() -> None:
    rows = [
        {"created_at": 200, "pr": "12", "head": HEAD, "stage": "", "kind": "loop-released"},
        {"created_at": 100, "pr": "12", "head": HEAD, "stage": "", "kind": "loop-parked"},
        {"created_at": 300, "pr": "14", "head": HEAD, "stage": "", "kind": "loop-released"},
        {"created_at": 400, "pr": "14", "head": HEAD, "stage": "", "kind": "loop-parked"},
    ]
    found = turso_loops.parks(rows)
    assert found["pr:12"]["at_heads"] == set()
    assert turso_loops.parked_heads(found["pr:14"]) == {HEAD}


class Candidate:
    kind = "pr"
    number = 12


def test_released_drops_the_parked_label(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(turso_loops, "release", lambda *_a: True)
    monkeypatch.setattr(holds.subprocess, "run", lambda args, **_k: calls.append(args))
    prs = [{"number": 12, "headRefOid": HEAD, "labels": [{"name": state.PARKED}]}]
    assert holds.released("o/r", Candidate(), prs) is True
    assert calls and calls[0][-1].endswith("/issues/12/labels/factory%3Aparked")


def test_released_never_blocks_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_a: Any) -> bool:
        raise RuntimeError("store down")

    monkeypatch.setattr(turso_loops, "release", broken)
    assert holds.released("o/r", Candidate(), [{"number": 12, "headRefOid": HEAD}]) is True


def test_a_targeted_run_releases_the_park(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_factory_item_loop import controller_with

    controller, picked = controller_with(monkeypatch, lambda *_a, **_k: {})
    seen: list[int] = []
    monkeypatch.setattr(
        controller.factory_dispatch_holds,
        "released",
        lambda _repo, candidate, _prs: seen.append(candidate.number) or True,
    )
    controller.assign_from_pool("6", target=("pr", 12))
    assert seen == [12] and [c.number for c in picked] == [12]


class Issues(Hub):
    def __init__(self, rows: dict[str, Any]) -> None:
        super().__init__()
        self.rows = rows

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if not args and path in self.rows:
            return self.rows[path]
        return super().api(path, *args, payload=payload)

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        self.query = str(variables["q"])
        return self.rows["graphql"]


def item(number: int, *labels: str) -> dict[str, Any]:
    return {"number": number, "state": "open", "labels": [{"name": n} for n in labels]}


def pr(number: int, issue: int, *labels: str) -> dict[str, Any]:
    return {**item(number, *labels), "body": f"Closes #{issue}.", "head": {"ref": "x"}}


def test_incident_work_marks_the_issue_and_its_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_INCIDENTS", "40:3")
    hub = Issues({"issues/40": item(40, "factory:incident"), state.INCIDENT_WORK_LISTING: []})
    found = incident_pass.sync_incident_work(hub, Store(), [pr(41, 40), pr(42, 9)], heal_budget())
    assert {(row["item"], row["action"]) for row in found} == {(40, "add"), (41, "add")}


def test_incident_work_goes_when_the_incident_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_INCIDENTS", "40:3")
    listing = [item(40, state.INCIDENT_WORK)]
    hub = Issues({"issues/40": item(40), state.INCIDENT_WORK_LISTING: listing})
    pulls = [pr(41, 40, state.INCIDENT_WORK)]
    found = incident_pass.sync_incident_work(hub, Store(), pulls, heal_budget())
    assert {(row["item"], row["action"]) for row in found} == {(40, "remove"), (41, "remove")}


def test_closed_items_lose_every_state_label() -> None:
    nodes = [
        {"number": 5768, "labels": {"nodes": [{"name": state.PARKED}, {"name": "bug"}]}},
        {"number": 7, "labels": {"nodes": [{"name": state.DUPLICATE}, {"name": state.COOLING}]}},
    ]
    hub = Issues({"graphql": {"data": {"search": {"nodes": nodes}}}})
    found = incident_pass.clear_closed(hub, Store(), [], heal_budget())
    assert {(row["item"], row["label"]) for row in found} == {
        (5768, state.PARKED), (7, state.DUPLICATE), (7, state.COOLING),
    }
    assert "is:closed" in hub.query and '"factory:parked"' in hub.query
    assert ("issues/5768/labels/factory%3Aparked", None) in hub.writes
