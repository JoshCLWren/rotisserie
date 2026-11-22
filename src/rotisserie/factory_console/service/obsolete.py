"""Is a pull request still worth working on, or has ``main`` moved past it?

A merge-conflict worker cannot resolve a pull request whose files ``main``
deleted (a retired program, a removed app, code moved to another repository).
Such work needs a person to close it, not another model. This checks, from
GitHub alone, how many of the files a pull request changes still exist on the
default branch, and which commit removed the first one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    items,
    obj,
    string,
)

# Share of a PR's pre-existing files that must be gone from main to call it obsolete.
OBSOLETE_SHARE = 0.5
MAX_FILES = 30


@dataclass(frozen=True)
class Staleness:
    """How much of a pull request's change still has somewhere to land."""

    pr: int
    checked: int  # files the PR changes that existed before it (added files are skipped)
    deleted: tuple[str, ...]  # of those, the ones main no longer has
    removed_by: str  # "<sha> <subject>" of the commit that removed the first deleted file
    obsolete: bool

    @property
    def text(self) -> str:
        if not self.obsolete:
            return f"PR #{self.pr}: its files still exist on main"
        sample = ", ".join(self.deleted[:2]) + ("…" if len(self.deleted) > 2 else "")
        why = f" (removed by {self.removed_by})" if self.removed_by else ""
        return (
            f"PR #{self.pr}: main deleted {len(self.deleted)} of the {self.checked} files it "
            f"changes, e.g. {sample}{why}. Probably obsolete: a person should close it."
        )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["text"] = self.text
        return data


def _exists_on_main(github: GitHub, path: str) -> bool:
    try:
        github.repo_api(f"contents/{path}?ref=HEAD")
    except GitHubNotFoundError:
        return False
    return True


def pr_staleness(github: GitHub, number: int) -> Staleness:
    """Check a pull request's pre-existing files against the default branch."""
    rows = items(github.repo_api(f"pulls/{number}/files?per_page=100"))
    existing = [
        string(row, "previous_filename") or string(row, "filename")
        for row in rows
        if string(row, "status") != "added"
    ][:MAX_FILES]
    deleted = tuple(path for path in existing if not _exists_on_main(github, path))
    removed_by = ""
    if deleted:
        try:
            commits = items(github.repo_api(f"commits?path={deleted[0]}&per_page=1"))
        except GitHubError:
            commits = []
        if commits:
            head = commits[0]
            subject = string(obj(head.get("commit")), "message").split("\n", 1)[0]
            removed_by = f"{string(head, 'sha')[:8]} {subject}".strip()
    share = len(deleted) / len(existing) if existing else 0.0
    return Staleness(number, len(existing), deleted, removed_by, share >= OBSOLETE_SHARE)
