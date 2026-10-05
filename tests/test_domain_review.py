from __future__ import annotations

from rotisserie.domain import (
    DUAL_REVIEWER,
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


def test_recorded_provenance_authorizes_a_single_independent_reviewer() -> None:
    verdict = evaluate_review_independence(
        signals(
            contributors=frozenset({worker("18")}),
            approvers=frozenset({worker("19")}),
            producer=worker("18"),
            provenance_recorded=True,
        )
    )
    assert verdict.satisfied
    assert verdict.required_reviewers == SINGLE_REVIEWER
    assert verdict.eligible_reviewers == frozenset({worker("19")})
    assert verdict.exclusions == ()


def test_missing_provenance_requires_two_distinct_independent_reviewers() -> None:
    single = evaluate_review_independence(
        signals(approvers=frozenset({worker("19")}), provenance_recorded=False)
    )
    assert not single.satisfied
    assert single.required_reviewers == DUAL_REVIEWER
    assert single.exclusions == (ReviewIndependenceBlock.INDEPENDENT_REVIEW_UNRECORDED,)
    assert "two distinct independent reviewers" in single.reasons[0]

    dual = evaluate_review_independence(
        signals(
            approvers=frozenset({worker("19"), worker("20")}),
            provenance_recorded=False,
        )
    )
    assert dual.satisfied
    assert dual.eligible_reviewers == frozenset({worker("19"), worker("20")})


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
            provenance_recorded=True,
        )
    )
    assert not blocked.satisfied
    allowed = evaluate_review_independence(
        signals(
            contributors=contributors,
            approvers=frozenset({worker("17")}),
            producer=worker("29"),
            provenance_recorded=True,
        )
    )
    assert allowed.satisfied
    assert allowed.eligible_reviewers == frozenset({worker("17")})


def test_missing_provenance_fails_closed_for_a_known_producer() -> None:
    contributors = frozenset({worker("29")})
    one_eligible = evaluate_review_independence(
        signals(
            contributors=contributors,
            approvers=frozenset({worker("29"), worker("17")}),
            provenance_recorded=False,
        )
    )
    assert not one_eligible.satisfied
    assert one_eligible.exclusions == (ReviewIndependenceBlock.INDEPENDENT_REVIEW_UNRECORDED,)

    two_eligible = evaluate_review_independence(
        signals(
            contributors=contributors,
            approvers=frozenset({worker("17"), worker("21")}),
            provenance_recorded=False,
        )
    )
    assert two_eligible.satisfied


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
        provenance_recorded=True,
    )
    assert evaluate_review_independence(fixed) == evaluate_review_independence(fixed)
