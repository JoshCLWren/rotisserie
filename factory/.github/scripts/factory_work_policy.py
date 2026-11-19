"""Pure ranking and lease policy for the factory work controller."""
from __future__ import annotations
import importlib
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from factory_review_policy import (
    TRUSTED_MARKER_APP_SLUGS,
    performed_via_untrusted_app,
    producer_worker_from_pr as producer_worker_from_values,
)

# factory_eligibility lives in scripts/ (the shared home for both selectors).
# Resolve it relative to this file and load it explicitly so the module
# imports cleanly no matter which directory the caller placed on sys.path.
# The trusted worker stages this policy outside the repository before reviewing
# untrusted PR branches. Prefer the staged canonical module when it is present;
# otherwise use the repository's shared scripts/ module.
_policy_dir = Path(__file__).resolve().parent
_eligibility_dir = (
    _policy_dir
    if (_policy_dir / "factory_eligibility.py").is_file()
    else Path(__file__).resolve().parents[2] / "scripts"
)
sys.path.insert(0, str(_eligibility_dir))
eligibility = importlib.import_module("factory_eligibility")

NON_EXECUTABLE_ISSUES = {679, 1093, 1109}
MANUAL_ONLY_MARKER = '<!-- factory-execution:manual-only -->'

OWNER_RE = re.compile(r'^factory:(?:unowned|local|[1-9][0-9]*)$')

FIXED_OWNER_RE = re.compile(r'^factory:(?P<worker>[6-9]|[1-9][0-9]+)$')

STAGE_LABELS = {'factory:building', 'factory:review', 'factory:changes-requested', 'factory:ci', 'factory:ready', 'factory:blocked'}
STAGE_PRECEDENCE = ('factory:blocked', 'factory:ready', 'factory:review', 'factory:changes-requested', 'factory:ci', 'factory:building')

INFRA_LABELS = {'infrastructure', 'e2e-infrastructure', 'policy-change', 'docs', 'documentation', 'quality-control'}

# factory:blocked is reserved for a genuine terminal blocker. Model no-diff
# failures use a separate bounded retry counter supplied by the controller.
BLOCKED_LABELS = {'factory:blocked', 'ralph-status:blocked', 'wontfix', 'invalid', 'duplicate'}
TRUSTED_ASSOCIATIONS = {'OWNER', 'MEMBER', 'COLLABORATOR'}
TRUSTED_FACTORY_APP_SLUGS = set(TRUSTED_MARKER_APP_SLUGS)
REQUIRED_CHECK_FAILURE_STATES = frozenset({'CANCELLED', 'ERROR', 'FAILURE', 'STALE', 'STARTUP_FAILURE', 'TIMED_OUT'})
NO_DIFF_ATTEMPT_RE = re.compile(
    r'<!--\s*comic-pile-factory-claim-released-v3:'
    r'(?P<kind>issue|pr)-(?P<number>\d+):(?P<worker>[^:>\s]+):(?P<epoch>\d{10}):'
    r'(?P<reason>(?:repair-)?no-persisted-change-handoff|transient-model-interruption|'
    r'review-agent-failed|controller-assignment-read-failed|semantic-review-head-read-failed|'
    r'missing-semantic-verdict|conflicting-semantic-verdict|authoritative-diff-read-failed|fork-repair-execution-failed|fork-repair-result-invalid|fork-dispatch-failed)'
    r'(?::sha=(?P<sha>[0-9a-fA-F]{7,40}))?'
    r'(?::stage=(?P<stage>factory:[a-z-]+))?'
    r'(?::conflicted=(?P<conflicted>[01]))?'
    r'\s*-->'
)
DEP_ON_RE = re.compile(r"(?:[Dd]epends?\s+on)\s+([^\n]+)")
NUMBER_REF_RE = re.compile(r"#(\d+)")
DEPENDENCY_SEPARATORS = frozenset({'and', '&', '+', ','})


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


