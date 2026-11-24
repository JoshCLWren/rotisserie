"""Lifecycle dedup, repository isolation and attempt rollup on the factory store.

Ported from the PostgreSQL state access suite. Races run in separate
processes with their own connections.
"""

from __future__ import annotations

import importlib
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

ledger = importlib.import_module("factory_state_ledger")
events = importlib.import_module("factory_turso_events")


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state.sqlite"
    with support.bound(request.param, path, monkeypatch) as (db, repo):
        yield support.environment(request.param, path), db, repo


def _race(environment: dict[str, str], calls: list[tuple[Any, ...]]) -> list[Any]:
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=len(calls), mp_context=context) as pool:
        futures = [pool.submit(support.in_child, environment, *call) for call in calls]
        return [future.result() for future in futures]


def test_lifecycle_dedup_is_idempotent_under_concurrent_collectors(env) -> None:
    environment, db, repo = env
    worker = support.uncapped_workers(1)[0]
    run = str(int(repo[-6:], 16) + 1)
    later = str(int(run) + 1)
    assert ledger.record_lifecycle(repo, run, worker, "started") == 1
    assert ledger.record_lifecycle(repo, run, worker, "started") == 0
    assert ledger.record_lifecycle(repo, run, worker, "finished") == 1
    added = _race(environment, [(ledger.record_lifecycle, repo, later, worker, "started")] * 6)
    assert sorted(added) == [0, 0, 0, 0, 0, 1]
    rows = db.read(
        "SELECT event_id FROM factory_state_events WHERE repo = :r ORDER BY id", {"r": repo}
    )
    assert [r["event_id"] for r in rows] == [
        f"lifecycle:{run}:started",
        f"lifecycle:{run}:finished",
        f"lifecycle:{later}:started",
    ]


def test_external_wip_counts_only_the_exact_repository(env) -> None:
    _, _, repo = env
    claim = ledger.reserve_external(
        repo,
        "alice",
        "cli",
        [7],
        target="issue:7",
        action="implement",
        head=None,
        observed_wip=0,
        wip_limit=8,
        offer_at="2026-11-22T00:00:00+00:00",
    )
    assert claim["status"] == "reserved"
    owner, name = repo.split("/")
    assert ledger.external_wip_count(repo) == 1
    assert ledger.external_wip_count(f"{owner}/{name[:-1]}_") == 0
    assert ledger.external_wip_count(f"{owner}/{name[:3]}%") == 0


def _attempt(repo: str, run: int, index: int, **detail: Any) -> dict[str, Any]:
    return {
        "event_type": "model_attempt",
        "event_id": f"{repo}:model-attempt:{run}:{index}",
        "worker": "3",
        "run_id": str(run),
        "source": "nvidia",
        "model": "m1",
        "outcome_class": "provider_failure",
        "duration_ms": 1000,
        **detail,
    }


def _age(db: Any, repo: str, run: int, days: int) -> None:
    db.batch(
        [
            (
                "UPDATE factory_state_events SET created_at = unixepoch() - :d * 86400"
                " WHERE repo = :r AND run_id = :run",
                {"r": repo, "run": str(run), "d": days},
            )
        ]
    )


def test_old_model_attempts_roll_into_daily_counts_once(env) -> None:
    environment, db, repo = env
    other = f"{repo}-other"
    try:
        events.record_events(repo, [_attempt(repo, 1, i) for i in range(4)])
        events.record_events(
            repo, [_attempt(repo, 2, 0, duration_ms=None, outcome_class="success")]
        )
        events.record_events(repo, [_attempt(repo, 3, 0)])
        events.record_events(other, [_attempt(other, 4, 0)])
        for target, run, days in ((repo, 1, 130), (repo, 2, 130), (repo, 3, 100), (other, 4, 130)):
            _age(db, target, run, days)
        moved = _race(environment, [(events.rollup_model_attempts, repo)] * 3)
        assert sum(moved) == 5
        daily = db.read(
            "SELECT outcome_class, attempts, timed_attempts, duration_ms"
            " FROM factory_model_attempt_daily WHERE repo = :r ORDER BY outcome_class",
            {"r": repo},
        )
        assert daily == [
            {
                "outcome_class": "provider_failure",
                "attempts": 4,
                "timed_attempts": 4,
                "duration_ms": 4000,
            },
            {"outcome_class": "success", "attempts": 1, "timed_attempts": 0, "duration_ms": 0},
        ]
        left = db.read(
            "SELECT run_id FROM factory_state_events WHERE repo IN (:r, :o) ORDER BY run_id",
            {"r": repo, "o": other},
        )
        assert [r["run_id"] for r in left] == ["3", "4"]
        assert events.rollup_model_attempts(repo) == 0
    finally:
        support.cleanup(db, other)
