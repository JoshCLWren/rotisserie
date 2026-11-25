"""Carry retry history from one dispatch job to the next through the factory store.

The first assignment of every dispatch job read the whole 24-hour window of
repository comments again: 12 to 17 pages per job (runs 45639940782,
45640509224, 45641285594). The store keeps one row: the start time of the last
read and only the trusted retry markers, so the next job reads one delta page.
A full read still happens every ``FULL_READ_SECONDS``, because a delta never
shows deleted comments. Store errors only cost a full GitHub read.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

from factory_attempt_history import Comment, HistorySnapshot
from factory_work_policy import NO_DIFF_ATTEMPT_RE, NO_PROGRESS_RETIRE_RE, comment_is_trusted

NAME = "attempt-history"
FULL_READ_SECONDS = 6 * 3600


def _updated_epoch(comment: Comment) -> int | None:
    value = comment.get("updated_at")
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def compact(comment: Comment) -> Comment | None:
    """Only what the retry parser reads; None for comments it would ignore."""
    if not comment_is_trusted(comment):
        return None
    body = str(comment.get("body") or "")
    markers = [
        m.group(0)
        for regex in (NO_DIFF_ATTEMPT_RE, NO_PROGRESS_RETIRE_RE)
        for m in regex.finditer(body)
    ]
    if not markers:
        return None
    kept: dict[str, Any] = {"id": comment["id"], "body": "\n".join(markers)}
    for key in ("author_association", "updated_at"):
        if comment.get(key) is not None:
            kept[key] = comment[key]
    app = comment.get("performed_via_github_app")
    if isinstance(app, dict) and app.get("slug"):
        kept["performed_via_github_app"] = {"slug": app["slug"]}
    return kept


def compact_rows(rows: list[Comment], *, now: int, reset_seconds: int) -> list[Comment]:
    kept = []
    for row in rows:
        updated = _updated_epoch(row)
        if updated is not None and updated < now - reset_seconds:
            continue
        if (small := compact(row)) is not None:
            kept.append(small)
    return kept


class SharedHistory:
    """The store row for one repository's retry history."""

    def __init__(self, repo: str, *, reset_seconds: int) -> None:
        self.repo = repo
        self.reset_seconds = reset_seconds

    def load(self, *, now: int) -> HistorySnapshot | None:
        try:
            import factory_turso_cursors as cursors

            found = cursors.get(self.repo, NAME)
        except Exception as exc:
            print(
                f"[factory-controller] shared retry history unavailable ({type(exc).__name__})",
                file=sys.stderr,
            )
            return None
        if found is None:
            return None
        fetched, detail = found
        created, rows = detail.get("created_at"), detail.get("comments")
        if detail.get("reset_seconds") != self.reset_seconds or not isinstance(rows, list):
            return None
        if not isinstance(created, int) or not now - FULL_READ_SECONDS <= created <= fetched <= now:
            return None
        if not all(isinstance(row, dict) and isinstance(row.get("id"), int) for row in rows):
            return None
        return HistorySnapshot(created, fetched, int(detail.get("full_pages") or 1), rows)

    def save(self, snapshot: HistorySnapshot) -> None:
        rows = compact_rows(
            snapshot.comments, now=snapshot.fetched_at, reset_seconds=self.reset_seconds
        )
        detail = {
            "created_at": snapshot.created_at,
            "full_pages": snapshot.full_pages,
            "reset_seconds": self.reset_seconds,
            "comments": rows,
        }
        try:
            import factory_turso_cursors as cursors

            cursors.advance(self.repo, NAME, snapshot.fetched_at, detail)
        except Exception as exc:
            print(
                f"[factory-controller] shared retry history not saved ({type(exc).__name__})",
                file=sys.stderr,
            )


def for_repo(repo: str, *, reset_seconds: int) -> SharedHistory | None:
    """The shared history when this trusted job has the store; otherwise None."""
    try:
        import factory_turso_core as core

        if not core.enabled():
            return None
    except Exception:
        return None
    return SharedHistory(repo, reset_seconds=reset_seconds)
