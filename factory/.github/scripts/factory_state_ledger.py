"""Transactional Neon worker leases and lifecycle events for trusted controllers.

Uses the already provisioned factory_state database. No secrets reach model-runner
jobs: only trusted dispatcher, completion collector, and runtime observer call this.
GitHub still authorizes issue labels, exact-head reviews, checks, and merges.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn
from factory_work_policy import FIXED_LEASE_TTL_SECONDS
from factory_adopter import repository

WORKER_RE = re.compile(r"^[0-9]+$")
LIFECYCLE_STATUSES = {"started": "worker_started", "finished": "worker_outcome_signal",
                      "failure": "worker_outcome_signal"}
LEASE_PREFIX = "factory-lease-v1:"


def enabled() -> bool:
    return bool(os.environ.get("FACTORY_STATE_DSN_FILE"))


def _keys(repo: str, worker: str, numbers: list[int]) -> list[str]:
    return [f"{LEASE_PREFIX}{repo}:worker:{worker}"] + [
        f"{LEASE_PREFIX}{repo}:target:{number}" for number in sorted(set(numbers))
    ]


async def _acquire(repo: str, worker: str, numbers: list[int], ttl: int) -> str | None:

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


def acquire_lease(repo: str, worker: str, numbers: list[int], ttl: int = FIXED_LEASE_TTL_SECONDS) -> str | None:
    """Return claim UUID on success, None on conflict, '' without Neon.

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
    return asyncio.run(_acquire(repo, worker, numbers, ttl))


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
                   WHERE cache_key=ANY($1::text[])
                     AND payload->0->>'claim_id'=$2""",
                keys, token,
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


OPERATION_HOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
# Controller operations (a branch sync) hold a target for seconds, not hours.
OPERATION_LEASE_TTL_SECONDS = 300


def _target_key(repo: str, number: int) -> str:
    return f"{LEASE_PREFIX}{repo}:target:{number}"


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
                   FROM public.factory_state_kv WHERE starts_with(cache_key, $1)
                   ORDER BY cache_key""",
                f"{LEASE_PREFIX}{repo}:",
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
                         WHERE starts_with(cache_key, $1)
                           AND expires_at <= now() - interval '1 hour'
                           AND COALESCE(payload->0->>'external', 'false') != 'true'
                         RETURNING 1)
                       SELECT count(*) FROM gone""",
                    f"{LEASE_PREFIX}{repo}:",
                )
            return int(count or 0)
        finally:
            await conn.close()

    return asyncio.run(_purge())


async def _events(repo: str, entries: list[dict[str, Any]]) -> int:

    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    inserted = 0
    # Large failover sessions can contain hundreds of attempts. Batch their
    # immutable observations instead of making two Neon round trips per model.
    model_attempts = [entry for entry in entries if entry["event_type"] == "model_attempt"]
    lifecycle = [entry for entry in entries if entry["event_type"] != "model_attempt"]
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            for entry in lifecycle:
                existing = await conn.fetchval(
                    """SELECT 1 FROM public.factory_state_events
                       WHERE source='github-actions'
                         AND detail->>'event_id'=$1 LIMIT 1""",
                    entry["event_id"],
                )
                if existing:
                    continue
                await conn.execute(
                    """INSERT INTO public.factory_state_events
                       (source,event_type,subject_type,subject_number,detail)
                       VALUES ('github-actions',$1,'worker',$2,$3::jsonb)""",
                    entry["event_type"], int(entry["worker"]), json.dumps({**entry, "repo": repo}),
                )
                inserted += 1
                # 'running' is an initial status marker, not a periodic
                # liveness check. A terminal success/failure never renews.
                if entry["event_type"] == "worker_started":
                    worker_key = _keys(repo, entry["worker"], [])[0]
                    row = await conn.fetchrow(
                        """SELECT payload FROM public.factory_state_kv
                           WHERE cache_key=$1 AND expires_at>now()""",
                        worker_key,
                    )
                    if row:
                        payload = row["payload"]
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        token = payload[0]["claim_id"]
                        await conn.execute(
                            """UPDATE public.factory_provider_reservation
                               SET expires_at=greatest(expires_at,now()+$2::int * interval '1 second')
                               WHERE claim_id=$1::uuid""", token, FIXED_LEASE_TTL_SECONDS,
                        )
                        await conn.execute(
                            """UPDATE public.factory_state_kv SET
                                 expires_at=greatest(expires_at,now()+$3::int * interval '1 second'),
                                 updated_at=now()
                               WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                            worker_key, token, FIXED_LEASE_TTL_SECONDS,
                        )
                        await conn.execute(
                            """UPDATE public.factory_state_kv SET
                                 expires_at=greatest(expires_at,now()+$3::int * interval '1 second'),
                                 updated_at=now()
                               WHERE cache_key LIKE $1
                                 AND payload->0->>'claim_id'=$2""",
                            f"{LEASE_PREFIX}{repo}:target:%", token, FIXED_LEASE_TTL_SECONDS,
                        )
            if model_attempts:
                records = [{**entry, "repo": repo} for entry in model_attempts]
                event_ids = [entry["event_id"] for entry in records]
                added = await conn.fetch(
                    """WITH incoming AS (
                           SELECT value AS detail FROM jsonb_array_elements($1::jsonb)
                       ), already AS (
                           SELECT detail->>'event_id' AS event_id
                             FROM public.factory_state_events
                            WHERE source='github-actions'
                              AND detail->>'event_id' = ANY($2::text[])
                       )
                       INSERT INTO public.factory_state_events
                           (source,event_type,subject_type,subject_number,detail)
                       SELECT 'github-actions','model_attempt','worker',
                              (incoming.detail->>'worker')::bigint, incoming.detail
                         FROM incoming
                        WHERE NOT EXISTS (
                            SELECT 1 FROM already
                             WHERE already.event_id=incoming.detail->>'event_id'
                        )
                       RETURNING id""",
                    json.dumps(records), event_ids,
                )
                inserted += len(added)
        return inserted
    finally:
        await conn.close()



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
                         AND (cache_key=$2 OR cache_key LIKE $3)""",
                    token, worker_key, f"{LEASE_PREFIX}{repo}:target:%", FIXED_LEASE_TTL_SECONDS,
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

def recorded_runtime_runs(repo: str, run_ids: list[str]) -> set[str]:
    """Return the Entry run IDs that already have a terminal runtime outcome.

    The deferred outcome reconciler uses this to avoid re-projecting a run
    (model-health streaks are not idempotent per run).
    """
    if not enabled() or not run_ids:
        return set()
    if any(not run_id.isdecimal() for run_id in run_ids):
        raise ValueError("invalid run ID")

    async def _read() -> set[str]:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            rows = await conn.fetch(
                """SELECT detail->>'run_id' AS run_id FROM public.factory_state_events
                   WHERE source='github-actions' AND detail->>'repo'=$1
                     AND detail->>'event_id' = ANY($2::text[])""",
                repo, [f"runtime:{run_id}" for run_id in run_ids],
            )
            return {str(row["run_id"]) for row in rows}
        finally:
            await conn.close()

    return asyncio.run(_read())


def record_events(repo: str, entries: list[dict[str, Any]]) -> int:
    if not enabled() or not entries:
        return 0
    return asyncio.run(_events(repo, entries))


def record_lifecycle(repo: str, run_id: str, worker: str, status: str) -> int:
    """Persist a trusted Actions lifecycle observation for one worker run.

    Only trusted run-workflow jobs (assignment validation and lease release)
    call this, using the dispatch-input worker and the Actions job result. It
    replaces the retired issue #1093 heartbeat comments: no GitHub comment is
    read or written. Repeated calls are idempotent per run and status.

    Args:
        repo: GitHub repository.
        run_id: Entry workflow run ID.
        worker: Numeric factory worker identifier.
        status: ``started``, ``finished`` or ``failure``.

    Returns:
        Number of newly inserted lifecycle events.
    """
    if not WORKER_RE.fullmatch(worker) or not run_id.isdecimal():
        raise ValueError("invalid trusted lifecycle identity")
    if status not in LIFECYCLE_STATUSES:
        raise ValueError("invalid trusted lifecycle status")
    return record_events(repo, [{
        "event_type": LIFECYCLE_STATUSES[status],
        "event_id": f"lifecycle:{run_id}:{status}",
        "worker": worker,
        "run_id": run_id,
        "status": "running" if status == "started" else status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }])


def refine_trusted_dynamic_models(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Persist *all* exact free-model smoke failures, even after a later success.

    Only timestamped output of the executed Actions smoke step qualifies.
    The model runner cannot create privileged Neon records itself.
    """
    if (outcome.get("source") != "opencode-free"
            or outcome.get("persisted_work") not in (True, False)):
        return outcome
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
    failures: set[str] = set()
    successful = ""
    for step in re.split(r"##\[group\]Run ", clean):
        # The smoke command is a bounded pre-work invocation. Do not inspect
        # arbitrary later model-agent output for retirement evidence.
        if "Smoke candidate: opencode/" not in step:
            continue
        matches = list(re.finditer(
            r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Smoke candidate: opencode/([\w./:-]+)\s*$",
            step,
        ))
        for i, match in enumerate(matches):
            model = match.group(1)
            fragment = step[match.end(): matches[i+1].start() if i+1 < len(matches) else len(step)]
            if re.search(
                rf"\bModel (?:opencode/)?{re.escape(model)} has been deprecated\b",
                fragment, re.IGNORECASE,
            ) or re.search(
                rf"\bModel unavailable: opencode/{re.escape(model)}\b",
                fragment, re.IGNORECASE,
            ):
                failures.add(model)
        success = re.findall(
            r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Dynamic free-model smoke succeeded: opencode/([\w./:-]+)\s*$",
            step,
        )
        if success:
            successful = success[-1]
    if not failures and not successful:
        return outcome
    refined = dict(outcome)
    if failures:
        refined["model_failures"] = sorted(failures)[:5]
    if successful and outcome.get("exact_model_proven") is True:
        refined["model"] = successful
    return refined

