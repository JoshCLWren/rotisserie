from __future__ import annotations

from rotisserie.domain import (
    EligibilitySignals,
    WorkBlock,
    WorkState,
    evaluate_eligibility,
)
from rotisserie.domain.model import RepositoryId, WorkId

REPO = RepositoryId("example.test", "org", "repo")


def wid(key: str) -> WorkId:
    return WorkId(REPO, key)


def test_unblocked_open_work_is_eligible() -> None:
    verdict = evaluate_eligibility(EligibilitySignals(work=wid("42"), state=WorkState.OPEN))
    assert verdict.eligible
    assert verdict.exclusions == ()
    assert verdict.reasons == ("eligible: no exclusion applies",)


def test_non_open_work_fails_closed() -> None:
    verdict = evaluate_eligibility(EligibilitySignals(work=wid("42"), state=WorkState.CANCELLED))
    assert not verdict.eligible
    assert verdict.exclusions == (WorkBlock.NOT_OPEN,)
    assert "not open" in verdict.reasons[0]


def test_product_exclusion_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, product_excluded=True)
    )
    assert verdict.exclusions == (WorkBlock.PRODUCT_EXCLUSION,)


def test_parent_or_aggregation_node_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, parent=True)
    )
    assert verdict.exclusions == (WorkBlock.PARENT_WORK,)
    assert not verdict.eligible


def test_human_gate_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, human_gate=True)
    )
    assert verdict.exclusions == (WorkBlock.HUMAN_BOUNDARY,)
    assert any("human-only" in reason for reason in verdict.reasons)


def test_blocked_signal_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, blocked=True)
    )
    assert verdict.exclusions == (WorkBlock.BLOCKED,)


def test_terminal_signal_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, terminal=True)
    )
    assert verdict.exclusions == (WorkBlock.TERMINAL,)


def test_active_lease_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, active_lease=True)
    )
    assert verdict.exclusions == (WorkBlock.ACTIVE_LEASE,)


def test_unresolved_dependencies_block_and_name_prerequisites() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(
            work=wid("42"),
            state=WorkState.OPEN,
            unresolved_dependencies=frozenset({wid("21"), wid("19")}),
        )
    )
    assert verdict.exclusions == (WorkBlock.DEPENDENCY_INCOMPLETE,)
    assert verdict.reasons[0] == "unresolved dependencies: 19, 21"


def test_self_referential_dependency_is_ignored() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(
            work=wid("42"),
            state=WorkState.OPEN,
            unresolved_dependencies=frozenset({wid("42")}),
        )
    )
    assert verdict.eligible


def test_existing_implementation_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, implementation_exists=True)
    )
    assert verdict.exclusions == (WorkBlock.IMPLEMENTATION_EXISTS,)


def test_retry_budget_exhaustion_blocks() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN, retry_budget_exhausted=True)
    )
    assert verdict.exclusions == (WorkBlock.RETRY_BUDGET_EXHAUSTED,)
    assert any("retry budget" in reason for reason in verdict.reasons)


def test_missing_selector_metadata_is_opt_in() -> None:
    assert evaluate_eligibility(EligibilitySignals(work=wid("42"), state=WorkState.OPEN)).eligible
    verdict = evaluate_eligibility(
        EligibilitySignals(
            work=wid("42"),
            state=WorkState.OPEN,
            missing_selector_metadata=frozenset({"queue-task", "queue-status:pending"}),
        )
    )
    assert verdict.exclusions == (WorkBlock.SELECTOR_METADATA_MISSING,)
    assert verdict.reasons[0] == "missing selector metadata: queue-status:pending, queue-task"


def test_all_exclusions_report_in_canonical_order() -> None:
    verdict = evaluate_eligibility(
        EligibilitySignals(
            work=wid("42"),
            state=WorkState.COMPLETED,
            product_excluded=True,
            parent=True,
            human_gate=True,
            blocked=True,
            terminal=True,
            active_lease=True,
            unresolved_dependencies=frozenset({wid("7")}),
            implementation_exists=True,
            retry_budget_exhausted=True,
            missing_selector_metadata=frozenset({"queue-task"}),
        )
    )
    assert verdict.exclusions == (
        WorkBlock.NOT_OPEN,
        WorkBlock.PRODUCT_EXCLUSION,
        WorkBlock.PARENT_WORK,
        WorkBlock.HUMAN_BOUNDARY,
        WorkBlock.BLOCKED,
        WorkBlock.TERMINAL,
        WorkBlock.ACTIVE_LEASE,
        WorkBlock.DEPENDENCY_INCOMPLETE,
        WorkBlock.IMPLEMENTATION_EXISTS,
        WorkBlock.RETRY_BUDGET_EXHAUSTED,
        WorkBlock.SELECTOR_METADATA_MISSING,
    )
    assert len(verdict.reasons) == len(verdict.exclusions)
    assert not verdict.eligible


def test_verdict_is_deterministic_for_identical_signals() -> None:
    signals = EligibilitySignals(
        work=wid("42"), state=WorkState.OPEN, blocked=True, human_gate=True
    )
    assert evaluate_eligibility(signals) == evaluate_eligibility(signals)
