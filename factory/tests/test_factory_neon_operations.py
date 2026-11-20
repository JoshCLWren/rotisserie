"""Focused contract tests for Neon factory operational state.

No external connections. Production Neon evidence is verified separately.
"""
from __future__ import annotations

import asyncio
import json
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
spec = importlib.util.spec_from_file_location("factory_state_operations", ROOT / ".github/scripts/factory_state_operations.py")
assert spec is not None and spec.loader is not None
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)

REPO = "JoshCLWren/comic-pile"


class FakeConnection:
    def __init__(self, *, models=(), budgets=(), reservations=(), events=(), retries=(), metrics=(), backoffs=(), model_backoffs=()):
        self.models = models
        self.budgets = budgets
        self.reservations = reservations
        self.events = events
        self.retries = retries
        self.metrics = metrics
        self.backoffs = backoffs
        self.model_backoffs = model_backoffs

    async def fetch(self, statement, *args):
        if "FROM public.factory_state_kv" in statement:
            return self.model_backoffs if "factory-model-backoff-v1" in str(args) else self.backoffs
        if "FROM public.factory_model_health" in statement:
            return self.models
        if "FROM public.factory_provider_budget" in statement:
            return self.budgets
        if "FROM public.factory_provider_reservation" in statement:
            return self.reservations
        if "FROM public.factory_state_events" in statement:
            return self.events
        if "FROM public.factory_pr_stage_retry" in statement:
            return self.retries
        if "FROM public.factory_worker_metrics" in statement:
            return self.metrics
        raise AssertionError(statement)


def run(awaitable):
    return asyncio.run(awaitable)


def test_schema_contains_all_separate_operational_relations():
    ddl = "\n".join(operations.SCHEMA)
    for relation in (
        "factory_model_health", "factory_provider_budget",
        "factory_provider_reservation", "factory_pr_stage_retry",
        "factory_worker_metrics",
        "factory_runtime_event_keys", "factory_reconciliation_cursor",
    ):
        assert relation in ddl
    assert all("DROP TABLE" not in statement for statement in operations.SCHEMA)
    assert "factory_check_projection" not in ddl


def test_missing_model_is_suppressed_but_unrelated_provider_is_available():
    future = datetime.now(UTC) + timedelta(minutes=20)
    db = FakeConnection(models=[{
        "source": "nvidia", "model": "nvidia/expired",
        "status": "unavailable", "cooldown_until": future,
    }])
    with patch.object(operations, "_roster", return_value={
        "41": ("nvidia", "nvidia/expired"),
        "42": ("opencode-free", "oc/big-pickle"),
    }):
        kept, denied = run(operations._eligibility(db, REPO, ["41", "42"]))
    assert kept == ["42"]
    assert "model" in denied[0]


def test_expired_model_cooldown_is_eligible_for_a_new_probe():
    past = datetime.now(UTC) - timedelta(minutes=1)
    db = FakeConnection(models=[{
        "source": "nvidia", "model": "nvidia/expired",
        "status": "unavailable", "cooldown_until": past,
    }])
    with patch.object(operations, "_roster", return_value={
        "41": ("nvidia", "nvidia/expired"),
    }):
        kept, denied = run(operations._eligibility(db, REPO, ["41"]))
    assert kept == ["41"]
    assert not denied


def test_provider_exponential_backoff_doubles_and_caps():
    base = operations.exponential_provider_delay
    assert base("provider_throttle", None, 0, float("inf")) == 1800
    assert base("provider_throttle", "provider_throttle", 1800, 1800) == 3600
    assert base("provider_throttle", "provider_throttle", 3600, 3600) == 7200
    assert base("provider_failure", "provider_throttle", 9000, 5) == 14400
    assert base("provider_throttle", "provider_throttle", 14400, 13 * 3600) == 1800
    assert base("provider_throttle", "success", 14400, 60) == 1800


