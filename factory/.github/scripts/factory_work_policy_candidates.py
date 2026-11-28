"""Static eligibility, candidate building and issue explanations."""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from typing import Any

from factory_work_policy_backlog import (
    factory_parked_count,
    factory_pr_wip_count,
    intake_backlog_count,
    issue_bypasses_wip_limit,
    parked_pr_limit,
    needs_urgent_reserve,
    urgent_reserve_available,
)
from factory_work_policy_base import (
    BLOCKED_LABELS,
    FACTORY_NO_DIFF_RETRY_LIMIT,
    FACTORY_PR_WIP_LIMIT,
    FACTORY_REVIEW_BACKLOG_LIMIT,
    NON_EXECUTABLE_ISSUES,
    NoDiffAttempt,
    eligibility,
    head_contributors_from_pr,
    is_factory_managed_pr,
    item_is_unowned,
    labels_of,
    linked_issue_from_pr,
    pr_head_sha,
    pr_is_conflicted,
    producer_worker_from_pr,
    stage_of,
)
import factory_issue_intake_guard as intake_guard
from factory_incident import active as active_incidents
from factory_incident import linked_issue as incident_issue_of
from factory_work_policy_priority import (
    OVERRIDE_LABEL,
    Candidate,
    priority_rank,
    provenance_lane,
)


def matching_pr_no_diff_attempts(
    attempts: Iterable[NoDiffAttempt],
    *,
    pr_number: int,
    head_sha: str,
    stage: str | None,
    conflicted: bool,
) -> int:
    """Count no-diff retries that still apply to this exact PR generation.

    Only trusted markers pinned to the current head and conflict state count.
    A repair that persisted no changes consumes the same budget across the
    review/changes-requested handoff: a label flip is not new code. Other
    failures remain stage-scoped. A new push or conflict starts a fresh budget.
    Unscoped legacy PR markers do not suppress a known current generation.
    """
    current_sha = head_sha.strip().lower()
    if not current_sha:
        return 0
    count = 0
    for attempt in attempts:
        if attempt.kind != 'pr' or attempt.number != pr_number:
            continue
        if not attempt.sha or attempt.sha != current_sha:
            continue
        if attempt.stage is None:
            continue
        if attempt.stage != stage and not (
            attempt.reason == 'repair-no-persisted-change-handoff'
            and {attempt.stage, stage} <= {'factory:review', 'factory:changes-requested'}
        ):
            continue
        if attempt.conflicted is None or attempt.conflicted != conflicted:
            continue
        count += 1
    return count


def issue_is_static_candidate(
    issue: dict[str, Any],
    suppressing_pr_issues: set[int],
    *,
    no_diff_attempts: int = 0,
) -> bool:
    """Return whether an issue is structurally eligible for assignment."""
    number = int(issue['number'])
    labels = labels_of(issue)
    body = str(issue.get('body') or '')
    if str(issue.get('state') or 'OPEN').upper() != 'OPEN':
        return False
    if number in NON_EXECUTABLE_ISSUES or number in suppressing_pr_issues:
        return False
    if labels & {'epic', 'prd'}:
        return False
    if eligibility.is_manual_only(body):
        return False
    if eligibility.is_acceptance_parent(body):
        return False
    if labels & BLOCKED_LABELS:
        return False
    if 'ralph-status:done' in labels or 'factory:ready' in labels:
        return False
    return item_is_unowned(labels)


def pr_is_static_candidate(
    pr: dict[str, Any],
    issue_map: dict[int, dict[str, Any]],
    *,
    no_diff_attempts: int = 0,
) -> bool:
    """Return whether an open autonomous-factory PR is structurally eligible."""
    if str(pr.get('state') or 'OPEN').upper() != 'OPEN' or pr.get('isDraft'):
        return False
    labels = labels_of(pr)
    # Explicit operator opt-out always wins, including on an already-enrolled PR.
    if eligibility.is_manual_only(str(pr.get("body") or "")):
        return False
    # Human-authored agent/* and chatgpt/* PRs are not autonomous factory work,
    # even if a stale/mistaken factory label was applied to them. Labeled local
    # Cursor/fix delivery PRs are admitted so they can finish through review.
    if not is_factory_managed_pr(pr):
        return False
    if labels & BLOCKED_LABELS or 'factory:ready' in labels:
        return False
    if not item_is_unowned(labels):
        return False
    linked = linked_issue_from_pr(pr)
    if linked is not None and linked in issue_map:
        issue_labels = labels_of(issue_map[linked])
        # An existing canonical PR remains executable even when its linked
        # issue carries a stale/terminal blocker from an earlier attempt. The
        # PR's own stage and blocker labels govern whether it can be repaired
        # or reviewed; requiring the issue to be unblocked strands
        # factory:review and factory:changes-requested PRs permanently.
        if not item_is_unowned(issue_labels):
            return False
    return True


