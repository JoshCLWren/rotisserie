"""Is a pull request's check failing because of the PR, or because of main?

A PR is tested as if merged into the default branch, so a breakage that lands
on main shows up on every open PR that runs the same check (for example, a
merge that leaves files failing the formatter turns every PR running that
formatter red in files none of them changed). Repair workers on those PRs
either fix main's files (and are rejected as out of scope) or leave them (and
stay red), round after round.

This tells the two apart from GitHub alone, with no repository-specific names:

* **File evidence**: the failure names files (a formatter diff, for example)
  that the PR does not change. Those files are main's as of the run.
* **Main evidence**: the same check's latest run on the default branch, found
  through the failing run's own workflow, and whether it fails the same way.
* **Spread**: how many PRs fail the same check with the same signature.

An inherited failure is not the PR's to repair. When main changed the failing
files (or the check passed on main) after the PR's run, the failure is
probably already fixed on main, and syncing the branch is the repair.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import cast

from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    integer,
    items,
    obj,
    string,
)
from rotisserie.factory_console.service import checks

RUN_JOB = re.compile(r"/actions/runs/(?P<run>\d+)/job/(?P<job>\d+)")
MAX_PR_FILE_PAGES = 3
PR_EVENTS = ("pull_request", "pull_request_target")

# Verdict origins.
OWN = "own"  # the PR's own change fails the check
INHERITED = "inherited"  # main fails it; nothing the PR should repair
FIXED_ON_MAIN = "fixed-on-main"  # inherited, but main changed since: sync the branch
MIXED = "mixed"  # fails in the PR's own files and in main's


@dataclass(frozen=True)
class MainCheck:
    """The latest default-branch run of a check, through the same workflow."""

    name: str
    conclusion: str  # success | failure | … | "" when it never ran on main
    sha: str
    url: str
    ran_at: str
    summary: str = ""
    files: tuple[str, ...] = ()

    @property
    def failing(self) -> bool:
        return self.conclusion in checks.FAILED


@dataclass(frozen=True)
class Change:
    """The default-branch commit that last changed a file."""

    path: str
    sha: str
    when: str
    subject: str
    pr: int | None


@dataclass(frozen=True)
class Verdict:
    """Where one failing check on one PR comes from."""

    pr: int
    check: str
    origin: str  # own | inherited | fixed-on-main | mixed
    reason: str
    summary: str
    inherited_files: tuple[str, ...] = ()
    own_files: tuple[str, ...] = ()
    main_fixed: bool = False  # main moved past its part of the failure after the run

    @property
    def not_the_prs(self) -> bool:
        return self.origin in (INHERITED, FIXED_ON_MAIN)


@dataclass(frozen=True)
class Incident:
    """One failure shared through main: the checks it breaks, who it hits, and why."""

    checks: tuple[str, ...]
    summary: str
    files: tuple[str, ...]
    prs: tuple[int, ...]
    state: str  # broken (main still fails) | fixed (main changed since the PRs ran)
    confirmed: bool  # main itself was seen failing the same way
    cause: str
    evidence: tuple[str, ...]

    @property
    def check(self) -> str:
        return " and ".join(self.checks)

    @property
    def text(self) -> str:
        who = ", ".join(f"#{pr}" for pr in self.prs)
        if self.state == "fixed":
            return (
                f"{self.check} failed on {who} because of main, and main has changed since. "
                "Sync each branch with main instead of repairing it."
            )
        verb = "fail" if len(self.checks) > 1 else "fails"
        sure = f"{verb} on main" if self.confirmed else f"probably {verb} on main"
        return (
            f"{self.check} {sure}, so PR {who} {'is' if len(self.prs) == 1 else 'are'} red "
            f"too ({self.summary}). "
            "Fix it once on main; no worker should repair it on each PR."
        )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["check"] = self.check
        data["text"] = self.text
        return data


@dataclass(frozen=True)
class Report:
    incidents: tuple[Incident, ...] = ()
    verdicts: tuple[Verdict, ...] = ()
    default_branch: str = ""
    problems: tuple[str, ...] = field(default=())

    def for_pr(self, pr: int) -> list[Verdict]:
        return [item for item in self.verdicts if item.pr == pr]

    def own_failures(
        self, pr: int, failing: list[checks.FailingCheck]
    ) -> list[checks.FailingCheck]:
        """The failing checks that are the PR's own to repair."""
        theirs = {item.check for item in self.for_pr(pr) if item.not_the_prs}
        return [item for item in failing if item.name not in theirs]

    def needs_sync(self, pr: int) -> bool:
        """Main has moved past at least one of the PR's failures since its checks ran.

        Merging main into the branch comes first: until then even the PR's own
        repair fails validation on main's old files. Its own failures are
        repaired once the checks run again on the synced head.
        """
        return any(item.main_fixed for item in self.for_pr(pr))

    def waits_on_main(self, pr: int) -> bool:
        """Main still breaks every failing check, so no PR-side repair can validate.

        A mixed failure counts: the PR's own part may be fixable, but the fast
        checks stay red on main's part, and fixing main's files from the PR is
        out of its scope.
        """
        found = self.for_pr(pr)
        return (
            bool(found)
            and all(item.origin in (INHERITED, MIXED) for item in found)
            and not any(item.main_fixed for item in found)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "default_branch": self.default_branch,
            "incidents": [item.to_dict() for item in self.incidents],
            "verdicts": [asdict(item) for item in self.verdicts],
            "problems": list(self.problems),
        }


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _after(later: str, earlier: str) -> bool:
    a, b = _parse(later), _parse(earlier)
    return a is not None and b is not None and a > b


