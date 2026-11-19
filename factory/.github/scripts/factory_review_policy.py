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
    r"reviewer-(?P<reviewer>\d+|local-[a-z0-9-]{3,80}):producer-(?P<producer>\d+|unknown):"
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
_MODEL_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._+-]*"
# Effort and speed suffixes that do not change model family.
_MODEL_FAMILY_SUFFIXES = (
    "-fast",
    "-thinking",
    "-none",
    "-minimal",
    "-low",
    "-medium",
    "-high",
    "-xhigh",
    "-max",
)
AGENT_MODEL_AUTHOR_RE = re.compile(
    r"^<!-- comic-pile-agent-model-author-v1:"
    rf"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
    rf"model-(?P<model>{_MODEL_TOKEN}):"
    rf"agent-(?P<agent>{_MODEL_TOKEN}) -->$"
)
AGENT_MODEL_REVIEW_RE = re.compile(
    r"^<!-- comic-pile-agent-model-review-v1:"
    rf"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
    rf"author-(?P<author>{_MODEL_TOKEN}):"
    rf"reviewer-(?P<reviewer>{_MODEL_TOKEN}):"
    rf"author-agent-(?P<author_agent>{_MODEL_TOKEN}):"
    rf"reviewer-agent-(?P<reviewer_agent>{_MODEL_TOKEN}):"
    r"verdict-(?P<verdict>approve) -->$"
)
CURSOR_AGENT_ID_RE = re.compile(r"\bbc-(?P<id>[0-9a-f]{8,})\b", re.IGNORECASE)
MODEL_TRAILER_RE = re.compile(r"(?m)^Model:[ \t]*(\S+)[ \t]*$")
# The single push identity every workflow step and numbered-worker wrapper
# shares. A head whose commit is attributed here was advanced by trusted
# automation rather than a foreign account.
TRUSTED_AUTOMATION_LOGINS = frozenset({"github-actions[bot]"})
# Documented fallback when an owner-authored head carries no ``Model:``
# trailer: attribute the head to the human, never to a guessed model.
HUMAN_AUTHOR_MODEL = "human"


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


def controller_comment_bodies(comments: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return comment bodies posted by ``github-actions[bot]`` only.

    Author and reviewer model markers must not be trusted from OWNER, MEMBER,
    or COLLABORATOR accounts. Cursor cloud agents act as those identities and
    can spoof ``author-<x>:reviewer-<y>`` strings. The review controller writes
    model provenance through ``GITHUB_TOKEN`` as ``github-actions[bot]`` after
    it records a contribution or interprets a factory review it launched.
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
        if login != TRUSTED_COMMENT_LOGIN:
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
    controller-authored contributor marker exists for this exact head; when one
    does not, review independence must fail closed instead of assuming the
    apparent reviewer never touched the head. A caller that also holds a
    controller-authored exact-head author attestation composes that in; see
    :func:`factory-review-controller.exact_head_provenance`.
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


def marker_model_token(model: str) -> str:
    """Sanitize a roster or Cursor model id for the marker token alphabet.

    Args:
        model: Raw model id, which may contain ``/`` or ``:``.

    Returns:
        Marker-safe token.

    Raises:
        ValueError: If the model collapses to an empty token.

    """
    cleaned = re.sub(r"[^A-Za-z0-9._+-]+", "-", model.strip())
    cleaned = cleaned.strip("-_+.")
    if not cleaned:
        raise ValueError("model token is empty")
    return cleaned


def agent_model_author_marker(*, pr: int, head: str, model: str, agent: str) -> str:
    """Build one controller-trusted author-model marker.

    Args:
        pr: Pull request number.
        head: Exact commit SHA being attributed.
        model: Factory roster or Cursor model that authored the head.
        agent: Distinct author identity (factory worker id or Cursor ``bc-`` id).

    Returns:
        Entire-comment-body marker text.

    """
    model_token = marker_model_token(model)
    agent_token = marker_model_token(agent)
    return (
        "<!-- comic-pile-agent-model-author-v1:"
        f"pr-{pr}:head-{head}:model-{model_token}:agent-{agent_token} -->"
    )


def parse_agent_model_author_marker(line: str) -> dict[str, str] | None:
    """Parse one exact author-model marker line.

    Args:
        line: Comment body or first line.

    Returns:
        Named groups when the line is a trusted author-model marker.

    """
    match = AGENT_MODEL_AUTHOR_RE.fullmatch(line.strip())
    return match.groupdict() if match else None


def agent_model_review_marker(
    *,
    pr: int,
    head: str,
    author_model: str,
    reviewer_model: str,
    author_agent: str,
    reviewer_agent: str,
) -> str:
    """Build one controller-trusted cross-model review marker.

    Args:
        pr: Pull request number.
        head: Exact commit SHA that was reviewed.
        author_model: Model that authored the head.
        reviewer_model: Model that reviewed the head.
        author_agent: Distinct author identity bound to the author marker.
        reviewer_agent: Distinct reviewer identity the controller launched.

    Returns:
        Entire-comment-body marker text.

    """
    return (
        "<!-- comic-pile-agent-model-review-v1:"
        f"pr-{pr}:head-{head}:author-{marker_model_token(author_model)}:"
        f"reviewer-{marker_model_token(reviewer_model)}:"
        f"author-agent-{marker_model_token(author_agent)}:"
        f"reviewer-agent-{marker_model_token(reviewer_agent)}:"
        "verdict-approve -->"
    )


def parse_agent_model_review_marker(line: str) -> dict[str, str] | None:
    """Parse one exact cross-model review marker line.

    Args:
        line: Comment first line.

    Returns:
        Named groups when the line is a trusted approve review marker.

    """
    match = AGENT_MODEL_REVIEW_RE.fullmatch(line.strip())
    return match.groupdict() if match else None


def normalize_model_family(value: str) -> str:
    """Canonicalize a recorded model id to its family for equality checks.

    Strips a leading ``cursor-`` prefix and trailing effort or speed suffixes
    (``none``, ``minimal``, ``low``, ``medium``, ``high``, ``xhigh``, ``max``,
    ``thinking``, ``fast``) so ``cursor-grok-4.6-high-fast`` and
    ``cursor-grok-4.6-low`` compare as the same model.

    Args:
        value: Raw model token from a marker or roster pin.

    Returns:
        Lowercased model-family token.

    """
    token = marker_model_token(value).lower()
    if token.startswith("cursor-"):
        token = token[len("cursor-") :]
    changed = True
    while changed and token:
        changed = False
        for suffix in _MODEL_FAMILY_SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix):
                token = token[: -len(suffix)]
                changed = True
    return token


