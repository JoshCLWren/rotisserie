"""The self-heal sweep's state-label pass: duplicate, cooling and gate-rejected PR labels.

Each label mirrors a fact the sweep already has or reads cheaply:

* ``factory:duplicate``: an older open PR delivers the same issue (open-PR list).
* ``factory:cooling``: the store holds the PR with exact-head retry cooling or
  review churn, not a park (one store read for the managed PRs).
* ``factory:gate-rejected``: the newest acceptance-gate record at the PR's head
  is a rejection. The review controller adds it; this pass reads comments only
  for PRs that carry it, and drops it once the head moves or the gate passes.

Writes share the sweep's action budget and use the workflow token.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import factory_self_heal as heal
import factory_self_heal_rules as rules
import factory_state_labels as state
from factory_work_policy_base import is_factory_managed_pr, stage_of

HoldsReader = Callable[[str, dict[int, tuple[str, str]], frozenset[int], frozenset[int]], dict]


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("number")
    return value if isinstance(value, int) else 0


def _head(row: Mapping[str, Any]) -> tuple[str, str]:
    head = row.get("head")
    if not isinstance(head, Mapping):
        return "", ""
    return str(head.get("sha") or ""), str(head.get("ref") or "")


def _default_holds(
    repo: str, heads: dict[int, tuple[str, str]], issues: frozenset[int], exempt: frozenset[int]
) -> dict[int, str]:
    from factory_state_operations import holds

    return holds(repo, heads, issues, exempt)


def cooling(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    reader: HoldsReader | None = None,
) -> dict[int, bool] | None:
    """Managed PR number -> whether a non-park dispatch hold is on; None when unknown."""
    from factory_work_policy_backlog import LOOP_HOLDS

    if not store.enabled():
        return None
    managed = [row for row in pulls if is_factory_managed_pr({**row, "headRefName": _head(row)[1]})]
    heads = {
        _number(row): (_head(row)[0], stage_of(heal.labels(row)) or "factory:review")
        for row in managed
    }
    exempt = frozenset(_number(row) for row in managed if state.BYPASS & heal.labels(row))
    try:
        found = (reader or _default_holds)(github.repo, heads, frozenset(), exempt)
    except Exception:  # noqa: BLE001 - an unreadable store changes no label
        return None
    return {number: found.get(number) not in (None, *LOOP_HOLDS) for number in heads}


def wanted(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    reader: HoldsReader | None = None,
) -> dict[int, dict[str, bool]]:
    """Per open PR, each state label the sweep can settle now."""
    duplicate = state.duplicates([{**row, "headRefName": _head(row)[1]} for row in pulls])
    cool = cooling(github, store, pulls, reader)
    found: dict[int, dict[str, bool]] = {}
    for row in pulls:
        number = _number(row)
        if not number:
            continue
        labels = heal.labels(row)
        found[number] = {state.DUPLICATE: duplicate.get(number, False)}
        if cool is not None and number in cool:
            found[number][state.COOLING] = cool[number]
        if state.GATE_REJECTED in labels:
            try:
                bodies = rules.policy.controller_comment_bodies(github.comments(number))
            except RuntimeError, ValueError, OSError, subprocess.TimeoutExpired:
                continue
            found[number][state.GATE_REJECTED] = state.gate_rejected_at(
                bodies, number, _head(row)[0]
            )
    return found


def sync_state(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
    *,
    reader: HoldsReader | None = None,
) -> list[dict[str, Any]]:
    by_number = {_number(row): row for row in pulls}
    done: list[dict[str, Any]] = []
    for number, labels in sorted(wanted(github, store, pulls, reader).items()):
        names = heal.labels(by_number[number])
        for name, want in sorted(labels.items()):
            action = state.change(names, name, want)
            if action is None:
                continue
            if budget.dry_run or not budget.take():
                done.append({"pr": number, "label": name, "status": f"would-{action}"})
                continue
            state.write(github, number, name, action)
            done.append({"pr": number, "label": name, "action": action})
    return done
