"""``rotisserie factory`` — the operator console for the software factory.

Every command reads real factory state; nothing is estimated. Human output is
the default. ``--json`` prints one machine-readable document. Exit codes:

  0  success (request accepted / worker started / work finished)
  2  the command was used incorrectly
  3  no work was assigned (nothing eligible, or the item is held by another worker)
  4  something failed (the scheduler, the worker, or the model)
  5  GitHub could not be reached, or the login lacks access
  6  the outcome could not be confirmed
  130  you stopped watching (the factory keeps working)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from rotisserie.factory_console import runtime
from rotisserie.factory_console.context import Context, UsageError, find_repository
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    Obj,
    integer,
    items,
    obj,
    string,
    timestamp,
)
from rotisserie.factory_console.service import (
    catalog,
    checks,
    close,
    dispatch,
    outcomes,
    providers,
    queue,
    reliability,
    unstick,
)
from rotisserie.factory_console.tracker import (
    DISPATCH_WORKFLOW,
    EXIT_CODES,
    WORKER_WORKFLOW,
    Assignment,
    Clock,
    DispatchOutcome,
    Result,
    Stage,
    Tracker,
    View,
    read_dispatcher,
    read_worker,
    session_started,
)
from rotisserie.factory_console.ui import Console, Prompt, duration, local_time, since
from rotisserie.factory_console.work import (
    Item,
    current_work,
    lookup,
    one_per_worker,
    open_items,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
TARGET = re.compile(r"^(issue|pr):([1-9][0-9]*)$")
ACTIONS = ("advance", "implement", "merge-conflict", "ci-repair", "review-findings", "review")
ACTION_WORDS = {
    "implement": "write the change",
    "review": "review the pull request",
    "review-findings": "address review feedback",
    "ci-repair": "fix failing checks",
    "merge-conflict": "resolve merge conflicts",
    "advance": "do whatever the item needs next",
}
STATS_BRANCH = "factory-status-live"
PRIORITY_LABELS = (
    "ralph-priority:critical",
    "ralph-priority:high",
    "ralph-priority:medium",
    "ralph-priority:low",
)
OUTCOME_WORDS = {
    "provider_quota": "provider out of quota",
    "provider_failure": "provider error",
    "model_retired_410": "model retired by provider",
    "control_plane_failure": "factory setup problem",
    "work_failure": "model could not finish the work",
    "no_progress": "model made no progress",
    "timeout": "ran out of time",
    "agent_timeout": "ran out of time",
    "unknown_failure": "unclassified failure",
    "unconfigured": "provider not configured",
}
FORCE_EXPLANATION = (
    "Ignores the factory's own limits (open-PR and review limits, cooldowns, "
    "health history, budgets, pauses, worker counts). It still cannot take "
    "work another worker holds; stop or take over that worker instead."
)


# Argument parsing ------------------------------------------------------------


def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    factory = subcommands.add_parser(
        "factory",
        help="operate the software factory: start work, watch it, see what happened",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    factory.add_argument("--repo", help="repository running the factory (owner/name)")
    factory.add_argument(
        "--json", action="store_true", help="print one JSON document, no progress text"
    )
    factory.add_argument("--verbose", action="store_true", help="also show raw diagnostic detail")
    commands = factory.add_subparsers(dest="factory_command", metavar="COMMAND")

    run = commands.add_parser(
        "run",
        help="start a worker on the next task or a chosen issue/PR",
        description=(
            "Start a factory worker. With no arguments, menus ask for the provider, "
            "model and work. With PROVIDER and MODEL given, it starts immediately."
        ),
    )
    run.add_argument("provider", nargs="?", help="factory provider, e.g. opencode-go")
    run.add_argument("model", nargs="?", help="exact model ID (provider/model) or auto")
    run.add_argument(
        "--force",
        "--force-slot",
        dest="force",
        action="store_true",
        help="override every factory limit; see 'Override' below",
    )
    run.add_argument(
        "--slot", type=int, help="use worker number N for this provider (implies --force)"
    )
    run.add_argument("--target", help="work on issue:N or pr:N instead of the next task")
    run.add_argument(
        "--action",
        choices=ACTIONS,
        default="advance",
        help="what to do on a chosen PR (with --force and --target)",
    )
    run.add_argument(
        "--take-over",
        action="store_true",
        help="if another worker holds the target, stop it and take over (implies --force)",
    )
    wait = run.add_mutually_exclusive_group()
    wait.add_argument(
        "--no-wait",
        "--detach",
        dest="no_wait",
        action="store_true",
        help="submit and return immediately",
    )
    wait.add_argument(
        "--follow",
        action="store_true",
        help="keep watching until the worker finishes and the PR is checked",
    )
    run.add_argument("--yes", "-y", action="store_true", help="skip the confirmation question")
    _common(run)

    watch = commands.add_parser("watch", help="resume watching a request (run ID or URL)")
    watch.add_argument("run", help="scheduler or worker run ID or URL")
    watch.add_argument("--follow", action="store_true", help="watch until the worker finishes")
    _common(watch)

    status = commands.add_parser("status", aliases=["who"], help="who is working on what right now")
    _common(status)

    cancel = commands.add_parser("cancel", help="stop the worker on an issue/PR and release it")
    cancel.add_argument("what", help="issue:N, pr:N, or a worker run ID")
    cancel.add_argument(
        "--no-wait", action="store_true", help="do not wait for the claim to be released"
    )
    _common(cancel)

    report = commands.add_parser("report", help="what happened since you last looked")
    report.add_argument(
        "--since", default="12h", help="how far back: 12h, 2d, or a date (default 12h)"
    )
    _common(report)

    stats = commands.add_parser("stats", help="which providers and models actually deliver")
    stats.add_argument("--window", choices=("24h", "7d"), default="7d")
    stats.add_argument("--provider", help="only this provider")
    _common(stats)

    settings = commands.add_parser("settings", help="view or change factory limits and priorities")
    settings.add_argument(
        "change",
        nargs="*",
        metavar="NAME [VALUE]",
        help="NAME VALUE to set, or NAME --reset to restore the default",
    )
    settings.add_argument("--reset", action="store_true", help="restore the default for NAME")
    _common(settings)

    prioritize = commands.add_parser("prioritize", help="move an issue or PR up the queue")
    prioritize.add_argument("what", help="issue:N or pr:N")
    prioritize.add_argument(
        "--level", choices=("critical", "high", "medium", "low", "none"), default="critical"
    )
    _common(prioritize)

    explain = commands.add_parser(
        "why",
        aliases=["explain"],
        help="why the factory is or is not picking up an issue",
        description=(
            "Explain, from the dispatcher's own rules, why an issue would or would not be "
            "picked up right now. With no argument, summarize every open issue."
        ),
    )
    explain.add_argument("what", nargs="?", help="issue:N (or just N); omit for every open issue")
    _common(explain)

    clear = commands.add_parser(
        "unstick",
        help="start the workers that clear whatever is holding the factory back",
        description=(
            "Read why work is not starting, plan the work that clears it (for a full review "
            "backlog: review and repair those pull requests), pick the provider and model with "
            "the best delivered track record, and start one worker per task after you confirm."
        ),
    )
    clear.add_argument("provider", nargs="?", help="use this provider instead of the best one")
    clear.add_argument("model", nargs="?", help="use this exact model (with PROVIDER)")
    clear.add_argument("--max", type=int, dest="max_workers", help="start at most N workers")
    clear.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="ITEM",
        help="never start this item (pr:N or issue:N); repeatable",
    )
    clear.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ITEM",
        help="start only these items (pr:N or issue:N); repeatable",
    )
    clear.add_argument("--dry-run", action="store_true", help="show the plan; start nothing")
    clear.add_argument("--yes", "-y", action="store_true", help="start without asking")
    _common(clear)

    results = commands.add_parser(
        "outcomes",
        help="did requested work actually happen? (worker result and the target's state now)",
        description=(
            "Follow each recent scheduler request through to its effect: what the scheduler "
            "decided, what the worker reported, and whether the goal holds now (merge conflict "
            "gone, checks fixed, review decided, PR delivered)."
        ),
    )
    results.add_argument("--since", default="2h", help="how far back: 2h, 1d, or a date")
    results.add_argument(
        "--all", action="store_true", help="include routine scheduler passes, not just targeted"
    )
    results.add_argument(
        "--by-item",
        action="store_true",
        help="one line per item: rounds, misses, latest result (find items going in circles)",
    )
    results.add_argument(
        "--limit", type=int, default=40, help="scheduler passes to read (default 40, newest first)"
    )
    _common(results)

    shut = commands.add_parser(
        "close",
        help="close a pull request that should not be done, and the issue it delivers",
        description=(
            "Stop any worker holding the pull request, close it with your reason as the "
            "comment, and close the issue it delivers as 'not planned' so intake does not "
            "pick it up again."
        ),
    )
    shut.add_argument("what", nargs="+", help="pr:N (one or more)")
    shut.add_argument("--reason", required=True, help="why; posted as the closing comment")
    shut.add_argument(
        "--keep-issue", action="store_true", help="close only the pull request, not its issue"
    )
    shut.add_argument("--yes", "-y", action="store_true", help="do not ask first")
    _common(shut)

    red = commands.add_parser("checks", help="why a pull request's checks fail, in one line each")
    red.add_argument("what", help="pr:N")
    _common(red)

    providers = commands.add_parser(
        "providers", help="providers the factory can use and whether they are set up"
    )
    _common(providers)

    models = commands.add_parser("models", help="models a provider offers, with track record")
    models.add_argument("provider")
    _common(models)


def _common(parser: argparse.ArgumentParser) -> None:
    # Accept global flags after the subcommand too (e.g. 'run ... --json').
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )


# Entry point -------------------------------------------------------------------


@dataclass
class Outcome:
    result: Result
    exit_code: int
    data: dict[str, object] = field(default_factory=dict)


def main(
    arguments: argparse.Namespace,
    *,
    env: dict[str, str] | None = None,
    stdout: TextIO | None = None,
    stdin: TextIO | None = None,
    clock: Clock | None = None,
    executable: str = "gh",
) -> int:
    values = dict(os.environ) if env is None else env
    json_mode = bool(getattr(arguments, "json", False))
    console = Console(
        stdout or sys.stdout,
        json_mode=json_mode,
        verbose=bool(getattr(arguments, "verbose", False)),
        env=values,
    )
    command = arguments.factory_command
    if command is None:
        print(_help_text(), file=console.stream)
        return EXIT_OK
    data: dict[str, object] = {"command": command}
    context: Context | None = None
    try:
        repo = find_repository(getattr(arguments, "repo", None), values, Path.cwd())
        github = GitHub(repo, executable=executable)
        input_stream = stdin or sys.stdin
        interactive = console.interactive and input_stream.isatty() and not json_mode
        context = Context(
            github, console, Prompt(console, input_stream), clock or Clock(), interactive
        )
        data["repository"] = repo
        handler = HANDLERS[command]
        outcome = handler(context, arguments)
        data.update(outcome.data)
        data["result"] = str(outcome.result)
        code = outcome.exit_code
    except KeyboardInterrupt:
        console.finish()
        if context is not None:
            data.update(context.progress)
        data.update(result="interrupted")
        code = EXIT_INTERRUPTED
        _stopped_watching(console, data)
    except UsageError as exc:
        console.finish()
        console.fail(str(exc))
        data.update(result="invalid", error=str(exc))
        code = EXIT_USAGE
    except ValueError as exc:
        console.finish()
        console.fail(str(exc))
        data.update(result="invalid", error=str(exc))
        code = EXIT_USAGE
    except GitHubError as exc:
        console.finish()
        console.fail(str(exc))
        data.update(result="github_unavailable", error=str(exc))
        code = exc.exit_code
    except runtime.RuntimeUnavailableError as exc:
        console.finish()
        console.fail(str(exc))
        data.update(result="invalid", error=str(exc))
        code = EXIT_USAGE
    data["exit_code"] = code
    if json_mode:
        print(json.dumps(data, default=str, sort_keys=True), file=console.stream)
    return code


def _help_text() -> str:
    return "\n".join(
        [
            "Factory operator console",
            "",
            "  rotisserie factory run [PROVIDER [MODEL]] [--force] [--target issue:N]",
            "  rotisserie factory status        who is working on what",
            "  rotisserie factory report        what merged, failed and is stuck",
            "  rotisserie factory stats         reliable providers and models",
            "  rotisserie factory settings      limits, worker counts, priorities",
            "  rotisserie factory why [issue:N] why work is or is not being picked up",
            "  rotisserie factory unstick       start the workers that clear the bottleneck",
            "  rotisserie factory outcomes      did the requested work actually happen",
            "  rotisserie factory checks pr:N   why its checks fail",
            "  rotisserie factory close pr:N --reason TEXT   close work that should not be done",
            "  rotisserie factory cancel issue:N",
            "  rotisserie factory watch RUN",
            "  rotisserie factory providers | models PROVIDER | prioritize issue:N",
            "",
            "Run 'rotisserie factory COMMAND --help' for details.",
        ]
    )


def _stopped_watching(console: Console, data: dict[str, object]) -> None:
    console.line()
    console.warn("Stopped watching.")
    console.line("  Your request keeps running on GitHub. Nothing was cancelled.")
    run = data.get("scheduler_run_id") or data.get("worker_run_id")
    if run:
        console.line(f"  Resume:  rotisserie factory watch {run}")
    target = data.get("target_ref")
    if target:
        console.line(f"  Stop it: rotisserie factory cancel {target}")


# run ---------------------------------------------------------------------------------


Request = dispatch.Request


def cmd_run(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    explicit = bool(arguments.provider and arguments.model)
    if arguments.slot is not None and arguments.slot < 1:
        raise UsageError("--slot must be 1 or more.")
    if arguments.target and not TARGET.match(arguments.target):
        raise UsageError("--target must look like issue:123 or pr:123.")
    if arguments.action != "advance" and not arguments.target:
        raise UsageError("--action needs --target: choose the pull request to work on.")
    force = bool(
        arguments.force
        or arguments.slot is not None
        or arguments.take_over
        or arguments.action != "advance"
    )
    if arguments.provider:
        provider = runtime.resolve_provider(arguments.provider)
    elif context.interactive:
        provider = _pick_provider(context)
    else:
        raise UsageError(
            "Name a provider (see 'rotisserie factory providers'), or run in a terminal for menus."
        )
    if arguments.model:
        model = arguments.model
    elif context.interactive:
        model = _pick_model(context, provider)
    else:
        model = "auto"
    runtime.validate_model(provider, model)
    request = Request(
        provider,
        model,
        arguments.target or "",
        force,
        arguments.slot,
        arguments.action,
        arguments.take_over,
    )
    # An explicit provider and model is a complete request: no further questions.
    if context.interactive and not explicit:
        if not request.target:
            chosen = _pick_work(context, request)
            if chosen is None:
                return Outcome(Result.NO_WORK, 0, {"cancelled": True})
        if not request.force:
            request.force = (
                context.prompt.choose(
                    "Respect the factory's limits for this run?",
                    [
                        "Yes, use normal scheduling",
                        "No, force it (ignore open-PR limits, cooldowns, worker counts, pauses)",
                    ],
                    default=1,
                )
                == 1
            )
    _show_request(console, request)
    if context.interactive and not explicit and not arguments.yes:
        if not context.prompt.confirm("Start this?"):
            console.line("Nothing was started.")
            return Outcome(Result.NO_WORK, 0, {"cancelled": True})
    data = context.progress
    data["request"] = request.to_dict()
    if request.target:
        data["target_ref"] = request.target
    if request.target and request.take_over:
        kind, number = _split_target(request.target)
        item = lookup(context.github, kind, number)
        if item.owner:
            stopped = _stop_worker(context, item, wait=True, purpose="take over")
            data["stopped_worker"] = item.owner
            if not stopped:
                return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    return _submit_and_watch(
        context, request, data, no_wait=arguments.no_wait, follow=arguments.follow
    )


def _show_request(console: Console, request: Request) -> None:
    console.heading("Factory")
    rows = [
        ("Provider", request.provider.name),
        ("Model", "chosen automatically" if request.model == "auto" else request.model),
        ("Override", "On — " + FORCE_EXPLANATION if request.force else "Off — normal scheduling"),
        ("Work", _describe_target(request)),
    ]
    if request.slot is not None:
        rows.append(("Worker", f"number {request.slot}"))
    console.fields(rows)
    console.line()


def _describe_target(request: Request) -> str:
    if not request.target:
        return "next task in the queue"
    kind, number = _split_target(request.target)
    text = f"{'issue' if kind == 'issue' else 'pull request'} #{number}"
    if request.action != "advance":
        text += f" ({ACTION_WORDS[request.action]})"
    if request.take_over:
        text += ", taking over from any current worker"
    return text


def _split_target(target: str) -> tuple[str, int]:
    match = TARGET.match(target)
    if match is None:
        raise UsageError("Targets look like issue:123 or pr:123.")
    return match.group(1), int(match.group(2))


def _submit_and_watch(
    context: Context, request: Request, data: dict[str, object], *, no_wait: bool, follow: bool
) -> Outcome:
    console = context.console
    submission = dispatch.submit(context.github, context.clock, request)
    console.debug(submission.raw.strip())
    console.ok("Request sent", local_time(submission.submitted_at))
    run_id = submission.run_id
    if run_id is None:
        console.fail("GitHub accepted the request but the scheduler run could not be found.")
        console.link("Look here", submission.url)
        return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], data)
    data["scheduler_run_id"] = run_id
    data["scheduler_url"] = submission.url
    if no_wait:
        console.link("Scheduler", submission.url)
        console.note(f"Follow it later: rotisserie factory watch {run_id}")
        return Outcome(Result.SUBMITTED, EXIT_CODES[Result.SUBMITTED], data)
    tracker = Tracker(context.github, context.clock)
    return _watch_scheduler(
        context, tracker, run_id, submission.submitted_at, data, follow=follow, request=request
    )


def _watch_scheduler(
    context: Context,
    tracker: Tracker,
    run_id: int,
    started: datetime,
    data: dict[str, object],
    *,
    follow: bool,
    request: Request | None = None,
) -> Outcome:
    console = context.console
    shown: set[Stage] = set()
    last = View(Stage.SUBMITTED, "", started)
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
    return _report_dispatch(context, tracker, outcome, data, follow=follow, request=request)


def _report_dispatch(
    context: Context,
    tracker: Tracker,
    outcome: DispatchOutcome,
    data: dict[str, object],
    *,
    follow: bool,
    request: Request | None,
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
            again = (
                f"rotisserie factory run {request.provider.key} {request.model} --target {target}"
            )
            console.line(f"  Take it over: {again} --take-over")
        console.line(f"  Stop it:      rotisserie factory cancel issue:{number}")
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
        console.line(f"See why: rotisserie factory why {explain_target}".rstrip())
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
        "Neon lease": "claim",
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
                console.note(f"Keep watching: rotisserie factory watch {run_id} --follow")
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
    except (GitHubError, ValueError):
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


# watch ---------------------------------------------------------------------------------


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
        return _watch_scheduler(context, tracker, run_id, created, data, follow=arguments.follow)
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


# status --------------------------------------------------------------------------------


def cmd_status(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    now = context.clock.now()
    work = current_work(context.github)
    jobs = one_per_worker(work)
    rows = [_work_row(item, now) for item in jobs]
    console.heading(f"Working now ({len(jobs)})")
    if not rows:
        console.line("Nothing is being worked on.")
    else:
        console.table(
            ["Item", "Title", "Provider", "Model", "State", "For"], [row[:6] for row in rows]
        )
        if console.verbose:
            for item in work:
                if item.run:
                    console.link(f"#{item.number}", item.run.url)
    data: dict[str, object] = {
        "working": [
            {
                "ref": item.ref,
                "title": item.title,
                "provider": item.provider,
                "model": item.run.model if item.run and item.run.model else None,
                "worker": item.owner,
                "run_url": item.run.url if item.run else None,
                "running_since": item.run.started.isoformat()
                if item.run and item.run.started
                else None,
                "coding_since": item.run.coding_since.isoformat()
                if item.run and item.run.coding_since
                else None,
            }
            for item in work
        ]
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _work_row(item: Item, now: datetime) -> list[str]:
    title = item.title if len(item.title) <= 60 else item.title[:59] + "…"
    if item.kind == "pr":
        title = "PR: " + title
    if item.run is None:
        state, age = "claimed, no GitHub worker running", since(item.updated, now)
    elif item.run.coding_since:
        state, age = "model working", since(item.run.coding_since, now)
    elif item.run.status == "queued":
        state, age = "waiting for a machine", since(item.run.started, now)
    else:
        state, age = "starting up", since(item.run.started, now)
    return [f"#{item.number}", title, item.provider, item.model, state, age]


# cancel --------------------------------------------------------------------------------


def cmd_cancel(context: Context, arguments: argparse.Namespace) -> Outcome:
    what = arguments.what
    if TARGET.match(what):
        kind, number = _split_target(what)
        item = lookup(context.github, kind, number)
    elif what.isdecimal():
        run = obj(context.github.repo_api(f"actions/runs/{what}"))
        title = re.match(r"^Factory (\d+) ", string(run, "display_title"))
        if not title:
            raise UsageError("That run is not a factory worker run.")
        held = [entry for entry in open_items(context.github) if entry.owner == title.group(1)]
        if not held:
            context.github.gh("run", "cancel", what, "--repo", context.repo)
            context.console.ok("Stopped the worker run", "it held no open item")
            return Outcome(Result.COMPLETED, EXIT_OK, {"cancelled_run": int(what)})
        item = held[0]
    else:
        raise UsageError("Say what to stop: issue:N, pr:N or a worker run ID.")
    if not item.owner:
        context.console.line(f"#{item.number} is not being worked on. Nothing to stop.")
        return Outcome(Result.IDLE, EXIT_OK, {"ref": item.ref})
    stopped = _stop_worker(context, item, wait=not arguments.no_wait, purpose="stop")
    result = Result.COMPLETED if stopped else Result.FAILED
    return Outcome(
        result, EXIT_CODES[result], {"ref": item.ref, "worker": item.owner, "released": stopped}
    )


def _stop_worker(context: Context, item: Item, *, wait: bool, purpose: str) -> bool:
    console = context.console
    who = item.provider or "a worker"
    if item.run is None:
        console.fail(f"#{item.number} is held by {who}, but no GitHub worker run is active for it")
        console.line(
            "  It may be a local worker. Stop it where it runs, or wait for its claim to expire."
        )
        return False
    console.note(
        f"Stopping {who} on #{item.number} "
        f"(running for {since(item.run.started, context.clock.now())})"
    )
    context.github.gh("run", "cancel", str(item.run.run_id), "--repo", context.repo)
    console.ok("Stop requested", "the worker's own cleanup releases its claim")
    if not wait:
        return True
    started = context.clock.now()
    for attempt, now in enumerate(Tracker(context.github, context.clock).poll(8)):
        current = lookup(context.github, item.kind, item.number)
        if current.owner != item.owner:
            console.finish()
            console.ok(
                f"#{item.number} is free", f"after {duration((now - started).total_seconds())}"
            )
            return True
        if attempt > 75:
            break
        console.waiting(
            f"Waiting for the claim on #{item.number} to be released "
            f"({duration((now - started).total_seconds())})"
        )
    console.finish()
    console.fail(
        f"The claim on #{item.number} was not released within 10 minutes; cannot {purpose} yet"
    )
    console.link("Worker", item.run.url)
    return False


# report --------------------------------------------------------------------------------


def parse_since(value: str, now: datetime) -> datetime:
    match = re.fullmatch(r"(\d+)\s*([hdm])", value.strip().lower())
    if match:
        amount = int(match.group(1))
        unit = {"h": "hours", "d": "days", "m": "minutes"}[match.group(2)]
        return now - timedelta(**{unit: amount})
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise UsageError("--since takes 12h, 2d, 90m or a date like 2026-11-21T22:00") from None
    return parsed if parsed.tzinfo else parsed.astimezone()


def cmd_report(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    github = context.github
    now = context.clock.now()
    start = parse_since(arguments.since, now)
    iso = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    console.heading(
        f"Factory report since {local_time(start)} ({duration((now - start).total_seconds())} ago)"
    )
    data: dict[str, object] = {"since": start.isoformat()}

    # Is it running at all?
    dispatches = items(
        github.repo_api(f"actions/workflows/{DISPATCH_WORKFLOW}/runs?per_page=1"), "workflow_runs"
    )
    last_dispatch = timestamp(dispatches[0], "created_at") if dispatches else None
    workers = _runs_since(github, WORKER_WORKFLOW, iso)
    coding_now = one_per_worker(current_work(github, with_session=False))
    console.line(console.bold("Is it running?"))
    if last_dispatch is None or now - last_dispatch > timedelta(minutes=20):
        console.fail(
            "The scheduler has not run recently", f"last run {since(last_dispatch, now)} ago"
        )
    else:
        console.ok("Scheduler is running", f"last run {since(last_dispatch, now)} ago")
    console.note(
        f"{len(coding_now)} item(s) being worked on now  (details: rotisserie factory status)"
    )
    data["scheduler_last_run"] = last_dispatch.isoformat() if last_dispatch else None
    data["working_now"] = len(coding_now)

    # What shipped?
    merged = _search(github, f"repo:{context.repo} is:pr is:merged merged:>={iso}")
    opened = _search(github, f"repo:{context.repo} is:pr created:>={iso}")
    console.line()
    console.line(console.bold(f"Merged ({len(merged)})"))
    for pr in merged[:25]:
        console.line(
            f"  {console.green(console.ok_mark)} #{integer(pr, 'number')}  "
            f"{string(pr, 'title')[:80]}"
        )
    if not merged:
        console.line("  Nothing merged.")
    open_now = [pr for pr in opened if string(pr, "state") == "open"]
    console.note(f"{len(opened)} pull request(s) opened; {len(open_now)} still open")
    data["merged"] = [
        {"number": integer(pr, "number"), "title": string(pr, "title")} for pr in merged
    ]
    data["opened"] = len(opened)

    # How did the workers do?
    conclusions = Counter(string(run, "conclusion") or string(run, "status") for run in workers)
    console.line()
    console.line(console.bold(f"Workers ({len(workers)} runs)"))
    console.note(
        ", ".join(f"{count} {_conclusion_word(name)}" for name, count in conclusions.most_common())
        or "No worker runs."
    )
    data["worker_runs"] = dict(conclusions)

    # What is broken?
    failed = [run for run in workers if string(run, "conclusion") == "failure"][:15]
    problems = _failure_reasons(github, failed)
    console.line()
    console.line(console.bold("Where it is breaking"))
    if failed:
        console.note(f"from the {len(failed)} most recent failed worker runs")
    if problems:
        for (reason, provider), count in problems.most_common(8):
            console.line(
                f"  {console.red(console.fail_mark)} {count}×  {reason}"
                + (f"  {console.dim('(' + provider + ')')}" if provider else "")
            )
    else:
        console.line("  No worker failures with a recorded reason.")
    data["failures"] = [
        {"reason": reason, "provider": provider, "count": count}
        for (reason, provider), count in problems.most_common(20)
    ]
    main_state = _main_health(github)
    if main_state:
        console.fail(f"main has failing checks: {', '.join(main_state[:5])}")
    else:
        console.ok("main is green")
    data["main_failing_checks"] = main_state
    stats = _load_stats(github)
    if stats is not None:
        rows = _provider_rows(stats, "24h")
        bad = [row for row in rows if row[2] == "0" and int(row[1]) >= 3]
        for row in bad:
            console.warn(f"{row[0]}: {row[1]} runs in 24h, none delivered")
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _conclusion_word(name: str) -> str:
    return {
        "success": "finished",
        "failure": "failed",
        "cancelled": "stopped",
        "in_progress": "running",
        "queued": "waiting",
        "skipped": "skipped",
    }.get(name, name)


def _runs_since(github: GitHub, workflow: str, iso: str, *, pages: int = 3) -> list[Obj]:
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


def _search(github: GitHub, query: str) -> list[Obj]:
    from urllib.parse import quote

    return items(github.api(f"search/issues?q={quote(query)}&per_page=100&sort=updated"), "items")


def _failure_reasons(github: GitHub, runs: list[Obj]) -> Counter[tuple[str, str]]:
    tracker = Tracker(github)

    def reasons(run: Obj) -> list[tuple[str, str]]:
        run_id = integer(run, "id")
        if run_id is None:
            return []
        title = re.match(r"^Factory (\d+) ", string(run, "display_title"))
        provider = runtime.provider_for_worker(title.group(1)) if title else None
        try:
            outcome = tracker.worker_outcome(tracker.jobs(run_id))
        except GitHubError:
            return []
        return [(plain_failure(outcome.message), provider.name if provider else "")]

    counter: Counter[tuple[str, str]] = Counter()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for found in pool.map(reasons, runs):
            counter.update(found)
    return counter


def plain_failure(message: str) -> str:
    """Turn the factory's outcome sentence into a short, plain reason."""
    match = re.search(r"worker failed: ([a-z_0-9]+)", message)
    if match is None:
        return message[:120]
    reason = OUTCOME_WORDS.get(match.group(1), match.group(1).replace("_", " "))
    if message.startswith("Code saved"):
        reason += "; its code was saved on the pull request"
    return reason


