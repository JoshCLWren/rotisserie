from __future__ import annotations

import argparse

from rotisserie.factory_console.commands import _shared
from rotisserie.factory_console.service.estimate import DEFAULT_SAMPLE, SAMPLE_RANGE


def add_insight_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    controller_runs = commands.add_parser(
        "runs", help="recent label-claim controller runs for an item, with their results"
    )
    controller_runs.add_argument("what", help="issue:N or pr:N")
    controller_runs.add_argument("--limit", type=int, default=5, help="runs to show (5)")
    _common(controller_runs)

    timeline = commands.add_parser(
        "history", help="label changes, close/reopen, factory markers and controller runs"
    )
    timeline.add_argument("what", help="issue:N or pr:N")
    timeline.add_argument("--limit", type=int, default=40, help="timeline items (40)")
    _common(timeline)

    guess = commands.add_parser(
        "estimate",
        help="expected attempts, time to merge and unattended chance, from similar past items",
        description=(
            "Compare the item with recent finished factory work in the same category, "
            "intake labels and issue size. Each figure shows a median, the middle 80% and "
            "how many past items it used. With too few similar items it gives no number."
        ),
    )
    guess.add_argument("what", help="issue:N or pr:N")
    guess.add_argument(
        "--sample",
        type=int,
        default=DEFAULT_SAMPLE,
        help=f"recent closed factory PRs to read ({SAMPLE_RANGE[0]} to {SAMPLE_RANGE[1]}, "
        f"default {DEFAULT_SAMPLE})",
    )
    _common(guess)

    failed = commands.add_parser(
        "failures",
        help="failed worker runs grouped by job and step, with error, class and run links",
    )
    failed.add_argument(
        "--since", default="10h", help="how far back: 10h, 2d, or a date (default 10h)"
    )
    failed.add_argument("--limit", type=int, default=60, help="newest failed runs to read (60)")
    _common(failed)

    choose = commands.add_parser("use", help="show or save the default factory repository")
    choose.add_argument("repository", nargs="?", help="owner/name to save as the default")
    _common(choose)


def _via(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--via",
        choices=("direct", "github"),
        default="direct",
        help=_shared.VIA_HELP,
    )


def _common(parser: argparse.ArgumentParser) -> None:
    # Accept global flags after the subcommand too (e.g. 'run ... --json').
    parser.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
