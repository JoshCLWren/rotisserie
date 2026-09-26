from __future__ import annotations

from dataclasses import replace

import pytest

from rotisserie.domain import (
    CapacityDecision,
    Change,
    ChangeId,
    Check,
    CheckId,
    CheckStatus,
    GraphSnapshot,
    Lease,
    LeaseAction,
    LeaseId,
    ReadinessBlock,
    Review,
    ReviewDecision,
    ReviewId,
    Revision,
    RevisionId,
    SchedulingPolicy,
    Work,
    WorkBlock,
    Worker,
    WorkerId,
    WorkId,
    WorkState,
    allocate_capacity,
    apply_intake_pressure,
    implementation_decision,
    lease_decision,
    ranked_implementation_work,
    readiness_decision,
    repair_worker,
    reviewer_is_independent,
)
from rotisserie.domain.model import RepositoryId

REPO = RepositoryId("example.test", "org", "repo")
PRODUCER = WorkerId("agent", "producer")
REVIEWER = WorkerId("agent", "reviewer")


def wid(key: str) -> WorkId:
    return WorkId(REPO, key)


def rid(value: str) -> RevisionId:
    return RevisionId(REPO, value)


def base_snapshot() -> GraphSnapshot:
    works = (
        Work(wid("done"), "Prerequisite", WorkState.COMPLETED),
        Work(wid("low"), "Low", priority=1),
        Work(wid("high-b"), "High B", priority=5),
        Work(wid("high-a"), "High A", priority=5),
    )
    return GraphSnapshot(
        works=works,
        workers=(Worker(PRODUCER), Worker(REVIEWER)),
        dependencies=frozenset({(wid("high-a"), wid("done"))}),
    )


def change_snapshot() -> tuple[GraphSnapshot, ChangeId]:
    work = Work(wid("work"), "Work")
    change_id = ChangeId(REPO, "change")
    head = rid("head")
    snapshot = GraphSnapshot(
        works=(work,),
        changes=(Change(change_id, work.id, head, PRODUCER),),
        revisions=(Revision(head),),
        workers=(Worker(PRODUCER), Worker(REVIEWER)),
        checks=(Check(CheckId(head, "test"), CheckStatus.PASSED),),
        reviews=(Review(ReviewId("review"), head, REVIEWER, ReviewDecision.APPROVED),),
    )
    return snapshot, change_id


def test_ranking_is_stable_across_snapshot_input_order() -> None:
    snapshot = base_snapshot()
    expected = (wid("high-a"), wid("high-b"), wid("low"))
    assert tuple(item.id for item in ranked_implementation_work(snapshot, at=10)) == expected
    reversed_snapshot = replace(snapshot, works=tuple(reversed(snapshot.works)))
    assert (
        tuple(item.id for item in ranked_implementation_work(reversed_snapshot, at=10)) == expected
    )


@pytest.mark.parametrize(
    ("at", "action"),
    [
        (9, LeaseAction.ACQUIRE),
        (10, LeaseAction.KEEP),
        (19, LeaseAction.KEEP),
        (20, LeaseAction.ACQUIRE),
    ],
)
def test_lease_boundaries_are_half_open(at: int, action: LeaseAction) -> None:
    snapshot = replace(
        base_snapshot(),
        leases=(Lease(LeaseId("lease"), wid("low"), PRODUCER, acquired_at=10, expires_at=20),),
    )
    assert lease_decision(snapshot, wid("low"), PRODUCER, at=at).action is action


def test_existing_change_suppresses_duplicate_implementation() -> None:
    snapshot, _ = change_snapshot()
    decision = implementation_decision(snapshot, wid("work"), at=0)
    assert decision.blocks == (WorkBlock.IMPLEMENTATION_EXISTS,)


def test_live_lease_is_released_when_work_is_no_longer_open() -> None:
    snapshot = replace(
        base_snapshot(),
        works=tuple(
            replace(item, state=WorkState.CANCELLED) if item.id == wid("low") else item
            for item in base_snapshot().works
        ),
        leases=(Lease(LeaseId("lease"), wid("low"), PRODUCER, acquired_at=10, expires_at=20),),
    )
    decision = lease_decision(snapshot, wid("low"), PRODUCER, at=15)
    assert decision.action is LeaseAction.RELEASE
    assert decision.reason is WorkBlock.NOT_OPEN


@pytest.mark.parametrize(
    ("completion", "production", "active", "policy", "expected"),
    [
        (0, 5, 0, SchedulingPolicy(3, 1), CapacityDecision(0, 3)),
        (5, 0, 0, SchedulingPolicy(3, 1), CapacityDecision(3, 0)),
        (2, 6, 0, SchedulingPolicy(4, 1), CapacityDecision(2, 2)),
        (2, 6, 4, SchedulingPolicy(4, 1), CapacityDecision(0, 0)),
    ],
)
def test_capacity_is_work_conserving_and_reserves_review(
    completion: int,
    production: int,
    active: int,
    policy: SchedulingPolicy,
    expected: CapacityDecision,
) -> None:
    assert (
        allocate_capacity(
            completion_demand=completion,
            production_demand=production,
            active=active,
            policy=policy,
        )
        == expected
    )


def test_backpressure_blocks_only_otherwise_eligible_intake() -> None:
    decisions = (
        implementation_decision(base_snapshot(), wid("low"), at=0),
        implementation_decision(base_snapshot(), wid("done"), at=0),
    )
    pressured = apply_intake_pressure(
        decisions, completion_backlog=2, policy=SchedulingPolicy(3, backlog_limit=2)
    )
    assert pressured[0].blocks == (WorkBlock.BACKPRESSURE,)
    assert pressured[1].blocks == (WorkBlock.NOT_OPEN,)


def test_review_identity_and_repairs_use_the_durable_producer() -> None:
    snapshot, change_id = change_snapshot()
    change = snapshot.changes[0]
    assert reviewer_is_independent(change, REVIEWER)
    assert not reviewer_is_independent(change, PRODUCER)
    assert repair_worker(change) == PRODUCER
    assert readiness_decision(snapshot, change_id).ready


def test_unknown_producer_fails_independent_review_closed() -> None:
    snapshot, change_id = change_snapshot()
    snapshot = replace(snapshot, changes=(replace(snapshot.changes[0], producer=None),))
    assert readiness_decision(snapshot, change_id).blocks == (
        ReadinessBlock.INDEPENDENT_REVIEW_REQUIRED,
    )


def test_changed_head_invalidates_old_checks_and_reviews() -> None:
    snapshot, change_id = change_snapshot()
    new_head = rid("new-head")
    snapshot = replace(snapshot, revisions=(*snapshot.revisions, Revision(new_head)))
    changed = snapshot.with_change_head(change_id, new_head)
    assert readiness_decision(changed, change_id).blocks == (
        ReadinessBlock.CHECKS_PENDING,
        ReadinessBlock.REVIEW_PENDING,
        ReadinessBlock.INDEPENDENT_REVIEW_REQUIRED,
    )


@pytest.mark.parametrize(
    "policy",
    [SchedulingPolicy(0), SchedulingPolicy(3, 3), SchedulingPolicy(3, backlog_limit=0)],
)
def test_valid_edge_configurations(policy: SchedulingPolicy) -> None:
    assert policy.wip_limit >= 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wip_limit": -1},
        {"wip_limit": 2, "reserved_review": 3},
        {"wip_limit": 2, "reserved_review": -1},
        {"wip_limit": 2, "backlog_limit": -1},
    ],
)
def test_invalid_policy_configuration_is_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        SchedulingPolicy(**kwargs)
