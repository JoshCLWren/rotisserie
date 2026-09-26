#!/usr/bin/env python3
"""Serialize final integration for factory PRs that change Alembic migrations.

ComicPile requires a single Alembic head, so migration-bearing PRs cannot safely
finish review/CI/merge in parallel. This controller keeps ordinary implementation
parallel, but parks all except one migration PR once they reach the finalization
stages that can lead to merge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

REPO = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
GH_TIMEOUT_SECONDS = 120
WAIT_LABEL = "factory:migration-wait"
MIGRATION_PREFIX = "alembic/versions/"
FINALIZATION_STAGES = (
    "factory:ready",
    "factory:ci",
    "factory:review",
    "factory:changes-requested",
)
STAGE_PRIORITY = {stage: index for index, stage in enumerate(FINALIZATION_STAGES)}
STAGE_LABELS = {
    "factory:building",
    "factory:review",
    "factory:changes-requested",
    "factory:ci",
    "factory:ready",
    "factory:blocked",
}
OWNER_RE = re.compile(r"^factory:(?:unowned|local|[1-9]|[1-7][0-9])$")
ACTIVE_OWNER_RE = re.compile(r"^factory:(?:local|[1-9]|[1-7][0-9])$")
RALPH_STATUS_RE = re.compile(r"^ralph-status:")
BRANCH_ISSUE_RE = re.compile(r"^factory/\d+-(\d+)-")


class LaneConflictError(RuntimeError):
    """Raised when more than one migration finalizer still has an active lease."""


@dataclass(frozen=True)
class MigrationPr:
    number: int
    created_at: str
    labels: frozenset[str]
    branch: str

    @property
    def stage(self) -> str | None:
        return next((stage for stage in FINALIZATION_STAGES if stage in self.labels), None)

    @property
    def is_waiting(self) -> bool:
        return WAIT_LABEL in self.labels and "factory:blocked" in self.labels

    @property
    def active_owner(self) -> str | None:
        return next((label for label in sorted(self.labels) if ACTIVE_OWNER_RE.fullmatch(label)), None)

    @property
    def is_active_finalizer(self) -> bool:
        return self.stage is not None and "factory:blocked" not in self.labels


def changed_file_is_migration(row: dict[str, Any]) -> bool:
    """Return whether a GitHub changed-file row touches Alembic version history."""
    for key in ("filename", "previous_filename"):
        path = str(row.get(key) or "")
        if path.startswith(MIGRATION_PREFIX):
            return True
    return False


def label_names(value: object) -> frozenset[str]:
    """Normalize REST or GraphQL label payloads into names."""
    if not isinstance(value, list):
        return frozenset()
    names: set[str] = set()
    for label in value:
        if isinstance(label, dict) and label.get("name"):
            names.add(str(label["name"]))
        elif isinstance(label, str):
            names.add(label)
    return frozenset(names)


def plan_lane(prs: Iterable[MigrationPr]) -> tuple[int | None, tuple[int, ...], int | None]:
    """Return (holder, PRs to park, waiter to release) for one snapshot.

    An actively leased finalizer wins the lane so reconciliation never steals a
    PR out from under a running worker. If more than one finalizer is actively
    leased, fail closed and let those workers finish/release before mutating
    lifecycle state. Without an active lease, prefer the PR furthest through
    finalization, then the oldest PR for deterministic tie-breaking.
    """
    rows = list(prs)
    active = [pr for pr in rows if pr.is_active_finalizer]
    waiting = [pr for pr in rows if pr.is_waiting]
    leased = [pr for pr in active if pr.active_owner is not None]

    if len(leased) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in sorted(leased, key=lambda row: row.number))
        raise LaneConflictError(f"multiple migration finalizers still have active leases: {numbers}")

    if leased:
        holder = leased[0]
    elif active:
        holder = min(
            active,
            key=lambda pr: (
                STAGE_PRIORITY.get(pr.stage or "", len(STAGE_PRIORITY)),
                pr.created_at,
                pr.number,
            ),
        )
    else:
        holder = None

    if holder is not None:
        park = tuple(sorted(pr.number for pr in active if pr.number != holder.number))
        return holder.number, park, None

    if waiting:
        release = min(waiting, key=lambda pr: (pr.created_at, pr.number))
        return None, (), release.number

    return None, (), None


def run_gh(args: list[str], *, input_json: object | None = None, check: bool = True) -> str:
    command = ["gh", *args]
    try:
        proc = subprocess.run(
            command,
            input=None if input_json is None else json.dumps(input_json),
            text=True,
            capture_output=True,
            check=False,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out") from exc
    if check and proc.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def gh_json(args: list[str], *, input_json: object | None = None) -> object | None:
    output = run_gh(args, input_json=input_json)
    return json.loads(output) if output.strip() else None


def flatten_pages(value: object | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return result
    for page in value:
        if isinstance(page, dict):
            result.append(page)
        elif isinstance(page, list):
            result.extend(item for item in page if isinstance(item, dict))
    return result


def ensure_wait_label() -> None:
    """Ensure the auxiliary wait marker exists without treating it as a stage."""
    probe = run_gh(["api", f"repos/{REPO}/labels/{WAIT_LABEL}"], check=False)
    if probe.strip():
        return
    run_gh(
        ["api", "--method", "POST", f"repos/{REPO}/labels", "--input", "-"],
        input_json={
            "name": WAIT_LABEL,
            "color": "D4C5F9",
            "description": "Migration PR waiting for the serialized finalization lane",
        },
        check=False,
    )
    # A concurrent reconciler may have won the label-create race. Re-read the
    # authoritative label instead of treating a duplicate-create response as fatal.
    run_gh(["api", f"repos/{REPO}/labels/{WAIT_LABEL}"])


def open_pull_requests() -> list[dict[str, Any]]:
    pages = gh_json(
        ["api", "--paginate", "--slurp", f"repos/{REPO}/pulls?state=open&per_page=100"]
    )
    return flatten_pages(pages)


def pr_changed_files(pr_number: int) -> list[dict[str, Any]]:
    pages = gh_json(
        ["api", "--paginate", "--slurp", f"repos/{REPO}/pulls/{pr_number}/files?per_page=100"]
    )
    return flatten_pages(pages)


def current_migration_prs() -> list[MigrationPr]:
    """Load only factory PRs currently relevant to migration finalization."""
    result: list[MigrationPr] = []
    for pr in open_pull_requests():
        labels = label_names(pr.get("labels"))
        if "factory" not in labels:
            continue
        if bool(pr.get("draft")):
            continue
        if not ((set(labels) & STAGE_LABELS) or WAIT_LABEL in labels):
            continue
        number = int(pr.get("number") or 0)
        if number <= 0:
            continue
        files = pr_changed_files(number)
        if not any(changed_file_is_migration(row) for row in files):
            continue
        head = pr.get("head") or {}
        branch = str(head.get("ref") or "") if isinstance(head, dict) else ""
        result.append(
            MigrationPr(
                number=number,
                created_at=str(pr.get("created_at") or ""),
                labels=labels,
                branch=branch,
            )
        )
    return result


def target_json(number: int) -> dict[str, Any]:
    value = gh_json(["api", f"repos/{REPO}/issues/{number}"])
    if not isinstance(value, dict):
        raise RuntimeError(f"GitHub target #{number} payload was unavailable")
    return value


def replace_labels(number: int, labels: set[str]) -> None:
    run_gh(
        ["api", "--method", "PUT", f"repos/{REPO}/issues/{number}/labels", "--input", "-"],
        input_json={"labels": sorted(labels)},
    )


def linked_issue(branch: str) -> int | None:
    match = BRANCH_ISSUE_RE.match(branch)
    return int(match.group(1)) if match else None


def factory_labels_for_wait(current: set[str]) -> set[str]:
    labels = {
        label
        for label in current
        if not OWNER_RE.fullmatch(label) and label not in STAGE_LABELS and label != "factory"
    }
    labels.update({"factory", "factory:unowned", "factory:blocked", WAIT_LABEL})
    return labels


def factory_labels_for_review(current: set[str]) -> set[str]:
    labels = {
        label
        for label in current
        if not OWNER_RE.fullmatch(label)
        and label not in STAGE_LABELS
        and label not in {"factory", WAIT_LABEL}
    }
    labels.update({"factory", "factory:unowned", "factory:review"})
    return labels


def issue_labels_for_wait(current: set[str]) -> set[str]:
    labels = {
        label
        for label in current
        if not OWNER_RE.fullmatch(label)
        and label not in STAGE_LABELS
        and label != "factory"
        and not RALPH_STATUS_RE.match(label)
    }
    labels.update(
        {
            "factory",
            "factory:unowned",
            "factory:blocked",
            WAIT_LABEL,
            "ralph-status:blocked",
        }
    )
    return labels


def issue_labels_for_review(current: set[str]) -> set[str]:
    labels = {
        label
        for label in current
        if not OWNER_RE.fullmatch(label)
        and label not in STAGE_LABELS
        and label not in {"factory", WAIT_LABEL}
        and not RALPH_STATUS_RE.match(label)
    }
    labels.update(
        {
            "factory",
            "factory:unowned",
            "factory:review",
            "ralph-status:in-review",
        }
    )
    return labels


def transition_linked_issue(branch: str, *, waiting: bool) -> None:
    issue_number = linked_issue(branch)
    if issue_number is None:
        return
    issue = target_json(issue_number)
    if str(issue.get("state") or "").lower() != "open":
        return
    current = set(label_names(issue.get("labels")))
    if waiting:
        replace_labels(issue_number, issue_labels_for_wait(current))
    elif WAIT_LABEL in current:
        replace_labels(issue_number, issue_labels_for_review(current))


def park_pr(pr: MigrationPr) -> None:
    target = target_json(pr.number)
    current = set(label_names(target.get("labels")))
    replace_labels(pr.number, factory_labels_for_wait(current))
    transition_linked_issue(pr.branch, waiting=True)


def release_pr(pr: MigrationPr) -> None:
    target = target_json(pr.number)
    current = set(label_names(target.get("labels")))
    replace_labels(pr.number, factory_labels_for_review(current))
    transition_linked_issue(pr.branch, waiting=False)


def reconcile() -> dict[str, Any]:
    ensure_wait_label()
    migration_prs = current_migration_prs()
    by_number = {pr.number: pr for pr in migration_prs}
    holder, park, release = plan_lane(migration_prs)

    for number in park:
        pr = by_number[number]
        if pr.active_owner is not None:
            raise LaneConflictError(f"refusing to park actively leased migration PR #{number}")
        park_pr(pr)

    if release is not None:
        release_pr(by_number[release])
        holder = release

    waiting = sorted(
        {
            *(pr.number for pr in migration_prs if pr.is_waiting and pr.number != release),
            *park,
        }
    )
    result = {
        "holder": holder,
        "parked": list(park),
        "released": release,
        "waiting": waiting,
    }
    print(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("reconcile",))
    args = parser.parse_args()
    if args.command == "reconcile":
        reconcile()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LaneConflictError as exc:
        print(f"migration finalization lane conflict: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
