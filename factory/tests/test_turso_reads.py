"""Every libSQL read, export and reset runs once per transport and returns its documented shape."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests import turso_support as support

leases = importlib.import_module("factory_turso_leases")
slots = importlib.import_module("factory_turso_slots")
external = importlib.import_module("factory_turso_external")
events = importlib.import_module("factory_turso_events")
operations = importlib.import_module("factory_turso_operations")
exports = importlib.import_module("factory_turso_exports")
TARGET = 55


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def _history(repo: str, worker: str) -> None:
    source = operations.roster_source(worker)
    events.record_events(
        repo,
        [
            {
                "event_type": "model_attempt",
                "event_id": f"{repo}:a1",
                "worker": worker,
                "run_id": "71",
                "source": source,
                "model": "m",
                "agent_exit_code": -1,
            },
            {
                "event_type": "model_attempt",
                "event_id": f"{repo}:a2",
                "worker": worker,
                "run_id": "71",
                "source": source,
                "model": "m",
                "outcome_class": "catalog_refusal",
            },
        ],
    )
    events.record_runtime_outcome(
        repo,
        "71",
        {
            "worker": worker,
            "source": source,
            "model": "m",
            "outcome_class": "success",
            "exact_model_proven": True,
            "selected_number": TARGET,
        },
    )


def test_a_failed_statement_rolls_back_on_every_transport(env, monkeypatch) -> None:
    db, repo = env
    original = leases.claim_statements

    def failing(*args, **kwargs):
        return [*original(*args, **kwargs)[:-1], ("INSERT INTO no_such_table VALUES (1)", {})]

    monkeypatch.setattr(leases, "claim_statements", failing)
    with pytest.raises(support.libsql.StoreError):
        leases.acquire_lease(repo, support.uncapped_workers(1)[0], [TARGET])
    assert support.count(db, "SELECT count(*) AS n FROM factory_lease WHERE repo = :r", r=repo) == 0


def test_event_history_reads(env) -> None:
    _, repo = env
    worker = support.uncapped_workers(1)[0]
    token = leases.acquire_lease(repo, worker, [TARGET])
    _history(repo, worker)
    assert events.recorded_runtime_runs(repo, ["71", "72"]) == {"71"}
    assert events.run_workers(repo, ["71"]) == {"71": worker}
    kinds = {
        (r["event_type"], r["row_count"])
        for r in events.history_rows(
            repo, 2, 100, ["worker_assigned", "model_attempt", "worker_completed"]
        )
    }
    assert ("model_attempt", 1) in kinds and ("worker_completed", 1) in kinds
    view = events.export_view(repo)
    assert view["attempts"][worker]["attempt outcome"] == "success"
    assert set(view["health"]) == {"models", "providers"}
    usage = operations.daily_usage(repo, [operations.roster_source(worker), "none"])
    assert usage == {operations.roster_source(worker): 1, "none": 0}
    assert events.prune_lifecycle(repo) == 0
    assert leases.release_lease(repo, worker, token, "pre-session-abort: test")


def test_operator_exports_and_resets(env) -> None:
    db, repo = env
    worker = support.workers_by_source()["nvidia"][0]
    failure = {
        "worker": worker,
        "source": "nvidia",
        "model": "m",
        "outcome_class": "unknown_failure",
        "exact_model_proven": True,
    }
    events.record_runtime_outcome(repo, "301", failure)
    events.record_runtime_outcome(repo, "302", {**failure, "outcome_class": "model_retired_410"})
    exclusions = exports.export_model_exclusions(repo, worker)
    assert "m" in exclusions["nvidia"]
    backoffs = exports.export_backoffs(repo)
    assert backoffs["providers"]["nvidia"]["fibonacci"]
    retry = exports.export_dashboard_retry(repo)
    assert {b["kind"] for b in retry["backoffs"]} == {"worker", "model"}
    reliability = exports.export_reliability(repo)
    model = next(m for m in reliability["models"] if m["model"] == "m")
    assert model["windows"]["24h"]["failed"] == 2
    stats = operations.stats(repo)
    assert stats["workers"][0]["attempts"] == 2
    assert operations.rank_workers(repo, [worker]) == [worker]
    assert "nvidia" in exports.export_fleet_failover_sources(repo, worker)
    reset = exports.reset_model_health(repo)
    assert reset["backoff_rows"] >= 2 and reset["model_health_rows"] == 0
    assert exports.reset_model_health(repo, include_retired=True)["model_health_rows"] == 1
    assert db.read("SELECT health_reset_at FROM factory_repo_state WHERE repo = :r", {"r": repo})


def test_slot_holders_and_external_renew_and_release(env) -> None:
    _, repo = env
    worker = support.workers_by_source()["nvidia"][0]
    reserved = slots.acquire_slot(repo, worker, TARGET, ["attempt-1"])
    holders = slots.slot_holders(repo, TARGET)
    assert [(h["slot"], h["worker"], h["active"]) for h in holders] == [("attempt-1", worker, True)]
    assert leases.release_lease(repo, worker, reserved["claim_id"], "done")
    assert slots.slot_holders(repo, TARGET) == []
    claim = external.reserve_external(
        repo,
        "alice",
        "cli",
        [TARGET + 1],
        target=f"issue:{TARGET + 1}",
        action="review",
        head=None,
        observed_wip=0,
        wip_limit=5,
        offer_at=datetime.now(UTC).isoformat(),
    )
    renewed = external.external_lease_action(
        repo, "alice", "cli", claim["claim_id"], operation="renew", ttl=900
    )
    assert renewed["status"] == "renewed" and renewed["expires_at"]
    released = external.external_lease_action(
        repo, "alice", "cli", claim["claim_id"], operation="release"
    )
    assert released["status"] == "released" and external.external_holder(repo, TARGET + 1) is None


def test_operator_provider_limits_hold_inside_the_acquisition(env) -> None:
    db, repo = env
    first, second, third = support.workers_by_source()["nvidia"][:3]
    db.batch(
        [
            (
                "INSERT INTO factory_provider_budget (repo, source, concurrency_limit, updated_at)"
                " VALUES (:r, 'nvidia', 1, unixepoch())",
                {"r": repo},
            )
        ]
    )
    assert leases.acquire_lease(repo, first, [TARGET])
    kept, rejected = operations.filter_workers(repo, [second])
    assert kept == [] and "full" in rejected[0]
    assert leases.acquire_lease(repo, second, [TARGET + 1]) is None
    other = support.workers_by_source()["mistral"][0]
    assert "nvidia" not in exports.export_fleet_failover_sources(repo, other)
    db.batch(
        [
            (
                "UPDATE factory_provider_budget SET concurrency_limit = NULL, daily_limit = 1"
                " WHERE repo = :r",
                {"r": repo},
            )
        ]
    )
    assert leases.acquire_lease(repo, third, [TARGET + 2]) is None
