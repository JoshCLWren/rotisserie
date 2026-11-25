from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge, _snapshot
from rotisserie.factory_console.context import Context
from rotisserie.factory_console.service import bridge
from rotisserie.factory_console.service import ready as ready_service
from rotisserie.factory_console.tracker import Result

HELP = """\
List claimable factory work in the exact order the dispatcher takes it.

  baste ready                  the ready list, read from GitHub on this machine
  baste ready --via github     the same list from the read-only workflow, with store holds
  baste ready --json           one JSON document

An item is ready when it is open, unowned (no factory owner or agent:NAME
label), not parked, cooling or waiting for a split, admitted by intake, and
no open item blocks it ("Blocked by #N" or "Depends on #N" lines, and links
the factory stored). Each row shows the stage the item is in and the action a
worker takes next. The list is the dispatcher's own pool; after it a worker
still checks its role, review independence, running checks and the lease.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "ready",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--limit", type=int, default=20, help="rows to show (20)")
    parser.add_argument(
        "--via",
        choices=("direct", "github"),
        default="direct",
        help="github runs the repository's read-only work-list workflow",
    )
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        parser.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )


def cmd_ready(context: Context, arguments: argparse.Namespace) -> Outcome:
    try:
        snapshot = _snapshot(context, arguments.via)
    except bridge.BridgeError as exc:
        return _bridge_failed(context, exc, "The ready list could not be read")
    data = ready_service.project(snapshot, limit=arguments.limit)
    console = context.console
    rows = data["ready"] if isinstance(data["ready"], list) else []
    console.heading(f"Ready work ({data['ready_count']})")
    if rows:
        console.table(
            ["#", "Item", "Stage", "Next action", "Title"],
            [
                [
                    str(row["rank"]),
                    str(row["target"]),
                    str(row["stage"] or ""),
                    str(row["action"]),
                    str(row["title"] or "")[:60],
                ]
                for row in rows
            ],
        )
    else:
        console.line("Nothing is claimable right now. Ask why: baste why")
    cycles = data["cycles"] if isinstance(data["cycles"], list) else []
    for cycle in cycles:
        loop = " -> ".join(f"#{n}" for n in [*cycle, cycle[0]])
        console.warn(f"Dependency cycle: {loop}. Remove one link to release it.")
    for note in data["notes"] if isinstance(data["notes"], list) else []:
        console.warn(str(note))
    _show_bridge(console, snapshot)
    data["bridge"] = snapshot.get("bridge")
    return Outcome(Result.COMPLETED, EXIT_OK, data)
