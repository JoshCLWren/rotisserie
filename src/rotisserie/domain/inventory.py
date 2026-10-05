"""Config-derived worker inventory with no hard identity ceiling."""

from __future__ import annotations

from dataclasses import dataclass

from rotisserie.domain.model import WorkerId
from rotisserie.domain.policy import SchedulingPolicy


@dataclass(frozen=True)
class WorkerInventory:
    """A declared set of worker identities whose size is config-derived.

    The inventory replaces hard-coded worker ceilings: capacity bounds and the
    set of recognized owners follow the ``expected`` membership supplied by
    configuration, never a numeric maximum baked into code. ``retired`` records
    identities removed from rotation so a later roster entry cannot silently
    re-enroll them, and ``scheduled`` marks the subset that participates in
    automatic dispatch. Adding or removing a worker is a configuration edit, not
    a code change to a ceiling constant.
    """

    expected: frozenset[WorkerId]
    retired: frozenset[WorkerId] = frozenset()
    scheduled: frozenset[WorkerId] = frozenset()

    def __post_init__(self) -> None:
        if not self.expected:
            raise ValueError("worker inventory requires at least one expected worker")
        overlap = self.expected & self.retired
        if overlap:
            raise ValueError("a worker cannot be both expected and retired")
        if not self.scheduled <= self.expected:
            raise ValueError("scheduled workers must be a subset of expected workers")

    @property
    def size(self) -> int:
        """Number of expected workers; the config-derived capacity ceiling."""

        return len(self.expected)

    def is_expected(self, worker: WorkerId) -> bool:
        return worker in self.expected

    def is_retired(self, worker: WorkerId) -> bool:
        return worker in self.retired

    def is_scheduled(self, worker: WorkerId) -> bool:
        return worker in self.scheduled


def scheduling_policy(
    inventory: WorkerInventory,
    *,
    reserved_review: int = 0,
    backlog_limit: int | None = None,
) -> SchedulingPolicy:
    """Derive a scheduling policy whose WIP limit is the inventory size."""

    return SchedulingPolicy(inventory.size, reserved_review, backlog_limit)


def resolve_wip_limit(inventory: WorkerInventory | None, override: int | None) -> int:
    """Resolve a WIP ceiling from an explicit override or the inventory size.

    An explicit override always wins. Otherwise the ceiling is the configured
    inventory size, falling back to a single concurrent worker only when no
    inventory is declared.
    """

    if override is not None:
        if override < 0:
            raise ValueError("WIP limit cannot be negative")
        return override
    if inventory is not None:
        return inventory.size
    return 1
