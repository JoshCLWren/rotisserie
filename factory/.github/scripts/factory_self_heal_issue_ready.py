#!/usr/bin/env python3
"""A PR stage label on an issue mirrors an open linked PR in that stage.

The review controller copies a PR's stage onto its linked issue. When the PR
merges without closing the issue (a ``Part of`` slice, a person's earlier
reopen, an acceptance hold) or closes, the label stays. ``factory:ready`` then
reads as finished, so the issue never gets more work, and ``factory:review``,
``factory:ci`` or ``factory:changes-requested`` point at a PR that no longer
exists. This pass drops ``factory:ready`` once no open linked PR is ready, and
the other PR stages once no open linked PR exists. ``factory:building`` is the
issue's own stage before a PR opens, so it stays. Eligibility then decides: a
manual-only, parent, claimed or blocked issue stays held.

Writes share the sweep's action budget.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import factory_self_heal as heal
from factory_issue_intake_guard import sliced_issue
from factory_work_policy_base import linked_issue_from_pr

READY = "factory:ready"
PR_STAGES = ("factory:review", "factory:ci", "factory:changes-requested")
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
    """Drop PR stage labels from open issues that no matching open PR delivers."""
    delivering = {_linked(pull) for pull in pulls if READY in heal.labels(pull)}
    linked = {_linked(pull) for pull in pulls}
    done: list[dict[str, Any]] = []
    for label in (READY, *PR_STAGES):
        keep = delivering if label == READY else linked
        rows = github.api(f"issues?labels={quote(label)}&state=open&per_page=100")
        for issue in rows if isinstance(rows, list) else []:
            if not isinstance(issue, Mapping) or "pull_request" in issue:
                continue
            number = int(issue["number"])
            if number in keep:
                continue
            if budget.dry_run or not budget.take():
                done.append({"issue": number, "label": label, "status": "would-drop"})
                continue
            github.api(f"issues/{number}/labels/{quote(label)}", "--method", "DELETE")
            done.append(
                {"kind": KIND, "issue": number, "label": label,
                 "text": f"dropped {label} from #{number}"}
            )
    return done
