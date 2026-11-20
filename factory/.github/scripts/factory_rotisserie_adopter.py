#!/usr/bin/env python3
"""Translate one captured ComicPile Factory view into Rotisserie shadow inputs.

The adapter deliberately performs no GitHub reads or writes.  An operator captures one
host view, records its upstream revision, and passes that immutable JSON document here.
Both the legacy decision baseline and Rotisserie graph snapshot are derived from those
same bytes, preventing a race between independently acquired views.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from factory_review_policy import head_has_authorized_approval
from factory_work_policy import (
    BLOCKED_LABELS,
    FACTORY_REVIEW_BACKLOG_LIMIT,
    FACTORY_PR_WIP_LIMIT,
    NON_EXECUTABLE_ISSUES,
    build_candidates,
    factory_review_backlog_count,
    factory_pr_wip_count,
    is_manual_only,
    labels_of,
    linked_issue_from_pr,
    owner_of,
    parse_depends_on_numbers,
    parse_time,
    priority_rank,
    provenance_lane,
    producer_worker_from_pr,
)

DIMENSIONS = ["completion", "eligibility", "ownership", "ranking", "recovery", "review"]
REPOSITORY = {"host": "github.com", "owner": "JoshCLWren", "name": "comic-pile"}


class AdopterInputError(ValueError):
    """Raised when a captured host view cannot be translated safely."""


def _labels(item: dict[str, Any]) -> list[str]:
    return sorted(labels_of(item))


def _work_id(number: int) -> dict[str, object]:
    return {"repository": REPOSITORY, "key": str(number)}


def _change_id(number: int) -> dict[str, object]:
    return {"repository": REPOSITORY, "key": str(number)}


def _revision_id(sha: str) -> dict[str, object]:
    return {"repository": REPOSITORY, "value": sha}


def _worker_id(value: str) -> dict[str, str]:
    return {"namespace": "comic-pile-factory", "value": value}


def _validate(view: dict[str, Any]) -> None:
    if view.get("schema_version") != 1:
        raise AdopterInputError("unsupported captured-view schema version")
    if view.get("repository") != "JoshCLWren/comic-pile":
        raise AdopterInputError("captured view is outside JoshCLWren/comic-pile")
    revision = view.get("revision")
    if not isinstance(revision, str) or len(revision) != 40:
        raise AdopterInputError("revision must be an exact 40-character commit SHA")
    captured_at = view.get("captured_at")
    if not isinstance(captured_at, int) or isinstance(captured_at, bool) or captured_at < 0:
        raise AdopterInputError("captured_at must be a non-negative epoch integer")
    if not isinstance(view.get("issues"), list) or not isinstance(view.get("pull_requests"), list):
        raise AdopterInputError("issues and pull_requests must be lists")


def _producer(pr: dict[str, Any]) -> str | None:
    declared = pr.get("producer_worker")
    if declared is not None:
        return str(declared)
    return producer_worker_from_pr(pr)


def _contributors(pr: dict[str, Any]) -> tuple[set[str], bool]:
    """Return the captured contributor set and whether provenance was recorded.

    The declared producer authored every head of its own branch, so it is
    always part of the contributor set even when the capture predates trusted
    contributor markers.
    """
    recorded = {str(worker) for worker in pr.get("head_contributors") or [] if worker}
    producer = _producer(pr)
    contributors = set(recorded)
    if producer is not None:
        contributors.add(producer)
    return contributors, bool(recorded)


def _human_gate(issue: dict[str, Any]) -> bool:
    """Return whether ComicPile policy requires an explicit non-worker gate."""
    labels = set(_labels(issue))
    return (
        int(issue["number"]) in NON_EXECUTABLE_ISSUES
        or bool(labels & {"epic", "prd"})
        or is_manual_only(issue.get("body"))
        or bool(labels & BLOCKED_LABELS)
        or "ralph-status:done" in labels
        or "factory:ready" in labels
    )


def _portable_priorities(issues: list[dict[str, Any]]) -> dict[int, int]:
    """Encode ComicPile's issue ordering as host-neutral numeric priorities."""
    ordered = sorted(
        issues,
        key=lambda issue: (
            provenance_lane(set(_labels(issue))),
            -priority_rank(_labels(issue)),
            parse_time(str(issue.get("createdAt") or "")),
            int(issue["number"]),
        ),
    )
    return {
        int(issue["number"]): len(ordered) - rank
        for rank, issue in enumerate(ordered)
    }


