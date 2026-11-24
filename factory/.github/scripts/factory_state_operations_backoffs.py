#!/usr/bin/env python3
"""Operator view and quota-only cleanup of learned backoffs.

``export-backoffs`` lists, per provider, the three things that keep a model or
source from running: model health cooldowns, Fibonacci backoffs of real
failures, and quota/throttle/credit cooldowns with the source of their reset
time. ``reset-model-health --quota-only`` clears what quota and throttle
refusals wrote before they stopped growing streaks, and caps the remaining
Fibonacci rows at one day. It keeps real-failure streaks and never writes the
full-reset marker, so those streaks are not forgotten.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from factory_quota_reset import CAPACITY_OUTCOMES
from factory_state_operations_backoff import MAX_FIBONACCI_DELAY_SECONDS, effective_retry_at
from factory_state_operations_core import REPO_RE
from factory_state_operations_quota import active_capacity_cooldowns

BACKOFF_PREFIXES = ("factory-model-backoff-v1", "factory-worker-backoff-v1")


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _state(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    state = payload[0] if isinstance(payload, list) and payload else {}
    return state if isinstance(state, dict) else {}


async def _export_backoffs(conn: Any, repo: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    providers: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def bucket(source: str) -> dict[str, list[dict[str, Any]]]:
        return providers.setdefault(
            source or "unknown", {"health": [], "fibonacci": [], "capacity": []}
        )

    for row in await conn.fetch(
        """SELECT source,model,status,last_outcome,cooldown_until
             FROM public.factory_model_health
            WHERE repo=$1 AND (status='retired' OR cooldown_until>now())""",
        repo,
    ):
        bucket(str(row["source"]))["health"].append(
            {
                "model": row["model"],
                "status": row["status"],
                "outcome": row["last_outcome"],
                "until": _iso(row["cooldown_until"]),
            }
        )
    for prefix in BACKOFF_PREFIXES:
        for row in await conn.fetch(
            """SELECT cache_key,payload,updated_at FROM public.factory_state_kv
                WHERE cache_key LIKE $1 AND expires_at>now()""",
            f"{prefix}:{repo}:%",
        ):
            state = _state(row["payload"])
            until = effective_retry_at(state, row["updated_at"])
            if until is None or until <= now:
                continue
            bucket(str(state.get("source") or ""))["fibonacci"].append(
                {
                    "kind": "worker" if prefix.startswith("factory-worker") else "model",
                    "key": str(row["cache_key"]).rsplit(":", 1)[-1],
                    "model": state.get("model"),
                    "outcome": state.get("outcome"),
                    "streak": int(state.get("streak") or 0),
                    "until": until.isoformat(),
                    "quota_caused": state.get("outcome") in CAPACITY_OUTCOMES,
                }
            )
    for scope, rows in (await active_capacity_cooldowns(conn, repo)).items():
        for source, state in rows.items():
            bucket(source)["capacity"].append(
                {
                    "scope": scope,
                    "model": state.get("model"),
                    "outcome": state.get("outcome"),
                    "kind": state.get("kind"),
                    "reset_source": state.get("reset_source", "policy"),
                    "until": _iso(state.get("until")),
                }
            )
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "providers": dict(sorted(providers.items())),
    }


async def _reset_quota_backoffs(conn: Any, repo: str) -> dict[str, int]:
    if not REPO_RE.fullmatch(repo):
        raise ValueError("invalid repository")
    outcomes = sorted(CAPACITY_OUTCOMES)
    async with conn.transaction():
        removed = await conn.fetchval(
            """WITH deleted AS (DELETE FROM public.factory_state_kv
                 WHERE (starts_with(cache_key,$1) OR starts_with(cache_key,$2))
                   AND payload->0->>'outcome' = ANY($3::text[]) RETURNING 1)
               SELECT count(*) FROM deleted""",
            f"{BACKOFF_PREFIXES[0]}:{repo}:",
            f"{BACKOFF_PREFIXES[1]}:{repo}:",
            outcomes,
        )
        health = await conn.fetchval(
            """WITH cleared AS (UPDATE public.factory_model_health
                 SET status='unknown',cooldown_until=NULL,updated_at=now()
                 WHERE repo=$1 AND status='cooling' AND last_outcome = ANY($2::text[])
                 RETURNING 1)
               SELECT count(*) FROM cleared""",
            repo,
            outcomes,
        )
        capped = await conn.fetchval(
            """WITH capped AS (UPDATE public.factory_state_kv SET
                 payload=jsonb_set(payload,'{0,next_retry_at}',
                   to_jsonb(to_char((updated_at + $3::int * interval '1 second')
                     AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"+00:00"')))
                 WHERE (starts_with(cache_key,$1) OR starts_with(cache_key,$2))
                   AND (payload->0->>'next_retry_at')::timestamptz
                       > updated_at + $3::int * interval '1 second'
                 RETURNING 1)
               SELECT count(*) FROM capped""",
            f"{BACKOFF_PREFIXES[0]}:{repo}:",
            f"{BACKOFF_PREFIXES[1]}:{repo}:",
            MAX_FIBONACCI_DELAY_SECONDS,
        )
    return {
        "quota_backoff_rows": int(removed or 0),
        "quota_health_rows": int(health or 0),
        "capped_fibonacci_rows": int(capped or 0),
    }

