"""Follow one factory request from submission to recorded outcome.

Everything shown is read from real GitHub signals: workflow run and job
status, step status and timestamps, check-run annotations, and the trusted
result lines the factory itself prints. Nothing is estimated. The phrases are
written for the operator, not in GitHub Actions or factory-internal terms.

Interpretation (``read_*`` functions) is pure so it can be tested against
recorded API payloads; :class:`Tracker` adds polling and timing.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    items,
    obj,
    string,
    timestamp,
)

DISPATCH_WORKFLOW = "fixed-model-factory-dispatch.yml"
WORKER_WORKFLOW = "free-model-factory-entry.yml"
RESULT_STATUSES = ("dispatched", "deferred", "busy")
OUTCOME_LINE = re.compile(r"\[factory-outcome\] (?P<verdict>[a-z_]+): (?P<message>.+)$", re.M)
TRACEBACK_ERROR = re.compile(
    r"^(?:\S+ )?(?:[A-Za-z_.]+(?:Error|Exception|Unavailable)): (.+)$", re.M
)
GENERIC_ANNOTATION = re.compile(r"^Process completed with exit code \d+\.?$")


class Stage(StrEnum):
    """Where a request is, in the order it normally moves."""

    SUBMITTED = "submitted"
    QUEUED = "queued"  # behind earlier factory requests
    WAITING_MACHINE = "waiting_machine"  # GitHub has no free machine yet
    PREPARING = "preparing"
    READING_WORK = "reading_work"
    CLAIMING = "claiming"
    ASSIGNED = "assigned"
    WORKER_STARTING = "worker_starting"
    WORKING = "working"  # the model session really started
    FINISHING = "finishing"
    DONE = "done"


class Result(StrEnum):
    PENDING = "pending"
    STARTED = "started"  # the model is working (default stopping point)
    DELIVERED = "delivered"  # a verified PR was handed off
    COMPLETED = "completed"  # finished without needing a PR (e.g. a review)
    IDLE = "idle"  # nothing useful to do
    NO_WORK = "no_work"  # nothing was assigned
    BUSY = "busy"  # the chosen item belongs to another worker
    FAILED = "failed"
    UNKNOWN = "unknown"  # finished but the result could not be read
    SUBMITTED = "submitted"  # --no-wait


EXIT_CODES = {
    Result.SUBMITTED: 0,
    Result.STARTED: 0,
    Result.DELIVERED: 0,
    Result.COMPLETED: 0,
    Result.IDLE: 0,
    Result.NO_WORK: 3,
    Result.BUSY: 3,
    Result.FAILED: 4,
    Result.UNKNOWN: 6,
    Result.PENDING: 6,
}


@dataclass
class Assignment:
    kind: str
    number: int
    worker: str
    action: str
    provider: str
    model: str
    forced: bool = False
    dispatched_at: datetime | None = None


@dataclass
class DispatchOutcome:
    """What the scheduler decided, read from its trusted result line."""

    status: str  # dispatched | deferred | busy | failed | unknown
    assignment: Assignment | None = None
    reason: str = ""
    details: list[str] = field(default_factory=list)
    owner_worker: str = ""
    busy_number: int | None = None
    blocker: str = ""


@dataclass
class View:
    stage: Stage
    text: str
    since: datetime | None = None
    finished: bool = False
    failed: bool = False


# Plain-language names for the scheduler's steps --------------------------

DISPATCH_STEPS: tuple[tuple[str, Stage, str], ...] = (
    (
        "Gate factory dispatch on Actions REST quota",
        Stage.PREPARING,
        "Checking GitHub API allowance",
    ),
    (
        "Configure shared Neon factory discovery cache",
        Stage.PREPARING,
        "Connecting to the claim database",
    ),
    ("Initialize Neon operational state", Stage.PREPARING, "Connecting to the claim database"),
    (
        "Capture scheduling inventory once",
        Stage.READING_WORK,
        "Reading open issues and pull requests",
    ),
    ("Advance selected issue or pull request", Stage.CLAIMING, "Choosing work and claiming it"),
)

WORKER_STEPS: tuple[tuple[str, Stage, str], ...] = (
    (
        "Verify original active claim and live target",
        Stage.WORKER_STARTING,
        "Worker is double-checking its claim",
    ),
    ("Exchange GitHub OIDC for OpenCode Go", Stage.WORKER_STARTING, "Signing in to OpenCode Go"),
    ("Install current OpenCode", Stage.WORKER_STARTING, "Installing the coding tools"),
    ("Install pinned Kilo", Stage.WORKER_STARTING, "Installing the coding tools"),
    ("Select execution candidate", Stage.WORKER_STARTING, "Checking the model is available"),
    (
        "Configure OpenAI-compatible factory provider",
        Stage.WORKER_STARTING,
        "Connecting to the provider",
    ),
    ("Run continuous fixed-model factory session", Stage.WORKING, "Model is working"),
)
SESSION_STEP = "Run continuous fixed-model factory session"


def _steps(job: Obj) -> list[Obj]:
    return items(job, "steps")


def _active_step(
    jobs: list[Obj], table: tuple[tuple[str, Stage, str], ...]
) -> tuple[Stage, str, datetime | None] | None:
    """The latest step from ``table`` that is running or finished."""
    found: tuple[Stage, str, datetime | None] | None = None
    for job in jobs:
        for step in _steps(job):
            name = string(step, "name")
            status = string(step, "status")
            if (
                status not in ("in_progress", "completed")
                or string(step, "conclusion") == "skipped"
            ):
                continue
            for prefix, stage, text in table:
                if name.startswith(prefix) and (
                    found is None or list(Stage).index(stage) >= list(Stage).index(found[0])
                ):
                    found = (stage, text, timestamp(step, "started_at"))
    return found


def read_dispatcher(run: Obj, jobs: list[Obj]) -> View:
    status = string(run, "status")
    created = timestamp(run, "created_at")
    if status in ("queued", "pending", "waiting", "requested") and not jobs:
        return View(Stage.QUEUED, "Waiting for earlier factory requests to finish", created)
    dispatch = next(
        (job for job in jobs if string(job, "name") == "dispatch"), jobs[0] if jobs else {}
    )
    job_status = string(dispatch, "status")
    if job_status in ("queued", "waiting", "pending", ""):
        if status == "completed":
            return View(
                Stage.DONE,
                "Scheduler finished",
                timestamp(run, "updated_at"),
                True,
                string(run, "conclusion") not in ("success", ""),
            )
        return View(
            Stage.WAITING_MACHINE,
            "Waiting for GitHub to start a machine for the scheduler",
            created,
        )
    if job_status == "completed" or status == "completed":
        conclusion = string(dispatch, "conclusion") or string(run, "conclusion")
        return View(
            Stage.DONE,
            "Scheduler finished",
            timestamp(dispatch, "completed_at"),
            True,
            conclusion not in ("success", "skipped"),
        )
    active = _active_step([dispatch], DISPATCH_STEPS)
    if active is None:
        return View(
            Stage.PREPARING, "Scheduler is getting ready", timestamp(dispatch, "started_at")
        )
    return View(active[0], active[1], active[2])


def read_worker(run: Obj, jobs: list[Obj], number: int | None = None) -> View:
    status = string(run, "status")
    target = f" on #{number}" if number else ""
    if status != "completed" and not any(
        string(job, "status") not in ("queued", "waiting", "pending") for job in jobs
    ):
        return View(
            Stage.ASSIGNED,
            "Waiting for GitHub to start a machine for the worker",
            timestamp(run, "created_at"),
        )
    session = _session_step(jobs)
    if session is not None:
        session_status = string(session, "status")
        conclusion = string(session, "conclusion")
        if session_status == "in_progress":
            return View(
                Stage.WORKING, f"Model is working{target}", timestamp(session, "started_at")
            )
        if session_status == "completed" and conclusion not in ("skipped", ""):
            if status == "completed":
                return View(
                    Stage.DONE,
                    "Worker finished",
                    timestamp(run, "updated_at"),
                    True,
                    string(run, "conclusion") != "success",
                )
            return View(
                Stage.FINISHING,
                "Model finished; recording the result",
                timestamp(session, "completed_at"),
            )
    if status == "completed":
        return View(
            Stage.DONE,
            "Worker stopped before the model started",
            timestamp(run, "updated_at"),
            True,
            True,
        )
    for job in jobs:
        name = string(job, "name")
        if "release" in name and string(job, "status") == "in_progress":
            return View(Stage.FINISHING, "Releasing the claim", timestamp(job, "started_at"))
        if (
            name.startswith("Persist trusted worker outcome")
            and string(job, "status") == "in_progress"
        ):
            return View(Stage.FINISHING, "Recording the result", timestamp(job, "started_at"))
    active = _active_step(jobs, WORKER_STEPS)
    if active is None:
        return View(Stage.WORKER_STARTING, "Worker is starting", timestamp(run, "run_started_at"))
    return View(active[0], active[1], active[2])


def _session_step(jobs: list[Obj]) -> Obj | None:
    for job in jobs:
        for step in _steps(job):
            if string(step, "name").startswith(SESSION_STEP):
                return step
    return None


def session_started(jobs: list[Obj]) -> datetime | None:
    """When the model session actually began, or None if it never did."""
    step = _session_step(jobs)
    if step is None or string(step, "conclusion") == "skipped":
        return None
    if string(step, "status") in ("in_progress", "completed"):
        return timestamp(step, "started_at")
    return None


def parse_result(log: str) -> DispatchOutcome | None:
    """Find the scheduler's trusted JSON decision anywhere in its log."""
    decoder = json.JSONDecoder()
    found: dict[str, object] | None = None
    for line in log.splitlines():
        start = line.find("{")
        while start != -1:
            try:
                value, _end = decoder.raw_decode(line[start:])
            except ValueError:
                start = line.find("{", start + 1)
                continue
            if isinstance(value, dict) and value.get("status") in RESULT_STATUSES:
                found = value
            break
    if found is None:
        return None
    status = str(found["status"])
    raw_details = found.get("details")
    details = (
        [item for item in raw_details if isinstance(item, str)]
        if isinstance(raw_details, list)
        else []
    )
    if status == "dispatched":
        try:
            number = int(str(found["number"]))
        except (KeyError, ValueError):
            return DispatchOutcome("unknown", reason="The scheduler's result did not name a task.")
        dispatched = found.get("dispatched_at")
        return DispatchOutcome(
            "dispatched",
            Assignment(
                kind=str(found.get("kind") or "issue"),
                number=number,
                worker=str(found.get("worker") or ""),
                action=str(found.get("action") or ""),
                provider=str(found.get("provider") or ""),
                model=str(found.get("model") or "auto"),
                forced=found.get("forced") is True,
                dispatched_at=datetime.fromtimestamp(dispatched, UTC)
                if isinstance(dispatched, int)
                else None,
            ),
        )
    number_value = found.get("number")
    return DispatchOutcome(
        status,
        reason=str(found.get("reason") or ""),
        details=details,
        owner_worker=str(found.get("owner_worker") or ""),
        busy_number=number_value if isinstance(number_value, int) else None,
        blocker=str(found.get("blocker") or ""),
    )


