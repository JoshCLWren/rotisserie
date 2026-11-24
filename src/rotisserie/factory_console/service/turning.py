"""Is the factory turning? One verdict from worker starts, dispatcher passes and failures.

``gather`` runs bounded reads side by side; a read that fails is named in
``unread`` and never fails the verdict. ``assess`` is pure: the CLI, ``--follow``,
doctor and the tests all call it, so they always agree. Worker starts come from
GitHub runs: the store's lifecycle events are readable only inside trusted jobs.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubRateLimitError,
    Obj,
    items,
    obj,
    string,
)
from rotisserie.factory_console.service import db_usage, providers, queue, reliability, sync
from rotisserie.factory_console.service import turning_reads as reads
from rotisserie.factory_console.tracker import Clock, Tracker

IDLE = timedelta(minutes=45)
WINDOW = timedelta(hours=1)
FAILURES = 3
PASSES = 3
SCHEDULER_LATE = timedelta(minutes=20)
READ_ERRORS = (GitHubError, RuntimeError, ValueError, OSError)
TURNING, SLOWING, BURNING = "turning", "slowing", "burning"
CHECK = "work turning"
NEXT = {
    "store-unreachable": "baste store selftest",
    "scheduler-stopped": "baste doctor",
    "dispatcher-none": "baste unstick --dry-run",
    "dispatch-broken": "baste doctor",
    "review-limit": "baste unstick --dry-run",
    "wip-limit": "baste unstick --dry-run",
    "no-capacity": "baste backoffs --via github",
    "unknown": "baste why",
    "control-failures": "baste failures",
    "scheduler-late": "baste doctor",
}


@dataclass
class Evidence:
    now: datetime
    idle: timedelta = IDLE
    window: timedelta = WINDOW
    threshold: int = FAILURES
    workers: reads.WorkerRuns | None = None
    passes: list[reads.DispatcherPass] = field(default_factory=list)
    newest_pass: str = ""
    failures: list[reads.ControlFailure] = field(default_factory=list)
    providers: dict[str, Any] = field(default_factory=dict)
    backlog: dict[str, Any] = field(default_factory=dict)
    holds: dict[str, Any] = field(default_factory=dict)
    store: dict[str, Any] = field(default_factory=dict)
    unread: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reason: str
    text: str
    next: str
    evidence: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "text": self.text,
            "next": self.next,
            "evidence": self.evidence,
        }


def moment(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def minutes_words(total: int) -> str:
    return f"{total // 60}h {total % 60:02d}m" if total >= 60 else f"{total}m"


def _minutes(span: timedelta) -> str:
    return minutes_words(int(span.total_seconds() // 60))


def _burning_reason(found: Evidence) -> tuple[str, str]:
    passes = found.passes
    latest = passes[0] if passes else None
    if (latest and latest.store == "failure") or found.store.get("blocked"):
        return "store-unreachable", "The factory store did not open on the last dispatcher pass."
    offered = [item for item in passes if item.eligible]
    if offered and all(item.assigned == 0 and item.none for item in offered):
        reasons: Counter[str] = Counter()
        for item in offered:
            reasons.update(item.reasons)
        logged = Counter({name: n for name, n in reasons.items() if name != "not logged"})
        why = (
            "the controller said " + ", ".join(f"{k} ({n})" for k, n in logged.most_common(3))
            if logged
            else "this dispatcher does not log the controller's reason"
        )
        text = (
            f"The last {len(offered)} dispatcher passes found {offered[0].eligible} eligible "
            f"workers and assigned none; {why}."
        )
        if found.backlog.get("limit_text"):
            text += f" {found.backlog['limit_text']}"
        return "dispatcher-none", text
    if any(item.broken for item in passes):
        return "dispatch-broken", "The dispatcher failed to assign or start workers."
    limit = found.backlog.get("limit")
    if limit:
        return str(limit), str(found.backlog.get("limit_text"))
    if passes and all(not item.eligible or item.cap_exhausted for item in passes):
        return "no-capacity", "No worker was eligible: every provider is capped, cooling or off."
    if found.providers.get("all_paused"):
        return "no-capacity", "Every provider with models is cooling down."
    newest = moment(found.newest_pass)
    if newest is None or found.now - newest > SCHEDULER_LATE:
        return "scheduler-stopped", "The dispatcher has not run in the last 20 minutes."
    return "unknown", "No single cause was found in the dispatcher passes."


def assess(found: Evidence) -> Verdict:
    workers = found.workers
    started = moment(workers.last_started) if workers else None
    idle_for = found.now - started if started else None
    work = found.backlog.get("eligible_issues", 0) + found.backlog.get("open_prs", 0)
    work_known = "eligible_issues" in found.backlog
    offered = any(item.eligible for item in found.passes)
    work_or_workers = offered or work > 0 or not (work_known or found.passes)
    evidence = evidence_dict(found)
    if (idle_for is None or idle_for > found.idle) and work_or_workers:
        reason, text = _burning_reason(found)
        idle_words = (
            "No worker has ever started"
            if idle_for is None
            else (f"No worker has started for {_minutes(idle_for)}")
        )
        return Verdict(BURNING, reason, f"{idle_words}. {text}", NEXT[reason], evidence)
    if len(found.failures) >= found.threshold:
        names = Counter(row.workflow for row in found.failures)
        listed = ", ".join(f"{name} {count}" for name, count in names.most_common())
        text = (
            f"{len(found.failures)} control runs failed in the last {_minutes(found.window)} "
            f"({listed})."
        )
        command = "baste failures" if "Entry persist" in names else "baste doctor"
        return Verdict(SLOWING, "control-failures", text, command, evidence)
    newest = moment(found.newest_pass)
    if found.passes and newest and found.now - newest > SCHEDULER_LATE:
        text = f"Workers run, but the dispatcher last ran {_minutes(found.now - newest)} ago."
        return Verdict(SLOWING, "scheduler-late", text, NEXT["scheduler-late"], evidence)
    if idle_for is not None and idle_for > found.idle:
        text = f"No worker for {_minutes(idle_for)}, and there is no work or worker to start."
        return Verdict(TURNING, "nothing-to-do", text, "", evidence)
    since = "" if idle_for is None else f"The last worker started {_minutes(idle_for)} ago."
    return Verdict(TURNING, "moving", since, "", evidence)


def evidence_dict(found: Evidence) -> dict[str, object]:
    workers = found.workers
    return {
        "starts_source": "github runs",
        "last_worker_started": workers.last_started if workers else None,
        "last_outcome_recorded": workers.last_outcome if workers else None,
        "workers_running": workers.running if workers else None,
        "idle_threshold_minutes": int(found.idle.total_seconds() // 60),
        "dispatcher_passes": [item.to_dict() for item in found.passes],
        "dispatcher_last_ran": found.newest_pass or None,
        "control_failures": [row.to_dict() for row in found.failures],
        "failure_window_minutes": int(found.window.total_seconds() // 60),
        "failure_threshold": found.threshold,
        "providers": found.providers,
        "backlog": found.backlog,
        "holds": found.holds,
        "store": found.store,
        "unread": sorted(found.unread),
    }


def provider_state(stats: Obj | None, now: datetime) -> dict[str, Any]:
    if stats is None:
        return {"read": False}
    models: dict[str, list[str]] = {}
    for row in items(stats, "models"):
        status = reliability.health_status(obj(row.get("health")), now)
        if status != "retired":
            models.setdefault(string(row, "source"), []).append(status)
    paused = sorted(src for src, states in models.items() if states and set(states) == {"paused"})
    return {
        "read": True,
        "sources": len(models),
        "cooling": paused,
        "all_paused": bool(models) and len(paused) == len(models),
    }


def backlog_state(github: GitHub, now: datetime) -> dict[str, Any]:
    board = queue.read_board(github, providers.repository_variables(github), int(now.timestamp()))
    explained = [queue.explain(board, row, blocker=None) for row in board.issues]
    backlog = runtime.completion_backlog(list(board.prs))
    found: dict[str, Any] = {
        "eligible_issues": sum(1 for item in explained if item.eligible),
        "open_prs": len(backlog),
        "limit": "",
        "limit_text": "",
    }
    sample = explained[0] if explained else None
    if sample is not None:
        if sample.review_backlog >= sample.review_backlog_limit:
            found["limit"] = "review-limit"
            found["limit_text"] = (
                f"The review backlog is at its limit ({sample.review_backlog} of "
                f"{sample.review_backlog_limit}), so new issue work waits."
            )
        elif sample.pr_wip >= sample.pr_wip_limit:
            found["limit"] = "wip-limit"
            found["limit_text"] = (
                f"The open-PR limit is reached ({sample.pr_wip} of {sample.pr_wip_limit})."
            )
    return found


def hold_state(github: GitHub) -> dict[str, Any]:
    if not sync.store_configured():
        return {"expired": None, "note": "store leases are readable only in trusted jobs"}
    report = sync.lease_report(github)
    return {"expired": sum(1 for row in report.rows if not row.active), "note": ""}


def store_state(stats: Obj | None) -> dict[str, Any]:
    usage = db_usage.from_stats(stats)
    blocked = obj(usage.get("blocked"))
    return {
        "db_usage_status": usage.get("status"),
        "db_usage_checked_at": usage.get("checked_at"),
        "blocked": bool(blocked.get("reads") or blocked.get("writes")),
    }


def gather(
    github: GitHub,
    *,
    now: datetime,
    clock: Clock | None = None,
    idle: timedelta = IDLE,
    window: timedelta = WINDOW,
    threshold: int = FAILURES,
    passes: int = PASSES,
) -> Evidence:
    found = Evidence(now=now, idle=idle, window=window, threshold=threshold)
    tracker = Tracker(github, clock)

    def workers() -> None:
        found.workers = reads.worker_runs(github)
        found.failures = reads.control_failures(github, tracker, now - window, found.workers.runs)

    def dispatcher() -> None:
        found.passes, found.newest_pass = reads.dispatcher_passes(github, tracker, passes)

    def stats() -> None:
        snapshot = reliability.load_stats(github)
        found.providers = provider_state(snapshot, now)
        found.store = store_state(snapshot)

    def backlog() -> None:
        found.backlog = backlog_state(github, now)

    def holds() -> None:
        found.holds = hold_state(github)

    steps: dict[str, Callable[[], None]] = {
        "worker runs": workers,
        "dispatcher passes": dispatcher,
        "reliability snapshot": stats,
        "backlog": backlog,
        "holds": holds,
    }

    def attempt(name: str) -> None:
        try:
            steps[name]()
        except GitHubRateLimitError:
            raise
        except READ_ERRORS:
            found.unread.append(name)

    with ThreadPoolExecutor(max_workers=len(steps)) as pool:
        list(pool.map(attempt, steps))
    if found.passes:
        found.store["dispatcher_store_step"] = found.passes[0].store or None
    return found


def health(github: GitHub, now: datetime, clock: Clock | None = None) -> tuple[str, str, str]:
    """(status, text, hint) for doctor: burning fails, slowing warns."""
    verdict = assess(gather(github, now=now, clock=clock))
    status = {BURNING: "fail", SLOWING: "warn"}.get(verdict.verdict, "ok")
    text = f"{verdict.verdict.capitalize()}: {verdict.text}".strip()
    return status, text, verdict.next


def longest_gap(
    runs: list[Obj], since: datetime, now: datetime, idle: timedelta = IDLE
) -> dict[str, object] | None:
    """The longest stretch in [since, now] with no worker start, when it passed ``idle``."""
    starts = sorted(
        start
        for run in runs
        if (start := moment(string(run, "created_at"))) is not None and since <= start <= now
    )
    edges = [since, *starts, now]
    best = max(zip(edges, edges[1:], strict=False), key=lambda pair: pair[1] - pair[0])
    if best[1] - best[0] <= idle:
        return None
    return {
        "from": best[0].isoformat(),
        "to": best[1].isoformat(),
        "minutes": int((best[1] - best[0]).total_seconds() // 60),
        "ongoing": best[1] == now,
    }
