"""Fail-closed policy for staged external-adopter cutover and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rotisserie.application.shadow import DecisionDimension, ShadowReport


class AdoptionAction(StrEnum):
    """The next mutation-boundary action authorized by adoption evidence."""

    HOLD = "hold"
    ENTER_CANARY = "enter_canary"
    EXPAND = "expand"
    ROLLBACK = "rollback"


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
    reasons: tuple[str, ...] = ()

    @property
    def authorized(self) -> bool:
        return self.action is not AdoptionAction.HOLD

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "action": self.action,
            "authorized": self.authorized,
            "lane": {"name": self.lane.name, "subjects": list(self.lane.subjects)},
            "reasons": list(self.reasons),
        }


def adoption_decision(
    reports: tuple[ShadowReport, ...],
    lane: AdoptionLane,
    *,
    policy: AdoptionPolicy,
    operator_approved: bool,
    rollback_tested: bool,
    canary_observed: bool = False,
    request_expansion: bool = False,
    rollback_requested: bool = False,
) -> AdoptionDecision:
    """Authorize one staged transition, with rollback taking precedence."""

    if rollback_requested:
        return AdoptionDecision(AdoptionAction.ROLLBACK, lane, ("rollback_requested",))

    reasons: list[str] = []
    if len(reports) < policy.minimum_matching_runs:
        reasons.append("insufficient_matching_runs")
    if any(not report.matches for report in reports):
        reasons.append("unresolved_divergence")
    revisions = [report.baseline_revision for report in reports]
    if len(revisions) != len(set(revisions)):
        reasons.append("duplicate_shadow_revision")
    if any(not policy.required_dimensions.issubset(report.dimensions) for report in reports):
        reasons.append("incomplete_decision_coverage")
    if not rollback_tested:
        reasons.append("rollback_not_tested")
    if not operator_approved:
        reasons.append("operator_approval_missing")
    if request_expansion and not canary_observed:
        reasons.append("canary_not_observed")
    if reasons:
        return AdoptionDecision(AdoptionAction.HOLD, lane, tuple(reasons))
    action = AdoptionAction.EXPAND if request_expansion else AdoptionAction.ENTER_CANARY
    return AdoptionDecision(action, lane)
