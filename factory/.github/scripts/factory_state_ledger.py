"""Transactional Neon worker leases and lifecycle events for trusted controllers.

Uses the already provisioned factory_state database. No secrets reach model-runner
jobs: only trusted dispatcher, completion collector, and runtime observer call this.
GitHub still authorizes issue labels, exact-head reviews, checks, and merges.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn
from factory_work_policy import FIXED_LEASE_TTL_SECONDS

WORKER_RE = re.compile(r"^[0-9]+$")
LIFECYCLE_STATUSES = {"started": "worker_started", "finished": "worker_outcome_signal",
                      "failure": "worker_outcome_signal"}
LEASE_PREFIX = "factory-lease-v1:"


def enabled() -> bool:
    return bool(os.environ.get("FACTORY_STATE_DSN_FILE"))


def _keys(repo: str, worker: str, numbers: list[int]) -> list[str]:
    return [f"{LEASE_PREFIX}{repo}:worker:{worker}"] + [
        f"{LEASE_PREFIX}{repo}:target:{number}" for number in sorted(set(numbers))
    ]


async def _acquire(repo: str, worker: str, numbers: list[int], ttl: int) -> str | None:

    token = str(uuid.uuid4())
    keys = _keys(repo, worker, numbers)
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        async with conn.transaction():
            # A repository-level transaction lock makes worker and *all* linked
            # issue/PR target rows one indivisible claim across Actions jobs.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            busy = await conn.fetch(
                """SELECT cache_key FROM public.factory_state_kv
                   WHERE cache_key = ANY($1::text[]) AND expires_at > now()""",
                keys,
            )
            if busy:
                return None
            from factory_state_operations import _roster, reserve_capacity
            if not await reserve_capacity(conn, repo, worker, token, ttl):
                return None
            payload = json.dumps([{
                "claim_id": token, "worker": worker, "targets": sorted(set(numbers)),
            }])
            for key in keys:
                await conn.execute(
                    """INSERT INTO public.factory_state_kv
                         (cache_key, payload, fetched_at, expires_at, updated_at)
                       VALUES ($1, $2::jsonb, now(), now() + $3::int * interval '1 second', now())
                       ON CONFLICT (cache_key) DO UPDATE SET
                         payload=EXCLUDED.payload, fetched_at=EXCLUDED.fetched_at,
                         expires_at=EXCLUDED.expires_at, updated_at=now(),
                         refresh_owner=NULL, refresh_until=NULL""",
                    key, payload, ttl,
                )
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source, event_type, subject_type, subject_number, detail)
                   VALUES ('github-actions', 'worker_assigned', 'worker', $1,
                     jsonb_build_object('claim_id', $2::text, 'worker', $3::text,
                                        'targets', $4::jsonb, 'run_id', $5::text,
                                        'source', $6::text, 'repo', $7::text))""",
                int(worker), token, worker, json.dumps(sorted(set(numbers))),
                os.environ.get("GITHUB_RUN_ID", ""),
                _roster()[worker][0], repo,
            )
        return token
    finally:
        await conn.close()


def acquire_lease(repo: str, worker: str, numbers: list[int], ttl: int = FIXED_LEASE_TTL_SECONDS) -> str | None:
    """Return claim UUID on success, None on conflict, '' without Neon.

    In trusted Actions with a configured DSN, connection failures are fatal:
    silently falling back after a failed database claim would split ownership.
    """
    if not WORKER_RE.fullmatch(worker) or not numbers or any(n <= 0 for n in numbers):
        raise ValueError("invalid factory lease target or worker")
    if not 60 <= ttl <= 86400:
        raise ValueError("invalid factory lease TTL")
    if not enabled():
        if os.environ.get("FACTORY_NEON_LEASE_REQUIRED") == "true":
            raise RuntimeError("trusted factory dispatcher requires Neon transaction lease connection")
        return ""
    return asyncio.run(_acquire(repo, worker, numbers, ttl))


