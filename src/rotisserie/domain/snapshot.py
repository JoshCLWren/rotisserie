"""Immutable, validated snapshots of the Rotisserie work graph."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Self

from rotisserie.domain.model import (
    Boundary,
    BoundaryId,
    BoundaryKind,
    Capacity,
    Change,
    ChangeId,
    Check,
    CheckId,
    CheckStatus,
    Evidence,
    EvidenceId,
    EvidenceKind,
    Lease,
    LeaseId,
    RepositoryId,
    Review,
    ReviewDecision,
    ReviewId,
    Revision,
    RevisionId,
    Work,
    Worker,
    WorkerId,
    WorkId,
    WorkState,
)


class InvalidGraph(ValueError):
    """Raised when a snapshot violates structural graph invariants."""


@dataclass(frozen=True)
class GraphSnapshot:
    """One effect-free view of coordination truth."""

    works: tuple[Work, ...] = ()
    changes: tuple[Change, ...] = ()
    revisions: tuple[Revision, ...] = ()
    workers: tuple[Worker, ...] = ()
    leases: tuple[Lease, ...] = ()
    checks: tuple[Check, ...] = ()
    reviews: tuple[Review, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    capacities: tuple[Capacity, ...] = ()
    boundaries: tuple[Boundary, ...] = ()
    dependencies: frozenset[tuple[WorkId, WorkId]] = frozenset()

    def __post_init__(self) -> None:
        self._validate()

    def prerequisites(self, work: WorkId) -> frozenset[WorkId]:
        return frozenset(required for dependent, required in self.dependencies if dependent == work)

    def active_lease(self, work: WorkId, at: int) -> Lease | None:
        active = [lease for lease in self.leases if lease.work == work and lease.is_active(at)]
        if len(active) > 1:
            raise InvalidGraph(f"work {work!r} has more than one active lease")
        return active[0] if active else None

    def boundaries_satisfied(self, work: WorkId) -> bool:
        workers = {worker.id: worker for worker in self.workers}
        return all(b.is_satisfied(workers) for b in self.boundaries if b.work == work)

    def current_evidence(self, change: ChangeId) -> tuple[Evidence, ...]:
        head = self._change_map()[change].head
        return tuple(item for item in self.evidence if item.revision == head)

    def current_checks(self, change: ChangeId) -> tuple[Check, ...]:
        head = self._change_map()[change].head
        return tuple(item for item in self.checks if item.id.revision == head)

    def current_reviews(self, change: ChangeId) -> tuple[Review, ...]:
        head = self._change_map()[change].head
        return tuple(item for item in self.reviews if item.revision == head)

    def with_change_head(self, change: ChangeId, head: RevisionId) -> Self:
        if head not in {revision.id for revision in self.revisions}:
            raise InvalidGraph(f"unknown revision {head!r}")
        updated = tuple(
            replace(item, head=head) if item.id == change else item for item in self.changes
        )
        if updated == self.changes:
            raise InvalidGraph(f"unknown change {change!r}")
        return replace(self, changes=updated)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-compatible durable representation."""

        def repo(value: RepositoryId) -> dict[str, str]:
            return {"host": value.host, "owner": value.owner, "name": value.name}

        def work_id(value: WorkId) -> dict[str, Any]:
            return {"repository": repo(value.repository), "key": value.key}

        def change_id(value: ChangeId) -> dict[str, Any]:
            return {"repository": repo(value.repository), "key": value.key}

        def revision_id(value: RevisionId) -> dict[str, Any]:
            return {"repository": repo(value.repository), "value": value.value}

        def worker_id(value: WorkerId) -> dict[str, str]:
            return {"namespace": value.namespace, "value": value.value}

        edges = sorted(
            self.dependencies, key=lambda e: (e[0].repository, e[0].key, e[1].repository, e[1].key)
        )
        return {
            "schema_version": 1,
            "works": [
                {"id": work_id(x.id), "title": x.title, "state": x.state, "priority": x.priority}
                for x in self.works
            ],
            "changes": [
                {
                    "id": change_id(x.id),
                    "work": work_id(x.work),
                    "head": revision_id(x.head),
                    "producer": worker_id(x.producer) if x.producer else None,
                }
                for x in self.changes
            ],
            "revisions": [{"id": revision_id(x.id)} for x in self.revisions],
            "workers": [{"id": worker_id(x.id), "human": x.human} for x in self.workers],
            "leases": [
                {
                    "id": x.id.value,
                    "work": work_id(x.work),
                    "worker": worker_id(x.worker),
                    "acquired_at": x.acquired_at,
                    "expires_at": x.expires_at,
                }
                for x in self.leases
            ],
            "checks": [
                {
                    "revision": revision_id(x.id.revision),
                    "name": x.id.name,
                    "status": x.status,
                    "required": x.required,
                }
                for x in self.checks
            ],
            "reviews": [
                {
                    "id": x.id.value,
                    "revision": revision_id(x.revision),
                    "reviewer": worker_id(x.reviewer),
                    "decision": x.decision,
                    "required": x.required,
                }
                for x in self.reviews
            ],
            "evidence": [
                {
                    "id": x.id.value,
                    "revision": revision_id(x.revision),
                    "kind": x.kind,
                    "source": x.source,
                }
                for x in self.evidence
            ],
            "capacities": [
                {
                    "worker": worker_id(x.worker),
                    "limit": x.limit,
                    "reserved_review": x.reserved_review,
                }
                for x in self.capacities
            ],
            "boundaries": [
                {
                    "id": x.id.value,
                    "work": work_id(x.work),
                    "kind": x.kind,
                    "satisfied_by": worker_id(x.satisfied_by) if x.satisfied_by else None,
                }
                for x in self.boundaries
            ],
            "dependencies": [
                {"dependent": work_id(a), "prerequisite": work_id(b)} for a, b in edges
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        if raw.get("schema_version") != 1:
            raise InvalidGraph("unsupported snapshot schema version")

        def repo(x: dict[str, Any]) -> RepositoryId:
            return RepositoryId(x["host"], x["owner"], x["name"])

        def wid(x: dict[str, Any]) -> WorkId:
            return WorkId(repo(x["repository"]), x["key"])

        def cid(x: dict[str, Any]) -> ChangeId:
            return ChangeId(repo(x["repository"]), x["key"])

        def rid(x: dict[str, Any]) -> RevisionId:
            return RevisionId(repo(x["repository"]), x["value"])

        def worker(x: dict[str, Any]) -> WorkerId:
            return WorkerId(x["namespace"], x["value"])

        return cls(
            works=tuple(
                Work(wid(x["id"]), x["title"], WorkState(x["state"]), x["priority"])
                for x in raw["works"]
            ),
            changes=tuple(
                Change(
                    cid(x["id"]),
                    wid(x["work"]),
                    rid(x["head"]),
                    worker(x["producer"]) if x["producer"] else None,
                )
                for x in raw["changes"]
            ),
            revisions=tuple(Revision(rid(x["id"])) for x in raw["revisions"]),
            workers=tuple(Worker(worker(x["id"]), x["human"]) for x in raw["workers"]),
            leases=tuple(
                Lease(
                    LeaseId(x["id"]),
                    wid(x["work"]),
                    worker(x["worker"]),
                    x["acquired_at"],
                    x["expires_at"],
                )
                for x in raw["leases"]
            ),
            checks=tuple(
                Check(
                    CheckId(rid(x["revision"]), x["name"]), CheckStatus(x["status"]), x["required"]
                )
                for x in raw["checks"]
            ),
            reviews=tuple(
                Review(
                    ReviewId(x["id"]),
                    rid(x["revision"]),
                    worker(x["reviewer"]),
                    ReviewDecision(x["decision"]),
                    x["required"],
                )
                for x in raw["reviews"]
            ),
            evidence=tuple(
                Evidence(
                    EvidenceId(x["id"]), rid(x["revision"]), EvidenceKind(x["kind"]), x["source"]
                )
                for x in raw["evidence"]
            ),
            capacities=tuple(
                Capacity(worker(x["worker"]), x["limit"], x["reserved_review"])
                for x in raw["capacities"]
            ),
            boundaries=tuple(
                Boundary(
                    BoundaryId(x["id"]),
                    wid(x["work"]),
                    BoundaryKind(x["kind"]),
                    worker(x["satisfied_by"]) if x["satisfied_by"] else None,
                )
                for x in raw["boundaries"]
            ),
            dependencies=frozenset(
                (wid(x["dependent"]), wid(x["prerequisite"])) for x in raw["dependencies"]
            ),
        )

    def _change_map(self) -> dict[ChangeId, Change]:
        return {change.id: change for change in self.changes}

    def _validate(self) -> None:
        identified: tuple[tuple[str, tuple[Any, ...]], ...] = (
            ("work", tuple(x.id for x in self.works)),
            ("change", tuple(x.id for x in self.changes)),
            ("revision", tuple(x.id for x in self.revisions)),
            ("worker", tuple(x.id for x in self.workers)),
            ("lease", tuple(x.id for x in self.leases)),
            ("check", tuple(x.id for x in self.checks)),
            ("review", tuple(x.id for x in self.reviews)),
            ("evidence", tuple(x.id for x in self.evidence)),
            ("boundary", tuple(x.id for x in self.boundaries)),
        )
        for kind, identities in identified:
            if len(identities) != len(set(identities)):
                raise InvalidGraph(f"duplicate {kind} identity")
        works = {x.id for x in self.works}
        revisions = {x.id for x in self.revisions}
        workers = {x.id for x in self.workers}
        for dependent, prerequisite in self.dependencies:
            if dependent not in works or prerequisite not in works:
                raise InvalidGraph("dependency references unknown work")
            if dependent == prerequisite:
                raise InvalidGraph("work cannot depend on itself")
        self._validate_acyclic(works)
        for change in self.changes:
            if (
                change.work not in works
                or change.head not in revisions
                or (change.producer is not None and change.producer not in workers)
            ):
                raise InvalidGraph("change references an unknown identity")
        for lease in self.leases:
            if lease.work not in works or lease.worker not in workers:
                raise InvalidGraph("lease references an unknown identity")
        for check in self.checks:
            if check.id.revision not in revisions:
                raise InvalidGraph("check references unknown revision")
        for review in self.reviews:
            if review.revision not in revisions or review.reviewer not in workers:
                raise InvalidGraph("review references an unknown identity")
        for item in self.evidence:
            if item.revision not in revisions:
                raise InvalidGraph("evidence references unknown revision")
        for capacity in self.capacities:
            if capacity.worker not in workers:
                raise InvalidGraph("capacity references unknown worker")
        for boundary in self.boundaries:
            if boundary.work not in works or (
                boundary.satisfied_by is not None and boundary.satisfied_by not in workers
            ):
                raise InvalidGraph("boundary references an unknown identity")

    def _validate_acyclic(self, works: set[WorkId]) -> None:
        visiting: set[WorkId] = set()
        visited: set[WorkId] = set()

        def visit(work: WorkId) -> None:
            if work in visiting:
                raise InvalidGraph("dependency cycle detected")
            if work in visited:
                return
            visiting.add(work)
            for prerequisite in self.prerequisites(work):
                visit(prerequisite)
            visiting.remove(work)
            visited.add(work)

        for work in works:
            visit(work)
