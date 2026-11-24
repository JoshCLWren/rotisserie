from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import EXIT_OK, TARGET, Outcome
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import GitHubError, GitHubRateLimitError
from rotisserie.factory_console.service import baseline, doctor, limits, outcomes, unstick
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import Console, local_time


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
    if not found.assignments and not found.syncs:
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
        work = [f"start {count} worker{'s' if count != 1 else ''}"] if count else []
        if found.syncs:
            work.append(f"sync {len(found.syncs)} PR(s) with main")
        if not context.prompt.confirm(" and ".join(work).capitalize() + "?"):
            console.line("Nothing was started.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    synced = [unstick.sync(context.github, item) for item in found.syncs]
    for change in synced:
        (console.ok if change.ok else console.warn)(change.text)
    data["synced"] = [asdict(change) for change in synced]
    if not found.assignments:
        result = Result.SUBMITTED if any(change.ok for change in synced) else Result.NO_WORK
        return Outcome(result, EXIT_CODES[result], data)
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
    if found.needs_person:
        console.line()
        console.heading(f"Needs you: models keep failing ({len(found.needs_person)})")
        for reason in found.needs_person:
            console.line(f"  {console.dot} {reason.text}")
    if found.shared_failures:
        _show_shared_failures(console, found.shared_failures)
    for reason in found.waiting_on_main:
        console.line(f"  {console.dot} {reason.text}")
    if found.syncs:
        console.line()
        console.heading(f"Sync with main ({len(found.syncs)})")
        for pending in found.syncs:
            console.line(f"  {console.dot} PR #{pending.pr}: {pending.why}")
        console.line("  No worker: merging main into the branch is the repair.")
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


def _show_shared_failures(console: Console, incidents: tuple[baseline.Incident, ...]) -> None:
    console.line()
    console.heading(f"Failing because of main ({len(incidents)})")
    for incident in incidents:
        console.line(f"  {console.dot} {incident.text}")
        if incident.cause:
            console.line(f"      Likely cause: {incident.cause}")
        for line in incident.evidence:
            console.line(f"      - {line}")


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


def _show_rate_limits(
    context: Context, *, blocked: GitHubRateLimitError | None = None
) -> dict[str, object]:
    """Present the authenticated CLI's quota, plus a truthful retry estimate if blocked."""
    console = context.console
    now = context.clock.now()
    report: limits.RateLimits | None
    try:
        report = limits.read(context.github)
    except GitHubError as exc:
        report = None
        console.warn(f"Cannot read GitHub API quota: {exc}")
    if report is not None:
        console.heading("GitHub API limits (current CLI credential)")
        for name, bucket in report.resources.items():
            reset = bucket.reset_at()
            console.line(
                f"  {name}: {bucket.remaining:,} / {bucket.limit:,} remaining "
                f"({bucket.used:,} used); resets {limits.local_deadline(reset)} "
                f"(in {limits.countdown(reset, now)})"
            )
    result: dict[str, object] = {
        "rate_limits": report.to_dict() if report is not None else None,
    }
    if blocked is not None:
        retry, reason = limits.retry_at(report, blocked, now)
        if retry is None:
            console.warn(f"Retry time unknown: {reason}.")
        else:
            console.note(
                f"Earliest known retry: {limits.local_deadline(retry)} "
                f"(in {limits.countdown(retry, now)}; {reason}). "
                "GitHub may still impose secondary limits."
            )
        result["rate_limit_block"] = {
            "kind": "secondary" if blocked.secondary else "primary_or_unknown",
            "retry_after_seconds": blocked.retry_after_seconds,
            "retry_at": retry.isoformat() if retry is not None else None,
            "retry_reason": reason,
        }
    return result


def cmd_limits(context: Context, arguments: argparse.Namespace) -> Outcome:
    data = _show_rate_limits(context)
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def cmd_doctor(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    console.heading("Factory health")
    findings = doctor.run_checks(context.github, now=context.clock.now(), clock=context.clock)
    for item in findings:
        line = f"{item.check}: {item.text}"
        if item.status == "ok":
            console.ok(line)
        elif item.status == "warn":
            console.warn(line)
        else:
            console.fail(line)
        if item.hint and item.status != "ok":
            console.line(f"    {item.hint}")
    data: dict[str, object] = {"findings": [i.to_dict() for i in findings]}
    blocker = next((i for i in findings if i.check == "GitHub API"), None)
    if blocker is not None:
        error = GitHubRateLimitError(
            blocker.text,
            secondary=blocker.secondary_limit,
            retry_after_seconds=blocker.retry_after_seconds,
        )
        data.update(_show_rate_limits(context, blocked=error))
    fixable = [item for item in findings if item.fix and item.status != "ok"]
    if fixable and not arguments.fix and blocker is None:
        console.line()
        console.line("Safe repairs are available: rotisserie factory doctor --fix")
    if arguments.fix:
        if blocker is not None:
            console.line("Repairs deferred: GitHub is rate limited; no fixes were attempted.")
            data["applied"] = []
        else:
            applied = doctor.apply_fixes(context.github, findings)
            console.line()
            for change in applied:
                console.ok(change.text, change.url)
            if not applied:
                console.line("Nothing to repair automatically.")
            data["applied"] = [change.to_dict() for change in applied]
    worst = "fail" if any(i.status == "fail" for i in findings) else "ok"
    result = Result.FAILED if worst == "fail" else Result.COMPLETED
    return Outcome(result, EXIT_CODES[result], data)
