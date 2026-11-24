"""The Neon to libSQL migration: counts, dry run, resume, mismatch refusal and the re-run guard."""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests import turso_migrate_support as support

libsql = importlib.import_module("factory_libsql")
migrate = importlib.import_module("factory_turso_migrate")
SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


@pytest.fixture
def db(tmp_path: Path) -> Any:
    return libsql.FileStore(tmp_path / "state.sqlite")


@pytest.fixture
def data() -> dict[str, list[dict[str, Any]]]:
    nvidia = support.workers()["nvidia"]
    return support.sample(nvidia[0], nvidia[1])


def _count(db: Any, table: str) -> int:
    return int(db.read(f"SELECT count(*) AS n FROM {table}")[0]["n"])


def test_a_real_run_copies_every_family_and_counts_it_back(db: Any, data: Any) -> None:
    summary = migrate.migrate(support.FakeNeon(data), db, repo=support.REPO, dry_run=False)
    assert summary["status"] == "ok", summary["mismatches"]
    groups = summary["migration"]
    lease = groups["kv:factory-lease-v1"]
    assert (lease["source"], lease["eligible"], lease["present"]) == (8, 7, 7)
    assert lease["skipped"] == {"claim_id_missing": 1} and lease["target"] == "factory_lease"
    assert groups["kv:factory-provider-backoff-v1"]["skipped"] == {"timestamp_invalid": 1}
    for name in ("worker", "model"):
        assert groups[f"kv:factory-{name}-backoff-v1"]["present"] == 1
    assert groups["kv:factory-credit-pool-v1"]["present"] == 1
    assert groups["kv:factory-model-health-reset-v1"]["target"] == "factory_repo_state"
    assert groups["kv:not_migrated"]["skipped"] == {
        "factory-projection-v1": 2,
        "factory-github-get-v1": 1,
        "open-lists": 1,
        "other": 1,
    }
    events = groups["factory_state_events"]
    assert (events["source"], events["eligible"], events["present"]) == (5, 3, 3)
    assert events["skipped"] == {"duplicate_key": 1, "detail_invalid": 1}
    assert events["notes"] == {"repo_defaulted": 1}
    assert groups["factory_model_health"]["skipped"] == {"status_invalid": 1}
    assert groups["factory_provider_budget"]["skipped"] == {"no_operator_limit": 2}
    assert groups["factory_provider_reservation"]["skipped"] == {"expired": 1}
    assert groups["factory_check_projection"]["skipped"] == {"retired_table": 3}
    assert groups["factory_reconciliation_cursor"]["source"] == 0
    for table, count in (
        ("factory_lease", 7),
        ("factory_backoff", 4),
        ("factory_repo_state", 1),
        ("factory_model_health", 2),
        ("factory_worker_metrics", 1),
        ("factory_provider_budget", 2),
        ("factory_provider_reservation", 1),
        ("factory_pr_stage_retry", 1),
        ("factory_runtime_event_keys", 2),
        ("factory_model_attempt_daily", 1),
        ("factory_state_events", 4),
    ):
        assert _count(db, table) == count, table
    assert summary["neon_tables"]["factory_state_kv"] == 19
    assert db.read("SELECT event_id FROM factory_state_events WHERE event_id = 'neon:1'")


def test_dry_run_writes_nothing_and_reports_the_three_checks(tmp_path: Path, data: Any) -> None:
    del data["factory_check_projection"]
    never = libsql.FileStore(tmp_path / "never.sqlite")
    summary = migrate.migrate(support.FakeNeon(data), never, repo=support.REPO, dry_run=True)
    assert summary["status"] == "ok" and summary["dry_run"] is True
    assert summary["checks"]["projection_keys"] == 2
    assert summary["checks"]["provider_budget_set"] == [
        {"source": "groq", "columns": ["cooldown_until"]},
        {"source": "kilo", "columns": ["daily_limit", "quota_reset_at"]},
        {"source": "nvidia", "columns": ["concurrency_limit"]},
    ]
    assert summary["neon_tables"] == {table: len(rows) for table, rows in data.items()}
    assert summary["absent_tables"] == ["factory_check_projection"]
    assert summary["migration"]["kv:factory-lease-v1"]["eligible"] == 7
    assert summary["migration"]["kv:factory-lease-v1"]["inserted"] == 0
    assert summary["batches"] == 0
    assert not list(tmp_path.iterdir())


def test_an_interrupted_run_resumes_without_duplicates(db: Any, data: Any) -> None:
    data["factory_state_events"] += support.events(400, start=10)

    class Failing:
        def __init__(self, inner: Any, after: int) -> None:
            self.inner, self.left = inner, after

        def batch(self, statements: Any) -> Any:
            if self.left == 0:
                raise libsql.StoreUnavailable("gone")
            self.left -= 1
            return self.inner.batch(statements)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    with pytest.raises(libsql.StoreUnavailable):
        migrate.migrate(
            support.FakeNeon(data), Failing(db, 12), repo=support.REPO, dry_run=False, batch=50
        )
    partial = _count(db, "factory_state_events")
    assert 0 < partial < 403
    first = migrate.migrate(support.FakeNeon(data), db, repo=support.REPO, dry_run=False, batch=50)
    assert first["status"] == "ok", first["mismatches"]
    events = first["migration"]["factory_state_events"]
    assert events["present"] == events["eligible"] == 403
    assert events["inserted"] == 403 - partial
    assert _count(db, "factory_state_events") == 404
    assert _count(db, "factory_lease") == 7


