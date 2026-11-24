from __future__ import annotations

import argparse
from collections.abc import Sequence

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome, _rows, _split_target
from rotisserie.factory_console.commands.settings import _variables
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import bridge, claimops, frontier, queue
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import Console


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
    if arguments.via == "github":
        context.console.waiting("Reading who holds it through the label claim workflow")
        try:
            held = claimops.leases(
                context.github, context.clock, [f"issue:{number}"], started=context.progress
            )
        except bridge.BridgeError as exc:
            context.console.finish()
            context.console.warn(f"Could not read Neon holders: {exc}")
        else:
            context.console.finish()
            rows = _rows(held.get("rows"))
            data["holders"] = rows
            for row in rows:
                context.console.line(f"  Held by {row.get('holder')} ({row.get('expires_at')})")
            if not rows:
                context.console.line("  No Neon lease holds it.")
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


def _snapshot(
    context: Context, via: str, timeout: float = bridge.FRONTIER_TIMEOUT
) -> dict[str, object]:
    """The ranked work list, read directly or through the installed workflow."""
    if via != "github":
        return frontier.capture(context.github, now=context.clock.now()).to_dict()
    context.console.waiting("Running the work-list workflow on GitHub")
    found = bridge.frontier(
        context.github, context.clock, started=context.progress, timeout=timeout
    )
    context.console.finish()
    return {**found.data, "bridge": found.summary()}


def _show_offers(console: Console, offers: Sequence[object], limit: int = 12) -> None:
    for offer in offers[:limit]:
        if not isinstance(offer, dict):
            continue
        holds = [str(reason) for reason in offer.get("hold_reasons") or []]
        state = "held" if holds else "candidate"
        console.line(
            f"  {offer.get('rank')}. {offer.get('target')} · {offer.get('action')} · {state}"
        )
        console.line(f"     {offer.get('title')}")
        for reason in holds[:2]:
            console.line(f"     Hold: {reason}")


def cmd_next(context: Context, arguments: argparse.Namespace) -> Outcome:
    try:
        data = _snapshot(context, arguments.via)
    except bridge.BridgeError as exc:
        return _bridge_failed(context, exc, "The work list could not be read")
    console = context.console
    console.heading("Next meaningful factory work (read-only)")
    offers = data.get("offers")
    # A direct capture keeps offers as a tuple; a workflow's JSON has a list.
    _show_offers(console, offers if isinstance(offers, (list, tuple)) else [])
    if data.get("authoritative_claim_available") is not True:
        console.warn("Lease authority is unavailable; all candidates are held.")
        console.line(str(data.get("authority_note") or ""))
    _show_bridge(console, data)
    console.line("This list never reserves work. A trusted fenced claim is required.")
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _show_bridge(console: Console, data: dict[str, object]) -> None:
    run = data.get("bridge")
    if isinstance(run, dict):
        console.line(f"  GitHub run: {run.get('url')}")


def _bridge_failed(context: Context, exc: bridge.BridgeError, what: str) -> Outcome:
    context.console.finish()
    context.console.fail(f"{what}: {exc}")
    data: dict[str, object] = {"status": "unknown", "reason": str(exc)}
    if exc.url:
        data["bridge"] = {"url": exc.url}
    return Outcome(Result.UNKNOWN, EXIT_CODES[Result.UNKNOWN], data)
