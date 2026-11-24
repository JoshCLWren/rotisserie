"""Bounded GitHub reads behind ``baste turning``: worker starts, dispatcher passes, failures.

Each function reads a fixed, small number of pages. The parsing helpers are
pure so tests can feed them recorded payloads and dispatcher logs.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from rotisserie.factory_console.github import GitHub, Obj, integer, items, string, timestamp
from rotisserie.factory_console.service.failures import PERSIST_JOB, first_failed_step
from rotisserie.factory_console.tracker import DISPATCH_WORKFLOW, WORKER_WORKFLOW, Tracker

ASSIGN_STEP = "Resolve and dispatch fixed workers"
STORE_STEP = "Initialize the factory store"
CONTROL_WORKFLOWS = {
    DISPATCH_WORKFLOW: "dispatcher",
    "factory-status-pages.yml": "status pages",
    "rotisserie-label-claim.yml": "label claim",
    "factory-ready-merge-drain.yml": "ready-merge drain",
    "factory-completion-drain.yml": "completion drain",
}
FAILED = ("failure", "timed_out", "startup_failure")
WORKER_PAGE = 30
DISPATCH_PAGE = 8
PERSIST_SAMPLE = 5
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[[0-9;]*m")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ")
ELIGIBLE = re.compile(r"(?:dispatch|batch): (\d+) eligible workers")
NO_WORKERS = "No executable workers in this roster tick"
CAP_EXHAUSTED = "free-entry cap is exhausted"
CAPACITY = re.compile(r"free-entry capacity: (\{.*\})")
ASSIGNED = re.compile(r"^Factory \d+ assignment: ")
NONE = re.compile(r"^Factory \d+: no executable control-plane assignment(?P<tail>.*)$")
UNAVAILABLE = re.compile(r"^Factory \d+: unavailable this tick")
BROKEN_WORDS = "controller assignment failed|unusable controller"
BROKEN = re.compile(rf"^(?:Factory \d+: (?:{BROKEN_WORDS})|Unable to dispatch Factory \d+)")
SKIPPED = re.compile(r"^Factory \d+ skipped: source (\S+) (.+?) reached")
OFF = re.compile(r"^Factory \d+: (?:provider )?(\S+) (is turned off|missing provider credentials)")
REASON = re.compile(r"reason[=:]\s*\"?([A-Za-z0-9_.-]+)")


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class DispatcherPass:
    run_id: int = 0
    url: str = ""
    at: str = ""
    store: str = ""  # the store step's conclusion, empty when the job had none
    eligible: int | None = None
    assigned: int = 0
    none: int = 0
    unavailable: int = 0
    broken: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    reason_logged: bool = False
    cap_exhausted: bool = False
    capacity_remaining: int | None = None
    filtered: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "url": self.url,
            "at": self.at,
            "store_step": self.store or None,
            "eligible": self.eligible,
            "assigned": self.assigned,
            "none": self.none,
            "unavailable": self.unavailable,
            "broken": self.broken,
            "none_reasons": dict(self.reasons.most_common()),
            "none_reason_logged": self.reason_logged,
            "cap_exhausted": self.cap_exhausted,
            "capacity_remaining": self.capacity_remaining,
            "filtered": dict(self.filtered.most_common()),
        }


def _none_reason(tail: str) -> str:
    text = tail.strip().lstrip(":;,-( ").rstrip(") .")
    found = REASON.search(text)
    if found:
        return found.group(1)
    return text[:80]


def parse_pass(log: str) -> DispatcherPass:
    """Counts from the assign step's output lines; echoed script source never matches."""
    found = DispatcherPass()
    for raw in log.splitlines():
        line = STAMP.sub("", ANSI.sub("", raw)).strip()
        if match := ELIGIBLE.search(line):
            found.eligible = int(match.group(1))
        elif line.startswith(NO_WORKERS):
            found.eligible = 0
        elif CAP_EXHAUSTED in line and not line.startswith("echo"):
            found.cap_exhausted = True
        elif match := CAPACITY.search(line):
            try:
                found.capacity_remaining = integer(json.loads(match.group(1)), "remaining")
            except ValueError:
                pass
        elif ASSIGNED.match(line):
            found.assigned += 1
        elif match := NONE.match(line):
            found.none += 1
            reason = _none_reason(match.group("tail"))
            if reason:
                found.reason_logged = True
            found.reasons[reason or "not logged"] += 1
        elif UNAVAILABLE.match(line):
            found.unavailable += 1
        elif BROKEN.match(line):
            found.broken += 1
        elif match := SKIPPED.match(line):
            found.filtered[f"{match.group(1)} {match.group(2)}"] += 1
        elif match := OFF.match(line):
            found.filtered[f"{match.group(1)} {match.group(2)}"] += 1
    return found


