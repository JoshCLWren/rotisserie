"""The default task an owner hands to an outside agent with ``baste assign``.

The text follows the item's current factory stage and adds what is already
known: conflicting files, failing checks and the linked issue's criteria.
"""

from __future__ import annotations

from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import checks, conflicts
from rotisserie.factory_console.service.external_checks import _required_checks_outcome

KINDS = ("implement", "review", "review-findings", "ci-repair", "merge-conflict")
MAX_CRITERIA = 10
MAX_DETAIL = 160

STEPS = {
    "implement": (
        "Implement issue {ref} on a new branch and open a pull request whose body says "
        "`Closes {ref}`. For an incident issue, say `Refs {ref}` instead: the incident closes "
        "only after production evidence.",
        "End every commit message with a `Model: <your exact model id>` line.",
    ),
    "review": (
        "Do not review this pull request if it is labeled `factory:parked`, if you or your "
        "model authored any of its commits (another session of the same model is not "
        "independent), or if another agent already reviewed its current head commit. "
        "Comment why and stop.",
        "Read the full diff of this pull request at its current head, with the surrounding "
        "code and tests.",
        "Submit one formal GitHub review on that head with one inline comment per blocking "
        "problem. If GitHub refuses **Approve** or **Request changes** because your account "
        "opened the PR, submit it as **Comment**.",
        "Post a summary that names the reviewed head commit SHA. End the summary and the "
        "review body with `Model: <your exact model id>` and then exactly one last line: "
        "`Verdict: changes-requested` or `Verdict: approve`.",
        "An approval merges this pull request. Approve only when it meets every acceptance "
        "criterion, its required CI is green at the current head, and no blocking findings "
        "remain. Check the head has not changed right before the verdict.",
        "If CI is pending or cannot be verified, do not approve and do not invent a change "
        "request: explain the limitation instead.",
    ),
    "review-findings": (
        "Read every unresolved review comment on this pull request.",
        "Push commits to this pull request's branch that fix each verified finding, and "
        "answer each finding you do not fix.",
        "End every commit message with a `Model: <your exact model id>` line.",
    ),
    "ci-repair": (
        "Make this pull request's failing required checks pass. Push the fix to its branch.",
        "End every commit message with a `Model: <your exact model id>` line.",
    ),
    "merge-conflict": (
        "Merge the base branch into this pull request's branch and resolve every conflict, "
        "keeping both sides' intent.",
        "End the merge commit message with a `Model: <your exact model id>` line.",
    ),
}
TITLES = {
    "implement": "implement this issue",
    "review": "review this PR",
    "review-findings": "fix the review findings on this PR",
    "ci-repair": "fix this PR's failing checks",
    "merge-conflict": "resolve this PR's merge conflict",
}


def _short(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= MAX_DETAIL else line[: MAX_DETAIL - 3] + "..."


def _criteria(body: str) -> list[str]:
    try:
        found = runtime.module("factory_acceptance_criteria").criteria(body)
    except ValueError, runtime.RuntimeUnavailableError, AttributeError:
        return []
    return [_short(str(item.text)) for item in found[:MAX_CRITERIA]]


def _issue(github: GitHub, pr: dict[str, Any], number: int) -> tuple[int | None, str]:
    link = runtime.module("factory_work_policy").linked_issue_from_pr(
        {
            "number": number,
            "headRefName": string(obj(pr.get("head")), "ref"),
            "body": string(pr, "body"),
            "title": string(pr, "title"),
        }
    )
    if link is None or int(link) == number:
        return None, ""
    try:
        issue = obj(github.repo_api(f"issues/{link}"))
    except GitHubError:
        return int(link), ""
    return int(link), string(issue, "body")


class NoDefaultTask(ValueError):
    """The PR's stage asks for no outside work, so the owner must name the task."""


def stage_kind(github: GitHub, kind: str, number: int, item: dict[str, Any]) -> str:
    if kind == "issue":
        return "implement"
    labels = runtime.module("factory_work_policy").labels_of(item)
    if string(item, "mergeable_state") == "dirty" or "factory:merge-conflict" in labels:
        return "merge-conflict"
    if "factory:changes-requested" in labels:
        return "review-findings"
    if "factory:ci" in labels and _required_checks_outcome(github, number) == "failed":
        return "ci-repair"
    if "factory:review" in labels:
        return "review"
    stage = runtime.module("factory_work_policy").stage_of(labels) or "no factory stage"
    raise NoDefaultTask(
        f"PR #{number} is at {stage}, which has no default task; pass --kind or --task"
    )


def kind_for(github: GitHub, target: str) -> str:
    noun, _, raw = target.partition(":")
    number = int(raw)
    if noun == "issue":
        return "implement"
    return stage_kind(github, noun, number, obj(github.repo_api(f"pulls/{number}")))


def _details(github: GitHub, kind: str, number: int) -> list[str]:
    try:
        if kind == "merge-conflict":
            report = conflicts.pr_conflicts(github, number)
            return [
                f"`{row.path}`: main changed it in {row.change}"
                + (f" (#{row.pr})" if row.pr else "")
                for row in report.overlaps
            ] or [report.text]
        if kind == "ci-repair":
            return [
                f"{row.name} ({row.kind}): {_short(row.summary)} {row.url}".strip()
                for row in checks.failing_checks(github, number)
            ]
    except GitHubError:
        return []
    return []


def build(github: GitHub, target: str, kind: str = "") -> tuple[str, str]:
    """``(kind, markdown)`` for the default task. ``kind`` overrides the stage."""
    noun, _, raw = target.partition(":")
    number = int(raw)
    item = obj(github.repo_api(f"pulls/{number}" if noun == "pr" else f"issues/{number}"))
    kind = kind or stage_kind(github, noun, number, item)
    issue, body = (
        (number, string(item, "body")) if noun == "issue" else _issue(github, item, number)
    )
    ref = f"#{issue}" if issue else "the linked issue"
    lines = [f"**Task: {TITLES[kind]}.**", ""]
    steps = [step.format(ref=ref) for step in STEPS[kind]]
    if issue and noun == "pr":
        steps.insert(0, f"Read issue #{issue}: its acceptance criteria decide what done means.")
    lines += [f"{index}. {step}" for index, step in enumerate(steps, 1)]
    details = _details(github, kind, number)
    if details:
        heading = "Files main also changed" if kind == "merge-conflict" else "Failing checks"
        lines += ["", f"{heading}:", *(f"- {row}" for row in details)]
    criteria = _criteria(body) if body else []
    if criteria:
        lines += ["", f"Acceptance criteria of #{issue}:", *(f"- {row}" for row in criteria)]
    return kind, "\n".join(lines)


def comment(target: str, agent: str, task: str, marker: str) -> str:
    """The posted task, consistent with the outside agent steps in external-agent-claims.md."""
    return "\n".join(
        [
            marker,
            f"**Assigned to `agent:{agent}`.** That label holds this item: factory workers "
            "stay off it while it is there.",
            "",
            task.strip(),
            "",
            "Push to this item's branch (for an issue, open a PR that says `Closes #N`). "
            "End every commit message with `Model: <your exact model id>`.",
            f"When you are done, comment `DONE` here and remove the `agent:{agent}` label. "
            f"The owner can also end it with `baste finish {target}`.",
            "",
            "Do not merge, push to main, or change other factory labels.",
        ]
    )