def graph_snapshot(view: dict[str, Any]) -> dict[str, object]:
    """Build Rotisserie's public GraphSnapshot v1 shape from a captured host view."""
    _validate(view)
    issues = [dict(item) for item in view["issues"]]
    prs = [dict(item) for item in view["pull_requests"]]
    portable_priorities = _portable_priorities(issues)
    issue_numbers = {int(issue["number"]) for issue in issues}
    workers: set[str] = set()
    leases: list[dict[str, object]] = []
    for issue in issues:
        owner = owner_of(_labels(issue))
        lease = issue.get("lease")
        if owner not in (None, "factory:unowned") and isinstance(lease, dict):
            worker = owner.removeprefix("factory:")
            workers.add(worker)
            leases.append(
                {
                    "id": f"issue-{issue['number']}-{worker}",
                    "work": _work_id(int(issue["number"])),
                    "worker": _worker_id(worker),
                    "acquired_at": int(lease["acquired_at"]),
                    "expires_at": int(lease["expires_at"]),
                }
            )

    changes: list[dict[str, object]] = []
    revisions: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []
    reviews: list[dict[str, object]] = []
    for pr in prs:
        linked = linked_issue_from_pr(pr)
        if linked is None or linked not in issue_numbers or str(pr.get("state", "OPEN")).upper() != "OPEN":
            continue
        sha = str(pr.get("headRefOid") or "")
        if len(sha) != 40:
            raise AdopterInputError(f"pull request {pr['number']} lacks an exact head SHA")
        producer = _producer(pr)
        if producer:
            workers.add(producer)
        changes.append(
            {
                "id": _change_id(int(pr["number"])),
                "work": _work_id(linked),
                "head": _revision_id(sha),
                "producer": _worker_id(producer) if producer else None,
            }
        )
        revisions.append({"id": _revision_id(sha)})
        for check in pr.get("checks") or []:
            checks.append(
                {
                    "revision": _revision_id(sha),
                    "name": str(check["name"]),
                    "status": str(check["status"]),
                    "required": bool(check.get("required", True)),
                }
            )
        for index, review in enumerate(pr.get("reviews") or []):
            reviewer = str(review["reviewer"])
            workers.add(reviewer)
            reviewed_head = str(review.get("head") or sha)
            if reviewed_head != sha and not any(
                item["id"]["value"] == reviewed_head for item in revisions
            ):
                revisions.append({"id": _revision_id(reviewed_head)})
            reviews.append(
                {
                    "id": f"pr-{pr['number']}-review-{index}",
                    "revision": _revision_id(reviewed_head),
                    "reviewer": _worker_id(reviewer),
                    "decision": str(review["decision"]),
                    "required": bool(review.get("required", True)),
                }
            )

    dependencies: list[dict[str, object]] = []
    for issue in issues:
        for required in sorted(parse_depends_on_numbers(str(issue.get("body") or ""))):
            if required in issue_numbers:
                dependencies.append(
                    {
                        "dependent": _work_id(int(issue["number"])),
                        "prerequisite": _work_id(required),
                    }
                )

    return {
        "schema_version": 1,
        "works": [
            {
                "id": _work_id(int(issue["number"])),
                "title": str(issue.get("title") or f"Issue {issue['number']}"),
                "state": "open" if str(issue.get("state", "OPEN")).upper() == "OPEN" else "completed",
                "priority": portable_priorities[int(issue["number"])],
            }
            for issue in issues
        ],
        "changes": changes,
        "revisions": revisions,
        "workers": [{"id": _worker_id(worker), "human": False} for worker in sorted(workers)],
        "leases": leases,
        "checks": checks,
        "reviews": reviews,
        "evidence": [],
        "capacities": [],
        "boundaries": [
            {
                "id": f"comic-pile-policy-{issue['number']}",
                "work": _work_id(int(issue["number"])),
                "kind": "human_approval",
                "satisfied_by": None,
            }
            for issue in issues
            if _human_gate(issue)
        ],
        "dependencies": dependencies,
    }


