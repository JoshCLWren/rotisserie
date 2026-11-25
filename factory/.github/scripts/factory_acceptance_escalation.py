"""Escalate a PR whose approvals the acceptance gate keeps rejecting at one head.

Each gate rejection of an approve verdict carries a head-stamped marker. After
``LIMIT`` rejections at the same head, another reviewer will not help: the PR
goes to repair, and the gate's reasons become findings for the implementer.
ComicPile PR 5737 got 20 rejections at head f5b7366 and stayed in review.
Pure: callers bring the trusted comment bodies.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from factory_acceptance_criteria import criteria  # noqa: E402
from factory_adopter import MARKER_PREFIX  # noqa: E402

LIMIT = 3
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
