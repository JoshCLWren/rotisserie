"""GitHub requests per factory job and the factory's remaining REST budget.

The dispatcher and the merge drain record each job's request counts and the
workflow token's REST budget at the end of the job. The status page job
publishes the last jobs as ``gh_usage`` inside ``factory-stats.json``. Recap
reuses its own snapshot read; ``limits`` makes one read. The operator's own
``gh`` credential cannot see the factory installation's budget, so this is
the only view of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rotisserie.factory_console.github import GitHub, Obj, integer, obj
from rotisserie.factory_console.service import reliability

KINDS = {"dispatch": "dispatcher", "merge": "merge drain"}
DISPATCH_RESERVE = 1200


def _jobs(value: object) -> list[Obj]:
    return [obj(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _average(values: list[int]) -> int:
    return round(sum(values) / len(values)) if values else 0


def _kind(kind: str, jobs: list[Obj]) -> dict[str, Any]:
    github = [integer(job, "github") or 0 for job in jobs]
    buckets = [obj(job.get("buckets")) for job in jobs]
    top: dict[str, int] = {}
    for job in jobs:
        entries = job.get("top")
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], int):
                top[str(entry[0])] = top.get(str(entry[0]), 0) + entry[1]
    return {
        "kind": kind,
        "label": KINDS[kind],
        "jobs": len(jobs),
        "average": _average(github),
        "most": max(github, default=0),
        "average_workflow": _average([integer(b, "workflow") or 0 for b in buckets]),
        "average_reader": _average([integer(b, "reader") or 0 for b in buckets]),
        "latest_at": max((integer(job, "at") or 0 for job in jobs), default=0),
        "top": sorted(top.items(), key=lambda item: (-item[1], item[0]))[:3],
    }


def from_stats(stats: Obj | None) -> dict[str, Any]:
    """Per-kind averages and the newest recorded budget, or ``unpublished``."""
    published = obj((stats or {}).get("gh_usage"))
    jobs = obj(published.get("jobs"))
    kinds = [_kind(kind, _jobs(jobs.get(kind))) for kind in KINDS if _jobs(jobs.get(kind))]
    if not kinds:
        return {"status": "unpublished", "kinds": [], "budget": None}
    budgets = [
        (integer(job, "at") or 0, obj(job.get("budget")))
        for kind in KINDS
        for job in _jobs(jobs.get(kind))
        if obj(job.get("budget"))
    ]
    budget: dict[str, int | None] | None = None
    left: int | None = None
    if budgets:
        at, newest = max(budgets, key=lambda pair: pair[0])
        remaining, limit = integer(newest, "remaining"), integer(newest, "limit")
        if remaining is not None and limit:
            reset = integer(newest, "reset")
            budget = {"remaining": remaining, "limit": limit, "reset": reset, "at": at}
            left = remaining
    low = left is not None and left < DISPATCH_RESERVE
    return {"status": "warn" if low else "ok", "kinds": kinds, "budget": budget}


def published(github: GitHub) -> dict[str, Any]:
    return from_stats(reliability.load_stats(github))


def moment(epoch: object) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if isinstance(epoch, int) and epoch > 0 else None


def kind_line(row: dict[str, Any]) -> str:
    top = ", ".join(f"{key} {count}" for key, count in row["top"])
    return (
        f"{row['label']}: about {row['average']} GitHub requests per job "
        f"(most {row['most']}, last {row['jobs']} jobs; workflow token "
        f"{row['average_workflow']}, read-only App {row['average_reader']})"
        + (f"; top: {top}" if top else "")
    )


def budget_line(budget: dict[str, Any] | None) -> str:
    if budget is None:
        return "Factory workflow token budget: not recorded yet"
    text = f"Factory workflow token budget: {budget['remaining']:,} of {budget['limit']:,} left"
    if budget["remaining"] < DISPATCH_RESERVE:
        text += f"; the dispatcher skips its pass below {DISPATCH_RESERVE:,}"
    return text


def summary(report: dict[str, Any]) -> str:
    parts = [f"{row['label']} about {row['average']} per job" for row in report["kinds"]]
    budget = report.get("budget")
    left = f"; {budget['remaining']:,} of {budget['limit']:,} left" if budget else ""
    return "GitHub requests: " + ", ".join(parts) + left