def _normalize_model(value: str) -> str:
    """Canonicalize a recorded model id for equality checks.

    Args:
        value: Raw model token from a marker.

    Returns:
        Model-family token.

    """
    return normalize_model_family(value)


def cursor_agent_ids_from_text(text: str | None) -> set[str]:
    """Return distinct Cursor ``bc-`` agent ids found in PR or comment text.

    Args:
        text: PR body, review footer, or other trusted Cursor metadata text.

    Returns:
        Lowercased ``bc-`` ids. Conflicting ids are the caller's problem: a
        unique id can attribute a head, more than one cannot.

    """
    if not text:
        return set()
    return {f"bc-{match.group('id').lower()}" for match in CURSOR_AGENT_ID_RE.finditer(text)}


def current_head_author_identity(
    comments: Iterable[str],
    *,
    pr: int,
    head: str,
) -> tuple[str, str] | None:
    """Return the unique author model family and agent for one exact PR head.

    Missing markers and conflicting markers both fail closed.

    Args:
        comments: Controller-authored comment bodies.
        pr: Pull request number.
        head: Exact commit SHA.

    Returns:
        ``(model_family, agent)`` when exactly one author identity is recorded,
        otherwise ``None``.

    """
    identities: set[tuple[str, str]] = set()
    for body in comments:
        stripped = str(body or "").strip()
        if not stripped or "\n" in stripped:
            continue
        marker = parse_agent_model_author_marker(stripped)
        if not marker or int(marker["pr"]) != pr or marker["head"] != head:
            continue
        identities.add(
            (normalize_model_family(marker["model"]), marker_model_token(marker["agent"]))
        )
    if len(identities) != 1:
        return None
    return next(iter(identities))


def current_head_author_model(
    comments: Iterable[str],
    *,
    pr: int,
    head: str,
) -> str | None:
    """Return the author model recorded against one exact PR head.

    Only a comment whose whole body is one author-model marker is honored.
    Missing or conflicting markers return ``None`` (fail closed).

    Args:
        comments: Controller-authored comment bodies.
        pr: Pull request number.
        head: Exact commit SHA.

    Returns:
        Normalized author model family, or ``None`` when none is recorded
        or the record is ambiguous.

    """
    identity = current_head_author_identity(comments, pr=pr, head=head)
    if identity is None:
        return None
    return identity[0]


