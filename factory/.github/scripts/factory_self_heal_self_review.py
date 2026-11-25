#!/usr/bin/env python3
"""Self-review after 6 hours: the factory grants the head-author exception itself.

A review run that finds no eligible reviewer (every eligible model authored the
PR's current head, ``factory_review_exclusion.for_pull``) ends ``no_capacity``
with "No reviewer model". The trusted Entry persister stores that worker event
with ``no_reviewer`` and the assigned head, so the store dates the wait.

When the first such event for a PR's current head is ``WAIT_SECONDS`` old and
the newest is at most ``FRESH_SECONDS`` old (the PR is still waiting), and the
exclusion still holds, the sweep grants self-review for that one head through
the same path as the owner's label (``factory_self_review_allowance``): a
trusted controller comment that names the head, and a ``self_review_allowed``
store event marked automatic, with its reason. A new head is not covered, and
the owner's ``factory:override``, a claim or an outside agent skips the PR.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import factory_item_loop as loop
import factory_self_heal as heal
import factory_self_review_allowance as allowance

WAIT_SECONDS = 6 * 3600
FRESH_SECONDS = 3 * 3600
LOOKBACK_SECONDS = 3 * 86400
MAX_PER_SWEEP = 2


def statement(repo: str) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT created_at, detail ->> 'selected_number' AS pr, detail ->> 'head' AS head"
        " FROM factory_state_events WHERE repo = :repo AND event_type = 'worker_idle'"
        " AND created_at > unixepoch() - :lookback AND detail ->> 'no_reviewer' = 1",
        {"repo": repo, "lookback": LOOKBACK_SECONDS},
    )


def waits(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[int, str], tuple[datetime, datetime]]:
    """(PR, head) -> first and newest time no reviewer model was eligible."""
    found: dict[tuple[int, str], tuple[datetime, datetime]] = {}
    for row in rows:
        if not str(row["pr"] or "").isdecimal() or not allowance.HEAD_RE.fullmatch(
            str(row["head"] or "")
        ):
            continue
        key = (int(row["pr"]), str(row["head"]))
        at = datetime.fromtimestamp(int(row["created_at"]), UTC)
        first, last = found.get(key, (at, at))
        found[key] = (min(first, at), max(last, at))
    return found


def due(wait: tuple[datetime, datetime] | None, now: datetime) -> bool:
    if wait is None:
        return False
    first, last = wait
    return now - first >= timedelta(seconds=WAIT_SECONDS) and now - last <= timedelta(
        seconds=FRESH_SECONDS
    )


def _head(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("sha") or "") if isinstance(head, Mapping) else ""


def grant(
    github: heal.GitHub, pull: Mapping[str, Any], wait: tuple[datetime, datetime]
) -> dict[str, Any]:
    """Re-check the exclusion on live data, then announce and record the grant."""
    import factory_review_exclusion as exclusion

    pr, head = int(pull["number"]), _head(pull)
    records = github.comments(pr)
    bodies = heal_bodies(records)
    if allowance.auto_granted(bodies, pr, head):
        return {"pr": pr, "status": "already granted"}
    found = exclusion.for_pull(lambda path: github.api(path), pr, pull=pull, records=records)
    if found.head != head or not found.excluded:
        return {"pr": pr, "status": "no head author is excluded"}
    hours = int((wait[1] - wait[0]).total_seconds() // 3600)
    reason = (
        f"no eligible reviewer for {hours}h: every eligible model authored head {head[:10]}"
        f" ({', '.join(found.excluded)})"
    )
    body = (
        f"The factory allowed self-review on this PR: {reason}.\n\n"
        f"It covers head `{head[:10]}` only. Every other merge gate still applies.\n\n"
        f"<!-- factory-self-review-auto pr={pr} head={head} -->"
    )
    github.api(f"issues/{pr}/comments", "--method", "POST", payload={"body": body})
    allowance.record_event(
        github.repo, pr, "self_review_allowed",
        {"grant": f"auto-{head}", "head": head, "actor": allowance.AUTO_ACTOR, "automatic": True,
         "reason": reason, "excluded": list(found.excluded)},
    )  # fmt: skip
    return {"pr": pr, "status": "self-review allowed", "head": head, "reason": reason}


def heal_bodies(records: Sequence[Mapping[str, Any]]) -> list[str]:
    import factory_review_policy as policy

    return policy.controller_comment_bodies(records)


def allow_long_waits(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Grant self-review on each PR whose current head waited 6 hours for any reviewer."""
    if not store.enabled():
        return []
    import factory_turso_core as core

    found = waits(core.store().reads([statement(github.repo)])[0])
    if not found:
        return []
    now = now or datetime.now(UTC)
    done: list[dict[str, Any]] = []
    granted = 0
    for pull in pulls:
        pr = int(pull.get("number") or 0)
        wait = found.get((pr, _head(pull)))
        if not due(wait, now):
            continue
        names = heal.labels(pull)
        if loop.bypassed(names) or any(name.startswith("agent:") for name in names):
            done.append({"pr": pr, "status": "owner or outside agent holds it"})
            continue
        if budget.dry_run or granted >= MAX_PER_SWEEP or not budget.take():
            done.append({"pr": pr, "status": "would-allow"})
            continue
        granted += 1
        done.append(grant(github, pull, wait))  # type: ignore[arg-type]
    return done
