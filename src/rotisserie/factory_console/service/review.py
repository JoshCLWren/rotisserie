"""Why a pull request's review does or does not count, and safe recovery.

The owner's rule (2026-11-24): the model that authored the current head must
not approve it. A model that authored only an earlier head may. This service
explains, for one PR, who authored the current head (following a sync merge to
the commit it carries), which models approved that head, whether that is known
self-review or merely missing metadata,
and whether the PR is parked in ``factory:blocked`` by the old review gate,
which no longer blocks. ``requeue`` moves such a PR back to ``factory:review``
so a different model reviews it. It never invents authorship, never writes a
marker, and never touches a PR a worker holds.

Callers may pass the PR and comment payloads they already fetched, so a scan
over many PRs costs one list call plus one comment read per candidate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from types import ModuleType

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, items, label_names, obj, string

BLOCKED = "factory:blocked"
REVIEW = "factory:review"
UNOWNED = "factory:unowned"
# Notes the retired review-gate blocks left on a PR. Only these are released.
REVIEW_GATE_BLOCK_NOTES = (
    "cross-model author/reviewer provenance",
    "cross-model review gate",
    "same model family as the head's author",
)
HEADER = "### Factory semantic review"
FAIL_CLOSED = {
    "unrecorded-worker": "a factory worker pushed it without a model record",
    "unprovable-merge": "a merge under it could not be proven to be a clean sync",
    "sync-chain-too-long": "it sits on more than five syncs",
}


@dataclass(frozen=True)
class ReviewReport:
    pr: int
    head: str
    stage: str
    owner: str  # factory owner label, "" when none
    author_models: tuple[str, ...]  # trusted records on any head (informational)
    head_attributed: bool  # the current head has its own author record
    head_authors: tuple[str, ...]  # excluded from reviewing the current head
    head_author_commit: str  # commit whose record decided ("" when none)
    head_author_source: str  # marker | trailer | unrecorded-worker | ""
    approvers: tuple[str, ...]  # trusted approval markers on the current head
    reviewer_models: tuple[str, ...]  # executed models recorded for those approvals
    status: str  # eligible | self-review | reviewer-model-unrecorded | no-approval
    eligible: bool
    reason: str
    blocked_by_review_gate: bool
    block_note: str  # the controller note that parked it, if blocked
    block_head: str  # the head that note was written against ("" if unknown)
    actions: tuple[str, ...] = field(default=())
    self_review_allowed_by: str = ""  # person whose allow-self-review label lifts the rule

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Requeued:
    pr: int
    ok: bool
    text: str
    issue: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _policy() -> ModuleType:
    return runtime.module("factory_review_policy")


def _exclusion() -> ModuleType:
    return runtime.module("factory_review_exclusion")


@dataclass(frozen=True)
class Excluded:
    """The models that may not review a PR's current head (owner rule, 2026-11-24)."""

    pr: int
    head: str
    excluded: tuple[str, ...]
    author_commit: str  # commit whose record decided ("" when none)
    source: str
    reason: str


def excluded_models(
    github: GitHub,
    number: int,
    *,
    pull: Obj | None = None,
    records: Sequence[Obj] | None = None,
) -> Excluded:
    """The one shared answer to "which models may not review this PR", for any caller."""
    found = _exclusion().for_pull(github.repo_api, number, pull=pull, records=records)
    return Excluded(
        pr=number,
        head=str(found.head),
        excluded=tuple(str(model) for model in found.excluded),
        author_commit=str(found.author_commit),
        source=str(found.source),
        reason=str(found.describe()),
    )


def _pages(github: GitHub, path: str, *, limit: int = 30) -> list[Obj]:
    rows: list[Obj] = []
    join = "&" if "?" in path else "?"
    for page in range(1, limit + 1):
        batch = items(github.repo_api(f"{path}{join}per_page=100&page={page}"))
        rows += batch
        if len(batch) < 100:
            break
    return rows


def comment_records(github: GitHub, number: int) -> list[Obj]:
    """Every issue comment on the PR (100 per request)."""
    return _pages(github, f"issues/{number}/comments")


