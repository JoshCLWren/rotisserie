"""Retry cooling and the item-loop breaker on the dispatcher's pool pass.

Each held candidate gets its reason in the assignment trace, so the dispatcher's
``none`` summary names it. Targeted and operator-forced passes skip this step.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any

import factory_assign_trace as trace


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
