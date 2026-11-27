#!/usr/bin/env python3
"""Serialize final integration for factory PRs that change database migrations.

An adopter's migration family (``factory_migration_families``: Alembic by default,
Refinery or another declared in adopter config) needs one ordered history, so PRs
that change the same family cannot safely finish review/CI/merge in parallel. This
controller keeps ordinary implementation parallel, but parks all except one PR per
family once they reach the finalization stages that can lead to merge. A PR still
marked waiting that no longer changes a migration is released.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from factory_adopter import AdopterConfigError, repository
import factory_crash
import factory_migration_families as migration_families

REPO = repository()
GH_TIMEOUT_SECONDS = 120
WAIT_LABEL = "factory:migration-wait"
DEFAULT_FAMILY = "alembic"
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
OWNER_RE = re.compile(r"^factory:(?:unowned|local|[1-9][0-9]*)$")
ACTIVE_OWNER_RE = re.compile(r"^factory:(?:local|[1-9][0-9]*)$")
RALPH_STATUS_RE = re.compile(r"^ralph-status:")


class LaneConflictError(RuntimeError):
    """Raised when more than one migration finalizer still has an active lease."""


class GitHubQuotaDeferredError(RuntimeError):
    """GitHub rate-limited an authoritative lane read/write; retry later."""


@dataclass(frozen=True)
class MigrationPr:
    number: int
    created_at: str
    labels: frozenset[str]
    branch: str
    families: frozenset[str] = frozenset({DEFAULT_FAMILY})
    body: str = ""
    title: str = ""

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


@__import__("functools").cache
def declared() -> tuple[migration_families.Family, ...]:
    return migration_families.families()


def changed_families(row: dict[str, Any]) -> frozenset[str]:
    """The migration families a GitHub changed-file row touches, before or after a rename."""
    return frozenset(change.family for change in migration_families.changes(row, declared()))


def changed_file_is_migration(row: dict[str, Any]) -> bool:
    return bool(changed_families(row))


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


def plan_lanes(
    prs: Iterable[MigrationPr],
) -> tuple[dict[str, int], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """(holder per family, PRs to park, waiters to release, stale waiters) for one snapshot.

    Each family is its own lane. A waiter is released only when no other PR holds
    any family it touches; a waiter that touches no family any more is stale.
    """
    rows = list(prs)
    by_number = {pr.number: pr for pr in rows}
    holders: dict[str, int] = {}
    park: set[int] = set()
    freed: set[int] = set()
    for name in sorted({family for pr in rows for family in pr.families}):
        holder, parked, release = plan_lane(pr for pr in rows if name in pr.families)
        if holder is not None:
            holders[name] = holder
        park.update(parked)
        if release is not None:
            freed.add(release)
    released: list[int] = []
    for pr in sorted((by_number[n] for n in freed - park), key=lambda r: (r.created_at, r.number)):
        if all(holders.get(name) in (None, pr.number) for name in pr.families):
            holders.update(dict.fromkeys(pr.families, pr.number))
            released.append(pr.number)
    release = tuple(sorted(released))
    stale = tuple(sorted(pr.number for pr in rows if pr.is_waiting and not pr.families))
    return holders, tuple(sorted(park)), release, stale


def run_gh(args: list[str], *, input_json: object | None = None, check: bool = True) -> str:
    command, stdin = ["gh", *args], None if input_json is None else json.dumps(input_json)
    try:
        proc = factory_crash.gh_with_retry(command, lambda: subprocess.run(
            command, input=stdin, text=True, capture_output=True, check=False,
            timeout=GH_TIMEOUT_SECONDS,
        ), stdin=stdin)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out") from exc
    if check and proc.returncode:
        detail = proc.stderr.strip()
        if any(marker in detail.lower() for marker in (
            "rate limit exceeded", "secondary rate limit", "abuse detection"
        )):
            raise GitHubQuotaDeferredError("GitHub installation API quota exhausted")
        raise RuntimeError(f"{' '.join(command)} failed ({proc.returncode}): {detail}")
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
    from factory_discovery import discovered_pull_requests

    return discovered_pull_requests(REPO, lambda: flatten_pages(gh_json(
        ["api", "--paginate", "--slurp", f"repos/{REPO}/pulls?state=open&per_page=100"]
    )))


def pr_changed_files(pr_number: int) -> list[dict[str, Any]]:
    pages = gh_json(
        ["api", "--paginate", "--slurp", f"repos/{REPO}/pulls/{pr_number}/files?per_page=100"]
    )
    return flatten_pages(pages)


def touched_families(number: int, sha: str, memo: Any) -> frozenset[str]:
    """A head's changed files never change, so its families are kept by SHA."""
    known = memo.get(sha) if sha else None
    if isinstance(known, list):
        return frozenset(str(name) for name in known)
    found: frozenset[str] = frozenset().union(*map(changed_families, pr_changed_files(number)))
    if sha:
        memo.put(sha, sorted(found))
    return found


