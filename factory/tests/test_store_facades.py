"""The ledger and operations facades: every name callers use resolves, imports need no
PostgreSQL driver, and new claims pause on critical usage or a BLOCKED store."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests import turso_support as support

FACTORY = Path(__file__).resolve().parents[1]
SCRIPTS = FACTORY / ".github" / "scripts"
SRC = FACTORY.parent / "src"
ledger = importlib.import_module("factory_state_ledger")
operations = importlib.import_module("factory_state_operations")
libsql = support.libsql

LEDGER_NAMES = (
    "ClaimsPaused",
    "LEASE_PREFIX",
    "acquire_lease",
    "acquire_operation_lease",
    "acquire_slot",
    "attempt_slots",
    "enabled",
    "external_holder",
    "external_lease_action",
    "external_wip_count",
    "list_leases",
    "quota_reset_hints",
    "record_events",
    "record_lifecycle",
    "record_runtime_outcome",
    "recorded_runtime_runs",
    "refine_trusted_dynamic_models",
    "refine_trusted_failover_models",
    "refine_trusted_smoke_timeout",
    "release_expired_leases",
    "release_lease",
    "release_operation_lease",
    "release_worker",
    "renew_verified_workers",
    "required",
    "reserve_external",
    "revoke_external",
    "slot_holders",
    "slot_key",
    "slot_prefix",
    "target_lease",
    "worker_claim",
)
OPERATIONS_NAMES = (
    "cooling_prs",
    "daily_usage",
    "enabled",
    "export_backoffs",
    "export_candidate_health",
    "export_dashboard_retry",
    "export_fleet_failover_sources",
    "export_model_exclusions",
    "export_reliability",
    "filter_workers",
    "ingest_check",
    "rank_workers",
    "record_runtime",
    "required",
    "reset_model_health",
    "reset_quota_backoffs",
    "roster",
    "stats",
)
CRITICAL = {"status": "critical", "reasons": ["rows written at 97% of the monthly quota"]}


@pytest.mark.parametrize(
    ("facade", "names"), [(ledger, LEDGER_NAMES), (operations, OPERATIONS_NAMES)]
)
def test_every_name_callers_use_resolves(facade, names) -> None:
    assert [name for name in names if not hasattr(facade, name)] == []


def test_every_ledger_attribute_production_code_reads_exists() -> None:
    sources = [*SCRIPTS.glob("*.py"), *(SRC / "rotisserie").rglob("*.py")]
    used: dict[str, set[str]] = {}
    for path in sources:
        if path.name.startswith(("factory_state_", "factory_turso_")):
            continue
        for name in re.findall(r"\bledger\.([A-Za-z_]\w*)", path.read_text(encoding="utf-8")):
            used.setdefault(name, set()).add(path.name)
    missing = {name: sorted(files) for name, files in used.items() if not hasattr(ledger, name)}
    assert missing == {}


def test_facades_and_store_modules_import_without_a_postgres_driver() -> None:
    modules = sorted(
        path.stem
        for path in SCRIPTS.glob("factory_*.py")
        if path.stem.startswith(("factory_turso_", "factory_state_"))
    )
    assert "factory_state_ledger" in modules and "factory_state_operations" in modules
    code = (
        "import importlib, sys\n"
        "sys.modules['asyncpg'] = None\n"
        "sys.modules['psycopg'] = None\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo, tmp_path


def _report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, report: dict) -> None:
    path = tmp_path / "db-usage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setenv("FACTORY_DB_USAGE_FILE", str(path))


def _rows(db, repo: str) -> int:
    return sum(
        support.count(db, f"SELECT count(*) AS n FROM {table} WHERE repo = :r", r=repo)
        for table in support.TABLES
    )


def test_critical_usage_pauses_factory_picks_but_not_an_explicit_target(env, monkeypatch) -> None:
    db, repo, tmp_path = env
    first, second = support.uncapped_workers(2)
    _report(monkeypatch, tmp_path, CRITICAL)
    with pytest.raises(ledger.ClaimsPaused, match="critical"):
        ledger.acquire_lease(repo, first, [7])
    assert _rows(db, repo) == 0
    assert ledger.acquire_lease(repo, first, [7], explicit=True)
    _report(monkeypatch, tmp_path, {"status": "ok", "reasons": []})
    assert ledger.acquire_lease(repo, second, [8])


def test_critical_usage_pauses_slot_acquisition(env, monkeypatch) -> None:
    db, repo, tmp_path = env
    worker = support.workers_by_source()["nvidia"][0]
    _report(monkeypatch, tmp_path, CRITICAL)
    with pytest.raises(ledger.ClaimsPaused):
        ledger.acquire_slot(repo, worker, 7, ["attempt-1"])
    assert _rows(db, repo) == 0
    _report(monkeypatch, tmp_path, {"status": "warning", "reasons": ["rows read at 80%"]})
    assert ledger.acquire_slot(repo, worker, 7, ["attempt-1"])["status"] == "reserved"


def test_an_unreadable_usage_report_never_pauses(env, monkeypatch) -> None:
    _, repo, tmp_path = env
    monkeypatch.setenv("FACTORY_DB_USAGE_FILE", str(tmp_path / "missing.json"))
    assert ledger.acquire_lease(repo, support.uncapped_workers(1)[0], [7])


class BlockedStore(libsql.FileStore):
    def _send_batch(self, statements):
        raise libsql.StoreBlocked("Operation was blocked: BLOCKED (quota exceeded)")


def test_a_blocked_store_stops_acquisition_with_nothing_written(env) -> None:
    db, repo, tmp_path = env
    worker = support.workers_by_source()["nvidia"][0]
    support.core.use_store(BlockedStore(tmp_path / "state.sqlite"))
    with pytest.raises(libsql.StoreBlocked):
        ledger.acquire_lease(repo, worker, [7])
    with pytest.raises(libsql.StoreBlocked):
        ledger.acquire_slot(repo, worker, 8, ["attempt-1"])
    with pytest.raises(libsql.StoreBlocked):
        ledger.renew_verified_workers(repo, [worker])
    assert _rows(db, repo) == 0
