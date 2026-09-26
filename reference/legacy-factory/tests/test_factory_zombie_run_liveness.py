"""Regression coverage for unresolved fixed-model run liveness."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = REPO_ROOT / ".github/scripts/factory-work-controller.py"


@pytest.fixture
def controller() -> types.ModuleType:
    """Load the factory controller for isolated liveness tests."""
    module_name = "factory_work_controller_zombie_liveness"
    spec = importlib.util.spec_from_file_location(module_name, CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def github_time(epoch: int) -> str:
    """Return one UTC epoch as a GitHub-style timestamp."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def unresolved_run(run_id: int, *, now: int, age_seconds: int) -> dict[str, object]:
    """Build one unresolved queued run with deterministic age."""
    timestamp = github_time(now - age_seconds)
    return {
        "id": run_id,
        "display_title": "Fixed Model Factory Entry",
        "status": "queued",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def test_recent_unresolved_run_keeps_global_safety_fence(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recent unresolved queued run must still fail closed."""
    now = 2_000_000_000
    monkeypatch.setattr(controller.time, "time", lambda: now)

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "status=queued" in joined:
            return [{"workflow_runs": [unresolved_run(99, now=now, age_seconds=60)]}]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        if "issues/1093/comments" in joined:
            return [[]]
        if "/actions/runs/99/jobs" in joined:
            raise AssertionError("recent unresolved runs must not require job evidence")
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    workers, unresolved = controller.active_fixed_workers()

    assert workers == set()
    assert unresolved == {"99"}


def test_old_zero_job_zombie_stops_counting_as_live_ambiguity(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old queued run with zero jobs must not freeze the fleet forever."""
    now = 2_000_000_000
    monkeypatch.setattr(controller.time, "time", lambda: now)

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "status=queued" in joined:
            return [{"workflow_runs": [unresolved_run(99, now=now, age_seconds=3_600)]}]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        if "issues/1093/comments" in joined:
            return [[]]
        if "/actions/runs/99/jobs" in joined:
            return [{"total_count": 0, "jobs": []}]
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    workers, unresolved = controller.active_fixed_workers()

    assert workers == set()
    assert unresolved == set()


def test_old_unresolved_run_fails_closed_when_job_evidence_is_unavailable(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing job evidence must remain uncertainty rather than inferred death."""
    now = 2_000_000_000
    monkeypatch.setattr(controller.time, "time", lambda: now)

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "status=queued" in joined:
            return [{"workflow_runs": [unresolved_run(99, now=now, age_seconds=3_600)]}]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        if "issues/1093/comments" in joined:
            return [[]]
        if "/actions/runs/99/jobs" in joined:
            raise RuntimeError("GitHub jobs API unavailable")
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    workers, unresolved = controller.active_fixed_workers()

    assert workers == set()
    assert unresolved == {"99"}


def test_unresolved_run_fails_closed_when_identity_registry_is_unavailable(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed durable identity read must not be mistaken for a zombie."""
    now = 2_000_000_000
    monkeypatch.setattr(controller.time, "time", lambda: now)

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "status=queued" in joined:
            return [{"workflow_runs": [unresolved_run(99, now=now, age_seconds=3_600)]}]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        if "issues/1093/comments" in joined:
            raise RuntimeError("attempt registry unavailable")
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    workers, unresolved = controller.active_fixed_workers()

    assert workers == set()
    assert unresolved == {"99"}


def test_active_run_query_failure_keeps_reconciliation_conservative(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure to enumerate active runs must preserve the global safety fence."""
    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "status=queued" in joined:
            raise RuntimeError("queued run query unavailable")
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    workers, unresolved = controller.active_fixed_workers()

    assert workers == set()
    assert unresolved == {"queued-run-query-unavailable"}


def test_expired_zombie_allows_unrelated_stale_lease_reclamation(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visible zombie must not suppress normal TTL reclamation elsewhere."""
    now = 2_000_000_000
    monkeypatch.setattr(controller.time, "time", lambda: now)

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "status=queued" in joined:
            return [{"workflow_runs": [unresolved_run(99, now=now, age_seconds=3_600)]}]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        if "issues/1093/comments" in joined:
            return [[]]
        if "/actions/runs/99/jobs" in joined:
            return [{"total_count": 0, "jobs": []}]
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    monkeypatch.setattr(controller, "owned_targets", lambda: [(701, "factory:13")])
    monkeypatch.setattr(
        controller,
        "latest_lease_activity_epoch",
        lambda number: now - controller.FIXED_LEASE_TTL_SECONDS - 1,
    )
    writes: list[tuple[int, str]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: writes.append((number, owner)),
    )

    assert controller.reconcile_stale_leases(now_epoch=now) == [701]
    assert writes == [(701, "factory:unowned")]


def test_known_active_worker_lease_remains_protected(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker resolved from an active run must retain its lease despite age."""
    now = 2_000_000_000
    timestamp = github_time(now - 3_600)
    monkeypatch.setattr(controller.time, "time", lambda: now)

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "status=queued" in joined:
            return [
                {
                    "workflow_runs": [
                        {
                            "id": 13,
                            "display_title": "Factory 13 · fixed-model entry",
                            "status": "queued",
                            "created_at": timestamp,
                            "updated_at": timestamp,
                        }
                    ]
                }
            ]
        if "status=in_progress" in joined:
            return [{"workflow_runs": []}]
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)
    monkeypatch.setattr(controller, "owned_targets", lambda: [(701, "factory:13")])
    monkeypatch.setattr(
        controller,
        "latest_lease_activity_epoch",
        lambda number: now - controller.FIXED_LEASE_TTL_SECONDS - 1,
    )
    writes: list[tuple[int, str]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: writes.append((number, owner)),
    )

    assert controller.reconcile_stale_leases(now_epoch=now) == []
    assert writes == []
