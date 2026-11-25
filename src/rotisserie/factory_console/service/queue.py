"""Queue eligibility: why the factory is or is not picking up an issue.

Front-end neutral: functions here read GitHub and return typed results with
structured reasons. The CLI (``baste why``) only renders them.

Every answer comes from the dispatcher's own rules: the shared eligibility
module, the controller's backpressure gates, and Rotisserie's intake decision,
run over the same GitHub rows the controller reads. Nothing is re-implemented
here except the wording.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import (
    JSON,
    GitHub,
    GitHubError,
    GitHubNotFoundError,
    Obj,
    items,
    obj,
    string,
)
from rotisserie.factory_console.service import parked

# The exact fields the controller's own discovery reads (factory-work-controller.py).
ISSUE_FIELDS = "number,title,body,labels,createdAt,updatedAt"
PR_FIELDS = (
    "number,title,body,labels,headRefName,headRefOid,createdAt,updatedAt,"
    "isDraft,mergeable,mergeStateStatus,isCrossRepository,baseRefName"
)
CONTROL_PATH = ".github/factory-rotisserie-control.json"
SHA = re.compile(r"^[0-9a-f]{40}$")

INTAKE_WORDS = {
    "backpressure": "the factory is finishing open pull requests before starting new issues",
    "capacity": "no worker capacity is free for new issues",
    "dependency_incomplete": "an issue it depends on is still open",
    "human_boundary": "it needs a person (manual-only, blocked, epic/PRD, done or excluded)",
    "implementation_exists": "a pull request already implements it",
    "active_lease": "a worker already holds it",
    "not_open": "it is not open",
    "parent_work": "it is parent work, not a task",
    "product_exclusion": "it is excluded from automatic work",
    "blocked": "it is labeled blocked",
    "terminal": "it is already finished",
    "retry_budget_exhausted": "its retry budget is spent",
    "selector_metadata_missing": "it lacks the labels the selector needs",
}


@dataclass
class Board:
    """One read of open work, as the controller would see it at this moment."""

    issues: list[Obj]
    prs: list[Obj]
    pr_wip_limit: int
    review_backlog_limit: int
    # None: legacy lane, no intake restriction. Otherwise number -> (eligible, reasons).
    intake: dict[int, tuple[bool, tuple[str, ...]]] | None
    intake_problem: str = ""
    incidents: dict[int, int] = field(default_factory=dict)

    def admitted(self) -> set[int] | None:
        if self.intake is None:
            return None
        return {number for number, (eligible, _) in self.intake.items() if eligible}


def _rows(raw: str) -> list[Obj]:
    try:
        value: JSON = json.loads(raw)
    except ValueError:
        raise GitHubError("GitHub returned something that is not JSON.") from None
    return items(value)


def read_board(
    github: GitHub, variables: dict[str, str], now: int, looping: frozenset[int] = frozenset()
) -> Board:
    listing = ("list", "--repo", github.repo, "--state", "open", "--json")
    issues = _rows(github.gh("issue", *listing, ISSUE_FIELDS, "--limit", "1000"))
    prs = _rows(github.gh("pr", *listing, PR_FIELDS, "--limit", "500"))
    wip_limit = runtime.setting_integer("FACTORY_PR_WIP_LIMIT", variables)
    backlog_limit = runtime.setting_integer("FACTORY_REVIEW_BACKLOG_LIMIT", variables)
    prs = parked.stamp(github, prs, backlog_limit, now, looping)
    board = Board(issues, prs, wip_limit, backlog_limit, intake={})
    board.incidents = runtime.module("factory_work_policy").declared_incidents(variables)
    _read_intake(github, board, now)
    return board


def _read_intake(github: GitHub, board: Board, now: int) -> None:
    """Run Rotisserie's intake decision; on any failure admit nothing, like the controller."""
    try:
        revision = string(obj(github.repo_api("commits/HEAD")), "sha").lower()
        if not SHA.fullmatch(revision):
            raise ValueError("could not read the default branch's commit")
        raw = obj(github.repo_api(f"contents/{CONTROL_PATH}?ref={revision}"))
        control: object = json.loads(base64.b64decode(string(raw, "content")).decode())
        board.intake = runtime.intake_verdicts(
            control,
            list(board.issues),
            list(board.prs),
            repository=github.repo,
            revision=revision,
            now=now,
            pr_wip_limit=board.pr_wip_limit,
            review_backlog_limit=board.review_backlog_limit,
        )
    except GitHubNotFoundError:
        board.intake = {}
        board.intake_problem = f"{CONTROL_PATH} is missing, so the factory admits no new issues"
    except runtime.INTAKE_ERRORS as exc:
        board.intake = {}
        board.intake_problem = f"the intake decision could not be made ({exc})"


