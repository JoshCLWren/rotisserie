"""Turn ``why``'s diagnosis into work: what clears the bottleneck, and how many workers.

The planner reads the same board ``why`` does. When the review backlog is at
its limit, new issues cannot start, so the work that helps is finishing the
pull requests in that backlog (review, CI repair, review findings, merge
conflicts). When nothing is backed up, it is the issues that can start now.
Each task gets the provider and model with the best delivered track record.
Starting the plan submits one forced, targeted request per task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import catalog, dispatch, providers, queue, reliability
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

    def request(self) -> dispatch.Request:
        return dispatch.Request(
            runtime.resolve_provider(self.provider),
            self.model,
            target=self.task.target,
            force=True,
            action=self.task.action,
        )


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

    for item in sorted(backlog, key=lambda pr: (ACTION_ORDER.index(pr.action), pr.number)):
        if not item.free:
            worker = item.owner.removeprefix("factory:")
            found = runtime.provider_for_worker(worker)
            who = (
                "A local worker"
                if worker == "local"
                else (found.name if found else f"Worker {worker}")
            )
            held.append(
                queue.Reason(
                    "held",
                    f"PR #{item.number}: {who} is {HELD_WORDS[item.action]}.",
                    prs=(item.number,),
                )
            )
            continue
        tasks.append(
            Task(
                f"pr:{item.number}",
                item.action,
                item.title,
                f"in the review backlog ({item.stage.removeprefix('factory:')}); "
                f"{ACTION_WORDS[item.action]}",
            )
        )
    if not review_full and wip < board.pr_wip_limit:
        for entry in summary.ready:
            tasks.append(
                Task(f"issue:{entry.issue}", "advance", entry.title, "can start now; implement it")
            )
        if not diagnosis:
            diagnosis.append(
                queue.Reason(
                    "ready",
                    f"No limit is holding work back; {len(summary.ready)} issues can start now.",
                )
            )

    capped = tasks if max_workers is None else tasks[:max_workers]
    choice_text, chosen, problems = _choose_model(github, variables, now, provider, model)
    assignments = tuple(Assignment(task, chosen[0], chosen[1]) for task in capped) if chosen else ()
    if not tasks:
        problems = (*problems, "nothing is waiting that a new worker could take")
    return Plan(
        tuple(diagnosis),
        assignments,
        tuple(held),
        len(tasks) - len(capped),
        needed,
        choice_text,
        tuple(problems),
    )


def _choose_model(
    github: GitHub,
    variables: dict[str, str],
    now: datetime,
    provider: str | None,
    model: str | None,
) -> tuple[str, tuple[str, str] | None, tuple[str, ...]]:
    if provider and model:
        found = runtime.resolve_provider(provider)
        runtime.validate_model(found, model)
        return "you chose it", (found.key, model), ()
    stats = reliability.load_stats(github)
    if stats is None:
        return (
            "no track record is published yet",
            None,
            ("no reliability snapshot to pick a model from; name PROVIDER MODEL",),
        )
    setup = providers.provider_setup(github, variables)
    ready = {item.provider.key for item in setup if item.ready}
    if provider:
        ready &= {runtime.resolve_provider(provider).key}
    choices = reliability.pick_models(stats, ready=ready, now=now, live=catalog.live_models)
    if not choices:
        return (
            "no set-up model has delivered recently",
            None,
            (
                "no set-up, unpaused model that the provider still offers has a track record; "
                "name PROVIDER MODEL",
            ),
        )
    best = choices[0]
    return f"best track record: {best.why}", (best.provider.key, best.model), ()


def start(github: GitHub, clock: Clock, chosen: Plan) -> list[dispatch.Submission]:
    """Submit every planned worker. Each is a forced, targeted scheduler request."""
    return [dispatch.submit(github, clock, item.request()) for item in chosen.assignments]
