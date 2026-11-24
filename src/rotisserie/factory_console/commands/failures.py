from __future__ import annotations

import argparse
from datetime import datetime

from rotisserie.factory_console.commands._shared import EXIT_OK, OUTCOME_WORDS, Outcome
from rotisserie.factory_console.commands.status import parse_since
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import failures
from rotisserie.factory_console.tracker import Result
from rotisserie.factory_console.ui import duration, local_time

RUNS_PER_GROUP = 8


def _when(stamp: str) -> str:
    try:
        return local_time(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    except ValueError:
        return stamp


def _class_word(name: str) -> str:
    return OUTCOME_WORDS.get(name, name.replace("_", " ")) if name else "unrecorded"


def cmd_failures(context: Context, arguments: argparse.Namespace) -> Outcome:
    if not 1 <= arguments.limit <= 300:
        raise UsageError("--limit must be between 1 and 300")
    console = context.console
    now = context.clock.now()
    start = parse_since(arguments.since, now)
    found = failures.worker_failures(context.github, start, limit=arguments.limit)
    groups = failures.group(found)
    console.heading(
        f"Failed worker runs since {local_time(start)} "
        f"({duration((now - start).total_seconds())} ago): {len(found)}"
    )
    if not found:
        console.ok("No worker run failed in this window")
    for entry in groups:
        console.line()
        where = f"{entry.job} > {entry.step}" if entry.step else entry.job
        console.line(console.bold(f"{len(entry.runs)}×  {where}"))
        for error, count in entry.errors().most_common(3):
            console.line(f"  error {count}×  {error[:160]}")
        classes = ", ".join(
            f"{count} {_class_word(name)}" for name, count in entry.classes().most_common()
        )
        console.line(f"  recorded as: {classes}")
        if entry.causes() != entry.classes():
            causes = ", ".join(
                f"{count} {_class_word(name)}" for name, count in entry.causes().most_common()
            )
            console.line(f"  current rules count these as: {causes}")
        for run in entry.runs[:RUNS_PER_GROUP]:
            details = "  ".join(
                part
                for part in (
                    _when(run.finished_at),
                    run.provider or "?",
                    run.target or "no target",
                    run.outcome_class or "unrecorded",
                )
                if part
            )
            console.line(f"    {details}  {run.url}")
            if run.selection:
                console.line(f"      chosen by {run.selection}")
        if len(entry.runs) > RUNS_PER_GROUP:
            console.note(f"    and {len(entry.runs) - RUNS_PER_GROUP} more (see --json)")
    data: dict[str, object] = {
        "since": start.isoformat(),
        "count": len(found),
        "groups": [entry.to_dict() for entry in groups],
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)
