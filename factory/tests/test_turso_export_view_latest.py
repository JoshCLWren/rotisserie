"""The runtime view reads only each worker's newest events (8,227 rows a call before, Nov 27)."""

from __future__ import annotations

import importlib
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests import turso_support as support

events = importlib.import_module("factory_turso_events")
view = importlib.import_module("factory_runtime_view")

FULL_READ = (
    "SELECT event_type, detail, created_at FROM factory_state_events"
    " WHERE repo = :repo AND event_type IN ('worker_started', 'worker_heartbeat',"
    " 'worker_outcome_signal', 'worker_completed', 'worker_idle', 'worker_failed')"
    " AND origin = :origin AND subject_type = 'worker' AND created_at > unixepoch() - 604800"
)


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state.sqlite"
    with support.bound(request.param, path, monkeypatch) as (db, repo):
        yield db, repo, path if request.param == "file" else None


def _event(repo: str, n: int, kind: str, worker: str, age: int, **extra: object) -> dict:
    stamp = int(time.time()) - age
    return {
        "event_type": kind,
        "event_id": f"{repo}:{n}",
        "age": age,
        "worker": worker,
        "run_id": str(500 + n),
        "status": "running" if kind == "worker_started" else "finished",
        "observed_at": datetime.fromtimestamp(stamp, UTC).isoformat(),
        **extra,
    }


def _seed(db, repo: str) -> None:
    a, b, c, d = (str(n) for n in (11, 12, 13, 14))
    rows = [
        _event(repo, 1, "worker_started", a, 900),
        _event(repo, 2, "worker_completed", a, 800, outcome_class="success"),
        _event(repo, 3, "worker_heartbeat", a, 100),
        _event(repo, 4, "worker_failed", a, 50, outcome_class="work_failure"),
        _event(repo, 5, "worker_started", b, 300),
        _event(repo, 6, "worker_heartbeat", b, 200),
        _event(repo, 7, "worker_idle", c, 8 * 86400),
        _event(repo, 8, "model_attempt", d, 60),
        *(_event(repo, 20 + n, "worker_heartbeat", a, 1000 + n) for n in range(40)),
    ]
    events.record_events(repo, rows)
    db.batch(
        [
            (
                "UPDATE factory_state_events SET created_at = unixepoch() - :age"
                " WHERE repo = :repo AND event_id = :id",
                {"age": row["age"], "repo": repo, "id": row["event_id"]},
            )
            for row in rows
        ]
    )


def test_the_view_matches_a_full_read_and_uses_the_worker_indexes(env, monkeypatch) -> None:
    db, repo, path = env
    _seed(db, repo)
    full = db.read(FULL_READ, {"repo": repo, "origin": events.ORIGIN})
    for row in full:
        row["created_at"] = datetime.fromtimestamp(int(row["created_at"]), UTC)
    expected = view.project_events(full)
    seen: list[tuple[str, dict]] = []
    original = db.read

    def capture(sql, params=None):
        seen.append((sql, dict(params or {})))
        return original(sql, params)

    monkeypatch.setattr(db, "read", capture)
    got = events.export_view(repo)
    assert {k: got[k] for k in ("attempts", "heartbeats")} == expected
    assert set(expected["attempts"]) == {"11"} and set(expected["heartbeats"]) == {"11", "12"}
    if path is None:
        return
    sql, params = seen[0]
    plan = " ".join(
        r[3] for r in sqlite3.connect(path).execute("EXPLAIN QUERY PLAN " + sql, params)
    )
    assert (
        "idx_factory_events_worker_terminal" in plan
        and "idx_factory_events_worker_heartbeat" in plan
    )
    assert "idx_factory_events_type_time" not in plan, plan
