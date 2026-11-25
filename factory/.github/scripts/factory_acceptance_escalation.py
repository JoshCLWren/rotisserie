"""Escalate a PR whose approvals the acceptance gate keeps rejecting at one head.

Each gate rejection of an approve verdict carries a head-stamped marker. After
``LIMIT`` rejections at the same head, another reviewer will not help: the PR
goes to repair, and the gate's reasons become findings for the implementer.
One adopter PR got 20 rejections at one head and stayed in review.
Pure: callers bring the trusted comment bodies.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_acceptance_criteria import criteria  # noqa: E402
from factory_adopter import MARKER_PREFIX  # noqa: E402

LIMIT = 3
GATE_REJECTED = "factory:gate-rejected"
GATE_REJECTED_DEFINITION = ("b60205", "Acceptance gate rejected an approval at this head")
KIND = f"{MARKER_PREFIX}-factory-acceptance-rejected-v1"
MARKER_RE = re.compile(rf"^<!-- {re.escape(KIND)}:pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}) -->")
CRITERION_RE = re.compile(r"^(?P<id>A\d+): (?P<problem>.+)$")


def marker(*, pr: int, head: str) -> str:
    return f"<!-- {KIND}:pr-{pr}:head-{head} -->"


def prior(bodies: Iterable[str], *, pr: int, head: str) -> int:
    """Gate rejections already recorded for this PR at this head."""
    count = 0
    for body in bodies:
        found = MARKER_RE.match(body)
        if found and int(found["pr"]) == pr and found["head"] == head:
            count += 1
    return count


def escalate(bodies: Iterable[str], *, pr: int, head: str) -> bool:
    """True when this rejection is the ``LIMIT``-th at the head."""
    return prior(bodies, pr=pr, head=head) + 1 >= LIMIT


def flag(gh: Callable[..., Any], repo: str, pr: int, labels: Iterable[str], wanted: bool) -> bool:
    """Add or remove ``factory:gate-rejected`` when it differs; a failed write is ignored.

    Returns ``wanted`` so a caller can write ``escalate = flag(...) and escalate(...)``.
    """
    if (GATE_REJECTED in set(labels)) == wanted:
        return wanted
    color, description = GATE_REJECTED_DEFINITION
    try:
        if wanted:
            gh(["api", f"repos/{repo}/labels", "--method", "POST", "-f", f"name={GATE_REJECTED}",
                "-f", f"color={color}", "-f", f"description={description}"], check=False)
            gh(["api", f"repos/{repo}/issues/{pr}/labels", "--method", "POST",
                "-f", f"labels[]={GATE_REJECTED}"])
        else:
            gh(["api", f"repos/{repo}/issues/{pr}/labels/{GATE_REJECTED.replace(':', '%3A')}",
                "--method", "DELETE"], check=False)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        pass
    return wanted


def findings(failures: Iterable[str], issue_body: str) -> str:
    """The gate's reasons, rewritten as work for the implementer."""
    try:
        texts = {f"A{i}": item.text for i, item in enumerate(criteria(issue_body), 1)}
    except ValueError:
        texts = {}
    lines = [
        f"Repair findings: {LIMIT} reviewers approved this head, and each time the acceptance"
        " gate could not tie these issue criteria to the PR's changes.",
    ]
    for failure in failures:
        found = CRITERION_RE.match(failure)
        if found is None:
            lines.append(f"- Acceptance gate: {failure}.")
            continue
        criterion = texts.get(found["id"], "")
        about = f" ({criterion[:300]})" if criterion else ""
        lines.append(
            f"- {found['id']}{about}: the gate found {found['problem']}. Implement this"
            " criterion in a file this PR changes and add a test that proves it, or state"
            " in the PR body why the existing change already meets it."
        )
    return "\n".join(lines)
