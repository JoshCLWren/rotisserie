"""Failed worker runs, grouped by the job and step where each one failed.

Each row carries the error annotation, the outcome class the persist step
recorded, the provider, the target and a link to the run.
"""

from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    items,
    job_name,
    string,
)
from rotisserie.factory_console.tracker import WORKER_WORKFLOW, Tracker, completed, run_version
from rotisserie.factory_console.verdicts import failure_reasons, outcome_class

WORKER_JOB = "run / factory"
PERSIST_JOB = "Persist trusted worker outcome"
FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
TITLE_WORKER = re.compile(r"^Factory (\d+) ")


@dataclass(frozen=True)
class WorkerFailure:
    run_id: int
    url: str
    finished_at: str
    worker: str
    job: str
    step: str
    error: str
    outcome_class: str
    provider: str
    model: str
    target: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class FailureGroup:
    job: str
    step: str
    runs: list[WorkerFailure] = field(default_factory=list)

    def errors(self) -> Counter[str]:
        return Counter(run.error or "(no error annotation)" for run in self.runs)

    def classes(self) -> Counter[str]:
        return Counter(run.outcome_class or "unrecorded" for run in self.runs)

    def causes(self) -> Counter[str]:
        """The classes the factory's current rules give these runs."""
        return Counter(real_cause(run) or "unrecorded" for run in self.runs)

    def to_dict(self) -> dict[str, object]:
        return {
            "job": self.job,
            "step": self.step,
            "count": len(self.runs),
            "errors": dict(self.errors().most_common()),
            "classes": dict(self.classes().most_common()),
            "causes": dict(self.causes().most_common()),
            "runs": [run.to_dict() for run in self.runs],
        }


def real_cause(row: WorkerFailure) -> str:
    """The recorded class, or ``no_capacity`` when the factory's capacity rule matches.

    Runs recorded before a capacity exit was recognized carry an older class
    for the same exit, so the error is checked with the factory's own rule.
    """
    if row.outcome_class == "no_capacity" or not row.error:
        return row.outcome_class
    try:
        rule = runtime.module("factory_dynamic_outcome").capacity_reason
    except runtime.RuntimeUnavailableError:
        return row.outcome_class
    return "no_capacity" if rule(row.error) else row.outcome_class


def runs_since(github: GitHub, workflow: str, iso: str, *, pages: int = 3) -> list[Obj]:
    found: list[Obj] = []
    for page in range(1, pages + 1):
        rows = items(
            github.repo_api(
                f"actions/workflows/{workflow}/runs?created=%3E%3D{iso}&per_page=100&page={page}"
            ),
            "workflow_runs",
        )
        found += rows
        if len(rows) < 100:
            break
    return found


def first_failed_step(jobs: list[Obj]) -> tuple[str, str]:
    """The first failing (job, step), checking the worker job before the others."""
    ordered = sorted(jobs, key=lambda job: job_name(job) != WORKER_JOB)
    for job in ordered:
        for step in items(job, "steps"):
            if string(step, "conclusion") == "failure":
                return job_name(job), string(step, "name")
    for job in ordered:
        if string(job, "conclusion") in FAILED_CONCLUSIONS:
            return job_name(job), ""
    return "", ""


def _job_named(jobs: list[Obj], name: str) -> Obj | None:
    return next((job for job in jobs if job_name(job).startswith(name)), None)


def read_failure(tracker: Tracker, run: Obj) -> WorkerFailure | None:
    run_id = integer(run, "id")
    if run_id is None:
        return None
    try:
        jobs = tracker.jobs(run_id, version=run_version(run))
    except GitHubError:
        return None
    job, step = first_failed_step(jobs)
    failing = _job_named(jobs, job) if job else None
    errors = (
        failure_reasons(tracker.annotations(integer(failing, "id") or 0, final=completed(failing)))
        if failing
        else []
    )
    persist = _job_named(jobs, PERSIST_JOB)
    recorded = (
        outcome_class(tracker.log(integer(persist, "id") or 0, final=completed(persist)))
        if persist
        else {}
    )
    title = TITLE_WORKER.match(string(run, "display_title"))
    worker = title.group(1) if title else ""
    source = recorded.get("source", "")
    if source:
        provider = runtime.provider_for_source(source)
    else:
        found = runtime.provider_for_worker(worker) if worker else None
        provider = found.name if found else ""
    return WorkerFailure(
        run_id=run_id,
        url=string(run, "html_url"),
        finished_at=string(run, "updated_at"),
        worker=worker,
        job=job or "unknown job",
        step=step,
        error=errors[0] if errors else "",
        outcome_class=recorded.get("class", ""),
        provider=provider,
        model=recorded.get("model", ""),
        target=recorded.get("target", ""),
    )


def worker_failures(
    github: GitHub,
    start: datetime,
    *,
    limit: int = 60,
    tracker: Tracker | None = None,
    runs: list[Obj] | None = None,
) -> list[WorkerFailure]:
    """The most recent failed worker runs created since ``start``, newest first."""
    if runs is None:
        iso = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        runs = runs_since(github, WORKER_WORKFLOW, iso)
    failed = [run for run in runs if string(run, "conclusion") in FAILED_CONCLUSIONS]
    failed.sort(key=lambda run: string(run, "updated_at"), reverse=True)
    reader = tracker or Tracker(github)
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = list(pool.map(lambda run: read_failure(reader, run), failed[:limit]))
    return [row for row in found if row is not None]


def group(failures: list[WorkerFailure]) -> list[FailureGroup]:
    groups: dict[tuple[str, str], FailureGroup] = {}
    for row in failures:
        groups.setdefault((row.job, row.step), FailureGroup(row.job, row.step)).runs.append(row)
    return sorted(groups.values(), key=lambda entry: (-len(entry.runs), entry.job, entry.step))
