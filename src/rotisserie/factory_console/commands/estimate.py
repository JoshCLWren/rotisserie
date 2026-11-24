from __future__ import annotations

import argparse
from typing import Any, cast

from rotisserie.factory_console.commands._shared import EXIT_OK, Outcome
from rotisserie.factory_console.commands.history import local_time
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.service import estimate, estimate_model
from rotisserie.factory_console.tracker import Result
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
    similar = cast(dict[str, Any], data["similar"])
    sample = cast(dict[str, Any], data["sample"])
    console.line()
    if similar["count"]:
        used = ", ".join(FEATURE_WORDS[name] for name in similar["features"])
        console.line(
            console.bold(f"Similar items: {similar['count']}")
            + f" of {sample['kept_items']} finished factory items (matched on {used})"
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
    for note in cast(list[str], data["notes"]):
        console.warn(note)


def cmd_estimate(context: Context, arguments: argparse.Namespace) -> Outcome:
    try:
        data = estimate.estimate(
            context.github,
            arguments.what,
            sample=arguments.sample,
            now=context.clock.now(),
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    render(context, data)
    return Outcome(Result.COMPLETED, EXIT_OK, data)
