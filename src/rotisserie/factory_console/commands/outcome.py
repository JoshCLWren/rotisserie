from __future__ import annotations

import argparse
import re

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import run_outcome
from rotisserie.factory_console.tracker import Result

RUN = re.compile(r"(?:/actions/runs/)?(?P<id>[1-9][0-9]{0,19})(?:/.*)?$")
OUTCOME_KEYS = ("class", "source", "model", "target", "kind", "selection")


def run_id(value: str) -> int:
    match = RUN.search(value.strip())
    if match is None:
        raise UsageError("RUN must be a workflow run ID or its URL")
    return int(match.group("id"))


def cmd_outcome(context: Context, arguments: argparse.Namespace) -> Outcome:
    number = run_id(arguments.run)
    console = context.console
    retried = 0
    if arguments.follow:
        if arguments.interval < 10:
            raise UsageError("--interval must be at least 10 seconds")
        console.waiting(f"Waiting for run {number} to finish")
        _, retried = run_outcome.wait_until_done(
            context.github,
            context.clock,
            number,
            interval=float(arguments.interval),
            timeout=float(arguments.timeout),
        )
        console.finish()
    found = run_outcome.read_retrying(context.github, context.clock, number)
    console.heading(f"Run {number}: {found.title or found.workflow}")
    state = found.conclusion or found.status
    (console.ok if found.conclusion == "success" else console.warn)(f"{found.status}: {state}")
    if found.outcome:
        console.line(
            "  Outcome: "
            + ", ".join(
                f"{key} {found.outcome[key]}" for key in OUTCOME_KEYS if found.outcome.get(key)
            )
        )
    else:
        console.line("  Outcome: no [factory-outcome-class] line in its job logs")
    if found.verdict:
        console.line(f"  Worker said: {found.verdict}")
    if found.failed_job:
        console.line(
            f"  Failed in: {found.failed_job}"
            + (f" / {found.failed_step}" if found.failed_step else "")
        )
    for error in found.errors:
        console.fail(f"Error: {error}")
    for notice in found.notices:
        console.line(f"  notice: {notice}")
    console.link("Run", found.url)
    for job in found.jobs:
        if job.conclusion not in ("", "success", "skipped"):
            console.link(f"Job {job.name} ({job.conclusion})", job.url)
    data = {**found.to_dict(), "retried_reads": retried}
    if found.status != "completed":
        console.line(f"  Still {found.status}. Wait for it: baste outcome {number} --follow")
        return Outcome(Result.PENDING, EXIT_OK, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)
