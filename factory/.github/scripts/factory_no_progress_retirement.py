#!/usr/bin/env python3
"""Retire exact-head factory PRs that exhausted substantive no-progress repairs.

This is not a provider/CI failure circuit breaker. An attempted implementation
is archived as a closed PR, with its branch and SHA retained for the next
worker. The original issue remains open and becomes immediately executable.
Only the existing serialized dispatcher calls this controller.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

from factory_work_policy import (
    FACTORY_NO_DIFF_RETRY_LIMIT,
    BLOCKED_LABELS,
    comment_is_trusted,
    is_manual_only,
    labels_of,
    linked_issue_from_branch,
    matching_pr_no_diff_attempts,
    parse_no_diff_attempts_from_comments,
)

REPO = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RETIRE_MARKER = "comic-pile-factory-no-progress-retired-v1"
STAGES = {"factory:review", "factory:changes-requested"}
OWNER_RE = re.compile(r"^factory:(?:local|[1-9][0-9]*)$")


def gh(args: list[str], *, payload: object | None = None) -> str:
    command = ["gh", *args]
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"GitHub command failed: {command[:3]}: {result.stderr.strip()}")
    return result.stdout


def gh_json(args: list[str]) -> Any:
    return json.loads(gh(args))


def pages_to_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("GitHub pagination payload was not a list")
    if not all(isinstance(page, list) for page in value):
        raise ValueError("GitHub pagination contained a malformed page")
    if not all(isinstance(row, dict) for page in value for row in page):
        raise ValueError("GitHub pagination contained a malformed record")
    return [row for page in value for row in page]


def read_comments(number: int) -> list[dict[str, Any]]:
    return pages_to_rows(gh_json([
        "api", "--paginate", "--slurp",
        f"repos/{REPO}/issues/{number}/comments?per_page=100",
    ]))


def read_pr(number: int) -> dict[str, Any]:
    payload = gh_json(["api", f"repos/{REPO}/pulls/{number}"])
    if not isinstance(payload, dict):
        raise ValueError("PR response is not an object")
    return payload


def read_issue(number: int) -> dict[str, Any]:
    payload = gh_json(["api", f"repos/{REPO}/issues/{number}"])
    if not isinstance(payload, dict):
        raise ValueError("Issue response is not an object")
    return payload


def candidate_reason(
    pr: dict[str, Any],
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    *,
    now_epoch: int,
) -> str | None:
    """Require trustworthy three-strike evidence on the unchanged PR head."""
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = head.get("repo") or {}
    base_repo = base.get("repo") or {}
    sha = str(head.get("sha") or "").lower()
    branch = str(head.get("ref") or "")
    number = pr.get("number")
    linked = linked_issue_from_branch(branch)
    if (
        pr.get("state") != "open"
        or pr.get("draft") is not False
        or not isinstance(number, int)
        or linked is None
        or linked != issue.get("number")
        or issue.get("state") != "open"
        or not SHA_RE.fullmatch(sha)
        or head_repo.get("full_name") != REPO
        or base_repo.get("full_name") != REPO
        or base.get("ref") != "main"
        or is_manual_only(pr.get("body"))
        or is_manual_only(issue.get("body"))
    ):
        return None

    pr_labels = labels_of(pr)
    issue_labels = labels_of(issue)
    if (
        not {"factory", "factory:unowned"} <= pr_labels
        or not {"factory", "factory:unowned"} <= issue_labels
        or pr_labels & BLOCKED_LABELS
        or issue_labels & BLOCKED_LABELS
        or "ralph-status:done" in issue_labels
        or any(OWNER_RE.fullmatch(label) for label in pr_labels | issue_labels)
        or len(pr_labels & STAGES) != 1
        or "factory:ready" in pr_labels
        or "factory:ci" in pr_labels
        or "factory:blocked" in issue_labels
        or pr.get("mergeable") is not True
        or pr.get("mergeable_state") == "dirty"
    ):
        return None

    stage = next(iter(pr_labels & STAGES))
    attempts = [
        attempt
        for attempt in parse_no_diff_attempts_from_comments(
            comments, now_epoch=now_epoch
        )
        if attempt.kind == "pr"
        and attempt.reason == "repair-no-persisted-change-handoff"
    ]
    total = matching_pr_no_diff_attempts(
        attempts,
        pr_number=number,
        head_sha=sha,
        stage=stage,
        conflicted=False,
    )
    if total < FACTORY_NO_DIFF_RETRY_LIMIT:
        return None
    return f"{total} verified no-progress repairs at unchanged head {sha}"


def retirement_marker(issue: int, pr: int, sha: str, epoch: int) -> str:
    return (
        f"<!-- {RETIRE_MARKER}:issue-{issue}:pr-{pr}:"
        f"sha-{sha}:epoch-{epoch} -->"
    )


def has_retirement_marker(
    comments: list[dict[str, Any]], issue: int, pr: int, sha: str
) -> bool:
    prefix = f"<!-- {RETIRE_MARKER}:issue-{issue}:pr-{pr}:sha-{sha}:"
    return any(
        comment_is_trusted(comment)
        and prefix in str(comment.get("body") or "")
        for comment in comments
    )


def restore_issue_intake(issue: dict[str, Any]) -> None:
    """Preserve unrelated labels while dropping the obsolete PR stage."""
    number = int(issue["number"])
    current = labels_of(issue)
    if (
        issue.get("state") != "open"
        or "factory:unowned" not in current
        or any(OWNER_RE.fullmatch(label) for label in current)
    ):
        raise RuntimeError("Issue changed ownership before requeue")
    labels = {
        label
        for label in current
        if label not in {
            "factory:building",
            "factory:review",
            "factory:changes-requested",
            "factory:ci",
            "factory:ready",
            "ralph-status:in-progress",
            "ralph-status:in-review",
            "ralph-status:validation",
        }
    }
    labels.update({"factory", "factory:unowned", "ralph-status:pending"})
    gh(
        ["api", "--method", "PUT", f"repos/{REPO}/issues/{number}/labels",
         "--input", "-"],
        payload={"labels": sorted(labels)},
    )


def retire_pr(number: int, *, now_epoch: int) -> dict[str, Any]:
    """Close only after a fresh exact-head and issue-owner revalidation."""
    pr = read_pr(number)
    branch = str((pr.get("head") or {}).get("ref") or "")
    linked = linked_issue_from_branch(branch)
    if linked is None:
        return {"pr": number, "status": "not-canonical"}
    issue = read_issue(linked)
    comments = read_comments(number)
    reason = candidate_reason(pr, issue, comments, now_epoch=now_epoch)
    if reason is None:
        return {"pr": number, "status": "not-exhausted"}

    sha = str(pr["head"]["sha"]).lower()
    issue_comments = read_comments(linked)
    marker = retirement_marker(linked, number, sha, now_epoch)
    if not has_retirement_marker(issue_comments, linked, number, sha):
        note = (
            f"Factory no-progress retirement for #{number} at {sha}.\n\n"
            f"Evidence: {reason}. The PR attempt is being closed, not merged. "
            f"**Preserved implementation:** PR #{number}, branch "
            f"{branch}, exact head {sha}, with its commits, reviews and diff. "
            "The next worker must inspect and reuse applicable code/tests rather "
            "than assume this partial implementation is deployed. The original "
            "issue remains open for a clean implementation and acceptance.\n\n"
            f"{marker}"
        )
        gh(["issue", "comment", str(linked), "--repo", REPO, "--body", note])

    # The issue handoff is durable before any PR-close side effect. A new lease,
    # new commit, or change to issue ownership aborts the retirement.
    pr = read_pr(number)
    issue = read_issue(linked)
    comments = read_comments(number)
    if (
        str((pr.get("head") or {}).get("sha") or "").lower() != sha
        or candidate_reason(pr, issue, comments, now_epoch=now_epoch) is None
    ):
        return {"pr": number, "status": "changed-during-retirement"}

    # Reset issue-level cooldown before closing the canonical PR. While this
    # PR remains open it still suppresses duplicate intake; after closure the
    # reset marker makes the original issue immediately selectable.
    restore_issue_intake(issue)
    latest = read_pr(number)
    if (
        latest.get("state") != "open"
        or str((latest.get("head") or {}).get("sha") or "").lower() != sha
        or "factory:unowned" not in labels_of(latest)
        or any(OWNER_RE.fullmatch(label) for label in labels_of(latest))
    ):
        return {"pr": number, "status": "changed-before-close"}

    final_issue = read_issue(linked)
    if (
        final_issue.get("state") != "open"
        or "factory:unowned" not in labels_of(final_issue)
        or any(OWNER_RE.fullmatch(label) for label in labels_of(final_issue))
    ):
        return {"pr": number, "status": "issue-changed-before-close"}

    gh([
        "pr", "close", str(number), "--repo", REPO, "--comment",
        f"Closed after {reason}. Original issue #{linked} was requeued. "
        f"Do not delete branch {branch}; preserve this exact head for reuse: {sha}.",
    ])
    return {"pr": number, "issue": linked, "status": "retired", "head": sha}


def main() -> int:
    now_epoch = int(time.time())
    from factory_discovery import discovered_pull_requests

    rows = discovered_pull_requests(REPO, lambda: pages_to_rows(gh_json([
        "api", "--paginate", "--slurp",
        f"repos/{REPO}/pulls?state=open&per_page=100",
    ])))
    results = []
    for row in rows:
        labels = labels_of(row)
        if not (
            {"factory", "factory:unowned"} <= labels
            and labels & STAGES
            and str((row.get("head") or {}).get("ref") or "").startswith("factory/")
        ):
            continue
        number = row.get("number")
        if not isinstance(number, int):
            continue
        # GitHub outages fail closed for this candidate, not for other work.
        try:
            result = retire_pr(number, now_epoch=now_epoch)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"[factory-retirement] PR #{number}: {error}", flush=True)
            continue
        if result["status"] == "retired":
            print(f"[factory-retirement] {json.dumps(result)}", flush=True)
        results.append(result)
    print(json.dumps({"retired": [r for r in results if r["status"] == "retired"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
