"""GitHub CLI adapters for post-merge closure: reads, mutations, and markers."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping

from factory_post_merge_closure_policy import (
    CLOSED_STATE,
    MERGE_CLOSURE_MARKER,
    UNREADABLE_WARNING_MARKER,
    eligibility,
    linked_issue,
)
from factory_adopter import MARKER_PREFIX


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


def _reopened_by_person(issue_number: int) -> bool | str:
    """Whether a person (not a bot) made the issue's latest reopen; "unknown" on failure."""
    owner, name = _repo_slug().split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
        "{issue(number:$number){timelineItems(last:1,itemTypes:[REOPENED_EVENT])"
        "{nodes{... on ReopenedEvent{actor{__typename login}}}}}}}"
    )
    try:
        raw = _run_gh(
            "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={issue_number}",
        )
        nodes = json.loads(raw)["data"]["repository"]["issue"]["timelineItems"]["nodes"]
    except (RuntimeError, ValueError, KeyError, TypeError):
        return "unknown"
    if not nodes:
        return False
    actor = nodes[-1].get("actor") or {}
    return actor.get("__typename") == "User"


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
            f"{MARKER_PREFIX}-factory-dependency-block-v1:pr-{pr_number}:"
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
        f"<!-- {MARKER_PREFIX}-factory-dependency-block-v1:pr-{pr_number}:"
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
