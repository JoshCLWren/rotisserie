from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import ACTION_WORDS, Outcome, Request
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    items,
    obj,
    string,
    timestamp,
)
from rotisserie.factory_console.tracker import (
    DISPATCH_WORKFLOW,
    EXIT_CODES,
    WORKER_WORKFLOW,
    Assignment,
    DispatchOutcome,
    Result,
    Stage,
    Tracker,
    View,
    WorkerOutcome,
    read_dispatcher,
    read_worker,
    session_started,
)
from rotisserie.factory_console.ui import Console, duration, local_time, since
from rotisserie.factory_console.work import open_items


def _watch_scheduler(
    context: Context,
    tracker: Tracker,
    run_id: int,
    started: datetime,
    data: dict[str, object],
    *,
    follow: bool,
    request: Request | None = None,
    decision_only: bool = False,
) -> Outcome:
    console = context.console
    shown: set[Stage] = set()
    last = View(Stage.SUBMITTED, "", started)
    jobs: list[Obj] = []  # poll yields at least once; initialized for the type checker
    for now in tracker.poll(4):
        run = tracker.run(run_id)
        jobs = tracker.jobs(run_id)
        view = read_dispatcher(run, jobs)
        if view.stage != last.stage and view.stage not in shown and not view.finished:
            console.note(view.text)
            shown.add(view.stage)
        last = view
        if view.finished:
            break
        console.waiting(
            f"{view.text} ({duration((now - (view.since or started)).total_seconds())})"
        )
    console.finish()
    outcome = tracker.dispatch_outcome(run_id, jobs)
    data["scheduler"] = asdict(outcome)
    return _report_dispatch(
        context, tracker, outcome, data, follow=follow, request=request, decision_only=decision_only
    )


def _report_dispatch(
    context: Context,
    tracker: Tracker,
    outcome: DispatchOutcome,
    data: dict[str, object],
    *,
    follow: bool,
    request: Request | None,
    decision_only: bool = False,
) -> Outcome:
    console = context.console
    scheduler_url = str(data.get("scheduler_url", ""))
    if outcome.status == "dispatched" and outcome.assignment is not None:
        assignment = outcome.assignment
        title = _title(context.github, assignment.number)
        label = "Issue" if assignment.kind == "issue" else "Pull request"
        console.ok(f"Claimed {label.lower()} #{assignment.number}", title)
        provider = runtime.provider_for_worker(assignment.worker)
        slot = runtime.worker_slot_number(assignment.worker)
        who = provider.name if provider else assignment.provider
        model = "model chosen at start" if assignment.model == "auto" else assignment.model
        extra = f"worker {slot}" + (
            " (extra worker for forced work)"
            if slot and slot > runtime.regular_slot_count()
            else ""
        )
        console.ok(f"Assigned to {who}: {model}", extra)
        console.note(f"Task: {ACTION_WORDS.get(assignment.action, assignment.action)}")
        data["target_ref"] = f"{assignment.kind}:{assignment.number}"
        data["assignment"] = asdict(assignment)
        if decision_only:
            console.link("Scheduler", scheduler_url)
            console.note(f"Follow the worker: baste watch {data['scheduler_run_id']}")
            return Outcome(Result.SUBMITTED, EXIT_CODES[Result.SUBMITTED], data)
        return _watch_worker(context, tracker, assignment, data, follow=follow)
    console.line()
    if outcome.status == "busy":
        holder = runtime.provider_for_worker(outcome.owner_worker)
        number = outcome.busy_number
        console.fail(
            f"#{number} is already being worked on",
            f"by {holder.name if holder else 'another worker'}",
        )
        if request is not None:
            target = request.target or f"issue:{number}"
            again = f"baste run {request.provider.key} {request.model} --target {target}"
            console.line(f"  Take it over: {again} --take-over")
        console.line(f"  Stop it:      baste cancel issue:{number}")
        console.link("Scheduler", scheduler_url)
        return Outcome(Result.BUSY, EXIT_CODES[Result.BUSY], data)
    if outcome.status == "deferred":
        console.fail("No work assigned")
        console.line()
        console.line(f"Reason: {_reason(outcome.reason)}")
        for detail in outcome.details[:4]:
            if detail != outcome.reason:
                console.line(f"  {console.dot} {detail}")
        if request is not None and not request.force and not outcome.blocker:
            console.line()
            console.line(
                "Add --force to ignore the factory's own limits "
                "(queue size, cooldowns, worker counts)."
            )
        explain_target = (
            request.target if request is not None and request.target.startswith("issue:") else ""
        )
        console.line(f"See why: baste why {explain_target}".rstrip())
        console.link("Scheduler", scheduler_url)
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    if outcome.status == "failed":
        console.fail("The scheduler failed")
        console.line(f"Reason: {outcome.reason}")
        for detail in outcome.details:
            console.line(f"  {console.dot} {detail}")
        console.link("Scheduler", scheduler_url)
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    console.fail("The scheduler finished, but its decision could not be read")
    console.line(f"Reason: {outcome.reason}")
    console.link("Scheduler", scheduler_url)
    return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], data)


