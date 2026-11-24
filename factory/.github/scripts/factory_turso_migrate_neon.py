"""Read-only Neon source for the store migration: one snapshot, keyset pages.

Every read runs inside one REPEATABLE READ READ ONLY transaction, so the live
counts and the copied rows describe the same moment. Events are also bounded
by the highest ``id`` seen when the snapshot opened. Table and column names
come only from the fixed lists below, never from input. asyncpg is the only
dependency, imported when the source opens.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DSN_FILE_ENV = "FACTORY_STATE_DSN_FILE"
KEYSETS: dict[str, tuple[str, ...]] = {
    "factory_state_kv": ("cache_key",),
    "factory_state_events": ("id",),
    "factory_model_health": ("repo", "source", "model"),
    "factory_worker_metrics": ("repo", "worker", "source", "model"),
    "factory_provider_budget": ("repo", "source"),
    "factory_provider_reservation": ("claim_id",),
    "factory_pr_stage_retry": ("repo", "pr_number", "head_sha", "stage"),
    "factory_runtime_event_keys": ("repo", "run_id"),
    "factory_model_attempt_daily": ("repo", "day", "source", "model", "outcome_class"),
}
RETIRED = ("factory_reconciliation_cursor", "factory_check_projection")
TABLES = (*KEYSETS, *RETIRED)
# Payloads of the families the migration copies; every other kv payload stays in Neon.
COPIED_KV = (
    "factory-lease-v1:%",
    "factory-worker-backoff-v1:%",
    "factory-model-backoff-v1:%",
    "factory-provider-backoff-v1:%",
    "factory-credit-pool-v1:%",
    "factory-model-health-reset-v1:%",
)


def dsn() -> str:
    """The Neon URL from the trusted file, without ``channel_binding`` (asyncpg rejects it)."""
    path = os.environ.get(DSN_FILE_ENV, "")
    if not path:
        raise RuntimeError("the Neon connection file is not configured")
    raw = Path(path).read_text(encoding="utf-8").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError("the Neon connection must be a Postgres URL")
    if not parsed.hostname or not parsed.username:
        raise ValueError("the Neon connection is missing its host or role")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "channel_binding"
    ]
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _columns(table: str) -> str:
    if table != "factory_state_kv":
        return "*"
    copied = " OR ".join(f"cache_key LIKE '{pattern}'" for pattern in COPIED_KV)
    return (
        "cache_key, fetched_at, expires_at, updated_at,"
        f" CASE WHEN {copied} THEN payload::text END AS payload"
    )


class NeonSource:
    """The migration's view of Neon: ``tables``, ``count``, ``page`` and ``now``."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._loop = asyncio.new_event_loop()
        self._conn: Any = None
        self._tx: Any = None
        self._present: set[str] = set()
        self._now = 0
        self._bound = 0

    def __enter__(self) -> NeonSource:
        self._run(self._open())
        return self

    def __exit__(self, *_: object) -> None:
        try:
            if self._conn is not None:
                self._run(self._close())
        finally:
            self._loop.close()

    def _run(self, call: Any) -> Any:
        return self._loop.run_until_complete(call)

    async def _open(self) -> None:
        import asyncpg

        self._conn = await asyncpg.connect(dsn=self._url, timeout=15, command_timeout=120)
        await self._conn.execute("SET default_transaction_read_only = on")
        self._tx = self._conn.transaction(isolation="repeatable_read", readonly=True)
        await self._tx.start()
        rows = await self._conn.fetch(
            "SELECT tablename FROM pg_catalog.pg_tables"
            " WHERE schemaname = 'public' AND tablename = ANY($1::text[])",
            list(TABLES),
        )
        self._present = {str(row["tablename"]) for row in rows}
        self._now = int(await self._conn.fetchval("SELECT extract(epoch FROM now())::bigint"))
        if "factory_state_events" in self._present:
            found = await self._conn.fetchval("SELECT max(id) FROM public.factory_state_events")
            self._bound = int(found or 0)

    async def _close(self) -> None:
        try:
            await self._tx.rollback()
        finally:
            await self._conn.close()

    def tables(self) -> set[str]:
        return set(self._present)

    def now(self) -> int:
        return self._now

    def _bounded(self, table: str) -> str:
        return " AND id <= $1" if table == "factory_state_events" else ""

    def count(self, table: str) -> int:
        if table not in TABLES:
            raise ValueError("unknown table")
        bound = self._bounded(table)
        args = [self._bound] if bound else []
        sql = f"SELECT count(*) FROM public.{table} WHERE true{bound}"
        return int(self._run(self._conn.fetchval(sql, *args)))

    def page(self, table: str, after: Sequence[Any] | None, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` rows ordered by the table's key, strictly after ``after``."""
        keys = KEYSETS[table]
        args: list[Any] = []
        where = "true"
        if table == "factory_state_events":
            args.append(self._bound)
            where = "id <= $1"
        if after is not None:
            marks = ", ".join(f"${len(args) + i + 1}" for i in range(len(keys)))
            where += f" AND ({', '.join(keys)}) > ({marks})"
            args.extend(after)
        args.append(limit)
        sql = (
            f"SELECT {_columns(table)} FROM public.{table} WHERE {where}"
            f" ORDER BY {', '.join(keys)} LIMIT ${len(args)}"
        )
        rows = self._run(self._conn.fetch(sql, *args))
        return [dict(row) for row in rows]
