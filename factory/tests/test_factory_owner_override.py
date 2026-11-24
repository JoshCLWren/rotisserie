"""Work the repository owner picked goes first in every stage (#212)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

policy = importlib.import_module("factory_work_policy")
label_policy = importlib.import_module("factory_label_policy")


def candidate(number: int, lane: int, *, override: bool = False) -> object:
    return policy.Candidate(
        kind="pr",
        number=number,
        lane=lane,
        priority=4,
        created_at="2026-01-01T00:00:00Z",
        override=override,
    )


def test_an_owner_pick_sorts_ahead_of_every_lane_and_priority() -> None:
    urgent = candidate(1, lane=0)
    picked = candidate(2, lane=5, override=True)
    ordered = sorted([urgent, picked], key=policy.Candidate.sort_key)
    assert [c.number for c in ordered] == [2, 1]


def test_an_owner_pick_bypasses_wip_and_review_backpressure() -> None:
    issue = {"labels": [{"name": "factory:override"}]}
    assert policy.issue_bypasses_wip_limit(issue)
    assert policy.issue_bypasses_wip_limit(issue, review_backlog_saturated=True)
    assert not policy.issue_bypasses_wip_limit({"labels": []}, review_backlog_saturated=True)


def test_the_claim_handler_and_the_policy_name_the_same_label() -> None:
    assert label_policy.OVERRIDE == policy.OVERRIDE_LABEL == "factory:override"
