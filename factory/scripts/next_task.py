#!/usr/bin/env python3
"""Select the next executable GitHub issue for an agent."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

# Shared eligibility semantics live in scripts/factory_eligibility.py. Resolve
# them relative to this file so the selector works both as a direct script
# (python scripts/next_task.py) and as an imported package module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
eligibility = importlib.import_module("factory_eligibility")


class IssueLabel(TypedDict):
    """GitHub issue label payload."""

    name: str


class IssuePayload(TypedDict):
    """Subset of GitHub issue data used by the selector."""

    number: int
    title: str
    body: str
    labels: list[IssueLabel]
    url: str


class ExplainReport(TypedDict):
    """Ralph-queue eligibility report for one issue."""

    issue: int
    selector: str
    eligible: bool
    reasons: list[str]
    labels: list[str]
    not_evaluated: list[str]


@dataclass(frozen=True)
class Candidate:
    """An eligible issue and its selection metadata."""

    issue: IssuePayload
    priority: int


PRIORITY_RANKS = {
    "ralph-priority:critical": 0,
    "ralph-priority:high": 1,
    "ralph-priority:medium": 2,
    "ralph-priority:low": 3,
}
EXCLUDED_LABELS = {
    "duplicate",
    "ralph-status:blocked",
    "ralph-status:done",
    "ralph-status:in-progress",
    "ralph-status:in-review",
}

EPIC_ACCEPTANCE_LABELS = {"epic", "prd"}


def _labels(issue: IssuePayload) -> set[str]:
    """Return the issue's label names."""
    return {label["name"] for label in issue["labels"]}


def _priority(issue: IssuePayload) -> int:
    """Return the explicit priority rank, placing unprioritized issues last."""
    return min((PRIORITY_RANKS.get(label, 99) for label in _labels(issue)), default=99)


def _dependency_numbers(body: str) -> set[int]:
    """Return issue numbers referenced as dependencies in an issue body."""
    return eligibility.parse_declared_dependencies(body)


def _has_unresolved_dependency(issue: IssuePayload, closed_numbers: set[int]) -> bool:
    """Return whether a referenced issue number is not known to be closed."""
    body = issue.get("body") or ""
    references = _dependency_numbers(body)
    return bool(references - closed_numbers)


def _is_manual_only(issue: IssuePayload) -> bool:
    """Return whether autonomous execution is explicitly disallowed."""
    return eligibility.is_manual_only(issue.get("body"))


def _is_acceptance_parent(issue: IssuePayload) -> bool:
    """Return whether the issue declares a product-acceptance parent contract.

    Covers the #1615 incident shape: a parent whose acceptance contract lives
    in the body even when it carries no epic/prd label. Both halves are
    required — the body must present itself as the acceptance parent and
    declare a checkbox child graph — so ordinary implementation issues that
    merely defer an operator acceptance pass (#3037, #2718, #2128) stay
    executable.
    """
    return eligibility.is_acceptance_parent(issue.get("body"))


def select_next(issues: list[IssuePayload], closed_numbers: set[int]) -> Candidate | None:
    """Select the highest-priority executable pending issue."""
    cutover_open = any(issue["number"] == eligibility.RUST_CUTOVER_EPIC for issue in issues)
    candidates: list[Candidate] = []
    for issue in issues:
        labels = _labels(issue)
        if not eligibility.rust_cutover_work_allowed(
            labels, issue.get("body"), cutover_open=cutover_open
        ):
            continue
        if "ralph-status:pending" not in labels or labels & EXCLUDED_LABELS:
            continue
        if labels & EPIC_ACCEPTANCE_LABELS or _is_manual_only(issue):
            continue
        if _is_acceptance_parent(issue):
            continue
        if _has_unresolved_dependency(issue, closed_numbers):
            continue
        candidates.append(Candidate(issue=issue, priority=_priority(issue)))

    return min(candidates, key=lambda item: (item.priority, item.issue["number"])) if candidates else None


