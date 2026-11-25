from __future__ import annotations

import argparse
from datetime import datetime

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.parser_insight import _common
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.commands.why import _bridge_failed, _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import bridge, crashes
from rotisserie.factory_console.service.sync import store_configured
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import Console, local_time

HELP = """\
Factory crashes: uncaught exceptions in factory scripts, grouped by fingerprint.

  baste crashes                     groups of the last 6 hours
  baste crashes --since 24h         a longer window (the status page keeps 24 hours)
  baste crashes --show FINGERPRINT  the newest full traceback of one group

Each factory entry point stores one row per crash: the exception, the script,
line and function, a short scrubbed message, the run and the scrubbed traceback.
A fingerprint is the exception type, script, function and line. The groups come
from the status page snapshot (every 15 minutes). A traceback is read through
the read-only operator workflow, unless this machine holds the store.
"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "crashes",
        help=HELP.splitlines()[0],
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--since", default="6h", help="how far back to list crash groups (6h)")
    parser.add_argument("--show", metavar="FINGERPRINT", help="the newest traceback of one group")
    _common(parser)


def _when(value: object) -> str:
    found = crashes.moment(value)
    return local_time(found) if found else "unknown"


def crash_line(group: dict[str, object], *, since: bool = True) -> str:
    """'💥 Crash: UnboundLocalError in x.py:236 f, 5 times since 10:04 AM, last run URL'."""
    face = "💥 Crash" if group.get("fatal") else "🩹 Item failure"
    count = group.get("count")
    times = "once" if count == 1 else f"{count} times"
    when = f" since {_when(group.get('first_at'))}" if since else ""
    url = group.get("last_run_url")
    tail = f", last run {url}" if url else ""
    return f"{face}: {crashes.headline(group)}, {times}{when}{tail}"


def render_groups(console: Console, groups: list[dict[str, object]], limit: int = 20) -> None:
    for group in groups[:limit]:
        console.line(crash_line(group))
        where = crashes.where_ran(group)
        detail = f"      {group.get('fingerprint')}  {where}  {group.get('message') or ''}"
        console.line(console.dim(detail.rstrip()))


def _show_trace(console: Console, data: dict[str, object]) -> None:
    if not data.get("found"):
        console.warn(f"No crash with fingerprint {data.get('fingerprint')} is stored")
        return
    console.heading(crash_line(data))
    console.line(
        f"  {data.get('fingerprint')}  {data.get('workflow') or 'no workflow'}"
        f"  first {_when(data.get('first_at'))}, last {_when(data.get('last_at'))}"
    )
    console.line()
    console.line(str(data.get("traceback") or "no traceback stored").rstrip())


def _since(value: str, now: datetime) -> datetime:
    try:
        return parse_since(value, now)
    except UsageError:
        raise UsageError("--since takes 6h, 1d or 90m") from None


def cmd_crashes(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    if arguments.show:
        fingerprint = str(arguments.show).strip()
        if len(fingerprint) < 6 or not fingerprint.isalnum():
            raise UsageError("--show takes a fingerprint of at least 6 letters and digits")
        try:
            if store_configured():
                data = crashes.show_direct(context.repo, fingerprint)
            else:
                console.waiting("Reading the traceback through the operator read workflow")
                data = crashes.show_via_github(
                    context.github, context.clock, fingerprint, started=context.progress
                )
                console.finish()
        except bridge.BridgeError as exc:
            return _bridge_failed(context, exc, "The crash could not be read")
        except crashes.CrashesUnavailableError as exc:
            raise UsageError(str(exc)) from None
        _show_trace(console, data)
        _show_bridge(console, data)
        return Outcome(Result.COMPLETED, EXIT_OK, data)
    now = context.clock.now()
    since = _since(arguments.since, now)
    hours = max(1, int((now - since).total_seconds() // 3600) + 1)
    data = (
        crashes.live(context.repo, hours)
        if store_configured()
        else crashes.published(context.github)
    )
    groups = crashes.within(list(data.get("groups") or []), since)
    data = {**data, "since": since.isoformat(), "groups": groups}
    console.heading(f"Factory crashes since {local_time(since)}: {len(groups)} groups")
    if not data.get("published"):
        console.note("  No crash groups have been published yet")
    render_groups(console, groups)
    if data.get("source") == "published":
        console.line(
            console.dim(
                f"  Published {_when(data.get('generated_at'))}; the status page keeps "
                f"{data.get('window_hours') or crashes.PUBLISHED_HOURS}h"
            )
        )
    if groups:
        console.line("  👉 Next: baste crashes --show FINGERPRINT")
    return Outcome(Result.COMPLETED, EXIT_OK, data)
