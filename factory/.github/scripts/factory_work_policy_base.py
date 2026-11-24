"""Shared constants, settings and label readers for the factory work policy."""
from __future__ import annotations
import importlib
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory_review_policy import (
    TRUSTED_MARKER_APP_SLUGS,
    producer_worker_from_pr as producer_worker_from_values,
)
from factory_adopter import MARKER_PREFIX, reserved_issues

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

NON_EXECUTABLE_ISSUES = set(reserved_issues())
MANUAL_ONLY_MARKER = '<!-- factory-execution:manual-only -->'

OWNER_RE = re.compile(r'^factory:(?:unowned|local|[1-9][0-9]*)$')

FIXED_OWNER_RE = re.compile(r'^factory:(?P<worker>[6-9]|[1-9][0-9]+)$')

STAGE_LABELS = {'factory:building', 'factory:review', 'factory:changes-requested', 'factory:ci', 'factory:ready', 'factory:blocked'}
STAGE_PRECEDENCE = ('factory:blocked', 'factory:ready', 'factory:review', 'factory:changes-requested', 'factory:ci', 'factory:building')

# factory:blocked is reserved for a genuine terminal blocker. Model no-diff
# failures use a separate bounded retry counter supplied by the controller.
BLOCKED_LABELS = {'factory:blocked', 'ralph-status:blocked', 'wontfix', 'invalid', 'duplicate'}
TRUSTED_ASSOCIATIONS = {'OWNER', 'MEMBER', 'COLLABORATOR'}
TRUSTED_FACTORY_APP_SLUGS = set(TRUSTED_MARKER_APP_SLUGS)
REQUIRED_CHECK_FAILURE_STATES = frozenset({'CANCELLED', 'ERROR', 'FAILURE', 'STALE', 'STARTUP_FAILURE', 'TIMED_OUT'})
NO_DIFF_ATTEMPT_RE = re.compile(
    rf'<!--\s*{MARKER_PREFIX}-factory-claim-released-v3:'
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


try:
    import factory_settings as _settings
except ImportError:  # trusted staged copies may carry only this module
    _settings = None


def env_positive_int(name: str, default: int) -> int:
    """Return a positive integer setting: environment, repository variable, default.

    Registered settings (``factory_settings.SETTINGS``) resolve through the
    operator settings registry so ``rotisserie factory settings`` changes apply.
    """
    if _settings is not None:
        if name in _settings.BY_NAME:
            return _settings.integer(name)
        return _settings.integer(name, default)
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
    rf'<!--\s*{MARKER_PREFIX}-factory-no-progress-retired-v1:'
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


def item_is_unowned(labels: set[str]) -> bool:
    """Return whether a label set has no active factory owner."""
    return owner_of(labels) in (None, 'factory:unowned')


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
