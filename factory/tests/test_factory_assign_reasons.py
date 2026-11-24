"""Assignment after the store cutover: conflicts stay visible and every refusal is named."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests import turso_support as support

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
from factory_work_policy_base import pr_is_conflicted  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = REPO_ROOT / ".github/scripts/factory-work-controller.py"
HEAD = "a" * 40


@pytest.fixture(scope="module")
def controller() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "factory_work_controller_reasons", CONTROLLER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rest_listing_row(number: int) -> dict[str, Any]:
    """A ``gh pr list`` row as the REST shim emits it: the list endpoint has no merge state."""
    return {
        "number": number,
        "title": "t",
        "body": "",
        "headRefName": "b",
        "headRefOid": HEAD,
        "labels": [{"name": "factory"}, {"name": "factory:changes-requested"}],
        "mergeable": None,
        "mergeStateStatus": "UNKNOWN",
        "isDraft": False,
        "isCrossRepository": False,
        "createdAt": "2026-11-24T00:00:00Z",
    }


def test_conflict_listed_through_the_rest_shim_is_live_executable(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5681 and #5674 never had CI on their conflicted heads; repair must still be leased."""
    monkeypatch.delenv("FACTORY_DISCOVERY_DIR", raising=False)
    monkeypatch.delenv("FACTORY_DISCOVERY_TARGET", raising=False)
    live_pr = {
        "number": 5681,
        "state": "open",
        "mergeable": False,
        "mergeable_state": "dirty",
        "head": {"sha": HEAD, "ref": "b", "repo": {"full_name": "o/r"}},
        "base": {"repo": {"full_name": "o/r"}},
        "labels": [],
    }

    def gh_json(args: list[str], **_: object) -> object:
        if args[:2] == ["pr", "list"]:
            return [rest_listing_row(5681), {**rest_listing_row(9), "labels": []}]
        if args[1].endswith("/pulls/5681"):
            return live_pr
        if "/check-runs" in args[1]:
            return {"total_count": 0, "check_runs": []}
        raise AssertionError(f"unexpected read {args}")

    monkeypatch.setattr(controller, "gh_json", gh_json)
    rows = controller.list_prs()
    assert [pr_is_conflicted(row) for row in rows] == [True, False]

    candidate = controller.Candidate(
        "pr",
        5681,
        3,
        0,
        "2026-11-24T00:00:00Z",
        stage="factory:changes-requested",
        conflicted=pr_is_conflicted(rows[0]),
    )
    monkeypatch.setattr(
        controller, "target_json", lambda n: {"labels": [{"name": "factory:changes-requested"}]}
    )
    monkeypatch.setattr(controller, "required_checks_outcome", lambda n: "waiting")
    assert controller.candidate_is_live_executable(candidate)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound("file", tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def ready_pr(controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, repo: str) -> Any:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(controller, "REPO", repo)
    monkeypatch.setattr(controller, "target_still_unowned", lambda n: True)
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda c: True)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda w: False)
    monkeypatch.setattr(
        controller, "replace_factory_labels", lambda *a, **k: pytest.fail("no GitHub write")
    )
    return controller.Candidate("pr", 5756, 3, 0, "2026-11-24T00:00:00Z", stage="factory:review")


def test_store_lease_left_by_the_cutover_is_named_in_the_none_reply(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, store, capsys
) -> None:
    """A migrated claim released only on Neon blocks the PR; the dispatcher must say so."""
    _, repo = store
    holder, worker = support.uncapped_workers(2)
    assert controller.acquire_lease(repo, holder, [5756])
    candidate = ready_pr(controller, monkeypatch, repo)
    monkeypatch.setattr(controller, "worker_skip_reason", lambda w: None)
    monkeypatch.setattr(
        controller,
        "assign_from_pool",
        lambda *a, **k: controller.assign_candidate(candidate, worker) or None,
    )
    monkeypatch.setattr(controller, "is_authorized_fixed_worker", lambda w: True)
    monkeypatch.setattr(
        sys, "argv", ["controller", "assign", "--worker", worker, "--batch-reconciled"]
    )

    assert controller.main() == 0
    reply = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert reply["kind"] == "none" and reply["reason"] == "no_work"
    assert reply["detail"].startswith(f"#5756: store lease held: #5756 held by {holder} until ")


def test_store_error_during_assignment_fails_loudly(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, store
) -> None:
    _, repo = store
    candidate = ready_pr(controller, monkeypatch, repo)

    def broken(*_a: object, **_k: object) -> str:
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(controller, "acquire_lease", broken)
    with pytest.raises(RuntimeError, match="factory store unavailable during assignment"):
        controller.assign_candidate(candidate, support.uncapped_workers(1)[0])


def test_paused_claims_are_a_named_no_work_reason(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, store
) -> None:
    _, repo = store
    candidate = ready_pr(controller, monkeypatch, repo)

    def paused(*_a: object, **_k: object) -> str:
        raise support.core.ClaimsPaused("store usage is critical")

    monkeypatch.setattr(controller, "acquire_lease", paused)
    controller.trace.reset()
    assert controller.assign_candidate(candidate, support.uncapped_workers(1)[0]) is False
    assert controller.trace.summary() == "#5756: new claims paused: store usage is critical"
