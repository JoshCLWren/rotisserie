"""Versioned comparison records for adopter decision shadowing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rotisserie.domain import Change, GraphSnapshot, Review, ReviewDecision, WorkerId
from rotisserie.domain.policy import (
    SchedulingPolicy,
    apply_intake_pressure,
    implementation_decision,
    readiness_decision,
    reviewer_is_independent,
)


def _required(value: str, field: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string")


class DecisionDimension(StrEnum):
    """Decision families required during an adopter parity run."""

    ELIGIBILITY = "eligibility"
    RANKING = "ranking"
    OWNERSHIP = "ownership"
    REVIEW = "review"
    COMPLETION = "completion"
    RECOVERY = "recovery"


class DivergenceKind(StrEnum):
    """Stable explanations for differences between two decision sets."""

    MISSING_BASELINE = "missing_baseline"
    MISSING_CANDIDATE = "missing_candidate"
    OUTCOME = "outcome"
    REASONS = "reasons"
    RANK = "rank"


@dataclass(frozen=True)
class DecisionObservation:
    """One normalized host decision, independent of its source representation."""

    dimension: DecisionDimension
    subject: str
    outcome: str
    reasons: tuple[str, ...] = ()
    rank: int | None = None

    def __post_init__(self) -> None:
        _required(self.subject, "decision subject")
        _required(self.outcome, "decision outcome")
        for reason in self.reasons:
            _required(reason, "decision reason")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("decision reasons must be unique")
        if self.rank is not None and self.rank < 0:
            raise ValueError("decision rank cannot be negative")
        if self.dimension is DecisionDimension.RANKING and self.rank is None:
            raise ValueError("ranking decisions require a rank")
        if self.dimension is not DecisionDimension.RANKING and self.rank is not None:
            raise ValueError("only ranking decisions may carry a rank")

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "subject": self.subject,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "rank": self.rank,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DecisionObservation:
        reasons = data.get("reasons", [])
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            raise ValueError("decision reasons must be a list of strings")
        rank = data.get("rank")
        if rank is not None and (not isinstance(rank, int) or isinstance(rank, bool)):
            raise ValueError("decision rank must be an integer or null")
        raw_dimension = data.get("dimension")
        subject = data.get("subject")
        outcome = data.get("outcome")
        if not isinstance(raw_dimension, str):
            raise ValueError("decision dimension must be a string")
        try:
            dimension = DecisionDimension(raw_dimension)
        except ValueError as error:
            raise ValueError("invalid decision observation") from error
        if not isinstance(subject, str) or not isinstance(outcome, str):
            raise ValueError("decision subject and outcome must be strings")
        return cls(dimension, subject, outcome, tuple(reasons), rank)


@dataclass(frozen=True)
class DecisionSnapshot:
    """A complete normalized decision set for one exact source revision."""

    source: str
    revision: str
    observations: tuple[DecisionObservation, ...]
    dimensions: frozenset[DecisionDimension] | None = None

    def __post_init__(self) -> None:
        _required(self.source, "decision source")
        _required(self.revision, "decision revision")
        keys = [(item.dimension, item.subject) for item in self.observations]
        if len(set(keys)) != len(keys):
            raise ValueError("decision dimension and subject pairs must be unique")
        observed = frozenset(item.dimension for item in self.observations)
        if self.dimensions is None:
            object.__setattr__(self, "dimensions", observed)
        elif not observed.issubset(self.dimensions):
            raise ValueError("observations must belong to declared decision dimensions")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": self.source,
            "revision": self.revision,
            "dimensions": sorted(self.dimensions or ()),
            "observations": [item.to_dict() for item in self.observations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DecisionSnapshot:
        if data.get("schema_version") != 1:
            raise ValueError("unsupported decision snapshot schema version")
        source = data.get("source")
        revision = data.get("revision")
        observations = data.get("observations")
        dimensions = data.get("dimensions")
        if not isinstance(source, str) or not isinstance(revision, str):
            raise ValueError("decision source and revision must be strings")
        if not isinstance(observations, list) or not all(
            isinstance(item, dict) for item in observations
        ):
            raise ValueError("decision observations must be a list of objects")
        if dimensions is not None and (
            not isinstance(dimensions, list)
            or not all(isinstance(item, str) for item in dimensions)
        ):
            raise ValueError("decision dimensions must be a list of strings")
        try:
            parsed_dimensions = (
                frozenset(DecisionDimension(item) for item in dimensions)
                if dimensions is not None
                else None
            )
        except ValueError as error:
            raise ValueError("invalid decision snapshot dimension") from error
        return cls(
            source,
            revision,
            tuple(DecisionObservation.from_dict(item) for item in observations),
            parsed_dimensions,
        )


def project_decisions(
    snapshot: GraphSnapshot,
    *,
    source: str,
    revision: str,
    at: int,
    completion_backlog: int = 0,
    backlog_limit: int | None = None,
) -> DecisionSnapshot:
    """Project portable policy outcomes for one exact adopter graph revision."""

    observations: list[DecisionObservation] = []
    work_decisions = tuple(
        implementation_decision(snapshot, work.id, at=at) for work in snapshot.works
    )
    if backlog_limit is not None:
        work_decisions = apply_intake_pressure(
            work_decisions,
            completion_backlog=completion_backlog,
            policy=SchedulingPolicy(wip_limit=0, backlog_limit=backlog_limit),
        )
    decisions = {item.work: item for item in work_decisions}
    ranked = tuple(
        sorted(
            (work for work in snapshot.works if decisions[work.id].eligible),
            key=lambda item: (-item.priority, item.id),
        )
    )
    ranks = {item.id: rank for rank, item in enumerate(ranked)}
    for work in sorted(snapshot.works, key=lambda item: item.id):
        subject = f"work:{work.id.key}"
        eligibility = decisions[work.id]
        observations.append(
            DecisionObservation(
                DecisionDimension.ELIGIBILITY,
                subject,
                "eligible" if eligibility.eligible else "blocked",
                tuple(str(block) for block in eligibility.blocks),
            )
        )
        if work.id in ranks:
            observations.append(
                DecisionObservation(
                    DecisionDimension.RANKING, subject, "ranked", rank=ranks[work.id]
                )
            )
        lease = snapshot.active_lease(work.id, at)
        observations.append(
            DecisionObservation(
                DecisionDimension.OWNERSHIP,
                subject,
                _worker_key(lease.worker) if lease else "unowned",
            )
        )

    for change in sorted(snapshot.changes, key=lambda item: item.id):
        subject = f"change:{change.id.key}"
        reviews = tuple(item for item in snapshot.current_reviews(change.id) if item.required)
        review_outcome, review_reasons = _review_decision(change, reviews)
        observations.append(
            DecisionObservation(DecisionDimension.REVIEW, subject, review_outcome, review_reasons)
        )
        readiness = readiness_decision(snapshot, change.id)
        observations.append(
            DecisionObservation(
                DecisionDimension.COMPLETION,
                subject,
                "ready" if readiness.ready else "blocked",
                tuple(str(block) for block in readiness.blocks),
            )
        )

    for lease in sorted(snapshot.leases, key=lambda item: item.id):
        expired = lease.expires_at <= at
        observations.append(
            DecisionObservation(
                DecisionDimension.RECOVERY,
                f"lease:{lease.id.value}",
                "release" if expired else "retain",
                ("expired",) if expired else (),
            )
        )
    return DecisionSnapshot(source, revision, tuple(observations), frozenset(DecisionDimension))


def _worker_key(worker: WorkerId) -> str:
    return f"{worker.namespace}:{worker.value}"


def _review_decision(change: Change, reviews: tuple[Review, ...]) -> tuple[str, tuple[str, ...]]:
    if any(item.decision is ReviewDecision.CHANGES_REQUESTED for item in reviews):
        return "changes_requested", ()
    if not reviews or any(item.decision is ReviewDecision.PENDING for item in reviews):
        return "pending", ()
    if any(
        item.decision is ReviewDecision.APPROVED and reviewer_is_independent(change, item.reviewer)
        for item in reviews
    ):
        return "approved", ()
    return "blocked", ("independent_review_required",)


@dataclass(frozen=True)
class DecisionDivergence:
    dimension: DecisionDimension
    subject: str
    kind: DivergenceKind
    baseline: object
    candidate: object

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "subject": self.subject,
            "kind": self.kind,
            "baseline": self.baseline,
            "candidate": self.candidate,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DecisionDivergence:
        raw_dimension = data.get("dimension")
        subject = data.get("subject")
        raw_kind = data.get("kind")
        if not isinstance(raw_dimension, str) or not isinstance(subject, str):
            raise ValueError("divergence dimension and subject must be strings")
        if not isinstance(raw_kind, str):
            raise ValueError("divergence kind must be a string")
        try:
            dimension = DecisionDimension(raw_dimension)
            kind = DivergenceKind(raw_kind)
        except ValueError as error:
            raise ValueError("invalid decision divergence") from error
        _required(subject, "divergence subject")
        return cls(dimension, subject, kind, data.get("baseline"), data.get("candidate"))


@dataclass(frozen=True)
class ShadowReport:
    """Machine-readable parity result with an entry for every difference."""

    baseline_source: str
    baseline_revision: str
    candidate_source: str
    candidate_revision: str
    compared: int
    dimensions: tuple[DecisionDimension, ...]
    divergences: tuple[DecisionDivergence, ...]

    @property
    def matches(self) -> bool:
        return not self.divergences

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "baseline": {
                "source": self.baseline_source,
                "revision": self.baseline_revision,
            },
            "candidate": {
                "source": self.candidate_source,
                "revision": self.candidate_revision,
            },
            "compared": self.compared,
            "dimensions": list(self.dimensions),
            "matches": self.matches,
            "divergences": [item.to_dict() for item in self.divergences],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ShadowReport:
        if data.get("schema_version") != 1:
            raise ValueError("unsupported shadow report schema version")
        baseline = data.get("baseline")
        candidate = data.get("candidate")
        compared = data.get("compared")
        dimensions = data.get("dimensions")
        divergences = data.get("divergences")
        if not isinstance(baseline, dict) or not isinstance(candidate, dict):
            raise ValueError("shadow report sources must be objects")
        baseline_source = baseline.get("source")
        baseline_revision = baseline.get("revision")
        candidate_source = candidate.get("source")
        candidate_revision = candidate.get("revision")
        if (
            not isinstance(baseline_source, str)
            or not isinstance(baseline_revision, str)
            or not isinstance(candidate_source, str)
            or not isinstance(candidate_revision, str)
        ):
            raise ValueError("shadow report source names and revisions must be strings")
        if baseline_revision != candidate_revision:
            raise ValueError("shadow report must describe one graph revision")
        if not isinstance(compared, int) or isinstance(compared, bool) or compared < 0:
            raise ValueError("shadow report compared count must be a non-negative integer")
        if not isinstance(dimensions, list) or not all(
            isinstance(item, str) for item in dimensions
        ):
            raise ValueError("shadow report dimensions must be a list of strings")
        if not isinstance(divergences, list) or not all(
            isinstance(item, dict) for item in divergences
        ):
            raise ValueError("shadow report divergences must be a list of objects")
        try:
            parsed_dimensions = tuple(DecisionDimension(item) for item in dimensions)
        except ValueError as error:
            raise ValueError("invalid shadow report dimension") from error
        if len(set(parsed_dimensions)) != len(parsed_dimensions):
            raise ValueError("shadow report dimensions must be unique")
        parsed_divergences = tuple(DecisionDivergence.from_dict(item) for item in divergences)
        report = cls(
            baseline_source,
            baseline_revision,
            candidate_source,
            candidate_revision,
            compared,
            parsed_dimensions,
            parsed_divergences,
        )
        if data.get("matches") is not report.matches:
            raise ValueError("shadow report matches flag is inconsistent with divergences")
        return report


def compare_decisions(baseline: DecisionSnapshot, candidate: DecisionSnapshot) -> ShadowReport:
    """Compare normalized host and Rotisserie decisions deterministically."""

    if baseline.revision != candidate.revision:
        raise ValueError("decision snapshots must describe the same graph revision")
    baseline_by_key = {(item.dimension, item.subject): item for item in baseline.observations}
    candidate_by_key = {(item.dimension, item.subject): item for item in candidate.observations}
    keys = sorted(set(baseline_by_key) | set(candidate_by_key))
    divergences: list[DecisionDivergence] = []
    for dimension, subject in keys:
        before = baseline_by_key.get((dimension, subject))
        after = candidate_by_key.get((dimension, subject))
        if before is None:
            divergences.append(
                DecisionDivergence(
                    dimension,
                    subject,
                    DivergenceKind.MISSING_BASELINE,
                    None,
                    after.to_dict() if after else None,
                )
            )
            continue
        if after is None:
            divergences.append(
                DecisionDivergence(
                    dimension,
                    subject,
                    DivergenceKind.MISSING_CANDIDATE,
                    before.to_dict(),
                    None,
                )
            )
            continue
        if before.outcome != after.outcome:
            divergences.append(
                DecisionDivergence(
                    dimension, subject, DivergenceKind.OUTCOME, before.outcome, after.outcome
                )
            )
        if before.reasons != after.reasons:
            divergences.append(
                DecisionDivergence(
                    dimension,
                    subject,
                    DivergenceKind.REASONS,
                    list(before.reasons),
                    list(after.reasons),
                )
            )
        if before.rank != after.rank:
            divergences.append(
                DecisionDivergence(dimension, subject, DivergenceKind.RANK, before.rank, after.rank)
            )
    return ShadowReport(
        baseline.source,
        baseline.revision,
        candidate.source,
        candidate.revision,
        len(keys),
        tuple(sorted((baseline.dimensions or frozenset()) & (candidate.dimensions or frozenset()))),
        tuple(divergences),
    )
