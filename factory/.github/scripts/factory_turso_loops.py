"""Store reads behind the item-loop breaker: recent terminal worker runs and loop parks.

Two reads on the events time index. The rule itself is ``factory_item_loop``.
A park holds a PR while its head is one it was parked at, whatever its stage or
the kind of work offered: a failed worker's own release flips the stage. The
live rule holds a PR or an issue from the moment it loops until the sweep
parks it or its runs age out of the window.
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any

import factory_item_loop as loop

PARK_LOOKBACK_SECONDS = 30 * 86400
PARKED = "held: loop-parked"
LOOPING = "held: looping"


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
    db: Any,
    repo: str,
    heads: Mapping[int, str],
    issues: Collection[int] = (),
    exempt: Collection[int] = (),
) -> dict[int, str]:
    """Why the breaker holds each PR (by head) or issue: a park at that head, or a loop now."""
    targets = {f"pr:{number}": number for number in heads if number not in exempt}
    targets |= {f"issue:{number}": number for number in issues if number not in exempt}
    if not targets:
        return {}
    threshold, window = loop.limits()
    run_rows, park_rows = db.reads([runs_statement(repo, window), parks_statement(repo)])
    parked = parks(park_rows)
    hold = {
        number: PARKED
        for target, number in targets.items()
        if (park := parked.get(target)) and heads.get(number) in parked_heads(park)
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
        hold.setdefault(targets[found.target], LOOPING)
        print(
            f"[factory-controller] item loop hold: {found.target}: {found.text()}", file=sys.stderr
        )
    return hold


def parked_heads(park: Mapping[str, Any]) -> set[str]:
    return {head for head, _ in park["at_heads"] if head}


def evidence(
    repo: str, seconds: int
) -> tuple[dict[str, list[loop.Run]], dict[str, dict[str, Any]]]:
    import factory_turso_core as core

    statements = [runs_statement(repo, seconds), parks_statement(repo)]
    run_rows, park_rows = core.store().reads(statements)
    return runs_by_item(run_rows), parks(park_rows)