async def _release(repo: str, worker: str, token: str, reason: str) -> bool:

    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            worker_key = _keys(repo, worker, [])[0]
            row = await conn.fetchrow(
                """SELECT payload FROM public.factory_state_kv
                   WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                worker_key, token,
            )
            if row is None:
                return False
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            numbers = payload[0]["targets"]
            keys = _keys(repo, worker, numbers)
            await conn.execute(
                """DELETE FROM public.factory_state_kv
                   WHERE cache_key=ANY($1::text[])
                     AND payload->0->>'claim_id'=$2""",
                keys, token,
            )
            from factory_state_operations import release_capacity
            await release_capacity(conn, token)
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source, event_type, subject_type, subject_number, detail)
                   VALUES ('github-actions', 'worker_released', 'worker', $1,
                     jsonb_build_object('claim_id',$2::text,'worker',$3::text,
                                        'reason',$4::text,'targets',$5::jsonb,'repo',$6::text))""",
                int(worker), token, worker, reason, json.dumps(numbers), repo,
            )
        return True
    finally:
        await conn.close()


def release_lease(repo: str, worker: str, token: str, reason: str) -> bool:
    if not enabled() or not token:
        return False
    return asyncio.run(_release(repo, worker, token, reason))


def release_worker(repo: str, worker: str, reason: str) -> bool:
    """Release a verified worker's Neon claim after GitHub labels are released."""
    if not enabled():
        return False

    async def _lookup() -> str | None:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            row = await conn.fetchrow(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1",
                _keys(repo, worker, [])[0],
            )
            if not row:
                return None
            value = row["payload"]
            if isinstance(value, str):
                value = json.loads(value)
            return str(value[0]["claim_id"])
        finally:
            await conn.close()

    token = asyncio.run(_lookup())
    return release_lease(repo, worker, token, reason) if token else False


async def _events(repo: str, entries: list[dict[str, Any]]) -> int:

    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    inserted = 0
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo)
            for entry in entries:
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
        return inserted
    finally:
        await conn.close()



async def _renew_verified(repo: str, workers: list[str]) -> int:
    """Extend existing, unexpired reservations for verified running workers."""

    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=12)
    renewed = 0
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                LEASE_PREFIX + repo,
            )
            for worker in sorted(set(workers)):
                worker_key = _keys(repo, worker, [])[0]
                raw = await conn.fetchval(
                    """SELECT payload FROM public.factory_state_kv
                       WHERE cache_key=$1 AND expires_at>now()""",
                    worker_key,
                )
                if raw is None:
                    continue
                payload = json.loads(raw) if isinstance(raw, str) else raw
                token = payload[0]["claim_id"]
                await conn.execute(
                    """UPDATE public.factory_provider_reservation
                       SET expires_at=greatest(expires_at,now()+$2::int * interval '1 second')
                       WHERE claim_id=$1::uuid""", token, FIXED_LEASE_TTL_SECONDS)
                await conn.execute(
                    """UPDATE public.factory_state_kv
                       SET expires_at=greatest(expires_at,now()+$4::int * interval '1 second'),
                           updated_at=now()
                       WHERE expires_at>now()
                         AND payload->0->>'claim_id'=$1
                         AND (cache_key=$2 OR cache_key LIKE $3)""",
                    token, worker_key, f"{LEASE_PREFIX}{repo}:target:%", FIXED_LEASE_TTL_SECONDS,
                )
                renewed += 1
        return renewed
    finally:
        await conn.close()


def renew_verified_workers(repo: str, workers: list[str]) -> int:
    """Renew only workers verified alive by the trusted GitHub Actions controller.

    Args:
        repo: Repository whose worker claims are being extended.
        workers: Numeric worker IDs observed in active GitHub Actions runs.

    Returns:
        Number of unexpired worker claim groups extended.
    """
    if not enabled() or not workers:
        return 0
    if any(not WORKER_RE.fullmatch(worker) for worker in workers):
        raise ValueError("invalid verified worker ID")
    return asyncio.run(_renew_verified(repo, workers))


def worker_claim(repo: str, worker: str, *, active_only: bool = False) -> tuple[str, list[int]] | None:
    """Capture the existing claim before a controller mutates ownership labels.

    Args:
        repo: GitHub repository.
        worker: Numeric factory worker identifier.

    Returns:
        Existing claim ID and target numbers, or None if unavailable.

    A token-bound release cannot remove a claim acquired after this snapshot.
    """
    if not enabled():
        return None

    async def _read() -> tuple[str, list[int]] | None:

        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            raw = await conn.fetchval(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1 AND (NOT $2::boolean OR expires_at>now())",
                _keys(repo, worker, [])[0], active_only,
            )
            if raw is None:
                return None
            payload = json.loads(raw) if isinstance(raw, str) else raw
            return str(payload[0]["claim_id"]), [
                int(number) for number in payload[0]["targets"]
            ]
        finally:
            await conn.close()

    return asyncio.run(_read())

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


