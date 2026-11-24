"""Learned backoffs: read them, and clear the ones quota refusals left behind.

The trusted factory runtime holds the store connection. On an operator machine
these calls go through the adopter's trusted workflows (``--via github``); the
direct path runs only inside a trusted job that has ``FACTORY_TURSO_URL``.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import bridge
from rotisserie.factory_console.service.sync import ledger_directories, store_configured
from rotisserie.factory_console.tracker import Clock

LABEL_WORKFLOW = "rotisserie-label-claim.yml"
LABEL_ARTIFACT = "rotisserie-label-claim-{run_id}"
RESET_WORKFLOW = "factory-model-health-reset.yml"
RESET_ARTIFACT = "factory-model-health-reset-{run_id}"


class BackoffsUnavailableError(RuntimeError):
    """The factory store is not reachable from here."""


def _operations(env: Mapping[str, str] | None = None) -> Any:
    if not store_configured(env):
        raise BackoffsUnavailableError(
            "The factory store is visible only to the trusted factory runtime; use --via github"
        )
    for directory in ledger_directories():
        sys.path.insert(0, str(directory))
        try:
            import factory_state_operations as operations  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BackoffsUnavailableError(f"the factory runtime could not load ({exc})") from exc
        finally:
            sys.path.remove(str(directory))
        return operations
    raise BackoffsUnavailableError("the factory runtime was not found (run from an adopter)")


def read_direct(repo: str) -> dict[str, object]:
    return dict(_operations().export_backoffs(repo))


def reset_quota_direct(repo: str) -> dict[str, object]:
    return dict(_operations().reset_quota_backoffs(repo))


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