def _step(job: Obj, name: str) -> str:
    for step in items(job, "steps"):
        if string(step, "name") == name:
            return string(step, "conclusion") or string(step, "status")
    return ""


def dispatcher_passes(
    github: GitHub, tracker: Tracker, count: int
) -> tuple[list[DispatcherPass], str]:
    """The newest ``count`` passes that ran the assign step, and the newest pass time."""
    runs = items(
        github.repo_api(f"actions/workflows/{DISPATCH_WORKFLOW}/runs?per_page={DISPATCH_PAGE}"),
        "workflow_runs",
    )
    newest = string(runs[0], "created_at") if runs else ""
    done = [run for run in runs if string(run, "status") == "completed"]

    def read(run: Obj) -> DispatcherPass | None:
        run_id = integer(run, "id") or 0
        for job in tracker.jobs(run_id):
            assign = _step(job, ASSIGN_STEP)
            if assign in ("", "skipped"):
                continue
            found = parse_pass(tracker.log(integer(job, "id") or 0))
            found.run_id = run_id
            found.url = string(run, "html_url")
            found.at = string(run, "created_at")
            found.store = _step(job, STORE_STEP)
            return found
        return None

    passes: list[DispatcherPass] = []
    for start in range(0, len(done), count):
        with ThreadPoolExecutor(max_workers=count) as pool:
            batch = list(pool.map(read, done[start : start + count]))
        passes += [item for item in batch if item is not None]
        if len(passes) >= count:
            break
    return passes[:count], newest


@dataclass(frozen=True)
class WorkerRuns:
    last_started: str
    last_outcome: str
    running: int
    runs: list[Obj]


def worker_runs(github: GitHub) -> WorkerRuns:
    runs = items(
        github.repo_api(f"actions/workflows/{WORKER_WORKFLOW}/runs?per_page={WORKER_PAGE}"),
        "workflow_runs",
    )
    started = max((string(run, "created_at") for run in runs), default="")
    finished = [string(run, "updated_at") for run in runs if string(run, "status") == "completed"]
    running = sum(1 for run in runs if string(run, "status") != "completed")
    return WorkerRuns(started, max(finished, default=""), running, runs)


@dataclass(frozen=True)
class ControlFailure:
    workflow: str
    url: str
    at: str
    step: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"workflow": self.workflow, "url": self.url, "at": self.at, "step": self.step}


def control_failures(
    github: GitHub, tracker: Tracker, since: datetime, workers: list[Obj]
) -> list[ControlFailure]:
    """Failed control workflow runs since ``since``, plus Entry runs whose persist job failed."""
    listed = items(
        github.repo_api(f"actions/runs?status=failure&created=%3E%3D{iso(since)}&per_page=100"),
        "workflow_runs",
    )
    found: list[ControlFailure] = []
    for run in listed:
        name = CONTROL_WORKFLOWS.get(string(run, "path").rsplit("/", 1)[-1])
        if name and string(run, "conclusion") in FAILED:
            found.append(ControlFailure(name, string(run, "html_url"), string(run, "updated_at")))
    failed = [
        run
        for run in workers
        if string(run, "conclusion") in FAILED
        and (done := timestamp(run, "updated_at")) is not None
        and done >= since
    ]
    failed.sort(key=lambda run: string(run, "updated_at"), reverse=True)

    def persist(run: Obj) -> ControlFailure | None:
        job, step = first_failed_step(tracker.jobs(integer(run, "id") or 0))
        if not job.startswith(PERSIST_JOB):
            return None
        return ControlFailure(
            "Entry persist", string(run, "html_url"), string(run, "updated_at"), step
        )

    with ThreadPoolExecutor(max_workers=PERSIST_SAMPLE) as pool:
        found += [row for row in pool.map(persist, failed[:PERSIST_SAMPLE]) if row is not None]
    found.sort(key=lambda row: row.at, reverse=True)
    return found
