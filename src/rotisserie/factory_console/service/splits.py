"""Split status: each issue held for a split, its split runs, rejections and result.

The intake guard holds an oversized or umbrella issue, the self-heal sweep sends
split runs (``factory_self_heal_split``) and creates the children from a valid
plan. This view reads GitHub only: one listing per hold label, one search for
split requests in the window, and the comments of each issue shown.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, integer, items, string
from rotisserie.factory_console.service.report import search

LIMIT = 20
PAGES = 3
HOLD_LABELS = ("factory%3Aneeds-split", "umbrella")
REQUEST_TEXT = "needs a split before the factory takes it"


@dataclass(frozen=True)
class Split:
    issue: int
    title: str
    url: str
    state: str
    requested_at: str
    requests: int
    runs: list[dict[str, Any]] = field(default_factory=list)
    max_runs: int = 0
    done_at: str = ""
    children: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def state(record: dict[str, Any], max_runs: int) -> str:
    runs = record["runs"]
    if record["done_at"]:
        return f"done: {record['children']} child issues"
    if not runs:
        return "waiting for a split run"
    last = runs[-1]
    if not last["end"]:
        return "split run in progress"
    if last["end"] == "handoff":
        return "plan accepted; the sweep creates the child issues"
    if len(runs) >= max_runs and all(run["end"] == "rejected" for run in runs):
        return f"held: {max_runs} split plans rejected, no fallback"
    if last["reason"]:
        return "plan rejected; the next split run starts on the next sweep"
    return "plan rejected with no recorded reason; the next split run waits 6 hours"


def comments(github: GitHub, number: int) -> list[Obj]:
    found: list[Obj] = []
    for page in range(1, PAGES + 1):
        rows = items(github.repo_api(f"issues/{number}/comments?per_page=100&page={page}"))
        found += rows
        if len(rows) < 100:
            break
    return found


def recent(github: GitHub, *, since: datetime, limit: int = LIMIT) -> list[Split]:
    plans = runtime.module("factory_split_plan")
    iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    listed: dict[int, Obj] = {}
    for label in HOLD_LABELS:
        for row in items(github.repo_api(f"issues?labels={label}&state=open&per_page=100")):
            if "pull_request" not in row and integer(row, "number"):
                listed[integer(row, "number") or 0] = row
    query = f'repo:{github.repo} is:issue "{REQUEST_TEXT}" in:comments updated:>={iso}'
    for row in search(github, query):
        listed.setdefault(integer(row, "number") or 0, row)
    found: list[Split] = []
    for number in sorted(listed, reverse=True)[:limit]:
        row = listed[number]
        record = plans.history(comments(github, number), number)
        found.append(
            Split(
                issue=number,
                title=string(row, "title"),
                url=string(row, "html_url"),
                state=state(record, plans.MAX_DISPATCHES),
                requested_at=record["requested_at"],
                requests=record["requests"],
                runs=record["runs"],
                max_runs=plans.MAX_DISPATCHES,
                done_at=record["done_at"],
                children=record["children"],
            )
        )
    return found
