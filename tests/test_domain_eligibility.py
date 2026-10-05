from __future__ import annotations

import pytest

from rotisserie.domain import (
    EligibilitySignals,
    EligibilityVerdict,
    WorkBlock,
    WorkState,
    evaluate_eligibility,
)
from rotisserie.domain.model import RepositoryId, WorkId

REPO = RepositoryId("example.test", "org", "repo")


def wid(key: str) -> WorkId:
    return WorkId(REPO, key)


def signals(
    *,
    state: WorkState = WorkState.OPEN,
    human_gate: bool = False,
    active_lease: bool = False,
    product_excluded: bool = False,
    parent: bool = False,
    blocked: bool = False,
    terminal: bool = False,
    unresolved_dependencies: frozenset[WorkId] = frozenset(),
    implementation_exists: bool = False,
    retry_budget_exhausted: bool = False,
    missing_selector_metadata: frozenset[str] = frozenset(),
) -> EligibilitySignals:
    return EligibilitySignals(
        work=wid("42"),
        state=state,
        human_gate=human_gate,
        active_lease=active_lease,
        product_excluded=product_excluded,
        parent=parent,
        blocked=blocked,
        terminal=terminal,
        unresolved_dependencies=unresolved_dependencies,
        implementation_exists=implementation_exists,
        retry_budget_exhausted=retry_budget_exhausted,
        missing_selector_metadata=missing_selector_metadata,
    )


def test_unblocked_open_work_is_eligible() -> None:
    verdict = evaluate_eligibility(signals())
    assert verdict.eligible
    assert verdict.blocks == ()
    assert verdict.reasons == ()


def test_non_open_work_fails_closed() -> None:
    verdict = evaluate_eligibility(signals(state=WorkState.CANCELLED))
    assert not verdict.eligible
    assert verdict.blocks == (WorkBlock.NOT_OPEN,)
    assert "not open" in verdict.reasons[0]


def test_product_exclusion_blocks() -> None:
    assert evaluate_eligibility(signals(product_excluded=True)).blocks == (
        WorkBlock.PRODUCT_EXCLUSION,
    )


def test_parent_or_aggregation_node_blocks() -> None:
    verdict = evaluate_eligibility(signals(parent=True))
    assert verdict.blocks == (WorkBlock.PARENT_WORK,)
    assert not verdict.eligible


def test_human_gate_blocks() -> None:
    verdict = evaluate_eligibility(signals(human_gate=True))
    assert verdict.blocks == (WorkBlock.HUMAN_BOUNDARY,)
    assert any("human-only" in reason for reason in verdict.reasons)


def test_blocked_signal_blocks() -> None:
    assert evaluate_eligibility(signals(blocked=True)).blocks == (WorkBlock.BLOCKED,)


def test_terminal_signal_blocks() -> None:
    assert evaluate_eligibility(signals(terminal=True)).blocks == (WorkBlock.TERMINAL,)


def test_active_lease_blocks() -> None:
    assert evaluate_eligibility(signals(active_lease=True)).blocks == (WorkBlock.ACTIVE_LEASE,)


def test_unresolved_dependencies_block_and_name_prerequisites() -> None:
    verdict = evaluate_eligibility(
        signals(unresolved_dependencies=frozenset({wid("21"), wid("19")}))
    )
    assert verdict.blocks == (WorkBlock.DEPENDENCY_INCOMPLETE,)
    assert verdict.reasons[0] == "unresolved dependencies: 19, 21"


def test_self_referential_dependency_is_ignored() -> None:
    assert evaluate_eligibility(signals(unresolved_dependencies=frozenset({wid("42")}))).eligible


def test_existing_implementation_blocks() -> None:
    assert evaluate_eligibility(signals(implementation_exists=True)).blocks == (
        WorkBlock.IMPLEMENTATION_EXISTS,
    )


def test_retry_budget_exhaustion_blocks() -> None:
    verdict = evaluate_eligibility(signals(retry_budget_exhausted=True))
    assert verdict.blocks == (WorkBlock.RETRY_BUDGET_EXHAUSTED,)
    assert any("retry budget" in reason for reason in verdict.reasons)


def test_missing_selector_metadata_is_opt_in() -> None:
    assert evaluate_eligibility(signals()).eligible
    verdict = evaluate_eligibility(
        signals(missing_selector_metadata=frozenset({"queue-task", "queue-status:pending"}))
    )
    assert verdict.blocks == (WorkBlock.SELECTOR_METADATA_MISSING,)
    assert verdict.reasons[0] == "missing selector metadata: queue-status:pending, queue-task"


def test_all_blocks_report_in_canonical_order() -> None:
    verdict = evaluate_eligibility(
        signals(
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
    assert verdict.blocks == (
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
    assert not verdict.eligible


def test_reasons_run_strictly_parallel_to_blocks() -> None:
    verdict = evaluate_eligibility(signals(blocked=True, human_gate=True))
    assert len(verdict.reasons) == len(verdict.blocks) == 2
    eligible = evaluate_eligibility(signals())
    assert eligible.reasons == () == eligible.blocks


def test_verdict_is_deterministic_for_identical_signals() -> None:
    repeated = signals(blocked=True, human_gate=True)
    assert evaluate_eligibility(repeated) == evaluate_eligibility(repeated)


def test_human_gate_and_lease_ownership_must_be_supplied() -> None:
    with pytest.raises(TypeError):
        EligibilitySignals(work=wid("42"), state=WorkState.OPEN)  # type: ignore[call-arg]


def test_verdict_rejects_inconsistent_construction() -> None:
    with pytest.raises(ValueError):
        EligibilityVerdict(wid("42"), eligible=True, blocks=(WorkBlock.BLOCKED,), reasons=("x",))
    with pytest.raises(ValueError):
        EligibilityVerdict(wid("42"), eligible=False, blocks=(WorkBlock.BLOCKED,), reasons=())
