"""Worker claims, controller operation leases and lease listing on the libSQL store.

The same public functions as the retired Neon ledger (``factory_state_ledger``), so the
cutover changes the facade's imports, not its callers. The repository advisory
lock is gone: each acquisition is one batch whose anchor row is written only
when every key is free, the external work-in-progress count still matches and
the provider's daily cap has room, all checked inside that one statement.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import factory_turso_core as core
from factory_state_ledger_core import LEASE_PREFIX, WORKER_RE, _keys, _target_key
from factory_state_ledger_operations import OPERATION_LEASE_TTL_SECONDS, _operation_request
from factory_work_policy import FIXED_LEASE_TTL_SECONDS

ORIGIN = "github-actions"
DAILY_CAP_GUARD = (
    "(:cap IS NULL OR (SELECT count(*) FROM factory_state_events WHERE repo = :repo"
    " AND event_type = 'worker_assigned' AND source = :source"
    " AND created_at >= unixepoch('now', 'start of day')) < :cap)"
)
BUDGET_GUARD = (
    "(SELECT count(*) FROM factory_provider_reservation WHERE repo = :repo"
    " AND source = :source AND expires_at > unixepoch()) < coalesce((SELECT concurrency_limit"
    " FROM factory_provider_budget WHERE repo = :repo AND source = :source), 1000000000)"
    " AND (SELECT count(*) FROM factory_state_events WHERE repo = :repo"
    " AND event_type = 'worker_assigned' AND source = :source"
    " AND created_at >= unixepoch('now', 'start of day')) < coalesce((SELECT daily_limit"
    " FROM factory_provider_budget WHERE repo = :repo AND source = :source), 1000000000)"
)
EXTERNAL_WIP = (
    "(SELECT count(*) FROM factory_lease WHERE repo = :repo AND kind = 'external' AND wip = 1)"
)


def _unconfigured(message: str) -> None:
    if core.required():
        raise RuntimeError(message)


def daily_cap(worker: str, source: str) -> int | None:
    from factory_source_budget import PROTECTED_WORKERS, load_budgets

    return None if int(worker) in PROTECTED_WORKERS else load_budgets().get(source)


def claim_statements(
    repo: str,
    worker: str,
    numbers: list[int],
    ttl: int,
    token: str,
    source: str,
    *,
    external_wip_seen: int | None,
    cap: int | None,
    slot: tuple[str, int, bool] | None = None,
) -> list[core.Statement]:
    """The acquisition batch: anchor worker row, then targets, reservation and event."""
    worker_key = _keys(repo, worker, [])[0]
    targets = sorted(set(numbers))
    if slot is None:
        held = _keys(repo, worker, targets)
        extra_keys: list[tuple[str, int]] = [(_target_key(repo, n), n) for n in targets]
        slot_sql, slot_params = "1", {}
    else:
        from factory_state_ledger_slots import slot_key

        name, number, distinct = slot
        held = [worker_key, slot_key(repo, number, name)]
        extra_keys = [(slot_key(repo, number, name), number)]
        slot_sql = (
            "(:distinct = 0 OR NOT EXISTS (SELECT 1 FROM factory_lease WHERE repo = :repo"
            " AND kind = 'slot' AND number = :slot_number AND source = :source"
            " AND expires_at > unixepoch()))"
        )
        slot_params = {"distinct": int(distinct), "slot_number": number}
    free_sql, free_params = core.free_keys_guard(held)
    guard = (
        f"{free_sql} AND {DAILY_CAP_GUARD} AND {BUDGET_GUARD} AND {slot_sql}"
        f" AND (:wip_seen IS NULL OR {EXTERNAL_WIP} <= :wip_seen)",
        {
            **free_params,
            **slot_params,
            "cap": cap,
            "source": source,
            "wip_seen": external_wip_seen,
        },
    )
    operation = None if slot is None else f"slot:{slot[0]}"
    common = {
        "repo": repo,
        "token": token,
        "ttl": ttl,
        "worker": worker,
        "source": source,
        "targets": targets,
        "operation": operation,
        "slot": None if slot is None else slot[0],
    }
    statements = [core.lease_upsert(key=worker_key, kind="worker", guard=guard, **common)]
    mine = core.held_guard(worker_key, token)
    for key, number in extra_keys:
        kind = "target" if slot is None else "slot"
        statements.append(
            core.lease_upsert(key=key, kind=kind, guard=mine, number=number, **common)
        )
    statements.append(
        (
            "INSERT INTO factory_provider_reservation (claim_id, repo, worker, source, expires_at)"
            " SELECT :token, :repo, :worker, :source, unixepoch() + :ttl"
            f" WHERE {mine[0]}",
            {
                **mine[1],
                "token": token,
                "repo": repo,
                "worker": worker,
                "source": source,
                "ttl": ttl,
            },
        )
    )
    detail: dict[str, Any] = {
        "event_id": f"assigned:{token}",
        "claim_id": token,
        "worker": worker,
        "targets": targets,
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "source": source,
    }
    if slot is not None:
        detail["slot"] = slot[0]
    statements.append(
        core.event_insert(
            repo,
            ORIGIN,
            "worker_assigned",
            detail,
            subject_type="worker",
            subject_number=int(worker),
            guard=mine,
        )
    )
    return statements


def acquire_lease(
    repo: str,
    worker: str,
    numbers: list[int],
    ttl: int = FIXED_LEASE_TTL_SECONDS,
    *,
    external_wip_seen: int | None = None,
    explicit: bool = False,
) -> str | None:
    """Claim UUID on success, ``None`` on conflict or ineligibility, ``''`` without a store.

    The factory's own picks raise ``ClaimsPaused`` while store usage is
    critical. An operator's explicit target (``explicit``) is never paused by
    usage; an exhausted store still refuses it.
    """
    if not WORKER_RE.fullmatch(worker) or not numbers or any(n <= 0 for n in numbers):
        raise ValueError("invalid factory lease target or worker")
    if not 60 <= ttl <= 86400:
        raise ValueError("invalid factory lease TTL")
    if not core.enabled():
        _unconfigured("trusted factory dispatcher requires the factory store")
        return ""
    if not explicit:
        core.refuse_if_paused()
    from factory_turso_operations import eligible_source

    db = core.store()
    source = eligible_source(db, repo, worker)
    if source is None:
        return None
    token = str(uuid.uuid4())
    statements = claim_statements(
        repo,
        worker,
        numbers,
        ttl,
        token,
        source,
        external_wip_seen=external_wip_seen,
        cap=daily_cap(worker, source),
    )
    worker_key = _keys(repo, worker, [])[0]
    core.reconciled_batch(db, statements, lambda: core.claim_of(db, worker_key) == token)
    return token if core.claim_of(db, worker_key) == token else None


def release_lease(repo: str, worker: str, token: str, reason: str) -> bool:
    """Release every row and the reservation of exactly this claim; a newer claim is untouched."""
    if not core.enabled() or not token:
        return False
    db = core.store()
    worker_key = _keys(repo, worker, [])[0]
    event_id = f"released:{token}"
    statements = [
        (
            "INSERT OR IGNORE INTO factory_state_events (repo, origin, event_type, subject_type,"
            " subject_number, event_id, worker, claim_id, created_at, detail)"
            " SELECT :repo, :origin, 'worker_released', 'worker', :number, :event_id, :worker,"
            " :token, unixepoch(), json_object('claim_id', :token, 'worker', :worker,"
            " 'reason', :reason, 'targets', json(targets), 'repo', :repo, 'event_id', :event_id)"
            " FROM factory_lease WHERE lease_key = :key AND claim_id = :token RETURNING id",
            {
                "repo": repo,
                "origin": ORIGIN,
                "number": int(worker),
                "event_id": event_id,
                "worker": worker,
                "token": token,
                "reason": reason,
                "key": worker_key,
            },
        ),
        (
            "DELETE FROM factory_lease WHERE repo = :repo AND claim_id = :token"
            " AND kind IN ('worker', 'target', 'slot')",
            {"repo": repo, "token": token},
        ),
        (
            "DELETE FROM factory_provider_reservation WHERE claim_id = :token",
            {"token": token},
        ),
    ]
    result = core.reconciled_batch(db, statements, lambda: core.event_exists(db, event_id))
    return bool(result[0]) if result is not None else True


def worker_claim(
    repo: str, worker: str, *, active_only: bool = False
) -> tuple[str, list[int]] | None:
    if not core.enabled():
        return None
    import json

    rows = core.store().read(
        "SELECT claim_id, targets FROM factory_lease WHERE lease_key = :k"
        " AND (:any = 1 OR expires_at > unixepoch())",
        {"k": _keys(repo, worker, [])[0], "any": int(not active_only)},
    )
    if not rows:
        return None
    return str(rows[0]["claim_id"]), [int(n) for n in json.loads(rows[0]["targets"])]


def release_worker(repo: str, worker: str, reason: str) -> bool:
    claim = worker_claim(repo, worker)
    return release_lease(repo, worker, claim[0], reason) if claim else False


def renewal_statements(repo: str, workers: list[str], ttl: int) -> list[core.Statement]:
    """Two statements for any number of verified workers: reservations, then lease rows."""
    import json

    keys = json.dumps([_keys(repo, worker, [])[0] for worker in sorted(set(workers))])
    claims = (
        "(SELECT claim_id FROM factory_lease WHERE lease_key IN"
        " (SELECT value FROM json_each(:keys)) AND expires_at > unixepoch())"
    )
    params = {"keys": keys, "ttl": ttl, "repo": repo}
    return [
        (
            "UPDATE factory_provider_reservation SET expires_at = max(expires_at, unixepoch() + :ttl)"
            f" WHERE claim_id IN {claims}",
            params,
        ),
        (
            "UPDATE factory_lease SET expires_at = max(expires_at, unixepoch() + :ttl)"
            f" WHERE claim_id IN {claims} AND repo = :repo AND expires_at > unixepoch()"
            " AND kind IN ('worker', 'target', 'slot') RETURNING kind",
            params,
        ),
    ]


def renew_verified_workers(repo: str, workers: list[str]) -> int:
    """Renew workers the trusted controller verified alive; returns claims extended."""
    if not core.enabled() or not workers:
        return 0
    if any(not WORKER_RE.fullmatch(worker) for worker in workers):
        raise ValueError("invalid verified worker ID")
    _, leases = core.store().batch(renewal_statements(repo, workers, FIXED_LEASE_TTL_SECONDS))
    return sum(1 for row in leases if row["kind"] == "worker")


def acquire_operation_lease(
    repo: str, holder: str, number: int, ttl: int = OPERATION_LEASE_TTL_SECONDS
) -> str | None:
    """Hold one target for a short controller operation; no provider capacity is reserved."""
    _operation_request(holder, number, ttl)
    if not core.enabled():
        _unconfigured("trusted factory controller requires the factory store")
        return ""
    db = core.store()
    token = str(uuid.uuid4())
    key = _target_key(repo, number)
    statements = [
        core.lease_upsert(
            key=key,
            repo=repo,
            kind="target",
            token=token,
            ttl=ttl,
            number=number,
            operation=holder,
            targets=[number],
            guard=core.free_keys_guard([key]),
        ),
        core.event_insert(
            repo,
            ORIGIN,
            "operation_lease_acquired",
            {
                "event_id": f"operation:{token}",
                "claim_id": token,
                "operation": holder,
                "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            },
            subject_type="target",
            subject_number=number,
            guard=core.held_guard(key, token),
        ),
    ]
    core.reconciled_batch(db, statements, lambda: core.claim_of(db, key) == token)
    return token if core.claim_of(db, key) == token else None


def release_operation_lease(repo: str, number: int, token: str, reason: str) -> bool:
    """Release only this operation claim; a worker claim on the target is untouchable."""
    if not core.enabled() or not token:
        return False
    db = core.store()
    key = _target_key(repo, number)
    event_id = f"operation-released:{token}"
    guard = (
        "EXISTS (SELECT 1 FROM factory_lease WHERE lease_key = :g_key AND claim_id = :g_token"
        " AND operation IS NOT NULL AND worker IS NULL AND external = 0)",
        {"g_key": key, "g_token": token},
    )
    statements = [
        core.event_insert(
            repo,
            ORIGIN,
            "operation_lease_released",
            {"event_id": event_id, "claim_id": token, "reason": reason},
            subject_type="target",
            subject_number=number,
            guard=guard,
        ),
        (
            "DELETE FROM factory_lease WHERE lease_key = :key AND claim_id = :token"
            " AND worker IS NULL AND external = 0",
            {"key": key, "token": token},
        ),
    ]
    result = core.reconciled_batch(db, statements, lambda: core.event_exists(db, event_id))
    return bool(result[0]) if result is not None else True


def target_lease(repo: str, number: int) -> dict[str, Any] | None:
    if not core.enabled():
        raise RuntimeError("factory store is not configured")
    rows = core.store().read(
        "SELECT *, 1 AS active FROM factory_lease"
        " WHERE lease_key = :k AND expires_at > unixepoch()",
        {"k": _target_key(repo, number)},
    )
    return core.lease_row(rows[0]) if rows else None


def list_leases(repo: str) -> list[dict[str, Any]]:
    if not core.enabled():
        raise RuntimeError("factory store is not configured")
    rows = core.store().read(
        "SELECT *, expires_at > unixepoch() AS active FROM factory_lease WHERE repo = :repo"
        " ORDER BY lease_key",
        {"repo": repo},
    )
    return [core.lease_row(row) for row in rows if str(row["lease_key"]).startswith(LEASE_PREFIX)]


def release_expired_leases(repo: str) -> int:
    """Delete factory rows expired over an hour ago; external claims are never purged."""
    if not core.enabled():
        raise RuntimeError("factory store is not configured")
    (rows,) = core.store().batch(
        [
            (
                "DELETE FROM factory_lease WHERE repo = :repo AND external = 0"
                " AND expires_at <= unixepoch() - 3600 RETURNING 1 AS x",
                {"repo": repo},
            )
        ]
    )
    return len(rows)
