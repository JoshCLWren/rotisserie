"""Slot leases (#239) against a real PostgreSQL server: one holder per slot.

Uses the disposable server fixture of the external claim suite. Set
``FACTORY_TEST_POSTGRES_URL`` to a LOCAL throwaway server when no server
binaries are installed; the fixture drops and recreates ``factory_state``.
"""

from __future__ import annotations

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from tests.test_external_claim_postgres import _sql, neon, pytestmark, server  # noqa: F401

ledger = importlib.import_module("factory_state_ledger")
operations = importlib.import_module("factory_state_operations")
worker_slots = importlib.import_module("factory_worker_slots").slots

REPO = "acme/widgets"
ISSUE = 239


def _workers_by_source() -> dict[str, list[str]]:
    """Providers without a daily run cap, so capacity never decides a race."""
    capped = importlib.import_module("factory_source_budget").load_budgets()
    found: dict[str, list[str]] = {}
    for entry in worker_slots():
        if entry.source not in capped:
            found.setdefault(entry.source, []).append(entry.worker)
    return found


BY_SOURCE = _workers_by_source()
ONE_PER_PROVIDER = [workers[0] for workers in BY_SOURCE.values()][:8]
SAME_PROVIDER = next(workers for workers in BY_SOURCE.values() if len(workers) >= 8)[:8]


@pytest.fixture
def slots_db(neon: str) -> str:  # noqa: F811
    async def prepare() -> None:
        conn = await ledger.measured_connect(dsn=ledger._dsn(), timeout=8, command_timeout=8)
        try:
            await operations.ensure_schema(conn)
            await conn.execute(
                "DELETE FROM public.factory_provider_reservation;"
                " DELETE FROM public.factory_provider_budget;"
                " DELETE FROM public.factory_model_health"
            )
        finally:
            await conn.close()

    asyncio.run(prepare())
    return neon


def _slot_rows() -> list[dict[str, Any]]:
    return _sql(
        """SELECT cache_key, payload->0->>'claim_id' AS claim, payload->0->>'worker' AS worker
           FROM public.factory_state_kv WHERE starts_with(cache_key, $1) ORDER BY cache_key""",
        ledger.slot_prefix(REPO, ISSUE),
    )


def _race(workers: list[str], slots: list[str], distinct: str = "provider") -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        return list(
            pool.map(
                lambda w: ledger.acquire_slot(REPO, w, ISSUE, slots, 600, distinct=distinct),
                workers,
            )
        )


def test_eight_clients_racing_one_attempt_slot_yield_exactly_one_holder(slots_db: str) -> None:
    answers = _race(ONE_PER_PROVIDER, ["attempt-1"])
    winners = [a for a in answers if a["status"] == "reserved"]
    assert len(winners) == 1
    assert {a["reason"] for a in answers if a["status"] != "reserved"} == {"no_free_slot"}
    rows = _slot_rows()
    assert [row["claim"] for row in rows] == [winners[0]["claim_id"]]


def test_eight_clients_racing_three_attempts_fill_each_slot_once(slots_db: str) -> None:
    answers = _race(ONE_PER_PROVIDER, ledger.attempt_slots(3))
    winners = [a for a in answers if a["status"] == "reserved"]
    assert sorted(a["slot"] for a in winners) == ["attempt-1", "attempt-2", "attempt-3"]
    rows = _slot_rows()
    assert len(rows) == 3
    assert len({row["claim"] for row in rows}) == 3
    assert len({row["worker"] for row in rows}) == 3
    reservations = _sql("SELECT count(*) AS n FROM public.factory_provider_reservation")
    assert reservations[0]["n"] == 3


def test_same_provider_clients_get_one_attempt_unless_sharing_is_allowed(slots_db: str) -> None:
    answers = _race(SAME_PROVIDER, ledger.attempt_slots(3))
    assert [a["status"] for a in answers].count("reserved") == 1
    assert {a["reason"] for a in answers if a["status"] != "reserved"} == {
        "provider_already_racing"
    }
    _sql("DELETE FROM public.factory_state_kv; DELETE FROM public.factory_provider_reservation")
    shared = _race(SAME_PROVIDER, ledger.attempt_slots(3), distinct="none")
    assert sorted(a["slot"] for a in shared if a["status"] == "reserved") == [
        "attempt-1",
        "attempt-2",
        "attempt-3",
    ]


