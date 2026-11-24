"""Pure post-merge closure policy: issue linking, labels, and acceptance gates."""

from __future__ import annotations

import importlib
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from factory_adopter import MARKER_PREFIX, reserved_issues

_eligibility_dir = Path(__file__).resolve().parent
if not (_eligibility_dir / 'factory_eligibility.py').is_file():
    _eligibility_dir = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(_eligibility_dir))
eligibility = importlib.import_module('factory_eligibility')

PROTECTED_ISSUES = reserved_issues()

FACTORY_LABEL = "factory"
DONE_STATUS_LABEL = "ralph-status:done"
EPIC_ACCEPTANCE_LABELS = frozenset({"epic", "prd"})
CLOSED_STATE = "CLOSED"

# Explicit product-acceptance verdict marker from docs/PRODUCT_ACCEPTANCE_PROTOCOL.md.
# Only a trusted comment carrying this marker with an ACCEPTED verdict lets an
# acceptance parent transition to completed. Merge automation never manufactures it.
PRODUCT_ACCEPTANCE_MARKER = "product-acceptance:v1"
ACCEPTED_VERDICT_RE = re.compile(r"^\s*ACCEPTED\s*$", re.MULTILINE)
NOT_ACCEPTED_VERDICT_RE = re.compile(r"^\s*NOT ACCEPTED\s*$", re.MULTILINE)
# Issues that opt out of autonomous execution, including human/interactive
# operator acceptance and cutover parents such as #2129. Merge automation must
# not close them either: their transition is human-controlled by contract.
MANUAL_ONLY_MARKER = "<!-- factory-execution:manual-only -->"
# Self-declared parent-contract language. A body only declares an acceptance
# parent when it presents itself as the parent; merely mentioning production
# acceptance is not a declaration. #3037 ("Production acceptance split"), #2718,
# and #2128 are executable implementation issues that explicitly defer the
# operator acceptance pass to a separate issue, so a looser phrase match would
# silently remove user-reported bugs and narrow children from delivery.
ACCEPTANCE_PARENT_BODY_RE = re.compile(
    r"acceptance parent|parent acceptance criteria",
    re.IGNORECASE,
)
CHECKBOX_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.*)$")
# A declared child graph (``- [x] #123``) is the structural discriminator
# between a parent contract and an ordinary issue that carries a checklist.
# Child extraction and parent-contract detection share this pattern so a parent
# can never declare a child graph the child gate cannot see.
CHILD_ITEM_RE = re.compile(r"^[ \t]*-[ \t]*\[[ xX]\][ \t]*#(\d+)\b", re.MULTILINE)
CHILD_REF_RE = re.compile(r"^#\d+\b")

OWNER_RE = re.compile(r"^factory:(?:unowned|local|[1-9][0-9]*)$")
ACTIVE_OWNER_RE = re.compile(r"^factory:(?:local|[1-9][0-9]*)$")
STAGE_LABELS = frozenset(
    {
        "factory:building",
        "factory:review",
        "factory:changes-requested",
        "factory:ci",
        "factory:ready",
        "factory:blocked",
    }
)
BLOCKED_STATUS_LABELS = frozenset({"ralph-status:blocked"})
RALPH_STATUS_RE = re.compile(r"^ralph-status:")

BRANCH_ISSUE_RE = re.compile(r"^factory/[0-9]+-(?P<issue>[0-9]+)-")
MERGE_CLOSURE_MARKER = f"{MARKER_PREFIX}-factory-merge-closure-v1"
UNREADABLE_WARNING_MARKER = f"{MARKER_PREFIX}-factory-dependency-unreadable-v1"
SWEEP_MAX_AGE_HOURS = 26


def linked_issue(branch: str, body: str | None) -> int | None:
    """Resolve the issue a delivered PR closes.

    Only explicit closing keywords and the canonical factory branch shape
    select a closure target. A non-closing review handoff such as
    ``Part of #N.`` deliberately keeps #N open, so it never closes or
    completes anything here.

    Args:
        branch: The PR head branch name; factory branches usually encode the issue.
        body: The PR body text, or None when unavailable.

    Returns:
        The explicit closing-reference issue when present, otherwise the issue
        encoded by the factory branch, or None when neither is available.
    """
    closing_issue = eligibility.explicit_closing_issue(body)
    if closing_issue is not None:
        return closing_issue
    match = BRANCH_ISSUE_RE.match(branch or "")
    if match is not None:
        issue_number = int(match.group("issue"))
        if issue_number in eligibility.nonclosing_issue_numbers(body):
            return None
        return issue_number
    return None