def record_events(repo: str, entries: list[dict[str, Any]]) -> int:
    if not enabled() or not entries:
        return 0
    return asyncio.run(_events(repo, entries))


def record_lifecycle(repo: str, run_id: str, worker: str, status: str) -> int:
    """Persist a trusted Actions lifecycle observation for one worker run.

    Only trusted run-workflow jobs (assignment validation and lease release)
    call this, using the dispatch-input worker and the Actions job result. It
    replaces the retired issue #1093 heartbeat comments: no GitHub comment is
    read or written. Repeated calls are idempotent per run and status.

    Args:
        repo: GitHub repository.
        run_id: Entry workflow run ID.
        worker: Numeric factory worker identifier.
        status: ``started``, ``finished`` or ``failure``.

    Returns:
        Number of newly inserted lifecycle events.
    """
    if not WORKER_RE.fullmatch(worker) or not run_id.isdecimal():
        raise ValueError("invalid trusted lifecycle identity")
    if status not in LIFECYCLE_STATUSES:
        raise ValueError("invalid trusted lifecycle status")
    return record_events(repo, [{
        "event_type": LIFECYCLE_STATUSES[status],
        "event_id": f"lifecycle:{run_id}:{status}",
        "worker": worker,
        "run_id": run_id,
        "status": "running" if status == "started" else status,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }])


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


def record_runtime_outcome(repo: str, run_id: str, outcome: dict[str, Any]) -> int:
    worker = str(outcome.get("worker") or "")
    if not WORKER_RE.fullmatch(worker) or not run_id.isdecimal():
        raise ValueError("invalid trusted runtime outcome identity")
    status = str(outcome.get("outcome_class") or "unknown_failure")
    # Execution can time out despite a verified code push and successful PR handoff.
    delivered = (outcome.get("delivery_outcome") == "persisted"
                 and outcome.get("persisted_work") is True
                 and str(outcome.get("delivery_pr") or "").isdecimal())
    event_type = ("worker_completed" if status == "success" or delivered
                  else "worker_idle" if status == "no_work" else "worker_failed")
    inserted = record_events(repo, [{
        "event_type": event_type,
        "event_id": f"runtime:{run_id}",
        "worker": worker,
        "run_id": run_id,
        "source": outcome.get("source"),
        "model": outcome.get("model"),
        "exact_model_proven": outcome.get("exact_model_proven") is True,
        "outcome_class": status,
        "delivery_outcome": "persisted" if delivered else "none",
        "delivery_pr": outcome.get("delivery_pr") if delivered else None,
        "selected_kind": outcome.get("selected_kind"),
        "selected_number": outcome.get("selected_number"),
        "persisted_work": bool(outcome.get("persisted_work")),
        "model_failures": outcome.get("model_failures") or [],
        "trusted_source_switch": outcome.get("trusted_source_switch") is True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }])
    # Separate projection key is idempotent across both trusted outcome writers.
    from factory_state_operations import enabled as health_enabled, record_runtime
    if health_enabled():
        record_runtime(repo, int(run_id), outcome)
    return inserted


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["runtime-outcome", "lifecycle"])
    parser.add_argument("--file")
    parser.add_argument("--worker")
    parser.add_argument("--status", choices=sorted(LIFECYCLE_STATUSES))
    parser.add_argument("--log", help="Trusted completed factory job log, for exact smoke timeout attribution")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile"))
    args = parser.parse_args()
    if args.command == "lifecycle":
        if not args.worker or not args.status:
            parser.error("lifecycle requires --worker and --status")
        result = record_lifecycle(args.repo, args.run_id, args.worker, args.status)
        print(f"[factory-ledger] recorded {result} lifecycle event(s)")
        return 0
    if args.command == "runtime-outcome":
        if not args.file:
            parser.error("runtime-outcome requires --file")
        with open(args.file, encoding="utf-8") as stream:
            outcome = json.load(stream)
        if args.log:
            with open(args.log, encoding="utf-8", errors="replace") as stream:
                job_log = stream.read()
                outcome = refine_trusted_dynamic_models(outcome, job_log)
                outcome = refine_trusted_failover_models(outcome, job_log)
                outcome = refine_trusted_smoke_timeout(outcome, job_log)
        result = record_runtime_outcome(args.repo, args.run_id, outcome)
        print(f"[factory-ledger] recorded {result} terminal worker event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
