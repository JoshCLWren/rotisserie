"""Label claim operations for agents and the console (issue #191 follow-up).

Every function returns plain data, so the CLI, the web UI and any other
consumer share one implementation. Bridge calls run the adopter's trusted
``rotisserie-label-claim.yml`` workflow; claim calls change only the
``factory:claimed`` label and wait for that workflow's answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    obj,
    string,
)
from rotisserie.factory_console.service import bridge, external
from rotisserie.factory_console.tracker import Clock

LABEL_WORKFLOW = "rotisserie-label-claim.yml"
LABEL_ARTIFACT = "rotisserie-label-claim-{run_id}"
CLAIMED = "factory:claimed"
STATUS_MARKER = "<!-- rotisserie-label-claim-status-v1 -->"
BOT = "github-actions[bot]"
DOCTOR_MODES = ("dry-run", "repair", "delete-legacy")
ANSWER_TIMEOUT = 600.0
TARGET = re.compile(r"(issue|pr):([1-9][0-9]*)\Z")


def parse_target(value: str) -> tuple[str, int]:
    match = TARGET.fullmatch(value)
    if not match:
        raise ValueError("target must look like issue:N or pr:N")
    return match.group(1), int(match.group(2))


# Bridge reads -----------------------------------------------------------------------


def doctor(
    github: GitHub,
    clock: Clock,
    mode: str,
    *,
    wait: bool = True,
    started: dict[str, object] | None = None,
) -> dict[str, object]:
    """Run the label doctor in the adopter's workflow; return its report or the run URL."""
    if mode not in DOCTOR_MODES:
        raise ValueError(f"mode must be one of {', '.join(DOCTOR_MODES)}")
    if not wait:
        run_id, url = bridge._start(github, LABEL_WORKFLOW, {"mode": mode})
        return {"status": "started", "bridge": {"run_id": run_id, "url": url}}
    result = bridge.run(
        github,
        clock,
        LABEL_WORKFLOW,
        {"mode": mode},
        artifact=LABEL_ARTIFACT,
        timeout=bridge.CLAIM_TIMEOUT,
        started=started,
    )
    return {**result.data, "bridge": result.summary()}


def leases(
    github: GitHub,
    clock: Clock,
    targets: list[str],
    *,
    started: dict[str, object] | None = None,
) -> dict[str, object]:
    """Neon lease holders (factory and outside claims), read by the trusted workflow."""
    for target in targets:
        parse_target(target)
    result = bridge.run(
        github,
        clock,
        LABEL_WORKFLOW,
        {"mode": "leases", "targets": " ".join(targets)},
        artifact=LABEL_ARTIFACT,
        timeout=bridge.CLAIM_TIMEOUT,
        started=started,
    )
    return {**result.data, "bridge": result.summary()}


# Controller runs for one item -------------------------------------------------------


