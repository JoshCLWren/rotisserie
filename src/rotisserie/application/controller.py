"""One bounded fresh-work controller tick over adopter-owned effect ports.

Acquisition and liveness are explicit observations, never inferred from empty
queues. The acquisition token must cover graph, capacity and liveness together.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from rotisserie.application.coordination import (
    EffectCommand,
    EffectConflict,
    EffectFailure,
    EffectKind,
    GraphView,
)
from rotisserie.application.process import assignment_to_dict
from rotisserie.application.runtime import Assignment, require_secret_free
from rotisserie.domain import (
    GraphSnapshot,
    Lease,
    LeaseId,
    RepositoryId,
    SchedulingPolicy,
    WorkerId,
)
from rotisserie.domain.policy import (
    WorkDecision,
    apply_intake_pressure,
    implementation_decision,
    ranked_implementation_work,
)


class AcquisitionFailure(RuntimeError):
    """The host could not acquire a complete authoritative controller view."""


class Liveness(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class ControllerStatus(StrEnum):
    SELECTED = "selected"
    ZERO = "no_eligible_fresh_work"
    CAPACITY = "capacity_hold"
    BACKPRESSURE = "backpressure_hold"
    ACQUISITION_FAILED = "acquisition_failed"
    LIVENESS_UNKNOWN = "liveness_unknown"
    WORKER_BUSY = "worker_busy"
    RECOVERY_REQUIRED = "recovery_required"
    STALE = "stale_state"
    CLAIM_FAILED = "claim_failed"
    CLAIM_UNCERTAIN = "claim_uncertain"
    DISPATCHED = "dispatched"
    DISPATCH_UNCERTAIN = "dispatch_uncertain"
    RELEASED = "dispatch_failed_released"
    RELEASE_HELD = "release_held"


@dataclass(frozen=True)
class ControllerView:
    graph: GraphView
    observed_at: int
    active: int
    completion_backlog: int
    liveness: tuple[tuple[WorkerId, Liveness], ...]

    def __post_init__(self) -> None:
        if any(
            type(x) is not int or x < 0
            for x in (self.observed_at, self.active, self.completion_backlog)
        ):
            raise ValueError("controller observations require non-negative integers")
        if len(dict(self.liveness)) != len(self.liveness):
            raise ValueError("duplicate worker liveness")
        if any(not isinstance(state, Liveness) for _, state in self.liveness):
            raise ValueError("invalid worker liveness")

    def state(self, worker: WorkerId) -> Liveness:
        return dict(self.liveness).get(worker, Liveness.UNKNOWN)


@dataclass(frozen=True)
class ControllerRequest:
    repository: RepositoryId
    assignment: Assignment
    lease_id: LeaseId
    operation: str
    policy: SchedulingPolicy
    at: int
    max_observation_age: int
    workers: frozenset[WorkerId]

    def __post_init__(self) -> None:
        if not self.operation or self.operation != self.operation.strip():
            raise ValueError("controller operation must be non-empty and trimmed")
        if any(type(x) is not int or x < 0 for x in (self.at, self.max_observation_age)):
            raise ValueError("controller time bounds require non-negative integers")
        if self.assignment.authority.work.repository != self.repository:
            raise ValueError("assignment escaped controller repository")
        if self.assignment.worker not in self.workers:
            raise ValueError("assignment worker is not configured")
        # This slice deliberately handles fresh implementation only.
        if self.assignment.change is not None or self.assignment.authority.revision is not None:
            raise ValueError("fresh-work controller does not accept change assignments")
        if self.assignment.authority.expires_at <= self.at:
            raise ValueError("assignment authority has expired")
        require_secret_free(self.operation, "controller operation")
        require_secret_free(self.lease_id.value, "controller lease identity")
        assignment_to_dict(self.assignment)


class DispatchOutcome(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNKNOWN = "unknown"


class ControllerPort(Protocol):
    def acquire(self) -> ControllerView:
        """Raise AcquisitionFailure on incomplete graph/capacity/liveness reads."""
        ...

    def apply(self, command: EffectCommand) -> None:
        """Durable CAS over the entire view token, including worker exclusivity.

        CLAIM must atomically reject another target owned by this worker;
        RELEASE must match the exact lease ID. EffectFailure proves no effect;
        an ambiguous acknowledgement must raise a different exception.
        """
        ...

    def dispatch(self, command: EffectCommand, assignment: Assignment) -> DispatchOutcome:
        """CAS and durably deduplicate command before handing off assignment.

        Verify live authority, lease, human gates and graph at the effect boundary.
        ACCEPTED includes a replay of an acknowledged handoff. REFUSED proves no
        worker was launched; UNKNOWN must retain ownership for reconciliation.
        """
        ...


@dataclass(frozen=True)
class ControllerResult:
    status: ControllerStatus
    selected: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    assignment: Assignment | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "selected": list(self.selected),
            "commands": list(self.commands),
            "assignment": assignment_to_dict(self.assignment) if self.assignment else None,
        }


def _scope(snapshot: GraphSnapshot, repository: RepositoryId) -> None:
    if any(
        repo != repository
        for repo in (
            *(x.id.repository for x in snapshot.works),
            *(x.id.repository for x in snapshot.changes),
            *(x.id.repository for x in snapshot.revisions),
        )
    ):
        raise AcquisitionFailure("foreign graph")


def _acquire(port: ControllerPort, request: ControllerRequest) -> ControllerView:
    view = port.acquire()
    _scope(view.graph.snapshot, request.repository)
    age = request.at - view.observed_at
    if age < 0 or age > request.max_observation_age:
        raise AcquisitionFailure("stale observation")
    return view


def plan_assignment(view: ControllerView, request: ControllerRequest) -> ControllerResult:
    """Pure same-view selection; no dispatch or completion authority is granted."""
    _scope(view.graph.snapshot, request.repository)
    age = request.at - view.observed_at
    if age < 0 or age > request.max_observation_age:
        return ControllerResult(ControllerStatus.ACQUISITION_FAILED)
    snapshot = view.graph.snapshot
    worker = request.assignment.worker
    if worker not in {x.id for x in snapshot.workers}:
        return ControllerResult(ControllerStatus.LIVENESS_UNKNOWN)
    if any(view.state(x) is Liveness.UNKNOWN for x in request.workers) or any(
        view.state(x.worker) is Liveness.UNKNOWN for x in snapshot.leases
    ):
        return ControllerResult(ControllerStatus.LIVENESS_UNKNOWN)
    if view.state(worker) is Liveness.ACTIVE or any(x.worker == worker for x in snapshot.leases):
        return ControllerResult(ControllerStatus.WORKER_BUSY)
    if any(x.expires_at <= request.at for x in snapshot.leases):
        return ControllerResult(ControllerStatus.RECOVERY_REQUIRED)
    # Every recorded lease fences a target until explicit release.
    candidates = tuple(
        x
        for x in ranked_implementation_work(snapshot, at=request.at)
        if not any(lease.work == x.id for lease in snapshot.leases)
    )
    selected = tuple(x.id.key for x in candidates)
    if not candidates:
        return ControllerResult(ControllerStatus.ZERO)
    pressured = apply_intake_pressure(
        tuple(WorkDecision(x.id, True) for x in candidates),
        completion_backlog=view.completion_backlog,
        policy=request.policy,
    )
    if not any(x.eligible for x in pressured):
        return ControllerResult(ControllerStatus.BACKPRESSURE, selected)
    if view.active >= request.policy.wip_limit:
        return ControllerResult(ControllerStatus.CAPACITY, selected)
    if candidates[0].id != request.assignment.authority.work:
        return ControllerResult(ControllerStatus.STALE, selected)
    return ControllerResult(ControllerStatus.SELECTED, selected, assignment=request.assignment)


class AssignmentController:
    """Claim and hand off at most one assignment; never loop or start a worker itself."""

    def __init__(self, port: ControllerPort) -> None:
        self._port = port

    def tick(self, request: ControllerRequest, *, apply: bool = False) -> ControllerResult:
        try:
            view = _acquire(self._port, request)
        except AcquisitionFailure:
            return ControllerResult(ControllerStatus.ACQUISITION_FAILED)
        plan = plan_assignment(view, request)
        if not apply or plan.status is not ControllerStatus.SELECTED:
            return plan
        assignment = request.assignment
        lease = Lease(
            request.lease_id,
            assignment.authority.work,
            assignment.worker,
            request.at,
            assignment.authority.expires_at,
        )
        claim = EffectCommand(
            EffectKind.CLAIM, view.graph.version, request.operation, lease.work, lease.worker, lease
        )
        try:
            self._port.apply(claim)
        except EffectConflict:
            return ControllerResult(ControllerStatus.STALE, plan.selected)
        except EffectFailure:
            return ControllerResult(ControllerStatus.CLAIM_FAILED, plan.selected)
        except Exception:
            # An unknown claim acknowledgement is not proof of an absent lease.
            return ControllerResult(ControllerStatus.CLAIM_UNCERTAIN, plan.selected)
        keys = (claim.idempotency_key,)
        try:
            live = _acquire(self._port, request)
        except AcquisitionFailure:
            return ControllerResult(ControllerStatus.RELEASE_HELD, plan.selected, keys)
        snapshot = live.graph.snapshot
        owned = snapshot.active_lease(lease.work, request.at)
        if (
            owned != lease
            or not implementation_decision(
                _snapshot_without_lease(snapshot, lease), lease.work, at=request.at
            ).eligible
        ):
            return ControllerResult(ControllerStatus.RELEASE_HELD, plan.selected, keys)
        if live.state(lease.worker) is not Liveness.IDLE:
            return ControllerResult(ControllerStatus.RELEASE_HELD, plan.selected, keys)
        assignment_digest = hashlib.sha256(
            json.dumps(
                assignment_to_dict(assignment), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        command = EffectCommand(
            EffectKind.DISPATCH,
            live.graph.version,
            f"{request.operation}:handoff:{assignment_digest}",
            lease.work,
            lease.worker,
            lease_id=lease.id,
        )
        try:
            outcome = self._port.dispatch(command, assignment)
        except EffectConflict:
            outcome = DispatchOutcome.REFUSED
        except Exception:
            outcome = DispatchOutcome.UNKNOWN
        if outcome is DispatchOutcome.ACCEPTED:
            return ControllerResult(
                ControllerStatus.DISPATCHED,
                plan.selected,
                (*keys, command.idempotency_key),
                assignment,
            )
        if outcome is not DispatchOutcome.REFUSED:
            return ControllerResult(ControllerStatus.DISPATCH_UNCERTAIN, plan.selected, keys)
        # Reacquire before compensating. Never release another lease or a live worker.
        try:
            live = _acquire(self._port, request)
            if (
                lease not in live.graph.snapshot.leases
                or live.state(lease.worker) is not Liveness.IDLE
            ):
                return ControllerResult(ControllerStatus.RELEASE_HELD, plan.selected, keys)
            release = EffectCommand(
                EffectKind.RELEASE,
                live.graph.version,
                request.operation,
                lease.work,
                lease.worker,
                lease_id=lease.id,
            )
            self._port.apply(release)
        except Exception:
            return ControllerResult(ControllerStatus.RELEASE_HELD, plan.selected, keys)
        return ControllerResult(
            ControllerStatus.RELEASED, plan.selected, (*keys, release.idempotency_key)
        )


def _snapshot_without_lease(snapshot: GraphSnapshot, lease: Lease) -> GraphSnapshot:
    return replace(snapshot, leases=tuple(x for x in snapshot.leases if x.id != lease.id))