def test_legacy_provider_quota_does_not_suppress_other_models_or_sources():
    future = datetime.now(UTC) + timedelta(minutes=20)
    db = FakeConnection(
        models=[{"source": "nvidia", "model": "nvidia/a",
                 "status": "cooling", "cooldown_until": future}],
        budgets=[{"source": "nvidia", "cooldown_until": future,
                  "last_outcome": "provider_quota", "concurrency_limit": None,
                  "daily_limit": None, "usage_day": datetime.now(UTC).date(),
                  "daily_used": 0}],
    )
    with patch.object(operations, "_roster", return_value={
        "41": ("nvidia", "nvidia/a"),
        "42": ("nvidia", "nvidia/b"),
        "43": ("opencode-free", "oc/big-pickle"),
    }):
        kept, denied = run(operations._eligibility(db, REPO, ["41", "42", "43"]))
    assert kept == ["42", "43"]
    assert len(denied) == 1 and "nvidia/a" in denied[0]


def test_model_failure_budget_does_not_cool_other_models_of_provider():
    future = datetime.now(UTC) + timedelta(minutes=20)
    for outcome in ("provider_failure", "provider_throttle"):
        db = FakeConnection(budgets=[{
            "source": "nvidia", "cooldown_until": future,
            "last_outcome": outcome,
            "concurrency_limit": None, "daily_limit": None,
            "usage_day": datetime.now(UTC).date(), "daily_used": 0,
        }])
        with patch.object(operations, "_roster", return_value={
            "41": ("nvidia", "synthetic/failed"),
            "42": ("nvidia", "synthetic/other"),
        }):
            kept, rejected = run(operations._eligibility(db, REPO, ["41", "42"]))
        assert kept == ["41", "42"] and not rejected


def test_provider_concurrency_limit_does_not_invent_unconfigured_caps():
    db = FakeConnection(
        budgets=[{
            "source": "nvidia", "cooldown_until": None,
            "concurrency_limit": 1, "daily_limit": None,
            "usage_day": datetime.now(UTC).date(), "daily_used": 0,
        }],
        reservations=[{"source": "nvidia", "n": 1}],
    )
    with patch.object(operations, "_roster", return_value={
        "41": ("nvidia", "nvidia/a"),
        "43": ("opencode-free", "oc/big-pickle"),
    }):
        kept, denied = run(operations._eligibility(db, REPO, ["41", "43"]))
    assert kept == ["43"]
    assert "full" in denied[0]


def test_daily_usage_reads_neon_assignments_not_github_history():
    db = FakeConnection(events=[
        {"worker": "41", "source": None},
        {"worker": "42", "source": "opencode-free"},
        {"worker": "41", "source": "nvidia"}
    ])
    with patch.object(operations, "_roster", return_value={
        "41": ("nvidia", "nvidia/a"),
        "42": ("opencode-free", "oc/big-pickle"),
    }):
        usage = run(operations._daily_usage(db, REPO, ["nvidia", "opencode-free", "mistral"]))
    assert usage == {"nvidia": 2, "opencode-free": 1, "mistral": 0}


def test_retry_suppression_is_exact_head_and_exact_stage_only():
    sha = "a" * 40
    db = FakeConnection(retries=[{
        "pr_number": 5000, "head_sha": sha,
        "stage": "factory:review",
    }])
    assert run(operations._cooling_prs(db, REPO, {5000: (sha, "factory:review")})) == {5000}
    assert run(operations._cooling_prs(db, REPO, {5000: ("b" * 40, "factory:review")})) == set()
    assert run(operations._cooling_prs(db, REPO, {5000: (sha, "factory:ci")})) == set()


def test_worker_ranking_preserves_source_diversity():
    db = FakeConnection(metrics=[
        {"worker": "41", "attempts": 10, "productive": 1},
        {"worker": "42", "attempts": 10, "productive": 7},
    ])
    with patch.object(operations, "_roster", return_value={
        "41": ("nvidia", "nvidia/a"),
        "42": ("nvidia", "nvidia/b"),
        "43": ("opencode-free", "oc/big-pickle"),
    }):
        ranked = run(operations._rank_workers(db, REPO, ["41", "43", "42"]))
    assert ranked == ["42", "43", "41"]


def test_model_scoped_provider_route_never_cools_unrelated_routes():
    assert "omniroute-free" in operations.MODEL_SCOPED_SOURCES
    assert "nvidia" not in operations.MODEL_SCOPED_SOURCES
    assert operations.MODEL_COOLDOWNS["model_unavailable"] == 21600