class Reader:
    """GitHub reads for the analysis, each cached for one report."""

    def __init__(self, github: GitHub) -> None:
        self.github = github
        self._branch = ""
        self._main: dict[str, MainCheck | None] = {}
        self._changes: dict[str, Change | None] = {}
        self._files: dict[int, frozenset[str]] = {}

    def default_branch(self) -> str:
        if not self._branch:
            self._branch = (
                string(obj(self.github.api(f"repos/{self.github.repo}")), "default_branch")
                or "main"
            )
        return self._branch

    def pr_files(self, pr: int) -> frozenset[str]:
        if pr not in self._files:
            found: set[str] = set()
            for page in range(1, MAX_PR_FILE_PAGES + 1):
                rows = items(self.github.repo_api(f"pulls/{pr}/files?per_page=100&page={page}"))
                for row in rows:
                    found.add(string(row, "filename"))
                    if string(row, "previous_filename"):
                        found.add(string(row, "previous_filename"))
                if len(rows) < 100:
                    break
            self._files[pr] = frozenset(found)
        return self._files[pr]

    def main_check(self, failing: checks.FailingCheck) -> MainCheck | None:
        """The same check's latest completed default-branch run, via the failing run's workflow."""
        match = RUN_JOB.search(failing.url)
        if not match:
            return None
        key = f"{match.group('run')}:{failing.name}"
        if key in self._main:
            return self._main[key]
        self._main[key] = None
        run = obj(self.github.repo_api(f"actions/runs/{match.group('run')}"))
        workflow = integer(run, "workflow_id")
        if workflow is None:
            return None
        runs = items(
            self.github.repo_api(
                f"actions/workflows/{workflow}/runs?branch={self.default_branch()}"
                "&status=completed&per_page=10"
            ),
            "workflow_runs",
        )
        for row in runs:
            if string(row, "event") in PR_EVENTS:
                continue
            run_id = integer(row, "id")
            jobs = items(self.github.repo_api(f"actions/runs/{run_id}/jobs?per_page=100"), "jobs")
            job = next((item for item in jobs if string(item, "name") == failing.name), None)
            if job is None:
                continue  # the job was skipped by a path or event filter on this run
            conclusion = string(job, "conclusion")
            summary, files = "", cast(tuple[str, ...], ())
            if conclusion in checks.FAILED and integer(job, "id") is not None:
                try:
                    log = self.github.text(f"actions/jobs/{integer(job, 'id')}/logs")
                    _, summary, files = checks.summarize_log(log)
                except GitHubError:
                    pass
            found = MainCheck(
                failing.name,
                conclusion,
                string(row, "head_sha"),
                string(job, "html_url") or string(row, "html_url"),
                string(job, "completed_at") or string(row, "updated_at"),
                summary,
                files,
            )
            self._main[key] = found
            return found
        return None

    def last_change(self, path: str) -> Change | None:
        if path in self._changes:
            return self._changes[path]
        self._changes[path] = None
        rows = items(
            self.github.repo_api(f"commits?sha={self.default_branch()}&path={path}&per_page=1")
        )
        if not rows:
            return None
        head = rows[0]
        sha = string(head, "sha")
        commit = obj(head.get("commit"))
        pr = None
        # A merged PR's commits keep their own dates; the change reached main at the merge.
        when = string(obj(commit.get("committer")), "date")
        try:
            pulls = items(self.github.repo_api(f"commits/{sha}/pulls"))
            merged = [row for row in pulls if string(row, "merged_at")]
            if merged:
                pr = integer(merged[0], "number")
                when = string(merged[0], "merged_at") or when
        except GitHubError:
            pr = None
        found = Change(
            path,
            sha,
            when,
            string(commit, "message").split("\n", 1)[0],
            pr,
        )
        self._changes[path] = found
        return found


