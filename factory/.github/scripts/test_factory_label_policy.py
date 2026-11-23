"""Label doctor and ready-for-work projection policy (issue #191)."""

from __future__ import annotations

import factory_label_policy as lp
import pytest
from factory_label_policy import AVAILABLE, CLAIMED, REJECTED, Facts, Holder


def facts(
    *labels: str,
    kind: str = "pr",
    holder: Holder = lp.NO_HOLDER,
    status: str | None = "candidate",
    action: str | None = "review",
    claimed_age: int | None = None,
    rejected_age: int | None = None,
) -> Facts:
    return Facts(7, kind, frozenset(labels), holder, status, action, claimed_age, rejected_age)


def apply(start: frozenset[str], result: lp.Plan) -> frozenset[str]:
    return frozenset((set(start) - set(result.remove)) | set(result.add))


def test_frontier_candidate_becomes_available():
    result = lp.plan(facts("factory", "factory:review", "factory:unowned"))
    assert result.add == (AVAILABLE,)
    assert result.remove == ()


@pytest.mark.parametrize(
    ("holder", "status", "action", "extra"),
    [
        (lp.UNKNOWN_HOLDER, "candidate", "review", ()),
        (Holder("factory", "factory:3"), "candidate", "review", ()),
        (lp.NO_HOLDER, "held", "review", ()),
        (lp.NO_HOLDER, None, None, ()),
        (lp.NO_HOLDER, "candidate", "merge", ()),
        (lp.NO_HOLDER, "candidate", "review", ("factory:blocked",)),
        (lp.NO_HOLDER, "candidate", "review", ("factory:4",)),
    ],
)
def test_false_availability_is_removed(holder, status, action, extra):
    start = ("factory", "factory:review", AVAILABLE, *extra)
    result = lp.plan(facts(*start, holder=holder, status=status, action=action))
    assert AVAILABLE in result.remove
    assert AVAILABLE not in result.add


def test_neon_unknown_never_touches_a_claim():
    start = ("factory", "factory:review", "factory:unowned", CLAIMED, AVAILABLE)
    result = lp.plan(facts(*start, holder=lp.UNKNOWN_HOLDER, claimed_age=99999))
    assert CLAIMED not in result.remove
    assert result.remove == (AVAILABLE,)


def test_label_claim_in_neon_projects_claimed_and_hides_availability():
    holder = Holder("external", "octo", lp.LABEL_CLIENT, "uuid")
    start = ("factory", "factory:review", "factory:unowned", AVAILABLE, REJECTED)
    result = lp.plan(facts(*start, holder=holder))
    assert result.add == (CLAIMED,)
    assert set(result.remove) == {AVAILABLE, REJECTED}


def test_young_claim_request_is_left_for_the_handler():
    start = ("factory", "factory:review", "factory:unowned", CLAIMED)
    result = lp.plan(facts(*start, claimed_age=30))
    assert not result.changes
    assert "claim request not answered yet" in result.holds


def test_stale_claim_label_without_neon_claim_is_removed_then_offered():
    start = frozenset({"factory", "factory:review", "factory:unowned", CLAIMED})
    result = lp.plan(facts(*start, claimed_age=lp.CLAIM_REQUEST_GRACE_SECONDS))
    assert CLAIMED in result.remove
    assert AVAILABLE in result.add


def test_claim_label_cannot_shadow_another_lease():
    start = ("factory", "factory:review", CLAIMED)
    result = lp.plan(facts(*start, holder=Holder("external", "a", "codebuff"), claimed_age=4000))
    assert CLAIMED in result.remove


def test_old_rejection_expires():
    result = lp.plan(facts("factory", "factory:review", REJECTED, rejected_age=3600))
    assert REJECTED in result.remove
    result = lp.plan(facts("factory", "factory:review", REJECTED, rejected_age=60))
    assert REJECTED not in result.remove


