"""Measure libSQL rows read and written per factory job against a production-sized store.

Run: ``FACTORY_TEST_LIBSQL_URL=http://127.0.0.1:8080 python -m tests.turso_usage`` from
``factory/``. It seeds one synthetic repository to the Nov 24 production size
(21k events, 811 model health rows, 840 metrics rows), replays each job type,
prints rows per job from the server's own counters, projects a month and
deletes the synthetic repository.
"""

from __future__ import annotations

import importlib
import json
import os
import random
from typing import Any

import conftest  # noqa: F401 - reference adopter environment

from tests import turso_support as support

leases = importlib.import_module("factory_turso_leases")
operations = importlib.import_module("factory_turso_operations")
exports = importlib.import_module("factory_turso_exports")
events = importlib.import_module("factory_turso_events")
core = support.core

EVENTS = 21_000
HEALTH = 811
METRICS = 840
DAY = 86400
PER_DAY = {
    "dispatch": int(os.environ.get("FACTORY_USAGE_DISPATCH_PER_DAY", "1237")),
    "run": int(os.environ.get("FACTORY_USAGE_RUN_PER_DAY", "421")),
    "status": int(os.environ.get("FACTORY_USAGE_STATUS_PER_DAY", "8")),
    "retention": 5,
}
FREE = {"rows_read": 500_000_000, "rows_written": 10_000_000}


def _seed(db: Any, repo: str, workers: list[str]) -> None:
    roster = [w for ws in support.workers_by_source().values() for w in ws]
    rnd = random.Random(7)
    types = [
        "worker_assigned",
        "worker_started",
        "worker_outcome_signal",
        "worker_completed",
        "worker_failed",
        "worker_released",
        "model_attempt",
        "model_attempt",
    ]
    rows = []
    for n in range(EVENTS):
        worker = rnd.choice(workers)
        kind = rnd.choice(types)
        rows.append(
            (
                "INSERT INTO factory_state_events (repo, origin, event_type, subject_type,"
                " subject_number, event_id, run_id, worker, source, model, outcome_class, created_at,"
                " detail) VALUES (:repo, 'github-actions', :kind, 'worker', :w, :eid, :run, :w,"
                " 'nvidia', :model, 'success', unixepoch() - :age, :detail)",
                {
                    "repo": repo,
                    "kind": kind,
                    "w": worker,
                    "eid": f"seed:{repo}:{n}",
                    "run": str(10_000 + n // 6),
                    "model": f"m{n % 40}",
                    "age": rnd.randint(0, 30 * DAY),
                    "detail": json.dumps(
                        {
                            "worker": worker,
                            "run_id": str(10_000 + n // 6),
                            "targets": [n % 300],
                            "duration_ms": 1000,
                        }
                    ),
                },
            )
        )
    for n in range(HEALTH):
        rows.append(
            (
                "INSERT INTO factory_model_health (repo, source, model, status, updated_at)"
                " VALUES (:repo, :s, :m, 'healthy', unixepoch())",
                {"repo": repo, "s": f"source{n % 20}", "m": f"model{n}"},
            )
        )
    for n in range(METRICS):
        rows.append(
            (
                "INSERT INTO factory_worker_metrics (repo, worker, source, model, attempts,"
                " productive, updated_at) VALUES (:repo, :w, 'nvidia', :m, 5, 2, unixepoch())",
                {"repo": repo, "w": roster[n % len(roster)], "m": f"m{n}"},
            )
        )
    for offset in range(0, len(rows), 400):
        db.batch(rows[offset : offset + 400])


def _measure(db: Any, job: Any) -> dict[str, int]:
    before = dict(db.usage)
    db.trace = []
    job()
    if os.environ.get("FACTORY_USAGE_TRACE"):
        for line in db.trace:
            print(line)
    db.trace = None
    return {k: db.usage[k] - before[k] for k in ("requests", "rows_read", "rows_written")}


def main() -> None:
    os.environ["FACTORY_TURSO_URL"] = support.SERVER_URL
    os.environ["FACTORY_TURSO_TOKEN_FILE"] = support.TOKEN_FILE
    core.use_store(None)
    db = core.store()
    repo = support.repo_name()
    workers = support.workers_by_source()["nvidia"][:12]
    _seed(db, repo, workers)
    worker = workers[0]
    attempts = [
        {
            "event_type": "model_attempt",
            "event_id": f"usage:{repo}:{n}",
            "worker": worker,
            "run_id": "999",
            "source": "nvidia",
            "model": f"m{n}",
            "outcome_class": "success",
        }
        for n in range(6)
    ]
    state: dict[str, Any] = {}

    def dispatch() -> None:
        kept, _ = operations.filter_workers(repo, workers[:20])
        operations.rank_workers(repo, kept)
        operations.cooling_prs(repo, {1: ("a" * 40, "factory:review")})

    def run() -> None:
        state["token"] = leases.acquire_lease(repo, worker, [500])
        events.record_lifecycle(repo, "999", worker, "started")
        exports.export_model_exclusions(repo, worker)
        exports.export_candidate_health(repo)
        exports.export_fleet_failover_sources(repo, worker)
        events.record_events(repo, attempts)
        events.record_runtime_outcome(
            repo,
            "999",
            {
                "worker": worker,
                "source": "nvidia",
                "model": "m1",
                "outcome_class": "success",
                "exact_model_proven": True,
            },
        )
        leases.release_lease(repo, worker, state["token"], "finished")

    def status() -> None:
        exports.export_reliability(repo)
        exports.export_dashboard_retry(repo)
        events.export_view(repo)
        leases.list_leases(repo)

    def retention() -> None:
        events.rollup_model_attempts(repo)
        events.prune_lifecycle(repo)

    try:
        jobs = {"dispatch": dispatch, "run": run, "status": status, "retention": retention}
        per_job = {}
        for name, job in jobs.items():
            print("==", name) if os.environ.get("FACTORY_USAGE_TRACE") else None
            per_job[name] = _measure(db, job)
        monthly = {
            key: sum(per_job[name][key] * PER_DAY[name] * 30 for name in jobs)
            for key in ("rows_read", "rows_written")
        }
        size = db.read(
            "SELECT page_count * page_size AS bytes FROM pragma_page_count, pragma_page_size"
        )
        print(
            json.dumps(
                {
                    "per_job": per_job,
                    "jobs_per_day": PER_DAY,
                    "monthly": monthly,
                    "free_plan_share": {k: round(monthly[k] / FREE[k], 4) for k in FREE},
                    "seeded_database_bytes": size[0]["bytes"] if size else None,
                },
                indent=2,
            )
        )
    finally:
        support.cleanup(db, repo)


if __name__ == "__main__":
    main()