def judge(
    pr: int,
    failing: checks.FailingCheck,
    pr_files: frozenset[str],
    main: MainCheck | None,
    changed: Callable[[str], Change | None],
) -> Verdict:
    """Where one failing check comes from. Pure given its inputs."""
    files = tuple(failing.files)
    theirs = tuple(path for path in files if path not in pr_files)
    mine = tuple(path for path in files if path in pr_files)
    same_on_main = (
        main is not None
        and main.failing
        and (
            (files and bool(set(main.files) & set(files)))
            or (not files and main.summary == failing.summary)
        )
    )
    outside = bool(files) and not mine
    if not (theirs or same_on_main):
        return Verdict(pr, failing.name, OWN, "the PR's own change", failing.summary, (), mine)
    fixed = _fixed_since(failing, theirs, main, changed)
    if mine and theirs:
        why = f"fails in {len(mine)} file(s) the PR changes and {len(theirs)} it does not"
        if fixed:
            why += f"; main's part is already fixed ({fixed})"
        return Verdict(
            pr, failing.name, MIXED, why, failing.summary, theirs, mine, main_fixed=bool(fixed)
        )
    if fixed:
        return Verdict(
            pr, failing.name, FIXED_ON_MAIN, f"inherited from main, which {fixed}",
            failing.summary, theirs, main_fixed=True,
        )  # fmt: skip
    why = (
        f"fails in {', '.join(_short(theirs))}, which the PR does not change"
        if outside
        else "main fails the same way"
    )
    return Verdict(pr, failing.name, INHERITED, why, failing.summary, theirs)


def _fixed_since(
    failing: checks.FailingCheck,
    theirs: tuple[str, ...],
    main: MainCheck | None,
    changed: Callable[[str], Change | None],
) -> str:
    """How main moved past an inherited failure after the PR's run; empty if it has not."""
    if (
        main is not None
        and main.conclusion == "success"
        and _after(main.ran_at, failing.completed_at)
    ):
        return f"passed on main since ({main.sha[:8]})"
    # A main run newer than the PR's that still fails outweighs a newer file change.
    if main is not None and main.failing and _after(main.ran_at, failing.completed_at):
        return ""
    touched = [
        path
        for path in theirs
        if (change := changed(path)) is not None and _after(change.when, failing.completed_at)
    ]
    return f"main changed {', '.join(_short(touched))} after this run" if touched else ""


def _short(paths: Iterable[str]) -> list[str]:
    return [path.rsplit("/", 1)[-1] for path in paths]