def _block_note(records: Sequence[Obj], policy: ModuleType, *, pr: int) -> tuple[str, str]:
    """``(note, head)`` of the most recent controller note that blocked this PR.

    Any head counts: a sync after a block moves the head but leaves the PR
    parked, so the newest blocking note is what keeps it there.
    """
    trusted_login = policy.TRUSTED_COMMENT_LOGIN
    for record in reversed(list(records)):
        if string(obj(record.get("user")), "login") != trusted_login:
            continue
        body = string(record, "body")
        if HEADER not in body or "Blocked" not in body:
            continue
        marker = policy.parse_review_marker(body.splitlines()[0] if body else "")
        if marker and int(marker["pr"]) != pr:
            continue
        note = body.split(HEADER, 1)[1].split("\n", 1)[-1].strip()
        return note.split("\n\n", 1)[0][:400], (marker["head"] if marker else "")
    return "", ""


def inspect(
    github: GitHub,
    number: int,
    *,
    pull: Obj | None = None,
    records: Sequence[Obj] | None = None,
) -> ReviewReport:
    """Explain the review gate for one PR from (optionally pre-fetched) GitHub state."""
    policy = _policy()
    raw = pull if pull is not None else obj(github.repo_api(f"pulls/{number}"))
    rows = list(records) if records is not None else comment_records(github, number)
    head = string(obj(raw.get("head")), "sha")
    labels = label_names(raw)
    # The factory's own stage rule, so every stage (and precedence) matches it.
    stage = runtime.module("factory_work_policy").stage_of(labels) or ""
    owner = next(
        (label for label in sorted(labels) if label.startswith("factory:") and _is_owner(label)),
        "",
    )
    model_bodies = policy.controller_comment_bodies(rows)
    trusted_bodies = policy.trusted_comment_bodies(rows)
    approvers = sorted(policy.current_head_approvers(trusted_bodies, pr=number, head=head))
    base = string(obj(raw.get("base")), "ref")
    exclusion = _exclusion().resolve(
        github.repo_api, pr=number, head=head, base_branch=base, comments=model_bodies, pull=raw
    )
    verdict = _exclusion().allowance.noted(
        policy.evaluate_review_eligibility(
            model_bodies, pr=number, head=head, approvers=approvers, excluded=exclusion.excluded
        ),
        exclusion.allowed_by,
    )
    reviewer_models = tuple(
        model for model, _agent in policy.head_reviewer_models(model_bodies, pr=number, head=head)
    )
    attributed = policy.current_head_author_identity(model_bodies, pr=number, head=head) is not None
    note, note_head = _block_note(rows, policy, pr=number) if stage == BLOCKED else ("", "")
    gate_block = stage == BLOCKED and any(text in note for text in REVIEW_GATE_BLOCK_NOTES)
    actions: list[str] = []
    if gate_block:
        actions.append(f"baste review pr:{number} --requeue (back to review for a different model)")
    elif stage == BLOCKED and note:
        actions.append("Blocked for another reason (not the review gate); see the note.")
    if not verdict.eligible and verdict.status == "self-review" and stage != BLOCKED:
        actions.append("Waiting for a different model to review; nothing to repair.")
    return ReviewReport(
        pr=number,
        head=head,
        stage=stage,
        owner=owner,
        author_models=tuple(exclusion.earlier_authors),
        head_attributed=attributed,
        head_authors=tuple(exclusion.excluded or exclusion.lifted),
        head_author_commit=exclusion.author_commit,
        head_author_source=exclusion.source,
        approvers=tuple(approvers),
        reviewer_models=reviewer_models,
        status=verdict.status,
        eligible=verdict.eligible,
        reason=verdict.reason,
        blocked_by_review_gate=gate_block,
        block_note=note,
        block_head=note_head,
        actions=tuple(actions),
        self_review_allowed_by=str(exclusion.allowed_by),
    )


