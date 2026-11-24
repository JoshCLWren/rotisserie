"""The shipped SQLite/libSQL factory schema loads, is strict and indexes its hot lookups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[1] / ".github" / "factory-state.sqlite.sql"
TABLES = {
    "_schema_version",
    "factory_lease",
    "factory_provider_reservation",
    "factory_backoff",
    "factory_repo_state",
    "factory_model_health",
    "factory_pr_stage_retry",
    "factory_worker_metrics",
    "factory_runtime_event_keys",
    "factory_state_events",
    "factory_model_attempt_daily",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text())
    return conn


def _plan(conn: sqlite3.Connection, sql: str, *args: object) -> str:
    return " ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_schema_is_idempotent_strict_and_versioned() -> None:
    conn = _db()
    conn.executescript(SCHEMA.read_text())
    rows = conn.execute("SELECT name, strict FROM pragma_table_list WHERE schema='main'").fetchall()
    found = {name: strict for name, strict in rows if not name.startswith("sqlite_")}
    assert set(found) == TABLES
    assert all(found.values())
    assert conn.execute("SELECT version FROM _schema_version").fetchall() == [(1,)]


def test_every_table_carries_a_repository_column() -> None:
    conn = _db()
    for table in TABLES - {"_schema_version"}:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "repo" in columns, table


def test_event_id_is_unique_so_duplicates_insert_once() -> None:
    conn = _db()
    insert = (
        "INSERT OR IGNORE INTO factory_state_events"
        " (repo, origin, event_type, event_id, created_at) VALUES ('a/b','github-actions','x',?,1)"
    )
    assert conn.execute(insert, ("runtime:1",)).rowcount == 1
    assert conn.execute(insert, ("runtime:1",)).rowcount == 0
    conn.execute(insert, (None,))
    conn.execute(insert, (None,))
    assert conn.execute("SELECT count(*) FROM factory_state_events").fetchone() == (3,)


def test_strict_columns_and_json_reject_bad_values() -> None:
    conn = _db()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO factory_lease (lease_key, repo, kind, claim_id, acquired_at, expires_at)"
            " VALUES ('k','a/b','worker','c','soon',1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO factory_lease"
            " (lease_key, repo, kind, claim_id, acquired_at, expires_at, detail)"
            " VALUES ('k','a/b','worker','c',1,1,'not json')"
        )


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        (
            "SELECT 1 FROM factory_state_events WHERE event_id=?",
            "sqlite_autoindex_factory_state_events_1",
        ),
        (
            "SELECT count(*) FROM factory_state_events"
            " WHERE repo=? AND event_type=? AND created_at>=?",
            "idx_factory_events_type_time",
        ),
        (
            "SELECT worker FROM factory_state_events WHERE repo=? AND run_id=?"
            " AND event_type <> 'model_attempt'",
            "idx_factory_events_run",
        ),
        (
            "SELECT 1 FROM factory_state_events WHERE source=? AND model=? AND created_at>?"
            " AND event_type IN ('worker_failed', 'worker_completed')",
            "idx_factory_events_model",
        ),
        (
            "SELECT count(*) FROM factory_state_events WHERE repo=? AND event_type='worker_assigned'"
            " AND source=? AND created_at>=?",
            "idx_factory_events_assigned",
        ),
        (
            "SELECT source, model FROM factory_model_health WHERE repo=?"
            " AND status IN ('retired', 'unavailable', 'cooling')"
            " AND (status='retired' OR cooldown_until>?)",
            "idx_factory_model_health_blocking",
        ),
        (
            "SELECT 1 FROM factory_lease WHERE claim_id=?",
            "idx_factory_lease_claim",
        ),
        (
            "SELECT count(*) FROM factory_lease WHERE repo=? AND kind='external' AND wip=1",
            "idx_factory_lease_repo_kind",
        ),
        (
            "SELECT count(*) FROM factory_provider_reservation"
            " WHERE repo=? AND source=? AND expires_at>?",
            "idx_factory_provider_reservation_active",
        ),
        (
            "SELECT 1 FROM factory_backoff WHERE repo=? AND kind='model' AND expires_at>?",
            "idx_factory_backoff_repo_kind",
        ),
    ],
)
def test_hot_lookups_use_an_index_not_a_scan(sql: str, index: str) -> None:
    plan = _plan(_db(), sql, *([1] * sql.count("?")))
    assert index in plan, plan
    assert not plan.startswith("SCAN"), plan
