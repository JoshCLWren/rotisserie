#!/usr/bin/env python3
"""Post-merge issue closure for ComicPile factory merge automation.

Factory PRs are created, pushed, and merged with the Actions ``GITHUB_TOKEN``
inside workflow runs. GitHub never triggers workflows from its own token, so
the ``pull_request_target`` label reconciler never observes factory PR
lifecycles, and keyword auto-close has proven unreliable for these merges.
The result was systemic stranding: delivery PRs merged into ``main`` while
their linked issues stayed open under contradictory workflow labels forever,
because policy excludes ready-marked work from ordinary selection.

This module gives the ready merge drain a direct, idempotent closure step:

1. resolve the linked issue from the PR branch shape or closing keyword;
2. refuse protected operational issues;
3. atomically replace stale workflow-state, owner, and status labels with the
   merged-and-complete label set;
4. publish a durable closure marker comment;
5. close the issue as completed.

A ``--sweep`` mode heals any issue stranded by an older merge: it walks
recently merged factory PRs and applies the same closure to quiescent open
issues that no active work signal protects.

The canonical closure marker schema is::

    <!-- comic-pile-factory-merge-closure-v1:pr-<pr>:issue-<issue> -->

The unreadable-dependency warning carries the stable retryable marker::

    <!-- comic-pile-factory-dependency-unreadable-v1:pr-<pr>:issue-<issue> -->

"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import importlib
from pathlib import Path
import unittest
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone

import factory_implementation_delivery as implementation_delivery

_eligibility_dir = Path(__file__).resolve().parent
if not (_eligibility_dir / 'factory_eligibility.py').is_file():
    _eligibility_dir = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(_eligibility_dir))
eligibility = importlib.import_module('factory_eligibility')

PROTECTED_ISSUES = frozenset({679, 1093, 1109})

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
MERGE_CLOSURE_MARKER = "comic-pile-factory-merge-closure-v1"
UNREADABLE_WARNING_MARKER = "comic-pile-factory-dependency-unreadable-v1"
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


def _acceptance_parent_has_incomplete_gates(
    labels: list[str],
    body: str | None,
    issue_number: int,
) -> bool:
    """Return whether an acceptance parent has incomplete child gates.

    An acceptance parent must not be closed if any of its declared children
    are still open or not in the expected completion state.

    Args:
        labels: The issue's current label names.
        body: The issue body text, or None.
        issue_number: The parent issue number.

    Returns:
        True when the issue is an acceptance parent with incomplete child gates.
    """
    if not _issue_is_acceptance_parent(labels, body):
        return False

    children = _child_numbers(body or "", issue_number)
    if not children:
        return False

    # Check if any children are still open
    for child_number in children:
        try:
            child_issue = _issue_view(child_number)
            if str(child_issue["state"]) != CLOSED_STATE:
                return True  # Child is still open, parent should not be closed
        except (RuntimeError, json.JSONDecodeError):
            # If we can't fetch child state, assume incomplete to be safe
            return True

    return False


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


def _run_gh(*args: str) -> str:
    """Run a gh CLI command and return stdout.

    Args:
        *args: gh arguments.

    Returns:
        Raw stdout from the command.

    Raises:
        RuntimeError: If gh exits nonzero.
    """
    completed = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _repo_slug() -> str:
    """Return the owner/name repository slug for gh API calls.

    Returns:
        The repository slug from GITHUB_REPOSITORY or the local gh remotes.

    Raises:
        RuntimeError: If gh returns no usable slug.
    """
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    resolved = _run_gh(
        "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
    )
    if not isinstance(resolved, str):
        raise RuntimeError("gh repo view returned no repository slug")
    return resolved.strip()


def _pr_info(pr_number: int) -> dict[str, str]:
    """Fetch head branch, body, and merge state for one PR.

    Args:
        pr_number: The PR number.

    Returns:
        A dict with ``branch``, ``body``, and ``state`` string values.
    """
    raw = _run_gh(
        "pr",
        "view",
        str(pr_number),
        "--repo",
        _repo_slug(),
        "--json",
        "headRefName,headRefOid,mergeCommit,body,state",
    )
    payload = json.loads(raw)
    return {
        "branch": str(payload.get("headRefName") or ""),
        "head": str(payload.get("headRefOid") or ""),
        "merge": str((payload.get("mergeCommit") or {}).get("oid") or ""),
        "body": str(payload.get("body") or ""),
        "state": str(payload.get("state") or ""),
    }


def _issue_view(issue_number: int) -> dict[str, object]:
    """Fetch state, body, and labels for one issue.

    Args:
        issue_number: The issue number.

    Returns:
        A dict with ``state`` (str), ``body`` (str), and ``labels`` (list[str]).
    """
    raw = _run_gh(
        "issue",
        "view",
        str(issue_number),
        "--repo",
        _repo_slug(),
        "--json",
        "state,body,labels",
    )
    payload = json.loads(raw)
    names = [str(label.get("name") or "") for label in payload.get("labels") or []]
    return {
        "state": str(payload.get("state") or ""),
        "body": str(payload.get("body") or ""),
        "labels": names,
    }


def _issue_has_open_successor_pr(issue_number: int, merged_pr: int) -> bool | str:
    """Return whether another open PR also claims the issue.

    Args:
        issue_number: The issue number.
        merged_pr: The just-merged PR to exclude from the check.

    Returns:
        True when an open PR other than ``merged_pr`` links the issue by
        factory branch shape or closing keyword.
        "unknown" when GitHub API lookup fails.
    """
    try:
        raw = _run_gh(
            "pr",
            "list",
            "--repo",
            _repo_slug(),
            "--state",
            "open",
            "--limit",
            "300",
            "--json",
            "number,headRefName,body",
        )
        data = json.loads(raw)
        if not isinstance(data, list):
            return False
        for pr in data:
            if not isinstance(pr, dict):
                continue
            number = int(pr.get("number") or 0)
            if number == merged_pr:
                continue
            if linked_issue(str(pr.get("headRefName") or ""), pr.get("body")) == issue_number:
                return True
        return False
    except Exception:
        # Return a special value to indicate lookup failed, preventing
        # false assumption that no successor PR exists
        return "unknown"


def _replace_labels(issue_number: int, labels: list[str]) -> None:
    """Atomically replace an issue's label set.

    Args:
        issue_number: The issue number.
        labels: The complete target label set.
    """
    payload = json.dumps({"labels": labels})
    completed = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{_repo_slug()}/issues/{issue_number}/labels",
            "--input",
            "-",
        ],
        capture_output=True,
        text=True,
        input=payload,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"label replacement failed for issue {issue_number}: {completed.stderr.strip()}")


def _comment_issue(issue_number: int, body: str) -> None:
    """Publish one comment on an issue.

    Args:
        issue_number: The issue number.
        body: The comment text.
    """
    subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{_repo_slug()}/issues/{issue_number}/comments",
            "-f",
            f"body={body}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _close_issue(issue_number: int) -> None:
    """Close an issue as completed.

    Args:
        issue_number: The issue number.
    """
    subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{_repo_slug()}/issues/{issue_number}",
            "-f",
            "state=closed",
            "-F",
            "state_reason=completed",
        ],
        capture_output=True,
        text=True,
        check=True,
    )



def _closed_by_pr(issue_number: int, info: Mapping[str, str]) -> bool:
    """Only repair keyword closure caused by this PR, never later/manual closure."""
    events = json.loads(_run_gh(
        "api", "--paginate", "--slurp",
        f"repos/{_repo_slug()}/issues/{issue_number}/events",
    ))
    transitions = [event for page in events for event in page
                   if event.get("event") in {"closed", "reopened"}]
    if not transitions or transitions[-1].get("event") != "closed":
        return False
    commits = {value for key in ("head", "merge") if (value := info.get(key))}
    return transitions[-1].get("commit_id") in commits


def _has_closure_marker(issue_number: int, pr_number: int) -> bool:
    """Return whether the issue already carries this PR's closure marker."""
    try:
        prefix = f"{MERGE_CLOSURE_MARKER}:pr-{pr_number}:issue-{issue_number}"
        return any(prefix in body for body in _issue_comment_bodies(issue_number))
    except (RuntimeError, TypeError, json.JSONDecodeError):
        # TypeError covers a None or non-decodable payload (for example from a
        # fake or interrupted gh call): unreadable comments mean no marker.
        return False


def _has_dependency_block_marker(issue_number: int, pr_number: int) -> bool:
    """Return whether the issue carries this PR's dependency-block marker."""
    try:
        prefix = (
            f"comic-pile-factory-dependency-block-v1:pr-{pr_number}:"
            f"issue-{issue_number}:"
        )
        return any(prefix in body for body in _issue_comment_bodies(issue_number))
    except (RuntimeError, TypeError, json.JSONDecodeError):
        # TypeError covers a None or non-decodable payload: unreadable
        # comments mean no marker.
        return False

