#!/usr/bin/env python3
"""Is one work item looping? The shared rule behind the console and the factory's breaker.

An item loops when it has at least ``RUNS`` finished worker runs inside the
window and at least half of them changed nothing (a failure, or no work done),
while it is still open and short of ``factory:ready``. A run that never reached
a model, or failed for a factory, provider or model-catalog reason (control
plane, runner environment, quota, throttle, a provider crash, an unclassified
exit), does not count: it says nothing about the item. A run that delivered work
(pushed repairs or opened the PR) counts as progress even when its agent then
hit the deadline.
Runs before the item's last park do not count, so an owner's retry starts fresh.

``baste turning``, ``baste recap``, ``baste doctor``, both control rooms, the
dispatcher's cooling and the self-heal sweep all call ``find``. Pure: callers
bring the runs; nothing here reads the network or the store.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

RUNS = 4
WINDOW_SECONDS = 6 * 3600
RUNS_SETTING = "FACTORY_LOOP_RUNS"
WINDOW_SETTING = "FACTORY_LOOP_WINDOW_SECONDS"
UNCOUNTED = frozenset(
    {
        "no_capacity",
        "control_plane_failure",
        "environment_failure",
        "worker_environment_failure",
        "provider_quota",
        "provider_throttle",
        "provider_failure",
        "unknown_failure",
        "model_unavailable",
        "model_retired_410",
    }
)
PRODUCTIVE = frozenset({"success"})
READY = "factory:ready"
BYPASS_LABELS = frozenset({"factory:override", "factory:claimed"})
PARK_KIND = "loop-parked"


@dataclass(frozen=True)
class Run:
    at: datetime
    kind: str
    model: str
    outcome: str
    url: str = ""
    delivered: bool = False

    @property
    def wasted(self) -> bool:
        return not self.delivered and self.outcome not in PRODUCTIVE

    @property
    def counted(self) -> bool:
        return self.delivered or self.outcome not in UNCOUNTED

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "kind": self.kind,
            "model": self.model,
            "outcome": self.outcome,
            "url": self.url,
            "delivered": self.delivered,
        }


@dataclass(frozen=True)
class Loop:
    target: str
    runs: tuple[Run, ...]
    wasted: int
    reason: str
    reason_count: int
    window_seconds: int

    @property
    def number(self) -> int:
        return int(self.target.partition(":")[2])

    def text(self) -> str:
        hours = self.window_seconds // 3600
        return (
            f"{self.wasted} of {len(self.runs)} worker runs in the last {hours}h changed "
            f"nothing; most often {self.reason} ({self.reason_count})."
        )

    def next_step(self) -> str:
        return (
            "Review it yourself, merge it, or force one more try with "
            f"`baste run --target {self.target}`."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "text": self.text(),
            "next": self.next_step(),
            "wasted": self.wasted,
            "count": len(self.runs),
            "reason": self.reason,
            "runs": [run.to_dict() for run in self.runs],
        }


def limits() -> tuple[int, int]:
    """(runs, window seconds): environment overrides, else the defaults."""
    try:
        from factory_settings import integer
    except ImportError:
        return RUNS, WINDOW_SECONDS
    return integer(RUNS_SETTING, RUNS), integer(WINDOW_SETTING, WINDOW_SECONDS)


def bypassed(labels: Collection[str]) -> bool:
    """An owner override or an explicit claim always wins over the breaker."""
    return bool(BYPASS_LABELS & set(labels))


def assess(
    target: str,
    runs: Iterable[Run],
    *,
    now: datetime,
    threshold: int = RUNS,
    window_seconds: int = WINDOW_SECONDS,
    since: datetime | None = None,
) -> Loop | None:
    start = now - timedelta(seconds=window_seconds)
    if since is not None and since > start:
        start = since
    counted = tuple(
        sorted(
            (run for run in runs if start < run.at <= now and run.counted),
            key=lambda run: run.at,
        )
    )
    wasted = [run for run in counted if run.wasted]
    if len(counted) < max(threshold, 1) or 2 * len(wasted) < len(counted):
        return None
    reason, count = Counter(run.outcome for run in wasted).most_common(1)[0]
    return Loop(target, counted, len(wasted), reason, count, window_seconds)


def find(
    runs: Mapping[str, Iterable[Run]],
    *,
    now: datetime,
    open_items: Mapping[str, Collection[str]],
    parked_at: Mapping[str, datetime] | None = None,
    threshold: int = RUNS,
    window_seconds: int = WINDOW_SECONDS,
) -> list[Loop]:
    """Loops among open items; ``open_items`` maps each open target to its labels."""
    found: list[Loop] = []
    for target, rows in runs.items():
        labels = open_items.get(target)
        if labels is None or READY in labels:
            continue
        loop = assess(
            target,
            rows,
            now=now,
            threshold=threshold,
            window_seconds=window_seconds,
            since=(parked_at or {}).get(target),
        )
        if loop is not None:
            found.append(loop)
    return sorted(found, key=lambda loop: (-loop.wasted, loop.target))
