"""Config-derived worker inventory with no hard identity ceiling."""

from __future__ import annotations

from dataclasses import dataclass

from rotisserie.domain.model import WorkerId


@dataclass(frozen=True)
class WorkerInventory:
    """A declared set of worker identities whose size is config-derived.

    The inventory replaces hard-coded worker ceilings: capacity bounds and the
    set of recognized owners follow the ``expected`` membership supplied by
    configuration, never a numeric maximum baked into code. ``retired`` records
    identities removed from rotation so a later roster entry cannot silently
    re-enroll them.

    ``scheduled`` is a declared-but-not-yet-enforced subset of ``expected``: it
    records which workers an operator intends for automatic dispatch, but no
    command consumes it today. Capacity still derives from the full ``expected``
    size, mirroring the deferred ``decide``/``shadow-project`` parity boundary.
    Adding or removing a worker is a configuration edit, not a code change to a
    ceiling constant.
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
