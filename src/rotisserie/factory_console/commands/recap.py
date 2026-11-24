from __future__ import annotations

import argparse

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.parser_insight import _common
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.context import Context
from rotisserie.factory_console.service import recap
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import Console, local_time


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "recap",
        help="morning summary: what merged, what needs you, what went wrong",
        description=(
            "A short morning summary of what the factory did: a one-line verdict, merged "
            "work, what only you can do, failures by cause, providers, stuck items."
        ),
    )
    parser.add_argument(
        "--since", default="12h", help="how far back: 12h, 2d, or a date (default 12h)"
    )
    _common(parser)


def _when(value: object) -> str:
    return local_time(recap.as_moment(value))


def _rows(value: object) -> list[dict[str, object]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def render(console: Console, data: dict[str, object]) -> None:
    console.heading(f"Morning recap since {_when(data['since'])}")
    console.line(console.bold(str(data["verdict"])))
    merged = data["merged"] if isinstance(data["merged"], dict) else {}
    for group, label in (("product", "Merged: product"), ("factory", "Merged: factory")):
        rows = _rows(merged.get(group))
        if rows:
            console.line()
            console.line(console.bold(f"{label} ({len(rows)})"))
            for row in rows:
                ref = console.dim(f"#{row['number']}")
                console.line(f"  {console.ok_mark} {row['title']}  {ref}")
    needs = _rows(data["needs_you"])
    if needs:
        console.line()
        console.line(console.bold(f"Needs you ({len(needs)})"))
        for row in needs:
            console.line(f"  {console.warn_mark} {row['text']}")
            if row.get("action"):
                console.line(f"      {row['action']}")
    _render_wrong(console, data["went_wrong"])
    _render_providers(console, data["providers"])
    stuck = _rows(data["stuck"])
    if stuck:
        console.line()
        console.line(console.bold(f"Stuck ({len(stuck)})"))
        for row in stuck:
            console.line(f"  {console.warn_mark} {row['text']}")
    database = data.get("database")
    if isinstance(database, dict):
        console.line()
        console.line(console.bold("Database"))
        mark = console.ok_mark if database.get("status") == "ok" else console.warn_mark
        console.line(f"  {mark} {database.get('text')}")
    unread = data.get("unread")
    if isinstance(unread, list) and unread:
        console.line()
        console.note("Could not read: " + ", ".join(str(name) for name in unread))


def _render_wrong(console: Console, wrong: object) -> None:
    if not isinstance(wrong, dict):
        return
    causes = _rows(wrong.get("causes"))
    if not causes:
        return
    console.line()
    console.line(console.bold("What went wrong"))
    read, total = wrong.get("read"), wrong.get("failed_runs")
    if read != total:
        console.note(f"from the newest {read} of {total} failed runs")
    for cause in causes:
        who = ", ".join(str(name) for name in _list(cause.get("providers")))
        console.line(
            f"  {console.fail_mark} {cause['count']}×  {cause['words']}"
            + (f"  {console.dim('(' + who + ')')}" if who else "")
        )
        if cause.get("example"):
            console.line(f"      {cause['example']}")
        if cause.get("fix"):
            console.line(f"      Fix: {cause['fix']}")


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _render_providers(console: Console, found: object) -> None:
    if not isinstance(found, dict) or not any(_rows(rows) for rows in found.values()):
        return
    console.line()
    console.line(console.bold("Providers (last 24 hours)"))
    for row in _rows(found.get("delivered")):
        console.line(
            f"  {console.ok_mark} {row['provider']} delivered {row['delivered']} "
            f"of {row['runs']} runs"
        )
    for row in _rows(found.get("wasted")):
        console.line(
            f"  {console.fail_mark} {row['provider']} delivered nothing: "
            f"{row['failed']} of {row['runs']} runs failed"
        )
    for row in _rows(found.get("off")):
        name = f"{row['provider']} {row['model']}" if row.get("model") else row["provider"]
        reason = row.get("reason") or "no reason given"
        console.line(f"  {console.warn_mark} {name} is switched off: {reason}")
    for row in _rows(found.get("paused")):
        why = f" after {row['why']}" if row.get("why") else ""
        console.line(
            f"  {console.warn_mark} {row['provider']}: {row['models']} model(s) paused{why} "
            f"until {_when(row.get('until'))}"
        )


def cmd_recap(context: Context, arguments: argparse.Namespace) -> Outcome:
    now = context.clock.now()
    start = parse_since(arguments.since, now)
    context.console.waiting("Reading the factory's night")
    data = recap.recap(context.github, since=start, now=now, clock=context.clock).to_dict()
    context.console.finish()
    render(context.console, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)
