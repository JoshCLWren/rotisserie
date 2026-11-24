#!/usr/bin/env python3
"""Neon connection, schema and roster shared by the factory state operations."""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn

SCHEMA = [
  "CREATE TABLE IF NOT EXISTS public.factory_model_health (\n repo text NOT NULL, source text NOT NULL, model text NOT NULL,\n status text NOT NULL DEFAULT 'unknown', last_outcome text,\n cooldown_until timestamptz, last_verified_at timestamptz,\n last_run_id bigint NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now(),\n PRIMARY KEY (repo,source,model))",
  "CREATE TABLE IF NOT EXISTS public.factory_provider_budget (\n repo text NOT NULL, source text NOT NULL,\n cooldown_until timestamptz, quota_reset_at timestamptz,\n concurrency_limit integer, daily_limit integer,\n usage_day date NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,\n daily_used integer NOT NULL DEFAULT 0, last_run_id bigint NOT NULL DEFAULT 0, last_outcome text,\n updated_at timestamptz NOT NULL DEFAULT now(),\n PRIMARY KEY (repo,source),\n CONSTRAINT factory_provider_capacity_nonnegative CHECK (concurrency_limit IS NULL OR concurrency_limit > 0),\n CONSTRAINT factory_provider_daily_nonnegative CHECK (daily_limit IS NULL OR daily_limit >= 0))",
  "CREATE TABLE IF NOT EXISTS public.factory_provider_reservation (\n claim_id uuid PRIMARY KEY, repo text NOT NULL, worker text NOT NULL,\n source text NOT NULL, expires_at timestamptz NOT NULL)",
  "CREATE INDEX IF NOT EXISTS idx_factory_provider_reservation_active ON public.factory_provider_reservation(repo,source,expires_at)",
  "CREATE TABLE IF NOT EXISTS public.factory_pr_stage_retry (\n repo text NOT NULL, pr_number bigint NOT NULL, head_sha text NOT NULL, stage text NOT NULL,\n attempts integer NOT NULL DEFAULT 0, outcome text NOT NULL,\n retry_after timestamptz, last_run_id bigint NOT NULL DEFAULT 0,\n updated_at timestamptz NOT NULL DEFAULT now(),\n PRIMARY KEY (repo,pr_number,head_sha,stage))",
  "CREATE TABLE IF NOT EXISTS public.factory_worker_metrics (\n repo text NOT NULL, worker text NOT NULL, source text NOT NULL, model text NOT NULL,\n attempts bigint NOT NULL DEFAULT 0, productive bigint NOT NULL DEFAULT 0,\n completed bigint NOT NULL DEFAULT 0, failures bigint NOT NULL DEFAULT 0,\n last_run_id bigint NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now(),\n PRIMARY KEY (repo,worker,source,model))",
  "CREATE TABLE IF NOT EXISTS public.factory_runtime_event_keys (\n repo text NOT NULL, run_id bigint NOT NULL, PRIMARY KEY (repo,run_id))",
  "CREATE TABLE IF NOT EXISTS public.factory_reconciliation_cursor (\n repo text NOT NULL, scope text NOT NULL,\n checkpoint text, checked_at timestamptz NOT NULL DEFAULT now(),\n changed_count bigint NOT NULL DEFAULT 0,\n PRIMARY KEY (repo,scope))"
]

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEAD_RE = re.compile(r"^[a-f0-9]{40}$")


def enabled() -> bool:
    return bool(os.environ.get("FACTORY_STATE_DSN_FILE"))


def _roster() -> dict[str, tuple[str, str]]:
    """Resolve current provider-derived worker identities, not a pin roster."""
    from factory_worker_slots import all_identities

    # Includes operator overflow identities used by forced requests.
    return {worker: (source, "") for worker, source in all_identities().items()}


async def ensure_schema(conn: Any) -> None:
    # Zero destructive DDL. Reproducible on an empty factory_state database.
    if await conn.fetchval("SELECT to_regclass('public.factory_reconciliation_cursor')") is not None:
        return
    for statement in SCHEMA:
        await conn.execute(statement)


async def _connection() -> Any:
    return await measured_connect(dsn=_dsn(), timeout=8, command_timeout=15)


def _run(fn: Any, *args: Any) -> Any:
    async def call() -> Any:
        conn = await _connection()
        try:
            await ensure_schema(conn)
            return await fn(conn, *args)
        finally:
            await conn.close()
    return asyncio.run(call())


def _neon_required() -> bool:
    """Adopters without Neon run with empty health and failover snapshots."""
    return os.environ.get("FACTORY_NEON_LEASE_REQUIRED") == "true"
