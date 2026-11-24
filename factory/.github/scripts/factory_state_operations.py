#!/usr/bin/env python3
"""Trusted decision projections for the factory, on the libSQL (Turso) store.

Only trusted Actions controllers use these functions. Agent/model runner jobs
must never receive the store token. GitHub remains authoritative for review,
code, exact-head checks, labels and merges. Store projections are hints and
capacity reservations, never merge authorization. The implementation lives in
the ``factory_turso_*`` modules; this module keeps the public names and the
command line that workflows call.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from factory_adopter import repository
from factory_state_operations_backoff import (  # noqa: F401
    MODEL_COOLDOWNS,
    MODEL_RETRY_CLASSES,
    MODEL_SCOPED_SOURCES,
    TRANSIENT,
    WORKER_RETRY_CLASSES,
    _provider_cooldown,
    attributable_model_failure,
    exponential_provider_delay,
    fibonacci_retry_at,
    fibonacci_worker_delay,
    historical_failure_streak,
    model_scoped_transient,
    provider_scoped_transient,
    validate_intermediate_model_failures,
    worker_backoff_reason,
)
from factory_state_operations_reliability import (  # noqa: F401
    RELIABILITY_WINDOWS,
    summarize_reliability,
)
from factory_turso_core import HEAD_RE, REPO_RE, enabled, required  # noqa: F401
from factory_turso_exports import (  # noqa: F401
    export_backoffs,
    export_candidate_health,
    export_dashboard_retry,
    export_fleet_failover_sources,
    export_model_exclusions,
    export_reliability,
    reset_model_health,
    reset_quota_backoffs,
)
from factory_turso_operations import (  # noqa: F401
    cooling_prs,
    daily_usage,
    filter_workers,
    ingest_check,
    rank_workers,
    record_runtime,
    roster,
    roster_source,
    stats,
)

COMMANDS = (
    "bootstrap",
    "filter",
    "rank",
    "stats",
    "ingest-check",
    "export-model-exclusions",
    "export-candidate-health",
    "export-fleet-failover-sources",
    "reset-model-health",
    "export-reliability",
    "export-backoffs",
    "prune",
)
STORE_COMMANDS = frozenset({"bootstrap", "reset-model-health", "export-backoffs", "prune"})


def prune(repo: str) -> dict[str, int]:
    """Bounded retention: aged lifecycle events, then expired raw model attempts."""
    from factory_turso_events import BATCH, prune_lifecycle, rollup_model_attempts

    pruned = rolled = 0
    for _ in range(5):
        count = prune_lifecycle(repo)
        pruned += count
        if count < BATCH:
            break
    for _ in range(5):
        count = rollup_model_attempts(repo)
        rolled += count
        if count < BATCH:
            break
    return {"lifecycle_events_pruned": pruned, "model_attempts_rolled_up": rolled}


def bootstrap() -> str:
    import factory_libsql
    import factory_turso_core

    created = factory_libsql.ensure_schema(factory_turso_core.store())
    return f"schema ready (version {factory_libsql.SCHEMA_VERSION}, created now: {created})"


def _store_command(args: argparse.Namespace) -> None:
    if not enabled():
        raise SystemExit(f"the factory store is required for {args.command}")
    if args.command == "export-backoffs":
        print(json.dumps(export_backoffs(args.repo), sort_keys=True, default=str))
    elif args.command == "reset-model-health" and args.quota_only:
        print(json.dumps(reset_quota_backoffs(args.repo), sort_keys=True))
    elif args.command == "reset-model-health":
        print(json.dumps(reset_model_health(args.repo, args.include_retired), sort_keys=True))
    elif args.command == "prune":
        print(f"[factory-store] {json.dumps(prune(args.repo), sort_keys=True)}")
    else:
        print(f"[factory-operations] {bootstrap()}")


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--scope")
    parser.add_argument("--worker", default="")
    parser.add_argument("--requested-model", default="auto")
    parser.add_argument("--include-retired", action="store_true")
    parser.add_argument(
        "--quota-only",
        action="store_true",
        help="clear only quota/throttle backoffs; cap real-failure streaks",
    )
    args = parser.parse_args()
    args.repo = args.repo or repository()
    if not REPO_RE.fullmatch(args.repo):
        parser.error("invalid repo")
    if args.command in STORE_COMMANDS:
        _store_command(args)
    elif args.command == "export-fleet-failover-sources":
        print(
            json.dumps(export_fleet_failover_sources(args.repo, args.worker), separators=(",", ":"))
        )
    elif args.command == "export-model-exclusions":
        print(_compact(export_model_exclusions(args.repo, args.worker, args.requested_model)))
    elif args.command == "export-reliability":
        print(_compact(export_reliability(args.repo)))
    elif args.command == "export-candidate-health":
        print(_compact(export_candidate_health(args.repo)))
    elif args.command == "filter":
        workers = [str(w) for w in json.load(sys.stdin)]
        kept, denied = filter_workers(args.repo, workers)
        for reason in denied:
            print(f"[factory-operations] {reason}", file=sys.stderr)
        print(json.dumps(kept, separators=(",", ":")))
    elif args.command == "rank":
        workers = [str(w) for w in json.load(sys.stdin)]
        print(json.dumps(rank_workers(args.repo, workers), separators=(",", ":")))
    elif args.command == "stats":
        print(json.dumps(stats(args.repo), default=str))
    elif args.command == "ingest-check":
        if args.file is None:
            parser.error("--file required")
        recorded = ingest_check(args.repo, json.loads(args.file.read_text()))
        print(json.dumps({"recorded_review_failures": recorded}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
