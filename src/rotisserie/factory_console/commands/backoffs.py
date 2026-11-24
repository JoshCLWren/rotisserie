from __future__ import annotations

import argparse
from datetime import datetime

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import backoffs, bridge
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import local_time

HELP = """\
What keeps each provider's models from running, and when they retry.

  baste backoffs --via github              list, per provider
  baste backoffs reset --quota-only --via github

The list shows three kinds of wait per provider: model health cooldowns,
Fibonacci backoffs of real failures (capped at one day), and quota, throttle
or credit cooldowns with where their reset time came from (header, key
endpoint, refusal text, or the provider's policy default).

reset --quota-only clears the backoffs that quota and throttle refusals wrote
before they stopped growing streaks, and caps the other streaks at one day. It
keeps real-failure streaks. Both run through the repository's trusted workflows;
this machine never connects to the factory store.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "backoffs",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("action", nargs="?", choices=("reset",), help="reset --quota-only")
    parser.add_argument("--quota-only", action="store_true", help="with reset: quota rows only")
    parser.add_argument("--yes", "-y", action="store_true", help="reset without asking")
    parser.add_argument(
        "--via",
        choices=("direct", "github"),
        default="direct",
        help="github runs the repository's trusted workflow",
    )
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in ("--json", "--verbose"):
        parser.add_argument(
            flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )


def _when(value: object) -> str:
    try:
        return local_time(datetime.fromisoformat(str(value)))
    except ValueError:
        return "unknown"


def _show(context: Context, data: dict[str, object]) -> None:
    console = context.console
    providers = data.get("providers")
    console.heading("Backoffs")
    if not isinstance(providers, dict) or not providers:
        console.line("  Nothing is cooling or backing off.")
        return
    for source, kinds in providers.items():
        if not isinstance(kinds, dict):
            continue
        console.heading(str(source))
        rows: list[list[str]] = []
        for item in kinds.get("capacity") or []:
            rows.append(
                [
                    f"{item.get('scope')} {item.get('kind')}",
                    str(item.get("model") or "-"),
                    f"reset from {item.get('reset_source')}",
                    _when(item.get("until")),
                ]
            )
        for item in kinds.get("fibonacci") or []:
            label = "quota (old streak)" if item.get("quota_caused") else "fibonacci"
            rows.append(
                [
                    label,
                    str(item.get("model") or item.get("key")),
                    f"streak {item.get('streak')}",
                    _when(item.get("until")),
                ]
            )
        for item in kinds.get("health") or []:
            rows.append(
                [
                    f"health {item.get('status')}",
                    str(item.get("model")),
                    str(item.get("outcome")),
                    _when(item.get("until")),
                ]
            )
        console.table(["Wait", "Model", "Why", "Next retry"], rows)


def cmd_backoffs(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    if arguments.action == "reset" and not arguments.quota_only:
        raise UsageError(
            "Only 'backoffs reset --quota-only' is offered here; the full reset is the "
            "Factory Model Health Reset workflow"
        )
    if arguments.quota_only and arguments.action != "reset":
        raise UsageError("--quota-only goes with: baste backoffs reset")
    try:
        if arguments.action == "reset":
            if not arguments.yes:
                if not context.interactive or not context.prompt.confirm(
                    "Clear quota and throttle backoffs and cap other streaks at one day?"
                ):
                    console.line("Nothing was changed. Add --yes to reset.")
                    return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
            if arguments.via == "github":
                console.waiting("Resetting through the model health reset workflow on GitHub")
                data = backoffs.reset_quota_via_github(
                    context.github, context.clock, started=context.progress
                )
                console.finish()
            else:
                data = backoffs.reset_quota_direct(context.github.repo)
            console.ok("Quota backoffs cleared", str(data.get("result", data)))
            _show_bridge(console, data)
            return Outcome(Result.COMPLETED, EXIT_OK, data)
        if arguments.via == "github":
            console.waiting("Reading backoffs through the label claim workflow on GitHub")
            data = backoffs.read_via_github(context.github, context.clock, started=context.progress)
            console.finish()
        else:
            data = backoffs.read_direct(context.github.repo)
    except bridge.BridgeError as exc:
        return _bridge_failed(context, exc, "The backoffs could not be read")
    except backoffs.BackoffsUnavailableError as exc:
        raise UsageError(str(exc)) from None
    _show(context, data)
    _show_bridge(console, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)
