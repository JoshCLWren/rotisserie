"""Explain one issue or PR's labels from the glossary, and what they show the factory does next.

Labels mirror the store and the controllers; they never hold work by themselves.
The last line therefore states only what the labels show, and says nothing
when they leave the next step open (``baste why`` reads the real eligibility).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import label_glossary

STAGE_STEPS = {
    "factory:building": "building: a worker implements or repairs it",
    "factory:review": "review: the next worker reviews the current head",
    "factory:changes-requested": "repair: the next worker repairs the review findings",
    "factory:ci": "checks: the factory verifies the required checks; a failure gets a CI repair",
    "factory:ready": "ready: every merge gate passes; no new worker, it waits for the merge",
}


def next_step(labels: Iterable[str], *, closed: bool = False) -> str | None:
    """What the labels show the factory does next, or None when they do not make it clear."""
    names = set(labels)
    if closed:
        return "closed: the factory sends no worker to a closed item"
    work = runtime.module("factory_work_policy_base")
    bypass = names & runtime.module("factory_state_labels").BYPASS
    agent = work.agent_hold(names)
    if agent:
        return f"outside agent: {agent} works on it; the factory sends no worker"
    if "factory:migration-wait" in names:
        return "waiting: another PR of the same migration family finishes first"
    blocked = sorted(names & work.BLOCKED_LABELS)
    if blocked:
        return f"blocked: no worker while {', '.join(blocked)} is on"
    owner = work.owner_of(names)
    if owner not in (None, "factory:unowned"):
        return f"owned: {owner} holds the next step"
    if "factory:claimed" in names:
        return "claimed: the claimant holds the next step; the status comment names who"
    if "factory:parked" in names and not bypass and "factory:incident-work" not in names:
        return "held: no worker until a new head, factory:override or a claim"
    if "factory:needs-split" in names and not bypass:
        return "waiting for a split: no implement run until the factory splits it"
    if "factory:cooling" in names:
        return "cooling: no worker until the retry cooling ends"
    if "factory:merge-conflict" in names:
        return "merge conflict: the next worker resolves the conflict with main"
    stage = work.stage_of(names)
    return STAGE_STEPS.get(stage) if stage else None


def _names(item: Mapping[str, object]) -> list[str]:
    raw = item.get("labels")
    rows = raw if isinstance(raw, list) else []
    return sorted(
        str(row.get("name") if isinstance(row, Mapping) else row)
        for row in rows
        if (row.get("name") if isinstance(row, Mapping) else row)
    )


def explain(github: GitHub, number: int) -> dict[str, object]:
    item = github.repo_api(f"issues/{number}")
    if not isinstance(item, Mapping):
        raise ValueError(f"GitHub returned no item for #{number}")
    kind = "pr" if item.get("pull_request") else "issue"
    labels = _names(item)
    rows: list[dict[str, object]] = []
    for name in labels:
        entry = label_glossary.lookup(name)
        if entry is None:
            rows.append({"label": name, "factory": False, "meaning": "not a factory label"})
        else:
            details: dict[str, object] = dict(entry.to_dict())
            details["entry"] = details.pop("name")
            rows.append({"label": name, "factory": True, **details})
    closed = str(item.get("state") or "open").lower() == "closed"
    return {
        "target": f"{kind}:{number}",
        "title": item.get("title"),
        "state": "closed" if closed else "open",
        "url": item.get("html_url"),
        "labels": rows,
        "next": next_step(labels, closed=closed),
    }