def test_exact_head_checks_are_advisory_not_merge_authorization():
    workflow = (ROOT / ".github/workflows/factory-check-projection.yml").read_text()
    assert "factory_state_operations.py ingest-check" in workflow
    assert "gh workflow run fixed-model-factory-dispatch.yml" in workflow
    assert "factory_check_projection" not in workflow
    assert "neon-api-key: ${{ secrets.NEON_API_KEY }}" in workflow
    assert "neon-api-key: \\${{" not in workflow
    assert "pull_request_target:" not in workflow
    assert "gh pr merge" not in workflow


def test_runtime_410_is_sticky_in_neon_projection():
    import inspect
    source = inspect.getsource(operations.project_runtime)
    assert 'classification == "model_retired_410"' in source
    assert 'status, until = "retired", None' in source


def test_trusted_model_snapshot_is_scoped_and_bounded() -> None:
    import inspect
    source = inspect.getsource(operations._export_model_exclusions)
    assert "WHERE repo=$1" in source
    assert "cooldown_until>now()" in source
    assert "retired" in source
    assert "export-model-exclusions" in inspect.getsource(operations.main)


def test_missing_marker_id_never_reads_obsolete_body_cache():
    class SnapshotDB:
        async def fetch(self, statement, *args):
            if "factory_reconciliation_cursor" in statement:
                return []
            if "factory_state_kv" in statement:
                return []
            return [{"source": "nvidia", "model": "gone"}]

        async def fetchval(self, statement, *args):
            raise AssertionError("no retired registry body read")

        async def execute(self, statement, *args):
            raise AssertionError("missing IDs do not create unverifiable locators")

    with patch.object(operations, "_roster", return_value={
        "111": ("opencode-free", "glm-5.3-flash-free"),
    }):
        record = run(operations._export_model_exclusions(SnapshotDB(), REPO, "111"))
    assert record == {"nvidia": ["gone"]}


def test_model_exclusions_never_export_registry_comment_locators():
    """Issue #1093 is retired: no comment locator crosses to the runner."""
    class ReadOnlyDB:
        def __init__(self):
            self.statements = []

        async def fetch(self, statement, *args):
            self.statements.append(statement)
            return []

        async def fetchval(self, statement, *args):
            raise AssertionError("exclusion export must not read a registry cursor")

        async def execute(self, statement, *args):
            raise AssertionError("exclusion export must not write")

    db = ReadOnlyDB()
    with patch.object(operations, "_roster", return_value={
        "111": ("opencode-free", "glm-5.3-flash-free"),
    }):
        result = run(operations._export_model_exclusions(db, REPO, "111"))
        with pytest.raises(ValueError):
            run(operations._export_model_exclusions(ReadOnlyDB(), REPO, "999"))
    assert result == {}
    assert not any("factory_reconciliation_cursor" in sql for sql in db.statements)


def test_intermediate_model_failures_use_terminal_runtime_idempotency():
    import inspect

    failures = [
        {"model": f"generated/model-{index}", "outcome": outcome}
        for index, outcome in enumerate(("provider_quota", "provider_throttle", "model_unavailable"))
    ]
    assert operations.validate_intermediate_model_failures(
        failures, "openrouter-free"
    ) == [("openrouter-free", item["model"], item["outcome"], "model") for item in failures]
    text = inspect.getsource(operations.project_runtime)
    assert "factory_runtime_event_keys" in text
    assert "factory_model_health.status" in text
    assert "last_run_id <= EXCLUDED.last_run_id" in text


def test_fibonacci_retry_progresses_without_cap():
    fib = operations.fibonacci_worker_delay
    assert [fib(i) for i in range(1, 8)] == [300, 300, 600, 900, 1500, 2400, 3900]
    # No 24h ceiling: per-model backoff keeps growing.
    assert fib(20) == 6765 * 300
    assert fib(50) > fib(49) > 86400


def test_fibonacci_retry_at_clamps_only_at_datetime_max():
    observed = datetime(2026, 11, 17, tzinfo=UTC)
    assert operations.fibonacci_retry_at(observed, 3) == observed + timedelta(seconds=600)
    assert operations.fibonacci_retry_at(observed, 30) - observed == timedelta(seconds=832040 * 300)
    huge = operations.fibonacci_retry_at(observed, 200)
    assert huge == datetime.max.replace(tzinfo=UTC)