def env_positive_int(name: str, default: int) -> int:
    """Return a positive integer environment setting or its safe default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f'[factory-controller] ignoring non-numeric {name}={raw!r}; using {default}', file=sys.stderr)
        return default
    if value <= 0:
        print(f'[factory-controller] ignoring non-positive {name}={raw!r}; using {default}', file=sys.stderr)
        return default
    return value


LOCAL_LEASE_TTL_SECONDS = env_positive_int('FACTORY_LOCAL_LEASE_TTL_SECONDS', 86400)
# Long-running agents retain exclusive ownership through the full 100m budget.
# Trusted release still runs immediately on exit; this is a crash-only ceiling.
FIXED_LEASE_TTL_SECONDS = env_positive_int('FACTORY_FIXED_LEASE_TTL_SECONDS', 7500)
FACTORY_PR_WIP_LIMIT = env_positive_int('FACTORY_PR_WIP_LIMIT', 8)
# Incident #2309: engage completion-stage backpressure earlier so review/repair
# drain wins before open PRs pile into conflicts.
FACTORY_REVIEW_BACKLOG_LIMIT = env_positive_int('FACTORY_REVIEW_BACKLOG_LIMIT', 6)
# A trusted retirement handoff resets prior *issue* no-diff suppression only.
# The exhausted PR head's retry history remains intact for the closure decision.
NO_PROGRESS_RETIRE_RE = re.compile(
    r'<!--\s*comic-pile-factory-no-progress-retired-v1:'
    r'issue-(?P<issue>\d+):pr-\d+:sha-[0-9a-f]{40}:epoch-(?P<epoch>\d{10})\s*-->'
)

FACTORY_NO_DIFF_RETRY_LIMIT = env_positive_int('FACTORY_NO_DIFF_RETRY_LIMIT', 4)
FACTORY_NO_DIFF_RETRY_RESET_SECONDS = env_positive_int('FACTORY_NO_DIFF_RETRY_RESET_SECONDS', 86400)
FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS = env_positive_int('FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS', 1800)


@dataclass(frozen=True)
class NoDiffAttempt:
    """One trusted no-diff release still inside the rolling retry window."""

    kind: str
    number: int
    epoch: int
    sha: str | None = None
    stage: str | None = None
    conflicted: bool | None = None
    reason: str = 'repair-no-persisted-change-handoff'


@dataclass(frozen=True)
class Candidate:
    """A ranked unit of executable factory work."""
    kind: str
    number: int
    lane: int
    priority: int
    created_at: str
    linked_issue: int | None = None
    stage: str | None = None
    producer_worker: str | None = None
    conflicted: bool = False
    head_contributors: frozenset[str] = frozenset()
    is_fork: bool = False

    def sort_key(self) -> tuple[int, int, float, int]:
        """Return the deterministic queue ordering key (oldest work first)."""
        return (self.lane, -self.priority, parse_time(self.created_at), self.number)


def parse_time(value: str | None) -> float:
    """Parse an ISO timestamp into a sortable epoch value."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


def labels_of(item: dict[str, Any]) -> set[str]:
    """Return the normalized label names from a GitHub item."""
    result: set[str] = set()
    for label in item.get('labels') or []:
        name = label.get('name') if isinstance(label, dict) else label
        if name:
            result.add(str(name))
    return result


def owner_of(labels: Iterable[str]) -> str | None:
    """Return the active factory owner represented by a label set."""
    owners = [label for label in labels if OWNER_RE.fullmatch(label)]
    active = [label for label in owners if label != 'factory:unowned']
    if active:
        return sorted(active)[0]
    return 'factory:unowned' if 'factory:unowned' in owners else None


def priority_rank(labels: Iterable[str]) -> int:
    """Map explicit priority labels to a deterministic numeric rank."""
    labels = set(labels)
    if 'ralph-priority:critical' in labels or 'priority:P0' in labels or 'priority: P0' in labels:
        return 4
    if 'ralph-priority:high' in labels or 'priority:high' in labels or 'priority: high' in labels:
        return 3
    if 'ralph-priority:medium' in labels:
        return 2
    if 'ralph-priority:low' in labels:
        return 1
    return 0


