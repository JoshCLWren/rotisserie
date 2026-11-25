"""Two more state-label passes for the self-heal sweep.

* ``factory:incident-work`` is on an active incident's issue and its open PRs
  while the incident runs (``factory_incident.active``). ``factory:incident``
  stays the owner's input label. Reads: one issue per declared incident and one
  label listing.
* Closed issues and PRs lose every state label in one search per sweep, so a
  merged or closed item never keeps a stale ``factory:parked`` or similar.

Writes share the sweep's action budget and use the workflow token.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

import factory_incident as incident
import factory_self_heal as heal
import factory_state_labels as state

READ_ERRORS = (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired)
CLOSED_QUERY = """query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on Issue { number labels(first: 50) { nodes { name } } }
      ... on PullRequest { number labels(first: 50) { nodes { name } } }
    }
  }
}"""


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("number")
    return value if isinstance(value, int) else 0


def _take(budget: Any, done: list[dict[str, Any]], number: int, name: str, action: str) -> bool:
    if budget.dry_run or not budget.take():
        done.append({"item": number, "label": name, "status": f"would-{action}"})
        return False
    done.append({"item": number, "label": name, "action": action})
    return True


def active_incidents(github: heal.GitHub) -> set[int] | None:
    """Open declared incident issues that still carry the owner's label; None when unread."""
    declared = incident.declared()
    issues: list[Mapping[str, Any]] = []
    for number in declared:
        try:
            row = github.api(f"issues/{number}")
        except READ_ERRORS:
            return None
        if isinstance(row, Mapping):
            issues.append({**row, "number": number})
    return set(incident.active(issues, declared))


def sync_incident_work(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
) -> list[dict[str, Any]]:
    del store
    issues = active_incidents(github)
    if issues is None:
        return []
    try:
        labeled = github.api(state.INCIDENT_WORK_LISTING)
    except READ_ERRORS:
        return []
    rows: dict[int, Mapping[str, Any]] = {
        _number(row): row for row in labeled or [] if isinstance(row, Mapping)
    }
    wanted = {number: True for number in issues}
    for pr in pulls:
        rows.setdefault(_number(pr), pr)
        wanted[_number(pr)] = incident.linked_issue(pr) in issues
    for number in rows:
        wanted.setdefault(number, number in issues)
    done: list[dict[str, Any]] = []
    for number, want in sorted(wanted.items()):
        names = heal.labels(rows.get(number, {}))
        action = state.change(names, state.INCIDENT_WORK, want)
        if number and action and _take(budget, done, number, state.INCIDENT_WORK, action):
            state.write(github, number, state.INCIDENT_WORK, action)
    return done


def closed_query(repo: str) -> str:
    names = ",".join(f'"{name}"' for name in sorted(state.DEFINITIONS))
    return f"repo:{repo} is:closed label:{names} sort:updated-desc"


def clear_closed(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
) -> list[dict[str, Any]]:
    """Drop every state label from closed or merged items."""
    del store, pulls
    try:
        data = github.graphql(CLOSED_QUERY, q=closed_query(github.repo))
    except READ_ERRORS:
        return []
    nodes = ((data.get("data") or {}).get("search") or {}).get("nodes") or []
    done: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, Mapping) or not _number(node):
            continue
        names = {
            str(label.get("name"))
            for label in (node.get("labels") or {}).get("nodes") or []
            if isinstance(label, Mapping)
        }
        for name in sorted(names & set(state.DEFINITIONS)):
            if _take(budget, done, _number(node), name, "remove"):
                state.write(github, _number(node), name, "remove")
    return done
