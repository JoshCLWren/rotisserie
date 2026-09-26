"""Regression coverage for factory CI-stage reconciliation."""

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

HEAD = "a" * 40
PR_NUMBER = 2001


def load_review_controller() -> ModuleType:
    """Load the hyphenated review controller as a testable module."""
    path = SCRIPTS / "factory-review-controller.py"
    spec = importlib.util.spec_from_file_location("factory_review_controller_ci", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ci_pr_payload() -> dict[str, Any]:
    """Build one unowned PR parked in the CI stage."""
    return {
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "headRefOid": HEAD,
        "headRefName": f"factory/43-{PR_NUMBER}-opencode-free",
        "body": f"Closes #{PR_NUMBER}.\n\nWorker: opencode-free-model-factory-43\n",
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": "factory:ci"},
        ],
    }


def owned_ci_pr_payload() -> dict[str, Any]:
    """Build one CI PR still leased to its producing factory."""
    payload = ci_pr_payload()
    payload["labels"] = [
        {"name": "factory"},
        {"name": "factory:43"},
        {"name": "factory:ci"},
    ]
    return payload


def test_failed_checks_become_actionable_changes_requested(
    monkeypatch: MonkeyPatch,
) -> None:
    """Terminal CI failures persist findings and leave the CI cul-de-sac."""
    module = load_review_controller()
    posted: list[dict[str, object]] = []
    transitions: list[tuple[int, str, str]] = []

    monkeypatch.setattr(module, "pr_json", lambda _pr: ci_pr_payload())
    monkeypatch.setattr(
        module,
        "required_checks_gate",
        lambda _pr: {"decision": "deny", "reason": "required checks failed: FAILURE"},
    )
    monkeypatch.setattr(
        module,
        "post_review_comment",
        lambda **kwargs: posted.append(kwargs),
    )
    monkeypatch.setattr(
        module,
        "replace_factory_labels",
        lambda number, owner, stage: transitions.append((number, owner, stage)),
    )
    monkeypatch.setattr(
        module,
        "mechanical_merge_gate",
        lambda _pr, _head: pytest.fail("terminal failed checks must not continue to merge gates"),
    )

    result = module.reconcile_ci_pr(PR_NUMBER)

    assert result == {
        "pr": PR_NUMBER,
        "status": "changes-requested",
        "reason": "required checks failed: FAILURE",
    }
    assert posted[0]["verdict"] == "repair"
    assert posted[0]["excerpt"] == "required checks failed: FAILURE"
    assert transitions == [(PR_NUMBER, "factory:unowned", "factory:changes-requested")]


def test_owned_failed_checks_override_lease_and_become_actionable_changes_requested(
    monkeypatch: MonkeyPatch,
) -> None:
    """Terminal CI failure overrides an active factory lease and returns to repair."""
    module = load_review_controller()
    posted: list[dict[str, object]] = []
    transitions: list[tuple[int, str, str]] = []

    monkeypatch.setattr(module, "pr_json", lambda _pr: owned_ci_pr_payload())
    monkeypatch.setattr(
        module,
        "required_checks_gate",
        lambda _pr: {"decision": "deny", "reason": "required checks failed: FAILURE"},
    )
    monkeypatch.setattr(
        module,
        "post_review_comment",
        lambda **kwargs: posted.append(kwargs),
    )
    monkeypatch.setattr(
        module,
        "replace_factory_labels",
        lambda number, owner, stage: transitions.append((number, owner, stage)),
    )
    monkeypatch.setattr(
        module,
        "mechanical_merge_gate",
        lambda _pr, _head: pytest.fail("terminal failed checks must not continue to merge gates"),
    )

    result = module.reconcile_ci_pr(PR_NUMBER)

    assert result == {
        "pr": PR_NUMBER,
        "status": "changes-requested",
        "reason": "required checks failed: FAILURE",
    }
    assert posted[0]["verdict"] == "repair"
    assert posted[0]["reviewer"] == "controller"
    assert posted[0]["excerpt"] == "required checks failed: FAILURE"
    assert transitions == [(PR_NUMBER, "factory:unowned", "factory:changes-requested")]


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        ("retry", "required checks are not terminal: PENDING"),
        ("pass", "all required checks are successful"),
    ],
)
def test_owned_nonfailed_checks_keep_existing_lease(
    monkeypatch: MonkeyPatch,
    decision: str,
    reason: str,
) -> None:
    """Only terminal CI failure can override an active factory lease."""
    module = load_review_controller()
    transitions: list[tuple[int, str, str]] = []
    posted: list[dict[str, object]] = []

    monkeypatch.setattr(module, "pr_json", lambda _pr: owned_ci_pr_payload())
    monkeypatch.setattr(
        module,
        "required_checks_gate",
        lambda _pr: {"decision": decision, "reason": reason},
    )
    monkeypatch.setattr(
        module,
        "post_review_comment",
        lambda **kwargs: posted.append(kwargs),
    )
    monkeypatch.setattr(
        module,
        "replace_factory_labels",
        lambda number, owner, stage: transitions.append((number, owner, stage)),
    )
    monkeypatch.setattr(
        module,
        "mechanical_merge_gate",
        lambda _pr, _head: pytest.fail("owned nonfailed CI must not continue to merge gates"),
    )

    result = module.reconcile_ci_pr(PR_NUMBER)

    assert result == {"pr": PR_NUMBER, "status": "owned"}
    assert posted == []
    assert transitions == []


