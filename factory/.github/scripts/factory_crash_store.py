"""Reads of the ``factory_crash`` table: recent groups, the newest traceback, retention.

One row is one occurrence. Groups are computed here by fingerprint, so a count
is always the number of stored occurrences in the window. The export carries no
traceback: it is published on the public status branch. ``newest`` returns the
full scrubbed traceback and runs only inside trusted jobs.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

RETENTION_DAYS = 14
ROW_LIMIT = 500
GROUP_LIMIT = 20
TIMES_LIMIT = 200
FIELDS = (
    "fingerprint, kind, fatal, script, function, line, exception_type, message,"
    " workflow, job, run_id, run_url, created_at"
)


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def recent_rows(store: Any, repo: str, since: int, limit: int = ROW_LIMIT) -> list[dict[str, Any]]:
    return list(
        store.read(
            f"SELECT {FIELDS} FROM factory_crash WHERE repo = :repo AND created_at >= :since"
            " ORDER BY created_at DESC, id DESC LIMIT :limit",
            {"repo": repo, "since": since, "limit": limit},
        )
    )


def group_rows(rows: list[dict[str, Any]], limit: int = GROUP_LIMIT) -> list[dict[str, Any]]:
    """Newest first; each group keeps its newest occurrence's details and every time."""
    groups: dict[str, dict[str, Any]] = {}
    for found in sorted(rows, key=lambda item: int(item["created_at"]), reverse=True):
        key = str(found["fingerprint"])
        group = groups.get(key)
        at = iso(int(found["created_at"]))
        if group is None:
            group = groups[key] = {
                "fingerprint": key,
                "kind": found["kind"],
                "fatal": bool(found["fatal"]),
                "script": found["script"],
                "function": found["function"],
                "line": int(found["line"]),
                "exception_type": found["exception_type"],
                "message": found["message"],
                "workflow": found["workflow"],
                "workflows": [],
                "count": 0,
                "first_at": at,
                "last_at": at,
                "last_run_url": found["run_url"],
                "times": [],
            }
        group["count"] += 1
        group["first_at"] = at
        if found["workflow"] and found["workflow"] not in group["workflows"]:
            group["workflows"].append(found["workflow"])
        if len(group["times"]) < TIMES_LIMIT:
            group["times"].append(at)
    return list(groups.values())[:limit]


def export(store: Any, repo: str, *, hours: int = 24, now: int | None = None) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    rows = recent_rows(store, repo, current - hours * 3600)
    return {
        "generated_at": iso(current),
        "window_hours": hours,
        "truncated": len(rows) >= ROW_LIMIT,
        "groups": group_rows(rows),
    }


def newest(store: Any, repo: str, fingerprint: str) -> dict[str, Any] | None:
    """The newest occurrence of one group (a fingerprint prefix of 6 or more), with traceback."""
    if len(fingerprint) < 6 or not fingerprint.isalnum():
        raise ValueError("a fingerprint is at least 6 letters and digits")
    rows = store.read(
        f"SELECT {FIELDS}, traceback FROM factory_crash WHERE repo = :repo"
        " AND fingerprint >= :low AND fingerprint < :high"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        {"repo": repo, "low": fingerprint, "high": fingerprint + "~"},
    )
    if not rows:
        return None
    found = dict(rows[0])
    count = store.read(
        "SELECT count(*) AS n, min(created_at) AS first FROM factory_crash"
        " WHERE repo = :repo AND fingerprint = :fingerprint",
        {"repo": repo, "fingerprint": found["fingerprint"]},
    )[0]
    found["count"] = int(count["n"])
    found["first_at"] = iso(int(count["first"]))
    found["last_at"] = iso(int(found.pop("created_at")))
    found["fatal"] = bool(found["fatal"])
    return found


def prune(store: Any, repo: str, *, now: int | None = None, days: int = RETENTION_DAYS) -> int:
    current = int(time.time()) if now is None else now
    rows = store.batch(
        [
            (
                "DELETE FROM factory_crash WHERE repo = :repo AND created_at < :before"
                " RETURNING id",
                {"repo": repo, "before": current - days * 86400},
            )
        ]
    )
    return len(rows[0]) if rows else 0
