"""Coordination races on the libSQL store, run by separate processes, never mocks.

Each case starts real processes with their own connections and lets them race
on one store: a SQLite file always, and a libSQL server when one is configured.
"""

from __future__ import annotations

import importlib
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

leases = importlib.import_module("factory_turso_leases")
slots = importlib.import_module("factory_turso_slots")
external = importlib.import_module("factory_turso_external")
events = importlib.import_module("factory_turso_events")
TARGET = 4242
RACERS = 8


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield support.environment(request.param, tmp_path / "state.sqlite"), db, repo


def _race(environment: dict[str, str], calls: list[tuple[Any, ...]]) -> list[Any]:
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=len(calls), mp_context=context) as pool:
        futures = [pool.submit(support.in_child, environment, *call) for call in calls]
        return [future.result() for future in futures]


def _external_args(repo: str, number: int, actor: str) -> tuple[Any, ...]:
    return (_reserve_external, repo, number, actor)


def _reserve_external(repo: str, number: int, actor: str) -> str | None:
    result = external.reserve_external(
        repo,
        actor,
        "cli",
        [number],
        target=f"issue:{number}",
        action="review",
        head=None,
        observed_wip=0,
        wip_limit=10,
        offer_at=datetime.now(UTC).isoformat(),
    )
    return result.get("claim_id") if result["status"] == "reserved" else None


def test_two_workers_claim_the_same_target_exactly_one_wins(env) -> None:
    environment, db, repo = env
    workers = support.uncapped_workers(RACERS)
    won = _race(environment, [(leases.acquire_lease, repo, w, [TARGET]) for w in workers])
    assert len([token for token in won if token]) == 1
    assert (
        support.count(
            db,
            "SELECT count(*) AS n FROM factory_lease WHERE repo = :r AND kind = 'target'",
            r=repo,
        )
        == 1
    )
    assert (
        support.count(
            db, "SELECT count(*) AS n FROM factory_provider_reservation WHERE repo = :r", r=repo
        )
        == 1
    )


def test_daily_capped_provider_admits_exactly_one_claim(env) -> None:
    environment, db, repo = env
    capped = support.workers_by_source()["aion-free"][:RACERS]
    calls = [(leases.acquire_lease, repo, w, [TARGET + i]) for i, w in enumerate(capped)]
    assert len([token for token in _race(environment, calls) if token]) == 1
    assert (
        support.count(
            db,
            "SELECT count(*) AS n FROM factory_state_events WHERE repo = :r"
            " AND event_type = 'worker_assigned'",
            r=repo,
        )
        == 1
    )


def test_operator_concurrency_limit_admits_exactly_one_claim(env) -> None:
    environment, db, repo = env
    db.batch(
        [
            (
                "INSERT INTO factory_provider_budget (repo, source, concurrency_limit, updated_at)"
                " VALUES (:r, 'nvidia', 1, unixepoch())",
                {"r": repo},
            )
        ]
    )
    same = support.workers_by_source()["nvidia"][:RACERS]
    calls = [(leases.acquire_lease, repo, w, [TARGET + i]) for i, w in enumerate(same)]
    assert len([token for token in _race(environment, calls) if token]) == 1
    assert (
        support.count(
            db, "SELECT count(*) AS n FROM factory_provider_reservation WHERE repo = :r", r=repo
        )
        == 1
    )


def test_same_provider_slot_race_admits_exactly_one_attempt(env) -> None:
    environment, db, repo = env
    same = support.workers_by_source()["nvidia"][:RACERS]
    names = ["attempt-1", "attempt-2", "attempt-3"]
    results = _race(environment, [(slots.acquire_slot, repo, w, TARGET, names) for w in same])
    assert [r["status"] for r in results].count("reserved") == 1
    reasons = {r["reason"] for r in results if r["status"] == "conflict"}
    assert reasons <= {"provider_already_racing", "provider_capacity"}


def test_distinct_providers_fill_distinct_slots(env) -> None:
    environment, db, repo = env
    one_each = [ws[0] for s, ws in support.workers_by_source().items() if s != "aion-free"][:4]
    names = ["attempt-1", "attempt-2", "attempt-3", "attempt-4"]
    results = _race(environment, [(slots.acquire_slot, repo, w, TARGET, names) for w in one_each])
    taken = [r["slot"] for r in results if r["status"] == "reserved"]
    assert sorted(taken) == names


def test_worker_and_external_agent_race_exactly_one_holds_the_target(env) -> None:
    environment, db, repo = env
    workers = support.uncapped_workers(4)
    calls = [(leases.acquire_lease, repo, w, [TARGET]) for w in workers]
    calls += [_external_args(repo, TARGET, f"agent{i}") for i in range(4)]
    assert len([token for token in _race(environment, calls) if token]) == 1
    assert (
        support.count(
            db,
            "SELECT count(*) AS n FROM factory_lease WHERE repo = :r AND kind = 'target'",
            r=repo,
        )
        == 1
    )


def test_worker_and_operation_race_exactly_one_holds_the_target(env) -> None:
    environment, db, repo = env
    workers = support.uncapped_workers(4)
    calls = [(leases.acquire_lease, repo, w, [TARGET]) for w in workers]
    calls += [(leases.acquire_operation_lease, repo, f"branch-sync-{i}", TARGET) for i in range(4)]
    assert len([token for token in _race(environment, calls) if token]) == 1


def test_linked_issue_and_pr_claims_are_rejected_atomically(env) -> None:
    _, db, repo = env
    first, second = support.uncapped_workers(2)
    assert leases.acquire_lease(repo, first, [TARGET])
    assert leases.acquire_lease(repo, second, [TARGET + 1, TARGET]) is None
    keys = {r["key"] for r in leases.list_leases(repo)}
    assert not any(
        k.endswith(f":target:{TARGET + 1}") or k.endswith(f":worker:{second}") for k in keys
    )
    assert (
        support.count(
            db, "SELECT count(*) AS n FROM factory_provider_reservation WHERE repo = :r", r=repo
        )
        == 1
    )


def test_concurrent_event_ingestion_inserts_each_event_once(env) -> None:
    environment, db, repo = env
    worker = support.uncapped_workers(1)[0]

    def entries(offset: int) -> list[dict[str, Any]]:
        return [
            {
                "event_type": "model_attempt",
                "event_id": f"attempt:{n}",
                "worker": worker,
                "run_id": "77",
                "source": "nvidia",
                "model": f"m{n}",
            }
            for n in range(offset, offset + 30)
        ]

    calls = [(events.record_events, repo, entries(i * 10)) for i in range(6)]
    inserted = _race(environment, calls)
    stored = support.count(
        db, "SELECT count(*) AS n FROM factory_state_events WHERE repo = :r", r=repo
    )
    assert stored == 80
    assert sum(inserted) == 80


def test_duplicate_completion_counts_metrics_and_events_once(env) -> None:
    environment, db, repo = env
    worker = support.uncapped_workers(1)[0]
    source = importlib.import_module("factory_turso_operations").roster_source(worker)
    outcome = {
        "worker": worker,
        "source": source,
        "model": "m1",
        "outcome_class": "success",
        "exact_model_proven": True,
    }
    calls = [(events.record_runtime_outcome, repo, "9001", outcome) for _ in range(4)]
    assert sum(_race(environment, calls)) == 1
    rows = db.read(
        "SELECT attempts, completed FROM factory_worker_metrics WHERE repo = :r", {"r": repo}
    )
    assert rows == [{"attempts": 1, "completed": 1}]
