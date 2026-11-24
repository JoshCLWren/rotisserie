from __future__ import annotations

import argparse
import os

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import bridge, db_usage
from rotisserie.factory_console.tracker import Result

HELP = """\
Turso rows read and written, storage and sync this month, against the free plan.

  baste db-usage                 last value the status page published
  baste db-usage --via github    fresh read in the label claim workflow
  baste db-usage --live          read the Turso API (trusted jobs only)

Status is ok, warn (any allowance projected over 70%, or the factory database
projected over the owner targets of 50M reads, 1M writes or 250 MB) or critical
(any allowance over 90% used, or Turso reports BLOCKED). New claims pause at
critical once the factory runs on Turso. Usage resets each calendar month; the
exact reset hour is unverified. The Platform API token stays in GitHub.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "db-usage",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--via",
        choices=("direct", "github"),
        default="direct",
        help="github runs the repository's trusted workflow for a fresh read",
    )
    parser.add_argument(
        "--live", action="store_true", help="call the Turso API with the token in this environment"
    )
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        parser.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )


def _show(context: Context, data: dict[str, object]) -> None:
    console = context.console
    status = str(data.get("status"))
    label = f" ({data['label']})" if data.get("label") else ""
    console.heading(f"Turso usage: {status}{label}")
    rows = [list(row) for row in db_usage.metric_lines(data)]
    if rows:
        console.table(["Metric", "Used this month", "Share", "Projected"], rows)
    if data.get("resets_at"):
        console.line(
            f"  {data.get('days_left')} days left; resets {data['resets_at']} "
            "(start of the UTC month; exact hour unverified)"
        )
        console.line(f"  Allowances from: {data.get('allowance_source')}")
    reasons = data.get("reasons")
    for reason in reasons if isinstance(reasons, list) else []:
        if status in ("ok", "not_configured", "unpublished"):
            console.note(f"  {reason}")
        elif status in ("critical", "error"):
            console.fail(str(reason))
        else:
            console.warn(str(reason))
    pause, why = db_usage.pause_new_claims(data)
    if pause:
        console.fail("New claims would pause", why)
    console.line(f"  Source: {data.get('source')}, checked {data.get('checked_at', 'never')}")


def cmd_db_usage(context: Context, arguments: argparse.Namespace) -> Outcome:
    if arguments.live and arguments.via == "github":
        raise UsageError("Use --live or --via github, not both")
    data: dict[str, object]
    if arguments.live:
        data = db_usage.live(dict(os.environ))
    elif arguments.via == "github":
        context.console.waiting("Reading Turso usage through the label claim workflow on GitHub")
        try:
            data = db_usage.via_github(context.github, context.clock, started=context.progress)
        except bridge.BridgeError as exc:
            return _bridge_failed(context, exc, "Turso usage could not be read")
        context.console.finish()
    else:
        data = db_usage.published(context.github)
    _show(context, data)
    _show_bridge(context.console, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)