def linked_issue_from_branch(branch: str | None) -> int | None:
    """Extract an issue number from the canonical factory branch shape."""
    if not branch:
        return None
    match = re.match('^factory/\\d+-(\\d+)-', branch)
    return int(match.group(1)) if match else None


TITLE_ISSUE_RE = re.compile(
    r'(?i)\b(?:fix|fixes|fixed|fixing|resolve|resolves|resolved|resolving|'
    r'close|closes|closed|closing)\s+#(\d+)'
)
def issue_explicitly_closed_by_pr(pr: dict[str, Any]) -> int | None:
    """Return the issue a PR explicitly claims to close, if any.

    Only explicit closing keywords (``Closes #N``, ``Fixes #N``,
    ``Resolves #N`` and their inflected forms) in the PR body count. Casual
    ``#N`` mentions used by stacked/child PRs for cross-reference never claim
    closure, so they must not suppress fresh intake.
    """
    return eligibility.explicit_closing_issue(str(pr.get('body') or ''))


def linked_issue_from_pr(pr: dict[str, Any]) -> int | None:
    """Resolve the canonical linked issue for factory assignment/suppression.

    Prefers the durable ``factory/<worker>-<issue>-...`` branch shape, then an
    explicit closing keyword in the body (``Closes #N``/``Fixes #N``/
    ``Resolves #N``), then a leading ``Fix #N`` reference in the PR title so
    labeled local Cursor/fix delivery PRs still suppress duplicate issue
    intake. Closing references are honored regardless of author or branch
    naming: a manually opened ``local/*`` PR that explicitly owns an issue must
    suppress fresh factory implementation (#2164).
    """
    linked = linked_issue_from_branch(str(pr.get('headRefName') or ''))
    if linked is not None:
        return linked
    closed = issue_explicitly_closed_by_pr(pr)
    if closed is not None:
        return closed
    title = str(pr.get('title') or '')
    match = TITLE_ISSUE_RE.search(title)
    return int(match.group(1)) if match else None


def is_factory_managed_pr(pr: dict[str, Any]) -> bool:
    """Return whether this PR is inside the autonomous factory work queue.

    Canonical factory branches always qualify. Local Cursor/fix delivery PRs
    also qualify once they carry the ``factory`` label so Josh-authored work can
    enter review/repair/drain without rewriting the branch name. An unlabeled PR
    on a non-factory branch stays outside the queue.
    """
    head = str(pr.get('headRefName') or '')
    if head.startswith('factory/'):
        return True
    # Explicitly factory-enrolled interactive PRs are reviewable regardless
    # of the branch prefix. Their GitHub author and provenance are still
    # verified by the trusted review controller before merge authorization.
    return 'factory' in labels_of(pr)


def producer_worker_from_pr(pr: dict[str, Any]) -> str | None:
    """Recover producer identity using the shared review provenance policy."""
    return producer_worker_from_values(branch=str(pr.get('headRefName') or ''), body=str(pr.get('body') or ''))


def head_contributors_from_pr(pr: dict[str, Any]) -> frozenset[str]:
    """Recover controller-recorded contributors of the PR's exact head.

    Only the Rotisserie capture view carries the trusted marker set; the live
    dispatcher list payload does not, and an absent key means "not resolved
    yet" rather than "nobody contributed".
    """
    recorded = pr.get('head_contributors')
    if not isinstance(recorded, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(worker) for worker in recorded if worker)


def stage_of(labels: Iterable[str]) -> str | None:
    """Return the deterministic current factory lifecycle stage."""
    present = set(labels)
    return next((label for label in STAGE_PRECEDENCE if label in present), None)