def target_labels(current: list[str]) -> list[str]:
    """Build the merged-and-complete label set for a delivered issue.

    Args:
        current: The issue's current label names.

    Returns:
        The atomic replacement set: every unrelated label preserved, transient
        workflow-state, owner, and ralph-status labels removed, plus ``factory``
        and ``ralph-status:done``.
    """
    kept = [
        label
        for label in current
        if not OWNER_RE.match(label)
        and label not in STAGE_LABELS
        and not RALPH_STATUS_RE.match(label)
        and label != FACTORY_LABEL
    ]
    return kept + [FACTORY_LABEL, DONE_STATUS_LABEL]


def issue_is_quiescent(labels: list[str]) -> bool:
    """Return whether an open issue shows no signal of live continuation.

    A stale workflow-stage or ralph-status label on an issue whose delivery
    already merged is not liveness: the truthful liveness signals are a real
    next-action owner lease and an open successor PR, which the caller checks
    separately. Only genuine blockers refuse closure here.

    Args:
        labels: The issue's current label names.

    Returns:
        False when a real owner lease or a blocked marker is present; True
        otherwise, including contradictory stale stages like ``in-progress``
        alongside ``factory:ready`` under ``factory:unowned``.
    """
    for label in labels:
        if label in BLOCKED_STATUS_LABELS or label == "factory:blocked":
            return False
        if ACTIVE_OWNER_RE.match(label):
            return False
    return True


def closure_blocked_by_active_work(labels: list[str], require_quiescent: bool) -> bool:
    """Return whether closure must wait because the issue shows live work.

    Args:
        labels: The issue's current label names.
        require_quiescent: Whether the caller needs a quiescent issue.

    Returns:
        True when the caller requires quiescence and any active workflow
        stage, active ralph status, or blocked marker is present.
    """
    return require_quiescent and not issue_is_quiescent(labels)


def _child_numbers(body: str, issue_number: int) -> set[int]:
    """Extract child issue numbers from checkbox-style references in the body.

    Only matches ``- [ ] #NNN``, ``- [x] #NNN``, or ``- [X] #NNN`` lines,
    ignoring unrelated issue references such as "Related prior work".

    Args:
        body: The issue body text.
        issue_number: The parent issue number to exclude from results.

    Returns:
        A set of child issue numbers.
    """
    return {
        int(match.group(1))
        for match in CHILD_ITEM_RE.finditer(body)
        if int(match.group(1)) != issue_number
    }


def _declares_child_graph(body: str | None) -> bool:
    """Return whether the body declares at least one checkbox child issue.

    Args:
        body: The issue body text, or None.

    Returns:
        True when a ``- [x] #NNN`` child reference is present.
    """
    return bool(CHILD_ITEM_RE.search(body or ""))


def _issue_has_label(labels: list[str], target: str) -> bool:
    """Return whether the issue has a specific label.

    Args:
        labels: The issue's current label names.
        target: The label name to search for.

    Returns:
        True when the target label is present.
    """
    return target in labels


def _issue_is_acceptance_parent(labels: list[str], body: str | None) -> bool:
    """Return whether the issue is an acceptance parent (epic/PRD with children).

    An acceptance parent is an issue with epic or prd labels — which always
    require an explicit product-acceptance verdict before completion, even when
    no child graph is declared — OR an issue whose body both declares itself a
    product-acceptance parent and ships a checkbox child graph, even when it
    carries no epic/prd label. The second shape is the #1615 incident: an
    enhancement-labeled parent whose child graph and acceptance contract lived
    only in the body.

    Both halves are required for the body shape. A bare mention of production
    acceptance is not a parent declaration: #3037, #2718, and #2128 are
    executable implementation issues that defer the operator acceptance pass to
    a separate issue, and a phrase-only match would strand them and remove
    user-reported work from the factory queue.

    Args:
        labels: The issue's current label names.
        body: The issue body text, or None.

    Returns:
        True when the issue is an acceptance parent whose completion requires
        an explicit product-acceptance verdict.
    """
    if any(_issue_has_label(labels, label) for label in EPIC_ACCEPTANCE_LABELS):
        return True
    text = body or ""
    return bool(ACCEPTANCE_PARENT_BODY_RE.search(text)) and _declares_child_graph(text)


def _unmet_parent_criteria(body: str | None) -> list[str]:
    """Return the parent's own unchecked acceptance criteria (issue #1620).

    Only unchecked boxes that are not child declarations count: child
    checkboxes are gated by child open/closed state instead. A controlled epic
    with every child closed but one unchecked parent criterion still has an
    unmet contract and must remain incomplete.

    Args:
        body: The issue body text, or None.

    Returns:
        The raw text of each unchecked non-child criterion, in body order.
    """
    unmet: list[str] = []
    for line in (body or "").splitlines():
        match = CHECKBOX_RE.match(line)
        if match is None or match.group(1) != " ":
            continue
        text = match.group(2).strip()
        if not text or CHILD_REF_RE.match(text):
            continue
        unmet.append(text)
    return unmet


