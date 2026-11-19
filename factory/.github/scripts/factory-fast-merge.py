#!/usr/bin/env python3
"""Fast-merge inert reference-only PRs after exact-head Python lint and types succeed.

Only trusted main is executed, the path/ownership allowlist fails closed, and
GitHub receives the exact verified head SHA. GitHub branch protection still
controls whether the merge is permitted for the selected token.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
OWNER_RE = re.compile(r"^factory:(?:local|[1-9][0-9]*)$")
# An executable script, CI test, workflow, policy/prompt, or gate input is not
# inert. This is deliberately a tiny explicit allowlist: only reference-only
# documents verified to be unreferenced by workers and privileged workflows.
# Tests scan code, workflow, prompt and agent paths for new consumers.
FACTORY_PATHS = frozenset({
    "docs/FACTORY_NEON_STATE.md",
    "docs/FACTORY_RESOURCE_MEASUREMENTS.md",
})
PROTECTED = frozenset(
    {
        ".github/factory-fast-merge.json",
        ".github/workflows/factory-fast-merge.yml",
        ".github/scripts/factory-fast-merge.py",
        "tests/test_factory_fast_merge.py",
    }
)
BLOCKED_LABELS = frozenset(
    {"factory:blocked", "needs-human", "do-not-merge", "security-review"}
)
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# These job names come from .github/workflows/ci.yml, not a provider roster.
# CI Summary prevents an owner-bypass token from skipping other required jobs.
REQUIRED_FAST_CHECKS = frozenset({
    "CI Summary",
    "Python Lint (ruff)",
    "Python Type Check (ty)",
    "Factory Tests (complete)",
    "Factory Workflow Syntax (actionlint)",
    "Factory Shell Scripts (shellcheck)",
})


def factory_path(path: str) -> bool:
    """Recognize only allowlisted factory paths, excluding the policy itself."""
    if (
        not isinstance(path, str)
        or path in PROTECTED
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        return False
    return path in FACTORY_PATHS


def factory_candidate(
    pr: dict[str, Any], files: list[dict[str, Any]], repo: str,
) -> tuple[bool, str]:
    """Fail closed for product, fork, draft, ownership, or untrusted changes."""
    if pr.get("state") != "open" or pr.get("draft") is not False:
        return False, "PR is not open and ready"
    if pr.get("mergeable") is not True:
        return False, "GitHub has not confirmed mergeability"
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    if base.get("ref") != "main" or base.get("repo", {}).get("full_name") != repo:
        return False, "PR base differs from this repository's main"
    if head.get("repo", {}).get("full_name") != repo:
        return False, "Fork heads are ineligible"
    if not HEAD_RE.fullmatch(str(head.get("sha", ""))):
        return False, "PR head SHA is invalid"
    user = pr.get("user") or {}
    author = user.get("login")
    if author in {"dependabot[bot]", "renovate[bot]"}:
        return False, "Dependency bots are excluded"
    if (
        pr.get("author_association") not in TRUSTED_ASSOCIATIONS
        and author != "github-actions[bot]"
    ):
        return False, "PR author is not a trusted repository collaborator"
    labels = {
        label.get("name")
        for label in pr.get("labels", [])
        if isinstance(label, dict)
    }
    if not {"factory", "factory:unowned"} <= labels:
        return False, "Factory and unowned labels are required"
    if labels & BLOCKED_LABELS or any(
        isinstance(label, str) and OWNER_RE.fullmatch(label)
        for label in labels
    ):
        return False, "Factory PR has an active owner or hold"
    changed = pr.get("changed_files")
    if (
        not isinstance(changed, int)
        or changed < 1
        or changed >= 3000
        or changed != len(files)
    ):
        return False, "Changed file listing is incomplete"
    # Never permit a recreated source-controlled model inventory.
    retired_inventory = {
        ".github/free-model-factories.tsv",
        ".github/factory-expected-workers.json",
        ".github/factory-roster-snapshot.json",
    }
    if any(
        item.get("filename") in retired_inventory and item.get("status") != "removed"
        for item in files
    ):
        return False, "Source-controlled factory model inventories are forbidden"
    for file in files:
        if not isinstance(file, dict):
            return False, "Malformed changed-file entry"
        if not factory_path(file.get("filename", "")):
            return False, f"Outside factory-only allowlist: {file.get('filename')}"
        if file.get("status") == "renamed" and not factory_path(
            file.get("previous_filename", "")
        ):
            return False, "Rename crosses factory boundary"
    return True, "Factory-only PR eligible"


def enabled(path: Path) -> bool:
    """Read the trusted-main repository switch, defaulting to disabled."""
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("enabled") is True


def gh_api(
    endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None,
) -> Any:
    """Call GitHub's authenticated REST API without executing PR source."""
    cmd = ["gh", "api", "--method", method, endpoint]
    for key, value in (fields or {}).items():
        cmd.extend(["-f", f"{key}={value}"])
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"GitHub API {method} {endpoint} failed: {result.stderr[:400]}"
        )
    return json.loads(result.stdout)


