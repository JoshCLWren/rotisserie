"""``factory:parked`` shows a loop-breaker hold exactly while the hold is in effect."""

from __future__ import annotations

from typing import Any

import factory_self_heal_loops as loops
import factory_state_labels as state
import pytest
from tests.test_factory_item_loop import HEAD, Hub, Store, heal_budget, moment, pull, sweep

NEW_HEAD = "e" * 40


@pytest.fixture(autouse=True)
def fresh_definitions() -> None:
    state._DEFINED.clear()


def parked_at(head: str = HEAD) -> Store:
    return Store({"pr:5766": {"at": moment("2026-11-25T00:00:00Z"), "at_heads": {(head, "")}}})


def at_head(row: dict[str, Any], head: str) -> dict[str, Any]:
    return {**row, "head": {"sha": head}}


def sync(store: Store, *pulls: dict[str, Any], dry_run: bool = False) -> tuple[Hub, list[Any]]:
    hub = Hub()
    budget = heal_budget()
    budget.dry_run = dry_run
    return hub, loops.sync_parked(hub, store, list(pulls), budget)


def test_a_park_adds_the_label_with_its_definition() -> None:
    hub, found = sync(parked_at(), pull(5766))
    assert found == [{"pr": 5766, "label": state.PARKED, "action": "add"}]
    definition = next(body for path, body in hub.writes if path == "labels")
    assert definition == {
        "name": state.PARKED,
        "color": "5319e7",
        "description": "Loop breaker stopped sending workers; see the Parked comment",
    }
    assert ("issues/5766/labels", {"labels": [state.PARKED]}) in hub.writes


def test_the_label_follows_a_park_recorded_in_the_same_sweep() -> None:
    store = Store()
    sweep(store, pull(5766))

    class Recorded(Store):
        def loop_evidence(self, seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
            event = store.events[0]
            return {}, {"pr:5766": {"at": None, "at_heads": {(event["head"], event["stage"])}}}

    _, found = sync(Recorded(), pull(5766))
    assert [row["action"] for row in found] == ["add"]


def test_a_new_head_removes_the_label() -> None:
    hub, found = sync(parked_at(), at_head(pull(5766, state.PARKED), NEW_HEAD))
    assert found == [{"pr": 5766, "label": state.PARKED, "action": "remove"}]
    assert hub.writes == [("issues/5766/labels/factory%3Aparked", None)]


def test_an_owner_override_or_claim_removes_the_label() -> None:
    for bypass in ("factory:override", "factory:claimed"):
        _, found = sync(parked_at(), pull(5766, state.PARKED, bypass))
        assert [row["action"] for row in found] == ["remove"]


def test_a_held_pr_with_the_label_needs_no_write() -> None:
    hub, found = sync(parked_at(), pull(5766, state.PARKED))
    assert hub.writes == [] and found == []


def test_a_dry_run_writes_nothing() -> None:
    hub, found = sync(parked_at(), pull(5766), dry_run=True)
    assert hub.writes == [] and found[0]["status"] == "would-add"


def test_the_label_is_never_an_owner_stage_or_blocker() -> None:
    import factory_work_policy_base as base

    assert not base.OWNER_RE.fullmatch(state.PARKED)
    assert state.PARKED not in base.STAGE_LABELS | base.BLOCKED_LABELS


def test_incident_work_drops_the_label(monkeypatch: Any) -> None:
    monkeypatch.setattr(loops, "incident_pulls", lambda _github, _pulls: frozenset({5766}))
    _, found = sync(parked_at(), pull(5766, state.PARKED))
    assert [row["action"] for row in found] == ["remove"]
