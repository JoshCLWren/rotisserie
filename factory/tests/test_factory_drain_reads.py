"""The merge drain asks GitHub only about heads it has not already seen."""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
sys.path.insert(0, str(SCRIPTS))
core = importlib.import_module("factory_turso_core")
presence = importlib.import_module("factory_drain_run_presence")
memo_module = importlib.import_module("factory_head_memo")
DRAIN = ROOT.parent / ".github/workflows/factory-ready-merge-drain.yml"
CALLER = ROOT / "install/callers/factory-ready-merge-drain.yml"
SHIM = SCRIPTS / "factory-gh-rest-shim.sh"
REPO = "acme/widgets"


def _lane() -> object:
    path = SCRIPTS / "factory_migration_lane.py"
    spec = importlib.util.spec_from_file_location("drain_reads_lane", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["drain_reads_lane"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "store.sqlite"))
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    core.use_store(None)
    yield core.store()
    core.use_store(None)


def _drain(
    heads: list[tuple[str, str, str]],
    counts: dict[tuple[str, str], int | None],
    asked: list[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    memo = memo_module.HeadMemo(REPO, presence.MEMO)
    found = presence.missing(REPO, heads, memo)
    memo.save({sha for sha, _, _ in heads})
    return found


def test_a_head_with_runs_is_counted_once_across_drains(
    store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []
    counts: dict[tuple[str, str], int | None] = {}

    def count(repo: str, workflow: str, sha: str) -> int | None:
        asked.append((sha, workflow))
        return counts.get((sha, workflow), 1)

    monkeypatch.setattr(presence, "run_count", count)
    heads = [(f"{n}" * 40, str(n), f"b{n}") for n in range(1, 4)]
    counts[("3" * 40, "ci.yml")] = 0
    assert _drain(heads, counts, asked) == [("3", "b3", "CI")]
    assert len(asked) == 6
    asked.clear()
    assert _drain(heads, counts, asked) == [("3", "b3", "CI")]
    assert asked == [("3" * 40, "ci.yml")]
    asked.clear()
    counts[("3" * 40, "ci.yml")] = 1
    heads.append(("4" * 40, "4", "b4"))
    assert _drain(heads, counts, asked) == []
    assert sorted(asked) == [
        ("3" * 40, "ci.yml"),
        ("4" * 40, "ci.yml"),
        ("4" * 40, "fixed-model-pr-repair-guard.yml"),
    ]


def test_a_failed_count_dispatches_nothing_and_is_asked_again(
    store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(presence, "run_count", lambda r, w, s: asked.append((s, w)) or None)
    heads = [("a" * 40, "7", "b7")]
    assert _drain(heads, {}, asked) == []
    assert _drain(heads, {}, asked) == []
    assert len(asked) == 4


def test_migration_files_are_read_once_per_head(
    store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = _lane()
    reads: list[int] = []
    pulls = [
        {
            "number": 5,
            "labels": [{"name": "factory"}, {"name": "factory:ready"}],
            "head": {"sha": "c" * 40, "ref": "f/5"},
            "created_at": "2026-11-25T00:00:00Z",
        },
        {
            "number": 6,
            "labels": [{"name": "factory"}, {"name": "factory:ready"}],
            "head": {"sha": "d" * 40, "ref": "f/6"},
            "created_at": "2026-11-25T00:00:00Z",
        },
    ]
    files = {5: [{"filename": "yoyo/migrations/0001.sql"}], 6: [{"filename": "README.md"}]}
    monkeypatch.setattr(lane, "REPO", REPO)
    monkeypatch.setattr(lane, "FINALIZATION_STAGES", ("factory:ready",))
    monkeypatch.setattr(lane, "open_pull_requests", lambda: pulls)
    monkeypatch.setattr(lane, "pr_changed_files", lambda n: reads.append(n) or files[n])
    import factory_migration_families as migration_families

    yoyo = migration_families.families(
        {"migrations": [{"family": "yoyo", "directory": "yoyo/migrations/"}]}
    )
    monkeypatch.setattr(lane, "declared", lambda: yoyo)
    first = [pr.number for pr in lane.current_migration_prs()]
    second = [pr.number for pr in lane.current_migration_prs()]
    assert first == second == [5]
    assert reads == [5, 6]
    pulls[1]["head"] = {"sha": "e" * 40, "ref": "f/6"}
    lane.current_migration_prs()
    assert reads == [5, 6, 6]


def _steps() -> dict[str, dict[str, object]]:
    job = yaml.safe_load(DRAIN.read_text())["jobs"]["merge-ready"]
    return {str(step.get("name")): step for step in job["steps"]}


def test_the_drain_reads_through_the_shim_and_the_read_only_app() -> None:
    steps = _steps()
    install = str(steps["Install REST-backed factory GitHub reads"]["run"])
    assert "factory-gh-rest-shim.sh" in install and "FACTORY_GH_CACHE_PR_CHECKS=0" in install
    assert "FACTORY_DISCOVERY_DIR" not in install
    assert "Mint optional read-only factory App token" in steps
    assert "Verify and enable read-only factory App token" in steps
    recover = str(steps["Recover held or missing workflow runs on factory PR heads"]["run"])
    assert "runs?head_sha=" not in recover and "factory_drain_run_presence.py" in recover
    caller = CALLER.read_text()
    assert "FACTORY_READER_APP_PRIVATE_KEY: ${{ secrets.FACTORY_READER_APP_PRIVATE_KEY }}" in caller


def _shim(tmp_path: Path, *args: str, checks_cached: str) -> None:
    fake = tmp_path / "real-gh"
    fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> {tmp_path / "live"}\n')
    fake.chmod(0o755)
    env = {
        **os.environ,
        "FACTORY_REAL_GH": str(fake),
        "GITHUB_WORKSPACE": str(ROOT),
        "FACTORY_GH_READ_CACHE_DIR": str(tmp_path / "cache"),
        "FACTORY_GH_CACHE_PR_CHECKS": checks_cached,
        "GITHUB_REPOSITORY": "o/r",
    }
    for name in ("FACTORY_GH_CALL_LOG", "FACTORY_READ_TOKEN", "FACTORY_DISCOVERY_DIR"):
        env.pop(name, None)
    subprocess.run(["bash", str(SHIM), *args], env=env, check=True, capture_output=True)


@pytest.mark.parametrize(("cached", "live"), [("1", 1), ("0", 2)])
def test_the_drain_keeps_pr_checks_live(tmp_path: Path, cached: str, live: int) -> None:
    for _ in range(2):
        _shim(tmp_path, "pr", "checks", "5", "--required", "--json", "state", checks_cached=cached)
    runs = ("api", "repos/o/r/actions/workflows/ci.yml/runs?per_page=1")
    for _ in range(2):
        _shim(tmp_path, *runs, checks_cached=cached)
    calls = (tmp_path / "live").read_text().splitlines()
    assert sum(call.startswith("pr checks") for call in calls) == live
    assert sum("ci.yml/runs" in call for call in calls) == 1
