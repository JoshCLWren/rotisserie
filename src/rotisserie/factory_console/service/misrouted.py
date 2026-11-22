"""Pull requests waiting for review whose own checks are red: route them to CI repair.

A review round cannot validate while the PR's own checks fail. Comic Pile PRs
#5669 (its own Clippy error) and #5674 (its own stale OpenAPI contract) sat in
``factory:review`` and kept drawing reviewers: their one required aggregate
check passed while a path-filtered check beside it failed.

``scan`` lists ``factory:review`` PRs with failing checks and uses the
shared-failure analysis (``baseline.analyze``) to keep only failures the PR
caused. A PR red only because of main keeps today's path (main is fixed once).
``requeue`` moves a misrouted PR to ``factory:ci`` (CI repair) with one
full label-set replacement, after re-reading the PR: it refuses when the head
moved, the stage changed, a worker's owner label appeared, or Neon (where
reachable) shows a live lease on it. It never touches a PR a worker holds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    GitHub,
    GitHubError,
    Obj,
    integer,
    label_names,
    obj,
    string,
)
from rotisserie.factory_console.service import baseline, checks, providers
from rotisserie.factory_console.service import sync as leases

REVIEW = "factory:review"
CI = "factory:ci"
UNOWNED = "factory:unowned"
ADVISORY_SETTING = "FACTORY_ADVISORY_CHECKS"


@dataclass(frozen=True)
class Misrouted:
    """One PR in review whose own checks fail."""

    pr: int
    head: str
    title: str
    owner: str  # factory owner label; "factory:unowned" when no worker holds it
    own: tuple[checks.FailingCheck, ...]  # failures the PR caused
    inherited: tuple[str, ...] = ()  # failing checks main caused (not the PR's)

    @property
    def held(self) -> bool:
        return self.owner not in ("", UNOWNED)

    @property
    def text(self) -> str:
        failing = "; ".join(f"{item.name}: {item.summary}" for item in self.own[:2])
        more = f" (and {len(self.own) - 2} more)" if len(self.own) > 2 else ""
        return f"PR #{self.pr}: in review, but its own checks fail ({failing}{more})"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["held"] = self.held
        data["text"] = self.text
        return data


@dataclass(frozen=True)
class Scan:
    """Review-stage PRs with red checks, split by who caused the failure."""

    misrouted: tuple[Misrouted, ...] = ()
    waiting_on_main: tuple[int, ...] = ()  # red only because of main: review stays
    scanned: int = 0  # review-stage PRs looked at
    problems: tuple[str, ...] = field(default=())

    @property
    def movable(self) -> tuple[Misrouted, ...]:
        return tuple(item for item in self.misrouted if not item.held)

    def to_dict(self) -> dict[str, object]:
        return {
            "misrouted": [item.to_dict() for item in self.misrouted],
            "waiting_on_main": list(self.waiting_on_main),
            "scanned": self.scanned,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class Moved:
    pr: int
    ok: bool
    text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def advisory_checks(github: GitHub) -> frozenset[str]:
    """Checks the adopter declared advisory; like the merge gate, they never count."""
    raw = providers.repository_variables(github).get(ADVISORY_SETTING, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def _failing(github: GitHub, number: int) -> list[checks.FailingCheck]:
    try:
        return checks.failing_checks(github, number)
    except GitHubError:
        return []


def scan(
    github: GitHub,
    *,
    rows: Sequence[Obj] | None = None,
    advisory: frozenset[str] | None = None,
) -> Scan:
    """Review-stage factory PRs whose own checks fail. Reads only; changes nothing."""
    pulls = list(rows) if rows is not None else leases.open_pull_rows(github)
    skip = advisory_checks(github) if advisory is None else advisory
    heads = {integer(row, "number") or 0: string(row, "headRefOid") for row in pulls}
    backlog = [
        item
        for item in runtime.completion_backlog(list(pulls))
        if item.stage == REVIEW and not item.conflicted
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = list(pool.map(lambda item: _failing(github, item.number), backlog))
    red = {
        item.number: [check for check in failing if check.name not in skip]
        for item, failing in zip(backlog, found, strict=True)
    }
    red = {number: failing for number, failing in red.items() if failing}
    if not red:
        return Scan(scanned=len(backlog))
    try:
        shared = baseline.analyze(github, red)
    except GitHubError as exc:
        # Without the main comparison nothing can be called the PR's own.
        return Scan(scanned=len(backlog), problems=(f"could not compare with main: {exc}",))
    misrouted: list[Misrouted] = []
    waiting: list[int] = []
    for item in backlog:
        failing = red.get(item.number)
        if not failing:
            continue
        own = shared.own_failures(item.number, failing)
        if not own or shared.waits_on_main(item.number):
            waiting.append(item.number)
            continue
        mine = {check.name for check in own}
        misrouted.append(
            Misrouted(
                item.number,
                heads.get(item.number, ""),
                item.title,
                item.owner or UNOWNED,
                tuple(own),
                tuple(check.name for check in failing if check.name not in mine),
            )
        )
    return Scan(tuple(misrouted), tuple(waiting), len(backlog), shared.problems)


def _owners(labels: set[str]) -> list[str]:
    return sorted(
        label
        for label in labels
        if label.startswith("factory:")
        and (label.removeprefix("factory:").isdigit() or label == "factory:local")
    )


def _neon_holder(github: GitHub, number: int) -> str:
    """Who holds a live Neon lease on the PR; empty when none or Neon is not configured.

    Where Neon is configured but cannot be read, this fails closed: the lease
    state is unknown, so the PR is treated as held.
    """
    try:
        report = leases.lease_report(github, rows=[], numbers=[number])
    except Exception as exc:  # the ledger's driver raises its own error types
        return f"Neon leases could not be read: {exc}"
    if report.source != "neon":
        return ""
    return next((row.holder for row in report.rows if row.active), "")


def requeue(github: GitHub, item: Misrouted) -> Moved:
    """Move one misrouted PR from review to CI repair, only if nothing changed since the scan."""
    if item.held:
        return Moved(item.pr, False, f"PR #{item.pr} is held by {item.owner}; left alone.")
    try:
        live = obj(github.repo_api(f"pulls/{item.pr}"))
    except GitHubError as exc:
        return Moved(item.pr, False, f"PR #{item.pr}: not moved: {exc}")
    labels = label_names(live)
    if string(live, "state") != "open" or string(obj(live.get("head")), "sha") != item.head:
        return Moved(item.pr, False, f"PR #{item.pr} changed since it was checked; re-run.")
    if REVIEW not in labels or labels & {CI, "factory:ready", "factory:blocked"}:
        return Moved(item.pr, False, f"PR #{item.pr} is no longer waiting for review; left alone.")
    owners = _owners(labels)
    if owners:
        return Moved(item.pr, False, f"PR #{item.pr} was just claimed by {owners[0]}; left alone.")
    holder = _neon_holder(github, item.pr)
    if holder:
        return Moved(item.pr, False, f"PR #{item.pr} has a live lease ({holder}); left alone.")
    stages = {REVIEW, "factory:changes-requested", "factory:building"}
    keep = sorted(label for label in labels if label not in stages and label != UNOWNED)
    wanted = sorted({*keep, "factory", UNOWNED, CI})
    try:
        github.gh(
            "api",
            "--method",
            "PUT",
            f"repos/{github.repo}/issues/{item.pr}/labels",
            "--input",
            "-",
            stdin=json.dumps({"labels": wanted}),
        )
    except GitHubError as exc:
        return Moved(item.pr, False, f"PR #{item.pr}: not moved: {exc}")
    names = ", ".join(check.name for check in item.own)
    return Moved(
        item.pr,
        True,
        f"PR #{item.pr} moved from review to CI repair (its own checks fail: {names}).",
    )
