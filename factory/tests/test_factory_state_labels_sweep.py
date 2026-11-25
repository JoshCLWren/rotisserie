"""Duplicate, cooling, gate-rejected and needs-split labels follow the facts that set them."""

from __future__ import annotations

from typing import Any

import factory_acceptance_escalation as escalation
import factory_issue_intake_guard as guard
import factory_self_heal_state as sweep_state
import factory_state_labels as state
import pytest
from tests.test_factory_item_loop import Hub, Store, heal_budget

HEAD = "d" * 40
NEW_HEAD = "e" * 40
BOT = {"login": "github-actions[bot]", "type": "Bot"}


@pytest.fixture(autouse=True)
def fresh() -> None:
    state._DEFINED.clear()
    guard._ASKED.clear()


def pr(
    number: int, issue: int, *labels: str, head: str = HEAD, part: bool = False
) -> dict[str, Any]:
    body = f"Part of #{issue}." if part else f"Closes #{issue}."
    return {
        "number": number,
        "body": body,
        "title": "t",
        "head": {"sha": head, "ref": f"factory/7-{issue}-x"},
        "labels": [{"name": n} for n in ("factory", "factory:unowned", "factory:review", *labels)],
    }


class Comments(Hub):
    def __init__(self, records: dict[int, list[dict[str, Any]]] | None = None) -> None:
        super().__init__()
        self.records = records or {}

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self.records.get(number, [])


class NoStore(Store):
    def enabled(self) -> bool:
        return False


def run(
    pulls: list[dict[str, Any]],
    *,
    hub: Hub | None = None,
    store: Store | None = None,
    holds: dict[int, str] | None = None,
) -> tuple[Hub, list[dict[str, Any]]]:
    github = hub or Comments()
    found = sweep_state.sync_state(
        github,
        store or Store(),
        pulls,
        heal_budget(),
        reader=lambda *_a: dict(holds or {}),
    )
    return github, found


def actions(found: list[dict[str, Any]]) -> set[tuple[int, str, str]]:
    return {(row["pr"], row["label"], row["action"]) for row in found if "action" in row}


def test_the_newer_pr_for_one_issue_is_labeled_duplicate() -> None:
    _, found = run([pr(5768, 5695, part=True), pr(5775, 5695, part=True), pr(9, 10)])
    assert actions(found) == {(5775, state.DUPLICATE, "add")}


def test_the_duplicate_label_goes_when_only_one_pr_remains() -> None:
    _, found = run([pr(5775, 5695, state.DUPLICATE, part=True)])
    assert actions(found) == {(5775, state.DUPLICATE, "remove")}


def test_retry_cooling_sets_the_cooling_label_and_a_park_does_not() -> None:
    _, found = run(
        [pr(12, 1), pr(13, 2)], holds={12: "exact-head retry cooling", 13: "held: loop-parked"}
    )
    assert actions(found) == {(12, state.COOLING, "add")}


def test_the_cooling_label_goes_when_the_hold_expires() -> None:
    _, found = run([pr(12, 1, state.COOLING)], holds={})
    assert actions(found) == {(12, state.COOLING, "remove")}


def test_an_unreadable_store_leaves_cooling_alone() -> None:
    _, found = run([pr(12, 1, state.COOLING)], store=NoStore())
    assert actions(found) == set()


def gate(head: str) -> dict[str, Any]:
    return {"user": BOT, "body": escalation.marker(pr=12, head=head) + "\n\nNOT-READY"}


def test_gate_rejected_stays_at_the_rejected_head() -> None:
    hub = Comments({12: [gate(HEAD)]})
    _, found = run([pr(12, 1, state.GATE_REJECTED)], hub=hub)
    assert actions(found) == set()


def test_gate_rejected_goes_on_a_new_head() -> None:
    hub = Comments({12: [gate(HEAD)]})
    _, found = run([pr(12, 1, state.GATE_REJECTED, head=NEW_HEAD)], hub=hub)
    assert actions(found) == {(12, state.GATE_REJECTED, "remove")}


def test_gate_rejected_goes_when_the_gate_passes_at_the_head() -> None:
    passed = {
        "user": BOT,
        "body": f"<!-- x-factory-acceptance-v1:issue-1:head-{HEAD}:digest-abc -->",
    }
    hub = Comments({12: [gate(HEAD), passed]})
    _, found = run([pr(12, 1, state.GATE_REJECTED)], hub=hub)
    assert actions(found) == {(12, state.GATE_REJECTED, "remove")}


def test_the_dispatcher_drops_needs_split_once_the_issue_fits() -> None:
    writes: list[tuple[str, object]] = []
    small = {
        "number": 4335,
        "body": "Now a small task.",
        "state": "OPEN",
        "labels": [{"name": n} for n in ("factory", "factory:unowned", guard.NEEDS_SPLIT)],
    }
    guard.request_splits([small], lambda path, body: writes.append((path, body)))
    assert writes == [("issues/4335/labels/factory%3Aneeds-split", None)]


def test_every_state_label_has_a_distinct_color() -> None:
    colors = [color for color, _ in state.DEFINITIONS.values()]
    assert len(set(colors)) == len(colors)
    assert set(state.DEFINITIONS) <= set(state.IGNORED_BY_WORKFLOWS)