def test_a_completed_run_refuses_to_run_again_and_force_never_overwrites(
    db: Any, data: Any
) -> None:
    assert migrate.migrate(support.FakeNeon(data), db, repo=support.REPO, dry_run=False)[
        "status"
    ] == ("ok")
    again = migrate.migrate(support.FakeNeon(data), db, repo=support.REPO, dry_run=False)
    assert again["status"] == "already_migrated" and again["migration"] == {}
    db.batch([("UPDATE factory_model_health SET status = 'retired' WHERE model = 'ok-model'", {})])
    forced = migrate.migrate(
        support.FakeNeon(data), db, repo=support.REPO, dry_run=False, force=True
    )
    assert forced["status"] == "ok"
    assert sum(group["inserted"] for group in forced["migration"].values()) == 0
    assert _count(db, "factory_state_events") == 4
    status = db.read("SELECT status FROM factory_model_health WHERE model = 'ok-model'")
    assert status == [{"status": "retired"}]


def test_a_dropped_row_is_a_mismatch_and_writes_no_marker(
    db: Any, data: Any, tmp_path: Path
) -> None:
    libsql.ensure_schema(db)
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute(
            "CREATE TRIGGER lose AFTER INSERT ON factory_backoff WHEN NEW.kind = 'pool'"
            " BEGIN DELETE FROM factory_backoff WHERE backoff_key = NEW.backoff_key; END"
        )
    summary = migrate.migrate(support.FakeNeon(data), db, repo=support.REPO, dry_run=False)
    assert summary["status"] == "mismatch"
    assert summary["mismatches"] == [
        {"group": "kv:factory-credit-pool-v1", "check": "destination_count"}
    ]
    assert not migrate._migrated(db)


def test_a_source_that_loses_rows_while_paging_is_a_mismatch(db: Any, data: Any) -> None:
    class Short(support.FakeNeon):
        def count(self, table: str) -> int:
            return super().count(table) + (table == "factory_worker_metrics")

    summary = migrate.migrate(Short(data), db, repo=support.REPO, dry_run=False)
    assert summary["status"] == "mismatch"
    assert {"table": "factory_worker_metrics", "check": "rows_read"} in summary["mismatches"]


def test_a_lost_batch_response_is_resent_once(db: Any, data: Any) -> None:
    class Lossy:
        def __init__(self, inner: Any) -> None:
            self.inner, self.lost = inner, False

        def batch(self, statements: Any) -> Any:
            result = self.inner.batch(statements)
            if not self.lost and "factory_lease" in statements[0][0]:
                self.lost = True
                raise libsql.StoreAmbiguous("lost")
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    summary = migrate.migrate(support.FakeNeon(data), Lossy(db), repo=support.REPO, dry_run=False)
    assert summary["status"] == "ok", summary["mismatches"]
    assert _count(db, "factory_lease") == 7


def test_pages_are_bounded_and_keyset_ordered(db: Any, data: Any) -> None:
    data["factory_state_events"] += support.events(120, start=10)
    source = support.FakeNeon(data)
    summary = migrate.migrate(source, db, repo=support.REPO, dry_run=False, page=25, batch=40)
    assert summary["status"] == "ok"
    assert source.pages >= 125 // 25
    ids = [row["id"] for row in db.read("SELECT id FROM factory_state_events ORDER BY id")]
    created = [
        row["created_at"]
        for row in db.read("SELECT created_at FROM factory_state_events ORDER BY id")
    ]
    assert ids == sorted(ids) and len(created) == 124


def test_the_summary_holds_no_keys_claims_or_values(db: Any, data: Any) -> None:
    summary = migrate.migrate(support.FakeNeon(data), db, repo=support.REPO, dry_run=False)
    text = json.dumps(summary)
    for secret in (support.CLAIM, support.EXTERNAL, "something-private", "acme", support.MODEL):
        assert secret not in text


def test_the_cli_reports_a_failure_by_class_and_still_writes_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "dsn"
    secret.write_text("postgresql://role:hunter2@db.example.invalid/factory_state")
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(secret))
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    out = tmp_path / "summary.json"
    code = migrate.main(["--repo", support.REPO, "--dry-run", "--out", str(out)])
    assert code == 1
    written = json.loads(out.read_text())
    assert written == {"status": "failed", "dry_run": True, "error": "ModuleNotFoundError"}
    printed = capsys.readouterr()
    assert "hunter2" not in printed.out + printed.err and "example" not in printed.out


def test_the_dsn_drops_channel_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    neon = importlib.import_module("factory_turso_migrate_neon")
    secret = tmp_path / "dsn"
    secret.write_text("postgresql://r:p@h.example/db?sslmode=require&channel_binding=require\n")
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(secret))
    assert neon.dsn() == "postgresql://r:p@h.example/db?sslmode=require"


def test_the_tool_imports_no_neon_factory_module() -> None:
    code = (
        "import sys, factory_turso_migrate;"
        "bad = sorted(m for m in sys.modules if m.startswith('factory_state_'));"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SCRIPTS,
        env={"PYTHONPATH": str(SCRIPTS)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_two_row_pages_cross_every_composite_key(db: Any, data: Any, tmp_path: Path) -> None:
    for table in ("factory_runtime_event_keys", "factory_model_attempt_daily"):
        extra = [dict(row) for row in data[table]]
        for number, row in enumerate(extra):
            row.update({"run_id": 900 + number} if "run_id" in row else {"source": "zz"})
        data[table] += extra
    small = migrate.migrate(
        support.FakeNeon(data), db, repo=support.REPO, dry_run=False, page=2, batch=3
    )
    whole = migrate.migrate(support.FakeNeon(data), None, repo=support.REPO, dry_run=True, page=500)
    assert small["status"] == "ok", small["mismatches"]
    for name, group in whole["migration"].items():
        assert small["migration"][name]["eligible"] == group["eligible"], name
        assert small["migration"][name]["skipped"] == group["skipped"], name
