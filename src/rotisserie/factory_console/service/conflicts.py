"""Which of a pull request's files ``main`` also changed, and in which change.

A merge-conflict worker that keeps failing usually faces a real overlap: PR
#5660 and the already-merged #5629 both edited the same three ComicVine Rust
files. GitHub's compare file list stops at 300 files and commit ``since``
filters go by commit date (merged commits keep older dates), so neither answers
this reliably. Comparing each PR file's blob at the merge base with its blob on
the default branch does, for any branch age.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    items,
    obj,
    string,
)

MAX_FILES = 60
PR_NUMBER = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")


@dataclass(frozen=True)
class Overlap:
    path: str
    change: str  # "<sha> <subject>" of main's latest commit touching it
    pr: int | None  # the merged PR that change came from, when it names one
    deleted: bool  # main no longer has the file


@dataclass(frozen=True)
class ConflictReport:
    pr: int
    merge_base: str
    behind: int
    checked: int
    overlaps: tuple[Overlap, ...]

    @property
    def prs(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(item.pr for item in self.overlaps if item.pr))

    @property
    def text(self) -> str:
        if not self.overlaps:
            return f"PR #{self.pr}: main changed none of its {self.checked} files since it branched"
        names = ", ".join(item.path.rsplit("/", 1)[-1] for item in self.overlaps[:3])
        more = f" and {len(self.overlaps) - 3} more" if len(self.overlaps) > 3 else ""
        by = f" (in {', '.join(f'#{n}' for n in self.prs)})" if self.prs else ""
        return f"PR #{self.pr}: main also changed {names}{more}{by}"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["prs"] = list(self.prs)
        data["text"] = self.text
        return data


def _blob(github: GitHub, path: str, ref: str) -> str | None:
    try:
        return string(obj(github.repo_api(f"contents/{path}?ref={ref}")), "sha") or None
    except GitHubNotFoundError:
        return None


def _latest_change(github: GitHub, path: str) -> tuple[str, int | None]:
    try:
        commits = items(github.repo_api(f"commits?path={path}&per_page=1"))
    except GitHubError:
        return "", None
    if not commits:
        return "", None
    head = commits[0]
    sha = string(head, "sha")
    subject = string(obj(head.get("commit")), "message").split("\n", 1)[0]
    number = _merged_pr(github, sha)
    if number is None:
        match = PR_NUMBER.search(subject)
        number = int(match.group(1) or match.group(2)) if match else None
    return f"{sha[:8]} {subject}".strip(), number


def _merged_pr(github: GitHub, sha: str) -> int | None:
    """The merged PR that brought a commit to main (factory checkpoint commits name none)."""
    try:
        pulls = items(github.repo_api(f"commits/{sha}/pulls"))
    except GitHubError:
        return None
    merged = [pull for pull in pulls if pull.get("merged_at")]
    found = (merged or pulls or [{}])[0].get("number")
    return found if isinstance(found, int) and not isinstance(found, bool) else None


def pr_conflicts(github: GitHub, number: int) -> ConflictReport:
    """Files the PR changes that main also changed since the PR's merge base."""
    pr = obj(github.repo_api(f"pulls/{number}"))
    head = string(obj(pr.get("head")), "ref")
    base = string(obj(pr.get("base")), "ref") or "HEAD"
    compare = obj(github.repo_api(f"compare/{base}...{head}"))
    merge_base = string(obj(compare.get("merge_base_commit")), "sha")
    behind = compare.get("behind_by")
    rows = items(github.repo_api(f"pulls/{number}/files?per_page=100"))
    paths = [
        string(row, "previous_filename") or string(row, "filename")
        for row in rows
        if string(row, "status") != "added"
    ][:MAX_FILES]

    def check(path: str) -> Overlap | None:
        before = _blob(github, path, merge_base)
        now = _blob(github, path, base)
        if before == now:
            return None
        change, by = _latest_change(github, path)
        return Overlap(path, change, by, now is None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [item for item in pool.map(check, paths) if item is not None]
    return ConflictReport(
        number,
        merge_base,
        behind if isinstance(behind, int) else 0,
        len(paths),
        tuple(found),
    )