def _main_health(github: GitHub) -> list[str]:
    try:
        repo = obj(github.repo_api(""))
        branch = string(repo, "default_branch") or "main"
        checks = items(github.repo_api(f"commits/{branch}/check-runs?per_page=100"), "check_runs")
    except GitHubError:
        return []
    return sorted(
        {
            string(check, "name")
            for check in checks
            if string(check, "conclusion") in ("failure", "timed_out", "startup_failure")
        }
    )


# stats ---------------------------------------------------------------------------------


_load_stats = reliability.load_stats
_window = reliability.window
_count = reliability.count


def _provider_rows(stats: Obj, window: str) -> list[list[str]]:
    rows = []
    for entry in items(stats, "providers"):
        counts = _window(entry, window)
        provider = runtime.provider_for_source(string(entry, "source"))
        rows.append(
            [
                provider,
                str(_count(counts, "runs")),
                str(_count(counts, "delivered")),
                str(_count(counts, "completed") - _count(counts, "delivered")),
                str(_count(counts, "idle")),
                str(_count(counts, "failed")),
            ]
        )
    return sorted(rows, key=lambda row: (-int(row[2]), -int(row[1])))


def cmd_stats(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    stats = _load_stats(context.github)
    if stats is None:
        console.fail("No reliability snapshot has been published yet")
        console.line(
            "  The dashboard job publishes factory-stats.json to the "
            f"{STATS_BRANCH} branch every 15 minutes."
        )
        return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], {})
    generated = timestamp(stats, "generated_at")
    window = arguments.window
    words = {"24h": "last 24 hours", "7d": "last 7 days"}[window]
    console.heading(f"Reliability, {words}")
    console.note(
        f"Counts of finished worker runs, updated {since(generated, context.clock.now())} ago"
    )
    console.line()
    console.line(console.bold("Providers"))
    console.table(
        ["Provider", "Runs", "PRs", "Done", "Idle", "Failed"], _provider_rows(stats, window)
    )
    console.line()
    console.line(console.bold("Models"))
    rows = []
    models = []
    for entry in items(stats, "models"):
        source = string(entry, "source")
        if arguments.provider and runtime.resolve_provider(arguments.provider).source != source:
            continue
        counts = _window(entry, window)
        runs = _count(counts, "runs")
        health = obj(entry.get("health"))
        status = reliability.health_status(health, context.clock.now())
        if not runs and status not in ("paused", "retired"):
            continue
        failures = obj(counts.get("failures"))
        top = (
            max(failures.items(), key=lambda pair: pair[1] if isinstance(pair[1], int) else 0)[0]
            if failures
            else ""
        )
        delivered = _count(counts, "delivered")
        rows.append(
            [
                string(entry, "model")[:40],
                runtime.provider_for_source(source),
                str(runs),
                str(delivered),
                str(_count(counts, "failed")),
                OUTCOME_WORDS.get(top, top.replace("_", " ")),
                _health_word(health, context.clock.now()),
            ]
        )
        models.append(
            {
                "source": source,
                "model": string(entry, "model"),
                "runs": runs,
                "delivered": delivered,
                "failed": _count(counts, "failed"),
                "top_failure": top or None,
                "health": status or None,
            }
        )
    rows.sort(key=lambda row: (-int(row[3]), int(row[4]), -int(row[2])))
    console.table(
        ["Model", "Provider", "Runs", "PRs", "Failed", "Most common failure", "Now"], rows
    )
    if not rows:
        console.line("  No model ran in this window.")
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {
            "window": window,
            "generated_at": generated.isoformat() if generated else None,
            "models": models,
        },
    )