def pr_suppresses_issue_candidate(pr: dict[str, Any], issue_map: dict[int, dict[str, Any]]) -> bool:
    """Return whether an open PR is canonical work for its issue.

    Any open PR that claims an issue — a ``factory/<worker>-<issue>-...``
    branch, an explicit ``Closes #N``/``Fixes #N``/``Resolves #N`` reference
    in its body, or a ``Fix #N`` title — owns that issue regardless of author
    or branch naming. Once canonical work exists, the issue must not become
    fresh implementation work again for any reason. Closing or explicitly
    superseding the PR releases the issue. Urgency changes ranking, never
    canonical PR identity.

    Factory provenance is intentionally NOT required here: a manually opened
    ``local/*`` (or any other) PR that explicitly closes an issue is the same
    canonical implementation candidate and must suppress duplicate factory
    intake (#2164). Draft PRs still claim identity for the same fail-closed
    reason; the issue re-enters fresh implementation only when the PR is
    actually closed or superseded.
    """
    del issue_map  # Kept in the signature for compatibility with existing callers.
    return (
        str(pr.get('state') or 'OPEN').upper() == 'OPEN'
        and linked_issue_from_pr(pr) is not None
    )


def parse_depends_on_numbers(body: str) -> set[int]:
    """Return issue numbers declared as explicit prerequisites in the body.

    One parser for every selector: inline ``Depends on #N`` / ``Blocked by #N``
    clusters and dependency-heading lists. Casual ``#N`` mentions never count.
    """
    return eligibility.parse_declared_dependencies(body)


def body_depends_on_unresolved(body: str, open_numbers: set[int]) -> bool:
    """Return whether the body declares a prerequisite that is still open."""
    return bool(parse_depends_on_numbers(body) & open_numbers)


