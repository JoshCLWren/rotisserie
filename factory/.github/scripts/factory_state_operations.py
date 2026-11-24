#!/usr/bin/env python3
"""Trusted Neon decision projections for the factory.

Only trusted Actions controllers use these functions. Agent/model runner jobs
must never receive FACTORY_STATE_DSN_FILE. GitHub remains authoritative for
review, code, exact-head checks, labels, and merges. SQL projections are hints
and capacity reservations, never merge authorization.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from factory_adopter import repository
from factory_state_operations_backoff import (  # noqa: F401
    MODEL_COOLDOWNS,
    MODEL_RETRY_CLASSES,
    MODEL_SCOPED_SOURCES,
    TRANSIENT,
    WORKER_RETRY_CLASSES,
    _provider_cooldown,
    _recent_streak,
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
from factory_state_operations_capacity import (  # noqa: F401
    _checkpoint,
    _claim_wakeup,
    _cooling_prs,
    _daily_usage,
    _eligibility,
    _ingest_check,
    _rank_workers,
    _seed_daily,
    _stats,
    _sync_catalog,
    release_capacity,
    reserve_capacity,
)
from factory_state_operations_core import (  # noqa: F401
    HEAD_RE,
    REPO_RE,
    SCHEMA,
    _connection,
    _neon_required,
    _roster,
    _run,
    enabled,
    ensure_schema,
)
from factory_state_operations_exports import (  # noqa: F401
    _export_candidate_health,
    _export_dashboard_retry,
    _export_fleet_failover_sources,
    _export_model_exclusions,
    _reset_model_health,
)
from factory_state_operations_reliability import (  # noqa: F401
    RELIABILITY_WINDOWS,
    _export_reliability,
    export_reliability,
    summarize_reliability,
)
from factory_state_operations_runtime import project_runtime  # noqa: F401


def record_runtime(repo: str, run_id: int, outcome: dict[str, Any]) -> bool:
    if not enabled():
        raise RuntimeError("trusted runtime requires restricted Neon connection")
    return _run(project_runtime, repo, run_id, outcome)


def filter_workers(repo: str, workers: list[str]) -> tuple[list[str],list[str]]:
    if not enabled():
        return workers, ["Neon unavailable; existing lease checks still required"]
    return _run(_eligibility,repo,workers)


def daily_usage(repo: str, sources: list[str]) -> dict[str,int] | None:
    if not enabled():
        return None
    return _run(_daily_usage,repo,sources)


def seed_daily(repo: str, counts: dict[str,int]) -> None:
    if enabled():
        _run(_seed_daily,repo,counts)


def cooling_prs(repo: str, heads: dict[int,tuple[str,str]]) -> set[int]:
    return _run(_cooling_prs,repo,heads) if enabled() and heads else set()


def ingest_check(repo: str, event: dict[str, Any]) -> int:
    if not enabled():
        raise RuntimeError("review cooldown ingestion requires restricted Neon connection")
    return _run(_ingest_check, repo, event)


def checkpoint(repo: str, scope: str, cursor: str, changes: int=0) -> None:
    if enabled():
        _run(_checkpoint,repo,scope,cursor,changes)


def sync_catalog(repo:str,report:dict[str,Any])->int:
    return _run(_sync_catalog,repo,report) if enabled() else 0


def rank_workers(repo:str,workers:list[str])->list[str]:
    return _run(_rank_workers,repo,workers) if enabled() else workers


def claim_wakeup(repo: str, scope: str, interval_seconds: int=120) -> bool:
    return _run(_claim_wakeup,repo,scope,interval_seconds) if enabled() else False


def export_dashboard_retry(repo: str) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("Factory retry evidence is not connected")
    return _run(_export_dashboard_retry, repo)


def export_candidate_health(repo: str) -> dict[str, Any]:
    if not enabled():
        if not _neon_required():
            return {"models": []}
        raise RuntimeError("Neon health unavailable; cannot prove executable candidates")
    return _run(_export_candidate_health, repo)


def export_fleet_failover_sources(repo: str, worker: str) -> list[str]:
    from factory_fleet_switch import filter_sources
    if not enabled():
        if not _neon_required():
            return []
        raise RuntimeError("Neon capacity read required for fleet failover")
    return filter_sources(_run(_export_fleet_failover_sources, repo, worker))


def export_model_exclusions(
    repo: str, worker: str = "", requested_model: str = "auto",
) -> dict[str, list[str]]:
    """Trusted exclusions plus models the operator turned off (factory_fleet_switch)."""
    from factory_fleet_switch import merge_exclusions
    if enabled():
        return merge_exclusions(_run(_export_model_exclusions, repo, worker, requested_model), requested_model=requested_model)
    if _neon_required():
        raise RuntimeError("Neon is required for model health snapshot")
    return merge_exclusions({}, requested_model=requested_model)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("bootstrap","filter","rank","stats","ingest-check","checkpoint","sync-catalog","export-model-exclusions","export-candidate-health","export-fleet-failover-sources","reset-model-health","export-reliability"))
    parser.add_argument("--repo",default=None)
    parser.add_argument("--file",type=Path)
    parser.add_argument("--scope")
    parser.add_argument("--worker",default="")
    parser.add_argument("--requested-model",default="auto")
    parser.add_argument("--cursor",default="")
    parser.add_argument("--include-retired",action="store_true")
    args=parser.parse_args()
    args.repo=args.repo or repository()
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
        print(json.dumps(export_model_exclusions(args.repo,args.worker,args.requested_model),sort_keys=True,separators=(",",":")))
    elif args.command=="export-reliability":
        print(json.dumps(export_reliability(args.repo),sort_keys=True,separators=(",",":")))
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
