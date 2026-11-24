"""Quota-only resets and reset-timed capacity cooldowns on the factory store.

Ported from the PostgreSQL quota backoff suite.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

operations = importlib.import_module("factory_state_operations")
backoff = importlib.import_module("factory_turso_backoff")
quota = importlib.import_module("factory_state_operations_quota")


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        _seed(db, repo)
        yield db, repo


def _seed(db: Any, repo: str) -> None:
    far = int((datetime.now(UTC) + timedelta(days=9)).timestamp())
    statements = [
        (
            "INSERT INTO factory_backoff (backoff_key, repo, kind, source, model, outcome, streak,"
            " next_retry_at, expires_at, updated_at) VALUES (:key, :repo, 'model', 'nvidia',"
            " :model, :outcome, 17, :far, unixepoch() + 90 * 86400, unixepoch())",
            {
                "key": f"factory-model-backoff-v1:{repo}:source:nvidia:model:{model}",
                "repo": repo,
                "model": model,
                "outcome": outcome,
                "far": far,
            },
        )
        for model, outcome in (("quota-pair", "provider_quota"), ("real-pair", "agent_timeout"))
    ]
    statements.append(
        (
            "INSERT INTO factory_backoff (backoff_key, repo, kind, source, outcome, expires_at,"
            " updated_at) VALUES (:key, :repo, 'pool', 'groq-free', 'provider_quota',"
            " unixepoch() + 20 * 86400, unixepoch())",
            {"key": f"{quota.POOL_PREFIX}:{repo}:source:groq-free", "repo": repo},
        )
    )
    statements.append(
        (
            "INSERT INTO factory_model_health (repo, source, model, status, last_outcome,"
            " cooldown_until, updated_at) VALUES (:repo, 'groq-free', 'm', 'cooling',"
            " 'provider_quota', unixepoch() + 3 * 86400, unixepoch())",
            {"repo": repo},
        )
    )
    db.batch(statements)


def test_quota_only_reset_keeps_real_streaks_and_caps_them(env) -> None:
    db, repo = env
    assert operations.reset_quota_backoffs(repo) == {
        "quota_backoff_rows": 1,
        "quota_health_rows": 1,
        "capacity_cooldowns": 1,
        "capped_fibonacci_rows": 1,
    }
    rows = db.read(
        "SELECT * FROM factory_backoff WHERE repo = :r AND kind IN ('model', 'worker')",
        {"r": repo},
    )
    assert [r["model"] for r in rows] == ["real-pair"]
    state = backoff.state(rows[0])
    assert state["streak"] == 17
    retry = datetime.fromisoformat(state["next_retry_at"])
    assert retry <= datetime.now(UTC) + timedelta(days=1, minutes=1)
    assert not db.read(
        "SELECT health_reset_at FROM factory_repo_state WHERE repo = :r", {"r": repo}
    )


def test_capacity_cooldown_never_shortens_and_is_listed(env) -> None:
    db, repo = env
    now = datetime.now(UTC)
    later = {
        "kind": "daily",
        "reset_at": int((now + timedelta(hours=6)).timestamp()),
        "reset_source": "header",
    }
    sooner = {
        "kind": "throttle",
        "reset_at": int((now + timedelta(minutes=5)).timestamp()),
        "reset_source": "policy",
    }
    for run_id, hint in ((1, later), (2, sooner)):
        db.batch(
            backoff.capacity_statements(
                repo, "nvidia", "m", "provider_quota", "model", hint, now, run_id, ("1", {})
            )
        )
    listed = operations.export_backoffs(repo)["providers"]
    (cooldown,) = listed["nvidia"]["capacity"]
    assert cooldown["reset_source"] == "header" and cooldown["scope"] == "account"
    assert datetime.fromisoformat(cooldown["until"]) > now + timedelta(hours=5)
    assert {f["model"] for f in listed["nvidia"]["fibonacci"]} == {"quota-pair", "real-pair"}
    assert [h["model"] for h in listed["groq-free"]["health"]] == ["m"]
