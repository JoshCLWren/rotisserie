"""One exact-head completion claim and guarded merge; no post-merge authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from rotisserie.application.controller import AcquisitionFailure, ControllerView, Liveness, _scope
from rotisserie.application.coordination import (
    EffectCommand,
    EffectConflict,
    EffectFailure,
    EffectKind,
)
from rotisserie.application.process import _secret_free
from rotisserie.application.recovery import lease_to_dict
from rotisserie.application.review_controller import ReviewCandidate, _worker
from rotisserie.domain import (
    ChangeId,
    Check,
    CheckStatus,
    Lease,
    LeaseId,
    RepositoryId,
    Review,
    ReviewDecision,
    RevisionId,
    SchedulingPolicy,
    WorkerId,
    WorkState,
    evaluate_review_independence,
)


class CompletionStatus(StrEnum):
    SELECTED = "selected"
    ZERO = "no_eligible_completion_work"
    ACQUISITION = "acquisition_failed"
    PROVENANCE = "provenance_hold"
    REVIEW = "review_hold"
    CI = "ci_hold"
    READINESS = "readiness_hold"
    HUMAN = "human_gate_hold"
    PREREQUISITE = "prerequisite_hold"
    LIVENESS = "liveness_hold"
    CAPACITY = "capacity_hold"
    BACKPRESSURE = "backpressure_hold"
    OWNERSHIP = "ownership_hold"
    STALE = "stale_state"
    CLAIM_FAILED = "claim_failed"
    CLAIM_UNCERTAIN = "claim_uncertain"
    COMPLETED = "completed"
    REFUSED = "completion_refused"
    FAILED = "completion_failed"
    UNCERTAIN = "completion_uncertain"


class CompletionPhase(StrEnum):
    CLAIMED = "claimed"
    # Persist BEFORE the remote effect. A crash here must never look unapplied.
    ATTEMPTING = "attempting"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompletionEvidence:
    """Complete trusted semantic reviews and CI/readiness reads for one change/head.

    Reviews are adapter-decoded semantic attestations, never host approvals.
    Check values also represent mechanical readiness gates (in a separate lane).
    The attestation covers pagination, latest verdicts, revocations and identity
    resolution. Unknown/ambiguous reads must set complete or trusted false.
    """

    provenance: ReviewCandidate
    reviews: tuple[Review, ...]
    ci: tuple[Check, ...]
    readiness: tuple[Check, ...]
    observed_at: int
    complete: bool
    trusted: bool

    def __post_init__(self) -> None:
        if type(self.observed_at) is not int or self.observed_at < 0:
            raise ValueError("completion evidence requires an explicit observation time")
        if any(type(x) is not bool for x in (self.complete, self.trusted)):
            raise ValueError("completion evidence attestations require booleans")
        if len({x.id for x in self.reviews}) != len(self.reviews) or len(
            {x.reviewer for x in self.reviews}
        ) != len(self.reviews):
            raise ValueError("ambiguous semantic review evidence")
        for checks in (self.ci, self.readiness):
            if len({x.id.name for x in checks}) != len(checks):
                raise ValueError("ambiguous completion check evidence")
            if any(
                type(x.required) is not bool or not isinstance(x.status, CheckStatus)
                for x in checks
            ):
                raise ValueError("invalid completion check")
        if any(
            type(x.required) is not bool or not isinstance(x.decision, ReviewDecision)
            for x in self.reviews
        ):
            raise ValueError("invalid semantic review")
        _secret_free(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        def checks(values: tuple[Check, ...]) -> list[dict[str, object]]:
            return [
                {
                    "revision": x.id.revision.value,
                    "name": x.id.name,
                    "status": x.status,
                    "required": x.required,
                }
                for x in values
            ]

        return {
            "provenance": self.provenance.to_dict(),
            "reviews": [
                {
                    "id": x.id.value,
                    "revision": x.revision.value,
                    "reviewer": _worker(x.reviewer),
                    "decision": x.decision,
                    "required": x.required,
                }
                for x in self.reviews
            ],
            "ci": checks(self.ci),
            "readiness": checks(self.readiness),
            "observed_at": self.observed_at,
            "complete": self.complete,
            "trusted": self.trusted,
        }


@dataclass(frozen=True)
class CompletionRequest:
    repository: RepositoryId
    change: ChangeId
    revision: RevisionId
    worker: WorkerId
    lease_id: LeaseId
    operation: str
    policy: SchedulingPolicy
    at: int
    max_observation_age: int
    lease_ttl: int
    workers: frozenset[WorkerId]
    required_ci: frozenset[str]
    required_readiness: frozenset[str]

    def __post_init__(self) -> None:
        if self.change.repository != self.repository or self.revision.repository != self.repository:
            raise ValueError("completion authority escaped repository")
        if self.worker not in self.workers:
            raise ValueError("completion owner must be configured")
        if (
            any(
                type(x) is not int or x < 0
                for x in (self.at, self.max_observation_age, self.lease_ttl)
            )
            or self.lease_ttl == 0
        ):
            raise ValueError("completion requires explicit time bounds and positive lease TTL")
        if not self.operation or self.operation != self.operation.strip():
            raise ValueError("completion operation must be non-empty and trimmed")
        for names in (self.required_ci, self.required_readiness):
            if not names or any(not x or x != x.strip() for x in names):
                raise ValueError("completion requires non-empty configured gate sets")
        _secret_free(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        r = self.repository
        return {
            "schema_version": 1,
            "repository": {"host": r.host, "owner": r.owner, "name": r.name},
            "change": self.change.key,
            "revision": self.revision.value,
            "worker": _worker(self.worker),
            "lease_id": self.lease_id.value,
            "operation": self.operation,
            "at": self.at,
            "max_observation_age": self.max_observation_age,
            "lease_ttl": self.lease_ttl,
            "workers": [_worker(x) for x in sorted(self.workers)],
            "wip_limit": self.policy.wip_limit,
            "backlog_limit": self.policy.backlog_limit,
            "reserved_review": self.policy.reserved_review,
            "required_ci": sorted(self.required_ci),
            "required_readiness": sorted(self.required_readiness),
        }


@dataclass(frozen=True)
class CompletionBinding:
    lease: Lease
    evidence: CompletionEvidence
    request: CompletionRequest
    authorization_digest: str

    def __post_init__(self) -> None:
        p, r = self.evidence.provenance, self.request
        if (
            p.change != r.change
            or p.signals.revision != r.revision
            or self.lease.worker != r.worker
            or self.lease.id != r.lease_id
            or self.lease.work.repository != r.repository
            or self.lease.acquired_at != r.at
            or self.lease.expires_at != r.at + r.lease_ttl
        ):
            raise ValueError("completion binding must match exact ownership and authority")
        if len(self.authorization_digest) != 64 or any(
            x not in "0123456789abcdef" for x in self.authorization_digest
        ):
            raise ValueError("completion requires a complete authorization digest")
        _secret_free(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "lease": lease_to_dict(self.lease),
            "evidence": self.evidence.to_dict(),
            "request": self.request.to_dict(),
            "authorization_digest": self.authorization_digest,
        }


@dataclass(frozen=True)
class CompletionRecord:
    binding: CompletionBinding
    phase: CompletionPhase = CompletionPhase.CLAIMED

    def __post_init__(self) -> None:
        if not isinstance(self.phase, CompletionPhase):
            raise ValueError("invalid durable completion phase")

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "binding": self.binding.to_dict(), "phase": self.phase}


@dataclass(frozen=True)
class CompletionView:
    controller: ControllerView
    candidates: tuple[CompletionEvidence, ...]
    liveness_complete: bool
    records: tuple[CompletionRecord, ...] = ()

    def __post_init__(self) -> None:
        if type(self.liveness_complete) is not bool:
            raise ValueError("completion liveness completeness must be explicit")
        if len({x.provenance.change for x in self.candidates}) != len(self.candidates):
            raise ValueError("ambiguous completion demand")
        if len({x.binding.lease.work for x in self.records}) != len(self.records) or len(
            {x.binding.lease.id for x in self.records}
        ) != len(self.records):
            raise ValueError("ambiguous durable completion ownership")


@dataclass(frozen=True)
class CompletionResult:
    status: CompletionStatus
    binding: CompletionBinding | None = None
    commands: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "binding": self.binding.to_dict() if self.binding else None,
            "commands": list(self.commands),
        }


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _observation(view: CompletionView) -> dict[str, object]:
    c = view.controller
    return {
        "snapshot": c.graph.snapshot.to_dict(),
        "observed_at": c.observed_at,
        "active": c.active,
        "completion_backlog": c.completion_backlog,
        "liveness": [{"worker": _worker(w), "state": s} for w, s in sorted(c.liveness)],
        "liveness_complete": view.liveness_complete,
        "candidates": [x.to_dict() for x in view.candidates],
        "records": [x.to_dict() for x in view.records],
    }


def _fresh(observed_at: int, request: CompletionRequest) -> bool:
    return 0 <= request.at - observed_at <= request.max_observation_age


def plan_completion(view: CompletionView, request: CompletionRequest) -> CompletionResult:
    """Evaluate the first declared completion demand; never bypass a held head."""
    c, r = view.controller, request
    s = c.graph.snapshot

    def hold(status: CompletionStatus) -> CompletionResult:
        return CompletionResult(status)

    try:
        _scope(s, r.repository)
    except AcquisitionFailure:
        return hold(CompletionStatus.ACQUISITION)
    changes = {x.id: x for x in s.changes}
    if any(
        x.binding.request.repository != r.repository
        or x.binding.request.change not in changes
        or changes[x.binding.request.change].work != x.binding.lease.work
        for x in view.records
    ):
        return hold(CompletionStatus.ACQUISITION)
    if not _fresh(c.observed_at, r):
        return hold(CompletionStatus.ACQUISITION)
    known = {x.id for x in s.workers}
    if (
        not view.liveness_complete
        or not r.workers <= known
        or any(
            c.state(x) is Liveness.UNKNOWN for x in r.workers | {lease.worker for lease in s.leases}
        )
    ):
        return hold(CompletionStatus.LIVENESS)
    if not view.candidates:
        return hold(CompletionStatus.ZERO)
    e = view.candidates[0]
    p, signals = e.provenance, e.provenance.signals
    change = next((x for x in s.changes if x.id == p.change), None)
    if change is None or p.change.repository != r.repository:
        return hold(CompletionStatus.ACQUISITION)
    if (
        not p.complete
        or not p.trusted
        or not _fresh(p.observed_at, r)
        or signals.revision != change.head
        or not signals.provenance_recorded
        or not signals.producer_attested
        or signals.producer is None
        or signals.producer != change.producer
        or signals.producer not in signals.contributors
        or not signals.contributors <= known
        or not signals.approvers <= known
    ):
        return hold(CompletionStatus.PROVENANCE)
    if change.id != r.change or change.head != r.revision:
        return hold(CompletionStatus.STALE)
    if not e.complete or not e.trusted or not _fresh(e.observed_at, r):
        return hold(CompletionStatus.READINESS)
    if (
        not e.reviews
        or any(x.revision != change.head or x.reviewer not in known for x in e.reviews)
        or any(
            x.decision is ReviewDecision.CHANGES_REQUESTED
            or (x.required and x.decision is not ReviewDecision.APPROVED)
            for x in e.reviews
        )
        or signals.approvers
        != frozenset(x.reviewer for x in e.reviews if x.decision is ReviewDecision.APPROVED)
        or not evaluate_review_independence(signals).satisfied
    ):
        return hold(CompletionStatus.REVIEW)
    if any(x.required and x not in e.ci for x in s.current_checks(change.id)):
        # Two incompatible exact-head readings are not authoritative evidence.
        return hold(CompletionStatus.CI)
    for checks, names, status in (
        (e.ci, r.required_ci, CompletionStatus.CI),
        (e.readiness, r.required_readiness, CompletionStatus.READINESS),
    ):
        if (
            {x.id.name for x in checks if x.required} != names
            or any(x.id.revision != change.head for x in checks)
            or any(x.status is not CheckStatus.PASSED for x in checks if x.required)
        ):
            return hold(status)
    work = next(x for x in s.works if x.id == change.work)
    if work.state is not WorkState.OPEN:
        return hold(CompletionStatus.READINESS)
    if not s.boundaries_satisfied(work.id):
        return hold(CompletionStatus.HUMAN)
    works = {x.id: x for x in s.works}
    if any(works[x].state is not WorkState.COMPLETED for x in s.prerequisites(work.id)):
        return hold(CompletionStatus.PREREQUISITE)
    if (
        any(x.work == work.id or x.worker == r.worker or x.id == r.lease_id for x in s.leases)
        or any(
            x.binding.lease.work == work.id
            or (x.binding.lease.worker == r.worker and x.phase is not CompletionPhase.COMPLETED)
            or x.binding.lease.id == r.lease_id
            for x in view.records
        )
        or sum(x.work == work.id for x in s.changes) != 1
    ):
        return hold(CompletionStatus.OWNERSHIP)
    # Require idle participants; unrelated known active workers consume capacity.
    # The full observation still fences ANY newly active worker after planning.
    participants = {r.worker} | signals.contributors | signals.approvers
    if any(c.state(x) is not Liveness.IDLE for x in participants):
        return hold(CompletionStatus.LIVENESS)
    if c.active >= r.policy.wip_limit:
        return hold(CompletionStatus.CAPACITY)
    if r.policy.backlog_limit is not None and c.completion_backlog >= r.policy.backlog_limit:
        return hold(CompletionStatus.BACKPRESSURE)
    lease = Lease(r.lease_id, work.id, r.worker, r.at, r.at + r.lease_ttl)
    return CompletionResult(
        CompletionStatus.SELECTED, CompletionBinding(lease, e, r, _digest(_observation(view)))
    )


@dataclass(frozen=True)
class CompletionEffect:
    command: EffectCommand
    binding: CompletionBinding


class CompletionPort(Protocol):
    def acquire(self) -> CompletionView:
        """Complete trusted graph/demand/evidence/liveness/capacity/records under ONE token.

        Missing, ambiguous or untrusted acquisition raises AcquisitionFailure.
        Records survive lease expiry and include completed tombstones.
        """
        ...

    def claim(self, effect: CompletionEffect) -> None:
        """Full-view CAS; atomically persist exact lease AND CLAIMED completion record.

        Recheck current clock/expiry/freshness, complete provenance/evidence,
        prerequisites, human gates, capacity/backpressure and worker exclusivity.
        Token fences ALL observations, including evidence revocation/liveness.
        Deduplicate commands durably. EffectFailure proves no write; all other
        unknown exceptions retain ownership. No credentials in durable state.
        """
        ...

    def complete(self, effect: CompletionEffect) -> CompletionPhase:
        """Full-view/full-binding CAS plus EXPECTED-HEAD fence at the actual merge.

        Recheck all claim conditions with injected current clock (own slot/lease
        excluded), exact full lease, request/evidence digest and CLAIMED phase.
        Atomically persist ATTEMPTING before crossing the remote boundary; never
        replay ATTEMPTING/UNKNOWN. Durably record COMPLETED, proven REFUSED/FAILED
        or UNKNOWN before acknowledgement; crashes remain ATTEMPTING. Refusal,
        EffectFailure and EffectConflict prove NO merge occurred. Other errors
        are uncertain. Retain ownership and receipts even after TTL expiry.
        Reject changed contributors/checks/reviews/gates/new activity even if head
        matches. A read immediately before merge is NOT an expected-head fence;
        host must enforce expected_revision as part of its merge transaction.
        No worker, issue closure, release publication or implicit post-merge effect.
        """
        ...


def _claimed_view(
    initial: CompletionView, live: CompletionView, binding: CompletionBinding
) -> bool:
    record = CompletionRecord(binding)
    c = live.controller
    if record not in live.records or binding.lease not in c.graph.snapshot.leases or c.active < 1:
        return False
    normalized = replace(
        live,
        records=tuple(x for x in live.records if x != record),
        controller=replace(
            c,
            active=c.active - 1,
            graph=replace(
                c.graph,
                snapshot=replace(
                    c.graph.snapshot,
                    leases=tuple(x for x in c.graph.snapshot.leases if x != binding.lease),
                ),
            ),
        ),
    )
    return _observation(initial) == _observation(normalized)


class CompletionController:
    def __init__(self, port: CompletionPort) -> None:
        self._port = port

    def tick(self, request: CompletionRequest, *, apply: bool = False) -> CompletionResult:
        try:
            initial = self._port.acquire()
            plan = plan_completion(initial, request)
        except Exception:
            return CompletionResult(CompletionStatus.ACQUISITION)
        if not apply or plan.status is not CompletionStatus.SELECTED:
            return plan
        assert plan.binding is not None
        binding = plan.binding

        def effect(kind: EffectKind, live: CompletionView) -> CompletionEffect:
            return CompletionEffect(
                EffectCommand(
                    kind,
                    live.controller.graph.version,
                    f"{request.operation}:{_digest(binding.to_dict())}",
                    binding.lease.work,
                    binding.lease.worker,
                    lease=binding.lease,
                    lease_id=binding.lease.id,
                    change=request.change,
                    expected_revision=request.revision,
                ),
                binding,
            )

        try:
            live = self._port.acquire()
            if _observation(initial) != _observation(live):
                return CompletionResult(CompletionStatus.STALE, binding)
            if plan_completion(live, request) != plan:
                return CompletionResult(CompletionStatus.STALE, binding)
        except Exception:
            return CompletionResult(CompletionStatus.ACQUISITION, binding)
        claim = effect(EffectKind.CLAIM, live)
        keys: tuple[str, ...] = (claim.command.idempotency_key,)
        try:
            self._port.claim(claim)
        except EffectConflict:
            return CompletionResult(CompletionStatus.STALE, binding, keys)
        except EffectFailure:
            return CompletionResult(CompletionStatus.CLAIM_FAILED, binding, keys)
        except Exception:
            return CompletionResult(CompletionStatus.CLAIM_UNCERTAIN, binding, keys)
        try:
            live = self._port.acquire()
            if not _fresh(live.controller.observed_at, request) or not _claimed_view(
                initial, live, binding
            ):
                return CompletionResult(CompletionStatus.STALE, binding, keys)
        except Exception:
            return CompletionResult(CompletionStatus.ACQUISITION, binding, keys)
        completion = effect(EffectKind.COMPLETE, live)
        keys = (*keys, completion.command.idempotency_key)
        try:
            phase = self._port.complete(completion)
        except EffectConflict:
            return CompletionResult(CompletionStatus.STALE, binding, keys)
        except EffectFailure:
            return CompletionResult(CompletionStatus.FAILED, binding, keys)
        except Exception:
            phase = CompletionPhase.UNKNOWN
        if not isinstance(phase, CompletionPhase):
            phase = CompletionPhase.UNKNOWN
        status = {
            CompletionPhase.COMPLETED: CompletionStatus.COMPLETED,
            CompletionPhase.REFUSED: CompletionStatus.REFUSED,
            CompletionPhase.FAILED: CompletionStatus.FAILED,
        }.get(phase, CompletionStatus.UNCERTAIN)
        return CompletionResult(status, binding, keys)
