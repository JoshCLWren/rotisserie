from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.parser_insight import _common
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.context import Context
from rotisserie.factory_console.service import attempts, recap, splits
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import local_time

ATTEMPTS_HELP = """\
The PRs the factory closed as failed attempts, and what each issue did next.

The self-heal sweep closes a parked PR as a failed attempt, keeps its branch
and puts its issue back in the queue. For each closed PR this shows the linked
issue (found as the factory finds it: branch, closing keyword, checkpoint
branch or "Part of" line) and what happened next: a later PR merged it, an
open PR works on it, it was split, it closed, or it waits at its stage label.
"""

SPLITS_HELP = """\
Each issue held for a split: its split runs, rejected plans and result.

The factory holds an oversized or umbrella issue and sends split runs to a
model. A valid plan becomes child issues. A rejected plan is retried, at most
3 runs per issue. This shows every issue labeled factory:needs-split or
umbrella, and every issue with a split request in the window.
"""

RUN_ENDS = {"handoff": "plan accepted", "rejected": "plan rejected"}


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tried = commands.add_parser(
        "attempts",
        help=ATTEMPTS_HELP.splitlines()[0],
        description=ATTEMPTS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tried.add_argument(
        "--since", default="24h", help="how far back: 24h, 3d, or a date (default 24h)"
    )
    _common(tried)
    split = commands.add_parser(
        "splits",
        help=SPLITS_HELP.splitlines()[0],
        description=SPLITS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    split.add_argument(
        "--since", default="3d", help="split requests this recent: 24h, 3d (default 3d)"
    )
    _common(split)


def _when(value: str) -> str:
    return local_time(recap.as_moment(value)) if value else ""


def cmd_attempts(context: Context, arguments: argparse.Namespace) -> Outcome:
    start = parse_since(arguments.since, context.clock.now())
    console = context.console
    console.waiting("Reading the PRs the factory closed as failed attempts")
    found = attempts.recent(context.github, since=start)
    console.finish()
    console.heading(f"Failed attempts closed since {_when(start.isoformat())}")
    if not found:
        console.line("None: the factory closed no PR as a failed attempt in this window.")
    for row in found:
        console.line(
            f"  {console.fail_mark} #{row.pr} {row.title}  {console.dim(_when(row.closed_at))}"
        )
        if row.issue is None:
            console.line("      no linked issue")
            continue
        after = f"#{row.next_pr}" if row.next_pr else ""
        detail = _when(row.detail) if row.next == "merged" else row.detail
        words = " ".join(
            part for part in (row.next, after, f"({detail})" if detail else "") if part
        )
        console.line(f"      issue #{row.issue}: {words}")
    data: dict[str, object] = {
        "since": start.isoformat(),
        "attempts": [row.to_dict() for row in found],
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def cmd_splits(context: Context, arguments: argparse.Namespace) -> Outcome:
    start = parse_since(arguments.since, context.clock.now())
    console = context.console
    console.waiting("Reading the issues held for a split")
    found = splits.recent(context.github, since=start)
    console.finish()
    console.heading("Issues held for a split")
    if not found:
        console.line("None: no issue is held for a split, and none was asked in this window.")
    for row in found:
        console.line(f"  #{row.issue} {row.title}")
        console.line(f"      {row.state}")
        asked = f"requested {_when(row.requested_at)}" if row.requested_at else "no request seen"
        if row.requests > 1:
            asked += f" ({row.requests} request comments)"
        console.line(f"      {asked}; split runs {len(row.runs)} of {row.max_runs}")
        for run in row.runs:
            end = {"handoff": "plan accepted", "rejected": "plan rejected"}.get(
                run["end"], "running"
            )
            why = f": {run['reason']}" if run.get("reason") else ""
            console.line(f"        {_when(run['started_at'])} worker {run['worker']}: {end}{why}")
    data: dict[str, object] = {
        "since": start.isoformat(),
        "splits": [row.to_dict() for row in found],
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)