def head_has_cross_model_review(
    comments: Iterable[str],
    *,
    pr: int,
    head: str,
) -> bool:
    """Return whether an exact head has a different-model independent review.

    Fail closed: the head must have exactly one controller-authored author
    marker. A review counts only when it is recorded against ``head``, names
    both models, those model families differ, the review's author model and
    author agent match the recorded author identity, and the reviewer agent is
    distinct from the author agent. A stale review bound to another SHA does
    not authorize the current head.

    Args:
        comments: Controller-authored comment bodies.
        pr: Pull request number.
        head: Exact commit SHA being merged.

    Returns:
        True when a qualifying cross-model approve review exists.

    """
    recorded = current_head_author_identity(comments, pr=pr, head=head)
    if recorded is None:
        return False
    recorded_author, recorded_agent = recorded
    for body in comments:
        stripped = str(body or "").strip()
        if not stripped or "\n" in stripped:
            continue
        marker = parse_agent_model_review_marker(stripped)
        if not marker:
            continue
        if int(marker["pr"]) != pr or marker["head"] != head:
            continue
        author = normalize_model_family(marker["author"])
        reviewer = normalize_model_family(marker["reviewer"])
        author_agent = marker_model_token(marker["author_agent"])
        reviewer_agent = marker_model_token(marker["reviewer_agent"])
        if not author or not reviewer or author == reviewer:
            continue
        if author != recorded_author or author_agent != recorded_agent:
            continue
        if reviewer_agent == recorded_agent:
            continue
        return True
    return False


def model_trailer_models(message: str | None) -> set[str]:
    """Return the distinct ``Model:`` trailer values in one commit message.

    A commit may legitimately carry zero or one trailer. More than one distinct
    value is ambiguous authorship and must fail closed rather than pick one.

    Args:
        message: Raw commit message text.

    Returns:
        Distinct trailer values, empty when none are present.

    """
    if not message:
        return set()
    return set(MODEL_TRAILER_RE.findall(message))


def interactive_agent_token(owner_login: str) -> str:
    """Return the marker agent token for one owner-attested interactive head.

    Args:
        owner_login: Repository owner login that opened the reconciled PR.

    Returns:
        Marker-safe agent token shared by every owner-attested head.

    """
    return marker_model_token(f"interactive-{owner_login}")


def owner_attested_head_identity(
    *,
    pr_author_login: str | None,
    owner_login: str,
    commit_author_login: str | None,
    commit_committer_login: str | None,
    commit_message: str | None,
    trusted_logins: Iterable[str] = TRUSTED_AUTOMATION_LOGINS,
) -> tuple[str, str] | None:
    """Attribute a wrapper-less head from owner PR authorship and commit evidence.

    This is the explicit reconciliation rule for heads no numbered wrapper
    recorded: a merge or rebase the owner pushed from a local worktree, or a
    head advanced by trusted repository automation. Trust is anchored on the
    pull request's verified GitHub author, which a worker cannot change, and the
    head's own commit evidence:

    - the PR author must be the repository owner;
    - every commit identity GitHub reports must be the owner or trusted
      automation, so a foreign account never inherits owner attribution;
    - the author model is the head's single ``Model:`` trailer, or ``human``
      when the trailer is absent (the documented fallback);
    - conflicting, ambiguous, or forged identities fail closed with ``None``.

    Args:
        pr_author_login: Verified GitHub author of the pull request.
        owner_login: Repository owner login.
        commit_author_login: GitHub login attributed to the head commit author.
        commit_committer_login: GitHub login attributed to the head committer.
        commit_message: Full head commit message, which may carry a trailer.
        trusted_logins: Push identities trusted for owner-authored heads.

    Returns:
        ``(model, agent)`` for the exact head, or ``None`` when provenance
        cannot be established without fabrication.

    """
    if not pr_author_login or str(pr_author_login) != owner_login:
        return None
    permitted = {owner_login, *trusted_logins}
    for identity in (commit_author_login, commit_committer_login):
        if identity and identity not in permitted:
            return None
    models = model_trailer_models(commit_message)
    if len(models) > 1:
        return None
    model = next(iter(models)) if models else HUMAN_AUTHOR_MODEL
    return normalize_model_family(model), interactive_agent_token(owner_login)
