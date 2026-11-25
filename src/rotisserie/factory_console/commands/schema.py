from __future__ import annotations

import argparse
from datetime import datetime

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import bridge, schema
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import Console, local_time

HELP = """\
Show the factory store's schema: each expected table and index, and the last upgrade.

  baste schema --via github            read it through the read-only workflow
  baste schema --apply --via github    run the idempotent schema now

The status lists each table and index the shipped SQLite schema creates and
whether the store holds it, the stored schema version against this code's
version, and the last upgrade attempt with its result and error. Every trusted
job upgrades the schema when the stored version is behind; --apply runs every
schema statement now, which also restores an object that is missing at the
current version. Each statement is IF NOT EXISTS, so nothing existing changes.
The read uses the read-only store token and --apply the label claim
workflow's write token; this machine never connects to the store.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "schema",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="run the schema statements now")
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


def _last(console: Console, last: object) -> None:
    if not isinstance(last, dict):
        console.line("  Last upgrade: none recorded")
        return
    where = str(last.get("source") or "unknown")
    if last.get("workflow"):
        where += f", {last['workflow']} run {last.get('run_id') or '?'}"
    console.line(
        f"  Last upgrade: {last.get('result')} at {_when(last.get('at'))} ({where}),"
        f" version {last.get('version')}"
    )
    created = last.get("created")
    if isinstance(created, list) and created:
        more = f" and {len(created) - 8} more" if len(created) > 8 else ""
        console.line(f"    created: {', '.join(str(name) for name in created[:8])}{more}")
    if last.get("error"):
        console.line(f"    error: {last['error']}")


def _show(context: Context, data: dict[str, object]) -> None:
    console = context.console
    console.heading(f"Store schema: {data.get('status') or 'unknown'}")
    console.line(
        f"  Code version {data.get('code_version')}, stored version"
        f" {data.get('stored_version') or 'none'}"
    )
    if data.get("applied"):
        console.line(f"  Apply: {data['applied']}")
        if data.get("apply_error"):
            console.line(f"    error: {data['apply_error']}")
    _last(console, data.get("last_upgrade"))
    reasons = data.get("reasons")
    for reason in reasons if isinstance(reasons, list) else []:
        console.warn(str(reason))
    objects = data.get("objects")
    rows = [
        [
            str(item.get("kind")),
            str(item.get("name")),
            "present" if item.get("present") else "MISSING",
        ]
        for item in (objects if isinstance(objects, list) else [])
        if isinstance(item, dict)
    ]
    if rows:
        console.table(["Kind", "Name", "State"], rows)


def cmd_schema(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    github = arguments.via == "github"
    try:
        if arguments.apply and github:
            console.waiting("Applying the schema through the label claim workflow on GitHub")
            data = schema.apply_via_github(context.github, context.clock, started=context.progress)
            console.finish()
        elif arguments.apply:
            data = schema.apply_direct()
        elif github:
            console.waiting("Reading the schema through the operator read workflow on GitHub")
            data = schema.read_via_github(context.github, context.clock, started=context.progress)
            console.finish()
        else:
            data = schema.read_direct()
    except bridge.BridgeError as exc:
        return _bridge_failed(context, exc, "The store schema could not be read")
    except schema.SchemaUnavailableError as exc:
        raise UsageError(str(exc)) from None
    _show(context, data)
    _show_bridge(console, data)
    if arguments.apply and data.get("applied") != "ok":
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)
