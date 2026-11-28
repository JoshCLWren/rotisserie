"""Urgent reserve must survive the issue-to-PR lifecycle without bypassing safety."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import factory_urgent_intake as portable
import factory_work_policy_candidates as policy


def issue(number, *labels, owner="factory:unowned", body="", state="OPEN"):
    return {
        "number": number, "state": state, "body": body,
        "labels": ["factory", owner, *labels], "createdAt": "2026-11-28T00:00:00Z",
    }


def urgent(number, *labels, **kwargs):
    return issue(number, "bug", "user-reported", *labels, **kwargs)


def pr(number, linked, *, state="OPEN", draft=False, stage="factory:review", partial=False):
    return {
        "number": number, "state": state, "isDraft": draft,
        "headRefName": "local/partial" if partial else f"factory/18-{linked}-nvidia",
        "body": f"Part of #{linked}." if partial else f"Closes #{linked}.",
        "labels": ["factory", "factory:unowned", stage],
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
    }


def fresh(issues, prs):
    return [
        c.number for c in policy.build_candidates(issues, prs, review_backlog_limit=1)
        if c.kind == "issue"
    ]


def test_saturated_queue_admits_one_ranked_urgent_issue():
    backlog = [pr(801, 800)]
    low = urgent(901, "ralph-priority:medium")
    high = urgent(902, "ralph-priority:high")
    normal = issue(903, "ralph-priority:high")
    assert fresh([low, high, normal], backlog) == [902]
    assert fresh([normal, high, low], backlog) == [902]
    assert policy.reserved_urgent_issue(
        [normal, high, low], backlog, review_backlog_limit=1
    ) == 902
    assert set(fresh([low, high, normal], [])) == {901, 902, 903}


def test_reserved_issue_does_not_override_dependency_or_manual_block():
    issues = [
        urgent(901, "ralph-priority:critical", body="Depends on #904."),
        urgent(902, "factory:blocked", "ralph-priority:critical"),
        urgent(903, "ralph-priority:high"),
        issue(904),
    ]
    assert fresh(issues, [pr(801, 800)]) == [903]


@pytest.mark.parametrize("stage,draft,partial", [
    ("factory:review", False, False),
    ("factory:ready", False, False),
    ("factory:blocked", False, False),
    ("factory:review", True, False),
    ("factory:review", False, True),
])
def test_urgent_open_pr_occupies_reserve_until_closed(stage, draft, partial):
    issues = [urgent(901), urgent(902)]
    backlog = [pr(801, 800)]
    current = pr(802, 901, stage=stage, draft=draft, partial=partial)
    assert fresh(issues, [*backlog, current]) == []
    current["state"] = "CLOSED"
    issues[0]["state"] = "CLOSED"
    assert fresh(issues, [*backlog, current]) == [902]


def test_claim_occupies_reserve_before_pr_publication():
    backlog = [pr(801, 800)]
    assert fresh([urgent(901, owner="factory:18"), urgent(902)], backlog) == []


def test_main_breakage_and_owner_override_remain_independent():
    issues = [
        urgent(901, owner="factory:18"),
        urgent(902),
        issue(903, "main-breakage"),
        issue(904, "factory:override"),
    ]
    assert set(fresh(issues, [pr(801, 800)])) == {903, 904}


@pytest.mark.parametrize("reasons,expected", [
    (["backpressure"], (True, ())),
    (["dependency_incomplete"], (False, ("dependency_incomplete",))),
    (["capacity"], (False, ("capacity",))),
    (["backpressure", "active_lease"], (False, ("backpressure", "active_lease"))),
])
def test_portable_gate_only_waives_pure_backpressure(reasons, expected):
    issues = [urgent(901), urgent(902), issue(903)]
    observations = [
        {
            "dimension": "eligibility", "subject": f"work:{n}", "outcome": "blocked",
            "reasons": reasons if n == 901 else ["backpressure"],
        }
        for n in (901, 902, 903)
    ]
    verdicts = portable.intake_with_urgent_reserve(
        observations, issues, [pr(801, 800)], pr_wip_limit=8,
        review_backlog_limit=1, operator_force=False,
    )
    assert verdicts == {
        901: expected, 902: (False, ("backpressure",)), 903: (False, ("backpressure",)),
    }
