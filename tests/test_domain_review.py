from __future__ import annotations

import pytest

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


def test_unknown_producer_with_recorded_provenance_still_requires_dual_review() -> None:
    # Regression for the round-2 L1/B5 fail-open: an exact-head contributor
    # record for one worker makes provenance "recorded", but when the producer
    # is unknown the real producer is unexcluded and could self-approve. The
    # single-reviewer path must therefore stay closed until the producer is
    # attested, and the cause carries its own stable code (round-3 C1/V2).
    verdict = evaluate_review_independence(
        signals(
            contributors=frozenset({worker("59")}),
            approvers=frozenset({worker("18")}),
            producer=None,
            provenance_recorded=True,
        )
    )
    assert not verdict.satisfied
    assert verdict.required_reviewers == DUAL_REVIEWER
    assert verdict.exclusions == (ReviewIndependenceBlock.INDEPENDENT_REVIEW_PRODUCER_UNKNOWN,)
    assert "producer is unattested" in verdict.reasons[0]


def test_unattested_caller_producer_cannot_authorize_single_review() -> None:
    # Round-3 V1/L1: a producer that is present but not attested by exact-head
    # evidence (for example a caller-supplied cross-check) is still excluded, but
    # must not by itself downgrade the fail-closed dual-review requirement.
    verdict = evaluate_review_independence(
        signals(
            contributors=frozenset({worker("59")}),
            approvers=frozenset({worker("18")}),
            producer=worker("29"),
            producer_attested=False,
            provenance_recorded=True,
        )
    )
    assert not verdict.satisfied
    assert verdict.required_reviewers == DUAL_REVIEWER
    assert verdict.exclusions == (ReviewIndependenceBlock.INDEPENDENT_REVIEW_PRODUCER_UNKNOWN,)


def test_unrecorded_and_unattested_causes_use_distinct_stable_codes() -> None:
    # Round-3 C1/V2: the machine-readable code must match the cause.
    unrecorded = evaluate_review_independence(
        signals(approvers=frozenset({worker("19")}), provenance_recorded=False)
    )
    assert unrecorded.exclusions == (ReviewIndependenceBlock.INDEPENDENT_REVIEW_UNRECORDED,)
    assert "provenance is unrecorded" in unrecorded.reasons[0]

    unattested = evaluate_review_independence(
        signals(
            approvers=frozenset({worker("19")}),
            producer=worker("29"),
            producer_attested=False,
            provenance_recorded=True,
        )
    )
    assert unattested.exclusions == (ReviewIndependenceBlock.INDEPENDENT_REVIEW_PRODUCER_UNKNOWN,)
    assert "producer is unattested" in unattested.reasons[0]


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
def test_gate_states_that_must_never_authorize_single_review(
    provenance_recorded: bool,
    producer_attested: bool,
    producer: WorkerId | None,
) -> None:
    # Round-4 B1 holistic sweep: single review requires provenance_recorded AND
    # producer_attested AND a present producer. Every other legal state holds the
    # head to two reviewers; none may satisfy a single-reviewer verdict.
    verdict = evaluate_review_independence(
        signals(
            contributors=frozenset({worker("59")}),
            approvers=frozenset({worker("18")}),
            producer=producer,
            producer_attested=producer_attested,
            provenance_recorded=provenance_recorded,
        )
    )
    assert verdict.required_reviewers == DUAL_REVIEWER
    assert not verdict.satisfied