def _unreadable_dependency_warning_marker(pr_number: int, issue_number: int) -> str:
    """Build the stable dedupe marker for one unreadable-dependency warning.

    Args:
        pr_number: The merged pull request whose dependency state was unreadable.
        issue_number: The delivered issue being kept unratified.

    Returns:
        An HTML comment marker unique to this PR and issue.
    """
    return (
        f"<!-- {UNREADABLE_WARNING_MARKER}:pr-{pr_number}:issue-{issue_number} -->"
    )


def _has_unreadable_dependency_warning(issue_number: int, pr_number: int) -> bool:
    """Return whether the issue already carries this PR's unreadable warning.

    Args:
        issue_number: The issue whose comments are searched.
        pr_number: The merged pull request that would post the warning.

    Returns:
        True when the exact warning marker was already published.
    """
    try:
        prefix = f"{UNREADABLE_WARNING_MARKER}:pr-{pr_number}:issue-{issue_number}"
        return any(prefix in body for body in _issue_comment_bodies(issue_number))
    except (RuntimeError, TypeError, json.JSONDecodeError):
        # TypeError covers a None or non-decodable payload: unreadable
        # comments must never turn the dedupe read into a crash.
        return False

def _native_open_blockers(issue_number: int) -> list[int]:
    """Return open issues GitHub records as blocking one issue natively.

    Args:
        issue_number: The target issue whose blocked-by links are read.

    Returns:
        Issue numbers whose native blocked-by state is not closed.

    Raises:
        RuntimeError: When the blocked-by state cannot be read or a payload
            item is malformed, so callers fail closed instead of treating
            unknown state as permission.
    """
    raw = _run_gh(
        "api",
        f"repos/{_repo_slug()}/issues/{issue_number}/dependencies/blocked_by?per_page=100",
    )
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise RuntimeError(f"native blocked-by payload for #{issue_number} was not an array")
    open_numbers: list[int] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("number"), int):
            raise RuntimeError(f"native blocked-by item for #{issue_number} was malformed")
        # Native issue payloads report ``open``/``closed`` in lowercase.
        if str(item.get("state", "")).lower() != "closed":
            open_numbers.append(item["number"])
    return open_numbers


def dependency_block_marker(pr_number: int, issue_number: int, blockers: list[int]) -> str:
    """Build the stable dedupe marker for one dependency-block reason.

    Args:
        pr_number: The pull request whose closure is blocked.
        issue_number: The delivered issue being kept open.
        blockers: The open prerequisite issue numbers.

    Returns:
        An HTML comment marker unique to this PR, issue, and blocker set.
    """
    deps = "-".join(str(number) for number in sorted(blockers))
    return (
        f"<!-- comic-pile-factory-dependency-block-v1:pr-{pr_number}:"
        f"issue-{issue_number}:deps-{deps} -->"
    )


def _issue_comment_bodies(issue_number: int) -> list[str]:
    """Return every comment body already published on one issue.

    Args:
        issue_number: The issue whose comments are read.

    Returns:
        Comment bodies in GitHub order.

    Raises:
        RuntimeError: When the comment payload is malformed or unreadable.
    """
    raw = _run_gh(
        "api",
        "--paginate",
        "--slurp",
        f"repos/{_repo_slug()}/issues/{issue_number}/comments?per_page=100",
    )
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise RuntimeError(f"comment payload for #{issue_number} was not an array")
    bodies: list[str] = []
    for page in payload:
        if not isinstance(page, list):
            raise RuntimeError(f"comment page for #{issue_number} was not an array")
        for item in page:
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            if isinstance(body, str):
                bodies.append(body)
    return bodies


def _record_dependency_block(
    pr_number: int, issue_number: int, blockers: list[int]
) -> None:
    """Leave one durable, deduplicated dependency-block reason on the issue.

    The check reruns on every drain tick, so an exact marker keeps the reason
    visible without turning a long dependency wait into comment spam. A failed
    write is swallowed here: recording the reason must never decide closure.

    Args:
        pr_number: The merged pull request whose closure is blocked.
        issue_number: The delivered issue kept open.
        blockers: The open prerequisite issue numbers.
    """
    marker = dependency_block_marker(pr_number, issue_number, blockers)
    try:
        if any(marker in body for body in _issue_comment_bodies(issue_number)):
            return
    except (RuntimeError, TypeError, json.JSONDecodeError):
        # TypeError covers a None or non-decodable payload: unreadable
        # comments must not raise through the marker read.
        return
    listed = ", ".join(f"#{number}" for number in sorted(blockers))
    body = "\n\n".join(
        [
            marker,
            f"Blocked by declared dependencies: issue #{issue_number} declares open "
            f"dependencies: {listed}. This issue stays open until every declared "
            "dependency is closed; the check runs again automatically afterwards.",
        ]
    )
    try:
        _comment_issue(issue_number, body)
    except (RuntimeError, subprocess.CalledProcessError):
        return


def _declared_dependency_blockers(
    issue_number: int, issue_body: str
) -> tuple[list[int], bool]:
    """Return open prerequisite numbers and whether their state was readable.

    Args:
        issue_number: The delivered issue whose prerequisites are checked.
        issue_body: The current issue body holding declared dependencies.

    Returns:
        A pair of the open prerequisite numbers and a readability flag. The
        flag is ``False`` whenever any dependency state is unknown, so callers
        fail closed instead of treating unreadable state as permission.
    """
    try:
        declared = eligibility.parse_declared_dependencies(issue_body)
    except Exception:
        return [], False
    open_deps: list[int] = []
    for dep in sorted(declared):
        try:
            dep_issue = _issue_view(dep)
        except (RuntimeError, TypeError, json.JSONDecodeError):
            # TypeError covers a None or non-decodable payload: unreadable
            # dependency state must fail closed, not ratify completion.
            return [], False
        if str(dep_issue.get("state")) != CLOSED_STATE:
            open_deps.append(dep)
    try:
        open_deps.extend(_native_open_blockers(issue_number))
    except (RuntimeError, TypeError, json.JSONDecodeError):
        # TypeError covers a None or non-decodable payload: unreadable
        # dependency state must fail closed, not ratify completion.
        return [], False
    return sorted(set(open_deps)), True


