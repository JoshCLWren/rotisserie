"""Factory state shown as GitHub labels: each one mirrors a fact the factory store holds.

The code that changes the state adds or removes its label, and the label doctor
puts it back in line with the store. A state label never starts or stops work:
the store is the authority, and the reusable workflows ignore these label
events (``tests/test_factory_state_label_workflows.py``). The sweep writes them
with the workflow token, so GitHub starts no workflow for them at all.
"""

from __future__ import annotations

import subprocess
from collections.abc import Collection, Iterable
from typing import Any, Protocol

PARKED = "factory:parked"
OVERRIDE = "factory:override"
CLAIMED = "factory:claimed"
BYPASS = frozenset({OVERRIDE, CLAIMED})

# name: (color, description). Later state labels add rows here.
DEFINITIONS: dict[str, tuple[str, str]] = {
    PARKED: ("5319e7", "Loop breaker stopped sending workers; see the Parked comment"),
}
# Every state label name the workflows must ignore, including ones a later
# change starts writing.
IGNORED_BY_WORKFLOWS = (
    PARKED,
    "factory:needs-owner",
    "factory:needs-split",
    "factory:gate-rejected",
    "factory:duplicate",
    "factory:incident",
    "factory:cooling",
)


class GitHub(Protocol):
    repo: str

    def api(self, path: str, *args: str, payload: object | None = None) -> Any: ...


def parked_in_effect(
    parked_heads: Collection[str], head: str, labels: Iterable[str], *, incident: bool
) -> bool:
    """The breaker holds this PR now: parked at its head, no owner bypass, not incident work."""
    return bool(head) and head in parked_heads and not (BYPASS & set(labels)) and not incident


def change(labels: Iterable[str], name: str, wanted: bool) -> str | None:
    """``add``, ``remove`` or None when the label already matches."""
    present = name in set(labels)
    if wanted == present:
        return None
    return "add" if wanted else "remove"


def ensure(github: GitHub, name: str) -> None:
    """Create the label definition when it is missing; an existing one is left alone."""
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
