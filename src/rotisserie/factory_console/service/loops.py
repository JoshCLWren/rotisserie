"""Items the factory keeps sending workers to without moving them toward merge.

The rule is the factory's own (``factory_item_loop``), so ``baste turning``,
``recap``, ``doctor``, the control rooms and the dispatcher's breaker agree.
Bounded reads: one worker-run list over the loop window (at most three pages),
the persist job of at most ``LIMIT`` finished runs (a finished job's log is
cached for good once read), and one open pull-request list.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    job_name,
    label_names,
    string,
    timestamp,
)
from rotisserie.factory_console.service import failures, review
from rotisserie.factory_console.tracker import WORKER_WORKFLOW, Tracker, completed, run_version
from rotisserie.factory_console.verdicts import outcome_class

LIMIT = 60


def rule() -> ModuleType:
    return runtime.module("factory_item_loop")


def read_run(tracker: Tracker, run: Obj) -> tuple[str, Any] | None:
    """(target, Run) from one finished run's persisted outcome-class line."""
    finished = timestamp(run, "updated_at")
    run_id = integer(run, "id")
    if string(run, "status") != "completed" or finished is None or run_id is None:
        return None
    try:
        jobs = tracker.jobs(run_id, version=run_version(run))
    except GitHubError:
        return None
    persist = next((j for j in jobs if job_name(j).startswith(failures.PERSIST_JOB)), None)
    if persist is None:
        return None
    try:
        log = tracker.log(integer(persist, "id") or 0, final=completed(persist))
    except GitHubError:
        return None
    found = outcome_class(log)
    target = found.get("target", "")
    if not target.startswith(("pr:", "issue:")) or not found.get("class"):
        return None
    item = rule().Run(
        at=finished,
        kind=found.get("kind", ""),
        model=found.get("model", ""),
        outcome=found["class"],
        url=string(run, "html_url"),
    )
    return target, item


def scan(
    github: GitHub,
    *,
    now: datetime,
    tracker: Tracker | None = None,
    runs: Sequence[Obj] | None = None,
    pulls: Sequence[Obj] | None = None,
    parked: Sequence[int] = (),
) -> list[dict[str, object]]:
    """Every looping open PR, worst first, with its title, link and runs."""
    module = rule()
    threshold, window = module.limits()
    start = now - timedelta(seconds=window)
    if runs is None:
        iso = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        runs = failures.runs_since(github, WORKER_WORKFLOW, iso)
    finished = [
        run
        for run in runs
        if string(run, "status") == "completed"
        and (done := timestamp(run, "updated_at")) is not None
        and done > start
    ]
    finished.sort(key=lambda run: string(run, "updated_at"), reverse=True)
    reader = tracker or Tracker(github)
    with ThreadPoolExecutor(max_workers=6) as pool:
        read = list(pool.map(lambda run: read_run(reader, run), finished[:LIMIT]))
    by_target: dict[str, list[Any]] = {}
    for row in read:
        if row is not None:
            by_target.setdefault(row[0], []).append(row[1])
    open_rows = list(pulls) if pulls is not None else review.open_pulls(github)
    by_number = {integer(pr, "number") or 0: pr for pr in open_rows}
    found = module.find(
        by_target,
        now=now,
        open_items={f"pr:{n}": label_names(pr) for n, pr in by_number.items() if n},
        threshold=threshold,
        window_seconds=window,
    )
    rows: list[dict[str, object]] = []
    for item in found:
        pr = by_number[item.number]
        rows.append(
            {
                **item.to_dict(),
                "number": item.number,
                "title": string(pr, "title"),
                "url": string(pr, "html_url"),
                "parked": item.number in parked,
                "owner": module.bypassed(label_names(pr)),
            }
        )
    return rows


def summary(rows: Sequence[dict[str, object]]) -> str:
    if not rows:
        return ""
    first = rows[0]
    more = f" and {len(rows) - 1} more" if len(rows) > 1 else ""
    return f"{first['title'] or first['target']} is looping{more}: {first['text']}"