def _gh_issue_list(state: str) -> list[IssuePayload]:
    """Load issues from GitHub using the gh CLI."""
    command = [
        "gh",
        "issue",
        "list",
        "--state",
        state,
        "--limit",
        "1000",
        "--json",
        "number,title,body,labels,url",
    ]
    payload = _run_gh_json(command, "GitHub issue query failed")
    if not isinstance(payload, list):
        raise RuntimeError("GitHub issue query failed: unexpected response")
    return cast(list[IssuePayload], payload)


def _run_gh(command: list[str], failure_message: str) -> str:
    """Run a GitHub CLI command and return its standard output."""
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError("gh CLI is required; install it and authenticate first") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or failure_message
        raise RuntimeError(detail) from error
    return result.stdout


def _run_gh_json(command: list[str], failure_message: str) -> object:
    """Run a GitHub CLI command and decode its JSON output."""
    try:
        payload = json.loads(_run_gh(command, failure_message))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{failure_message}: invalid JSON response") from error
    return payload


def _issue_context(issue: IssuePayload, closed_numbers: set[int]) -> str:
    """Render the bounded context an agent needs before starting an issue."""
    body = issue.get("body") or "No issue body was provided."
    dependencies = sorted(_dependency_numbers(body))
    required_files = sorted(
        {
            reference
            for reference in re.findall(r"`([^`]+)`", body)
            if "/" in reference
            or reference.endswith((".md", ".py", ".js", ".jsx", ".ts", ".tsx"))
        }
    )
    dependency_text = "none"
    if dependencies:
        dependency_text = ", ".join(
            f"#{number} ({'closed' if number in closed_numbers else 'open'})"
            for number in dependencies
        )
    files_text = ", ".join(required_files) if required_files else "none explicitly named"

    lines = [
        "Scope:",
        body.strip(),
        f"Dependencies: {dependency_text}",
        f"Required files named by issue: {files_text}",
        "Required verification: follow AGENTS.md and the issue acceptance criteria.",
    ]

    return "\n".join(lines)


def _gh_issue(issue_number: int) -> IssuePayload:
    """Load one issue from GitHub."""
    command = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--json",
        "number,title,body,labels,url",
    ]
    payload = _run_gh_json(command, "GitHub issue query failed")
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub issue query failed: unexpected response")
    return cast(IssuePayload, payload)


def _start_task(issue_number: int) -> int:
    """Validate an issue and move it from pending to in-progress."""
    issue = _gh_issue(issue_number)
    labels = _labels(issue)
    if "ralph-status:pending" not in labels:
        raise RuntimeError(f"#{issue_number} is not pending; no status change made")
    if "ralph-task" not in labels:
        raise RuntimeError(f"#{issue_number} is not an executable ralph-task")
    if labels & EPIC_ACCEPTANCE_LABELS:
        raise RuntimeError(f"#{issue_number} is a parent PRD/epic; autonomous start is not allowed")
    if _is_manual_only(issue):
        raise RuntimeError(f"#{issue_number} is marked manual-only; autonomous start is not allowed")
    if _is_acceptance_parent(issue):
        raise RuntimeError(
            f"#{issue_number} declares a product-acceptance parent contract; "
            "autonomous start is not allowed"
        )

    closed_numbers = {
        closed_issue["number"] for closed_issue in _gh_issue_list("closed")
    }
    if _has_unresolved_dependency(issue, closed_numbers):
        raise RuntimeError(f"#{issue_number} has an unresolved dependency; no status change made")
    cutover_open = any(
        row["number"] == eligibility.RUST_CUTOVER_EPIC
        for row in _gh_issue_list("open")
    )
    if not eligibility.rust_cutover_work_allowed(
        labels, issue.get("body"), cutover_open=cutover_open
    ):
        raise RuntimeError(f"#{issue_number} must wait for Rust cutover #4335")

    _run_gh(
        [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--remove-label",
            "ralph-status:pending",
            "--add-label",
            "ralph-status:in-progress",
        ],
        "GitHub issue status update failed",
    )
    try:
        _run_gh(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--body",
                "Starting implementation from the repository issue workflow.",
            ],
            "GitHub issue comment failed",
        )
    except RuntimeError as error:
        print(f"start-task: label updated, but comment failed: {error}", file=sys.stderr)
    print(f"Started #{issue_number}: {issue['title']}")
    return 0


