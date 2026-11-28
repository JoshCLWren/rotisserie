"""Repair budget regression for attributed and unattributed pull requests."""

import pytest

from tests.test_factory_review_gate import (
    REVIEWED_HEAD,
    load_review_controller,
    pr_payload,
    review_marker,
    wire_controller,
)

pytestmark = pytest.mark.usefixtures("passthrough_line_review")


@pytest.mark.parametrize(
    ("producer", "prior_heads", "expected", "closes"),
    [
        (None, ["b", "c"], "repair", False),
        (None, ["b", "c", "d"], "strike-limit-blocked", False),
        (None, ["b", "c", "d", "a"], "strike-limit-blocked", False),
        ("43", ["b", "c", "d"], "strike-limit-cancelled", True),
    ],
)
def test_repair_budget_is_independent_of_producer_attribution(
    monkeypatch: pytest.MonkeyPatch,
    producer: str | None,
    prior_heads: list[str],
    expected: str,
    closes: bool,
) -> None:
    controller = load_review_controller()
    payload = pr_payload(worker="17", branch_worker="43")
    if producer is None:
        payload["headRefName"] = "automation/regenerate-artifacts"
        payload["body"] = "Part of #1386."
    comments = [
        review_marker(
            pr=1390, head=head * 40, reviewer="29", producer=producer, verdict="repair"
        )
        for head in prior_heads
    ]
    transitions, posted, commands = wire_controller(
        monkeypatch, controller, [payload], comments=comments
    )

    result = controller.handle_review(
        worker="17",
        pr_number=1390,
        verdict="repair",
        reviewed_head=REVIEWED_HEAD,
        review_log="/tmp/model.log",
    )

    assert result["producer"] == producer
    assert result["status"] == expected
    assert result["repair_cycles"] == len(set(prior_heads) | {"a"})
    assert posted[-1]["verdict"] == "repair"
    assert transitions[-1]["pr_stage"] == (
        "factory:changes-requested" if expected == "repair" else "factory:blocked"
    )
    assert (["pr", "close", "1390", "--repo", controller.REPO] in commands) == closes
    if expected == "strike-limit-blocked":
        assert "budget exhausted" in posted[-1]["note"]
        assert transitions[-1]["issue_stage"] == "factory:blocked"
        assert not any(command[:2] == ["issue", "comment"] for command in commands)
