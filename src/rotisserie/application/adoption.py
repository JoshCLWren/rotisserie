"""Fail-closed policy for staged external-adopter cutover and rollback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from rotisserie.application.shadow import DecisionDimension, ShadowReport


class AdoptionAction(StrEnum):
    """The next mutation-boundary action authorized by adoption evidence."""

    HOLD = "hold"
    ENTER_CANARY = "enter_canary"
    EXPAND = "expand"
    ROLLBACK = "rollback"


class AdoptionStage(StrEnum):
    """The adopter-owned stage before and after one policy decision."""

    LEGACY = "legacy"
    CANARY = "canary"
    EXPANDED = "expanded"


class AdoptionEvidenceKind(StrEnum):
    """Successful adopter-owned observations used by transition policy."""

    ROLLBACK_DRILL = "rollback_drill"
    CANARY_OBSERVATION = "canary_observation"


@dataclass(frozen=True)
class AdoptionLane:
    """An adopter-owned, bounded set of subjects eligible for a canary."""

    name: str
    subjects: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError("adoption lane name must be a non-empty, trimmed string")
        if not self.subjects:
            raise ValueError("adoption lane must contain at least one subject")
        if any(not item or item != item.strip() for item in self.subjects):
            raise ValueError("adoption lane subjects must be non-empty, trimmed strings")
        if len(set(self.subjects)) != len(self.subjects):
            raise ValueError("adoption lane subjects must be unique")

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "subjects": list(self.subjects)}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AdoptionLane:
        name = data.get("name")
        subjects = data.get("subjects")
        if (
            not isinstance(name, str)
            or not isinstance(subjects, list)
            or not all(isinstance(item, str) for item in subjects)
        ):
            raise ValueError("adoption evidence lane must contain a name and subjects")
        return cls(name, tuple(subjects))


@dataclass(frozen=True)
class AdoptionEvidence:
    """Versioned proof of a successful adopter-owned drill or observation."""

    kind: AdoptionEvidenceKind
    lane: AdoptionLane
    control_revision: str
    from_stage: AdoptionStage
    to_stage: AdoptionStage

    def __post_init__(self) -> None:
        if not self.control_revision or self.control_revision != self.control_revision.strip():
            raise ValueError("adoption evidence control revision must be non-empty and trimmed")
        if self.kind is AdoptionEvidenceKind.ROLLBACK_DRILL and not (
            self.from_stage in {AdoptionStage.CANARY, AdoptionStage.EXPANDED}
            and self.to_stage is AdoptionStage.LEGACY
        ):
            raise ValueError("rollback evidence must restore an active lane to legacy")
        if self.kind is AdoptionEvidenceKind.CANARY_OBSERVATION and not (
            self.from_stage is AdoptionStage.CANARY and self.to_stage is AdoptionStage.CANARY
        ):
            raise ValueError("canary evidence must observe a stable canary stage")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "lane": self.lane.to_dict(),
            "control_revision": self.control_revision,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AdoptionEvidence:
        if data.get("schema_version") != 1:
            raise ValueError("unsupported adoption evidence schema version")
        lane = data.get("lane")
        kind = data.get("kind")
        control_revision = data.get("control_revision")
        from_stage = data.get("from_stage")
        to_stage = data.get("to_stage")
        if not (
            isinstance(lane, dict)
            and isinstance(kind, str)
            and isinstance(control_revision, str)
            and isinstance(from_stage, str)
            and isinstance(to_stage, str)
        ):
            raise ValueError("invalid adoption evidence")
        try:
            return cls(
                AdoptionEvidenceKind(kind),
                AdoptionLane.from_dict(lane),
                control_revision,
                AdoptionStage(from_stage),
                AdoptionStage(to_stage),
            )
        except ValueError as error:
            raise ValueError(f"invalid adoption evidence: {error}") from error


@dataclass(frozen=True)
class AdoptionPolicy:
    """Portable evidence threshold; host-specific controls remain adapter policy."""

    minimum_matching_runs: int = 1
    required_dimensions: frozenset[DecisionDimension] = frozenset(DecisionDimension)

    def __post_init__(self) -> None:
        if self.minimum_matching_runs < 1:
            raise ValueError("minimum matching runs must be positive")
        if not self.required_dimensions:
            raise ValueError("required decision dimensions cannot be empty")


@dataclass(frozen=True)
class AdoptionDecision:
    """A deterministic, machine-readable cutover or rollback decision."""

    action: AdoptionAction
    lane: AdoptionLane
    from_stage: AdoptionStage
    to_stage: AdoptionStage
    reasons: tuple[str, ...] = ()

    @property
    def authorized(self) -> bool:
        return self.action is not AdoptionAction.HOLD

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "action": self.action,
            "authorized": self.authorized,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "lane": {"name": self.lane.name, "subjects": list(self.lane.subjects)},
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class AdoptionTransition:
    """Exact compare-and-swap command for an adopter-owned control plane."""

    operation_key: str
    control_revision: str
    action: AdoptionAction
    lane: AdoptionLane
    expected_stage: AdoptionStage
    target_stage: AdoptionStage

    def __post_init__(self) -> None:
        if not self.operation_key or self.operation_key != self.operation_key.strip():
            raise ValueError("adoption transition operation key must be non-empty and trimmed")
        if not self.control_revision or self.control_revision != self.control_revision.strip():
            raise ValueError("adoption transition control revision must be non-empty and trimmed")
        if self.action is AdoptionAction.HOLD:
            raise ValueError("a hold decision cannot become an adoption transition")
        expected_target = {
            AdoptionAction.ENTER_CANARY: (AdoptionStage.LEGACY, AdoptionStage.CANARY),
            AdoptionAction.EXPAND: (AdoptionStage.CANARY, AdoptionStage.EXPANDED),
            AdoptionAction.ROLLBACK: (None, AdoptionStage.LEGACY),
        }[self.action]
        if self.target_stage is not expected_target[1] or (
            expected_target[0] is not None and self.expected_stage is not expected_target[0]
        ):
            raise ValueError("adoption transition stages do not match its action")
        if self.action is AdoptionAction.ROLLBACK and self.expected_stage is AdoptionStage.LEGACY:
            raise ValueError("rollback transition must start from an active adopter stage")
        expected_key = _adoption_transition_key(
            self.control_revision,
            self.action,
            self.lane,
            self.expected_stage,
            self.target_stage,
        )
        if self.operation_key != expected_key:
            raise ValueError("adoption transition operation key does not match its content")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation_key": self.operation_key,
            "control_revision": self.control_revision,
            "action": self.action,
            "lane": self.lane.to_dict(),
            "expected_stage": self.expected_stage,
            "target_stage": self.target_stage,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AdoptionTransition:
        if data.get("schema_version") != 1:
            raise ValueError("unsupported adoption transition schema version")
        operation_key = data.get("operation_key")
        control_revision = data.get("control_revision")
        action = data.get("action")
        lane = data.get("lane")
        expected_stage = data.get("expected_stage")
        target_stage = data.get("target_stage")
        if not (
            isinstance(operation_key, str)
            and isinstance(control_revision, str)
            and isinstance(action, str)
            and isinstance(lane, dict)
            and isinstance(expected_stage, str)
            and isinstance(target_stage, str)
        ):
            raise ValueError("invalid adoption transition")
        try:
            return cls(
                operation_key,
                control_revision,
                AdoptionAction(action),
                AdoptionLane.from_dict(lane),
                AdoptionStage(expected_stage),
                AdoptionStage(target_stage),
            )
        except ValueError as error:
            raise ValueError(f"invalid adoption transition: {error}") from error


def _adoption_transition_key(
    control_revision: str,
    action: AdoptionAction,
    lane: AdoptionLane,
    expected_stage: AdoptionStage,
    target_stage: AdoptionStage,
) -> str:
    payload = {
        "schema_version": 1,
        "control_revision": control_revision,
        "action": action,
        "lane": lane.to_dict(),
        "expected_stage": expected_stage,
        "target_stage": target_stage,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"adoption-transition:{sha256(encoded).hexdigest()}"


def prepare_adoption_transition(
    decision: AdoptionDecision, *, control_revision: str
) -> AdoptionTransition:
    """Bind an authorized decision to one idempotent host-control mutation."""

    if not decision.authorized:
        raise ValueError("a hold decision cannot become an adoption transition")
    if not control_revision or control_revision != control_revision.strip():
        raise ValueError("adoption control revision must be non-empty and trimmed")
    operation_key = _adoption_transition_key(
        control_revision,
        decision.action,
        decision.lane,
        decision.from_stage,
        decision.to_stage,
    )
    return AdoptionTransition(
        operation_key,
        control_revision,
        decision.action,
        decision.lane,
        decision.from_stage,
        decision.to_stage,
    )


def adoption_decision(
    reports: tuple[ShadowReport, ...],
    lane: AdoptionLane,
    evidence: tuple[AdoptionEvidence, ...],
    *,
    policy: AdoptionPolicy,
    control_revision: str,
    current_stage: AdoptionStage,
    operator_approved: bool,
    request_expansion: bool = False,
    rollback_requested: bool = False,
) -> AdoptionDecision:
    """Authorize one staged transition, with rollback taking precedence."""

    if not control_revision or control_revision != control_revision.strip():
        raise ValueError("adoption control revision must be non-empty and trimmed")
    if rollback_requested and current_stage is not AdoptionStage.LEGACY:
        return AdoptionDecision(
            AdoptionAction.ROLLBACK,
            lane,
            current_stage,
            AdoptionStage.LEGACY,
            ("rollback_requested",),
        )

    reasons: list[str] = []
    applicable = tuple(
        item for item in evidence if item.lane == lane and item.control_revision == control_revision
    )
    if rollback_requested:
        reasons.append("already_on_legacy")
    if request_expansion and current_stage is not AdoptionStage.CANARY:
        reasons.append("expansion_requires_canary_stage")
    if not request_expansion and current_stage is not AdoptionStage.LEGACY:
        reasons.append("canary_requires_legacy_stage")
    if len(reports) < policy.minimum_matching_runs:
        reasons.append("insufficient_matching_runs")
    if any(not report.matches for report in reports):
        reasons.append("unresolved_divergence")
    revisions = [report.baseline_revision for report in reports]
    if len(revisions) != len(set(revisions)):
        reasons.append("duplicate_shadow_revision")
    if any(not policy.required_dimensions.issubset(report.dimensions) for report in reports):
        reasons.append("incomplete_decision_coverage")
    if not any(item.kind is AdoptionEvidenceKind.ROLLBACK_DRILL for item in applicable):
        reasons.append("rollback_not_tested")
    if not operator_approved:
        reasons.append("operator_approval_missing")
    if request_expansion and not any(
        item.kind is AdoptionEvidenceKind.CANARY_OBSERVATION for item in applicable
    ):
        reasons.append("canary_not_observed")
    if reasons:
        return AdoptionDecision(
            AdoptionAction.HOLD, lane, current_stage, current_stage, tuple(reasons)
        )
    action = AdoptionAction.EXPAND if request_expansion else AdoptionAction.ENTER_CANARY
    to_stage = AdoptionStage.EXPANDED if request_expansion else AdoptionStage.CANARY
    return AdoptionDecision(action, lane, current_stage, to_stage)