def test_exclusive_stages_collapse_to_precedence_winner():
    start = ("factory", "factory:unowned", "factory:review", "factory:ci", "factory:building")
    result = lp.plan(facts(*start, status="held"))
    assert set(result.remove) == {"factory:ci", "factory:building"}


def test_owner_contradictions_use_work_policy_normaliser():
    result = lp.plan(
        facts(
            "factory",
            "factory:unowned",
            "factory:building",
            kind="issue",
            status="held",
            action="implement",
        )
    )
    assert result.remove == ("factory:building",)
    result = lp.plan(
        facts("factory", "factory:unowned", "factory:3", "factory:review", status="held")
    )
    assert result.remove == ("factory:unowned",)


def test_several_active_owners_hold_without_rewriting_owners():
    start = ("factory", "factory:3", "factory:4", "factory:review", AVAILABLE)
    result = lp.plan(facts(*start))
    assert result.remove == (AVAILABLE,)
    assert any("several active owners" in hold for hold in result.holds)


def test_merge_conflict_label_on_issue_is_wrong_type():
    result = lp.plan(
        facts(
            "factory",
            "factory:unowned",
            lp.MERGE_CONFLICT,
            kind="issue",
            status="held",
            action="implement",
        )
    )
    assert result.remove == (lp.MERGE_CONFLICT,)


def test_legacy_labels_map_when_certain_and_drop_otherwise():
    start = ('"factory:review"', '"bug"', "factory:in-review", "ralph-status:todo", "factory",
             "ralph-priority:high", '"factory:41"', '"ralph-priority:high"')
    result = lp.plan(facts(*start, status="held"))
    assert result.add == ("bug",)
    assert set(result.remove) == {'"factory:review"', '"bug"', "factory:in-review",
                                  "ralph-status:todo", '"factory:41"', '"ralph-priority:high"'}


def test_quoted_labels_never_invent_a_stage_or_owner():
    for quoted in ('"factory:ready"', '"factory:41"', '"factory:unowned"', '"factory"'):
        assert lp.legacy_replacement(quoted) is None


@pytest.mark.parametrize(
    "live",
    [
        "ralph-status:done",
        "ralph-status:blocked",
        "ralph-status:pending",
        "ralph-status:in-progress",
        "ralph-priority:high",
        "factory:merge-conflict",
    ],
)
def test_live_labels_are_never_legacy(live):
    assert not lp.is_legacy(live)


@pytest.mark.parametrize(
    "start",
    [
        ("factory", "factory:review", "factory:ci", AVAILABLE, '"factory:ci"', CLAIMED),
        ("factory", "factory:unowned", "factory:3", "factory:building", REJECTED),
        ('"factory"', '"factory:unowned"', '"factory:review"'),
        ("factory", "factory:3", "factory:4", AVAILABLE),
    ],
)
@pytest.mark.parametrize(
    "holder", [lp.NO_HOLDER, lp.UNKNOWN_HOLDER, Holder("external", "x", lp.LABEL_CLIENT)]
)
def test_repair_converges_in_one_pass(start, holder):
    first = lp.plan(facts(*start, holder=holder, claimed_age=9999, rejected_age=9999))
    repaired = apply(frozenset(start), first)
    second = lp.plan(facts(*repaired, holder=holder, claimed_age=9999, rejected_age=9999))
    assert not second.changes, second


def test_unrelated_labels_survive():
    start = frozenset({"factory", "factory:review", "factory:ci", "bug", "priority:P0"})
    result = lp.plan(facts(*start))
    assert {"bug", "priority:P0"} <= apply(start, result)


def test_holder_from_lease_decodes_external_operation():
    holder = lp.holder_from_lease({"operation": "external:octo/label", "claim_id": "u"})
    assert holder == Holder("external", "octo", "label", "u")
    assert holder.label_claim
    assert lp.holder_from_lease(None) == lp.NO_HOLDER
    assert lp.holder_from_lease({"worker": "factory:3"}).kind == "factory"
