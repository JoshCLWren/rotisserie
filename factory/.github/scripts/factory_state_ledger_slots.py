"""Slot leases: several workers on one target, exactly one holder per slot.

A normal lease lets one worker hold an issue or PR. Incident racing (#239)
needs several separate fix attempts on one issue, and a later collaborative
mode needs one lease per planned piece. Both are slots of one target:

    factory-lease-v1:<repo>:slot:<number>:attempt-2
    factory-lease-v1:<repo>:slot:<number>:piece-api-schema

``acquire_slot`` is the only allocator. Under the repository advisory lock it
takes the first free slot from the caller's ordered list, writes the slot row
and the worker row with one claim ID, and reserves provider capacity like a
normal lease. A worker holds at most one claim. Siblings on the same target use
distinct providers unless the caller allows a shared one. The normal
``release_lease`` and ``renew_verified_workers`` release and renew slot rows by
claim ID, so a slot claim needs no separate release path. An expired slot is
free again, like an expired worker lease: renewal keeps a live run's slot.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Sequence
from typing import Any

from factory_state_cache import _dsn
from factory_state_ledger_core import LEASE_PREFIX, WORKER_RE, _keys, enabled, prefix_pattern
from factory_state_metrics import connect as measured_connect
from factory_work_policy import FIXED_LEASE_TTL_SECONDS

SLOT_RE = re.compile(r"^(?:attempt-[1-9][0-9]?|piece-[a-z0-9][a-z0-9-]{0,39})$")
MAX_SLOTS = 32
DISTINCT_CHOICES = ("provider", "none")


def slot_prefix(repo: str, number: int) -> str:
    return f"{LEASE_PREFIX}{repo}:slot:{number}:"


def slot_key(repo: str, number: int, slot: str) -> str:
    return slot_prefix(repo, number) + slot


def attempt_slots(count: int) -> list[str]:
    return [f"attempt-{index}" for index in range(1, count + 1)]


def _validate(worker: str, number: int, slots: Sequence[str], ttl: int, distinct: str) -> None:
    if not WORKER_RE.fullmatch(worker) or number <= 0:
        raise ValueError("invalid factory slot worker or target")
    if not slots or len(slots) > MAX_SLOTS or len(set(slots)) != len(slots):
        raise ValueError("invalid factory slot list")
    if any(not SLOT_RE.fullmatch(slot) for slot in slots):
        raise ValueError("invalid factory slot name")
    if not 60 <= ttl <= 86400:
        raise ValueError("invalid factory lease TTL")
    if distinct not in DISTINCT_CHOICES:
        raise ValueError("invalid factory slot distinctness rule")


def _first(raw: Any) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}


async def _acquire_slot(
    repo: str,
    worker: str,
    number: int,
    slots: Sequence[str],
    ttl: int,
    distinct: str,
) -> dict[str, Any]:
    from factory_state_operations import _roster, reserve_capacity

    source = _roster().get(worker, ("", ""))[0]
    worker_key = _keys(repo, worker, [])[0]
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo
            )
            if await conn.fetchval(
                """SELECT 1 FROM public.factory_state_kv
                   WHERE cache_key=$1 AND expires_at > now()""",
                worker_key,
            ):
                return {"status": "conflict", "reason": "worker_busy"}
            rows = await conn.fetch(
                """SELECT cache_key, payload FROM public.factory_state_kv
                   WHERE cache_key LIKE $1 AND expires_at > now()""",
                prefix_pattern(slot_prefix(repo, number)),
            )
            taken = {str(row["cache_key"]).removeprefix(slot_prefix(repo, number)) for row in rows}
            sources = {str(_first(row["payload"]).get("source") or "") for row in rows}
            if distinct == "provider" and source in sources:
                return {"status": "conflict", "reason": "provider_already_racing"}
            free = [slot for slot in slots if slot not in taken]
            if not free:
                return {"status": "conflict", "reason": "no_free_slot"}
            token = str(uuid.uuid4())
            if not await reserve_capacity(conn, repo, worker, token, ttl):
                return {"status": "conflict", "reason": "provider_capacity"}
            slot = free[0]
            key = slot_key(repo, number, slot)
            payload = json.dumps(
                [
                    {
                        "claim_id": token,
                        "worker": worker,
                        "targets": [number],
                        "operation": f"slot:{slot}",
                        "slot": slot,
                        "source": source,
                    }
                ]
            )
            for row_key in (worker_key, key):
                await conn.execute(
                    """INSERT INTO public.factory_state_kv
                         (cache_key, payload, fetched_at, expires_at, updated_at)
                       VALUES ($1, $2::jsonb, now(), now() + $3::int * interval '1 second', now())
                       ON CONFLICT (cache_key) DO UPDATE SET
                         payload=EXCLUDED.payload, fetched_at=EXCLUDED.fetched_at,
                         expires_at=EXCLUDED.expires_at, updated_at=now(),
                         refresh_owner=NULL, refresh_until=NULL""",
                    row_key,
                    payload,
                    ttl,
                )
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source, event_type, subject_type, subject_number, detail)
                   VALUES ('github-actions', 'worker_assigned', 'worker', $1,
                     jsonb_build_object('claim_id', $2::text, 'worker', $3::text,
                                        'targets', $4::jsonb, 'slot', $5::text,
                                        'run_id', $6::text, 'source', $7::text,
                                        'repo', $8::text))""",
                int(worker),
                token,
                worker,
                json.dumps([number]),
                slot,
                os.environ.get("GITHUB_RUN_ID", ""),
                source,
                repo,
            )
        return {"status": "reserved", "claim_id": token, "slot": slot, "key": key}
    finally:
        await conn.close()


def acquire_slot(
    repo: str,
    worker: str,
    number: int,
    slots: Sequence[str],
    ttl: int = FIXED_LEASE_TTL_SECONDS,
    *,
    distinct: str = "provider",
) -> dict[str, Any]:
    """Lease the first free slot in ``slots`` on one target for one worker.

    Returns ``{"status": "reserved", "claim_id", "slot", "key"}`` or
    ``{"status": "conflict", "reason"}``. Without Neon there is no shared
    ledger to fence several holders, so racing is refused (``ledger_disabled``).
    """
    _validate(worker, number, slots, ttl, distinct)
    if not enabled():
        return {"status": "conflict", "reason": "ledger_disabled"}
    return asyncio.run(_acquire_slot(repo, worker, number, list(slots), ttl, distinct))


def slot_holders(repo: str, number: int) -> list[dict[str, Any]]:
    """Every slot row on one target, active or expired."""
    if not enabled():
        return []

    async def _read() -> list[dict[str, Any]]:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            rows = await conn.fetch(
                """SELECT cache_key, payload, expires_at > now() AS active
                   FROM public.factory_state_kv WHERE cache_key LIKE $1
                   ORDER BY cache_key""",
                prefix_pattern(slot_prefix(repo, number)),
            )
        finally:
            await conn.close()
        return [
            {
                "slot": str(row["cache_key"]).removeprefix(slot_prefix(repo, number)),
                "claim_id": str(_first(row["payload"]).get("claim_id") or ""),
                "worker": str(_first(row["payload"]).get("worker") or ""),
                "source": str(_first(row["payload"]).get("source") or ""),
                "active": bool(row["active"]),
            }
            for row in rows
        ]

    return asyncio.run(_read())
