"""Read finished factory pull requests and their linked issues from GitHub for estimates.

Everything comes from GitHub data the console already reads: labels, label
events, merge facts and the factory's marker comments. Nothing here reads the factory store.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    _classify,
    integer,
    items,
    obj,
    string,
    timestamp,
)

COMMENT_PAGE = 100
REST_PAGES = 10
SEARCH_PAGE = 50
ISSUE_CHUNK = 20
LOW_BUDGET = 100
REST_WORKERS = 6
LINK = re.compile(r"(?i)\b(?:closes|closed|fixes|fixed|resolves|resolved|part of)\s+#(\d+)")
COMMON = f"""
  number state createdAt closedAt body
  labels(first: 100) {{ nodes {{ name }} }}
  comments(last: {COMMENT_PAGE}) {{
    totalCount nodes {{ createdAt author {{ __typename login }} body }}
  }}
  early: timelineItems(first: 100, itemTypes: [LABELED_EVENT]) {{
    totalCount nodes {{ ... on LabeledEvent {{ label {{ name }} }} }}
  }}
  late: timelineItems(last: 100, itemTypes: [LABELED_EVENT]) {{
    nodes {{ ... on LabeledEvent {{ label {{ name }} }} }}
  }}
"""
PR_FRAGMENT = f"""
fragment pr on PullRequest {{
  {COMMON}
  mergedAt mergedBy {{ __typename login }}
  closingIssuesReferences(first: 3) {{ nodes {{ number }} }}
}}
"""
FRAGMENTS = f"{PR_FRAGMENT}fragment issue on Issue {{ {COMMON} stateReason }}\n"
LOOKUP = "... on Issue { ...issue } ... on PullRequest { ...pr }"
SEARCH = f"""
query($q: String!, $first: Int!, $after: String) {{
  rateLimit {{ cost remaining resetAt }}
  search(query: $q, type: ISSUE, first: $first, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{ ...pr }} }}
  }}
}}
{PR_FRAGMENT}"""


@dataclass(frozen=True)
class Comment:
    at: datetime | None
    author: str
    bot: bool
    body: str


@dataclass(frozen=True)
class Thread:
    """One issue or pull request, reduced to what an estimate needs."""

    kind: str
    number: int
    state: str
    created: datetime | None
    closed: datetime | None
    merged: datetime | None
    merged_by_bot: bool | None
    labels: frozenset[str]
    labeled: frozenset[str]
    body_size: int
    linked: int | None
    comments: tuple[Comment, ...]
    truncated: bool = False
    labels_incomplete: bool = False
    reason: str = ""


@dataclass
class Budget:
    """GraphQL points this run spent and what GitHub says is left."""

    cost: int = 0
    remaining: int | None = None
    reset_at: str = ""
    calls: int = 0

    def record(self, data: Obj) -> None:
        limit = obj(data.get("rateLimit"))
        self.cost += integer(limit, "cost") or 0
        self.remaining = integer(limit, "remaining")
        self.reset_at = string(limit, "resetAt")
        self.calls += 1

    @property
    def low(self) -> bool:
        return self.remaining is not None and self.remaining < LOW_BUDGET

    def to_dict(self) -> dict[str, object]:
        return {
            "graphql_cost": self.cost,
            "graphql_remaining": self.remaining,
            "graphql_reset_at": self.reset_at,
            "graphql_calls": self.calls,
        }


_SAMPLES: dict[tuple[str, int], list[Thread]] = {}
_THREADS: dict[tuple[str, int], Thread] = {}


def _graphql(github: GitHub, budget: Budget, query: str, **variables: str | int) -> Obj:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    try:
        reply = obj(json.loads(github.gh(*args, allow_exit_one=True)))
    except ValueError:
        raise GitHubError("GitHub returned something that is not JSON.") from None
    problems = [row for row in items(reply, "errors") if string(row, "type") != "NOT_FOUND"]
    if problems or "data" not in reply:
        message = "; ".join(string(row, "message") for row in problems)
        raise _classify(message or string(reply, "message") or "GitHub GraphQL request failed")
    data = obj(reply.get("data"))
    budget.record(data)
    return data


def _names(node: Obj, key: str, inner: str) -> set[str]:
    return {
        string(obj(entry.get(inner)) if inner else entry, "name")
        for entry in items(obj(node.get(key)), "nodes")
    } - {""}


def _rest_comments(github: GitHub, number: int) -> tuple[Comment, ...]:
    found: list[Comment] = []
    for page in range(1, REST_PAGES + 1):
        rows = items(github.repo_api(f"issues/{number}/comments?per_page=100&page={page}"))
        for row in rows:
            user = obj(row.get("user"))
            found.append(
                Comment(
                    timestamp(row, "created_at"),
                    string(user, "login"),
                    string(user, "type") == "Bot",
                    string(row, "body"),
                )
            )
        if len(rows) < 100:
            break
    return tuple(found)


def parse_thread(node: Obj) -> Thread | None:
    """A GraphQL issue or pull request node; ``truncated`` when GraphQL cut comments off."""
    number = integer(node, "number")
    if number is None:
        return None
    pull = "mergedAt" in node
    block = obj(node.get("comments"))
    comments = tuple(
        Comment(
            timestamp(row, "createdAt"),
            string(obj(row.get("author")), "login"),
            string(obj(row.get("author")), "__typename") == "Bot",
            string(row, "body"),
        )
        for row in items(block, "nodes")
    )
    body = string(node, "body")
    closing = [
        integer(row, "number") for row in items(obj(node.get("closingIssuesReferences")), "nodes")
    ]
    mentioned = LINK.search(body)
    linked = next((n for n in closing if n), None) or (
        int(mentioned.group(1)) if pull and mentioned else None
    )
    merged_by = node.get("mergedBy")
    state = string(node, "state").lower()
    return Thread(
        kind="pr" if pull else "issue",
        number=number,
        state="merged" if timestamp(node, "mergedAt") else state,
        created=timestamp(node, "createdAt"),
        closed=timestamp(node, "closedAt"),
        merged=timestamp(node, "mergedAt"),
        merged_by_bot=string(obj(merged_by), "__typename") == "Bot"
        if isinstance(merged_by, dict)
        else None,
        labels=frozenset(_names(node, "labels", "")),
        labeled=frozenset(_names(node, "early", "label") | _names(node, "late", "label")),
        body_size=len(body),
        linked=linked if pull else None,
        comments=comments,
        truncated=(integer(block, "totalCount") or 0) > len(comments),
        labels_incomplete=(integer(obj(node.get("early")), "totalCount") or 0) > 200,
        reason=string(node, "stateReason").lower(),
    )


def _complete(github: GitHub, nodes: list[Obj]) -> list[Thread]:
    """Parse nodes, reading every comment over REST where GraphQL returned only the last page."""
    parsed = [thread for node in nodes if (thread := parse_thread(node)) is not None]
    cut = [thread for thread in parsed if thread.truncated]
    if not cut:
        return parsed
    with ThreadPoolExecutor(max_workers=REST_WORKERS) as pool:
        full = dict(
            zip(cut, pool.map(lambda t: _rest_comments(github, t.number), cut), strict=True)
        )
    return [replace(t, comments=full[t], truncated=False) if t in full else t for t in parsed]


def recent_pulls(github: GitHub, budget: Budget, sample: int) -> tuple[list[Thread], bool]:
    """The ``sample`` most recently updated closed ``factory`` PRs.

    The flag is True when a low GraphQL budget stopped the read early.
    """
    key = (github.repo, sample)
    if key in _SAMPLES:
        return _SAMPLES[key], False
    query = f"repo:{github.repo} is:pr is:closed label:factory sort:updated-desc"
    found: list[Thread] = []
    after = ""
    short = False
    while len(found) < sample:
        variables: dict[str, str | int] = {
            "q": query,
            "first": min(SEARCH_PAGE, sample - len(found)),
        }
        if after:
            variables["after"] = after
        search = obj(_graphql(github, budget, SEARCH, **variables).get("search"))
        found.extend(_complete(github, items(search, "nodes")))
        page = obj(search.get("pageInfo"))
        after = string(page, "endCursor")
        if page.get("hasNextPage") is not True or not after:
            break
        if budget.low:
            short = True
            break
    _SAMPLES[key] = found
    for thread in found:
        _THREADS[(github.repo, thread.number)] = thread
    return found, short


def threads(github: GitHub, budget: Budget, numbers: list[int]) -> dict[int, Thread]:
    """Issues or PRs by number, read in batches of aliased GraphQL lookups."""
    known = {n: _THREADS[(github.repo, n)] for n in numbers if (github.repo, n) in _THREADS}
    wanted = sorted(set(numbers) - set(known))
    owner, name = github.repo.split("/", 1)
    for start in range(0, len(wanted), ISSUE_CHUNK):
        chunk = wanted[start : start + ISSUE_CHUNK]
        fields = " ".join(f"n{n}: issueOrPullRequest(number: {n}) {{ {LOOKUP} }}" for n in chunk)
        query = (
            "query($owner: String!, $name: String!) { rateLimit { cost remaining resetAt } "
            f"repository(owner: $owner, name: $name) {{ {fields} }} }}\n{FRAGMENTS}"
        )
        repository = obj(_graphql(github, budget, query, owner=owner, name=name).get("repository"))
        for thread in _complete(github, [obj(repository.get(f"n{n}")) for n in chunk]):
            known[thread.number] = _THREADS[(github.repo, thread.number)] = thread
    return known


def clear_cache() -> None:
    _SAMPLES.clear()
    _THREADS.clear()
