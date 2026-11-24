from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands._shared import (
    EXIT_OK,
    Outcome,
    _split_target,
    discover_models,
)
from rotisserie.factory_console.commands.report import _count, _load_stats, _provider_rows, _window
from rotisserie.factory_console.commands.status import _stop_worker
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import items, string
from rotisserie.factory_console.service import close, groom, providers
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.work import current_work, lookup


def cmd_groom(context: Context, arguments: argparse.Namespace) -> Outcome:
    if arguments.step == "inventory":
        return _groom_inventory(context, arguments)
    return _groom_apply(context, arguments)


def _groom_inventory(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    try:
        found = groom.inventory(context.github, timelines=not arguments.no_timelines)
    except groom.InventoryError as exc:
        console.fail(f"Could not read the whole backlog: {exc}")
        console.line("Nothing is reported, because a partial inventory would look complete.")
        return Outcome(Result.FAILED, EXIT_CODES[Result.FAILED], {"error": str(exc)})
    data = found.to_dict()
    if arguments.out:
        Path(arguments.out).write_text(json.dumps(data, indent=2) + "\n")
    console.heading(f"Open backlog: {len(found.issues)} issues, {len(found.prs)} pull requests")
    for kind, counts in found.counts.items():
        parts = ", ".join(f"{counts[key]} {key}" for key in groom.DISPOSITIONS if key in counts)
        console.line(f"  {'Issues' if kind == 'issue' else 'Pull requests'}: {parts}")
    shown = [
        item
        for item in (*found.prs, *found.issues)
        if arguments.all or item.signals or item.disposition != "active"
    ]
    if shown:
        console.line()
        rows = [
            [
                item.ref,
                item.disposition,
                item.owner.removeprefix("factory:"),
                "; ".join(signal.text for signal in item.signals)[:110],
            ]
            for item in shown
        ]
        console.table(["Item", "Disposition", "Held by", "Evidence"], rows)
    console.line()
    console.line(
        "Review the evidence, write a plan, then: rotisserie factory groom apply PLAN.json"
    )
    if arguments.out:
        console.line(f"Full inventory: {arguments.out}")
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def _groom_apply(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    try:
        actions = groom.parse_plan(Path(arguments.plan).read_text())
    except OSError as exc:
        raise UsageError(f"Cannot read the plan: {exc}") from None
    except groom.PlanError as exc:
        raise UsageError(f"The plan is not valid: {exc}") from None
    log = Path(arguments.log) if arguments.log else None

    def record(result: groom.Applied) -> None:
        if log is not None:
            entry = {**asdict(result), "repo": context.github.repo,
                     "at": context.clock.now().isoformat()}  # fmt: skip
            with log.open("a") as handle:
                handle.write(json.dumps(entry) + "\n")

    console.heading(
        f"{'Applying' if arguments.yes else 'Checking (dry run)'} {len(actions)} action(s)"
    )
    results = groom.apply(context.github, actions, write=arguments.yes, record=record)
    for result in results:
        line = f"{result.target} {result.op}: {result.text}"
        if result.status in ("applied", "would-apply"):
            console.ok(line)
        elif result.status == "skipped":
            console.line(f"  {console.dot} {line}")
        else:
            console.warn(f"{result.status}: {line}")
    tally = Counter(result.status for result in results)
    console.line()
    console.line(", ".join(f"{count} {status}" for status, count in sorted(tally.items())))
    if not arguments.yes and tally.get("would-apply"):
        console.line("Nothing was changed. Add --yes to apply.")
    data: dict[str, object] = {"results": [asdict(result) for result in results]}
    failed = tally.get("failed", 0)
    result_code = Result.FAILED if failed else Result.COMPLETED
    return Outcome(result_code, EXIT_CODES[result_code], data)


def cmd_close(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    numbers = []
    for what in arguments.what:
        kind, number = _split_target(what)
        if kind != "pr":
            raise UsageError("close takes pull requests: pr:N")
        numbers.append(number)
    plan = []
    for number in numbers:
        item = lookup(context.github, "pr", number)
        issue = None if arguments.keep_issue else close.linked_issue(context.github, number)
        plan.append((item, issue))
        held = f"; stop {item.provider or 'its worker'} first" if item.owner else ""
        also = f" and issue #{issue} (not planned)" if issue else ""
        console.line(f"  {console.dot} PR #{number}{also}{held}: {item.title}")
    console.line(f"  Comment: {arguments.reason}")
    if not arguments.yes:
        if not context.interactive:
            console.line("Nothing was closed. Add --yes to close these.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
        if not context.prompt.confirm("Close these?", default=False):
            console.line("Nothing was closed.")
            return Outcome(Result.NO_WORK, EXIT_CODES[Result.NO_WORK], {})
    done = []
    for item, _ in plan:
        if item.owner and not _stop_worker(context, item, wait=True, purpose="close it"):
            console.fail(f"PR #{item.number} was left open: its worker did not stop")
            continue
        closed = close.close_pr(
            context.github, item.number, arguments.reason, with_issue=not arguments.keep_issue
        )
        with_issue = f" and issue #{closed.issue}" if closed.issue and closed.issue_was_open else ""
        console.ok(f"Closed PR #{item.number}{with_issue}")
        done.append(closed.to_dict())
    result = Result.COMPLETED if len(done) == len(plan) else Result.FAILED
    return Outcome(result, EXIT_CODES[result], {"closed": done})


def cmd_providers(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    working = Counter(item.provider for item in current_work(context.github, with_session=False))
    stats = _load_stats(context.github)
    delivered: dict[str, str] = {}
    if stats is not None:
        for row in _provider_rows(stats, "7d"):
            delivered[row[0]] = f"{row[2]} PRs / {row[1]} runs"
    rows = []
    out = []
    for setup in providers.provider_setup(context.github):
        provider = setup.provider
        ready = (
            "unknown"
            if not setup.known
            else ("ready" if not setup.missing else "missing " + ", ".join(setup.missing))
        )
        rows.append(
            [
                provider.key,
                provider.name,
                ready,
                str(working.get(provider.name, 0)),
                delivered.get(provider.name, ""),
            ]
        )
        out.append(
            {
                "provider": provider.key,
                "name": provider.name,
                "setup": ready,
                "working_now": working.get(provider.name, 0),
                "model_prefix": provider.model_prefix or None,
            }
        )
    console.heading("Providers")
    console.table(["Provider", "Name", "Setup", "Working", "Last 7 days"], rows)
    console.line()
    console.note(
        "A ready provider still has to prove itself: "
        "the worker checks price, quota and access at start."
    )
    return Outcome(Result.COMPLETED, EXIT_OK, {"providers": out})


def cmd_models(context: Context, arguments: argparse.Namespace) -> Outcome:
    console = context.console
    provider = runtime.resolve_provider(arguments.provider)
    stats = _load_stats(context.github)
    record: dict[str, str] = {}
    if stats is not None:
        for entry in items(stats, "models"):
            if string(entry, "source") == provider.source:
                counts = _window(entry, "7d")
                model = string(entry, "model")
                full = (
                    model
                    if model.startswith(provider.model_prefix)
                    else provider.model_prefix + model
                )
                record[full] = f"{_count(counts, 'delivered')} PRs / {_count(counts, 'runs')} runs"
    models = discover_models(provider)
    known = {model for model, _ in models}
    rows = [[model, record.get(model, ""), origin] for model, origin in models]
    # A model with a track record that the live lists no longer include would be
    # refused by the worker; say so instead of offering it.
    gone = "no longer offered (the worker would refuse it)" if known else "ran in the factory"
    rows += [[model, value, gone] for model, value in record.items() if model not in known]
    rows.sort(key=lambda row: (row[1] == "", row[0]))
    console.heading(f"{provider.name} models")
    if not provider.model_prefix:
        console.line("This provider picks its model automatically; use 'auto'.")
    elif not rows:
        console.line("No models discovered. You can still pass an exact model ID.")
    console.table(["Model", "Last 7 days", "Source"], rows)
    console.line()
    console.note(
        "A listed model is not proof it will run: "
        "the worker checks price and access when it starts."
    )
    return Outcome(
        Result.COMPLETED,
        EXIT_OK,
        {
            "provider": provider.key,
            "models": [
                {
                    "model": r[0],
                    "track_record": r[1] or None,
                    "source": r[2],
                    "offered": (r[0] in known) if known else None,
                }
                for r in rows
            ],
        },
    )
