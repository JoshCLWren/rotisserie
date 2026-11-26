#!/usr/bin/env python3
"""A parked PR is a failed attempt: the sweep closes it and requeues its issue.

A PR the loop breaker parked, still at a parked head, is closed in the same
sweep. The closing comment gives the park's findings and the last review, the
branch stays, and the linked issue goes back to the queue with a link to the
closed PR. The second closed attempt on one issue labels it ``umbrella``, so the
split sweep (``factory_self_heal_split``) breaks it into smaller issues instead
of sending a third worker at the same task. An issue that cannot be split (a
split child, an epic or PRD, an acceptance parent) is requeued as before.

A push ends the park by itself, because a park holds only its parked heads.
The exemptions of the park itself apply: owner override or claim, an outside
agent, an active incident, a live lease. Reopening a closed PR undoes the
closure; the factory never closes the same park twice.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import factory_item_loop as loop
import factory_self_heal as heal
import factory_self_heal_rules as rules
import factory_turso_loops as turso_loops
from factory_issue_intake_guard import sliced_issue
from factory_work_policy_base import linked_issue_from_pr

MAX_PER_SWEEP = 3
SPLIT_AFTER = 2
SPLIT_LABEL = "umbrella"
NOTE_MARKER = "factory-park-closed-v1"
NOTE_RE = re.compile(rf"{NOTE_MARKER}:issue-(?P<issue>\d+):pr-(?P<pr>\d+):park-")


def closures_statement(repo: str) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT created_at, detail ->> 'pr' AS pr, detail ->> 'kind' AS kind,"
        " detail ->> 'park' AS park, detail ->> 'text' AS text FROM factory_state_events"
        " WHERE repo = :repo AND event_type = 'factory_self_heal'"
        " AND detail ->> 'kind' IN (:closed, :parked)"
        " AND created_at > unixepoch() - :lookback",
        {
            "repo": repo,
            "closed": rules.PARK_CLOSED,
            "parked": rules.LOOP_PARKED,
            "lookback": turso_loops.PARK_LOOKBACK_SECONDS,
        },
    )


def evidence(repo: str) -> dict[str, Any]:
    """Parks, closures and park texts."""
    import factory_turso_core as core

    park_rows, rows = core.store().reads(
        [turso_loops.parks_statement(repo), closures_statement(repo)]
    )
    closed: set[tuple[int, int]] = set()
    texts: dict[int, str] = {}
    for row in sorted(rows, key=lambda row: int(row["created_at"])):
        if not str(row["pr"] or "").isdecimal():
            continue
        pr = int(row["pr"])
        if row["kind"] == rules.LOOP_PARKED:
            texts[pr] = str(row["text"] or "")
        else:
            closed.add((pr, int(row["park"] or 0)))
    return {"parks": turso_loops.parks(park_rows), "closed": closed, "texts": texts}


def runtime_model(source: str, model: str) -> str:
    """The dispatcher's provider/model identifier, or "" when this source has none."""
    from factory_manual_model import ManualModelUnavailable, runtime_prefix, validate_model_syntax

    try:
        prefix = runtime_prefix(source)
        name = model if model.startswith(prefix) else f"{prefix}{model}"
        validate_model_syntax(source, name)
    except ManualModelUnavailable:
        return ""
    return name


def _number(row: Mapping[str, Any]) -> int:
    value = row.get("number")
    return value if isinstance(value, int) else 0


def _head(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("sha") or "") if isinstance(head, Mapping) else ""


def _branch(row: Mapping[str, Any]) -> str:
    head = row.get("head")
    return str(head.get("ref") or "") if isinstance(head, Mapping) else ""


def last_review(comments: Sequence[Mapping[str, Any]]) -> str:
    """The newest trusted semantic review on the PR, trimmed, or a note that none exists."""
    import factory_review_policy as policy

    for body in reversed(policy.controller_comment_bodies(comments)):
        first = body.splitlines()[0] if body else ""
        found = policy.parse_review_marker(first)
        if found:
            excerpt = " ".join(body.split("\n", 1)[-1].split())[:1200]
            return f"{found['verdict']} at `{found['head'][:12]}`: {excerpt}"
    return "none recorded"


def closing_text(pull: Mapping[str, Any], park: str, review: str) -> str:
    pr = _number(pull)
    return (
        f"Closed #{pr} as a failed attempt: {park or 'its workers looped'}. Last review:"
        f" {review}. The branch `{_branch(pull)}` stays at `{_head(pull)[:12]}`; the linked"
        " issue is back in the queue with this PR as context. Reopen this PR to undo."
    )


def earlier_attempts(comments: Sequence[Mapping[str, Any]], issue: int, pr: int) -> set[int]:
    """Other PRs the factory already closed as failed attempts on this issue."""
    found = set()
    for comment in comments:
        for match in NOTE_RE.finditer(str(comment.get("body") or "")):
            if int(match["issue"]) == issue and int(match["pr"]) != pr:
                found.add(int(match["pr"]))
    return found


