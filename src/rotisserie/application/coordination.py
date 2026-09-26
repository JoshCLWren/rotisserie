"""Provider-neutral orchestration over versioned graph and effect ports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from rotisserie.domain import ChangeId, Lease, LeaseId, RevisionId, WorkerId, WorkId
from rotisserie.domain.policy import (
    LeaseAction,
    SchedulingPolicy,
    allocate_capacity,
    apply_intake_pressure,
    implementation_decision,
    lease_decision,
    ranked_implementation_work,
    readiness_decision,
    reviewer_is_independent,
)
from rotisserie.domain.snapshot import GraphSnapshot


class EffectKind(StrEnum):
    CLAIM = "claim"
    RELEASE = "release"
    DISPATCH = "dispatch"
    REQUEST_REVIEW = "request_review"
    COMPLETE = "complete"


class OperationStatus(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"
    REJECTED = "rejected"


class FailureCategory(StrEnum):
    INELIGIBLE = "ineligible"
    STALE_STATE = "stale_state"
    LEASE_MISSING = "lease_missing"
    LEASE_OWNER_MISMATCH = "lease_owner_mismatch"
    REVISION_MISMATCH = "revision_mismatch"
    REVIEWER_NOT_INDEPENDENT = "reviewer_not_independent"
    NOT_READY = "not_ready"
    EFFECT_FAILED = "effect_failed"


@dataclass(frozen=True)
class GraphView:
    """A graph snapshot paired with an opaque adapter concurrency token."""

    snapshot: GraphSnapshot
    version: str

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("graph version cannot be empty")


@dataclass(frozen=True)
class EffectCommand:
    """One idempotent, compare-and-swap application effect."""

    kind: EffectKind
    expected_version: str
    operation: str
    work: WorkId
    worker: WorkerId | None = None
    lease: Lease | None = None
    lease_id: LeaseId | None = None
    change: ChangeId | None = None
    expected_revision: RevisionId | None = None
    idempotency_key: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.operation or self.operation != self.operation.strip():
            raise ValueError("operation must be a non-empty, trimmed string")
        if self.kind is EffectKind.CLAIM and self.lease is None:
            raise ValueError("claim effects require a lease")
        if self.kind in {EffectKind.DISPATCH, EffectKind.REQUEST_REVIEW, EffectKind.COMPLETE}:
            if self.worker is None and self.kind is not EffectKind.COMPLETE:
                raise ValueError("worker effect requires a worker")
        if self.change is not None and self.expected_revision is None:
            raise ValueError("change effects require an exact revision")
        identity = {
            "kind": self.kind,
            "operation": self.operation,
            "work": _work_key(self.work),
            "worker": _worker_key(self.worker),
            "lease": (
                {
                    "id": self.lease.id.value,
                    "acquired_at": self.lease.acquired_at,
                    "expires_at": self.lease.expires_at,
                }
                if self.lease
                else None
            ),
            "lease_id": self.lease_id.value if self.lease_id else None,
            "change": self.change.key if self.change else None,
            "revision": self.expected_revision.value if self.expected_revision else None,
        }
        key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        object.__setattr__(self, "idempotency_key", key)


class EffectConflict(RuntimeError):
    """The compare-and-swap precondition no longer matches durable state."""


class EffectFailure(RuntimeError):
    """An effect failed without proving that it was applied."""


class CoordinationPort(Protocol):
    """Durable graph/effect boundary implemented by a host adapter."""

    def read(self) -> GraphView: ...

    def apply(self, command: EffectCommand) -> None:
        """Apply once by idempotency key, or raise ``EffectConflict``."""


@dataclass(frozen=True)
class OperationResult:
    operation: str
    status: OperationStatus
    commands: tuple[str, ...] = ()
    failure: FailureCategory | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation": self.operation,
            "status": self.status,
            "commands": list(self.commands),
            "failure": self.failure,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RefillDecision:
    completion: int
    production: tuple[WorkId, ...]


class CoordinationService:
    """Execute application operations with mutation-boundary revalidation."""

    def __init__(self, port: CoordinationPort) -> None:
        self._port = port

    def select(
        self, *, at: int, completion_backlog: int, policy: SchedulingPolicy
    ) -> tuple[WorkId, ...]:
        snapshot = self._port.read().snapshot
        decisions = tuple(
            implementation_decision(snapshot, work.id, at=at)
            for work in ranked_implementation_work(snapshot, at=at)
        )
        pressured = apply_intake_pressure(
            decisions, completion_backlog=completion_backlog, policy=policy
        )
        return tuple(decision.work for decision in pressured if decision.eligible)

    def refill(
        self,
        *,
        at: int,
        completion_demand: int,
        active: int,
        completion_backlog: int,
        policy: SchedulingPolicy,
    ) -> RefillDecision:
        production = self.select(at=at, completion_backlog=completion_backlog, policy=policy)
        capacity = allocate_capacity(
            completion_demand=completion_demand,
            production_demand=len(production),
            active=active,
            policy=policy,
        )
        return RefillDecision(capacity.completion, production[: capacity.production])

    def completion_drain(self, *, limit: int) -> tuple[ChangeId, ...]:
        """Select exact-head-ready changes in stable identity order."""

        if limit < 0:
            raise ValueError("completion drain limit cannot be negative")
        snapshot = self._port.read().snapshot
        ready = (
            change.id
            for change in sorted(snapshot.changes, key=lambda item: item.id)
            if readiness_decision(snapshot, change.id).ready
        )
        return tuple(ready)[:limit]

    def claim(self, lease: Lease, *, operation: str, at: int) -> OperationResult:
        view = self._port.read()
        decision = lease_decision(view.snapshot, lease.work, lease.worker, at=at)
        if decision.action not in {LeaseAction.ACQUIRE, LeaseAction.KEEP}:
            return _rejected(operation, FailureCategory.INELIGIBLE, str(decision.reason))
        if decision.action is LeaseAction.KEEP:
            return OperationResult(operation, OperationStatus.NOOP)
        return self._apply(
            EffectCommand(
                EffectKind.CLAIM, view.version, operation, lease.work, lease.worker, lease
            )
        )

    def revalidate(
        self, work: WorkId, worker: WorkerId, *, operation: str, at: int
    ) -> OperationResult:
        view = self._port.read()
        decision = lease_decision(view.snapshot, work, worker, at=at)
        if decision.action is LeaseAction.KEEP:
            return OperationResult(operation, OperationStatus.NOOP)
        if decision.action is LeaseAction.RELEASE:
            lease = view.snapshot.active_lease(work, at)
            assert lease is not None
            return self._apply(
                EffectCommand(
                    EffectKind.RELEASE,
                    view.version,
                    operation,
                    work,
                    worker,
                    lease_id=lease.id,
                )
            )
        return _rejected(operation, FailureCategory.LEASE_MISSING, str(decision.reason))

    def release(
        self, work: WorkId, worker: WorkerId, *, operation: str, at: int
    ) -> OperationResult:
        view = self._port.read()
        lease = view.snapshot.active_lease(work, at)
        if lease is None:
            return OperationResult(operation, OperationStatus.NOOP)
        if lease.worker != worker:
            return _rejected(operation, FailureCategory.LEASE_OWNER_MISMATCH)
        return self._apply(
            EffectCommand(
                EffectKind.RELEASE, view.version, operation, work, worker, lease_id=lease.id
            )
        )

    def dispatch(
        self, work: WorkId, worker: WorkerId, *, operation: str, at: int
    ) -> OperationResult:
        view = self._port.read()
        lease = view.snapshot.active_lease(work, at)
        if lease is None:
            return _rejected(operation, FailureCategory.LEASE_MISSING)
        if lease.worker != worker:
            return _rejected(operation, FailureCategory.LEASE_OWNER_MISMATCH)
        return self._apply(
            EffectCommand(
                EffectKind.DISPATCH, view.version, operation, work, worker, lease_id=lease.id
            )
        )

    def review_transition(
        self,
        change: ChangeId,
        revision: RevisionId,
        reviewer: WorkerId,
        *,
        operation: str,
    ) -> OperationResult:
        view = self._port.read()
        item = next((item for item in view.snapshot.changes if item.id == change), None)
        if item is None or item.head != revision:
            return _rejected(operation, FailureCategory.REVISION_MISMATCH)
        if not reviewer_is_independent(item, reviewer):
            return _rejected(operation, FailureCategory.REVIEWER_NOT_INDEPENDENT)
        return self._apply(
            EffectCommand(
                EffectKind.REQUEST_REVIEW,
                view.version,
                operation,
                item.work,
                reviewer,
                change=change,
                expected_revision=revision,
            )
        )

    def completion(
        self, change: ChangeId, revision: RevisionId, *, operation: str
    ) -> OperationResult:
        view = self._port.read()
        item = next((item for item in view.snapshot.changes if item.id == change), None)
        if item is None or item.head != revision:
            return _rejected(operation, FailureCategory.REVISION_MISMATCH)
        readiness = readiness_decision(view.snapshot, change)
        if not readiness.ready:
            return _rejected(operation, FailureCategory.NOT_READY, ",".join(readiness.blocks))
        return self._apply(
            EffectCommand(
                EffectKind.COMPLETE,
                view.version,
                operation,
                item.work,
                change=change,
                expected_revision=revision,
            )
        )

    def recover(self, *, operation: str, at: int) -> OperationResult:
        command_keys: list[str] = []
        while True:
            view = self._port.read()
            expired = sorted(
                (lease for lease in view.snapshot.leases if lease.expires_at <= at),
                key=lambda lease: (lease.expires_at, lease.id),
            )
            if not expired:
                status = OperationStatus.APPLIED if command_keys else OperationStatus.NOOP
                return OperationResult(operation, status, tuple(command_keys))
            lease = expired[0]
            result = self._apply(
                EffectCommand(
                    EffectKind.RELEASE,
                    view.version,
                    operation,
                    lease.work,
                    lease.worker,
                    lease_id=lease.id,
                )
            )
            if result.status is OperationStatus.REJECTED:
                return OperationResult(
                    operation, result.status, tuple(command_keys), result.failure, result.detail
                )
            command_keys.extend(result.commands)

    def _apply(self, command: EffectCommand) -> OperationResult:
        try:
            self._port.apply(command)
        except EffectConflict as exc:
            return _rejected(command.operation, FailureCategory.STALE_STATE, str(exc))
        except EffectFailure as exc:
            return _rejected(command.operation, FailureCategory.EFFECT_FAILED, str(exc))
        return OperationResult(
            command.operation, OperationStatus.APPLIED, (command.idempotency_key,)
        )


def _rejected(
    operation: str, failure: FailureCategory, detail: str | None = None
) -> OperationResult:
    return OperationResult(operation, OperationStatus.REJECTED, failure=failure, detail=detail)


def _work_key(work: WorkId) -> tuple[str, str, str, str]:
    repo = work.repository
    return repo.host, repo.owner, repo.name, work.key


def _worker_key(worker: WorkerId | None) -> tuple[str, str] | None:
    return (worker.namespace, worker.value) if worker else None
