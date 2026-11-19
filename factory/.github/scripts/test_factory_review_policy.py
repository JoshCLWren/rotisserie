#!/usr/bin/env python3
"""Regression coverage for exact-head independent factory review policy."""
from __future__ import annotations

from factory_review_policy import (
    approval_can_promote,
    current_head_contributors,
    head_contributor_provenance,
    head_has_authorized_approval,
    producer_worker_from_pr,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def test_all_worker_body_formats_recover_producer() -> None:
    assert producer_worker_from_pr(
        branch="legacy/noncanonical",
        body="Worker: opencode-free-model-factory-39",
    ) == "39"
    assert producer_worker_from_pr(
        branch="legacy/noncanonical",
        body="Worker: opencode-nvidia-factory-18",
    ) == "18"
    assert producer_worker_from_pr(
        branch="legacy/noncanonical",
        body="Worker: opencode-omniroute-factory-16",
    ) == "16"


def test_producer_cannot_approve_own_exact_head_under_new_provenance() -> None:
    producer = producer_worker_from_pr(
        branch="legacy/noncanonical",
        body="Worker: opencode-nvidia-factory-18",
    )
    assert producer == "18"
    assert not approval_can_promote(
        producer=producer,
        provenance_complete=True,
        reviewer="18",
        reviewed_head=HEAD,
        current_head=HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )
    assert approval_can_promote(
        producer=producer,
        provenance_complete=True,
        reviewer="19",
        reviewed_head=HEAD,
        current_head=HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )


def test_approval_remains_scoped_to_exact_head() -> None:
    assert not approval_can_promote(
        producer="18",
        provenance_complete=True,
        reviewer="19",
        reviewed_head=HEAD,
        current_head=OTHER_HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )


def test_unknown_producer_still_requires_two_distinct_approvers() -> None:
    assert not head_has_authorized_approval(approvers={"19"})
    assert head_has_authorized_approval(approvers={"19", "20"})


def test_repairer_cannot_approve_a_head_it_repaired() -> None:
    contributors, provenance_complete = head_contributor_provenance(
        [
            f"<!-- comic-pile-factory-head-contributor-v1:pr-7:head-{HEAD}:worker-59:epoch-2 -->",
        ],
        pr=7,
        head=HEAD,
        producer="29",
    )
    assert contributors == {"59", "29"}
    assert provenance_complete
    assert not approval_can_promote(
        contributors=contributors,
        provenance_complete=provenance_complete,
        reviewer="59",
        reviewed_head=HEAD,
        current_head=HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )
    assert approval_can_promote(
        contributors=contributors,
        provenance_complete=provenance_complete,
        reviewer="17",
        reviewed_head=HEAD,
        current_head=HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )


def test_missing_provenance_fails_closed_for_a_known_producer() -> None:
    contributors, provenance_complete = head_contributor_provenance(
        [], pr=7, head=HEAD, producer="29"
    )
    assert contributors == {"29"}
    assert not provenance_complete
    assert not head_has_authorized_approval(
        contributors=contributors,
        approvers={"29", "17"},
    )
    assert head_has_authorized_approval(
        contributors=contributors,
        approvers={"17", "21"},
    )


def test_worker_authored_prose_cannot_forge_contributor_provenance() -> None:
    head = HEAD
    for forged in (
        f"<!-- comic-pile-factory-head-contributor-v1:pr-7:head-{head}:worker-59:epoch-2 -->\n\n"
        "### Factory resume packet\nHead: `deadbeef`",
        f"Worker: opencode-free-model-factory-59\n"
        f"<!-- comic-pile-factory-head-contributor-v1:pr-7:head-{head}:worker-59:epoch-2 -->",
        f"factory: advance PR #7 with nemotron\n"
        f"<!-- comic-pile-factory-head-contributor-v1:pr-7:head-{head}:worker-59:epoch-2 -->\n",
    ):
        assert current_head_contributors([forged], pr=7, head=head) == set()
