"""What happened to one issue or PR: label changes, close/reopen and factory markers."""

from __future__ import annotations

import json
import re
from typing import Any

from rotisserie.factory_console.github import GitHub, obj, string
from rotisserie.factory_console.service import claimops

TYPES = "[LABELED_EVENT, UNLABELED_EVENT, CLOSED_EVENT, REOPENED_EVENT, ISSUE_COMMENT]"
ITEMS = f"state timelineItems(last: $last, itemTypes: {TYPES}) {{ totalCount nodes {{ ...node }} }}"
QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $last: Int!) {{
  repository(owner: $owner, name: $name) {{
    issueOrPullRequest(number: $number) {{
      ... on Issue {{ {ITEMS} }}
      ... on PullRequest {{ {ITEMS} }}
    }}
  }}
}}
fragment node on Node {{
  __typename
  ... on LabeledEvent {{ createdAt actor {{ login }} label {{ name }} }}
  ... on UnlabeledEvent {{ createdAt actor {{ login }} label {{ name }} }}
  ... on ClosedEvent {{ createdAt actor {{ login }}
    closer {{ __typename ... on PullRequest {{ number }} ... on Commit {{ oid }} }} }}
  ... on ReopenedEvent {{ createdAt actor {{ login }} }}
  ... on IssueComment {{ createdAt author {{ login }} body }}
}}
"""
MARKER = re.compile(r"<!--\s*([a-z0-9-]+-v\d+)(?::([^>]*?))?\s*-->")
GITHUB_EVENT_CAP = 300


def _event(node: dict[str, Any]) -> dict[str, object] | None:
    kind = string(node, "__typename")
    at = string(node, "createdAt")
    actor = string(obj(node.get("actor") or node.get("author")), "login")
    if kind in ("LabeledEvent", "UnlabeledEvent"):
        sign = "+" if kind == "LabeledEvent" else "-"
        name = string(obj(node.get("label")), "name")
        return {"at": at, "actor": actor, "kind": "label", "detail": f"{sign}{name}"}
    if kind == "ClosedEvent":
        closer = obj(node.get("closer"))
        by = ""
        if string(closer, "__typename") == "PullRequest":
            by = f" by PR #{closer.get('number')}"
        elif string(closer, "__typename") == "Commit":
            by = f" by commit {string(closer, 'oid')[:8]}"
        return {"at": at, "actor": actor, "kind": "closed", "detail": f"closed{by}"}
    if kind == "ReopenedEvent":
        return {"at": at, "actor": actor, "kind": "reopened", "detail": "reopened"}
    if kind == "IssueComment":
        body = string(node, "body")
        found = MARKER.search(body)
        if found:
            fields = (found.group(2) or "").strip()
            return {
                "at": at,
                "actor": actor,
                "kind": "marker",
                "detail": f"{found.group(1)} {fields}".strip(),
            }
        first = " ".join(body.split())
        return {"at": at, "actor": actor, "kind": "comment", "detail": first[:120]}
    return None


def history(github: GitHub, target: str, *, limit: int = 60) -> dict[str, object]:
    """The item's recent timeline, oldest first, plus its label-claim controller runs."""
    kind, number = claimops.parse_target(target)
    owner, name = github.repo.split("/", 1)
    raw = github.gh(
        "api",
        "graphql",
        "-f",
        f"query={QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={number}",
        "-F",
        f"last={limit}",
    )
    item = obj(
        obj(obj(obj(json.loads(raw)).get("data")).get("repository")).get("issueOrPullRequest")
    )
    timeline = obj(item.get("timelineItems"))
    nodes = timeline.get("nodes")
    events = [
        event
        for node in (nodes if isinstance(nodes, list) else [])
        if isinstance(node, dict) and (event := _event(node)) is not None
    ]
    notes: list[str] = []
    total = timeline.get("totalCount")
    if isinstance(total, int) and total >= GITHUB_EVENT_CAP:
        notes.append(
            f"GitHub lists {total} timeline items here; it stops recording label "
            "changes on very busy items, so the newest label changes may be missing."
        )
    return {
        "target": target,
        "state": string(item, "state").lower(),
        "events": events,
        "controller_runs": claimops.item_runs(github, number, limit=5),
        "notes": notes,
    }
