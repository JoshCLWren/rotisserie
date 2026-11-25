"""A dispatch job lists live worker runs once and keeps its own dispatches visible."""

from __future__ import annotations

import importlib.util
import sys
import types
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / ".github/scripts/factory-work-controller.py"
NOW = 2_000_000_000
PASS_READS = 8


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    monkeypatch.delenv("FACTORY_DISCOVERY_DIR", raising=False)
    monkeypatch.delenv("FACTORY_ACTIVE_WORKERS_MAX_AGE", raising=False)
    name = "factory_work_controller_active_reads"
    spec = importlib.util.spec_from_file_location(name, CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    monkeypatch.setitem(
        sys.modules,
        "factory_runtime_view",
        types.SimpleNamespace(run_workers=lambda repo, ids: {}),
    )
    return module


def _stamp(age: int) -> str:
    return datetime.fromtimestamp(NOW - age, tz=UTC).isoformat().replace("+00:00", "Z")


class FakeGitHub:
    def __init__(self, *, fork_runs: int = 0, fail_in_progress: int = 0) -> None:
        self.calls: Counter[str] = Counter()
        self.fork_runs = fork_runs
        self.fail_in_progress = fail_in_progress

    def __call__(self, args: list[str], **_: object) -> object:
        path = next(arg for arg in args if arg.startswith("repos/"))
        endpoint = path.split("?")[0].split("/", 3)[3]
        self.calls[endpoint] += 1
        if "factory-fork-execution.yml/runs?per_page=1" in path:
            return {"total_count": self.fork_runs, "workflow_runs": []}
        if "/jobs" in path:
            return [{"jobs": []}]
        if "status=in_progress" in path and self.fail_in_progress:
            self.fail_in_progress -= 1
            raise RuntimeError("listing failed")
        if "free-model-factory-entry.yml" in path and "status=queued" in path:
            return [
                {
                    "workflow_runs": [
                        {"id": 1, "display_title": "Factory 13 · fixed-model entry"},
                        {
                            "id": 99,
                            "status": "queued",
                            "created_at": _stamp(90_000),
                            "updated_at": _stamp(90_000),
                        },
                    ]
                }
            ]
        return [{"workflow_runs": []}]

    def total(self) -> int:
        return sum(self.calls.values())


def test_a_dispatch_pass_lists_live_runs_once(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_DISCOVERY_DIR", str(tmp_path))
    fake = FakeGitHub()
    monkeypatch.setattr(controller, "gh_json", fake)
    results = [controller.active_fixed_workers() for _ in range(PASS_READS)]
    assert all(result == {13} for result in results)
    assert fake.calls["actions/runs/99/jobs"] == 1
    assert fake.calls["actions/workflows/factory-fork-execution.yml/runs"] == 1
    assert fake.total() <= 4


def test_without_a_job_directory_every_read_is_live(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub()
    monkeypatch.setattr(controller, "gh_json", fake)
    for _ in range(3):
        controller.active_fixed_workers()
    assert fake.calls["actions/workflows/free-model-factory-entry.yml/runs"] == 6
    assert fake.calls["actions/workflows/factory-fork-execution.yml/runs"] == 6


def test_a_fork_workflow_with_runs_is_still_listed(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_DISCOVERY_DIR", str(tmp_path))
    fake = FakeGitHub(fork_runs=3)
    monkeypatch.setattr(controller, "gh_json", fake)
    controller.active_fixed_workers()
    assert fake.calls["actions/workflows/factory-fork-execution.yml/runs"] == 3


def test_a_failed_listing_is_never_kept(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_DISCOVERY_DIR", str(tmp_path))
    fake = FakeGitHub(fail_in_progress=1)
    monkeypatch.setattr(controller, "gh_json", fake)
    first = controller.active_fixed_workers()
    assert "in_progress-run-query-unavailable" in first[1]
    second = controller.active_fixed_workers()
    assert second[1] == set()
    assert fake.calls["actions/workflows/free-model-factory-entry.yml/runs"] == 4


def test_a_worker_this_job_dispatched_counts_as_live(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_DISCOVERY_DIR", str(tmp_path))
    fake = FakeGitHub()
    monkeypatch.setattr(controller, "gh_json", fake)
    controller.active_fixed_workers()
    before = fake.total()
    assert controller.factory_active_workers.note_dispatched(controller.REPO, "41")
    assert controller.active_fixed_workers() == {13, 41}
    assert fake.total() == before


def test_an_unconfirmed_dispatch_forces_a_live_read(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_DISCOVERY_DIR", str(tmp_path))
    fake = FakeGitHub()
    monkeypatch.setattr(controller, "gh_json", fake)
    controller.active_fixed_workers()
    controller.factory_active_workers.forget(controller.REPO)
    controller.active_fixed_workers()
    assert fake.calls["actions/workflows/free-model-factory-entry.yml/runs"] == 4


def test_an_old_memo_is_read_again(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_DISCOVERY_DIR", str(tmp_path))
    fake = FakeGitHub()
    monkeypatch.setattr(controller, "gh_json", fake)
    controller.active_fixed_workers()
    monkeypatch.setattr(controller.time, "time", lambda: NOW + 301)
    controller.active_fixed_workers()
    assert fake.calls["actions/workflows/free-model-factory-entry.yml/runs"] == 4
