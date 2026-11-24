"""Turn ``why``'s diagnosis into work: what clears the bottleneck, and how many workers.

The planner reads the same board ``why`` does. When the review backlog is at
its limit, new issues cannot start, so the work that helps is finishing the
pull requests in that backlog (review, CI repair, review findings, merge
conflicts). When nothing is backed up, it is the issues that can start now.
Each task gets the provider and model with the best delivered track record.
Starting the plan submits one forced, targeted request per task.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    JSON,
    GitHub,
    GitHubError,
    Obj,
    label_names,
    obj,
    string,
)
from rotisserie.factory_console.service import (
    baseline,
    checks,
    dispatch,
    model_choice,
    outcomes,
    providers,
    queue,
)
from rotisserie.factory_console.service import conflicts as conflict_check
from rotisserie.factory_console.service import obsolete as obsolete_check
from rotisserie.factory_console.tracker import Clock

# Order within the backlog: a review can move a PR straight to ready (out of the
# backlog); repairs come next; conflicts need a rebase before anything merges.
ACTION_ORDER = ("review", "ci-repair", "review-findings", "merge-conflict")
ACTION_WORDS = {
    "review": "review it",
    "ci-repair": "fix its failing checks",
    "review-findings": "address its review feedback",
    "merge-conflict": "resolve its merge conflicts",
    "advance": "implement it",
}

HELD_WORDS = {
    "review": "reviewing it",
    "ci-repair": "fixing its checks",
    "review-findings": "addressing its review feedback",
    "merge-conflict": "resolving its merge conflicts",
}


@dataclass(frozen=True)
class Task:
    """One unit of work that helps: a target and the action it needs."""

    target: str  # pr:N | issue:N
    action: str  # review | ci-repair | review-findings | merge-conflict | advance
    title: str
    why: str


@dataclass(frozen=True)
class Assignment:
    task: Task
    provider: str  # provider key
    model: str
    why: str = ""  # why this model for this task

    def request(self) -> dispatch.Request:
        return dispatch.Request(
            runtime.resolve_provider(self.provider),
            self.model,
            target=self.task.target,
            force=True,
            action=self.task.action,
        )


@dataclass(frozen=True)
class Sync:
    """Merge the default branch into a PR whose only failures main already fixed."""

    pr: int
    head: str  # the head the plan saw; the update is refused if it moved
    title: str
    why: str


@dataclass(frozen=True)
class Synced:
    pr: int
    ok: bool
    text: str


@dataclass(frozen=True)
class Plan:
    """What is stuck, the work that clears it, and who does it."""

    diagnosis: tuple[queue.Reason, ...]
    assignments: tuple[Assignment, ...]
    held: tuple[queue.Reason, ...]  # helpful work someone is already doing
    left_out: int  # tasks beyond the worker cap
    needed_to_reopen_intake: int  # backlog PRs that must leave review for new issues to start
    model_choice: str  # why this provider/model, or why none could be picked
    problems: tuple[str, ...] = field(default=())  # why nothing can be started
    # Backlog PRs main has moved past (their files are gone): a person should close them.
    obsolete: tuple[queue.Reason, ...] = field(default=())
    # Items several models already failed on; another round is unlikely to help.
    needs_person: tuple[queue.Reason, ...] = field(default=())
    # Failures main caused: fixed once on main, never repaired PR by PR.
    shared_failures: tuple[baseline.Incident, ...] = field(default=())
    # PRs red only because of main; no worker until main is fixed.
    waiting_on_main: tuple[queue.Reason, ...] = field(default=())
    # PRs red only because of an old main that has since moved on: sync, no worker.
    syncs: tuple[Sync, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        from dataclasses import asdict

        return asdict(self)


def plan(
    github: GitHub,
    *,
    now: datetime,
    max_workers: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    skip: frozenset[str] = frozenset(),
    only: frozenset[str] = frozenset(),
) -> Plan:
    """Diagnose the queue and plan workers for it. Reads only; starts nothing."""
    variables = providers.repository_variables(github)
    summary = queue.summarize_queue(github, now=int(now.timestamp()), variables=variables)
    board = summary.board
    backlog = runtime.completion_backlog(list(board.prs))
    wip = runtime.pr_wip_count(list(board.prs))
    diagnosis: list[queue.Reason] = []
    tasks: list[Task] = []
    held: list[queue.Reason] = []

    review_full = len(backlog) >= board.review_backlog_limit
    needed = max(0, len(backlog) - board.review_backlog_limit + 1) if review_full else 0
    if review_full:
        diagnosis.append(
            queue.Reason(
                "review-limit",
                f"{len(backlog)} pull requests are waiting on review or checks "
                f"(limit {board.review_backlog_limit}). New issues cannot start until "
                f"{needed} of them {'is' if needed == 1 else 'are'} merged or ready to merge.",
                prs=tuple(item.number for item in backlog),
                setting="FACTORY_REVIEW_BACKLOG_LIMIT",
            )
        )
    if wip >= board.pr_wip_limit:
        diagnosis.append(
            queue.Reason(
                "wip-limit",
                f"{wip} of {board.pr_wip_limit} factory pull requests already have a worker. "
                "Those workers have to finish before more issues start.",
                setting="FACTORY_PR_WIP_LIMIT",
            )
        )

    free = [item for item in backlog if item.free]
    with ThreadPoolExecutor(max_workers=6) as pool:
        stale = dict(
            zip(
                (item.number for item in free),
                pool.map(lambda pr: _staleness(github, pr.number), free),
                strict=True,
            )
        )
    obsolete = [
        queue.Reason("obsolete", found.text, prs=(found.pr,))
        for found in stale.values()
        if found is not None and found.obsolete
    ]
    live = [item for item in backlog if stale.get(item.number) is None or not _gone(stale, item)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        red = dict(
            zip(
                (item.number for item in live),
                pool.map(lambda pr: _red_checks(github, pr.number), live),
                strict=True,
            )
        )
    try:
        shared = baseline.analyze(github, {number: found for number, found in red.items() if found})
    except GitHubError:
        shared = baseline.Report()
    waiting: list[queue.Reason] = []
    syncs: list[Sync] = []
    for item in sorted(backlog, key=lambda pr: (ACTION_ORDER.index(pr.action), pr.number)):
        found = stale.get(item.number)
        if found is not None and found.obsolete:
            continue
        everything = red.get(item.number) or []
        if everything and shared.waits_on_main(item.number):
            waiting.append(
                queue.Reason(
                    "waiting-on-main",
                    f"PR #{item.number}: red because main fails "
                    f"({_inherited_text(shared, item.number)}). No worker until main is fixed.",
                    prs=(item.number,),
                )
            )
            continue
        # A conflicting PR cannot be synced by update-branch; its merge-conflict work
        # merges main anyway, so it takes the normal path below.
        if everything and shared.needs_sync(item.number) and not item.conflicted:
            if item.free:
                head = _head(github, item.number)
                if head:
                    syncs.append(
                        Sync(
                            item.number,
                            head,
                            item.title,
                            f"red because of an older main "
                            f"({_inherited_text(shared, item.number)}); main has moved on"
                            + (
                                "; its own failures get a worker once the checks run again"
                                if shared.own_failures(item.number, everything)
                                else ""
                            ),
                        )
                    )
                    continue
            else:
                held.append(
                    queue.Reason(
                        "held",
                        f"PR #{item.number}: red because of an older main; sync it with "
                        "main once its worker finishes.",
                        prs=(item.number,),
                    )
                )
                continue
        failing = shared.own_failures(item.number, everything)
        if failing and item.action in ("review", "review-findings"):
            # No review round can validate while the PR's own checks are red.
            item = replace(item, action="ci-repair")
        if not item.free:
            if failing and item.action == "ci-repair":
                held.append(
                    queue.Reason(
                        "red-checks",
                        f"PR #{item.number}: its checks fail ({_red_text(failing)}), but the "
                        "worker on it is not repairing them. Take it over: run PROVIDER MODEL "
                        f"--target pr:{item.number} --action ci-repair --take-over",
                        prs=(item.number,),
                    )
                )
                continue
            worker = item.owner.removeprefix("factory:")
            holder = runtime.provider_for_worker(worker)
            who = (
                "A local worker"
                if worker == "local"
                else (holder.name if holder else f"Worker {worker}")
            )
            held.append(
                queue.Reason(
                    "held",
                    f"PR #{item.number}: {who} is {HELD_WORDS[item.action]}.",
                    prs=(item.number,),
                )
            )
            continue
        detail = f"; checks fail: {_red_text(failing)}" if failing else ""
        tasks.append(
            Task(
                f"pr:{item.number}",
                item.action,
                item.title,
                f"in the review backlog ({item.stage.removeprefix('factory:')}); "
                f"{ACTION_WORDS[item.action]}{detail}",
            )
        )
    if not review_full and wip < board.pr_wip_limit:
        # Every started issue becomes a PR in the backlog: start only as many as the
        # limits leave room for, or unstick recreates the jam it just cleared.
        room = max(
            0,
            min(board.review_backlog_limit - len(backlog), board.pr_wip_limit - wip),
        )
        for entry in summary.ready[:room]:
            tasks.append(
                Task(f"issue:{entry.issue}", "advance", entry.title, "can start now; implement it")
            )
        if not diagnosis:
            diagnosis.append(
                queue.Reason(
                    "ready",
                    f"No limit is holding work back; {len(summary.ready)} issues can start now. "
                    "The scheduler fills free slots on its own; unstick adds at most "
                    f"{room} (the room left under the review-backlog and open-PR limits).",
                )
            )

    # The operator's own scope wins over the planner: never start what they excluded.
    tasks = [
        task for task in tasks if task.target not in skip and (not only or task.target in only)
    ]
    capped = tasks if max_workers is None else tasks[:max_workers]
    left_out = len(tasks) - len(capped)
    choice_text, ranked, problems = model_choice.choose_models(
        github, variables, now, provider, model
    )
    missed = _recent_misses(github, now) if ranked and len(ranked) > 1 else {}
    needs_person: list[queue.Reason] = []
    keep: list[Task] = []
    for task in capped:
        tried = missed.get(task.target, set())
        if task.action == "merge-conflict" and len(tried) >= GIVE_UP_AFTER:
            overlap = _overlap_text(github, task.target)
            needs_person.append(
                queue.Reason(
                    "needs-person",
                    f"{task.target}: {len(tried)} models failed to resolve its conflicts"
                    f"{'; ' + overlap if overlap else ''}. Merge it by hand, or close it if "
                    "main already did this work (baste conflicts "
                    f"{task.target}).",
                    prs=(int(task.target.split(":")[1]),),
                )
            )
            continue
        if task.action == "merge-conflict":
            overlap = _overlap_text(github, task.target)
            if overlap:
                task = replace(task, why=f"{task.why}; {overlap}")
        keep.append(task)
    capped = keep
    assignments = tuple(_assign(task, ranked, missed) for task in capped) if ranked else ()
    if not tasks:
        problems = (*problems, "nothing is waiting that a new worker could take")
    syncs = [item for item in syncs if f"pr:{item.pr}" not in skip]
    if only:
        syncs = [item for item in syncs if f"pr:{item.pr}" in only]
    return Plan(
        tuple(diagnosis),
        assignments,
        tuple(held),
        left_out,
        needed,
        choice_text,
        tuple(problems),
        tuple(obsolete),
        tuple(needs_person),
        shared.incidents,
        tuple(waiting),
        tuple(syncs),
    )


def _inherited_text(shared: baseline.Report, pr: int) -> str:
    found = [item for item in shared.for_pr(pr) if item.not_the_prs]
    names = ", ".join(dict.fromkeys(item.check for item in found))
    return f"{names}: {found[0].summary}" if found else names


def _head(github: GitHub, number: int) -> str:
    try:
        return string(obj(obj(github.repo_api(f"pulls/{number}")).get("head")), "sha")
    except GitHubError:
        return ""


def sync(github: GitHub, item: Sync) -> Synced:
    """Merge the default branch into the PR, only if its head is still the planned one.

    GitHub refuses the update when the head moved (a worker pushed) or the merge
    conflicts; either way nothing changes and the reason is reported. This is
    the shared ``factory_branch_sync`` path: a Neon lease where Neon is
    reachable, otherwise owner labels (and the result says so).
    """
    from rotisserie.factory_console.service import sync as branch_sync

    plan = branch_sync.Plan((branch_sync.Candidate(item.pr, item.head, item.title, item.why),))
    pull = None
    try:
        pull = obj(github.repo_api(f"pulls/{item.pr}"))
    except GitHubError as exc:
        return Synced(item.pr, False, f"PR #{item.pr}: not synced: {exc}")
    base = string(obj(pull.get("base")), "ref")
    if not base:
        try:
            base = string(obj(github.api(f"repos/{github.repo}")), "default_branch")
        except GitHubError:
            base = ""
    labels: list[JSON] = [*sorted(label_names(pull))]
    row: Obj = {
        "number": item.pr,
        "state": string(pull, "state") or "open",
        "headRefOid": string(obj(pull.get("head")), "sha") or item.head,
        "baseRefName": base,
        "labels": labels,
        "mergeStateStatus": string(pull, "mergeable_state"),
    }
    changes, _ = branch_sync.execute(
        github, plan, [row], holder=f"console-{item.pr}", limit=1, now=True
    )
    if not changes:
        return Synced(item.pr, False, f"PR #{item.pr}: not synced")
    change = changes[0]
    return Synced(item.pr, change.ok, change.text)


def runtime_owner(label: str) -> bool:
    """A factory owner label naming a worker (not the unowned marker)."""
    return label.startswith("factory:") and (
        label.removeprefix("factory:").isdigit() or label == "factory:local"
    )


# Distinct models that must have missed a merge conflict before a person is asked.
GIVE_UP_AFTER = 3


def _overlap_text(github: GitHub, target: str) -> str:
    try:
        report = conflict_check.pr_conflicts(github, int(target.split(":")[1]))
    except GitHubError:
        return ""
    return report.text.split(": ", 1)[1] if report.overlaps else ""


def _gone(stale: dict[int, obsolete_check.Staleness | None], item: runtime.BacklogPR) -> bool:
    found = stale.get(item.number)
    return found is not None and found.obsolete


def _red_checks(github: GitHub, number: int) -> list[checks.FailingCheck]:
    try:
        return checks.failing_checks(github, number)
    except GitHubError:
        return []


def _red_text(failing: list[checks.FailingCheck]) -> str:
    return "; ".join(f"{item.name}: {item.summary}" for item in failing[:2])


def _staleness(github: GitHub, number: int) -> obsolete_check.Staleness | None:
    try:
        return obsolete_check.pr_staleness(github, number)
    except GitHubError:
        return None


# How far back a model's miss on an item keeps it off that item.
MISS_WINDOW = timedelta(hours=6)


def _recent_misses(github: GitHub, now: datetime) -> dict[str, set[str]]:
    """Models whose recent request on each target did not reach its goal."""
    found: dict[str, set[str]] = {}
    try:
        recent = outcomes.request_outcomes(github, since=now - MISS_WINDOW)
    except GitHubError:
        return found
    for item in recent:
        if item.goal == "not-met" and item.model and item.target:
            found.setdefault(item.target, set()).add(item.model)
    return found


def _assign(
    task: Task, ranked: list[tuple[str, str, str]], missed: dict[str, set[str]]
) -> Assignment:
    """The best-ranked model that has not just missed on this task's target."""
    tried = missed.get(task.target, set())
    for provider, model, why in ranked:
        if model not in tried:
            note = f"; {', '.join(sorted(tried))} just missed on it" if tried else ""
            return Assignment(task, provider, model, why + note)
    provider, model, why = ranked[0]
    return Assignment(
        task, provider, model, why + "; every offered model already missed on it recently"
    )


def start(github: GitHub, clock: Clock, chosen: Plan) -> list[dispatch.Submission]:
    """Submit every planned worker. Each is a forced, targeted scheduler request."""
    return [dispatch.submit(github, clock, item.request()) for item in chosen.assignments]
