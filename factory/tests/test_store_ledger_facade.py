"""The ledger facade on a SQLite store: conflicts, outages, idempotency and operation leases."""
from __future__ import annotations

import importlib

import pytest

ledger = importlib.import_module("factory_state_ledger")
core = importlib.import_module("factory_turso_core")


@pytest.fixture
def file_store(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "store.db"))
    core.use_store(None)
    yield core.store()
    core.use_store(None)


@pytest.fixture
def no_store(monkeypatch):
    for name in ("FACTORY_TURSO_URL", "FACTORY_STATE_SQLITE", "FACTORY_STATE_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    core.use_store(None)
    yield
    core.use_store(None)


def test_claim_conflict_propagates_without_github_fallback(file_store):
    from tests.turso_support import uncapped_workers

    first, second = uncapped_workers(2)
    assert ledger.acquire_lease("owner/repo", first, [5001])
    assert ledger.acquire_lease("owner/repo", second, [5001]) is None


def test_db_error_is_not_silenced_after_configured_claim(monkeypatch, no_store):
    from factory_libsql import StoreUnavailable
    from tests.turso_support import free_port

    monkeypatch.setenv("FACTORY_TURSO_URL", f"http://127.0.0.1:{free_port()}")
    with pytest.raises(StoreUnavailable):
        ledger.acquire_lease("owner/repo", "42", [5001])


def test_terminal_event_idempotency_spans_classification_outcomes(file_store) -> None:
    """A single source run cannot be counted as both success and failure."""
    from tests.turso_support import uncapped_workers

    worker = uncapped_workers(1)[0]
    first = {"worker": worker, "outcome_class": "success", "persisted_work": True}
    second = {"worker": worker, "outcome_class": "provider_failure", "persisted_work": False}
    assert ledger.record_runtime_outcome("owner/repo", "777", first) == 1
    assert ledger.record_runtime_outcome("owner/repo", "777", second) == 0
    rows = file_store.read("SELECT event_type FROM factory_state_events WHERE event_id = 'runtime:777'")
    assert [row["event_type"] for row in rows] == ["worker_completed"]


def test_operation_lease_uses_the_worker_target_key(file_store):
    from tests.turso_support import uncapped_workers

    # Same key a worker claim checks, so a held sync blocks worker assignment.
    assert ledger._target_key("owner/repo", 5001) == ledger._keys("owner/repo", "1", [5001])[1]
    token = ledger.acquire_operation_lease("owner/repo", "sync-1", 5001)
    assert token
    assert ledger.acquire_lease("owner/repo", uncapped_workers(1)[0], [5001]) is None
    assert ledger.release_operation_lease("owner/repo", 5001, token, "done") is True


def test_lease_rows_describe_workers_and_operations(file_store):
    token = ledger.acquire_operation_lease("o/r", "sync-1", 9)
    (row,) = ledger.list_leases("o/r")
    assert row["claim_id"] == token and row["active"] is True
    assert row["operation"] == "sync-1" and row["targets"] == [9] and row["worker"] == ""
