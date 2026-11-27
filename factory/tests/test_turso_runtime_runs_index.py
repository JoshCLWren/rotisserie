"""The recorded-run lookup reads by event_id, not every repo event (30,205 rows a call, Nov 27)."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from tests import turso_support as support

events = importlib.import_module("factory_turso_events")
operations = importlib.import_module("factory_turso_operations")
FILLER = 300


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state.sqlite"
    with support.bound(request.param, path, monkeypatch) as (db, repo):
        yield db, repo, path if request.param == "file" else None


def _seed(repo: str, run_id: str) -> None:
    worker = support.uncapped_workers(1)[0]
    source = operations.roster_source(worker)
    filler = [
        {
            "event_type": "model_attempt",
            "event_id": f"{repo}:f{n}",
            "worker": worker,
            "run_id": str(9000 + n),
            "source": source,
            "model": "m",
        }
        for n in range(FILLER)
    ]
    events.record_events(repo, filler)
    events.record_runtime_outcome(
        repo,
        run_id,
        {"worker": worker, "source": source, "model": "m", "outcome_class": "success"},
    )


def test_recorded_runs_use_the_event_id_index_and_keep_the_repo_filter(env, monkeypatch) -> None:
    db, repo, path = env
    other = support.repo_name()
    _seed(repo, "71")
    _seed(other, "72")
    seen: list[tuple[str, dict]] = []
    original = db.read

    def capture(sql, params=None):
        seen.append((sql, dict(params or {})))
        return original(sql, params)

    monkeypatch.setattr(db, "read", capture)
    before = db.usage.get("rows_read", 0)
    # The outcome reconciler asks about 100 runs at once; a short IN list hid the scan.
    asked = ["71", "72", *(str(n) for n in range(100, 198))]
    assert events.recorded_runtime_runs(repo, asked) == {"71"}
    try:
        if path is None:
            assert db.usage["rows_read"] - before < 20
            return
        sql, params = seen[-1]
        plan = [r[3] for r in sqlite3.connect(path).execute("EXPLAIN QUERY PLAN " + sql, params)]
        assert any("sqlite_autoindex_factory_state_events" in step for step in plan), plan
    finally:
        support.cleanup(db, other)
