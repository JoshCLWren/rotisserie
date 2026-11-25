"""Pure ranking and lease policy for the factory work controller.

The policy lives in the ``factory_work_policy_*`` sibling modules. This module
keeps the trusted no-diff marker parser and re-exports the rest so callers
keep importing ``factory_work_policy``.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from typing import Any

from factory_review_policy import TRUSTED_MARKER_APP_SLUGS, performed_via_untrusted_app
from factory_work_policy_assignment import (
    candidate_is_independent_for_worker,
    order_candidates_for_worker,
    plan_distinct_assignments,
    review_capacity_worker,
    review_share_for_backlog,
    worker_delivery_role,
)
from factory_work_policy_backlog import (
    LOOP_HOLD_KEY,
    completion_action_for,
    completion_backlog,
    factory_parked_count,
    factory_pr_wip_count,
    factory_ready_count,
    factory_review_backlog_count,
    in_completion_backlog,
    intake_backlog_count,
    issue_bypasses_wip_limit,
    parked_pr_limit,
)
from factory_work_policy_base import (
    BLOCKED_LABELS,
    DEP_ON_RE,
    DEPENDENCY_SEPARATORS,
    eligibility,
    _eligibility_dir,
    env_positive_int,
    FACTORY_NO_DIFF_RETRY_LIMIT,
    FACTORY_NO_DIFF_RETRY_RESET_SECONDS,
    FACTORY_PR_WIP_LIMIT,
    FACTORY_REVIEW_BACKLOG_LIMIT,
    FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS,
    FIXED_LEASE_TTL_SECONDS,
    FIXED_OWNER_RE,
    agent_hold,
    head_contributors_from_pr,
    is_acceptance_parent,
    is_factory_managed_pr,
    is_manual_only,
    issue_explicitly_closed_by_pr,
    item_is_unowned,
    labels_of,
    lease_is_stale,
    linked_issue_from_branch,
    linked_issue_from_pr,
    LOCAL_LEASE_TTL_SECONDS,
    MANUAL_ONLY_MARKER,
    NO_DIFF_ATTEMPT_RE,
    NO_PROGRESS_RETIRE_RE,
    NoDiffAttempt,
    NON_EXECUTABLE_ISSUES,
    normalize_target_state,
    NUMBER_REF_RE,
    owner_of,
    OWNER_RE,
    parse_declared_dependencies,
    _policy_dir,
    pr_head_sha,
    pr_is_conflicted,
    producer_worker_from_pr,
    producer_worker_from_values,
    REQUIRED_CHECK_FAILURE_STATES,
    _settings,
    STAGE_LABELS,
    stage_of,
    STAGE_PRECEDENCE,
    TITLE_ISSUE_RE,
    TRUSTED_ASSOCIATIONS,
    TRUSTED_FACTORY_APP_SLUGS,
)
from factory_work_policy_candidates import (
    body_depends_on_unresolved,
    build_candidates,
    explain_issue_report,
    issue_is_static_candidate,
    _leading_reference_numbers,
    matching_pr_no_diff_attempts,
    parse_depends_on_numbers,
    pr_is_static_candidate,
    pr_suppresses_issue_candidate,
)
from factory_incident import declared as declared_incidents
from factory_work_policy_priority import (
    OVERRIDE_LABEL,
    Candidate,
    _CATEGORIES,
    configured_lane_rank,
    INFRA_LABELS,
    parse_time,
    priority_rank,
    provenance_lane,
    work_category,
)


def comment_is_trusted(comment: Mapping[str, Any]) -> bool:
    """Return whether GitHub metadata proves a factory marker is trusted.

    Worker GitHub Apps are never trusted marker authors. A non-github-actions
    App slug loses even when author_association is OWNER, MEMBER, or
    COLLABORATOR. Human comments with no App stay on the association path.
    """
    if performed_via_untrusted_app(comment):
        return False
    if comment.get('author_association') in TRUSTED_ASSOCIATIONS:
        return True
    app = comment.get('performed_via_github_app')
    return isinstance(app, Mapping) and app.get('slug') in TRUSTED_FACTORY_APP_SLUGS


def parse_no_diff_attempts_from_comments(
    comments: Iterable[dict[str, Any]],
    *,
    now_epoch: int,
    reset_seconds: int = FACTORY_NO_DIFF_RETRY_RESET_SECONDS,
) -> list[NoDiffAttempt]:
    """Return trusted no-diff attempts still inside the rolling retry window."""
    cutoff = now_epoch - reset_seconds
    trusted_bodies = [
        str(comment.get('body') or '')
        for comment in comments
        if comment_is_trusted(comment)
    ]
    # Reset markers may occur later than issue release events in the input.
    # Derive their cutoffs first rather than depending on GitHub page order.
    retired_issue_epochs: dict[int, int] = {}
    for body in trusted_bodies:
        for reset in NO_PROGRESS_RETIRE_RE.finditer(body):
            epoch = int(reset.group('epoch'))
            if cutoff < epoch <= now_epoch:
                issue = int(reset.group('issue'))
                retired_issue_epochs[issue] = max(
                    epoch, retired_issue_epochs.get(issue, 0)
                )

    attempts: list[NoDiffAttempt] = []
    for body in trusted_bodies:
        for match in NO_DIFF_ATTEMPT_RE.finditer(body):
            epoch = int(match.group('epoch'))
            if (
                match.group('kind') == 'issue'
                and epoch <= retired_issue_epochs.get(int(match.group('number')), 0)
            ):
                continue
            transient = match.group('reason') in {
                'transient-model-interruption', 'review-agent-failed',
                'controller-assignment-read-failed', 'semantic-review-head-read-failed',
                'missing-semantic-verdict', 'conflicting-semantic-verdict',
                'authoritative-diff-read-failed', 'fork-repair-execution-failed',
                'fork-repair-result-invalid', 'fork-dispatch-failed',
            }
            if transient and match.group('kind') != 'pr':
                continue
            attempt_cutoff = max(cutoff, now_epoch - FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS) if transient else cutoff
            if epoch <= attempt_cutoff or epoch > now_epoch:
                continue
            sha = match.group('sha')
            conflicted_raw = match.group('conflicted')
            attempts.append(
                NoDiffAttempt(
                    kind=str(match.group('kind')),
                    number=int(match.group('number')),
                    epoch=epoch,
                    sha=None if sha is None else sha.lower(),
                    stage=match.group('stage'),
                    conflicted=None if conflicted_raw is None else conflicted_raw == '1',
                    reason=match.group('reason'),
                )
            )
    return attempts


def no_diff_attempts_from_comments(
    comments: Iterable[dict[str, Any]],
    *,
    now_epoch: int,
    reset_seconds: int = FACTORY_NO_DIFF_RETRY_RESET_SECONDS,
) -> dict[int, int]:
    """Count trusted no-diff attempts still inside the rolling retry window."""
    counts: dict[int, int] = {}
    for attempt in parse_no_diff_attempts_from_comments(
        comments,
        now_epoch=now_epoch,
        reset_seconds=reset_seconds,
    ):
        counts[attempt.number] = counts.get(attempt.number, 0) + 1
    return counts
