"""Three gate rejections of approvals at one head send the PR to repair, not to another reviewer."""

from __future__ import annotations

from typing import Any

import factory_acceptance_escalation as escalation
import pytest
from pytest import MonkeyPatch
from tests.test_factory_review_gate import (
    REVIEWED_HEAD,
    load_review_controller,
    pr_payload,
    producer_contribution,
    wire_controller,
)

pytestmark = pytest.mark.usefixtures("passthrough_line_review")
ISSUE = "## Acceptance criteria\n- The creator list refreshes after a remap\n- A test covers it\n"
FAILURES = [
    "A1: cite a file changed by this PR (not changed: frontend/src/hooks/useMapping.ts)",
    "A2: name the test/check and what it verifies",
]


def review(monkeypatch: MonkeyPatch, prior: int) -> tuple[dict[str, Any], Any, Any]:
    module = load_review_controller()
    payload = pr_payload(worker="17", branch_worker="43")
    earlier = [escalation.marker(pr=1390, head=REVIEWED_HEAD) + "\n\nNOT-READY"] * prior
    transitions, posted, _ = wire_controller(
        monkeypatch, module, [payload, payload], comments=[*producer_contribution(), *earlier]
    )
    monkeypatch.setattr(module, "target_json", lambda _n: {"state": "open", "body": ISSUE})
    monkeypatch.setattr(module.issue_acceptance, "validate", lambda *_a, **_k: list(FAILURES))
    result = module.handle_review(
        worker="17",
        pr_number=1390,
        verdict="approve",
        reviewed_head=REVIEWED_HEAD,
        review_log="/tmp/model.log",
    )
    return result, transitions, posted


def test_the_first_two_rejections_go_back_to_review_with_a_head_marker(
    monkeypatch: MonkeyPatch,
) -> None:
    result, transitions, posted = review(monkeypatch, prior=1)
    assert result["status"] == "acceptance-evidence-required"
    assert transitions[-1]["pr_stage"] == "factory:review"
    assert posted[-1]["marker"] == escalation.marker(pr=1390, head=REVIEWED_HEAD)
    assert posted[-1]["verdict"] == "not-ready"


def test_the_third_rejection_at_one_head_routes_to_repair_with_findings(
    monkeypatch: MonkeyPatch,
) -> None:
    result, transitions, posted = review(monkeypatch, prior=2)
    assert result["status"] == "acceptance-evidence-required"
    assert transitions[-1]["pr_stage"] == "factory:changes-requested"
    comment = posted[-1]
    assert comment["verdict"] == "repair"
    assert (
        "A1 (Acceptance criteria: The creator list refreshes after a remap)" in comment["excerpt"]
    )
    assert "Implement this criterion in a file this PR changes" in comment["excerpt"]


def test_rejections_at_another_head_or_pr_do_not_count() -> None:
    bodies = [
        escalation.marker(pr=1390, head="b" * 40),
        escalation.marker(pr=1391, head=REVIEWED_HEAD),
        "quoted " + escalation.marker(pr=1390, head=REVIEWED_HEAD),
    ]
    assert escalation.prior(bodies, pr=1390, head=REVIEWED_HEAD) == 0
    assert not escalation.escalate(bodies, pr=1390, head=REVIEWED_HEAD)


def test_the_findings_pass_the_actionable_findings_check() -> None:
    import factory_review_policy as policy

    text = escalation.findings(FAILURES, ISSUE)
    assert policy.has_actionable_review_findings(text)


def test_a_gate_escalation_ends_the_empty_verdict_self_heal() -> None:
    import factory_self_heal_rules as rules
    from tests.test_factory_self_heal import NARRATION, trusted, verdict

    head = "c" * 40
    escalated = (
        escalation.marker(pr=44, head=head)
        + "\n\n### Factory semantic review · Factory 17 · REPAIR\n\n"
        + escalation.findings(FAILURES, ISSUE)
    )
    assert rules.empty_verdict([verdict(NARRATION)], 44) is not None
    assert rules.empty_verdict([verdict(NARRATION), trusted(escalated)], 44) is None


def gated(monkeypatch: MonkeyPatch, failures: list[str], *labels: str) -> list[list[str]]:
    module = load_review_controller()
    payload = pr_payload(worker="17", branch_worker="43")
    payload["labels"] += [{"name": name} for name in labels]
    _, _, commands = wire_controller(
        monkeypatch, module, [payload, payload], comments=producer_contribution()
    )
    monkeypatch.setattr(module, "target_json", lambda _n: {"state": "open", "body": ISSUE})
    monkeypatch.setattr(module.issue_acceptance, "validate", lambda *_a, **_k: list(failures))
    module.handle_review(
        worker="17",
        pr_number=1390,
        verdict="approve",
        reviewed_head=REVIEWED_HEAD,
        review_log="/tmp/model.log",
    )
    return [c for c in commands if any("gate-rejected" in part for part in c)]


def test_a_gate_rejection_adds_the_gate_rejected_label(monkeypatch: MonkeyPatch) -> None:
    writes = gated(monkeypatch, FAILURES)
    assert any("labels[]=factory:gate-rejected" in c for c in writes)


def test_a_gate_pass_removes_the_gate_rejected_label(monkeypatch: MonkeyPatch) -> None:
    writes = gated(monkeypatch, [], "factory:gate-rejected")
    assert any(c[-2:] == ["--method", "DELETE"] for c in writes)
    assert gated(monkeypatch, []) == []
