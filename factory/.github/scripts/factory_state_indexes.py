"""Additive factory_state indexes and tables applied by trusted bootstrap.

``ensure_schema`` stops at its first table check, so it never reaches objects
added after the original tables existed. The dispatcher's bootstrap step runs
these instead. Every statement is idempotent and additive: nothing is dropped
or rewritten, and a statement that fails (for example a role that does not
own the table) is reported without stopping the factory.
"""

from __future__ import annotations

from typing import Any

LOCK_KEY = "factory-state-upgrades-v1"

UPGRADES: tuple[tuple[str, str], ...] = (
    (
        "idx_factory_state_events_event_id",
        "CREATE INDEX IF NOT EXISTS idx_factory_state_events_event_id"
        " ON public.factory_state_events ((detail->>'event_id'))",
    ),
    (
        "idx_factory_state_events_run_id",
        "CREATE INDEX IF NOT EXISTS idx_factory_state_events_run_id"
        " ON public.factory_state_events ((detail->>'run_id'))",
    ),
    (
        "idx_factory_state_events_worker_recent",
        "CREATE INDEX IF NOT EXISTS idx_factory_state_events_worker_recent"
        " ON public.factory_state_events (created_at)"
        " WHERE event_type IN ('worker_started', 'worker_heartbeat', 'worker_outcome_signal',"
        " 'worker_completed', 'worker_idle', 'worker_failed')",
    ),
    (
        "idx_factory_state_events_retired",
        "CREATE INDEX IF NOT EXISTS idx_factory_state_events_retired"
        " ON public.factory_state_events (id)"
        " WHERE event_type IN ('cache_refresh', 'target_reconciled')",
    ),
    (
        "idx_factory_state_kv_key_prefix",
        "CREATE INDEX IF NOT EXISTS idx_factory_state_kv_key_prefix"
        " ON public.factory_state_kv (cache_key text_pattern_ops)",
    ),
    (
        "factory_model_attempt_daily",
        "CREATE TABLE IF NOT EXISTS public.factory_model_attempt_daily ("
        " repo text NOT NULL, day date NOT NULL, source text NOT NULL, model text NOT NULL,"
        " outcome_class text NOT NULL, attempts bigint NOT NULL DEFAULT 0,"
        " timed_attempts bigint NOT NULL DEFAULT 0, duration_ms bigint NOT NULL DEFAULT 0,"
        " PRIMARY KEY (repo, day, source, model, outcome_class))",
    ),
)


async def ensure_upgrades(conn: Any) -> dict[str, list[str]]:
    """Create missing upgrade objects once, under a non-blocking session lock."""
    import asyncpg

    missing = set(
        await conn.fetchval(
            """SELECT coalesce(array_agg(name), '{}'::text[]) FROM unnest($1::text[]) AS name
           WHERE to_regclass('public.' || name) IS NULL""",
            [name for name, _ in UPGRADES],
        )
    )
    result: dict[str, list[str]] = {"created": [], "failed": [], "deferred": []}
    if not missing:
        return result
    if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", LOCK_KEY):
        result["deferred"] = sorted(missing)
        return result
    try:
        # A plain CREATE INDEX briefly blocks writers; never queue behind a long holder.
        await conn.execute("SET lock_timeout = '5s'")
        for name, statement in UPGRADES:
            if name not in missing:
                continue
            try:
                await conn.execute(statement)
            except asyncpg.PostgresError as error:
                result["failed"].append(f"{name}: {type(error).__name__}")
            else:
                result["created"].append(name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", LOCK_KEY)
    return result
