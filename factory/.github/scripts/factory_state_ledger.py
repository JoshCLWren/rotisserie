"""Trusted factory claims and lifecycle events on the libSQL (Turso) store.

Only trusted dispatcher, completion collector and runtime observer jobs call
this; model-runner jobs never receive the store token. GitHub still
authorizes issue labels, exact-head reviews, checks and merges. The
implementation lives in the ``factory_turso_*`` modules; this module keeps the
public names and the command line that workflows call.
"""
from __future__ import annotations

import json

from factory_adopter import repository
from factory_state_ledger_core import LEASE_PREFIX, WORKER_RE, _keys, _target_key  # noqa: F401
from factory_state_ledger_events import (  # noqa: F401
    quota_reset_hints, refine_trusted_dynamic_models, refine_trusted_failover_models,
    refine_trusted_smoke_timeout,
)
from factory_state_ledger_external import (  # noqa: F401
    EXTERNAL_ACTIONS, EXTERNAL_ID_RE, EXTERNAL_WIP_ACTIONS, _external_request,
)
from factory_state_ledger_operations import (  # noqa: F401
    OPERATION_HOLDER_RE, OPERATION_LEASE_TTL_SECONDS, _operation_request,
)
from factory_state_ledger_slots import (  # noqa: F401
    SLOT_RE, attempt_slots, slot_key, slot_prefix,
)
from factory_turso_core import ClaimsPaused, enabled, required  # noqa: F401
from factory_turso_events import (  # noqa: F401
    LIFECYCLE_STATUSES, history_rows, record_events, record_lifecycle, record_runtime_outcome,
    recorded_runtime_runs,
)
from factory_turso_external import (  # noqa: F401
    _external_key, external_holder, external_lease_action, external_wip_count,
    reserve_external, revoke_external,
)
from factory_turso_leases import (  # noqa: F401
    acquire_lease, acquire_operation_lease, list_leases, release_expired_leases, release_lease,
    release_operation_lease, release_worker, renew_verified_workers, target_lease, worker_claim,
)
from factory_turso_slots import acquire_slot, slot_holders  # noqa: F401


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
