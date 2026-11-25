#!/usr/bin/env python3
"""Park looping PRs: the self-heal sweep's circuit breaker (``factory_item_loop``).

A park is a ``loop-parked`` store event that names the PR's head and stage,
then one trusted comment. The event goes first and the comment follows only a
fresh insert, so a repeated sweep or a failed store write never posts twice.
The dispatcher's pool pass reads that event and sends no worker of any kind
while the head is unchanged, whatever the stage: a failed worker's own release
flips the stage. An owner's targeted run, ``factory:override`` or claim still
reaches it; a new head releases the hold. An active incident's PRs are never
parked: owner-directed work is not throttled. ``factory:parked`` shows the hold
while it is in effect; the same pass removes it once the hold ends.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import factory_crash
import factory_incident as incident
import factory_item_loop as loop
import factory_self_heal as heal
import factory_self_heal_rules as rules
import factory_state_labels as state
from factory_turso_loops import park_ref, parked_heads
from factory_work_policy_base import stage_of


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("number")
    return value if isinstance(value, int) else 0


def _head(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("sha") or "") if isinstance(head, Mapping) else ""


def incident_pulls(github: heal.GitHub, pulls: Sequence[Mapping[str, Any]]) -> frozenset[int]:
    """Open PRs of an active incident; an unreadable incident issue counts as active."""
    declared = incident.declared()
    issues: list[Mapping[str, Any]] = []
    for number in declared:
        try:
            row = github.api(f"issues/{number}")
        except RuntimeError, ValueError, OSError, subprocess.TimeoutExpired:
            row = {"number": number, "labels": [incident.LABEL]}
        if isinstance(row, Mapping):
            issues.append({**row, "number": number})
    return incident.incident_prs(pulls, incident.active(issues, declared))


def park_text(found: loop.Loop, title: str) -> str:
    name = f"{title} (#{found.number})" if title else f"#{found.number}"
    return (
        f"Parked {name}: {found.text()} The factory sends no more workers to it at this head."
        f" {found.next_step()} An owner `factory:override` label also sends it back."
    )


def park_loops(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Park each looping open PR once per head."""
    if not store.enabled():
        return []
    threshold, window = loop.limits()
    runs, parks = store.loop_evidence(window)
    by_target = {f"pr:{_number(row)}": row for row in pulls if _number(row)}
    found = loop.find(
        runs,
        now=now or datetime.now(UTC),
        open_items={target: heal.labels(row) for target, row in by_target.items()},
        parked_at={target: park["at"] for target, park in parks.items()},
        threshold=threshold,
        window_seconds=window,
    )
    exempt = incident_pulls(github, pulls) if found else frozenset()
    done: list[dict[str, Any]] = []
    for item in found:
        with factory_crash.item_failure(f"self-heal park #{item.number}"):
            pull = by_target[item.target]
            names = heal.labels(pull)
            if item.number in exempt:
                done.append({"pr": item.number, "status": "incident", "text": item.text()})
                continue
            if loop.bypassed(names):
                done.append({"pr": item.number, "status": "owner", "text": item.text()})
                continue
            if any(name.startswith("agent:") for name in names):
                done.append({"pr": item.number, "status": "outside agent", "text": item.text()})
                continue
            head = _head(pull)
            stage = stage_of(names) or "factory:review"
            park = parks.get(item.target)
            if park and head in parked_heads(park):
                continue
            text = park_text(item, str(pull.get("title") or ""))
            if budget.dry_run or not budget.take():
                done.append({"pr": item.number, "status": "would-park", "text": text})
                continue
            ref = park_ref(stage, head, park)
            entry = heal.event(
                kind=rules.LOOP_PARKED,
                pr=item.number,
                head=head,
                ref=ref,
                text=text,
                stage=stage,
                loop=item.to_dict(),
            )
            if not store.record([entry]):
                continue
            body = rules.comment_body(
                kind=rules.LOOP_PARKED, pr=item.number, head=head, ref=ref, text=text
            )
            github.api(f"issues/{item.number}/comments", "--method", "POST", payload={"body": body})
            done.append({"kind": rules.LOOP_PARKED, "pr": item.number, "head": head, "text": text})
    return done


def sync_parked(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
) -> list[dict[str, Any]]:
    """Add ``factory:parked`` while the hold is in effect and remove it once it ends.

    Runs after ``park_loops``, so this pass reads the parks it just recorded.
    """
    if not store.enabled():
        return []
    parks = store.loop_evidence(loop.limits()[1])[1]

    def heads(row: Mapping[str, Any]) -> set[str]:
        park = parks.get(f"pr:{_number(row)}")
        return parked_heads(park) if park else set()

    rows = [row for row in pulls if state.PARKED in heal.labels(row) or _head(row) in heads(row)]
    if not rows:
        return []
    exempt = incident_pulls(github, pulls)
    done: list[dict[str, Any]] = []
    for row in rows:
        with factory_crash.item_failure(f"self-heal parked label #{_number(row)}"):
            number, names = _number(row), heal.labels(row)
            wanted = state.parked_in_effect(
                heads(row), _head(row), names, incident=number in exempt
            )
            action = state.change(names, state.PARKED, wanted)
            if action is None:
                continue
            if budget.dry_run or not budget.take():
                done.append({"pr": number, "label": state.PARKED, "status": f"would-{action}"})
                continue
            state.write(github, number, state.PARKED, action)
            done.append({"pr": number, "label": state.PARKED, "action": action})
    return done
