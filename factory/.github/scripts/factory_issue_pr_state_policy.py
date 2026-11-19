#!/usr/bin/env python3
"""Issue-state precedence, lease preservation, and skips for factory reconciliation."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from factory_work_policy import (
    BLOCKED_LABELS,
    OWNER_RE,
    is_manual_only,
    lease_is_stale,
)

# A history-derived stage rewrite must never erase a live lease. These names are
# the stable CLI contract consumed by the reconciler workflow.
LEASE_ACTION_RECONCILE = "reconcile"
LEASE_ACTION_HOLD_LOCAL = "hold-local-lease"
LEASE_ACTION_HOLD_NUMBERED = "hold-numbered-lease"
LEASE_ACTION_HOLD_AMBIGUOUS = "hold-ambiguous-lease"


def stage_from_trusted_prs(records: Iterable[Mapping[str, Any]]) -> str:
    """Derive issue lifecycle from trusted canonical PR history.

    Durable merged provenance permanently wins. Without a merged PR, any open
    trusted canonical PR owns the lifecycle. Only when every trusted attempt is
    closed unmerged does the issue return to claimable backlog state.
    """
    trusted = [record for record in records if bool(record.get("trusted"))]
    if any(bool(record.get("merged")) for record in trusted):
        return "factory:ready"
    if any(str(record.get("state") or "").upper() == "OPEN" for record in trusted):
        return "factory:review"
    return "claimable"


def issue_labels_block_reconciliation(labels: Iterable[str]) -> bool:
    """Return whether claim policy forbids rewriting this issue's factory labels.

    Reuses ``BLOCKED_LABELS`` from factory work policy so the scheduled
    reconciler leaves ``ralph-status:blocked`` (and the rest of that set)
    alone instead of restoring ``factory`` + ``factory:unowned``.
    """
    return bool(set(labels) & BLOCKED_LABELS)


def body_blocks_reconciliation(body: str | None) -> bool:
    """Return whether a body forbids rewriting this target's factory labels.

    Mirrors ``stale_pr_decay.process_stale_prs``: a standalone
    ``<!-- factory-execution:manual-only -->`` directive or a standalone
    ``manual-only`` line is an operator hold.
    ``gh issue list --label factory`` includes pull requests, so the scheduled
    reconciler must skip those PR bodies the same way stale-expiration does.
    """
    return is_manual_only(body)


def _label_names(decoded: object) -> list[str]:
    """Return string label names from a decoded JSON labels payload."""
    if not isinstance(decoded, list):
        raise TypeError("labels JSON must decode to a list")
    return [item for item in decoded if isinstance(item, str)]


def active_owner_labels(labels: Iterable[str]) -> list[str]:
    """Return canonical active factory owners, excluding ``factory:unowned``.

    ``factory:unowned`` is a released owner, not a lease, so it never blocks a
    history-derived rewrite. Multiple active owners are reported, not guessed.
    """
    return sorted(
        {label for label in labels if OWNER_RE.fullmatch(label) and label != "factory:unowned"}
    )


def _issue_label_names(issue: Mapping[str, Any]) -> list[str]:
    """Return string label names from a decoded GitHub issue payload."""
    names: list[str] = []
    for item in issue.get("labels") or []:
        name = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _timestamp_epoch(value: object) -> int | None:
    """Parse an ISO-8601 GitHub timestamp into an epoch, or return ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def lease_reconciliation_action(
    labels: Iterable[str],
    *,
    latest_activity_epoch: int | None,
    now_epoch: int,
    active_fixed_workers: set[int] | None = None,
    has_unresolved_active_runs: bool | None = None,
) -> str:
    """Decide whether a PR-history sweep may rewrite this target's factory state.

    The reconciler projects workflow stage from trusted PR history, but a live
    owner lease is authoritative next-action state written by the claiming
    worker. Replacing it would strip ``factory:local``/``factory:<n>`` and
    ``ralph-status:in-progress`` from an actively worked issue.

    ``factory:local`` follows the shared local lease TTL. A fresh or
    unknown-activity local lease is preserved; only a provably stale one falls
    through to history reconciliation. A numbered owner is preserved unless the
    caller supplies authoritative active-worker/run evidence, because this
    sweep has no such evidence and must not steal another worker's lease.
    """
    owners = active_owner_labels(labels)
    if not owners:
        return LEASE_ACTION_RECONCILE
    if len(owners) > 1:
        return LEASE_ACTION_HOLD_AMBIGUOUS
    owner = owners[0]
    if owner == "factory:local":
        stale = lease_is_stale(
            owner,
            active_fixed_workers=set(),
            latest_activity_epoch=latest_activity_epoch,
            now_epoch=now_epoch,
        )
        return LEASE_ACTION_RECONCILE if stale else LEASE_ACTION_HOLD_LOCAL
    if active_fixed_workers is None:
        return LEASE_ACTION_HOLD_NUMBERED
    stale = lease_is_stale(
        owner,
        active_fixed_workers=active_fixed_workers,
        has_unresolved_active_runs=has_unresolved_active_runs,
        latest_activity_epoch=latest_activity_epoch,
        now_epoch=now_epoch,
    )
    return LEASE_ACTION_RECONCILE if stale else LEASE_ACTION_HOLD_NUMBERED


