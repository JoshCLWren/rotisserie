"""Versioned comparison records for adopter decision shadowing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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

    def __post_init__(self) -> None:
        _required(self.source, "decision source")
        _required(self.revision, "decision revision")
        keys = [(item.dimension, item.subject) for item in self.observations]
        if len(set(keys)) != len(keys):
            raise ValueError("decision dimension and subject pairs must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": self.source,
            "revision": self.revision,
            "observations": [item.to_dict() for item in self.observations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DecisionSnapshot:
        if data.get("schema_version") != 1:
            raise ValueError("unsupported decision snapshot schema version")
        source = data.get("source")
        revision = data.get("revision")
        observations = data.get("observations")
        if not isinstance(source, str) or not isinstance(revision, str):
            raise ValueError("decision source and revision must be strings")
        if not isinstance(observations, list) or not all(
            isinstance(item, dict) for item in observations
        ):
            raise ValueError("decision observations must be a list of objects")
        return cls(
            source,
            revision,
            tuple(DecisionObservation.from_dict(item) for item in observations),
        )


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


@dataclass(frozen=True)
class ShadowReport:
    """Machine-readable parity result with an entry for every difference."""

    baseline_source: str
    baseline_revision: str
    candidate_source: str
    candidate_revision: str
    compared: int
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
            "matches": self.matches,
            "divergences": [item.to_dict() for item in self.divergences],
        }


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
        tuple(divergences),
    )
