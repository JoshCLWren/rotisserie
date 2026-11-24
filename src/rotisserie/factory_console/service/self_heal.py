"""What the factory cleared by itself: its trusted self-heal comments, newest first.

The factory's dispatcher sweep (``factory_self_heal.py``) leaves one trusted
comment per action. This machine never reads the factory store, so the comments
are the record here: one search for PRs with such a comment, then one comment
read per PR found, at most ``LIMIT`` PRs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, integer, items, string
from rotisserie.factory_console.service.report import search

LIMIT = 20
WORDS = {
    "empty-verdict": "moved back to review",
    "conflict-sync": "merged main in",
    "thread-resolved": "resolved an answered thread",
}


@dataclass(frozen=True)
class Heal:
    pr: int
    kind: str
    words: str
    text: str
    at: str
    url: str
    title: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def from_comments(records: list[Obj], *, pr: int, title: str = "") -> list[Heal]:
    """Self-heals recorded on one PR by trusted factory comments."""
    rules = runtime.module("factory_self_heal_rules")
    found: list[Heal] = []
    for record in records:
        for heal in rules.heals([record], pr=pr):
            found.append(
                Heal(
                    pr=pr,
                    kind=heal["kind"],
                    words=WORDS.get(heal["kind"], heal["kind"]),
                    text=heal["text"],
                    at=string(record, "created_at"),
                    url=string(record, "html_url"),
                    title=title,
                )
            )
    return found


def recent(github: GitHub, *, since: datetime, limit: int = LIMIT) -> list[Heal]:
    iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f'repo:{github.repo} is:pr "Factory self-heal" in:comments updated:>={iso}'
    found: list[Heal] = []
    for hit in search(github, query)[:limit]:
        number = integer(hit, "number")
        if not number:
            continue
        records = items(github.repo_api(f"issues/{number}/comments?since={iso}&per_page=100"))
        found += [
            heal
            for heal in from_comments(records, pr=number, title=string(hit, "title"))
            if heal.at >= iso
        ]
    return sorted(found, key=lambda heal: heal.at, reverse=True)
