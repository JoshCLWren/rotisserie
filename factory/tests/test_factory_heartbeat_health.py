"""Per-worker freshness must not become per-worker recovery authority."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load_health() -> ModuleType:
    """Load the standalone heartbeat reporter."""
    spec = importlib.util.spec_from_file_location("heartbeat_health_test", ROOT / ".github/scripts/factory_heartbeat_health.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def record(worker: int, updated: str, *, attempt: bool = False, detail: str = "") -> dict[str, object]:
    """Construct a trusted registry heartbeat/attempt record."""
    kind = "factory-attempt-outcome" if attempt else "factory-heartbeat"
    return {"body": (
        f"<!-- {kind}:v1 worker=opencode-free-model-factory-{worker} -->\n"
        f"Worker: opencode-free-model-factory-{worker}\nUpdated: {updated}\nRun: 12345\n"
        f"Attempt outcome: provider_throttle\nDetail: {detail}\n"
    )}


def test_fresh_peer_does_not_hide_stale_expected_worker_or_include_retired() -> None:
    """Each active identity is measured independently; retired/paused rows are absent."""
    health = load_health()
    report = health.heartbeat_health(
        [record(45, "2026-10-10T11:55:00Z"), record(46, "2026-10-09T12:00:00Z"), record(47, "2026-08-01T12:00:00Z")],
        {"expected_workers": [45, 46, 48], "retired_workers": [47], "paused_workers": [48]},
        now=datetime(2026, 10, 10, 12, tzinfo=UTC),
    )
    assert report["affected_workers"] == ["46"]
    assert report["newest_active_age_seconds"] == 300
    assert [row["worker"] for row in report["workers"]] == ["45", "46"]
    assert report["workers"][1]["run"] == "12345"
    diagnostic = health.markdown_report(report)
    assert "opencode-free-model-factory-46 | stale" in diagnostic
    assert "2026-10-09T12:00:00Z" in diagnostic
    assert "opencode-free-model-factory-47" not in diagnostic


def test_missing_heartbeat_with_weekly_quota_attempt_is_not_a_crash() -> None:
    """A legitimate quota limit remains runtime evidence even with absent heartbeat."""
    report = load_health().heartbeat_health(
        [record(46, "2026-10-10T11:00:00Z", attempt=True, detail="Weekly quota exhausted")],
        {"expected_workers": [46]}, now=datetime(2026, 10, 10, 12, tzinfo=UTC),
    )
    row = report["workers"][0]
    assert row["freshness"] == "missing"
    assert row["runtime_status"] == "quota-limited"
    assert row["attempt_run"] == "12345"
    assert report["newest_active_age_seconds"] is None
    assert "no per-worker stale retries" in report["recovery_policy"]


def test_identity_timestamp_and_latest_record_are_not_inferred_from_comment_order() -> None:
    """Spoofed fields, future observations and old late-arriving comments are ignored."""
    invalid = record(45, "2026-10-10T11:59:00Z")
    invalid["body"] = str(invalid["body"]).replace("Worker: opencode-free-model-factory-45", "Worker: opencode-free-model-factory-46")
    report = load_health().heartbeat_health(
        [record(45, "2026-10-10T11:55:00Z"), record(45, "2026-10-10T11:00:00Z"), invalid, record(46, "2026-10-11T12:00:00Z"), record(46, "invalid")],
        {"expected_workers": [45, 46]}, now=datetime(2026, 10, 10, 12, tzinfo=UTC),
    )
    assert report["workers"][0]["updated"] == "2026-10-10T11:55:00Z"
    assert report["affected_workers"] == ["46"]


def test_recovery_keeps_fleet_gate_and_publishes_per_worker_diagnostics() -> None:
    """Partial staleness cannot directly dispatch a worker or bypass quota selection."""
    workflow = (ROOT / ".github/workflows/fixed-model-factory-dispatch-recovery.yml").read_text()
    assert "factory_heartbeat_health.py" in workflow
    assert '(( newest_age < 1200 ))' in workflow
    assert "--summary" in workflow
    assert "actions/upload-artifact@" in workflow
    assert "fixed-model-factory-entry" not in workflow
    assert "-f mode=roster" in workflow
    assert "--ref main" in workflow
