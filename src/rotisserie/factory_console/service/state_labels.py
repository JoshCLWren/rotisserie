"""What the factory's state labels should say, for the label doctor.

``factory:parked``: the console cannot read the store, so it finds parks the way
``baste turning`` does, by the trusted park comment at the PR's current head
(the sweep writes that comment only after the store event). The hold rule
itself is the factory's (``factory_state_labels.parked_in_effect``). A failed
read returns None, and the doctor leaves the label as it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubRateLimitError,
    integer,
    label_names,
    obj,
    string,
)
from rotisserie.factory_console.service import incident, loops, review

SEARCH_LIMIT = 100


def parked(github: GitHub, *, now: datetime) -> dict[int, bool] | None:
    """Open PR number -> whether ``factory:parked`` belongs on it now."""
    rule = runtime.module("factory_state_labels")
    try:
        pulls = review.open_pulls(github)
        incident_issues = incident.active_issues(github)
        found = loops.parked(
            github, now=now, pulls=pulls, limit=SEARCH_LIMIT, incident_issues=incident_issues
        )
    except GitHubRateLimitError:
        raise
    except Exception:  # noqa: BLE001 - a missing park read must never stop the doctor
        return None
    held = {int(str(row["number"])) for row in found}
    wanted: dict[int, bool] = {}
    for pr in pulls:
        number = integer(pr, "number") or 0
        head = string(obj(pr.get("head")), "sha")
        wanted[number] = rule.parked_in_effect(
            {head} if number in held else set(),
            head,
            label_names(pr),
            incident=loops.is_incident(pr, incident_issues),
        )
    return wanted


def desired(github: GitHub, *, now: datetime) -> Mapping[str, Mapping[str, bool]]:
    """Per target (``pr:N`` / ``issue:N``), each state label the store settles."""
    rule = runtime.module("factory_state_labels")
    found: dict[str, dict[str, bool]] = {}
    for number, wanted in (parked(github, now=now) or {}).items():
        found.setdefault(f"pr:{number}", {})[rule.PARKED] = wanted
    return found
