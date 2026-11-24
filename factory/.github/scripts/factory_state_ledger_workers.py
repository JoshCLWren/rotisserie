"""Transactional Neon worker leases: acquire, release, renew and inspect worker claims."""
from __future__ import annotations

import asyncio
import json
import os
import uuid

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn
from factory_work_policy import FIXED_LEASE_TTL_SECONDS
from factory_state_ledger_core import LEASE_PREFIX, WORKER_RE, _keys, enabled


async def _acquire(
    repo: str, worker: str, numbers: list[int], ttl: int, external_wip_seen: int | None = None,
) -> str | None:

    token = str(uuid.uuid4())
    keys = _keys(repo, worker, numbers)
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        async with conn.transaction():
            # A repository-level transaction lock makes worker and *all* linked
            # issue/PR target rows one indivisible claim across Actions jobs.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            busy = await conn.fetch(
                """SELECT cache_key FROM public.factory_state_kv
                   WHERE cache_key = ANY($1::text[]) AND (expires_at > now() OR payload->0->>'external'='true')""",
                keys,
            )
            if busy:
                return None
            if external_wip_seen is not None:
                # The caller chose new intake against this many outside-agent
                # WIP claims. If one appeared since, its slot is already taken:
                # back off rather than start work past the shared WIP limit.
                current = await conn.fetchval(
                    """SELECT count(*) FROM public.factory_state_kv
                       WHERE starts_with(cache_key, $1) AND payload->0->>'wip' = 'true'""",
                    f"{LEASE_PREFIX}{repo}:external:",
                )
                if int(current or 0) > external_wip_seen:
                    return None
            from factory_state_operations import _roster, reserve_capacity
            if not await reserve_capacity(conn, repo, worker, token, ttl):
                return None
            payload = json.dumps([{
                "claim_id": token, "worker": worker, "targets": sorted(set(numbers)),
            }])
            for key in keys:
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
                   VALUES ('github-actions', 'worker_assigned', 'worker', $1,
                     jsonb_build_object('claim_id', $2::text, 'worker', $3::text,
                                        'targets', $4::jsonb, 'run_id', $5::text,
                                        'source', $6::text, 'repo', $7::text))""",
                int(worker), token, worker, json.dumps(sorted(set(numbers))),
                os.environ.get("GITHUB_RUN_ID", ""),
                _roster()[worker][0], repo,
            )
        return token
    finally:
        await conn.close()


def acquire_lease(
    repo: str, worker: str, numbers: list[int], ttl: int = FIXED_LEASE_TTL_SECONDS,
    *, external_wip_seen: int | None = None,
) -> str | None:
    """Return claim UUID on success, None on conflict, '' without Neon.

    ``external_wip_seen`` (new issue intake) is the outside-agent WIP count the
    caller planned against; a larger count under the lock is a conflict.

    In trusted Actions with a configured DSN, connection failures are fatal:
    silently falling back after a failed database claim would split ownership.
    """
    if not WORKER_RE.fullmatch(worker) or not numbers or any(n <= 0 for n in numbers):
        raise ValueError("invalid factory lease target or worker")
    if not 60 <= ttl <= 86400:
        raise ValueError("invalid factory lease TTL")
    if not enabled():
        if os.environ.get("FACTORY_NEON_LEASE_REQUIRED") == "true":
            raise RuntimeError("trusted factory dispatcher requires Neon transaction lease connection")
        return ""
    return asyncio.run(_acquire(repo, worker, numbers, ttl, external_wip_seen))


async def _release(repo: str, worker: str, token: str, reason: str) -> bool:

    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            worker_key = _keys(repo, worker, [])[0]
            row = await conn.fetchrow(
                """SELECT payload FROM public.factory_state_kv
                   WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                worker_key, token,
            )
            if row is None:
                return False
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            numbers = payload[0]["targets"]
            keys = _keys(repo, worker, numbers)
            await conn.execute(
                """DELETE FROM public.factory_state_kv
                   WHERE (cache_key=ANY($1::text[]) OR starts_with(cache_key, $3))
                     AND payload->0->>'claim_id'=$2""",
                keys, token, f"{LEASE_PREFIX}{repo}:slot:",
            )
            from factory_state_operations import release_capacity
            await release_capacity(conn, token)
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source, event_type, subject_type, subject_number, detail)
                   VALUES ('github-actions', 'worker_released', 'worker', $1,
                     jsonb_build_object('claim_id',$2::text,'worker',$3::text,
                                        'reason',$4::text,'targets',$5::jsonb,'repo',$6::text))""",
                int(worker), token, worker, reason, json.dumps(numbers), repo,
            )
        return True
    finally:
        await conn.close()


def release_lease(repo: str, worker: str, token: str, reason: str) -> bool:
    if not enabled() or not token:
        return False
    return asyncio.run(_release(repo, worker, token, reason))


def release_worker(repo: str, worker: str, reason: str) -> bool:
    """Release a verified worker's Neon claim after GitHub labels are released."""
    if not enabled():
        return False

    async def _lookup() -> str | None:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            row = await conn.fetchrow(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1",
                _keys(repo, worker, [])[0],
            )
            if not row:
                return None
            value = row["payload"]
            if isinstance(value, str):
                value = json.loads(value)
            return str(value[0]["claim_id"])
        finally:
            await conn.close()

    token = asyncio.run(_lookup())
    return release_lease(repo, worker, token, reason) if token else False


