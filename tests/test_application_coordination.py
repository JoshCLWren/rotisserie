from __future__ import annotations

from dataclasses import replace

from rotisserie.application import (
    CoordinationService,
    EffectCommand,
    EffectConflict,
    EffectKind,
    FailureCategory,
    GraphView,
    OperationStatus,
)
from rotisserie.application.coordination import RefillDecision
from rotisserie.domain import (
    Change,
    ChangeId,
    Check,
    CheckId,
    CheckStatus,
    GraphSnapshot,
    Lease,
    LeaseId,
    RepositoryId,
    Review,
    ReviewDecision,
    ReviewId,
    Revision,
    RevisionId,
    SchedulingPolicy,
    Work,
    Worker,
    WorkerId,
    WorkId,
    WorkState,
)

REPO = RepositoryId("example.test", "org", "repo")
PRODUCER = WorkerId("agent", "producer")
REVIEWER = WorkerId("agent", "reviewer")
WORK = WorkId(REPO, "12")
CHANGE = ChangeId(REPO, "34")
HEAD = RevisionId(REPO, "abc123")


class FakePort:
    """Compare-and-swap fake whose effect keys survive simulated crashes."""

    def __init__(self, snapshot: GraphSnapshot) -> None:
        self.snapshot = snapshot
        self.version = 1
        self.applied: set[str] = set()
        self.calls: list[EffectCommand] = []
        self.conflict_next = False
        self.crash_after_next = False

    def read(self) -> GraphView:
        return GraphView(self.snapshot, str(self.version))

    def apply(self, command: EffectCommand) -> None:
        self.calls.append(command)
        if command.idempotency_key in self.applied:
            return
        if self.conflict_next:
            self.conflict_next = False
            self.version += 1
        if command.expected_version != str(self.version):
            raise EffectConflict("graph changed")
        if command.change is not None:
            change = next(item for item in self.snapshot.changes if item.id == command.change)
            if change.head != command.expected_revision:
                raise EffectConflict("head changed")
        if command.kind is EffectKind.CLAIM:
            assert command.lease is not None
            self.snapshot = replace(self.snapshot, leases=(*self.snapshot.leases, command.lease))
        elif command.kind is EffectKind.RELEASE:
            self.snapshot = replace(
                self.snapshot,
                leases=tuple(x for x in self.snapshot.leases if x.id != command.lease_id),
            )
        elif command.kind is EffectKind.COMPLETE:
            self.snapshot = replace(
                self.snapshot,
                works=tuple(
                    replace(item, state=WorkState.COMPLETED) if item.id == command.work else item
                    for item in self.snapshot.works
                ),
            )
        self.applied.add(command.idempotency_key)
        self.version += 1
        if self.crash_after_next:
            self.crash_after_next = False
            raise ConnectionError("simulated process loss after effect")


def base_snapshot(*, ready: bool = False) -> GraphSnapshot:
    return GraphSnapshot(
        works=(Work(WORK, "Implement capability", priority=2),),
        changes=(Change(CHANGE, WORK, HEAD, PRODUCER),) if ready else (),
        revisions=(Revision(HEAD),) if ready else (),
        workers=(Worker(PRODUCER), Worker(REVIEWER)),
        checks=(Check(CheckId(HEAD, "test"), CheckStatus.PASSED),) if ready else (),
        reviews=(Review(ReviewId("review"), HEAD, REVIEWER, ReviewDecision.APPROVED),)
        if ready
        else (),
    )


def test_claim_revalidates_at_the_effect_boundary() -> None:
    port = FakePort(base_snapshot())
    port.conflict_next = True
    lease = Lease(LeaseId("lease"), WORK, PRODUCER, 10, 20)
    result = CoordinationService(port).claim(lease, operation="claim-12", at=10)
    assert result.failure is FailureCategory.STALE_STATE
    assert port.snapshot.leases == ()