def _health_word(health: Obj, now: datetime) -> str:
    status = reliability.health_status(health, now)
    if status == "paused":
        until = timestamp(health, "cooldown_until")
        return f"paused until {local_time(until)}" if until else "paused"
    return {"healthy": "ok", "retired": "retired", "unknown": ""}.get(status, status)


# settings ------------------------------------------------------------------------------


def cmd_settings(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    change: list[str] = arguments.change
    known = {setting.name: setting for setting in runtime.settings()}
    if change:
        name = _setting_name(change[0], known)
        setting = known[name]
        if arguments.reset:
            try:
                context.github.gh("variable", "delete", name, "--repo", context.repo)
            except GitHubNotFoundError:
                pass
            console.ok(
                f"{setting.title} reset to the default ({_format(setting.unit, setting.default)})"
            )
            return Outcome(
                Result.COMPLETED,
                EXIT_OK,
                {"name": name, "value": setting.default, "origin": "default"},
            )
        if len(change) != 2:
            raise UsageError(f"Give a value: rotisserie factory settings {name} VALUE")
        value = runtime.validate_setting(name, change[1])
        context.github.gh("variable", "set", name, "--repo", context.repo, "--body", value)
        console.ok(
            f"{setting.title} set to {_format(setting.unit, value)}",
            "applies to the next factory run",
        )
        return Outcome(
            Result.COMPLETED,
            EXIT_OK,
            {"name": name, "value": value, "origin": "repository variable"},
        )
    configured = _variables(context.github)
    console.heading("Factory settings")
    rows = []
    out = []
    group = ""
    for setting in runtime.settings():
        if setting.group != group:
            group = setting.group
        current = configured.get(setting.name)
        value_text = current if current is not None else setting.default
        shown = _format(setting.unit, value_text)
        rows.append(
            [setting.title, shown, "set" if current is not None else "default", setting.name]
        )
        out.append(
            {
                "name": setting.name,
                "title": setting.title,
                "value": value_text,
                "origin": "repository variable" if current is not None else "default",
                "description": setting.description,
            }
        )
    console.table(["Setting", "Value", "", "Name"], rows)
    console.line()
    console.note("Change one: rotisserie factory settings NAME VALUE   (or NAME --reset)")
    console.note("Forced runs (--force) ignore these limits.")
    if console.verbose:
        for setting in runtime.settings():
            console.line(f"  {setting.name}: {setting.description}")
    return Outcome(Result.COMPLETED, EXIT_OK, {"settings": out})


def _setting_name(value: str, known: dict[str, runtime.SettingInfo]) -> str:
    candidate = value.upper().replace("-", "_")
    for name in (candidate, f"FACTORY_{candidate}"):
        if name in known:
            return name
    raise UsageError(f"Unknown setting {value}. See: rotisserie factory settings")


def _format(unit: str, value: str) -> str:
    if unit == "seconds" and value.isdecimal():
        return duration(int(value))
    if unit == "order":
        return " > ".join(value.split(","))
    return value


_variables = providers.repository_variables


# prioritize ----------------------------------------------------------------------------


def cmd_prioritize(context: Context, arguments: argparse.Namespace) -> Outcome:
    kind, number = _split_target(arguments.what)
    current = lookup(context.github, kind, number)
    remove = [label for label in PRIORITY_LABELS if label in current.labels]
    for label in remove:
        context.github.gh(
            "issue", "edit", str(number), "--repo", context.repo, "--remove-label", label
        )
    if arguments.level != "none":
        label = f"ralph-priority:{arguments.level}"
        context.github.gh(
            "label",
            "create",
            label,
            "--repo",
            context.repo,
            "--force",
            "--color",
            "b60205",
            "--description",
            "Factory pick-up priority",
        )
        context.github.gh(
            "issue", "edit", str(number), "--repo", context.repo, "--add-label", label
        )
        context.console.ok(
            f"#{number} priority set to {arguments.level}",
            "picked ahead of lower priorities in its category",
        )
    else:
        context.console.ok(f"#{number} priority cleared")
    context.console.note(
        "Category order (main breakage, user bugs, …) is the FACTORY_PRIORITY_ORDER setting."
    )
    return Outcome(Result.COMPLETED, EXIT_OK, {"ref": arguments.what, "level": arguments.level})


# why ------------------------------------------------------------------------------------


def cmd_why(context: Context, arguments: argparse.Namespace) -> Outcome:
    number: int | None = None
    if arguments.what:
        if arguments.what.isdecimal():
            number = int(arguments.what)
        else:
            kind, number = _split_target(arguments.what)
            if kind != "issue":
                raise UsageError(
                    "'why' explains issues. For pull requests, see 'rotisserie factory status'."
                )
    now = int(context.clock.now().timestamp())
    variables = _variables(context.github)
    if number is None:
        summary = queue.summarize_queue(context.github, now=now, variables=variables)
        _show_queue(context.console, summary)
        return Outcome(Result.COMPLETED, EXIT_OK, summary.to_dict())
    answer = queue.answer_issue(context.github, number, now=now, variables=variables)
    _show_answer(context, answer)
    data = answer.to_dict()
    data["ref"] = f"issue:{number}"
    data["reasons"] = [reason.text for reason in answer.verdict.reasons]
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _show_answer(context: Context, answer: queue.IssueAnswer) -> None:
    console = context.console
    number, found = answer.issue, answer.verdict
    console.heading(f"#{number}  {answer.title}" if answer.title else f"#{number}")
    if found.status == "ready":
        console.ok("Eligible now", "the next free worker can pick it up")
        console.line(
            "  Order among eligible work follows the priority settings; "
            f"to move it up: rotisserie factory prioritize issue:{number}"
        )
    elif found.status == "waiting":
        console.warn("Eligible, but held back by the factory's limits")
    else:
        console.fail("Not picked up automatically")
    for reason in found.reasons:
        console.line(f"  {console.dot} {reason.text}")
    if found.status != "ready":
        console.line()
        for hint in _why_hints(found, number):
            console.line(hint)
    if answer.intake_problem and found.category != "intake":
        console.warn(f"Rotisserie intake problem: {answer.intake_problem}")
    if console.verbose:
        explanation = answer.explanation
        console.line()
        console.line("Dispatcher reasons, verbatim:")
        for raw in explanation.reasons:
            console.line(f"  {raw}")
        console.line(
            f"  open PRs with a worker {explanation.pr_wip}/{explanation.pr_wip_limit}, "
            f"review backlog {explanation.review_backlog}/{explanation.review_backlog_limit}, "
            f"intake {explanation.intake}"
        )
    console.link("Issue", context.url(f"/issues/{number}"))


def _why_hints(found: queue.Verdict, number: int) -> list[str]:
    if found.category in ("closed", "finished", "manual-only"):
        return []
    if found.category == "held":
        return [
            f"Take it over: rotisserie factory run PROVIDER MODEL --target issue:{number} "
            "--take-over",
            f"Stop it:      rotisserie factory cancel issue:{number}",
        ]
    if found.category == "has-pr":
        return ["The pull request moves on through review, checks and repair."]
    setting = found.reasons[0].setting if found.reasons else None
    if setting:
        return [
            "Clear it:         rotisserie factory unstick",
            f"Raise the limit:  rotisserie factory settings {setting} N",
            f"Start it anyway: rotisserie factory run PROVIDER MODEL --target issue:{number} "
            "--force",
        ]
    return [
        f"Start it anyway: rotisserie factory run PROVIDER MODEL --target issue:{number} --force",
        "  (--force ignores the factory's limits and blocked/dependency labels)",
    ]


def _show_queue(console: Console, summary: queue.QueueSummary) -> None:
    ready = summary.ready
    held = summary.not_starting
    console.heading(f"Open issues ({len(summary.entries)})")
    console.line(f"{len(ready)} can start now.")
    if ready:
        rows = [
            [f"#{entry.issue}", entry.title if len(entry.title) <= 70 else entry.title[:69] + "…"]
            for entry in ready[:15]
        ]
        console.table(["Issue", "Title"], rows)
        if len(ready) > 15:
            console.line(f"  …and {len(ready) - 15} more")
    if held:
        console.line()
        console.line("Not starting:")
        for category, count in held.most_common():
            console.line(f"  {count:>4}  {queue.CATEGORY_WORDS.get(category, category)}")
    if summary.board.intake_problem:
        console.line()
        console.warn(f"Rotisserie intake admits nothing: {summary.board.intake_problem}")
    console.line()
    if any(code in held for code in ("review-limit", "wip-limit", "backpressure")):
        console.line("Clear the backlog: rotisserie factory unstick")
    console.line("Ask about one:     rotisserie factory why issue:N")


# unstick --------------------------------------------------------------------------------


def cmd_unstick(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    if arguments.model and not arguments.provider:
        raise UsageError("Name the provider before the model.")
    if arguments.max_workers is not None and arguments.max_workers < 1:
        raise UsageError("--max must be 1 or more.")
    for item in [*arguments.skip, *arguments.only]:
        if not TARGET.match(item):
            raise UsageError(f"--skip/--only take pr:N or issue:N, not {item!r}.")
    found = unstick.plan(
        context.github,
        now=context.clock.now(),
        max_workers=arguments.max_workers,
        provider=arguments.provider,
        model=arguments.model,
        skip=frozenset(arguments.skip),
        only=frozenset(arguments.only),
    )
    data: dict[str, object] = {"plan": found.to_dict()}
    _show_plan(console, found)
    if not found.assignments:
        for problem in found.problems:
            console.fail(problem[0].upper() + problem[1:])
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    if arguments.dry_run:
        console.line("Dry run: nothing was started.")
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    if not arguments.yes:
        if not context.interactive:
            console.line("Nothing was started. Add --yes to start these workers.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
        count = len(found.assignments)
        if not context.prompt.confirm(f"Start {count} worker{'s' if count != 1 else ''}?"):
            console.line("Nothing was started.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    submissions = unstick.start(context.github, context.clock, found)
    started = []
    for submission in submissions:
        target = submission.request.target
        if submission.run_id is None:
            console.warn(f"{target}: sent, but its scheduler run could not be found")
        else:
            console.ok(f"{target}: request sent", submission.url)
        started.append({"target": target, "run_id": submission.run_id, "url": submission.url})
    data["started"] = started
    console.line()
    console.line("Watch them: rotisserie factory status")
    return Outcome(Result.SUBMITTED, EXIT_CODES[Result.SUBMITTED], data)


def _show_plan(console: Console, found: unstick.Plan) -> None:
    console.heading("What is holding the factory back")
    for reason in found.diagnosis:
        console.line(f"  {console.dot} {reason.text}")
    for reason in found.held:
        console.line(f"  {console.dot} {reason.text}")
    if found.obsolete:
        console.line()
        console.heading(f"Needs you: probably obsolete ({len(found.obsolete)})")
        for reason in found.obsolete:
            console.line(f"  {console.dot} {reason.text}")
        console.line(
            "  No worker is sent to these. Closing them frees their review-backlog slots: "
            "gh pr close N --comment REASON"
        )
    if not found.assignments:
        return
    first = found.assignments[0]
    models = {(item.provider, item.model) for item in found.assignments}
    console.heading(f"Plan: {len(found.assignments)} workers")
    rows_meta = [("Why", first.why.split("; ", 1)[0])] if len(models) == 1 else []
    if len(models) == 1:
        rows_meta = [
            ("Provider", runtime.resolve_provider(first.provider).name),
            ("Model", first.model),
            *rows_meta,
        ]
    console.fields(
        [*rows_meta, ("Override", "On: targeted requests ignore the factory's own limits")]
    )
    rows = [
        [
            item.task.target.replace(":", " #").replace("pr", "PR"),
            unstick.ACTION_WORDS[item.task.action],
            item.model.split("/", 1)[-1],
            item.task.title if len(item.task.title) <= 50 else item.task.title[:49] + "…",
        ]
        for item in found.assignments
    ]
    console.table(["Item", "Work", "Model", "Title"], rows)
    for item in found.assignments:
        if "missed" in item.why:
            console.line(f"  {item.task.target}: {item.why.split('; ', 1)[-1]}")
    if found.left_out:
        console.line(f"  {found.left_out} more could be started (raise --max)")
    console.line()


# outcomes -------------------------------------------------------------------------------

GOAL_WORDS = {
    "met": "done",
    "not-met": "NOT done",
    "pending": "in progress",
    "not-started": "",
    "closed": "closed",
    "unknown": "?",
}


def cmd_outcomes(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    start = parse_since(arguments.since, context.clock.now())
    if arguments.limit < 1:
        raise UsageError("--limit must be 1 or more.")
    found = outcomes.request_outcomes(
        context.github,
        since=start,
        clock=context.clock,
        targeted_only=not arguments.all,
        limit=arguments.limit,
    )
    if arguments.by_item:
        return _show_by_item(console, start, outcomes.by_item(found))
    console.heading(f"Requests since {local_time(start)} ({len(found)})")
    if not found:
        console.line("No requests in that window.")
    rows = []
    for item in found:
        worker = item.worker_result or item.decision
        if item.decision != "dispatched":
            worker = f"not started: {item.decision}"
        rows.append(
            [
                local_time(item.requested_at),
                item.target or "next task",
                item.action or "",
                item.model.removeprefix("opencode-go/") if item.model else "",
                worker,
                GOAL_WORDS.get(item.goal, item.goal),
            ]
        )
    console.table(["Asked", "Item", "Action", "Model", "Worker", "Goal"], rows)
    misses = outcomes.latest_misses(found)
    if misses:
        console.line()
        console.line("Not done (latest request per item):")
        for item in misses:
            detail = item.reason if item.decision != "dispatched" else item.goal_text
            console.line(f"  {console.dot} {item.target or 'next task'}: {detail}")
            if item.worker_result == str(Result.IDLE):
                console.line("      the worker reported nothing to do, but the goal does not hold")
    summary = Counter(item.goal for item in found)
    data: dict[str, object] = {
        "since": start.isoformat(),
        "requests": [item.to_dict() for item in found],
        "goals": dict(summary),
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _show_by_item(
    console: Console, start: datetime, histories: list[outcomes.ItemHistory]
) -> Outcome:
    console.heading(f"Work per item since {local_time(start)} ({len(histories)})")
    rows = [
        [
            item.target,
            str(item.rounds),
            str(item.missed),
            ", ".join(dict.fromkeys(item.actions)),
            item.latest,
            GOAL_WORDS.get(item.latest_goal, item.latest_goal),
            "going in circles" if item.churning else "",
        ]
        for item in histories
    ]
    console.table(["Item", "Rounds", "Missed", "Actions", "Latest", "Goal", ""], rows)
    churning = [item for item in histories if item.churning]
    if churning:
        console.line()
        console.line(
            f"{len(churning)} item(s) missed twice or more with nothing done. Another round "
            "on the same models is unlikely to help: try a different model "
            "(run PROVIDER MODEL --target …), or look at the item yourself."
        )
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {"since": start.isoformat(), "items": [item.to_dict() for item in histories]},
    )


# checks ---------------------------------------------------------------------------------


def cmd_checks(context: Context, arguments: argparse.Namespace) -> Outcome:
    kind, number = _split_target(arguments.what)
    if kind != "pr":
        raise UsageError("checks takes a pull request: pr:N")
    console = context.console
    failing = checks.failing_checks(context.github, number)
    if not failing:
        console.ok(f"PR #{number}: no failing checks on its current head")
    else:
        console.fail(f"PR #{number}: {len(failing)} failing check(s)")
        for item in failing:
            console.line(f"  {console.dot} {item.name} ({item.kind}): {item.summary}")
            console.link("    Log", item.url)
        console.line()
        console.line(
            f"Repair it: rotisserie factory run PROVIDER MODEL --target pr:{number} "
            "--action ci-repair"
        )
    return Outcome(
        Result.COMPLETED, EXIT_OK, {"pr": number, "failing": [i.to_dict() for i in failing]}
    )


# close ----------------------------------------------------------------------------------


def cmd_close(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    numbers = []
    for what in arguments.what:
        kind, number = _split_target(what)
        if kind != "pr":
            raise UsageError("close takes pull requests: pr:N")
        numbers.append(number)
    plan = []
    for number in numbers:
        item = lookup(context.github, "pr", number)
        issue = None if arguments.keep_issue else close.linked_issue(context.github, number)
        plan.append((item, issue))
        held = f"; stop {item.provider or 'its worker'} first" if item.owner else ""
        also = f" and issue #{issue} (not planned)" if issue else ""
        console.line(f"  {console.dot} PR #{number}{also}{held}: {item.title}")
    console.line(f"  Comment: {arguments.reason}")
    if not arguments.yes:
        if not context.interactive:
            console.line("Nothing was closed. Add --yes to close these.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
        if not context.prompt.confirm("Close these?", default=False):
            console.line("Nothing was closed.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
    done = []
    for item, _ in plan:
        if item.owner and not _stop_worker(context, item, wait=True, purpose="close it"):
            console.fail(f"PR #{item.number} was left open: its worker did not stop")
            continue
        closed = close.close_pr(
            context.github, item.number, arguments.reason, with_issue=not arguments.keep_issue
        )
        with_issue = f" and issue #{closed.issue}" if closed.issue and closed.issue_was_open else ""
        console.ok(f"Closed PR #{item.number}{with_issue}")
        done.append(closed.to_dict())
    result = Result.COMPLETED if len(done) == len(plan) else Result.FAILED
    return Outcome(result, EXIT_CODES[result], {"closed": done})


# providers / models --------------------------------------------------------------------


def cmd_providers(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    working = Counter(item.provider for item in current_work(context.github, with_session=False))
    stats = _load_stats(context.github)
    delivered: dict[str, str] = {}
    if stats is not None:
        for row in _provider_rows(stats, "7d"):
            delivered[row[0]] = f"{row[2]} PRs / {row[1]} runs"
    rows = []
    out = []
    for setup in providers.provider_setup(context.github):
        provider = setup.provider
        ready = (
            "unknown"
            if not setup.known
            else ("ready" if not setup.missing else "missing " + ", ".join(setup.missing))
        )
        rows.append(
            [
                provider.key,
                provider.name,
                ready,
                str(working.get(provider.name, 0)),
                delivered.get(provider.name, ""),
            ]
        )
        out.append(
            {
                "provider": provider.key,
                "name": provider.name,
                "setup": ready,
                "working_now": working.get(provider.name, 0),
                "model_prefix": provider.model_prefix or None,
            }
        )
    console.heading("Providers")
    console.table(["Provider", "Name", "Setup", "Working", "Last 7 days"], rows)
    console.line()
    console.note(
        "A ready provider still has to prove itself: "
        "the worker checks price, quota and access at start."
    )
    return Outcome(Result.COMPLETED, EXIT_OK, {"providers": out})


discover_models = catalog.discover_models


def cmd_models(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    provider = runtime.resolve_provider(arguments.provider)
    stats = _load_stats(context.github)
    record: dict[str, str] = {}
    if stats is not None:
        for entry in items(stats, "models"):
            if string(entry, "source") == provider.source:
                counts = _window(entry, "7d")
                model = string(entry, "model")
                full = (
                    model
                    if model.startswith(provider.model_prefix)
                    else provider.model_prefix + model
                )
                record[full] = f"{_count(counts, 'delivered')} PRs / {_count(counts, 'runs')} runs"
    models = discover_models(provider)
    known = {model for model, _ in models}
    rows = [[model, record.get(model, ""), origin] for model, origin in models]
    # A model with a track record that the live lists no longer include would be
    # refused by the worker; say so instead of offering it.
    gone = "no longer offered (the worker would refuse it)" if known else "ran in the factory"
    rows += [[model, value, gone] for model, value in record.items() if model not in known]
    rows.sort(key=lambda row: (row[1] == "", row[0]))
    console.heading(f"{provider.name} models")
    if not provider.model_prefix:
        console.line("This provider picks its model automatically; use 'auto'.")
    elif not rows:
        console.line("No models discovered. You can still pass an exact model ID.")
    console.table(["Model", "Last 7 days", "Source"], rows)
    console.line()
    console.note(
        "A listed model is not proof it will run: "
        "the worker checks price and access when it starts."
    )
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {
            "provider": provider.key,
            "models": [
                {
                    "model": r[0],
                    "track_record": r[1] or None,
                    "source": r[2],
                    "offered": (r[0] in known) if known else None,
                }
                for r in rows
            ],
        },
    )


# interactive pickers -------------------------------------------------------------------


def _pick_provider(context: Context) -> runtime.Provider:
    providers = runtime.providers()
    index = context.prompt.choose("Which provider?", [f"{p.name}  ({p.key})" for p in providers])
    return providers[index]


def _pick_model(context: Context, provider: runtime.Provider) -> str:
    if not provider.model_prefix:
        return "auto"
    context.console.note("Looking up models…")
    models = discover_models(provider)[:40]
    options = (
        ["Let the factory choose a working model"]
        + [model for model, _ in models]
        + ["Type an exact model ID"]
    )
    index = context.prompt.choose("Which model?", options, default=0)
    if index == 0:
        return "auto"
    if index == len(options) - 1:
        return context.prompt.ask(f"Exact model ID (starts with {provider.model_prefix}):")
    return options[index]


def _pick_work(context: Context, request: Request) -> str | None:
    choice = context.prompt.choose(
        "What should it work on?",
        [
            "The next task in the queue",
            "Choose an open issue",
            "Choose an open pull request",
            "Type an issue or PR number",
        ],
        default=0,
    )
    if choice == 0:
        return ""
    if choice == 3:
        return _ask_target(context, request)
    kind = "issue" if choice == 1 else "pr"
    now = context.clock.now()
    entries = [item for item in open_items(context.github, limit=200) if item.kind == kind][:40]
    runs = {item.owner for item in entries if item.owner}
    if runs:
        work = {item.number: item for item in current_work(context.github, with_session=False)}
        for item in entries:
            if item.number in work:
                item.run = work[item.number].run
    options = []
    for item in entries:
        line = f"#{item.number}  {item.title[:70]}"
        if item.owner:
            running = since(item.run.started, now) if item.run else since(item.updated, now)
            line += f"   [{item.provider}, {running}]"
        options.append(line)
    if not options:
        context.console.line(f"No open {'issues' if kind == 'issue' else 'pull requests'}.")
        return _ask_target(context, request)
    index = context.prompt.choose(
        f"Open {'issues' if kind == 'issue' else 'pull requests'} "
        "([provider, time] = someone is working on it)",
        options + ["Type a number instead"],
    )
    if index == len(options):
        return _ask_target(context, request)
    item = entries[index]
    request.target = item.ref
    if item.owner:
        return _resolve_busy(context, request, item)
    return request.target


def _ask_target(context: Context, request: Request) -> str | None:
    while True:
        reply = context.prompt.ask("Issue or PR (e.g. issue:123, pr:45):")
        if TARGET.match(reply):
            request.target = reply
            item = lookup(context.github, *_split_target(reply))
            return _resolve_busy(context, request, item) if item.owner else reply
        context.console.line("Use issue:NUMBER or pr:NUMBER.")


def _resolve_busy(context: Context, request: Request, item: Item) -> str | None:
    running = since(item.run.started, context.clock.now()) if item.run else "unknown time"
    choice = context.prompt.choose(
        f"#{item.number} is being worked on by {item.provider} for {running}. What now?",
        [
            f"Take it over with {request.provider.name} ({request.model})",
            "Stop that work and leave it unclaimed",
            "Leave it and pick something else",
        ],
    )
    if choice == 0:
        request.take_over = True
        request.force = True
        return request.target
    if choice == 1:
        _stop_worker(context, item, wait=True, purpose="stop")
        return None
    request.target = ""
    return _pick_work(context, request)


HANDLERS = {
    "run": cmd_run,
    "watch": cmd_watch,
    "status": cmd_status,
    "who": cmd_status,
    "cancel": cmd_cancel,
    "report": cmd_report,
    "stats": cmd_stats,
    "settings": cmd_settings,
    "prioritize": cmd_prioritize,
    "why": cmd_why,
    "unstick": cmd_unstick,
    "outcomes": cmd_outcomes,
    "close": cmd_close,
    "checks": cmd_checks,
    "explain": cmd_why,
    "providers": cmd_providers,
    "models": cmd_models,
}


def run_cli(argv: Sequence[str]) -> int:
    """Standalone entry for tests: parse ``factory ...`` arguments and run."""
    parser = argparse.ArgumentParser(prog="rotisserie")
    sub = parser.add_subparsers(dest="command")
    add_parser(sub)
    return main(parser.parse_args(list(argv)))
