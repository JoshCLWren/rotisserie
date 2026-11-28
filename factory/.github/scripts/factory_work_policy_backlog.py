"""Worker WIP and completion backlog counting for the factory work policy."""
from __future__ import annotations
import os
from collections.abc import Iterable
from typing import Any

from factory_work_policy_base import (
    BLOCKED_LABELS,
    FACTORY_REVIEW_BACKLOG_LIMIT,
    is_factory_managed_pr,
    item_is_unowned,
    linked_issue_from_pr,
    labels_of,
    owner_of,
    pr_is_conflicted,
    stage_of,
)
from factory_work_policy_priority import OVERRIDE_LABEL
from factory_issue_intake_guard import sliced_issue

LOOP_HOLD_KEY = 'factoryLoopHold'
LOOP_HOLDS = frozenset({'held: loop-parked', 'held: looping'})
PARKED_LIMIT_ENV = 'FACTORY_PARKED_PR_LIMIT'


def loop_held(pr: dict[str, Any]) -> bool:
    """Whether the loop breaker holds this PR; it waits on a person, not the fleet."""
    return pr.get(LOOP_HOLD_KEY) in LOOP_HOLDS


def parked_pr_limit(review_backlog_limit: int = FACTORY_REVIEW_BACKLOG_LIMIT) -> int:
    """How many loop-held PRs may sit open before new issue intake stops.

    ``FACTORY_PARKED_PR_LIMIT`` in the job environment overrides the default of
    twice the review backlog limit.
    """
    raw = os.environ.get(PARKED_LIMIT_ENV, '').strip()
    if raw.isdecimal() and int(raw) > 0:
        return int(raw)
    return 2 * max(1, int(review_backlog_limit))


def factory_parked_count(prs: Iterable[dict[str, Any]]) -> int:
    """Count open factory PRs the loop breaker holds (parked or looping)."""
    return sum(1 for pr in prs if loop_held(pr) and _open_factory_pr(pr))


def intake_backlog_count(
    prs: Iterable[dict[str, Any]],
    review_backlog_limit: int = FACTORY_REVIEW_BACKLOG_LIMIT,
) -> int:
    """The completion backlog that gates new issue intake.

    Loop-held PRs do not count: no worker can drain them, so counting them
    deadlocked intake on 2026-11-25. Once the parked cap is reached,
    the backlog reads as saturated so the factory stops opening new PRs.
    """
    rows = list(prs)
    count = factory_review_backlog_count(rows)
    if factory_parked_count(rows) >= parked_pr_limit(review_backlog_limit):
        return max(count, int(review_backlog_limit))
    return count


def _open_factory_pr(pr: dict[str, Any]) -> bool:
    if str(pr.get('state') or 'OPEN').upper() != 'OPEN' or pr.get('isDraft'):
        return False
    return is_factory_managed_pr(pr) and 'factory:ready' not in labels_of(pr)


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
    opening new work without a reserve once the review backlog is saturated.
    The bounded urgent reserve is evaluated separately on the full queue.
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


def needs_urgent_reserve(issue: dict[str, Any]) -> bool:
    """Existing urgent WIP exemptions, excluding unconditional owner/main exceptions."""
    return issue_bypasses_wip_limit(issue) and not issue_bypasses_wip_limit(
        issue, review_backlog_saturated=True
    )


def urgent_reserve_available(
    issues: Iterable[dict[str, Any]], prs: Iterable[dict[str, Any]]
) -> bool:
    """One overflow opportunity; keep it occupied until the urgent work closes.

    A live claim, or any open urgent PR including a draft/partial/blocked PR,
    occupies the reserve. This prevents replacing stalled urgent work with an
    ever-growing pile of new urgent PRs. Owner-directed incidents and overrides
    retain their separate existing policy.
    """
    urgent = {
        int(issue['number']): issue for issue in issues
        if str(issue.get('state') or 'OPEN').upper() == 'OPEN'
        and needs_urgent_reserve(issue)
    }
    if any(not item_is_unowned(labels_of(issue)) for issue in urgent.values()):
        return False
    for pr in prs:
        if str(pr.get('state') or 'OPEN').upper() != 'OPEN':
            continue
        if needs_urgent_reserve(pr) or linked_issue_from_pr(pr) in urgent or sliced_issue(pr) in urgent:
            return False
    return True


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
    if str(pr.get('state') or 'OPEN').upper() != 'OPEN' or pr.get('isDraft') or loop_held(pr):
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
        if 'factory:ready' in labels or labels & BLOCKED_LABELS or loop_held(pr):
            continue
        owner = owner_of(labels)
        if owner not in (None, 'factory:unowned'):
            count += 1
    return count
