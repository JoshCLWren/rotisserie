"""Deferred Entry outcome persistence after GitHub rate-limit failures."""
from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
RECONCILE = importlib.import_module("factory_outcome_reconcile")

REPO = "owner/repo"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
WORKER = "1236998703304"
TAG = f"[factory:{WORKER}][source:nvidia][model:a/b] "
LOG = (
    f"Factory {WORKER} assigned dynamic provider slot: source=nvidia configured=true\n"
    f"{TAG}checked out issue #4843 on factory/work\n"
    f"{TAG}agent exit status 0 for issue #4843\n"
    f"{TAG}opened/updated PR #5298 for issue #4843\n"
    f"{TAG}released issue #4843 to factory:unowned at factory:review (handoff)\n"
    f"{TAG}released pr #5298 to factory:unowned at factory:review (handoff)\n"
)


def _run(run_id: int, worker: str = WORKER, **extra: object) -> dict[str, Any]:
    return {
        "id": run_id, "display_title": f"Factory {worker} · fixed-model entry",
        "event": "workflow_dispatch", "head_repository": {"full_name": REPO}, **extra,
    }


def _api(runs: list[dict[str, Any]], *, fail_on: str = "", persist_status: str = "completed"):
    calls: list[str] = []

    def api(path: str, *, raw: bool = False) -> str:
        calls.append(path)
        if fail_on and fail_on in path:
            raise RECONCILE.GitHubUnavailableError("API rate limit exceeded for installation")
        if "/workflows/" in path:
            return json.dumps({"workflow_runs": runs})
        if path.endswith("/jobs?per_page=100"):
            return json.dumps({"jobs": [
                {"id": 7, "name": "run / factory", "status": "completed"},
                {"id": 8, "name": RECONCILE.PERSIST_JOB, "status": persist_status},
            ]})
        if path.endswith("/jobs/7/logs"):
            assert raw
            return LOG
        if path.endswith("/pulls/5298"):
            return json.dumps({"number": 5298, "state": "open", "merged": False,
                               "head": {"sha": "a" * 40, "repo": {"full_name": REPO}}})
        raise AssertionError(path)

    return api, calls


def _reconcile(api, *, recorded: set[str], limit: int = 3, exclude: set[str] | None = None):
    persisted: list[tuple[str, dict[str, Any]]] = []
    summary = RECONCILE.reconcile(
        REPO, api=api, recorded=lambda repo, ids: recorded & set(ids),
        record=lambda repo, run_id, outcome, log: persisted.append((run_id, outcome)) or 1,
        now=NOW, hours=12, limit=limit, exclude=exclude or set(),
    )
    return summary, persisted


def test_missing_rate_limited_outcome_is_persisted_with_trusted_worker() -> None:
    api, _ = _api([_run(44031401652), _run(44000000001)])
    summary, persisted = _reconcile(api, recorded={"44000000001"})
    assert summary["missing"] == ["44031401652"]
    assert [run_id for run_id, _ in persisted] == ["44031401652"]
    outcome = persisted[0][1]
    assert outcome["worker"] == WORKER
    assert outcome["delivery_pr"] == "5298"
    assert outcome["reconciled"] is True


def test_recorded_runs_are_never_reprojected() -> None:
    api, calls = _api([_run(1), _run(2)])
    summary, persisted = _reconcile(api, recorded={"1", "2"})
    assert persisted == [] and summary["missing"] == []
    assert not any("/logs" in call for call in calls)


def test_still_rate_limited_github_defers_without_hammering() -> None:
    api, calls = _api([_run(1), _run(2), _run(3)], fail_on="/runs/1/jobs")
    summary, persisted = _reconcile(api, recorded=set())
    assert persisted == []
    assert "rate limit" in summary["deferred"]
    assert sum("/jobs?per_page" in call for call in calls) == 1


def test_untrusted_or_in_flight_runs_are_skipped() -> None:
    runs = [
        _run(1, display_title="Factory 9 · injected"),
        {**_run(2), "event": "push"},
        {**_run(3), "head_repository": {"full_name": "fork/repo"}},
        _run(4),
    ]
    api, _ = _api(runs, persist_status="in_progress")
    summary, persisted = _reconcile(api, recorded=set(), exclude={"5"})
    assert summary["checked"] == 1
    assert summary["skipped"] == ["4"] and persisted == []


def test_limit_bounds_one_pass() -> None:
    api, _ = _api([_run(n) for n in (1, 2, 3, 4)])
    summary, persisted = _reconcile(api, recorded=set(), limit=2)
    assert len(persisted) == 2 and len(summary["missing"]) == 4


def test_entry_persister_runs_reconciliation_without_owning_the_verdict() -> None:
    entry = yaml.safe_load((ROOT / ".github/workflows/free-model-factory-entry.yml").read_text())
    steps = entry["jobs"]["persist_outcome"]["steps"]
    step = next(s for s in steps if s.get("name") == "Reconcile earlier Entry outcomes missing from Neon")
    assert step["if"] == "${{ always() }}"
    assert step["continue-on-error"] is True
    assert '--exclude-run "$GITHUB_RUN_ID"' in step["run"]
    assert entry["jobs"]["persist_outcome"]["permissions"]["actions"] == "read"