def refine_trusted_failover_models(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Attest exact failed pairs, account scope and final provider transitions.

    Only the completed trusted Actions job log and verified worker marker
    qualify for durable health. A prior candidate can never classify the
    terminal successful model or overwrite its delivery state.
    """
    from factory_provider_candidates import ADAPTERS

    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
    worker = str(outcome.get("worker") or "")
    pattern = re.compile(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z "
        r"\[factory:(\d+)\]\[source:([^\]\n]+)\]\[model:[^\]\n]+\] "
        r"FACTORY_TRUSTED_FAILOVER: source=(\S+) model=([\w./:-]{1,180}) "
        r"outcome=([a-z_]+)(?: scope=(account|model))?\s*$"
    )
    accepted = {
        "provider_failure", "provider_throttle", "provider_quota",
        "model_unavailable", "model_retired_410", "model_policy_violation",
    }
    failures = list(outcome.get("model_failures") or [])
    seen = {
        (str(item.get("source") or outcome.get("source") or ""), item.get("model"))
        for item in failures if isinstance(item, dict)
    }
    for match in pattern.finditer(clean):
        log_worker, log_source, source, model, classification, scope = match.groups()
        if (log_worker != worker or log_source != source
                or source not in ADAPTERS or classification not in accepted
                or (source, model) in seen):
            continue
        if scope == "account" and classification not in {"provider_quota", "provider_failure"}:
            continue
        failures.append({
            "source": source, "model": model, "outcome": classification,
            "scope": scope or "model",
        })
        seen.add((source, model))

    # The model cannot choose an unregistered source merely by mentioning one
    # in its text. The canonical runtime source transition must appear in the
    # trusted, timestamped wrapper log before a final-source override.
    transitions = re.findall(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z "
        r"\[factory:(\d+)\]\[source:([^\]\n]+)\]\[model:[^\]\n]+\] "
        r"FACTORY_FLEET_TRANSITION: source=([^\s]+) model=([^\s]+) ",
        clean,
    )
    final_source = str(outcome.get("source") or "")
    final_model = str(outcome.get("model") or "")
    trusted_switch = any(
        w == worker and log_source == source == final_source
        and model == final_model and source in ADAPTERS
        for w, log_source, source, model in transitions
    )
    result = dict(outcome)
    if failures:
        result["model_failures"] = failures
    if trusted_switch:
        result["trusted_source_switch"] = True
    return result



def refine_trusted_smoke_timeout(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Attribute a 180-second pinned-model smoke timeout to the provider.

    Unlike a generic exit 124, this requires the actual Actions smoke step to
    end with exit 124. The already-classified trusted outcome must be unknown,
    pre-proof, and nonproductive. No model fault is inferred from shell source
    alone or from the runner's subsequent cleanup.
    """
    worker = re.escape(str(outcome.get("worker") or ""))
    # A wrapper-enforced execution deadline must not cool down the provider.
    # Other evidence can be present merely because the agent inspected files.
    if (worker and outcome.get("persisted_work") is not True
            and outcome.get("outcome_class") not in {
                "provider_quota", "model_unavailable", "model_retired_410",
                "model_policy_violation",
            } and re.search(
                rf"(?m)^\d{{4}}-\d{{2}}-\d{{2}}T\S+Z \[factory:{worker}\].*"
                r"agent exit status 124 for (?:issue|pr) #\d+\b",
                job_log,
            )):
        return {
            **outcome,
            "outcome": "AGENT DEADLINE",
            "outcome_class": "agent_timeout",
            "detail": "trusted worker hit its local execution deadline; no provider failure inferred",
        }
    if (
        outcome.get("outcome_class") not in ({"unknown_failure", "model_unavailable"} if outcome.get("source") == "nvidia" else {"unknown_failure"})
        or outcome.get("exact_model_proven") is True
        or outcome.get("persisted_work") is True
        or not outcome.get("model") or not outcome.get("source")
    ):
        return outcome

    # GitHub separates executed steps with a group header. Require the smoke
    # command and terminal timeout inside the same step, not just anywhere
    # in the complete worker log.
    steps = re.split(r"##\[group\]Run ", job_log)
    for step in steps:
        if (
            "timeout --signal=TERM --kill-after=10s 180s" in step
            and re.search(r"##\[error\]Process completed with exit code 124\b", step)
        ):
            return {
                **outcome,
                "outcome": "PROVIDER FAILURE",
                "outcome_class": "provider_failure",
                "detail": "pinned OpenCode model exceeded its 180-second smoke deadline",
            }
    # A dynamic runner can rotate away from its originally selected model.
    # Recover the *executed* final smoke selector only from timestamped runner
    # output in the failing Actions smoke step, never echoed shell source.
    if outcome.get("source") == "opencode-free":
        clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
        for step in re.split(r"##\[group\]Run ", clean):
            if not re.search(r"##\[error\]Process completed with exit code 1\b", step):
                continue
            candidates = re.findall(
                r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Smoke candidate: opencode/([\w./:-]+)\s*$",
                step,
            )
            if not candidates:
                continue
            attempted = candidates[-1]
            if re.search(
                rf"\bModel (?:opencode/)?{re.escape(attempted)} has been deprecated\b",
                step, re.IGNORECASE,
            ) or re.search(
                rf"\bModel unavailable: opencode/{re.escape(attempted)}\b",
                step, re.IGNORECASE,
            ):
                return {
                    **outcome,
                    "model": attempted,
                    "outcome": "MODEL MISSING",
                    "outcome_class": "model_unavailable",
                    "detail": "actual final OpenCode smoke selector is unavailable or deprecated",
                }
    # A trusted NVIDIA 410 has an irreversible provider meaning. Preserve its
    # retirement in Neon, rather than retrying a known-gone model after 6h.
    if (outcome.get("source") == "nvidia"
            and outcome.get("outcome_class") in {"unknown_failure", "model_unavailable"}
            and outcome.get("model")):
        model = re.escape(str(outcome["model"]))
        if re.search(
            rf"(?m)^\d{{4}}-\d{{2}}-\d{{2}}T\S+Z NVIDIA permanently retired model: {model}\s*$",
            re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log),
        ):
            return {
                **outcome,
                "outcome": "MODEL RETIRED",
                "outcome_class": "model_retired_410",
                "detail": "trusted NVIDIA probe returned HTTP 410 for the exact pinned model",
            }
    return outcome


