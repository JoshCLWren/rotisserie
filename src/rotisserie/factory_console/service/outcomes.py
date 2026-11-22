"""Did the requested work actually happen? One answer per scheduler request.

A worker's own verdict is not enough: a merge-conflict worker can finish
"without changes" while the pull request is still conflicting. For every
request this reads what the scheduler decided, what the worker reported, and
the target's state now, and judges whether the request's goal is met.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    items,
    label_names,
    obj,
    string,
    timestamp,
)
from rotisserie.factory_console.service import checks
from rotisserie.factory_console.tracker import (
    DISPATCH_WORKFLOW,
    Assignment,
    Clock,
    Result,
    Tracker,
    WorkerOutcome,
)

TITLE = re.compile(r"^Factory dispatch · (?P<target>(?:issue|pr):[1-9][0-9]*)$")

# What each action is for, as a check on the PR's labels and merge state now.
GOALS = {
    "merge-conflict": "the pull request no longer has merge conflicts",
    "ci-repair": "the pull request left the failing-checks stage",
    "review-findings": "the pull request left the changes-requested stage",
    "review": "the pull request has a review decision (ready or changes requested)",
    "implement": "a pull request was delivered",
    "advance": "the item moved forward",
}


@dataclass(frozen=True)
class RequestOutcome:
    """One scheduler request, followed through to its effect."""

    scheduler_run: int
    requested_at: datetime | None
    target: str
    decision: str  # dispatched | deferred | busy | failed | unknown | running
    reason: str
    action: str
    model: str
    worker: str
    worker_run: int | None
    worker_result: str  # Result value, "running", or "" before a worker exists
    worker_message: str
    goal: str  # met | not-met | pending | not-started | closed | unknown
    goal_text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _stage(labels: set[str]) -> str:
    for label in (
        "factory:blocked",
        "factory:ready",
        "factory:review",
        "factory:changes-requested",
        "factory:ci",
        "factory:building",
    ):
        if label in labels:
            return label
    return ""


def judge(action: str, kind: str, pr: Obj, worker: WorkerOutcome | None) -> tuple[str, str]:
    """Whether the action's goal holds for the target now: (goal, explanation)."""
    goal = GOALS.get(action, "the item moved forward")
    if kind == "issue":
        if worker is not None and worker.result is Result.DELIVERED:
            return "met", f"pull request #{worker.pr} was delivered"
        if worker is None:
            return "pending", goal
        return "not-met", f"no pull request was delivered ({worker.result})"
    if pr.get("merged") is True or string(pr, "state") == "closed":
        return "met", "the pull request was merged or closed"
    labels = label_names(pr)
    stage = _stage(labels)
    if action == "merge-conflict":
        state = string(pr, "mergeable_state")
        if pr.get("mergeable") is None or state == "unknown":
            return "pending", "GitHub is still computing whether it can merge"
        conflicted = pr.get("mergeable") is False or state == "dirty"
        return ("not-met", "it still has merge conflicts") if conflicted else ("met", goal)
    wanted_gone = {
        "ci-repair": "factory:ci",
        "review-findings": "factory:changes-requested",
        "review": "factory:review",
    }.get(action)
    if wanted_gone is None:
        return "unknown", f"no goal check for action {action!r}"
    if stage == wanted_gone:
        return "not-met", f"it is still {stage.removeprefix('factory:')}"
    return "met", f"{goal} (now {stage.removeprefix('factory:') or 'unlabeled'})"


EXCERPT_START = "=== factory fast check failure output ==="
EXCERPT_END = "=== end factory fast check failure output ==="
FAST_CHECK_LINE = re.compile(r"factory fast checks failed: (?P<what>.+?) \(log: ")


def validation_failure(tracker: Tracker, jobs: list[Obj]) -> str:
    """Which factory gate rejected the worker's change, from its session log.

    The detail is otherwise only in /tmp on the runner. Empty when not found.
    """
    session = next((job for job in jobs if string(job, "name").endswith("/ factory")), None)
    if session is None:
        return ""
    try:
        log = tracker.log(integer(session, "id") or 0)
    except GitHubError:
        return ""
    found = list(FAST_CHECK_LINE.finditer(log))
    if not found:
        return ""
    text = f"factory fast checks failed: {found[-1].group('what')}"
    # Workers publish each failed step's output between these markers.
    excerpt = (
        log.rsplit(EXCERPT_START, 1)[1].split(EXCERPT_END, 1)[0] if EXCERPT_START in log else ""
    )
    if excerpt:
        kind, summary, _ = checks.summarize_log(excerpt)
        if not summary.startswith("the check failed"):
            text += f" ({summary})"
        else:
            first = next(
                (line.strip() for line in excerpt.splitlines() if "error" in line.lower()), ""
            )
            if first:
                text += f" ({first[:200]})"
    return text


