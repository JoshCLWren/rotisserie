#!/usr/bin/env python3
"""Persist Entry terminal outcomes that the in-run persister could not write.

Entry's ``persist_outcome`` job downloads the completed factory job log,
classifies it and writes the terminal outcome to Neon. When GitHub's
installation rate limit is exhausted (runs 44031401652 and 44032359658) the
log download fails, the job exits and no outcome ever reaches Neon. This
bounded reconciler, run by later Entry persisters, finds recent completed
Entry runs without a recorded ``runtime:<run_id>`` event and classifies them
with the same trusted code path. It is idempotent, and it stops at the first
GitHub API failure so a still-limited installation is not hammered.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

ENTRY_WORKFLOW = "free-model-factory-entry.yml"
FACTORY_JOB = "run / factory"
PERSIST_JOB = "Persist trusted worker outcome in Neon"
# run-name is derived only from the trusted dispatcher's `worker` input.
TITLE_RE = re.compile(r"^Factory (?P<worker>[1-9][0-9]*) · fixed-model entry$")

GhApi = Callable[..., str]


class GitHubUnavailableError(RuntimeError):
    """A GitHub read failed; defer the rest of the pass to a later run."""


def gh_api(path: str, *, raw: bool = False) -> str:
    """Read one GitHub REST resource through the CLI, without retries."""
    command = ["gh", "api", path]
    if raw:
        # Keep ANSI colour: the classifier uses it to drop echoed commands.
        command.insert(2, "--allow-escape-sequences")
    proc = subprocess.run(command, text=True, capture_output=True, check=False, timeout=60)
    if raw and proc.returncode and "unknown flag" in proc.stderr:
        # Older local gh builds always pass escape sequences through.
        proc = subprocess.run(["gh", "api", path], text=True, capture_output=True,
                              check=False, timeout=60)
    if proc.returncode:
        raise GitHubUnavailableError(proc.stderr.strip()[:300] or f"gh api {path} failed")
    return proc.stdout


def candidate_runs(repo: str, api: GhApi, *, now: datetime, hours: int,
                   exclude: set[str]) -> list[dict[str, Any]]:
    """Recent completed Entry runs with a trusted worker identity, newest first."""
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.loads(api(
        f"repos/{repo}/actions/workflows/{ENTRY_WORKFLOW}/runs"
        f"?status=completed&per_page=100&created=%3E%3D{since}"
    ))
    runs = []
    for run in payload.get("workflow_runs") or []:
        run_id = str(run.get("id") or "")
        title = TITLE_RE.fullmatch(str(run.get("display_title") or ""))
        if (not run_id.isdecimal() or run_id in exclude or title is None
                or run.get("event") != "workflow_dispatch"
                or run.get("head_repository", {}).get("full_name") != repo):
            continue
        runs.append({"id": run_id, "worker": title["worker"]})
    return runs


def classify_run(repo: str, run: dict[str, Any], api: GhApi) -> tuple[dict[str, Any], str] | None:
    """Return the attested outcome and job log, or None when not reconcilable."""
    from factory_dynamic_outcome import classify

    jobs = json.loads(api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")).get("jobs") or []
    persist = [job for job in jobs if job.get("name") == PERSIST_JOB]
    # Only completed persisters; one still running may yet succeed itself.
    if not persist or any(job.get("status") != "completed" for job in persist):
        return None
    factory = [job for job in jobs if job.get("name") == FACTORY_JOB and job.get("status") == "completed"]
    if not factory:
        return None
    log = api(f"repos/{repo}/actions/jobs/{factory[0]['id']}/logs", raw=True)
    outcome = classify(log)
    delivery_pr = str(outcome.get("delivery_pr") or "")
    if delivery_pr:
        pr = json.loads(api(f"repos/{repo}/pulls/{delivery_pr}"))
        verified = (
            str(pr.get("number")) == delivery_pr
            and (pr.get("head") or {}).get("repo", {}).get("full_name") == repo
            and (pr.get("state") == "open" or pr.get("merged") is True)
            and re.fullmatch(r"[0-9a-f]{40}", str((pr.get("head") or {}).get("sha") or ""))
        )
        if not verified:
            outcome = {**outcome, "delivery_outcome": "none", "delivery_pr": ""}
    # Worker identity comes from the trusted dispatch input, never the log.
    return {**outcome, "worker": run["worker"], "reconciled": True}, log


def reconcile(repo: str, *, api: GhApi = gh_api, recorded: Callable[[str, list[str]], set[str]],
              record: Callable[[str, str, dict[str, Any], str], int], now: datetime,
              hours: int, limit: int, exclude: set[str]) -> dict[str, Any]:
    """Persist at most ``limit`` missing outcomes; never re-project a recorded run."""
    summary: dict[str, Any] = {"checked": 0, "missing": [], "persisted": [], "skipped": [],
                               "deferred": ""}
    try:
        runs = candidate_runs(repo, api, now=now, hours=hours, exclude=exclude)
    except GitHubUnavailableError as exc:
        summary["deferred"] = str(exc)
        return summary
    summary["checked"] = len(runs)
    done = recorded(repo, [run["id"] for run in runs])
    missing = [run for run in runs if run["id"] not in done]
    summary["missing"] = [run["id"] for run in missing]
    for run in missing[:limit]:
        try:
            result = classify_run(repo, run, api)
        except GitHubUnavailableError as exc:
            summary["deferred"] = str(exc)
            break
        if result is None:
            summary["skipped"].append(run["id"])
            continue
        outcome, log = result
        record(repo, run["id"], outcome, log)
        summary["persisted"].append(run["id"])
    return summary


def _record(repo: str, run_id: str, outcome: dict[str, Any], log: str) -> int:
    from factory_state_ledger import (
        record_runtime_outcome,
        refine_trusted_dynamic_models,
        refine_trusted_failover_models,
        refine_trusted_smoke_timeout,
    )

    outcome = refine_trusted_dynamic_models(outcome, log)
    outcome = refine_trusted_failover_models(outcome, log)
    outcome = refine_trusted_smoke_timeout(outcome, log)
    return record_runtime_outcome(repo, run_id, outcome)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--hours", type=int, default=12)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--exclude-run", action="append", default=[])
    args = parser.parse_args()
    from factory_state_ledger import enabled, recorded_runtime_runs

    if not enabled():
        print("[factory-outcome-reconcile] Neon unavailable; nothing to reconcile", file=sys.stderr)
        return 0
    summary = reconcile(
        args.repo, recorded=recorded_runtime_runs, record=_record,
        now=datetime.now(UTC), hours=max(1, args.hours),
        limit=max(0, args.limit), exclude=set(args.exclude_run),
    )
    print(f"[factory-outcome-reconcile] {json.dumps(summary, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
