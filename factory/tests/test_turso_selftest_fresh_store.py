"""The hosted self-test works against a database that has never held the schema."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

libsql = importlib.import_module("factory_libsql")
selftest = importlib.import_module("factory_turso_selftest")


def test_a_fresh_store_gets_the_schema_before_the_sweep(tmp_path: Path) -> None:
    store = libsql.FileStore(tmp_path / "fresh.db")
    with pytest.raises(libsql.StoreError, match="no such table"):
        selftest.sweep(store)
    libsql.ensure_schema(store)
    assert selftest.sweep(store) == 0


def test_a_crash_writes_a_failed_summary_without_the_store_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://factory-db.example.turso.io"
    monkeypatch.setenv("FACTORY_TEST_LIBSQL_URL", url)

    def boom(namespace: str, *, measure: bool) -> dict[str, object]:
        raise libsql.StoreError(f"request to {url} failed")

    monkeypatch.setattr(selftest, "run", boom)
    out = tmp_path / "summary.json"
    assert selftest.main(["--namespace", "selftest-1-1", "--out", str(out)]) == 1
    summary = json.loads(out.read_text())
    assert summary["status"] == "failed"
    assert url not in summary["error"] and "<store>" in summary["error"]
