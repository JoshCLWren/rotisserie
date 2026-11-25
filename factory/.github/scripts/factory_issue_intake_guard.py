#!/usr/bin/env python3
"""Two issue-intake holds on the dispatcher's pool pass, and the context an implement run gets.

* An issue with an open PR that delivers part of it (a ``Part of #N.`` line or a
  ``factory/work-checkpoints/issue-N/`` branch) gets no new implement run. A
  closing PR already suppresses its issue (``pr_suppresses_issue_candidate``).
  Merged slices do not hold the issue: the remaining work is real, so the
  implement run gets the merged PRs as context instead.
* An issue whose body is over ``BODY_LIMIT`` characters, or that carries an
  epic, umbrella or tracking label, is not one task. The factory posts one
  comment that asks the owner for a split proposal and labels it
  ``factory:needs-split``. The owner approves each split; the factory never
  creates child issues (Rotisserie issue #56).

An owner ``factory:override`` label, a claim or a targeted run is never held.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

BODY_LIMIT = 16_000
BODY_LIMIT_SETTING = "FACTORY_ISSUE_BODY_LIMIT"
SPLIT_LABELS = frozenset({"epic", "umbrella", "tracking"})
NEEDS_SPLIT = "factory:needs-split"
NEEDS_SPLIT_DEFINITION = ("d93f0b", "Too large for one factory task; the owner approves a split")
BYPASS = frozenset({"factory:override", "factory:claimed"})
MARKER = "factory-split-request-v1"
PART_OF_RE = re.compile(r"(?im)^[ \t]*part[ \t]+of[ \t]+#(?P<issue>\d+)\b")
CHECKPOINT_BRANCH_RE = re.compile(r"^factory/work-checkpoints/issue-(?P<issue>\d+)/")


def _labels(item: Mapping[str, Any]) -> set[str]:
    found = set()
    for label in item.get("labels") or []:
        name = label.get("name") if isinstance(label, Mapping) else label
        if isinstance(name, str):
            found.add(name)
    return found


def body_limit() -> int:
    try:
        from factory_settings import integer
    except ImportError:
        return BODY_LIMIT
    return integer(BODY_LIMIT_SETTING, BODY_LIMIT)


def sliced_issue(pr: Mapping[str, Any]) -> int | None:
    """The issue an open PR delivers only part of, if any."""
    branch = CHECKPOINT_BRANCH_RE.match(str(pr.get("headRefName") or ""))
    if branch:
        return int(branch["issue"])
    part = PART_OF_RE.search(str(pr.get("body") or ""))
    return int(part["issue"]) if part else None


def open_slice_issues(prs: Iterable[Mapping[str, Any]]) -> set[int]:
    return {
        issue
        for pr in prs
        if str(pr.get("state") or "OPEN").upper() == "OPEN"
        and (issue := sliced_issue(pr)) is not None
    }


def split_reason(issue: Mapping[str, Any], limit: int | None = None) -> str | None:
    """Why this issue is not one implement task, or None."""
    labels = {label.lower() for label in _labels(issue)}
    tagged = sorted(labels & SPLIT_LABELS)
    if tagged:
        return f"it is labeled {tagged[0]}"
    size = len(str(issue.get("body") or ""))
    cap = body_limit() if limit is None else limit
    if size > cap:
        return f"its body is {size:,} characters, over the {cap:,}-character limit"
    return None


def held(issue: Mapping[str, Any], open_slices: set[int], *, limit: int | None = None) -> bool:
    if BYPASS & _labels(issue):
        return False
    return int(issue["number"]) in open_slices or split_reason(issue, limit) is not None


def split_comment(number: int, reason: str) -> str:
    return (
        f"<!-- {MARKER}:issue-{number} -->\n\n"
        f"**Factory intake:** issue #{number} needs a split before the factory takes it: "
        f"{reason}. The factory does not dispatch it as one task. Post a split proposal"
        " (the child issues and what each one delivers) and create the issues you approve;"
        " the factory never creates them. Remove the `factory:needs-split` label or add"
        " `factory:override` to send it as one task anyway."
    )


def request_splits(
    issues: Iterable[Mapping[str, Any]],
    write: Callable[[str, object], object],
    *,
    limit: int | None = None,
    budget: int = 3,
) -> list[int]:
    """Comment and label each oversized issue once; the label records that it was asked."""
    asked: list[int] = []
    for issue in issues:
        labels = _labels(issue)
        if len(asked) >= budget or NEEDS_SPLIT in labels or BYPASS & labels:
            continue
        reason = split_reason(issue, limit)
        if reason is None:
            continue
        number = int(issue["number"])
        color, description = NEEDS_SPLIT_DEFINITION
        try:
            write("labels", {"name": NEEDS_SPLIT, "color": color, "description": description})
        except RuntimeError, OSError, subprocess.SubprocessError, ValueError:
            pass
        write(f"issues/{number}/labels", {"labels": [NEEDS_SPLIT]})
        write(f"issues/{number}/comments", {"body": split_comment(number, reason)})
        asked.append(number)
    return asked


def request_splits_gh(issues: Iterable[Mapping[str, Any]], repo: str) -> list[int]:
    """``request_splits`` over the gh CLI (the dispatcher's workflow token); never raises."""

    def write(path: str, payload: object) -> object:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/{path}", "--method", "POST", "--input", "-"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout).strip() or "gh api failed")
        return proc.stdout

    try:
        return request_splits(issues, write)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"[factory-controller] split request deferred: {exc}", file=sys.stderr)
        return []


def merged_context(number: int, merged: Iterable[Mapping[str, Any]]) -> str:
    """One prompt line naming the merged PRs that already delivered part of the issue."""
    rows = [
        pr
        for pr in merged
        if PART_OF_RE.search(str(pr.get("body") or "")) and sliced_issue(pr) == number
    ]
    if not rows:
        return ""
    names = ", ".join(f"#{pr['number']} ({str(pr.get('title') or '')[:80]})" for pr in rows[:10])
    return (
        f" Merged PRs already delivered part of issue #{number}: {names}. Read their diffs first"
        " and implement only what remains; do not redo or revert that work."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["merged-context"])
    parser.add_argument("issue", type=int)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            args.repo,
            "--state",
            "merged",
            "--limit",
            "20",
            "--search",
            f'"Part of #{args.issue}" in:body',
            "--json",
            "number,title,body",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode:
        return 0
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        return 0
    sys.stdout.write(merged_context(args.issue, rows if isinstance(rows, list) else []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
