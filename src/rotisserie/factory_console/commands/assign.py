from __future__ import annotations

import argparse
from pathlib import Path

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.pause import paused_refusal
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import assign, assign_task, claimops
from rotisserie.factory_console.tracker import EXIT_CODES, Result

MAX_TASK = 20000


def _task(arguments: argparse.Namespace) -> str:
    if arguments.task and arguments.task_file:
        raise UsageError("Give --task or --task-file, not both")
    if arguments.task_file:
        try:
            text = Path(arguments.task_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"Cannot read --task-file: {exc}") from None
    else:
        text = arguments.task or ""
    if len(text) > MAX_TASK:
        raise UsageError(f"The task is longer than {MAX_TASK} characters")
    return text


def cmd_assign(context: Context, arguments: argparse.Namespace) -> Outcome:
    claimops.parse_target(arguments.what)
    try:
        agent = assign.check_agent(arguments.agent)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    task = _task(arguments)
    refusal = paused_refusal(context, f"{arguments.what} was not assigned")
    if refusal is not None:
        return refusal
    console = context.console
    console.waiting(f"Claiming {arguments.what} for agent:{agent}")
    try:
        data = assign.assign(
            context.github,
            context.clock,
            arguments.what,
            agent,
            task=task,
            kind=arguments.kind or "",
            wait=not arguments.no_wait,
            timeout=float(arguments.timeout),
        )
    except assign_task.NoDefaultTask as exc:
        console.finish()
        raise UsageError(str(exc)) from None
    console.finish()
    if data["status"] != "assigned":
        console.warn(f"{arguments.what}: not assigned; the claim was {data.get('reason')}")
        console.line("  Nothing was labelled and no task was posted.")
        return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], data)
    claim = data.get("claim")
    claim_status = claim.get("status") if isinstance(claim, dict) else "requested"
    console.ok(f"{arguments.what} assigned to agent:{agent}", f"{data['kind']} task")
    console.line(f"  Claim: {claim_status}")
    replaced = data.get("replaced")
    for old in replaced if isinstance(replaced, list) else []:
        console.line(f"  Removed {old}")
    if data.get("label_created"):
        console.line(f"  Created the label agent:{agent}")
    console.line(f"  Task comment ({data.get('comment')}): {data.get('comment_url')}")
    console.line(f"  The agent ends it with: baste finish {arguments.what}")
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def cmd_assigned(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    rows = assign.assigned(context.github)
    now = context.clock.now()
    console.heading(f"Held by outside agents ({len(rows)})")
    if not rows:
        console.line("  No open item has an agent:* label.")
    else:
        console.table(
            ["Item", "Agent", "For", "Title"],
            [[row.target, row.agent, assign.held_for(row.since, now), row.title] for row in rows],
        )
    data: dict[str, object] = {
        "assigned": [{**row.to_dict(), "held_for": assign.held_for(row.since, now)} for row in rows]
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)
