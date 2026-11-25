"""What the factory's state labels should say, for the label doctor.

``factory:parked``: the console cannot read the store, so it finds parks the way
``baste turning`` does, by the trusted park comment at the PR's current head
(the sweep writes that comment only after the store event). The hold rule
itself is the factory's (``factory_state_labels.parked_in_effect``). A failed
read returns None, and the doctor leaves the label as it is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubRateLimitError,
    Obj,
    integer,
    label_names,
    obj,
    string,
)
from rotisserie.factory_console.service import incident, loops, review

SEARCH_LIMIT = 100


def parked(github: GitHub, *, now: datetime, pulls: Sequence[Obj]) -> dict[int, bool] | None:
    """Open PR number -> whether ``factory:parked`` belongs on it now."""
    rule = runtime.module("factory_state_labels")
    try:
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


def gate_rejected(github: GitHub, pr: Obj) -> bool | None:
    """Whether the newest gate record at the PR's head is a rejection; None when unread."""
    rule = runtime.module("factory_state_labels")
    policy = runtime.module("factory_review_policy")
    number = integer(pr, "number") or 0
    try:
        bodies = policy.controller_comment_bodies(review.comment_records(github, number))
    except GitHubRateLimitError:
        raise
    except Exception:  # noqa: BLE001 - an unread PR keeps its label
        return None
    return bool(rule.gate_rejected_at(bodies, number, string(obj(pr.get("head")), "sha")))


def desired(
    github: GitHub, *, now: datetime, items: Sequence[Obj] = ()
) -> Mapping[str, Mapping[str, bool]]:
    """Per target (``pr:N`` / ``issue:N``), each state label the doctor can settle.

    ``factory:cooling`` is left out: only the factory store knows it.
    """
    rule = runtime.module("factory_state_labels")
    found: dict[str, dict[str, bool]] = {}
    for item in items:
        if "pull_request" not in item:
            found[f"issue:{integer(item, 'number') or 0}"] = {
                rule.PARKED: False,
                rule.NEEDS_SPLIT: bool(rule.needs_split(item)),
            }
    try:
        pulls = review.open_pulls(github)
    except GitHubRateLimitError:
        raise
    except Exception:  # noqa: BLE001 - no PR list settles no PR label
        return found
    for number, wanted in (parked(github, now=now, pulls=pulls) or {}).items():
        found.setdefault(f"pr:{number}", {})[rule.PARKED] = wanted
    for number, newer in rule.duplicates(pulls).items():
        found.setdefault(f"pr:{number}", {})[rule.DUPLICATE] = bool(newer)
    for pr in pulls:
        if rule.GATE_REJECTED in label_names(pr):
            rejected = gate_rejected(github, pr)
            if rejected is not None:
                found.setdefault(f"pr:{integer(pr, 'number') or 0}", {})[rule.GATE_REJECTED] = (
                    rejected
                )
    return found
