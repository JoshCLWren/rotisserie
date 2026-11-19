"""Regression coverage for dispatcher-only numbered lease assignment."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = REPO_ROOT / ".github" / "scripts" / "factory-work-controller.py"
REPOSITORY = "JoshCLWren/comic-pile"
DISPATCHER_WORKFLOW_REF = (
    f"{REPOSITORY}/.github/workflows/fixed-model-factory-dispatch.yml@refs/heads/main"
)


@pytest.fixture()
def controller() -> types.ModuleType:
    """Load a fresh controller module for isolated environment tests."""
    name = "factory_work_controller_dispatcher_authorization"
    spec = importlib.util.spec_from_file_location(name, CONTROLLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))
    spec.loader.exec_module(module)
    return module


def set_actions_identity(
    monkeypatch: pytest.MonkeyPatch,
    workflow_ref: str | None = DISPATCHER_WORKFLOW_REF,
    *,
    workflow_name: str = "Fixed Model Factory Dispatcher",
) -> None:
    """Set the Actions identity used by dispatcher authorization."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    if workflow_ref is None:
        monkeypatch.delenv("GITHUB_WORKFLOW_REF", raising=False)
    else:
        monkeypatch.setenv("GITHUB_WORKFLOW_REF", workflow_ref)
    monkeypatch.setenv("GITHUB_WORKFLOW", workflow_name)


def test_dispatcher_workflow_file_is_authorized(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical dispatcher file passes authorization."""
    set_actions_identity(monkeypatch)

    assert controller.dispatcher_identity_verified() is True


def test_non_dispatcher_workflow_file_is_denied(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different Actions workflow cannot create a numbered lease."""
    set_actions_identity(
        monkeypatch,
        f"{REPOSITORY}/.github/workflows/free-model-factory-entry.yml@refs/heads/main",
    )

    assert controller.dispatcher_identity_verified() is False


def test_display_name_cannot_spoof_dispatcher_identity(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching display name cannot replace workflow-file identity."""
    set_actions_identity(
        monkeypatch,
        f"{REPOSITORY}/.github/workflows/not-the-dispatcher.yml@refs/heads/main",
        workflow_name="Fixed Model Factory Dispatcher",
    )

    assert controller.dispatcher_identity_verified() is False


def test_missing_workflow_identity_is_denied_in_actions(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actions invocation fails closed without workflow-file identity."""
    set_actions_identity(monkeypatch, workflow_ref=None)

    assert controller.dispatcher_identity_verified() is False


def test_local_invocation_remains_available(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local operator and test invocations are not Actions-gated."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_WORKFLOW_REF", raising=False)

    assert controller.dispatcher_identity_verified() is True


def test_unauthorized_actions_workflow_cannot_replace_numbered_labels(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization is enforced before a numbered ownership write."""
    set_actions_identity(
        monkeypatch,
        f"{REPOSITORY}/.github/workflows/free-model-factory-entry.yml@refs/heads/main",
    )
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda _number: {"labels": [{"name": "factory:unowned"}]},
    )
    writes: list[tuple[list[str], object | None]] = []

    def fake_run(
        args: list[str],
        *,
        input_json: object | None = None,
        check: bool = True,
    ) -> str:
        writes.append((args, input_json))
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run)

    with pytest.raises(RuntimeError, match="dispatcher authorization failed"):
        controller.replace_factory_labels(2859, "factory:46", "factory:building")

    assert writes == []


def test_dispatcher_can_replace_numbered_labels(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical dispatcher can create a numbered ownership label."""
    set_actions_identity(monkeypatch)
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda _number: {"labels": [{"name": "factory:unowned"}]},
    )
    writes: list[tuple[list[str], object | None]] = []

    def fake_run(
        args: list[str],
        *,
        input_json: object | None = None,
        check: bool = True,
    ) -> str:
        writes.append((args, input_json))
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run)

    controller.replace_factory_labels(2859, "factory:46", "factory:building")

    payload = cast(dict[str, Any], writes[0][1])
    assert set(cast(list[str], payload["labels"])) == {
        "factory",
        "factory:46",
        "factory:building",
    }


def test_non_dispatcher_can_release_numbered_lease(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery workflows retain release authority without assignment authority."""
    set_actions_identity(
        monkeypatch,
        f"{REPOSITORY}/.github/workflows/free-model-factory-entry.yml@refs/heads/main",
    )
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda _number: {
            "labels": [
                {"name": "factory"},
                {"name": "factory:46"},
                {"name": "factory:review"},
            ]
        },
    )
    writes: list[dict[str, Any]] = []

    def fake_run(
        args: list[str],
        *,
        input_json: object | None = None,
        check: bool = True,
    ) -> str:
        assert isinstance(input_json, dict)
        writes.append(input_json)
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run)

    controller.replace_factory_labels(2859, "factory:unowned")

    assert "factory:unowned" in writes[0]["labels"]


def test_assign_candidate_denies_unauthorized_actions_workflow(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unauthorized Actions assignment returns before reading or mutating targets."""
    set_actions_identity(
        monkeypatch,
        f"{REPOSITORY}/.github/workflows/free-model-factory-entry.yml@refs/heads/main",
    )
    candidate = controller.Candidate(
        kind="issue",
        number=2859,
        lane=1,
        priority=1,
        created_at="2026-01-01T00:00:00Z",
    )

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unauthorized assignment attempted a target read")

    monkeypatch.setattr(controller, "target_json", fail_if_called)

    assert controller.assign_candidate(candidate, "46") is False


def test_assign_candidate_rechecks_worker_lease_before_mutation(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that became busy after selection receives no second lease."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    candidate = controller.Candidate(
        kind="issue",
        number=2859,
        lane=1,
        priority=1,
        created_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(controller, "target_still_unowned", lambda _number: True)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _worker: True)
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda *args: writes.append(args),
    )

    assert controller.assign_candidate(candidate, "46") is False
    assert writes == []


def test_assign_candidate_rechecks_target_before_mutation(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target claimed after the snapshot is not mutated by stale assignment."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    candidate = controller.Candidate(
        kind="issue",
        number=2859,
        lane=1,
        priority=1,
        created_at="2026-01-01T00:00:00Z",
    )
    calls = 0

    def target_state(_number: int) -> bool:
        nonlocal calls
        calls += 1
        return calls <= 1

    monkeypatch.setattr(controller, "target_still_unowned", target_state)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _worker: False)
    monkeypatch.setattr(controller, "target_owned_by", lambda _number, _owner: True)
    monkeypatch.setattr(controller, "record_controller_lease_activity", lambda *args: None)
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda *args: writes.append(args),
    )
    # Mock the GitHub API call to avoid GH_TOKEN error
    monkeypatch.setattr(controller, "target_json", lambda _number: {"labels": [{"name": "factory:unowned"}]})

    assert controller.assign_candidate(candidate, "46") is False
    assert writes == []