def failure_reasons(annotations: list[Obj], log: str = "") -> list[str]:
    """Specific error messages, without GitHub's generic exit-code line."""
    reasons: list[str] = []
    for note in annotations:
        if string(note, "annotation_level") != "failure":
            continue
        message = string(note, "message").strip()
        if message and not GENERIC_ANNOTATION.match(message) and message not in reasons:
            reasons.append(message)
    if not reasons and log:
        matches = TRACEBACK_ERROR.findall(log)
        if matches:
            reasons.append(matches[-1].strip())
    return reasons


@dataclass(frozen=True)
class WorkerOutcome:
    result: Result
    message: str
    pr: int | None = None


VERDICTS = {
    "code_delivered": Result.DELIVERED,
    "work_completed": Result.COMPLETED,
    "idle": Result.IDLE,
    "failed": Result.FAILED,
}


def parse_outcome(log: str) -> WorkerOutcome | None:
    match = None
    for match in OUTCOME_LINE.finditer(log):
        pass
    if match is None:
        return None
    message = match.group("message").strip()
    # "Verified PR #N was handed off" or "Code saved on the work PR; ... (work PR: N)".
    pr = re.search(r"PR #(\d+)|work PR: (\d+)", message)
    return WorkerOutcome(
        VERDICTS.get(match.group("verdict"), Result.UNKNOWN),
        message,
        int(pr.group(1) or pr.group(2)) if pr else None,
    )


