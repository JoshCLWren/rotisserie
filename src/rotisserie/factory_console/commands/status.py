from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta

from rotisserie.factory_console.commands._shared import EXIT_OK, TARGET, Outcome, _split_target
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import obj, string
from rotisserie.factory_console.tracker import EXIT_CODES, Result, Tracker
from rotisserie.factory_console.ui import duration, since
from rotisserie.factory_console.work import Item, current_work, lookup, one_per_worker, open_items


def cmd_status(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    now = context.clock.now()
    work = current_work(context.github)
    jobs = one_per_worker(work)
    rows = [_work_row(item, now) for item in jobs]
    console.heading(f"Working now ({len(jobs)})")
    if not rows:
        console.line("Nothing is being worked on.")
    else:
        console.table(
            ["Item", "Title", "Provider", "Model", "State", "For"], [row[:6] for row in rows]
        )
        if console.verbose:
            for item in work:
                if item.run:
                    console.link(f"#{item.number}", item.run.url)
    data: dict[str, object] = {
        "working": [
            {
                "ref": item.ref,
                "title": item.title,
                "provider": item.provider,
                "model": item.run.model if item.run and item.run.model else None,
                "worker": item.owner,
                "run_url": item.run.url if item.run else None,
                "running_since": item.run.started.isoformat()
                if item.run and item.run.started
                else None,
                "coding_since": item.run.coding_since.isoformat()
                if item.run and item.run.coding_since
                else None,
            }
            for item in work
        ]
    }
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _work_row(item: Item, now: datetime) -> list[str]:
    title = item.title if len(item.title) <= 60 else item.title[:59] + "…"
    if item.kind == "pr":
        title = "PR: " + title
    if item.run is None:
        state, age = "claimed, no GitHub worker running", since(item.updated, now)
    elif item.run.coding_since:
        state, age = "model working", since(item.run.coding_since, now)
    elif item.run.status == "queued":
        state, age = "waiting for a machine", since(item.run.started, now)
    else:
        state, age = "starting up", since(item.run.started, now)
    return [f"#{item.number}", title, item.provider, item.model, state, age]


def cmd_cancel(context: Context, arguments: argparse.Namespace) -> Outcome:
    what = arguments.what
    if TARGET.match(what):
        kind, number = _split_target(what)
        item = lookup(context.github, kind, number)
    elif what.isdecimal():
        run = obj(context.github.repo_api(f"actions/runs/{what}"))
        title = re.match(r"^Factory (\d+) ", string(run, "display_title"))
        if not title:
            raise UsageError("That run is not a factory worker run.")
        held = [entry for entry in open_items(context.github) if entry.owner == title.group(1)]
        if not held:
            context.github.gh("run", "cancel", what, "--repo", context.repo)
            context.console.ok("Stopped the worker run", "it held no open item")
            return Outcome(Result.COMPLETED, EXIT_OK, {"cancelled_run": int(what)})
        item = held[0]
    else:
        raise UsageError("Say what to stop: issue:N, pr:N or a worker run ID.")
    if not item.owner:
        context.console.line(f"#{item.number} is not being worked on. Nothing to stop.")
        return Outcome(Result.IDLE, EXIT_OK, {"ref": item.ref})
    stopped = _stop_worker(context, item, wait=not arguments.no_wait, purpose="stop")
    result = Result.COMPLETED if stopped else Result.FAILED
    return Outcome(
        result, EXIT_CODES[result], {"ref": item.ref, "worker": item.owner, "released": stopped}
    )


def _stop_worker(context: Context, item: Item, *, wait: bool, purpose: str) -> bool:
    console = context.console
    who = item.provider or "a worker"
    if item.run is None:
        console.fail(f"#{item.number} is held by {who}, but no GitHub worker run is active for it")
        console.line(
            "  It may be a local worker. Stop it where it runs, or wait for its claim to expire."
        )
        return False
    console.note(
        f"Stopping {who} on #{item.number} "
        f"(running for {since(item.run.started, context.clock.now())})"
    )
    context.github.gh("run", "cancel", str(item.run.run_id), "--repo", context.repo)
    console.ok("Stop requested", "the worker's own cleanup releases its claim")
    if not wait:
        return True
    started = context.clock.now()
    for attempt, now in enumerate(Tracker(context.github, context.clock).poll(8)):
        current = lookup(context.github, item.kind, item.number)
        if current.owner != item.owner:
            console.finish()
            console.ok(
                f"#{item.number} is free", f"after {duration((now - started).total_seconds())}"
            )
            return True
        if attempt > 75:
            break
        console.waiting(
            f"Waiting for the claim on #{item.number} to be released "
            f"({duration((now - started).total_seconds())})"
        )
    console.finish()
    console.fail(
        f"The claim on #{item.number} was not released within 10 minutes; cannot {purpose} yet"
    )
    console.link("Worker", item.run.url)
    return False


def parse_since(value: str, now: datetime) -> datetime:
    match = re.fullmatch(r"(\d+)\s*([hdm])", value.strip().lower())
    if match:
        amount = int(match.group(1))
        unit = {"h": "hours", "d": "days", "m": "minutes"}[match.group(2)]
        return now - timedelta(**{unit: amount})
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise UsageError("--since takes 12h, 2d, 90m or a date like 2026-11-21T22:00") from None
    return parsed if parsed.tzinfo else parsed.astimezone()
