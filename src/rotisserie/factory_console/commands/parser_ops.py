from __future__ import annotations

import argparse

from rotisserie.factory_console.commands import health_reset
from rotisserie.factory_console.commands.parser_insight import _common
from rotisserie.factory_console.service import assign_task, pause

ASSIGN_HELP = (
    "Claim the item as you (factory:claimed, so factory workers stay off it), add the "
    "agent:NAME label and post one task comment for the agent. Without --task the task "
    "follows the item's stage (review, merge-conflict, ci-repair, review-findings or "
    "implement) with conflicting files, failing checks and the linked issue's criteria. "
    "A merge-conflict task reads a few files per changed path. The agent ends it with "
    "'baste finish', which also removes agent:NAME."
)
PAUSE_HELP = (
    "Turn off the workflows that start or record factory work: "
    + ", ".join(pause.WORKFLOWS.values())
    + f". The reason goes in the {pause.VARIABLE} repository variable and shows in "
    "status, turning and recap. Running work is not cancelled. Needs your own gh login "
    "with the workflow scope."
)


def add_operator_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    hand = commands.add_parser(
        "assign",
        help="hand an issue or PR to an outside agent: claim, agent:NAME label, task comment",
        description=ASSIGN_HELP,
    )
    hand.add_argument("what", help="issue:N or pr:N")
    hand.add_argument("--agent", required=True, help="agent name, e.g. claude (label agent:NAME)")
    hand.add_argument("--task", default="", help="the task text instead of the stage default")
    hand.add_argument("--task-file", default="", help="read the task text from this file")
    hand.add_argument(
        "--kind",
        choices=assign_task.KINDS,
        help="use this stage's default task instead of the item's current stage",
    )
    hand.add_argument("--no-wait", action="store_true", help="do not wait for the claim answer")
    hand.add_argument("--timeout", type=int, default=600, help="seconds to wait (600)")
    _common(hand)

    held = commands.add_parser(
        "assigned", help="items held by outside agents: who has each one and for how long"
    )
    _common(held)

    drain = commands.add_parser(
        "pause",
        help="maintenance drain: turn off factory dispatch and claims",
        description=PAUSE_HELP,
    )
    drain.add_argument("--reason", default="", help="why; shown in status, turning and recap")
    drain.add_argument("--yes", "-y", action="store_true", help="pause without asking")
    _common(drain)

    wake = commands.add_parser(
        "resume",
        help="turn back on the factory workflows that 'pause' turned off, and list them",
    )
    wake.add_argument("--yes", "-y", action="store_true", help="resume without asking")
    _common(wake)

    result = commands.add_parser(
        "outcome",
        help="what one worker run did: outcome class, notices, error, failing step, links",
        description=(
            "Read every job log of one run (not the truncated 'gh run view --log') and show "
            "the [factory-outcome-class] record, notices, the error annotation and the job "
            "and step that failed. --follow waits for an unfinished run and retries GitHub "
            "errors that a dropped connection causes."
        ),
    )
    result.add_argument("run", help="workflow run ID or URL")
    result.add_argument("--follow", action="store_true", help="wait until the run finishes")
    result.add_argument("--interval", type=int, default=30, help="seconds between checks (30)")
    result.add_argument("--timeout", type=int, default=3600, help="give up after (3600; 0 never)")
    _common(result)
    health_reset.add_parser(commands)
