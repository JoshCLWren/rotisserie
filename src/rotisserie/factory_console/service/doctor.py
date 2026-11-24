"""Is the factory itself healthy? Each check is something that broke for real.

Every check here replaces a diagnosis first done by hand:

* the reliability snapshot never published (stats were empty for hours);
* the dashboard job that publishes it failed (an empty workers-per-provider
  setting crashed it);
* the scheduler stopped running;
* Rotisserie intake could not decide, so no fresh issue was admitted;
* most workers were rejected by the *same* factory gate: the fast checks ran
  tests the repository had deleted, so every change to that code "failed validation";
* main broke a check (a PR merged with unformatted code), so every backlog PR
  running it went red and repair workers were sent to fix main's files PR by PR;
* main had no product CI at all: factory merges use GITHUB_TOKEN, which fires no
  push event, and "no run" was being read as green.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubRateLimitError,
    Obj,
    integer,
    items,
    job_name,
    obj,
    string,
    timestamp,
)
from rotisserie.factory_console.service import (
    baseline,
    checks,
    main_health,
    outcomes,
    providers,
    queue,
    reliability,
    review,
    stalls,
    unstick,
)
from rotisserie.factory_console.tracker import (
    DISPATCH_WORKFLOW,
    WORKER_WORKFLOW,
    Clock,
    Tracker,
    failure_reasons,
)

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
    targets: tuple[str, ...] = ()  # what the fix acts on, e.g. "pr:N@HEAD"
    secondary_limit: bool = False
    retry_after_seconds: int | None = None

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


# The worker step that picks a provider and model. When it fails, the worker
# had no model it was allowed to use (every candidate cooling down, used up,
# or an author of the PR) and released its lease without working.
MODEL_PICK_STEP = "Select execution candidate at dispatch time"
SESSION_STEP = "Run continuous fixed-model factory session"
WORKER_JOB = "run / factory"
MODEL_PICK_WINDOW = timedelta(hours=1)
# Workers may run up to two hours, so look for starts well before the window.
MODEL_PICK_LOOKBACK = timedelta(hours=3)
MODEL_PICK_SAMPLE = 15


def _first_failed_step(jobs: list[Obj]) -> str:
    """The first failing step of the worker job (else of any job), or ""."""
    ordered = sorted(jobs, key=lambda job: job_name(job) != WORKER_JOB)
    for job in ordered:
        for step in items(job, "steps"):
            if string(step, "conclusion") == "failure":
                return string(step, "name")
    return ""


def model_availability(github: GitHub, now: datetime, tracker: Tracker) -> Finding:
    """Are workers getting a model and finishing? Every worker failing stalls the factory.

    A worker that fails at the model-pick step had no model it was allowed to
    use. The other checks only see workers that finished their session, so a
    fleet where every worker failed this way looked healthy. Counts workers
    that finished in the last hour, whenever they started.
    """
    start = (now - MODEL_PICK_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    listed = items(
        github.repo_api(
            f"actions/workflows/{WORKER_WORKFLOW}/runs?status=completed"
            f"&created={start}..{until}&per_page=100"
        ),
        "workflow_runs",
    )
    finished = [
        run
        for run in listed
        if (done := timestamp(run, "updated_at")) is not None
        and now - MODEL_PICK_WINDOW <= done <= now
    ]
    finished.sort(key=lambda run: string(run, "updated_at"), reverse=True)
    runs = finished[:MODEL_PICK_SAMPLE]
    if not runs:
        return Finding("model availability", "ok", "no worker finished in the last hour")
    succeeded = no_model = in_session = ran = 0
    elsewhere: Counter[str] = Counter()
    other = 0  # cancelled, timed out, skipped: not a success, cause unknown
    for run in runs:
        conclusion = string(run, "conclusion")
        if conclusion == "success":
            succeeded += 1
            continue
        run_id = integer(run, "id")
        if conclusion != "failure" or run_id is None:
            other += 1
            continue
        jobs = tracker.jobs(run_id)
        worker = next((job for job in jobs if job_name(job) == WORKER_JOB), None)
        if worker is not None and string(worker, "conclusion") == "success":
            # The worker had a model and ran; the run failed afterwards (its
            # recorded outcome, e.g. no change delivered).
            ran += 1
            continue
        step = _first_failed_step(jobs)
        if step == MODEL_PICK_STEP:
            no_model += 1
        elif step == SESSION_STEP:
            in_session += 1
        else:
            elsewhere[step or "an unknown step"] += 1
    unsuccessful = len(runs) - succeeded
    if no_model == 0 and unsuccessful * 2 <= len(runs):
        return Finding("model availability", "ok", "workers are finding models and finishing")
    parts = [f"{succeeded} of the last {len(runs)} finished workers succeeded"]
    if no_model:
        parts.append(
            f"{no_model} could not pick any model (every candidate cooling down, used up, "
            "or an author of the PR)"
        )
    if in_session:
        parts.append(f"{in_session} failed during their session")
    if ran:
        parts.append(f"{ran} ran their session but ended without a successful outcome")
    for step, count in elsewhere.most_common(2):
        parts.append(f"{count} failed at '{step}'")
    if other:
        parts.append(f"{other} were cancelled or timed out")
    hint = "rotisserie factory report shows the failed runs"
    if no_model or in_session:
        hint = (
            "rotisserie factory stats shows provider health; cooldowns clear by themselves. "
            "If they are wrong: gh workflow run factory-model-health-reset.yml"
        )
    # No worker succeeding means nothing is being delivered at all.
    status = "fail" if succeeded == 0 else "warn"
    return Finding("model availability", status, "; ".join(parts), hint)


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


def main_verification(github: GitHub, health: main_health.MainHealth | None = None) -> Finding:
    """Serialized merges wait until the exact main commit has passed product CI."""
    health = health or main_health.assess(github)
    short = health.sha[:12]
    if health.state == "green":
        return Finding("main verification", "ok", health.explanation)
    if health.state == "pending":
        return Finding(
            "main verification", "ok", health.explanation, "merges wait for this run to finish"
        )
    if health.state == "red":
        return Finding(
            "main verification",
            "fail",
            health.explanation,
            "only PRs labelled main-breakage may merge until main is green again",
        )
    return Finding(
        "main verification",
        "warn",
        health.explanation,
        "merges wait for it; doctor --fix dispatches product CI on main now",
        fix="verify-main",
        targets=(f"main@{short}",),
    )


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


def shared_failures(github: GitHub, now: datetime) -> Finding:
    """Backlog PRs red because of main, not because of their own change."""
    variables = providers.repository_variables(github)
    board = queue.summarize_queue(github, now=int(now.timestamp()), variables=variables).board
    backlog = runtime.completion_backlog(list(board.prs))
    red = {item.number: checks.failing_checks(github, item.number) for item in backlog}
    report = baseline.analyze(github, {number: found for number, found in red.items() if found})
    broken = [item for item in report.incidents if item.state == "broken"]
    if broken:
        first = broken[0]
        return Finding(
            "shared failures",
            "fail",
            first.text,
            (f"Likely cause: {first.cause}. " if first.cause else "")
            + "rotisserie factory unstick --dry-run shows the evidence; no worker is sent "
            "to these PRs until main is fixed",
        )
    # Conflicting PRs get merge-conflict work, which merges main; update-branch cannot.
    free = {item.number for item in backlog if item.free and not item.conflicted}
    syncs = []
    waiting = []
    for number in sorted(red):
        if not report.needs_sync(number):
            continue
        if number not in free:
            waiting.append(f"#{number}")
            continue
        head = string(obj(obj(github.repo_api(f"pulls/{number}")).get("head")), "sha")
        if head:
            syncs.append(f"pr:{number}@{head}")
    held = (
        f"; {', '.join(waiting)} also need it once their workers finish (a review or "
        "repair round on the old base fails validation on main's old files)"
        if waiting
        else ""
    )
    if syncs:
        names = ", ".join(f"#{item.split('@')[0][3:]}" for item in syncs)
        return Finding(
            "shared failures",
            "warn",
            f"main has moved past the failures on {names}; their branches need main merged "
            f"in{held}",
            fix="sync-main",
            targets=tuple(syncs),
        )
    if waiting:
        return Finding(
            "shared failures",
            "warn",
            f"main has moved past the failures on {', '.join(waiting)}, but workers hold them; "
            "sync each once its worker finishes",
            "rotisserie factory doctor --fix syncs the ones no worker holds",
        )
    return Finding("shared failures", "ok", "no backlog PR is red because of main")


def review_deadlocks(github: GitHub, pulls: list[Obj] | None = None) -> Finding:
    """PRs held by a deadlock the controller does not clear by itself."""
    found = stalls.scan(github, pulls if pulls is not None else review.open_pulls(github))
    if not found:
        return Finding(
            "review deadlocks", "ok", "no PR is stuck on an answered thread or an empty verdict"
        )
    names = ", ".join(sorted({f"#{stall.pr}" for stall in found}))
    return Finding(
        "review deadlocks",
        "warn",
        f"{len(found)} deadlock(s) on {names}: " + "; ".join(stall.text for stall in found[:3]),
        hint="rotisserie factory review pr:N shows each one and what clears it",
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
        ("main verification", lambda: main_verification(github)),
        ("rotisserie intake", lambda: _intake(github, now)),
        ("worker rejections", rejections),
        ("model availability", lambda: model_availability(github, now, tracker)),
        ("shared failures", lambda: shared_failures(github, now)),
        ("review deadlocks", lambda: review_deadlocks(github)),
    ]
    for index, (name, check) in enumerate(checks):
        try:
            found.append(check())
        except GitHubRateLimitError as exc:
            found.append(Finding(name, "warn", "not checked: GitHub API is rate limited"))
            found.append(
                Finding(
                    "GitHub API",
                    "fail",
                    str(exc),
                    secondary_limit=exc.secondary,
                    retry_after_seconds=exc.retry_after_seconds,
                )
            )
            skipped = [label for label, _ in checks[index + 1 :]]
            if skipped:
                found.append(
                    Finding(
                        "checks skipped",
                        "warn",
                        f"{len(skipped)} checks not run because GitHub is rate limited: "
                        + ", ".join(skipped),
                    )
                )
            break  # Do not continue hammering a throttled GitHub API.
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
    if "verify-main" in wanted:
        # Re-read first: the drain or the dispatcher may have started CI since.
        health = main_health.assess(github)
        started = main_health.dispatch_verification(github, health)
        if started:
            done.append(
                Applied(
                    "verify-main",
                    f"started {', '.join(started)} on main {health.sha[:12]}",
                )
            )
    for finding in findings:
        if finding.fix != "sync-main" or finding.status == "ok":
            continue
        for target in finding.targets:
            ref, head = target.split("@", 1)
            result = unstick.sync(github, unstick.Sync(int(ref[3:]), head, "", ""))
            if result.ok:
                done.append(Applied("sync-main", result.text))
    return done
