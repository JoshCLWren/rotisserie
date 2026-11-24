"""Locate factory workflows: reusable logic in Rotisserie, thin callers for adopters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

REUSABLE = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CALLERS = Path(__file__).resolve().parents[1] / "install" / "callers"
RUNTIME_STEPS = frozenset(
    {
        "Set up factory Python",
        "Check out Rotisserie factory runtime",
        "Overlay Rotisserie factory runtime",
    }
)


def own_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """The job's steps without the Python setup and runtime overlay every job shares."""
    return [step for step in job.get("steps") or [] if step.get("name") not in RUNTIME_STEPS]


def _sections(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in text.splitlines(keepends=True):
        if line[:1].isalpha() and ":" in line:
            found.append((line.split(":", 1)[0], line))
        elif found:
            found[-1] = (found[-1][0], found[-1][1] + line)
        else:
            found.append(("", line))
    return found


def adopter_view(name: str) -> str:
    """The workflow as it runs in an adopter: caller triggers around the reusable jobs."""
    reusable = (REUSABLE / name).read_text(encoding="utf-8")
    caller = CALLERS / name
    if not caller.exists():
        return reusable
    triggers = "".join(
        body
        for key, body in _sections(caller.read_text(encoding="utf-8"))
        if key in ("run-name", "on", "concurrency")
    )
    return "".join(
        triggers if key == "on" else body
        for key, body in _sections(reusable)
        if key not in ("run-name", "concurrency")
    )
