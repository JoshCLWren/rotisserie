"""Trusted event projection and heartbeat diagnostics."""
from __future__ import annotations
import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
runtime = importlib.import_module("factory_runtime_view")
heartbeat = importlib.import_module("factory_heartbeat_health")

def test_events_do_not_infer_success_from_running_status():
    view = runtime.project_events([
        {"event_type": "worker_started", "created_at": "2026-11-16T00:00:00Z",
         "detail": {"worker": "43", "run_id": "10", "status": "running"}},
        {"event_type": "worker_heartbeat", "created_at": "2026-11-16T00:05:00Z",
         "detail": {"worker": "43", "run_id": "10", "status": "finished"}},
        {"event_type": "worker_failed", "created_at": "2026-11-16T00:06:00Z",
         "detail": {"worker": "43", "run_id": "10", "outcome_class": "provider_quota",
                    "source": "requesty", "model": "x"}},
    ])
    assert view["heartbeats"]["43"]["outcome"] == "finished"
    assert view["attempts"]["43"]["attempt outcome"] == "provider_quota"

def test_stale_worker_is_visible_without_retry_authority():
    view = {"heartbeats": {"43": {"updated": "2026-11-15T23:00:00Z",
                                  "run": "10", "outcome": "finished"}},
            "attempts": {"43": {"attempt outcome": "provider_quota",
                                 "updated": "2026-11-16T00:00:00Z"}}}
    report = heartbeat.heartbeat_health_from_view(
        view, {"expected_workers": [43]}, now=datetime(2026, 11, 16, 1, tzinfo=UTC),
    )
    workers = report["workers"]
    assert isinstance(workers, list)
    assert workers[0]["freshness"] == "stale"
    assert workers[0]["runtime_status"] == "quota-limited"
    assert report["newest_active_age_seconds"] == 7200
    recovery = report["recovery_policy"]
    assert isinstance(recovery, str)
    assert "no per-worker stale retries" in recovery


def test_failed_execution_keeps_verified_persisted_delivery_separate():
    view = runtime.project_events([
        {"event_type": "worker_failed", "created_at": "2026-11-16T15:00:00Z",
         "detail": {"worker": "43", "run_id": "99", "outcome_class": "agent_timeout",
                    "source": "dynamic-source", "model": "publisher/discovered",
                    "persisted_work": True, "delivery_outcome": "persisted",
                    "delivery_pr": "5254"}},
    ])
    attempt = view["attempts"]["43"]
    assert attempt["attempt outcome"] == "agent_timeout"
    assert attempt["persisted_work"] == "true"
    assert attempt["delivery_outcome"] == "persisted"
    assert attempt["delivery_pr"] == "5254"



def test_delivery_evidence_uses_distinct_runs_and_prs_without_conflating_failures():
    import asyncio

    class Connection:
        async def fetchrow(self, sql, repo, event_types):
            assert repo == "owner/repo"
            assert "COUNT(DISTINCT detail->>'run_id')" in sql
            assert "COUNT(DISTINCT detail->>'work_pr')" in sql
            assert "checkpoint_pr_merged" in event_types
            assert "worker_failed" in event_types
            assert "created_at>now()-interval '7 days'" in sql
            return {
                "terminal_runs": 127,
                "successful_runs": 4,
                "failed_runs": 123,
                "checkpoint_runs": 18,
                "linked_prs": 6,
                "merged_prs": 2,
            }

    evidence = asyncio.run(runtime._delivery_evidence(Connection(), "owner/repo"))
    assert evidence == {
        "terminal_runs": 127, "successful_runs": 4, "failed_runs": 123,
        "checkpoint_runs": 18, "linked_prs": 6, "merged_prs": 2,
    }
    assert evidence["failed_runs"] > evidence["successful_runs"]



def test_evidence_sql_classifies_old_delivered_timeout_as_failed_execution() -> None:
    """Legacy worker_completed rows with agent_timeout are not successes."""
    import asyncio

    class Conn:
        async def fetchrow(self, query: str, *_args: object) -> dict:
            assert "detail->>'outcome_class'='success'" in query
            assert "'no_work','unconfigured'" in query
            assert "event_type='worker_completed'" in query
            return dict.fromkeys((
                "terminal_runs", "successful_runs", "failed_runs",
                "checkpoint_runs", "linked_prs", "merged_prs",
            ), 0)

    assert asyncio.run(runtime._delivery_evidence(Conn(), "owner/repo"))["failed_runs"] == 0
