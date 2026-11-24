"""Slot leases on the factory store: one holder per slot, raced by real processes.

Ported from the PostgreSQL suite. Each case runs on a SQLite file, and on a
libSQL server when one is configured.
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
ISSUE = 239


def _uncapped_by_source() -> dict[str, list[str]]:
    capped = importlib.import_module("factory_source_budget").load_budgets()
    return {s: ws for s, ws in support.workers_by_source().items() if s not in capped}


BY_SOURCE = _uncapped_by_source()
ONE_PER_PROVIDER = [workers[0] for workers in BY_SOURCE.values()][:8]
SAME_PROVIDER = next(workers for workers in BY_SOURCE.values() if len(workers) >= 8)[:8]


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state.sqlite"
    with support.bound(request.param, path, monkeypatch) as (db, repo):
        yield support.environment(request.param, path), db, repo


def _acquire(repo: str, worker: str, slots: list[str], distinct: str) -> dict[str, Any]:
    return ledger.acquire_slot(repo, worker, ISSUE, slots, 600, distinct=distinct)


def _race(env: Any, workers: list[str], slots: list[str], distinct: str = "provider") -> list[Any]:
    environment, _, repo = env
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=len(workers), mp_context=context) as pool:
        futures = [
            pool.submit(support.in_child, environment, _acquire, repo, w, slots, distinct)
            for w in workers
        ]
        return [future.result() for future in futures]


def _slot_rows(db: Any, repo: str) -> list[dict[str, Any]]:
    return db.read(
        "SELECT lease_key, claim_id AS claim, worker FROM factory_lease"
        " WHERE repo = :r AND kind = 'slot' AND number = :n ORDER BY lease_key",
        {"r": repo, "n": ISSUE},
    )


def _count(db: Any, table: str, repo: str) -> int:
    return support.count(db, f"SELECT count(*) AS n FROM {table} WHERE repo = :r", r=repo)


def test_eight_clients_racing_one_attempt_slot_yield_exactly_one_holder(env) -> None:
    _, db, repo = env
    answers = _race(env, ONE_PER_PROVIDER, ["attempt-1"])
    winners = [a for a in answers if a["status"] == "reserved"]
    assert len(winners) == 1
    assert {a["reason"] for a in answers if a["status"] != "reserved"} == {"no_free_slot"}
    assert [row["claim"] for row in _slot_rows(db, repo)] == [winners[0]["claim_id"]]


def test_eight_clients_racing_three_attempts_fill_each_slot_once(env) -> None:
    _, db, repo = env
    answers = _race(env, ONE_PER_PROVIDER, ledger.attempt_slots(3))
    winners = [a for a in answers if a["status"] == "reserved"]
    assert sorted(a["slot"] for a in winners) == ["attempt-1", "attempt-2", "attempt-3"]
    rows = _slot_rows(db, repo)
    assert len(rows) == 3
    assert len({row["claim"] for row in rows}) == 3
    assert len({row["worker"] for row in rows}) == 3
    assert _count(db, "factory_provider_reservation", repo) == 3


def test_same_provider_clients_get_one_attempt_unless_sharing_is_allowed(env) -> None:
    _, db, repo = env
    answers = _race(env, SAME_PROVIDER, ledger.attempt_slots(3))
    assert [a["status"] for a in answers].count("reserved") == 1
    reasons = {a["reason"] for a in answers if a["status"] != "reserved"}
    assert reasons == {"provider_already_racing"}
    support.cleanup(db, repo)
    shared = _race(env, SAME_PROVIDER, ledger.attempt_slots(3), distinct="none")
    assert sorted(a["slot"] for a in shared if a["status"] == "reserved") == [
        "attempt-1",
        "attempt-2",
        "attempt-3",
    ]


def test_a_worker_holds_one_claim_at_a_time(env) -> None:
    _, _, repo = env
    worker = ONE_PER_PROVIDER[0]
    assert _acquire(repo, worker, ledger.attempt_slots(3), "provider")["status"] == "reserved"
    again = _acquire(repo, worker, ledger.attempt_slots(3), "none")
    assert again == {"status": "conflict", "reason": "worker_busy"}
    assert ledger.acquire_lease(repo, worker, [300], 600) is None


def test_the_normal_release_frees_the_slot_worker_and_capacity(env) -> None:
    _, db, repo = env
    worker = ONE_PER_PROVIDER[0]
    claim = _acquire(repo, worker, ["attempt-1"], "provider")
    assert ledger.worker_claim(repo, worker, active_only=True) == (claim["claim_id"], [ISSUE])
    assert ledger.release_lease(repo, worker, claim["claim_id"], "superseded") is True
    assert _slot_rows(db, repo) == []
    assert _count(db, "factory_lease", repo) == 0
    assert _count(db, "factory_provider_reservation", repo) == 0
    other = _acquire(repo, ONE_PER_PROVIDER[1], ["attempt-1"], "provider")
    assert other["status"] == "reserved"


def test_a_stale_token_cannot_release_a_new_holder(env) -> None:
    _, db, repo = env
    worker = ONE_PER_PROVIDER[0]
    old = _acquire(repo, worker, ["attempt-1"], "provider")
    ledger.release_lease(repo, worker, old["claim_id"], "done")
    new = _acquire(repo, worker, ["attempt-1"], "provider")
    assert ledger.release_lease(repo, worker, old["claim_id"], "stale") is False
    assert [row["claim"] for row in _slot_rows(db, repo)] == [new["claim_id"]]


def test_renewal_extends_slot_rows(env) -> None:
    _, db, repo = env
    worker = ONE_PER_PROVIDER[0]
    ledger.acquire_slot(repo, worker, ISSUE, ["attempt-1"], 60)
    assert ledger.renew_verified_workers(repo, [worker]) == 1
    left = db.read(
        "SELECT expires_at - unixepoch() AS s FROM factory_lease WHERE repo = :r AND kind = 'slot'",
        {"r": repo},
    )
    assert left[0]["s"] > 120


def test_an_expired_slot_and_its_provider_are_free_again(env) -> None:
    _, db, repo = env
    first = _acquire(repo, ONE_PER_PROVIDER[0], ["attempt-1"], "provider")
    blocked = _acquire(repo, ONE_PER_PROVIDER[1], ["attempt-1"], "provider")
    assert blocked == {"status": "conflict", "reason": "no_free_slot"}
    support.expire(db, repo, first["claim_id"])
    freed = _acquire(repo, ONE_PER_PROVIDER[1], ["attempt-1"], "provider")
    assert freed["status"] == "reserved"
    assert [row["claim"] for row in _slot_rows(db, repo)] == [freed["claim_id"]]
    assert ledger.release_lease(repo, ONE_PER_PROVIDER[0], first["claim_id"], "late") is True
    assert [row["claim"] for row in _slot_rows(db, repo)] == [freed["claim_id"]]


def test_pieces_are_slots_taken_in_the_callers_order(env) -> None:
    _, _, repo = env
    pieces = ["piece-api", "piece-ui", "piece-docs"]
    taken = [_acquire(repo, w, pieces, "provider")["slot"] for w in ONE_PER_PROVIDER[:3]]
    assert taken == pieces
    holders = ledger.slot_holders(repo, ISSUE)
    assert [row["slot"] for row in holders] == sorted(pieces)
    assert all(row["active"] for row in holders)


def test_slot_rows_list_as_leases_on_their_issue(env) -> None:
    _, _, repo = env
    claim = _acquire(repo, ONE_PER_PROVIDER[0], ["attempt-2"], "provider")
    rows = {row["key"]: row for row in ledger.list_leases(repo)}
    row = rows[ledger.slot_key(repo, ISSUE, "attempt-2")]
    assert row["claim_id"] == claim["claim_id"]
    assert row["targets"] == [ISSUE]
    assert row["operation"] == "slot:attempt-2"
    assert not any(key.startswith(f"factory-lease-v1:{repo}:target:") for key in rows)
