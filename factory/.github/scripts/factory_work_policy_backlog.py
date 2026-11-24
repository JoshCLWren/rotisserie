"""Worker WIP and completion backlog counting for the factory work policy."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any

from factory_work_policy_base import (
    BLOCKED_LABELS,
    is_factory_managed_pr,
    labels_of,
    owner_of,
    pr_is_conflicted,
    stage_of,
)
from factory_work_policy_priority import OVERRIDE_LABEL


def issue_bypasses_wip_limit(
    issue: dict[str, Any],
    *,
    review_backlog_saturated: bool = False,
) -> bool:
    """Keep genuinely urgent product defects executable while worker WIP is saturated.

    Urgent defects (main-breakage, user-reported bugs, P0/critical) bypass only
    the worker WIP gate (>=5 leased PRs). When review_backlog_saturated is set,
    end-to-end backpressure takes over: user-reported bugs and P0 issues must
    wait like everything else so the fleet drains existing completion-stage PRs
    instead of manufacturing unbounded new intake. Only main-breakage keeps
    opening new work once the review backlog is saturated.
    """
    labels = labels_of(issue)
    if OVERRIDE_LABEL in labels:
        return True
    if review_backlog_saturated:
        return 'main-breakage' in labels
    return (
        'main-breakage' in labels
        or ('user-reported' in labels and 'bug' in labels)
        or 'priority:P0' in labels
        or 'ralph-priority:critical' in labels
    )


def factory_review_backlog_count(prs: Iterable[dict[str, Any]]) -> int:
    """Count factory PRs waiting in completion stages.

    Owned and unowned review/ci/changes-requested PRs both consume fleet time
    before merge. Counting only unowned PRs under-reported pressure when
    reviewers held leases, so intake kept opening work while promotion stalled
    (incident #2309). Ready PRs remain excluded: the merge drain owns them.
    """
    return sum(1 for pr in prs if in_completion_backlog(pr))


def in_completion_backlog(pr: dict[str, Any]) -> bool:
    """Whether a PR counts toward ``FACTORY_REVIEW_BACKLOG_LIMIT``."""
    if str(pr.get('state') or 'OPEN').upper() != 'OPEN' or pr.get('isDraft'):
        return False
    labels = labels_of(pr)
    if not is_factory_managed_pr(pr):
        return False
    if labels & BLOCKED_LABELS or 'factory:ready' in labels:
        return False
    return stage_of(labels) in ('factory:review', 'factory:ci', 'factory:changes-requested')


def completion_action_for(stage: str | None, conflicted: bool) -> str | None:
    """The completion action a PR in this stage needs (merge conflicts first)."""
    if conflicted:
        return 'merge-conflict'
    return {
        'factory:ci': 'ci-repair',
        'factory:changes-requested': 'review-findings',
        'factory:review': 'review',
    }.get(stage or '')


def completion_backlog(prs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The PRs behind ``factory_review_backlog_count``, each with the action it needs.

    ``owner`` is the factory owner label (``factory:unowned`` when a worker
    may take it). Read-only: assignment still goes through the controller.
    """
    backlog: list[dict[str, Any]] = []
    for pr in prs:
        if not in_completion_backlog(pr):
            continue
        labels = labels_of(pr)
        stage = stage_of(labels)
        conflicted = pr_is_conflicted(pr)
        backlog.append({
            'number': int(pr['number']),
            'title': str(pr.get('title') or ''),
            'stage': stage,
            'owner': owner_of(labels),
            'conflicted': conflicted,
            'action': completion_action_for(stage, conflicted),
        })
    return backlog


def factory_ready_count(prs: Iterable[dict[str, Any]]) -> int:
    """Count open factory PRs parked at the factory:ready merge gate.

    Neither the worker WIP counter nor the review-backlog counter sees ready
    PRs because they consume no lease and wait outside the completion stages.
    During a red-main pause that pile can grow unbounded, so this aggregate
    exposes it for callers reporting end-to-end backpressure health.
    """
    count = 0
    for pr in prs:
        if str(pr.get('state') or 'OPEN').upper() != 'OPEN' or pr.get('isDraft'):
            continue
        labels = labels_of(pr)
        if not is_factory_managed_pr(pr):
            continue
        if labels & BLOCKED_LABELS:
            continue
        if 'factory:ready' in labels:
            count += 1
    return count


def factory_pr_wip_count(prs: Iterable[dict[str, Any]]) -> int:
    """Count factory PRs that currently consume a worker lease.

    Queue depth is not worker WIP. Unowned PRs waiting for a reviewer and PRs in
    factory:ready waiting for the merge drain consume no fixed-model worker
    capacity, so they must not shut off issue intake merely by existing.
    """
    count = 0
    for pr in prs:
        if str(pr.get('state') or 'OPEN').upper() != 'OPEN' or pr.get('isDraft'):
            continue
        labels = labels_of(pr)
        if not is_factory_managed_pr(pr):
            continue
        if 'factory:ready' in labels or labels & BLOCKED_LABELS:
            continue
        owner = owner_of(labels)
        if owner not in (None, 'factory:unowned'):
            count += 1
    return count
