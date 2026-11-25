"""Declare, list and end incidents: free workers go to one issue until it is fixed.

The state lives in repository variables (see ``factory_incident`` in the
factory runtime), so the next dispatcher tick obeys a change without a deploy.
Nothing here cancels a run that already started.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from rotisserie.factory_console.service import fleet, providers
from rotisserie.factory_console.service.labelclaims import _add, _remove

MAX_REASON = 200


class IncidentError(ValueError):
    """The operator named an issue that cannot become or stop being an incident."""


def _module() -> ModuleType:
    return runtime.module("factory_incident")


@dataclass(frozen=True)
class Incident:
    issue: int
    attempts: int
    reason: str = ""
    since: str = ""
    issue_state: str | None = None
    labeled: bool | None = None

    @property
    def active(self) -> bool:
        return self.issue_state != "closed" and self.labeled is not False

    def to_dict(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "attempts": self.attempts,
            "reason": self.reason or None,
            "since": self.since or None,
            "issue_state": self.issue_state,
            "labeled": self.labeled,
            "active": self.active,
        }


@dataclass(frozen=True)
class Change:
    action: str
    incident: Incident
    changed: bool
    closed_prs: list[int] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "changed": self.changed,
            **self.incident.to_dict(),
            "closed_prs": self.closed_prs,
            "incidents": [item.to_dict() for item in self.incidents],
        }


def _notes(variables: Mapping[str, str]) -> dict[str, dict[str, str]]:
    try:
        parsed = json.loads(variables.get(_module().NOTES_VAR, "") or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): {str(k): str(v) for k, v in value.items()}
        for key, value in parsed.items()
        if isinstance(value, dict)
    }


def state(variables: Mapping[str, str]) -> list[Incident]:
    module = _module()
    notes = _notes(variables)
    declared = module.parse(variables.get(module.INCIDENTS_VAR, ""))
    return [
        Incident(
            number,
            attempts,
            notes.get(str(number), {}).get("reason", ""),
            notes.get(str(number), {}).get("at", ""),
        )
        for number, attempts in sorted(declared.items())
    ]


def _issue(github: GitHub, number: int) -> dict[str, Any] | None:
    try:
        payload = json.loads(github.gh("api", f"repos/{github.repo}/issues/{number}"))
    except GitHubNotFoundError:
        return None
    return payload if isinstance(payload, dict) else None


def _labels(issue: Mapping[str, Any]) -> set[str]:
    return {
        str(label.get("name"))
        for label in issue.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    }


def _with_issue(github: GitHub, incident: Incident) -> Incident:
    issue = _issue(github, incident.issue)
    if issue is None:
        return incident
    return Incident(
        incident.issue,
        incident.attempts,
        incident.reason,
        incident.since,
        str(issue.get("state") or "").lower() or None,
        _module().LABEL in _labels(issue),
    )


def read(github: GitHub) -> list[Incident]:
    return [_with_issue(github, item) for item in state(providers.repository_variables(github))]


def active_issues(github: GitHub) -> frozenset[int]:
    """Issues of the active incidents; a failed read names none."""
    try:
        return frozenset(item.issue for item in read(github) if item.active)
    except GitHubRateLimitError:
        raise
    except GitHubError, ValueError, KeyError, TypeError:
        return frozenset()


def _save(github: GitHub, incidents: Mapping[int, int]) -> None:
    module = _module()
    fleet._write(github, module.INCIDENTS_VAR, module.format_incidents(incidents))


def _save_notes(github: GitHub, notes: dict[str, dict[str, str]]) -> None:
    fleet._write(github, _module().NOTES_VAR, json.dumps(notes, sort_keys=True) if notes else "")


def start(
    github: GitHub, number: int, *, attempts: int | None, reason: str, now: datetime
) -> Change:
    module = _module()
    wanted = module.DEFAULT_ATTEMPTS if attempts is None else attempts
    if not 1 <= wanted <= module.MAX_ATTEMPTS:
        raise IncidentError(f"--attempts must be between 1 and {module.MAX_ATTEMPTS}")
    issue = _issue(github, number)
    if issue is None or "pull_request" in issue:
        raise IncidentError(f"#{number} is not an issue in {github.repo}")
    if str(issue.get("state") or "").lower() != "open":
        raise IncidentError(f"Issue #{number} is closed; an incident needs an open issue")
    variables = fleet._current(github)
    declared = module.parse(variables.get(module.INCIDENTS_VAR, ""))
    changed = declared.get(number) != wanted
    if changed:
        _save(github, {**declared, number: wanted})
    notes = _notes(variables)
    note = notes.get(str(number), {})
    if changed or reason:
        notes[str(number)] = {
            "reason": (reason or note.get("reason", ""))[:MAX_REASON],
            "at": note.get("at") or now.isoformat(timespec="seconds"),
        }
        _save_notes(github, notes)
    if module.LABEL not in _labels(issue):
        _add(github, number, module.LABEL)
    incidents = read(github)
    current = next(item for item in incidents if item.issue == number)
    return Change("start", current, changed, incidents=incidents)


def _open_prs(github: GitHub) -> list[dict[str, Any]]:
    rows = json.loads(
        github.gh(
            "pr",
            "list",
            "--repo",
            github.repo,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number,title,body,labels,headRefName,state",
        )  # fmt: skip
    )
    return [row for row in rows if isinstance(row, dict)]


def stop(github: GitHub, number: int) -> Change:
    """End an incident. A closed issue's leftover attempt PRs close as superseded."""
    module = _module()
    variables = fleet._current(github)
    declared = module.parse(variables.get(module.INCIDENTS_VAR, ""))
    notes = _notes(variables)
    before = next((item for item in state(variables) if item.issue == number), None)
    changed = number in declared
    if changed:
        _save(github, {k: v for k, v in declared.items() if k != number})
    if notes.pop(str(number), None) is not None:
        _save_notes(github, notes)
    issue = _issue(github, number)
    closed: list[int] = []
    if issue is not None:
        if str(issue.get("state") or "").lower() == "closed":
            for pr in module.superseded_prs(_open_prs(github), number):
                github.gh(
                    "pr", "close", str(pr), "--repo", github.repo,
                    "--comment", module.superseded_comment(number),
                )  # fmt: skip
                closed.append(pr)
        if module.LABEL in _labels(issue):
            _remove(github, number, module.LABEL)
    ended = before or Incident(number, 0)
    ended = Incident(
        number,
        ended.attempts,
        ended.reason,
        ended.since,
        str((issue or {}).get("state") or "").lower() or None,
        False if issue is not None else None,
    )
    return Change("stop", ended, changed, closed, read(github))
