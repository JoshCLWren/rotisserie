"""Doctor checks read from the published ``factory-stats.json`` snapshot, plus ``Finding``.

These checks make no GitHub call of their own: the status page job publishes
the reliability counts, Turso usage, the store schema status and recent crash
groups, and each check judges one of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from rotisserie.factory_console.github import Obj, timestamp
from rotisserie.factory_console.service import crashes, db_usage, schema

DASHBOARD_WORKFLOW = "factory-status-pages.yml"
STATS_STALE = timedelta(hours=1)


@dataclass(frozen=True)
class Finding:
    """One health check: ok, warn or fail, with what to do about it."""

    check: str
    status: str  # ok | warn | fail
    text: str
    hint: str = ""
    fix: str = ""  # a safe repair `apply_fixes` can make; empty when none
    targets: tuple[str, ...] = ()  # what the fix acts on, e.g. "pr:N@HEAD"
    secondary_limit: bool = False
    retry_after_seconds: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def snapshot(stats: Obj | None, now: datetime) -> Finding:
    if stats is None:
        return Finding(
            "reliability snapshot",
            "fail",
            "factory-stats.json has never been published, so stats and model picking are blind",
            f"check the {DASHBOARD_WORKFLOW} runs below; doctor --fix runs it now",
            fix="publish-snapshot",
        )
    generated = timestamp(stats, "generated_at")
    if generated is None or now - generated > STATS_STALE:
        age = "unknown" if generated is None else f"{int((now - generated).total_seconds() // 60)}m"
        return Finding(
            "reliability snapshot",
            "warn",
            f"factory-stats.json is stale (age {age}; it should refresh every 15 minutes)",
            "GitHub's 15-minute schedule can lag by hours; doctor --fix runs the publisher now",
            fix="publish-snapshot",
        )
    return Finding("reliability snapshot", "ok", "published and fresh")


def published_checks(
    stats: Callable[[], Obj | None], now: datetime
) -> list[tuple[str, Callable[[], Finding]]]:
    """(name, check) pairs; ``stats`` is the cached snapshot read."""
    return [
        ("reliability snapshot", lambda: snapshot(stats(), now)),
        ("database usage", lambda: Finding("database usage", *db_usage.health(stats(), now))),
        ("store schema", lambda: Finding("store schema", *schema.health(stats(), now))),
        (crashes.CHECK, lambda: Finding(crashes.CHECK, *crashes.health(stats(), now))),
    ]