def test_pending_checks_stay_in_ci(monkeypatch: MonkeyPatch) -> None:
    """Non-terminal checks remain parked for cheap CI reconciliation."""
    module = load_review_controller()
    transitions: list[tuple[int, str, str]] = []
    handoffs: list[dict[str, object]] = []

    monkeypatch.setattr(module, "pr_json", lambda _pr: ci_pr_payload())
    monkeypatch.setattr(
        module,
        "required_checks_gate",
        lambda _pr: {"decision": "retry", "reason": "required checks are not terminal: PENDING"},
    )
    monkeypatch.setattr(
        module,
        "replace_factory_labels",
        lambda number, owner, stage: transitions.append((number, owner, stage)),
    )
    monkeypatch.setattr(
        module,
        "persist_repair_handoff",
        lambda **kwargs: handoffs.append(kwargs),
    )
    monkeypatch.setattr(
        module,
        "mechanical_merge_gate",
        lambda _pr, _head: pytest.fail("pending checks must stay in factory:ci"),
    )

    result = module.reconcile_ci_pr(PR_NUMBER)

    assert result == {
        "pr": PR_NUMBER,
        "status": "retry-ci",
        "reason": "required checks are not terminal: PENDING",
    }
    assert transitions == []
    assert handoffs == []


def test_passing_checks_continue_through_authorization_and_mechanical_gates(
    monkeypatch: MonkeyPatch,
) -> None:
    """Green CI proceeds through exact-head authorization and mechanical gating."""
    module = load_review_controller()
    mechanical_calls: list[tuple[int, str]] = []
    transitions: list[tuple[int, str, str]] = []
    approval = module.review_marker(
        pr=PR_NUMBER,
        head=HEAD,
        reviewer="17",
        producer="43",
        verdict="approve",
    )

    monkeypatch.setattr(module, "pr_json", lambda _pr: ci_pr_payload())
    monkeypatch.setattr(
        module,
        "required_checks_gate",
        lambda _pr: {"decision": "pass", "reason": "all required checks are successful"},
    )
    monkeypatch.setattr(module, "review_comment_bodies", lambda _pr: [approval])

    def mechanical_gate(pr_number: int, head: str) -> dict[str, str]:
        mechanical_calls.append((pr_number, head))
        return {"decision": "pass", "reason": "all exact-head mechanical gates passed"}

    monkeypatch.setattr(module, "mechanical_merge_gate", mechanical_gate)
    monkeypatch.setattr(
        module,
        "replace_factory_labels",
        lambda number, owner, stage: transitions.append((number, owner, stage)),
    )

    result = module.reconcile_ci_pr(PR_NUMBER)

    assert result["status"] == "ready"
    assert result["head"] == HEAD
    assert mechanical_calls == [(PR_NUMBER, HEAD)]
    assert transitions == [(PR_NUMBER, "factory:unowned", "factory:ready")]