def open_blocker(github: GitHub, number: int) -> tuple[bool, str]:
    """GitHub's own dependency links. Unreadable counts as blocked, like the controller."""
    try:
        rows = items(github.repo_api(f"issues/{number}/dependencies/blocked_by?per_page=100"))
    except GitHubError:
        return True, "GitHub's dependency list could not be read, which counts as blocked"
    open_numbers = [
        str(row.get("number")) for row in rows if string(row, "state").lower() == "open"
    ]
    if not open_numbers:
        return False, ""
    return True, "GitHub lists open blocking issues: " + ", ".join(f"#{n}" for n in open_numbers)


def issue_row(github: GitHub, board: Board, number: int) -> Obj:
    """The issue as the controller sees it; closed issues are fetched directly."""
    for row in board.issues:
        if row.get("number") == number:
            return row
    raw = obj(github.repo_api(f"issues/{number}"))
    if "pull_request" in raw:
        raise ValueError(
            f"#{number} is a pull request. 'why' explains issues; "
            f"see 'baste status' for pull requests."
        )
    return {
        "number": number,
        "title": string(raw, "title"),
        "body": string(raw, "body"),
        "labels": raw.get("labels") or [],
        "state": string(raw, "state").upper() or "OPEN",
        "createdAt": string(raw, "created_at"),
        "updatedAt": string(raw, "updated_at"),
    }


def explain(board: Board, row: Obj, *, blocker: bool | None) -> runtime.Explanation:
    number = row.get("number")
    assert isinstance(number, int)
    issues = (
        board.issues
        if any(r.get("number") == number for r in board.issues)
        else [
            *board.issues,
            row,
        ]
    )
    return runtime.explain_issue(
        number,
        row,
        issues=list(issues),
        prs=list(board.prs),
        intake=board.admitted(),
        open_blocker=blocker,
        pr_wip_limit=board.pr_wip_limit,
        review_backlog_limit=board.review_backlog_limit,
        incidents=board.incidents,
    )


# Verdicts in operator words ------------------------------------------------------


@dataclass(frozen=True)
class Reason:
    """One thing standing between an issue and a worker.

    ``code`` is stable (see ``CATEGORY_WORDS``) for front ends to switch on;
    ``text`` is the default English sentence. ``issues``/``prs`` name the
    items involved and ``setting`` the factory setting that governs it.
    """

    code: str
    text: str
    issues: tuple[int, ...] = ()
    prs: tuple[int, ...] = ()
    setting: str | None = None


@dataclass(frozen=True)
class Verdict:
    """One issue's answer: can it start, and if not, what stops it."""

    status: str  # ready | waiting | not-eligible
    reasons: tuple[Reason, ...]

    @property
    def category(self) -> str:
        """The first reason's code, for grouping; ``ready`` when nothing stops it."""
        return self.reasons[0].code if self.reasons else "ready"


def verdict(
    explanation: runtime.Explanation,
    board: Board,
    *,
    blocker_detail: str = "",
) -> Verdict:
    lines: list[Reason] = []
    for reason in explanation.reasons:
        lines.extend(_reason_lines(reason, explanation, board, blocker_detail))
    if explanation.intake == "not-authorized":
        # Intake restates most static reasons; add only what it knows beyond them.
        lines.extend(_intake_lines(explanation.issue, board, {item.code for item in lines}))
    if lines and any(item.code != "backpressure" for item in lines):
        return Verdict("not-eligible", tuple(lines))
    # Only pacing stands in the way: name the limit and its numbers.
    held = _limit_lines(explanation) or lines
    if held:
        return Verdict("waiting", tuple(held))
    return Verdict("ready", ())


