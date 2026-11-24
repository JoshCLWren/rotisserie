"""Dispatch eligibility, usage, ranking, exports and resets of the factory store operations."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

operations = importlib.import_module("factory_state_operations")
turso_operations = importlib.import_module("factory_turso_operations")
MINUTES_20 = 20 * 60


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def _eligible(
    monkeypatch: pytest.MonkeyPatch, db: Any, repo: str, names: dict[str, tuple[str, str]]
) -> tuple[list[str], list[str]]:
    support.use_roster(monkeypatch, names)
    return turso_operations.eligibility(db, repo, list(names))


def test_missing_model_is_suppressed_but_unrelated_provider_is_available(env, monkeypatch) -> None:
    db, repo = env
    support.health(db, repo, "nvidia", "nvidia/expired", "unavailable", MINUTES_20)
    names = {"41": ("nvidia", "nvidia/expired"), "42": ("opencode-free", "oc/big-pickle")}
    kept, denied = _eligible(monkeypatch, db, repo, names)
    assert kept == ["42"]
    assert "model" in denied[0]


def test_expired_model_cooldown_is_eligible_for_a_new_probe(env, monkeypatch) -> None:
    db, repo = env
    support.health(db, repo, "nvidia", "nvidia/expired", "unavailable", -60)
    kept, denied = _eligible(monkeypatch, db, repo, {"41": ("nvidia", "nvidia/expired")})
    assert kept == ["41"] and not denied


def test_cooling_model_does_not_suppress_other_models_or_sources(env, monkeypatch) -> None:
    db, repo = env
    support.health(db, repo, "nvidia", "nvidia/a", "cooling", MINUTES_20)
    names = {
        "41": ("nvidia", "nvidia/a"),
        "42": ("nvidia", "nvidia/b"),
        "43": ("opencode-free", "oc/big-pickle"),
    }
    kept, denied = _eligible(monkeypatch, db, repo, names)
    assert kept == ["42", "43"]
    assert len(denied) == 1 and "nvidia/a" in denied[0]


def test_model_failure_backoff_does_not_cool_other_models_of_provider(env, monkeypatch) -> None:
    db, repo = env
    for outcome, expected in (("provider_failure", ["42"]), ("provider_throttle", ["41", "42"])):
        support.cleanup(db, repo)
        support.backoff(
            db,
            repo,
            "model",
            source="nvidia",
            model="synthetic/failed",
            outcome=outcome,
            streak=3,
            retry_in=MINUTES_20,
        )
        names = {"41": ("nvidia", "synthetic/failed"), "42": ("nvidia", "synthetic/other")}
        kept, _ = _eligible(monkeypatch, db, repo, names)
        assert kept == expected, outcome


def test_provider_concurrency_limit_does_not_invent_unconfigured_caps(env, monkeypatch) -> None:
    db, repo = env
    first = support.workers_by_source()["nvidia"][0]
    db.batch(
        [
            (
                "INSERT INTO factory_provider_budget (repo, source, concurrency_limit, updated_at)"
                " VALUES (:r, 'nvidia', 1, unixepoch())",
                {"r": repo},
            )
        ]
    )
    assert importlib.import_module("factory_state_ledger").acquire_lease(repo, first, [9])
    names = {"41": ("nvidia", "nvidia/a"), "43": ("opencode-free", "oc/big-pickle")}
    kept, denied = _eligible(monkeypatch, db, repo, names)
    assert kept == ["43"]
    assert "full" in denied[0]


def test_daily_usage_counts_store_assignments(env) -> None:
    db, repo = env
    statements = [
        support.core.event_insert(
            repo,
            "github-actions",
            "worker_assigned",
            {"event_id": f"{repo}:assigned:{n}", "worker": worker, "source": source},
            subject_type="worker",
            subject_number=int(worker),
        )
        for n, (worker, source) in enumerate(
            (("41", "nvidia"), ("42", "opencode-free"), ("41", "nvidia"))
        )
    ]
    db.batch(statements)
    usage = operations.daily_usage(repo, ["nvidia", "opencode-free", "mistral"])
    assert usage == {"nvidia": 2, "opencode-free": 1, "mistral": 0}


def test_retry_suppression_is_exact_head_and_exact_stage_only(env) -> None:
    _, repo = env
    sha = "a" * 40
    event = {
        "workflow_run": {
            "status": "completed",
            "conclusion": "failure",
            "name": "Local Ollama Review",
            "head_sha": sha,
            "id": 5,
            "pull_requests": [{"number": 5000}],
        }
    }
    assert operations.ingest_check(repo, event) == 1
    assert operations.cooling_prs(repo, {5000: (sha, "factory:review")}) == {5000}
    assert operations.cooling_prs(repo, {5000: ("b" * 40, "factory:review")}) == set()
    assert operations.cooling_prs(repo, {5000: (sha, "factory:ci")}) == set()


def test_worker_ranking_preserves_source_diversity(env, monkeypatch) -> None:
    db, repo = env
    db.batch(
        [
            (
                "INSERT INTO factory_worker_metrics (repo, worker, source, model, attempts,"
                " productive, updated_at) VALUES (:r, :w, 'nvidia', 'm', 10, :p, unixepoch())",
                {"r": repo, "w": worker, "p": productive},
            )
            for worker, productive in (("41", 1), ("42", 7))
        ]
    )
    support.use_roster(
        monkeypatch,
        {
            "41": ("nvidia", "nvidia/a"),
            "42": ("nvidia", "nvidia/b"),
            "43": ("opencode-free", "oc/big-pickle"),
        },
    )
    assert operations.rank_workers(repo, ["41", "43", "42"]) == ["42", "43", "41"]


def test_model_exclusions_are_scoped_bounded_and_include_retired(env, monkeypatch) -> None:
    db, repo = env
    support.use_roster(monkeypatch, {"111": ("opencode-free", "")})
    assert operations.export_model_exclusions(repo, "111") == {}
    support.health(db, repo, "nvidia", "retired-model", "retired", None)
    support.health(db, repo, "nvidia", "cooling-model", "cooling", MINUTES_20)
    support.health(db, repo, "nvidia", "cooled-model", "cooling", -60)
    support.health(db, f"{repo}-other", "nvidia", "other-repo", "retired", None)
    try:
        found = operations.export_model_exclusions(repo, "111")
        assert sorted(found["nvidia"]) == ["cooling-model", "retired-model"]
        with pytest.raises(ValueError):
            operations.export_model_exclusions(repo, "999")
    finally:
        support.cleanup(db, f"{repo}-other")


def test_model_backoffs_are_in_trusted_catalog_exclusions(env, monkeypatch) -> None:
    db, repo = env
    support.use_roster(monkeypatch, {"501": ("openrouter-free", "")})
    support.backoff(
        db,
        repo,
        "model",
        source="openrouter-free",
        model="vendor/failed:free",
        outcome="provider_failure",
        streak=3,
        retry_in=MINUTES_20,
    )
    exclusions = operations.export_model_exclusions(repo)
    assert exclusions["openrouter-free"] == ["vendor/failed:free"]


def test_legacy_suspension_no_longer_excludes_worker(env, monkeypatch) -> None:
    db, repo = env
    support.backoff(
        db,
        repo,
        "worker",
        worker="139",
        source="vercel-ai-gateway",
        model="reflection/beam-free",
        streak=14,
        detail={"suspended": True},
    )
    names = {
        "139": ("vercel-ai-gateway", "reflection/beam-free"),
        "41": ("kilo-auto", "kilo-auto/free"),
    }
    kept, denied = _eligible(monkeypatch, db, repo, names)
    assert kept == ["139", "41"] and not denied


def test_shared_model_fibonacci_backoff_blocks_both_workers_not_other_models(
    env, monkeypatch
) -> None:
    db, repo = env
    support.backoff(
        db, repo, "model", source="kilo-auto", model="kilo-auto/free", streak=5, retry_in=1500
    )
    names = {
        "46": ("kilo-auto", "kilo-auto/free"),
        "98": ("kilo-auto", "kilo-auto/free"),
        "41": ("opencode-free", "other-free"),
    }
    kept, denied = _eligible(monkeypatch, db, repo, names)
    assert kept == ["41"]
    assert len(denied) == 2 and all("source/model" in item for item in denied)


def test_catalog_slot_model_failure_does_not_suppress_other_models(env, monkeypatch) -> None:
    db, repo = env
    support.backoff(
        db,
        repo,
        "worker",
        worker="501",
        source="openrouter-free",
        model="vendor/paid",
        outcome="provider_quota",
        streak=3,
        retry_in=MINUTES_20,
        expires_in=MINUTES_20,
    )
    kept, denied = _eligible(monkeypatch, db, repo, {"501": ("openrouter-free", "")})
    assert kept == ["501"] and not denied


def test_worker_provider_model_backoffs_remain_independent(env, monkeypatch) -> None:
    db, repo = env
    support.backoff(
        db,
        repo,
        "worker",
        worker="41",
        source="nvidia",
        model="generated/model",
        outcome="provider_throttle",
        streak=3,
        retry_in=900,
        expires_in=900,
    )
    kept, rejected = _eligible(monkeypatch, db, repo, {"41": ("nvidia", "@catalog")})
    assert kept == ["41"] and not rejected


def test_proven_account_cooldown_blocks_only_its_provider(env, monkeypatch) -> None:
    db, repo = env
    support.backoff(
        db, repo, "provider", source="nvidia", outcome="provider_quota", streak=2, expires_in=900
    )
    names = {"41": ("nvidia", "@catalog"), "42": ("requesty", "@catalog")}
    kept, rejected = _eligible(monkeypatch, db, repo, names)
    assert kept == ["42"]
    assert len(rejected) == 1 and "nvidia" in rejected[0]


def test_fleet_admission_does_not_double_book_capped_provider(env, monkeypatch) -> None:
    db, repo = env
    import factory_source_budget

    support.use_roster(monkeypatch, {"41": ("nvidia", "@catalog")})
    monkeypatch.setattr(factory_source_budget, "load_budgets", lambda: {"cloudflare-workers-ai": 1})
    db.batch(
        [
            (
                "INSERT INTO factory_provider_budget (repo, source, concurrency_limit, updated_at)"
                " VALUES (:r, 'requesty', 1, unixepoch())",
                {"r": repo},
            )
        ]
    )
    allowed = operations.export_fleet_failover_sources(repo, "41")
    assert "nvidia" in allowed
    assert "requesty" not in allowed
    assert "cloudflare-workers-ai" not in allowed


def test_reset_is_scoped_and_preserves_usage_and_runtime_history(env) -> None:
    db, repo = env
    other = f"{repo}-other"
    worker = support.uncapped_workers(1)[0]
    token = importlib.import_module("factory_state_ledger").acquire_lease(repo, worker, [9])
    support.health(db, repo, "nvidia", "cooling", "cooling", MINUTES_20)
    support.health(db, repo, "nvidia", "gone", "retired", None)
    support.backoff(db, repo, "worker", worker="41", source="nvidia", model="m", retry_in=60)
    support.backoff(db, repo, "model", source="nvidia", model="m", retry_in=60)
    support.backoff(db, repo, "provider", source="nvidia", outcome="provider_quota")
    support.backoff(db, other, "model", source="nvidia", model="m", retry_in=60)
    try:
        assert operations.reset_model_health(repo) == {
            "model_health_rows": 1,
            "backoff_rows": 3,
            "provider_cooldowns": 1,
        }
        remaining = db.read("SELECT model FROM factory_model_health WHERE repo = :r", {"r": repo})
        assert remaining == [{"model": "gone"}]
        assert (
            support.count(db, "SELECT count(*) AS n FROM factory_backoff WHERE repo = :r", r=other)
            == 1
        )
        assert (
            support.count(
                db, "SELECT count(*) AS n FROM factory_provider_reservation WHERE repo = :r", r=repo
            )
            == 1
        )
        assert (
            support.count(
                db, "SELECT count(*) AS n FROM factory_state_events WHERE repo = :r", r=repo
            )
            == 1
        )
        assert db.read(
            "SELECT health_reset_at IS NOT NULL AS marked FROM factory_repo_state WHERE repo = :r",
            {"r": repo},
        ) == [{"marked": 1}]
        assert operations.reset_model_health(repo, True)["model_health_rows"] == 1
        assert token
    finally:
        support.cleanup(db, other)
