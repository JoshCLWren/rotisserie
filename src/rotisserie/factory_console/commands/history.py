from __future__ import annotations

import argparse
import os
from datetime import datetime

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome, _rows
from rotisserie.factory_console.context import (
    Context,
    UsageError,
    default_repository,
    save_default_repository,
)
from rotisserie.factory_console.service import claimops, history
from rotisserie.factory_console.tracker import Result


def local_time(stamp: object) -> str:
    """A GitHub timestamp in the operator's own time zone."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(stamp)
    return moment.strftime("%b %d %I:%M %p %Z").replace(" 0", " ")


def cmd_history(context: Context, arguments: argparse.Namespace) -> Outcome:
    claimops.parse_target(arguments.what)
    if not 1 <= arguments.limit <= 100:
        raise UsageError("--limit must be between 1 and 100")
    data = history.history(context.github, arguments.what, limit=arguments.limit)
    console = context.console
    console.heading(f"History of {arguments.what} ({data['state']})")
    for event in _rows(data.get("events")):
        console.line(
            f"  {local_time(event.get('at'))}  {event.get('actor') or '?'}: {event.get('detail')}"
        )
    runs = _rows(data.get("controller_runs"))
    if runs:
        console.heading("Label claim controller runs")
        for run in runs:
            console.line(
                f"  {local_time(run.get('created_at'))}  {run.get('event')} by "
                f"{run.get('actor')}: {run.get('conclusion') or run.get('status')}"
            )
            console.line(f"    {run.get('url')}")
    notes = data.get("notes")
    for note in notes if isinstance(notes, list) else []:
        console.warn(str(note))
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def cmd_use(context: Context, arguments: argparse.Namespace) -> Outcome:
    env = dict(os.environ)
    if arguments.repository:
        context.github.api(f"repos/{arguments.repository}")
        path = save_default_repository(env, arguments.repository)
        context.console.ok(f"Factory repository is now {arguments.repository}")
        context.console.line(f"  Saved in {path}. --repo and FACTORY_REPO still override it.")
        return Outcome(Result.COMPLETED, EXIT_OK, {"default": arguments.repository})
    saved = default_repository(env)
    context.console.line(f"Using {context.repo}")
    context.console.line(f"  Saved default: {saved or 'none (set one: baste use owner/name)'}")
    return Outcome(Result.COMPLETED, EXIT_OK, {"default": saved, "using": context.repo})
