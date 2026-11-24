"""Fill the merge state that a REST pull request listing leaves out.

``GET /pulls`` never returns ``mergeable`` or ``mergeable_state``. In trusted
jobs the gh REST shim serves ``gh pr list`` from that endpoint, so without this
every conflicted PR looks merely unknown, its live check waits for CI that a
conflicted head never gets, and merge-conflict repair is never leased.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from factory_discovery import canonical_target

KNOWN = {"MERGEABLE", "CONFLICTING"}


def needs_live_state(row: dict[str, Any]) -> bool:
    labels = {str(item.get("name")) for item in row.get("labels") or [] if isinstance(item, dict)}
    factory = any(name == "factory" or name.startswith("factory:") for name in labels)
    return factory and str(row.get("mergeable") or "").upper() not in KNOWN


def fill_merge_state(
    rows: list[dict[str, Any]], read_pr: Callable[[int], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return ``rows`` with live merge state on factory PRs the listing left unknown."""
    filled = []
    for row in rows:
        if needs_live_state(row):
            try:
                live = canonical_target("pr", read_pr(int(row["number"])))
            except RuntimeError, KeyError, TypeError, ValueError:
                live = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
            row = {
                **row,
                "mergeable": live["mergeable"],
                "mergeStateStatus": live["mergeStateStatus"],
            }
        filled.append(row)
    return filled
