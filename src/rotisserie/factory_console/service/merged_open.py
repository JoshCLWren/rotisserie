"""Merged factory PRs whose issue is still open, so a worker can take it again.

Closure keeps an issue open on purpose when the PR says it is only part of it,
or names it only in the title. Those issues stay claimable, and a worker can
redo merged work, so the recap puts each one in front of the owner.
"""

from __future__ import annotations

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, integer, label_names, obj, string

LIMIT = 30
FACTORY_LABEL = "factory"


def scan(github: GitHub, merged: list[Obj]) -> list[dict[str, object]]:
    """Read each merged factory PR's linked issue and keep the ones still open."""
    linkage = runtime.module("factory_merge_linkage").linkage
    rows: list[dict[str, object]] = []
    states: dict[int, Obj] = {}
    factory = [pr for pr in merged if FACTORY_LABEL in label_names(pr)][:LIMIT]
    for pr in factory:
        number = integer(pr, "number")
        if number is None:
            continue
        head = obj(obj(github.repo_api(f"pulls/{number}")).get("head"))
        link = linkage(string(head, "ref"), string(pr, "body"), string(pr, "title"))
        issue = link.target
        if issue is None:
            continue
        if issue not in states:
            states[issue] = obj(github.repo_api(f"issues/{issue}"))
        found = states[issue]
        if string(found, "state") != "open":
            continue
        rows.append(
            {
                "pr": number,
                "pr_title": string(pr, "title"),
                "issue": issue,
                "issue_title": string(found, "title"),
                "url": string(found, "html_url"),
                "kind": link.kind,
                "closes": link.issue is not None,
            }
        )
    return rows
