"""Every read that read-token jobs run works on a store that refuses writes."""

from __future__ import annotations

import importlib
import os
import stat
from pathlib import Path

import pytest

from tests import turso_support as support

ledger = importlib.import_module("factory_state_ledger")
operations = importlib.import_module("factory_state_operations")
core = support.core
REPO = "acme/widgets"


@pytest.fixture
def read_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    path = tmp_path / "store.db"
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(path))
    core.use_store(None)
    db = core.store()
    worker, other = support.uncapped_workers(2)
    assert ledger.acquire_lease(REPO, worker, [11])
    ledger.record_lifecycle(REPO, "901", worker, "started")
    core.use_store(None)
    os.chmod(path, stat.S_IRUSR)
    yield db, worker, other
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    core.use_store(None)


def test_read_paths_never_write(read_only) -> None:
    _, worker, other = read_only
    assert {row["worker"] for row in ledger.list_leases(REPO)} == {worker}
    assert ledger.target_lease(REPO, 11)
    assert ledger.external_holder(REPO, 11) is None
    assert ledger.external_wip_count(REPO) == 0
    assert ledger.slot_holders(REPO, 11) == []
    assert ledger.worker_claim(REPO, worker) is not None
    assert ledger.recorded_runtime_runs(REPO, ["901"]) == set()
    assert ledger.history_rows(REPO, 7, 100, ("worker_started",))
    assert isinstance(operations.export_backoffs(REPO), dict)
    assert isinstance(operations.export_model_exclusions(REPO), dict)
    assert "models" in operations.export_candidate_health(REPO)
    assert "reservations" in operations.export_dashboard_retry(REPO)
    assert "models" in operations.export_reliability(REPO)
    assert operations.daily_usage(REPO, ["x"]) == {"x": 0}
    assert operations.cooling_prs(REPO, {5: ("a" * 40, "factory:review")}) == set()
    assert operations.rank_workers(REPO, [other]) == [other]
    assert "workers" in operations.stats(REPO)
    assert isinstance(operations.filter_workers(REPO, [other])[0], list)
    from factory_runtime_view import export_view, run_workers

    assert worker in export_view(REPO)["heartbeats"]
    assert run_workers(REPO, ["901"]) == {"901": worker}


def test_the_fixture_really_refuses_writes(read_only) -> None:
    with pytest.raises(support.libsql.StoreError):
        ledger.release_expired_leases(REPO)