def record_runtime_outcome(
    repo: str, run_id: str, outcome: dict[str, Any], job_log: str | None = None,
) -> int:
    worker = str(outcome.get("worker") or "")
    if not WORKER_RE.fullmatch(worker) or not run_id.isdecimal():
        raise ValueError("invalid trusted runtime outcome identity")
    status = str(outcome.get("outcome_class") or "unknown_failure")
    # Execution can time out despite a verified code push and successful PR handoff.
    delivered = (outcome.get("delivery_outcome") == "persisted"
                 and outcome.get("persisted_work") is True
                 and str(outcome.get("delivery_pr") or "").isdecimal())
    event_type = ("worker_completed" if status == "success" or delivered
                  else "worker_idle" if status == "no_work" else "worker_failed")
    entries = [{
        "event_type": event_type,
        "event_id": f"runtime:{run_id}",
        "worker": worker,
        "run_id": run_id,
        "source": outcome.get("source"),
        "model": outcome.get("model"),
        "exact_model_proven": outcome.get("exact_model_proven") is True,
        "outcome_class": status,
        "delivery_outcome": "persisted" if delivered else "none",
        "delivery_pr": outcome.get("delivery_pr") if delivered else None,
        "selected_kind": outcome.get("selected_kind"),
        "selected_number": outcome.get("selected_number"),
        "persisted_work": bool(outcome.get("persisted_work")),
        "model_failures": outcome.get("model_failures") or [],
        "trusted_source_switch": outcome.get("trusted_source_switch") is True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }]
    # The log is fetched by the trusted completed-job collector. Per-candidate
    # observations share the terminal event transaction and retain distinct IDs.
    if job_log is not None:
        from factory_model_attempt_events import model_attempt_events
        entries.extend(model_attempt_events(run_id, outcome, job_log))
    inserted = record_events(repo, entries)
    # Separate projection key is idempotent across both trusted outcome writers.
    from factory_state_operations import enabled as health_enabled, record_runtime
    if health_enabled():
        record_runtime(repo, int(run_id), outcome)
    return inserted


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["runtime-outcome", "lifecycle"])
    parser.add_argument("--file")
    parser.add_argument("--worker")
    parser.add_argument("--status", choices=sorted(LIFECYCLE_STATUSES))
    parser.add_argument("--log", help="Trusted completed factory job log, for exact smoke timeout attribution")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()
    args.repo = args.repo or repository()
    if args.command == "lifecycle":
        if not args.worker or not args.status:
            parser.error("lifecycle requires --worker and --status")
        result = record_lifecycle(args.repo, args.run_id, args.worker, args.status)
        print(f"[factory-ledger] recorded {result} lifecycle event(s)")
        return 0
    if args.command == "runtime-outcome":
        if not args.file:
            parser.error("runtime-outcome requires --file")
        with open(args.file, encoding="utf-8") as stream:
            outcome = json.load(stream)
        if args.log:
            with open(args.log, encoding="utf-8", errors="replace") as stream:
                job_log = stream.read()
                outcome = refine_trusted_dynamic_models(outcome, job_log)
                outcome = refine_trusted_failover_models(outcome, job_log)
                outcome = refine_trusted_smoke_timeout(outcome, job_log)
        result = record_runtime_outcome(
            args.repo, args.run_id, outcome,
            job_log=job_log if args.log else None,
        )
        print(f"[factory-ledger] recorded {result} terminal worker event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# External agents share the same repository advisory lock and target keys as
# factory workers. They do NOT impersonate numeric worker slots or spend model
# provider reservations. An expired external claim is intentionally still a
# hold until its identity-fenced release or verified operator reconciliation.
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXTERNAL_ACTIONS = frozenset({
    "implement", "review", "review-findings", "ci-repair", "merge-conflict",
})
EXTERNAL_WIP_ACTIONS = frozenset({
    "implement", "review-findings", "ci-repair", "merge-conflict",
})


def _external_request(repo: str, actor: str, client: str, numbers: list[int]) -> list[int]:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not EXTERNAL_ID_RE.fullmatch(actor)
            or not EXTERNAL_ID_RE.fullmatch(client)):
        raise ValueError("invalid external actor, client or repository")
    if not numbers or len(numbers) > 2 or any(type(n) is not int or n < 1 for n in numbers):
        raise ValueError("invalid external lease targets")
    if len(set(numbers)) != len(numbers):
        raise ValueError("external lease targets must be distinct")
    return sorted(numbers)


