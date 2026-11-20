#!/usr/bin/env python3
"""Capture a read-only ComicPile host view for Rotisserie decision shadowing."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from factory_review_policy import (
    parse_review_marker,
    recorded_pr_contributors,
    trusted_comment_bodies,
)
from factory_work_policy import FIXED_LEASE_TTL_SECONDS, LOCAL_LEASE_TTL_SECONDS, owner_of

REPOSITORY = "JoshCLWren/comic-pile"
FACTORY_OWNER_RE = re.compile(r"^factory:(?P<worker>local|[1-9]|[1-9][0-9])$")
JsonCommand = Callable[[list[str]], object]


class CaptureError(RuntimeError):
    """Raised when the live view cannot be captured completely."""


def _run_json(arguments: list[str]) -> object:
    completed = subprocess.run(arguments, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def _labels(item: dict[str, Any]) -> list[str]:
    labels = item.get("labels") or []
    return [str(label.get("name")) if isinstance(label, dict) else str(label) for label in labels]


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _lease(item: dict[str, Any]) -> dict[str, int] | None:
    owner = owner_of(_labels(item))
    if owner in (None, "factory:unowned") or not FACTORY_OWNER_RE.fullmatch(owner):
        return None
    acquired_at = _epoch(str(item["updatedAt"]))
    ttl = LOCAL_LEASE_TTL_SECONDS if owner == "factory:local" else FIXED_LEASE_TTL_SECONDS
    return {"acquired_at": acquired_at, "expires_at": acquired_at + ttl}


def _check_status(check: dict[str, Any]) -> str:
    state = str(check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
    if state in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "passed"
    if state in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "STALE", "ACTION_REQUIRED"}:
        return "failed"
    return "pending"


def _reviews(comments: list[dict[str, Any]], *, pr: int, head: str) -> list[dict[str, object]]:
    reviews: list[dict[str, object]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        marker = parse_review_marker(body.splitlines()[0] if body else "")
        if not marker or int(marker["pr"]) != pr:
            continue
        verdict = marker["verdict"]
        decision = "approved" if verdict == "approve" else "changes_requested"
        reviews.append(
            {
                "reviewer": marker["reviewer"],
                "decision": decision,
                "required": True,
                "head": marker["head"],
            }
        )
    return reviews


def capture_view(
    *, revision: str, captured_at: int, run_json: JsonCommand = _run_json
) -> dict[str, object]:
    """Acquire one immutable view using only read-only GitHub CLI operations."""
    issues_raw = run_json(
        [
            "gh", "issue", "list", "--repo", REPOSITORY, "--state", "open", "--limit", "500",
            "--json", "number,title,body,state,labels,createdAt,updatedAt",
        ]
    )
    prs_raw = run_json(
        [
            "gh", "pr", "list", "--repo", REPOSITORY, "--state", "open", "--limit", "200",
            "--json", "number,title,body,state,isDraft,labels,createdAt,updatedAt,headRefName,headRefOid",
        ]
    )
    if not isinstance(issues_raw, list) or not isinstance(prs_raw, list):
        raise CaptureError("GitHub list responses must be arrays")
    issues: list[dict[str, object]] = []
    for raw in issues_raw:
        if not isinstance(raw, dict) or "factory" not in _labels(raw):
            continue
        issue = dict(raw)
        issue["labels"] = _labels(raw)
        lease = _lease(raw)
        if lease:
            issue["lease"] = lease
        issues.append(issue)

    prs: list[dict[str, object]] = []
    for raw in prs_raw:
        if not isinstance(raw, dict):
            continue
        branch = str(raw.get("headRefName") or "")
        if "factory" not in _labels(raw) and not branch.startswith("factory/"):
            continue
        number = int(raw["number"])
        detail = run_json(
            ["gh", "pr", "view", str(number), "--repo", REPOSITORY, "--json", "statusCheckRollup"]
        )
        comments = run_json(
            ["gh", "api", f"repos/{REPOSITORY}/issues/{number}/comments", "--paginate"]
        )
        if not isinstance(detail, dict) or not isinstance(comments, list):
            raise CaptureError(f"incomplete pull request detail for #{number}")
        pr = dict(raw)
        pr["labels"] = _labels(raw)
        pr["checks"] = [
            {
                "name": str(check.get("name") or check.get("context") or "unknown"),
                "status": _check_status(check),
                "required": True,
            }
            for check in detail.get("statusCheckRollup") or []
            if isinstance(check, dict)
        ]
        head = str(pr.get("headRefOid") or "")
        rows = [comment for comment in comments if isinstance(comment, dict)]
        pr["reviews"] = _reviews(rows, pr=number, head=head)
        # Contributor provenance must be captured under the identical trusted
        # filter the review controller applies, otherwise the shadow decision
        # and the real controller would disagree about who authored a head.
        pr["head_contributors"] = sorted(
            recorded_pr_contributors(trusted_comment_bodies(rows), pr=number)
        )
        lease = _lease(raw)
        if lease:
            pr["lease"] = lease
        prs.append(pr)
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "revision": revision,
        "captured_at": captured_at,
        "issues": issues,
        "pull_requests": prs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        revision = arguments.revision.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise CaptureError("--revision must be an exact 40-character commit SHA")
        view = capture_view(revision=revision, captured_at=int(time.time()))
        rendered = json.dumps(view, sort_keys=True, separators=(",", ":")) + "\n"
        if arguments.output:
            arguments.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (CaptureError, OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as error:
        print(f"factory-rotisserie-capture: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
