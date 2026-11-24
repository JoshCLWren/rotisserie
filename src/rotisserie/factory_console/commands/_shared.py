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
from typing import Any

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

PROVIDERS_HELP = """\
List providers, or turn one off or on for the whole fleet.

  rotisserie factory providers
  rotisserie factory providers disable local-ollama --reason "fans too loud"
  rotisserie factory providers enable local-ollama

A turned-off provider is skipped by every lane on the next dispatcher tick:
roster offers, automatic targets, unstick, cross-provider failover, the local
Ollama review offer and PR-Agent advisory review. Only a run that names the
provider explicitly ('rotisserie factory run PROVIDER ...') may still use it.
Work already running is not cancelled; stop it with
'rotisserie factory cancel issue:N' (or pr:N).

Adding a provider is a Rotisserie code change, not a switch: add its entry to
factory/.github/scripts/factory_provider_registry.py, add a FACTORY_HAS_<SECRET>
line to both dispatch steps in .github/workflows/fixed-model-factory-dispatch.yml,
pass the secret through the adopter's caller workflows, and set the secret in
the adopter repository. See docs/factory-console.md.
"""
MODELS_HELP = """\
List a provider's models, or turn one model off or on for the whole fleet.

  rotisserie factory models opencode-go
  rotisserie factory models orcarouter --billing factory-fleet-catalogs.json
  rotisserie factory models disable opencode-go opencode-go/hy4-flash-free --reason "loops"
  rotisserie factory models enable opencode-go opencode-go/hy4-flash-free

MODEL is the ID the listing shows. Automatic model choice, failover and
unstick never pick a turned-off model; a run that names that exact model may.

--billing groups the catalog into paid-credit, free and unknown-price models,
the rule a worker uses to stop trying paid models after an out-of-credits
refusal. Give it a saved provider catalog or worker fleet snapshot when the
provider has no public catalog.
"""
VIA_HELP = (
    "direct: use this machine's claim store connection (default); github: run the "
    "repository's installed workflow and wait for its result (no Neon needed here)"
)
BILLING_OPTION: dict[str, Any] = {
    "nargs": "?",
    "const": "",
    "metavar": "CATALOG",
    "help": "group by paid, free and unknown price (optional saved catalog file)",
}