def authorship_lines(report: ReviewReport) -> list[str]:
    """Who authored the current head, which models that excludes, and who may review."""
    names = ", ".join(report.head_authors)
    commit = report.head_author_commit[:10]
    if not report.head_author_commit:
        lines = ["Head author: none recorded (a human or outside push); no model is excluded."]
    elif not report.head_authors:
        lines = ["Head author: a human, or no model is recorded; no model is excluded."]
    elif report.head_author_source in FAIL_CLOSED:
        lines = [f"Head author: unknown ({FAIL_CLOSED[report.head_author_source]})."]
    elif report.head_author_commit != report.head:
        lines = [f"Head author: {names} (a sync carries {commit}, which {names} authored)"]
    else:
        how = "Model: trailer" if report.head_author_source == "trailer" else "controller record"
        lines = [f"Head author: {names} (its {how})"]
    if report.self_review_allowed_by:
        lines.append(f"self-review allowed by owner (label by {report.self_review_allowed_by})")
    elif report.head_authors:
        lines.append(f"Excluded from reviewing this head: {names}")
    same = _policy().same_actual_model
    earlier = [
        model
        for model in report.author_models
        if not any(same(model, author) for author in report.head_authors)
    ]
    if earlier:
        lines.append(f"Earlier-head authors, now eligible to review: {', '.join(earlier)}")
    return lines


def requeue(github: GitHub, report: ReviewReport) -> Requeued:
    """Move a PR the retired review gate blocked back to review (and its linked issue).

    Refuses anything else: a PR blocked for another reason, a PR a worker holds,
    or a head that moved since ``report`` was taken.
    """
    if not report.blocked_by_review_gate:
        return Requeued(
            report.pr, False, f"PR #{report.pr} is not blocked by the review gate; left alone."
        )
    if report.owner and report.owner != UNOWNED:
        return Requeued(report.pr, False, f"PR #{report.pr} is held by {report.owner}; left alone.")
    live = obj(github.repo_api(f"pulls/{report.pr}"))
    labels = label_names(live)
    if string(obj(live.get("head")), "sha") != report.head or BLOCKED not in labels:
        return Requeued(
            report.pr, False, f"PR #{report.pr} changed since it was inspected; re-run."
        )
    if any(
        label.startswith("factory:") and _is_owner(label) and label != UNOWNED for label in labels
    ):
        return Requeued(
            report.pr, False, f"PR #{report.pr} was just claimed by a worker; left alone."
        )
    _restage(github, report.pr, labels)
    issue = runtime.linked_issue(_row(live))
    moved_issue = None
    if issue is not None:
        issue_labels = label_names(obj(github.repo_api(f"issues/{issue}")))
        if BLOCKED in issue_labels and not any(
            label.startswith("factory:") and _is_owner(label) and label != UNOWNED
            for label in issue_labels
        ):
            _restage(github, issue, issue_labels)
            moved_issue = issue
    also = f" and issue #{moved_issue}" if moved_issue else ""
    return Requeued(
        report.pr, True, f"PR #{report.pr}{also} moved from blocked back to review.", moved_issue
    )


def open_pulls(github: GitHub) -> list[Obj]:
    """Every open pull request (100 per request)."""
    return _pages(github, "pulls?state=open")


def blocked_candidates(github: GitHub, *, pulls: Sequence[Obj] | None = None) -> list[Obj]:
    """Open factory PRs parked in ``factory:blocked``, from one list call."""
    rows = list(pulls) if pulls is not None else _pages(github, "pulls?state=open")
    return [row for row in rows if {"factory", BLOCKED} <= label_names(row)]


def _is_owner(label: str) -> bool:
    rest = label.removeprefix("factory:")
    return rest.isdigit() or rest in ("local", "unowned")


def _row(raw: Obj) -> dict[str, object]:
    return {
        "number": raw.get("number"),
        "title": string(raw, "title"),
        "body": string(raw, "body"),
        "headRefName": string(obj(raw.get("head")), "ref"),
        "labels": [{"name": name} for name in label_names(raw)],
        "state": string(raw, "state").upper(),
    }


def _restage(github: GitHub, number: int, labels: set[str]) -> None:
    github.repo_api(f"issues/{number}/labels", method="POST", fields={"labels[]": REVIEW})
    if UNOWNED not in labels:
        github.repo_api(f"issues/{number}/labels", method="POST", fields={"labels[]": UNOWNED})
    github.repo_api(f"issues/{number}/labels/{BLOCKED}", method="DELETE")
