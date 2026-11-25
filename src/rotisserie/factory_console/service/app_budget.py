"""The factory GitHub App installation's REST budget, as the dispatcher last saw it.

The dispatcher's quota gate prints one line per run with the installation
token's ``core`` budget, and skips dispatch below its reserve. Operators'
own ``gh`` credentials have a separate budget, so this reads that line from
the newest dispatcher run's job log instead of calling ``rate_limit``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    items,
    string,
    timestamp,
)
from rotisserie.factory_console.tracker import Tracker, completed

DISPATCHER = "fixed-model-factory-dispatch.yml"
GATE_STEP = "Gate factory dispatch on Actions REST quota"
RESERVE = 1200
NEAR = 1800
LINE = re.compile(
    r"Factory installation REST budget: (?P<remaining>\d+)/(?P<limit>\d+) remaining "
    r"\(reset epoch (?P<reset>\d+)\)"
)
RUNS_TO_READ = 3


@dataclass(frozen=True)
class AppBudget:
    remaining: int
    limit: int
    reset: int
    seen_at: str
    run_url: str
    reserve: int = RESERVE

    def reset_at(self) -> datetime:
        return datetime.fromtimestamp(self.reset, UTC)

    def state(self, now: datetime) -> str:
        if self.reset_at() <= now:
            return "stale"
        if self.remaining < self.reserve:
            return "below_reserve"
        if self.remaining < NEAR:
            return "near_reserve"
        return "ok"

    def to_dict(self, now: datetime) -> dict[str, object]:
        return {**asdict(self), "reset_at": self.reset_at().isoformat(), "state": self.state(now)}


def parse(log: str) -> tuple[int, int, int] | None:
    found = None
    for found in LINE.finditer(log):
        pass
    if found is None:
        return None
    return int(found["remaining"]), int(found["limit"]), int(found["reset"])


def _gate_ran(job: Obj) -> str:
    for step in items(job, "steps"):
        if string(step, "name") == GATE_STEP and string(step, "conclusion") == "success":
            return string(step, "completed_at")
    return ""


def latest(github: GitHub) -> AppBudget | None:
    """The newest reading, or ``None`` when no recent dispatcher run printed one."""
    tracker = Tracker(github)
    runs = items(
        github.repo_api(
            f"actions/workflows/{DISPATCHER}/runs?status=success&per_page={RUNS_TO_READ}"
        ),
        "workflow_runs",
    )
    for run in runs:
        run_id = integer(run, "id")
        if run_id is None:
            continue
        try:
            jobs = tracker.jobs(run_id)
        except GitHubError:
            continue
        for job in jobs:
            seen = _gate_ran(job)
            job_id = integer(job, "id")
            if not seen or job_id is None:
                continue
            found = parse(tracker.log(job_id, final=completed(job)))
            if found is not None:
                return AppBudget(*found, seen_at=seen, run_url=string(run, "html_url"))
    return None


def age(budget: AppBudget, now: datetime) -> str:
    moment = timestamp({"at": budget.seen_at}, "at")
    if moment is None:
        return "unknown age"
    minutes = max(0, int((now - moment).total_seconds() // 60))
    return f"{minutes}m ago" if minutes < 120 else f"{minutes // 60}h ago"