def pages(endpoint: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Fetch full page sets, refusing GitHub's 3,000-file truncation boundary."""
    items: list[dict[str, Any]] = []
    for number in range(1, limit + 1):
        separator = "&" if "?" in endpoint else "?"
        page = gh_api(f"{endpoint}{separator}per_page=100&page={number}")
        if not isinstance(page, list) or not all(
            isinstance(item, dict) for item in page
        ):
            raise RuntimeError("GitHub returned an invalid paged response")
        items.extend(page)
        if len(page) < 100:
            return items
    raise RuntimeError("GitHub pagination exceeded safe 3,000-item limit")


def numbers(repo: str, *, pr: int | None, head: str | None) -> list[int]:
    """Select one PR, exact-head PRs, or all open PRs."""
    if pr is not None:
        return [pr]
    if head is not None:
        if not HEAD_RE.fullmatch(head):
            raise ValueError("Invalid CI head SHA")
        data = pages(f"repos/{repo}/commits/{head}/pulls")
    else:
        data = pages(f"repos/{repo}/pulls?state=open")
    return sorted({
        item["number"] for item in data
        if isinstance(item.get("number"), int)
        and not isinstance(item["number"], bool)
        and item["number"] > 0
    })


def required_checks_passed(repo: str, head: str) -> tuple[bool, str]:
    """Require all exact-head CI syntax, test, lint and type checks.

    Missing, pending, failing, stale, or ambiguously paginated check evidence
    fails closed. Never execute PR-authored code in this privileged workflow.
    """
    data = gh_api(f"repos/{repo}/commits/{head}/check-runs?per_page=100")
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("check_runs"), list)
        or not isinstance(data.get("total_count"), int)
        or data["total_count"] > 100
    ):
        return False, "Required CI check evidence missing or truncated"
    checks: dict[str, dict[str, Any]] = {}
    for check in data["check_runs"]:
        if not isinstance(check, dict) or check.get("name") not in REQUIRED_FAST_CHECKS:
            continue
        if check.get("head_sha") != head or (check.get("app") or {}).get("slug") != "github-actions":
            continue
        name = check["name"]
        previous = checks.get(name)
        if previous is None or int(check.get("id") or 0) > int(previous.get("id") or 0):
            checks[name] = check
    for name in sorted(REQUIRED_FAST_CHECKS):
        check = checks.get(name)
        if not check or check.get("status") != "completed" or check.get("conclusion") != "success":
            return False, f"Exact-head {name} has not passed"
    return True, "All factory CI checks passed at exact head"


def handle(repo: str, number: int) -> tuple[bool, str]:
    """Attempt one SHA-pinned merge after every exact-head factory CI check passes."""
    pr = gh_api(f"repos/{repo}/pulls/{number}")
    if not isinstance(pr, dict):
        raise RuntimeError("Invalid pull request response")
    files = pages(f"repos/{repo}/pulls/{number}/files")
    valid, reason = factory_candidate(pr, files, repo)
    if not valid:
        return False, reason
    head = pr["head"]["sha"]
    # The separate CI workflow runs untrusted PR code without privileged merge
    # credentials. Reuse only its signed GitHub Actions check-run conclusions.
    # An incomplete or failing result is a skip, never a bypass authorization.
    checks_ok, checks_reason = required_checks_passed(repo, head)
    if not checks_ok:
        return False, checks_reason
    try:
        result = gh_api(
            f"repos/{repo}/pulls/{number}/merge",
            method="PUT",
            fields={
                "sha": head,
                "merge_method": "squash",
                "commit_title": f"factory: immediate fast-merge #{number}",
            },
        )
    except RuntimeError as exc:
        return False, f"GitHub rejected immediate merge: {exc}"
    if not isinstance(result, dict) or result.get("merged") is not True:
        raise RuntimeError("GitHub did not confirm PR merge")
    return True, str(result.get("sha", ""))



def dispatch_production(merged_count: int) -> None:
    """Dispatch production after any bot merge, even if later PR handling fails."""
    if merged_count <= 0:
        return
    command = (
        'source .github/scripts/factory-dispatch-production-deploy.sh; '
        'factory_dispatch_production_deploy "$1"'
    )
    result = subprocess.run(
        [
            "bash", "-euo", "pipefail", "-c", command, "factory-fast-merge",
            f"Factory-only Fast Merge merged {merged_count} PR(s)",
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Post-merge production dispatch failed: " + result.stderr[:400]
        )


def main() -> None:
    """Attempt immediate owner-enabled merges and report results."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--pr", type=int)
    group.add_argument("--head")
    parser.add_argument(
        "--config", type=Path, default=Path(".github/factory-fast-merge.json"),
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("Invalid repository")
    if args.pr is not None and args.pr <= 0:
        parser.error("PR number must be positive")
    if not enabled(args.config):
        print(json.dumps({"enabled": False, "merged": [], "skipped": []}))
        return
    merged: list[int] = []
    skipped: list[dict[str, Any]] = []
    try:
        for number in numbers(args.repo, pr=args.pr, head=args.head):
            did_merge, reason = handle(args.repo, number)
            if did_merge:
                merged.append(number)
            else:
                skipped.append({"pr": number, "reason": reason})
    finally:
        # GitHub's GITHUB_TOKEN merge emits no push event. Do not lose the
        # deployment if a later PR fails during this same scan.
        dispatch_production(len(merged))
    print(json.dumps({"enabled": True, "merged": merged, "skipped": skipped}))


if __name__ == "__main__":
    main()
