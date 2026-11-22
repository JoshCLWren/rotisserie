"""Close work that should not be done: a pull request and the issue it delivers.

Used for work the owner cancelled or that ``main`` has moved past. The issue is
closed as "not planned" so the factory's intake does not pick it up again.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, label_names, obj, string


@dataclass(frozen=True)
class Closed:
    pr: int
    issue: int | None  # the linked issue closed with it, if any
    issue_was_open: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def linked_issue(github: GitHub, number: int) -> int | None:
    """The issue a pull request delivers, by the factory's own linking rule."""
    raw = obj(github.repo_api(f"pulls/{number}"))
    row = {
        "number": number,
        "title": string(raw, "title"),
        "body": string(raw, "body"),
        "headRefName": string(obj(raw.get("head")), "ref"),
        "labels": [{"name": name} for name in label_names(raw)],
        "state": string(raw, "state").upper(),
    }
    return runtime.linked_issue(row)


def close_pr(github: GitHub, number: int, reason: str, *, with_issue: bool = True) -> Closed:
    """Close the PR with ``reason`` as its comment, then its linked issue as not planned."""
    if not reason.strip():
        raise ValueError("Say why it is being closed (--reason); the comment is the record.")
    issue = linked_issue(github, number) if with_issue else None
    github.gh("pr", "close", str(number), "--repo", github.repo, "--comment", reason)
    was_open = False
    if issue is not None:
        state = string(obj(github.repo_api(f"issues/{issue}")), "state")
        was_open = state == "open"
        if was_open:
            github.gh(
                "issue", "close", str(issue), "--repo", github.repo,
                "--reason", "not planned",
                "--comment", f"{reason}\n\nClosed together with pull request #{number}.",
            )  # fmt: skip
    return Closed(number, issue, was_open, reason)