def _work_blocks(
    issue: dict[str, Any],
    *,
    open_numbers: set[int],
    implemented: set[int],
    at: int,
) -> list[str]:
    blocks: list[str] = []
    number = int(issue["number"])
    if str(issue.get("state", "OPEN")).upper() != "OPEN":
        blocks.append("not_open")
    if parse_depends_on_numbers(str(issue.get("body") or "")) & open_numbers:
        blocks.append("dependency_incomplete")
    if _human_gate(issue):
        blocks.append("human_boundary")
    lease = issue.get("lease")
    if isinstance(lease, dict) and int(lease["acquired_at"]) <= at < int(lease["expires_at"]):
        blocks.append("active_lease")
    if number in implemented:
        blocks.append("implementation_exists")
    return blocks


def legacy_decisions(view: dict[str, Any]) -> dict[str, object]:
    """Normalize current ComicPile Factory decisions into DecisionSnapshot v1."""
    _validate(view)
    issues = [dict(item) for item in view["issues"]]
    prs = [dict(item) for item in view["pull_requests"]]
    at = int(view["captured_at"])
    revision = str(view["revision"])
    open_numbers = {
        int(issue["number"])
        for issue in issues
        if str(issue.get("state", "OPEN")).upper() == "OPEN"
    }
    implemented = {
        linked
        for pr in prs
        if str(pr.get("state", "OPEN")).upper() == "OPEN"
        and (linked := linked_issue_from_pr(pr)) is not None
    }
    raw_retry_counts = view.get("no_diff_attempts_by_issue") or {}
    retry_counts = {int(number): int(count) for number, count in raw_retry_counts.items()}
    candidates = build_candidates(issues, prs, no_diff_attempts_by_issue=retry_counts)
    issue_candidates = [candidate for candidate in candidates if candidate.kind == "issue"]
    rank_by_issue = {candidate.number: rank for rank, candidate in enumerate(issue_candidates)}
    eligible_issues = set(rank_by_issue)
    observations: list[dict[str, object]] = []
    for issue in sorted(issues, key=lambda item: int(item["number"])):
        number = int(issue["number"])
        blocks = _work_blocks(
            issue,
            open_numbers=open_numbers - {number},
            implemented=implemented,
            at=at,
        )
        if (
            number not in eligible_issues
            and not blocks
            and factory_review_backlog_count(prs) >= FACTORY_REVIEW_BACKLOG_LIMIT
        ):
            blocks.append("backpressure")
        elif number not in eligible_issues and not blocks:
            # Provider topology, retry-generation limits, manual-only markers,
            # or fleet backpressure can suppress otherwise portable work. Keep
            # that deliberate adopter policy visible instead of pretending the
            # legacy controller selected the issue.
            blocks.append("adopter_policy")
        observations.append(
            {
                "dimension": "eligibility",
                "subject": f"work:{number}",
                "outcome": "blocked" if blocks else "eligible",
                "reasons": blocks,
                "rank": None,
            }
        )
        if number in rank_by_issue:
            observations.append(
                {
                    "dimension": "ranking",
                    "subject": f"work:{number}",
                    "outcome": "ranked",
                    "reasons": [],
                    "rank": rank_by_issue[number],
                }
            )
        lease = issue.get("lease")
        active_owner = "unowned"
        if isinstance(lease, dict) and int(lease["acquired_at"]) <= at < int(lease["expires_at"]):
            owner = owner_of(_labels(issue))
            if owner and owner != "factory:unowned":
                active_owner = f"comic-pile-factory:{owner.removeprefix('factory:')}"
        observations.append(
            {
                "dimension": "ownership",
                "subject": f"work:{number}",
                "outcome": active_owner,
                "reasons": [],
                "rank": None,
            }
        )

    for pr in sorted(prs, key=lambda item: int(item["number"])):
        linked = linked_issue_from_pr(pr)
        if linked is None or linked not in open_numbers or str(pr.get("state", "OPEN")).upper() != "OPEN":
            continue
        head = str(pr.get("headRefOid") or "")
        contributors, provenance_complete = _contributors(pr)
        reviews = [
            dict(review)
            for review in pr.get("reviews") or []
            if str(review.get("head") or head) == head
        ]
        if any(review["decision"] == "changes_requested" for review in reviews):
            review_outcome = "changes_requested"
        elif not reviews or any(review["decision"] == "pending" for review in reviews):
            review_outcome = "pending"
        elif head_has_authorized_approval(
            contributors=contributors,
            approvers=(str(review["reviewer"]) for review in reviews if review["decision"] == "approved"),
            provenance_complete=provenance_complete,
        ):
            review_outcome = "approved"
        else:
            review_outcome = "blocked"
        review_reasons = ["independent_review_required"] if review_outcome == "blocked" else []
        observations.append(
            {
                "dimension": "review",
                "subject": f"change:{pr['number']}",
                "outcome": review_outcome,
                "reasons": review_reasons,
                "rank": None,
            }
        )
        required_checks = [check for check in pr.get("checks") or [] if check.get("required", True)]
        completion_blocks: list[str] = []
        if any(check["status"] == "failed" for check in required_checks):
            completion_blocks.append("checks_failed")
        elif not required_checks or any(check["status"] == "pending" for check in required_checks):
            completion_blocks.append("checks_pending")
        if review_outcome == "changes_requested":
            completion_blocks.extend(["review_rejected", "independent_review_required"])
        elif review_outcome == "pending":
            completion_blocks.extend(["review_pending", "independent_review_required"])
        elif review_outcome == "blocked":
            completion_blocks.append("independent_review_required")
        observations.append(
            {
                "dimension": "completion",
                "subject": f"change:{pr['number']}",
                "outcome": "blocked" if completion_blocks else "ready",
                "reasons": completion_blocks,
                "rank": None,
            }
        )

    for issue in sorted(issues, key=lambda item: int(item["number"])):
        lease = issue.get("lease")
        owner = owner_of(_labels(issue))
        if not isinstance(lease, dict) or owner in (None, "factory:unowned"):
            continue
        expired = int(lease["expires_at"]) <= at
        observations.append(
            {
                "dimension": "recovery",
                "subject": f"lease:issue-{issue['number']}-{owner.removeprefix('factory:')}",
                "outcome": "release" if expired else "retain",
                "reasons": ["expired"] if expired else [],
                "rank": None,
            }
        )
    return {
        "schema_version": 1,
        "source": "comic-pile-factory",
        "revision": revision,
        "dimensions": DIMENSIONS,
        "observations": observations,
    }


