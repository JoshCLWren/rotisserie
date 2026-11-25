#!/usr/bin/env python3
"""Park looping PRs: the self-heal sweep's circuit breaker (``factory_item_loop``).

A park is a ``loop-parked`` store event that names the PR's head and stage,
then one trusted comment. The event goes first and the comment follows only a
fresh insert, so a repeated sweep or a failed store write never posts twice.
The dispatcher's pool pass reads that event and sends no worker of any kind
while the head is unchanged, whatever the stage: a failed worker's own release
flips the stage. Nothing changes the PR's labels, so an owner's targeted run,
``factory:override`` or claim still reaches it; a new head releases the hold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import factory_item_loop as loop
import factory_self_heal as heal
import factory_self_heal_rules as rules
from factory_turso_loops import parked_heads
from factory_work_policy_base import stage_of


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("number")
    return value if isinstance(value, int) else 0


def _head(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("sha") or "") if isinstance(head, Mapping) else ""


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
    done: list[dict[str, Any]] = []
    for item in found:
        pull = by_target[item.target]
        names = heal.labels(pull)
        if loop.bypassed(names):
            done.append({"pr": item.number, "status": "owner", "text": item.text()})
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
        ref = f"{stage.removeprefix('factory:')}-{head[:12] or 'none'}"
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
