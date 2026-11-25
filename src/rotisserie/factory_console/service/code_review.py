"""The factory's line-anchored code reviews: what each found, and how they ended.

The trusted controller posts every review as one GitHub PR review whose body
starts with a summary marker, plus one inline comment per valid finding whose
body starts with a finding marker (``factory_review_findings``). These reads
parse only those markers, never the prose around them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from types import ModuleType

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, integer, items, string, timestamp

LIMIT = 40
RESULTS = ("approve", "repair", "reject", "failed")


def _policy() -> ModuleType:
    return runtime.module("factory_review_findings")


@dataclass(frozen=True)
class LineFinding:
    severity: str
    location: str  # path:line or path:first-last
    text: str
    url: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CodeReview:
    pr: int
    head: str
    verdict: str
    blocking: int
    valid: int
    dropped: int
    at: str
    url: str
    findings: tuple[LineFinding, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["findings"] = [item.to_dict() for item in self.findings]
        return data


def _pages(github: GitHub, path: str, *, limit: int = 10) -> list[Obj]:
    rows: list[Obj] = []
    for page in range(1, limit + 1):
        batch = items(github.repo_api(f"{path}?per_page=100&page={page}"))
        rows += batch
        if len(batch) < 100:
            break
    return rows


def summaries(reviews: Iterable[Obj], *, pr: int) -> list[tuple[Obj, dict[str, str]]]:
    """The factory's review summaries on one PR, oldest first, with their parsed markers."""
    pattern = _policy().SUMMARY_MARKER_RE
    found: list[tuple[Obj, dict[str, str]]] = []
    for review in reviews:
        first = string(review, "body").splitlines()[:1]
        match = pattern.match(first[0]) if first else None
        if match and int(match.group("pr")) == pr:
            found.append((review, match.groupdict()))
    return found


def findings_of(comments: Sequence[Obj], review_id: int) -> tuple[LineFinding, ...]:
    pattern = _policy().FINDING_MARKER_RE
    rows: list[LineFinding] = []
    for comment in comments:
        if integer(comment, "pull_request_review_id") != review_id:
            continue
        lines = string(comment, "body").splitlines()
        match = pattern.match(lines[0]) if lines else None
        if not match:
            continue
        first, last = match.group("start"), match.group("end")
        span = first if first == last else f"{first}-{last}"
        text = " ".join(lines[1:]).strip()
        rows.append(
            LineFinding(
                severity=match.group("severity"),
                location=f"{string(comment, 'path')}:{span}",
                text=text,
                url=string(comment, "html_url"),
            )
        )
    return tuple(rows)


def latest(github: GitHub, number: int) -> CodeReview | None:
    """The newest factory code review on a PR, with its line findings."""
    found = summaries(_pages(github, f"pulls/{number}/reviews"), pr=number)
    if not found:
        return None
    review, marker = found[-1]
    review_id = integer(review, "id") or 0
    comments = _pages(github, f"pulls/{number}/comments") if int(marker["valid"]) else []
    return CodeReview(
        pr=number,
        head=marker["head"],
        verdict=marker["verdict"],
        blocking=int(marker["blocking"]),
        valid=int(marker["valid"]),
        dropped=int(marker["dropped"]),
        at=string(review, "submitted_at"),
        url=string(review, "html_url"),
        findings=findings_of(comments, review_id),
    )


def describe(found: CodeReview | None) -> list[str]:
    """Plain lines for ``baste review``."""
    if found is None:
        return ["No line-anchored factory code review yet."]
    lines = [
        f"Code review at head {found.head[:10]}: {found.verdict.upper()}, "
        f"{found.blocking} blocking, {found.valid - found.blocking} non-blocking, "
        f"{found.dropped} dropped (not on a changed line)"
    ]
    lines += [f"  {item.severity} {item.location}: {item.text[:160]}" for item in found.findings]
    return lines


def counts(
    github: GitHub, numbers: Iterable[int], *, since: datetime, failed: int = 0
) -> dict[str, int]:
    """Factory code reviews since ``since`` by result, over at most ``LIMIT`` PRs."""
    tally: Counter[str] = Counter({name: 0 for name in RESULTS})
    tally["failed"] = failed
    for number in list(dict.fromkeys(numbers))[:LIMIT]:
        for review, marker in summaries(_pages(github, f"pulls/{number}/reviews"), pr=number):
            when = timestamp(review, "submitted_at")
            if when is not None and when >= since:
                tally[marker["verdict"]] += 1
                tally["dropped findings"] += int(marker["dropped"])
    return dict(tally)
