"""Bounded expired-lease recovery over authoritative observations and durable CAS."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from rotisserie.application.controller import AcquisitionFailure, ControllerView, Liveness, _scope
from rotisserie.application.coordination import (
    EffectCommand,
    EffectConflict,
    EffectFailure,
    EffectKind,
)
from rotisserie.application.runtime import require_secret_free
from rotisserie.domain import Lease, RepositoryId, WorkerId


class RecoveryStatus(StrEnum):
    PLANNED = "recovery_planned"
    ZERO = "no_expired_lease"
    OBSERVATION_FAILED = "observation_failed"
    LIVENESS_HOLD = "liveness_hold"
    ACTIVITY_HOLD = "activity_hold"
    STALE = "stale_state"
    RELEASED = "lease_released"
    FAILED = "release_failed"
    UNCERTAIN = "release_uncertain"


@dataclass(frozen=True)
class ActivityEvidence:
    """Adapter attestation of complete trusted activity acquisition for an exact lease."""

    lease: Lease
    observed_at: int
    latest_activity_at: int | None
    trusted: bool

    def __post_init__(self) -> None:
        if type(self.trusted) is not bool:
            raise ValueError("activity trust must be explicit")
        if type(self.observed_at) is not int:
            raise ValueError("activity observation time is required")
        for value in (self.observed_at, self.latest_activity_at):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("activity timestamps require non-negative integers")


@dataclass(frozen=True)
class RecoveryView:
    controller: ControllerView
    activity: tuple[ActivityEvidence, ...]
    liveness_complete: bool

    def __post_init__(self) -> None:
        if type(self.liveness_complete) is not bool:
            raise ValueError("liveness completeness must be explicit")
        if len({x.lease.id for x in self.activity}) != len(self.activity):
            raise ValueError("ambiguous lease activity")


@dataclass(frozen=True)
class RecoveryRequest:
    repository: RepositoryId
    operation: str
    at: int
    max_observation_age: int
    inactivity_ttl: int
    workers: frozenset[WorkerId]

    def __post_init__(self) -> None:
        if any(
            type(x) is not int or x < 0
            for x in (self.at, self.max_observation_age, self.inactivity_ttl)
        ):
            raise ValueError("recovery bounds require non-negative integers")
        if not self.operation or self.operation != self.operation.strip():
            raise ValueError("recovery operation must be non-empty and trimmed")
        require_secret_free(self.operation, "recovery operation")


@dataclass(frozen=True)
class RecoveryResult:
    status: RecoveryStatus
    lease: Lease | None = None
    commands: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "lease": lease_to_dict(self.lease) if self.lease else None,
            "commands": list(self.commands),
        }


def lease_to_dict(lease: Lease) -> dict[str, object]:
    repo = lease.work.repository
    return {
        "id": lease.id.value,
        "work": {
            "repository": {"host": repo.host, "owner": repo.owner, "name": repo.name},
            "key": lease.work.key,
        },
        "worker": {"namespace": lease.worker.namespace, "value": lease.worker.value},
        "acquired_at": lease.acquired_at,
        "expires_at": lease.expires_at,
    }


def _fresh(observed_at: int, request: RecoveryRequest) -> bool:
    return 0 <= request.at - observed_at <= request.max_observation_age


def plan_recovery(view: RecoveryView, request: RecoveryRequest) -> RecoveryResult:
    """Inspect only the first expired lease, ordered by expiry and identity; never skip holds."""
    controller = view.controller
    try:
        _scope(controller.graph.snapshot, request.repository)
    except AcquisitionFailure:
        return RecoveryResult(RecoveryStatus.OBSERVATION_FAILED)
    if not _fresh(controller.observed_at, request):
        return RecoveryResult(RecoveryStatus.OBSERVATION_FAILED)
    if not view.liveness_complete or any(
        controller.state(worker) is Liveness.UNKNOWN
        for worker in request.workers | {x.worker for x in controller.graph.snapshot.leases}
    ):
        return RecoveryResult(RecoveryStatus.LIVENESS_HOLD)
    expired = sorted(
        (x for x in controller.graph.snapshot.leases if x.expires_at <= request.at),
        key=lambda x: (x.expires_at, x.id.value),
    )
    if not expired:
        return RecoveryResult(RecoveryStatus.ZERO)
    lease = expired[0]
    if lease.worker not in request.workers or controller.state(lease.worker) is not Liveness.IDLE:
        return RecoveryResult(RecoveryStatus.LIVENESS_HOLD, lease)
    evidence = next((x for x in view.activity if x.lease == lease), None)
    if (
        evidence is None
        or not evidence.trusted
        or not _fresh(evidence.observed_at, request)
        or evidence.latest_activity_at is None
        or not lease.acquired_at <= evidence.latest_activity_at <= evidence.observed_at
    ):
        return RecoveryResult(RecoveryStatus.OBSERVATION_FAILED, lease)
    if request.at - evidence.latest_activity_at <= request.inactivity_ttl:
        return RecoveryResult(RecoveryStatus.ACTIVITY_HOLD, lease)
    return RecoveryResult(RecoveryStatus.PLANNED, lease)


@dataclass(frozen=True)
class RecoveryRelease:
    """CAS preconditions include the full lease and complete observation token."""

    command: EffectCommand
    expected_lease: Lease
    request: RecoveryRequest


class RecoveryPort(Protocol):
    def acquire(self) -> RecoveryView:
        """Acquire graph, capacity, complete liveness and trusted activity under one token."""
        ...

    def release(self, release: RecoveryRelease) -> None:
        """Atomically compare token and full lease, revalidate expiry/inactivity/idle now.

        Token must fence activity and liveness changes as well as graph changes.
        Durably deduplicate by command key. EffectFailure proves no mutation;
        ambiguous acknowledgements must use another exception. No worker launch.
        """
        ...


class LeaseRecoveryController:
    def __init__(self, port: RecoveryPort) -> None:
        self._port = port

    def recover(self, request: RecoveryRequest, *, apply: bool = False) -> RecoveryResult:
        try:
            plan = plan_recovery(self._port.acquire(), request)
        except Exception:
            return RecoveryResult(RecoveryStatus.OBSERVATION_FAILED)
        if not apply or plan.status is not RecoveryStatus.PLANNED:
            return plan
        try:
            live = self._port.acquire()
            current = plan_recovery(live, request)
        except Exception:
            return RecoveryResult(RecoveryStatus.OBSERVATION_FAILED, plan.lease)
        if current.status is not RecoveryStatus.PLANNED:
            return current
        if current.lease != plan.lease:
            return RecoveryResult(RecoveryStatus.STALE, plan.lease)
        assert current.lease is not None
        lease = current.lease
        # Bind retries to the entire lease, including renewal timestamps.
        digest = hashlib.sha256(
            json.dumps(lease_to_dict(lease), sort_keys=True).encode()
        ).hexdigest()
        command = EffectCommand(
            EffectKind.RELEASE,
            live.controller.graph.version,
            f"{request.operation}:{digest}",
            lease.work,
            lease.worker,
            lease_id=lease.id,
        )
        try:
            self._port.release(RecoveryRelease(command, lease, request))
        except EffectConflict:
            return RecoveryResult(RecoveryStatus.STALE, lease)
        except EffectFailure:
            return RecoveryResult(RecoveryStatus.FAILED, lease)
        except Exception:
            return RecoveryResult(RecoveryStatus.UNCERTAIN, lease)
        return RecoveryResult(RecoveryStatus.RELEASED, lease, (command.idempotency_key,))
