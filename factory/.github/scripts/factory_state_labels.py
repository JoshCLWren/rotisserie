"""Factory state shown as GitHub labels: each one mirrors a fact the factory store holds.

The code that changes the state adds or removes its label, and the label doctor
puts it back in line with the store. A state label never starts or stops work:
the store is the authority, and the reusable workflows ignore these label
events (``tests/test_factory_state_label_workflows.py``). The sweep writes them
with the workflow token, so GitHub starts no workflow for them at all.
"""

from __future__ import annotations

import subprocess
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Protocol

from factory_acceptance_escalation import GATE_REJECTED, GATE_REJECTED_DEFINITION, MARKER_RE
from factory_incident import linked_issue
from factory_issue_intake_guard import NEEDS_SPLIT, NEEDS_SPLIT_DEFINITION, sliced_issue, split_reason
from factory_work_policy_candidates import issue_is_static_candidate

PARKED = "factory:parked"
DUPLICATE = "factory:duplicate"
COOLING = "factory:cooling"
OVERRIDE = "factory:override"
CLAIMED = "factory:claimed"
BYPASS = frozenset({OVERRIDE, CLAIMED})

DEFINITIONS: dict[str, tuple[str, str]] = {
    PARKED: ("5319e7", "Loop breaker stopped sending workers; see the Parked comment"),
    NEEDS_SPLIT: NEEDS_SPLIT_DEFINITION,
    GATE_REJECTED: GATE_REJECTED_DEFINITION,
    DUPLICATE: ("cfd3d7", "Another open PR delivers the same issue; this one is newer"),
    COOLING: ("c5def5", "Dispatch backoff: no worker until the retry cooling ends"),
}
# Every state label name the workflows must ignore, including ones a later
# change starts writing.
IGNORED_BY_WORKFLOWS = (
    PARKED,
    "factory:needs-owner",
    NEEDS_SPLIT,
    GATE_REJECTED,
    DUPLICATE,
    COOLING,
)
# The owner's incident label (factory_incident.LABEL) starts work; it is an input, not a mirror.
OWNER_INPUTS = ("factory:incident",)


_DEFINED: set[tuple[str, str]] = set()


class GitHub(Protocol):
    repo: str

    def api(self, path: str, *args: str, payload: object | None = None) -> Any: ...


def parked_in_effect(
    parked_heads: Collection[str], head: str, labels: Iterable[str], *, incident: bool
) -> bool:
    """The breaker holds this PR now: parked at its head, no owner bypass, not incident work."""
    return bool(head) and head in parked_heads and not (BYPASS & set(labels)) and not incident


def duplicates(pulls: Iterable[Mapping[str, Any]]) -> dict[int, bool]:
    """Open PR number -> whether an older open PR already delivers the same issue."""
    by_issue: dict[int, list[int]] = {}
    numbers: list[int] = []
    for pr in pulls:
        number = int(pr.get("number") or 0)
        if not number:
            continue
        numbers.append(number)
        issue = linked_issue(pr)
        if issue is None:
            head = pr.get("head")
            branch = head.get("ref") if isinstance(head, Mapping) else pr.get("headRefName")
            issue = sliced_issue({**pr, "headRefName": branch or ""})
        if issue is not None:
            by_issue.setdefault(issue, []).append(number)
    newer = {n for group in by_issue.values() for n in sorted(group)[1:]}
    return {number: number in newer for number in numbers}


def needs_split(issue: Mapping[str, Any]) -> bool:
    """The pool would dispatch this issue but for its size or an epic-type label."""
    labels = {
        str(label.get("name") if isinstance(label, Mapping) else label)
        for label in issue.get("labels") or []
    }
    if BYPASS & labels or split_reason(issue) is None:
        return False
    return issue_is_static_candidate(dict(issue), set())


def gate_rejected_at(bodies: Iterable[str], pr: int, head: str) -> bool:
    """The newest acceptance-gate record at this head is a rejection, not a pass.

    ``bodies`` are trusted controller comments, oldest first.
    """
    rejected = False
    for body in bodies:
        found = MARKER_RE.match(body)
        if found and int(found["pr"]) == pr and found["head"] == head:
            rejected = True
        elif "-factory-acceptance-v1:" in body and f":head-{head}:" in body:
            rejected = False
    return rejected


def change(labels: Iterable[str], name: str, wanted: bool) -> str | None:
    """``add``, ``remove`` or None when the label already matches."""
    present = name in set(labels)
    if wanted == present:
        return None
    return "add" if wanted else "remove"


def ensure(github: GitHub, name: str) -> None:
    """Create the label definition when it is missing, once per process."""
    if (github.repo, name) in _DEFINED:
        return
    _DEFINED.add((github.repo, name))
    color, description = DEFINITIONS[name]
    try:
        github.api(
            "labels",
            "--method",
            "POST",
            payload={"name": name, "color": color, "description": description},
        )
    except RuntimeError, ValueError, OSError, subprocess.SubprocessError:
        pass


def write(github: GitHub, number: int, name: str, action: str) -> None:
    if action == "add":
        ensure(github, name)
        github.api(f"issues/{number}/labels", "--method", "POST", payload={"labels": [name]})
    else:
        try:
            github.api(f"issues/{number}/labels/{name.replace(':', '%3A')}", "--method", "DELETE")
        except RuntimeError, ValueError, OSError, subprocess.SubprocessError:
            pass
