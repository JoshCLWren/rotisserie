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
        " detail ->> 'delivery_outcome' AS delivery, model, outcome_class"
        " FROM factory_state_events"
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
                delivered=row.get("delivery") == "persisted",
            )
        )
    return found


def parks_statement(repo: str) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT created_at, detail ->> 'pr' AS pr, detail ->> 'head' AS head,"
        " detail ->> 'stage' AS stage, detail ->> 'kind' AS kind FROM factory_state_events"
        " WHERE repo = :repo AND event_type = 'factory_self_heal'"
        " AND detail ->> 'kind' IN (:kind, :release) AND created_at > unixepoch() - :lookback",
        {
            "repo": repo,
            "kind": loop.PARK_KIND,
            "release": loop.RELEASE_KIND,
            "lookback": PARK_LOOKBACK_SECONDS,
        },
    )


def parks(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per PR target: the newest park time and every (head, stage) it was parked at.

    A release (a forced or targeted run) newer than every park ends them all:
    the target keeps no parked head, and its runs count from the release.
    """
    found: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        rows, key=lambda row: (int(row["created_at"]), row.get("kind") == loop.RELEASE_KIND)
    )
    for row in ordered:
        if not str(row["pr"] or "").isdecimal():
            continue
        entry = found.setdefault(
            f"pr:{int(row['pr'])}", {"at": None, "at_heads": set(), "released": None}
        )
        at = _moment(row["created_at"])
        entry["at"] = at if entry["at"] is None or entry["at"] < at else entry["at"]
        if row.get("kind") == loop.RELEASE_KIND:
            entry["at_heads"] = set()
            entry["released"] = at
        else:
            entry["at_heads"].add((str(row["head"] or ""), str(row["stage"] or "")))
    return found


def park_ref(stage: str, head: str, park: Mapping[str, Any] | None) -> str:
    """The park event's ref; a park after a release gets a new one, so it can be recorded."""
    ref = f"{stage.removeprefix('factory:')}-{head[:12] or 'none'}"
    released = (park or {}).get("released")
    return f"{ref}-r{int(released.timestamp())}" if released else ref


def release(repo: str, number: int, head: str, run: str) -> bool:
    """Record that an owner-directed run ended this PR's park; True when a park was on."""
    import factory_turso_core as core

    if not core.enabled():
        return False
    parked = parks(core.store().reads([parks_statement(repo)])[0]).get(f"pr:{number}")
    if not parked or not parked["at_heads"]:
        return False
    import factory_self_heal as heal

    entry = heal.event(
        kind=loop.RELEASE_KIND, pr=number, head=head, ref=f"run-{run}", text="forced run"
    )
    return bool(heal.LedgerStore(repo).record([entry]))


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