def build_bundle(raw: bytes) -> dict[str, object]:
    """Return inputs and provenance bound to the exact captured bytes."""
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise AdopterInputError("captured view must be a JSON object")
    return {
        "schema_version": 1,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_revision": parsed.get("revision"),
        "captured_at": parsed.get("captured_at"),
        "legacy": legacy_decisions(parsed),
        "snapshot": graph_snapshot(parsed),
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("view", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--rotisserie-command", default="rotisserie")
    parser.add_argument("--rotisserie-config", type=Path)
    args = parser.parse_args()
    try:
        raw = args.view.read_bytes()
        bundle = build_bundle(raw)
        if args.output:
            _write_json(args.output, bundle)
        if not args.shadow:
            if not args.output:
                json.dump(bundle, sys.stdout, sort_keys=True)
                sys.stdout.write("\n")
            return 0
        if args.rotisserie_config is None:
            raise AdopterInputError("--rotisserie-config is required with --shadow")
        with tempfile.TemporaryDirectory(prefix="comic-pile-rotisserie-") as directory:
            root = Path(directory)
            baseline = root / "legacy.json"
            snapshot = root / "snapshot.json"
            _write_json(baseline, bundle["legacy"])
            _write_json(snapshot, bundle["snapshot"])
            command = [
                args.rotisserie_command,
                "--config",
                str(args.rotisserie_config),
                "shadow-project",
                "--baseline",
                str(baseline),
                "--snapshot",
                str(snapshot),
                "--revision",
                str(bundle["source_revision"]),
                "--at",
                str(bundle["captured_at"]),
                "--completion-backlog",
                str(factory_review_backlog_count(json.loads(raw)["pull_requests"])),
                "--backlog-limit",
                str(FACTORY_REVIEW_BACKLOG_LIMIT),
                "--active-changes",
                str(factory_pr_wip_count(json.loads(raw)["pull_requests"])),
                "--wip-limit",
                str(FACTORY_PR_WIP_LIMIT),
            ]
            return subprocess.run(command, check=False).returncode
    except (AdopterInputError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"factory-rotisserie-adopter: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
