"""Reuse retry history within a dispatch pass and across passes, refreshing changed comments."""

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sys
import tempfile
from typing import Protocol


Comment = dict[str, object]
FetchPages = Callable[[int], tuple[list[Comment], int]]
MAX_PASS_SECONDS = 3600


@dataclass(frozen=True)
class HistorySnapshot:
    """A bounded pass snapshot, including the start of its last successful read."""

    created_at: int
    fetched_at: int
    full_pages: int
    comments: list[Comment]


def _comments(value: object) -> list[Comment]:
    if not isinstance(value, list):
        raise ValueError("comment history must be a list")
    result: list[Comment] = []
    for comment in value:
        if not isinstance(comment, dict):
            raise ValueError("comment history contains an invalid row")
        identity = comment.get("id")
        if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
            raise ValueError("comment history contains an invalid identity")
        result.append(comment)
    return result


def _read_snapshot(
    path: Path, *, now: int, reset_seconds: int, scope: str,
) -> HistorySnapshot | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        if value.get("version") != 1 or value.get("scope") != scope:
            return None
        if value.get("reset_seconds") != reset_seconds:
            return None
        created, fetched, pages = (
            value.get("created_at"), value.get("fetched_at"), value.get("full_pages")
        )
        if any(not isinstance(v, int) or isinstance(v, bool) for v in (created, fetched, pages)):
            return None
        if not isinstance(created, int) or not isinstance(fetched, int) or not isinstance(pages, int):
            return None
        if not now - MAX_PASS_SECONDS <= created <= fetched <= now or pages < 1:
            return None
        return HistorySnapshot(created, fetched, pages, _comments(value.get("comments")))
    except (OSError, ValueError):
        return None


def _write_snapshot(
    path: Path, snapshot: HistorySnapshot, *, reset_seconds: int, scope: str,
) -> None:
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
        ) as output:
            pending = Path(output.name)
            json.dump({
                "version": 1, "scope": scope, "reset_seconds": reset_seconds,
                "created_at": snapshot.created_at, "fetched_at": snapshot.fetched_at,
                "full_pages": snapshot.full_pages, "comments": snapshot.comments,
            }, output)
        pending.replace(path)
    except OSError as exc:
        raise RuntimeError("retry history snapshot could not be persisted") from exc
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


class Shared(Protocol):
    def load(self, *, now: int) -> HistorySnapshot | None: ...
    def save(self, snapshot: HistorySnapshot) -> None: ...


def read_comment_history(
    *, now_epoch: int, reset_seconds: int, scope: str,
    cache_path: Path | None, fetch_pages: FetchPages, shared: Shared | None = None,
) -> list[Comment]:
    """Read full history once per pass, then fully paginate fresh changed comments.

    Args:
        now_epoch: Query start time, captured before any paginated read.
        reset_seconds: Existing retry-policy history window.
        scope: Repository identity; snapshots cannot cross repositories.
        cache_path: Trusted runner-local, unique-per-pass file, or None for live reads.
        fetch_pages: Fetch every page since the supplied epoch; errors must propagate.
        shared: Cross-job snapshot, read and saved by the first read of each pass.

    Returns:
        Fresh comment rows for the existing trusted-marker/window parser.
    """
    snapshot = (
        _read_snapshot(cache_path, now=now_epoch, reset_seconds=reset_seconds, scope=scope)
        if cache_path is not None else None
    )
    source = "delta" if snapshot is not None else "full"
    if snapshot is None and shared is not None:
        snapshot = shared.load(now=now_epoch)
        source = "shared" if snapshot is not None else source
    # GitHub timestamps have second precision. Overlap the previous query start,
    # not its completion, so updates during pagination cannot fall into a gap.
    since = max(0, snapshot.fetched_at - 1 if snapshot else now_epoch - reset_seconds)
    rows, pages = fetch_pages(since)
    if pages < 1:
        raise RuntimeError("retry history pagination returned no pages")
    if snapshot is not None:
        merged = {comment["id"]: comment for comment in snapshot.comments}
        try:
            merged.update({comment["id"]: comment for comment in _comments(rows)})
        except ValueError as exc:
            raise RuntimeError("retry history returned invalid comment identities") from exc
        rows = list(merged.values())
    if cache_path is not None:
        try:
            rows = _comments(rows)
        except ValueError as exc:
            raise RuntimeError("retry history returned invalid comment identities") from exc
    current = HistorySnapshot(
        snapshot.created_at if snapshot else now_epoch,
        now_epoch, snapshot.full_pages if snapshot else pages, rows,
    )
    if cache_path is not None:
        # A shared snapshot may predate this pass; the pass bound starts now.
        local = replace(current, created_at=now_epoch) if source == "shared" else current
        _write_snapshot(cache_path, local, reset_seconds=reset_seconds, scope=scope)
    if shared is not None and source != "delta":
        shared.save(current)
    print(
        f"[factory-controller] attempt-history source={source} "
        f"pages_read={pages} comments={len(rows)} "
        f"full_history_pages={current.full_pages}",
        file=sys.stderr,
    )
    return rows
