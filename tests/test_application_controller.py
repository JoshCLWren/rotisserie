from __future__ import annotations

from dataclasses import replace

import pytest

from rotisserie.application.controller import (
    AcquisitionFailure,
    AssignmentController,
    ControllerRequest,
    ControllerStatus,
    ControllerView,
    DispatchOutcome,
    Liveness,
    plan_assignment,
)
from rotisserie.application.coordination import EffectCommand, EffectConflict, EffectKind, GraphView
from rotisserie.application.runtime import Assignment, AssignmentId, Authority, AuthorityManifest
from rotisserie.domain import (
    Boundary,
    BoundaryId,
    BoundaryKind,
    GraphSnapshot,
    LeaseId,
    RepositoryId,
    SchedulingPolicy,
    Work,
    Worker,
    WorkerId,
    WorkId,
)

REPO = RepositoryId("example.test", "org", "repo")
WORK = WorkId(REPO, "12")
WORKER = WorkerId("agent", "one")
ASSIGNMENT = Assignment(
    AssignmentId("assignment-12"),
    WORKER,
    "Implement capability",
    AuthorityManifest(WORK, None, frozenset({Authority.READ}), 30),
)
REQUEST = ControllerRequest(
    REPO,
    ASSIGNMENT,
    LeaseId("lease-12"),
    "tick-12",
    SchedulingPolicy(1, 0, 3),
    10,
    0,
    frozenset({WORKER}),
)


class Port:
    def __init__(self) -> None:
        self.snapshot = GraphSnapshot(works=(Work(WORK, "Capability"),), workers=(Worker(WORKER),))
        self.version = 1
        self.liveness = Liveness.IDLE
        self.active = 0
        self.backlog = 0
        self.fail_read = False
        self.conflict: EffectKind | None = None
        self.outcome = DispatchOutcome.ACCEPTED
        self.commands: list[EffectCommand] = []
        self.assignments: list[Assignment] = []
        self.ambiguous_claim = False
        self.reads = 0

    def acquire(self) -> ControllerView:
        self.reads += 1
        if self.fail_read:
            raise AcquisitionFailure("unavailable")
        return ControllerView(
            GraphView(self.snapshot, str(self.version)),
            10,
            self.active,
            self.backlog,
            ((WORKER, self.liveness),),
        )

    def apply(self, command: EffectCommand) -> None:
        self.commands.append(command)
        if command.kind is self.conflict or command.expected_version != str(self.version):
            raise EffectConflict("changed")
        if command.kind is EffectKind.CLAIM:
            assert command.lease is not None
            self.snapshot = replace(self.snapshot, leases=(command.lease,))
            if self.ambiguous_claim:
                raise ConnectionError("acknowledgement lost")
        elif command.kind is EffectKind.RELEASE:
            self.snapshot = replace(self.snapshot, leases=())
        self.version += 1

    def dispatch(self, command: EffectCommand, assignment: Assignment) -> DispatchOutcome:
        self.commands.append(command)
        if command.kind is self.conflict:
            raise EffectConflict("changed")
        if self.outcome is DispatchOutcome.ACCEPTED:
            self.assignments.append(assignment)
        return self.outcome


def test_dry_run_never_claims_or_hands_off() -> None:
    port = Port()
    result = AssignmentController(port).tick(REQUEST)
    assert result.status is ControllerStatus.SELECTED
    assert result.assignment == ASSIGNMENT
    assert port.commands == [] and port.assignments == [] and port.reads == 1
    assert result.to_dict()["schema_version"] == 1


def test_claim_handoff_and_duplicate_suppression() -> None:
    port = Port()
    controller = AssignmentController(port)
    result = controller.tick(REQUEST, apply=True)
    assert result.status is ControllerStatus.DISPATCHED
    assert port.assignments == [ASSIGNMENT]
    assert controller.tick(REQUEST, apply=True).status is ControllerStatus.WORKER_BUSY
    assert len(port.assignments) == 1
    assert [x.kind for x in port.commands] == [EffectKind.CLAIM, EffectKind.DISPATCH]
    assert port.commands[-1].lease_id == REQUEST.lease_id


@pytest.mark.parametrize(
    "outcome,status,release",
    [
        (DispatchOutcome.REFUSED, ControllerStatus.RELEASED, True),
        (DispatchOutcome.UNKNOWN, ControllerStatus.DISPATCH_UNCERTAIN, False),
    ],
)
def test_dispatch_failure_only_compensates_proven_refusal(
    outcome: DispatchOutcome,
    status: ControllerStatus,
    release: bool,
) -> None:
    port = Port()
    port.outcome = outcome
    assert AssignmentController(port).tick(REQUEST, apply=True).status is status
    assert bool(port.snapshot.leases) is not release
    assert (EffectKind.RELEASE in [x.kind for x in port.commands]) is release


def test_ambiguous_claim_retains_lease_without_dispatch() -> None:
    port = Port()
    port.ambiguous_claim = True
    assert (
        AssignmentController(port).tick(REQUEST, apply=True).status
        is ControllerStatus.CLAIM_UNCERTAIN
    )
    assert port.snapshot.leases and not port.assignments


@pytest.mark.parametrize(
    "kind,status",
    [
        (EffectKind.CLAIM, ControllerStatus.STALE),
        (EffectKind.RELEASE, ControllerStatus.RELEASE_HELD),
    ],
)
def test_compare_and_swap_conflicts_fail_closed(kind: EffectKind, status: ControllerStatus) -> None:
    port = Port()
    port.conflict = kind
    port.outcome = DispatchOutcome.REFUSED
    assert AssignmentController(port).tick(REQUEST, apply=True).status is status
    assert not port.assignments


