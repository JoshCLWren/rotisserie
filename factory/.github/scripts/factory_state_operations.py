#!/usr/bin/env python3
"""Trusted Neon decision projections for the ComicPile factory.

Only trusted Actions controllers use these functions. Agent/model runner jobs
must never receive FACTORY_STATE_DSN_FILE. GitHub remains authoritative for
review, code, exact-head checks, labels, and merges. SQL projections are hints
and capacity reservations, never merge authorization.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
TRANSIENT = {"provider_failure": 900, "provider_throttle": 1800, "provider_quota": 1800}
MODEL_COOLDOWNS = {"model_unavailable": 21600, "model_policy_violation": 21600,
                   "model_retired_410": 21600}
# A failing model must never cool another model on the same platform.
# Keep the old OmniRoute constant for compatibility with existing tests.
MODEL_SCOPED_SOURCES = {"omniroute-free", "omniroute-opencode"}


def model_scoped_transient(source: str, outcome: str) -> bool:
    return outcome in TRANSIENT


def provider_scoped_transient(source: str, outcome: str) -> bool:
    # Compatibility for legacy reconciliation callers; quota, throttle and
    # failures are always tracked by their exact (source, model) identity.
    return False


def enabled() -> bool:
    return bool(os.environ.get("FACTORY_STATE_DSN_FILE"))


def _roster() -> dict[str, tuple[str, str]]:
    """Resolve current provider-derived worker identities, not a pin roster."""
    from factory_worker_slots import slots

    return {item.worker: (item.source, "") for item in slots()}


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


def _provider_cooldown(source: str, outcome: str, observed: datetime, seconds: int | None = None) -> datetime | None:
    if outcome not in TRANSIENT:
        return None
    if outcome == "provider_quota":
        try:
            from factory_quota_pause import quota_reset_epoch
            reset = quota_reset_epoch(source, int(observed.timestamp()))
            if reset > observed.timestamp():
                return datetime.fromtimestamp(reset, timezone.utc)
        except (ImportError, ValueError, KeyError):
            pass
    return observed + timedelta(seconds=seconds if seconds is not None else TRANSIENT[outcome])


def exponential_provider_delay(
    outcome: str, prior_outcome: str | None, prior_duration: float,
    age_seconds: float,
) -> int:
    """Double recent provider cooldowns without penalizing recovered providers.

    This derives the prior delay from the existing durable Neon budget row,
    avoiding new schema or one independent timer per concurrent worker.
    """
    base = TRANSIENT[outcome]
    if prior_outcome not in TRANSIENT or age_seconds < 0 or age_seconds > 12 * 3600:
        return base
    return min(4 * 3600, max(base, int(max(0, prior_duration) * 2)))



# Worker failures get durable Fibonacci backoff while retaining their roster pins.
# Credential absence is gated before lease assignment, never charged to a model.
WORKER_RETRY_CLASSES = frozenset({
    "agent_timeout", "provider_failure", "provider_throttle", "provider_quota",
    "model_unavailable", "model_policy_violation", "environment_failure",
    "control_plane_failure", "unknown_failure", "work_failure",
})


def fibonacci_worker_delay(streak: int) -> int:
    """Five-minute Fibonacci retries per exact model, with no upper bound."""
    if streak < 1:
        raise ValueError("failure streak must be positive")
    previous, current = 1, 1
    for _ in range(1, streak):
        previous, current = current, previous + current
    return previous * 300


def fibonacci_retry_at(observed: datetime, streak: int) -> datetime:
    """Next retry time; clamps only at the largest representable datetime.

    SQL writers clamp expires_at to 9999-12-31 too, because asyncpg cannot
    decode a later timestamptz into a Python datetime.
    """
    try:
        return observed + timedelta(seconds=fibonacci_worker_delay(streak))
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def worker_backoff_reason(record: Any, source: str, model: str, now: datetime) -> str | None:
    """The 90-day state TTL is not the not-before time for a retry."""
    if record is None:
        return None
    payload = record["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    state = payload[0] if isinstance(payload, list) and payload else {}
    if not isinstance(state, dict):
        return None
    # A catalog-backed slot can execute a different published model each
    # session. Do not erase its worker-level Fibonacci backoff simply because
    # the TSV no longer contains a permanently pinned model ID.
    if state.get("source") and state["source"] != source:
        return None
    # Dynamic slots are not permanently assigned to a model. A known model
    # failure must not suppress the entire slot's alternate model capacity.
    if not model and state.get("outcome") in MODEL_RETRY_CLASSES:
        return None
    if model and model != "@catalog" and state.get("model") != model:
        return None
    retry_at = state.get("next_retry_at")
    # Legacy suspended rows without a not-before timestamp must not gate for 90 days.
    if retry_at is None and "suspended" in state:
        return None
    if retry_at is not None:
        try:
            if datetime.fromisoformat(str(retry_at)) > now:
                return f"Fibonacci retry deferred until {retry_at}"
        except ValueError:
            return "invalid worker backoff state"
        return None
    # Backward compatibility: old timeout keys stored their delay in expires_at.
    return "legacy timeout cooling in Neon" if record["expires_at"] > now else None



MODEL_RETRY_CLASSES = frozenset({
    "agent_timeout", "provider_failure", "provider_throttle", "provider_quota",
    "model_unavailable", "model_policy_violation",
})


def validate_intermediate_model_failures(
    raw: Any, source: str,
) -> list[tuple[str, str, str, str]]:
    """Normalize only attested exact (source, model, outcome, scope) events."""
    from factory_provider_candidates import ADAPTERS

    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("invalid trusted intermediate model failures")
    accepted = MODEL_RETRY_CLASSES | {"model_retired_410"}
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for entry in raw:
        if isinstance(entry, str) and source == "opencode-free":
            failure_source, model, classification, scope = source, entry, "model_unavailable", "model"
        elif isinstance(entry, dict):
            failure_source = entry.get("source", source)
            model, classification = entry.get("model"), entry.get("outcome")
            scope = entry.get("scope", "model")
        else:
            raise ValueError("invalid trusted intermediate model failure entry")
        if (not isinstance(failure_source, str) or failure_source not in ADAPTERS
                or not isinstance(model, str)
                or not re.fullmatch(r"[\w./:-]{1,180}", model)
                or classification not in accepted
                or scope not in {"account", "model"}
                or (scope == "account" and classification not in {"provider_quota", "provider_failure"})):
            raise ValueError("invalid trusted intermediate model failure identity")
        # Old account-labelled credit refusals must not suppress healthy peers.
        seen[(failure_source, model)] = (classification, "model")
    return [
        (s, model, *seen[(s, model)]) for s, model in sorted(seen)
    ]



def attributable_model_failure(classification: str, proven: bool) -> bool:
    """Only blame the shared source/model when inference was attempted."""
    return classification in MODEL_RETRY_CLASSES or (
        proven and classification in {"work_failure", "unknown_failure"}
    )


def historical_failure_streak(
    rows: list[Any], run_id: int, source: str, model: str, *,
    model_only: bool, model_independent: bool = False,
) -> int:
    """Recover a consecutive streak from trusted terminal Neon events.

    The current outcome can already be present in the event log. Ignore it
    and anything newer to avoid double counting or reversing a fresh success.
    Pre-session/unconfigured events cannot prove a model was attempted.
    """
    streak = 0
    for row in rows:
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        if not isinstance(detail, dict):
            continue
        previous_run = int(detail.get("run_id") or 0)
        if previous_run == 0 or previous_run >= run_id:
            continue
        if (detail.get("source") != source or
                (detail.get("model") != model and not model_independent)):
            # Fixed pins reset their streak when repinned. Catalog-backed
            # slots retain worker streak across real upstream model rotation.
            if not model_only:
                break
            continue
        if row["event_type"] == "worker_completed":
            break
        if row["event_type"] != "worker_failed" or detail.get("configured") is False:
            continue
        outcome = str(detail.get("outcome_class") or "unknown_failure")
        if model_only:
            if not attributable_model_failure(outcome, detail.get("exact_model_proven") is True):
                continue
        elif outcome not in WORKER_RETRY_CLASSES:
            continue
        if outcome == "unknown_failure" and detail.get("exact_model_proven") is not True:
            # Old unconfigured workers were incorrectly classified this way.
            continue
        streak += 1
    return streak


async def _recent_streak(
    conn: Any, run_id: int, source: str, model: str, *,
    repo: str, worker: str | None = None, model_independent: bool = False,
) -> int:
    """Read prior attempts after the latest manual health reset in one query."""
    if worker is None:
        rows = await conn.fetch(
            """SELECT event_type,detail FROM public.factory_state_events
               WHERE subject_type='worker'
                 AND event_type IN ('worker_failed','worker_completed')
                 AND detail->>'source'=$1 AND detail->>'model'=$2
                 AND created_at>GREATEST(
                     now()-interval '7 days',
                     COALESCE((SELECT updated_at FROM public.factory_state_kv
                               WHERE cache_key=$3), '-infinity'::timestamptz))
               ORDER BY id DESC LIMIT 64""",
            source, model, f"factory-model-health-reset-v1:{repo}",
        )
    else:
        rows = await conn.fetch(
            """SELECT event_type,detail FROM public.factory_state_events
               WHERE subject_type='worker' AND subject_number=$1::bigint
                 AND event_type IN ('worker_failed','worker_completed')
                 AND created_at>GREATEST(
                     now()-interval '7 days',
                     COALESCE((SELECT updated_at FROM public.factory_state_kv
                               WHERE cache_key=$2), '-infinity'::timestamptz))
               ORDER BY id DESC LIMIT 64""",
            int(worker), f"factory-model-health-reset-v1:{repo}",
        )
    return historical_failure_streak(
        rows, run_id, source, model, model_only=worker is None,
        model_independent=model_independent,
    )


async def project_runtime(conn: Any, repo: str, run_id: int, outcome: dict[str, Any]) -> bool:
    """Idempotently update model, provider, retry and worker metrics projections."""
    worker = str(outcome.get("worker") or "")
    roster = _roster()
    if worker not in roster or not repo or not REPO_RE.fullmatch(repo):
        raise ValueError("untrusted worker or repository")
    source, _ = roster[worker]
    reported_source = str(outcome.get("source") or "")
    if reported_source and reported_source != source:
        if outcome.get("trusted_source_switch") is not True:
            raise ValueError("runtime source changed without trusted worker transition")
        from factory_provider_candidates import ADAPTERS
        if reported_source not in ADAPTERS:
            raise ValueError("runtime source is not an integrated provider")
        source = reported_source
    # Pre-session failures cannot attest a provider/model. Track the worker
    # event without attributing faults to an arbitrary configured model.
    attributed = bool(reported_source and outcome.get("model"))
    model = str(outcome.get("model") or "unclassified")
    if not model or len(model) > 180:
        raise ValueError("invalid model")
    classification = ("unconfigured" if outcome.get("configured") is False else
                      str(outcome.get("outcome_class") or "unknown_failure"))
    proven = outcome.get("exact_model_proven") is True
    persisted = outcome.get("persisted_work") is True
    delivered = (persisted and outcome.get("delivery_outcome") == "persisted"
                 and str(outcome.get("delivery_pr") or "").isdecimal())
    completed = classification == "success" or delivered
    failed = classification not in {"success", "no_work", "unconfigured"} and not delivered
    observed = datetime.now(timezone.utc)
    # A trusted job-log collector can attest multiple attempted free models.
    # This evidence belongs to this same idempotent runtime transaction.
    model_failures = validate_intermediate_model_failures(outcome.get("model_failures"), source)
    async with conn.transaction():
        inserted = await conn.fetchval(
            """INSERT INTO public.factory_runtime_event_keys(repo,run_id)
               VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING run_id""", repo, run_id)
        if inserted is None:
            return False
        await conn.execute(
            """INSERT INTO public.factory_worker_metrics
                 (repo,worker,source,model,attempts,productive,completed,failures,last_run_id)
               VALUES($1,$2,$3,$4,1,$5,$6,$7,$8)
               ON CONFLICT(repo,worker,source,model) DO UPDATE SET
                 attempts=factory_worker_metrics.attempts+1,
                 productive=factory_worker_metrics.productive+EXCLUDED.productive,
                 completed=factory_worker_metrics.completed+EXCLUDED.completed,
                 failures=factory_worker_metrics.failures+EXCLUDED.failures,
                 last_run_id=greatest(factory_worker_metrics.last_run_id,EXCLUDED.last_run_id),
                 updated_at=now()""",
            repo, worker, source, model, int(delivered), int(completed),
            int(failed), run_id)
        if classification == "model_retired_410":
            status, until = "retired", None
        elif classification in MODEL_COOLDOWNS:
            status, until = "unavailable", observed + timedelta(seconds=MODEL_COOLDOWNS[classification])
        elif model_scoped_transient(source, classification):
            status, until = "cooling", observed + timedelta(seconds=fibonacci_worker_delay(1))
        elif proven:
            status, until = "healthy", None
        else:
            status, until = "unknown", None
        if attributed and (classification in MODEL_COOLDOWNS or model_scoped_transient(source, classification) or proven):
            await conn.execute(
                """INSERT INTO public.factory_model_health
                     (repo,source,model,status,last_outcome,cooldown_until,last_verified_at,last_run_id)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT(repo,source,model) DO UPDATE SET
                     status=EXCLUDED.status,last_outcome=EXCLUDED.last_outcome,
                     cooldown_until=EXCLUDED.cooldown_until,
                     last_verified_at=EXCLUDED.last_verified_at,
                     last_run_id=EXCLUDED.last_run_id,updated_at=now()
                   WHERE factory_model_health.last_run_id <= EXCLUDED.last_run_id
                     AND factory_model_health.status != 'retired'""",
                repo, source, model, status, classification, until, observed if proven else None, run_id)
        # Record every failed candidate independently even if another model
        # eventually completed the same leased task. No provider-wide breaker.
        for failed_source, failed_model, failed_outcome, _scope in model_failures:
            failed_status = (
                "retired" if failed_outcome == "model_retired_410"
                else "unavailable" if failed_outcome in MODEL_COOLDOWNS
                else "cooling"
            )
            failed_until = (
                None if failed_status == "retired"
                else observed + timedelta(seconds=MODEL_COOLDOWNS[failed_outcome])
                if failed_outcome in MODEL_COOLDOWNS
                else observed + timedelta(seconds=fibonacci_worker_delay(1))
            )
            await conn.execute(
                """INSERT INTO public.factory_model_health
                   (repo,source,model,status,last_outcome,cooldown_until,last_run_id)
                   VALUES($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT(repo,source,model) DO UPDATE SET
                     status=EXCLUDED.status,last_outcome=EXCLUDED.last_outcome,
                     cooldown_until=EXCLUDED.cooldown_until,
                     last_run_id=EXCLUDED.last_run_id,updated_at=now()
                   WHERE factory_model_health.last_run_id <= EXCLUDED.last_run_id
                     AND (factory_model_health.status!='retired'
                          OR EXCLUDED.status='retired')""",
                repo, failed_source, failed_model, failed_status, failed_outcome,
                failed_until, run_id,
            )
            # Apply the same source/model Fibonacci persistence used by the
            # terminal candidate. A later successful candidate cannot erase it.
            failed_key = f"factory-model-backoff-v1:{repo}:source:{failed_source}:model:{failed_model}"
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", failed_key,
            )
            previous = await conn.fetchrow(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1 FOR UPDATE",
                failed_key,
            )
            prior_payload = previous["payload"] if previous else None
            if isinstance(prior_payload, str):
                prior_payload = json.loads(prior_payload)
            prior_state = prior_payload[0] if isinstance(prior_payload, list) and prior_payload else {}
            prior_run = int(prior_state.get("run_id") or 0) if isinstance(prior_state, dict) else 0
            if prior_run <= run_id:
                streak = (int(prior_state.get("streak") or 0) if previous
                          else await _recent_streak(conn, run_id, failed_source, failed_model, repo=repo)) + 1
                suspended = False
                retry_at = fibonacci_retry_at(observed, streak).isoformat()
                await conn.execute(
                    """INSERT INTO public.factory_state_kv
                       (cache_key,payload,fetched_at,expires_at,updated_at)
                       VALUES($1,$2::jsonb,now(),LEAST(GREATEST(now(),($2::jsonb->0->>'next_retry_at')::timestamptz)+interval '90 days',timestamptz '9999-12-31 23:59:59+00'),now())
                       ON CONFLICT(cache_key) DO UPDATE SET
                         payload=EXCLUDED.payload,fetched_at=EXCLUDED.fetched_at,
                         expires_at=EXCLUDED.expires_at,updated_at=EXCLUDED.updated_at""",
                    failed_key, json.dumps([{
                        "source": failed_source, "model": failed_model,
                        "outcome": failed_outcome, "streak": streak,
                        "run_id": run_id, "suspended": suspended,
                        "next_retry_at": retry_at,
                    }]),
                )
        # Provider success clears a transient breaker only if no later breaker
        # was recorded. Another concurrent worker may still have hit a limit.
        if attributed and proven and classification not in TRANSIENT and classification != "no_work":
            await conn.execute(
                """UPDATE public.factory_provider_budget SET cooldown_until=NULL,
                   last_outcome=$3,last_run_id=$4,updated_at=now()
                   WHERE repo=$1 AND source=$2 AND last_run_id <= $4""",
                repo, source, classification, run_id)
            await conn.execute(
                """DELETE FROM public.factory_state_kv WHERE cache_key=$1
                   AND coalesce((payload->0->>'run_id')::bigint,0) <= $2""",
                f"factory-provider-backoff-v1:{repo}:source:{source}", run_id)
        # Each worker pins one model, so this breaker is model-local and does
        # not punish other models from the same provider. Retain streak across
        # retry windows; a successful verified session resets it.
        worker_key = f"factory-worker-backoff-v1:{repo}:worker:{worker}"
        if classification in WORKER_RETRY_CLASSES and not delivered:
            previous_worker = await conn.fetchrow(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1 FOR UPDATE",
                worker_key,
            )
            prior = previous_worker["payload"] if previous_worker else None
            if isinstance(prior, str):
                prior = json.loads(prior)
            old = prior[0] if isinstance(prior, list) and prior else {}
            if not isinstance(old, dict):
                old = {}
            prior_run = int(old.get("run_id") or 0)
            if prior_run <= run_id:
                catalog_slot = roster[worker][1] == "" or roster[worker][1] == "@catalog"
                same_pin = old.get("source") == source and (
                    catalog_slot or old.get("model") == model
                )
                streak = (int(old.get("streak") or 0) if same_pin else
                          await _recent_streak(
                              conn, run_id, source, model, repo=repo, worker=worker,
                              model_independent=catalog_slot,
                          )) + 1
                suspended = False
                retry_at = fibonacci_retry_at(observed, streak).isoformat()
                await conn.execute(
                    """INSERT INTO public.factory_state_kv
                         (cache_key,payload,fetched_at,expires_at,updated_at)
                       VALUES($1,$2::jsonb,now(),LEAST(GREATEST(now(),($2::jsonb->0->>'next_retry_at')::timestamptz)+interval '90 days',timestamptz '9999-12-31 23:59:59+00'),now())
                       ON CONFLICT(cache_key) DO UPDATE SET
                         payload=EXCLUDED.payload,fetched_at=EXCLUDED.fetched_at,
                         expires_at=EXCLUDED.expires_at,updated_at=EXCLUDED.updated_at""",
                    worker_key, json.dumps([{
                        "source": source, "model": model, "outcome": classification,
                        "streak": streak, "run_id": run_id,
                        "suspended": suspended, "next_retry_at": retry_at,
                    }]),
                )
        elif delivered or (proven and classification in {"success", "no_work"}):
            await conn.execute(
                """DELETE FROM public.factory_state_kv WHERE cache_key=$1
                   AND coalesce((payload->0->>'run_id')::bigint,0) <= $2""",
                worker_key, run_id,
            )
        # In addition to worker-local faults, quarantine the *source/model*
        # across all workers pinning it. A code/test failure is attributable
        # only when the exact model actually ran. Never blame the model for
        # checkout, credential, or controller failures.
        model_key = f"factory-model-backoff-v1:{repo}:source:{source}:model:{model}"
        if (attributed and not delivered
                and attributable_model_failure(classification, proven)):
            # Serialize first updates too, when no KV row exists to lock yet.
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", model_key,
            )
            previous_model = await conn.fetchrow(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1 FOR UPDATE",
                model_key,
            )
            prior_model = previous_model["payload"] if previous_model else None
            if isinstance(prior_model, str):
                prior_model = json.loads(prior_model)
            model_old = prior_model[0] if isinstance(prior_model, list) and prior_model else {}
            if not isinstance(model_old, dict):
                model_old = {}
            prior_model_run = int(model_old.get("run_id") or 0)
            if prior_model_run <= run_id:
                prior_streak = (
                    int(model_old.get("streak") or 0) if previous_model else
                    await _recent_streak(conn, run_id, source, model, repo=repo)
                )
                streak = prior_streak + 1
                suspended = False
                retry_at = fibonacci_retry_at(observed, streak).isoformat()
                await conn.execute(
                    """INSERT INTO public.factory_state_kv
                         (cache_key,payload,fetched_at,expires_at,updated_at)
                       VALUES($1,$2::jsonb,now(),LEAST(GREATEST(now(),($2::jsonb->0->>'next_retry_at')::timestamptz)+interval '90 days',timestamptz '9999-12-31 23:59:59+00'),now())
                       ON CONFLICT(cache_key) DO UPDATE SET
                         payload=EXCLUDED.payload,fetched_at=EXCLUDED.fetched_at,
                         expires_at=EXCLUDED.expires_at,updated_at=EXCLUDED.updated_at""",
                    model_key, json.dumps([{
                        "source": source, "model": model, "outcome": classification,
                        "streak": streak, "run_id": run_id,
                        "suspended": suspended, "next_retry_at": retry_at,
                    }]),
                )
        elif attributed and (delivered or (proven and classification in {"success", "no_work"})):
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", model_key,
            )
            await conn.execute(
                """DELETE FROM public.factory_state_kv WHERE cache_key=$1
                   AND coalesce((payload->0->>'run_id')::bigint,0) <= $2""",
                model_key, run_id,
            )
        kind = str(outcome.get("selected_kind") or "")
        number = str(outcome.get("selected_number") or "")
        if kind == "pr" and number.isdigit() and not delivered and classification in {"work_failure","agent_timeout","unknown_failure","environment_failure"}:
            key = f"factory-projection-v1:{repo}:pr:{int(number)}"
            record = await conn.fetchval(
                "SELECT payload->0->'item' FROM public.factory_state_kv WHERE cache_key=$1", key)
            if isinstance(record, str):
                record = json.loads(record)
            head = str((record or {}).get("headRefOid") or (record or {}).get("head_sha") or "")
            labels = (record or {}).get("labels") or []
            names = {v.get("name") if isinstance(v,dict) else str(v) for v in labels}
            stage = next((s for s in ("factory:changes-requested","factory:ci","factory:review")
                          if s in names), "factory:review")
            if HEAD_RE.fullmatch(head):
                seconds = 120 if classification == "agent_timeout" else 180 if classification == "work_failure" else 300 if classification == "unknown_failure" else 600
                await conn.execute(
                    """INSERT INTO public.factory_pr_stage_retry
                         (repo,pr_number,head_sha,stage,attempts,outcome,retry_after,last_run_id)
                       VALUES($1,$2,$3,$4,1,$5,now()+$6::int * interval '1 second',$7)
                       ON CONFLICT(repo,pr_number,head_sha,stage) DO UPDATE SET
                         attempts=factory_pr_stage_retry.attempts+1,outcome=EXCLUDED.outcome,
                         retry_after=now() + least(3600,
                           $6::int * (1 << least(factory_pr_stage_retry.attempts, 5)))
                           * interval '1 second',
                         last_run_id=greatest(factory_pr_stage_retry.last_run_id,EXCLUDED.last_run_id),
                         updated_at=now()
                       WHERE factory_pr_stage_retry.last_run_id < EXCLUDED.last_run_id""",
                    repo, int(number), head, stage, classification, seconds, run_id)
    return True


def record_runtime(repo: str, run_id: int, outcome: dict[str, Any]) -> bool:
    if not enabled():
        raise RuntimeError("trusted runtime requires restricted Neon connection")
    return _run(project_runtime, repo, run_id, outcome)


async def _eligibility(conn: Any, repo: str, workers: list[str]) -> tuple[list[str],list[str]]:
    roster = _roster()
    rows = await conn.fetch(
        """SELECT source,model,status,cooldown_until FROM public.factory_model_health WHERE repo=$1""",repo)
    models = {(r["source"],r["model"]):r for r in rows}
    budgets = {r["source"]:r for r in await conn.fetch(
        "SELECT * FROM public.factory_provider_budget WHERE repo=$1",repo)}
    counts = {r["source"]:r["n"] for r in await conn.fetch(
        """SELECT source,count(*) n FROM public.factory_provider_reservation
           WHERE repo=$1 AND expires_at>now() GROUP BY source""",repo)}
    now = datetime.now(timezone.utc)
    backoffs = {
        str(r["cache_key"]).rsplit(":", 1)[-1]: r
        for r in await conn.fetch(
            """SELECT cache_key,payload,expires_at FROM public.factory_state_kv
               WHERE cache_key LIKE $1 AND expires_at>now()""",
            f"factory-worker-backoff-v1:{repo}:worker:%")
    }
    # Only a proven account-scoped failure can stop a provider's other models.
    # Historical model-only quota records in provider_budget are insufficient.
    provider_backoffs: dict[str, Any] = {}
    for row in await conn.fetch(
        """SELECT cache_key,payload,expires_at FROM public.factory_state_kv
           WHERE cache_key LIKE $1 AND expires_at>now()""",
        f"factory-provider-backoff-v1:{repo}:source:%"
    ):
        key = str(row["cache_key"])
        prefix = f"factory-provider-backoff-v1:{repo}:source:"
        if not key.startswith(prefix):
            continue
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        state = payload[0] if isinstance(payload, list) and payload else {}
        if isinstance(state, dict) and state.get("outcome") in {"provider_quota", "provider_failure"}:
            provider_backoffs[key.removeprefix(prefix)] = state
    model_backoffs = {}
    for row in await conn.fetch(
        """SELECT cache_key,payload,expires_at FROM public.factory_state_kv
           WHERE cache_key LIKE $1 AND expires_at>now()""",
        f"factory-model-backoff-v1:{repo}:source:%"
    ):
        state = row["payload"]
        if isinstance(state, str):
            state = json.loads(state)
        state = state[0] if isinstance(state, list) and state else {}
        if isinstance(state, dict) and state.get("source") and state.get("model"):
            model_backoffs[(state["source"], state["model"])] = row
    kept, rejected = [], []
    for worker in workers:
        pair = roster.get(worker)
        if pair is None:
            rejected.append(f"{worker}: missing trusted roster entry")
            continue
        source,model = pair
        m,b = models.get(pair),budgets.get(source)
        retry_reason = worker_backoff_reason(backoffs.get(worker), source, model, now)
        # Inference cooldown lives on the exact source/model or provider account.
        # Older worker-scoped retry records must not suppress other fleet models.
        worker_record = backoffs.get(worker)
        if worker_record:
            raw = worker_record["payload"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            worker_state = raw[0] if isinstance(raw, list) and raw else {}
            if isinstance(worker_state, dict) and worker_state.get("outcome") in (
                MODEL_RETRY_CLASSES | {"model_retired_410"}
            ):
                retry_reason = None
        model_retry = worker_backoff_reason(model_backoffs.get(pair), source, model, now)
        if source in provider_backoffs:
            rejected.append(f"{worker}: provider account {source} cooling")
        elif retry_reason:
            rejected.append(f"{worker}: {retry_reason}")
        elif model_retry:
            rejected.append(f"{worker}: source/model {model} {model_retry}")
        elif m and (m["status"]=="retired" or (m["status"] in {"unavailable","cooling"}
                    and m["cooldown_until"] and m["cooldown_until"]>now)):
            rejected.append(f"{worker}: model {model} {m['status']}")
        elif b and b["concurrency_limit"] is not None and counts.get(source,0)>=b["concurrency_limit"]:
            rejected.append(f"{worker}: provider {source} full")
        elif b and b["daily_limit"] is not None and b["usage_day"]==now.date() and b["daily_used"]>=b["daily_limit"]:
            rejected.append(f"{worker}: provider {source} daily budget exhausted")
        else:
            kept.append(worker)
    return kept, rejected


def filter_workers(repo: str, workers: list[str]) -> tuple[list[str],list[str]]:
    if not enabled():
        return workers, ["Neon unavailable; existing lease checks still required"]
    return _run(_eligibility,repo,workers)


async def reserve_capacity(conn: Any, repo: str, worker: str, claim_id: str, ttl: int) -> bool:
    """Called inside the existing exclusive repo lease transaction."""
    roster=_roster()
    if worker not in roster:
        return False
    kept,_=await _eligibility(conn,repo,[worker])
    if not kept:
        return False
    source, _ = roster[worker]
    # The checked-in source policy remains authoritative; never invent a quota
    # for unmetered providers. Check it under the same repo advisory lock.
    from factory_source_budget import load_budgets
    from factory_source_budget import PROTECTED_WORKERS
    cap=None if int(worker) in PROTECTED_WORKERS else load_budgets().get(source)
    if cap is not None and (await _daily_usage(conn,repo,[source]))[source] >= cap:
        return False
    row = await conn.fetchrow(
        "SELECT daily_limit,usage_day,daily_used FROM public.factory_provider_budget WHERE repo=$1 AND source=$2 FOR UPDATE",
        repo,source)
    if row and row["daily_limit"] is not None and row["usage_day"]==datetime.now(timezone.utc).date() and row["daily_used"]>=row["daily_limit"]:
        return False
    await conn.execute(
        """INSERT INTO public.factory_provider_reservation
             (claim_id,repo,worker,source,expires_at)
           VALUES($1::uuid,$2,$3,$4,now()+$5::int * interval '1 second')""",
        claim_id,repo,worker,source,ttl)
    await conn.execute(
        """INSERT INTO public.factory_provider_budget(repo,source,usage_day,daily_used)
           VALUES($1,$2,(now() AT TIME ZONE 'UTC')::date,1)
           ON CONFLICT(repo,source) DO UPDATE SET
            daily_used=CASE WHEN factory_provider_budget.usage_day=(now() AT TIME ZONE 'UTC')::date
                            THEN factory_provider_budget.daily_used+1 ELSE 1 END,
            usage_day=(now() AT TIME ZONE 'UTC')::date,updated_at=now()""",repo,source)
    return True


async def release_capacity(conn: Any, claim_id: str) -> None:
    await conn.execute("DELETE FROM public.factory_provider_reservation WHERE claim_id=$1::uuid",claim_id)


async def _daily_usage(conn: Any, repo: str, sources: list[str]) -> dict[str,int]:
    # Assignments are committed in the SAME transaction as capacity claims,
    # and carry verified worker IDs. This avoids an Actions-wide daily scan.
    since=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    rows=await conn.fetch(
        """SELECT detail->>'worker' AS worker, detail->>'source' AS source
           FROM public.factory_state_events
           WHERE event_type='worker_assigned' AND created_at >= $1""",since)
    roster=_roster()
    counts=dict.fromkeys(sources, 0)
    for row in rows:
        pair=roster.get(str(row["worker"] or ""))
        source=row["source"] if row["source"] else (pair[0] if pair else None)
        if source in counts:
            counts[source]+=1
    return counts


def daily_usage(repo: str, sources: list[str]) -> dict[str,int] | None:
    if not enabled():
        return None
    return _run(_daily_usage,repo,sources)


async def _seed_daily(conn: Any, repo: str, counts: dict[str,int]) -> None:
    for source,count in counts.items():
        if count<0:
            raise ValueError("negative usage")
        await conn.execute(
            """INSERT INTO public.factory_provider_budget(repo,source,usage_day,daily_used)
               VALUES($1,$2,(now() AT TIME ZONE 'UTC')::date,$3)
               ON CONFLICT(repo,source) DO UPDATE SET
                 usage_day=EXCLUDED.usage_day,daily_used=EXCLUDED.daily_used,updated_at=now()
               WHERE factory_provider_budget.usage_day <> EXCLUDED.usage_day""",repo,source,count)


def seed_daily(repo: str, counts: dict[str,int]) -> None:
    if enabled():
        _run(_seed_daily,repo,counts)


async def _cooling_prs(conn: Any, repo: str, heads: dict[int,tuple[str,str]]) -> set[int]:
    rows=await conn.fetch(
        """SELECT pr_number,head_sha,stage FROM public.factory_pr_stage_retry
           WHERE repo=$1 AND retry_after>now()""",repo)
    return {int(r["pr_number"]) for r in rows
            if heads.get(int(r["pr_number"]))==(r["head_sha"],r["stage"])}


def cooling_prs(repo: str, heads: dict[int,tuple[str,str]]) -> set[int]:
    return _run(_cooling_prs,repo,heads) if enabled() and heads else set()


async def _ingest_check(conn: Any, repo: str, event: dict[str, Any]) -> int:
    """Record only failed-review cooldowns, never an advisory check snapshot."""
    run = event.get("workflow_run") or {}
    sha = str(run.get("head_sha") or "")
    name = str(run.get("name") or "")
    try:
        number = int(run.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    if (
        not HEAD_RE.fullmatch(sha)
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or name not in {"PR-Agent Advisory Review", "Local Ollama Review"}
        or number <= 0
    ):
        return 0

    recorded = 0
    for pr in run.get("pull_requests") or []:
        pr_num = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(pr_num, int) or isinstance(pr_num, bool) or pr_num <= 0:
            continue
        changed = await conn.fetchval(
            """INSERT INTO public.factory_pr_stage_retry
                 (repo,pr_number,head_sha,stage,attempts,outcome,retry_after,last_run_id)
               VALUES($1,$2,$3,'factory:review',1,'review_transport_failure',
                      now()+interval '5 minutes',$4)
               ON CONFLICT(repo,pr_number,head_sha,stage) DO UPDATE SET
                 attempts=factory_pr_stage_retry.attempts+1,
                 outcome=EXCLUDED.outcome,retry_after=EXCLUDED.retry_after,
                 last_run_id=EXCLUDED.last_run_id,updated_at=now()
               WHERE factory_pr_stage_retry.last_run_id<EXCLUDED.last_run_id
               RETURNING 1""",
            repo, pr_num, sha, number,
        )
        recorded += bool(changed)
    return recorded


def ingest_check(repo: str, event: dict[str, Any]) -> int:
    if not enabled():
        raise RuntimeError("review cooldown ingestion requires restricted Neon connection")
    return _run(_ingest_check, repo, event)


async def _checkpoint(conn: Any, repo: str, scope: str, cursor: str, changes: int) -> None:
    await conn.execute(
        """INSERT INTO public.factory_reconciliation_cursor
             (repo,scope,checkpoint,changed_count)
           VALUES($1,$2,$3,$4)
           ON CONFLICT(repo,scope) DO UPDATE SET checkpoint=EXCLUDED.checkpoint,
             changed_count=EXCLUDED.changed_count,checked_at=now()""",
        repo,scope,cursor,changes)


def checkpoint(repo: str, scope: str, cursor: str, changes: int=0) -> None:
    if enabled():
        _run(_checkpoint,repo,scope,cursor,changes)


async def _stats(conn:Any,repo:str)->dict[str,Any]:
    rows=await conn.fetch(
        """SELECT worker,source,model,attempts,productive,completed,failures
           FROM public.factory_worker_metrics WHERE repo=$1 ORDER BY productive DESC,attempts DESC LIMIT 100""",repo)
    return {"workers":[dict(r) for r in rows],
            "cooling": [dict(r) for r in await conn.fetch(
                """SELECT source,model,status,cooldown_until FROM public.factory_model_health
                   WHERE repo=$1 AND cooldown_until>now() ORDER BY cooldown_until""",repo)]}




async def _sync_catalog(conn:Any,repo:str,report:dict[str,Any])->int:
    """Record trusted catalog absence without editing the checked-in roster."""
    count=0
    for item in report.get("retirements") or []:
        if not isinstance(item,dict):
            continue
        source,model=str(item.get("source") or ""),str(item.get("model") or "")
        if not source or not model:
            continue
        reason=str(item.get("reason") or "")
        retired="410" in reason or "permanently retired" in reason
        await conn.execute(
            """INSERT INTO public.factory_model_health
                (repo,source,model,status,last_outcome,cooldown_until)
               VALUES($1,$2,$3,$4,$5,$6)
               ON CONFLICT(repo,source,model) DO UPDATE SET
                 status=EXCLUDED.status,last_outcome=EXCLUDED.last_outcome,
                 cooldown_until=EXCLUDED.cooldown_until,updated_at=now()
               WHERE factory_model_health.status!='retired'""",
            repo,source,model,"retired" if retired else "unavailable",
            "model_retired_410" if retired else "model_unavailable",
            None if retired else datetime.now(timezone.utc)+timedelta(hours=6))
        count+=1
    # A later successful catalog read proves availability, not invocation.
    # Clear an expired absence without claiming the model executed correctly.
    for item in report.get("kept") or []:
        if not isinstance(item,dict):
            continue
        source,model=str(item.get("source") or ""),str(item.get("model") or "")
        if source and model:
            await conn.execute(
                """UPDATE public.factory_model_health SET status='unknown',
                   cooldown_until=NULL,updated_at=now()
                   WHERE repo=$1 AND source=$2 AND model=$3 AND status='unavailable'
                     AND last_outcome='model_unavailable'""",repo,source,model)
    return count


def sync_catalog(repo:str,report:dict[str,Any])->int:
    return _run(_sync_catalog,repo,report) if enabled() else 0


async def _rank_workers(conn:Any,repo:str,workers:list[str])->list[str]:
    roster=_roster()
    rows=await conn.fetch(
        """SELECT worker,sum(attempts)::int AS attempts,sum(productive)::int AS productive
           FROM public.factory_worker_metrics WHERE repo=$1 GROUP BY worker""",repo)
    scores={str(r["worker"]):(int(r["productive"]),int(r["attempts"])) for r in rows}
    groups:dict[str,list[tuple[int,str]]]={}
    for index,w in enumerate(workers):
        source=roster.get(w,("unknown",""))[0]
        groups.setdefault(source,[]).append((index,w))
    for entries in groups.values():
        # Keep the source-diverse cadence/slot positions. Order ONLY within
        # each source, with unsampled workers remaining neutral.
        sorted_workers=sorted((w for _,w in entries),key=lambda w:(
            -((scores[w][0]/scores[w][1]) if w in scores and scores[w][1]>=3 else 0.20),
            w))
        for (position,_),replacement in zip(entries,sorted_workers,strict=True):
            workers[position]=replacement
    return workers


def rank_workers(repo:str,workers:list[str])->list[str]:
    return _run(_rank_workers,repo,workers) if enabled() else workers



async def _claim_wakeup(conn: Any, repo: str, scope: str, interval_seconds: int) -> bool:
    # Every event worker races on one row, no additional GitHub-wide polling.
    row=await conn.fetchval(
        """INSERT INTO public.factory_reconciliation_cursor(repo,scope,checkpoint)
           VALUES($1,$2,'event-wakeup')
           ON CONFLICT(repo,scope) DO UPDATE SET
             checked_at=now(),checkpoint=EXCLUDED.checkpoint
           WHERE factory_reconciliation_cursor.checked_at
               < now()-$3::int * interval '1 second'
           RETURNING scope""",repo,scope,interval_seconds)
    return row is not None


def claim_wakeup(repo: str, scope: str, interval_seconds: int=120) -> bool:
    return _run(_claim_wakeup,repo,scope,interval_seconds) if enabled() else False

async def _export_model_exclusions(conn: Any, repo: str, worker: str = "") -> dict[str, list[str]]:
    """Trusted read-only snapshot for credential-isolated factory workers."""
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
        """SELECT payload FROM public.factory_state_kv
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
        try:
            retry_at = datetime.fromisoformat(str(state.get("next_retry_at")))
        except (ValueError, TypeError):
            retry_at = None
        if retry_at is not None and retry_at > now:
            if model not in excluded.setdefault(source, []):
                excluded[source].append(model)
    # The worker identity is validated for the trusted caller; issue #1093
    # comment locators are retired and never exported to the runner.
    if worker and (not worker.isdecimal() or worker not in _roster()):
        raise ValueError("invalid trusted worker for model exclusions")
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


def export_dashboard_retry(repo: str) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("Factory retry evidence is not connected")
    return _run(_export_dashboard_retry, repo)


def export_candidate_health(repo: str) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("Neon health unavailable; cannot prove executable candidates")
    return _run(_export_candidate_health, repo)


async def _export_fleet_failover_sources(conn: Any, repo: str, worker: str) -> list[str]:
    """Admit sources that cannot bypass a configured provider capacity limit.

    The assigned source already owns capacity under the target's exclusive
    claim. Other sources without concurrency/daily limits may be used without
    taking a second reservation. Capped sources require an atomic transfer
    before they can become candidates, so they are excluded here.
    """
    from factory_provider_registry import PROVIDERS
    from factory_source_budget import load_budgets

    roster = _roster()
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
    sources = set(PROVIDERS) | {"opencode-free", "openrouter-free", "kilo-auto"}
    selected = {assigned}
    for source in sources - {assigned}:
        budget = budgets.get(source)
        if source in caps:
            continue
        if budget is not None:
            if budget["concurrency_limit"] is not None or budget["daily_limit"] is not None:
                continue
            # No provider-wide cooldown: failing models back off individually.
        selected.add(source)
    return sorted(selected)


def export_fleet_failover_sources(repo: str, worker: str) -> list[str]:
    if not enabled():
        raise RuntimeError("Neon capacity read required for fleet failover")
    return _run(_export_fleet_failover_sources, repo, worker)



def export_model_exclusions(repo: str, worker: str = "") -> dict[str, list[str]]:
    if not enabled():
        raise RuntimeError("Neon is required for model health snapshot")
    return _run(_export_model_exclusions, repo, worker)


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
                    OR starts_with(cache_key,$3) RETURNING 1)
               SELECT count(*) FROM deleted""",
            f"factory-model-backoff-v1:{repo}:",
            f"factory-worker-backoff-v1:{repo}:",
            f"factory-provider-backoff-v1:{repo}:")
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


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("bootstrap","filter","rank","stats","ingest-check","checkpoint","sync-catalog","export-model-exclusions","export-candidate-health","export-fleet-failover-sources","reset-model-health"))
    parser.add_argument("--repo",default=os.environ.get("GITHUB_REPOSITORY","JoshCLWren/comic-pile"))
    parser.add_argument("--file",type=Path)
    parser.add_argument("--scope")
    parser.add_argument("--worker",default="")
    parser.add_argument("--cursor",default="")
    parser.add_argument("--include-retired",action="store_true")
    args=parser.parse_args()
    if not REPO_RE.fullmatch(args.repo):
        parser.error("invalid repo")
    if args.command=="reset-model-health":
        if not enabled():
            raise SystemExit("Neon is required for model health reset")
        print(json.dumps(_run(_reset_model_health,args.repo,args.include_retired),sort_keys=True))
    elif args.command=="bootstrap":
        if not enabled():
            raise SystemExit("Neon is required for factory operations")
        _run(lambda conn: conn.fetchval("SELECT 1"))
        print("[factory-operations] schema ready")
    elif args.command=="export-fleet-failover-sources":
        print(json.dumps(export_fleet_failover_sources(args.repo,args.worker),separators=(",",":")))
    elif args.command=="export-model-exclusions":
        print(json.dumps(export_model_exclusions(args.repo,args.worker),sort_keys=True,separators=(",",":")))
    elif args.command=="export-candidate-health":
        print(json.dumps(export_candidate_health(args.repo),sort_keys=True,separators=(",",":")))
    elif args.command=="filter":
        workers=[str(w) for w in json.load(sys.stdin)]
        kept,denied=filter_workers(args.repo,workers)
        for reason in denied:
            print(f"[factory-operations] {reason}",file=sys.stderr)
        print(json.dumps(kept,separators=(",",":")))
    elif args.command=="rank":
        print(json.dumps(rank_workers(args.repo,[str(w) for w in json.load(sys.stdin)]),
                         separators=(",",":")))
    elif args.command=="sync-catalog":
        if args.file is None:
            parser.error("--file required")
        print(json.dumps({"missing_models":sync_catalog(args.repo,json.loads(args.file.read_text()))}))
    elif args.command=="stats":
        print(json.dumps(_run(_stats,args.repo),default=str))
    elif args.command=="ingest-check":
        if args.file is None:
            parser.error("--file required")
        recorded=ingest_check(args.repo,json.loads(args.file.read_text()))
        print(json.dumps({"recorded_review_failures":recorded}))
    elif args.command=="checkpoint":
        if not args.scope:
            parser.error("--scope required")
        checkpoint(args.repo,args.scope,args.cursor)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