def test_retry_time_and_retained_streak_are_independent_of_ttl():
    now = datetime.now(UTC)
    retry = now + timedelta(minutes=5)
    record = {
        "payload": [{"source": "vercel-ai-gateway", "model": "reflection/beam-free",
                     "streak": 7, "next_retry_at": retry.isoformat(),
                     "suspended": False}],
        "expires_at": now + timedelta(days=90),
    }
    assert operations.worker_backoff_reason(record, "vercel-ai-gateway", "reflection/beam-free", now)
    assert operations.worker_backoff_reason(record, "vercel-ai-gateway", "reflection/beam-free",
                                            retry + timedelta(seconds=1)) is None
    assert operations.worker_backoff_reason(record, "vercel-ai-gateway", "new-pin", now) is None


def test_legacy_suspension_no_longer_excludes_worker():
    db = FakeConnection(backoffs=[{
        "cache_key": f"factory-worker-backoff-v1:{REPO}:worker:139",
        "payload": [{"source": "vercel-ai-gateway", "model": "reflection/beam-free",
                     "streak": 14, "suspended": True, "next_retry_at": None}],
        "expires_at": datetime.now(UTC) + timedelta(days=90),
    }])
    with patch.object(operations, "_roster", return_value={
        "139": ("vercel-ai-gateway", "reflection/beam-free"),
        "41": ("kilo-auto", "kilo-auto/free"),
    }):
        kept, denied = run(operations._eligibility(db, REPO, ["139", "41"]))
    assert kept == ["139", "41"] and not denied


def test_missing_credentials_are_not_inference_failures():
    # The upstream pre-lease gate rejects missing credentials entirely.
    # A runner in flight during cutover still reports this separately.
    assert "unconfigured" not in operations.WORKER_RETRY_CLASSES
    assert "unknown_failure" in operations.WORKER_RETRY_CLASSES
    assert "agent_timeout" in operations.WORKER_RETRY_CLASSES


def test_one_model_failure_does_not_cool_an_entire_provider():
    """Catalog model failures leave independent models on the same provider usable."""
    for source in ("vercel-ai-gateway", "ollama-cloud", "mistral", "pollinations"):
        for outcome in ("provider_failure", "provider_throttle"):
            assert operations.model_scoped_transient(source, outcome)
            assert not operations.provider_scoped_transient(source, outcome)
        assert not operations.provider_scoped_transient(source, "provider_quota")
        assert operations.model_scoped_transient(source, "provider_quota")
    for source in operations.MODEL_SCOPED_SOURCES:
        assert operations.model_scoped_transient(source, "provider_quota")
        assert not operations.provider_scoped_transient(source, "provider_quota")
    for outcome in ("model_unavailable", "model_policy_violation", "unknown_failure"):
        assert not operations.provider_scoped_transient("mistral", outcome)


def test_all_actionable_model_failures_use_fibonacci_without_credential_penalty():
    # Every real inference failure class must count; credential, checkout,
    # and controller failures belong to the worker, not an unrelated model.
    for name in ("provider_failure", "provider_throttle", "provider_quota",
                 "model_unavailable", "model_policy_violation", "agent_timeout"):
        assert operations.attributable_model_failure(name, False), name
    for name in ("work_failure", "unknown_failure"):
        assert operations.attributable_model_failure(name, True), name
        assert not operations.attributable_model_failure(name, False)
    for name in ("environment_failure", "control_plane_failure", "unconfigured", "no_work"):
        assert not operations.attributable_model_failure(name, True), name
    assert "work_failure" in operations.WORKER_RETRY_CLASSES