def test_a_worker_holds_one_claim_at_a_time(slots_db: str) -> None:
    worker = ONE_PER_PROVIDER[0]
    first = ledger.acquire_slot(REPO, worker, ISSUE, ledger.attempt_slots(3), 600)
    assert first["status"] == "reserved"
    again = ledger.acquire_slot(REPO, worker, ISSUE, ledger.attempt_slots(3), 600, distinct="none")
    assert again == {"status": "conflict", "reason": "worker_busy"}
    assert ledger.acquire_lease(REPO, worker, [300], 600) is None


def test_the_normal_release_frees_the_slot_worker_and_capacity(slots_db: str) -> None:
    worker = ONE_PER_PROVIDER[0]
    claim = ledger.acquire_slot(REPO, worker, ISSUE, ["attempt-1"], 600)
    assert ledger.worker_claim(REPO, worker, active_only=True) == (claim["claim_id"], [ISSUE])
    assert ledger.release_lease(REPO, worker, claim["claim_id"], "superseded") is True
    assert _slot_rows() == []
    assert _sql("SELECT count(*) AS n FROM public.factory_state_kv")[0]["n"] == 0
    assert _sql("SELECT count(*) AS n FROM public.factory_provider_reservation")[0]["n"] == 0
    other = ledger.acquire_slot(REPO, ONE_PER_PROVIDER[1], ISSUE, ["attempt-1"], 600)
    assert other["status"] == "reserved"


def test_a_stale_token_cannot_release_a_new_holder(slots_db: str) -> None:
    worker = ONE_PER_PROVIDER[0]
    old = ledger.acquire_slot(REPO, worker, ISSUE, ["attempt-1"], 600)
    ledger.release_lease(REPO, worker, old["claim_id"], "done")
    new = ledger.acquire_slot(REPO, worker, ISSUE, ["attempt-1"], 600)
    assert ledger.release_lease(REPO, worker, old["claim_id"], "stale") is False
    assert [row["claim"] for row in _slot_rows()] == [new["claim_id"]]


def test_renewal_extends_slot_rows(slots_db: str) -> None:
    worker = ONE_PER_PROVIDER[0]
    ledger.acquire_slot(REPO, worker, ISSUE, ["attempt-1"], 60)
    assert ledger.renew_verified_workers(REPO, [worker]) == 1
    left = _sql(
        """SELECT extract(epoch FROM expires_at - now()) AS s FROM public.factory_state_kv
           WHERE starts_with(cache_key, $1)""",
        ledger.slot_prefix(REPO, ISSUE),
    )
    assert left[0]["s"] > 120


def test_an_expired_slot_and_its_provider_are_free_again(slots_db: str) -> None:
    first = ledger.acquire_slot(REPO, ONE_PER_PROVIDER[0], ISSUE, ["attempt-1"], 600)
    blocked = ledger.acquire_slot(REPO, ONE_PER_PROVIDER[1], ISSUE, ["attempt-1"], 600)
    assert blocked == {"status": "conflict", "reason": "no_free_slot"}
    _sql("UPDATE public.factory_state_kv SET expires_at = now() - interval '1 minute' RETURNING 1")
    freed = ledger.acquire_slot(REPO, ONE_PER_PROVIDER[1], ISSUE, ["attempt-1"], 600)
    assert freed["status"] == "reserved"
    assert [row["claim"] for row in _slot_rows()] == [freed["claim_id"]]
    assert ledger.release_lease(REPO, ONE_PER_PROVIDER[0], first["claim_id"], "late") is True
    assert [row["claim"] for row in _slot_rows()] == [freed["claim_id"]]


def test_pieces_are_slots_taken_in_the_callers_order(slots_db: str) -> None:
    pieces = ["piece-api", "piece-ui", "piece-docs"]
    taken = [
        ledger.acquire_slot(REPO, worker, ISSUE, pieces, 600)["slot"]
        for worker in ONE_PER_PROVIDER[:3]
    ]
    assert taken == pieces
    holders = ledger.slot_holders(REPO, ISSUE)
    assert [row["slot"] for row in holders] == sorted(pieces)
    assert all(row["active"] for row in holders)


def test_slot_rows_list_as_leases_on_their_issue(slots_db: str) -> None:
    claim = ledger.acquire_slot(REPO, ONE_PER_PROVIDER[0], ISSUE, ["attempt-2"], 600)
    rows = {row["key"]: row for row in ledger.list_leases(REPO)}
    row = rows[ledger.slot_key(REPO, ISSUE, "attempt-2")]
    assert row["claim_id"] == claim["claim_id"]
    assert row["targets"] == [ISSUE]
    assert row["operation"] == "slot:attempt-2"
    assert not any(key.startswith(f"factory-lease-v1:{REPO}:target:") for key in rows)
