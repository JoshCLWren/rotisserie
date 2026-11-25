"""Store reads behind the item-loop breaker: recent terminal worker runs and loop parks.

Two reads on the events time index. The rule itself is ``factory_item_loop``.
A park holds an item while its head and stage are the ones it was parked at;
the live rule holds it from the moment it loops until the sweep parks it.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

import factory_item_loop as loop

PARK_LOOKBACK_SECONDS = 30 * 86400


def _moment(epoch: Any) -> datetime:
    return datetime.fromtimestamp(int(epoch), UTC)


def runs_statement(repo: str, seconds: int) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT created_at, detail ->> 'selected_kind' AS kind,"
        " detail ->> 'selected_number' AS number, detail ->> 'action_kind' AS action,"
        " model, outcome_class FROM factory_state_events"
        " WHERE repo = :repo AND event_type IN ('worker_completed', 'worker_idle', 'worker_failed')"
        " AND created_at > unixepoch() - :seconds AND subject_type = 'worker'",
        {"repo": repo, "seconds": seconds},
    )


def runs_by_item(rows: list[dict[str, Any]]) -> dict[str, list[loop.Run]]:
    found: dict[str, list[loop.Run]] = {}
    for row in rows:
        kind, number = str(row["kind"] or ""), str(row["number"] or "")
        if kind not in ("issue", "pr") or not number.isdecimal():
            continue
        found.setdefault(f"{kind}:{int(number)}", []).append(
            loop.Run(
                at=_moment(row["created_at"]),
                kind=str(row["action"] or ""),
                model=str(row["model"] or ""),
                outcome=str(row["outcome_class"] or "unknown_failure"),
            )
        )
    return found


def parks_statement(repo: str) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT created_at, detail ->> 'pr' AS pr, detail ->> 'head' AS head,"
        " detail ->> 'stage' AS stage FROM factory_state_events"
        " WHERE repo = :repo AND event_type = 'factory_self_heal'"
        " AND detail ->> 'kind' = :kind AND created_at > unixepoch() - :lookback",
        {"repo": repo, "kind": loop.PARK_KIND, "lookback": PARK_LOOKBACK_SECONDS},
    )


def parks(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per PR target: the newest park time and every (head, stage) it was parked at."""
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not str(row["pr"] or "").isdecimal():
            continue
        entry = found.setdefault(f"pr:{int(row['pr'])}", {"at": None, "at_heads": set()})
        at = _moment(row["created_at"])
        entry["at"] = at if entry["at"] is None or entry["at"] < at else entry["at"]
        entry["at_heads"].add((str(row["head"] or ""), str(row["stage"] or "")))
    return found


def held(
    db: Any, repo: str, heads: dict[int, tuple[str, str]], exempt: frozenset[int] | set[int]
) -> set[int]:
    """PRs the breaker holds: parked at their current head and stage, or looping now."""
    targets = {f"pr:{number}": number for number in heads if number not in exempt}
    if not targets:
        return set()
    threshold, window = loop.limits()
    run_rows, park_rows = db.reads([runs_statement(repo, window), parks_statement(repo)])
    parked = parks(park_rows)
    hold = {
        number
        for target, number in targets.items()
        if (park := parked.get(target)) and heads[number] in park["at_heads"]
    }
    loops = loop.find(
        runs_by_item(run_rows),
        now=datetime.now(UTC),
        open_items={target: () for target in targets},
        parked_at={target: park["at"] for target, park in parked.items()},
        threshold=threshold,
        window_seconds=window,
    )
    for found in loops:
        hold.add(found.number)
        print(
            f"[factory-controller] item loop hold: {found.target}: {found.text()}", file=sys.stderr
        )
    return hold


def evidence(
    repo: str, seconds: int
) -> tuple[dict[str, list[loop.Run]], dict[str, dict[str, Any]]]:
    import factory_turso_core as core

    statements = [runs_statement(repo, seconds), parks_statement(repo)]
    run_rows, park_rows = core.store().reads(statements)
    return runs_by_item(run_rows), parks(park_rows)
