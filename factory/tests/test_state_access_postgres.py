"""Index upgrades, single-statement dedup and attempt retention on real PostgreSQL (#254).

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
retention = importlib.import_module("factory_state_retention")

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
    _sql(
        "DROP TABLE IF EXISTS public.factory_model_attempt_daily;"
        + "".join(f" DROP INDEX IF EXISTS public.{name};" for name in NAMES[:-1])
    )
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


def _attempt(repo: str, run: int, index: int, days_old: int, **detail: Any) -> None:
    body = {
        "event_id": f"model-attempt:{run}:{index}",
        "repo": repo,
        "worker": "3",
        "run_id": str(run),
        "source": "nvidia",
        "model": "m1",
        "outcome_class": "provider_failure",
        "duration_ms": 1000,
        **detail,
    }
    _sql(
        """INSERT INTO public.factory_state_events
             (source, event_type, subject_type, subject_number, created_at, detail)
           VALUES ('github-actions', 'model_attempt', 'worker', 3,
                   now() - $1::int * interval '1 day',
                   $2::jsonb)""",
        days_old,
        json.dumps(body),
    )


def test_old_model_attempts_roll_into_daily_counts_once(neon: str) -> None:  # noqa: F811
    _sql("DELETE FROM public.factory_model_attempt_daily;")
    for index in range(4):
        _attempt(REPO, 1, index, 130)
    _attempt(REPO, 2, 0, 130, duration_ms=None, outcome_class="success")
    _attempt(REPO, 3, 0, 100)
    _attempt("other/repo", 4, 0, 130)
    with ThreadPoolExecutor(max_workers=3) as pool:
        moved = list(pool.map(lambda _: retention.rollup_model_attempts(REPO), range(3)))
    assert sum(moved) == 5
    daily = _sql(
        """SELECT outcome_class, attempts, timed_attempts, duration_ms
             FROM public.factory_model_attempt_daily WHERE repo=$1 ORDER BY outcome_class""",
        REPO,
    )
    assert daily == [
        {
            "outcome_class": "provider_failure",
            "attempts": 4,
            "timed_attempts": 4,
            "duration_ms": 4000,
        },
        {"outcome_class": "success", "attempts": 1, "timed_attempts": 0, "duration_ms": 0},
    ]
    left = _sql("SELECT detail->>'run_id' AS run FROM public.factory_state_events ORDER BY run")
    assert [row["run"] for row in left] == ["3", "4"]
    assert retention.prune_model_attempts(REPO) == 0
