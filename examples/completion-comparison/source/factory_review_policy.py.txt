"""Pure semantic review authorization policy for ComicPile factories."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

TRUSTED_COMMENT_LOGIN = "github-actions[bot]"
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# Worker GitHub Apps must never be added here. factory_work_policy
# re-exports this as TRUSTED_FACTORY_APP_SLUGS.
TRUSTED_MARKER_APP_SLUGS = frozenset({"github-actions"})

REVIEW_MARKER_RE = re.compile(
    r"^<!-- comic-pile-factory-semantic-review-v1:"
    r"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{40}):"
    r"reviewer-(?P<reviewer>\d+):producer-(?P<producer>\d+|unknown):"
    r"verdict-(?P<verdict>approve|repair|reject|obsolete|duplicate|superseded|delivered) -->$"
)
BRANCH_PRODUCER_RE = re.compile(r"^factory/(?P<worker>\d+)-\d+-")
BODY_PRODUCER_RE = re.compile(
    r"(?m)^Worker:\s*opencode-(?:free-model|nvidia|omniroute)-factory-"
    r"(?P<worker>\d+)\s*$"
)
HEAD_CONTRIBUTOR_RE = re.compile(
    r"^<!-- comic-pile-factory-head-contributor-v1:"
    r"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{40}):"
    r"worker-(?P<worker>\d+):epoch-(?P<epoch>\d+) -->$"
)


def classify_ci_reconciliation(
    *,
    checks_decision: str,
    authorized: bool,
    mechanical_decision: str | None = None,
) -> str:
    """Classify one exact-head CI-stage PR without invoking a worker.

    The caller supplies decisions from the authoritative GitHub reads and
    gate functions.  This pure boundary makes the lifecycle ordering explicit
    and keeps unknown state fail-closed.
    """
    if checks_decision == "retry":
        return "retry-ci"
    if checks_decision == "deny":
        return "repair-ci"
    if checks_decision != "pass":
        return "retry-ci"
    if not authorized:
        return "review"
    if mechanical_decision == "pass":
        return "ready"
    if mechanical_decision == "deny":
        return "changes-requested"
    return "retry-ci"


def producer_worker_from_pr(*, branch: str | None, body: str | None) -> str | None:
    """Recover durable producer identity without inventing historical provenance."""
    if branch:
        match = BRANCH_PRODUCER_RE.match(branch)
        if match:
            return match.group("worker")
    if body:
        match = BODY_PRODUCER_RE.search(body)
        if match:
            return match.group("worker")
    return None


def review_marker(
    *,
    pr: int,
    head: str,
    reviewer: str,
    producer: str | None,
    verdict: str,
) -> str:
    """Build one controller-authored semantic review marker."""
    if verdict not in {"approve", "repair", "reject", "obsolete", "duplicate", "superseded", "delivered"}:
        raise ValueError(f"unsupported verdict: {verdict}")
    producer_value = producer or "unknown"
    return (
        "<!-- comic-pile-factory-semantic-review-v1:"
        f"pr-{pr}:head-{head}:reviewer-{reviewer}:producer-{producer_value}:"
        f"verdict-{verdict} -->"
    )


def parse_review_marker(line: str) -> dict[str, str] | None:
    """Parse one exact semantic review marker line."""
    match = REVIEW_MARKER_RE.fullmatch(line.strip())
    return match.groupdict() if match else None


def parse_head_contributor_marker(line: str) -> dict[str, str] | None:
    """Parse one exact head contributor marker line."""
    match = HEAD_CONTRIBUTOR_RE.fullmatch(line.strip())
    return match.groupdict() if match else None


def performed_via_untrusted_app(comment: Mapping[str, Any]) -> bool:
    """Return whether a worker GitHub App must not author a trusted marker.

    A present App slug other than ``github-actions`` loses even when
    ``author_association`` is OWNER, MEMBER, or COLLABORATOR. Comments with
    no App stay on the login and association paths. Human owner comments are
    unchanged.
    """
    app = comment.get("performed_via_github_app")
    if not isinstance(app, Mapping):
        return False
    slug = app.get("slug")
    if not isinstance(slug, str) or not slug:
        return False
    return slug not in TRUSTED_MARKER_APP_SLUGS


def trusted_comment_bodies(comments: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return comment bodies written by the actors trusted to forge no marker.

    Factory workers post through ``GITHUB_TOKEN`` as ``github-actions[bot]``, and
    the repository owner may run the review controller during incidents, so an
    owner/member/collaborator body is honored alongside it. Worker GitHub Apps
    are not those actors: a non-github-actions App slug is ignored even if the
    association looks trusted. Every consumer of review provenance must apply
    this identical filter; if the controller, the Rotisserie capture, and the
    dispatcher disagree about who authored a marker they can disagree about
    who authored a head.
    """
    bodies: list[str] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        if performed_via_untrusted_app(comment):
            continue
        user = comment.get("user")
        if not isinstance(user, Mapping):
            continue
        login = str(user.get("login") or "")
        association = str(comment.get("author_association") or "")
        if login != TRUSTED_COMMENT_LOGIN and association not in TRUSTED_COMMENT_ASSOCIATIONS:
            continue
        bodies.append(str(comment.get("body") or ""))
    return bodies