def current_migration_prs() -> list[MigrationPr]:
    """Load only factory PRs currently relevant to migration finalization."""
    from factory_head_memo import HeadMemo

    memo = HeadMemo(REPO, f"migration-families-{migration_families.fingerprint(declared())}")
    heads: set[str] = set()
    result: list[MigrationPr] = []
    for pr in open_pull_requests():
        labels = label_names(pr.get("labels"))
        if "factory" not in labels:
            continue
        if bool(pr.get("draft")):
            continue
        # Building/blocked PRs cannot occupy or wait for a finalization
        # slot unless explicitly marked as migration waiters. Avoid one
        # paginated changed-files read for every irrelevant factory PR.
        if not ((set(labels) & set(FINALIZATION_STAGES)) or WAIT_LABEL in labels):
            continue
        number = int(pr.get("number") or 0)
        if number <= 0:
            continue
        head = pr.get("head") or {}
        sha = str(head.get("sha") or "") if isinstance(head, dict) else ""
        heads.add(sha)
        families = touched_families(number, sha, memo)
        if not families and WAIT_LABEL not in labels:
            continue
        branch = str(head.get("ref") or "") if isinstance(head, dict) else ""
        result.append(
            MigrationPr(
                number=number,
                created_at=str(pr.get("created_at") or ""),
                labels=labels,
                branch=branch,
                families=families,
                body=str(pr.get("body") or ""),
                title=str(pr.get("title") or ""),
            )
        )
    memo.save(heads)
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


def linked_issue(pr: MigrationPr) -> int | None:
    """The issue the PR closes, as merge closure reads it; never a ``Part of`` parent."""
    from factory_merge_linkage import linkage

    return linkage(pr.branch, pr.body, pr.title).issue


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


def transition_linked_issue(pr: MigrationPr, *, waiting: bool) -> None:
    issue_number = linked_issue(pr)
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
    transition_linked_issue(pr, waiting=True)


def release_pr(pr: MigrationPr) -> None:
    target = target_json(pr.number)
    current = set(label_names(target.get("labels")))
    replace_labels(pr.number, factory_labels_for_review(current))
    transition_linked_issue(pr, waiting=False)


def reconcile(*, dry_run: bool = False) -> dict[str, Any]:
    migration_prs = current_migration_prs()
    by_number = {pr.number: pr for pr in migration_prs}
    holders, park, release, stale = plan_lanes(migration_prs)

    if not dry_run:
        # Only a genuine park transition needs a GitHub label-existence probe.
        if park:
            ensure_wait_label()
        for number in park:
            pr = by_number[number]
            if pr.active_owner is not None:
                raise LaneConflictError(f"refusing to park actively leased migration PR #{number}")
            park_pr(pr)
        for number in (*release, *stale):
            release_pr(by_number[number])

    freed = {*release, *stale}
    waiting = sorted(
        {*(pr.number for pr in migration_prs if pr.is_waiting and pr.number not in freed), *park}
    )
    distinct = sorted(set(holders.values()))
    result = {
        "holder": distinct[0] if len(distinct) == 1 else None,
        "lanes": dict(sorted(holders.items())),
        "parked": list(park),
        "released": release[0] if len(release) == 1 else (list(release) or None),
        "stale_released": list(stale),
        "waiting": waiting,
        "dry_run": dry_run,
    }
    print(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("reconcile",))
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    args = parser.parse_args()
    if args.command == "reconcile":
        reconcile(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    try:
        raise SystemExit(main())
    except GitHubQuotaDeferredError:
        # Keep migration PR owners and stages untouched until GitHub API reset.
        # Regular dispatcher/recovery schedules provide the next attempt.
        print(json.dumps({"status": "deferred", "reason": "github_api_rate_limit"}, sort_keys=True))
        raise SystemExit(0) from None
    except LaneConflictError as exc:
        print(f"migration finalization lane conflict: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except AdopterConfigError as exc:
        # Fail closed like a lane conflict: no migration PR moves until the config is fixed.
        print(f"::error::migration lane config: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