def split_labels(row: Mapping[str, Any], labels: set[str]) -> set[str] | None:
    """The requeued labels plus ``umbrella`` when the split sweep would take the issue."""
    from factory_self_heal_split import splittable

    wanted = labels | {SPLIT_LABEL}
    candidate = {**row, "labels": [{"name": name} for name in wanted]}
    return wanted if splittable(candidate) else None


def requeue(github: heal.GitHub, issue: int, pull: Mapping[str, Any], park: int) -> str:
    from factory_adopter import MARKER_PREFIX
    from factory_no_progress_retirement import requeued_labels

    row = github.api(f"issues/{issue}")
    if not isinstance(row, Mapping) or row.get("state") != "open":
        return "closed"
    pr = _number(pull)
    comments = github.comments(issue)
    tries = len(earlier_attempts(comments, issue, pr)) + 1
    labels = requeued_labels(heal.labels(row))
    split = split_labels(row, labels) if labels is not None and tries >= SPLIT_AFTER else None
    marker = f"<!-- {MARKER_PREFIX}-{NOTE_MARKER}:issue-{issue}:pr-{pr}:park-{park} -->"
    if not any(marker in str(c.get("body") or "") for c in comments):
        note = (
            f"{marker}\n\n**Factory:** PR #{pr} ({pull.get('html_url') or ''}) was closed as a"
            " failed attempt. Read that PR and its last review before you start, and reuse"
            f" what applies: branch `{_branch(pull)}`, head `{_head(pull)}`."
        )
        if split is not None:
            note += (
                f" This issue now has {tries} failed attempts, so the factory labels it"
                f" `{SPLIT_LABEL}` and splits it into smaller issues."
            )
        github.api(f"issues/{issue}/comments", "--method", "POST", payload={"body": note})
    if labels is None:
        return "owned"
    github.api(
        f"issues/{issue}/labels", "--method", "PUT", payload={"labels": sorted(split or labels)}
    )
    return "split" if split is not None else "requeued"


def close(
    github: heal.GitHub,
    store: heal.Store,
    pull: Mapping[str, Any],
    found: Mapping[str, Any],
    park: int,
) -> dict[str, Any]:
    """Comment, hand the issue back and close; the store event comes last.

    Each step before the close is safe to repeat, so a failed pass is finished by
    the next one. A closure already in the store means an owner reopened the PR.
    """
    pr, head = _number(pull), _head(pull)
    comments = github.comments(pr)
    text = closing_text(pull, found["texts"].get(pr, ""), last_review(comments))
    ref = f"closed-{park}"
    posted = any(
        heal_row["kind"] == rules.PARK_CLOSED and heal_row["ref"] == ref
        for heal_row in rules.heals(comments, pr=pr)
    )
    if not posted:
        body = rules.comment_body(kind=rules.PARK_CLOSED, pr=pr, head=head, ref=ref, text=text)
        github.api(f"issues/{pr}/comments", "--method", "POST", payload={"body": body})
    shape = {"headRefName": _branch(pull), "body": pull.get("body"), "title": pull.get("title")}
    issue = linked_issue_from_pr(shape) or sliced_issue(shape)
    handoff = requeue(github, issue, pull, park) if issue else "no linked issue"
    github.api(f"pulls/{pr}", "--method", "PATCH", payload={"state": "closed"})
    store.record(
        [heal.event(kind=rules.PARK_CLOSED, pr=pr, head=head, ref=ref, text=text, park=park)]
    )
    return {
        "kind": rules.PARK_CLOSED,
        "pr": pr,
        "issue": issue,
        "issue_status": handoff,
        "text": text,
    }


def exempt(store: heal.Store, pull: Mapping[str, Any], incidents: frozenset[int]) -> str:
    names = heal.labels(pull)
    if loop.bypassed(names):
        return "owner"
    if any(name.startswith("agent:") for name in names):
        return "outside agent"
    if _number(pull) in incidents:
        return "incident"
    return store.holder(_number(pull)) or ""


def resolve_long_parks(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Close each parked PR as a failed attempt and requeue its issue."""
    if not store.enabled():
        return []
    found = evidence(github.repo)
    due = []
    for pull in pulls:
        park = found["parks"].get(f"pr:{_number(pull)}")
        if park and _head(pull) in turso_loops.parked_heads(park):
            due.append((pull, int(park["at"].timestamp())))
    if not due:
        return []
    from factory_self_heal_loops import incident_pulls

    incidents = incident_pulls(github, pulls)
    done: list[dict[str, Any]] = []
    taken = 0
    for pull, park in due:
        pr = _number(pull)
        why = exempt(store, pull, incidents)
        if why:
            done.append({"pr": pr, "status": why})
            continue
        if (pr, park) in found["closed"]:
            done.append({"pr": pr, "status": "reopened after closure"})
            continue
        if budget.dry_run or taken >= MAX_PER_SWEEP or not budget.take():
            done.append({"pr": pr, "status": "would-close"})
            continue
        taken += 1
        done.append(close(github, store, pull, found, park))
    return done
