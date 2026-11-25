"""What one worker run actually did, read from its own job logs.

``gh run view --log`` truncates long runs, so every job's log is fetched on
its own (``actions/jobs/<id>/logs``). The persisted outcome class, the
notices, the error annotations and the failing job and step come from there.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from rotisserie.factory_console.github import (
    GitHub,
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    Obj,
    integer,
    job_name,
    string,
)
from rotisserie.factory_console.service.failures import first_failed_step
from rotisserie.factory_console.tracker import Clock, Tracker, completed
from rotisserie.factory_console.verdicts import failure_reasons, outcome_class, parse_outcome

NOTICE = re.compile(r"##\[notice\](?P<text>.+?)\s*$", re.M)
MAX_JOBS = 8
MAX_NOTICES = 20


@dataclass(frozen=True)
class JobView:
    name: str
    url: str
    status: str
    conclusion: str


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    url: str
    title: str
    workflow: str
    status: str
    conclusion: str
    outcome: dict[str, str]
    verdict: str
    notices: tuple[str, ...]
    errors: tuple[str, ...]
    failed_job: str
    failed_step: str
    jobs: tuple[JobView, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read(github: GitHub, run_id: int) -> RunOutcome:
    tracker = Tracker(github)
    run = tracker.run(run_id)
    if not string(run, "html_url"):
        raise GitHubNotFoundError(f"Run {run_id} was not found in {github.repo}.")
    jobs = tracker.jobs(run_id)
    failed_job, failed_step = first_failed_step(jobs)
    recorded: dict[str, str] = {}
    verdict = ""
    notices: list[str] = []
    errors: list[str] = []
    for job in jobs[:MAX_JOBS]:
        job_id = integer(job, "id")
        if job_id is None or string(job, "status") == "queued":
            continue
        done = completed(job)
        log = tracker.log(job_id, final=done)
        found = outcome_class(log)
        if found.get("class"):
            recorded = found
        parsed = parse_outcome(log)
        if parsed is not None:
            verdict = parsed.message
        for match in NOTICE.finditer(log):
            text = match.group("text").strip()
            if text and text not in notices:
                notices.append(text)
        if string(job, "conclusion") in ("failure", "timed_out", "startup_failure"):
            for reason in failure_reasons(tracker.annotations(job_id, final=done), log):
                if reason not in errors:
                    errors.append(reason)
    return RunOutcome(
        run_id=run_id,
        url=string(run, "html_url"),
        title=string(run, "display_title"),
        workflow=string(run, "name"),
        status=string(run, "status"),
        conclusion=string(run, "conclusion"),
        outcome=recorded,
        verdict=verdict,
        notices=tuple(notices[:MAX_NOTICES]),
        errors=tuple(errors),
        failed_job=failed_job,
        failed_step=failed_step,
        jobs=tuple(
            JobView(
                job_name(job),
                string(job, "html_url"),
                string(job, "status"),
                string(job, "conclusion"),
            )
            for job in jobs
        ),
    )


def transient(error: GitHubError) -> bool:
    """A dropped or garbled answer is retried; a refusal, a limit or a missing run is not."""
    return not isinstance(error, (GitHubAuthError, GitHubNotFoundError, GitHubRateLimitError))


def read_retrying(github: GitHub, clock: Clock, run_id: int, *, retries: int = 5) -> RunOutcome:
    for attempt in range(retries + 1):
        try:
            return read(github, run_id)
        except GitHubError as exc:
            if not transient(exc) or attempt == retries:
                raise
            clock.sleep(5.0 * (attempt + 1))
    raise AssertionError("unreachable")


def wait_until_done(
    github: GitHub,
    clock: Clock,
    run_id: int,
    *,
    interval: float = 30.0,
    timeout: float = 3600.0,
    retries: int = 5,
) -> tuple[Obj, int]:
    """Poll the run until it completes; returns it and how many reads were retried."""
    tracker = Tracker(github, clock)
    deadline = clock.now().timestamp() + timeout
    failures = retried = 0
    while True:
        try:
            run = tracker.run(run_id)
            failures = 0
        except GitHubError as exc:
            if not transient(exc) or failures >= retries:
                raise
            failures += 1
            retried += 1
            clock.sleep(min(interval, 5.0 * failures))
            continue
        if string(run, "status") == "completed":
            return run, retried
        if timeout and clock.now().timestamp() >= deadline:
            return run, retried
        clock.sleep(interval)