def _external_key(repo: str, token: str) -> str:
    return f"{LEASE_PREFIX}{repo}:external:{token}"


async def _reserve_external(
    repo: str, actor: str, client: str, numbers: list[int], *,
    target: str, action: str, head: str | None, ttl: int,
    observed_wip: int, wip_limit: int, offer_at: str,
) -> dict[str, Any]:
    token = str(uuid.uuid4())
    keys = [_target_key(repo, n) for n in numbers]
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=12)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo
            )
            # Unlike the historic numeric-worker fallback, an expired but
            # unreconciled external row is NOT implicitly reusable.
            busy = await conn.fetch(
                "SELECT cache_key FROM public.factory_state_kv WHERE cache_key = ANY($1::text[])",
                keys,
            )
            if busy:
                return {
                    "status": "conflict", "reason": "existing_or_unreconciled_lease",
                    "targets": sorted(str(row["cache_key"]) for row in busy),
                    "retry": "refresh_frontier",
                }
            count = await conn.fetchval(
                """SELECT count(*) FROM public.factory_state_kv
                   WHERE starts_with(cache_key, $1)
                     AND payload->0->>'wip' = 'true'""",
                f"{LEASE_PREFIX}{repo}:external:",
            )
            if action == "implement" and observed_wip + int(count or 0) >= wip_limit:
                return {
                    "status": "conflict", "reason": "wip_capacity_changed",
                    "retry": "refresh_frontier",
                }
            claim = {
                "claim_id": token,
                "operation": f"external:{actor}/{client}",
                "external": True,
                "actor": actor,
                "client": client,
                "targets": numbers,
                "target": target,
                "action": action,
                "head": head,
                "wip": action in EXTERNAL_WIP_ACTIONS,
                "offer_at": offer_at,
            }
            for key in [*keys, _external_key(repo, token)]:
                await conn.execute(
                    """INSERT INTO public.factory_state_kv
                       (cache_key, payload, fetched_at, expires_at, updated_at)
                       VALUES ($1, $2::jsonb, now(),
                               now() + $3::int * interval '1 second', now())
                       ON CONFLICT (cache_key) DO NOTHING""",
                    key, json.dumps([claim]), ttl,
                )
                # CAS never overwrites a claim, including an expired one.
                matches = await conn.fetchval(
                    """SELECT count(*) FROM public.factory_state_kv
                       WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                    key, token,
                )
                if matches != 1:
                    raise RuntimeError("external lease CAS lost under repository lock")
            expires_at = await conn.fetchval(
                "SELECT expires_at FROM public.factory_state_kv WHERE cache_key=$1",
                _external_key(repo, token),
            )
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source,event_type,subject_type,subject_number,detail)
                   VALUES ('rotisserie-external','external_reserved','target',$1,
                           $2::jsonb)""",
                numbers[-1], json.dumps({**claim, "repo": repo}),
            )
        return {
            "status": "reserved", "claim_id": token, "actor": actor,
            "client": client, "target": target, "action": action,
            "head": head, "targets": numbers,
            "expires_at": expires_at.isoformat(),
        }
    finally:
        await conn.close()


