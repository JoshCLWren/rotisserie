"""Is the factory itself healthy? Each check is something that broke for real.

Every check here replaces a diagnosis first done by hand:

* the reliability snapshot never published (stats were empty for hours);
* the dashboard job that publishes it failed (an empty workers-per-provider
  setting crashed it);
* the scheduler stopped running;
* Rotisserie intake could not decide, so no fresh issue was admitted;
* most workers were rejected by the *same* factory gate: the fast checks ran
  tests the repository had deleted, so every Rust change "failed validation".
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    integer,
    items,
    string,
    timestamp,
)
from rotisserie.factory_console.service import outcomes, providers, queue, reliability
from rotisserie.factory_console.tracker import DISPATCH_WORKFLOW, Clock, Tracker, failure_reasons

DASHBOARD_WORKFLOW = "factory-status-pages.yml"
STATS_STALE = timedelta(hours=1)
SCHEDULER_STALE = timedelta(minutes=20)
# A gate rejecting this share of recent finished workers (and at least this many)
# is a factory problem, not a model problem.
SYSTEMIC_SHARE = 0.5
SYSTEMIC_MINIMUM = 3
GATE = re.compile(r"factory fast checks failed: (?P<gate>[^(;]+)")


@dataclass(frozen=True)
class Finding:
    """One health check: ok, warn or fail, with what to do about it."""

    check: str
    status: str  # ok | warn | fail
    text: str
    hint: str = ""
    fix: str = ""  # a safe repair `apply_fixes` can make; empty when none

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _stats(github: GitHub, now: datetime) -> Finding:
    stats = reliability.load_stats(github)
    if stats is None:
        return Finding(
            "reliability snapshot",
            "fail",
            "factory-stats.json has never been published, so stats and model picking are blind",
            f"check the {DASHBOARD_WORKFLOW} runs below; doctor --fix runs it now",
            fix="publish-snapshot",
        )
    generated = timestamp(stats, "generated_at")
    if generated is None or now - generated > STATS_STALE:
        age = "unknown" if generated is None else f"{int((now - generated).total_seconds() // 60)}m"
        return Finding(
            "reliability snapshot",
            "warn",
            f"factory-stats.json is stale (age {age}; it should refresh every 15 minutes)",
            "GitHub's 15-minute schedule can lag by hours; doctor --fix runs the publisher now",
            fix="publish-snapshot",
        )
    return Finding("reliability snapshot", "ok", "published and fresh")


def _dashboard(github: GitHub, tracker: Tracker) -> Finding:
    runs = items(
        github.repo_api(f"actions/workflows/{DASHBOARD_WORKFLOW}/runs?per_page=5"),
        "workflow_runs",
    )
    done = [run for run in runs if string(run, "status") == "completed"]
    if not done:
        return Finding("dashboard publisher", "warn", "no finished run found")
    last = done[0]
    if string(last, "conclusion") == "success":
        return Finding("dashboard publisher", "ok", "last run succeeded")
    run_id = integer(last, "id") or 0
    detail = ""
    for job in tracker.jobs(run_id):
        failed = [
            string(step, "name")
            for step in items(job, "steps")
            if string(step, "conclusion") == "failure"
        ]
        if failed:
            job_id = integer(job, "id") or 0
            reasons = failure_reasons(tracker.annotations(job_id))
            if not reasons:
                # Tracebacks only reach the log; annotations say "exit code 1".
                try:
                    reasons = failure_reasons([], tracker.log(job_id))
                except GitHubError:
                    reasons = []
            detail = f" at step '{failed[0]}'" + (f": {reasons[0]}" if reasons else "")
            break
    return Finding(
        "dashboard publisher",
        "fail",
        f"last run {string(last, 'conclusion')}{detail}",
        f"https://github.com/{github.repo}/actions/runs/{run_id}",
    )


def _scheduler(github: GitHub, now: datetime) -> Finding:
    runs = items(
        github.repo_api(f"actions/workflows/{DISPATCH_WORKFLOW}/runs?per_page=1"),
        "workflow_runs",
    )
    last = timestamp(runs[0], "created_at") if runs else None
    if last is None or now - last > SCHEDULER_STALE:
        return Finding(
            "scheduler",
            "fail",
            "the scheduler has not run in the last 20 minutes",
            "rotisserie factory report shows its last runs",
        )
    return Finding("scheduler", "ok", "running")


def _intake(github: GitHub, now: datetime) -> Finding:
    board = queue.read_board(github, providers.repository_variables(github), int(now.timestamp()))
    if board.intake_problem:
        return Finding(
            "rotisserie intake",
            "fail",
            f"admits no fresh issue: {board.intake_problem}",
            "rotisserie factory why issue:N explains a single issue",
        )
    return Finding("rotisserie intake", "ok", "deciding normally")


def systemic_rejection(found: list[outcomes.RequestOutcome]) -> Finding:
    """One factory gate rejecting most finished workers is a factory bug."""
    finished = [item for item in found if item.worker_result and item.worker_result != "running"]
    gates = Counter(
        match.group("gate").strip()
        for item in finished
        if (match := GATE.search(item.worker_message))
    )
    if not gates:
        return Finding(
            "worker rejections",
            "ok",
            f"no factory gate is rejecting workers ({len(finished)} finished recently)",
        )
    gate, count = gates.most_common(1)[0]
    if count >= SYSTEMIC_MINIMUM and count >= SYSTEMIC_SHARE * len(finished):
        return Finding(
            "worker rejections",
            "fail",
            f"{count} of {len(finished)} recent workers were rejected by the same factory gate: "
            f"{gate}. That points at the gate or the repository, not the models",
            "rotisserie factory outcomes --all shows each rejection; compare the gate with main",
        )
    return Finding(
        "worker rejections",
        "warn" if count >= SYSTEMIC_MINIMUM else "ok",
        f"most common factory gate rejection: {gate} ({count} of {len(finished)})",
    )


def run_checks(github: GitHub, *, now: datetime, clock: Clock | None = None) -> list[Finding]:
    """Every health check; a check that cannot read GitHub reports that instead of failing."""
    tracker = Tracker(github, clock)
    found: list[Finding] = []

    def rejections() -> Finding:
        recent = outcomes.request_outcomes(
            github, since=now - timedelta(hours=2), clock=clock, targeted_only=False
        )
        return systemic_rejection(recent)

    checks: list[tuple[str, Callable[[], Finding]]] = [
        ("reliability snapshot", lambda: _stats(github, now)),
        ("dashboard publisher", lambda: _dashboard(github, tracker)),
        ("scheduler", lambda: _scheduler(github, now)),
        ("rotisserie intake", lambda: _intake(github, now)),
        ("worker rejections", rejections),
    ]
    for name, check in checks:
        try:
            found.append(check())
        except GitHubError as exc:
            found.append(Finding(name, "warn", f"could not check: {exc}"))
    return found


@dataclass(frozen=True)
class Applied:
    fix: str
    text: str
    url: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def apply_fixes(github: GitHub, findings: list[Finding]) -> list[Applied]:
    """Make the safe repairs the findings name. Each is idempotent and reversible."""
    done: list[Applied] = []
    wanted = {item.fix for item in findings if item.fix and item.status != "ok"}
    if "publish-snapshot" in wanted:
        # The same job the schedule runs: regenerate the dashboard and stats snapshot.
        output = github.gh("workflow", "run", DASHBOARD_WORKFLOW, "--repo", github.repo)
        url = next((part for part in output.split() if "/actions/runs/" in part), "")
        done.append(Applied("publish-snapshot", f"started {DASHBOARD_WORKFLOW}", url))
    return done