def head_contributor_marker(
    *,
    pr: int,
    head: str,
    worker: str,
    epoch: int,
) -> str:
    """Build one controller-authored head contributor marker.

    The controller writes this marker as the entire comment body. Requiring a
    whole-body match keeps worker-authored resume packets, review prose, PR
    body text, and commit messages from ever reading as trusted provenance.
    """
    return (
        "<!-- comic-pile-factory-head-contributor-v1:"
        f"pr-{pr}:head-{head}:worker-{worker}:epoch-{epoch} -->"
    )


def recorded_pr_contributors(comments: Iterable[str], *, pr: int) -> set[str]:
    """Return every worker with a trusted contribution record on one pull request.

    Only a comment whose whole body is one contributor marker is honored, so a
    worker cannot forge provenance by pasting a marker into a resume packet or
    a review comment. Every marker for one PR names a head of that PR's branch,
    and factory repairs build on the previous head instead of rewriting history,
    so a worker recorded on any head of the branch authored commits that are
    still reachable from every later head of that branch.
    """
    workers: set[str] = set()
    for body in comments:
        stripped = str(body or "").strip()
        if not stripped or "\n" in stripped:
            continue
        marker = parse_head_contributor_marker(stripped)
        if not marker or int(marker["pr"]) != pr:
            continue
        workers.add(marker["worker"])
    return workers


def current_head_contributors(
    comments: Iterable[str],
    *,
    pr: int,
    head: str,
) -> set[str]:
    """Return distinct factory workers recorded against one exact PR head.

    This is the marker set for the head itself, which is what
    ``provenance_complete`` is derived from: a head whose own record is absent
    is a head whose history is not accounted for, so review fails closed.
    Eligibility to review that head additionally excludes the whole recorded
    lineage; see :func:`head_contributor_provenance`.
    """
    contributors: set[str] = set()
    for body in comments:
        stripped = str(body or "").strip()
        if not stripped or "\n" in stripped:
            continue
        marker = parse_head_contributor_marker(stripped)
        if not marker:
            continue
        if int(marker["pr"]) != pr or marker["head"] != head:
            continue
        contributors.add(marker["worker"])
    return contributors


