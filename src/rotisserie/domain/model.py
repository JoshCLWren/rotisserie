"""Typed values for Rotisserie's provider-neutral work graph."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


def _required(value: str, field: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string")


@dataclass(frozen=True, order=True)
class RepositoryId:
    host: str
    owner: str
    name: str

    def __post_init__(self) -> None:
        for value, field in ((self.host, "host"), (self.owner, "owner"), (self.name, "name")):
            _required(value, field)


@dataclass(frozen=True, order=True)
class WorkId:
    repository: RepositoryId
    key: str

    def __post_init__(self) -> None:
        _required(self.key, "work key")


@dataclass(frozen=True, order=True)
class ChangeId:
    repository: RepositoryId
    key: str

    def __post_init__(self) -> None:
        _required(self.key, "change key")


@dataclass(frozen=True, order=True)
class RevisionId:
    repository: RepositoryId
    value: str

    def __post_init__(self) -> None:
        _required(self.value, "revision value")


@dataclass(frozen=True, order=True)
class WorkerId:
    namespace: str
    value: str

    def __post_init__(self) -> None:
        _required(self.namespace, "worker namespace")
        _required(self.value, "worker value")


@dataclass(frozen=True, order=True)
class LeaseId:
    value: str

    def __post_init__(self) -> None:
        _required(self.value, "lease id")


@dataclass(frozen=True, order=True)
class CheckId:
    revision: RevisionId
    name: str

    def __post_init__(self) -> None:
        _required(self.name, "check name")


@dataclass(frozen=True, order=True)
class ReviewId:
    value: str

    def __post_init__(self) -> None:
        _required(self.value, "review id")


@dataclass(frozen=True, order=True)
class EvidenceId:
    value: str

    def __post_init__(self) -> None:
        _required(self.value, "evidence id")


@dataclass(frozen=True, order=True)
class BoundaryId:
    value: str

    def __post_init__(self) -> None:
        _required(self.value, "boundary id")


class WorkState(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CheckStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ReviewDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class EvidenceKind(StrEnum):
    CHECK = "check"
    REVIEW = "review"
    READINESS = "readiness"
    COMPLETION = "completion"


class BoundaryKind(StrEnum):
    HUMAN_APPROVAL = "human_approval"
    HUMAN_ACTION = "human_action"


@dataclass(frozen=True)
class Work:
    id: WorkId
    title: str
    state: WorkState = WorkState.OPEN
    priority: int = 0

    def __post_init__(self) -> None:
        _required(self.title, "work title")


@dataclass(frozen=True)
class Change:
    id: ChangeId
    work: WorkId
    head: RevisionId
    producer: WorkerId | None = None

    def __post_init__(self) -> None:
        if self.id.repository != self.work.repository or self.id.repository != self.head.repository:
            raise ValueError("change, work, and head must belong to the same repository")


@dataclass(frozen=True)
class Revision:
    id: RevisionId


@dataclass(frozen=True)
class Worker:
    id: WorkerId
    human: bool = False


@dataclass(frozen=True)
class Lease:
    id: LeaseId
    work: WorkId
    worker: WorkerId
    acquired_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expiry must be after acquisition")

    def is_active(self, at: int) -> bool:
        return self.acquired_at <= at < self.expires_at


@dataclass(frozen=True)
class Check:
    id: CheckId
    status: CheckStatus
    required: bool = True


@dataclass(frozen=True)
class Review:
    id: ReviewId
    revision: RevisionId
    reviewer: WorkerId
    decision: ReviewDecision
    required: bool = True


@dataclass(frozen=True)
class Evidence:
    id: EvidenceId
    revision: RevisionId
    kind: EvidenceKind
    source: str

    def __post_init__(self) -> None:
        _required(self.source, "evidence source")


@dataclass(frozen=True)
class Capacity:
    worker: WorkerId
    limit: int
    reserved_review: int = 0

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("capacity limit cannot be negative")
        if not 0 <= self.reserved_review <= self.limit:
            raise ValueError("reserved review capacity must be within the limit")


@dataclass(frozen=True)
class Boundary:
    id: BoundaryId
    work: WorkId
    kind: BoundaryKind
    satisfied_by: WorkerId | None = None

    def is_satisfied(self, workers: dict[WorkerId, Worker]) -> bool:
        if self.satisfied_by is None:
            return False
        worker = workers.get(self.satisfied_by)
        return worker is not None and worker.human
