#!/usr/bin/env python3
"""The trusted controller's line-anchored review step.

It reads the reviewer's findings record from the trusted log, validates it
against the exact head's diff, decides the verdict, posts the valid findings as
one PR review with inline comments anchored by path, line and side, verifies
each anchor, and resolves the factory's own threads that the new review
replaces. ``ctl`` is the loaded review controller, so GitHub calls share its
token, repository and redaction.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factory_review_findings as findings  # noqa: E402

LINE_VERDICTS = frozenset({findings.APPROVE, findings.REPAIR, findings.REJECT})
FAILURE_STATUSES = frozenset(
    {"review-findings-missing", "review-diff-unavailable", "review-findings-post-failed"}
)
THREADS = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){reviewThreads(first:100){pageInfo{hasNextPage} nodes{id "
    "isResolved comments(first:1){nodes{body author{login __typename}}}}}}}}"
)
RESOLVE = "mutation($thread:ID!){resolveReviewThread(input:{threadId:$thread}){thread{id}}}"


@dataclass(frozen=True)
class Outcome:
    """``verdict`` is approve, repair, reject or failed; ``status`` names a failure."""

    verdict: str
    review: findings.Review = field(default_factory=lambda: findings.Review(parsed=False))
    status: str = ""
    note: str = ""

    @property
    def handoff(self) -> str:
        return findings.handoff_text(self.review)


def head_files(ctl: Any, pr: int) -> list[dict[str, Any]]:
    pages = ctl.gh_json(
        ["api", "--paginate", "--slurp", f"repos/{ctl.REPO}/pulls/{pr}/files?per_page=100"]
    )
    return list(ctl.flatten_pages(pages))


def post(
    ctl: Any, *, pr: int, head: str, reviewer: str, verdict: str, review: findings.Review
) -> str:
    """Post the review; return a problem, or "" when every comment is anchored."""
    payload = findings.review_payload(
        pr=pr, head=head, reviewer=reviewer, verdict=verdict, review=review
    )
    payload["body"] = ctl.redact_review_text(payload["body"])
    for comment in payload["comments"]:
        comment["body"] = ctl.redact_review_text(comment["body"])
    created = ctl.json_object(
        ctl.gh_json(
            ["api", "--method", "POST", f"repos/{ctl.REPO}/pulls/{pr}/reviews", "--input", "-"],
            input_json=payload,
        )
    )
    review_id = created.get("id") if created else None
    if not review_id:
        return "GitHub did not return the posted review"
    if not payload["comments"]:
        return ""
    rows = ctl.flatten_pages(
        ctl.gh_json(
            ["api", "--paginate", "--slurp", f"repos/{ctl.REPO}/pulls/{pr}/comments?per_page=100"]
        )
    )
    mine = [row for row in rows if row.get("pull_request_review_id") == review_id]
    missing = findings.unanchored(mine, payload["comments"])
    return f"inline comments not anchored to their line: {', '.join(missing)}" if missing else ""


def superseded(ctl: Any, *, pr: int, head: str, verdict: str) -> list[str]:
    """Read before posting, so an unreadable thread list never leaves a duplicate review."""
    owner, name = ctl.REPO.split("/", 1)
    data = (
        ctl.json_object(
            ctl.gh_json(
                [
                    "api",
                    "graphql",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"number={pr}",
                    "-f",
                    f"query={THREADS}",
                ]
            )
        )
        or {}
    )
    threads = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get(
        "reviewThreads"
    ) or {}
    if (threads.get("pageInfo") or {}).get("hasNextPage"):
        raise RuntimeError("review threads were only partially read")
    return findings.superseded_threads(
        threads.get("nodes") or [], pr=pr, head=head, verdict=verdict
    )


def run(
    ctl: Any, *, pr: int, head: str, worker: str, verdict: str, review_log: str | None
) -> Outcome:
    """Decide and publish one review. The submitted verdict only says whether the
    model printed REJECT, or the wrapper downgraded an approval to repair."""
    text = ctl.trusted_review_log(review_log, worker=worker)
    record, error = findings.parse_record(text, head=head)
    if record is None:
        return Outcome(
            findings.FAILED,
            status="review-findings-missing",
            note=f"Review run failed: {error}. A review must print one {findings.PREFIX} "
            "record; the head returns to review.",
        )
    try:
        files = head_files(ctl, pr)
    except RuntimeError as exc:
        detail = ctl.redact_review_text(str(exc))[-300:]
        return Outcome(
            findings.FAILED,
            status="review-diff-unavailable",
            note=f"Head diff unreadable: {detail}",
        )
    review = findings.validate(record, error, files)
    decided = findings.decide(review, rejects=verdict == findings.REJECT)
    if verdict == findings.REPAIR:
        decided = findings.stricter(findings.REPAIR, decided)
    try:
        stale = superseded(ctl, pr=pr, head=head, verdict=decided)
        problem = post(ctl, pr=pr, head=head, reviewer=worker, verdict=decided, review=review)
        for thread in [] if problem else stale:
            ctl.gh_json(["api", "graphql", "-F", f"thread={thread}", "-f", f"query={RESOLVE}"])
    except RuntimeError as exc:
        problem = ctl.redact_review_text(str(exc))[-300:]
    if problem:
        return Outcome(
            findings.FAILED,
            review=review,
            status="review-findings-post-failed",
            note=f"Inline review could not be published: {problem}",
        )
    return Outcome(decided, review=review)
