"""Index upgrades, single-statement dedup and escaped key prefixes on real PostgreSQL (#254).

Uses the disposable server fixture of the external claim suite. Set
``FACTORY_TEST_POSTGRES_URL`` to a LOCAL throwaway server when no server
binaries are installed; the fixture drops and recreates ``factory_state``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tests.test_external_claim_postgres import _sql, neon, pytestmark, server  # noqa: F401

ledger = importlib.import_module("factory_state_ledger")
indexes = importlib.import_module("factory_state_indexes")
core = importlib.import_module("factory_state_ledger_core")

REPO = "acme/widgets"
NAMES = [name for name, _ in indexes.UPGRADES]


def _upgrade() -> dict[str, list[str]]:
    async def run() -> dict[str, list[str]]:
        conn = await ledger.measured_connect(dsn=ledger._dsn(), timeout=8, command_timeout=8)
        try:
            return await indexes.ensure_upgrades(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


def _plan(statement: str, *args: Any) -> str:
    async def run() -> str:
        conn = await ledger.measured_connect(dsn=ledger._dsn(), timeout=8, command_timeout=8)
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL enable_seqscan = off")
                rows = await conn.fetch("EXPLAIN " + statement, *args)
            return "\n".join(str(row[0]) for row in rows)
        finally:
            await conn.close()

    return asyncio.run(run())


def _present() -> set[str]:
    rows = _sql(
        "SELECT name FROM unnest($1::text[]) AS name WHERE to_regclass('public.' || name) IS NOT NULL",
        NAMES,
    )
    return {str(row["name"]) for row in rows}


def test_bootstrap_upgrades_a_database_created_before_the_indexes(neon: str) -> None:  # noqa: F811
    assert _present() == set(NAMES)
    _sql("".join(f"DROP INDEX IF EXISTS public.{name};" for name in NAMES))
    assert _present() == set()
    first = _upgrade()
    assert first["created"] == NAMES
    assert first["failed"] == [] and first["deferred"] == []
    assert _present() == set(NAMES)
    assert _upgrade() == {"created": [], "failed": [], "deferred": []}


def test_hot_lookups_can_use_their_indexes(neon: str) -> None:  # noqa: F811
    dedup = _plan(
        "SELECT 1 FROM public.factory_state_events"
        " WHERE source='github-actions' AND detail->>'event_id'=$1 LIMIT 1",
        "runtime:1",
    )
    assert "idx_factory_state_events_event_id" in dedup
    runs = _plan(
        "SELECT 1 FROM public.factory_state_events"
        " WHERE source='github-actions' AND detail->>'run_id'=ANY($1::text[])",
        ["1", "2"],
    )
    assert "idx_factory_state_events_run_id" in runs
    prefix = _plan(
        "SELECT 1 FROM public.factory_state_kv WHERE cache_key LIKE $1",
        core.prefix_pattern("factory-lease-v1:acme/repo_name:external:"),
    )
    assert "idx_factory_state_kv_key_prefix" in prefix


def test_lifecycle_dedup_is_one_statement_and_still_idempotent(neon: str) -> None:  # noqa: F811
    assert ledger.record_lifecycle(REPO, "901", "3", "started") == 1
    assert ledger.record_lifecycle(REPO, "901", "3", "started") == 0
    assert ledger.record_lifecycle(REPO, "901", "3", "finished") == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        added = list(
            pool.map(lambda _: ledger.record_lifecycle(REPO, "902", "3", "started"), range(6))
        )
    assert sorted(added) == [0, 0, 0, 0, 0, 1]
    ids = _sql("SELECT detail->>'event_id' AS event FROM public.factory_state_events ORDER BY id")
    assert [row["event"] for row in ids] == [
        "lifecycle:901:started",
        "lifecycle:901:finished",
        "lifecycle:902:started",
    ]


def test_prefix_patterns_never_match_another_repository(neon: str) -> None:  # noqa: F811
    claim = json.dumps([{"claim_id": "x", "wip": "true", "external": "true"}])
    _sql(
        """INSERT INTO public.factory_state_kv (cache_key, payload, fetched_at, expires_at)
           VALUES ($1, $2::jsonb, now(), now() + interval '1 hour')""",
        f"{ledger.LEASE_PREFIX}acme/widgets:external:7",
        claim,
    )
    assert ledger.external_wip_count("acme/widgets") == 1
    assert ledger.external_wip_count("acme/wid_ets") == 0
    assert ledger.external_wip_count("acme/wid%") == 0

