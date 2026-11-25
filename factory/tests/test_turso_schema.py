"""Schema status and upgrade: what the store holds, the stored version and the recorded result."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

libsql = importlib.import_module("factory_libsql")
schema = importlib.import_module("factory_turso_schema")
INDEX = "idx_factory_backoff_repo_kind"


def _store(tmp_path: Path) -> object:
    store = libsql.FileStore(tmp_path / "store.db")
    libsql.ensure_schema(store)
    return store


def _sql(path: Path, *statements: str) -> None:
    conn = sqlite3.connect(path)
    for sql in statements:
        conn.execute(sql)
    conn.commit()
    conn.close()


def test_a_complete_schema_reports_every_object_present(tmp_path: Path) -> None:
    report = schema.status(_store(tmp_path))
    assert report["status"] == "ok" and report["reasons"] == []
    assert report["code_version"] == report["stored_version"] == libsql.SCHEMA_VERSION
    names = {item["name"] for item in report["objects"]}
    assert {"_schema_version", "_schema_upgrade", "factory_lease", INDEX} <= names
    assert all(item["present"] for item in report["objects"]) and report["missing"] == []
    kinds = {item["name"]: item["kind"] for item in report["objects"]}
    assert kinds["factory_lease"] == "table" and kinds[INDEX] == "index"
    last = report["last_upgrade"]
    assert last["result"] == "ok" and last["source"] == "runtime" and last["error"] is None
    assert set(last["created"]) == names


def test_a_missing_index_at_the_current_version_is_reported_and_apply_restores_it(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _sql(tmp_path / "store.db", f"DROP INDEX {INDEX}")
    report = schema.status(store)
    assert report["status"] == "missing" and report["missing"] == [INDEX]
    assert report["stored_version"] == libsql.SCHEMA_VERSION
    assert INDEX in report["reasons"][0]
    assert libsql.ensure_schema(store) is False
    assert schema.status(store)["missing"] == [INDEX]
    applied = schema.apply(store)
    assert applied["applied"] == "ok" and applied["apply_error"] is None
    assert applied["status"] == "ok" and applied["missing"] == []
    assert applied["last_upgrade"]["created"] == [INDEX]
    assert applied["last_upgrade"]["source"] == "apply"


def test_a_store_behind_this_code_is_reported_then_upgraded_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _sql(
        tmp_path / "store.db",
        "DROP TABLE _schema_upgrade",
        "DELETE FROM _schema_version WHERE version > 1",
    )
    report = schema.status(store)
    assert report["status"] == "missing" and report["stored_version"] == 1
    assert report["missing"] == ["_schema_upgrade"] and report["last_upgrade"] is None
    assert any("behind this code's" in reason for reason in report["reasons"])
    monkeypatch.setenv("GITHUB_WORKFLOW", "Factory Dispatch")
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    assert libsql.ensure_schema(store) is True
    after = schema.status(store)
    assert after["status"] == "ok" and after["stored_version"] == libsql.SCHEMA_VERSION
    last = after["last_upgrade"]
    assert last["created"] == ["_schema_upgrade"] and last["result"] == "ok"
    assert last["workflow"] == "Factory Dispatch" and last["run_id"] == "4242"


class RefusesUpgrades(libsql.FileStore):
    """Refuses the schema batch, as a token without DDL rights would, but takes the record."""

    def _send_batch(self, statements: Sequence[libsql.Statement]) -> list[list[libsql.Row]]:
        if len(statements) > 2:
            raise libsql.StoreError(
                "InsufficientPrivilegeError at https://db.example.turso.io: CREATE INDEX denied"
            )
        return super()._send_batch(statements)


def test_a_failed_upgrade_is_stored_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(tmp_path)
    _sql(tmp_path / "store.db", f"DROP INDEX {INDEX}")
    monkeypatch.setenv("FACTORY_TURSO_URL", "https://db.example.turso.io")
    store = RefusesUpgrades(tmp_path / "store.db")
    with pytest.raises(libsql.StoreError, match="InsufficientPrivilege"):
        libsql.ensure_schema(store, force=True)
    report = schema.status(store)
    assert report["status"] == "missing"
    last = report["last_upgrade"]
    assert last["result"] == "failed" and last["created"] is None
    assert "CREATE INDEX denied" in last["error"] and "turso.io" not in last["error"]
    assert any(reason.startswith("the last upgrade failed") for reason in report["reasons"])
    applied = schema.apply(store)
    assert applied["applied"] == "failed" and "<store>" in applied["apply_error"]


def test_the_status_reads_a_store_that_refuses_writes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = tmp_path / "store.db"
    _sql(path, "DELETE FROM _schema_version WHERE version > 1")
    os.chmod(path, stat.S_IRUSR)
    try:
        report = schema.status(store)
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    assert report["status"] == "behind" and report["stored_version"] == 1


def test_an_empty_database_has_every_object_missing(tmp_path: Path) -> None:
    report = schema.status(libsql.FileStore(tmp_path / "empty.db"))
    assert report["status"] == "missing" and report["stored_version"] is None
    assert len(report["missing"]) == len(report["objects"]) and report["last_upgrade"] is None


def test_the_command_line_prints_json_and_says_when_no_store_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    assert schema.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_configured"
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "cli.db"))
    assert schema.main(["apply"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert schema.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["last_upgrade"]["source"] == "apply"
