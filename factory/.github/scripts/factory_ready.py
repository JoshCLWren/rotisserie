"""The dispatcher's ranked pool of claimable work, after Beads' ``bd ready``.

Idea credit: Beads (MIT, github.com/gastownhall/beads); reimplemented here.

``pool`` is the one function the dispatcher (``factory-work-controller.py``),
``baste next`` and ``baste ready`` call, so what ``ready`` prints is the order
the factory walks. The callers read GitHub and the store their own way and
pass the results in; this module reads nothing itself.

After this list, the dispatcher still applies per-worker and live checks that
no snapshot can answer: worker role ordering, review independence, required
checks still running, the live GitHub dependency list, strike-retry producer
exclusion, the Jev ranking and the transactional lease itself.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import factory_deps
from factory_work_policy_candidates import build_candidates
from factory_work_policy_priority import Candidate

PUBLISHED_CURSOR = "ready"
PUBLISHED_LIMIT = 10
HoldReader = Callable[[list[Candidate]], Mapping[int, str]]


@dataclass(frozen=True)
class Ready:
    candidates: tuple[Candidate, ...]
    held: dict[str, str] = field(default_factory=dict)
    cycles: tuple[tuple[int, ...], ...] = ()
    ranked: tuple[Candidate, ...] = ()


def _issue_retries(records: Iterable[Any] | None) -> dict[int, int]:
    counts: dict[int, int] = {}
    for attempt in records or ():
        if attempt.kind == "issue":
            counts[attempt.number] = counts.get(attempt.number, 0) + 1
    return counts


def pool(
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    *,
    attempt_records: list[Any] | None,
    external_wip: int = 0,
    intake: set[int] | None = None,
    links: Iterable[factory_deps.Link] = (),
    holds: HoldReader | None = None,
    kinds: tuple[str, ...] | None = None,
    operator_force: bool = False,
    intake_holds: bool = True,
    rank: Callable[..., list[Candidate]] = build_candidates,
    **limits: Any,
) -> Ready:
    """Rank and filter the shared pool exactly as the dispatcher does.

    ``attempt_records`` of None means retry history is unknown, which holds
    every fresh issue. ``intake`` of None means the legacy, unrestricted lane.
    ``holds`` reads retry cooling and loop parks; None skips them, as the
    dispatcher does for a targeted or operator-forced pass.
    """
    ranked = rank(
        issues,
        prs,
        no_diff_attempts_by_issue=_issue_retries(attempt_records),
        no_diff_attempt_records=attempt_records,
        include_backpressured_issues=operator_force,
        external_wip=external_wip,
        intake_holds=intake_holds,
        **limits,
    )
    held: dict[str, str] = {}
    graph = factory_deps.graph(issues, links)
    kept: list[Candidate] = []
    for candidate in ranked:
        target = f"{candidate.kind}:{candidate.number}"
        if candidate.kind == "issue":
            if intake is not None and not candidate.incident and candidate.number not in intake:
                held[target] = "not admitted by Rotisserie intake"
                continue
            if graph.held(candidate.number):
                held[target] = graph.reason(candidate.number)
                continue
        if kinds is not None and candidate.kind not in kinds:
            continue
        if kinds is None and attempt_records is None and candidate.kind == "issue":
            held[target] = "retry history unknown; fresh issues wait"
            continue
        kept.append(candidate)
    if holds is not None:
        found = holds(kept)
        for candidate in kept:
            if candidate.number in found:
                held[f"{candidate.kind}:{candidate.number}"] = found[candidate.number]
        kept = [candidate for candidate in kept if candidate.number not in found]
    for row in issues:
        number = int(row["number"])
        if f"issue:{number}" not in held and graph.held(number):
            held[f"issue:{number}"] = graph.reason(number)
    return Ready(tuple(kept), held, graph.cycles(), tuple(ranked))


def action_for(candidate: Candidate) -> str:
    if candidate.kind == "issue":
        return "implement"
    from factory_work_policy import completion_action_for

    return completion_action_for(candidate.stage, bool(candidate.conflicted)) or "inspect"


def rows(
    found: Ready, issues: list[dict[str, Any]], prs: list[dict[str, Any]], limit: int
) -> list[dict[str, object]]:
    """Number, title, stage and action only: safe to publish on the status page."""
    titles = {int(row["number"]): str(row.get("title") or "")[:120] for row in (*issues, *prs)}
    return [
        {
            "rank": position,
            "target": f"{candidate.kind}:{candidate.number}",
            "title": titles.get(candidate.number, ""),
            "stage": candidate.stage or ("new" if candidate.kind == "issue" else ""),
            "action": action_for(candidate),
            "lane": int(candidate.lane),
            "priority": int(candidate.priority),
        }
        for position, candidate in enumerate(found.candidates[:limit], 1)
    ]


def publish(repo: str, items: list[dict[str, object]], cycles: Iterable[Iterable[int]]) -> bool:
    """Store the top of the pool for the status page; no GitHub write."""
    import time

    import factory_turso_cursors as cursors

    detail = {"items": items, "cycles": [list(cycle) for cycle in cycles]}
    return cursors.advance(repo, PUBLISHED_CURSOR, int(time.time()), detail)


def published(repo: str) -> dict[str, object]:
    from datetime import UTC, datetime

    import factory_turso_cursors as cursors

    found = cursors.get(repo, PUBLISHED_CURSOR)
    if found is None:
        return {"generated_at": None, "items": [], "cycles": []}
    at, detail = found
    return {
        "generated_at": datetime.fromtimestamp(at, UTC).isoformat(),
        "items": detail.get("items", []),
        "cycles": detail.get("cycles", []),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["published"])
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(published(args.repo), sort_keys=True))
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    sys.exit(main())
