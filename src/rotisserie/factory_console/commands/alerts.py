from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.parser_insight import _common
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import alerts
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import local_time

EXIT_ALERT = 4
HELP = """\
Factory health alerts: the signals that catch a stalled factory within one check.

  repeated failure   the same worker error 3 or more times in 20 minutes
  factory state      baste turning says Slowing or Burning
  no review verdict  none in 45 minutes while PRs wait in review or ready
  no merge           none in 90 minutes while PRs wait in review or ready

Without --follow it prints the current alerts and exits 0 when there are none,
or 4 when there are some. --follow N checks every N minutes (default 10) and
prints a line only when an alert starts, changes or clears. It reads GitHub
only and changes nothing.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "alerts",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--follow",
        type=int,
        nargs="?",
        const=10,
        default=None,
        metavar="MINUTES",
        help="keep checking every MINUTES (default 10)",
    )
    _common(parser)


def cmd_alerts(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    if arguments.follow is None:
        console.waiting("Checking factory health")
        found = alerts.check(context.github, clock=context.clock)
        console.finish()
        console.heading(f"Factory alerts at {local_time(context.clock.now())}")
        if not found:
            console.ok("No alert: workers succeed, reviews record verdicts and PRs merge")
        for alert in found:
            console.line(f"  {console.fail_mark} {alert.text}")
        data: dict[str, object] = {"alerts": [alert.to_dict() for alert in found]}
        return Outcome(Result.COMPLETED, EXIT_ALERT if found else EXIT_OK, data)
    if not 1 <= arguments.follow <= 120:
        raise UsageError("--follow must be between 1 and 120 minutes")
    previous: dict[str, alerts.Alert] = {}
    while True:
        found = alerts.check(context.github, clock=context.clock)
        moment = local_time(context.clock.now())
        for change, alert in alerts.changes(previous, found):
            word = "ALERT" if change == "alert" else "CLEARED"
            console.line(f"{moment} {word} {alert.text}")
        previous = {alert.key: alert for alert in found}
        context.clock.sleep(arguments.follow * 60)
