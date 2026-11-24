from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.parser_insight import _common
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.context import Context
from rotisserie.factory_console.service import recap, self_heal
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import local_time

HELP = """\
What the factory cleared by itself, newest first.

On each dispatcher pass the factory moves a PR whose repair verdict named no
finding back to review (the next review avoids the model that wrote it),
resolves a bot reviewer's thread it answered "Fixed" with a commit in the head,
and merges main into a merge-conflict PR that GitHub can now merge, once per
head and never while a worker or person holds it. Each action leaves a factory
comment on the PR and an event in the factory store. `baste review pr:N` shows
a stall the factory has not cleared yet.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "heal",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--since", default="24h", help="how far back: 24h, 3d, or a date (default 24h)"
    )
    _common(parser)


def cmd_heal(context: Context, arguments: argparse.Namespace) -> Outcome:
    now = context.clock.now()
    start = parse_since(arguments.since, now)
    context.console.waiting("Reading what the factory fixed by itself")
    found = self_heal.recent(context.github, since=start)
    context.console.finish()
    console = context.console
    console.heading(f"Fixed by itself since {local_time(recap.as_moment(start.isoformat()))}")
    if not found:
        console.line("Nothing: the factory cleared no stall by itself in this window.")
    for heal in found:
        when = local_time(recap.as_moment(heal.at))
        console.line(f"  {console.ok_mark} {heal.text}  {console.dim(when)}")
        if heal.url:
            console.line(f"      {heal.url}")
    data: dict[str, object] = {
        "since": start.isoformat(),
        "healed": [heal.to_dict() for heal in found],
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)
