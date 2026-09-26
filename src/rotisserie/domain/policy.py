"""Pure scheduling and coordination policy over a graph snapshot."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from rotisserie.domain.model import (
    Change,
    ChangeId,
    CheckStatus,
    ReviewDecision,
    Work,
    WorkerId,
    WorkId,
    WorkState,
)
from rotisserie.domain.snapshot import GraphSnapshot


class WorkBlock(StrEnum):
    """A stable reason why fresh implementation must not start."""

    NOT_OPEN = "not_open"
    DEPENDENCY_INCOMPLETE = "dependency_incomplete"
    HUMAN_BOUNDARY = "human_boundary"
    ACTIVE_LEASE = "active_lease"
    IMPLEMENTATION_EXISTS = "implementation_exists"
    CAPACITY = "capacity"
    BACKPRESSURE = "backpressure"


class LeaseAction(StrEnum):
    ACQUIRE = "acquire"
    KEEP = "keep"
    RELEASE = "release"
    DENY = "deny"


class ReadinessBlock(StrEnum):
    UNKNOWN_CHANGE = "unknown_change"
    WORK_INCOMPLETE = "work_incomplete"
    HUMAN_BOUNDARY = "human_boundary"
    CHECKS_PENDING = "checks_pending"
    CHECKS_FAILED = "checks_failed"
    REVIEW_PENDING = "review_pending"
    REVIEW_REJECTED = "review_rejected"
    INDEPENDENT_REVIEW_REQUIRED = "independent_review_required"


@dataclass(frozen=True)
class SchedulingPolicy:
    """Explicit configuration for deterministic scheduling decisions."""

    wip_limit: int
    reserved_review: int = 0
    backlog_limit: int | None = None

    def __post_init__(self) -> None:
        if self.wip_limit < 0:
            raise ValueError("WIP limit cannot be negative")
        if not 0 <= self.reserved_review <= self.wip_limit:
            raise ValueError("reserved review capacity must be within the WIP limit")
        if self.backlog_limit is not None and self.backlog_limit < 0:
            raise ValueError("backlog limit cannot be negative")


@dataclass(frozen=True)
class WorkDecision:
    work: WorkId
    eligible: bool
    blocks: tuple[WorkBlock, ...] = ()


@dataclass(frozen=True)
class LeaseDecision:
    action: LeaseAction
    work: WorkId
    worker: WorkerId
    reason: WorkBlock | None = None


@dataclass(frozen=True)
class CapacityDecision:
    completion: int
    production: int

    @property
    def total(self) -> int:
        return self.completion + self.production


@dataclass(frozen=True)
class ReadinessDecision:
    change: ChangeId
    ready: bool
    blocks: tuple[ReadinessBlock, ...] = ()


def implementation_decision(snapshot: GraphSnapshot, work: WorkId, *, at: int) -> WorkDecision:
    """Decide whether one work node may receive a new implementation lease."""

    work_by_id = {item.id: item for item in snapshot.works}
    item = work_by_id[work]
    blocks: list[WorkBlock] = []
    if item.state is not WorkState.OPEN:
        blocks.append(WorkBlock.NOT_OPEN)
    if any(
        work_by_id[required].state is not WorkState.COMPLETED
        for required in snapshot.prerequisites(work)
    ):
        blocks.append(WorkBlock.DEPENDENCY_INCOMPLETE)
    if not snapshot.boundaries_satisfied(work):
        blocks.append(WorkBlock.HUMAN_BOUNDARY)
    if snapshot.active_lease(work, at) is not None:
        blocks.append(WorkBlock.ACTIVE_LEASE)
    if any(change.work == work for change in snapshot.changes):
        blocks.append(WorkBlock.IMPLEMENTATION_EXISTS)
    return WorkDecision(work, not blocks, tuple(blocks))


def ranked_implementation_work(snapshot: GraphSnapshot, *, at: int) -> tuple[Work, ...]:
    """Return eligible work in stable priority-descending identity order."""

    eligible = [
        item
        for item in snapshot.works
        if implementation_decision(snapshot, item.id, at=at).eligible
    ]
    return tuple(sorted(eligible, key=lambda item: (-item.priority, item.id)))


def lease_decision(
    snapshot: GraphSnapshot,
    work: WorkId,
    worker: WorkerId,
    *,
    at: int,
    release: bool = False,
) -> LeaseDecision:
    """Acquire, retain, release, or deny a lease without performing effects."""

    current = snapshot.active_lease(work, at)
    if release:
        return LeaseDecision(
            LeaseAction.RELEASE
            if current is not None and current.worker == worker
            else LeaseAction.DENY,
            work,
            worker,
            None if current is not None and current.worker == worker else WorkBlock.ACTIVE_LEASE,
        )
    if current is not None:
        if current.worker == worker:
            invalid = _lease_revalidation_block(snapshot, work)
            return LeaseDecision(
                LeaseAction.RELEASE if invalid is not None else LeaseAction.KEEP,
                work,
                worker,
                invalid,
            )
        return LeaseDecision(LeaseAction.DENY, work, worker, WorkBlock.ACTIVE_LEASE)
    decision = implementation_decision(snapshot, work, at=at)
    reason = decision.blocks[0] if decision.blocks else None
    return LeaseDecision(
        LeaseAction.ACQUIRE if decision.eligible else LeaseAction.DENY, work, worker, reason
    )


def _lease_revalidation_block(snapshot: GraphSnapshot, work: WorkId) -> WorkBlock | None:
    """Return the first condition that invalidates an already-owned lease."""

    work_by_id = {item.id: item for item in snapshot.works}
    if work_by_id[work].state is not WorkState.OPEN:
        return WorkBlock.NOT_OPEN
    if any(
        work_by_id[required].state is not WorkState.COMPLETED
        for required in snapshot.prerequisites(work)
    ):
        return WorkBlock.DEPENDENCY_INCOMPLETE
    if not snapshot.boundaries_satisfied(work):
        return WorkBlock.HUMAN_BOUNDARY
    return None


def allocate_capacity(
    *,
    completion_demand: int,
    production_demand: int,
    active: int,
    policy: SchedulingPolicy,
) -> CapacityDecision:
    """Allocate remaining WIP, reserving completion capacity under pressure."""

    if min(completion_demand, production_demand, active) < 0:
        raise ValueError("demand and active counts cannot be negative")
    available = max(0, policy.wip_limit - active)
    if available == 0:
        return CapacityDecision(0, 0)
    completion = min(completion_demand, available, policy.reserved_review)
    remaining = available - completion
    if completion_demand > completion and remaining:
        total_remaining_demand = completion_demand - completion + production_demand
        proportional = math.ceil(
            remaining * (completion_demand - completion) / total_remaining_demand
        )
        completion += min(completion_demand - completion, proportional)
        remaining = available - completion
    production = min(production_demand, remaining)
    return CapacityDecision(completion, production)


def apply_intake_pressure(
    decisions: tuple[WorkDecision, ...],
    *,
    completion_backlog: int,
    policy: SchedulingPolicy,
) -> tuple[WorkDecision, ...]:
    """Fail closed on fresh intake when the configured completion queue is full."""

    if completion_backlog < 0:
        raise ValueError("completion backlog cannot be negative")
    if policy.backlog_limit is None or completion_backlog < policy.backlog_limit:
        return decisions
    return tuple(
        decision
        if not decision.eligible
        else WorkDecision(decision.work, False, (WorkBlock.BACKPRESSURE,))
        for decision in decisions
    )


def reviewer_is_independent(change: Change, reviewer: WorkerId) -> bool:
    """Require a known producer and a different reviewer identity."""

    return change.producer is not None and change.producer != reviewer


def repair_worker(change: Change) -> WorkerId | None:
    """Route requested repairs back to the original producer."""

    return change.producer


def readiness_decision(snapshot: GraphSnapshot, change: ChangeId) -> ReadinessDecision:
    """Evaluate required checks and independent review for the exact current head."""

    change_by_id = {item.id: item for item in snapshot.changes}
    item = change_by_id.get(change)
    if item is None:
        return ReadinessDecision(change, False, (ReadinessBlock.UNKNOWN_CHANGE,))
    work = next(work for work in snapshot.works if work.id == item.work)
    blocks: list[ReadinessBlock] = []
    if work.state is not WorkState.OPEN:
        blocks.append(ReadinessBlock.WORK_INCOMPLETE)
    if not snapshot.boundaries_satisfied(work.id):
        blocks.append(ReadinessBlock.HUMAN_BOUNDARY)

    required_checks = tuple(check for check in snapshot.current_checks(change) if check.required)
    if any(check.status is CheckStatus.FAILED for check in required_checks):
        blocks.append(ReadinessBlock.CHECKS_FAILED)
    elif not required_checks or any(
        check.status is CheckStatus.PENDING for check in required_checks
    ):
        blocks.append(ReadinessBlock.CHECKS_PENDING)

    required_reviews = tuple(
        review for review in snapshot.current_reviews(change) if review.required
    )
    if any(review.decision is ReviewDecision.CHANGES_REQUESTED for review in required_reviews):
        blocks.append(ReadinessBlock.REVIEW_REJECTED)
    elif not required_reviews or any(
        review.decision is ReviewDecision.PENDING for review in required_reviews
    ):
        blocks.append(ReadinessBlock.REVIEW_PENDING)
    if not any(
        review.decision is ReviewDecision.APPROVED
        and reviewer_is_independent(item, review.reviewer)
        for review in required_reviews
    ):
        blocks.append(ReadinessBlock.INDEPENDENT_REVIEW_REQUIRED)
    return ReadinessDecision(change, not blocks, tuple(blocks))
