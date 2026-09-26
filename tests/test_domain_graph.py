"""Contract tests for the provider-neutral work graph domain."""

import json
from dataclasses import replace

import pytest

from rotisserie.domain import (
    Boundary,
    BoundaryId,
    BoundaryKind,
    Capacity,
    Change,
    ChangeId,
    Check,
    CheckId,
    CheckStatus,
    Evidence,
    EvidenceId,
    EvidenceKind,
    GraphSnapshot,
    InvalidGraph,
    Lease,
    LeaseId,
    RepositoryId,
    Review,
    ReviewDecision,
    ReviewId,
    Revision,
    RevisionId,
    Work,
    Worker,
    WorkerId,
    WorkId,
)

REPOSITORY = RepositoryId("forge.example", "team", "project")
OTHER_REPOSITORY = RepositoryId("forge.example", "other", "project")
WORK = WorkId(REPOSITORY, "42")
PREREQUISITE_A = WorkId(REPOSITORY, "10")
PREREQUISITE_B = WorkId(REPOSITORY, "20")
CHANGE = ChangeId(REPOSITORY, "change-7")
OLD_HEAD = RevisionId(REPOSITORY, "a" * 40)
NEW_HEAD = RevisionId(REPOSITORY, "b" * 40)
PRODUCER = WorkerId("people", "producer")
REVIEWER = WorkerId("people", "reviewer")
HUMAN = WorkerId("people", "maintainer")


def graph() -> GraphSnapshot:
    return GraphSnapshot(
        works=(Work(WORK, "Deliver graph"),),
        changes=(Change(CHANGE, WORK, OLD_HEAD, PRODUCER),),
        revisions=(Revision(OLD_HEAD), Revision(NEW_HEAD)),
        workers=(Worker(PRODUCER), Worker(REVIEWER), Worker(HUMAN, human=True)),
        leases=(Lease(LeaseId("lease-1"), WORK, PRODUCER, 100, 200),),
        checks=(Check(CheckId(OLD_HEAD, "test"), CheckStatus.PASSED),),
        reviews=(Review(ReviewId("review-1"), OLD_HEAD, REVIEWER, ReviewDecision.APPROVED),),
        evidence=(Evidence(EvidenceId("evidence-1"), OLD_HEAD, EvidenceKind.READINESS, "policy"),),
        capacities=(Capacity(PRODUCER, 2, 1),),
        boundaries=(Boundary(BoundaryId("gate-1"), WORK, BoundaryKind.HUMAN_APPROVAL, HUMAN),),
    )


def test_repository_namespace_prevents_identity_collisions() -> None:
    other = WorkId(OTHER_REPOSITORY, "42")
    assert WORK != other
    assert len(GraphSnapshot(works=(Work(WORK, "Local"), Work(other, "Remote"))).works) == 2


def test_multiple_explicit_prerequisites_are_preserved() -> None:
    snapshot = GraphSnapshot(
        works=(Work(WORK, "Child"), Work(PREREQUISITE_A, "A"), Work(PREREQUISITE_B, "B")),
        dependencies=frozenset({(WORK, PREREQUISITE_A), (WORK, PREREQUISITE_B)}),
    )
    assert snapshot.prerequisites(WORK) == {PREREQUISITE_A, PREREQUISITE_B}


def test_dependency_cycle_is_rejected() -> None:
    with pytest.raises(InvalidGraph, match="cycle"):
        GraphSnapshot(
            works=(Work(WORK, "Child"), Work(PREREQUISITE_A, "Parent")),
            dependencies=frozenset({(WORK, PREREQUISITE_A), (PREREQUISITE_A, WORK)}),
        )


def test_lease_expiry_boundary_is_explicit() -> None:
    assert graph().active_lease(WORK, 199) is not None
    assert graph().active_lease(WORK, 200) is None


def test_changed_head_invalidates_current_revision_evidence() -> None:
    original = graph()
    updated = original.with_change_head(CHANGE, NEW_HEAD)
    assert original.current_evidence(CHANGE) and original.current_checks(CHANGE)
    assert original.current_reviews(CHANGE)
    assert updated.current_evidence(CHANGE) == ()
    assert updated.current_checks(CHANGE) == ()
    assert updated.current_reviews(CHANGE) == ()
    assert updated.evidence == original.evidence


def test_human_boundary_cannot_be_satisfied_by_non_human_worker() -> None:
    snapshot = graph()
    assert snapshot.boundaries_satisfied(WORK)
    automated = replace(snapshot.boundaries[0], satisfied_by=REVIEWER)
    assert not replace(snapshot, boundaries=(automated,)).boundaries_satisfied(WORK)


def test_snapshot_round_trips_through_json() -> None:
    original = graph()
    encoded = json.loads(json.dumps(original.to_dict()))
    assert GraphSnapshot.from_dict(encoded) == original


@pytest.mark.parametrize("limit,reserved", [(-1, 0), (1, 2)])
def test_invalid_capacity_is_rejected(limit: int, reserved: int) -> None:
    with pytest.raises(ValueError):
        Capacity(PRODUCER, limit, reserved)


def test_cross_repository_change_is_rejected() -> None:
    with pytest.raises(ValueError, match="same repository"):
        Change(ChangeId(OTHER_REPOSITORY, "change"), WORK, OLD_HEAD)
