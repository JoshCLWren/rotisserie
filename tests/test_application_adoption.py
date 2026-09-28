from __future__ import annotations

import pytest

from rotisserie.application import (
    AdoptionAction,
    AdoptionLane,
    AdoptionPolicy,
    AdoptionStage,
    DecisionDimension,
    DecisionObservation,
    DecisionSnapshot,
    ShadowReport,
    adoption_decision,
    compare_decisions,
)

LANE = AdoptionLane("issue-intake", ("label:ready",))


def matching_report(revision: str = "snapshot-1") -> ShadowReport:
    observations = tuple(
        DecisionObservation(
            dimension,
            f"{dimension}:subject",
            "same",
            rank=0 if dimension is DecisionDimension.RANKING else None,
        )
        for dimension in DecisionDimension
    )
    return compare_decisions(
        DecisionSnapshot("legacy", revision, observations),
        DecisionSnapshot("rotisserie", revision, observations),
    )


def test_canary_requires_complete_distinct_parity_and_human_approval() -> None:
    decision = adoption_decision(
        (matching_report("one"), matching_report("two")),
        LANE,
        policy=AdoptionPolicy(minimum_matching_runs=2),
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
        rollback_tested=True,
    )

    assert decision.action is AdoptionAction.ENTER_CANARY
    assert decision.authorized
    assert decision.to_dict() == {
        "schema_version": 2,
        "action": "enter_canary",
        "authorized": True,
        "from_stage": "legacy",
        "to_stage": "canary",
        "lane": {"name": "issue-intake", "subjects": ["label:ready"]},
        "reasons": [],
    }


def test_cutover_holds_with_every_failed_precondition_explained() -> None:
    partial = compare_decisions(
        DecisionSnapshot("legacy", "one", ()),
        DecisionSnapshot("rotisserie", "one", ()),
    )

    decision = adoption_decision(
        (partial, partial),
        LANE,
        policy=AdoptionPolicy(minimum_matching_runs=3),
        current_stage=AdoptionStage.CANARY,
        operator_approved=False,
        rollback_tested=False,
        request_expansion=True,
    )

    assert decision.action is AdoptionAction.HOLD
    assert decision.reasons == (
        "insufficient_matching_runs",
        "duplicate_shadow_revision",
        "incomplete_decision_coverage",
        "rollback_not_tested",
        "operator_approval_missing",
        "canary_not_observed",
    )


def test_unresolved_divergence_blocks_cutover() -> None:
    baseline = DecisionSnapshot(
        "legacy",
        "one",
        (DecisionObservation(DecisionDimension.REVIEW, "change:1", "approved"),),
    )
    candidate = DecisionSnapshot(
        "rotisserie",
        "one",
        (DecisionObservation(DecisionDimension.REVIEW, "change:1", "pending"),),
    )

    decision = adoption_decision(
        (compare_decisions(baseline, candidate),),
        LANE,
        policy=AdoptionPolicy(required_dimensions=frozenset({DecisionDimension.REVIEW})),
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
        rollback_tested=True,
    )

    assert decision.reasons == ("unresolved_divergence",)


def test_expansion_requires_an_observed_canary() -> None:
    decision = adoption_decision(
        (matching_report(),),
        LANE,
        policy=AdoptionPolicy(),
        current_stage=AdoptionStage.CANARY,
        operator_approved=True,
        rollback_tested=True,
        canary_observed=True,
        request_expansion=True,
    )

    assert decision.action is AdoptionAction.EXPAND


def test_rollback_switch_preempts_missing_or_bad_evidence() -> None:
    decision = adoption_decision(
        (),
        LANE,
        policy=AdoptionPolicy(minimum_matching_runs=5),
        current_stage=AdoptionStage.CANARY,
        operator_approved=False,
        rollback_tested=False,
        rollback_requested=True,
    )

    assert decision.action is AdoptionAction.ROLLBACK
    assert decision.reasons == ("rollback_requested",)


def test_adoption_transitions_hold_when_the_reported_stage_is_invalid() -> None:
    enter_again = adoption_decision(
        (matching_report(),),
        LANE,
        policy=AdoptionPolicy(),
        current_stage=AdoptionStage.CANARY,
        operator_approved=True,
        rollback_tested=True,
    )
    expand_early = adoption_decision(
        (matching_report(),),
        LANE,
        policy=AdoptionPolicy(),
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
        rollback_tested=True,
        canary_observed=True,
        request_expansion=True,
    )
    redundant_rollback = adoption_decision(
        (),
        LANE,
        policy=AdoptionPolicy(),
        current_stage=AdoptionStage.LEGACY,
        operator_approved=False,
        rollback_tested=False,
        rollback_requested=True,
    )

    assert "canary_requires_legacy_stage" in enter_again.reasons
    assert "expansion_requires_canary_stage" in expand_early.reasons
    assert redundant_rollback.action is AdoptionAction.HOLD
    assert redundant_rollback.reasons[0] == "already_on_legacy"


def test_lane_and_policy_reject_unbounded_configuration() -> None:
    with pytest.raises(ValueError, match="at least one subject"):
        AdoptionLane("empty", ())
    with pytest.raises(ValueError, match="positive"):
        AdoptionPolicy(minimum_matching_runs=0)
