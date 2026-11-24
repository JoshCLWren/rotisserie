"""Short controller operation leases on one target, and lease listing for operators."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn
from factory_state_ledger_core import LEASE_PREFIX, WORKER_RE, _target_key, enabled, prefix_pattern


OPERATION_HOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
# Controller operations (a branch sync) hold a target for seconds, not hours.
OPERATION_LEASE_TTL_SECONDS = 300


def _operation_request(holder: str, number: int, ttl: int) -> None:
    if not OPERATION_HOLDER_RE.fullmatch(holder) or WORKER_RE.fullmatch(holder):
        # A bare number would read as a worker ID in lease payloads and events.
        raise ValueError("invalid controller operation holder")
    if number <= 0:
        raise ValueError("invalid controller operation target")
    if not 30 <= ttl <= 3600:
        raise ValueError("invalid controller operation lease TTL")


async def _acquire_operation(repo: str, holder: str, number: int, ttl: int) -> str | None:
    token = str(uuid.uuid4())
    key = _target_key(repo, number)
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        async with conn.transaction():
            # The same repository lock worker claims take: a worker and an
            # operation can never both see the target free.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            busy = await conn.fetchval(
                "SELECT 1 FROM public.factory_state_kv WHERE cache_key=$1 AND (expires_at > now() OR payload->0->>'external'='true')",
                key,
            )
            if busy:
                return None
            payload = json.dumps([{
                "claim_id": token, "operation": holder, "targets": [number],
            }])
            # Only an expired row can exist here, and overwriting it is what a
            # worker claim does too; an active claim was refused above.
            await conn.execute(
                """INSERT INTO public.factory_state_kv
                     (cache_key, payload, fetched_at, expires_at, updated_at)
                   VALUES ($1, $2::jsonb, now(), now() + $3::int * interval '1 second', now())
                   ON CONFLICT (cache_key) DO UPDATE SET
                     payload=EXCLUDED.payload, fetched_at=EXCLUDED.fetched_at,
                     expires_at=EXCLUDED.expires_at, updated_at=now(),
                     refresh_owner=NULL, refresh_until=NULL""",
                key, payload, ttl,
            )
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source, event_type, subject_type, subject_number, detail)
                   VALUES ('github-actions', 'operation_lease_acquired', 'target', $1,
                     jsonb_build_object('claim_id', $2::text, 'operation', $3::text,
                                        'run_id', $4::text, 'repo', $5::text))""",
                number, token, holder, os.environ.get("GITHUB_RUN_ID", ""), repo,
            )
        return token
    finally:
        await conn.close()


def acquire_operation_lease(
    repo: str, holder: str, number: int, ttl: int = OPERATION_LEASE_TTL_SECONDS
) -> str | None:
    """Hold one target exclusively for a short controller operation.

    Returns the claim UUID, ``None`` when a worker or another operation holds
    the target, or ``''`` when Neon is not configured. No worker capacity is
    reserved: the operation is the controller's, not a model's.
    """
    _operation_request(holder, number, ttl)
    if not enabled():
        if os.environ.get("FACTORY_NEON_LEASE_REQUIRED") == "true":
            raise RuntimeError("trusted factory controller requires the Neon lease connection")
        return ""
    return asyncio.run(_acquire_operation(repo, holder, number, ttl))


async def _release_operation(repo: str, number: int, token: str, reason: str) -> bool:
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            removed = await conn.fetchval(
                """WITH gone AS (
                     DELETE FROM public.factory_state_kv
                     WHERE cache_key=$1 AND payload->0->>'claim_id'=$2
                       AND payload->0 ? 'operation'
                     RETURNING 1)
                   SELECT count(*) FROM gone""",
                _target_key(repo, number), token,
            )
            if not removed:
                return False
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source, event_type, subject_type, subject_number, detail)
                   VALUES ('github-actions', 'operation_lease_released', 'target', $1,
                     jsonb_build_object('claim_id', $2::text, 'reason', $3::text,
                                        'repo', $4::text))""",
                number, token, reason, repo,
            )
        return True
    finally:
        await conn.close()


def release_operation_lease(repo: str, number: int, token: str, reason: str) -> bool:
    """Release only the operation claim this token holds; a worker claim is untouchable."""
    if not enabled() or not token:
        return False
    return asyncio.run(_release_operation(repo, number, token, reason))


def _lease_row(key: str, payload: Any, expires_at: Any, active: bool) -> dict[str, Any]:
    value = json.loads(payload) if isinstance(payload, str) else payload
    first = value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}
    return {
        "key": key,
        "claim_id": str(first.get("claim_id") or ""),
        "worker": str(first.get("worker") or ""),
        "operation": str(first.get("operation") or ""),
        "wip": first.get("wip") is True,
        "targets": [int(n) for n in first.get("targets") or [] if str(n).isdecimal()],
        "expires_at": expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at),
        "active": bool(active),
    }


def target_lease(repo: str, number: int) -> dict[str, Any] | None:
    """The active claim on one target, or ``None``. Raises when Neon is not configured."""
    if not enabled():
        raise RuntimeError("Neon is not configured")

    async def _read() -> dict[str, Any] | None:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            row = await conn.fetchrow(
                """SELECT cache_key, payload, expires_at FROM public.factory_state_kv
                   WHERE cache_key=$1 AND expires_at > now()""",
                _target_key(repo, number),
            )
            return None if row is None else _lease_row(
                row["cache_key"], row["payload"], row["expires_at"], True
            )
        finally:
            await conn.close()

    return asyncio.run(_read())


def list_leases(repo: str) -> list[dict[str, Any]]:
    """Every worker and target lease row for one repository, active or expired."""
    if not enabled():
        raise RuntimeError("Neon is not configured")

    async def _read() -> list[dict[str, Any]]:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            rows = await conn.fetch(
                """SELECT cache_key, payload, expires_at, expires_at > now() AS active
                   FROM public.factory_state_kv WHERE cache_key LIKE $1
                   ORDER BY cache_key""",
                prefix_pattern(f"{LEASE_PREFIX}{repo}:"),
            )
            return [
                _lease_row(row["cache_key"], row["payload"], row["expires_at"], row["active"])
                for row in rows
            ]
        finally:
            await conn.close()

    return asyncio.run(_read())


def release_expired_leases(repo: str) -> int:
    """Delete lease rows that expired over an hour ago. An active claim is never touched.

    Expired rows already block nothing (every claim checks ``expires_at``), so
    this only clears what an operator reads; it cannot steal a lease. The hour
    of grace leaves a just-expired worker's own token-bound release intact.
    """
    if not enabled():
        raise RuntimeError("Neon is not configured")

    async def _purge() -> int:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo
                )
                count = await conn.fetchval(
                    """WITH gone AS (
                         DELETE FROM public.factory_state_kv
                         WHERE cache_key LIKE $1
                           AND expires_at <= now() - interval '1 hour'
                           AND COALESCE(payload->0->>'external', 'false') != 'true'
                         RETURNING 1)
                       SELECT count(*) FROM gone""",
                    prefix_pattern(f"{LEASE_PREFIX}{repo}:"),
                )
            return int(count or 0)
        finally:
            await conn.close()

    return asyncio.run(_purge())
