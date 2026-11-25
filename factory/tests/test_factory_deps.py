"""Dependency links: body parsing, blocker semantics, cycles and the store table."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import factory_deps as deps
import pytest

from tests import turso_support as support


@pytest.fixture(params=support.kinds())
def store(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def issue(number: int, body: str = "") -> dict[str, Any]:
    return {"number": number, "state": "OPEN", "body": body}


def test_anchored_body_lines_declare_blockers_and_casual_mentions_do_not() -> None:
    assert deps.body_links(5, "Blocked by #3") == {deps.Link(3, 5, "blocks", "body")}
    assert {found.src for found in deps.body_links(5, "Depends on #1 and #2, see #9")} == {1, 2}
    assert deps.body_links(5, "Blocked by the flaky test in #9") == set()
    assert deps.body_links(5, "Related to #4; mentions #6") == set()
    assert deps.body_links(5, "Depends on #5") == set()


def test_only_open_blockers_hold_and_children_inherit_their_parents_blockers() -> None:
    links = [
        deps.Link(1, 2, "blocks"),
        deps.Link(10, 11, "parent-child"),
        deps.Link(3, 10, "blocks"),
        deps.Link(7, 8, "related"),
        deps.Link(9, 2, "blocks"),
    ]
    graph = deps.Graph(links, {1, 2, 3, 7, 8, 10, 11})
    assert graph.blockers(2) == (1,)
    assert graph.blockers(11) == (3,)
    assert graph.blockers(10) == (3, 11)
    assert graph.blockers(8) == ()
    assert deps.Graph(links, {2, 10, 11}).blockers(11) == ()


def test_a_tracking_parent_waits_for_its_children_and_never_blocks_them() -> None:
    links = [
        deps.Link(10, 11, "parent-child"),
        deps.Link(10, 12, "parent-child"),
        deps.Link(11, 10, "blocks"),
        deps.Link(12, 10, "blocks"),
        deps.Link(11, 12, "blocks"),
        deps.Link(20, 10, "blocks"),
    ]
    graph = deps.Graph(links, {10, 11, 12, 20})
    assert graph.blockers(11) == (20,)
    assert graph.blockers(12) == (11, 20)
    assert graph.blockers(10) == (11, 12, 20)
    assert graph.cycles() == ()
    assert deps.Graph(links, {10, 11, 12}).blockers(11) == ()


def test_a_cycle_declared_in_bodies_is_reported_and_its_members_are_held() -> None:
    rows = [issue(1, "Blocked by #3"), issue(2, "Blocked by #1"), issue(3, "Depends on #2")]
    rows.append(issue(4, "Blocked by #1"))
    graph = deps.graph(rows)
    assert graph.cycles() == ((1, 3, 2),)
    assert graph.held(1) and graph.held(2) and graph.held(3)
    assert graph.reason(2).startswith("in a dependency cycle: #1 -> #3 -> #2 -> #1")
    assert graph.reason(4) == "blocked by open #1"


def test_a_long_chain_and_a_parent_loop_terminate() -> None:
    chain = [deps.Link(n, n + 1, "blocks") for n in range(1, 3000)]
    graph = deps.Graph(chain, range(1, 3001))
    assert graph.blockers(3000) == (2999,)
    assert graph.cycles() == ()
    loop = deps.Graph([deps.Link(1, 2, "parent-child"), deps.Link(2, 1, "parent-child")], {1, 2})
    assert loop.blockers(1) == (2,) and loop.cycles() == ((1, 2),)


def test_add_refuses_a_link_that_closes_a_cycle_and_reports_existing_links(store: Any) -> None:
    _db, repo = store
    assert deps.add(repo, "issue:1", "issue:2", "blocks") == "added"
    assert deps.add(repo, 2, 3, "parent-child") == "added"
    assert deps.add(repo, 1, 2, "blocks") == "exists"
    assert deps.add(repo, 3, 2, "blocks") == "added"
    with pytest.raises(deps.DependencyCycle):
        deps.add(repo, 2, 1, "blocks")
    with pytest.raises(deps.DependencyCycle):
        deps.add(repo, 3, 2, "parent-child")
    assert deps.add(repo, 2, 1, "related") == "added"
    with pytest.raises(deps.DependencyCycle):
        deps.add(repo, 4, 4, "blocks")
    with pytest.raises(ValueError):
        deps.add(repo, 1, 5, "waits-for")
    assert deps.blockers(repo, "issue:2", open_numbers={1, 2, 3}) == [1, 3]
    assert deps.blockers(repo, 3, open_numbers={1, 2, 3}) == [1]
    assert deps.remove(repo, 1, 2, "blocks") is True
    assert deps.remove(repo, 1, 2, "blocks") is False


def test_reconcile_mirrors_bodies_keeps_factory_links_and_writes_only_a_change(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, repo = store
    deps.add(repo, 7, 2, "blocks")
    first = deps.reconcile(repo, [issue(2, "Blocked by #1"), issue(3, "Depends on #2")])
    assert (first["added"], first["removed"], first["cycles"]) == (2, 0, [])
    writes: list[int] = []
    original = db.batch
    monkeypatch.setattr(db, "batch", lambda statements: writes.append(1) or original(statements))
    again = deps.reconcile(repo, [issue(2, "Blocked by #1"), issue(3, "Depends on #2")])
    assert (again["added"], again["removed"], writes) == (0, 0, [])
    later = deps.reconcile(repo, [issue(2, "No longer blocked"), issue(3, "Blocked by #2")])
    assert (later["added"], later["removed"]) == (0, 1)
    stored = {(found.src, found.dst, found.kind, found.origin) for found in deps.read_links(repo)}
    assert stored == {(7, 2, "blocks", "factory"), (2, 3, "blocks", "body")}


def test_reconcile_reports_a_body_cycle(store: Any) -> None:
    _db, repo = store
    found = deps.reconcile(repo, [issue(1, "Blocked by #2"), issue(2, "Blocked by #1")])
    assert found["cycles"] == [[1, 2]]


def test_stored_holds_nothing_when_the_store_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    assert deps.stored("o/r") == []
