from __future__ import annotations

import pytest

from rotisserie.application import (
    AdoptionAction,
    AdoptionEvidence,
    AdoptionEvidenceKind,
    AdoptionLane,
    AdoptionPolicy,
    AdoptionStage,
    AdoptionTransition,
    DecisionDimension,
    DecisionObservation,
    DecisionSnapshot,
    ShadowReport,
    adoption_decision,
    compare_decisions,
    prepare_adoption_transition,
)

LANE = AdoptionLane("issue-intake", ("label:ready",))
CONTROL_REVISION = "controls-1"


def evidence(kind: AdoptionEvidenceKind, lane: AdoptionLane = LANE) -> AdoptionEvidence:
    if kind is AdoptionEvidenceKind.ROLLBACK_DRILL:
        return AdoptionEvidence(
            kind, lane, CONTROL_REVISION, AdoptionStage.CANARY, AdoptionStage.LEGACY
        )
    return AdoptionEvidence(
        kind, lane, CONTROL_REVISION, AdoptionStage.CANARY, AdoptionStage.CANARY
    )


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
        (evidence(AdoptionEvidenceKind.ROLLBACK_DRILL),),
        policy=AdoptionPolicy(minimum_matching_runs=2),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
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
        (),
        policy=AdoptionPolicy(minimum_matching_runs=3),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.CANARY,
        operator_approved=False,
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
        (evidence(AdoptionEvidenceKind.ROLLBACK_DRILL),),
        policy=AdoptionPolicy(required_dimensions=frozenset({DecisionDimension.REVIEW})),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
    )

    assert decision.reasons == ("unresolved_divergence",)


def test_expansion_requires_an_observed_canary() -> None:
    decision = adoption_decision(
        (matching_report(),),
        LANE,
        (
            evidence(AdoptionEvidenceKind.ROLLBACK_DRILL),
            evidence(AdoptionEvidenceKind.CANARY_OBSERVATION),
        ),
        policy=AdoptionPolicy(),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.CANARY,
        operator_approved=True,
        request_expansion=True,
    )

    assert decision.action is AdoptionAction.EXPAND


def test_rollback_switch_preempts_missing_or_bad_evidence() -> None:
    decision = adoption_decision(
        (),
        LANE,
        (),
        policy=AdoptionPolicy(minimum_matching_runs=5),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.CANARY,
        operator_approved=False,
        rollback_requested=True,
    )

    assert decision.action is AdoptionAction.ROLLBACK
    assert decision.reasons == ("rollback_requested",)


def test_adoption_transitions_hold_when_the_reported_stage_is_invalid() -> None:
    enter_again = adoption_decision(
        (matching_report(),),
        LANE,
        (evidence(AdoptionEvidenceKind.ROLLBACK_DRILL),),
        policy=AdoptionPolicy(),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.CANARY,
        operator_approved=True,
    )
    expand_early = adoption_decision(
        (matching_report(),),
        LANE,
        (
            evidence(AdoptionEvidenceKind.ROLLBACK_DRILL),
            evidence(AdoptionEvidenceKind.CANARY_OBSERVATION),
        ),
        policy=AdoptionPolicy(),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
        request_expansion=True,
    )
    redundant_rollback = adoption_decision(
        (),
        LANE,
        (),
        policy=AdoptionPolicy(),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.LEGACY,
        operator_approved=False,
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


def test_evidence_is_versioned_and_bound_to_lane_stage_and_control_revision() -> None:
    item = evidence(AdoptionEvidenceKind.ROLLBACK_DRILL)
    assert AdoptionEvidence.from_dict(item.to_dict()) == item

    wrong_lane = evidence(AdoptionEvidenceKind.ROLLBACK_DRILL, AdoptionLane("other", ("x",)))
    decision = adoption_decision(
        (matching_report(),),
        LANE,
        (wrong_lane,),
        policy=AdoptionPolicy(),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
    )
    assert decision.reasons == ("rollback_not_tested",)

    with pytest.raises(ValueError, match="restore an active lane"):
        AdoptionEvidence(
            AdoptionEvidenceKind.ROLLBACK_DRILL,
            LANE,
            CONTROL_REVISION,
            AdoptionStage.LEGACY,
            AdoptionStage.LEGACY,
        )


def test_authorized_decision_becomes_stable_exact_control_transition() -> None:
    decision = adoption_decision(
        (matching_report(),),
        LANE,
        (evidence(AdoptionEvidenceKind.ROLLBACK_DRILL),),
        policy=AdoptionPolicy(),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.LEGACY,
        operator_approved=True,
    )

    first = prepare_adoption_transition(decision, control_revision=CONTROL_REVISION)
    second = prepare_adoption_transition(decision, control_revision=CONTROL_REVISION)

    assert first == second
    assert first.operation_key.startswith("adoption-transition:")
    assert first.to_dict() == {
        "schema_version": 1,
        "operation_key": first.operation_key,
        "control_revision": CONTROL_REVISION,
        "action": "enter_canary",
        "lane": {"name": "issue-intake", "subjects": ["label:ready"]},
        "expected_stage": "legacy",
        "target_stage": "canary",
    }
    assert AdoptionTransition.from_dict(first.to_dict()) == first
    tampered = first.to_dict()
    tampered["operation_key"] = "adoption-transition:wrong"
    with pytest.raises(ValueError, match="does not match its content"):
        AdoptionTransition.from_dict(tampered)


def test_transition_contract_rejects_hold_and_impossible_stage_changes() -> None:
    hold = adoption_decision(
        (),
        LANE,
        (),
        policy=AdoptionPolicy(),
        control_revision=CONTROL_REVISION,
        current_stage=AdoptionStage.LEGACY,
        operator_approved=False,
    )
    with pytest.raises(ValueError, match="hold decision"):
        prepare_adoption_transition(hold, control_revision=CONTROL_REVISION)
    with pytest.raises(ValueError, match="stages do not match"):
        AdoptionTransition(
            "operation-1",
            CONTROL_REVISION,
            AdoptionAction.EXPAND,
            LANE,
            AdoptionStage.LEGACY,
            AdoptionStage.EXPANDED,
        )
    with pytest.raises(ValueError, match="schema version"):
        AdoptionTransition.from_dict({"schema_version": 2})
