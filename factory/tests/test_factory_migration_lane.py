"""Regression coverage for serialized Alembic migration PR finalization."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pytest import MonkeyPatch

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_lane() -> ModuleType:
    path = SCRIPTS / "factory_migration_lane.py"
    spec = importlib.util.spec_from_file_location("factory_migration_lane_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def migration_pr(
    module: ModuleType,
    number: int,
    stage: str,
    *,
    created_at: str,
    owner: str = "factory:unowned",
    waiting: bool = False,
) -> object:
    labels = {"factory", owner, stage}
    if waiting:
        labels.add(module.WAIT_LABEL)
    return module.MigrationPr(
        number=number,
        created_at=created_at,
        labels=frozenset(labels),
        branch=f"factory/43-{number}-test",
    )


def test_migration_detection_uses_current_changed_file_paths() -> None:
    module = load_lane()

    assert module.changed_file_is_migration(
        {"filename": "alembic/versions/123_add_column.py", "status": "added"}
    )
    assert module.changed_file_is_migration(
        {
            "filename": "docs/old-name.md",
            "previous_filename": "alembic/versions/old_name.py",
            "status": "renamed",
        }
    )
    assert not module.changed_file_is_migration(
        {"filename": "app/models/reading.py", "status": "modified"}
    )


def test_only_one_of_two_migration_finalizers_is_holder() -> None:
    module = load_lane()
    older_review = migration_pr(
        module,
        1971,
        "factory:review",
        created_at="2026-08-28T12:00:00Z",
    )
    newer_review = migration_pr(
        module,
        2000,
        "factory:review",
        created_at="2026-08-29T12:00:00Z",
    )

    holder, park, release = module.plan_lane([newer_review, older_review])

    assert holder == 1971
    assert park == (2000,)
    assert release is None


def test_farthest_progressed_unowned_migration_pr_wins_lane() -> None:
    module = load_lane()
    changes_requested = migration_pr(
        module,
        1971,
        "factory:changes-requested",
        created_at="2026-08-20T12:00:00Z",
    )
    ready = migration_pr(
        module,
        1980,
        "factory:ready",
        created_at="2026-08-29T12:00:00Z",
    )

    holder, park, release = module.plan_lane([changes_requested, ready])

    assert holder == 1980
    assert park == (1971,)
    assert release is None


def test_active_lease_is_not_stolen_by_reconciliation() -> None:
    module = load_lane()
    leased = migration_pr(
        module,
        1971,
        "factory:review",
        created_at="2026-08-29T12:00:00Z",
        owner="factory:57",
    )
    ready = migration_pr(
        module,
        1980,
        "factory:ready",
        created_at="2026-08-20T12:00:00Z",
    )

    holder, park, release = module.plan_lane([ready, leased])

    assert holder == 1971
    assert park == (1980,)
    assert release is None


def test_multiple_active_leases_fail_closed() -> None:
    module = load_lane()
    first = migration_pr(
        module,
        1971,
        "factory:review",
        created_at="2026-08-28T12:00:00Z",
        owner="factory:57",
    )
    second = migration_pr(
        module,
        1980,
        "factory:ci",
        created_at="2026-08-29T12:00:00Z",
        owner="factory:58",
    )

    with pytest.raises(module.LaneConflictError, match="multiple migration finalizers"):
        module.plan_lane([first, second])


def test_genuine_blocked_migration_pr_does_not_hold_lane() -> None:
    module = load_lane()
    blocked = migration_pr(
        module,
        1971,
        "factory:blocked",
        created_at="2026-08-20T12:00:00Z",
    )
    review = migration_pr(
        module,
        1980,
        "factory:review",
        created_at="2026-08-29T12:00:00Z",
    )

    holder, park, release = module.plan_lane([blocked, review])

    assert holder == 1980
    assert park == ()
    assert release is None


def test_waiter_is_released_when_no_active_holder_remains() -> None:
    module = load_lane()
    older_waiter = migration_pr(
        module,
        1971,
        "factory:blocked",
        created_at="2026-08-20T12:00:00Z",
        waiting=True,
    )
    newer_waiter = migration_pr(
        module,
        1980,
        "factory:blocked",
        created_at="2026-08-29T12:00:00Z",
        waiting=True,
    )

    holder, park, release = module.plan_lane([newer_waiter, older_waiter])

    assert holder is None
    assert park == ()
    assert release == 1971


def test_normal_pr_is_not_part_of_migration_lane() -> None:
    module = load_lane()
    migration = migration_pr(
        module,
        1971,
        "factory:review",
        created_at="2026-08-28T12:00:00Z",
    )

    holder, park, release = module.plan_lane([migration])

    assert holder == 1971
    assert park == ()
    assert release is None
    assert not module.changed_file_is_migration({"filename": "frontend/src/App.tsx"})


def test_wait_transitions_preserve_non_factory_labels_and_block_linked_issue(
    monkeypatch: MonkeyPatch,
) -> None:
    module = load_lane()
    pr = module.MigrationPr(
        number=1971,
        created_at="2026-08-28T12:00:00Z",
        labels=frozenset({"factory", "factory:unowned", "factory:review", "bug"}),
        branch="factory/57-1704-reading-effort",
    )
    targets: dict[int, dict[str, Any]] = {
        1971: {
            "state": "open",
            "labels": [
                {"name": "factory"},
                {"name": "factory:unowned"},
                {"name": "factory:review"},
                {"name": "bug"},
            ],
        },
        1704: {
            "state": "open",
            "labels": [
                {"name": "factory"},
                {"name": "factory:unowned"},
                {"name": "factory:review"},
                {"name": "ralph-status:in-review"},
                {"name": "ralph-task"},
                {"name": "ralph-priority:high"},
            ],
        },
    }
    writes: dict[int, set[str]] = {}

    monkeypatch.setattr(module, "target_json", lambda number: targets[number])
    monkeypatch.setattr(module, "replace_labels", lambda number, labels: writes.__setitem__(number, labels))

    module.park_pr(pr)

    assert writes[1971] == {
        "factory",
        "factory:unowned",
        "factory:blocked",
        module.WAIT_LABEL,
        "bug",
    }
    assert writes[1704] == {
        "factory",
        "factory:unowned",
        "factory:blocked",
        module.WAIT_LABEL,
        "ralph-status:blocked",
        "ralph-task",
        "ralph-priority:high",
    }


def test_release_restores_review_state_for_pr_and_linked_issue(
    monkeypatch: MonkeyPatch,
) -> None:
    module = load_lane()
    pr = module.MigrationPr(
        number=1971,
        created_at="2026-08-28T12:00:00Z",
        labels=frozenset(
            {
                "factory",
                "factory:unowned",
                "factory:blocked",
                module.WAIT_LABEL,
            }
        ),
        branch="factory/57-1704-reading-effort",
    )
    targets: dict[int, dict[str, Any]] = {
        1971: {
            "state": "open",
            "labels": [
                {"name": "factory"},
                {"name": "factory:unowned"},
                {"name": "factory:blocked"},
                {"name": module.WAIT_LABEL},
                {"name": "bug"},
            ],
        },
        1704: {
            "state": "open",
            "labels": [
                {"name": "factory"},
                {"name": "factory:unowned"},
                {"name": "factory:blocked"},
                {"name": module.WAIT_LABEL},
                {"name": "ralph-status:blocked"},
                {"name": "ralph-task"},
            ],
        },
    }
    writes: dict[int, set[str]] = {}

    monkeypatch.setattr(module, "target_json", lambda number: targets[number])
    monkeypatch.setattr(module, "replace_labels", lambda number, labels: writes.__setitem__(number, labels))

    module.release_pr(pr)

    assert writes[1971] == {
        "factory",
        "factory:unowned",
        "factory:review",
        "bug",
    }
    assert writes[1704] == {
        "factory",
        "factory:unowned",
        "factory:review",
        "ralph-status:in-review",
        "ralph-task",
    }


def test_closed_lane_holder_immediately_releases_oldest_waiter(
    monkeypatch: MonkeyPatch,
) -> None:
    """Simulate the close event removing the holder from the open-PR snapshot."""
    module = load_lane()
    holder = migration_pr(
        module, 3200, "factory:review", created_at="2026-10-07T10:00:00Z"
    )
    waiter = migration_pr(
        module, 3201, "factory:blocked",
        created_at="2026-10-07T11:00:00Z", waiting=True
    )
    snapshots = iter([[holder, waiter], [waiter]])
    released: list[int] = []
    monkeypatch.setattr(module, "ensure_wait_label", lambda: None)
    monkeypatch.setattr(module, "current_migration_prs", lambda: next(snapshots))
    monkeypatch.setattr(module, "release_pr", lambda pr: released.append(pr.number))
    monkeypatch.setattr(module, "park_pr", lambda pr: pytest.fail("must not park a waiter"))

    assert module.reconcile()["holder"] == 3200
    result = module.reconcile()
    assert result["holder"] == 3201
    assert result["released"] == 3201
    assert released == [3201]


def test_closed_factory_pr_triggers_trusted_migration_reconciliation() -> None:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/factory-migration-lane-release.yml"
    ).read_text()
    assert "pull_request_target:" in workflow
    assert "types: [closed, converted_to_draft, labeled, unlabeled]" in workflow
    assert "ref: main" in workflow
    assert "persist-credentials: false" in workflow
    assert "python3 .github/scripts/factory_migration_lane.py reconcile" in workflow
    assert "group: fixed-model-factory-dispatch" in workflow


def test_migration_file_reads_only_for_finalizers_and_waiters(monkeypatch: MonkeyPatch) -> None:
    module = load_lane()
    def row(number: int, labels: set[str]) -> dict[str, Any]:
        return {"number": number, "draft": False, "created_at": "2026-11-16T00:00:00Z",
                "head": {"ref": f"factory/43-{number}-work"},
                "labels": [{"name": label} for label in labels]}
    prs = [
        row(1, {"factory", "factory:building"}),
        row(2, {"factory", "factory:review"}),
        row(3, {"factory", "factory:blocked"}),
        row(4, {"factory", "factory:blocked", module.WAIT_LABEL}),
        row(5, {"factory:review"}),
    ]
    reads: list[int] = []
    monkeypatch.setattr(module, "open_pull_requests", lambda: prs)
    def changed_files(number: int) -> list[dict[str, Any]]:
        reads.append(number)
        return [{"filename": "alembic/versions/123_schema.py"}]
    monkeypatch.setattr(module, "pr_changed_files", changed_files)
    assert [pr.number for pr in module.current_migration_prs()] == [2, 4]
    assert reads == [2, 4]


def test_read_only_migration_sweep_does_not_probe_label_or_change_ownership(monkeypatch: MonkeyPatch) -> None:
    module = load_lane()
    monkeypatch.setattr(module, "current_migration_prs", lambda: [])
    monkeypatch.setattr(module, "ensure_wait_label", lambda: pytest.fail("no park: no label probe"))
    monkeypatch.setattr(module, "replace_labels", lambda *_: pytest.fail("no lane work: no labels"))
    assert module.reconcile() == {"holder": None, "parked": [], "released": None, "waiting": []}



def test_migration_lane_classifies_actual_api_quota_not_permissions(
    monkeypatch: MonkeyPatch,
) -> None:
    module = load_lane()

    def failure(message: str):
        def run(*args: object, **kwargs: object) -> object:
            return module.subprocess.CompletedProcess(
                ["gh", "api"], 1, "", message,
            )
        return run

    monkeypatch.setattr(
        module.subprocess, "run",
        failure("gh: API rate limit exceeded for installation"),
    )
    with pytest.raises(module.GitHubQuotaDeferredError):
        module.run_gh(["api", "repos/example/repo/pulls?state=open"])

    monkeypatch.setattr(
        module.subprocess, "run",
        failure("gh: HTTP 403: Resource not accessible by integration"),
    )
    with pytest.raises(RuntimeError, match="Resource not accessible"):
        module.run_gh(["api", "repos/example/repo/pulls?state=open"])


def test_migration_lane_quota_failure_preserves_pending_migrations(
    monkeypatch: MonkeyPatch,
) -> None:
    module = load_lane()
    writes: list[int] = []

    def unavailable() -> list[object]:
        raise module.GitHubQuotaDeferredError("GitHub installation API quota exhausted")

    monkeypatch.setattr(module, "current_migration_prs", unavailable)
    monkeypatch.setattr(module, "replace_labels", lambda *_: writes.append(1))
    with pytest.raises(module.GitHubQuotaDeferredError):
        module.reconcile()
    assert writes == []
