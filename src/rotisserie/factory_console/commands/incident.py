from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome, _split_target
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import incident
from rotisserie.factory_console.tracker import Result

USAGE = "baste incident [start issue:N [--attempts K] [--reason TEXT] | stop issue:N]"
HELP = """\
Send free workers to one issue until it is fixed.

  baste incident                     list incidents
  baste incident start issue:N       declare an incident
  baste incident stop issue:N        end it

While an incident's issue is open, its issue and PRs rank ahead of every other
item (owner picks included), its issue skips intake limits, and new intake of
other issues pauses. Running work is not cancelled. The next dispatcher tick
obeys a change without a deploy. When the issue closes, normal intake resumes
and the dispatcher closes the incident's leftover PRs as superseded.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "incident",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("words", nargs="*", metavar="start|stop issue:N")
    parser.add_argument("--attempts", type=int, help="separate fix attempts to race (default 3)")
    parser.add_argument("--reason", default="", help="why (shown in listings)")
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        parser.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )


def _issue(word: str) -> int:
    kind, number = _split_target(word)
    if kind != "issue":
        raise UsageError("An incident is an issue: issue:N")
    return number


def _show(context: Context, incidents: list[incident.Incident]) -> None:
    console = context.console
    console.heading("Incidents")
    if not incidents:
        console.line("  No incident. Normal intake.")
        return
    rows = []
    for item in incidents:
        status = "active" if item.active else "ended (issue closed)"
        rows.append([f"#{item.issue}", str(item.attempts), status, item.reason, item.since])
    console.table(["Issue", "Attempts", "State", "Reason", "Since"], rows)


def cmd_incident(context: Context, arguments: argparse.Namespace) -> Outcome:
    words: list[str] = arguments.words
    if not words:
        incidents = incident.read(context.github)
        _show(context, incidents)
        return Outcome(
            Result.COMPLETED, EXIT_OK, {"incidents": [item.to_dict() for item in incidents]}
        )
    if len(words) != 2 or words[0] not in ("start", "stop"):
        raise UsageError(f"Usage: {USAGE}")
    number = _issue(words[1])
    console = context.console
    try:
        if words[0] == "start":
            change = incident.start(
                context.github,
                number,
                attempts=arguments.attempts,
                reason=arguments.reason,
                now=context.clock.now(),
            )
        else:
            if arguments.attempts is not None:
                raise UsageError("--attempts goes with incident start")
            change = incident.stop(context.github, number)
    except incident.IncidentError as exc:
        raise UsageError(str(exc)) from None
    if change.action == "start":
        console.ok(
            f"Incident on #{number}" + (" declared" if change.changed else " already declared"),
            "the next dispatcher tick sends free workers to it; other new intake pauses",
        )
    else:
        console.ok(
            f"Incident on #{number}" + (" ended" if change.changed else " was not declared"),
            "normal intake resumes on the next dispatcher tick",
        )
        if change.closed_prs:
            closed = ", ".join(f"#{pr}" for pr in change.closed_prs)
            console.note(f"Closed as superseded: {closed}")
    return Outcome(Result.COMPLETED, EXIT_OK, change.to_dict())