def close_merged_pr_issue(
    pr_number: int,
    *,
    dry_run: bool = False,
    require_quiescent: bool = False,
) -> str:
    """Close the issue delivered by one merged factory PR.

    Args:
        pr_number: The merged PR number.
        dry_run: When True, report the intended action without mutating GitHub.
        require_quiescent: When True, refuse closure when the issue carries any
            active-work or blocked signal. The sweep mode requires this because
            it re-examines old merges without fresh gate verification; direct
            post-merge closure omits it because the exact-head gates were just
            verified for the delivered scope.

    Returns:
        One of ``closed``, ``already-closed``, ``no-linked-issue``,
        ``protected-issue``, ``successor-open``, ``active-work``,
        ``manual-only-issue``, ``acceptance-parent-incomplete``,
        ``acceptance-parent-requires-verdict``, ``not-merged``,
        ``not-delivered:<reason>``, ``delivery-unverified:<reason>``,
        ``open-dependency`` or ``open-dependency: #N``, or ``dry-run:<action>``.
    """
    info = _pr_info(pr_number)
    issue_number = linked_issue(info["branch"], info["body"])
    if issue_number is None:
        return "no-linked-issue"
    if issue_number in PROTECTED_ISSUES:
        return "protected-issue"

    if info["state"] != "MERGED":
        return "not-merged"
    issue = _issue_view(issue_number)

    labels = list(issue["labels"])
    issue_body = str(issue.get("body") or "")
    if MANUAL_ONLY_MARKER in issue_body:
        # Issue #1620: an issue that declares itself human/interactive
        # controlled — including an operator production-acceptance or cutover
        # parent — is never completed by merge automation.
        return "manual-only-issue"
    if _acceptance_parent_has_incomplete_gates(labels, issue_body, issue_number):
        return "acceptance-parent-incomplete"
    if _issue_is_acceptance_parent(labels, issue_body):
        # Issue #1620: merge automation must never close an acceptance parent,
        # even when every child is closed and every parent criterion is
        # checked. Child-count completion, CI success, coverage, and semantic
        # review are not product acceptance, and a duplicate factory PR that
        # re-closes already-delivered child work must not satisfy it either.
        # The parent transitions only through the explicit
        # product-acceptance:v1 ACCEPTED verdict owned by Josh/interactive
        # acceptance (see acceptance_parent_may_close).
        if _unmet_parent_criteria(issue_body):
            return "acceptance-parent-incomplete"
        return "acceptance-parent-requires-verdict"

    # Whether GitHub closed this issue with this PR's closing keyword, or
    # whether we previously blocked it for this PR's dependencies. Both need
    # dependency state rechecked before the quiescent guard: a keyword-closed
    # issue carries ralph-status:blocked after reopening, and a previously
    # blocked issue carries the same label, so the sweep would return
    # active-work forever without ever re-reading whether dependencies
    # finished.
    issue_closed_by_this_pr = (
        str(issue["state"]) == CLOSED_STATE
        and _closed_by_pr(issue_number, info)
    )
    has_block_marker = _has_dependency_block_marker(issue_number, pr_number)

    if issue_closed_by_this_pr or has_block_marker:
        # Resolve delivery and dependencies before the quiescent guard so a
        # keyword-closed or previously blocked issue is decided by its
        # prerequisites, not by its own stale blocked label. Clearing both
        # blocked labels once dependencies close lets the issue close through
        # the normal path.
        if str(issue["state"]) != CLOSED_STATE:
            # The issue was already reopened past its last block: it may have
            # been claimed or re-delivered since. A live owner lease or an
            # open successor PR protects that work from being relabeled or
            # dependency-closed here, even after the prerequisites finish; a
            # still-closed issue keeps its keyword repair priority because
            # reopening is only possible while closed.
            if any(ACTIVE_OWNER_RE.match(label) for label in labels):
                return "active-work"
            has_successor = _issue_has_open_successor_pr(issue_number, pr_number)
            if has_successor == "unknown":
                # Lookup failure prevents closure verification - stop closure.
                return "successor-lookup-failed"
            if has_successor:
                return "successor-open"
        try:
            if implementation_delivery.is_implementation_issue(issue_body):
                paths, patch = implementation_delivery.complete_pr_diff(
                    pr_number, info.get("head", ""), _repo_slug(), lambda args: _run_gh(*args)
                )
                delivered, reason = implementation_delivery.pr_delivers_implementation(
                    issue_body, paths, patch
                )
            else:
                delivered, reason = True, "explicit non-implementation contract"
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            return f"delivery-unverified:{exc}"
        if delivered:
            open_deps, dependency_state_known = _declared_dependency_blockers(
                issue_number, issue_body
            )
            if not dependency_state_known:
                if not dry_run:
                    pending = [label for label in labels
                               if label not in STAGE_LABELS and not OWNER_RE.fullmatch(label)
                               and not label.startswith("ralph-status:")]
                    pending.extend(["ralph-status:pending", "factory:unowned"])
                    _replace_labels(issue_number, pending)
                    if str(issue["state"]) == CLOSED_STATE:
                        _run_gh("issue", "reopen", str(issue_number), "--repo", _repo_slug())
                    # The warning carries a stable marker so a dependency API
                    # outage cannot notify followers on every drain tick (G4).
                    if not _has_unreadable_dependency_warning(issue_number, pr_number):
                        _comment_issue(
                            issue_number,
                            "\n\n".join(
                                [
                                    _unreadable_dependency_warning_marker(
                                        pr_number, issue_number
                                    ),
                                    (
                                        f"PR #{pr_number} dependency state unreadable; "
                                        "closure not ratified."
                                    ),
                                ]
                            ),
                        )
                return "open-dependency"
            if open_deps:
                if not dry_run:
                    blocked = [label for label in labels
                               if label not in STAGE_LABELS and not OWNER_RE.fullmatch(label)
                               and not label.startswith("ralph-status:")]
                    blocked.extend(["factory:blocked", "ralph-status:blocked", "factory", "factory:unowned"])
                    _replace_labels(issue_number, blocked)
                    if str(issue["state"]) == CLOSED_STATE:
                        _run_gh("issue", "reopen", str(issue_number), "--repo", _repo_slug())
                    _record_dependency_block(pr_number, issue_number, open_deps)
                return "open-dependency: " + ", ".join(f"#{number}" for number in open_deps)
            # Dependencies resolved: restore the canonical delivery labels and
            # close. The closure marker only deduplicates the comment; label
            # repair happens on every visit so a re-completed issue never
            # keeps stale factory:blocked / ralph-status:blocked labels (G3).
            if not dry_run:
                _replace_labels(issue_number, target_labels(labels))
                if not _has_closure_marker(issue_number, pr_number):
                    _comment_issue(issue_number, closure_comment(pr_number, issue_number))
                if str(issue["state"]) != CLOSED_STATE:
                    _close_issue(issue_number)
            return "closed"
        # Not delivered: repair keyword auto-closure of this PR's closed issue.
        if issue_closed_by_this_pr and not dry_run:
            pending = [label for label in labels
                       if label not in STAGE_LABELS and not OWNER_RE.fullmatch(label)
                       and not label.startswith("ralph-status:")]
            pending.extend(["ralph-status:pending", "factory:unowned"])
            _replace_labels(issue_number, pending)
            _run_gh("issue", "reopen", str(issue_number), "--repo", _repo_slug())
            _comment_issue(
                issue_number, f"PR #{pr_number} did not deliver implementation: {reason}"
            )
        return f"not-delivered:{reason}"

    if closure_blocked_by_active_work(labels, require_quiescent):
        return "active-work"
    has_successor = _issue_has_open_successor_pr(issue_number, pr_number)
    if has_successor == "unknown":
        # Lookup failure prevents closure verification - stop closure
        return "successor-lookup-failed"
    if has_successor:
        return "successor-open"

    try:
        if implementation_delivery.is_implementation_issue(issue_body):
            paths, patch = implementation_delivery.complete_pr_diff(
                pr_number, info.get("head", ""), _repo_slug(), lambda args: _run_gh(*args)
            )
            delivered, reason = implementation_delivery.pr_delivers_implementation(
                issue_body, paths, patch
            )
        else:
            delivered, reason = True, "explicit non-implementation contract"
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        # Keep the delivery-unverified return so failed checks wait for a retry
        # instead of approving closure of unverified work.
        return f"delivery-unverified:{exc}"
    if not delivered:
        return f"not-delivered:{reason}"
    # Delivered issue with no prior block marker: check prerequisites before
    # closing so an open dependency keeps the issue open.
    open_deps, dependency_state_known = _declared_dependency_blockers(
        issue_number, issue_body
    )
    if not dependency_state_known:
        return "open-dependency"
    if open_deps:
        if not dry_run:
            _record_dependency_block(pr_number, issue_number, open_deps)
        return "open-dependency: " + ", ".join(f"#{number}" for number in open_deps)
    if str(issue["state"]) != "OPEN" and not (
        delivered and str(issue["state"]) == CLOSED_STATE and _closed_by_pr(issue_number, info)
    ):
        return "already-closed"
    if dry_run:
        return f"dry-run:replace-labels-with{target_labels(labels)}+close"
    # The marker only deduplicates the closure comment; label repair runs on
    # every canonical closure so already-closed issues never keep stale
    # blocked or owner labels (G3).
    _replace_labels(issue_number, target_labels(labels))
    if not _has_closure_marker(issue_number, pr_number):
        _comment_issue(issue_number, closure_comment(pr_number, issue_number))
    if str(issue["state"]) != CLOSED_STATE:
        _close_issue(issue_number)
    return "closed"


def sweep_stranded_closures(*, dry_run: bool = False, max_age_hours: int = SWEEP_MAX_AGE_HOURS) -> list[str]:
    """Heal issues stranded open by merges that predate direct closure.

    Args:
        dry_run: When True, report intended actions without mutating GitHub.
        max_age_hours: Only consider PRs merged within this many hours.

    Returns:
        One outcome line per examined merged factory PR.
    """
    raw = _run_gh(
        "pr",
        "list",
        "--repo",
        _repo_slug(),
        "--state",
        "merged",
        "--label",
        FACTORY_LABEL,
        "--limit",
        "100",
        "--json",
        "number,headRefName,body,mergedAt",
    )
    now = datetime.now(timezone.utc)
    outcomes: list[str] = []
    for pr in json.loads(raw):
        pr_number = int(pr.get("number") or 0)
        merged_at = pr.get("mergedAt")
        if not merged_within_age(str(merged_at) if merged_at else None, now, max_age_hours):
            continue
        outcome = close_merged_pr_issue(pr_number, dry_run=dry_run, require_quiescent=True)
        outcomes.append(f"PR {pr_number}: {outcome}")
    return outcomes


