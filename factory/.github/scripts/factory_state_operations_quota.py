#!/usr/bin/env python3
"""Reset-timed cooldowns for quota, throttle and empty-balance refusals.

These refusals describe provider capacity, so they never grow a Fibonacci
streak (factory_state_operations_backoff). One refusal writes one cooldown,
scoped by factory_quota_reset:

- account: factory-provider-backoff-v1:<repo>:source:<source>, which the
  dispatcher filter and the fleet failover export honor for the whole source;
- pool: factory-credit-pool-v1:<repo>:source:<source>, exported as the
  "@credit_pool" exclusion so only that source's priced models are skipped;
- model: factory_model_health for the exact pair.

A later refusal never shortens an active cooldown.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from factory_quota_reset import OVERSIZE, Reset, resolve, scope

PROVIDER_PREFIX = "factory-provider-backoff-v1"
POOL_PREFIX = "factory-credit-pool-v1"
CREDIT_POOL_KEY = "@credit_pool"


def capacity_hints(raw: Any) -> dict[tuple[str, str], dict[str, object]]:
    """Validated FACTORY_QUOTA_RESET markers keyed by (source, model)."""
    from factory_quota_reset import KINDS, RESET_SOURCES

    hints: dict[tuple[str, str], dict[str, object]] = {}
    if not isinstance(raw, list):
        return hints
    for item in raw[:200]:
        if not isinstance(item, dict):
            continue
        source, model = item.get("source"), item.get("model")
        epoch = item.get("reset_at")
        if (
            isinstance(source, str)
            and isinstance(model, str)
            and item.get("kind") in KINDS
            and item.get("reset_source") in RESET_SOURCES
            and isinstance(epoch, int)
            and not isinstance(epoch, bool)
        ):
            hints[(source, model)] = item
    return hints


async def _extend_key(conn: Any, key: str, until: datetime, state: dict[str, object]) -> None:
    await conn.execute(
        """INSERT INTO public.factory_state_kv
             (cache_key,payload,fetched_at,expires_at,updated_at)
           VALUES($1,$2::jsonb,now(),$3,now())
           ON CONFLICT(cache_key) DO UPDATE SET
             payload=EXCLUDED.payload,fetched_at=EXCLUDED.fetched_at,
             expires_at=EXCLUDED.expires_at,updated_at=EXCLUDED.updated_at
           WHERE factory_state_kv.expires_at <= EXCLUDED.expires_at""",
        key,
        json.dumps([state]),
        until,
    )


async def record_capacity_cooldown(
    conn: Any,
    repo: str,
    source: str,
    model: str,
    outcome: str,
    marker_scope: str,
    hint: dict[str, object] | None,
    observed: datetime,
    run_id: int,
) -> Reset:
    """Write the one cooldown a capacity refusal earns; return what was decided."""
    reset = resolve(source, outcome, hint, observed)
    if reset.kind == OVERSIZE or reset.until is None:
        return reset
    where = "pool" if marker_scope == "pool" else scope(source, reset.kind)
    state = {
        "source": source,
        "model": model,
        "outcome": outcome,
        "kind": reset.kind,
        "scope": where,
        "reset_source": reset.reset_source,
        "run_id": run_id,
        "next_retry_at": reset.until.isoformat(),
    }
    # The refused pair also cools: a same-run success on this source clears the
    # account key, which proves the account works but not this model.
    if where == "account":
        await _extend_key(conn, f"{PROVIDER_PREFIX}:{repo}:source:{source}", reset.until, state)
    elif where == "pool":
        await _extend_key(conn, f"{POOL_PREFIX}:{repo}:source:{source}", reset.until, state)
    await conn.execute(
        """INSERT INTO public.factory_model_health
             (repo,source,model,status,last_outcome,cooldown_until,last_run_id)
           VALUES($1,$2,$3,'cooling',$4,$5,$6)
           ON CONFLICT(repo,source,model) DO UPDATE SET
             status=EXCLUDED.status,last_outcome=EXCLUDED.last_outcome,
             cooldown_until=GREATEST(EXCLUDED.cooldown_until,
               CASE WHEN factory_model_health.status IN ('cooling','unavailable')
                    THEN factory_model_health.cooldown_until END),
             last_run_id=EXCLUDED.last_run_id,updated_at=now()
           WHERE factory_model_health.last_run_id <= EXCLUDED.last_run_id
             AND factory_model_health.status != 'retired'""",
        repo,
        source,
        model,
        outcome,
        reset.until,
        run_id,
    )
    return reset


async def active_capacity_cooldowns(conn: Any, repo: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Active account and pool cooldowns: {"account"|"pool": {source: state}}."""
    found: dict[str, dict[str, dict[str, Any]]] = {"account": {}, "pool": {}}
    for kind, prefix in (("account", PROVIDER_PREFIX), ("pool", POOL_PREFIX)):
        head = f"{prefix}:{repo}:source:"
        for row in await conn.fetch(
            """SELECT cache_key,payload,expires_at FROM public.factory_state_kv
               WHERE cache_key LIKE $1 AND expires_at>now()""",
            head + "%",
        ):
            key = str(row.get("cache_key") or "")
            if not key.startswith(head):
                continue
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            state = payload[0] if isinstance(payload, list) and payload else {}
            if isinstance(state, dict):
                found[kind][key.removeprefix(head)] = {**state, "until": row.get("expires_at")}
    return found
