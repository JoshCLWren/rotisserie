from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence
from dataclasses import asdict

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome, _rows, _split_target
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import Obj, integer
from rotisserie.factory_console.service import (
    baseline,
    bridge,
    checks,
    claimops,
    conflicts,
    main_health,
    review,
    stalls,
)
from rotisserie.factory_console.service import sync as branch_sync
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import Console


def cmd_conflicts(context: Context, arguments: argparse.Namespace) -> Outcome:
    kind, number = _split_target(arguments.what)
    if kind != "pr":
        raise UsageError("conflicts takes a pull request: pr:N")
    console = context.console
    report = conflicts.pr_conflicts(context.github, number)
    if not report.overlaps:
        console.ok(report.text)
    else:
        console.warn(report.text)
        for item in report.overlaps:
            gone = " (deleted on main)" if item.deleted else ""
            console.line(f"  {console.dot} {item.path}{gone}: {item.change or 'unknown change'}")
        console.line()
        if report.prs:
            console.line(
                "Both sides changed these files. If the merged change already did this PR's "
                "work, close it: baste close pr:"
                f"{number} --reason TEXT. Otherwise a person may need to merge by hand."
            )
    return Outcome(Result.COMPLETED, EXIT_OK, report.to_dict())


def _pr_numbers(values: Sequence[str], command: str) -> list[int]:
    numbers: list[int] = []
    for value in values:
        kind, number = _split_target(value)
        if kind != "pr":
            raise UsageError(f"{command} takes pull requests: pr:N")
        numbers.append(number)
    return numbers