ASSIGNMENT_LINE = re.compile(
    r"^(?:\S+ )?Factory (?P<worker>[0-9]+) assignment: (?P<json>\{.*\})\s*$", re.M
)
STAGE_ACTIONS = {
    "factory:review": "review",
    "factory:ci": "ci-repair",
    "factory:changes-requested": "review-findings",
    "factory:building": "implement",
}


def routine_assignments(log: str, at: datetime | None) -> list[Assignment]:
    """Every worker a routine scheduler pass started (``Factory N assignment: {...}``)."""
    found = []
    for match in ASSIGNMENT_LINE.finditer(log):
        try:
            data = json.loads(match.group("json"))
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("kind") not in ("issue", "pr"):
            continue
        number = data.get("number")
        if not isinstance(number, int):
            continue
        stage = str(data.get("stage") or "")
        action = STAGE_ACTIONS.get(stage, "implement" if data["kind"] == "issue" else "advance")
        found.append(
            Assignment(
                str(data["kind"]), number, match.group("worker"), action, "", "",
                dispatched_at=at,
            )
        )  # fmt: skip
    return found


def _follow(github: GitHub, tracker: Tracker, run: Obj) -> list[RequestOutcome]:
    run_id = integer(run, "id") or 0
    target = TITLE.match(string(run, "display_title"))
    ref = target.group("target") if target else ""
    requested = timestamp(run, "created_at")

    def early(decision: str, reason: str, goal: str, text: str) -> list[RequestOutcome]:
        return [
            RequestOutcome(
                scheduler_run=run_id,
                requested_at=requested,
                target=ref,
                decision=decision,
                reason=reason,
                action="",
                model="",
                worker="",
                worker_run=None,
                worker_result="",
                worker_message="",
                goal=goal,
                goal_text=text,
            )
        ]

    if string(run, "status") != "completed":
        return early("running", "", "pending", "the scheduler has not decided yet")
    if string(run, "conclusion") == "cancelled":
        return early("cancelled", "a newer scheduler pass replaced it", "not-started", "")
    jobs = tracker.jobs(run_id)
    if target is None:
        dispatch = next((job for job in jobs if string(job, "name") == "dispatch"), None)
        log = tracker.log(integer(dispatch, "id") or 0) if dispatch else ""
        started = routine_assignments(log, requested)
        if not started:
            return early("no_work", "the pass assigned no worker", "not-started", "")
        return [
            _follow_assignment(github, tracker, run_id, requested, "", item, "") for item in started
        ]
    decision = tracker.dispatch_outcome(run_id, jobs)
    assignment = decision.assignment
    if decision.status != "dispatched" or assignment is None:
        return early(decision.status, decision.reason, "not-started", "no worker was started")
    return [
        _follow_assignment(github, tracker, run_id, requested, ref, assignment, decision.reason)
    ]


