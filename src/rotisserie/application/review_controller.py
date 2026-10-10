"""One exact-head independent review claim/request and bounded worker handoff."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from rotisserie.application.controller import (
    AcquisitionFailure,
    ControllerView,
    DispatchOutcome,
    Liveness,
    _scope,
)
from rotisserie.application.coordination import (
    EffectCommand,
    EffectConflict,
    EffectFailure,
    EffectKind,
)
from rotisserie.application.process import assignment_to_dict
from rotisserie.application.recovery import lease_to_dict
from rotisserie.application.runtime import Assignment, require_secret_free
from rotisserie.domain import (
    ChangeId,
    Lease,
    LeaseId,
    RepositoryId,
    ReviewIndependenceSignals,
    SchedulingPolicy,
    WorkerId,
    WorkState,
)


class ReviewStatus(StrEnum):
    SELECTED = "selected"
    ZERO = "no_eligible_review_work"
    PROVENANCE = "provenance_hold"
    LIVENESS = "liveness_hold"
    CAPACITY = "capacity_hold"
    BACKPRESSURE = "backpressure_hold"
    ACQUISITION = "acquisition_failed"
    BUSY = "worker_busy"
    RECOVERY = "recovery_required"
    STALE = "stale_state"
    CLAIM_FAILED = "claim_failed"
    CLAIM_UNCERTAIN = "claim_uncertain"
    DISPATCHED = "dispatched"
    DISPATCH_UNCERTAIN = "dispatch_uncertain"
    RELEASED = "dispatch_failed_released"
    RELEASE_HELD = "release_held"


@dataclass(frozen=True)
class ReviewCandidate:
    """Adapter-declared review demand; signals include the complete reachable lineage.

    Approvers are semantic exact-head attestations, never formal host approvals.
    complete/trusted attest acquisition, identity resolution and provenance trust.
    """

    change: ChangeId
    signals: ReviewIndependenceSignals
    observed_at: int
    complete: bool
    trusted: bool

    def __post_init__(self) -> None:
        if type(self.observed_at) is not int or self.observed_at < 0:
            raise ValueError("provenance observation time must be explicit")
        if any(
            type(x) is not bool
            for x in (
                self.complete,
                self.trusted,
                self.signals.producer_attested,
                self.signals.provenance_recorded,
            )
        ):
            raise ValueError("provenance trust/completeness must be explicit")

    def to_dict(self) -> dict[str, object]:
        s = self.signals
        return {
            "change": self.change.key,
            "revision": s.revision.value,
            "contributors": [_worker(x) for x in sorted(s.contributors)],
            "approvers": [_worker(x) for x in sorted(s.approvers)],
            "producer": _worker(s.producer) if s.producer else None,
            "producer_attested": s.producer_attested,
            "provenance_recorded": s.provenance_recorded,
            "observed_at": self.observed_at,
            "complete": self.complete,
            "trusted": self.trusted,
        }


def _worker(worker: WorkerId) -> dict[str, str]:
    return {"namespace": worker.namespace, "value": worker.value}


@dataclass(frozen=True)
class ReviewBinding:
    """Durable claim AND review request, bound to the full process assignment."""

    lease: Lease
    candidate: ReviewCandidate
    assignment: Assignment

    def __post_init__(self) -> None:
        a = self.assignment
        if (
            a.change != self.candidate.change
            or a.authority.revision != self.candidate.signals.revision
            or a.worker != self.lease.worker
            or a.authority.work != self.lease.work
            or a.authority.expires_at != self.lease.expires_at
        ):
            raise ValueError("review binding must match exact assignment and lease")
        require_secret_free(self.lease.id.value, "review binding")
        assignment_to_dict(a)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "lease": lease_to_dict(self.lease),
            "candidate": self.candidate.to_dict(),
            "assignment": assignment_to_dict(self.assignment),
        }


@dataclass(frozen=True)
class ReviewView:
    controller: ControllerView
    candidates: tuple[ReviewCandidate, ...]
    liveness_complete: bool
    bindings: tuple[ReviewBinding, ...] = ()

    def __post_init__(self) -> None:
        if type(self.liveness_complete) is not bool:
            raise ValueError("liveness completeness must be explicit")
        if len({x.change for x in self.candidates}) != len(self.candidates):
            raise ValueError("ambiguous review demand")
        if len({x.lease.id for x in self.bindings}) != len(self.bindings):
            raise ValueError("ambiguous review binding")


@dataclass(frozen=True)
class ReviewRequest:
    repository: RepositoryId
    assignment: Assignment
    lease_id: LeaseId
    operation: str
    policy: SchedulingPolicy
    at: int
    max_observation_age: int
    workers: frozenset[WorkerId]

    def __post_init__(self) -> None:
        if any(type(x) is not int or x < 0 for x in (self.at, self.max_observation_age)):
            raise ValueError("review time bounds require non-negative integers")
        a = self.assignment
        if (
            a.authority.work.repository != self.repository
            or a.change is None
            or a.authority.revision is None
            or a.worker not in self.workers
            or a.authority.expires_at <= self.at
        ):
            raise ValueError("review requires configured worker and live exact-change authority")
        if not self.operation or self.operation != self.operation.strip():
            raise ValueError("review operation must be non-empty and trimmed")
        require_secret_free(self.operation, "review operation")
        require_secret_free(self.lease_id.value, "review lease identity")
        assignment_to_dict(a)


@dataclass(frozen=True)
class ReviewResult:
    status: ReviewStatus
    binding: ReviewBinding | None = None
    commands: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "binding": self.binding.to_dict() if self.binding else None,
            "commands": list(self.commands),
        }


def _fresh(at: int, request: ReviewRequest) -> bool:
    return 0 <= request.at - at <= request.max_observation_age


def plan_review(view: ReviewView, request: ReviewRequest) -> ReviewResult:
    c = view.controller
    snapshot = c.graph.snapshot
    try:
        _scope(snapshot, request.repository)
    except AcquisitionFailure:
        return ReviewResult(ReviewStatus.ACQUISITION)
    if not _fresh(c.observed_at, request):
        return ReviewResult(ReviewStatus.ACQUISITION)
    known = {x.id for x in snapshot.workers}
    if (
        not view.liveness_complete
        or not request.workers <= known
        or any(
            c.state(x) is Liveness.UNKNOWN
            for x in request.workers | {lease.worker for lease in snapshot.leases}
        )
    ):
        return ReviewResult(ReviewStatus.LIVENESS)
    a = request.assignment
    if c.state(a.worker) is not Liveness.IDLE or any(
        lease.worker == a.worker for lease in snapshot.leases
    ):
        return ReviewResult(ReviewStatus.BUSY)
    if any(lease.id == request.lease_id for lease in snapshot.leases):
        return ReviewResult(ReviewStatus.STALE)
    if any(lease.expires_at <= request.at for lease in snapshot.leases):
        return ReviewResult(ReviewStatus.RECOVERY)
    changes = {x.id: x for x in snapshot.changes}
    works = {x.id: x for x in snapshot.works}
    for candidate in view.candidates:
        change = changes.get(candidate.change)
        if change is None or candidate.change.repository != request.repository:
            return ReviewResult(ReviewStatus.ACQUISITION)
        s = candidate.signals
        if (
            not candidate.complete
            or not candidate.trusted
            or not _fresh(candidate.observed_at, request)
            or s.revision != change.head
            or not s.provenance_recorded
            or not s.producer_attested
            or s.producer is None
            or s.producer != change.producer
            or s.producer not in s.contributors
            or not s.contributors <= known
            or not s.approvers <= known
        ):
            return ReviewResult(ReviewStatus.PROVENANCE)
        # Reuse the domain contributor-set independence rule; approvals never grant authority.
        if a.worker in s.excluded_reviewers() or a.worker in s.approvers:
            continue
        if (
            works[change.work].state is not WorkState.OPEN
            or not snapshot.boundaries_satisfied(change.work)
            or any(
                works[p].state is not WorkState.COMPLETED
                for p in snapshot.prerequisites(change.work)
            )
            or any(lease.work == change.work for lease in snapshot.leases)
            or any(b.lease.work == change.work for b in view.bindings)
        ):
            continue
        if (
            request.policy.backlog_limit is not None
            and c.completion_backlog >= request.policy.backlog_limit
        ):
            return ReviewResult(ReviewStatus.BACKPRESSURE)
        if c.active >= request.policy.wip_limit:
            return ReviewResult(ReviewStatus.CAPACITY)
        if (
            a.change != change.id
            or a.authority.revision != change.head
            or a.authority.work != change.work
        ):
            return ReviewResult(ReviewStatus.STALE)
        lease = Lease(request.lease_id, change.work, a.worker, request.at, a.authority.expires_at)
        return ReviewResult(ReviewStatus.SELECTED, ReviewBinding(lease, candidate, a))
    return ReviewResult(ReviewStatus.ZERO)


@dataclass(frozen=True)
class ReviewEffect:
    command: EffectCommand
    binding: ReviewBinding
    request: ReviewRequest


class ReviewPort(Protocol):
    def acquire(self) -> ReviewView:
        """Acquire graph, demand, provenance, capacity and liveness under one token."""
        ...

    def apply(self, effect: ReviewEffect) -> None:
        """Durable full-view CAS. CLAIM atomically persists lease AND review binding/request.

        RELEASE matches/removes only the full exact binding and lease. Recheck
        clock, observation freshness, head, contributors, human gates, worker
        exclusivity and capacity.
        Tokens fence ALL observations. EffectFailure proves unapplied; other
        failures are uncertain. Deduplicate by command identity before effects.
        """
        ...

    def dispatch(self, effect: ReviewEffect) -> DispatchOutcome:
        """CAS the full view and exact durable binding; revalidate authority with clock.

        Durably deduplicate before handing effect.binding.assignment to the
        existing process boundary. REFUSED/EffectConflict prove no worker start;
        uncertain acknowledgement retains ownership. No host approval authority.
        """
        ...


def _same_view(before: ReviewView, after: ReviewView, binding: ReviewBinding | None = None) -> bool:
    """Any concurrent semantic change holds closed, including other worker activity."""
    if binding is not None:
        if (
            binding not in after.bindings
            or binding.lease not in after.controller.graph.snapshot.leases
        ):
            return False
        c = after.controller
        if c.active < 1:
            return False
        after = replace(
            after,
            bindings=tuple(b for b in after.bindings if b != binding),
            controller=replace(
                c,
                active=c.active - 1,
                graph=replace(
                    c.graph,
                    snapshot=replace(
                        c.graph.snapshot,
                        leases=tuple(
                            lease for lease in c.graph.snapshot.leases if lease != binding.lease
                        ),
                    ),
                ),
            ),
        )
    return (
        before.controller.graph.snapshot == after.controller.graph.snapshot
        and before.controller.active == after.controller.active
        and before.controller.completion_backlog == after.controller.completion_backlog
        and dict(before.controller.liveness) == dict(after.controller.liveness)
        and before.liveness_complete == after.liveness_complete
        and before.candidates == after.candidates
        and before.bindings == after.bindings
    )


class ReviewAssignmentController:
    def __init__(self, port: ReviewPort) -> None:
        self._port = port

    def tick(self, request: ReviewRequest, *, apply: bool = False) -> ReviewResult:
        try:
            initial = self._port.acquire()
            plan = plan_review(initial, request)
        except Exception:
            return ReviewResult(ReviewStatus.ACQUISITION)
        if not apply or plan.status is not ReviewStatus.SELECTED:
            return plan
        assert plan.binding is not None
        binding = plan.binding
        keys: tuple[str, ...] = ()

        def effect(kind: EffectKind, live: ReviewView) -> ReviewEffect:
            digest = hashlib.sha256(
                json.dumps(binding.to_dict(), sort_keys=True).encode()
            ).hexdigest()
            return ReviewEffect(
                EffectCommand(
                    kind,
                    live.controller.graph.version,
                    f"{request.operation}:{digest}",
                    binding.lease.work,
                    binding.lease.worker,
                    lease=binding.lease,
                    lease_id=binding.lease.id,
                    change=binding.candidate.change,
                    expected_revision=binding.candidate.signals.revision,
                ),
                binding,
                request,
            )

        try:
            live = self._port.acquire()
            if not _same_view(initial, live):
                return ReviewResult(ReviewStatus.STALE, binding)
            current = plan_review(live, request)
            if current.status is not ReviewStatus.SELECTED:
                return current
            if current.binding != binding:
                return ReviewResult(ReviewStatus.STALE, binding)
        except Exception:
            return ReviewResult(ReviewStatus.ACQUISITION, binding)
        claim = effect(EffectKind.CLAIM, live)
        try:
            self._port.apply(claim)
        except EffectConflict:
            return ReviewResult(ReviewStatus.STALE, binding)
        except EffectFailure:
            return ReviewResult(ReviewStatus.CLAIM_FAILED, binding)
        except Exception:
            return ReviewResult(ReviewStatus.CLAIM_UNCERTAIN, binding)
        keys = (claim.command.idempotency_key,)
        try:
            live = self._port.acquire()
            if not _fresh(live.controller.observed_at, request) or not _same_view(
                initial, live, binding
            ):
                return ReviewResult(ReviewStatus.STALE, binding, keys)
            handoff = effect(EffectKind.DISPATCH, live)
        except Exception:
            return ReviewResult(ReviewStatus.ACQUISITION, binding, keys)
        try:
            outcome = self._port.dispatch(handoff)
        except EffectConflict:
            outcome = DispatchOutcome.REFUSED
        except Exception:
            outcome = DispatchOutcome.UNKNOWN
        if outcome is DispatchOutcome.ACCEPTED:
            return ReviewResult(
                ReviewStatus.DISPATCHED, binding, (*keys, handoff.command.idempotency_key)
            )
        if outcome is not DispatchOutcome.REFUSED:
            return ReviewResult(ReviewStatus.DISPATCH_UNCERTAIN, binding, keys)
        try:
            live = self._port.acquire()
            if not _fresh(live.controller.observed_at, request) or not _same_view(
                initial, live, binding
            ):
                return ReviewResult(ReviewStatus.RELEASE_HELD, binding, keys)
            release = effect(EffectKind.RELEASE, live)
            self._port.apply(release)
        except Exception:
            return ReviewResult(ReviewStatus.RELEASE_HELD, binding, keys)
        return ReviewResult(
            ReviewStatus.RELEASED, binding, (*keys, release.command.idempotency_key)
        )
