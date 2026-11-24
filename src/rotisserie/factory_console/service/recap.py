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
from rotisserie.factory_console.service import doctor, failures, fleet, reliability, review, stalls
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
    went_wrong: dict[str, object]
    providers: dict[str, list[dict[str, object]]]
    stuck: list[dict[str, object]]
    database: dict[str, object] | None
    unread: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "since": self.since,
            "generated_at": self.generated_at,
            "tone": self.tone,
            "verdict": self.verdict,
            "main": self.main,
            "merged": self.merged,
            "needs_you": self.needs_you,
            "went_wrong": self.went_wrong,
            "providers": self.providers,
            "stuck": self.stuck,
            "database": self.database,
            "unread": self.unread,
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
        went_wrong=wrong,
        providers=build.providers(sources),
        stuck=build.stuck(sources),
        database=build.database(sources),
        unread=sorted(sources.unread),
    )


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

    def claims() -> None:
        sources.claims = search(
            github,
            f'repo:{github.repo} is:open label:"{build.CLAIMED}","{build.CLAIM_REJECTED}"',
        )

    def merged() -> None:
        sources.merged = search(github, f"repo:{github.repo} is:pr is:merged merged:>={iso}")

    def checks() -> None:
        sources.findings = doctor.run_checks(github, now=now, clock=clock)

    def stats() -> None:
        sources.stats = reliability.load_stats(github)

    def switches() -> None:
        sources.fleet = fleet.read(github)

    def work() -> None:
        sources.work = current_work(github, with_session=False)

    reads: dict[str, Callable[[], None]] = {
        "doctor": checks,
        "worker runs": worker_runs,
        "merged pull requests": merged,
        "open pull requests": pulls,
        "claims": claims,
        "reliability snapshot": stats,
        "provider switches": switches,
        "current work": work,
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
