#!/usr/bin/env python3
"""A PR stage label on an issue mirrors an open linked PR in that stage.

The review controller copies a PR's stage onto its linked issue. When the PR
merges without closing the issue (a ``Part of`` slice, a person's earlier
reopen, an acceptance hold) or closes, the label stays. ``factory:ready`` then
reads as finished, so the issue never gets more work, and ``factory:review``,
``factory:ci`` or ``factory:changes-requested`` point at a PR that no longer
exists. This pass drops ``factory:ready`` once no open linked PR is ready (but
first closes the issue through the normal post-merge closure when a merged PR
already says it closes it, so a delivered issue is never handed back), and
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


CLOSING_PRS = (
    "query($o:String!,$n:String!,$i:Int!){repository(owner:$o,name:$n){issue(number:$i)"
    "{closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number merged}}}}}"
)


def _merged_closers(github: heal.GitHub, number: int) -> list[int]:
    owner, name = github.repo.split("/", 1)
    data = github.graphql(CLOSING_PRS, o=owner, n=name, i=number)
    issue = ((data.get("data") or {}).get("repository") or {}).get("issue") or {}
    nodes = (issue.get("closedByPullRequestsReferences") or {}).get("nodes") or []
    return [int(node["number"]) for node in nodes if node.get("merged")]


def _closed_by_merge(github: heal.GitHub, number: int, dry_run: bool) -> str:
    """Run the normal post-merge closure for a merged PR that says it closes the issue."""
    from factory_post_merge_closure import decide

    for pr in _merged_closers(github, number):
        outcome, _line = decide(pr, dry_run=dry_run, require_quiescent=True)
        if outcome in ("closed", "already-closed") or outcome.startswith("dry-run:"):
            return f"closed by merged #{pr}"
    return ""


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
            if label == READY:
                closed = _closed_by_merge(github, number, budget.dry_run)
                if closed:
                    done.append({"kind": KIND, "issue": number, "label": label, "text": closed})
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
