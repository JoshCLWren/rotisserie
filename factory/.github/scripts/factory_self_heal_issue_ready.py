#!/usr/bin/env python3
"""``factory:ready`` on an issue mirrors an open linked PR at ``factory:ready``.

The review controller copies a PR's ready stage onto its linked issue at
approval. When the merge does not close the issue (a ``Part of`` slice, a
person's earlier reopen, an acceptance hold), the label stays, and the
dispatcher reads it as finished, so the issue never gets more work. This pass
drops it once no open linked PR is ready. Eligibility then decides: a
manual-only, parent, claimed or blocked issue stays held.

Writes share the sweep's action budget.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import factory_self_heal as heal
from factory_issue_intake_guard import sliced_issue
from factory_work_policy_base import linked_issue_from_pr

READY = "factory:ready"
KIND = "issue-ready-dropped"


def _linked(pull: Mapping[str, Any]) -> int | None:
    head = pull.get("head")
    shape = {
        "headRefName": str(head.get("ref") or "") if isinstance(head, Mapping) else "",
        "body": pull.get("body"),
        "title": pull.get("title"),
    }
    return linked_issue_from_pr(shape) or sliced_issue(shape)


def sync_issue_ready(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
) -> list[dict[str, Any]]:
    """Drop ``factory:ready`` from open issues that no open ready PR delivers."""
    rows = github.api("issues?labels=factory%3Aready&state=open&per_page=100")
    issues = [
        row for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, Mapping) and "pull_request" not in row
    ]
    if not issues:
        return []
    delivering = {_linked(pull) for pull in pulls if READY in heal.labels(pull)}
    done: list[dict[str, Any]] = []
    for issue in issues:
        number = int(issue["number"])
        if number in delivering:
            continue
        if budget.dry_run or not budget.take():
            done.append({"issue": number, "status": "would-drop"})
            continue
        github.api(f"issues/{number}/labels/factory%3Aready", "--method", "DELETE")
        done.append({"kind": KIND, "issue": number, "text": f"dropped {READY} from #{number}"})
    return done