def _acceptance_verdicts(comment_bodies: Iterable[str]) -> list[str]:
    """Return the product-acceptance verdicts posted in durable comments.

    Args:
        comment_bodies: The raw bodies of comments on the parent issue.

    Returns:
        One ``ACCEPTED``/``NOT ACCEPTED`` entry per comment carrying the
        canonical ``product-acceptance:v1`` marker, in comment order. A marker
        comment whose verdict section names NOT ACCEPTED (or names no ACCEPTED
        verdict) records ``NOT ACCEPTED`` so a silent or failed report can
        never read as acceptance.
    """
    verdicts: list[str] = []
    for comment_body in comment_bodies:
        if PRODUCT_ACCEPTANCE_MARKER not in (comment_body or ""):
            continue
        if NOT_ACCEPTED_VERDICT_RE.search(comment_body or ""):
            verdicts.append("NOT ACCEPTED")
        elif ACCEPTED_VERDICT_RE.search(comment_body or ""):
            verdicts.append("ACCEPTED")
        else:
            verdicts.append("NOT ACCEPTED")
    return verdicts


def _has_accepted_product_acceptance(comment_bodies: Iterable[str]) -> bool:
    """Return whether the latest acceptance report accepts the parent.

    The latest verdict wins: a newer NOT ACCEPTED report revokes an older
    ACCEPTED one, so acceptance always reflects the most recent integrated
    verification against current ``main``.

    Args:
        comment_bodies: The raw bodies of comments on the parent issue.

    Returns:
        True only when at least one acceptance report exists and the latest
        verdict is ACCEPTED.
    """
    verdicts = _acceptance_verdicts(comment_bodies)
    return bool(verdicts) and verdicts[-1] == "ACCEPTED"


def acceptance_parent_may_close(
    labels: list[str],
    body: str | None,
    issue_number: int,
    child_states: Mapping[str, str],
    acceptance_comments: Iterable[str],
) -> bool:
    """Return whether an acceptance parent may transition to completed.

    This is the pure issue-#1620 completion gate: child closure, CI success,
    code coverage, and semantic review are evidence of progress but never
    satisfy it on their own. All of these must hold:

    1. every declared child issue is closed;
    2. every parent acceptance criterion checkbox is checked;
    3. the latest durable ``product-acceptance:v1`` report verdict is ACCEPTED.

    Args:
        labels: The parent issue's current label names.
        body: The parent issue body text, or None.
        issue_number: The parent issue number (excluded from child refs).
        child_states: Mapping of child issue number to its state string
            (``"OPEN"``/``"CLOSED"``); children absent from the map are
            treated as still open (fail closed).
        acceptance_comments: Raw bodies of comments on the parent issue.

    Returns:
        True only when the issue is an acceptance parent whose children are
        all closed, whose own criteria are all met, and whose latest
        acceptance verdict is ACCEPTED.
    """
    if not _issue_is_acceptance_parent(labels, body):
        return False
    children = _child_numbers(body or "", issue_number)
    for child_number in children:
        if str(child_states.get(child_number, "OPEN")) != CLOSED_STATE:
            return False
    if _unmet_parent_criteria(body):
        return False
    return _has_accepted_product_acceptance(acceptance_comments)


def closure_comment(pr_number: int, issue_number: int) -> str:
    """Build the durable closure marker comment for a merged delivery PR.

    Args:
        pr_number: The merged PR number.
        issue_number: The linked issue number being closed.

    Returns:
        The comment text embedding the canonical merge-closure marker.
    """
    return (
        f"<!-- {MERGE_CLOSURE_MARKER}:pr-{pr_number}:issue-{issue_number} -->\n"
        f"Closed automatically: merge automation merged PR #{pr_number} through "
        "the exact-head factory gates, completing this issue's delivery contract."
    )


def merged_within_age(merged_at: str | None, now: datetime, max_age_hours: int) -> bool:
    """Return whether a PR merge timestamp falls inside the sweep window.

    Args:
        merged_at: The PR ``mergedAt`` ISO timestamp, or None when unknown.
        now: The current UTC time.
        max_age_hours: Inclusive age bound in hours.

    Returns:
        True when the timestamp parses and is no older than the bound.
    """
    if not merged_at:
        return False
    try:
        merged = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if merged.tzinfo is None:
        merged = merged.replace(tzinfo=timezone.utc)
    return timedelta(0) <= now - merged <= timedelta(hours=max_age_hours)
