"""``rotisserie factory`` — the operator console for the software factory.

Every command reads real factory state; nothing is estimated. Human output is
the default. ``--json`` prints one machine-readable document. Exit codes:

  0  success (request accepted / worker started / work finished)
  2  the command was used incorrectly
  3  no work was assigned (nothing eligible, or the item is held by another worker)
  4  something failed (the scheduler, the worker, or the model)
  5  GitHub could not be reached, or the login lacks access
  6  the outcome could not be confirmed
  130  you stopped watching (the factory keeps working)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rotisserie.factory_console.context import UsageError
from rotisserie.factory_console.service import catalog, dispatch
from rotisserie.factory_console.tracker import Result

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
TARGET = re.compile(r"^(issue|pr):([1-9][0-9]*)$")
ACTIONS = ("advance", "implement", "merge-conflict", "ci-repair", "review-findings", "review")
ACTION_WORDS = {
    "implement": "write the change",
    "review": "review the pull request",
    "review-findings": "address review feedback",
    "ci-repair": "fix failing checks",
    "merge-conflict": "resolve merge conflicts",
    "advance": "do whatever the item needs next",
}
STATS_BRANCH = "factory-status-live"
PRIORITY_LABELS = (
    "ralph-priority:critical",
    "ralph-priority:high",
    "ralph-priority:medium",
    "ralph-priority:low",
)
OUTCOME_WORDS = {
    "provider_quota": "provider out of quota",
    "provider_failure": "provider error",
    "model_retired_410": "model retired by provider",
    "control_plane_failure": "factory setup problem",
    "work_failure": "model could not finish the work",
    "no_progress": "model made no progress",
    "timeout": "ran out of time",
    "agent_timeout": "ran out of time",
    "unknown_failure": "unclassified failure",
    "unconfigured": "provider not configured",
}
TAKE_OVER_ATTEMPTS = 2
FORCE_EXPLANATION = (
    "Ignores the factory's own limits (open-PR and review limits, cooldowns, "
    "health history, budgets, pauses, worker counts). It still cannot take "
    "work another worker holds; stop or take over that worker instead."
)


@dataclass
class Outcome:
    result: Result
    exit_code: int
    data: dict[str, object] = field(default_factory=dict)


Request = dispatch.Request


def _split_target(target: str) -> tuple[str, int]:
    match = TARGET.match(target)
    if match is None:
        raise UsageError("Targets look like issue:123 or pr:123.")
    return match.group(1), int(match.group(2))


def _rows(value: object) -> list[dict[str, object]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


discover_models = catalog.discover_models
