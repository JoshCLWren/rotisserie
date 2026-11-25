#!/usr/bin/env python3
"""Post-merge issue closure for factory merge automation.

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

The canonical closure marker schema, where ``<prefix>`` is
``factory_adopter.marker_prefix()``, is::

    <!-- <prefix>-factory-merge-closure-v1:pr-<pr>:issue-<issue> -->

The unreadable-dependency warning carries the stable retryable marker::

    <!-- <prefix>-factory-dependency-unreadable-v1:pr-<pr>:issue-<issue> -->

"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from datetime import datetime, timezone

import factory_acceptance_criteria as acceptance_criteria
import factory_implementation_delivery as implementation_delivery
import factory_post_merge_closure_github as github
from factory_merge_linkage import explain, linkage

try:
    import factory_memory as memory
except ImportError:  # a worker's staged copy has no store and no memory module
    memory = None
from factory_post_merge_closure_policy import (
    ACTIVE_OWNER_RE,
    CLOSED_STATE,
    FACTORY_LABEL,
    MANUAL_ONLY_MARKER,
    MARKER_PREFIX,
    OWNER_RE,
    PROTECTED_ISSUES,
    STAGE_LABELS,
    SWEEP_MAX_AGE_HOURS,
    _child_numbers,
    _issue_is_acceptance_parent,
    _unmet_parent_criteria,
    closure_blocked_by_active_work,
    closure_comment,
    eligibility,
    linked_issue,
    merged_within_age,
    target_labels,
)

SELF_TEST_MODULES = (
    "test_factory_post_merge_closure_policy",
    "test_factory_post_merge_closure_acceptance",
    "test_factory_post_merge_closure_dependencies",
    "test_factory_post_merge_closure_sweep",
    "test_factory_post_merge_closure_reopen",
    "test_factory_post_merge_closure_person_reopen",
    "test_factory_merge_linkage",
)


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
            child_issue = github._issue_view(child_number)
            if str(child_issue["state"]) != CLOSED_STATE:
                return True  # Child is still open, parent should not be closed
        except (RuntimeError, json.JSONDecodeError):
            # If we can't fetch child state, assume incomplete to be safe
            return True

    return False


VERIFY_AFTER_MERGE_MARKER = f"{MARKER_PREFIX}-factory-verify-after-merge-v1"


def _hold_for_verification(
    pr_number: int, issue_number: int, issue: dict, labels: list[str], dry_run: bool
) -> str | None:
    """Keep a delivered issue open while its verify-after-merge criteria are unchecked.

    The issue gets the terminal done label so no factory selects it again, and
    one checklist comment per merged PR names what a person must confirm.
    """
    pending = acceptance_criteria.pending_after_merge(str(issue.get("body") or ""))
    if not pending:
        return None
    if dry_run:
        return "dry-run:verify-after-merge"
    github._replace_labels(issue_number, target_labels(labels))
    if str(issue["state"]) == CLOSED_STATE:
        github._run_gh("issue", "reopen", str(issue_number), "--repo", github._repo_slug())
    marker = f"<!-- {VERIFY_AFTER_MERGE_MARKER}:pr-{pr_number}:issue-{issue_number} -->"
    try:
        posted = any(marker in body for body in github._issue_comment_bodies(issue_number))
    except (RuntimeError, TypeError, ValueError):
        posted = False
    if not posted:
        checklist = "\n".join(f"- [ ] {name}: {text}" for name, text in pending)
        github._comment_issue(issue_number, (
            f"{marker}\nPR #{pr_number} merged. These criteria need verification after"
            " deploy, so this issue stays open. Once each one is confirmed, check its box"
            f" in the issue body if it has one, then close the issue.\n\n{checklist}"
        ))
    return "verify-after-merge"


def close_merged_pr_issue(
    pr_number: int,
    *,
    dry_run: bool = False,
    require_quiescent: bool = False,
    info: dict[str, str] | None = None,
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
        info: The PR view when the caller already read it.

    Returns:
        One of ``closed``, ``already-closed``, ``no-linked-issue``,
        ``protected-issue``, ``successor-open``, ``active-work``,
        ``manual-only-issue``, ``reopened-by-person``, ``acceptance-parent-incomplete``,
        ``acceptance-parent-requires-verdict``, ``not-merged``,
        ``not-delivered:<reason>``, ``delivery-unverified:<reason>``,
        ``open-dependency`` or ``open-dependency: #N``, ``verify-after-merge``,
        or ``dry-run:<action>``.
    """
    info = info or github._pr_info(pr_number)
    issue_number = linked_issue(info["branch"], info["body"])
    if issue_number is None:
        return "no-linked-issue"
    if issue_number in PROTECTED_ISSUES:
        return "protected-issue"

    if info["state"] != "MERGED":
        return "not-merged"
    issue = github._issue_view(issue_number)

    labels = list(issue["labels"])
    issue_body = str(issue.get("body") or "")
    if MANUAL_ONLY_MARKER in issue_body:
        # Issue #1620: an issue that declares itself human/interactive
        # controlled — including an operator production-acceptance or cutover
        # parent — is never completed by merge automation.
        return "manual-only-issue"
    if str(issue["state"]) != CLOSED_STATE:
        # A person who reopened the issue says the merged PR did not finish it.
        reopened = github._reopened_by_person(issue_number)
        if reopened:
            return "reopened-by-person" if reopened is True else "reopen-lookup-failed"
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
        and github._closed_by_pr(issue_number, info)
    )
    has_block_marker = github._has_dependency_block_marker(issue_number, pr_number)

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
            has_successor = github._issue_has_open_successor_pr(issue_number, pr_number)
            if has_successor == "unknown":
                # Lookup failure prevents closure verification - stop closure.
                return "successor-lookup-failed"
            if has_successor:
                return "successor-open"
        try:
            if implementation_delivery.is_implementation_issue(issue_body):
                paths, patch = implementation_delivery.complete_pr_diff(
                    pr_number, info.get("head", ""), github._repo_slug(), lambda args: github._run_gh(*args)
                )
                delivered, reason = implementation_delivery.pr_delivers_implementation(
                    issue_body, paths, patch
                )
            else:
                delivered, reason = True, "explicit non-implementation contract"
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            return f"delivery-unverified:{exc}"
        if delivered:
            open_deps, dependency_state_known = github._declared_dependency_blockers(
                issue_number, issue_body
            )
            if not dependency_state_known:
                if not dry_run:
                    pending = [label for label in labels
                               if label not in STAGE_LABELS and not OWNER_RE.fullmatch(label)
                               and not label.startswith("ralph-status:")]
                    pending.extend(["ralph-status:pending", "factory:unowned"])
                    github._replace_labels(issue_number, pending)
                    if str(issue["state"]) == CLOSED_STATE:
                        github._run_gh("issue", "reopen", str(issue_number), "--repo", github._repo_slug())
                    # The warning carries a stable marker so a dependency API
                    # outage cannot notify followers on every drain tick (G4).
                    if not github._has_unreadable_dependency_warning(issue_number, pr_number):
                        github._comment_issue(
                            issue_number,
                            "\n\n".join(
                                [
                                    github._unreadable_dependency_warning_marker(
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
                    github._replace_labels(issue_number, blocked)
                    if str(issue["state"]) == CLOSED_STATE:
                        github._run_gh("issue", "reopen", str(issue_number), "--repo", github._repo_slug())
                    github._record_dependency_block(pr_number, issue_number, open_deps)
                return "open-dependency: " + ", ".join(f"#{number}" for number in open_deps)
            held = _hold_for_verification(pr_number, issue_number, issue, labels, dry_run)
            if held:
                return held
            # Dependencies resolved: restore the canonical delivery labels and
            # close. The closure marker only deduplicates the comment; label
            # repair happens on every visit so a re-completed issue never
            # keeps stale factory:blocked / ralph-status:blocked labels (G3).
            if not dry_run:
                github._replace_labels(issue_number, target_labels(labels))
                if not github._has_closure_marker(issue_number, pr_number):
                    github._comment_issue(issue_number, closure_comment(pr_number, issue_number))
                if str(issue["state"]) != CLOSED_STATE:
                    github._close_issue(issue_number)
            return "closed"
        # Not delivered: repair keyword auto-closure of this PR's closed issue.
        if issue_closed_by_this_pr and not dry_run:
            pending = [label for label in labels
                       if label not in STAGE_LABELS and not OWNER_RE.fullmatch(label)
                       and not label.startswith("ralph-status:")]
            pending.extend(["ralph-status:pending", "factory:unowned"])
            github._replace_labels(issue_number, pending)
            github._run_gh("issue", "reopen", str(issue_number), "--repo", github._repo_slug())
            github._comment_issue(
                issue_number, f"PR #{pr_number} did not deliver implementation: {reason}"
            )
        return f"not-delivered:{reason}"

    if closure_blocked_by_active_work(labels, require_quiescent):
        return "active-work"
    has_successor = github._issue_has_open_successor_pr(issue_number, pr_number)
    if has_successor == "unknown":
        # Lookup failure prevents closure verification - stop closure
        return "successor-lookup-failed"
    if has_successor:
        return "successor-open"

    try:
        if implementation_delivery.is_implementation_issue(issue_body):
            paths, patch = implementation_delivery.complete_pr_diff(
                pr_number, info.get("head", ""), github._repo_slug(), lambda args: github._run_gh(*args)
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
    open_deps, dependency_state_known = github._declared_dependency_blockers(
        issue_number, issue_body
    )
    if not dependency_state_known:
        return "open-dependency"
    if open_deps:
        if not dry_run:
            github._record_dependency_block(pr_number, issue_number, open_deps)
        return "open-dependency: " + ", ".join(f"#{number}" for number in open_deps)
    if str(issue["state"]) != "OPEN" and not (
        delivered and str(issue["state"]) == CLOSED_STATE and github._closed_by_pr(issue_number, info)
    ):
        return "already-closed"
    held = _hold_for_verification(pr_number, issue_number, issue, labels, dry_run)
    if held:
        return held
    if dry_run:
        return f"dry-run:replace-labels-with{target_labels(labels)}+close"
    # The marker only deduplicates the closure comment; label repair runs on
    # every canonical closure so already-closed issues never keep stale
    # blocked or owner labels (G3).
    github._replace_labels(issue_number, target_labels(labels))
    if not github._has_closure_marker(issue_number, pr_number):
        github._comment_issue(issue_number, closure_comment(pr_number, issue_number))
    if str(issue["state"]) != CLOSED_STATE:
        github._close_issue(issue_number)
    return "closed"


def decide(
    pr_number: int, *, dry_run: bool = False, require_quiescent: bool = False
) -> tuple[str, str]:
    """Run closure for one merged PR and explain the decision.

    Args:
        pr_number: The merged PR number.
        dry_run: When True, report the intended action without mutating GitHub.
        require_quiescent: Passed through to ``close_merged_pr_issue``.

    Returns:
        The closure outcome and one plain line naming the issue and the reason.
    """
    info = github._pr_info(pr_number)
    outcome = close_merged_pr_issue(
        pr_number, dry_run=dry_run, require_quiescent=require_quiescent, info=info
    )
    if memory and outcome == "closed" and (number := linked_issue(info["branch"], info["body"])):
        memory.remember_closure(github._repo_slug(), number, pr_number, info)
    link = linkage(info["branch"], info["body"], info.get("title", ""))
    return outcome, explain(pr_number, outcome, link)


def sweep_stranded_closures(*, dry_run: bool = False, max_age_hours: int = SWEEP_MAX_AGE_HOURS) -> list[str]:
    """Heal issues stranded open by merges that predate direct closure.

    Args:
        dry_run: When True, report intended actions without mutating GitHub.
        max_age_hours: Only consider PRs merged within this many hours.

    Returns:
        One outcome line per examined merged factory PR.
    """
    raw = github._run_gh(
        "pr",
        "list",
        "--repo",
        github._repo_slug(),
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
        _outcome, line = decide(pr_number, dry_run=dry_run, require_quiescent=True)
        outcomes.append(line)
    return outcomes


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
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite(
            loader.loadTestsFromName(name) for name in SELF_TEST_MODULES
        )
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1

    if args.sweep:
        outcomes = sweep_stranded_closures(dry_run=args.dry_run)
        for line in outcomes:
            print(line)
        return 0

    if args.pr is None:
        parser.error("--pr or --sweep is required unless --self-test is used")
    outcome, line = decide(args.pr, dry_run=args.dry_run)
    print(line, file=sys.stderr)
    print(outcome)
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