def test_history_seeds_failed_source_model_without_double_counting_current_run():
    def event(kind: str, run_id: int, outcome: str, *, proven=False):
        return {"event_type": kind, "detail": {
            "source": "kilo-auto", "model": "kilo-auto/free",
            "run_id": str(run_id), "outcome_class": outcome,
            "exact_model_proven": proven,
        }}
    old = [
        event("worker_failed", 50, "work_failure", proven=True),
        event("worker_failed", 49, "provider_throttle"),
        event("worker_failed", 48, "unknown_failure"),  # Old pre-session abort
        event("worker_failed", 47, "agent_timeout"),
        event("worker_completed", 46, "success", proven=True),
        event("worker_failed", 45, "work_failure", proven=True),
    ]
    # A current terminal event already in Neon must never count twice.
    rows = [event("worker_failed", 51, "work_failure", proven=True), *old]
    assert operations.historical_failure_streak(rows, 51, "kilo-auto", "kilo-auto/free", model_only=True) == 3
    assert operations.historical_failure_streak(rows, 51, "kilo-auto", "kilo-auto/free", model_only=False) == 3
    assert operations.historical_failure_streak(rows, 51, "z-ai", "x", model_only=True) == 0


def test_completed_delivery_resets_shared_streak_across_workers():
    def event(kind: str, run_id: int, outcome: str, *, delivered=False, proven=True):
        return {"event_type": kind, "detail": {
            "source": "kilo-auto", "model": "kilo-auto/free",
            "run_id": str(run_id), "outcome_class": outcome,
            "exact_model_proven": proven, "persisted_work": delivered,
        }}
    # This can come from either worker 46 or 98: shared source/model is key.
    rows = [event("worker_failed", 71, "work_failure"),
            event("worker_completed", 70, "unknown_failure", delivered=True),
            event("worker_failed", 69, "work_failure")]
    assert operations.historical_failure_streak(rows, 72, "kilo-auto", "kilo-auto/free", model_only=True) == 1


def test_shared_model_fibonacci_backoff_blocks_both_workers_not_other_models():
    now = datetime.now(UTC)
    model_state = [{
        "cache_key": f"factory-model-backoff-v1:{REPO}:source:kilo-auto:model:kilo-auto/free",
        "payload": [{"source": "kilo-auto", "model": "kilo-auto/free",
                     "streak": 5, "next_retry_at": (now + timedelta(minutes=25)).isoformat(),
                     "suspended": False}],
        "expires_at": now + timedelta(days=90),
    }]
    db = FakeConnection(model_backoffs=model_state)
    with patch.object(operations, "_roster", return_value={
        "46": ("kilo-auto", "kilo-auto/free"),
        "98": ("kilo-auto", "kilo-auto/free"),
        "41": ("opencode-free", "other-free"),
    }):
        kept, denied = run(operations._eligibility(db, REPO, ["46", "98", "41"]))
    assert kept == ["41"]
    assert len(denied) == 2
    assert all("source/model" in item for item in denied)


def test_retry_updates_are_fenced_and_read_only_from_dispatcher():
    import inspect
    source = inspect.getsource(operations.project_runtime)
    assert "pg_advisory_xact_lock" in source
    assert "prior_model_run <= run_id" in source
    assert "model_key, run_id" in source
    assert "factory-model-backoff-v1:" in source
    # Fresh attempts bootstrap from prior Neon terminal events, not GitHub.
    assert "await _recent_streak(conn, run_id, source, model, repo=repo)" in source
    assert "classification in WORKER_RETRY_CLASSES" in source

class RuntimeConn:
    """In-memory runtime projection connection recording KV writes."""

    def __init__(self):
        self.kv = {}
        self.runtime_keys = set()

    def transaction(self):
        class Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return Tx()

    async def execute(self, query, *args):
        if "INSERT INTO public.factory_runtime_event_keys" in query:
            repo, run_id = args[0], int(args[1])
            if (repo, run_id) in self.runtime_keys:
                return "INSERT 0 0"
            self.runtime_keys.add((repo, run_id))
            return "INSERT 0 1"
        if "INSERT INTO public.factory_state_kv" in query:
            key = args[0]
            payload = args[1]
            if isinstance(payload, str):
                payload = json.loads(payload)
            self.kv[key] = payload
            return "INSERT 0 1"
        if "DELETE FROM public.factory_state_kv" in query:
            self.kv.pop(args[0], None)
            return "DELETE 1"
        return "OK"

    async def fetchval(self, query, *args):
        if "INSERT INTO public.factory_runtime_event_keys" in query:
            repo, run_id = args[0], int(args[1])
            if (repo, run_id) in self.runtime_keys:
                return None
            self.runtime_keys.add((repo, run_id))
            return run_id
        if "pg_advisory_xact_lock" in query:
            return None
        if "FROM public.factory_state_kv" in query:
            return None
        return None

    async def fetchrow(self, query, *args):
        if "FROM public.factory_state_kv" in query:
            payload = self.kv.get(args[0])
            return {"payload": payload} if payload is not None else None
        return None

    async def fetch(self, query, *args):
        return []


