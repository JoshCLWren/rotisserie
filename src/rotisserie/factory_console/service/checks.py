"""Why a pull request's checks fail, in one line each.

A review-feedback round cannot pass validation while the PR's own checks are
red. PR #5629 went through seven such rounds while its only failure was a
``cargo fmt --check`` diff. This reads the failing check runs on the PR's
head and pulls the first real error out of each job log.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass

import _rotisserie_native
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    integer,
    items,
    obj,
    string,
)

FAILED = ("failure", "timed_out", "startup_failure", "action_required")


@dataclass(frozen=True)
class FailingCheck:
    name: str
    url: str
    kind: str  # formatting | compile | tests | other
    summary: str
    files: tuple[str, ...] = ()
    completed_at: str = ""  # ISO time the check run finished

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def summarize_log(log: str) -> tuple[str, str, tuple[str, ...]]:
    """(kind, one-line summary, files) for the first real failure in a job log."""
    return _rotisserie_native.summarize_log(log)


def failing_checks(github: GitHub, number: int, *, read_logs: bool = True) -> list[FailingCheck]:
    """Failing check runs on the PR's current head, each with its first real error."""
    head = string(obj(obj(github.repo_api(f"pulls/{number}")).get("head")), "sha")
    return failing_checks_at(github, head, read_logs=read_logs)


def branch_head(github: GitHub, branch: str) -> str:
    return string(obj(obj(github.repo_api(f"branches/{branch}")).get("commit")), "sha")


def failing_checks_at(
    github: GitHub, head: str, *, read_logs: bool = True, run_ids: Sequence[int] = ()
) -> list[FailingCheck]:
    """Failing checks on one commit, each with its first real error.

    With ``run_ids``, only those workflow runs' jobs count: a branch's product
    CI, not factory workers that happen to run on the same commit.
    """
    if not head:
        return []
    if run_ids:
        runs = [
            job
            for run_id in run_ids
            for job in items(github.repo_api(f"actions/runs/{run_id}/jobs?per_page=100"), "jobs")
        ]
    else:
        runs = items(github.repo_api(f"commits/{head}/check-runs?per_page=100"), "check_runs")
    found = []
    seen: set[str] = set()
    for run in runs:
        if string(run, "conclusion") not in FAILED:
            continue
        name = string(run, "name")
        if name in seen:
            continue
        seen.add(name)
        kind, summary = "other", "the check failed; see its log"
        files: tuple[str, ...] = ()
        job = integer(run, "id")
        if read_logs and job is not None:
            try:
                kind, summary, files = summarize_log(
                    github.text(f"actions/jobs/{job}/logs", final="completed")
                )
            except GitHubError:
                pass
        found.append(
            FailingCheck(
                name,
                string(run, "html_url"),
                kind,
                summary,
                files,
                string(run, "completed_at"),
            )
        )
    return found
