"""Priority and lane ranking for factory work candidates."""
from __future__ import annotations
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from factory_work_policy_base import _settings

INFRA_LABELS = {'infrastructure', 'e2e-infrastructure', 'policy-change', 'docs', 'documentation', 'quality-control'}

# Work categories in default pick-up order; index == historical lane number.
_CATEGORIES = (
    'main-breakage', 'user-bug', 'user-bug-pr', 'normal', 'e2e', 'infrastructure',
)


@dataclass(frozen=True)
class Candidate:
    """A ranked unit of executable factory work."""
    kind: str
    number: int
    lane: int
    priority: int
    created_at: str
    linked_issue: int | None = None
    stage: str | None = None
    producer_worker: str | None = None
    conflicted: bool = False
    head_contributors: frozenset[str] = frozenset()
    is_fork: bool = False

    def sort_key(self) -> tuple[int, int, float, int]:
        """Return the deterministic queue ordering key (oldest work first)."""
        return (configured_lane_rank(self.lane), -self.priority,
                parse_time(self.created_at), self.number)


def parse_time(value: str | None) -> float:
    """Parse an ISO timestamp into a sortable epoch value."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


def priority_rank(labels: Iterable[str]) -> int:
    """Map explicit priority labels to a deterministic numeric rank."""
    labels = set(labels)
    if 'ralph-priority:critical' in labels or 'priority:P0' in labels or 'priority: P0' in labels:
        return 4
    if 'ralph-priority:high' in labels or 'priority:high' in labels or 'priority: high' in labels:
        return 3
    if 'ralph-priority:medium' in labels:
        return 2
    if 'ralph-priority:low' in labels:
        return 1
    return 0


def work_category(labels: set[str], *, pull_request: bool = False) -> str:
    """Name the operator-visible priority category for a label set."""
    if 'main-breakage' in labels or 'deploy-fix' in labels:
        return 'main-breakage'
    if 'user-reported' in labels and 'bug' in labels:
        return 'user-bug-pr' if pull_request else 'user-bug'
    if labels & INFRA_LABELS:
        return 'infrastructure'
    if 'e2e-discovered' in labels:
        return 'e2e'
    return 'normal'


def provenance_lane(labels: set[str], *, pull_request: bool = False) -> int:
    """Return the fixed semantic lane for a label set.

    Lane numbers keep their historical meaning (0 main-breakage, 1 user bug,
    2 user-bug PR, 3 normal, 4 e2e, 5 infrastructure) because other policy
    reads them. Pick-up *order* is configurable separately; see
    :func:`configured_lane_rank`.
    """
    return _CATEGORIES.index(work_category(labels, pull_request=pull_request))


def configured_lane_rank(lane: int) -> int:
    """Return the operator-configured pick-up rank for a semantic lane."""
    if _settings is None or not 0 <= lane < len(_CATEGORIES):
        return lane
    return _settings.priority_ranks()[_CATEGORIES[lane]]
