"""Idempotent trusted lifecycle events and the trusted job-log outcome refinements."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn
from factory_work_policy import FIXED_LEASE_TTL_SECONDS
from factory_state_ledger_core import LEASE_PREFIX, _keys, enabled


async def _events(repo: str, entries: list[dict[str, Any]]) -> int:

    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    inserted = 0
    # Large failover sessions can contain hundreds of attempts. Batch their
    # immutable observations instead of making two Neon round trips per model.
    model_attempts = [entry for entry in entries if entry["event_type"] == "model_attempt"]
    lifecycle = [entry for entry in entries if entry["event_type"] != "model_attempt"]
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            for entry in lifecycle:
                existing = await conn.fetchval(
                    """SELECT 1 FROM public.factory_state_events
                       WHERE source='github-actions'
                         AND detail->>'event_id'=$1 LIMIT 1""",
                    entry["event_id"],
                )
                if existing:
                    continue
                await conn.execute(
                    """INSERT INTO public.factory_state_events
                       (source,event_type,subject_type,subject_number,detail)
                       VALUES ('github-actions',$1,'worker',$2,$3::jsonb)""",
                    entry["event_type"], int(entry["worker"]), json.dumps({**entry, "repo": repo}),
                )
                inserted += 1
                # 'running' is an initial status marker, not a periodic
                # liveness check. A terminal success/failure never renews.
                if entry["event_type"] == "worker_started":
                    worker_key = _keys(repo, entry["worker"], [])[0]
                    row = await conn.fetchrow(
                        """SELECT payload FROM public.factory_state_kv
                           WHERE cache_key=$1 AND expires_at>now()""",
                        worker_key,
                    )
                    if row:
                        payload = row["payload"]
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        token = payload[0]["claim_id"]
                        await conn.execute(
                            """UPDATE public.factory_provider_reservation
                               SET expires_at=greatest(expires_at,now()+$2::int * interval '1 second')
                               WHERE claim_id=$1::uuid""", token, FIXED_LEASE_TTL_SECONDS,
                        )
                        await conn.execute(
                            """UPDATE public.factory_state_kv SET
                                 expires_at=greatest(expires_at,now()+$3::int * interval '1 second'),
                                 updated_at=now()
                               WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                            worker_key, token, FIXED_LEASE_TTL_SECONDS,
                        )
                        await conn.execute(
                            """UPDATE public.factory_state_kv SET
                                 expires_at=greatest(expires_at,now()+$3::int * interval '1 second'),
                                 updated_at=now()
                               WHERE cache_key LIKE $1
                                 AND payload->0->>'claim_id'=$2""",
                            f"{LEASE_PREFIX}{repo}:target:%", token, FIXED_LEASE_TTL_SECONDS,
                        )
            if model_attempts:
                records = [{**entry, "repo": repo} for entry in model_attempts]
                event_ids = [entry["event_id"] for entry in records]
                added = await conn.fetch(
                    """WITH incoming AS (
                           SELECT value AS detail FROM jsonb_array_elements($1::jsonb)
                       ), already AS (
                           SELECT detail->>'event_id' AS event_id
                             FROM public.factory_state_events
                            WHERE source='github-actions'
                              AND detail->>'event_id' = ANY($2::text[])
                       )
                       INSERT INTO public.factory_state_events
                           (source,event_type,subject_type,subject_number,detail)
                       SELECT 'github-actions','model_attempt','worker',
                              (incoming.detail->>'worker')::bigint, incoming.detail
                         FROM incoming
                        WHERE NOT EXISTS (
                            SELECT 1 FROM already
                             WHERE already.event_id=incoming.detail->>'event_id'
                        )
                       RETURNING id""",
                    json.dumps(records), event_ids,
                )
                inserted += len(added)
        return inserted
    finally:
        await conn.close()


def recorded_runtime_runs(repo: str, run_ids: list[str]) -> set[str]:
    """Return the Entry run IDs that already have a terminal runtime outcome.

    The deferred outcome reconciler uses this to avoid re-projecting a run
    (model-health streaks are not idempotent per run).
    """
    if not enabled() or not run_ids:
        return set()
    if any(not run_id.isdecimal() for run_id in run_ids):
        raise ValueError("invalid run ID")

    async def _read() -> set[str]:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            rows = await conn.fetch(
                """SELECT detail->>'run_id' AS run_id FROM public.factory_state_events
                   WHERE source='github-actions' AND detail->>'repo'=$1
                     AND detail->>'event_id' = ANY($2::text[])""",
                repo, [f"runtime:{run_id}" for run_id in run_ids],
            )
            return {str(row["run_id"]) for row in rows}
        finally:
            await conn.close()

    return asyncio.run(_read())


