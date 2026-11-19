#!/usr/bin/env python3
"""Backfill trusted historical source-run delivery without changing worker outcomes.

A merged PR or a matching issue number alone is never delivery proof. Require a
controller-bot checkpoint comment, canonical same-repository PR provenance,
trusted Actions source run and immutable changed bytes in the merge commit.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_checkpoint_handoff import (META, SHA, api, incorporated, obj, pages,
                                        source_worker, verified_run)

BODY_MARKER = re.compile(r"(?m)^<!-- factory-checkpoint-handoff:v1 run=([1-9][0-9]*) -->$")
SAVED_MARKER = re.compile(
    r"(?m)^<!-- factory-persisted-handoff:v1 run=([1-9][0-9]*) head=([0-9a-f]{40}) -->$"
)


def recent_candidate_prs(repo: str, *, now: datetime, days: int,
                         max_pages: int = 3, max_prs: int = 12) -> list[int]:
    """Find only a bounded number of recently merged checkpoint PRs.

    Args:
        repo: Canonical repository.
        now: Trusted controller time.
        days: Recent history window.
        max_pages: GitHub pagination bound.
        max_prs: Work/credential budget for one reconciliation.

    Returns:
        Eligible PR numbers; no claim mutation is performed.
    """
    floor = now - timedelta(days=max(1, days))
    chosen: list[int] = []
    seen: set[int] = set()
    for page in range(1, max(1, min(max_pages, 10)) + 1):
        batch = api(f"repos/{repo}/pulls?state=closed&sort=updated"
                    f"&direction=desc&per_page=100&page={page}")
        if (not isinstance(batch, list)
                or not all(isinstance(x, dict) for x in batch)
                or len(batch) > 100):
            raise RuntimeError("Invalid closed PR page")
        for pr in batch:
            number = pr.get("number")
            if not isinstance(number, int) or number in seen:
                continue
            seen.add(number)
            merged = pr.get("merged_at")
            if not isinstance(merged, str) or not merged:
                continue
            at = datetime.fromisoformat(merged.replace("Z", "+00:00"))
            if at < floor or at > now:
                continue
            if (pr.get("base", {}).get("ref") != "main"
                    or not BODY_MARKER.search(pr.get("body") or "")):
                continue
            chosen.append(number)
            if len(chosen) >= max_prs:
                return chosen
        if len(batch) < 100:
            break
    return chosen


def incident_prs(repo: str, issue_number: int, *, max_prs: int = 25) -> list[int]:
    """Find user-documented historical examples, then independently verify all.

    This input is a discovery hint, NEVER an attestation. A referenced PR
    earns no credit without bot checkpoint + run + byte-level merge proof.
    """
    if issue_number <= 0:
        raise ValueError("Invalid incident tracking issue")
    issue = obj(f"repos/{repo}/issues/{issue_number}")
    body = str(issue.get("body") or "")
    pair = re.compile(
        r"(?m)^\|\s*\[([1-9][0-9]*)\]\([^\n|]+\)\s*\|"
        r"\s*#([1-9][0-9]*)\s*\|"
    )
    found = list(dict.fromkeys(int(pr) for _run, pr in pair.findall(body)))
    return found[:max_prs]


def snapshot_for_run(repo: str, sha: str, run_id: str, worker: str) -> dict[str, str]:
    """Resolve immutable source-run checkpoint metadata, never mutable ref tip."""
    if not SHA.fullmatch(sha):
        raise ValueError("Unverified checkpoint SHA")
    cursor = obj(f"repos/{repo}/git/commits/{sha}")
    for _ in range(100):
        message = str(cursor.get("message") or "")
        matched = META.fullmatch(message.splitlines()[0] if message else "")
        if matched and matched[2] == run_id and matched[1] == worker:
            metadata = dict(re.findall(r"(?m)^([A-Za-z-]+): (.+)$", message))
            base = metadata.get("Base-commit") or ""
            head = str(cursor.get("sha") or "")
            origin_tree = (cursor.get("tree") or {}).get("sha")
            if not (SHA.fullmatch(base) and SHA.fullmatch(head)
                    and isinstance(origin_tree, str) and SHA.fullmatch(origin_tree)):
                raise ValueError("Incomplete immutable factory checkpoint provenance")
            return {"base": base, "head": head, "tree": origin_tree}
        parents = cursor.get("parents") or []
        if not parents or not isinstance(parents[0], dict):
            break
        cursor = obj(f"repos/{repo}/git/commits/{parents[0]['sha']}")
    raise ValueError("Checkpoint does not prove this run and worker")


def receipt_for_pr(repo: str, number: int) -> dict[str, Any] | None:
    """Verify trusted source-run linkage and independent eventual merge proof.

    Args:
        repo: The repository to reconcile.
        number: GitHub work PR number.

    Returns:
        Verified saved/link receipts, plus merge proof if the source bytes
        were actually included. None means no eligible factory provenance.
    """
    pr = obj(f"repos/{repo}/pulls/{number}")
    matches = BODY_MARKER.findall(pr.get("body") or "")
    if (len(matches) != 1 or pr.get("base", {}).get("ref") != "main"
            or (pr.get("head") or {}).get("repo", {}).get("full_name") != repo):
        return None
    run_id = matches[0]
    head = pr.get("head") or {}
    branch = str(head.get("ref") or "")
    if not (branch.startswith("factory/work-checkpoints/")
            and branch.endswith("/" + run_id)
            and SHA.fullmatch(str(head.get("sha") or ""))):
        return None
    run = verified_run(repo, run_id)
    if run.get("status") != "completed":
        return None
    worker = source_worker(run)
    comments = pages(f"repos/{repo}/issues/{number}/comments")
    candidates: list[str] = []
    for comment in comments:
        if comment.get("user", {}).get("login") != "github-actions[bot]":
            continue
        for source, sha in SAVED_MARKER.findall(comment.get("body") or ""):
            if source == run_id and sha not in candidates:
                candidates.append(sha)
    if not candidates:
        return None
    # Every original snapshot is checked; no trusted source can be replaced
    # by a later worker's mutable checkpoint tip.
    verified: dict[str, str] | None = None
    for checkpoint in candidates:
        try:
            source = snapshot_for_run(repo, checkpoint, run_id, worker)
        except ValueError:
            continue
        comparison = obj(f"repos/{repo}/compare/{source['head']}...{head['sha']}")
        if comparison.get("status") not in {"ahead", "identical"}:
            continue
        # The final bot-recorded revision of *this same source run* is the
        # last complete checkpoint, not another worker's later PR head.
        verified = source
    if verified is None:
        return None
    if not incorporated(repo, verified, verified["head"]):
        return None  # No saved code; checkpoint marker alone is not productivity.
    result: dict[str, Any] = {
        "run_id": run_id, "worker": worker, "saved": True,
        "checkpoint_sha": verified["head"], "published": True,
        "work_pr": number, "validated": False,
    }
    merge_sha = str(pr.get("merge_commit_sha") or "")
    # The source commit is verified as an ancestor of the actual PR head.
    # A later repair can change every original file without erasing that
    # source-run contribution to the PR's history. Report merge ancestry
    # separately from exact original-byte survival.
    if pr.get("merged") is True and SHA.fullmatch(merge_sha):
        try:
            exact = incorporated(repo, verified, merge_sha)
        except RuntimeError:
            exact = None
        result.update({
            "merged": True, "merge_sha": merge_sha,
            "merge_basis": "pr_ancestry", "exact_bytes_included": exact,
        })
    return result


def backfill(repo: str, prs: list[int], *,
             receipt_writer: Any) -> dict[str, Any]:
    """Record each proven PR independently; continue past unresolved history."""
    summary: dict[str, Any] = {"checked": [], "saved": [], "merged": [],
                               "unverified": [], "errors": {}}
    for number in prs:
        summary["checked"].append(number)
        try:
            result = receipt_for_pr(repo, number)
            if not result:
                summary["unverified"].append(number)
                continue
            receipt_writer(repo, [result])
            summary["saved"].append(number)
            if result.get("merged"):
                summary["merged"].append(number)
        except (RuntimeError, ValueError, KeyError, TimeoutError) as exc:
            summary["errors"][str(number)] = str(exc)[:250]
    return summary


def main() -> int:
    """Run bounded, trusted PR evidence reconciliation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=int, action="append", default=[])
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-prs", type=int, default=12)
    parser.add_argument("--incident-issue", type=int)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("Invalid repository")
    from factory_state_ledger import enabled, record_checkpoint_evidence
    if not enabled():
        raise RuntimeError("Neon required for trusted delivery backfill")
    limit = max(1, min(args.max_prs, 25))
    if args.pr:
        prs = list(dict.fromkeys(args.pr))[:limit]
    elif args.incident_issue:
        prs = incident_prs(args.repo, args.incident_issue, max_prs=limit)
    else:
        prs = recent_candidate_prs(
            args.repo, now=datetime.now(UTC), days=max(1, min(args.days, 14)),
            max_pages=max(1, min(args.max_pages, 10)), max_prs=limit,
        )
    if any(number <= 0 for number in prs):
        raise ValueError("Invalid work PR")
    summary = backfill(args.repo, prs, receipt_writer=record_checkpoint_evidence)
    print(json.dumps(summary, sort_keys=True))
    return int(bool(summary["errors"]))


if __name__ == "__main__":
    sys.exit(main())
