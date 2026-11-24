"""Learned backoffs: read them, and clear the ones quota refusals left behind.

The trusted factory runtime holds the Neon connection. On an operator machine
these calls go through the adopter's trusted workflows (``--via github``); the
direct path runs only inside a trusted job that has ``FACTORY_STATE_DSN_FILE``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import bridge
from rotisserie.factory_console.service.sync import ledger_directories
from rotisserie.factory_console.tracker import Clock

LABEL_WORKFLOW = "rotisserie-label-claim.yml"
LABEL_ARTIFACT = "rotisserie-label-claim-{run_id}"
RESET_WORKFLOW = "factory-model-health-reset.yml"
RESET_ARTIFACT = "factory-model-health-reset-{run_id}"


class BackoffsUnavailableError(RuntimeError):
    """Neon is not reachable from here."""


def _operations(env: Mapping[str, str] | None = None) -> tuple[Any, Any]:
    values = os.environ if env is None else env
    if not values.get("FACTORY_STATE_DSN_FILE"):
        raise BackoffsUnavailableError(
            "Neon is visible only to the trusted factory runtime; use --via github"
        )
    for directory in ledger_directories():
        sys.path.insert(0, str(directory))
        try:
            import factory_state_operations as operations  # type: ignore[import-not-found]
            import factory_state_operations_backoffs as backoffs  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BackoffsUnavailableError(f"the factory runtime could not load ({exc})") from exc
        finally:
            sys.path.remove(str(directory))
        return operations, backoffs
    raise BackoffsUnavailableError("the factory runtime was not found (run from an adopter)")


def read_direct(repo: str) -> dict[str, object]:
    operations, backoffs = _operations()
    return dict(operations._run(backoffs._export_backoffs, repo))


def reset_quota_direct(repo: str) -> dict[str, object]:
    operations, backoffs = _operations()
    return dict(operations._run(backoffs._reset_quota_backoffs, repo))


def read_via_github(
    github: GitHub, clock: Clock, *, started: dict[str, object] | None = None
) -> dict[str, object]:
    result = bridge.run(
        github,
        clock,
        LABEL_WORKFLOW,
        {"mode": "backoffs"},
        artifact=LABEL_ARTIFACT,
        timeout=bridge.CLAIM_TIMEOUT,
        started=started,
    )
    return {**result.data, "bridge": result.summary()}


def reset_quota_via_github(
    github: GitHub, clock: Clock, *, started: dict[str, object] | None = None
) -> dict[str, object]:
    result = bridge.run(
        github,
        clock,
        RESET_WORKFLOW,
        {"quota_only": "true"},
        artifact=RESET_ARTIFACT,
        timeout=bridge.CLAIM_TIMEOUT,
        started=started,
    )
    return {**result.data, "bridge": result.summary()}