def lease_action_for_issue(
    issue: Mapping[str, Any],
    *,
    now_epoch: int,
    active_fixed_workers: set[int] | None = None,
    has_unresolved_active_runs: bool | None = None,
) -> str:
    """Return the lease-preservation decision for a decoded GitHub issue payload.

    ``updated_at`` is the freshest cheap acquisition/activity signal available to
    the scheduled sweep: applying claim labels updates it. A missing or
    unparseable timestamp is unknown activity, which preserves a local lease.
    """
    latest_activity = _timestamp_epoch(issue.get("updated_at") or issue.get("updatedAt"))
    return lease_reconciliation_action(
        _issue_label_names(issue),
        latest_activity_epoch=latest_activity,
        now_epoch=now_epoch,
        active_fixed_workers=active_fixed_workers,
        has_unresolved_active_runs=has_unresolved_active_runs,
    )


def main() -> int:
    """Print derived issue stage, or whether labels block reconciliation."""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--records-json")
    mode.add_argument("--blocked-by-labels-json")
    mode.add_argument("--blocked-by-body-file")
    mode.add_argument("--lease-action-issue-file")
    parser.add_argument("--now-epoch", type=int)
    args = parser.parse_args()
    if args.blocked_by_labels_json is not None:
        try:
            labels = _label_names(json.loads(args.blocked_by_labels_json))
        except (TypeError, json.JSONDecodeError) as exc:
            parser.error(f"--blocked-by-labels-json must decode to a list: {exc}")
        print("blocked" if issue_labels_block_reconciliation(labels) else "ok")
        return 0
    if args.blocked_by_body_file is not None:
        body = Path(args.blocked_by_body_file).read_text(encoding="utf-8")
        print("blocked" if body_blocks_reconciliation(body) else "ok")
        return 0
    if args.lease_action_issue_file is not None:
        try:
            issue = json.loads(Path(args.lease_action_issue_file).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            parser.error(f"--lease-action-issue-file must decode to an object: {exc}")
        if not isinstance(issue, dict):
            parser.error("--lease-action-issue-file must decode to an object")
        now_epoch = args.now_epoch if args.now_epoch is not None else int(time.time())
        print(lease_action_for_issue(issue, now_epoch=now_epoch))
        return 0
    if args.records_json is None:
        parser.error("--records-json is required")
    records = json.loads(args.records_json)
    if not isinstance(records, list):
        parser.error("--records-json must decode to a list")
    print(stage_from_trusted_prs(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
