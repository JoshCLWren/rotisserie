"""Read-only fleet Site telemetry does not expose tokens or invent retry eligibility."""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))
operations = importlib.import_module("factory_state_operations")


def test_dashboard_retry_export_is_bounded_and_sanitized() -> None:
    now = datetime(2026, 11, 16, 15, tzinfo=UTC)
    source = "fixture-" + __name__.rsplit(".", 1)[-1]
    worker = "42"

    class Connection:
        async def fetch(self, sql: str, *args: object) -> list[dict]:
            if "factory_state_kv" in sql:
                assert "LIMIT 750" in sql
                return [{
                    "cache_key": f"factory-worker-backoff-v1:owner/repo:worker:{worker}",
                    "payload": [{
                        "source": source, "model": "dynamic-catalog-selection",
                        "outcome": "agent_timeout", "streak": 3, "run_id": 123,
                        "next_retry_at": (now + timedelta(minutes=10)).isoformat(),
                        "suspended": False, "secret": "must-never-export",
                    }],
                    "expires_at": now + timedelta(days=90), "updated_at": now,
                }]
            if "factory_provider_reservation" in sql:
                assert "expires_at>now()" in sql
                return [{"worker": worker, "source": source,
                         "expires_at": now + timedelta(minutes=15)}]
            if "factory_pr_stage_retry" in sql:
                assert "retry_after>now()" in sql
                return [{"pr_number": 123, "head_sha": "a" * 40, "stage": "review",
                         "attempts": 2, "outcome": "provider_failure",
                         "retry_after": now + timedelta(minutes=20),
                         "last_run_id": 456}]
            raise AssertionError("Unexpected query")

    result = asyncio.run(operations._export_dashboard_retry(Connection(), "owner/repo"))
    assert len(result["backoffs"]) == 1
    assert result["backoffs"][0]["worker"] == worker
    assert result["backoffs"][0]["streak"] == 3
    assert result["backoffs"][0]["outcome"] == "agent_timeout"
    assert result["reservations"][0]["worker"] == worker
    assert result["pr_retries"][0]["pr"] == 123
    assert "must-never-export" not in str(result)
    assert "secret" not in str(result)
