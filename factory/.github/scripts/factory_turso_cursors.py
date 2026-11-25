"""One-row cursors on the libSQL store: a watermark plus its compact state.

``advance`` never moves a cursor backwards, so a slower job cannot replace a
newer snapshot. ``claim_interval`` lets one job per interval run a periodic
sweep: the row's time moves inside the same statement that checks it.
"""

from __future__ import annotations

import json
from typing import Any

import factory_turso_core as core

TABLE = "factory_cursor"


def get(repo: str, name: str) -> tuple[int, dict[str, Any]] | None:
    rows = core.store().read(
        f"SELECT cursor_at, detail FROM {TABLE} WHERE repo = :repo AND name = :name",
        {"repo": repo, "name": name},
    )
    if not rows:
        return None
    detail = json.loads(rows[0]["detail"] or "{}")
    return int(rows[0]["cursor_at"]), detail if isinstance(detail, dict) else {}


def advance(repo: str, name: str, cursor_at: int, detail: dict[str, Any]) -> bool:
    """Store a newer watermark; False when the stored one is already as new."""
    found = core.store().batch(
        [
            (
                f"INSERT INTO {TABLE} (repo, name, cursor_at, updated_at, detail)"
                " VALUES (:repo, :name, :at, unixepoch(), :detail)"
                " ON CONFLICT (repo, name) DO UPDATE SET cursor_at = excluded.cursor_at,"
                " updated_at = excluded.updated_at, detail = excluded.detail"
                f" WHERE excluded.cursor_at > {TABLE}.cursor_at RETURNING cursor_at",
                {
                    "repo": repo,
                    "name": name,
                    "at": int(cursor_at),
                    "detail": json.dumps(detail, separators=(",", ":")),
                },
            )
        ]
    )
    return bool(found and found[0])


def claim_interval(repo: str, name: str, seconds: int) -> bool:
    """True for the one caller that finds the cursor at least ``seconds`` old."""
    found = core.store().batch(
        [
            (
                f"INSERT INTO {TABLE} (repo, name, cursor_at, updated_at)"
                " VALUES (:repo, :name, unixepoch(), unixepoch())"
                " ON CONFLICT (repo, name) DO UPDATE SET cursor_at = excluded.cursor_at,"
                " updated_at = excluded.updated_at"
                f" WHERE {TABLE}.cursor_at <= unixepoch() - :seconds RETURNING cursor_at",
                {"repo": repo, "name": name, "seconds": int(seconds)},
            )
        ]
    )
    return bool(found and found[0])
