#!/usr/bin/env python3
"""Decide whether a newly opened factory PR has durable producer provenance."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BRANCH_RE = re.compile(r"^factory/(?P<worker>[0-9]+)-(?P<issue>[0-9]+)-")
PROVENANCE_RE = re.compile(
    r"<!--\s*comic-pile-factory-pr-provenance-v1:"
    r"pr-(?P<pr>[0-9]+):issue-(?P<issue>[0-9]+):"
    r"(?P<worker_id>opencode-(?:free-model|nvidia|omniroute)-factory-(?P<worker>[0-9]+)):"
    r"(?P<epoch>[0-9]{10}):(?P<head>[0-9a-f]{40})\s*-->"
)
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
TRUSTED_BOT_LOGIN = "github-actions[bot]"


@dataclass(frozen=True)
class Decision:
    """Fence result for one pull request event."""

    allow: bool
    reason: str
    worker: str = ""
    issue: str = ""


def flatten_comments(payload: Any) -> list[dict[str, Any]]:
    """Flatten REST pagination or connector-style comment payloads."""
    if isinstance(payload, dict):
        payload = payload.get("comments", [])
    if not isinstance(payload, list):
        return []

    comments: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            comments.append(item)
        elif isinstance(item, list):
            comments.extend(entry for entry in item if isinstance(entry, dict))
    return comments


def comment_is_trusted(comment: dict[str, Any]) -> bool:
    """Accept workflow-bot comments or comments authored by trusted collaborators."""
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    association = str(comment.get("author_association") or "").upper()
    return login == TRUSTED_BOT_LOGIN or association in TRUSTED_ASSOCIATIONS


def decide(
    *,
    head_ref: str,
    pr_number: int,
    head_sha: str,
    comments: Any,
    provenance_available: bool = True,
) -> Decision:
    """Require an exact trusted persistence marker for canonical factory branches."""
    if not head_ref.startswith("factory/"):
        return Decision(True, "not-a-factory-branch")

    branch_match = BRANCH_RE.match(head_ref)
    if not branch_match:
        return Decision(True, "noncanonical-factory-branch")

    worker = branch_match.group("worker")
    issue = branch_match.group("issue")
    if not provenance_available:
        return Decision(False, "provenance-unavailable", worker, issue)

    for comment in flatten_comments(comments):
        if not comment_is_trusted(comment):
            continue
        body = str(comment.get("body") or "")
        for marker in PROVENANCE_RE.finditer(body):
            if (
                marker.group("pr") == str(pr_number)
                and marker.group("issue") == issue
                and marker.group("worker") == worker
                and marker.group("head") == head_sha.lower()
            ):
                return Decision(True, "matching-pr-provenance", worker, issue)

    return Decision(False, "no-matching-pr-provenance", worker, issue)


class FenceTests(unittest.TestCase):
    """Network-free regression tests for the revocation fence."""

    HEAD = "a" * 40
    BRANCH = "factory/39-1477-opencode-free"
    MARKER = (
        "<!-- comic-pile-factory-pr-provenance-v1:"
        f"pr-1517:issue-1477:opencode-free-model-factory-39:1787060686:{HEAD} -->"
    )

    @classmethod
    def trusted_comment(cls, body: str) -> dict[str, Any]:
        return {
            "body": body,
            "user": {"login": TRUSTED_BOT_LOGIN},
            "author_association": "NONE",
        }

    def test_normal_pr_opened_handoff_is_allowed(self) -> None:
        comments = [
            self.trusted_comment(self.MARKER),
            self.trusted_comment(
                "<!-- comic-pile-factory-claim-released-v3:"
                "issue-1477:opencode-free-model-factory-39:1787060687:pr-opened-handoff -->"
            ),
        ]
        result = decide(
            head_ref=self.BRANCH,
            pr_number=1517,
            head_sha=self.HEAD,
            comments=comments,
        )
        self.assertTrue(result.allow)

    def test_repair_handoff_to_different_worker_is_allowed(self) -> None:
        comments = [
            self.trusted_comment(self.MARKER),
            self.trusted_comment(
                "<!-- free-model-factory-owner:41 -->\n"
                "Factory 41 adopted this unowned PR for repair."
            ),
        ]
        result = decide(
            head_ref=self.BRANCH,
            pr_number=1517,
            head_sha=self.HEAD,
            comments=comments,
        )
        self.assertTrue(result.allow)

    def test_genuinely_revoked_assignment_is_closed(self) -> None:
        comments = [
            self.trusted_comment(
                "<!-- comic-pile-factory-implement-claim-v3:"
                "issue-1477:opencode-free-model-factory-39:1787060000:attempt-1 -->"
            ),
            self.trusted_comment(
                "<!-- comic-pile-factory-claim-released-v3:"
                "issue-1477:opencode-free-model-factory-39:1787060100:cancelled-worker -->"
            ),
        ]
        result = decide(
            head_ref=self.BRANCH,
            pr_number=1517,
            head_sha=self.HEAD,
            comments=comments,
        )
        self.assertFalse(result.allow)
        self.assertEqual(result.reason, "no-matching-pr-provenance")

    def test_unavailable_provenance_fails_closed(self) -> None:
        result = decide(
            head_ref=self.BRANCH,
            pr_number=1517,
            head_sha=self.HEAD,
            comments=[],
            provenance_available=False,
        )
        self.assertFalse(result.allow)
        self.assertEqual(result.reason, "provenance-unavailable")

    def test_wrong_head_does_not_authorize_replayed_marker(self) -> None:
        result = decide(
            head_ref=self.BRANCH,
            pr_number=1517,
            head_sha="b" * 40,
            comments=[self.trusted_comment(self.MARKER)],
        )
        self.assertFalse(result.allow)

    def test_untrusted_forged_marker_is_closed(self) -> None:
        result = decide(
            head_ref=self.BRANCH,
            pr_number=1517,
            head_sha=self.HEAD,
            comments=[
                {
                    "body": self.MARKER,
                    "user": {"login": "random-user"},
                    "author_association": "NONE",
                }
            ],
        )
        self.assertFalse(result.allow)


def main() -> int:
    """Run self-tests or classify one pull request event."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--head-ref")
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--head-sha")
    parser.add_argument("--comments-json", type=Path)
    parser.add_argument("--provenance-unavailable", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(FenceTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1

    if not args.head_ref or not args.pr_number or not args.head_sha:
        parser.error("--head-ref, --pr-number, and --head-sha are required")

    comments: Any = []
    provenance_available = not args.provenance_unavailable
    if provenance_available:
        if args.comments_json is None:
            parser.error("--comments-json is required unless provenance is unavailable")
        try:
            comments = json.loads(args.comments_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            provenance_available = False

    decision = decide(
        head_ref=args.head_ref,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        comments=comments,
        provenance_available=provenance_available,
    )
    print(json.dumps(asdict(decision), sort_keys=True))
    return 0 if decision.allow else 1


if __name__ == "__main__":
    sys.exit(main())