def _reason_lines(
    reason: str, explanation: runtime.Explanation, board: Board, blocker_detail: str
) -> list[Reason]:
    if reason.startswith("eligible: "):
        return []
    if reason.endswith(", not open"):
        return [Reason("closed", "It is closed.")]
    if reason.endswith("non-executable product exclusion"):
        if explanation.intake == "not-authorized":
            return []  # an intake refusal; explained by _intake_lines
        return [Reason("excluded", "It is on the factory's do-not-execute list.")]
    if reason.startswith("labeled epic/prd"):
        return [Reason("parent", "It is an epic or PRD (parent work), not a task.")]
    if reason.startswith("structured manual-only"):
        return [
            Reason(
                "manual-only",
                "Its description marks it manual-only. The factory never overrides that, "
                "even with --force.",
            )
        ]
    if reason.startswith("body declares a product-acceptance parent"):
        return [
            Reason("parent", "It is a product-acceptance parent; its child issues are the work.")
        ]
    if reason.startswith("blocked labels present: "):
        labels = reason.removeprefix("blocked labels present: ")
        return [Reason("blocked", f"It is labeled {labels}.")]
    if reason.startswith("terminal label present: "):
        labels = reason.removeprefix("terminal label present: ")
        return [Reason("finished", f"It is already marked finished ({labels}).")]
    if reason.startswith("active factory lease held by "):
        owner = reason.removeprefix("active factory lease held by ")
        worker = owner.removeprefix("factory:")
        if worker == "local":
            who = "A local worker"
        else:
            provider = runtime.provider_for_worker(worker)
            who = f"Worker {worker}" + (f" ({provider.name})" if provider else "")
        return [Reason("held", f"{who} is already on it.")]
    if reason.startswith("unresolved dependencies: "):
        waiting = reason.removeprefix("unresolved dependencies: ")
        numbers = tuple(int(n) for n in re.findall(r"#(\d+)", waiting))
        return [Reason("dependency", f"It depends on {waiting}, still open.", issues=numbers)]
    if reason == "an open canonical PR already owns this issue":
        prs = ", ".join(f"#{n}" for n in explanation.owning_prs) or "an open pull request"
        return [
            Reason(
                "has-pr",
                f"Pull request {prs} already implements it; work continues there.",
                prs=explanation.owning_prs,
            )
        ]
    if reason == "a live open dependency blocker is reported by GitHub":
        text = (blocker_detail or "GitHub lists an open blocking issue") + "."
        numbers = tuple(int(n) for n in re.findall(r"#(\d+)", blocker_detail))
        return [Reason("dependency", text, issues=numbers)]
    return [Reason("other", reason)]


# Intake block codes already covered when the static rules gave one of these reasons.
INTAKE_RESTATES = {
    "human_boundary": {"manual-only", "blocked", "finished", "parent", "excluded"},
    "dependency_incomplete": {"dependency"},
    "implementation_exists": {"has-pr"},
    "active_lease": {"held"},
    "not_open": {"closed"},
    "parent_work": {"parent"},
    "blocked": {"blocked"},
    "terminal": {"finished"},
}


def _intake_lines(number: int, board: Board, known: set[str]) -> list[Reason]:
    if board.intake_problem:
        return [
            Reason("intake", f"Rotisserie intake admits nothing right now: {board.intake_problem}.")
        ]
    _, codes = (board.intake or {}).get(number, (False, ()))
    fresh = [code for code in codes if not INTAKE_RESTATES.get(code, set()) & known]
    if codes and not fresh:
        return []
    if not codes:
        return [] if known else [Reason("intake", "Rotisserie's intake decision did not admit it.")]
    category = "backpressure" if fresh == ["backpressure"] else "intake"
    words = "; ".join(INTAKE_WORDS.get(code, code.replace("_", " ")) for code in fresh)
    return [Reason(category, f"Rotisserie intake is holding it: {words}.")]


def _limit_lines(explanation: runtime.Explanation) -> list[Reason]:
    """The controller's backpressure gates, applied to an otherwise eligible issue."""
    held: list[Reason] = []
    if explanation.incident_pause:
        held.append(
            Reason(
                "incident",
                "An incident is open (baste incident). New intake of other "
                "issues pauses until the incident issue closes.",
                setting="FACTORY_INCIDENTS",
            )
        )
    if explanation.pr_wip >= explanation.pr_wip_limit and not explanation.bypasses_wip_limit:
        held.append(
            Reason(
                "wip-limit",
                f"{explanation.pr_wip} of {explanation.pr_wip_limit} factory pull requests "
                "already have a worker (FACTORY_PR_WIP_LIMIT). Only urgent work (main breakage, "
                "user-reported bugs, critical priority) starts now.",
                setting="FACTORY_PR_WIP_LIMIT",
            )
        )
    full = explanation.review_backlog >= explanation.review_backlog_limit
    if full and not explanation.bypasses_saturated_review_backlog and parked.full(explanation):
        held.append(Reason("parked-limit", parked.text(explanation)))
    elif full and not explanation.bypasses_saturated_review_backlog:
        held.append(
            Reason(
                "review-limit",
                f"{explanation.review_backlog} pull requests are waiting on review or checks "
                f"(limit {explanation.review_backlog_limit}, FACTORY_REVIEW_BACKLOG_LIMIT). "
                "Only main-breakage issues start new work until that drains.",
                setting="FACTORY_REVIEW_BACKLOG_LIMIT",
            )
        )
    return held