def _reason(text: str) -> str:
    replacements = {
        "store lease": "claim",
        "lease": "claim",
        "slot": "worker",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _title(github: GitHub, number: int) -> str:
    try:
        return string(obj(github.repo_api(f"issues/{number}")), "title")
    except GitHubError:
        return ""


def _watch_worker(
    context: Context,
    tracker: Tracker,
    assignment: Assignment,
    data: dict[str, object],
    *,
    follow: bool,
) -> Outcome:
    console = context.console
    after = assignment.dispatched_at or context.clock.now() - timedelta(minutes=3)
    run_id: int | None = None
    for attempt, now in enumerate(tracker.poll(5)):
        run_id = tracker.find_worker_run(assignment.worker, after)
        if run_id is not None or attempt >= 24:
            break
        console.waiting(
            f"Waiting for GitHub to create the worker ({duration((now - after).total_seconds())})"
        )
    console.finish()
    if run_id is None:
        console.fail("The worker run did not appear on GitHub within two minutes")
        console.link("Worker runs", context.url(f"/actions/workflows/{WORKER_WORKFLOW}"))
        return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], data)
    data["worker_run_id"] = run_id
    data["worker_url"] = context.url(f"/actions/runs/{run_id}")
    shown: set[str] = set()
    started_at: datetime | None = None
    jobs: list[Obj] = []
    run: Obj = {}  # poll yields at least once; initialized for the type checker
    for now in tracker.poll(10 if not follow else 15):
        run = tracker.run(run_id)
        jobs = tracker.jobs(run_id)
        view = read_worker(run, jobs, assignment.number)
        if view.text not in shown and view.stage not in (Stage.WORKING, Stage.DONE):
            console.note(view.text)
            shown.add(view.text)
        if view.stage == Stage.WORKING and started_at is None:
            started_at = view.since or now
            console.ok(f"Model started working on #{assignment.number}", local_time(started_at))
            data["coding_started_at"] = started_at.isoformat()
            if not follow:
                console.line()
                console.line(
                    f"Working on: #{assignment.number}  {_title(context.github, assignment.number)}"
                )
                console.link("Worker", str(data["worker_url"]))
                console.link(
                    "Issue" if assignment.kind == "issue" else "Pull request",
                    context.url(
                        f"/{'issues' if assignment.kind == 'issue' else 'pull'}/{assignment.number}"
                    ),
                )
                console.note(f"Keep watching: baste watch {run_id} --follow")
                return Outcome(Result.STARTED, EXIT_CODES[Result.STARTED], data)
        if view.finished:
            break
        anchor = started_at or view.since or now
        console.waiting(f"{view.text} ({duration((now - anchor).total_seconds())})")
    console.finish()
    outcome = tracker.worker_outcome(jobs)
    data["worker_outcome"] = {
        "result": str(outcome.result),
        "message": outcome.message,
        "pr": outcome.pr,
    }
    if outcome.result is Result.NO_CAPACITY:
        return show_no_capacity(console, outcome, data)
    if started_at is None and session_started(jobs) is None:
        console.fail("The model never started", "the claim was released")
        console.line(f"Reason: {outcome.message}")
        console.link("Worker", str(data["worker_url"]))
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    finished = timestamp(run, "updated_at")
    took = f"after {since(started_at, finished)}" if started_at and finished else ""
    if outcome.result is Result.DELIVERED:
        console.ok(f"Pull request #{outcome.pr} delivered", took)
    elif outcome.result is Result.COMPLETED:
        console.ok("Finished (no pull request needed)", took)
    elif outcome.result is Result.IDLE:
        console.warn("Finished without changes: the model found nothing useful to do", took)
    elif outcome.result is Result.FAILED:
        console.fail("The model did not finish the work", took)
        console.line(f"Reason: {outcome.message}")
    else:
        console.warn("Finished, but the result could not be read", took)
    pr = outcome.pr or (assignment.number if assignment.kind == "pr" else None)
    if pr is not None:
        data["pull_request"] = _show_pull_request(context, pr)
    console.link("Worker", str(data["worker_url"]))
    return Outcome(outcome.result, EXIT_CODES[outcome.result], data)


