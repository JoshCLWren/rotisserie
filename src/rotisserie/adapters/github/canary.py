"""Guarded, exact-head merge adapter for the Rotisserie self-dogfood canary."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class CanaryMergeError(RuntimeError):
    """The canary merge was refused."""


@dataclass(frozen=True)
class CanaryScope:
    repository: str
    issue: int
    branch: str
    base_branch: str = "main"
    required_checks: frozenset[str] = frozenset({"Python 3.12", "Python 3.13", "Python 3.14"})
    producer: str = "codex-implementation"


CommandRunner = Callable[[Sequence[str]], str]


def merge_canary(
    *,
    scope: CanaryScope,
    repository: str,
    pull_request: int,
    expected_head: str,
    enabled: bool,
    run: CommandRunner,
) -> None:
    """Revalidate every gate and merge one allowlisted pull request."""

    if not enabled:
        raise CanaryMergeError("canary kill switch is disabled")
    if repository != scope.repository:
        raise CanaryMergeError("repository is outside the canary scope")
    if pull_request <= 0 or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise CanaryMergeError("pull request or expected head is invalid")

    pull = _object(
        run(
            (
                "gh",
                "api",
                f"repos/{repository}/pulls/{pull_request}",
            )
        )
    )
    head = _path(pull, "head", "sha")
    if (
        pull.get("state") != "open"
        or pull.get("draft") is not False
        or _path(pull, "base", "repo", "full_name") != scope.repository
        or _path(pull, "base", "ref") != scope.base_branch
        or _path(pull, "head", "repo", "full_name") != scope.repository
        or _path(pull, "head", "ref") != scope.branch
        or head != expected_head
    ):
        raise CanaryMergeError("pull request identity or exact head is outside the canary scope")

    body = pull.get("body")
    if not isinstance(body, str) or not re.search(
        rf"(?im)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#{scope.issue}\b", body
    ):
        raise CanaryMergeError("pull request does not close the allowlisted canary issue")

    comments = _pages(
        run(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/issues/{pull_request}/comments",
            )
        )
    )
    if not _has_independent_review_marker(
        comments,
        pull_request=pull_request,
        expected_head=expected_head,
        producer=scope.producer,
    ):
        raise CanaryMergeError("exact head lacks independent semantic review evidence")
    reviews = _array(run(("gh", "api", f"repos/{repository}/pulls/{pull_request}/reviews")))
    if any(
        review.get("state") == "CHANGES_REQUESTED" and review.get("commit_id") == expected_head
        for review in reviews
    ):
        raise CanaryMergeError("exact head has a blocking review")

    checks = _array(
        run(
            (
                "gh",
                "pr",
                "checks",
                str(pull_request),
                "--repo",
                repository,
                "--required",
                "--json",
                "name,state",
            )
        )
    )
    states = {str(check.get("name")): check.get("state") for check in checks}
    if set(states) != scope.required_checks:
        raise CanaryMergeError("exact head lacks the complete required CI set")
    if any(state != "SUCCESS" for state in states.values()):
        raise CanaryMergeError("exact head has unsuccessful required CI")

    refreshed = _object(run(("gh", "api", f"repos/{repository}/pulls/{pull_request}")))
    if _path(refreshed, "head", "sha") != expected_head:
        raise CanaryMergeError("pull request head changed after gate evaluation")

    run(
        (
            "gh",
            "pr",
            "merge",
            str(pull_request),
            "--repo",
            repository,
            "--merge",
            "--match-head-commit",
            expected_head,
            "--delete-branch",
        )
    )


def subprocess_runner(command: Sequence[str]) -> str:
    """Run a fixed-argument GitHub CLI command without shell interpolation."""

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise CanaryMergeError("GitHub evidence command failed closed") from exc
    return result.stdout


def _object(raw: str) -> Mapping[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise CanaryMergeError("GitHub returned a malformed object")
    return value


def _array(raw: str) -> list[Mapping[str, Any]]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CanaryMergeError("GitHub returned a malformed list")
    return value


def _pages(raw: str) -> list[Mapping[str, Any]]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(page, list) for page in value):
        raise CanaryMergeError("GitHub returned malformed paginated data")
    flattened = [item for page in value for item in page]
    if not all(isinstance(item, dict) for item in flattened):
        raise CanaryMergeError("GitHub returned malformed paginated data")
    return flattened


def _path(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


_REVIEW_MARKER = re.compile(
    r"^<!-- rotisserie-semantic-review-v1:"
    r"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{40}):"
    r"reviewer-(?P<reviewer>[a-z0-9._-]+):producer-(?P<producer>[a-z0-9._-]+):"
    r"verdict-(?P<verdict>approve|repair|reject) -->$"
)


def _has_independent_review_marker(
    comments: Sequence[Mapping[str, Any]],
    *,
    pull_request: int,
    expected_head: str,
    producer: str,
) -> bool:
    """Accept controller-persisted semantic evidence from a distinct worker identity."""

    for comment in reversed(comments):
        if comment.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}:
            continue
        body = comment.get("body")
        first_line = body.splitlines()[0] if isinstance(body, str) and body else ""
        match = _REVIEW_MARKER.fullmatch(first_line.strip())
        if not match:
            continue
        marker = match.groupdict()
        if int(marker["pr"]) != pull_request or marker["head"] != expected_head:
            continue
        if marker["producer"] != producer or marker["reviewer"] == producer:
            continue
        return marker["verdict"] == "approve"
    return False