def item_runs(
    github: GitHub, number: int, *, limit: int = 5, since: datetime | None = None
) -> list[dict[str, object]]:
    """The label claim controller's recent runs for one item, newest first."""
    listing = obj(github.repo_api(f"actions/workflows/{LABEL_WORKFLOW}/runs?per_page=50"))
    runs = listing.get("workflow_runs")
    rows: list[dict[str, object]] = []
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        title = string(run, "display_title")
        if f"#{number} " not in f"{title} ":
            continue
        created = string(run, "created_at")
        if since is not None and created:
            if datetime.fromisoformat(created.replace("Z", "+00:00")) < since:
                continue
        rows.append(
            {
                "run_id": run.get("id"),
                "url": string(run, "html_url"),
                "event": string(run, "event"),
                "actor": string(obj(run.get("actor")), "login"),
                "status": string(run, "status"),
                "conclusion": string(run, "conclusion"),
                "created_at": created,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def run_result(github: GitHub, row: dict[str, object]) -> dict[str, object]:
    """The JSON a finished controller run published, or why it is unavailable."""
    if row.get("status") != "completed":
        return {"result": "pending"}
    if row.get("conclusion") == "skipped":
        return {"result": "skipped", "reason": "the event was not a claim event"}
    run_id = int(str(row["run_id"]))
    try:
        return bridge._artifact(
            github, run_id, LABEL_ARTIFACT.format(run_id=run_id), str(row.get("url"))
        )
    except bridge.BridgeError as exc:
        return {"result": "unavailable", "reason": str(exc)}


def status_comment(github: GitHub, number: int) -> str:
    """The controller's one status comment on the item, or "" when there is none."""
    pages = json.loads(
        github.gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{github.repo}/issues/{number}/comments?per_page=100",
        )
    )
    for page in pages if isinstance(pages, list) else []:
        for row in page if isinstance(page, list) else []:
            if (
                isinstance(row, dict)
                and string(obj(row.get("user")), "login") == BOT
                and string(row, "body").startswith(STATUS_MARKER)
            ):
                return string(row, "body").removeprefix(STATUS_MARKER).strip()
    return ""


# Claims -----------------------------------------------------------------------------


def _wait_answer(
    github: GitHub,
    clock: Clock,
    number: int,
    since: datetime,
    timeout: float,
    on_progress: Callable[[str], None] | None,
) -> dict[str, object]:
    deadline = clock.now().timestamp() + timeout
    seen: dict[str, object] | None = None
    while True:
        rows = item_runs(github, number, limit=3, since=since)
        if rows:
            seen = rows[-1]
            if seen.get("status") == "completed":
                result = run_result(github, seen)
                return {
                    "run": seen,
                    "result": result,
                    "status_comment": status_comment(github, number),
                }
            if on_progress is not None:
                on_progress(f"controller run {seen.get('url')} is {seen.get('status')}")
        if clock.now().timestamp() >= deadline:
            where = seen.get("url") if seen else f"https://github.com/{github.repo}/actions"
            return {
                "run": seen,
                "result": {"result": "unknown", "reason": f"no answer within {int(timeout)}s"},
                "status_comment": status_comment(github, number),
                "follow": where,
            }
        clock.sleep(bridge.POLL_SECONDS)


def claim(
    github: GitHub,
    clock: Clock,
    target: str,
    *,
    wait: bool = True,
    timeout: float = ANSWER_TIMEOUT,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Add ``factory:claimed`` and return the controller's answer.

    The label is only a request: the answer (status, claim UUID, reason) comes
    from the trusted workflow, which takes the Neon lease or refuses.
    """
    _, number = parse_target(target)
    since = clock.now()
    github.gh(
        "api",
        f"repos/{github.repo}/issues/{number}/labels",
        "--method",
        "POST",
        "-f",
        f"labels[]={CLAIMED}",
    )
    if not wait:
        return {"status": "requested", "target": target}
    answer = _wait_answer(github, clock, number, since, timeout, on_progress)
    return {"target": target, **_summarize(answer), **answer}


def finish(
    github: GitHub,
    clock: Clock,
    target: str,
    *,
    wait: bool = True,
    timeout: float = ANSWER_TIMEOUT,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Remove ``factory:claimed`` (the finished signal) and return the hand-off answer."""
    _, number = parse_target(target)
    since = clock.now()
    try:
        github.gh(
            "api",
            f"repos/{github.repo}/issues/{number}/labels/{quote(CLAIMED, safe='')}",
            "--method",
            "DELETE",
        )
    except GitHubNotFoundError:
        return {"status": "not-claimed", "target": target, "reason": "factory:claimed is not set"}
    if not wait:
        return {"status": "requested", "target": target}
    answer = _wait_answer(github, clock, number, since, timeout, on_progress)
    return {"target": target, **_summarize(answer), **answer}


def _summarize(answer: dict[str, object]) -> dict[str, object]:
    result = answer.get("result")
    data = result if isinstance(result, dict) else {}
    keys = ("status", "reason", "claim_id", "expires_at", "operation", "evidence")
    return {key: data[key] for key in keys if key in data}


# Following an item to the end -------------------------------------------------------


def snapshot(github: GitHub, target: str) -> dict[str, object]:
    """One read of where an issue or PR stands in the factory lifecycle."""
    kind, number = parse_target(target)
    core = runtime.module("factory_work_policy")
    item = obj(github.repo_api(f"issues/{number}"))
    labels = core.labels_of(item)
    view: dict[str, object] = {
        "target": target,
        "state": string(item, "state"),
        "stage": core.stage_of(labels),
        "owner": core.owner_of(labels),
        "claim_labels": sorted(
            label
            for label in labels
            if label.startswith("factory:claim") or label == "factory:available"
        ),
        "done": "ralph-status:done" in labels,
    }
    if kind == "pr":
        pr = obj(github.repo_api(f"pulls/{number}"))
        view["merged"] = pr.get("merged") is True
        view["head"] = string(obj(pr.get("head")), "sha")
        view["checks"] = (
            external._required_checks_outcome(github, number)
            if string(pr, "state") == "open"
            else "closed"
        )
        linked = core.linked_issue_from_pr(
            {
                "number": number,
                "headRefName": string(obj(pr.get("head")), "ref"),
                "body": string(pr, "body"),
                "title": string(pr, "title"),
            }
        )
        if linked is not None and int(linked) != number:
            issue = obj(github.repo_api(f"issues/{linked}"))
            issue_labels = core.labels_of(issue)
            view["issue"] = {
                "target": f"issue:{linked}",
                "state": string(issue, "state"),
                "stage": core.stage_of(issue_labels),
                "done": "ralph-status:done" in issue_labels,
            }
    view["finished"], view["reason"] = _finished(view)
    return view


def _finished(view: dict[str, object]) -> tuple[bool, str]:
    if view.get("stage") == "factory:blocked":
        return True, "blocked"
    if str(view.get("target", "")).startswith("pr:"):
        if view.get("state") == "closed" and not view.get("merged"):
            return True, "closed without merging"
        if view.get("merged"):
            issue = view.get("issue")
            if not isinstance(issue, dict):
                return True, "merged"
            if issue.get("state") == "closed":
                return True, "merged and its issue is closed"
            if issue.get("stage") == "factory:blocked":
                return True, "merged, but its issue is blocked"
        return False, ""
    if view.get("state") == "closed":
        return True, "closed"
    return False, ""


CHANGE_KEYS = ("state", "stage", "owner", "claim_labels", "merged", "checks", "head", "issue")


def follow(
    github: GitHub,
    clock: Clock,
    target: str,
    *,
    interval: float = 60.0,
    timeout: float = 3600.0,
    on_change: Callable[[dict[str, object], list[str]], None] | None = None,
) -> dict[str, object]:
    """Poll ``snapshot`` until the item finishes, is held, or ``timeout`` passes."""
    deadline = clock.now().timestamp() + timeout
    previous: dict[str, object] = {}
    history: list[dict[str, object]] = []
    while True:
        try:
            view = snapshot(github, target)
        except GitHubError as exc:
            view = {**previous, "read_error": str(exc)}
        changed = [key for key in CHANGE_KEYS if view.get(key) != previous.get(key)]
        if changed or not history:
            entry = {"at": clock.now().isoformat(), **view}
            history.append(entry)
            if on_change is not None:
                on_change(view, changed)
        previous = view
        if view.get("finished"):
            return {"status": "finished", "reason": view.get("reason"), "history": history}
        if timeout and clock.now().timestamp() >= deadline:
            return {"status": "timeout", "history": history, "last": view}
        clock.sleep(interval)


def own_review(github: GitHub, offer: dict[str, Any], login: str) -> bool:
    """GitHub refuses a formal review from the PR's author, so the offer is not takeable."""
    if offer.get("action") != "review":
        return False
    _, number = parse_target(str(offer.get("target")))
    author = string(obj(obj(github.repo_api(f"pulls/{number}")).get("user")), "login")
    return author == login