def _follow_assignment(
    github: GitHub,
    tracker: Tracker,
    run_id: int,
    requested: datetime | None,
    ref: str,
    assignment: Assignment,
    reason: str,
) -> RequestOutcome:
    action = assignment.action
    ref = ref or f"{assignment.kind}:{assignment.number}"
    after = assignment.dispatched_at or requested or datetime.now(UTC)
    worker_run = tracker.find_worker_run(assignment.worker, after)
    worker: WorkerOutcome | None = None
    worker_result = ""
    if worker_run is not None:
        jobs = tracker.jobs(worker_run)
        if string(tracker.run(worker_run), "status") == "completed":
            worker = tracker.worker_outcome(jobs)
            worker_result = str(worker.result)
            if worker.result is Result.FAILED and "validation" in worker.message:
                gate = validation_failure(tracker, jobs)
                if gate:
                    worker = WorkerOutcome(worker.result, f"{worker.message}; {gate}", worker.pr)
        else:
            worker_result = "running"
    if worker is None and worker_run is not None:
        goal, goal_text = "pending", "the worker is still running"
    else:
        pr: Obj = {}
        if assignment.kind == "pr":
            try:
                pr = obj(github.repo_api(f"pulls/{assignment.number}"))
            except GitHubError:
                pr = {}
        goal, goal_text = judge(action, assignment.kind, pr, worker)
        if action == "ci-repair" and assignment.kind == "pr" and goal == "met" and pr:
            # The goal is green checks, not a stage label.
            try:
                red = checks.failing_checks(github, assignment.number, read_logs=False)
            except GitHubError:
                red = []
            if red:
                names = ", ".join(item.name for item in red[:3])
                goal, goal_text = "not-met", f"its checks still fail: {names}"
        if pr.get("merged") is not True and string(pr, "state") == "closed":
            # Closed by a person: the request was overtaken, not missed.
            goal, goal_text = "closed", "the pull request was closed"
        elif worker is not None and worker.result in (Result.FAILED, Result.UNKNOWN):
            # The state may hold now through later work; this worker did not get it there.
            later = " (it holds now through later work)" if goal == "met" else ""
            goal, goal_text = "not-met", f"the worker failed: {worker.message}{later}"
    return RequestOutcome(
        scheduler_run=run_id,
        requested_at=requested,
        target=ref,
        decision="dispatched",
        reason=reason,
        action=action,
        model=assignment.model,
        worker=assignment.worker,
        worker_run=worker_run,
        worker_result=worker_result,
        worker_message=worker.message if worker else "",
        goal=goal,
        goal_text=goal_text,
    )


def request_outcomes(
    github: GitHub,
    *,
    since: datetime,
    clock: Clock | None = None,
    targeted_only: bool = True,
    limit: int = 40,
) -> list[RequestOutcome]:
    """Scheduler requests since ``since``, newest first, each followed to its effect.

    ``targeted_only`` keeps requests for a chosen issue or PR (``run --target``,
    ``unstick``); otherwise routine scheduler passes are included too.
    """
    stamp = (since - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = items(
        github.repo_api(
            f"actions/workflows/{DISPATCH_WORKFLOW}/runs?created=%3E%3D{stamp}&per_page=100"
        ),
        "workflow_runs",
    )
    if targeted_only:
        runs = [run for run in runs if TITLE.match(string(run, "display_title"))]
    tracker = Tracker(github, clock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [
            item
            for batch in pool.map(lambda run: _follow(github, tracker, run), runs[:limit])
            for item in batch
        ]
    return sorted(
        found, key=lambda item: item.requested_at or datetime.fromtimestamp(0, UTC), reverse=True
    )


def latest_misses(found: list[RequestOutcome]) -> list[RequestOutcome]:
    """Targets whose most recent request did not reach its goal (``found`` newest first)."""
    seen: set[str] = set()
    misses = []
    for item in found:
        key = item.target or f"run:{item.scheduler_run}"
        if item.goal == "not-started" or key in seen:
            continue
        seen.add(key)
        if item.goal == "not-met":
            misses.append(item)
    return misses


@dataclass(frozen=True)
class ItemHistory:
    """Every started request on one item: is it converging or going round in circles?"""

    target: str
    rounds: int  # workers started on it
    missed: int  # rounds whose goal did not hold (failed, idle with the problem left, ...)
    running: int
    actions: tuple[str, ...]  # newest first
    latest: str  # latest round's worker result
    latest_goal: str

    @property
    def churning(self) -> bool:
        """Two or more misses with nothing done: more of the same is unlikely to help."""
        return self.missed >= 2 and self.latest_goal != "met"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["churning"] = self.churning
        return data


def by_item(found: list[RequestOutcome]) -> list[ItemHistory]:
    """Group started requests (newest first) per item, the most-missed items first."""
    groups: dict[str, list[RequestOutcome]] = {}
    for item in found:
        if item.decision == "dispatched" and item.target:
            groups.setdefault(item.target, []).append(item)
    histories = [
        ItemHistory(
            target,
            len(rows),
            sum(1 for row in rows if row.goal == "not-met"),
            sum(1 for row in rows if row.worker_result == "running"),
            tuple(row.action for row in rows),
            rows[0].worker_result,
            rows[0].goal,
        )
        for target, rows in groups.items()
    ]
    return sorted(histories, key=lambda item: (-item.missed, -item.rounds, item.target))
