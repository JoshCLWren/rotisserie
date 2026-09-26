"""Demand-driven capacity allocation for ComicPile factories."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

# OmniRoute's shared free coding pool 429s when many Entry smokes overlap.
# Three concurrent Entry/lease units is the working ceiling that still lets
# light-load Sessions start; dispatcher, assignment, and drain must share it.
DEFAULT_OMNIROUTE_FREE_ENTRY_CAP = 3
# While OmniRoute is disabled, Entry uses independent multi-provider capacity
# so nvidia/opencode/openrouter/kilo can restore service.
DEFAULT_MULTI_PROVIDER_ENTRY_CAP = 12


def omniroute_enabled(raw: str | None = None) -> bool:
    """Return whether OmniRoute (including auto/*) may execute.

    Defaults to disabled for the 2026-09-06 incident. When false, multi-provider
    Entry still has capacity via ``DEFAULT_MULTI_PROVIDER_ENTRY_CAP``. Explicit
    on/true/1 re-enables OmniRoute; missing/empty/off/false/0 keep it dark.
    """
    value = (raw if raw is not None else os.environ.get("FACTORY_OMNIROUTE_ENABLED", "off"))
    return value.strip().lower() in {"1", "on", "true", "yes"}




@dataclass(frozen=True)
class FleetDemand:
    """Current executable work and idle worker capacity."""

    completion: int
    production: int
    idle_workers: int

    def __post_init__(self) -> None:
        if self.completion < 0 or self.production < 0 or self.idle_workers < 0:
            raise ValueError("factory demand counts cannot be negative")

    @property
    def total(self) -> int:
        return self.completion + self.production

    @property
    def completion_share(self) -> float:
        """Return the fraction of executable demand that is completion work."""
        if self.total == 0:
            return 0.0
        return self.completion / self.total


def remaining_omniroute_free_entry_slots(
    in_flight: int,
    *,
    cap: int = DEFAULT_OMNIROUTE_FREE_ENTRY_CAP,
    enabled: bool | None = None,
) -> int:
    """Return how many new OmniRoute free Entry sessions may start.

    Args:
        in_flight: Occupied Entry runs plus equivalent fixed-model leases.
        cap: Maximum concurrent OmniRoute free-entry units.
        enabled: Optional override for the OmniRoute enable gate. When omitted,
            reads ``FACTORY_OMNIROUTE_ENABLED`` (default off).

    Returns:
        Non-negative remaining slots. When OmniRoute is disabled, uses the multi-provider Entry budget so service can continue without OmniRoute.

    Raises:
        ValueError: If ``in_flight`` or ``cap`` is negative.
    """
    if enabled is None:
        enabled = omniroute_enabled()
    if in_flight < 0:
        raise ValueError("in-flight factory entry count cannot be negative")
    if cap < 0:
        raise ValueError("omniroute free entry cap cannot be negative")
    if not enabled:
        # OmniRoute dark: always use the multi-provider Entry budget.
        # Callers that need a custom budget pass enabled=True with their cap.
        return max(0, DEFAULT_MULTI_PROVIDER_ENTRY_CAP - in_flight)
    return max(0, cap - in_flight)


def apply_omniroute_free_entry_cap(
    demand: FleetDemand,
    in_flight: int,
    *,
    cap: int = DEFAULT_OMNIROUTE_FREE_ENTRY_CAP,
) -> FleetDemand:
    """Bound idle workers to remaining OmniRoute free-entry slots.

    Args:
        demand: Uncapped completion/production demand and idle fleet size.
        in_flight: Occupied Entry runs plus equivalent fixed-model leases.
        cap: Maximum concurrent OmniRoute free-entry units.

    Returns:
        The original demand when idle workers already fit the remaining
        slots; otherwise a copy whose idle count equals remaining slots.
    """
    remaining = remaining_omniroute_free_entry_slots(in_flight, cap=cap)
    if remaining >= demand.idle_workers:
        return demand
    return FleetDemand(
        completion=demand.completion,
        production=demand.production,
        idle_workers=remaining,
    )


def completion_worker_target(demand: FleetDemand) -> int:
    """Allocate idle workers proportionally to live completion demand.

    The allocator is work-conserving and derived from current queue figures:
    - no completion demand consumes no completion workers;
    - no production demand lets completion consume the idle fleet;
    - mixed demand receives a proportional share of idle capacity;
    - any non-empty completion queue gets at least one worker when capacity exists;
    - assignments never exceed either completion queue depth or idle capacity.
    """
    if demand.completion == 0 or demand.idle_workers == 0:
        return 0
    if demand.production == 0:
        return min(demand.completion, demand.idle_workers)

    proportional = math.ceil(demand.idle_workers * demand.completion_share)
    return min(demand.completion, demand.idle_workers, max(1, proportional))


def production_worker_target(demand: FleetDemand) -> int:
    """Return idle capacity left for fresh implementation work."""
    return max(0, demand.idle_workers - completion_worker_target(demand))
