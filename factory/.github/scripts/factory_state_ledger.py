"""Transactional Neon worker leases and lifecycle events for trusted controllers.

Uses the already provisioned factory_state database. No secrets reach model-runner
jobs: only trusted dispatcher, completion collector, and runtime observer call this.
GitHub still authorizes issue labels, exact-head reviews, checks, and merges.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn
from factory_work_policy import FIXED_LEASE_TTL_SECONDS
from factory_adopter import repository
from factory_state_ledger_core import LEASE_PREFIX, WORKER_RE, _keys, _target_key, enabled
from factory_state_ledger_workers import (
    _acquire, _release, _renew_verified, acquire_lease, release_lease, release_worker,
    renew_verified_workers, worker_claim,
)
from factory_state_ledger_operations import (
    OPERATION_HOLDER_RE, OPERATION_LEASE_TTL_SECONDS, _acquire_operation, _lease_row,
    _operation_request, _release_operation, acquire_operation_lease, list_leases,
    release_expired_leases, release_operation_lease, target_lease,
)
from factory_state_ledger_events import (
    _events, recorded_runtime_runs, refine_trusted_dynamic_models,
    refine_trusted_failover_models, refine_trusted_smoke_timeout,
)
from factory_state_ledger_slots import (
    SLOT_RE, acquire_slot, attempt_slots, slot_holders, slot_key, slot_prefix,
)
from factory_state_ledger_external import (
    EXTERNAL_ACTIONS, EXTERNAL_ID_RE, EXTERNAL_WIP_ACTIONS, _external_holder, _external_key,
    _external_request, _finish_external, _reserve_external, _revoke_external,
    external_holder, external_lease_action, external_wip_count, reserve_external,
    revoke_external,
)

LIFECYCLE_STATUSES = {"started": "worker_started", "finished": "worker_outcome_signal",
                      "failure": "worker_outcome_signal"}


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


def record_runtime_outcome(
    repo: str, run_id: str, outcome: dict[str, Any], job_log: str | None = None,
) -> int:
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
    entries = [{
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
    }]
    # The log is fetched by the trusted completed-job collector. Per-candidate
    # observations share the terminal event transaction and retain distinct IDs.
    if job_log is not None:
        from factory_model_attempt_events import model_attempt_events
        entries.extend(model_attempt_events(run_id, outcome, job_log))
    inserted = record_events(repo, entries)
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
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()
    args.repo = args.repo or repository()
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
        result = record_runtime_outcome(
            args.repo, args.run_id, outcome,
            job_log=job_log if args.log else None,
        )
        print(f"[factory-ledger] recorded {result} terminal worker event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