def test_dynamic_slot_worker_backoff_persists_across_model_rotation(monkeypatch):
    """project_runtime must accumulate worker streak across discovered models."""

    monkeypatch.setenv("FACTORY_SLOTS_PER_PROVIDER", "1")
    monkeypatch.delenv("FACTORY_SLOT_PROVIDER_SOURCES", raising=False)
    roster = operations._roster()  # actual provider-derived projection
    worker, pair = next(
        item for item in sorted(roster.items()) if item[1][0] == "openrouter-free"
    )
    source, model_slot = pair
    assert model_slot == ""

    conn = RuntimeConn()
    worker_key = f"factory-worker-backoff-v1:{REPO}:worker:{worker}"

    async def fail(run_id: int, model: str):
        return await operations.project_runtime(
            conn, REPO, run_id,
            {
                "worker": worker, "source": source, "model": model,
                "outcome_class": "provider_failure", "exact_model_proven": True,
                "persisted_work": False, "configured": True,
            },
        )

    assert run(fail(1001, "sample/a")) is True
    first = conn.kv[worker_key]
    assert first[0]["streak"] == 1
    assert run(fail(1002, "sample/b")) is True
    second = conn.kv[worker_key]
    assert second[0]["streak"] == 2
    assert second[0]["next_retry_at"] > first[0]["next_retry_at"]
    assert run(fail(1003, "sample/c")) is True
    third = conn.kv[worker_key]
    assert third[0]["streak"] == 3
    assert third[0]["next_retry_at"] > second[0]["next_retry_at"]

    model_keys = sorted(
        k for k in conn.kv
        if k.startswith(f"factory-model-backoff-v1:{REPO}:source:{source}:model:")
    )
    assert model_keys == [
        f"factory-model-backoff-v1:{REPO}:source:{source}:model:sample/a",
        f"factory-model-backoff-v1:{REPO}:source:{source}:model:sample/b",
        f"factory-model-backoff-v1:{REPO}:source:{source}:model:sample/c",
    ]
    assert [conn.kv[k][0]["streak"] for k in model_keys] == [1, 1, 1]

    assert run(operations.project_runtime(
        conn, REPO, 1004,
        {
            "worker": worker, "source": source, "model": "sample/c",
            "outcome_class": "success", "exact_model_proven": True,
            "persisted_work": False, "configured": True,
        },
    )) is True
    assert worker_key not in conn.kv
    assert f"factory-model-backoff-v1:{REPO}:source:{source}:model:sample/c" not in conn.kv
    assert f"factory-model-backoff-v1:{REPO}:source:{source}:model:sample/a" in conn.kv
    assert f"factory-model-backoff-v1:{REPO}:source:{source}:model:sample/b" in conn.kv

    db = FakeConnection(backoffs=[{
        "cache_key": worker_key,
        "payload": [{"source": source, "model": "sample/d",
                     "streak": 3, "next_retry_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                     "suspended": False}],
        "expires_at": datetime.now(UTC) + timedelta(days=90),
    }])
    kept, denied = run(operations._eligibility(db, REPO, [worker]))
    assert kept == []
    assert any("Fibonacci retry deferred" in d for d in denied)

def test_catalog_slot_model_failure_does_not_suppress_other_models():
    future = datetime.now(UTC) + timedelta(minutes=20)
    db = FakeConnection(backoffs=[{
        "cache_key": "worker",
        "payload": [{"source": "openrouter-free", "model": "vendor/paid",
                     "outcome": "provider_quota", "streak": 3,
                     "next_retry_at": future.isoformat()}],
        "expires_at": future,
    }])
    with patch.object(operations, "_roster", return_value={
        "501": ("openrouter-free", ""),
    }):
        kept, denied = run(operations._eligibility(db, REPO, ["501"]))
    assert kept == ["501"] and not denied


def test_model_backoffs_are_in_trusted_catalog_exclusions():
    future = datetime.now(UTC) + timedelta(minutes=20)
    db = FakeConnection(model_backoffs=[{
        "payload": [{"source": "openrouter-free", "model": "vendor/failed:free",
                     "outcome": "provider_throttle", "streak": 3,
                     "next_retry_at": future.isoformat()}],
    }])
    exclusions = run(operations._export_model_exclusions(db, REPO))
    assert exclusions["openrouter-free"] == ["vendor/failed:free"]
    assert "vendor/other:free" not in exclusions["openrouter-free"]


def test_worker_provider_model_backoffs_remain_independent() -> None:
    now = datetime.now(UTC)
    future = now + timedelta(minutes=15)
    model = "generated/model"
    worker = "41"
    row = {
        "cache_key": f"factory-worker-backoff-v1:{REPO}:worker:{worker}",
        "payload": [{"source": "nvidia", "model": model,
                     "outcome": "provider_throttle", "streak": 3,
                     "next_retry_at": future.isoformat()}],
        "expires_at": future,
    }
    db = FakeConnection(backoffs=[row])
    with patch.object(operations, "_roster", return_value={
        worker: ("nvidia", "@catalog"),
    }):
        kept, rejected = run(operations._eligibility(db, REPO, [worker]))
    assert kept == [worker]
    assert not rejected


def test_proven_account_cooldown_blocks_only_its_provider() -> None:
    now = datetime.now(UTC)
    future = now + timedelta(minutes=15)
    row = {
        "cache_key": f"factory-provider-backoff-v1:{REPO}:source:nvidia",
        "payload": [{"source": "nvidia", "outcome": "provider_quota",
                     "streak": 2, "delay_seconds": 600}],
        "expires_at": future,
    }
    db = FakeConnection(backoffs=[row])
    with patch.object(operations, "_roster", return_value={
        "41": ("nvidia", "@catalog"),
        "42": ("requesty", "@catalog"),
    }):
        kept, rejected = run(operations._eligibility(db, REPO, ["41", "42"]))
    assert kept == ["42"]
    assert len(rejected) == 1 and "nvidia" in rejected[0]


def test_fleet_admission_does_not_double_book_capped_provider() -> None:
    from unittest.mock import patch
    import factory_source_budget

    class CapacityConnection:
        async def fetch(self, sql, *args):
            assert "factory_provider_budget" in sql
            return [
                {"source": "requesty", "concurrency_limit": 1, "daily_limit": None,
                 "cooldown_until": None, "quota_reset_at": None},
                {"source": "nvidia", "concurrency_limit": None, "daily_limit": None,
                 "cooldown_until": None, "quota_reset_at": None},
            ]

    with (
        patch.object(operations, "_roster", return_value={"41": ("nvidia", "@catalog")}),
        patch.object(factory_source_budget, "load_budgets", return_value={"cloudflare-workers-ai": 1}),
    ):
        allowed = run(operations._export_fleet_failover_sources(
            CapacityConnection(), REPO, "41",
        ))
    assert "nvidia" in allowed
    assert "requesty" not in allowed
    assert "cloudflare-workers-ai" not in allowed


def test_legacy_account_refusal_only_backs_off_the_exact_model(monkeypatch):
    """Old account-labelled records are downgraded to exact-model Fibonacci."""
    monkeypatch.setenv("FACTORY_SLOTS_PER_PROVIDER", "1")
    monkeypatch.delenv("FACTORY_SLOT_PROVIDER_SOURCES", raising=False)
    roster = operations._roster()
    worker, (terminal_source, _) = sorted(roster.items())[0]
    from factory_provider_candidates import ADAPTERS

    refused_source = next(s for s in sorted(ADAPTERS) if s != terminal_source)
    refused_model = "generated/refused-model"
    model_key = f"factory-model-backoff-v1:{REPO}:source:{refused_source}:model:{refused_model}"
    provider_key = f"factory-provider-backoff-v1:{REPO}:source:{refused_source}"
    conn = RuntimeConn()

    def outcome(run_id: int) -> dict:
        return {
            "worker": worker, "source": terminal_source, "model": f"generated/terminal-{run_id}",
            "outcome_class": "provider_throttle", "exact_model_proven": True,
            "persisted_work": False, "configured": True,
            "model_failures": [{
                "source": refused_source, "model": refused_model,
                "outcome": "provider_quota", "scope": "account",
            }],
        }

    for run_id, expected_streak in [(2001, 1), (2002, 2), (2003, 3)]:
        assert run(operations.project_runtime(conn, REPO, run_id, outcome(run_id))) is True
        record = conn.kv[model_key][0]
        assert record["outcome"] == "provider_quota"
        assert record["streak"] == expected_streak
        assert record["suspended"] is False
        assert record["next_retry_at"] is not None
        assert provider_key not in conn.kv

    # The cooled model is excluded, but the provider's other models and slots remain available.
    model_row = {
        "cache_key": model_key, "payload": conn.kv[model_key],
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    db = FakeConnection(model_backoffs=[model_row])
    exclusions = run(operations._export_model_exclusions(db, REPO))
    assert exclusions == {refused_source: [refused_model]}
    with patch.object(operations, "_roster", return_value={
        "41": (refused_source, "@catalog"), "42": (terminal_source, "@catalog"),
    }):
        kept, rejected = run(operations._eligibility(db, REPO, ["41", "42"]))
    assert kept == ["41", "42"] and not rejected


def test_legacy_account_failure_is_now_model_scoped():
    rows = [{"source": "nvidia", "model": "generated/paid",
             "outcome": "provider_quota", "scope": "account"}]
    assert operations.validate_intermediate_model_failures(rows, "nvidia") == [
        ("nvidia", "generated/paid", "provider_quota", "model")]


def test_legacy_suspended_record_does_not_block_model():
    now = datetime.now(UTC)
    old = {"payload": [{"source": "nvidia", "model": "generated/paid",
                        "streak": 20, "suspended": True, "next_retry_at": None}],
           "expires_at": now + timedelta(days=90)}
    assert operations.worker_backoff_reason(old, "nvidia", "generated/paid", now) is None


def test_reset_is_scoped_and_preserves_usage_and_runtime_history():
    calls = []

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self):
                    return None
                async def __aexit__(self, *exc):
                    return False
            return Tx()
        async def fetchval(self, sql, *args):
            calls.append((sql, args))
            return len(calls)
        async def execute(self, sql, *args):
            calls.append((sql, args))
            return "INSERT 0 1"

    assert run(operations._reset_model_health(Conn(), REPO)) == {
        "model_health_rows": 1, "backoff_rows": 2, "provider_cooldowns": 3}
    assert calls[0][1] == (REPO, False)
    assert "status != 'retired'" in calls[0][0]
    assert all(key.endswith(f"{REPO}:") for key in calls[1][1])
    assert "daily_used" not in calls[2][0]
    assert "factory_provider_reservation" not in str(calls)
    assert "factory_state_events" not in str(calls)
    assert calls[3][1] == (f"factory-model-health-reset-v1:{REPO}",)
    assert "30 days" in calls[3][0]
    calls.clear()
    run(operations._reset_model_health(Conn(), REPO, True))
    assert calls[0][1] == (REPO, True)


def test_reset_cutoff_prevents_old_failures_rebuilding_fibonacci():
    calls = []
    class Conn:
        async def fetch(self, sql, *args):
            calls.append((sql, args))
            return []

    assert run(operations._recent_streak(
        Conn(), 9001, "nvidia", "generated/model", repo=REPO)) == 0
    assert run(operations._recent_streak(
        Conn(), 9002, "nvidia", "generated/model", repo=REPO,
        worker="41", model_independent=True)) == 0
    marker = f"factory-model-health-reset-v1:{REPO}"
    assert calls[0][1] == ("nvidia", "generated/model", marker)
    assert calls[1][1] == (41, marker)
    for sql, _ in calls:
        assert "created_at>GREATEST" in sql
        assert "SELECT updated_at FROM public.factory_state_kv" in sql
        assert "'-infinity'::timestamptz" in sql
