"""Live Entry and fork worker runs, listed once per trusted dispatch job.

Every ``assign`` in a roster pass is its own controller process, and each one
listed queued and in-progress runs of both worker workflows: 32 listings and
eight ``runs/N/jobs`` reads per pass (runs 45639940782, 45640509224,
45641285594). Inside a dispatch job (``FACTORY_DISCOVERY_DIR`` set) the first
successful read is kept in a job-local file, and each worker the job
dispatches is added to it. A fork workflow that has never run is probed once
and then not listed. Without that directory every call reads GitHub live.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, os.path.dirname(__file__))
import factory_lease_owners
from factory_lease_owners import ActiveWorkerResult, github_timestamp_epoch

ENTRY_WORKFLOW = "free-model-factory-entry.yml"
FORK_WORKFLOW = "factory-fork-execution.yml"
MAX_AGE_SECONDS = 300
GhJson = Callable[[list[str]], object]


def memo_path(repo: str) -> Path | None:
    directory = os.environ.get("FACTORY_DISCOVERY_DIR")
    if not directory:
        return None
    digest = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return Path(directory) / f"{digest}-active-workers.json"


def _max_age() -> int:
    raw = os.environ.get("FACTORY_ACTIVE_WORKERS_MAX_AGE", "")
    return int(raw) if raw.isdecimal() else MAX_AGE_SECONDS


def read_memo(repo: str, *, now: int) -> ActiveWorkerResult | None:
    path = memo_path(repo)
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not 0 <= now - int(value["read_at"]) < _max_age():
            return None
        return ActiveWorkerResult(
            {int(worker) for worker in value["workers"]},
            {str(run) for run in value["unresolved"]},
            [dict(run) for run in value["runs"]],
            {str(key): str(item) for key, item in value["run_workers"].items()},
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(f".{os.getpid()}.tmp")
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(path)


def write_memo(repo: str, result: ActiveWorkerResult, *, now: int) -> None:
    path = memo_path(repo)
    if path is None:
        return
    _write(
        path,
        {
            "read_at": now,
            "workers": sorted(result[0]),
            "unresolved": sorted(result[1]),
            "runs": list(result.live_runs),
            "run_workers": result.run_workers,
        },
    )


def note_dispatched(repo: str, worker: str) -> bool:
    """Count a worker this job just dispatched as live, before GitHub lists its run."""
    path = memo_path(repo)
    if path is None or not worker.isdecimal():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["workers"] = sorted({*map(int, value["workers"]), int(worker)})
    except OSError, ValueError, KeyError, TypeError:
        return False
    _write(path, value)
    return True


def forget(repo: str) -> None:
    path = memo_path(repo)
    if path is not None:
        path.unlink(missing_ok=True)


def _fork_marker(repo: str) -> Path | None:
    path = memo_path(repo)
    return (
        None if path is None else path.with_name(path.name.replace("active-workers", "fork-idle"))
    )


def fork_never_ran(repo: str, gh_json: GhJson) -> bool:
    """True when the fork workflow has no runs at all; probed once per job."""
    marker = _fork_marker(repo)
    if marker is None:
        return False
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip() == "idle"
    try:
        page = gh_json(["api", f"repos/{repo}/actions/workflows/{FORK_WORKFLOW}/runs?per_page=1"])
    except RuntimeError:
        return False
    if not isinstance(page, dict) or not isinstance(page.get("total_count"), int):
        return False
    idle = page["total_count"] == 0
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("idle" if idle else "active", encoding="utf-8")
    return idle


def run_is_live(
    run: dict[str, Any], *, repo: str, gh_json: GhJson, now_epoch: int, ttl: int
) -> bool:
    """Return whether an unresolved active-status run still merits a safety fence."""
    status = str(run.get("status") or "").lower()
    if status not in ("queued", "in_progress"):
        return True
    run_activity = [
        epoch
        for key in ("created_at", "updated_at")
        if (epoch := github_timestamp_epoch(run.get(key))) is not None
    ]
    if not run_activity:
        return True
    if now_epoch - max(run_activity) <= ttl:
        return True
    run_id = str(run.get("id") or "")
    if not run_id.isdigit():
        return True
    try:
        pages = gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
            ]
        )
    except RuntimeError:
        return True
    jobs: list[dict[str, Any]] = []
    saw_jobs_payload = False
    for page in pages if isinstance(pages, list) else []:
        page_jobs = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(page_jobs, list):
            continue
        saw_jobs_payload = True
        jobs.extend(job for job in page_jobs if isinstance(job, dict))
    if not saw_jobs_payload:
        return True
    if not jobs:
        return False
    for job in jobs:
        if str(job.get("status") or "").lower() in ("queued", "in_progress"):
            return True
        job_activity = [
            epoch
            for key in ("started_at", "completed_at")
            if (epoch := github_timestamp_epoch(job.get(key))) is not None
        ]
        if not job_activity or now_epoch - max(job_activity) <= ttl:
            return True
    return False


def read_live(
    repo: str,
    *,
    gh_json: GhJson,
    authorized: Callable[[str], bool],
    live: Callable[[dict[str, Any], int], bool],
) -> ActiveWorkerResult:
    """List both worker workflows' active runs and resolve each run's worker."""
    workers: set[int] = set()
    runs: list[dict[str, Any]] = []
    unresolved_run_ids: set[str] = set()
    unresolved_runs: dict[str, dict[str, Any]] = {}
    workflows = [ENTRY_WORKFLOW] + ([] if fork_never_ran(repo, gh_json) else [FORK_WORKFLOW])
    for workflow in workflows:
        for status in ("queued", "in_progress"):
            try:
                path = f"repos/{repo}/actions/workflows/{workflow}/runs?status={status}"
                pages = gh_json(["api", "--paginate", "--slurp", f"{path}&per_page=100"])
            except RuntimeError:
                prefix = "" if workflow == ENTRY_WORKFLOW else "fork-"
                unresolved_run_ids.add(f"{prefix}{status}-run-query-unavailable")
                continue
            for page in pages if isinstance(pages, list) else []:
                if isinstance(page, dict):
                    runs.extend(cast(list[dict[str, Any]], page.get("workflow_runs") or []))
    resolved_by_store: dict[str, str] = {}
    for index, run in enumerate(runs):
        if worker := factory_lease_owners.run_worker(run, {}):
            workers.add(int(worker))
            continue
        run_id = str(run.get("id") or "")
        unresolved_run_ids.add(run_id or f"missing-run-id-{index}")
        if run_id:
            unresolved_runs[run_id] = run
    numeric_unresolved = {value for value in unresolved_run_ids if value.isdigit()}
    if numeric_unresolved:
        try:
            from factory_runtime_view import run_workers

            for run_id, worker in run_workers(repo, sorted(numeric_unresolved)).items():
                if run_id in numeric_unresolved and worker.isdecimal() and authorized(worker):
                    workers.add(int(worker))
                    resolved_by_store[run_id] = worker
                    unresolved_run_ids.discard(run_id)
        except Exception as exc:
            print(
                "[factory-controller] run-to-worker evidence unavailable "
                f"({type(exc).__name__}); preserving unresolved run fence",
                file=sys.stderr,
            )
    now_epoch = int(time.time())
    for run_id, run in unresolved_runs.items():
        if run_id not in unresolved_run_ids or live(run, now_epoch):
            continue
        unresolved_run_ids.discard(run_id)
        print(
            "[factory-controller] ignoring expired unresolved active-status run "
            f"{run_id}: no credible worker execution evidence",
            file=sys.stderr,
        )
    return ActiveWorkerResult(workers, unresolved_run_ids, runs, resolved_by_store)


def active_workers(
    repo: str,
    *,
    gh_json: GhJson,
    authorized: Callable[[str], bool],
    live: Callable[[dict[str, Any], int], bool],
) -> ActiveWorkerResult:
    """The job's memo when fresh; otherwise a live read, kept only when every listing succeeded."""
    now = int(time.time())
    if (memo := read_memo(repo, now=now)) is not None:
        return memo
    result = read_live(repo, gh_json=gh_json, authorized=authorized, live=live)
    if not any(run.endswith("-run-query-unavailable") for run in result[1]):
        write_memo(repo, result, now=now)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    note = sub.add_parser("note-dispatched")
    note.add_argument("--worker", required=True)
    sub.add_parser("forget")
    args = parser.parse_args()
    from factory_adopter import repository

    repo = repository()
    if args.command == "forget":
        forget(repo)
    elif not note_dispatched(repo, args.worker):
        forget(repo)
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    raise SystemExit(main())