CATEGORY_WORDS = {
    "has-pr": "already have a pull request",
    "held": "are being worked on",
    "dependency": "wait on another issue",
    "blocked": "are labeled blocked",
    "manual-only": "are marked manual-only",
    "parent": "are epics or parent work",
    "finished": "are already finished",
    "excluded": "are on the do-not-execute list",
    "backpressure": "wait while open pull requests finish (intake backpressure)",
    "intake": "were held back by Rotisserie intake",
    "wip-limit": "wait for the open-PR limit",
    "review-limit": "wait for the review backlog to drain",
    "parked-limit": "wait for the owner to handle parked PRs (baste recap)",
    "incident": "wait for an open incident to end",
    "closed": "are closed",
    "other": "for other reasons",
}


def sweep(github: GitHub, board: Board) -> list[tuple[Obj, Verdict]]:
    """Every open issue's verdict; live dependency links are read for the eligible ones."""
    first = [(row, explain(board, row, blocker=None)) for row in board.issues]
    candidates = [(row, item) for row, item in first if item.eligible]

    def check(entry: tuple[Obj, runtime.Explanation]) -> tuple[Obj, Verdict]:
        row, _ = entry
        number = row.get("number")
        assert isinstance(number, int)
        blocked, detail = open_blocker(github, number)
        return row, verdict(explain(board, row, blocker=blocked), board, blocker_detail=detail)

    with ThreadPoolExecutor(max_workers=8) as pool:
        checked = list(pool.map(check, candidates))
    rest = [(row, verdict(item, board)) for row, item in first if not item.eligible]
    return checked + rest


# Entry points for front ends (CLI, TUI, web, MCP) ---------------------------------


@dataclass(frozen=True)
class IssueAnswer:
    """The full answer for one issue."""

    issue: int
    title: str
    verdict: Verdict
    explanation: runtime.Explanation
    intake_problem: str

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["status"] = self.verdict.status
        data["category"] = self.verdict.category
        return data


@dataclass(frozen=True)
class QueueEntry:
    issue: int
    title: str
    verdict: Verdict


@dataclass(frozen=True)
class QueueSummary:
    """Every open issue's verdict, plus the board it was computed from."""

    entries: tuple[QueueEntry, ...]
    board: Board

    @property
    def ready(self) -> tuple[QueueEntry, ...]:
        return tuple(
            sorted(
                (entry for entry in self.entries if entry.verdict.status == "ready"),
                key=lambda entry: entry.issue,
            )
        )

    @property
    def not_starting(self) -> Counter[str]:
        return Counter(
            entry.verdict.category for entry in self.entries if entry.verdict.status != "ready"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "open_issues": len(self.entries),
            "ready": [entry.issue for entry in self.ready],
            "not_starting": dict(self.not_starting),
            "issues": [asdict(entry) for entry in self.entries],
            "intake_problem": self.board.intake_problem or None,
        }


def _number(row: Obj) -> int:
    value = row.get("number")
    assert isinstance(value, int)
    return value


def answer_issue(
    github: GitHub, number: int, *, now: int, variables: Mapping[str, str]
) -> IssueAnswer:
    board = read_board(github, dict(variables), now)
    row = issue_row(github, board, number)
    blocked, detail = open_blocker(github, number)
    explanation = explain(board, row, blocker=blocked)
    return IssueAnswer(
        number,
        string(row, "title"),
        verdict(explanation, board, blocker_detail=detail),
        explanation,
        board.intake_problem,
    )


def summarize_queue(github: GitHub, *, now: int, variables: Mapping[str, str]) -> QueueSummary:
    board = read_board(github, dict(variables), now)
    entries = tuple(
        QueueEntry(_number(row), string(row, "title"), item) for row, item in sweep(github, board)
    )
    return QueueSummary(entries, board)
