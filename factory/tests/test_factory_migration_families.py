"""Adopter-declared migration families, one serialized lane per family (Rotisserie #366)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import factory_migration_families as families
import pytest
from factory_adopter import AdopterConfigError
from pytest import MonkeyPatch

from tests.test_factory_migration_lane import SCRIPTS, load_lane

REFINERY = {
    "family": "refinery",
    "directory": "native/migrate/migrations/",
    "version": r"^V(?P<version>[0-9]+)__[A-Za-z0-9_]+\.sql$",
}
ALEMBIC = {"family": "alembic", "directory": "alembic/versions/"}
BOTH = families.families({"migrations": [REFINERY, ALEMBIC]})


def test_alembic_is_the_default_and_refinery_is_declared() -> None:
    assert [f.name for f in families.families({})] == ["alembic"]
    assert [f.name for f in BOTH] == ["refinery", "alembic"]
    assert families.fingerprint(BOTH) != families.fingerprint(families.families({}))


@pytest.mark.parametrize(
    "entry",
    [
        {**REFINERY, "version": r"^V([0-9]+)__.*\.sql$"},
        {**REFINERY, "version": "("},
        {**REFINERY, "directory": "/abs/"},
        {**REFINERY, "directory": "no-slash"},
        {**REFINERY, "family": "Refinery!"},
    ],
)
def test_an_invalid_family_fails_closed(entry: dict[str, Any]) -> None:
    with pytest.raises(AdopterConfigError):
        families.families({"migrations": [entry]})


def test_repeated_or_nested_families_fail_closed() -> None:
    with pytest.raises(AdopterConfigError):
        families.families({"migrations": [REFINERY, {**REFINERY, "directory": "other/"}]})
    with pytest.raises(AdopterConfigError):
        families.families({"migrations": [REFINERY, {**ALEMBIC, "directory": "native/"}]})


def test_changes_read_versions_and_flag_ambiguous_files() -> None:
    def one(path: str, status: str = "added") -> list[families.Change]:
        return families.changes({"filename": path, "status": status}, BOTH)

    (added,) = one("native/migrate/migrations/V9__reading_lists.sql")
    assert (added.family, added.version, added.ambiguous) == ("refinery", 9, False)
    (edited,) = one("native/migrate/migrations/V8__retire.sql", "modified")
    assert edited.version == 8 and edited.ambiguous
    (odd,) = one("native/migrate/migrations/README.md")
    assert odd.version is None and odd.ambiguous
    assert one("scripts/seed.sql") == []
    (alembic,) = one("alembic/versions/123_add_column.py")
    assert alembic.family == "alembic" and not alembic.ambiguous


def lane_with(monkeypatch: MonkeyPatch, declared: tuple[families.Family, ...]) -> Any:
    lane = load_lane()
    monkeypatch.setattr(lane, "declared", lambda: declared)
    return lane


def pr(lane: Any, number: int, stage: str, fams: set[str], *, waiting: bool = False,
       created: str = "2026-11-27T10:00:00Z") -> Any:
    labels = {"factory", "factory:unowned", stage} | ({lane.WAIT_LABEL} if waiting else set())
    return lane.MigrationPr(number, created, frozenset(labels), f"factory/1-{number}-x",
                            frozenset(fams))


def test_two_concurrent_refinery_v9_prs_serialize(monkeypatch: MonkeyPatch) -> None:
    lane = lane_with(monkeypatch, BOTH)
    first = pr(lane, 10, "factory:ready", {"refinery"}, created="2026-11-27T09:00:00Z")
    second = pr(lane, 11, "factory:review", {"refinery"})
    holders, park, release, stale = lane.plan_lanes([first, second])
    assert holders == {"refinery": 10} and park == (11,) and release == () and stale == ()


def test_separate_families_do_not_serialize_each_other(monkeypatch: MonkeyPatch) -> None:
    lane = lane_with(monkeypatch, BOTH)
    rust = pr(lane, 10, "factory:review", {"refinery"})
    python = pr(lane, 11, "factory:review", {"alembic"})
    holders, park, _release, _stale = lane.plan_lanes([rust, python])
    assert holders == {"alembic": 11, "refinery": 10} and park == ()


def test_a_waiter_is_released_only_when_every_family_it_touches_is_free(
    monkeypatch: MonkeyPatch,
) -> None:
    lane = lane_with(monkeypatch, BOTH)
    holder = pr(lane, 10, "factory:review", {"alembic"})
    waiter = pr(lane, 11, "factory:blocked", {"refinery", "alembic"}, waiting=True)
    assert lane.plan_lanes([holder, waiter])[2] == ()
    assert lane.plan_lanes([waiter])[2] == (11,)


def test_a_stale_wait_label_is_released_through_reconcile(monkeypatch: MonkeyPatch) -> None:
    lane = lane_with(monkeypatch, BOTH)
    stale = pr(lane, 12, "factory:blocked", set(), waiting=True)
    released: list[int] = []
    monkeypatch.setattr(lane, "current_migration_prs", lambda: [stale])
    monkeypatch.setattr(lane, "release_pr", lambda row: released.append(row.number))
    monkeypatch.setattr(lane, "park_pr", lambda row: pytest.fail("nothing to park"))
    result = lane.reconcile()
    assert released == [12] and result["stale_released"] == [12] and result["waiting"] == []


def test_a_dry_run_plans_but_writes_nothing(monkeypatch: MonkeyPatch) -> None:
    lane = lane_with(monkeypatch, BOTH)
    rows = [pr(lane, 10, "factory:ready", {"refinery"}), pr(lane, 11, "factory:review", {"refinery"})]
    monkeypatch.setattr(lane, "current_migration_prs", lambda: rows)
    for name in ("park_pr", "release_pr", "ensure_wait_label"):
        monkeypatch.setattr(lane, name, lambda *_: pytest.fail("a dry run writes nothing"))
    result = lane.reconcile(dry_run=True)
    assert result["lanes"] == {"refinery": 10} and result["parked"] == [11]


class Memo:
    def __init__(self) -> None:
        self.facts: dict[str, Any] = {}

    def get(self, sha: str) -> Any:
        return self.facts.get(sha)

    def put(self, sha: str, value: Any) -> None:
        self.facts[sha] = value


def test_a_changed_head_is_read_again_and_a_known_head_is_not(monkeypatch: MonkeyPatch) -> None:
    lane = lane_with(monkeypatch, BOTH)
    reads: list[int] = []
    files = {"a": [{"filename": "docs/x.md"}],
             "b": [{"filename": "native/migrate/migrations/V9__x.sql", "status": "added"}]}
    head = {"sha": "a"}

    def changed(number: int) -> list[dict[str, Any]]:
        reads.append(number)
        return files[head["sha"]]

    monkeypatch.setattr(lane, "pr_changed_files", changed)
    memo = Memo()
    assert lane.touched_families(7, "a", memo) == frozenset()
    assert lane.touched_families(7, "a", memo) == frozenset()
    head["sha"] = "b"
    assert lane.touched_families(7, "b", memo) == frozenset({"refinery"})
    assert reads == [7, 7]


def test_a_checkpoint_branch_finds_its_issue(monkeypatch: MonkeyPatch) -> None:
    lane = lane_with(monkeypatch, BOTH)
    row = lane.MigrationPr(5, "t", frozenset(), "factory/work-checkpoints/issue-5701/m/1",
                           frozenset({"refinery"}), "Part of #5701.", "V2 API 01")
    assert lane.linked_issue(row) == 5701


def test_an_invalid_config_exits_2_before_any_github_call(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[[migrations]]\nfamily = "refinery"\ndirectory = "x/"\nversion = "("\n')
    env = {**os.environ, "ROTISSERIE_CONFIG": str(config), "GITHUB_REPOSITORY": "o/r",
           "PATH": str(tmp_path)}
    out = subprocess.run([sys.executable, str(SCRIPTS / "factory_migration_lane.py"), "reconcile"],
                         env=env, text=True, capture_output=True, check=False, cwd=SCRIPTS)
    assert out.returncode == 2, out.stderr
    assert "migration lane config" in out.stderr
