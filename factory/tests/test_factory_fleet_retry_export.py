"""Read-only fleet Site telemetry does not expose tokens or invent retry eligibility."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))
from tests import turso_support as support  # noqa: E402

operations = importlib.import_module("factory_state_operations")


def test_dashboard_retry_export_is_bounded_and_sanitized(tmp_path, monkeypatch) -> None:
    source = "fixture-" + __name__.rsplit(".", 1)[-1]
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        worker = support.uncapped_workers(1)[0]
        support.backoff(
            db,
            repo,
            "worker",
            worker=worker,
            source=source,
            model="dynamic-catalog-selection",
            outcome="agent_timeout",
            streak=3,
            retry_in=600,
            detail={"suspended": False, "secret": "must-never-export"},
        )
        assert importlib.import_module("factory_state_ledger").acquire_lease(repo, worker, [9])
        operations.ingest_check(
            repo,
            {
                "workflow_run": {
                    "status": "completed",
                    "conclusion": "failure",
                    "name": "Local Ollama Review",
                    "head_sha": "a" * 40,
                    "id": 456,
                    "pull_requests": [{"number": 123}],
                }
            },
        )
        result = operations.export_dashboard_retry(repo)
    assert len(result["backoffs"]) == 1
    assert result["backoffs"][0]["worker"] == worker
    assert result["backoffs"][0]["streak"] == 3
    assert result["backoffs"][0]["outcome"] == "agent_timeout"
    assert result["reservations"][0]["worker"] == worker
    assert result["pr_retries"][0]["pr"] == 123
    assert result["pr_retries"][0]["run_id"] == 456
    assert "must-never-export" not in str(result)
    assert "secret" not in str(result)
