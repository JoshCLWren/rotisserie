"""Maintenance drain: turn off the workflows that start or record factory work.

``pause`` disables the adopter's dispatch, claim and state-writing workflows
and records why in the ``FACTORY_PAUSED`` repository variable. ``resume``
turns back on only the workflows that ``pause`` turned off, so a workflow
the owner had disabled before stays off. Running work is never cancelled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    items,
    obj,
    string,
)

VARIABLE = "FACTORY_PAUSED"
WORKFLOWS = {
    "fixed-model-factory-dispatch.yml": "dispatcher",
    "fixed-model-factory-dispatch-recovery.yml": "dispatch recovery",
    "free-model-factory-dispatch.yml": "compatibility dispatcher",
    "fixed-model-factory-roster-successor.yml": "roster successor",
    "rotisserie-label-claim.yml": "label claim",
    "rotisserie-external-claim.yml": "external claim",
    "fixed-model-runtime-observer.yml": "runtime observer",
    "factory-check-projection.yml": "check projection",
    "factory-status-pages.yml": "status pages",
}
CREDENTIALS = (
    "Pausing needs your own GitHub CLI login with the workflow scope "
    "(run: gh auth refresh -s workflow). A GitHub Actions or App token cannot "
    f"turn workflows off and on or write the {VARIABLE} repository variable."
)


class PauseCredentialError(RuntimeError):
    """The current GitHub credential cannot pause or resume the factory."""


@dataclass(frozen=True)
class Workflow:
    file: str
    name: str
    workflow_id: int
    state: str


def _files(value: object) -> list[str]:
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def require_owner_login(github: GitHub) -> str:
    try:
        raw = github.gh("api", "-i", "user")
    except GitHubError as exc:
        raise PauseCredentialError(f"{CREDENTIALS} ({exc})") from None
    head, _, body = raw.replace("\r\n", "\n").partition("\n\n")
    scopes: set[str] = set()
    for line in head.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "x-oauth-scopes":
            scopes = {scope.strip() for scope in value.split(",") if scope.strip()}
    if "workflow" not in scopes:
        raise PauseCredentialError(CREDENTIALS)
    try:
        return string(obj(json.loads(body)), "login")
    except ValueError:
        return ""


def workflows(github: GitHub) -> dict[str, Workflow]:
    found: dict[str, Workflow] = {}
    for page in range(1, 4):
        rows = items(github.repo_api(f"actions/workflows?per_page=100&page={page}"), "workflows")
        for row in rows:
            file = string(row, "path").rsplit("/", 1)[-1]
            workflow_id = row.get("id")
            if file in WORKFLOWS and isinstance(workflow_id, int):
                found[file] = Workflow(file, WORKFLOWS[file], workflow_id, string(row, "state"))
        if len(rows) < 100:
            break
    return found


def _read(github: GitHub) -> dict[str, object] | None:
    """The pause record, or ``None`` when none is set. Other read errors propagate."""
    try:
        row = obj(github.repo_api(f"actions/variables/{VARIABLE}"))
    except GitHubNotFoundError:
        return None
    try:
        data = json.loads(string(row, "value"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def record(github: GitHub) -> dict[str, object] | None:
    try:
        return _read(github)
    except GitHubError:
        return None


def state(github: GitHub) -> dict[str, object] | None:
    """Whether the factory is paused, for status, turning and recap.

    The variable carries the reason. A token that cannot read variables (the
    Actions token) still sees the pause in the dispatcher's own workflow state.
    """
    try:
        return _read(github)
    except GitHubError:
        pass
    try:
        dispatcher = workflows(github).get("fixed-model-factory-dispatch.yml")
    except GitHubError:
        return None
    if dispatcher is not None and dispatcher.state.startswith("disabled"):
        return {"since": "", "reason": "the dispatcher workflow is turned off", "disabled": []}
    return None


def pause(github: GitHub, *, reason: str, now: datetime) -> dict[str, object]:
    login = require_owner_login(github)
    found = workflows(github)
    previous = record(github) or {}
    already = _files(previous.get("disabled"))
    active = [row for row in found.values() if row.state == "active"]
    disabled = sorted({*already, *(row.file for row in active)})
    value = {
        "since": previous.get("since") or now.isoformat(timespec="seconds"),
        "reason": reason or str(previous.get("reason") or ""),
        "by": login,
        "disabled": disabled,
    }
    # The record goes first: if a disable fails midway, resume still knows every target.
    github.gh("variable", "set", VARIABLE, "--repo", github.repo, "--body", json.dumps(value))
    turned_off: list[str] = []
    errors: dict[str, str] = {}
    for row in active:
        try:
            github.repo_api(f"actions/workflows/{row.workflow_id}/disable", method="PUT")
            turned_off.append(row.file)
        except GitHubError as exc:
            errors[row.file] = str(exc)
    return {
        **value,
        "turned_off": turned_off,
        "left_off": sorted(row.file for row in found.values() if row.file not in disabled),
        "missing": sorted(set(WORKFLOWS) - set(found)),
        "errors": errors,
        "status": "paused" if not errors else "partial",
    }


def resume(github: GitHub) -> dict[str, object]:
    require_owner_login(github)
    found = workflows(github)
    previous = record(github)
    if previous is None:
        targets = [row.file for row in found.values() if row.state == "disabled_manually"]
        recorded = False
    else:
        targets = _files(previous.get("disabled"))
        recorded = True
    turned_on: list[str] = []
    errors: dict[str, str] = {}
    for file in targets:
        row = found.get(file)
        if row is None:
            errors[file] = "not installed"
            continue
        if row.state == "active":
            turned_on.append(file)
            continue
        try:
            github.repo_api(f"actions/workflows/{row.workflow_id}/enable", method="PUT")
            turned_on.append(file)
        except GitHubError as exc:
            errors[file] = str(exc)
    if not errors and recorded:
        try:
            github.gh("variable", "delete", VARIABLE, "--repo", github.repo)
        except GitHubNotFoundError:
            pass
    return {
        "recorded": recorded,
        "paused": previous or {},
        "turned_on": turned_on,
        "names": {file: WORKFLOWS.get(file, file) for file in turned_on},
        "errors": errors,
        "status": "resumed" if not errors else "partial",
    }
