"""A morning summary of what the factory did: the answer first, in plain words.

It reuses the reads behind ``report``, ``failures``, ``doctor``, ``status``,
``stats`` and the fleet switches, runs them side by side so the whole recap
fits the local web server's 90 second limit, and reads worker failures once.
A read that fails leaves its section empty and is named in ``unread``; it
never fails the recap. Backoffs come from the reliability snapshot's model
health, because the live backoff ledger is reachable only from trusted jobs.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rotisserie.factory_console.github import GitHub, GitHubError, string
from rotisserie.factory_console.service import (
    code_review,
    doctor,
    failures,
    fleet,
    incident,
    loops,
    merged_open,
    reliability,
    review,
    self_heal,
    stalls,
    turning,
)
from rotisserie.factory_console.service import recap_build as build
from rotisserie.factory_console.service.report import search
from rotisserie.factory_console.tracker import WORKER_WORKFLOW, Clock
from rotisserie.factory_console.work import current_work

FAILURE_SAMPLE = 40
READ_ERRORS = (GitHubError, RuntimeError, ValueError, OSError)


@dataclass(frozen=True)
class Recap:
    since: str
    generated_at: str
    tone: str
    verdict: str
    main: str
    merged: dict[str, list[dict[str, object]]]
    needs_you: list[dict[str, object]]
    healed: list[dict[str, object]]
    clearing: list[dict[str, object]]
    went_wrong: dict[str, object]
    providers: dict[str, list[dict[str, object]]]
    stuck: list[dict[str, object]]
    database: dict[str, object] | None
    unread: list[str]
    burning: dict[str, object] | None = None
    reviews: dict[str, int] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "since": self.since,
            "generated_at": self.generated_at,
            "tone": self.tone,
            "verdict": self.verdict,
            "main": self.main,
            "merged": self.merged,
            "needs_you": self.needs_you,
            "healed": self.healed,
            "clearing": self.clearing,
            "went_wrong": self.went_wrong,
            "providers": self.providers,
            "stuck": self.stuck,
            "database": self.database,
            "unread": self.unread,
            "burning": self.burning,
            "reviews": self.reviews,
        }


def assemble(sources: build.Sources) -> Recap:
    groups = build.merged(sources)
    needs = build.needs_you(sources)
    wrong = build.went_wrong(sources)
    causes = wrong["causes"] if isinstance(wrong["causes"], list) else []
    main = build.main_state(sources.findings)
    finished = [run for run in sources.runs if string(run, "status") == "completed"]
    tone, verdict = build.verdict(
        merged_count=len(groups["product"]) + len(groups["factory"]),
        main=main,
        scheduler_stopped=any(
            f.check == "scheduler" and f.status == "fail" for f in sources.findings
        ),
        capacity=sum(int(c["count"]) for c in causes if c["cause"] == "no_capacity"),
        failures_read=len(sources.failures),
        failed_runs=int(str(wrong["failed_runs"])),
        finished=len(finished),
        succeeded=sum(1 for run in finished if string(run, "conclusion") == "success"),
        needs=len(needs),
    )
    return Recap(
        since=sources.since.isoformat(),
        generated_at=sources.now.isoformat(),
        tone=tone,
        verdict=verdict,
        main=main,
        merged=groups,
        needs_you=needs,
        healed=build.healed(sources),
        clearing=build.clearing(sources),
        went_wrong=wrong,
        providers=build.providers(sources),
        stuck=build.stuck(sources),
        database=build.database(sources),
        unread=sorted(sources.unread),
        burning=burning_stretch(sources),
        reviews=reviews_by_result(sources),
    )


def reviews_by_result(sources: build.Sources) -> dict[str, int] | None:
    """Line-anchored code reviews by result. Failed runs come from the sampled worker failures."""
    if not sources.reviews:
        return None
    failed = sum(1 for row in sources.failures if row.kind == "review")
    return {**sources.reviews, "failed": failed}


def burning_stretch(sources: build.Sources) -> dict[str, object] | None:
    if "worker runs" in sources.unread:
        return None
    gap = turning.longest_gap(sources.runs, sources.since, sources.now)
    if gap is None:
        return None
    span = turning.minutes_words(int(str(gap["minutes"])))
    until = "until now" if gap["ongoing"] else "then workers started again"
    burning = gap["ongoing"] and any(
        item.check == turning.CHECK and item.status == "fail" for item in sources.findings
    )
    text = f"no worker started for {span}, {until}."
    return {**gap, "burning": burning, "text": f"Burning: {text}" if burning else text.capitalize()}


def gather(
    github: GitHub, *, since: datetime, now: datetime, clock: Clock | None = None
) -> build.Sources:
    sources = build.Sources(since=since, now=now)
    iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def worker_runs() -> None:
        sources.runs = failures.runs_since(github, WORKER_WORKFLOW, iso)
        sources.failures = failures.worker_failures(
            github, since, limit=FAILURE_SAMPLE, runs=sources.runs
        )

    def pulls() -> None:
        sources.pulls = review.open_pulls(github)
        sources.stalls = stalls.scan(github, sources.pulls)

    def healed() -> None:
        sources.healed = self_heal.recent(github, since=since)

    def claims() -> None:
        sources.claims = search(
            github,
            f'repo:{github.repo} is:open label:"{build.CLAIMED}","{build.CLAIM_REJECTED}"',
        )

    def merged() -> None:
        sources.merged = search(github, f"repo:{github.repo} is:pr is:merged merged:>={iso}")
        try:
            sources.open_after_merge = merged_open.scan(github, sources.merged)
        except READ_ERRORS:
            sources.unread.append("issues left open by merges")

    def checks() -> None:
        sources.findings = doctor.run_checks(github, now=now, clock=clock)

    def stats() -> None:
        sources.stats = reliability.load_stats(github)

    def switches() -> None:
        sources.fleet = fleet.read(github)

    def work() -> None:
        sources.work = current_work(github, with_session=False)

    def code_reviews() -> None:
        pulls = [
            int(str(row["number"]))
            for row in review.open_pulls(github)
            if string(row, "updated_at") >= iso
        ]
        merged = search(github, f"repo:{github.repo} is:pr is:merged merged:>={iso}")
        numbers = pulls + [int(str(row["number"])) for row in merged if row.get("number")]
        sources.reviews = code_review.counts(github, numbers, since=since)

    def looping() -> None:
        pulls = review.open_pulls(github)
        sources.parked = loops.parked(github, now=now, pulls=pulls)
        held = [int(str(row["number"])) for row in sources.parked]
        sources.loops = loops.scan(github, now=now, pulls=pulls, parked=held)
        if sources.parked or sources.loops:
            hot = incident.active_issues(github)
            loops.mark_incidents([*sources.parked, *sources.loops], pulls, hot)

    reads: dict[str, Callable[[], None]] = {
        "doctor": checks,
        "worker runs": worker_runs,
        "merged pull requests": merged,
        "open pull requests": pulls,
        "self-heals": healed,
        "claims": claims,
        "reliability snapshot": stats,
        "provider switches": switches,
        "current work": work,
        "looping items": looping,
        "code reviews": code_reviews,
    }

    def attempt(name: str) -> None:
        try:
            reads[name]()
        except READ_ERRORS:
            sources.unread.append(name)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(attempt, reads))
    return sources


def recap(github: GitHub, *, since: datetime, now: datetime, clock: Clock | None = None) -> Recap:
    return assemble(gather(github, since=since, now=now, clock=clock))


def as_moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