def test_acquisition_failure_is_not_zero() -> None:
    port = Port()
    port.fail_read = True
    assert AssignmentController(port).tick(REQUEST).status is ControllerStatus.ACQUISITION_FAILED


@pytest.mark.parametrize(
    "liveness,status",
    [
        (Liveness.UNKNOWN, ControllerStatus.LIVENESS_UNKNOWN),
        (Liveness.ACTIVE, ControllerStatus.WORKER_BUSY),
    ],
)
def test_worker_liveness_holds_closed(liveness: Liveness, status: ControllerStatus) -> None:
    port = Port()
    port.liveness = liveness
    assert AssignmentController(port).tick(REQUEST, apply=True).status is status
    assert port.commands == []


def test_zero_capacity_and_backpressure_are_distinct() -> None:
    port = Port()
    port.active = 1
    assert AssignmentController(port).tick(REQUEST).status is ControllerStatus.CAPACITY
    port.active = 0
    port.backlog = 3
    assert AssignmentController(port).tick(REQUEST).status is ControllerStatus.BACKPRESSURE
    port.snapshot = replace(port.snapshot, works=())
    assert AssignmentController(port).tick(REQUEST).status is ControllerStatus.ZERO


def test_stale_observation_and_foreign_graph_are_rejected() -> None:
    port = Port()
    view = port.acquire()
    assert (
        plan_assignment(replace(view, observed_at=9), REQUEST).status
        is ControllerStatus.ACQUISITION_FAILED
    )
    foreign = WorkId(RepositoryId("example.test", "other", "repo"), "12")
    with pytest.raises(AcquisitionFailure):
        plan_assignment(
            replace(
                view,
                graph=GraphView(replace(port.snapshot, works=(Work(foreign, "foreign"),)), "1"),
            ),
            REQUEST,
        )


def test_human_gate_blocks_assignment() -> None:
    port = Port()
    port.snapshot = replace(
        port.snapshot, boundaries=(Boundary(BoundaryId("gate"), WORK, BoundaryKind.HUMAN_APPROVAL),)
    )
    assert AssignmentController(port).tick(REQUEST, apply=True).status is ControllerStatus.ZERO
    assert not port.commands


def test_revalidation_after_claim_preserves_new_human_gate() -> None:
    class GatedPort(Port):
        def apply(self, command: EffectCommand) -> None:
            super().apply(command)
            if command.kind is EffectKind.CLAIM:
                self.snapshot = replace(
                    self.snapshot,
                    boundaries=(Boundary(BoundaryId("gate"), WORK, BoundaryKind.HUMAN_APPROVAL),),
                )

    port = GatedPort()
    assert (
        AssignmentController(port).tick(REQUEST, apply=True).status is ControllerStatus.RELEASE_HELD
    )
    assert not port.assignments


def test_request_refuses_expiry_and_unconfigured_worker() -> None:
    with pytest.raises(ValueError):
        replace(REQUEST, at=30)
    with pytest.raises(ValueError):
        replace(REQUEST, workers=frozenset())


def test_expired_lease_is_recovery_required_never_zero_or_reassigned() -> None:
    from rotisserie.domain import Lease

    port = Port()
    other = WorkerId("agent", "other")
    port.snapshot = replace(
        port.snapshot,
        workers=(*port.snapshot.workers, Worker(other)),
        leases=(Lease(LeaseId("old"), WORK, other, 1, 5),),
    )
    view = replace(port.acquire(), liveness=((WORKER, Liveness.IDLE), (other, Liveness.IDLE)))
    assert plan_assignment(view, REQUEST).status is ControllerStatus.RECOVERY_REQUIRED
    view = replace(view, liveness=((WORKER, Liveness.IDLE),))
    assert plan_assignment(view, REQUEST).status is ControllerStatus.LIVENESS_UNKNOWN


def test_failure_to_reacquire_after_claim_retains_lease() -> None:
    class FailedReadPort(Port):
        def apply(self, command: EffectCommand) -> None:
            super().apply(command)
            self.fail_read = True

    port = FailedReadPort()
    assert (
        AssignmentController(port).tick(REQUEST, apply=True).status is ControllerStatus.RELEASE_HELD
    )
    assert port.snapshot.leases and not port.assignments


def test_compensation_cannot_release_replaced_lease() -> None:
    class ReplacedLeasePort(Port):
        def dispatch(self, command: EffectCommand, assignment: Assignment) -> DispatchOutcome:
            self.snapshot = replace(
                self.snapshot, leases=(replace(self.snapshot.leases[0], id=LeaseId("replacement")),)
            )
            return DispatchOutcome.REFUSED

    port = ReplacedLeasePort()
    assert (
        AssignmentController(port).tick(REQUEST, apply=True).status is ControllerStatus.RELEASE_HELD
    )
    assert port.snapshot.leases[0].id == LeaseId("replacement")
    assert [x.kind for x in port.commands] == [EffectKind.CLAIM]


def test_dispatch_conflict_is_proven_refusal_but_release_still_requires_cas() -> None:
    port = Port()
    port.conflict = EffectKind.DISPATCH
    assert AssignmentController(port).tick(REQUEST, apply=True).status is ControllerStatus.RELEASED
    assert not port.snapshot.leases


def test_handoff_idempotency_binds_the_complete_assignment() -> None:
    first = Port()
    second = Port()
    AssignmentController(first).tick(REQUEST, apply=True)
    changed = replace(REQUEST, assignment=replace(ASSIGNMENT, objective="Different objective"))
    AssignmentController(second).tick(changed, apply=True)
    assert first.commands[0].idempotency_key == second.commands[0].idempotency_key
    assert first.commands[1].idempotency_key != second.commands[1].idempotency_key