def normalize_target_state(labels: set[str], current_owner: str | None) -> tuple[str, str | None]:
    """Enforce label-state invariants for a target.

    Returns a tuple of (normalized_owner, normalized_stage).
    
    Invariants:
    1. If owner is 'factory:unowned':
       - Stage cannot be 'factory:building'.
       - If 'ralph-status:blocked' is present, stage must be 'factory:blocked'.
       - Otherwise, if no other stage is present, it is None.
    2. If 'ralph-status:done' is present:
       - No transient factory stage or owner labels should remain.
       - Owner becomes 'factory:unowned'.
       - Stage becomes None.
    3. If an active owner coexists with 'factory:unowned':
       - Drop the redundant unowned marker and preserve the actual stage.
    4. If 'ralph-status:in-progress' is present but owner is 'factory:unowned':
       - Normalize to 'ralph-status:pending' (implicit via labels) and owner 'factory:unowned'.
    """
    # Handle Done state first - it overrides everything
    if 'ralph-status:done' in labels:
        return 'factory:unowned', None

    # Resolve ownership
    owner = current_owner or owner_of(labels) or 'factory:unowned'
    has_unowned = 'factory:unowned' in labels

    # Contradiction: an active owner may not coexist with factory:unowned.
    # A no-persisted-change release can leave both behind; drop the redundant
    # unowned marker but preserve the actual stage.
    if owner != 'factory:unowned' and has_unowned:
        return owner, stage_of(labels)

    # Handle blocked unowned
    if owner == 'factory:unowned' and 'ralph-status:blocked' in labels:
        return 'factory:unowned', 'factory:blocked'

    # Handle building unowned (contradiction)
    if owner == 'factory:unowned':
        stage = stage_of(labels)
        if stage == 'factory:building':
            return 'factory:unowned', None  # Remove building stage if unowned
        return owner, stage

    # Owned targets
    return owner, stage_of(labels)


def provenance_lane(labels: set[str]) -> int:
    """Return the deterministic assignment lane for a label set."""
    if 'main-breakage' in labels or 'deploy-fix' in labels:
        return 0
    if 'user-reported' in labels and 'bug' in labels:
        return 1
    if labels & INFRA_LABELS:
        return 5
    if 'e2e-discovered' in labels:
        return 4
    return 3


def item_is_unowned(labels: set[str]) -> bool:
    """Return whether a label set has no active factory owner."""
    return owner_of(labels) in (None, 'factory:unowned')


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
    count = 0
    for pr in prs:
        if str(pr.get('state') or 'OPEN').upper() != 'OPEN' or pr.get('isDraft'):
            continue
        labels = labels_of(pr)
        if not is_factory_managed_pr(pr):
            continue
        if labels & BLOCKED_LABELS or 'factory:ready' in labels:
            continue
        if stage_of(labels) in ('factory:review', 'factory:ci', 'factory:changes-requested'):
            count += 1
    return count


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


def pr_is_conflicted(pr: dict[str, Any]) -> bool:
    """Return whether GitHub reports the current PR head as merge-conflicted."""
    mergeable = str(pr.get('mergeable') or '').upper()
    merge_state = str(pr.get('mergeStateStatus') or '').upper()
    return mergeable == 'CONFLICTING' or merge_state == 'DIRTY'


def pr_head_sha(pr: Mapping[str, Any]) -> str:
    """Return the lowercase head SHA for a pull-request payload when present."""
    for key in ('headRefOid', 'headOid', 'oid'):
        value = pr.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    head = pr.get('head')
    if isinstance(head, Mapping):
        for key in ('oid', 'sha'):
            value = head.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return ''


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
    if no_diff_attempts >= FACTORY_NO_DIFF_RETRY_LIMIT:
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
    if no_diff_attempts >= FACTORY_NO_DIFF_RETRY_LIMIT:
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


def _leading_reference_numbers(text: str) -> set[int]:
    """Return the '#N' references at the head of a dependency declaration.

    Only the leading reference cluster counts: consecutive ``#N`` tokens
    joined by separators such as ``and``, ``,``, or ``+``.  Prose or casual
    ``#N`` mentions later on the line end the cluster so they never become
    blocking prerequisites.
    """
    result: set[int] = set()
    for token in text.split():
        clean = token.rstrip(',.;:')
        ref = NUMBER_REF_RE.fullmatch(clean)
        if ref:
            result.add(int(ref.group(1)))
        elif clean.lower() not in DEPENDENCY_SEPARATORS:
            break
    return result