async def _renew_verified(repo: str, workers: list[str]) -> int:
    """Extend existing, unexpired reservations for verified running workers."""

    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=12)
    renewed = 0
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                LEASE_PREFIX + repo,
            )
            for worker in sorted(set(workers)):
                worker_key = _keys(repo, worker, [])[0]
                raw = await conn.fetchval(
                    """SELECT payload FROM public.factory_state_kv
                       WHERE cache_key=$1 AND expires_at>now()""",
                    worker_key,
                )
                if raw is None:
                    continue
                payload = json.loads(raw) if isinstance(raw, str) else raw
                token = payload[0]["claim_id"]
                await conn.execute(
                    """UPDATE public.factory_provider_reservation
                       SET expires_at=greatest(expires_at,now()+$2::int * interval '1 second')
                       WHERE claim_id=$1::uuid""", token, FIXED_LEASE_TTL_SECONDS)
                await conn.execute(
                    """UPDATE public.factory_state_kv
                       SET expires_at=greatest(expires_at,now()+$4::int * interval '1 second'),
                           updated_at=now()
                       WHERE expires_at>now()
                         AND payload->0->>'claim_id'=$1
                         AND (cache_key=$2 OR cache_key LIKE $3 OR cache_key LIKE $5)""",
                    token, worker_key, f"{LEASE_PREFIX}{repo}:target:%", FIXED_LEASE_TTL_SECONDS,
                    f"{LEASE_PREFIX}{repo}:slot:%",
                )
                renewed += 1
        return renewed
    finally:
        await conn.close()


def renew_verified_workers(repo: str, workers: list[str]) -> int:
    """Renew only workers verified alive by the trusted GitHub Actions controller.

    Args:
        repo: Repository whose worker claims are being extended.
        workers: Numeric worker IDs observed in active GitHub Actions runs.

    Returns:
        Number of unexpired worker claim groups extended.
    """
    if not enabled() or not workers:
        return 0
    if any(not WORKER_RE.fullmatch(worker) for worker in workers):
        raise ValueError("invalid verified worker ID")
    return asyncio.run(_renew_verified(repo, workers))


def worker_claim(repo: str, worker: str, *, active_only: bool = False) -> tuple[str, list[int]] | None:
    """Capture the existing claim before a controller mutates ownership labels.

    Args:
        repo: GitHub repository.
        worker: Numeric factory worker identifier.

    Returns:
        Existing claim ID and target numbers, or None if unavailable.

    A token-bound release cannot remove a claim acquired after this snapshot.
    """
    if not enabled():
        return None

    async def _read() -> tuple[str, list[int]] | None:

        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            raw = await conn.fetchval(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1 AND (NOT $2::boolean OR expires_at>now())",
                _keys(repo, worker, [])[0], active_only,
            )
            if raw is None:
                return None
            payload = json.loads(raw) if isinstance(raw, str) else raw
            return str(payload[0]["claim_id"]), [
                int(number) for number in payload[0]["targets"]
            ]
        finally:
            await conn.close()

    return asyncio.run(_read())