def reserve_external(
    repo: str, actor: str, client: str, numbers: list[int], *,
    target: str, action: str, head: str | None, ttl: int = 1800,
    observed_wip: int, wip_limit: int, offer_at: str,
) -> dict[str, Any]:
    """Atomically fence one external claim using the factory's existing keys."""
    numbers = _external_request(repo, actor, client, numbers)
    if (action not in EXTERNAL_ACTIONS or not re.fullmatch(r"(issue|pr):[1-9][0-9]*", target)
            or (head is not None and not re.fullmatch(r"[0-9a-f]{40}", head))
            or not 300 <= ttl <= 3600 or not 1 <= wip_limit <= 10000
            or not 0 <= observed_wip <= 10000 or not offer_at):
        raise ValueError("invalid external reservation request")
    if not enabled():
        raise RuntimeError("trusted external claims require Neon")
    return asyncio.run(_reserve_external(
        repo, actor, client, numbers, target=target, action=action,
        head=head, ttl=ttl, observed_wip=observed_wip, wip_limit=wip_limit,
        offer_at=offer_at,
    ))


async def _finish_external(
    repo: str, actor: str, client: str, token: str, *,
    operation: str, evidence: str, ttl: int,
) -> dict[str, Any]:
    key = _external_key(repo, token)
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=12)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo
            )
            row = await conn.fetchrow(
                "SELECT payload, expires_at FROM public.factory_state_kv WHERE cache_key=$1",
                key,
            )
            if not row:
                return {"status": "conflict", "reason": "claim_missing_or_replaced"}
            payload = row["payload"]
            value = json.loads(payload) if isinstance(payload, str) else payload
            record = value[0] if isinstance(value, list) and value else {}
            if (record.get("claim_id") != token or record.get("actor") != actor
                    or record.get("client") != client or record.get("external") is not True):
                return {"status": "conflict", "reason": "stale_or_wrong_client"}
            numbers = record["targets"]
            keys = [_target_key(repo, n) for n in numbers]
            confirmed = await conn.fetch(
                """SELECT cache_key, payload->0->>'claim_id' AS id
                   FROM public.factory_state_kv WHERE cache_key=ANY($1::text[])""",
                keys,
            )
            if len(confirmed) != len(keys) or any(row["id"] != token for row in confirmed):
                return {"status": "conflict", "reason": "stale_target_fence"}
            if operation == "renew":
                if row["expires_at"] <= datetime.now(timezone.utc):
                    return {"status": "conflict", "reason": "lease_expired_liveness_unknown"}
                for target_key in [key, *keys]:
                    await conn.execute(
                        """UPDATE public.factory_state_kv
                           SET expires_at=now()+$3::int*interval '1 second',
                               updated_at=now()
                           WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                        target_key, token, ttl,
                    )
                expires_at = await conn.fetchval(
                    "SELECT expires_at FROM public.factory_state_kv WHERE cache_key=$1", key
                )
                event = "external_renewed"
            else:
                # Handoff is evidence, not approval: independent review, exact-head
                # CI, merge authorization and deployment remain controller gates.
                await conn.execute(
                    """DELETE FROM public.factory_state_kv
                       WHERE cache_key=ANY($1::text[])
                         AND payload->0->>'claim_id'=$2""",
                    [key, *keys], token,
                )
                expires_at = None
                event = "external_handoff" if operation == "handoff" else "external_released"
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source,event_type,subject_type,subject_number,detail)
                   VALUES ('rotisserie-external',$1,'target',$2,$3::jsonb)""",
                event, numbers[-1], json.dumps({
                    "repo": repo, "claim_id": token, "actor": actor,
                    "client": client, "target": record["target"],
                    "action": record["action"], "head": record.get("head"),
                    "evidence": evidence,
                }),
            )
        return {
            "status": "renewed" if operation == "renew" else "released",
            "claim_id": token,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "handoff_recorded": operation == "handoff",
        }
    finally:
        await conn.close()


