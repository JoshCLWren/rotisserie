"""An `agent:NAME` label is a hold by itself: the factory leaves the item alone."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

policy = importlib.import_module("factory_work_policy")
label_policy = importlib.import_module("factory_label_policy")
heal = importlib.import_module("factory_self_heal")


def item(number: int, labels: list[str], *, pr: bool = False) -> dict[str, object]:
    row: dict[str, object] = {
        "number": number,
        "state": "OPEN",
        "labels": [{"name": label} for label in labels],
        "body": "Do the thing.",
        "createdAt": "2026-11-20T00:00:00Z",
    }
    if pr:
        row |= {
            "isDraft": False,
            "headRefName": f"factory/18-{number + 1}-nvidia",
            "headRefOid": "a" * 40,
            "body": f"Closes #{number + 1}",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
    return row


def test_selection_skips_an_agent_held_issue_and_pr() -> None:
    free = item(10, ["factory", "factory:unowned"])
    held = item(11, ["factory", "factory:unowned", "agent:chatgpt"])
    assert policy.issue_is_static_candidate(free, set())
    assert not policy.issue_is_static_candidate(held, set())
    review = ["factory", "factory:unowned", "factory:review"]
    assert policy.pr_is_static_candidate(item(20, review, pr=True), {})
    assert not policy.pr_is_static_candidate(item(22, [*review, "agent:claude"], pr=True), {})
    assert policy.agent_hold({"factory", "agent:"}) is None


def test_the_label_policy_keeps_agent_labels_and_a_claim_beside_them() -> None:
    facts = label_policy.Facts(
        7,
        "pr",
        frozenset({"factory", "factory:review", "factory:review", "factory:claimed", "agent:x"}),
        label_policy.NO_HOLDER,
        claimed_age=3 * 3600,
    )
    plan = label_policy.plan(facts)
    assert not plan.changes
    assert plan.holds == ("outside agent hold agent:x",)
    unheld = label_policy.plan(
        label_policy.Facts(
            7,
            "pr",
            frozenset({"factory", "factory:review", "factory:claimed"}),
            label_policy.NO_HOLDER,
            claimed_age=3 * 3600,
        )
    )
    assert "factory:claimed" in unheld.remove


def test_self_heal_treats_an_agent_label_as_a_holder() -> None:
    assert heal.label_holder({"factory:changes-requested", "agent:codex"}) == (
        "outside agent label agent:codex"
    )
