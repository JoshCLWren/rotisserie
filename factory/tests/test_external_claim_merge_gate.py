"""CI promotion and merge wait for an outside agent's shared claim to end."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
ledger = importlib.import_module("factory_state_ledger")

HEAD = "a" * 40


def load_controller() -> ModuleType:
    path = SCRIPTS / "factory-review-controller.py"
    spec = importlib.util.spec_from_file_location("factory_review_controller_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    module = load_controller()
    dsn = tmp_path / "dsn"
    dsn.write_text("postgresql://role@host/factory_state")
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", str(dsn))
    monkeypatch.setattr(module, "linked_issue_for_pr", lambda number, branch: 7)
    return module


def pr(stage: str) -> dict[str, Any]:
    return {
        "number": 44,
        "state": "OPEN",
        "headRefName": "factory/3-7-fix",
        "headRefOid": HEAD,
        "labels": [{"name": "factory"}, {"name": stage}, {"name": "factory:unowned"}],
    }


def holder_on(number: int) -> Any:
    def holder(repo: str, target: int) -> dict[str, object] | None:
        if target != number:
            return None
        return {"claim_id": "11111111-1111-4111-8111-111111111111",
                "client": "codebuff-phone", "actor": "alice"}  # fmt: skip

    return holder


@pytest.mark.parametrize("held", [44, 7])
def test_ci_reconcile_leaves_a_pr_an_outside_agent_holds(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, held: int
) -> None:
    monkeypatch.setattr(controller, "pr_json", lambda number: pr("factory:ci"))
    monkeypatch.setattr(ledger, "external_holder", holder_on(held))
    monkeypatch.setattr(
        controller, "required_checks_gate", lambda number: pytest.fail("must not promote")
    )
    result = controller.reconcile_ci_pr(44)
    assert result["status"] == "owned"
    assert f"codebuff-phone (alice) holds #{held}" in result["reason"]


def test_merge_authorization_waits_for_the_outside_claim(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "pr_json", lambda number: pr("factory:ready"))
    monkeypatch.setattr(ledger, "external_holder", holder_on(44))
    monkeypatch.setattr(
        controller, "replace_factory_labels", lambda *a, **k: pytest.fail("no demotion")
    )
    result = controller.authorize_ready(44)
    assert result["authorized"] is False
    assert result["outside_claim"] is True


def test_unreadable_claims_hold_closed(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(repo: str, number: int) -> None:
        raise ConnectionError("neon down")

    monkeypatch.setattr(ledger, "external_holder", broken)
    reason = controller.outside_claim(44, pr("factory:ready"))
    assert reason == "outside-agent claims could not be read (ConnectionError)"


def test_without_neon_configured_nothing_changes(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE")
    monkeypatch.setattr(
        ledger, "external_holder", lambda *a: pytest.fail("no claim store to read")
    )
    assert controller.outside_claim(44, pr("factory:ready")) is None
