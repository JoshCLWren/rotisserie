"""Who is working on what: live claims joined with running workers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    items,
    label_names,
    string,
    timestamp,
)
from rotisserie.factory_console.tracker import WORKER_WORKFLOW, Tracker, session_started

OWNER_LABEL = re.compile(r"^factory:(\d+)$")
WORKER_TITLE = re.compile(r"^Factory (\d+) · fixed-model entry$")


@dataclass
class WorkerRun:
    run_id: int
    worker: str
    status: str
    started: datetime | None
    url: str
    coding_since: datetime | None = None


@dataclass
class Item:
    kind: str  # issue | pr
    number: int
    title: str
    labels: set[str]
    updated: datetime | None
    url: str
    owner: str = ""  # worker identity holding it, if any
    run: WorkerRun | None = None

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.number}"

    @property
    def provider(self) -> str:
        if not self.owner:
            return ""
        found = runtime.provider_for_worker(self.owner)
        return found.name if found else "local or unknown worker"


def owner_of(labels: set[str]) -> str:
    for label in sorted(labels):
        match = OWNER_LABEL.match(label)
        if match:
            return match.group(1)
    return ""


def open_items(github: GitHub, *, limit: int = 300) -> list[Item]:
    found: list[Item] = []
    page = 1
    while len(found) < limit:
        rows = items(github.repo_api(f"issues?state=open&per_page=100&page={page}&sort=updated"))
        if not rows:
            break
        for row in rows:
            number = integer(row, "number")
            if number is None:
                continue
            labels = label_names(row)
            found.append(
                Item(
                    kind="pr" if "pull_request" in row else "issue",
                    number=number,
                    title=string(row, "title").replace("\n", " "),
                    labels=labels,
                    updated=timestamp(row, "updated_at"),
                    url=string(row, "html_url"),
                    owner=owner_of(labels),
                )
            )
        if len(rows) < 100:
            break
        page += 1
    return found[:limit]


def active_worker_runs(github: GitHub) -> dict[str, WorkerRun]:
    """Running or queued worker runs, keyed by worker identity."""
    runs: dict[str, WorkerRun] = {}
    for status in ("in_progress", "queued"):
        rows = items(
            github.repo_api(
                f"actions/workflows/{WORKER_WORKFLOW}/runs?status={status}&per_page=100"
            ),
            "workflow_runs",
        )
        for row in rows:
            match = WORKER_TITLE.match(string(row, "display_title"))
            run_id = integer(row, "id")
            if match is None or run_id is None:
                continue
            runs.setdefault(
                match.group(1),
                WorkerRun(
                    run_id,
                    match.group(1),
                    string(row, "status"),
                    timestamp(row, "run_started_at") or timestamp(row, "created_at"),
                    string(row, "html_url"),
                ),
            )
    return runs


def current_work(github: GitHub, *, with_session: bool = True) -> list[Item]:
    """Every open item a worker holds, with its live worker run when there is one."""
    held = [item for item in open_items(github) if item.owner]
    runs = active_worker_runs(github)
    tracker = Tracker(github)
    for item in held:
        item.run = runs.get(item.owner)
        if item.run is not None and with_session:
            try:
                item.run.coding_since = session_started(tracker.jobs(item.run.run_id))
            except GitHubError:
                pass
    return sorted(
        held, key=lambda item: item.run.started.timestamp() if item.run and item.run.started else 0
    )


def lookup(github: GitHub, kind: str, number: int) -> Item:
    row = github.repo_api(f"issues/{number}")
    labels = label_names(row if isinstance(row, dict) else {})
    data: Obj = row if isinstance(row, dict) else {}
    item = Item(
        kind="pr" if "pull_request" in data else "issue",
        number=number,
        title=string(data, "title"),
        labels=labels,
        updated=timestamp(data, "updated_at"),
        url=string(data, "html_url"),
        owner=owner_of(labels),
    )
    if item.owner:
        item.run = active_worker_runs(github).get(item.owner)
    return item
