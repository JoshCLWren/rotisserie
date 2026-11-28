"""A measured pass names the statements that read the most rows, summed over its processes."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import factory_libsql as libsql
import factory_turso_core as core
import pytest
import yaml
from tests.workflow_files import adopter_view


def measured_store(monkeypatch: pytest.MonkeyPatch, path: Path) -> libsql.HttpStore:
    candidate = libsql.HttpStore("http://127.0.0.1:9")
    monkeypatch.setenv(core.METRICS_FILE_ENV, str(path))
    monkeypatch.setattr(libsql, "connect", lambda: candidate)
    monkeypatch.setattr(libsql, "ensure_schema", lambda _db: None)
    monkeypatch.setattr(core, "_STORE", None)
    assert core.store() is candidate
    return candidate


def test_a_measured_process_records_its_heaviest_statements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "counts.jsonl"
    db = measured_store(monkeypatch, path)
    assert db.trace == []
    for sql, read in (
        ("SELECT a FROM t WHERE x = :x", 700),
        ("SELECT b  FROM u", 5),
        ("SELECT a FROM t WHERE x = :x", 300),
    ):
        db._count({"rows_read": read, "rows_written": 0}, sql)
    core._record_usage(db)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["rows_read"] == 1005
    assert record["statements"][0] == {
        "sql": "SELECT a FROM t WHERE x = :x",
        "calls": 2,
        "rows_read": 1000,
        "rows_written": 0,
    }
    assert record["statements"][1]["sql"] == "SELECT b FROM u"


def test_an_unmeasured_process_keeps_the_trace_off(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = libsql.HttpStore("http://127.0.0.1:9")
    monkeypatch.delenv(core.METRICS_FILE_ENV, raising=False)
    monkeypatch.setattr(libsql, "connect", lambda: candidate)
    monkeypatch.setattr(libsql, "ensure_schema", lambda _db: None)
    monkeypatch.setattr(core, "_STORE", None)
    core.store()
    assert candidate.trace is None


def _report_step() -> str:
    workflow: Any = yaml.safe_load(adopter_view("fixed-model-factory-dispatch.yml"))
    steps = workflow["jobs"]["dispatch"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "Report factory query measurements")


def test_the_dispatch_report_sums_statements_over_processes(tmp_path: Path) -> None:
    lines = [
        {
            "requests": 2,
            "rows_read": 900,
            "rows_written": 0,
            "statements": [
                {"sql": "SELECT a", "calls": 3, "rows_read": 600, "rows_written": 0},
                {"sql": "SELECT b", "calls": 1, "rows_read": 300, "rows_written": 0},
            ],
        },
        {
            "requests": 1,
            "rows_read": 500,
            "rows_written": 6,
            "statements": [{"sql": "SELECT b", "calls": 2, "rows_read": 500, "rows_written": 6}],
        },
        {"requests": 1, "rows_read": 1, "rows_written": 0},
    ]
    (tmp_path / "factory-query-counts.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    summary = tmp_path / "summary.md"
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(summary)}
    out = subprocess.run(
        ["bash", "-c", _report_step()], env=env, text=True, capture_output=True, check=True
    ).stdout.splitlines()
    assert out[0] == "Factory store: 4 requests, 1401 rows read, 6 rows written, in 3 process(es)"
    assert out[1:] == [
        "store statement: 800 rows read, 6 written, 3 calls: SELECT b",
        "store statement: 600 rows read, 0 written, 3 calls: SELECT a",
    ]


def test_every_dispatch_pass_records_and_reports_its_store_reads() -> None:
    workflow: Any = yaml.safe_load(adopter_view("fixed-model-factory-dispatch.yml"))
    steps = {s.get("name"): s for s in workflow["jobs"]["dispatch"]["steps"]}
    enable = steps["Enable bounded factory query measurements"]
    assert "if" not in enable and core.METRICS_FILE_ENV in enable["run"]
    assert steps["Report factory query measurements"]["if"] == "always()"