def build_candidates(
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    *,
    no_diff_attempts_by_issue: Mapping[int, int] | None = None,
    no_diff_attempt_records: Iterable[NoDiffAttempt] | None = None,
    include_backpressured_issues: bool = False,
    pr_wip_limit: int = FACTORY_PR_WIP_LIMIT,
    review_backlog_limit: int = FACTORY_REVIEW_BACKLOG_LIMIT,
    external_wip: int = 0,
    incidents: Mapping[int, int] | None = None,
    intake_holds: bool = True,
) -> list[Candidate]:
    """Build and rank executable issue and pull-request candidates.

    Args:
        issues: Open issue payloads from GitHub.
        prs: Open pull-request payloads from GitHub.
        no_diff_attempts_by_issue: Retry counts keyed by issue number.
        no_diff_attempt_records: Structured no-diff retry records.
        pr_wip_limit: Effective repository limit for open implementation PR work.
        review_backlog_limit: Effective repository completion backlog limit.
        include_backpressured_issues: Keep otherwise-eligible issues that are
            only held back by the PR WIP or review-backlog gate. This is for
            queue visibility and ranking, never for bypassing backpressure:
            lease assignment still uses the default filtered form.
        incidents: Declared incidents (issue -> attempts); None reads
            ``FACTORY_INCIDENTS``. While one is open its work ranks first, its
            issue bypasses intake limits, and other new issue intake pauses.
        intake_holds: Hold issues with an open partial PR or that need a split
            (``factory_issue_intake_guard``). A targeted run passes False.
    """
    issue_map = {int(issue['number']): issue for issue in issues}
    retry_counts = no_diff_attempts_by_issue or {}
    attempt_records = None if no_diff_attempt_records is None else list(no_diff_attempt_records)
    suppressing_pr_issues = {
        linked
        for pr in prs
        if (linked := linked_issue_from_pr(pr)) is not None
        and pr_suppresses_issue_candidate(pr, issue_map)
    }
    open_numbers = {
        int(issue['number'])
        for issue in issues
        if str(issue.get('state') or 'OPEN').upper() == 'OPEN'
    }
    if pr_wip_limit < 1 or review_backlog_limit < 1:
        raise ValueError('factory WIP and review backlog limits must be positive')
    if external_wip < 0:
        raise ValueError('external WIP must be nonnegative')
    pr_wip_full = factory_pr_wip_count(prs) + external_wip >= pr_wip_limit
    review_backlog_full = (
        intake_backlog_count(prs, review_backlog_limit) >= review_backlog_limit
    )
    incident_issues = set(active_incidents(issues, incidents))
    slices = intake_guard.open_slice_issues(prs) if intake_holds else set()
    limit = intake_guard.body_limit()
    candidates: list[Candidate] = []
    overflow: list[Candidate] = []
    reserve = review_backlog_full and urgent_reserve_available(issues, prs)
    for issue in issues:
        number = int(issue['number'])
        in_incident = number in incident_issues
        if not issue_is_static_candidate(
            issue,
            suppressing_pr_issues,
            no_diff_attempts=max(0, int(retry_counts.get(number, 0))),
        ):
            continue
        body = str(issue.get('body') or '')
        if body_depends_on_unresolved(body, open_numbers - {number}):
            continue
        if intake_holds and not in_incident and intake_guard.held(issue, slices, limit=limit):
            continue
        use_reserve = False
        if not include_backpressured_issues and not in_incident:
            if incident_issues:
                continue
            if pr_wip_full and not issue_bypasses_wip_limit(issue):
                continue
            if review_backlog_full and not issue_bypasses_wip_limit(
                issue,
                review_backlog_saturated=True,
            ):
                if not reserve or not needs_urgent_reserve(issue):
                    continue
                use_reserve = True
        labels = labels_of(issue)
        (overflow if use_reserve else candidates).append(
            Candidate(
                kind='issue',
                number=number,
                lane=provenance_lane(labels),
                priority=priority_rank(labels),
                created_at=str(issue.get('createdAt') or ''),
                stage=stage_of(labels),
                override=OVERRIDE_LABEL in labels,
                incident=in_incident,
            )
        )
    if overflow:
        candidates.append(min(overflow, key=Candidate.sort_key))
    for pr in prs:
        pr_number = int(pr['number'])
        pr_labels = labels_of(pr)
        if attempt_records is not None:
            pr_attempts = matching_pr_no_diff_attempts(
                attempt_records,
                pr_number=pr_number,
                head_sha=pr_head_sha(pr),
                stage=stage_of(pr_labels),
                conflicted=pr_is_conflicted(pr),
            )
        else:
            pr_attempts = max(0, int(retry_counts.get(pr_number, 0)))
        if not pr_is_static_candidate(
            pr,
            issue_map,
            no_diff_attempts=pr_attempts,
        ):
            continue
        # Recent no-diff retries suppress only the unchanged execution
        # generation. A new head, newly actionable review stage, or newly
        # developed merge conflict starts a fresh budget without rewriting
        # truthful labels to factory:blocked.
        linked = linked_issue_from_pr(pr)
        labels = set(pr_labels)
        if linked is not None and linked in issue_map:
            labels |= labels_of(issue_map[linked])
        lane = provenance_lane(labels, pull_request=True)
        candidates.append(
            Candidate(
                kind='pr',
                number=pr_number,
                lane=lane,
                priority=priority_rank(labels),
                created_at=str(pr.get('createdAt') or ''),
                linked_issue=linked,
                stage=stage_of(pr_labels),
                producer_worker=producer_worker_from_pr(pr),
                conflicted=pr_is_conflicted(pr),
                head_contributors=head_contributors_from_pr(pr),
                is_fork=pr.get('isCrossRepository') is True,
                override=OVERRIDE_LABEL in labels,
                incident=incident_issue_of(pr) in incident_issues,
            )
        )
    return sorted(candidates, key=Candidate.sort_key)


def reserved_urgent_issue(
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    *,
    pr_wip_limit: int = FACTORY_PR_WIP_LIMIT,
    review_backlog_limit: int = FACTORY_REVIEW_BACKLOG_LIMIT,
) -> int | None:
    """Identify the sole ranked overflow issue for portable intake and diagnostics."""
    if intake_backlog_count(prs, review_backlog_limit) < review_backlog_limit:
        return None
    if not urgent_reserve_available(issues, prs):
        return None
    urgent = {int(issue['number']) for issue in issues if needs_urgent_reserve(issue)}
    return next((
        candidate.number for candidate in build_candidates(
            issues, prs, pr_wip_limit=pr_wip_limit, review_backlog_limit=review_backlog_limit
        ) if candidate.kind == 'issue' and not candidate.incident and candidate.number in urgent
    ), None)


