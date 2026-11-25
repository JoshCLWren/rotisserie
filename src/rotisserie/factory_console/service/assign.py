"""Hand one issue or pull request to an outside agent, and list who holds what.

``assign`` reuses the owner's label claim (``factory:claimed``), so factory
workers stay off the item, then marks the agent with an ``agent:NAME`` label
and posts the agent's task. ``finish`` removes the ``agent:*`` label again.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import quote

from rotisserie.factory_console.github import (
    JSON,
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    items,
    label_names,
    obj,
    string,
    timestamp,
)
from rotisserie.factory_console.service import assign_task, claimops
from rotisserie.factory_console.tracker import Clock

PREFIX = "agent:"
AGENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,38}$")
COLOR = "5319e7"
DESCRIPTION = "Held by an outside agent (baste assign); baste finish releases it"
MARKER = "<!-- rotisserie-agent-task:v1 agent={agent} task={digest} -->"
HELD = frozenset({"reserved", "already-held", "requested"})


@dataclass(frozen=True)
class Assignment:
    target: str
    title: str
    agent: str
    since: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def check_agent(name: str) -> str:
    agent = name.removeprefix(PREFIX).strip().lower()
    if not AGENT.fullmatch(agent):
        raise ValueError(
            "--agent must be a short name of letters, digits, '.', '_' or '-' (e.g. claude)"
        )
    return agent


def _labels(github: GitHub, number: int) -> set[str]:
    return label_names(obj(github.repo_api(f"issues/{number}")))


def _ensure_label(github: GitHub, name: str) -> bool:
    try:
        github.repo_api(f"labels/{quote(name, safe='')}")
        return False
    except GitHubNotFoundError:
        github.repo_api(
            "labels",
            method="POST",
            fields={"name": name, "color": COLOR, "description": DESCRIPTION},
        )
        return True


def remove_agent_labels(github: GitHub, number: int, *, keep: str = "") -> list[str]:
    removed = []
    for name in sorted(_labels(github, number)):
        if not name.startswith(PREFIX) or name == keep:
            continue
        try:
            github.repo_api(f"issues/{number}/labels/{quote(name, safe='')}", method="DELETE")
        except GitHubNotFoundError:
            continue
        removed.append(name)
    return removed


COMMENTS = "comments(last:100){nodes{body url}}"
RECENT = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{issueOrPullRequest(number:$number){... on Issue{" + COMMENTS + "}"
    "... on PullRequest{" + COMMENTS + "}}}}"
)


def _posted(github: GitHub, number: int, marker: str) -> str:
    """The newest comments, since factory PRs carry hundreds of bot comments."""
    owner, name = github.repo.split("/", 1)
    raw = github.gh(
        "api", "graphql", "-f", f"query={RECENT}",
        "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
    )  # fmt: skip
    try:
        found = json.loads(raw)
    except ValueError:
        return ""
    node = obj(obj(obj(obj(found).get("data")).get("repository")).get("issueOrPullRequest"))
    for row in items(obj(node.get("comments")), "nodes"):
        if marker in string(row, "body"):
            return string(row, "url")
    return ""


def assign(
    github: GitHub,
    clock: Clock,
    target: str,
    agent: str,
    *,
    task: str = "",
    kind: str = "",
    wait: bool = True,
    timeout: float = claimops.ANSWER_TIMEOUT,
) -> dict[str, object]:
    """Claim, label and brief. Nothing is labelled when the controller refuses the claim."""
    _, number = claimops.parse_target(target)
    agent = check_agent(agent)
    chosen = (kind or "custom") if task.strip() else (kind or assign_task.kind_for(github, target))
    answer = claimops.claim(github, clock, target, wait=wait, timeout=timeout)
    status = str(answer.get("status") or "unknown")
    data: dict[str, object] = {
        "target": target,
        "agent": agent,
        "label": PREFIX + agent,
        "claim": answer,
        "status": status,
    }
    if status not in HELD:
        return {**data, "status": "refused", "reason": answer.get("reason") or status}
    if task.strip():
        data["kind"] = chosen
    else:
        data["kind"], task = assign_task.build(github, target, chosen)
    label = PREFIX + agent
    data["label_created"] = _ensure_label(github, label)
    data["replaced"] = remove_agent_labels(github, number, keep=label)
    github.repo_api(f"issues/{number}/labels", method="POST", fields={"labels[]": label})
    digest = hashlib.sha256(task.strip().encode()).hexdigest()[:12]
    marker = MARKER.format(agent=agent, digest=digest)
    existing = _posted(github, number, marker)
    if existing:
        data["comment_url"], data["comment"] = existing, "already posted"
    else:
        body = assign_task.comment(target, agent, task, marker)
        posted = obj(
            github.repo_api(f"issues/{number}/comments", method="POST", fields={"body": body})
        )
        data["comment_url"], data["comment"] = string(posted, "html_url"), "posted"
    data["task"] = task
    data["status"] = "assigned"
    return data


EVENTS = (
    "timelineItems(last:50,itemTypes:[LABELED_EVENT])"
    "{nodes{... on LabeledEvent{createdAt label{name}}}}"
)
HOLDS = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name)"
    '{labels(first:20,query:"agent:"){nodes{name'
    " issues(states:OPEN,first:50){nodes{number title url " + EVENTS + "}}"
    " pullRequests(states:OPEN,first:50){nodes{number title url " + EVENTS + "}}}}}}"
)


def _since(node: dict[str, JSON], label: str) -> str:
    stamp = ""
    for event in items(obj(node.get("timelineItems")), "nodes"):
        if string(obj(event.get("label")), "name") == label:
            stamp = string(event, "createdAt")
    return stamp


def assigned(github: GitHub) -> list[Assignment]:
    """Open items holding an ``agent:*`` label, oldest assignment first, in one query."""
    owner, name = github.repo.split("/", 1)
    raw = github.gh(
        "api", "graphql", "-f", f"query={HOLDS}", "-f", f"owner={owner}", "-f", f"name={name}"
    )
    try:
        found = json.loads(raw)
    except ValueError:
        raise GitHubError("GitHub returned something that is not JSON.") from None
    repository = obj(obj(obj(found).get("data")).get("repository"))
    rows: list[Assignment] = []
    for label in items(obj(repository.get("labels")), "nodes"):
        label_name = string(label, "name")
        if not label_name.startswith(PREFIX):
            continue
        for kind, key in (("issue", "issues"), ("pr", "pullRequests")):
            for node in items(obj(label.get(key)), "nodes"):
                rows.append(
                    Assignment(
                        target=f"{kind}:{node.get('number')}",
                        title=string(node, "title"),
                        agent=label_name.removeprefix(PREFIX),
                        since=_since(node, label_name),
                        url=string(node, "url"),
                    )
                )
    return sorted(rows, key=lambda row: (row.since or "~", row.target))


def held_for(since: str, now: datetime) -> str:
    moment = timestamp({"at": since}, "at")
    if moment is None:
        return "unknown"
    minutes = max(0, int((now - moment).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h {minutes % 60:02d}m"
    return f"{minutes // 1440}d {minutes % 1440 // 60}h"