def refine_trusted_dynamic_models(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Persist *all* exact free-model smoke failures, even after a later success.

    Only timestamped output of the executed Actions smoke step qualifies.
    The model runner cannot create privileged Neon records itself.
    """
    if (outcome.get("source") != "opencode-free"
            or outcome.get("persisted_work") not in (True, False)):
        return outcome
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
    failures: set[str] = set()
    successful = ""
    for step in re.split(r"##\[group\]Run ", clean):
        # The smoke command is a bounded pre-work invocation. Do not inspect
        # arbitrary later model-agent output for retirement evidence.
        if "Smoke candidate: opencode/" not in step:
            continue
        matches = list(re.finditer(
            r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Smoke candidate: opencode/([\w./:-]+)\s*$",
            step,
        ))
        for i, match in enumerate(matches):
            model = match.group(1)
            fragment = step[match.end(): matches[i+1].start() if i+1 < len(matches) else len(step)]
            if re.search(
                rf"\bModel (?:opencode/)?{re.escape(model)} has been deprecated\b",
                fragment, re.IGNORECASE,
            ) or re.search(
                rf"\bModel unavailable: opencode/{re.escape(model)}\b",
                fragment, re.IGNORECASE,
            ):
                failures.add(model)
        success = re.findall(
            r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Dynamic free-model smoke succeeded: opencode/([\w./:-]+)\s*$",
            step,
        )
        if success:
            successful = success[-1]
    if not failures and not successful:
        return outcome
    refined = dict(outcome)
    if failures:
        refined["model_failures"] = sorted(failures)[:5]
    if successful and outcome.get("exact_model_proven") is True:
        refined["model"] = successful
    return refined

def refine_trusted_failover_models(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Attest exact failed pairs, account scope and final provider transitions.

    Only the completed trusted Actions job log and verified worker marker
    qualify for durable health. A prior candidate can never classify the
    terminal successful model or overwrite its delivery state.
    """
    from factory_provider_candidates import ADAPTERS

    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
    worker = str(outcome.get("worker") or "")
    pattern = re.compile(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z "
        r"\[factory:(\d+)\]\[source:([^\]\n]+)\]\[model:[^\]\n]+\] "
        r"FACTORY_TRUSTED_FAILOVER: source=(\S+) model=([\w./:-]{1,180}) "
        r"outcome=([a-z_]+)(?: scope=(account|model))?\s*$"
    )
    accepted = {
        "provider_failure", "provider_throttle", "provider_quota",
        "model_unavailable", "model_retired_410", "model_policy_violation",
    }
    failures = list(outcome.get("model_failures") or [])
    seen = {
        (str(item.get("source") or outcome.get("source") or ""), item.get("model"))
        for item in failures if isinstance(item, dict)
    }
    for match in pattern.finditer(clean):
        log_worker, log_source, source, model, classification, scope = match.groups()
        if (log_worker != worker or log_source != source
                or source not in ADAPTERS or classification not in accepted
                or (source, model) in seen):
            continue
        if scope == "account" and classification not in {"provider_quota", "provider_failure"}:
            continue
        failures.append({
            "source": source, "model": model, "outcome": classification,
            "scope": scope or "model",
        })
        seen.add((source, model))

    # The model cannot choose an unregistered source merely by mentioning one
    # in its text. The canonical runtime source transition must appear in the
    # trusted, timestamped wrapper log before a final-source override.
    transitions = re.findall(
        r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z "
        r"\[factory:(\d+)\]\[source:([^\]\n]+)\]\[model:[^\]\n]+\] "
        r"FACTORY_FLEET_TRANSITION: source=([^\s]+) model=([^\s]+) ",
        clean,
    )
    final_source = str(outcome.get("source") or "")
    final_model = str(outcome.get("model") or "")
    trusted_switch = any(
        w == worker and log_source == source == final_source
        and model == final_model and source in ADAPTERS
        for w, log_source, source, model in transitions
    )
    result = dict(outcome)
    if failures:
        result["model_failures"] = failures
    if trusted_switch:
        result["trusted_source_switch"] = True
    return result



def refine_trusted_smoke_timeout(outcome: dict[str, Any], job_log: str) -> dict[str, Any]:
    """Attribute a 180-second pinned-model smoke timeout to the provider.

    Unlike a generic exit 124, this requires the actual Actions smoke step to
    end with exit 124. The already-classified trusted outcome must be unknown,
    pre-proof, and nonproductive. No model fault is inferred from shell source
    alone or from the runner's subsequent cleanup.
    """
    worker = re.escape(str(outcome.get("worker") or ""))
    # A wrapper-enforced execution deadline must not cool down the provider.
    # Other evidence can be present merely because the agent inspected files.
    if (worker and outcome.get("persisted_work") is not True
            and outcome.get("outcome_class") not in {
                "provider_quota", "model_unavailable", "model_retired_410",
                "model_policy_violation",
            } and re.search(
                rf"(?m)^\d{{4}}-\d{{2}}-\d{{2}}T\S+Z \[factory:{worker}\].*"
                r"agent exit status 124 for (?:issue|pr) #\d+\b",
                job_log,
            )):
        return {
            **outcome,
            "outcome": "AGENT DEADLINE",
            "outcome_class": "agent_timeout",
            "detail": "trusted worker hit its local execution deadline; no provider failure inferred",
        }
    if (
        outcome.get("outcome_class") not in ({"unknown_failure", "model_unavailable"} if outcome.get("source") == "nvidia" else {"unknown_failure"})
        or outcome.get("exact_model_proven") is True
        or outcome.get("persisted_work") is True
        or not outcome.get("model") or not outcome.get("source")
    ):
        return outcome

    # GitHub separates executed steps with a group header. Require the smoke
    # command and terminal timeout inside the same step, not just anywhere
    # in the complete worker log.
    steps = re.split(r"##\[group\]Run ", job_log)
    for step in steps:
        if (
            "timeout --signal=TERM --kill-after=10s 180s" in step
            and re.search(r"##\[error\]Process completed with exit code 124\b", step)
        ):
            return {
                **outcome,
                "outcome": "PROVIDER FAILURE",
                "outcome_class": "provider_failure",
                "detail": "pinned OpenCode model exceeded its 180-second smoke deadline",
            }
    # A dynamic runner can rotate away from its originally selected model.
    # Recover the *executed* final smoke selector only from timestamped runner
    # output in the failing Actions smoke step, never echoed shell source.
    if outcome.get("source") == "opencode-free":
        clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log)
        for step in re.split(r"##\[group\]Run ", clean):
            if not re.search(r"##\[error\]Process completed with exit code 1\b", step):
                continue
            candidates = re.findall(
                r"(?m)^\d{4}-\d{2}-\d{2}T\S+Z Smoke candidate: opencode/([\w./:-]+)\s*$",
                step,
            )
            if not candidates:
                continue
            attempted = candidates[-1]
            if re.search(
                rf"\bModel (?:opencode/)?{re.escape(attempted)} has been deprecated\b",
                step, re.IGNORECASE,
            ) or re.search(
                rf"\bModel unavailable: opencode/{re.escape(attempted)}\b",
                step, re.IGNORECASE,
            ):
                return {
                    **outcome,
                    "model": attempted,
                    "outcome": "MODEL MISSING",
                    "outcome_class": "model_unavailable",
                    "detail": "actual final OpenCode smoke selector is unavailable or deprecated",
                }
    # A trusted NVIDIA 410 has an irreversible provider meaning. Preserve its
    # retirement in Neon, rather than retrying a known-gone model after 6h.
    if (outcome.get("source") == "nvidia"
            and outcome.get("outcome_class") in {"unknown_failure", "model_unavailable"}
            and outcome.get("model")):
        model = re.escape(str(outcome["model"]))
        if re.search(
            rf"(?m)^\d{{4}}-\d{{2}}-\d{{2}}T\S+Z NVIDIA permanently retired model: {model}\s*$",
            re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", job_log),
        ):
            return {
                **outcome,
                "outcome": "MODEL RETIRED",
                "outcome_class": "model_retired_410",
                "detail": "trusted NVIDIA probe returned HTTP 410 for the exact pinned model",
            }
    return outcome