def external_lease_action(
    repo: str, actor: str, client: str, claim_id: str, *,
    operation: str, evidence: str = "", ttl: int = 1800,
) -> dict[str, Any]:
    _external_request(repo, actor, client, [1])
    try:
        uuid.UUID(claim_id)
    except (ValueError, AttributeError):
        raise ValueError("invalid claim ID") from None
    if operation not in {"renew", "release", "handoff"} or not 300 <= ttl <= 3600:
        raise ValueError("invalid external lease operation")
    if operation == "handoff" and not evidence.strip():
        raise ValueError("handoff requires durable evidence")
    if len(evidence) > 2000 or any(c in evidence for c in "\r\n"):
        raise ValueError("invalid external evidence")
    if not enabled():
        raise RuntimeError("trusted external claims require Neon")
    return asyncio.run(_finish_external(
        repo, actor, client, claim_id, operation=operation, evidence=evidence, ttl=ttl
    ))


def external_wip_count(repo: str) -> int:
    """Count all external WIP claims, even expired/unreconciled ones, once each."""
    if not enabled():
        raise RuntimeError("Neon is not configured")

    async def _read() -> int:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            result = await conn.fetchval(
                """SELECT count(*) FROM public.factory_state_kv
                   WHERE starts_with(cache_key, $1)
                     AND payload->0->>'wip'='true'""",
                f"{LEASE_PREFIX}{repo}:external:",
            )
            return int(result or 0)
        finally:
            await conn.close()

    return asyncio.run(_read())


def external_code_authors(repo: str, numbers: list[int]) -> set[str]:
    """Return known external coding clients for the current issue/PR lineage.

    GitHub actor names are NOT model identities: several independent agents
    may operate using the repository owner's account. The durable client ID,
    not that shared actor, is the review-independence fence.
    """
    if not enabled():
        raise RuntimeError("Neon is not configured")
    if not numbers or any(type(n) is not int or n < 1 for n in numbers):
        raise ValueError("invalid author-provenance targets")

    async def _read() -> set[str]:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            targets = [f"issue:{n}" for n in numbers] + [f"pr:{n}" for n in numbers]
            rows = await conn.fetch(
                """SELECT DISTINCT detail->>'client' AS client
                   FROM public.factory_state_events
                   WHERE source='rotisserie-external'
                     AND event_type='external_reserved'
                     AND detail->>'repo'=$1
                     AND detail->>'target'=ANY($2::text[])
                     AND detail->>'action' IN
                         ('implement','review-findings','ci-repair','merge-conflict')""",
                repo, targets,
            )
            return {str(row["client"]) for row in rows if row["client"]}
        finally:
            await conn.close()

    return asyncio.run(_read())