def show_no_capacity(console: Console, outcome: WorkerOutcome, data: dict[str, object]) -> Outcome:
    console.warn("No capacity: no eligible model was free; the item stays queued")
    console.line(f"Reason: {outcome.message.removeprefix('No capacity: ')}")
    console.link("Worker", str(data["worker_url"]))
    return Outcome(Result.NO_CAPACITY, EXIT_CODES[Result.NO_CAPACITY], data)


def _show_pull_request(context: Context, number: int) -> dict[str, object]:
    """Checks, review and merge state; an open PR is not finished work."""
    console = context.console
    try:
        raw = context.github.gh(
            "pr",
            "view",
            str(number),
            "--repo",
            context.repo,
            "--json",
            "state,mergedAt,reviewDecision,statusCheckRollup,url,title,isDraft",
        )
        view = obj(json.loads(raw))
    except GitHubError, ValueError:
        console.warn(f"Could not read pull request #{number}")
        return {"number": number}
    checks = items(view, "statusCheckRollup")
    states = Counter(
        (string(check, "conclusion") or string(check, "state") or string(check, "status")).upper()
        for check in checks
    )
    failing = sum(
        states[name] for name in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE")
    )
    pending = sum(
        states[name] for name in ("", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING")
    )
    state = string(view, "state")
    review = string(view, "reviewDecision")
    console.line()
    console.line(f"Pull request #{number}: {string(view, 'title')}")
    if state == "MERGED":
        console.ok("Merged", local_time(timestamp(view, "mergedAt")))
    elif state == "CLOSED":
        console.fail("Closed without merging")
    else:
        if failing:
            console.fail(f"{failing} check(s) failing")
        elif pending:
            console.note(f"{pending} check(s) still running")
        elif checks:
            console.ok("All checks passed")
        console.note(
            {
                "APPROVED": "Approved",
                "CHANGES_REQUESTED": "Changes requested",
                "REVIEW_REQUIRED": "Waiting for review",
            }.get(review, "No review yet")
        )
        console.note("Not merged yet")
    console.link("Pull request", string(view, "url"))
    return {
        "number": number,
        "state": state,
        "review": review,
        "failing_checks": failing,
        "pending_checks": pending,
        "url": string(view, "url"),
    }


def cmd_watch(context: Context, arguments: argparse.Namespace) -> Outcome:
    match = re.search(r"(\d{6,})\s*/?$", arguments.run)
    if match is None:
        raise UsageError("Give a run ID or a GitHub Actions run URL.")
    run_id = int(match.group(1))
    tracker = Tracker(context.github, context.clock)
    run = tracker.run(run_id)
    path = string(run, "path")
    data = context.progress
    created = timestamp(run, "created_at") or context.clock.now()
    if path.endswith(DISPATCH_WORKFLOW):
        data["scheduler_run_id"] = run_id
        data["scheduler_url"] = context.url(f"/actions/runs/{run_id}")
        context.console.heading("Factory request")
        context.console.ok("Request sent", local_time(created))
        return _watch_scheduler(
            context,
            tracker,
            run_id,
            created,
            data,
            follow=arguments.follow,
            decision_only=arguments.decision,
        )
    if path.endswith(WORKER_WORKFLOW):
        title = re.match(r"^Factory (\d+) ", string(run, "display_title"))
        worker = title.group(1) if title else ""
        context.console.heading("Factory worker")
        held = [item for item in open_items(context.github) if item.owner == worker]
        number = held[0].number if held else 0
        kind = held[0].kind if held else "issue"
        assignment = Assignment(kind, number, worker, "advance", "", "auto", dispatched_at=created)
        if number:
            context.console.note(f"Working on #{number}: {held[0].title}")
        return _watch_worker(context, tracker, assignment, data, follow=arguments.follow)
    raise UsageError("That run is not a factory scheduler or worker run.")
