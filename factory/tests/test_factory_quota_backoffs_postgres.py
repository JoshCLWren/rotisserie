"""Quota cooldown SQL against a real local PostgreSQL server (disposable fixture)."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta

from tests.test_external_claim_postgres import _sql, neon, pytestmark, server  # noqa: F401

ledger = importlib.import_module("factory_state_ledger")
operations = importlib.import_module("factory_state_operations")
backoffs = importlib.import_module("factory_state_operations_backoffs")
quota = importlib.import_module("factory_state_operations_quota")

REPO = "acme/widgets"


def _with_conn(fn, *args):
    async def go():
        conn = await ledger.measured_connect(dsn=ledger._dsn(), timeout=8, command_timeout=8)
        try:
            await operations.ensure_schema(conn)
            return await fn(conn, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


async def _seed(conn) -> None:
    await conn.execute(
        "DELETE FROM public.factory_state_kv WHERE starts_with(cache_key,'factory-');"
        " DELETE FROM public.factory_model_health"
    )
    far = (datetime.now(UTC) + timedelta(days=9)).isoformat()
    for key, outcome in (("quota-pair", "provider_quota"), ("real-pair", "agent_timeout")):
        await conn.execute(
            """INSERT INTO public.factory_state_kv(cache_key,payload,fetched_at,expires_at,updated_at)
               VALUES($1,$2::jsonb,now(),now()+interval '90 days',now())""",
            f"factory-model-backoff-v1:{REPO}:source:nvidia:model:{key}",
            json.dumps(
                [
                    {
                        "source": "nvidia",
                        "model": key,
                        "outcome": outcome,
                        "streak": 17,
                        "next_retry_at": far,
                    }
                ]
            ),
        )
    await conn.execute(
        """INSERT INTO public.factory_state_kv(cache_key,payload,fetched_at,expires_at,updated_at)
           VALUES($1,'[{"outcome":"provider_quota"}]'::jsonb,now(),now()+interval '20 days',now())""",
        f"factory-credit-pool-v1:{REPO}:source:groq-free",
    )
    await conn.execute(
        """INSERT INTO public.factory_model_health(repo,source,model,status,last_outcome,cooldown_until)
           VALUES($1,'groq-free','m','cooling','provider_quota',now()+interval '3 days')""",
        REPO,
    )


def test_quota_only_reset_keeps_real_streaks_and_caps_them(neon):  # noqa: F811
    _with_conn(_seed)
    result = _with_conn(backoffs._reset_quota_backoffs, REPO)
    assert result == {
        "quota_backoff_rows": 1,
        "quota_health_rows": 1,
        "capacity_cooldowns": 1,
        "capped_fibonacci_rows": 1,
    }
    rows = _sql(
        "SELECT cache_key, payload FROM public.factory_state_kv "
        "WHERE starts_with(cache_key,'factory-model-backoff-v1:')"
    )
    assert [r["cache_key"].rsplit(":", 1)[-1] for r in rows] == ["real-pair"]
    state = (
        json.loads(rows[0]["payload"])[0]
        if isinstance(rows[0]["payload"], str)
        else rows[0]["payload"][0]
    )
    assert state["streak"] == 17
    retry = datetime.fromisoformat(state["next_retry_at"])
    assert retry <= datetime.now(UTC) + timedelta(days=1, minutes=1)
    assert not _sql(
        "SELECT 1 FROM public.factory_state_kv "
        "WHERE cache_key LIKE 'factory-model-health-reset-v1:%'"
    )


def test_capacity_cooldown_never_shortens_and_is_listed(neon):  # noqa: F811
    _with_conn(_seed)
    now = datetime.now(UTC)
    later = {
        "kind": "daily",
        "reset_at": int((now + timedelta(hours=6)).timestamp()),
        "reset_source": "header",
    }
    sooner = {
        "kind": "throttle",
        "reset_at": int((now + timedelta(minutes=5)).timestamp()),
        "reset_source": "policy",
    }
    for run_id, hint in ((1, later), (2, sooner)):
        _with_conn(
            quota.record_capacity_cooldown,
            REPO,
            "nvidia",
            "m",
            "provider_quota",
            "model",
            hint,
            now,
            run_id,
        )
    listed = _with_conn(backoffs._export_backoffs, REPO)["providers"]
    (cooldown,) = listed["nvidia"]["capacity"]
    assert cooldown["reset_source"] == "header" and cooldown["scope"] == "account"
    assert datetime.fromisoformat(cooldown["until"]) > now + timedelta(hours=5)
    assert {f["model"] for f in listed["nvidia"]["fibonacci"]} == {"quota-pair", "real-pair"}
    assert [h["model"] for h in listed["groq-free"]["health"]] == ["m"]