def explain_issue(
    issue: IssuePayload,
    *,
    open_numbers: set[int],
) -> ExplainReport:
    """Explain whether one issue is currently visible to the Ralph queue.

    Uses the same shared eligibility module as the fixed-model controller, with
    ``require_ralph_labels`` enabled because this selector serves the Ralph
    queue. This is not an authorization for fixed-model dispatch: that
    controller also evaluates canonical PRs, Rotisserie intake, retry history,
    and worker-specific gates that this selector does not query. The report
    explicitly names those omitted checks.

    Args:
        issue: The GitHub issue payload to explain.
        open_numbers: Numbers of currently open issues.

    Returns:
        A JSON-serializable verdict with human-readable reasons.
    """
    number = int(issue["number"])
    verdict = eligibility.explain_issue_eligibility(
        number=number,
        state="OPEN",
        labels=_labels(issue),
        body=issue.get("body"),
        open_numbers=open_numbers,
        require_ralph_labels=True,
    )
    cutover_allowed = eligibility.rust_cutover_work_allowed(
        _labels(issue), issue.get("body"),
        cutover_open=eligibility.RUST_CUTOVER_EPIC in open_numbers,
    )
    return {
        "issue": number,
        "selector": "next_task",
        "eligible": verdict.eligible and cutover_allowed,
        "reasons": list(verdict.reasons) + (
            [] if cutover_allowed else ["Rust cutover #4335 is still open"]
        ),
        "labels": sorted(_labels(issue)),
        "not_evaluated": [
            "canonical open-PR suppression (controller-only gate)",
            "Rotisserie issue-intake authorization (fixed-model only)",
            "rolling no-diff retry budget (fixed-model only)",
            "worker WIP, review-backlog, quota, and capacity (fixed-model dispatch gates)",
            "live GitHub dependency blockers (controller-only gate)",
            "lease liveness and worker-specific strike-retry exclusion (assignment-time gates)",
        ],
    }


def _explain_task(issue_number: int) -> int:
    """Print the Ralph-queue eligibility report for one issue."""
    issue = _gh_issue(issue_number)
    open_numbers = {row["number"] for row in _gh_issue_list("open")}
    report = explain_issue(issue, open_numbers=open_numbers)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main() -> int:
    """Select the next issue, start one, or explain its eligibility."""
    parser = ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    start_parser = subparsers.add_parser("start", help="validate and start an issue")
    start_parser.add_argument("issue", type=int)
    explain_parser = subparsers.add_parser(
        "explain", help="explain why an issue is or is not queue-eligible"
    )
    explain_parser.add_argument("issue", type=int)
    args = parser.parse_args()

    if args.command == "start":
        try:
            return _start_task(args.issue)
        except RuntimeError as error:
            print(f"start-task: {error}", file=sys.stderr)
            return 1

    if args.command == "explain":
        try:
            return _explain_task(args.issue)
        except RuntimeError as error:
            print(f"explain: {error}", file=sys.stderr)
            return 1

    try:
        open_issues = _gh_issue_list("open")
        closed_issues = _gh_issue_list("closed")
    except (RuntimeError, json.JSONDecodeError) as error:
        print(f"next-task: {error}", file=sys.stderr)
        return 1

    closed_numbers = {issue["number"] for issue in closed_issues}
    candidate = select_next(open_issues, closed_numbers)
    if candidate is None:
        print("No eligible pending GitHub issue found.")
        return 0

    issue = candidate.issue
    labels = ", ".join(sorted(_labels(issue)))
    print(f"Next issue: #{issue['number']} — {issue['title']}")
    print(f"URL: {issue['url']}")
    print(f"Priority rank: {candidate.priority}")
    print(f"Labels: {labels}")
    print()
    print(_issue_context(issue, closed_numbers))
    print("Agent context: read AGENTS.md and docs/ISSUE_EXECUTION_PROTOCOL.md.")
    print("If the issue has a linked local plan, read that plan before editing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
