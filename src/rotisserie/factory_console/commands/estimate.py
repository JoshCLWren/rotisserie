from __future__ import annotations

import argparse
import os
from typing import Any, cast

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.history import local_time
from rotisserie.factory_console.commands.why import _show_bridge
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import GitHubError
from rotisserie.factory_console.service import bridge, estimate, estimate_model, estimate_source
from rotisserie.factory_console.service import estimate_neon as neon
from rotisserie.factory_console.tracker import EXIT_CODES, Result
from rotisserie.factory_console.ui import duration

FEATURE_WORDS = {
    "origin": "starting point",
    "category": "category",
    "labels": "intake labels",
    "size": "issue size",
}
ORIGIN_WORDS = {"issue": "starts from an issue", "pr": "standalone pull request"}


def _count(value: float) -> str:
    return f"{round(value)}"


def _time(hours: float) -> str:
    return duration(hours * 3600)


def _range(spread: dict[str, Any] | None, show: Any, unit: str) -> str:
    if spread is None:
        return "not enough similar merged items"
    return (
        f"median {show(spread['median'])}, middle 80% {show(spread['low'])} to "
        f"{show(spread['high'])}  (from {spread['n']} merged {unit})"
    )


def render(context: Context, data: dict[str, Any]) -> None:
    console = context.console
    features = cast(dict[str, Any], data["features"])
    labels = ", ".join(features["labels"]) or "none of " + ", ".join(estimate_model.INTAKE_LABELS)
    console.heading(f"Estimate for {data['target']} ({data['state']})")
    console.line(
        f"  {ORIGIN_WORDS[features['origin']].capitalize()}; category {features['category']}; "
        f"labels: {labels}; "
        f"size {features['size']} ({features['body_chars']:,} characters)"
    )
    so_far = cast(dict[str, Any], data["so_far"])
    if so_far["started_at"]:
        console.line(
            f"  So far: {so_far['attempts']} attempt(s); started "
            f"{local_time(so_far['started_at'])} ({_time(so_far['elapsed_hours'])} ago)"
        )
    else:
        console.line(f"  So far: {so_far['attempts']} attempt(s); the factory has not started it")
    if so_far.get("refusals") is not None:
        console.line(
            f"  Also: {so_far['capacity_runs']} run(s) where no model worked; "
            f"{so_far['refusals']} catalog refusal(s), not counted as attempts"
        )
    source = cast(dict[str, Any], data["source"])
    console.line(f"  Attempts from: {source['label']}")
    similar = cast(dict[str, Any], data["similar"])
    sample = cast(dict[str, Any], data["sample"])
    console.line()
    if similar["count"]:
        used = ", ".join(FEATURE_WORDS[name] for name in similar["features"])
        console.line(
            console.bold(f"Similar items: {similar['count']}")
            + f" of {sample['finished_items']} finished factory items (matched on {used})"
        )
        clock = cast(dict[str, str], data["clock"])
        console.line(f"  Attempts to merge:  {_range(data['attempts'], _count, 'items')}")
        console.line(f"  Time to merge:      {_range(data['hours'], _time, 'items')}")
        console.line(f"    (time runs {clock['meaning']})")
        chance = data["unattended"]
        if chance is not None:
            console.line(
                f"  Without a human:    {chance['probability']:.0%}, likely "
                f"{chance['low']:.0%} to {chance['high']:.0%}  "
                f"({chance['unattended']} of {chance['n']} finished items)"
            )
    else:
        console.line(console.bold("Similar items: too few for an estimate"))
    for row in cast(list[dict[str, Any]], similar["tried"]):
        if row["tier"] == similar["tier"]:
            break
        console.note(f"  {row['tier']}: {row['count']} item(s)")
    console.line()
    oldest, newest = sample["oldest_finish"], sample["newest_finish"]
    window = f", finished {local_time(oldest)} to {local_time(newest)}" if oldest else ""
    console.note(
        f"Read the {sample['scanned_prs']} most recently updated closed factory PRs{window}; "
        f"kept {sample['kept_items']}: {sample['rule']}."
    )
    console.note(
        "A human counts when: " + "; ".join(cast(dict[str, str], data["human_signals"]).values())
    )
    record = data.get("factory_record")
    if record:
        console.note(
            f"Factory-wide, the last 7 days: {record['delivered']} of {record['runs']} "
            "worker runs delivered a pull request (factory-stats.json)."
        )
    if source["attempts"] == "neon":
        console.note(
            f"Neon history: {source['rows_read']:,} event(s) over {source['window_days']} days "
            f"from {local_time(source['covered_from'])}; {source['items_from_neon']} item(s) "
            f"counted from Neon, {source['items_from_github']} from GitHub markers."
        )
        if source["truncated"]:
            console.warn("Neon history hit its row cap; older items use GitHub markers.")
    for note in cast(list[str], data["notes"]):
        console.warn(note)


def _neon(context: Context, arguments: argparse.Namespace) -> tuple[Any, str, Outcome | None]:
    console = context.console
    if arguments.run:
        found = estimate_source.earlier(context.github, arguments.run)
        if found.get("result") == "pending":
            _show_bridge(console, found)
            console.line("The Neon history run has not finished. Try again shortly.")
            return None, "", Outcome(Result.PENDING, EXIT_CODES[Result.PENDING], found)
        return (*estimate_source.load(found), None)
    if arguments.no_wait:
        started = estimate_source.start(context.github)
        console.ok("Started the Neon history run")
        _show_bridge(console, started)
        console.line(f"Read it with: baste estimate {arguments.what} --run ID")
        return None, "", Outcome(Result.SUBMITTED, EXIT_OK, started)
    console.waiting("Reading Neon history through the label claim workflow on GitHub")
    try:
        found = estimate_source.fetch(context.github, context.clock, started=context.progress)
    except bridge.BridgeError as exc:
        console.finish()
        follow = f" Read it later with --run when {exc.url} finishes." if exc.unfinished else ""
        return None, f"the workflow run failed: {exc}.{follow}", None
    except GitHubError as exc:
        console.finish()
        return None, f"GitHub refused to start the workflow: {exc}", None
    console.finish()
    return (*estimate_source.load(found), None)


def cmd_estimate(context: Context, arguments: argparse.Namespace) -> Outcome:
    history, fallback = None, ""
    if arguments.via == "github" or arguments.run:
        history, fallback, early = _neon(context, arguments)
        if early is not None:
            return early
    elif arguments.no_wait:
        raise UsageError("--no-wait needs --via github")
    try:
        data = estimate.estimate(
            context.github,
            arguments.what,
            sample=arguments.sample,
            now=context.clock.now(),
            history=history,
            fallback=fallback,
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    render(context, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)


def cmd_estimate_history(context: Context, arguments: argparse.Namespace) -> Outcome:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        context.console.fail("estimate-history runs only in trusted Actions")
        return Outcome(Result.FAILED, 4, {"status": "refused"})
    try:
        neon.bounds(arguments.days, arguments.rows)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    data = neon.read_history(
        context.github.repo, days=arguments.days, cap=arguments.rows, now=context.clock.now()
    )
    context.console.line(f"estimate history: {data['status']} ({data.get('rows_read', 0)} rows)")
    return Outcome(Result.COMPLETED, EXIT_OK, data)
