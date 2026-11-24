"""The per-PR reads ``unstick`` plans from, each tolerant of one GitHub failure."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import baseline, checks, outcomes
from rotisserie.factory_console.service import conflicts as conflict_check
from rotisserie.factory_console.service import obsolete as obsolete_check
from rotisserie.factory_console.service.steps import Step


def each_pr[T](
    numbers: list[int],
    read: Callable[[int], T],
    step: Step,
    *,
    phase: str,
    label: str,
) -> dict[int, T]:
    """Read every PR in parallel; report each completed read from this thread, in order."""
    found: dict[int, T] = {}
    if not numbers:
        return found
    step(f"{label} on {len(numbers)} pull requests", phase=phase, done=0, total=len(numbers))
    with ThreadPoolExecutor(max_workers=6) as pool:
        for count, (number, value) in enumerate(zip(numbers, pool.map(read, numbers)), 1):
            found[number] = value
            step(
                f"{label}: {count} of {len(numbers)} pull requests",
                phase=phase,
                done=count,
                total=len(numbers),
            )
    return found


def inherited_text(shared: baseline.Report, pr: int) -> str:
    found = [item for item in shared.for_pr(pr) if item.not_the_prs]
    names = ", ".join(dict.fromkeys(item.check for item in found))
    return f"{names}: {found[0].summary}" if found else names


def head(github: GitHub, number: int) -> str:
    try:
        return string(obj(obj(github.repo_api(f"pulls/{number}")).get("head")), "sha")
    except GitHubError:
        return ""


def overlap_text(github: GitHub, target: str) -> str:
    try:
        report = conflict_check.pr_conflicts(github, int(target.split(":")[1]))
    except GitHubError:
        return ""
    return report.text.split(": ", 1)[1] if report.overlaps else ""


def gone(stale: dict[int, obsolete_check.Staleness | None], item: runtime.BacklogPR) -> bool:
    found = stale.get(item.number)
    return found is not None and found.obsolete


def red_checks(github: GitHub, number: int) -> list[checks.FailingCheck]:
    try:
        return checks.failing_checks(github, number)
    except GitHubError:
        return []


def red_text(failing: list[checks.FailingCheck]) -> str:
    return "; ".join(f"{item.name}: {item.summary}" for item in failing[:2])


def staleness(github: GitHub, number: int) -> obsolete_check.Staleness | None:
    try:
        return obsolete_check.pr_staleness(github, number)
    except GitHubError:
        return None


# How far back a model's miss on an item keeps it off that item.
MISS_WINDOW = timedelta(hours=6)


def recent_misses(github: GitHub, now: datetime) -> dict[str, set[str]]:
    """Models whose recent request on each target did not reach its goal."""
    found: dict[str, set[str]] = {}
    try:
        recent = outcomes.request_outcomes(github, since=now - MISS_WINDOW)
    except GitHubError:
        return found
    for item in recent:
        if item.goal == "not-met" and item.model and item.target:
            found.setdefault(item.target, set()).add(item.model)
    return found
