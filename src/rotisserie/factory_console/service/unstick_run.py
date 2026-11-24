"""Carry out an ``unstick`` plan one action at a time, reporting each as it happens.

One failed request does not hide the others: every action gets a result. A
GitHub rate limit stops the run, because every later request would fail too.
"""

from __future__ import annotations

from dataclasses import dataclass

from rotisserie.factory_console.github import GitHub, GitHubError, GitHubRateLimitError
from rotisserie.factory_console.service import dispatch, unstick
from rotisserie.factory_console.service.steps import Step, quiet
from rotisserie.factory_console.tracker import Clock


@dataclass(frozen=True)
class Started:
    target: str
    action: str
    model: str
    run_id: int | None = None
    url: str = ""
    error: str = ""


@dataclass(frozen=True)
class Executed:
    synced: tuple[unstick.Synced, ...]
    started: tuple[Started, ...]
    not_attempted: tuple[str, ...] = ()
    stopped: str = ""


def execute(github: GitHub, clock: Clock, chosen: unstick.Plan, step: Step = quiet) -> Executed:
    total = len(chosen.syncs) + len(chosen.assignments)
    synced: list[unstick.Synced] = []
    for count, item in enumerate(chosen.syncs, 1):
        target = f"pr:{item.pr}"
        step(f"Syncing {target} with main ({count} of {total})", phase="act", target=target)
        try:
            change = unstick.sync(github, item)
        except GitHubRateLimitError as exc:
            step(f"{target}: not synced: {exc}", phase="result", target=target, error=str(exc))
            return Executed(
                (*synced, unstick.Synced(item.pr, False, f"PR #{item.pr}: not synced: {exc}")),
                (),
                _rest(chosen, count),
                str(exc),
            )
        except GitHubError as exc:
            change = unstick.Synced(item.pr, False, f"PR #{item.pr}: not synced: {exc}")
        synced.append(change)
        step(
            change.text,
            phase="result",
            target=target,
            **({} if change.ok else {"error": change.text}),
        )
    started: list[Started] = []
    offset = len(chosen.syncs)
    for count, assignment in enumerate(chosen.assignments, offset + 1):
        task = assignment.task
        step(
            f"Starting a worker to {unstick.ACTION_WORDS[task.action]} on {task.target} "
            f"with {assignment.model} ({count} of {total})",
            phase="act",
            target=task.target,
        )
        try:
            submission = dispatch.submit(github, clock, assignment.request())
        except GitHubError as exc:
            started.append(Started(task.target, task.action, assignment.model, error=str(exc)))
            step(
                f"{task.target}: not started: {exc}",
                phase="result",
                target=task.target,
                error=str(exc),
            )
            if isinstance(exc, GitHubRateLimitError):
                return Executed(tuple(synced), tuple(started), _rest(chosen, count), str(exc))
            continue
        started.append(
            Started(task.target, task.action, assignment.model, submission.run_id, submission.url)
        )
        if submission.run_id is None:
            text = f"{task.target}: sent, but its scheduler run could not be found"
        else:
            text = f"{task.target}: request sent"
        step(text, phase="result", target=task.target, url=submission.url)
    return Executed(tuple(synced), tuple(started))


def _rest(chosen: unstick.Plan, done: int) -> tuple[str, ...]:
    targets = [f"pr:{item.pr}" for item in chosen.syncs]
    targets += [item.task.target for item in chosen.assignments]
    return tuple(targets[done:])