def group(
    verdicts: list[Verdict],
    mains: dict[str, MainCheck | None],
    changed: Callable[[str], Change | None],
) -> list[Incident]:
    """Inherited failures, one incident per root cause.

    Failures naming the same main files are one cause even across checks (two
    workflows running the same formatter); failures without files group by
    check and error.
    """
    buckets: dict[tuple[tuple[str, ...], str], list[Verdict]] = {}
    for item in verdicts:
        if item.origin == OWN:
            continue
        files = tuple(sorted(item.inherited_files))
        key = (files, "") if files else ((), f"{item.check}\n{item.summary}")
        buckets.setdefault(key, []).append(item)
    incidents = []
    for (files, _), found in sorted(buckets.items()):
        names = tuple(dict.fromkeys(item.check for item in found))
        label = " and ".join(names)
        seen = [main for name in names if (main := mains.get(name)) is not None]
        failing_mains = [main for main in seen if main.failing]
        confirmed = bool(failing_mains)
        prs = tuple(sorted({item.pr for item in found}))
        state = "fixed" if all(item.main_fixed for item in found) else "broken"
        evidence = [
            f"{label} fail{'s' if len(names) == 1 else ''} on {len(prs)} PR(s) "
            f"({', '.join(f'#{pr}' for pr in prs)}) with the same error: {found[0].summary}"
        ]
        if files:
            evidence.append(
                f"none of them change {', '.join(_short(files[:4]))}"
                f"{' and more' if len(files) > 4 else ''}"
            )
        if not seen:
            evidence.append(f"no completed {label} run on the default branch was found")
        for main in seen:
            if main.failing:
                evidence.append(
                    f"main's latest {main.name} run ({main.sha[:8]}) fails: {main.summary}"
                )
            else:
                evidence.append(
                    f"main's latest {main.name} run ({main.sha[:8]}, {main.ran_at}) "
                    f"{main.conclusion}"
                )
        cause = ""
        changes = [change for path in files if (change := changed(path)) is not None]
        if changes:
            latest = max(changes, key=lambda change: change.when)
            via = f" (PR #{latest.pr})" if latest.pr else ""
            what = f"{latest.sha[:8]} {latest.subject}{via}"
            where = f"{latest.path.rsplit('/', 1)[-1]} on main ({latest.when})"
            cause = (
                f"fixed on main by {what}, which changed {where}"
                if state == "fixed"
                else f"{what} last changed {where}"
            )
            stale = [main for main in seen if _after(latest.when, main.ran_at)]
            if state == "broken" and stale and len(stale) == len(seen):
                evidence.append(
                    f"main has not run {label} since that change landed, so its green "
                    "result is out of date (merges made with a workflow token start no "
                    "push CI)"
                )
        elif failing_mains:
            cause = f"main fails {label} since {failing_mains[0].sha[:8]}"
        incidents.append(
            Incident(names, found[0].summary, files, prs, state, confirmed, cause, tuple(evidence))
        )
    return incidents


def analyze(github: GitHub, red: dict[int, list[checks.FailingCheck]]) -> Report:
    """Classify every failing check on the given PRs and group what main caused."""
    reader = Reader(github)
    verdicts: list[Verdict] = []
    mains: dict[str, MainCheck | None] = {}
    problems: list[str] = []
    try:
        branch = reader.default_branch()
    except GitHubError as exc:
        return Report(problems=(f"could not read the default branch: {exc}",))

    def changed(path: str) -> Change | None:
        try:
            return reader.last_change(path)
        except GitHubError:
            return None

    for pr, failing in sorted(red.items()):
        if not failing:
            continue
        try:
            files = reader.pr_files(pr)
        except GitHubError as exc:
            problems.append(f"PR #{pr}: could not read its files: {exc}")
            files = frozenset()
        if not files:
            # Every PR changes something: an empty list means we could not tell, and
            # "not in the PR" would then be true of every file. Treat it as the PR's own.
            problems.append(f"PR #{pr}: its changed files are unknown; failures count as its own")
            verdicts.extend(
                Verdict(pr, item.name, OWN, "the PR's changed files are unknown", item.summary)
                for item in failing
            )
            continue
        for item in failing:
            try:
                main = reader.main_check(item)
            except GitHubError:
                main = None
            if mains.get(item.name) is None:
                mains[item.name] = main
            verdicts.append(judge(pr, item, files, main, changed))
    incidents = group(verdicts, mains, changed)
    return Report(tuple(incidents), tuple(verdicts), branch, tuple(problems))
