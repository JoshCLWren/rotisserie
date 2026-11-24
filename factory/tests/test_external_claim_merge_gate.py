"""CI promotion and merge wait for an outside agent's shared claim to end."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
ledger = importlib.import_module("factory_state_ledger")
from tests import turso_support as support  # noqa: E402

HEAD = "a" * 40


def load_controller() -> ModuleType:
    path = SCRIPTS / "factory-review-controller.py"
    spec = importlib.util.spec_from_file_location("factory_review_controller_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ModuleType]:
    module = load_controller()
    monkeypatch.setenv("FACTORY_STATE_SQLITE", str(tmp_path / "state.sqlite"))
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(module, "linked_issue_for_pr", lambda number, branch: 7)
    support.core.use_store(None)
    yield module
    support.core.use_store(None)


def without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_STATE_SQLITE", raising=False)
    monkeypatch.delenv("FACTORY_TURSO_URL", raising=False)
    monkeypatch.delenv("FACTORY_STATE_REQUIRED", raising=False)


def pr(stage: str) -> dict[str, Any]:
    return {
        "number": 44,
        "state": "OPEN",
        "headRefName": "factory/3-7-fix",
        "headRefOid": HEAD,
        "labels": [{"name": "factory"}, {"name": stage}, {"name": "factory:unowned"}],
    }


def hold(controller: ModuleType, number: int) -> None:
    claim = ledger.reserve_external(
        controller.REPO,
        "alice",
        "codebuff-phone",
        [number],
        target=f"pr:{number}",
        action="review-findings",
        head=HEAD,
        observed_wip=0,
        wip_limit=8,
        offer_at="2026-11-22T00:00:00+00:00",
    )
    assert claim["status"] == "reserved"


@pytest.mark.parametrize("held", [44, 7])
def test_ci_reconcile_leaves_a_pr_an_outside_agent_holds(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, held: int
) -> None:
    monkeypatch.setattr(controller, "pr_json", lambda number: pr("factory:ci"))
    hold(controller, held)
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
    hold(controller, 44)
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
        raise ConnectionError("store down")

    monkeypatch.setattr(ledger, "external_holder", broken)
    reason = controller.outside_claim(44, pr("factory:ready"))
    assert reason == "outside-agent claims could not be read (ConnectionError)"


def test_without_the_store_configured_nothing_changes(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    without_store(monkeypatch)
    monkeypatch.setenv("FACTORY_STATE_REQUIRED", "false")
    monkeypatch.setattr(
        ledger, "external_holder", lambda *a: pytest.fail("no claim store to read")
    )
    assert controller.outside_claim(44, pr("factory:ready")) is None


def test_required_but_unconnected_store_holds_merges(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    without_store(monkeypatch)
    monkeypatch.setenv("FACTORY_STATE_REQUIRED", "true")
    reason = controller.outside_claim(44, pr("factory:ready"))
    assert reason == (
        "outside-agent claims could not be read (factory store required, not connected)"
    )


def test_merge_drain_reads_claims_with_the_python_it_runs() -> None:
    text = (SCRIPTS.parents[2] / ".github" / "workflows" / "factory-ready-merge-drain.yml").read_text()
    store = text.index("uses: ./.github/actions/factory-store")
    parsers = text.index("uses: ./.github/actions/factory-syntax")
    drain = text.index("name: Drain exact-head ready factory PRs")
    assert store < parsers < drain
    assert "asyncpg" not in text
    assert "FACTORY_STATE_REQUIRED: ${{ secrets.FACTORY_TURSO_URL != ''" in text


@pytest.mark.parametrize(("configured", "held"), [(None, True), ("false", False)])
def test_a_workflow_step_must_say_whether_the_store_is_required(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    held: bool,
) -> None:
    without_store(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    if configured is not None:
        monkeypatch.setenv("FACTORY_STATE_REQUIRED", configured)
    reason = controller.outside_claim(44, pr("factory:ci"))
    if held:
        assert reason == "outside-agent claims could not be read (FACTORY_STATE_REQUIRED unset)"
    else:
        assert reason is None


def _steps(text: str) -> list[str]:
    return re.split(r"\n(?=      - (?:name|uses|id):)", text)


def test_every_claim_gated_controller_step_reads_outside_claims() -> None:
    """Steps that promote or authorize PRs must see outside-agent claims."""
    gated = re.compile(r"\b(?:reconcile-ci|authorized)\b")
    found = 0
    for workflow in sorted((SCRIPTS.parents[2] / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text()
        steps = _steps(text)
        for index, step in enumerate(steps):
            if "factory-review-controller" not in step or not gated.search(step):
                continue
            found += 1
            assert "FACTORY_STATE_REQUIRED: ${{ secrets.FACTORY_TURSO_URL != ''" in text, (
                workflow.name
            )
            earlier = "".join(steps[:index])
            assert "uses: ./.github/actions/factory-store" in earlier, workflow.name
    assert found == 4
