"""Failed attempts: the PRs the factory closed as failed, and what each issue did next.

The self-heal sweep closes a parked PR as a failed attempt and requeues its
issue (``factory_self_heal_park_retry``). This view reads GitHub only: one
listing of recently updated closed PRs, one of open PRs, the comments of each
closed factory PR in the window, and each linked issue that has no merge yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, integer, items, label_names, obj, string

LIMIT = 25
SPLIT_LABELS = ("umbrella", "tracking")


@dataclass(frozen=True)
class Attempt:
    pr: int
    title: str
    closed_at: str
    url: str
    issue: int | None
    next: str
    next_pr: int | None
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def linked_issue(pull: Obj) -> int | None:
    """The issue a PR works on, by the resolver the factory itself uses."""
    shape = {
        "headRefName": string(obj(pull.get("head")), "ref"),
        "body": pull.get("body"),
        "title": pull.get("title"),
    }
    base = runtime.module("factory_work_policy_base")
    guard = runtime.module("factory_issue_intake_guard")
    found = base.linked_issue_from_pr(shape) or guard.sliced_issue(shape)
    return int(found) if found else None


def closed_as_failed(comments: list[Obj], pr: int) -> bool:
    rules = runtime.module("factory_self_heal_rules")
    return any(row["kind"] == rules.PARK_CLOSED for row in rules.heals(comments, pr=pr))


def issue_state(issue: Obj) -> tuple[str, str]:
    if string(issue, "state") == "closed":
        reason = string(issue, "state_reason").replace("_", " ") or "completed"
        return "issue closed", reason
    names = label_names(issue)
    split = [name for name in SPLIT_LABELS if name in names]
    if split:
        return "split", f"labeled {split[0]}"
    base = runtime.module("factory_work_policy_base")
    stage = next((name for name in base.STAGE_PRECEDENCE if name in names), "")
    if stage:
        return "open", stage
    return "open", "queued (factory:unowned)" if "factory:unowned" in names else "open"


def follow_up(
    issue: int, closed_at: str, merged: list[tuple[str, int, int]], open_prs: dict[int, int]
) -> tuple[str, int | None, str] | None:
    later = sorted((at, pr) for at, pr, target in merged if target == issue and at > closed_at)
    if later:
        return "merged", later[0][1], later[0][0]
    if issue in open_prs:
        return "open PR", open_prs[issue], ""
    return None


def recent(github: GitHub, *, since: datetime, limit: int = LIMIT) -> list[Attempt]:
    iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed = items(github.repo_api("pulls?state=closed&sort=updated&direction=desc&per_page=100"))
    merged: list[tuple[str, int, int]] = []
    candidates: list[Obj] = []
    for pull in closed:
        if pull.get("merged_at"):
            target = linked_issue(pull)
            if target:
                merged.append((string(pull, "merged_at"), integer(pull, "number") or 0, target))
        elif string(pull, "closed_at") >= iso and "factory" in label_names(pull):
            candidates.append(pull)
    open_prs: dict[int, int] = {}
    for pull in items(github.repo_api("pulls?state=open&per_page=100")):
        target = linked_issue(pull)
        if target and "factory" in label_names(pull):
            open_prs.setdefault(target, integer(pull, "number") or 0)
    found: list[Attempt] = []
    issues: dict[int, Obj] = {}
    for pull in sorted(candidates, key=lambda row: string(row, "closed_at"), reverse=True):
        number = integer(pull, "number") or 0
        comments = items(github.repo_api(f"issues/{number}/comments?since={iso}&per_page=100"))
        if not closed_as_failed(comments, number):
            continue
        closed_at = string(pull, "closed_at")
        issue = linked_issue(pull)
        next_word, next_pr, detail = "no linked issue", None, ""
        if issue:
            after = follow_up(issue, closed_at, merged, open_prs)
            if after:
                next_word, next_pr, detail = after
            else:
                if issue not in issues:
                    issues[issue] = obj(github.repo_api(f"issues/{issue}"))
                next_word, detail = issue_state(issues[issue])
        found.append(
            Attempt(
                pr=number,
                title=string(pull, "title"),
                closed_at=closed_at,
                url=string(pull, "html_url"),
                issue=issue,
                next=next_word,
                next_pr=next_pr,
                detail=detail,
            )
        )
        if len(found) >= limit:
            break
    return found