# Polling -----------------------------------------------------------------


@dataclass
class Clock:
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = field(default=time.sleep)


class Tracker:
    """Reads one request's real GitHub state on demand."""

    def __init__(self, github: GitHub, clock: Clock | None = None) -> None:
        self.github = github
        self.clock = clock or Clock()

    def run(self, run_id: int) -> Obj:
        return obj(self.github.repo_api(f"actions/runs/{run_id}"))

    def jobs(self, run_id: int) -> list[Obj]:
        return items(self.github.repo_api(f"actions/runs/{run_id}/jobs?per_page=100"), "jobs")

    def annotations(self, job_id: int) -> list[Obj]:
        try:
            return items(self.github.repo_api(f"check-runs/{job_id}/annotations"))
        except GitHubError:
            return []

    def log(self, job_id: int) -> str:
        try:
            return self.github.text(f"actions/jobs/{job_id}/logs")
        except GitHubError:
            return ""

    def find_submitted_run(
        self, submitted_at: datetime, title: str, *, attempts: int = 20
    ) -> int | None:
        """Find the scheduler run GitHub created for our request."""
        since = (submitted_at - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _ in range(attempts):
            runs = items(
                self.github.repo_api(
                    f"actions/workflows/{DISPATCH_WORKFLOW}/runs?event=workflow_dispatch&created=%3E%3D{since}&per_page=30"
                ),
                "workflow_runs",
            )
            for run in sorted(runs, key=lambda item: string(item, "created_at")):
                if string(run, "display_title") == title:
                    run_id = integer(run, "id")
                    if run_id is not None:
                        return run_id
            self.clock.sleep(3)
        return None

    def find_worker_run(self, worker: str, after: datetime) -> int | None:
        since = (after - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        runs = items(
            self.github.repo_api(
                f"actions/workflows/{WORKER_WORKFLOW}/runs?event=workflow_dispatch&created=%3E%3D{since}&per_page=50"
            ),
            "workflow_runs",
        )
        title = re.compile(rf"^Factory {re.escape(worker)}(?: · [^·]+?)? · fixed-model entry$")
        matches = sorted(
            (run for run in runs if title.match(string(run, "display_title"))),
            key=lambda item: string(item, "created_at"),
        )
        return integer(matches[0], "id") if matches else None

    def dispatch_outcome(self, run_id: int, jobs: list[Obj]) -> DispatchOutcome:
        dispatch = next((job for job in jobs if string(job, "name") == "dispatch"), None)
        if dispatch is None:
            return DispatchOutcome("unknown", reason="The scheduler run has no scheduling job.")
        job_id = integer(dispatch, "id") or 0
        for step in _steps(dispatch):
            if (
                string(step, "name").startswith("Report quota-deferred")
                and string(step, "conclusion") == "success"
            ):
                return DispatchOutcome(
                    "deferred",
                    blocker="github_api",
                    reason=(
                        "GitHub's API allowance for the factory is below its safety "
                        "reserve, so nothing was started."
                    ),
                )
        log = self.log(job_id)
        result = parse_result(log)
        if result is not None:
            return result
        if string(dispatch, "conclusion") not in ("success", ""):
            reasons = failure_reasons(self.annotations(job_id), log)
            return DispatchOutcome(
                "failed",
                reason=reasons[0] if reasons else "The scheduler failed.",
                details=reasons[1:4],
            )
        return DispatchOutcome(
            "unknown", reason="The scheduler finished without recording a decision."
        )

    def worker_outcome(self, jobs: list[Obj]) -> WorkerOutcome:
        persist = next(
            (
                job
                for job in jobs
                if string(job, "name").startswith("Persist trusted worker outcome")
            ),
            None,
        )
        if persist is not None:
            outcome = parse_outcome(self.log(integer(persist, "id") or 0))
            if outcome is not None:
                return outcome
        reasons: list[str] = []
        for job in jobs:
            if string(job, "conclusion") == "failure":
                for reason in failure_reasons(self.annotations(integer(job, "id") or 0)):
                    if reason not in reasons:
                        reasons.append(reason)
        if reasons:
            return WorkerOutcome(Result.FAILED, "; ".join(reasons[:3]))
        return WorkerOutcome(
            Result.UNKNOWN, "The worker finished but its result could not be read."
        )

    def poll(self, interval: float) -> Iterator[datetime]:
        while True:
            yield self.clock.now()
            self.clock.sleep(interval)
