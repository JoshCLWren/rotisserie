"""Pure backoff and retry policy of the factory operations; no store connection."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
operations = importlib.import_module("factory_state_operations")
REPO = "acme/widgets"


def test_provider_exponential_backoff_doubles_and_caps():
    base = operations.exponential_provider_delay
    assert base("provider_throttle", None, 0, float("inf")) == 1800
    assert base("provider_throttle", "provider_throttle", 1800, 1800) == 3600
    assert base("provider_throttle", "provider_throttle", 3600, 3600) == 7200
    assert base("provider_failure", "provider_throttle", 9000, 5) == 14400
    assert base("provider_throttle", "provider_throttle", 14400, 13 * 3600) == 1800
    assert base("provider_throttle", "success", 14400, 60) == 1800


def test_model_scoped_provider_route_never_cools_unrelated_routes():
    assert "omniroute-free" in operations.MODEL_SCOPED_SOURCES
    assert "nvidia" not in operations.MODEL_SCOPED_SOURCES
    assert operations.MODEL_COOLDOWNS["model_unavailable"] == 21600


def test_exact_head_checks_are_advisory_not_merge_authorization():
    workflow = (ROOT.parent / ".github/workflows/factory-check-projection.yml").read_text()
    assert "factory_state_operations.py ingest-check" in workflow
    assert "gh workflow run fixed-model-factory-dispatch.yml" in workflow
    assert "factory_check_projection" not in workflow
    assert "url: ${{ secrets.FACTORY_TURSO_URL }}" in workflow
    assert "url: \\${{" not in workflow
    assert "pull_request_target:" not in workflow
    assert "gh pr merge" not in workflow


def test_intermediate_model_failures_are_validated_as_model_scoped():
    failures = [
        {"model": f"generated/model-{index}", "outcome": outcome}
        for index, outcome in enumerate(
            ("provider_quota", "provider_throttle", "model_unavailable")
        )
    ]
    assert operations.validate_intermediate_model_failures(failures, "openrouter-free") == [
        ("openrouter-free", item["model"], item["outcome"], "model") for item in failures
    ]


def test_fibonacci_retry_progresses_to_a_one_day_cap():
    fib = operations.fibonacci_worker_delay
    assert [fib(i) for i in range(1, 8)] == [300, 300, 600, 900, 1500, 2400, 3900]
    assert fib(13) == 233 * 300 and fib(14) == fib(20) == fib(500) == 86400


def test_fibonacci_retry_at_is_capped_at_one_day():
    observed = datetime(2026, 11, 17, tzinfo=UTC)
    assert operations.fibonacci_retry_at(observed, 3) == observed + timedelta(seconds=600)
    assert operations.fibonacci_retry_at(observed, 30) - observed == timedelta(days=1)
    assert operations.fibonacci_retry_at(observed, 200) == observed + timedelta(days=1)


def test_retry_time_and_retained_streak_are_independent_of_ttl():
    now = datetime.now(UTC)
    retry = now + timedelta(minutes=5)
    record = {
        "payload": [
            {
                "source": "vercel-ai-gateway",
                "model": "reflection/beam-free",
                "streak": 7,
                "next_retry_at": retry.isoformat(),
                "suspended": False,
            }
        ],
        "expires_at": now + timedelta(days=90),
    }
    assert operations.worker_backoff_reason(
        record, "vercel-ai-gateway", "reflection/beam-free", now
    )
    assert (
        operations.worker_backoff_reason(
            record, "vercel-ai-gateway", "reflection/beam-free", retry + timedelta(seconds=1)
        )
        is None
    )
    assert operations.worker_backoff_reason(record, "vercel-ai-gateway", "new-pin", now) is None


def test_missing_credentials_are_not_inference_failures():
    # The upstream pre-lease gate rejects missing credentials entirely.
    # A runner in flight during cutover still reports this separately.
    assert "unconfigured" not in operations.WORKER_RETRY_CLASSES
    assert "unknown_failure" in operations.WORKER_RETRY_CLASSES
    assert "agent_timeout" in operations.WORKER_RETRY_CLASSES


def test_one_model_failure_does_not_cool_an_entire_provider():
    """Catalog model failures leave independent models on the same provider usable."""
    for source in ("vercel-ai-gateway", "ollama-cloud", "mistral", "pollinations"):
        assert operations.model_scoped_transient(source, "provider_failure")
        for outcome in ("provider_failure", "provider_throttle", "provider_quota"):
            assert not operations.provider_scoped_transient(source, outcome)
        # Capacity refusals get a reset-timed cooldown, not the model-transient path.
        assert not operations.model_scoped_transient(source, "provider_quota")
    for outcome in ("model_unavailable", "model_policy_violation", "unknown_failure"):
        assert not operations.provider_scoped_transient("mistral", outcome)


def test_all_actionable_model_failures_use_fibonacci_without_credential_penalty():
    # Every real inference failure class must count; credential, checkout,
    # and controller failures belong to the worker, not an unrelated model.
    for name in (
        "provider_failure",
        "model_unavailable",
        "model_policy_violation",
        "agent_timeout",
    ):
        assert operations.attributable_model_failure(name, False), name
    for name in ("provider_throttle", "provider_quota"):
        assert not operations.attributable_model_failure(name, True), name
    for name in ("work_failure", "unknown_failure"):
        assert operations.attributable_model_failure(name, True), name
        assert not operations.attributable_model_failure(name, False)
    for name in ("environment_failure", "control_plane_failure", "unconfigured", "no_work"):
        assert not operations.attributable_model_failure(name, True), name
    assert "work_failure" in operations.WORKER_RETRY_CLASSES


def test_history_seeds_failed_source_model_without_double_counting_current_run():
    def event(kind: str, run_id: int, outcome: str, *, proven=False):
        return {
            "event_type": kind,
            "detail": {
                "source": "kilo-auto",
                "model": "kilo-auto/free",
                "run_id": str(run_id),
                "outcome_class": outcome,
                "exact_model_proven": proven,
            },
        }

    old = [
        event("worker_failed", 50, "work_failure", proven=True),
        event("worker_failed", 49, "provider_throttle"),
        event("worker_failed", 48, "unknown_failure"),  # Old pre-session abort
        event("worker_failed", 47, "agent_timeout"),
        event("worker_completed", 46, "success", proven=True),
        event("worker_failed", 45, "work_failure", proven=True),
    ]
    # A current terminal event already in the store must never count twice.
    rows = [event("worker_failed", 51, "work_failure", proven=True), *old]
    # The throttle at run 49 is capacity, not a model fault: it never counts.
    assert (
        operations.historical_failure_streak(
            rows, 51, "kilo-auto", "kilo-auto/free", model_only=True
        )
        == 2
    )
    assert (
        operations.historical_failure_streak(
            rows, 51, "kilo-auto", "kilo-auto/free", model_only=False
        )
        == 2
    )
    assert operations.historical_failure_streak(rows, 51, "z-ai", "x", model_only=True) == 0


def test_completed_delivery_resets_shared_streak_across_workers():
    def event(kind: str, run_id: int, outcome: str, *, delivered=False, proven=True):
        return {
            "event_type": kind,
            "detail": {
                "source": "kilo-auto",
                "model": "kilo-auto/free",
                "run_id": str(run_id),
                "outcome_class": outcome,
                "exact_model_proven": proven,
                "persisted_work": delivered,
            },
        }

    # This can come from either worker 46 or 98: shared source/model is key.
    rows = [
        event("worker_failed", 71, "work_failure"),
        event("worker_completed", 70, "unknown_failure", delivered=True),
        event("worker_failed", 69, "work_failure"),
    ]
    assert (
        operations.historical_failure_streak(
            rows, 72, "kilo-auto", "kilo-auto/free", model_only=True
        )
        == 1
    )


def test_legacy_account_failure_is_now_model_scoped():
    rows = [
        {
            "source": "nvidia",
            "model": "generated/paid",
            "outcome": "provider_quota",
            "scope": "account",
        }
    ]
    assert operations.validate_intermediate_model_failures(rows, "nvidia") == [
        ("nvidia", "generated/paid", "provider_quota", "model")
    ]


def test_legacy_suspended_record_does_not_block_model():
    now = datetime.now(UTC)
    old = {
        "payload": [
            {
                "source": "nvidia",
                "model": "generated/paid",
                "streak": 20,
                "suspended": True,
                "next_retry_at": None,
            }
        ],
        "expires_at": now + timedelta(days=90),
    }
    assert operations.worker_backoff_reason(old, "nvidia", "generated/paid", now) is None