def test_claim_dispatch_and_release_require_the_live_lease_owner() -> None:
    port = FakePort(base_snapshot())
    service = CoordinationService(port)
    lease = Lease(LeaseId("lease"), WORK, PRODUCER, 10, 20)
    assert service.claim(lease, operation="claim-12", at=10).status is OperationStatus.APPLIED
    assert (
        service.dispatch(WORK, REVIEWER, operation="wrong", at=11).failure
        is FailureCategory.LEASE_OWNER_MISMATCH
    )
    assert (
        service.dispatch(WORK, PRODUCER, operation="dispatch", at=11).status
        is OperationStatus.APPLIED
    )
    assert (
        service.release(WORK, PRODUCER, operation="release", at=11).status
        is OperationStatus.APPLIED
    )


def test_changed_head_cannot_cross_review_or_completion_boundary() -> None:
    port = FakePort(base_snapshot(ready=True))
    service = CoordinationService(port)
    stale = RevisionId(REPO, "old")
    assert (
        service.review_transition(CHANGE, stale, REVIEWER, operation="review").failure
        is FailureCategory.REVISION_MISMATCH
    )
    assert (
        service.completion(CHANGE, stale, operation="complete").failure
        is FailureCategory.REVISION_MISMATCH
    )
    port.conflict_next = True
    assert (
        service.completion(CHANGE, HEAD, operation="complete").failure
        is FailureCategory.STALE_STATE
    )


def test_review_transition_requires_an_independent_reviewer() -> None:
    service = CoordinationService(FakePort(base_snapshot(ready=True)))
    result = service.review_transition(CHANGE, HEAD, PRODUCER, operation="review")
    assert result.failure is FailureCategory.REVIEWER_NOT_INDEPENDENT


def test_completion_is_serializable() -> None:
    port = FakePort(base_snapshot(ready=True))
    result = CoordinationService(port).completion(CHANGE, HEAD, operation="complete-34")
    assert result.status is OperationStatus.APPLIED
    assert result.to_dict()["schema_version"] == 1
    assert port.snapshot.works[0].state is WorkState.COMPLETED


def test_completion_drain_selects_only_ready_changes() -> None:
    service = CoordinationService(FakePort(base_snapshot(ready=True)))
    assert service.completion_drain(limit=1) == (CHANGE,)
    assert service.completion_drain(limit=0) == ()


def test_retry_after_effect_before_ack_does_not_duplicate_dispatch() -> None:
    lease = Lease(LeaseId("lease"), WORK, PRODUCER, 10, 20)
    port = FakePort(replace(base_snapshot(), leases=(lease,)))
    port.crash_after_next = True
    service = CoordinationService(port)
    try:
        service.dispatch(WORK, PRODUCER, operation="dispatch-12", at=11)
    except ConnectionError:
        pass
    result = service.dispatch(WORK, PRODUCER, operation="dispatch-12", at=11)
    assert result.status is OperationStatus.APPLIED
    assert len(port.applied) == 1
    assert port.calls[0].idempotency_key == port.calls[1].idempotency_key


def test_recovery_releases_expired_and_preserves_live_leases() -> None:
    old = Lease(LeaseId("old"), WORK, PRODUCER, 1, 5)
    other_work = WorkId(REPO, "13")
    live = Lease(LeaseId("live"), other_work, REVIEWER, 1, 50)
    snapshot = replace(
        base_snapshot(),
        works=(*base_snapshot().works, Work(other_work, "Other")),
        leases=(old, live),
    )
    port = FakePort(snapshot)
    result = CoordinationService(port).recover(operation="recover", at=10)
    assert result.status is OperationStatus.APPLIED
    assert port.snapshot.leases == (live,)


def test_refill_combines_capacity_backpressure_and_stable_selection() -> None:
    other = WorkId(REPO, "13")
    snapshot = replace(
        base_snapshot(), works=(*base_snapshot().works, Work(other, "Other", priority=1))
    )
    service = CoordinationService(FakePort(snapshot))
    assert service.refill(
        at=0,
        completion_demand=2,
        active=0,
        completion_backlog=1,
        policy=SchedulingPolicy(3, reserved_review=1, backlog_limit=5),
    ) == RefillDecision(2, (WORK,))
    assert (
        service.select(at=0, completion_backlog=5, policy=SchedulingPolicy(3, backlog_limit=5))
        == ()
    )