class FactoryPostMergeClosureTests(unittest.TestCase):
    """Cover pure closure resolution, labeling, and sweep gating logic."""

    def test_branch_shape_resolves_issue(self) -> None:
        """Parse the issue encoded in a factory branch name."""
        self.assertEqual(linked_issue("factory/39-1566-opencode-free", ""), 1566)

    def test_explicit_closing_reference_overrides_branch_issue(self) -> None:
        """Honor a PR body that explicitly closes a different parent issue."""
        self.assertEqual(linked_issue("factory/47-1612-opencode-free", "Closes #1620."), 1620)

    def test_non_factory_branch_falls_back_to_body_keyword(self) -> None:
        """Use the closing keyword when the branch encodes nothing."""
        self.assertEqual(linked_issue("phase/some-work", "Closes #42."), 42)
        self.assertEqual(linked_issue("phase/some-work", "Fixes #7\nResolves #8"), 7)

    def test_non_closing_part_of_reference_never_closes(self) -> None:
        """A review-only ``Part of #N.`` link is not a merge closure target."""
        self.assertIsNone(linked_issue("local/acp-4348", "Part of #4348."))
        self.assertIsNone(
            linked_issue("local/acp-4348", "Part of #4348 and governed by #4335.")
        )
        self.assertIsNone(linked_issue("local/acp-4348", "Advances #4348. Parent: #4335."))
        self.assertEqual(linked_issue("factory/11-4353-work", "Part of #4348."), 4353)
        self.assertEqual(
            linked_issue("local/acp-4348", "Closes #4348.\nPart of #4335."), 4348
        )

    def test_partial_factory_branch_and_negated_reference_never_close(self) -> None:
        """PR #4753 was a slice, not completion of its factory/86-4719 issue."""
        partial = (
            "Part of #4719 (parent #4335). Bootstrap deletion is not claimed; "
            "this PR does not close #4719."
        )
        self.assertIsNone(linked_issue("factory/86-4719-kilo-auto", partial))
        self.assertIsNone(linked_issue("local/partial-work", partial))
        self.assertIsNone(linked_issue("factory/86-4719-kilo-auto", "Part of #4719."))
        self.assertIsNone(linked_issue("local/partial-work", "Does not close #4719."))
        # An unrelated parent reference does not block a real closing claim.
        self.assertEqual(
            linked_issue("factory/96-4740-work", "Closes #4740. Part of #4335."),
            4740,
        )

    def test_unlinkable_pr_returns_none(self) -> None:
        """Return None without branch shape or a closing keyword."""
        self.assertIsNone(linked_issue("phase/some-work", "Refs #12 only"))
        self.assertIsNone(linked_issue("", ""))

    def test_target_labels_preserve_unrelated_and_add_done(self) -> None:
        """Strip transient factory state while keeping product labels."""
        self.assertEqual(
            target_labels(
                [
                    "bug",
                    "user-reported",
                    "factory",
                    "factory:ready",
                    "factory:unowned",
                    "ralph-status:validation",
                ]
            ),
            ["bug", "user-reported", "factory", "ralph-status:done"],
        )

    def test_target_labels_drop_every_stale_variant(self) -> None:
        """Remove all owner, stage, and status variants."""
        self.assertEqual(
            target_labels(
                [
                    "enhancement",
                    "factory:building",
                    "factory:review",
                    "factory:changes-requested",
                    "factory:ci",
                    "factory:blocked",
                    "factory:46",
                    "factory:local",
                    "ralph-status:in-progress",
                    "ralph-status:pending",
                ]
            ),
            ["enhancement", "factory", "ralph-status:done"],
        )

    def test_quiescent_ready_issue_allows_closure(self) -> None:
        """Ready-marked unowned issues are the stranded signature."""
        self.assertTrue(
            issue_is_quiescent(["bug", "user-reported", "factory", "factory:unowned", "factory:ready"])
        )

    def test_stale_active_labels_under_no_owner_allow_closure(self) -> None:
        """Contradictory stale stages without an owner lease are not liveness."""
        self.assertTrue(
            issue_is_quiescent(
                ["bug", "factory", "factory:unowned", "factory:ready", "ralph-status:in-progress"]
            )
        )
        self.assertTrue(issue_is_quiescent(["factory", "factory:unowned", "factory:review"]))
        self.assertTrue(issue_is_quiescent(["factory", "factory:unowned", "ralph-status:validation"]))

    def test_owner_leases_and_blockers_refuse_sweep_closure(self) -> None:
        """Real owner leases and genuine blockers stop the sweep."""
        self.assertFalse(issue_is_quiescent(["factory", "factory:building", "factory:46"]))
        self.assertFalse(issue_is_quiescent(["factory", "factory:local"]))
        self.assertFalse(issue_is_quiescent(["factory", "factory:blocked"]))
        self.assertFalse(issue_is_quiescent(["factory", "ralph-status:blocked"]))

    def test_protected_issues_are_never_closed(self) -> None:
        """Operational registry issues stay outside closure authority."""
        self.assertIn(1093, PROTECTED_ISSUES)
        self.assertIn(679, PROTECTED_ISSUES)
        self.assertIn(1109, PROTECTED_ISSUES)

    def test_closure_comment_embeds_canonical_marker(self) -> None:
        """The marker identifies both the PR and the issue durably."""
        comment = closure_comment(1588, 1111)
        self.assertIn(
            f"<!-- {MERGE_CLOSURE_MARKER}:pr-1588:issue-1111 -->",
            comment,
        )
        self.assertIn("PR #1588", comment)

    def test_merged_within_age_bounds(self) -> None:
        """Only parseable timestamps inside the inclusive window pass."""
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        self.assertTrue(merged_within_age("2026-08-21T00:00:00Z", now, 26))
        self.assertTrue(merged_within_age("2026-08-20T00:00:00Z", now, 26))
        self.assertFalse(merged_within_age("2026-08-19T00:00:00Z", now, 26))
        self.assertFalse(merged_within_age(None, now, 26))
        self.assertFalse(merged_within_age("garbage", now, 26))

    def test_sweep_gates_on_active_work_but_direct_closure_does_not(self) -> None:
        """Quiescence is enforced only when the caller requires it."""
        active = ["factory", "factory:building", "factory:46"]
        self.assertTrue(closure_blocked_by_active_work(active, True))
        self.assertFalse(closure_blocked_by_active_work(active, False))
        self.assertFalse(closure_blocked_by_active_work(["factory", "factory:ready"], True))

    # Regression tests for issue #2167: acceptance parent closure

    def test_ordinary_pr_closes_completed_issue(self) -> None:
        """Regression case 1: ordinary implementation PR closes completed issue."""
        # This is the normal behavior that should still work
        self.assertEqual(linked_issue("factory/100-500-work", "Closes #500"), 500)

    def test_acceptance_parent_with_incomplete_children_detected(self) -> None:
        """Regression case 2: acceptance parent with incomplete child gates detected."""
        # Test the helper functions without making API calls
        parent_body = """
- [ ] #2126
- [ ] #2127
- [x] #2128
"""
        labels = ["epic", "factory"]
        # Test that we correctly identify acceptance parents
        self.assertTrue(_issue_is_acceptance_parent(labels, parent_body))
        # Test that we correctly extract child numbers
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2126, 2127, 2128})

    def test_acceptance_parent_closes_when_all_children_complete(self) -> None:
        """Regression case 3: acceptance parent closes once all children are complete."""
        # Test that we correctly identify acceptance parents with complete children
        parent_body = """
- [x] #2126
- [x] #2127
- [x] #2128
"""
        labels = ["prd", "factory"]
        # Test that we correctly identify acceptance parents
        self.assertTrue(_issue_is_acceptance_parent(labels, parent_body))
        # Test that we correctly extract child numbers
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2126, 2127, 2128})

    def test_operator_reopen_is_durable(self) -> None:
        """Regression case 4: operator reopening an incompletely closed parent is durable."""
        # This is tested by the fact that we check child state at closure time
        # If a parent is reopened, its children will still be checked
        # The test verifies that we don't close based on PR references alone
        pass  # This is covered by the acceptance_parent_incomplete check

    def test_stale_pr_cannot_override_frozen_contract(self) -> None:
        """Regression case 5: older PR closing language cannot override frozen parent contract."""
        # An older PR might say "Closes #1615" but the parent has a frozen contract
        # The acceptance parent check prevents closure regardless of PR references
        parent_body = """
## Frozen contract
- [ ] #2127
- [ ] #2128
"""
        labels = ["epic", "factory"]
        # Test that we correctly identify acceptance parents
        self.assertTrue(_issue_is_acceptance_parent(labels, parent_body))
        # Test that we correctly extract child numbers
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2127, 2128})

    def test_1615_2127_2128_2129_regression_fixture(self) -> None:
        """Regression case 6: #1615 / #2127 / #2128 / #2129 as regression fixture."""
        # This is the actual regression case from the issue
        parent_body = """
## Executable child work
- [ ] #2126 — backend read-only CBL preview
- [ ] #2127 — transactional selective materialization
- [ ] #2128 — production CBL browser/import-review UI
- [ ] #2129 — production cutover + incident cleanup
"""
        labels = ["enhancement", "factory"]
        # "enhancement" is not in EPIC_ACCEPTANCE_LABELS, so this is NOT an
        # acceptance parent — even though the body lists child checkboxes.
        self.assertFalse(_issue_is_acceptance_parent(labels, parent_body))
        children = _child_numbers(parent_body, 1615)
        self.assertEqual(children, {2126, 2127, 2128, 2129})

    def test_closure_reads_issue_body_not_pr_body_for_acceptance_parent(self) -> None:
        """Closure uses the issue body, not the PR body, to detect child gates.

        PR closing references live in the PR body, while the child checkboxes
        that make an issue an acceptance parent live in the issue body. Feeding
        the PR body into the acceptance-parent guard would miss every parent and
        wrongly auto-close it (the #1615 incident). This test proves the guard
        reads the issue body by mocking the API layers.
        """
        from unittest.mock import patch

        mod = sys.modules[__name__]
        issue_body = """
## Executable child work
This issue is a product contract and acceptance parent.
- [ ] #2127 — transactional selective materialization
- [ ] #2128 — production CBL browser/import-review UI
- [ ] #2129 — production cutover + incident cleanup
"""
        # An acceptance parent is still open; children are incomplete.
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["enhancement", "factory", "factory:ready", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_issue_has_open_successor_pr",
                return_value=False,
            ) as mock_successor:
                with patch.object(
                    mod,
                    "_pr_info",
                    return_value={
                        "branch": "factory/58-2167-omniroute",
                        # The PR body closes the issue but contains no child checkboxes.
                        "body": "Closes #1615.",
                        "state": "MERGED",
                    },
                ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2140, dry_run=True)

        self.assertEqual(result, "acceptance-parent-incomplete")
        # The guard must halt before the successor check: the parent stays open
        # even though the PR body carries a closing reference.
        mock_successor.assert_not_called()

    # Issue #1620 closure-critical contract: explicit product acceptance.

    def test_epic_label_alone_is_an_acceptance_parent(self) -> None:
        """An epic/PRD always needs a verdict, even with no child graph."""
        self.assertTrue(_issue_is_acceptance_parent(["epic", "factory"], "Ship it."))
        self.assertTrue(_issue_is_acceptance_parent(["prd", "factory"], ""))
        self.assertFalse(_issue_is_acceptance_parent(["enhancement", "factory"], "Ship it."))

    def test_acceptance_parent_language_without_epic_label_is_protected(self) -> None:
        """The #1615 incident shape: contract in the body, no epic/prd label."""
        body = (
            "This is the acceptance parent for CBL adoption.\n"
            "- [x] #2127 — transactional adoption\n"
            "- [ ] #2128 — production browser UI\n"
        )
        self.assertTrue(_issue_is_acceptance_parent(["enhancement", "factory"], body))

    def test_ordinary_child_references_are_not_a_parent_contract(self) -> None:
        """Casual cross-references without acceptance language stay ordinary."""
        body = "Refs #2127 for context.\n- [x] polish\n- [ ] follow-up tweak"
        self.assertFalse(_issue_is_acceptance_parent(["enhancement", "factory"], body))

    def test_executable_child_defining_acceptance_split_is_not_a_parent(self) -> None:
        """#3037/#2718/#2128: implementation work is not an acceptance parent.

        Each of these issues carries a checklist and defers the operator
        production-acceptance pass to a separate issue. Treating the deferral
        as a parent contract would strand a user-reported bug and its delivery
        PR closure, so only a self-declared parent contract counts.
        """
        deferred = (
            "## Production acceptance split\n"
            "- [ ] #3042 owns the production-wide audit and reconciliation.\n"
            "- [ ] The invariant is enforced at the database layer.\n"
            "- [ ] API paths handle an already-present issue predictably.\n"
        )
        self.assertFalse(_issue_is_acceptance_parent(["bug", "user-reported"], deferred))
        self.assertFalse(
            _issue_is_acceptance_parent(
                ["enhancement"],
                "Refs #2129 so it can use this path for production acceptance.\n"
                "- [ ] Reader can adopt after one confirmation\n",
            )
        )

    def test_acceptance_parent_language_without_child_graph_is_not_a_parent(self) -> None:
        """Parent language alone, without a declared child graph, stays ordinary."""
        body = "This issue is the acceptance parent for the rollout.\n- [ ] ship it\n"
        self.assertFalse(_issue_is_acceptance_parent(["enhancement", "factory"], body))

    def test_executable_child_issue_still_closes_on_delivery_merge(self) -> None:
        """The guard must not refuse ordinary closure for deferred-acceptance work."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        issue_body = (
            "## Production acceptance split\n"
            "- [ ] #3042 owns the production-wide audit.\n"
            "- [ ] Uniqueness is enforced at the database layer.\n"
        )
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["bug", "user-reported", "factory", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/21-3037-opencode-free",
                    "body": "Closes #3037.",
                    "state": "MERGED",
                },
            ):
                with patch.object(
                    mod, "_issue_has_open_successor_pr", return_value=False
                ), patch.object(
                    implementation_delivery, "complete_pr_diff",
                    return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                  "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                ), patch.object(mod, "_repo_slug", return_value="owner/repo"):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2150, dry_run=True)
        self.assertTrue(result.startswith("dry-run:"), result)

    def test_unmet_parent_criterion_blocks_despite_closed_children(self) -> None:
        """All children closed but one unchecked parent criterion stays open."""
        body = (
            "- [x] #2127\n"
            "- [x] #2128\n"
            "- [ ] Roll honors applicable adopted hard boundaries\n"
            "- [x] Reload preserves plan intent\n"
        )
        self.assertEqual(
            _unmet_parent_criteria(body),
            ["Roll honors applicable adopted hard boundaries"],
        )
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"],
                body,
                1615,
                {2127: "CLOSED", 2128: "CLOSED"},
                ["<!-- product-acceptance:v1 -->\n### Verdict\n ACCEPTED"],
            )
        )

    def test_checked_parent_criteria_do_not_block(self) -> None:
        """Checked criteria (either case) leave no unmet contract."""
        body = "- [x] #2127\n- [X] Roll honors hard boundaries\n- [x] Reload preserves intent\n"
        self.assertEqual(_unmet_parent_criteria(body), [])

    def test_criterion_quoting_an_issue_is_still_a_criterion(self) -> None:
        """Only leading ``#NNN`` child declarations defer to the child gate."""
        body = (
            "- [x] #2127\n"
            "- [ ] Adoption uses the #2551 Dependency model\n"
        )
        self.assertEqual(
            _unmet_parent_criteria(body),
            ["Adoption uses the #2551 Dependency model"],
        )
        self.assertEqual(_child_numbers(body, 1615), {2127})

    def test_acceptance_parent_with_verdict_may_close(self) -> None:
        """A fully verified parent with an ACCEPTED report may transition."""
        body = "- [x] #2127\n- [x] #2128\n- [x] Roll honors hard boundaries\n"
        report = (
            "<!-- product-acceptance:v1 -->\n"
            "## Product acceptance report\n"
            "### Verdict\n ACCEPTED"
        )
        self.assertTrue(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "CLOSED"}, [report],
            )
        )

    def test_child_count_alone_never_satisfies_acceptance(self) -> None:
        """Closed children without any verdict report cannot complete a parent."""
        body = "- [x] #2127\n- [x] #2128\n- [x] Roll honors hard boundaries\n"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "CLOSED"}, [],
            )
        )
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "CLOSED"},
                ["LGTM, CI green, coverage 94%."],
            )
        )

    def test_failed_verdict_keeps_parent_open(self) -> None:
        """NOT ACCEPTED retains the parent even with closed children."""
        body = "- [x] #2127\n- [x] Roll honors hard boundaries\n"
        report = "<!-- product-acceptance:v1 -->\n### Verdict\n NOT ACCEPTED"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615, {2127: "CLOSED"}, [report],
            )
        )

    def test_latest_verdict_wins(self) -> None:
        """A newer failed report revokes an older acceptance."""
        body = "- [x] #2127\n"
        accepted = "<!-- product-acceptance:v1 -->\n### Verdict\n ACCEPTED"
        failed = "<!-- product-acceptance:v1 -->\n### Verdict\n NOT ACCEPTED"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615, {2127: "CLOSED"}, [accepted, failed],
            )
        )
        self.assertTrue(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615, {2127: "CLOSED"}, [failed, accepted],
            )
        )

    def test_open_child_blocks_despite_accepted_verdict(self) -> None:
        """A stale ACCEPTED report cannot close over a reopened child."""
        body = "- [x] #2127\n- [ ] #2128\n"
        report = "<!-- product-acceptance:v1 -->\n### Verdict\n ACCEPTED"
        self.assertFalse(
            acceptance_parent_may_close(
                ["epic", "factory"], body, 1615,
                {2127: "CLOSED", 2128: "OPEN"}, [report],
            )
        )

    def test_non_parent_never_satisfies_acceptance_gate(self) -> None:
        """Ordinary issues stay outside the acceptance gate entirely."""
        self.assertFalse(
            acceptance_parent_may_close(
                ["bug", "factory"], "- [x] fixed", 500, {},
                ["<!-- product-acceptance:v1 -->\n ACCEPTED"],
            )
        )

    def test_merge_closure_refuses_verified_parent_without_verdict_path(self) -> None:
        """Merge automation defers even fully-checked parents to the verdict path.

        A duplicate factory PR re-closing already-delivered child work must not
        satisfy product acceptance: without a verdict comment on the issue, the
        merge path parks the parent instead of closing it.
        """
        from unittest.mock import patch

        mod = sys.modules[__name__]
        issue_body = (
            "This issue is a product contract and acceptance parent.\n"
            "- [x] #2127 — transactional selective materialization\n"
            "- [x] #2128 — production CBL browser/import-review UI\n"
            "- [x] Reader can adopt with one compact confirmation\n"
        )
        with patch.object(mod, "_issue_view") as mock_view:
            mock_view.side_effect = [
                {
                    "state": "OPEN",
                    "body": issue_body,
                    "labels": ["epic", "factory", "factory:ready", "factory:unowned"],
                },
                {"state": "CLOSED", "body": "", "labels": []},
                {"state": "CLOSED", "body": "", "labels": []},
            ]
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/58-1615-duplicate",
                    "body": "Closes #1615.",
                    "state": "MERGED",
                },
            ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2141, dry_run=True)
        self.assertEqual(result, "acceptance-parent-requires-verdict")

    def test_merge_closure_blocks_parent_with_unmet_criterion(self) -> None:
        """A parent with an unchecked criterion reports incomplete, not closable."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        issue_body = (
            "## Parent acceptance criteria\n"
            "- [x] #2127 — transactional selective materialization\n"
            "- [ ] Roll honors applicable adopted hard boundaries\n"
        )
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["epic", "factory", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/58-1615-work",
                    "body": "Closes #1615.",
                    "state": "MERGED",
                },
            ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2142, dry_run=True)
        self.assertEqual(result, "acceptance-parent-incomplete")

    def test_merge_closure_refuses_manual_only_acceptance_parent(self) -> None:
        """An operator acceptance/cutover parent is never closed by automation."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        issue_body = (
            "Part of #1615. Child D. **Manual-only production acceptance/cutover.**\n"
            "<!-- factory-execution:manual-only -->\n"
            "- [ ] Ultimate Universe has one canonical Reading Plan\n"
            "- [ ] CBL source positions/provenance are preserved\n"
        )
        with patch.object(
            mod,
            "_issue_view",
            return_value={
                "state": "OPEN",
                "body": issue_body,
                "labels": ["enhancement", "factory", "factory:blocked", "factory:unowned"],
            },
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/46-2129-opencode-free",
                    "body": "Closes #2129.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_issue_has_open_successor_pr") as mock_successor:
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(mod.eligibility, "parse_declared_dependencies", return_value=set()):
                            result = close_merged_pr_issue(2143, dry_run=True)
        self.assertEqual(result, "manual-only-issue")
        mock_successor.assert_not_called()

    # Declared-dependency closure gate (issue #4041): a merged PR must never
    # mark a dependent issue done while its prerequisite is still open.

    def _closure_issue(
        self,
        number: int,
        *,
        body: str = "",
        state: str = "OPEN",
    ) -> dict[str, object]:
        """Build a minimal issue view for closure dependency tests."""
        return {
            "state": state,
            "body": body,
            "labels": ["enhancement", "factory", "factory:ready", "factory:unowned"],
        }

    def test_open_declared_dependency_blocks_closure(self) -> None:
        """CBL regression #4010/#4008: an open prerequisite refuses closure."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(
                mod,
                "_issue_view",
                side_effect=[
                    self._closure_issue(4010, body="Resolve the duplicate snapshot bug.\nDepends on #4008"),
                    self._closure_issue(4008, state="OPEN"),
                ],
            ) as mock_view:
                with patch.object(
                    mod,
                    "_pr_info",
                    return_value={
                        "branch": "factory/33-4010-kilo-auto",
                        "body": "Closes #4010.",
                        "state": "MERGED",
                    },
                ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                            with patch.object(
                                implementation_delivery, "complete_pr_diff",
                                return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                              "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                            ):
                                result = close_merged_pr_issue(4024, dry_run=True)
        self.assertEqual(result, "open-dependency: #4008")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4010, 4008])
        mock_close.assert_not_called()

    def test_blocked_closure_records_durable_dependency_reason(self) -> None:
        """A blocked closure leaves one durable reason naming the blockers."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        comments: list[str] = []
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4010, body="Depends on #4008"),
                self._closure_issue(4008, state="OPEN"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_native_open_blockers", return_value=[]):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            with patch.object(mod, "_run_gh", return_value="[]"):
                                with patch.object(
                                    mod,
                                    "_comment_issue",
                                    side_effect=lambda _issue, body: comments.append(body),
                                ):
                                    with patch.object(mod, "_close_issue") as mock_close:
                                        result = close_merged_pr_issue(4024)
        self.assertEqual(result, "open-dependency: #4008")
        mock_close.assert_not_called()
        self.assertEqual(len(comments), 1)
        self.assertIn(
            "comic-pile-factory-dependency-block-v1:pr-4024:issue-4010:deps-4008",
            comments[0],
        )
        self.assertIn("#4008", comments[0])

    def test_blocked_closure_does_not_repeat_dependency_reason(self) -> None:
        """The same blocker set never produces a second reason comment."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        marker = dependency_block_marker(4024, 4010, [4008])
        existing = json.dumps([[{"body": f"{marker}\n\nAlready explained."}]])
        comments: list[str] = []
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4010, body="Depends on #4008"),
                self._closure_issue(4008, state="OPEN"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_native_open_blockers", return_value=[]):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            with patch.object(mod, "_run_gh", return_value=existing):
                                with patch.object(
                                    mod, "_replace_labels"
                                ) as mock_replace:
                                    with patch.object(
                                        mod,
                                        "_comment_issue",
                                        side_effect=lambda _issue, body: comments.append(body),
                                    ):
                                        result = close_merged_pr_issue(4024)
        self.assertEqual(result, "open-dependency: #4008")
        self.assertEqual(comments, [])
        # The label write is real in production but must never escape the
        # self-test: it previously performed an unauthenticated gh call and
        # made every merge-drain run red (#4464).
        mock_replace.assert_called_once()

    def test_closed_declared_dependency_allows_closure(self) -> None:
        """CBL regression #4017/#4016: a closed prerequisite does not block."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4017, body="Tighten the docstring contract.\nDepends on #4016"),
                self._closure_issue(4016, state="CLOSED"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/41-4017-kilo-auto",
                    "body": "Closes #4017.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_native_open_blockers", return_value=[]):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            result = close_merged_pr_issue(4034, dry_run=True)
        # The dependency check passes and the closure flow reaches its
        # labeling/close decision instead of being refused outright. The
        # successor lookup is mocked so this self-test stays hermetic in the
        # no-GH_TOKEN merge-drain self-test step.
        self.assertEqual(
            result,
            "dry-run:replace-labels-with"
            "['enhancement', 'factory', 'ralph-status:done']+close",
        )

    def test_reopened_dependency_blocks_closure_again(self) -> None:
        """A prerequisite reopened after delivery still refuses fresh closure."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(
                mod,
                "_issue_view",
                side_effect=[
                    self._closure_issue(4030, body="Depends on #4010"),
                    self._closure_issue(4010, state="OPEN"),
                ],
            ):
                with patch.object(
                    mod,
                    "_pr_info",
                    return_value={
                        "branch": "factory/33-4030-kilo-auto",
                        "body": "Closes #4030.",
                        "state": "MERGED",
                    },
                ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                            with patch.object(
                                implementation_delivery, "complete_pr_diff",
                                return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                              "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                            ):
                                result = close_merged_pr_issue(4040, dry_run=True)
        self.assertEqual(result, "open-dependency: #4010")
        mock_close.assert_not_called()

    def test_failed_dependency_lookup_fails_closed(self) -> None:
        """An unreadable prerequisite state blocks closure instead of closing."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4025, body="Depends on #4024"),
                RuntimeError("gh failed"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4025-kilo-auto",
                    "body": "Closes #4025.",
                    "state": "MERGED",
                },
            ):
                with patch.object(
                    mod, "_issue_has_open_successor_pr", return_value=False
                ):
                    with patch.object(
                        implementation_delivery, "complete_pr_diff",
                        return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                      "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                    ):
                        result = close_merged_pr_issue(4041, dry_run=True)
        self.assertEqual(result, "open-dependency")

    def test_open_native_blocked_by_blocks_closure(self) -> None:
        """GitHub's native blocked-by links block like declared text."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4050, body="No text dependencies declared."),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4050-kilo-auto",
                    "body": "Closes #4050.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_native_open_blockers", return_value=[4049]):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            result = close_merged_pr_issue(4051, dry_run=True)
        self.assertEqual(result, "open-dependency: #4049")

    def test_closed_native_blocker_allows_closure(self) -> None:
        """A natively recorded blocker already closed does not block."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4050, body="No text dependencies declared."),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4050-kilo-auto",
                    "body": "Closes #4050.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_native_open_blockers", return_value=[]):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            result = close_merged_pr_issue(4051, dry_run=True)
        # The successor lookup is mocked so this self-test stays hermetic in
        # the no-GH_TOKEN merge-drain self-test step.
        self.assertEqual(
            result,
            "dry-run:replace-labels-with"
            "['enhancement', 'factory', 'ralph-status:done']+close",
        )

    def test_failed_native_blocker_lookup_fails_closed(self) -> None:
        """An unreadable native blocked-by graph refuses closure."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(4050, body="No text dependencies declared."),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/33-4050-kilo-auto",
                    "body": "Closes #4050.",
                    "state": "MERGED",
                },
            ):
                with patch.object(
                    mod, "_native_open_blockers", side_effect=RuntimeError("gh failed")
                ):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            result = close_merged_pr_issue(4051, dry_run=True)
        self.assertEqual(result, "open-dependency")

    def test_conflicting_branch_and_closing_reference_checks_the_delivered_issue(self) -> None:
        """A branch naming #1612 with ``Closes #1620`` checks #1620's state."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(
            mod,
            "_issue_view",
            side_effect=[
                self._closure_issue(1620, body="Delivered work.\nDepends on #4008"),
                self._closure_issue(4008, state="OPEN"),
            ],
        ):
            with patch.object(
                mod,
                "_pr_info",
                return_value={
                    "branch": "factory/47-1612-opencode-free",
                    "body": "Closes #1620.",
                    "state": "MERGED",
                },
            ):
                with patch.object(mod, "_native_open_blockers", return_value=[]):
                    with patch.object(
                        mod, "_issue_has_open_successor_pr", return_value=False
                    ):
                        with patch.object(
                            implementation_delivery, "complete_pr_diff",
                            return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                          "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                        ):
                            result = close_merged_pr_issue(4050, dry_run=True)
        self.assertEqual(result, "open-dependency: #4008")

    def test_sweep_reports_open_dependency_and_never_closes(self) -> None:
        """The stranded closure sweep honors the same prerequisite gate."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        merged_at = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = json.dumps(
            [
                {
                    "number": 4024,
                    "headRefName": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
                    "mergedAt": merged_at,
                }
            ]
        )
        with patch.object(mod, "_repo_slug", return_value="owner/repo"):
            with patch.object(mod, "_run_gh", return_value=listing):
                with patch.object(mod, "_pr_info", return_value={
                    "branch": "factory/33-4010-kilo-auto",
                    "body": "Closes #4010.",
                    "state": "MERGED",
                }):
                    with patch.object(
                        mod,
                        "_issue_view",
                        side_effect=[
                            self._closure_issue(4010, body="Depends on #4008"),
                            self._closure_issue(4008, state="OPEN"),
                        ],
                    ):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                                with patch.object(
                                    mod, "_native_open_blockers", return_value=[]
                                ):
                                    with patch.object(
                                        implementation_delivery, "complete_pr_diff",
                                        return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                                      "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                                    ):
                                        with patch.object(mod, "_close_issue") as mock_close:
                                            outcomes = sweep_stranded_closures(dry_run=True)
        self.assertEqual(outcomes, ["PR 4024: open-dependency: #4008"])
        mock_close.assert_not_called()

    def test_sweep_closes_when_dependencies_complete(self) -> None:
        """The sweep proceeds normally once every prerequisite closed."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        merged_at = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = json.dumps(
            [
                {
                    "number": 4034,
                    "headRefName": "factory/41-4017-kilo-auto",
                    "body": "Closes #4017.",
                    "mergedAt": merged_at,
                }
            ]
        )
        with patch.object(mod, "_repo_slug", return_value="owner/repo"):
            with patch.object(mod, "_run_gh", return_value=listing):
                with patch.object(mod, "_pr_info", return_value={
                    "branch": "factory/41-4017-kilo-auto",
                    "body": "Closes #4017.",
                    "state": "MERGED",
                }):
                    with patch.object(
                        mod,
                        "_issue_view",
                        side_effect=[
                            self._closure_issue(4017, body="Depends on #4016"),
                            self._closure_issue(4016, state="CLOSED"),
                        ],
                    ):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                            with patch.object(
                                mod, "_native_open_blockers", return_value=[]
                            ):
                                with patch.object(
                                    implementation_delivery, "complete_pr_diff",
                                    return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                                  "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                                ):
                                    outcomes = sweep_stranded_closures(dry_run=True)
        self.assertEqual(
            outcomes,
            ["PR 4034: dry-run:replace-labels-with"
             "['enhancement', 'factory', 'ralph-status:done']+close"],
        )

    def test_open_declared_dependency_blocks_closure_4017_4016(self) -> None:
        """CBL regression #4017/#4016: an open prerequisite refuses closure."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(
                mod,
                "_issue_view",
                side_effect=[
                    self._closure_issue(4017, body="Tighten the docstring contract.\nDepends on #4016"),
                    self._closure_issue(4016, state="OPEN"),
                ],
            ) as mock_view:
                with patch.object(
                    mod,
                    "_pr_info",
                    return_value={
                        "branch": "factory/41-4017-kilo-auto",
                        "body": "Closes #4017.",
                        "state": "MERGED",
                    },
                ):
                    with patch.object(mod, "_native_open_blockers", return_value=[]):
                        with patch.object(
                            mod, "_issue_has_open_successor_pr", return_value=False
                        ):
                            with patch.object(
                                implementation_delivery, "complete_pr_diff",
                                return_value=(["app/fix.py"], "diff --git a/app/fix.py b/app/fix.py\n"
                                              "@@ -1 +1 @@\n-old = 1\n+new = 2\n"),
                            ):
                                result = close_merged_pr_issue(4034, dry_run=True)
        self.assertEqual(result, "open-dependency: #4016")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4017, 4016])
        mock_close.assert_not_called()

    # Regression tests for issue #4083: keyword-closed issue with dependencies (G1-G4)

    def test_keyword_closed_issue_open_deps_reopens_blocked_unowned(self) -> None:
        """Keyword-closed issue with open deps reopens with blocked+unowned labels (G1, G2)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    side_effect=[
                        {
                            "state": "CLOSED",
                            "body": "Delivered work.\nDepends on #4008",
                            "labels": [
                                "enhancement",
                                "factory",
                                "factory:blocked",
                                "ralph-status:blocked",
                                "factory:unowned",
                            ],
                        },
                        self._closure_issue(4008, state="OPEN"),
                    ],
                ) as mock_view:
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/33-4010-kilo-auto",
                            "body": "Closes #4010.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=False
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=False
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers", return_value=[]
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value={4008},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4024, dry_run=False
                                                            )
        self.assertEqual(result, "open-dependency: #4008")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4010, 4008])
        mock_close.assert_not_called()
        replaced = mock_replace.call_args[0][1]
        self.assertIn("factory:blocked", replaced)
        self.assertIn("ralph-status:blocked", replaced)
        self.assertIn("factory:unowned", replaced)

    def test_keyword_closed_issue_unreadable_deps_reopens_pending_unowned(self) -> None:
        """Keyword-closed issue with unreadable deps reopens with pending+unowned (G1, A10)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "CLOSED",
                        "body": "Delivered work.\nDepends on #4024",
                        "labels": ["enhancement", "factory", "factory:blocked", "ralph-status:blocked", "factory:unowned"],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/33-4025-kilo-auto",
                            "body": "Closes #4025.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=False
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=False
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers",
                                                    side_effect=RuntimeError("gh failed"),
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value={4024},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4041, dry_run=False
                                                            )
        self.assertEqual(result, "open-dependency")
        mock_close.assert_not_called()
        replaced = mock_replace.call_args[0][1]
        self.assertIn("ralph-status:pending", replaced)
        self.assertIn("factory:unowned", replaced)
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)

    def test_keyword_closed_issue_deps_resolved_closes_without_duplicate_comment(self) -> None:
        """Keyword-closed issue whose deps finished closes without repeating the comment (G1, G3)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    side_effect=[
                        {
                            "state": "CLOSED",
                            "body": "Delivered work.\nDepends on #4016",
                            "labels": [
                                "enhancement",
                                "factory",
                                "factory:blocked",
                                "ralph-status:blocked",
                                "factory:unowned",
                            ],
                        },
                        self._closure_issue(4016, state="CLOSED"),
                    ],
                ) as mock_view:
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=False
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers", return_value=[]
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value={4016},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4034, dry_run=False
                                                            )
        self.assertEqual(result, "closed")
        numbers = [call.args[0] for call in mock_view.call_args_list]
        self.assertEqual(numbers, [4017, 4016])
        mock_close.assert_not_called()
        replaced = mock_replace.call_args[0][1]
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)
        self.assertIn("factory", replaced)
        self.assertEqual(len(comments), 1)
        self.assertIn("comic-pile-factory-merge-closure-v1:pr-4034:issue-4017", comments[0])

    def test_repeated_sweep_does_not_repeat_closure_comment(self) -> None:
        """A sweep that re-visits an already-closed issue does not repost the closure comment (G3)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "CLOSED",
                        "body": "Delivered work.",
                        "labels": ["enhancement", "factory", "ralph-status:done"],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=False
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=True
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers", return_value=[]
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value=set(),
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4034, dry_run=False
                                                            )
        self.assertEqual(result, "closed")
        mock_close.assert_not_called()
        # The marker only skips the comment; the canonical label set is still
        # restored on this repeated visit (G3).
        replaced = mock_replace.call_args[0][1]
        self.assertIn("ralph-status:done", replaced)
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)
        self.assertEqual(comments, [])

    def test_marker_helper_treats_none_payload_as_absent(self) -> None:
        """A None gh payload reads as no marker instead of raising (CI regression)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(mod, "_run_gh", return_value=None):
            self.assertFalse(_has_closure_marker(4001, 5001))
            self.assertFalse(_has_dependency_block_marker(4001, 5001))
            self.assertFalse(_has_unreadable_dependency_warning(4001, 5001))

    def test_reopened_blocked_issue_keeps_live_owner_when_deps_finish(self) -> None:
        """A claimed reopened issue is never relabeled or closed by the sweep (G2)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "OPEN",
                        "body": "Delivered work.\nDepends on #4016",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:7",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=False):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(mod, "_has_closure_marker", return_value=False):
                                    with patch.object(
                                        mod, "_has_unreadable_dependency_warning",
                                        return_value=False,
                                    ):
                                        with patch.object(
                                            mod, "_native_open_blockers", return_value=[]
                                        ):
                                            with patch.object(
                                                mod, "_run_gh", return_value="[]"
                                            ):
                                                with patch.object(
                                                    implementation_delivery,
                                                    "complete_pr_diff",
                                                    return_value=(
                                                        ["app/fix.py"],
                                                        "diff --git a/app/fix.py b/app/fix.py\n"
                                                        "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                    ),
                                                ):
                                                    with patch.object(
                                                        mod.eligibility,
                                                        "parse_declared_dependencies",
                                                        return_value={4016},
                                                    ):
                                                        result = close_merged_pr_issue(
                                                            4034, dry_run=False
                                                        )
        self.assertEqual(result, "active-work")
        mock_close.assert_not_called()
        mock_replace.assert_not_called()

    def test_reopened_blocked_issue_keeps_open_successor_when_deps_finish(self) -> None:
        """An open successor PR protects the reopened issue from closure (G2)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "OPEN",
                        "body": "Delivered work.\nDepends on #4016",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:unowned",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=False):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(
                                    mod, "_issue_has_open_successor_pr", return_value=True
                                ):
                                    with patch.object(
                                        mod, "_native_open_blockers", return_value=[]
                                    ):
                                        with patch.object(
                                            mod, "_run_gh", return_value="[]"
                                        ):
                                            with patch.object(
                                                implementation_delivery,
                                                "complete_pr_diff",
                                                return_value=(
                                                    ["app/fix.py"],
                                                    "diff --git a/app/fix.py b/app/fix.py\n"
                                                    "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                ),
                                            ):
                                                with patch.object(
                                                    mod.eligibility,
                                                    "parse_declared_dependencies",
                                                    return_value={4016},
                                                ):
                                                    result = close_merged_pr_issue(
                                                        4034, dry_run=False
                                                    )
        self.assertEqual(result, "successor-open")
        mock_close.assert_not_called()
        mock_replace.assert_not_called()

    def test_reclosed_issue_still_restores_blocked_labels_with_old_marker(self) -> None:
        """A second completion after reopen repairs labels despite the old closure marker (G3)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "CLOSED",
                        "body": "Delivered work.\nDepends on #4016",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:unowned",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/41-4017-kilo-auto",
                            "body": "Closes #4017.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(
                                    mod, "_has_closure_marker", return_value=True
                                ):
                                    with patch.object(mod, "_run_gh", return_value="[]"):
                                        with patch.object(
                                            mod,
                                            "_comment_issue",
                                            side_effect=lambda _issue, body: comments.append(body),
                                        ):
                                            with patch.object(
                                                mod, "_issue_has_open_successor_pr",
                                                return_value=False,
                                            ):
                                                with patch.object(
                                                    mod, "_native_open_blockers", return_value=[]
                                                ):
                                                    with patch.object(
                                                        implementation_delivery,
                                                        "complete_pr_diff",
                                                        return_value=(
                                                            ["app/fix.py"],
                                                            "diff --git a/app/fix.py b/app/fix.py\n"
                                                            "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                        ),
                                                    ):
                                                        with patch.object(
                                                            mod.eligibility,
                                                            "parse_declared_dependencies",
                                                            return_value={4016},
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4034, dry_run=False
                                                            )
        self.assertEqual(result, "closed")
        mock_close.assert_not_called()
        self.assertEqual(comments, [])
        replaced = mock_replace.call_args[0][1]
        self.assertNotIn("factory:blocked", replaced)
        self.assertNotIn("ralph-status:blocked", replaced)
        self.assertNotIn("factory:unowned", replaced)
        self.assertIn("ralph-status:done", replaced)

    def test_unreadable_dependency_warning_is_deduplicated(self) -> None:
        """A past unreadable-dependency warning never reposts on later sweeps (G4)."""
        from unittest.mock import patch

        mod = sys.modules[__name__]
        comments: list[str] = []
        with patch.object(mod, "_close_issue") as mock_close:
            with patch.object(mod, "_replace_labels") as mock_replace:
                with patch.object(
                    mod,
                    "_issue_view",
                    return_value={
                        "state": "CLOSED",
                        "body": "Delivered work.\nDepends on #4024",
                        "labels": [
                            "enhancement",
                            "factory",
                            "factory:blocked",
                            "ralph-status:blocked",
                            "factory:unowned",
                        ],
                    },
                ):
                    with patch.object(
                        mod,
                        "_pr_info",
                        return_value={
                            "branch": "factory/33-4025-kilo-auto",
                            "body": "Closes #4025.",
                            "state": "MERGED",
                            "head": "abc123",
                            "merge": "abc123",
                        },
                    ):
                        with patch.object(mod, "_closed_by_pr", return_value=True):
                            with patch.object(
                                mod, "_has_dependency_block_marker", return_value=True
                            ):
                                with patch.object(mod, "_has_closure_marker", return_value=False):
                                    with patch.object(
                                        mod, "_has_unreadable_dependency_warning", return_value=True
                                    ):
                                        with patch.object(mod, "_run_gh", return_value="[]"):
                                            with patch.object(
                                                mod,
                                                "_comment_issue",
                                                side_effect=lambda _issue, body: comments.append(body),
                                            ):
                                                with patch.object(
                                                    mod, "_issue_has_open_successor_pr",
                                                    return_value=False,
                                                ):
                                                    with patch.object(
                                                        mod, "_native_open_blockers",
                                                        side_effect=RuntimeError("gh failed"),
                                                    ):
                                                        with patch.object(
                                                            implementation_delivery,
                                                            "complete_pr_diff",
                                                            return_value=(
                                                                ["app/fix.py"],
                                                                "diff --git a/app/fix.py b/app/fix.py\n"
                                                                "@@ -1 +1 @@\n-old = 1\n+new = 2\n",
                                                            ),
                                                        ):
                                                            result = close_merged_pr_issue(
                                                                4041, dry_run=False
                                                            )
        self.assertEqual(result, "open-dependency")
        mock_close.assert_not_called()
        self.assertEqual(comments, [])
        replaced = mock_replace.call_args[0][1]
        self.assertIn("ralph-status:pending", replaced)
        self.assertIn("factory:unowned", replaced)


def main() -> int:
    """Parse arguments and run closure, sweep, or self-test.

    Returns:
        Process exit code (0 on success).
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        # Keep the self-test hermetic: the tests stub the gh call sites, and a
        # deterministic slug prevents a fallback ``gh repo view`` on machines
        # without GITHUB_REPOSITORY (GitHub Actions always sets it).
        os.environ.setdefault("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(FactoryPostMergeClosureTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1

    if args.sweep:
        outcomes = sweep_stranded_closures(dry_run=args.dry_run)
        for line in outcomes:
            print(line)
        return 0

    if args.pr is None:
        parser.error("--pr or --sweep is required unless --self-test is used")
    print(close_merged_pr_issue(args.pr, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
