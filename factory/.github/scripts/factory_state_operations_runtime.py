#!/usr/bin/env python3
"""Project one trusted runtime outcome into model, provider, retry and worker state."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import factory_state_operations_core as core
from factory_state_operations_backoff import (
    MODEL_COOLDOWNS,
    TRANSIENT,
    WORKER_RETRY_CLASSES,
    _recent_streak,
    attributable_model_failure,
    fibonacci_retry_at,
    fibonacci_worker_delay,
    model_scoped_transient,
    validate_intermediate_model_failures,
)
from factory_state_operations_core import HEAD_RE, REPO_RE


async def project_runtime(conn: Any, repo: str, run_id: int, outcome: dict[str, Any]) -> bool:
    """Idempotently update model, provider, retry and worker metrics projections."""
    worker = str(outcome.get("worker") or "")
    roster = core._roster()
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
        for failed_source, failed_model, failed_outcome, failed_scope in model_failures:
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
                        "outcome": failed_outcome, "scope": failed_scope, "streak": streak,
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
