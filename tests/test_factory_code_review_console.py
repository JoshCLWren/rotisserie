"""``baste review`` and ``baste recap`` read the factory's line-anchored code reviews."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from rotisserie.factory_console import runtime
from rotisserie.factory_console.commands.recap import render
from rotisserie.factory_console.github import GitHub, Obj
from rotisserie.factory_console.service import code_review, recap
from rotisserie.factory_console.service import recap_build as build
from rotisserie.factory_console.service.failures import WorkerFailure
from rotisserie.factory_console.ui import Console

HEAD = "a" * 40
SINCE = datetime(2026, 11, 24, 0, 0, tzinfo=UTC)


def policy() -> Any:
    return runtime.module("factory_review_findings")


def posted_review(verdict: str, *, rid: int, at: str, dropped: int = 0) -> Obj:
    findings = policy()
    review = findings.Review(
        parsed=True,
        valid=(
            findings.Finding("app/run.py", 14, "blocking", "Null check missing", 11),
            findings.Finding("app/run.py", 43, "non-blocking", "Name it clearly"),
        ),
        dropped=tuple(findings.Dropped(n, "x.py:1", "off the diff") for n in range(dropped)),
    )
    body = findings.summary_body(pr=7, head=HEAD, reviewer="41", verdict=verdict, review=review)
    return {"id": rid, "body": body, "submitted_at": at, "html_url": f"https://x/r{rid}"}


def inline(rid: int) -> list[Obj]:
    findings = policy()
    rows: list[Obj] = []
    for finding in (
        findings.Finding("app/run.py", 14, "blocking", "Null check missing", 11),
        findings.Finding("app/run.py", 43, "non-blocking", "Name it clearly"),
    ):
        payload = findings.comment_payload(pr=7, head=HEAD, finding=finding)
        rows.append({**payload, "pull_request_review_id": rid, "html_url": "https://x/c"})
    rows.append({"pull_request_review_id": rid, "path": "a.py", "body": "Greptile note"})
    return rows


class Hub:
    repo = "acme/widgets"

    def __init__(self, reviews: list[Obj], comments: list[Obj]) -> None:
        self.reviews, self.comments = reviews, comments

    def repo_api(self, path: str, **_kwargs: object) -> object:
        if path.startswith("pulls/7/reviews"):
            return self.reviews
        if path.startswith("pulls/7/comments"):
            return self.comments
        return []


def test_baste_review_shows_findings_with_file_and_line() -> None:
    reviews: list[Obj] = [
        {"id": 1, "body": "Greptile summary", "submitted_at": "2026-11-24T01:00:00Z"},
        posted_review("repair", rid=5, at="2026-11-24T02:00:00Z", dropped=1),
    ]
    found = code_review.latest(cast(GitHub, Hub(reviews, inline(5))), 7)
    assert found is not None
    assert (found.verdict, found.blocking, found.valid, found.dropped) == ("repair", 1, 2, 1)
    lines = code_review.describe(found)
    assert "REPAIR, 1 blocking, 1 non-blocking, 1 dropped" in lines[0]
    assert "  blocking app/run.py:11-14: **Blocking:** Null check missing" == lines[1]
    assert lines[2].startswith("  non-blocking app/run.py:43:")
    assert code_review.describe(None) == ["No line-anchored factory code review yet."]


def test_recap_counts_reviews_by_result() -> None:
    reviews = [
        posted_review("repair", rid=5, at="2026-11-23T23:00:00Z"),
        posted_review("repair", rid=6, at="2026-11-24T02:00:00Z", dropped=2),
        posted_review("approve", rid=7, at="2026-11-24T03:00:00Z"),
    ]
    counts = code_review.counts(cast(GitHub, Hub(reviews, [])), [7, 7], since=SINCE)
    assert counts == {"approve": 1, "repair": 1, "reject": 0, "failed": 0, "dropped findings": 2}
    sources = build.Sources(since=SINCE, now=SINCE)
    sources.reviews = counts
    failure = WorkerFailure(1, "", "", "41", "job", "step", "", "work_failure", "", "", "pr:7")
    sources.failures = [failure, replace(failure, kind="review")]
    data = recap.assemble(sources).to_dict()
    assert data["reviews"] == {**counts, "failed": 1}
    stream = io.StringIO()
    render(Console(stream, env={"NO_COLOR": "1"}), data)
    text = stream.getvalue()
    assert "Code reviews by result" in text
    assert "1 approved, 1 changes requested, 0 rejected, 1 failed runs" in text
