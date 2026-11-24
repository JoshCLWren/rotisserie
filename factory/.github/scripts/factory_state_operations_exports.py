#!/usr/bin/env python3
"""Trusted read-only exports and the learned-health reset."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import factory_state_operations_core as core
from factory_state_operations_backoff import effective_retry_at, legacy_capacity_backoff
from factory_state_operations_core import REPO_RE
from factory_state_operations_quota import CREDIT_POOL_KEY, active_capacity_cooldowns


async def _export_model_exclusions(
    conn: Any, repo: str, worker: str = "", requested_model: str = "auto",
) -> dict[str, list[str]]:
    """Trusted exclusions; permit one explicitly pinned, healthy Go work-failure retry.

    An owner-selected *exact* free Go model can bypass advisory Fibonacci
    backoff from earlier unsuccessful coding work. No stored cooldown is
    deleted, and retired/cooling model health or active quota remains
    authoritative. Automatic fleet selection uses the original exclusions.
    """
    retry_override: tuple[str, str] | None = None
    if requested_model not in ("", "auto"):
        roster = core._roster()
        if not worker or worker not in roster:
            raise ValueError("trusted worker required for exact-model override")
        source = roster[worker][0]
        from factory_manual_model import validate_model_syntax
        validate_model_syntax(source, requested_model)
        if source == "opencode-free":
            bare_model = requested_model.split("/", 1)[1]
            health = await conn.fetchrow(
                """SELECT status,last_outcome,cooldown_until
                   FROM public.factory_model_health
                   WHERE repo=$1 AND source=$2 AND model=$3""",
                repo, source, bare_model,
            )
            budget = await conn.fetchrow(
                """SELECT last_outcome,cooldown_until,quota_reset_at
                   FROM public.factory_provider_budget
                   WHERE repo=$1 AND source=$2""",
                repo, source,
            )
            now = datetime.now(timezone.utc)
            healthy = (
                health is not None
                and health["status"] == "healthy"
                and health["last_outcome"] == "work_failure"
                and (health["cooldown_until"] is None or health["cooldown_until"] <= now)
            )
            quota_active = (
                budget is not None
                and budget["last_outcome"] == "provider_quota"
                and any(
                    value is not None and value > now
                    for value in (budget["cooldown_until"], budget["quota_reset_at"])
                )
            )
            if healthy and not quota_active:
                retry_override = (source, bare_model)
    rows = await conn.fetch(
        """SELECT source, model FROM public.factory_model_health
           WHERE repo=$1 AND (status='retired' OR
               (status IN ('unavailable','cooling') AND cooldown_until>now()))
           ORDER BY source, model""", repo,
    )
    excluded: dict[str, list[str]] = {}
    for row in rows:
        excluded.setdefault(str(row["source"]), []).append(str(row["model"]))
    # Durable source/model Fibonacci backoffs outlive the shorter model
    # health cooldowns. Export them as candidate exclusions for catalog slots.
    backoffs = await conn.fetch(
        """SELECT payload,updated_at FROM public.factory_state_kv
           WHERE cache_key LIKE $1""",
        f"factory-model-backoff-v1:{repo}:source:%"
    )
    now = datetime.now(timezone.utc)
    for row in backoffs:
        record = row["payload"]
        if isinstance(record, str):
            record = json.loads(record)
        state = record[0] if isinstance(record, list) and record else {}
        if not isinstance(state, dict):
            continue
        source, model = state.get("source"), state.get("model")
        if not isinstance(source, str) or not isinstance(model, str) or not model:
            continue
        if legacy_capacity_backoff(state):
            continue
        retry_at = effective_retry_at(state, row.get("updated_at") if hasattr(row, "get") else None)
        if retry_at is not None and retry_at > now:
            if (
                retry_override == (source, model)
                and state.get("outcome") == "work_failure"
                and state.get("suspended") is not True
            ):
                # Operator may retry prior unsuccessful coding work. Actual
                # price, tool capability and provider access are checked live
                # by factory_manual_model before running the model.
                continue
            if model not in excluded.setdefault(source, []):
                excluded[source].append(model)
    # The worker identity is validated for the trusted caller; issue #1093
    # comment locators are retired and never exported to the runner.
    if worker and (not worker.isdecimal() or worker not in core._roster()):
        raise ValueError("invalid trusted worker for model exclusions")
    pools = sorted((await active_capacity_cooldowns(conn, repo))["pool"])
    if pools:
        excluded[CREDIT_POOL_KEY] = pools
    return excluded


async def _export_candidate_health(conn: Any, repo: str) -> dict[str, Any]:
    """Compact trusted execution evidence, passed to the unprivileged runner.

    No secrets, leases or comment bodies cross the Actions job boundary.
    This is advisory candidate ranking, never authorization to dispatch.
    """
    models = await conn.fetch(
        """SELECT source, model, status, last_outcome, cooldown_until,
                  updated_at FROM public.factory_model_health WHERE repo=$1""",
        repo,
    )
    providers = await conn.fetch(
        """SELECT source, last_outcome, cooldown_until, quota_reset_at,
                  updated_at FROM public.factory_provider_budget WHERE repo=$1""",
        repo,
    )
    def seconds(value: Any) -> int | None:
        return int(value.timestamp()) if value is not None else None
    return {
        "models": [
            {"source": row["source"], "model": row["model"],
             "status": row["status"], "outcome": row["last_outcome"],
             "updated": seconds(row["updated_at"]),
             "until": seconds(row["cooldown_until"])}
            for row in models
        ],
        "providers": [
            {"source": row["source"], "outcome": row["last_outcome"],
             "updated": seconds(row["updated_at"]),
             "until": max(
                 seconds(row["cooldown_until"]) or 0,
                 seconds(row["quota_reset_at"]) or 0)}
            for row in providers
        ],
    }



async def _export_dashboard_retry(conn: Any, repo: str) -> dict[str, Any]:
    """Bounded, read-only factory retry and capacity evidence for public status.

    Never export credentials, raw comments, private agent prompts, or lease
    tokens. These observations do not grant dispatch or merge authority.
    """
    rows = await conn.fetch(
        """SELECT cache_key,payload,expires_at,updated_at
           FROM public.factory_state_kv
           WHERE cache_key LIKE $1 OR cache_key LIKE $2 OR cache_key LIKE $3
           ORDER BY updated_at DESC LIMIT 750""",
        f"factory-worker-backoff-v1:{repo}:%",
        f"factory-model-backoff-v1:{repo}:%",
        f"factory-provider-backoff-v1:{repo}:%",
    )
    backoffs = []
    for row in rows:
        key = str(row["cache_key"])
        kind = ("worker" if key.startswith("factory-worker-backoff-v1:")
                else "model" if key.startswith("factory-model-backoff-v1:")
                else "provider")
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        record = payload[0] if isinstance(payload, list) and payload else {}
        if not isinstance(record, dict):
            continue
        backoffs.append({
            "kind": kind,
            "worker": key.rsplit(":", 1)[-1] if kind == "worker" else "",
            "source": str(record.get("source") or
                          (key.rsplit(":", 1)[-1] if kind == "provider" else "")),
            "model": str(record.get("model") or ""),
            "outcome": str(record.get("outcome") or ""),
            "streak": int(record.get("streak") or 0),
            "delay_seconds": int(record.get("delay_seconds") or 0),
            "suspended": record.get("suspended") is True,
            "next_retry_at": record.get("next_retry_at"),
            "until": row["expires_at"].isoformat() if kind == "provider" else None,
            "run_id": int(record.get("run_id") or 0),
            "updated_at": row["updated_at"].isoformat(),
        })
    reservations = await conn.fetch(
        """SELECT worker,source,expires_at FROM public.factory_provider_reservation
           WHERE repo=$1 AND expires_at>now() ORDER BY expires_at LIMIT 300""",
        repo,
    )
    retries = await conn.fetch(
        """SELECT pr_number,head_sha,stage,attempts,outcome,retry_after,last_run_id
           FROM public.factory_pr_stage_retry
           WHERE repo=$1 AND retry_after>now()
           ORDER BY retry_after LIMIT 150""",
        repo,
    )
    return {
        "backoffs": backoffs,
        "reservations": [
            {"worker": r["worker"], "source": r["source"],
             "expires_at": r["expires_at"].isoformat()} for r in reservations
        ],
        "pr_retries": [
            {"pr": r["pr_number"], "head": r["head_sha"], "stage": r["stage"],
             "attempts": r["attempts"], "outcome": r["outcome"],
             "retry_after": r["retry_after"].isoformat(), "run_id": r["last_run_id"]}
            for r in retries
        ],
    }


async def _export_fleet_failover_sources(conn: Any, repo: str, worker: str) -> list[str]:
    """Admit sources that cannot bypass a configured provider capacity limit.

    The assigned source already owns capacity under the target's exclusive
    claim. Other sources without concurrency/daily limits may be used without
    taking a second reservation. Capped sources require an atomic transfer
    before they can become candidates, so they are excluded here.
    """
    from factory_provider_registry import PROVIDERS
    from factory_source_budget import load_budgets

    roster = core._roster()
    if worker not in roster:
        raise ValueError("unknown leased worker")
    assigned = roster[worker][0]
    budgets = {
        r["source"]: r for r in await conn.fetch(
            "SELECT source,concurrency_limit,daily_limit "
            "FROM public.factory_provider_budget WHERE repo=$1", repo,
        )
    }
    caps = load_budgets()
    cooling = (await active_capacity_cooldowns(conn, repo))["account"]
    sources = set(PROVIDERS) | {"opencode-free", "openrouter-free", "kilo-auto"}
    selected = {assigned}
    for source in sources - {assigned}:
        budget = budgets.get(source)
        if source in caps or source in cooling:
            continue
        if budget is not None:
            if budget["concurrency_limit"] is not None or budget["daily_limit"] is not None:
                continue
        selected.add(source)
    return sorted(selected)


async def _reset_model_health(conn: Any, repo: str, include_retired: bool = False) -> dict[str, int]:
    """Clear learned failure/cooldown state; retain delivery, lease and budget usage."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError("invalid repository")
    async with conn.transaction():
        health = await conn.fetchval(
            """WITH deleted AS (DELETE FROM public.factory_model_health
                 WHERE repo=$1 AND ($2::boolean OR status != 'retired') RETURNING 1)
               SELECT count(*) FROM deleted""", repo, include_retired)
        backoffs = await conn.fetchval(
            """WITH deleted AS (DELETE FROM public.factory_state_kv
                 WHERE starts_with(cache_key,$1) OR starts_with(cache_key,$2)
                    OR starts_with(cache_key,$3) OR starts_with(cache_key,$4) RETURNING 1)
               SELECT count(*) FROM deleted""",
            f"factory-model-backoff-v1:{repo}:",
            f"factory-worker-backoff-v1:{repo}:",
            f"factory-provider-backoff-v1:{repo}:",
            f"factory-credit-pool-v1:{repo}:")
        providers = await conn.fetchval(
            """WITH cleared AS (UPDATE public.factory_provider_budget
                 SET cooldown_until=NULL,quota_reset_at=NULL,updated_at=now()
                 WHERE repo=$1 AND (cooldown_until IS NOT NULL OR quota_reset_at IS NOT NULL)
                 RETURNING 1)
               SELECT count(*) FROM cleared""", repo)
        # Preserve the event ledger but never rehydrate failures from before reset.
        await conn.execute(
            """INSERT INTO public.factory_state_kv
                 (cache_key,payload,fetched_at,expires_at,updated_at)
                 VALUES($1,'[]'::jsonb,now(),now()+interval '30 days',now())
                 ON CONFLICT(cache_key) DO UPDATE SET
                   payload=EXCLUDED.payload,fetched_at=EXCLUDED.fetched_at,
                   expires_at=EXCLUDED.expires_at,updated_at=EXCLUDED.updated_at""",
            f"factory-model-health-reset-v1:{repo}")
    return {"model_health_rows": int(health or 0), "backoff_rows": int(backoffs or 0),
            "provider_cooldowns": int(providers or 0)}
