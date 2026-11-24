"""Why a pull request's checks fail, in one line each.

A review-feedback round cannot pass validation while the PR's own checks are
red. PR #5629 went through seven such rounds while its only failure was a
``cargo fmt --check`` diff. This reads the failing check runs on the PR's
head and pulls the first real error out of each job log.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    integer,
    items,
    obj,
    string,
)

FAILED = ("failure", "timed_out", "startup_failure", "action_required")
FMT_DIFF = re.compile(r"^(?:\S+ )?Diff in (?P<path>\S+?):\d+:", re.M)
COMPILE = re.compile(r"^(?:\S+ )?(error(?:\[E\d+\])?: .+)$", re.M)
RUST_TEST_FAIL = re.compile(r"^(?:\S+ )?test (\S+) \.\.\. FAILED$", re.M)
PYTEST_FAIL = re.compile(r"^(?:\S+ )?FAILED (\S+)", re.M)
ERROR_LINE = re.compile(r"^(?:\S+ )?(?:##\[error\]|::error::)(?P<message>.+)$", re.M)
RUNNER_ROOT = re.compile(r"^.*?/work/[^/]+/[^/]+/")
TSC = re.compile(r"^(?:\S+ )?(?P<text>(?P<path>[\w./-]+\.tsx?)\(\d+,\d+\): error TS\d+: .+)$", re.M)
GENERIC = re.compile(r"^Process completed with exit code \d+\.?$")


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
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log)
    fmt = list(dict.fromkeys(match.group("path") for match in FMT_DIFF.finditer(clean)))
    if fmt:
        # Runner paths are <workspace>/<repo>/<repo>/...; keep the repository-relative part.
        short = [RUNNER_ROOT.sub("", path) for path in fmt]
        names = ", ".join(path.rsplit("/", 1)[-1] for path in short[:3])
        more = f" and {len(short) - 3} more" if len(short) > 3 else ""
        return "formatting", f"cargo fmt --check wants changes in {names}{more}", tuple(short)
    typescript = TSC.search(clean)
    if typescript:
        return "compile", typescript.group("text")[:200], (typescript.group("path"),)
    tests = list(dict.fromkeys(RUST_TEST_FAIL.findall(clean) + PYTEST_FAIL.findall(clean)))
    if tests:
        more = f" and {len(tests) - 2} more" if len(tests) > 2 else ""
        hint = ""
        if any(name.endswith("checked_in_contract_is_current") for name in tests):
            hint = " (the checked-in generated contract is stale; regenerate it)"
        return "tests", "failing tests: " + ", ".join(tests[:2]) + more + hint, ()
    compile_error = next(
        (
            match.group(1).strip()
            for match in COMPILE.finditer(clean)
            if not match.group(1).startswith("error: test failed")
        ),
        None,
    )
    if compile_error:
        return "compile", compile_error[:200], ()
    for match in ERROR_LINE.finditer(clean):
        message = match.group("message").strip()
        if not GENERIC.match(message):
            return "other", message[:200], ()
    return "other", "the check failed; see its log", ()


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
                kind, summary, files = summarize_log(github.text(f"actions/jobs/{job}/logs"))
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