def cmd_sync(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    numbers = _pr_numbers(arguments.what, "sync")
    if bool(numbers) == bool(arguments.all_stale):
        raise UsageError("Name pull requests (pr:N) or pass --all-stale, not both.")
    if arguments.max < 1:
        raise UsageError("--max must be 1 or more.")
    if arguments.contains and not re.fullmatch(r"[0-9a-f]{40}", arguments.contains):
        raise UsageError("--contains takes a full 40-character commit SHA.")
    rows = branch_sync.open_pull_rows(context.github)  # the one PR read this pass shares
    plan = (
        branch_sync.stale_plan(context.github, rows)
        if arguments.all_stale
        else branch_sync.explicit_plan(numbers, target=arguments.contains)
    )
    data: dict[str, object] = plan.to_dict()
    console.heading(f"Sync with main ({len(plan.candidates)})")
    for item in plan.candidates:
        console.line(f"  {console.dot} PR #{item.pr}: {item.why}")
    for held in plan.held:
        console.line(f"  {console.dot} {held.text}")
    for problem in plan.problems:
        console.warn(problem)
    if not plan.candidates:
        console.line("Nothing needs a sync.")
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    if arguments.dry_run:
        console.line("Dry run: nothing was changed.")
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    if not arguments.yes:
        if not context.interactive:
            console.line("Nothing was changed. Add --yes to sync these pull requests.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
        if not context.prompt.confirm(f"Sync {len(plan.candidates)} PR(s) with main?"):
            console.line("Nothing was changed.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    ledger, why = branch_sync.load_ledger()
    if ledger is None and not arguments.now:
        console.line(f"Neon is not reachable from here: {why}.")
        console.line("Queueing instead: the scheduler syncs each one under a Neon lease.")
        queued = branch_sync.queue(context.github, plan, rows)
        for entry in queued:
            (console.ok if entry.ok else console.warn)(entry.text)
        data["queued"] = [asdict(entry) for entry in queued]
        data["neon"] = False
        result = Result.SUBMITTED if any(entry.ok for entry in queued) else Result.NO_WORK
        return Outcome(result, EXIT_CODES[result], data)
    holder = f"console-{os.environ.get('GITHUB_RUN_ID') or os.getpid()}"
    changes, note = branch_sync.execute(
        context.github, plan, rows, holder=holder, limit=arguments.max, now=arguments.now
    )
    if note:
        console.warn(f"Neon was not consulted: {note}. Owner labels decided who holds each PR.")
    for change in changes:
        (console.ok if change.ok else console.warn)(change.text)
    data["synced"] = [change.to_dict() for change in changes]
    data["neon"] = not note
    if any(change.status == "error" for change in changes):
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    result = Result.SUBMITTED if any(change.ok for change in changes) else Result.NO_WORK
    return Outcome(result, EXIT_CODES[result], data)


def cmd_leases(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    numbers = [_split_target(value)[1] for value in arguments.what]
    if arguments.release_expired:
        purged, why = branch_sync.release_expired(context.github)
        if purged is None:
            console.fail(f"Cannot release leases: {why}.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {"note": why})
        console.ok(f"Removed {purged} lease row(s) that expired over an hour ago.")
        return Outcome(Result.COMPLETED, EXIT_OK, {"released": purged})
    if arguments.via == "github":
        console.waiting("Reading Neon leases through the label claim workflow on GitHub")
        try:
            data = claimops.leases(
                context.github, context.clock, list(arguments.what), started=context.progress
            )
        except bridge.BridgeError as exc:
            return _bridge_failed(context, exc, "The leases could not be read")
        console.finish()
    else:
        data = branch_sync.lease_report(context.github, numbers=numbers).to_dict()
    _show_leases(console, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _show_leases(console: Console, data: dict[str, object]) -> None:
    if data.get("note"):
        console.warn(f"{data['note']}. Showing owner labels instead.")
    console.heading(f"Leases ({'Neon' if data.get('source') == 'neon' else 'owner labels'})")
    rows = _rows(data.get("rows"))
    if not rows:
        console.line("Nothing is held.")
    else:
        console.table(
            ["Item", "Held by", "State", "Expires"],
            [
                [
                    str(row.get("target")),
                    str(row.get("holder")),
                    "active" if row.get("active") else "expired",
                    str(row.get("expires_at")),
                ]
                for row in rows
            ],
        )
    _show_bridge(console, data)


def _branch_checks(context: Context, branch: str) -> Outcome:
    head = checks.branch_head(context.github, branch)
    latest: dict[str, int] = {}
    names = main_health.workflows(context.github)
    for name, listing in zip(names, main_health.fetch_runs(context.github, head, names)):
        runs = listing.get("workflow_runs") if isinstance(listing, dict) else None
        if isinstance(runs, list) and runs and isinstance(runs[0], dict):
            latest[name] = int(str(runs[0].get("id")))
    if not latest:
        context.console.warn(f"{branch} ({head[:8]}): product CI has not run on this commit")
        return Outcome(Result.UNKNOWN, EXIT_OK, {"branch": branch, "head": head, "failing": []})
    failing = checks.failing_checks_at(context.github, head, run_ids=list(latest.values()))
    console = context.console
    if not failing:
        console.ok(f"{branch} ({head[:8]}): no failing checks")
    else:
        console.fail(f"{branch} ({head[:8]}): {len(failing)} failing check(s)")
        for item in failing:
            console.line(f"  {console.dot} {item.name} ({item.kind}): {item.summary}")
            console.link("    Log", item.url)
    data: dict[str, object] = {
        "branch": branch,
        "head": head,
        "failing": [item.__dict__ for item in failing],
    }
    return Outcome(Result.COMPLETED if not failing else Result.FAILED, EXIT_OK, data)


def cmd_checks(context: Context, arguments: argparse.Namespace) -> Outcome:
    if arguments.what == "main" or arguments.what.startswith("branch:"):
        return _branch_checks(context, arguments.what.removeprefix("branch:"))
    kind, number = _split_target(arguments.what)
    if kind != "pr":
        raise UsageError("checks takes pr:N, main or branch:NAME")
    console = context.console
    failing = checks.failing_checks(context.github, number)
    shared = baseline.analyze(context.github, {number: failing}) if failing else baseline.Report()
    origins = {item.check: item for item in shared.for_pr(number)}
    if not failing:
        console.ok(f"PR #{number}: no failing checks on its current head")
    else:
        console.fail(f"PR #{number}: {len(failing)} failing check(s)")
        for item in failing:
            console.line(f"  {console.dot} {item.name} ({item.kind}): {item.summary}")
            verdict = origins.get(item.name)
            if verdict is not None and verdict.origin != baseline.OWN:
                console.line(f"    From main ({verdict.origin}): {verdict.reason}")
            console.link("    Log", item.url)
        for incident in shared.incidents:
            if incident.cause:
                console.line(f"  Likely cause on main: {incident.cause}")
        console.line()
        own = shared.own_failures(number, failing)
        if own:
            console.line(
                f"Repair it: baste run PROVIDER MODEL --target pr:{number} --action ci-repair"
            )
        elif shared.needs_sync(number):
            console.line("Main has moved past these failures: sync the branch with main.")
        else:
            console.line("Nothing here is the PR's to repair: fix main once, then sync.")
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {"pr": number, "failing": [i.to_dict() for i in failing], "origins": shared.to_dict()},
    )


def cmd_review(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    github = context.github
    targets: list[tuple[int, Obj | None]] = []
    for what in arguments.what:
        kind, number = _split_target(what)
        if kind != "pr":
            raise UsageError("review takes pull requests: pr:N")
        targets.append((number, None))
    if arguments.blocked:
        targets += [(integer(row, "number") or 0, row) for row in review.blocked_candidates(github)]
    if not targets:
        raise UsageError("Name pull requests (pr:N) or pass --blocked.")
    reports = [review.inspect(github, number, pull=pull) for number, pull in targets]
    held: dict[int, list[stalls.Stall]] = {
        number: stalls.inspect(github, number, pull=pull)
        for number, pull in targets
        if not arguments.blocked or pull is None
    }
    for report in reports:
        mark = console.ok if report.eligible else console.fail
        mark(f"PR #{report.pr} ({report.stage or 'no stage'}): {report.status}")
        authors = ", ".join(report.author_models) or "none recorded (any model may review)"
        console.line(f"  Known author models: {authors}")
        if not report.head_attributed:
            console.line(
                "  The current head has no author record of its own (a sync or hand push);"
                " earlier heads' authors still count."
            )
        reviewers = ", ".join(report.reviewer_models) or "none recorded"
        console.line(
            f"  Approvals on this head: {len(report.approvers)}; reviewer models: {reviewers}"
        )
        console.line(f"  {report.reason}")
        if report.block_note:
            moved = (
                f" (written on earlier head {report.block_head[:10]})"
                if report.block_head and report.block_head != report.head
                else ""
            )
            console.line(f"  Blocked note{moved}: {report.block_note[:200]}")
        elif report.stage == review.BLOCKED:
            console.line("  Blocked without a controller note; left alone.")
        for action in report.actions:
            console.line(f"  Next: {action}")
        for stall in held.get(report.pr, []):
            console.warn(f"  Deadlock: {stall.text}")
            console.line(f"    {stall.hint}")
            if stall.url:
                console.line(f"    {stall.url}")
    data: dict[str, object] = {
        "reports": [report.to_dict() for report in reports],
        "deadlocks": [stall.to_dict() for found in held.values() for stall in found],
    }
    if not arguments.requeue:
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    candidates = [report for report in reports if report.blocked_by_review_gate]
    if not candidates:
        console.line("Nothing to requeue: no PR here is blocked by the review gate.")
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    console.line(f"Requeue for review: {', '.join(f'#{item.pr}' for item in candidates)}")
    if not arguments.yes:
        if not context.interactive:
            console.line("Nothing was changed. Add --yes to requeue these.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
        if not context.prompt.confirm("Requeue these?", default=False):
            console.line("Nothing was changed.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    done = [review.requeue(github, item) for item in candidates]
    for item in done:
        (console.ok if item.ok else console.fail)(item.text)
    data["requeued"] = [item.to_dict() for item in done]
    result = Result.COMPLETED if all(item.ok for item in done) else Result.FAILED
    return Outcome(result, EXIT_CODES[result], data)
