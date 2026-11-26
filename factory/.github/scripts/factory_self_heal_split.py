#!/usr/bin/env python3
"""Oversized issues split themselves: dispatch, create the children, track the parent.

An issue the intake guard labeled ``factory:needs-split`` (over the body limit,
or labeled umbrella or tracking), or a ``factory`` issue labeled ``umbrella``
even after a merged slice made it ``factory:ready``, gets one ``split`` run on the model with
the best track record for splits, through the dispatcher's targeted path
(``target=split:N``). The trusted Entry persister records the validated plan in
the store once per parent (``factory_split_plan``). A run whose plan failed
validation (or that wrote none) is retried at once, and its reason is a comment
the next run reads; a run that crashed or hit a provider failure is retried
after ``RETRY_SECONDS``. This sweep step then:

* creates the children in blocker order, each with ``factory``,
  ``factory:unowned``, a ``Part of #PARENT.`` line and a ``Blocked by:`` list of
  its siblings. GitHub has no transactions, so every created child is a store
  event, and a later pass resumes the same plan: it adopts a child it created
  but did not record (by the body marker) before it creates a new one;
* writes ``parent-child`` and sibling ``blocks`` links to ``factory_deps``,
  labels the parent ``tracking`` and posts the checklist with the split-done
  marker. The store plan keeps the parent from being split again, and a child
  is never split (depth 1);
* closes the parent once every child closed as completed. A child closed as not
  planned is the owner's undo: the parent stays open.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import factory_self_heal as heal
import factory_split_plan as plans
from factory_adopter import MARKER_PREFIX

DISPATCH_WORKFLOW = "fixed-model-factory-dispatch.yml"
RETRY_SECONDS = 6 * 3600
MAX_DISPATCHES = plans.MAX_DISPATCHES
TRACKING = "tracking"
EVENT = "factory_split"
LISTINGS = ("factory%3Aneeds-split", "factory%2Cumbrella")


def statement(repo: str) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT created_at, detail ->> 'kind' AS kind, detail ->> 'issue' AS issue,"
        " detail ->> 'index' AS idx, detail ->> 'child' AS child, detail ->> 'plan' AS plan"
        " FROM factory_state_events WHERE repo = :repo AND event_type = :event",
        {"repo": repo, "event": EVENT},
    )


def state(repo: str) -> dict[int, dict[str, Any]]:
    """Per parent: its plan, created children, dispatch times and how far it got."""
    import json

    import factory_turso_core as core

    rows = core.store().reads([statement(repo)])[0]
    found: dict[int, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda row: int(row["created_at"])):
        if not str(row["issue"] or "").isdecimal():
            continue
        entry = found.setdefault(
            int(row["issue"]),
            {
                "plan": None,
                "at": None,
                "children": {},
                "dispatches": [],
                "rejected": [],
                "done": False,
                "closed": False,
            },
        )
        at = datetime.fromtimestamp(int(row["created_at"]), UTC)
        kind = row["kind"]
        if kind == "plan":
            plan = row["plan"]
            entry["plan"] = json.loads(plan) if isinstance(plan, str) else plan
            entry["at"] = at
        elif kind == "child":
            entry["children"][int(row["idx"])] = int(row["child"])
        elif kind == "dispatch":
            entry["dispatches"].append(at)
        elif kind == "rejected":
            entry["rejected"].append(at)
        elif kind in ("done", "closed"):
            entry[kind] = True
    return found


def event(kind: str, issue: int, ref: str, **detail: object) -> dict[str, Any]:
    return {
        "event_type": EVENT,
        "event_id": f"split:{kind}:{issue}:{ref}",
        "worker": "0",
        "kind": kind,
        "issue": issue,
        **detail,
    }


def dispatch_inputs(parent: int) -> tuple[dict[str, str], str] | None:
    import factory_model_track_record as track
    from factory_self_heal_park_retry import runtime_model

    found = track.best("split")
    if found is None:
        return None
    source, model, why = found
    name = runtime_model(source, model)
    if not name:
        return None
    inputs = {
        "mode": "smoke",
        "action": "advance",
        "provider": source,
        "model": name,
        "force_slot": "true",
        "target": f"split:{parent}",
    }
    return inputs, f"{name} ({why})"


def splittable(issue: Mapping[str, Any]) -> bool:
    """Held for its size or label, and no one else's: a merged slice (``factory:ready``) is fine.

    A child, an epic or PRD, an acceptance parent and an issue that already
    lists its children (``- [ ] #N``) are never split.
    """
    from factory_issue_intake_guard import split_reason
    from factory_work_policy_base import eligibility, item_is_unowned

    import factory_state_labels as labels

    names = heal.labels(issue)
    body = str(issue.get("body") or "")
    if "pull_request" in issue or "factory" not in names or labels.BYPASS & names:
        return False
    if plans.is_child(body) or names & {"epic", "prd"} or split_reason(issue) is None:
        return False
    if eligibility.is_manual_only(body) or eligibility.is_acceptance_parent(body):
        return False
    if eligibility.ACCEPTANCE_PARENT_CHILD_RE.search(body):
        return False
    return item_is_unowned(names)


def dispatch_splits(
    github: heal.GitHub,
    store: heal.Store,
    found: Mapping[int, Mapping[str, Any]],
    budget: Any,
    now: datetime,
) -> list[dict[str, Any]]:
    """One split run per pass, for the oldest held issue with no plan."""
    listed: dict[int, Mapping[str, Any]] = {}
    for query in LISTINGS:
        rows = github.api(f"issues?labels={query}&state=open&per_page=50")
        listed |= {int(row["number"]): row for row in (rows if isinstance(rows, list) else [])}
    for number, issue in sorted(listed.items()):
        entry = found.get(number, {})
        if entry.get("plan") or not splittable(issue):
            continue
        tries = entry.get("dispatches", [])
        rejected = entry.get("rejected", [])
        answered = bool(tries) and len(rejected) >= len(tries)
        if len(tries) >= MAX_DISPATCHES or (
            tries and not answered and now - tries[-1] < timedelta(seconds=RETRY_SECONDS)
        ):
            continue
        chosen = dispatch_inputs(number)
        if chosen is None:
            return [{"issue": number, "status": "no proven model for split"}]
        if budget.dry_run or not budget.take():
            return [{"issue": number, "status": "would-dispatch"}]
        inputs, model = chosen
        if not store.record([event("dispatch", number, str(len(tries) + 1), model=model)]):
            return []
        github.api(
            f"actions/workflows/{DISPATCH_WORKFLOW}/dispatches",
            "--method",
            "POST",
            payload={"ref": "main", "inputs": inputs},
        )
        return [{"issue": number, "status": "split run dispatched", "model": model}]
    return []


def _adoptable(github: heal.GitHub, since: datetime) -> dict[str, int]:
    """Issues the factory created since the plan, by child marker."""
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = github.api(
        f"issues?creator=github-actions%5Bbot%5D&state=all&since={stamp}&per_page=100"
    )
    found: dict[str, int] = {}
    for row in rows if isinstance(rows, list) else []:
        first = str(row.get("body") or "").split("\n", 1)[0]
        if plans.is_child(first):
            found[first] = int(row["number"])
    return found


def create_children(
    github: heal.GitHub, store: heal.Store, parent: int, entry: Mapping[str, Any], budget: Any
) -> dict[str, Any]:
    plan = entry["plan"]
    children = plan["children"]
    numbers: dict[int, int] = dict(entry["children"])
    adoptable: dict[str, int] | None = None
    for index in plan["order"]:
        if index in numbers:
            continue
        child = children[index - 1]
        body = plans.compose(parent, index, child, numbers)
        plans.check_body(body, {numbers[b] for b in child["blocked_by"]}, plans.body_limit())
        if adoptable is None:
            adoptable = _adoptable(github, entry["at"] - timedelta(minutes=5))
        number = adoptable.get(plans.child_marker(parent, index))
        if number is None:
            if budget.dry_run or not budget.take():
                return {"issue": parent, "status": "would-create", "created": len(numbers)}
            row = github.api(
                "issues",
                "--method",
                "POST",
                payload={
                    "title": child["title"],
                    "body": body,
                    "labels": ["factory", "factory:unowned"],
                },
            )
            number = int(row["number"])
        store.record([event("child", parent, str(index), index=index, child=number)])
        numbers[index] = number
    return finish_parent(github, store, parent, plan, numbers)


def write_links(repo: str, parent: int, plan: Mapping[str, Any], numbers: Mapping[int, int]) -> None:
    """Parent-child and sibling ``blocks`` links in the dependency store; safe to repeat."""
    import factory_deps as deps

    for index, child in enumerate(plan["children"], start=1):
        deps.add(repo, parent, numbers[index], "parent-child")
        for blocker in child["blocked_by"]:
            deps.add(repo, numbers[blocker], numbers[index], "blocks")


def done_marker(parent: int) -> str:
    return f"<!-- {MARKER_PREFIX}-{plans.DONE_MARKER}:issue-{parent} -->"


def finish_parent(
    github: heal.GitHub,
    store: heal.Store,
    parent: int,
    plan: Mapping[str, Any],
    numbers: Mapping[int, int],
) -> dict[str, Any]:
    write_links(github.repo, parent, plan, numbers)
    marker = done_marker(parent)
    if not any(marker in str(c.get("body") or "") for c in github.comments(parent)):
        items = "\n".join(
            f"- [ ] #{numbers[i]} {child['title']}"
            for i, child in enumerate(plan["children"], start=1)
        )
        body = (
            f"{marker}\n\n**Factory split:** issue #{parent} is now tracked through"
            f" {len(numbers)} child issues, created from a split run. The factory works"
            " the children and closes this issue when every child is completed. To undo"
            f" the split, close the children.\n\n{items}\n"
        )
        github.api(f"issues/{parent}/comments", "--method", "POST", payload={"body": body})
    github.api(f"issues/{parent}/labels", "--method", "POST", payload={"labels": [TRACKING]})
    store.record([event("done", parent, "1", children=sorted(numbers.values()))])
    return {"issue": parent, "status": "split done", "children": sorted(numbers.values())}


def close_parent(
    github: heal.GitHub, store: heal.Store, parent: int, entry: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Close the parent when every child closed as completed; None while any is open."""
    children = sorted(entry["children"].values())
    fields = " ".join(f"c{n}: issue(number: {n}) {{ state stateReason }}" for n in children)
    owner, name = github.repo.split("/", 1)
    data = github.graphql(
        f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
    )
    found = ((data.get("data") or {}).get("repository") or {}).values()
    if len(found) != len(children) or any(row.get("state") != "CLOSED" for row in found):
        return None
    if any(row.get("stateReason") != "COMPLETED" for row in found):
        store.record([event("closed", parent, "undone")])
        return {"issue": parent, "status": "split undone: a child closed as not planned"}
    github.api(
        f"issues/{parent}/comments",
        "--method",
        "POST",
        payload={
            "body": f"**Factory split:** every child of #{parent} is completed, so it is closed."
        },
    )
    github.api(
        f"issues/{parent}",
        "--method",
        "PATCH",
        payload={"state": "closed", "state_reason": "completed"},
    )
    store.record([event("closed", parent, "completed")])
    return {"issue": parent, "status": "closed: every child completed"}


def advance_splits(
    github: heal.GitHub,
    store: heal.Store,
    pulls: Sequence[Mapping[str, Any]],
    budget: Any,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Resume recorded plans, close finished parents, then dispatch one new split."""
    if not store.enabled():
        return []
    found = state(github.repo)
    done: list[dict[str, Any]] = []
    for parent, entry in sorted(found.items()):
        if entry["plan"] and not entry["done"]:
            done.append(create_children(github, store, parent, entry, budget))
        elif entry["done"] and not entry["closed"] and entry["children"]:
            row = close_parent(github, store, parent, entry)
            if row:
                done.append(row)
    done += dispatch_splits(github, store, found, budget, now or datetime.now(UTC))
    return done
