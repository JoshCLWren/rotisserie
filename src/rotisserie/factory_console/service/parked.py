"""Mark the PRs the loop breaker parked, so the console counts backlog as the dispatcher does.

The dispatcher reads parks from the store. The console cannot, so it finds them by
their park comment at the PR's current head (``loops.parked``). It reads only when
the backlog looks full, and a failed read marks nothing: the old count stands.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, Obj, string
from rotisserie.factory_console.service import loops

PARKED = "held: loop-parked"
LOOPING = "held: looping"


def stamp(
    github: GitHub,
    prs: list[Obj],
    review_backlog_limit: int,
    now: int,
    looping: frozenset[int] = frozenset(),
) -> list[Obj]:
    policy = runtime.module("factory_work_policy")
    rows = [dict(row) for row in prs]
    if policy.factory_review_backlog_count(rows) < review_backlog_limit:
        return prs
    pulls: list[Obj] = [
        {
            "number": row.get("number"),
            "title": string(row, "title"),
            "html_url": string(row, "url"),
            "head": {"sha": string(row, "headRefOid")},
        }
        for row in prs
    ]
    limit = policy.parked_pr_limit(review_backlog_limit) + review_backlog_limit
    try:
        found = loops.parked(github, now=datetime.fromtimestamp(now, UTC), pulls=pulls, limit=limit)
    except GitHubError, RuntimeError, ValueError, KeyError, TypeError:
        return prs
    held = {row["number"]: PARKED for row in found}
    held |= {number: LOOPING for number in looping if number not in held}
    key = policy.LOOP_HOLD_KEY
    return [{**row, key: held[row["number"]]} if row.get("number") in held else row for row in prs]


def full(explanation: runtime.Explanation) -> bool:
    return 0 < explanation.parked_limit <= explanation.parked


def text(explanation: runtime.Explanation) -> str:
    return (
        f"{explanation.parked} parked PRs waiting on the owner (limit {explanation.parked_limit}, "
        "FACTORY_PARKED_PR_LIMIT). New issues wait until the owner handles them: baste recap."
    )
