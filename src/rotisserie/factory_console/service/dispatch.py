"""Submit work to the factory scheduler. Watching and reporting are the caller's job."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.tracker import DISPATCH_WORKFLOW, Clock, Tracker


@dataclass
class Request:
    """One worker request, exactly as the scheduler workflow takes it."""

    provider: runtime.Provider
    model: str
    target: str = ""
    force: bool = False
    slot: int | None = None
    action: str = "advance"
    take_over: bool = False

    def inputs(self) -> dict[str, str]:
        values = {
            "provider": self.provider.key,
            "model": self.model,
            "mode": "smoke",
            "action": self.action,
            "force_slot": "true" if self.force else "false",
        }
        if self.target:
            values["target"] = self.target
        if self.slot is not None:
            values["slot_index"] = str(self.slot)
        return values

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider.key,
            "model": self.model,
            "target": self.target or None,
            "force": self.force,
            "slot": self.slot,
            "action": self.action,
            "take_over": self.take_over,
        }


@dataclass(frozen=True)
class Submission:
    """A request GitHub accepted. ``run_id`` is None when its run could not be found."""

    request: Request
    submitted_at: datetime
    run_id: int | None
    url: str
    raw: str  # what ``gh workflow run`` printed, for diagnostics


def submit(github: GitHub, clock: Clock, request: Request) -> Submission:
    """Send one request to the scheduler and find its run."""
    submitted_at = clock.now()
    output = github.workflow_run(DISPATCH_WORKFLOW, request.inputs())
    match = re.search(r"/actions/runs/(\d+)", output)
    run_id = (
        int(match.group(1))
        if match
        else Tracker(github, clock).find_submitted_run(
            submitted_at, f"Factory dispatch · {request.target or request.provider.key}"
        )
    )
    url = (
        f"https://github.com/{github.repo}/actions/runs/{run_id}"
        if run_id is not None
        else f"https://github.com/{github.repo}/actions/workflows/{DISPATCH_WORKFLOW}"
    )
    return Submission(request, submitted_at, run_id, url, output)
