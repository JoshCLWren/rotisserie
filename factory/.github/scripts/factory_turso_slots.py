"""Slot leases on the libSQL store: several workers on one target, one holder per slot.

The read picks the first free slot and names a conflict; the write batch
re-checks worker, slot, provider distinctness and the daily cap in its anchor
statement. A lost race re-reads, so the caller always gets a current reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import factory_turso_core as core
from factory_state_ledger_core import _keys
from factory_state_ledger_slots import _validate, slot_prefix
from factory_turso_leases import claim_statements, daily_cap
from factory_work_policy import FIXED_LEASE_TTL_SECONDS

ATTEMPTS = 4


def _plan(
    db: core.Store, repo: str, worker: str, number: int, slots: Sequence[str], distinct: str
) -> dict[str, Any]:
    from factory_turso_operations import eligible_source, roster_source

    worker_rows, slot_rows = db.reads(
        [
            (
                "SELECT 1 AS x FROM factory_lease WHERE lease_key = :k AND expires_at > unixepoch()",
                {"k": _keys(repo, worker, [])[0]},
            ),
            (
                "SELECT slot, source FROM factory_lease WHERE repo = :repo AND kind = 'slot'"
                " AND number = :n AND expires_at > unixepoch()",
                {"repo": repo, "n": number},
            ),
        ]
    )
    if worker_rows:
        return {"status": "conflict", "reason": "worker_busy"}
    source = roster_source(worker)
    if distinct == "provider" and source in {str(r["source"] or "") for r in slot_rows}:
        return {"status": "conflict", "reason": "provider_already_racing"}
    taken = {str(r["slot"]) for r in slot_rows}
    free = [slot for slot in slots if slot not in taken]
    if not free:
        return {"status": "conflict", "reason": "no_free_slot"}
    if eligible_source(db, repo, worker) is None:
        return {"status": "conflict", "reason": "provider_capacity"}
    return {"status": "free", "slot": free[0], "source": source}


def acquire_slot(
    repo: str,
    worker: str,
    number: int,
    slots: Sequence[str],
    ttl: int = FIXED_LEASE_TTL_SECONDS,
    *,
    distinct: str = "provider",
) -> dict[str, Any]:
    """Lease the first free slot in ``slots`` on one target for one worker."""
    _validate(worker, number, slots, ttl, distinct)
    if not core.enabled():
        return {"status": "conflict", "reason": "ledger_disabled"}
    db = core.store()
    worker_key = _keys(repo, worker, [])[0]
    for _ in range(ATTEMPTS):
        plan = _plan(db, repo, worker, number, slots, distinct)
        if plan["status"] != "free":
            return plan
        token = str(uuid.uuid4())
        statements = claim_statements(
            repo,
            worker,
            [number],
            ttl,
            token,
            plan["source"],
            external_wip_seen=None,
            cap=daily_cap(worker, plan["source"]),
            slot=(plan["slot"], number, distinct == "provider"),
        )
        core.reconciled_batch(
            db, statements, lambda token=token: core.claim_of(db, worker_key) == token
        )
        if core.claim_of(db, worker_key) == token:
            key = slot_prefix(repo, number) + plan["slot"]
            return {"status": "reserved", "claim_id": token, "slot": plan["slot"], "key": key}
    return {"status": "conflict", "reason": "provider_capacity"}


def slot_holders(repo: str, number: int) -> list[dict[str, Any]]:
    if not core.enabled():
        return []
    rows = core.store().read(
        "SELECT slot, claim_id, worker, source, expires_at > unixepoch() AS active"
        " FROM factory_lease WHERE repo = :repo AND kind = 'slot' AND number = :n ORDER BY slot",
        {"repo": repo, "n": number},
    )
    return [
        {
            "slot": str(r["slot"]),
            "claim_id": str(r["claim_id"]),
            "worker": str(r["worker"] or ""),
            "source": str(r["source"] or ""),
            "active": bool(r["active"]),
        }
        for r in rows
    ]