def head_contributor_provenance(
    comments: Iterable[str],
    *,
    pr: int,
    head: str,
    producer: str | None = None,
) -> tuple[set[str], bool]:
    """Return the trusted contributor set and whether provenance was recorded.

    The excluded set is every worker with a trusted record anywhere on this
    pull request plus the declared branch/body producer, because a repair
    stacks commits onto the previous head rather than replacing it. Excluding
    only the workers recorded against ``head`` would let an earlier repairer
    approve a later head that still carries its own commits, which is the #2846
    defect one repair cycle later. ``provenance_complete`` reports whether a
    controller-authored marker exists for this exact head; when one does not,
    review independence must fail closed instead of assuming the apparent
    reviewer never touched the head.
    """
    exact = current_head_contributors(comments, pr=pr, head=head)
    contributors = recorded_pr_contributors(comments, pr=pr)
    if producer is not None:
        contributors.add(producer)
    return contributors, bool(exact)


def semantic_repair_heads(
    comments: Iterable[str],
    *,
    pr: int,
) -> set[str]:
    """Return distinct PR heads that received an authoritative repair verdict."""
    heads: set[str] = set()
    for body in comments:
        first_line = str(body or "").splitlines()[0] if body else ""
        marker = parse_review_marker(first_line)
        if not marker or int(marker["pr"]) != pr:
            continue
        if marker["verdict"] == "repair":
            heads.add(marker["head"])
    return heads


def semantic_terminal_heads(
    comments: Iterable[str],
    *,
    pr: int,
) -> set[str]:
    """Return distinct PR heads that received an authoritative terminal verdict."""
    heads: set[str] = set()
    for body in comments:
        first_line = str(body or "").splitlines()[0] if body else ""
        marker = parse_review_marker(first_line)
        if not marker or int(marker["pr"]) != pr:
            continue
        if marker["verdict"] in {"obsolete", "duplicate", "superseded", "delivered"}:
            heads.add(marker["head"])
    return heads


def current_head_approvers(
    comments: Iterable[str],
    *,
    pr: int,
    head: str,
) -> set[str]:
    """Return distinct approving reviewers attested for one exact PR head."""
    reviewers: set[str] = set()
    for body in comments:
        first_line = str(body or "").splitlines()[0] if body else ""
        marker = parse_review_marker(first_line)
        if not marker:
            continue
        if int(marker["pr"]) != pr or marker["head"] != head:
            continue
        if marker["verdict"] == "approve":
            reviewers.add(marker["reviewer"])
    return reviewers


def head_has_authorized_approval(
    *,
    approvers: Iterable[str],
    contributors: Iterable[str] = (),
    producer: str | None = None,
    provenance_complete: bool = False,
) -> bool:
    """Return whether exact-head approvals satisfy worker-independent review policy.

    A reviewer is eligible only if they are absent from the trusted contributor
    set for the exact head being reviewed. This is a factory-identity boundary,
    not a model or provider boundary: OmniRoute may route both workers through
    the same upstream model.

    ``provenance_complete`` reports whether controller-authored contributor
    markers exist for this head. When they do, one eligible reviewer authorizes
    the head. When they do not, the head is fail-closed: two distinct eligible
    reviewers are required, because a missing or incomplete record must never
    be read as proof that the apparent reviewer never authored the head.
    """
    excluded = {str(worker) for worker in contributors}
    if producer is not None:
        excluded.add(str(producer))
    eligible = {str(reviewer) for reviewer in approvers} - excluded
    if not eligible:
        return False
    if provenance_complete:
        return True
    return len(eligible) >= 2


def approval_can_promote(
    *,
    reviewer: str,
    reviewed_head: str,
    current_head: str,
    verdict: str,
    mechanical_gates_passed: bool,
    contributors: Iterable[str] = (),
    prior_approvers: Iterable[str] = (),
    producer: str | None = None,
    provenance_complete: bool = False,
) -> bool:
    """Apply the controller-side semantic promotion trust boundary."""
    if verdict != "approve":
        return False
    if reviewed_head != current_head:
        return False
    if not mechanical_gates_passed:
        return False
    return head_has_authorized_approval(
        contributors=contributors,
        approvers={*prior_approvers, reviewer},
        producer=producer,
        provenance_complete=provenance_complete,
    )
