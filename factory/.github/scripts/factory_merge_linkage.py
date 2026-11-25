"""How a merged PR names the issue it delivers, and whether merging may close it.

Only an explicit closing keyword or the canonical ``factory/<worker>-<issue>-``
branch lets merge automation close an issue. A ``Part of #N.`` line or a
"does not close #N" sentence keeps #N open: the PR delivered a slice and says
so. A title that ends in ``(#N)`` is a mention, not a factory record: no
controller links a PR from its title, so it never closes anything.
"""

from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_eligibility_dir = Path(__file__).resolve().parent
if not (_eligibility_dir / "factory_eligibility.py").is_file():
    _eligibility_dir = Path(__file__).resolve().parents[2] / "scripts"
if str(_eligibility_dir) not in sys.path:
    sys.path.insert(0, str(_eligibility_dir))
eligibility = importlib.import_module("factory_eligibility")

BRANCH_ISSUE_RE = re.compile(r"^factory/[0-9]+-(?P<issue>[0-9]+)-")
TITLE_ISSUE_RE = re.compile(r"\(#(?P<issue>[0-9]+)\)\s*$")

CLOSING_KEYWORD = "closing-keyword"
FACTORY_BRANCH = "factory-branch"
NON_CLOSING = "non-closing"
TITLE_ONLY = "title-only"
NO_LINK = "no-link"


@dataclass(frozen=True)
class Linkage:
    """The issue a merge may close, and the issue the PR names at all."""

    kind: str
    issue: int | None = None
    named: int | None = None

    @property
    def target(self) -> int | None:
        return self.issue if self.issue is not None else self.named

    def reason(self) -> str:
        if self.kind == CLOSING_KEYWORD:
            return f"the PR body says it closes #{self.issue}"
        if self.kind == FACTORY_BRANCH:
            return f"the factory branch names #{self.issue}"
        if self.kind == NON_CLOSING:
            return f"the PR says it is only part of #{self.named}, so #{self.named} stays open"
        if self.kind == TITLE_ONLY:
            return (
                f"only the title names #{self.named}; no factory record links it, "
                f"so #{self.named} stays open"
            )
        return "the PR names no issue"


def linkage(branch: str, body: str | None, title: str = "") -> Linkage:
    """Classify how a merged PR links its issue.

    Args:
        branch: The PR head branch.
        body: The PR body, or None.
        title: The PR title.

    Returns:
        The linkage. ``issue`` is set only when merge automation may close it.
    """
    closing = eligibility.explicit_closing_issue(body)
    if closing is not None:
        return Linkage(CLOSING_KEYWORD, issue=closing, named=closing)
    nonclosing = eligibility.nonclosing_issue_numbers(body)
    match = BRANCH_ISSUE_RE.match(branch or "")
    if match is not None:
        number = int(match.group("issue"))
        if number in nonclosing:
            return Linkage(NON_CLOSING, named=number)
        return Linkage(FACTORY_BRANCH, issue=number, named=number)
    if nonclosing:
        return Linkage(NON_CLOSING, named=min(nonclosing))
    titled = TITLE_ISSUE_RE.search(title or "")
    if titled is not None:
        return Linkage(TITLE_ONLY, named=int(titled.group("issue")))
    return Linkage(NO_LINK)


def explain(pr_number: int, outcome: str, link: Linkage) -> str:
    """One plain line that says what closure decided for a merged PR, and why."""
    target = f" -> issue #{link.target}" if link.target is not None else ""
    return f"Post-merge closure: PR #{pr_number}{target}: {outcome} ({link.reason()})"
