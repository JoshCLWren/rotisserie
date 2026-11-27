"""A review approval is a boolean: an approved, green, current head merges.

Owner rule (2026-11-25): no acceptance-criteria record, diff-evidence proof,
second model run or advice thread may turn an approval back into review.
"""

from __future__ import annotations

from pathlib import Path

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
SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


def deny(*_args: object, **_kwargs: object) -> dict[str, str]:
    return {"decision": "deny", "reason": "old acceptance rule"}


def test_approval_is_ready_without_acceptance_record_or_diff_proof(
    monkeypatch: MonkeyPatch,
) -> None:
    module = load_review_controller()
    payload = pr_payload(worker="17", branch_worker="43")
    transitions, _posted, _commands = wire_controller(
        monkeypatch,
        module,
        [payload, payload],
        comments=producer_contribution(),
        mechanical=True,
        include_diff_inspection=False,
    )
    monkeypatch.setattr(module.issue_acceptance, "validate", lambda *_a, **_k: ["A1 missing"])
    monkeypatch.setattr(module, "implementation_delivery_gate", deny)
    result = module.handle_review(
        worker="17",
        pr_number=1390,
        verdict="approve",
        reviewed_head=REVIEWED_HEAD,
        review_log="/tmp/model.log",
    )
    assert result["status"] == "ready"
    assert transitions[-1]["pr_stage"] == "factory:ready"


def test_mechanical_gate_passes_without_acceptance_attestation(
    monkeypatch: MonkeyPatch,
) -> None:
    module = load_review_controller()
    passing = {"decision": "pass", "reason": "ok"}
    for gate in (
        "poll_mergeable_gate",
        "required_checks_gate",
        "current_head_review_gate",
        "declared_dependency_gate",
    ):
        monkeypatch.setattr(module, gate, lambda *_a, **_k: passing)
    monkeypatch.setattr(module, "pr_json", lambda _n: {"labels": [{"name": "factory"}]})
    monkeypatch.setattr(module, "issue_acceptance_gate", deny, raising=False)
    assert module.mechanical_merge_gate(1390, REVIEWED_HEAD)["decision"] == "pass"


def thread(module: object, severity: str) -> dict[str, object]:
    marker = (
        f"<!-- {module.MARKER_PREFIX}-factory-review-finding-v1:pr-1390:"  # type: ignore[attr-defined]
        f"head-{REVIEWED_HEAD}:severity-{severity}:lines-1-2 -->"
    )
    return {
        "isResolved": False,
        "comments": {
            "nodes": [
                {
                    "author": {"login": "github-actions"},
                    "commit": {"oid": REVIEWED_HEAD},
                    "body": marker + " **Note:** advice",
                }
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }


def test_factory_advice_thread_never_blocks_the_approved_head() -> None:
    module = load_review_controller()
    advice = module.interpret_review_threads([thread(module, "non-blocking")], head=REVIEWED_HEAD)
    blocker = module.interpret_review_threads([thread(module, "blocking")], head=REVIEWED_HEAD)
    assert advice["decision"] == "pass"
    assert blocker["decision"] == "deny"


def test_worker_takes_the_findings_verdict_without_a_recovery_run() -> None:
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text(encoding="utf-8")
    helper = (SCRIPTS / "factory-semantic-verdict.sh").read_text(encoding="utf-8")
    prompt = (SCRIPTS / "factory-review-findings.sh").read_text(encoding="utf-8")
    assert "factory_recover_semantic_verdict" not in worker + helper
    assert "factory_has_acceptance_report" not in worker + helper
    assert "FACTORY_ACCEPTANCE_V1" not in prompt
    assert "do not approve" not in prompt.lower()