def parse_depends_on_numbers(body: str) -> set[int]:
    """Return issue numbers declared as explicit prerequisites in the body.

    Only matches the canonical ``Depends on #NNN`` / ``Depends on #N, #M``
    format used by structured issue declarations.  Casual ``#N`` mentions
    elsewhere in the body are not treated as blocking prerequisites.
    """
    numbers: set[int] = set()
    for match in DEP_ON_RE.finditer(body):
        numbers.update(_leading_reference_numbers(match.group(1)))
    return numbers


def body_depends_on_unresolved(body: str, open_numbers: set[int]) -> bool:
    """Return whether the body declares a prerequisite that is still open."""
    return bool(parse_depends_on_numbers(body) & open_numbers)


def pr_cutover_allowed(
    pr: dict[str, Any],
    issue_map: Mapping[int, dict[str, Any]],
    *,
    cutover_open: bool,
) -> bool:
    """Keep all PR lifecycle and capacity gates on the same cutover boundary."""
    if not cutover_open:
        return True
    delivered = {
        number for number in (
            linked_issue_from_pr(pr), issue_explicitly_closed_by_pr(pr)
        ) if number is not None
    }
    if delivered:
        return all(
            number in issue_map
            and eligibility.rust_cutover_work_allowed(
                labels_of(issue_map[number]),
                str(issue_map[number].get('body') or ''),
                cutover_open=True,
            )
            for number in delivered
        )
    return eligibility.rust_cutover_work_allowed(
        labels_of(pr), str(pr.get('body') or ''), cutover_open=True
    )


