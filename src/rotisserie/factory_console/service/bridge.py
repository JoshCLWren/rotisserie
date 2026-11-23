"""Run the outside-agent workflows installed in an adopter and read their results.

An agent without the shared claim store (Neon) reserves, renews, releases and
hands off work through the adopter's trusted claim workflow, and reads the
ranked work list through its frontier workflow. Each call here starts one run,
waits for it to finish and returns the JSON artifact it published, so the CLI
can show the same result it shows for a direct call.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service.external import ACTIONS
from rotisserie.factory_console.tracker import Clock, Tracker

CLAIM_WORKFLOW = "rotisserie-external-claim.yml"
FRONTIER_WORKFLOW = "rotisserie-external-frontier.yml"
# The claim job has a 12-minute limit; allow for queueing on top of that.
CLAIM_TIMEOUT = 900.0
FRONTIER_TIMEOUT = 600.0
POLL_SECONDS = 10.0


class BridgeError(RuntimeError):
    """The workflow run could not be started, found, finished or read."""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message)
        self.url = url


@dataclass(frozen=True)
class BridgeRun:
    workflow: str
    run_id: int
    url: str
    conclusion: str
    data: dict[str, object]

    def summary(self) -> dict[str, object]:
        return {
            "workflow": self.workflow,
            "run_id": self.run_id,
            "url": self.url,
            "conclusion": self.conclusion,
        }


def _start(
    github: GitHub, clock: Clock, workflow: str, inputs: dict[str, str], title: str
) -> tuple[int, str]:
    submitted_at = clock.now()
    output = github.workflow_run(workflow, inputs)
    match = re.search(r"/actions/runs/(\d+)", output)
    run_id = (
        int(match.group(1))
        if match
        else Tracker(github, clock).find_submitted_run(submitted_at, title, workflow=workflow)
    )
    if run_id is None:
        listing = f"https://github.com/{github.repo}/actions/workflows/{workflow}"
        raise BridgeError(
            f"GitHub accepted the request, but its run did not appear. Check {listing}",
            url=listing,
        )
    return run_id, f"https://github.com/{github.repo}/actions/runs/{run_id}"


def _artifact(github: GitHub, run_id: int, name: str, url: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="rotisserie-bridge-") as folder:
        try:
            github.gh(
                "run",
                "download",
                str(run_id),
                "--repo",
                github.repo,
                "--name",
                name,
                "--dir",
                folder,
            )
        except GitHubError as exc:
            raise BridgeError(
                f"the run finished but its result could not be downloaded: {exc}", url=url
            ) from exc
        found = sorted(Path(folder).rglob("*.json"))
        if len(found) != 1:
            raise BridgeError("the run's result artifact holds no single JSON file", url=url)
        try:
            value: Any = json.loads(found[0].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BridgeError("the run's result is not valid JSON", url=url) from exc
    if not isinstance(value, dict):
        raise BridgeError("the run's result is not a JSON object", url=url)
    return {str(key): item for key, item in value.items()}


def run(
    github: GitHub,
    clock: Clock,
    workflow: str,
    inputs: dict[str, str],
    *,
    title: str,
    artifact: str,
    timeout: float,
    started: dict[str, object] | None = None,
) -> BridgeRun:
    """Start ``workflow``, wait until it completes, and return its JSON artifact.

    ``artifact`` may contain ``{run_id}``. ``started`` (when given) receives the
    run's URL as soon as it is known, so an interrupted caller can say where the
    run that may already have acted is.
    """
    run_id, url = _start(github, clock, workflow, inputs, title)
    if started is not None:
        started["bridge_run"] = url
    deadline = clock.now().timestamp() + timeout
    while True:
        status = obj(github.repo_api(f"actions/runs/{run_id}"))
        if string(status, "status") == "completed":
            break
        if clock.now().timestamp() >= deadline:
            raise BridgeError(
                f"the run did not finish within {int(timeout)}s; it may still act. Follow {url}",
                url=url,
            )
        clock.sleep(POLL_SECONDS)
    conclusion = string(status, "conclusion") or "unknown"
    data = _artifact(github, run_id, artifact.format(run_id=run_id), url)
    return BridgeRun(workflow, run_id, url, conclusion, data)


def claim(
    github: GitHub,
    clock: Clock,
    *,
    operation: str,
    client: str,
    claim_id: str = "",
    evidence: str = "",
    abilities: frozenset[str] = frozenset(),
    expected: str = "",
    started: dict[str, object] | None = None,
) -> BridgeRun:
    """Run one claim operation through the adopter's trusted claim workflow."""
    if abilities - ACTIONS:
        raise ValueError("unsupported agent abilities")
    inputs = {"operation": operation, "client": client}
    if abilities:
        inputs["capabilities"] = ",".join(sorted(abilities))
    if expected:
        inputs["target"] = expected
    if claim_id:
        inputs["claim_id"] = claim_id
    if evidence:
        inputs["evidence"] = evidence
    actor = github.whoami()
    return run(
        github,
        clock,
        CLAIM_WORKFLOW,
        inputs,
        title=f"External {operation} · {actor} / {client}",
        artifact="rotisserie-external-claim-{run_id}",
        timeout=CLAIM_TIMEOUT,
        started=started,
    )


def frontier(
    github: GitHub, clock: Clock, *, started: dict[str, object] | None = None
) -> BridgeRun:
    """Read the ranked work list through the adopter's read-only frontier workflow."""
    actor = github.whoami()
    return run(
        github,
        clock,
        FRONTIER_WORKFLOW,
        {},
        title=f"Rotisserie next work · {actor}",
        artifact="rotisserie-work-frontier-{run_id}",
        timeout=FRONTIER_TIMEOUT,
        started=started,
    )


def takeable(snapshot: dict[str, object], abilities: frozenset[str]) -> list[dict[str, object]]:
    """Offers an outside agent with these abilities could reserve right now.

    The same rule reserve-next applies: lease authority must be readable, and
    only unheld candidates for an action the agent can do count.
    """
    if snapshot.get("authoritative_claim_available") is not True:
        return []
    offers = snapshot.get("offers")
    if not isinstance(offers, list):
        return []
    found: list[dict[str, object]] = []
    for offer in offers:
        if (
            isinstance(offer, dict)
            and offer.get("status") == "candidate"
            and offer.get("action") in abilities
        ):
            found.append({str(key): value for key, value in offer.items()})
    return found
