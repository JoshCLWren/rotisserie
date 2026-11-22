from __future__ import annotations

import pytest

from rotisserie.domain import (
    SINGLE_REVIEWER,
    ReviewIndependenceBlock,
    ReviewIndependenceSignals,
    evaluate_review_independence,
)
from rotisserie.domain.model import RepositoryId, RevisionId, WorkerId

REPO = RepositoryId("example.test", "org", "repo")
HEAD = RevisionId(REPO, "a" * 40)


def worker(token: str) -> WorkerId:
    return WorkerId("factory-worker", token)


def signals(**kwargs: object) -> ReviewIndependenceSignals:
    base: dict[str, object] = {"revision": HEAD}
    base.update(kwargs)
    return ReviewIndependenceSignals(**base)  # type: ignore[arg-type]


def test_attested_producer_with_recorded_provenance_authorizes_single_review() -> None:
    verdict = evaluate_review_independence(
        signals(
            contributors=frozenset({worker("18")}),
            approvers=frozenset({worker("19")}),
            producer=worker("18"),
            producer_attested=True,
            provenance_recorded=True,
        )
    )
    assert verdict.satisfied
    assert verdict.required_reviewers == SINGLE_REVIEWER
    assert verdict.eligible_reviewers == frozenset({worker("19")})
    assert verdict.exclusions == ()


def test_contributor_reviewer_overlap_is_never_independent() -> None:
    verdict = evaluate_review_independence(
        signals(
            contributors=frozenset({worker("59"), worker("29")}),
            approvers=frozenset({worker("59")}),
            provenance_recorded=True,
        )
    )
    assert not verdict.satisfied
    assert verdict.eligible_reviewers == frozenset()
    assert verdict.exclusions == (ReviewIndependenceBlock.NO_ELIGIBLE_REVIEWER,)
    assert "independent of the exact-head contributor set" in verdict.reasons[0]


def test_producer_cannot_approve_its_own_exact_head() -> None:
    verdict = evaluate_review_independence(
        signals(
            approvers=frozenset({worker("18")}),
            producer=worker("18"),
            provenance_recorded=True,
        )
    )
    assert not verdict.satisfied
    assert verdict.exclusions == (ReviewIndependenceBlock.NO_ELIGIBLE_REVIEWER,)


def test_repairer_in_lineage_cannot_approve_a_later_head() -> None:
    contributors = frozenset({worker("59"), worker("29")})
    blocked = evaluate_review_independence(
        signals(
            contributors=contributors,
            approvers=frozenset({worker("59")}),
            producer=worker("29"),
            producer_attested=True,
            provenance_recorded=True,
        )
    )
    assert not blocked.satisfied
    allowed = evaluate_review_independence(
        signals(
            contributors=contributors,
            approvers=frozenset({worker("17")}),
            producer=worker("29"),
            producer_attested=True,
            provenance_recorded=True,
        )
    )
    assert allowed.satisfied
    assert allowed.eligible_reviewers == frozenset({worker("17")})


def test_no_approvals_fail_closed() -> None:
    verdict = evaluate_review_independence(signals(provenance_recorded=True))
    assert not verdict.satisfied
    assert verdict.exclusions == (ReviewIndependenceBlock.NO_ELIGIBLE_REVIEWER,)


def test_excluded_and_eligible_reviewer_sets_are_derived_consistently() -> None:
    pending = signals(
        contributors=frozenset({worker("5")}),
        approvers=frozenset({worker("5"), worker("6")}),
        producer=worker("7"),
    )
    assert pending.excluded_reviewers() == frozenset({worker("5"), worker("7")})
    assert pending.eligible_reviewers() == frozenset({worker("6")})


def test_verdict_is_deterministic_for_identical_signals() -> None:
    fixed = signals(
        contributors=frozenset({worker("1")}),
        approvers=frozenset({worker("2")}),
        producer=worker("1"),
        producer_attested=True,
        provenance_recorded=True,
    )
    assert evaluate_review_independence(fixed) == evaluate_review_independence(fixed)


def test_producer_attested_without_a_producer_identity_is_rejected() -> None:
    # Round-4 B1: the fail-closed invariant lives in the domain, so an illegal
    # signal (attested with nothing excluded) cannot be constructed at all.
    with pytest.raises(ValueError):
        ReviewIndependenceSignals(revision=HEAD, producer=None, producer_attested=True)


@pytest.mark.parametrize(
    ("provenance_recorded", "producer_attested", "producer"),
    [
        (True, False, None),  # provenance but producer unattested and absent
        (True, False, WorkerId("factory-worker", "9")),  # present but unattested
        (False, False, None),  # nothing recorded
        (False, True, WorkerId("factory-worker", "9")),  # attested but unrecorded provenance
    ],
)
def test_missing_evidence_states_need_one_independent_reviewer(
    provenance_recorded: bool,
    producer_attested: bool,
    producer: WorkerId | None,
) -> None:
    # Owner policy (2026-11-22): missing provenance or an unattested producer is
    # a diagnostic only. One independent reviewer satisfies every such state.
    verdict = evaluate_review_independence(
        signals(
            contributors=frozenset({worker("59")}),
            approvers=frozenset({worker("18")}),
            producer=producer,
            producer_attested=producer_attested,
            provenance_recorded=provenance_recorded,
        )
    )
    assert verdict.required_reviewers == SINGLE_REVIEWER
    assert verdict.satisfied


def test_missing_provenance_needs_one_independent_reviewer() -> None:
    """Missing attribution is not grounds to demand a second approval."""
    verdict = evaluate_review_independence(
        signals(approvers=frozenset({worker("19")}), provenance_recorded=False)
    )
    assert verdict.satisfied
    assert verdict.required_reviewers == SINGLE_REVIEWER


def test_missing_provenance_still_excludes_a_known_producer() -> None:
    contributors = frozenset({worker("29")})
    only_producer = evaluate_review_independence(
        signals(contributors=contributors, approvers=frozenset({worker("29")}))
    )
    assert not only_producer.satisfied
    assert only_producer.exclusions == (ReviewIndependenceBlock.NO_ELIGIBLE_REVIEWER,)
    with_other = evaluate_review_independence(
        signals(contributors=contributors, approvers=frozenset({worker("29"), worker("17")}))
    )
    assert with_other.satisfied
    assert with_other.eligible_reviewers == frozenset({worker("17")})
