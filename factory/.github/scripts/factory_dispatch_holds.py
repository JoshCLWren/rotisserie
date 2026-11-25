"""Retry cooling and the item-loop breaker on the dispatcher's pool pass.

Each held candidate gets its reason in the assignment trace, so the dispatcher's
``none`` summary names it. Targeted and operator-forced passes skip this step.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any

import factory_assign_trace as trace
from factory_work_policy_backlog import LOOP_HOLD_KEY, LOOP_HOLDS
from factory_work_policy_base import is_factory_managed_pr, labels_of, stage_of
from factory_work_policy_priority import OVERRIDE_LABEL


def apply(repo: str, candidates: list[Any], prs: Iterable[Mapping[str, Any]]) -> list[Any]:
    """Drop every held candidate; a failed store read holds nothing."""
    try:
        from factory_state_operations import holds

        pr_heads = {int(pr["number"]): str(pr.get("headRefOid") or "") for pr in prs}
        heads = {
            c.number: (pr_heads.get(c.number, ""), c.stage or "factory:review")
            for c in candidates
            if c.kind == "pr"
        }
        issues = frozenset(c.number for c in candidates if c.kind == "issue")
        exempt = frozenset(c.number for c in candidates if c.override)
        found = holds(repo, heads, issues, exempt)
    except Exception as exc:  # noqa: BLE001 - advisory holds never block the pool
        print(
            f"[factory-controller] store retry read deferred ({type(exc).__name__})",
            file=sys.stderr,
        )
        return candidates
    for reason in sorted(set(found.values())):
        numbers = sorted(number for number, why in found.items() if why == reason)
        print(f"[factory-controller] {reason}: {numbers}", file=sys.stderr)
    return [c for c in candidates if c.number not in found or trace.skip(c.number, found[c.number])]


def stamp(repo: str, prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each PR the loop breaker holds, so backlog limits skip it; a failed read marks none."""
    heads = {
        int(pr["number"]): (
            str(pr.get("headRefOid") or ""),
            stage_of(labels_of(pr)) or "factory:review",
        )
        for pr in prs
        if is_factory_managed_pr(pr)
    }
    if not heads:
        return prs
    try:
        from factory_state_operations import holds

        exempt = frozenset(
            int(pr["number"]) for pr in prs if OVERRIDE_LABEL in labels_of(pr)
        )
        found = holds(repo, heads, frozenset(), exempt)
    except Exception as exc:  # noqa: BLE001 - a failed read keeps the old backlog count
        print(
            f"[factory-controller] loop hold read deferred ({type(exc).__name__})",
            file=sys.stderr,
        )
        return prs
    held = {number: why for number, why in found.items() if why in LOOP_HOLDS}
    return [
        {**pr, LOOP_HOLD_KEY: held[int(pr["number"])]} if int(pr["number"]) in held else pr
        for pr in prs
    ]
