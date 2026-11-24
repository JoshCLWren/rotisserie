#!/usr/bin/env python3
"""Dispatch eligibility, provider capacity, PR retry cooldowns and catalog projections."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import factory_state_operations_core as core
from factory_state_operations_backoff import MODEL_RETRY_CLASSES, worker_backoff_reason
from factory_state_operations_core import HEAD_RE


async def _eligibility(conn: Any, repo: str, workers: list[str]) -> tuple[list[str],list[str]]:
    roster = core._roster()
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
        if isinstance(state, dict) and state.get("outcome") in {"provider_quota", "provider_failure", "provider_throttle"}:
            provider_backoffs[key.removeprefix(prefix)] = state
    model_backoffs = {}
    for row in await conn.fetch(
        """SELECT cache_key,payload,expires_at,updated_at FROM public.factory_state_kv
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


async def reserve_capacity(conn: Any, repo: str, worker: str, claim_id: str, ttl: int) -> bool:
    """Called inside the existing exclusive repo lease transaction."""
    roster=core._roster()
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
    roster=core._roster()
    counts=dict.fromkeys(sources, 0)
    for row in rows:
        pair=roster.get(str(row["worker"] or ""))
        source=row["source"] if row["source"] else (pair[0] if pair else None)
        if source in counts:
            counts[source]+=1
    return counts


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


REVIEW_ABORT_LIMIT = 2
REVIEW_ABORT_LOOKBACK_SECONDS = 24 * 3600
REVIEW_ABORT_MAX_COOLDOWN_SECONDS = 6 * 3600


async def _cooling_prs(conn: Any, repo: str, heads: dict[int,tuple[str,str]]) -> set[int]:
    rows=await conn.fetch(
        """SELECT pr_number,head_sha,stage FROM public.factory_pr_stage_retry
           WHERE repo=$1 AND retry_after>now()""",repo)
    cooling = {int(r["pr_number"]) for r in rows
               if heads.get(int(r["pr_number"]))==(r["head_sha"],r["stage"])}
    return cooling | await _review_churn(conn, repo, heads)


async def _review_churn(conn: Any, repo: str, heads: dict[int, tuple[str, str]]) -> set[int]:
    """Review PRs whose leases keep ending before a session starts.

    A review whose every eligible model authored the PR aborts before the
    session and releases the lease, so the PR was re-dispatched every few
    minutes (#5688: 37 times overnight). From the REVIEW_ABORT_LIMIT-th abort
    in the lookback, the PR waits FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS times
    (aborts - 1) after its latest abort, up to six hours.
    """
    review = {number for number, (_, stage) in heads.items() if stage == "factory:review"}
    if not review:
        return set()
    from factory_settings import integer

    window = integer("FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS", 1800)
    counts: dict[int, int] = {}
    latest: dict[int, datetime] = {}
    for row in await conn.fetch(
        """SELECT detail->'targets' AS targets, created_at FROM public.factory_state_events
           WHERE event_type='worker_released' AND detail->>'repo'=$1
             AND starts_with(detail->>'reason','pre-session-abort')
             AND created_at > now() - $2::int * interval '1 second'""",
        repo, REVIEW_ABORT_LOOKBACK_SECONDS,
    ):
        targets = row["targets"]
        if isinstance(targets, str):
            targets = json.loads(targets)
        for item in targets if isinstance(targets, list) else []:
            number = int(item) if str(item).isdecimal() else None
            if number in review:
                counts[number] = counts.get(number, 0) + 1
                latest[number] = max(latest.get(number, row["created_at"]), row["created_at"])
    now = datetime.now(timezone.utc)
    return {
        number for number, count in counts.items()
        if count >= REVIEW_ABORT_LIMIT and now < latest[number] + timedelta(
            seconds=min(window * (count - 1), REVIEW_ABORT_MAX_COOLDOWN_SECONDS))
    }


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


async def _checkpoint(conn: Any, repo: str, scope: str, cursor: str, changes: int) -> None:
    await conn.execute(
        """INSERT INTO public.factory_reconciliation_cursor
             (repo,scope,checkpoint,changed_count)
           VALUES($1,$2,$3,$4)
           ON CONFLICT(repo,scope) DO UPDATE SET checkpoint=EXCLUDED.checkpoint,
             changed_count=EXCLUDED.changed_count,checked_at=now()""",
        repo,scope,cursor,changes)


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


async def _rank_workers(conn:Any,repo:str,workers:list[str])->list[str]:
    roster=core._roster()
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