def build_candidates(
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    *,
    no_diff_attempts_by_issue: Mapping[int, int] | None = None,
    no_diff_attempt_records: Iterable[NoDiffAttempt] | None = None,
    include_backpressured_issues: bool = False,
) -> list[Candidate]:
    """Build and rank executable issue and pull-request candidates.

    Args:
        issues: Open issue payloads from GitHub.
        prs: Open pull-request payloads from GitHub.
        no_diff_attempts_by_issue: Retry counts keyed by issue number.
        no_diff_attempt_records: Structured no-diff retry records.
        include_backpressured_issues: Keep otherwise-eligible issues that are
            only held back by the PR WIP or review-backlog gate. This is for
            queue visibility and ranking, never for bypassing backpressure:
            lease assignment still uses the default filtered form.
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
    cutover_open = eligibility.RUST_CUTOVER_EPIC in open_numbers
    allowed_prs = [
        pr for pr in prs
        if pr_cutover_allowed(pr, issue_map, cutover_open=cutover_open)
    ]
    # Deferred PRs do not consume the Rust lane's review or active WIP budget.
    pr_wip_full = factory_pr_wip_count(allowed_prs) >= FACTORY_PR_WIP_LIMIT
    review_backlog_full = (
        factory_review_backlog_count(allowed_prs) >= FACTORY_REVIEW_BACKLOG_LIMIT
    )
    candidates: list[Candidate] = []
    for issue in issues:
        number = int(issue['number'])
        if not eligibility.rust_cutover_work_allowed(
            labels_of(issue), str(issue.get('body') or ''), cutover_open=cutover_open
        ):
            continue
        if not issue_is_static_candidate(
            issue,
            suppressing_pr_issues,
            no_diff_attempts=max(0, int(retry_counts.get(number, 0))),
        ):
            continue
        body = str(issue.get('body') or '')
        if body_depends_on_unresolved(body, open_numbers - {number}):
            continue
        if not include_backpressured_issues:
            if pr_wip_full and not issue_bypasses_wip_limit(issue):
                continue
            if review_backlog_full and not issue_bypasses_wip_limit(
                issue,
                review_backlog_saturated=True,
            ):
                # End-to-end backpressure: while the completion stages are
                # saturated, the fleet drains existing PRs instead of
                # manufacturing new ones. Only main-breakage bypasses this gate
                # when the review backlog is saturated (>= FACTORY_REVIEW_BACKLOG_LIMIT).
                continue
        labels = labels_of(issue)
        candidates.append(
            Candidate(
                kind='issue',
                number=number,
                lane=provenance_lane(labels),
                priority=priority_rank(labels),
                created_at=str(issue.get('createdAt') or ''),
                stage=stage_of(labels),
            )
        )
    for pr in allowed_prs:
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
            if pr_attempts >= FACTORY_NO_DIFF_RETRY_LIMIT:
                reasons = Counter(
                    attempt.reason for attempt in attempt_records or []
                    if matching_pr_no_diff_attempts(
                        (attempt,), pr_number=pr_number, head_sha=pr_head_sha(pr),
                        stage=stage_of(pr_labels), conflicted=pr_is_conflicted(pr),
                    )
                )
                print(
                    f'[factory-controller] retry cooldown: PR #{pr_number} '
                    f'head={pr_head_sha(pr)} stage={stage_of(pr_labels)} '
                    f'recent_failures={pr_attempts} reasons={dict(sorted(reasons.items()))}; '
                    f'new head/stage or expiry restores eligibility',
                    file=sys.stderr,
                )
            continue
        # Recent no-diff retries suppress only the unchanged execution
        # generation. A new head, newly actionable review stage, or newly
        # developed merge conflict starts a fresh budget without rewriting
        # truthful labels to factory:blocked.
        linked = linked_issue_from_pr(pr)
        labels = set(pr_labels)
        if linked is not None and linked in issue_map:
            labels |= labels_of(issue_map[linked])
        lane = provenance_lane(labels)
        if lane == 1:
            lane = 2
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
            )
        )
    return sorted(candidates, key=Candidate.sort_key)


def review_share_for_backlog(review_backlog: int) -> float:
    """Return the review-first worker share for the current completion pressure.

    Use a continuous ratio: 25% at idle, rising to 90% by three pending
    completion PRs. Reserve reviewers before new issue intake can outrun them.
    """
    depth = max(0, int(review_backlog))
    return min(0.90, 0.25 + (0.65 * min(depth, 3) / 3.0))


def review_capacity_worker(worker: str, *, review_backlog: int = 0) -> bool:
    """Return whether this worker slot prioritizes review under current pressure.

    Membership in the review-first cohort is deterministic from the worker id and
    the ratio from :func:`review_share_for_backlog`.
    """
    share = review_share_for_backlog(review_backlog)
    # Multiply by a fixed odd constant so nearby worker ids do not clump.
    return (int(worker) * 37) % 100 < int(round(share * 100))


def candidate_is_independent_for_worker(candidate: Candidate, worker: str) -> bool:
    """Prevent a contributing factory from being assigned semantic review of its PR.

    Issue #3059 makes every worker that authored the exact head ineligible to
    attest it, not only the worker that opened the PR. Excluding contributors at
    selection time is what lets queue selection hand the head to another
    eligible reviewer automatically instead of churning the same worker
    through a controller refusal.
    """
    if candidate.kind == 'pr' and candidate.is_fork:
        # Fork execution and review authorization are independent of model IDs.
        from factory_worker_slots import resolve

        try:
            source = resolve(worker).source
        except ValueError:
            return False
        if candidate.stage == 'factory:review':
            if source == 'kilo-auto':
                return False
        elif source != 'kilo-auto':
            return False
    if candidate.kind != 'pr' or candidate.stage != 'factory:review':
        return True
    if candidate.producer_worker == worker:
        return False
    return worker not in candidate.head_contributors


def worker_delivery_role(worker: str) -> str:
    """No provider/model pins dictate intake or completion preferences."""
    del worker
    return ''


def order_candidates_for_worker(candidates: list[Candidate], worker: str) -> list[Candidate]:
    """Balance completion pressure with a deterministic fresh-issue intake lane."""
    eligible = [
        candidate
        for candidate in candidates
        if candidate_is_independent_for_worker(candidate, worker)
    ]
    review_backlog = sum(1 for c in candidates if c.kind == "pr" and c.stage in ("factory:review", "factory:ci", "factory:changes-requested"))
    review_first = review_capacity_worker(worker, review_backlog=review_backlog)
    role = worker_delivery_role(worker)

    def role_preference(candidate: Candidate) -> int:
        # Protected main-breakage and user-reported lanes outrank role preferences.
        if candidate.lane <= 1:
            return candidate.lane
        if (
            review_first
            and candidate.kind == 'pr'
            and candidate.stage == 'factory:review'
            and not candidate.conflicted
        ):
            return 1
        if role == 'starter' and candidate.kind == 'issue':
            return 2
        if role == 'finisher' and candidate.kind == 'pr' and candidate.stage in {
            'factory:changes-requested', 'factory:ci',
        }:
            return 2
        return 3

    def work_class(candidate: Candidate) -> int:
        if candidate.lane == 0:
            return 0
        if candidate.kind == 'pr':
            # A clean review-stage PR is one independent approval from merge.
            # Review-first workers finish it ahead of everything but lane 0;
            # non-review workers preserve product capacity (issue first).
            if candidate.stage == 'factory:review' and not candidate.conflicted:
                return 1 if review_first else 6
            if candidate.conflicted:
                return 2 if review_first else 8
            if candidate.stage == 'factory:ci':
                return 3
            if candidate.stage == 'factory:changes-requested':
                return 4
            return 5 if review_first else 9
        if candidate.lane == 1:
            return 6 if review_first else 4
        return 7 if review_first else 5

    return sorted(
        eligible,
        key=lambda candidate: (role_preference(candidate), work_class(candidate), candidate.sort_key()),
    )


def plan_distinct_assignments(candidates: list[Candidate], workers: list[str]) -> dict[str, Candidate]:
    """Pure helper used by regression coverage for one dispatcher batch."""
    remaining = list(candidates)
    assignments: dict[str, Candidate] = {}
    for worker in workers:
        ordered = order_candidates_for_worker(remaining, worker)
        if not ordered:
            continue
        selected = ordered[0]
        assignments[worker] = selected
        remaining.remove(selected)
    return assignments


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


def lease_is_stale(owner: str, *, active_fixed_workers: set[int], has_unresolved_active_runs: bool | None = None, latest_activity_epoch: int | None, now_epoch: int, local_ttl_seconds: int=LOCAL_LEASE_TTL_SECONDS, fixed_ttl_seconds: int=FIXED_LEASE_TTL_SECONDS) -> bool:
    """Return whether a factory lease can be proven stale."""
    if owner == 'factory:local':
        return latest_activity_epoch is not None and now_epoch - latest_activity_epoch > local_ttl_seconds
    match = FIXED_OWNER_RE.fullmatch(owner)
    if match:
        # Callers predating the run-identity fence did not provide this bit.
        # Preserve their conservative legacy decision while current callers
        # pass an explicit value and therefore fail closed on unknown runs.
        if has_unresolved_active_runs is None:
            return int(match.group('worker')) not in active_fixed_workers
        if has_unresolved_active_runs:
            return False
        worker = int(match.group('worker'))
        if worker in active_fixed_workers or latest_activity_epoch is None:
            return False
        return now_epoch - latest_activity_epoch > fixed_ttl_seconds
    return False


# Shared eligibility functions exported for consistency across selectors
def is_manual_only(body: str | None) -> bool:
    """Return whether a body carries the structured manual-only directive.
    
    Delegates to the shared eligibility module to ensure consistency across selectors.
    """
    return eligibility.is_manual_only(body)


def is_acceptance_parent(body: str | None) -> bool:
    """Return whether a body declares a product-acceptance parent contract.
    
    Delegates to the shared eligibility module to ensure consistency across selectors.
    """
    return eligibility.is_acceptance_parent(body)


def parse_declared_dependencies(body: str | None) -> set[int]:
    """Return every issue number declared as an explicit prerequisite.
    
    Delegates to the shared eligibility module to ensure consistency across selectors.
    """
    return eligibility.parse_declared_dependencies(body)



