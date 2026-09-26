#!/usr/bin/env python3
"""Regression coverage for exact-head independent factory review policy."""
from __future__ import annotations

from factory_review_policy import (
    approval_can_promote,
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
        reviewer="18",
        reviewed_head=HEAD,
        current_head=HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )
    assert approval_can_promote(
        producer=producer,
        reviewer="19",
        reviewed_head=HEAD,
        current_head=HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )


def test_approval_remains_scoped_to_exact_head() -> None:
    assert not approval_can_promote(
        producer="18",
        reviewer="19",
        reviewed_head=HEAD,
        current_head=OTHER_HEAD,
        verdict="approve",
        mechanical_gates_passed=True,
    )


def test_unknown_producer_still_requires_two_distinct_approvers() -> None:
    assert not head_has_authorized_approval(producer=None, approvers={"19"})
    assert head_has_authorized_approval(producer=None, approvers={"19", "20"})