def explain_issue_report(
    number: int,
    payload: dict[str, Any],
    *,
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    no_diff_attempts: Mapping[int, int],
    intake: set[int] | None,
    open_blocker: bool | None,
    pr_wip_limit: int = FACTORY_PR_WIP_LIMIT,
    review_backlog_limit: int = FACTORY_REVIEW_BACKLOG_LIMIT,
    no_diff_limit: int = FACTORY_NO_DIFF_RETRY_LIMIT,
    incidents: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    """Explain one issue's queue eligibility from already-read factory state.

    This is the pure core of the controller's ``explain`` command and of the
    operator console's ``rotisserie factory why``: both read GitHub their own
    way, then hand the same inputs here so neither can drift from dispatch.

    Args:
        number: The issue number to explain.
        payload: The issue row (state, labels, body).
        issues: Open issue rows, including ``payload``, for dependency checks.
        prs: Open pull request rows for canonical-PR suppression and gates.
        no_diff_attempts: Rolling no-diff retry counts by issue number.
        intake: Rotisserie-authorized issue numbers, or None in legacy mode.
        open_blocker: Live GitHub dependency blocker result; None if unchecked.
        pr_wip_limit: Effective ``FACTORY_PR_WIP_LIMIT``.
        review_backlog_limit: Effective ``FACTORY_REVIEW_BACKLOG_LIMIT``.
        no_diff_limit: Effective ``FACTORY_NO_DIFF_RETRY_LIMIT``.
        incidents: Declared incidents; None reads ``FACTORY_INCIDENTS``.

    Returns:
        A JSON-serializable report with the verdict, its reasons, the canonical
        owning PRs, and the dynamic gate results.
    """
    issue_map = {int(row['number']): row for row in issues}
    suppressing = {
        linked
        for pr in prs
        if (linked := linked_issue_from_pr(pr)) is not None
        and pr_suppresses_issue_candidate(pr, issue_map)
    }
    non_executable = set(NON_EXECUTABLE_ISSUES)
    incident_issues = sorted(active_incidents([*issues, payload], incidents))
    unauthorized: set[int] = set()
    if intake is not None:
        unauthorized = set(issue_map) - intake - set(incident_issues)
        non_executable |= unauthorized
    verdict = eligibility.explain_issue_eligibility(
        number=number,
        state=str(payload.get('state') or 'OPEN'),
        labels=labels_of(payload),
        body=str(payload.get('body') or ''),
        open_numbers=set(issue_map),
        suppressing_issue_numbers=suppressing,
        non_executable_numbers=non_executable,
        no_diff_attempts=no_diff_attempts.get(number, 0),
        no_diff_limit=no_diff_limit,
    )
    owning_prs = sorted(
        int(pr['number'])
        for pr in prs
        if linked_issue_from_pr(pr) == number and pr_suppresses_issue_candidate(pr, issue_map)
    )
    review_backlog = intake_backlog_count(prs, review_backlog_limit)
    in_incident = number in incident_issues
    reserved = reserved_urgent_issue(issues, prs, pr_wip_limit=pr_wip_limit, review_backlog_limit=review_backlog_limit)
    gates: dict[str, Any] = {
        'active_incidents': incident_issues,
        'incident_intake_pause': bool(incident_issues) and not in_incident,
        'factory_pr_wip': factory_pr_wip_count(prs),
        'factory_pr_wip_limit': pr_wip_limit,
        'review_backlog': review_backlog,
        'review_backlog_limit': review_backlog_limit,
        'parked_prs': factory_parked_count(prs),
        'parked_pr_limit': parked_pr_limit(review_backlog_limit),
        'bypasses_wip_limit': in_incident or issue_bypasses_wip_limit(payload),
        'urgent_reserve_issue': reserved,
        'bypasses_saturated_review_backlog': in_incident or number == reserved or issue_bypasses_wip_limit(
            payload, review_backlog_saturated=True
        ),
        'rotisserie_intake': (
            'legacy-unrestricted'
            if intake is None
            else ('authorized' if number not in unauthorized else 'not-authorized')
        ),
    }
    if open_blocker is not None:
        gates['open_dependency_blocker'] = open_blocker
        gates['worker_specific_note'] = (
            'strike-retry producer exclusion is per worker and evaluated at assignment'
        )
    report: dict[str, Any] = {
        'issue': number,
        'eligible': verdict.eligible,
        'reasons': list(verdict.reasons),
        'labels': sorted(labels_of(payload)),
        'open_canonical_prs': owning_prs,
        'dynamic_gates': gates,
    }
    if verdict.eligible and open_blocker:
        report['eligible'] = False
        report['reasons'] = [
            reason for reason in verdict.reasons if not reason.startswith('eligible: ')
        ] + ['a live open dependency blocker is reported by GitHub']
    return report
