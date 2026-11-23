"""Read-only GitHub API budgets and retry guidance for factory operators.

The authenticated `gh` credentials are the source of truth, not the
factory worker installations' separate tokens. The rate_limit endpoint does
not consume primary REST quota, and errors must never produce guessed ETAs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubRateLimitError,
    integer,
    obj,
)

BUCKETS = ("core", "graphql", "search")


@dataclass(frozen=True)
class Bucket:
    limit: int
    remaining: int
    used: int
    reset: int

    def reset_at(self) -> datetime:
        return datetime.fromtimestamp(self.reset, UTC)

    def to_dict(self) -> dict[str, int | str]:
        return {**asdict(self), "reset_at": self.reset_at().isoformat()}


@dataclass(frozen=True)
class RateLimits:
    resources: dict[str, Bucket]

    def to_dict(self) -> dict[str, dict[str, int | str]]:
        return {name: bucket.to_dict() for name, bucket in self.resources.items()}


def read(github: GitHub) -> RateLimits:
    """Query the REST rate-limit endpoint using this CLI's actual credentials."""
    resources = obj(obj(github.api("rate_limit")).get("resources"))
    buckets: dict[str, Bucket] = {}
    for name in BUCKETS:
        item = obj(resources.get(name))
        limit = integer(item, "limit")
        remaining = integer(item, "remaining")
        used = integer(item, "used")
        reset = integer(item, "reset")
        if None in (limit, remaining, used, reset):
            continue
        assert limit is not None and remaining is not None
        assert used is not None and reset is not None
        if min(limit, remaining, used, reset) < 0:
            continue
        buckets[name] = Bucket(limit, remaining, used, reset)
    if not buckets:
        raise GitHubError("GitHub did not return usable API quota information.")
    return RateLimits(buckets)


def retry_at(
    report: RateLimits | None, error: GitHubRateLimitError, now: datetime
) -> tuple[datetime | None, str]:
    """Return a defensible retry bound, not a guarantee GitHub will accept it."""
    if error.retry_after_seconds is not None:
        return now + timedelta(seconds=error.retry_after_seconds), "GitHub Retry-After"
    if error.secondary:
        return None, "secondary rate limit; GitHub provided no Retry-After"
    core = report.resources.get("core") if report is not None else None
    if core is not None and core.remaining == 0 and core.reset_at() > now:
        return core.reset_at(), "REST core quota reset"
    return None, "no confirmed retry time (quota may be secondary or another bucket)"


def local_deadline(moment: datetime) -> str:
    return moment.astimezone().strftime("%a %b %d, %I:%M:%S %p %Z").replace(" 0", " ")


def countdown(moment: datetime, now: datetime) -> str:
    seconds = max(0, int((moment - now).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
