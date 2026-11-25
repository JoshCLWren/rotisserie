"""Retry cooling and the item-loop breaker on the dispatcher's pool pass.

Each held candidate gets its reason in the assignment trace, so the dispatcher's
``none`` summary names it. Targeted and operator-forced passes skip this step.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import factory_assign_trace as trace
import factory_incident as incident
from factory_work_policy_backlog import LOOP_HOLD_KEY, LOOP_HOLDS
from factory_work_policy_base import is_factory_managed_pr, labels_of, stage_of
from factory_work_policy_priority import OVERRIDE_LABEL

PARKED_LABEL = "factory:parked"
# Wakeups dispatch targeted runs with the workflow token; only a person ends a park.
AUTOMATIC = frozenset({"github-actions[bot]"})


def reasons(repo: str, candidates: list[Any], prs: Iterable[Mapping[str, Any]]) -> dict[int, str]:
    """Why each held candidate is held; a failed store read holds nothing."""
    try:
        from factory_state_operations import holds

        pr_heads = {int(pr["number"]): str(pr.get("headRefOid") or "") for pr in prs}
        heads = {
            c.number: (pr_heads.get(c.number, ""), c.stage or "factory:review")
            for c in candidates
            if c.kind == "pr"
        }
        issues = frozenset(c.number for c in candidates if c.kind == "issue")
        exempt = frozenset(c.number for c in candidates if c.override or c.incident)
        return dict(holds(repo, heads, issues, exempt))
    except Exception as exc:  # noqa: BLE001 - advisory holds never block the pool
        print(
            f"[factory-controller] store retry read deferred ({type(exc).__name__})",
            file=sys.stderr,
        )
        return {}


def reader(repo: str, prs: Iterable[Mapping[str, Any]]) -> Callable[[list[Any]], dict[int, str]]:
    """A hold reader for ``factory_ready.pool`` that names each hold in the trace."""

    def read(candidates: list[Any]) -> dict[int, str]:
        found = reasons(repo, candidates, prs)
        for reason in sorted(set(found.values())):
            numbers = sorted(number for number, why in found.items() if why == reason)
            print(f"[factory-controller] {reason}: {numbers}", file=sys.stderr)
        for candidate in candidates:
            if candidate.number in found:
                trace.skip(candidate.number, found[candidate.number])
        return found

    return read


def apply(repo: str, candidates: list[Any], prs: Iterable[Mapping[str, Any]]) -> list[Any]:
    """Drop every held candidate; a failed store read holds nothing."""
    found = reader(repo, prs)(candidates)
    return [c for c in candidates if c.number not in found]


def stamp(
    repo: str,
    prs: list[dict[str, Any]],
    issues: Iterable[Mapping[str, Any]] = (),
    incidents: Mapping[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Mark each PR the loop breaker holds, so backlog limits skip it; a failed read marks none.

    An active incident's PRs are never marked: owner-directed work is not throttled.
    """
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
        ) | incident.incident_prs(prs, incident.active(issues, incidents))
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


def released(repo: str, candidate: Any, prs: Iterable[Mapping[str, Any]]) -> bool:
    """A targeted or forced run on a parked PR ends the park; always True so callers can chain.

    The release goes to the store first; ``factory:parked`` goes with it. Only runs
    after the release count toward a new park. A failed write ends nothing.
    """
    if getattr(candidate, "kind", "") != "pr" or os.environ.get("GITHUB_ACTOR") in AUTOMATIC:
        return True
    pr = next((row for row in prs if int(row["number"]) == candidate.number), None)
    head = str((pr or {}).get("headRefOid") or "")
    try:
        from factory_turso_loops import release

        ended = release(repo, candidate.number, head, os.environ.get("GITHUB_RUN_ID", "local"))
    except Exception as exc:  # noqa: BLE001 - a failed release keeps the park, never the run
        print(f"[factory-controller] park release deferred ({type(exc).__name__})", file=sys.stderr)
        return True
    if ended:
        import factory_self_heal_rules as rules

        body = rules.comment_body(
            kind=rules.LOOP_RELEASED, pr=candidate.number, head=head or "none",
            ref=f"run-{os.environ.get('GITHUB_RUN_ID', 'local')}",
            text=f"An owner-directed run on #{candidate.number} ended its park; "
            "only later runs count toward a new one.",
        )
        subprocess.run(
            ["gh", "api", "--method", "POST", f"repos/{repo}/issues/{candidate.number}/comments",
             "-f", f"body={body}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    if ended and pr is not None and PARKED_LABEL in labels_of(pr):
        subprocess.run(
            ["gh", "api", "--method", "DELETE",
             f"repos/{repo}/issues/{candidate.number}/labels/{PARKED_LABEL.replace(':', '%3A')}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    if ended:
        print(f"[factory-controller] released the park on PR #{candidate.number}", file=sys.stderr)
    return True
