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
