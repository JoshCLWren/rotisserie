"""Runtime outcome projection on the factory store: streaks, cooldowns, retirement and resets."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

operations = importlib.import_module("factory_state_operations")
turso_operations = importlib.import_module("factory_turso_operations")
events = importlib.import_module("factory_turso_events")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FACTORY_SLOTS_PER_PROVIDER", "1")
    monkeypatch.delenv("FACTORY_SLOT_PROVIDER_SOURCES", raising=False)
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def _backoffs(db: Any, repo: str, kind: str) -> dict[str, dict[str, Any]]:
    rows = db.read(
        "SELECT * FROM factory_backoff WHERE repo = :r AND kind = :k", {"r": repo, "k": kind}
    )
    return {str(r["backoff_key"]): r for r in rows}


def _health(db: Any, repo: str, model: str) -> str:
    rows = db.read(
        "SELECT status FROM factory_model_health WHERE repo = :r AND model = :m",
        {"r": repo, "m": model},
    )
    return str(rows[0]["status"])


def _outcome(worker: str, source: str, model: str, outcome: str, **extra: Any) -> dict[str, Any]:
    return {
        "worker": worker,
        "source": source,
        "model": model,
        "outcome_class": outcome,
        "exact_model_proven": True,
        "persisted_work": False,
        "configured": True,
        **extra,
    }


def test_dynamic_slot_worker_backoff_persists_across_model_rotation(env) -> None:
    db, repo = env
    worker, (source, slot_model) = next(
        item
        for item in sorted(turso_operations.roster().items())
        if item[1][0] == "openrouter-free"
    )
    assert slot_model == ""
    worker_key = f"factory-worker-backoff-v1:{repo}:worker:{worker}"
    model_prefix = f"factory-model-backoff-v1:{repo}:source:{source}:model:"
    retries: list[int] = []
    for streak, (run_id, model) in enumerate(
        ((1001, "sample/a"), (1002, "sample/b"), (1003, "sample/c")), start=1
    ):
        outcome = _outcome(worker, source, model, "provider_failure")
        assert operations.record_runtime(repo, run_id, outcome) is True
        row = _backoffs(db, repo, "worker")[worker_key]
        assert row["streak"] == streak
        retries.append(int(row["next_retry_at"]))
    assert retries == sorted(retries) and retries[2] > retries[0]
    models = _backoffs(db, repo, "model")
    assert sorted(models) == [f"{model_prefix}sample/{m}" for m in "abc"]
    assert [models[k]["streak"] for k in sorted(models)] == [1, 1, 1]
    success = _outcome(worker, source, "sample/c", "success")
    assert operations.record_runtime(repo, 1004, success) is True
    assert worker_key not in _backoffs(db, repo, "worker")
    assert sorted(_backoffs(db, repo, "model")) == [f"{model_prefix}sample/{m}" for m in "ab"]
    support.backoff(
        db, repo, "worker", worker=worker, source=source, model="sample/d", streak=3, retry_in=600
    )
    kept, denied = operations.filter_workers(repo, [worker])
    assert kept == []
    assert any("Fibonacci retry deferred" in d for d in denied)


def test_account_quota_refusal_never_writes_a_model_streak(env) -> None:
    db, repo = env
    worker, (terminal_source, _) = sorted(turso_operations.roster().items())[0]
    refused = "openrouter-free" if terminal_source != "openrouter-free" else "nvidia"
    for run_id in (2001, 2002, 2003):
        outcome = _outcome(
            worker,
            terminal_source,
            f"generated/t-{run_id}",
            "provider_throttle",
            model_failures=[
                {
                    "source": refused,
                    "model": "generated/refused",
                    "outcome": "provider_quota",
                    "scope": "account",
                }
            ],
        )
        assert operations.record_runtime(repo, run_id, outcome) is True
    model_key = f"factory-model-backoff-v1:{repo}:source:{refused}:model:generated/refused"
    assert model_key not in _backoffs(db, repo, "model")
    providers = _backoffs(db, repo, "provider")
    provider_key = f"factory-provider-backoff-v1:{repo}:source:{refused}"
    assert providers[provider_key]["outcome"] == "provider_quota"
    assert _backoffs(db, repo, "worker") == {}


def test_runtime_410_is_sticky(env) -> None:
    db, repo = env
    worker = support.workers_by_source()["nvidia"][0]
    retired = _outcome(worker, "nvidia", "gone", "model_retired_410")
    assert operations.record_runtime(repo, 3001, retired) is True
    assert _health(db, repo, "gone") == "retired"
    assert operations.record_runtime(repo, 3002, _outcome(worker, "nvidia", "gone", "success"))
    assert _health(db, repo, "gone") == "retired"
    assert "gone" in operations.export_model_exclusions(repo)["nvidia"]


def _terminal(repo: str, worker: str, run_id: int, outcome: str) -> dict[str, Any]:
    return {
        "event_type": "worker_failed" if outcome != "success" else "worker_completed",
        "event_id": f"{repo}:terminal:{run_id}",
        "worker": worker,
        "run_id": str(run_id),
        "source": "nvidia",
        "model": "generated/model",
        "outcome_class": outcome,
        "exact_model_proven": True,
    }


def _model_streak(db: Any, repo: str) -> int:
    key = f"factory-model-backoff-v1:{repo}:source:nvidia:model:generated/model"
    return int(_backoffs(db, repo, "model")[key]["streak"])


def test_a_fresh_streak_bootstraps_from_prior_terminal_events(env) -> None:
    db, repo = env
    worker = support.workers_by_source()["nvidia"][0]
    events.record_events(
        repo, [_terminal(repo, worker, run, "work_failure") for run in (4001, 4002)]
    )
    failure = _outcome(worker, "nvidia", "generated/model", "work_failure")
    assert operations.record_runtime(repo, 4003, failure) is True
    assert _model_streak(db, repo) == 3


def test_reset_cutoff_prevents_old_failures_rebuilding_fibonacci(env) -> None:
    db, repo = env
    worker = support.workers_by_source()["nvidia"][0]
    events.record_events(
        repo, [_terminal(repo, worker, run, "work_failure") for run in (5001, 5002)]
    )
    operations.reset_model_health(repo)
    failure = _outcome(worker, "nvidia", "generated/model", "work_failure")
    assert operations.record_runtime(repo, 5003, failure) is True
    assert _model_streak(db, repo) == 1
    worker_rows = _backoffs(db, repo, "worker")
    assert [r["streak"] for r in worker_rows.values()] == [1]
