"""Did the requested work actually happen? One answer per scheduler request.

A worker's own verdict is not enough: a merge-conflict worker can finish
"without changes" while the pull request is still conflicting. For every
request this reads what the scheduler decided, what the worker reported, and
the target's state now, and judges whether the request's goal is met.
"""

from __future__ import annotations

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
from rotisserie.factory_console.tracker import (
    DISPATCH_WORKFLOW,
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
    goal: str  # met | not-met | pending | not-started | unknown
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


def _follow(github: GitHub, tracker: Tracker, run: Obj) -> RequestOutcome:
    run_id = integer(run, "id") or 0
    target = TITLE.match(string(run, "display_title"))
    ref = target.group("target") if target else ""
    requested = timestamp(run, "created_at")

    def early(decision: str, reason: str, goal: str, text: str) -> RequestOutcome:
        return RequestOutcome(
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

    if string(run, "status") != "completed":
        return early("running", "", "pending", "the scheduler has not decided yet")
    decision = tracker.dispatch_outcome(run_id, tracker.jobs(run_id))
    assignment = decision.assignment
    if decision.status != "dispatched" or assignment is None:
        return early(decision.status, decision.reason, "not-started", "no worker was started")
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
        if worker is not None and worker.result in (Result.FAILED, Result.UNKNOWN):
            # The state may hold now through later work; this worker did not get it there.
            later = " (it holds now through later work)" if goal == "met" else ""
            goal, goal_text = "not-met", f"the worker failed: {worker.message}{later}"
    return RequestOutcome(
        scheduler_run=run_id,
        requested_at=requested,
        target=ref,
        decision="dispatched",
        reason=decision.reason,
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
        found = list(pool.map(lambda run: _follow(github, tracker, run), runs[:limit]))
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
