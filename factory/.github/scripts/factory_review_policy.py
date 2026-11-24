"""Pure semantic review authorization policy for Rotisserie factories."""
from __future__ import annotations
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from factory_adopter import MARKER_PREFIX

TRUSTED_COMMENT_LOGIN = "github-actions[bot]"
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# Worker GitHub Apps must never be added here. factory_work_policy
# re-exports this as TRUSTED_FACTORY_APP_SLUGS.
TRUSTED_MARKER_APP_SLUGS = frozenset({"github-actions"})

REVIEW_MARKER_RE = re.compile(
    rf"^<!-- {MARKER_PREFIX}-factory-semantic-review-v1:"
    r"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{40}):"
    r"reviewer-(?P<reviewer>\d+|local-[a-z0-9-]{3,80}|external-[a-z0-9-]{3,80}):producer-(?P<producer>\d+|unknown):"
    r"verdict-(?P<verdict>approve|repair|reject|obsolete|duplicate|superseded|delivered) -->$"
)
BRANCH_PRODUCER_RE = re.compile(r"^factory/(?P<worker>\d+)-\d+-")
BODY_PRODUCER_RE = re.compile(
    r"(?m)^Worker:\s*opencode-(?:free-model|nvidia|omniroute)-factory-"
    r"(?P<worker>\d+)\s*$"
)
HEAD_CONTRIBUTOR_RE = re.compile(
    rf"^<!-- {MARKER_PREFIX}-factory-head-contributor-v1:"
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
    rf"^<!-- {MARKER_PREFIX}-agent-model-author-v1:"
    rf"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
    rf"model-(?P<model>{_MODEL_TOKEN}):"
    rf"agent-(?P<agent>{_MODEL_TOKEN}) -->$"
)
AGENT_MODEL_REVIEW_RE = re.compile(
    rf"^<!-- {MARKER_PREFIX}-agent-model-review-v1:"
    rf"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
    rf"author-(?P<author>{_MODEL_TOKEN}):"
    rf"reviewer-(?P<reviewer>{_MODEL_TOKEN}):"
    rf"author-agent-(?P<author_agent>{_MODEL_TOKEN}):"
    rf"reviewer-agent-(?P<reviewer_agent>{_MODEL_TOKEN}):"
    r"verdict-(?P<verdict>approve) -->$"
)
# A reviewer-model record written for every trusted approval whose executed
# model is known, whether or not the head's author is known. v1 records (above)
# carry the author too; both count as reviewer-model evidence.
AGENT_MODEL_REVIEW_V2_RE = re.compile(
    rf"^<!-- {MARKER_PREFIX}-agent-model-review-v2:"
    rf"pr-(?P<pr>\d+):head-(?P<head>[0-9a-f]{{40}}):"
    rf"reviewer-(?P<reviewer>{_MODEL_TOKEN}):"
    rf"reviewer-agent-(?P<reviewer_agent>{_MODEL_TOKEN}):"
    r"verdict-(?P<verdict>approve) -->$"
)
# Pricing-tier and routing-variant suffixes that name the same weights.
_MODEL_ALIAS_SUFFIXES = ("-free",)
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
        f"<!-- {MARKER_PREFIX}-factory-semantic-review-v1:"
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
        f"<!-- {MARKER_PREFIX}-factory-head-contributor-v1:"
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
    model_cleared: Iterable[str] = (),
) -> bool:
    """Return whether exact-head approvals include one independent approval.

    A worker recorded as a contributor (or the declared producer) is not
    independent by worker identity. The owner's rule is about models, though:
    a worker whose approval the controller recorded as executed by a model that
    authored none of this PR (``model_cleared``) is independent even if the
    same worker id contributed earlier with another model.

    ``provenance_complete`` is accepted for compatibility and reported by
    callers as a diagnostic. Missing provenance no longer demands a second
    approval: missing attribution is not grounds to block a valid review.
    Known self-review is enforced separately by
    :func:`evaluate_review_eligibility`.
    """
    del provenance_complete
    excluded = {str(worker) for worker in contributors}
    if producer is not None:
        excluded.add(str(producer))
    approving = {str(reviewer) for reviewer in approvers}
    cleared = {marker_model_token(str(agent)) for agent in model_cleared}
    eligible = (approving - excluded) | {
        reviewer for reviewer in approving if marker_model_token(reviewer) in cleared
    }
    return bool(eligible)


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
    model_cleared: Iterable[str] = (),
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
        model_cleared=model_cleared,
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


def model_record_token(model: str) -> str:
    """Marker token for an executed model that keeps its route boundary.

    ``/`` is outside the marker alphabet, and sanitizing it to ``-`` would make
    ``opencode-go/hy4-flash`` indistinguishable from a model named
    ``opencode-go-hy4-flash``. ``+`` is in the alphabet, so the route survives
    and :func:`canonical_model_identity` can strip it.
    """
    return marker_model_token(str(model).strip().replace("/", "+"))


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
    model_token = model_record_token(model)
    agent_token = marker_model_token(agent)
    return (
        f"<!-- {MARKER_PREFIX}-agent-model-author-v1:"
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
        f"<!-- {MARKER_PREFIX}-agent-model-review-v1:"
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

    ``None`` when the head has no marker or conflicting markers.
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
    approvers: Iterable[str] = (),
) -> bool:
    """Return whether the exact head has a review that is not known self-review.

    Thin boolean over :func:`evaluate_review_eligibility`; see it for the rule.
    """
    return evaluate_review_eligibility(
        comments, pr=pr, head=head, approvers=approvers
    ).eligible


def canonical_model_identity(value: str) -> str:
    """Canonical actual-model identity used to detect self-review.

    Provider, route, and pricing-tier spellings of the same weights collapse to
    one identity: ``opencode-go/hy4-flash-free``, ``hy4-flash`` and
    ``vendor/hy4-flash:free`` are one model. Effort and speed suffixes collapse
    too (see :func:`normalize_model_family`). Genuinely different models stay
    distinct even inside one family (``glm-4.6`` versus ``glm-4.5``).

    Args:
        value: A model id as executed, or a sanitized marker token.

    Returns:
        Lowercased canonical identity, empty when nothing remains.

    """
    raw = str(value or "").strip().lower()
    raw = re.split(r"[/+]", raw)[-1]
    raw = raw.split(":", 1)[0]
    try:
        token = normalize_model_family(raw)
    except ValueError:
        return ""
    changed = True
    while changed and token:
        changed = False
        for suffix in _MODEL_ALIAS_SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix):
                token = normalize_model_family(token[: -len(suffix)])
                changed = True
    return token


def _substantive_identity(token: str) -> bool:
    return token.count("-") >= 1 or any(char.isdigit() for char in token)


def same_actual_model(left: str, right: str) -> bool:
    """Whether two recorded model ids name the same actual model.

    Markers store a sanitized token, so ``opencode-go/hy4-flash`` was recorded
    as ``opencode-go-hy4-flash``: the provider prefix can no longer be split
    off structurally. A token that ends with the other identity at a ``-``
    boundary is therefore the same model behind a provider prefix. The shorter
    identity must be substantive (a version digit or two segments) so a bare
    word never matches unrelated models.

    Args:
        left: One recorded model id or marker token.
        right: The other.

    Returns:
        True when both name the same weights.

    """
    a, b = canonical_model_identity(left), canonical_model_identity(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return _substantive_identity(short) and long.endswith("-" + short)


def known_author_models(comments: Iterable[str], *, pr: int) -> list[str]:
    """Every model a controller author record names on any head of this PR, sorted.

    Informational since the 2026-11-24 rule; :func:`head_author_record` decides
    who may not review. Human attributions are omitted (they are not models).
    """
    models: set[str] = set()
    for body in comments:
        stripped = str(body or "").strip()
        if not stripped or "\n" in stripped:
            continue
        marker = parse_agent_model_author_marker(stripped)
        if not marker or int(marker["pr"]) != pr:
            continue
        if canonical_model_identity(marker["model"]) == HUMAN_AUTHOR_MODEL:
            continue
        models.add(marker["model"])
    return sorted(models)


def _models(found: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({m for m in found if canonical_model_identity(m) != HUMAN_AUTHOR_MODEL}))


def head_author_record(
    comments: Iterable[str],
    *,
    pr: int,
    heads: Sequence[str],
    trailers: Mapping[str, Iterable[str]] | None = None,
) -> tuple[str, tuple[str, ...], str]:
    """``(commit, models, source)``: the current head's authors, who may not review it.

    Owner rule (2026-11-24): only the current head's author is excluded, so an
    earlier head's author may review once someone else's commit is the head.
    ``heads`` is the current head, then the PR-side parent of each clean
    default-branch merge it sits on (a sync carries that parent's author). The
    first commit with evidence decides: every controller author marker for it
    (all of them when they disagree; ``human`` excludes no model), else its
    ``Model:`` trailers, else, when a worker recorded it but no model, every
    author on the PR (fail closed). ``("", (), "")``: no evidence, nobody excluded.
    """
    rows = [str(body or "").strip() for body in comments]
    for sha in heads:
        markers = {
            marker["model"]
            for body in rows
            if "\n" not in body
            and (marker := parse_agent_model_author_marker(body))
            and int(marker["pr"]) == pr
            and marker["head"] == sha
        }
        found = markers or set((trailers or {}).get(sha, ()))
        if found:
            return sha, _models(found), "marker" if found & markers else "trailer"
        if current_head_contributors(rows, pr=pr, head=sha):
            return sha, tuple(known_author_models(rows, pr=pr)), "unrecorded-worker"
    return "", (), ""


def reviewer_model_marker(*, pr: int, head: str, reviewer_model: str, reviewer_agent: str) -> str:
    """Build the controller record of the model that executed one approval.

    Args:
        pr: Pull request number.
        head: Exact reviewed commit SHA.
        reviewer_model: Model the trusted runtime actually invoked.
        reviewer_agent: Worker (or local lane) the controller launched.

    Returns:
        Entire-comment-body marker text.

    """
    return (
        f"<!-- {MARKER_PREFIX}-agent-model-review-v2:"
        f"pr-{pr}:head-{head}:reviewer-{model_record_token(reviewer_model)}:"
        f"reviewer-agent-{marker_model_token(reviewer_agent)}:verdict-approve -->"
    )


def head_reviewer_models(
    comments: Iterable[str], *, pr: int, head: str
) -> list[tuple[str, str]]:
    """``(reviewer model, reviewer agent)`` for each recorded approval of ``head``.

    Args:
        comments: Controller-authored comment bodies.
        pr: Pull request number.
        head: Exact commit SHA.

    Returns:
        Distinct records from v1 and v2 reviewer-model markers, in order seen.

    """
    seen: list[tuple[str, str]] = []
    for body in comments:
        stripped = str(body or "").strip()
        if not stripped or "\n" in stripped:
            continue
        marker = parse_agent_model_review_marker(stripped)
        if marker is None:
            match = AGENT_MODEL_REVIEW_V2_RE.fullmatch(stripped)
            marker = match.groupdict() if match else None
        if not marker or int(marker["pr"]) != pr or marker["head"] != head:
            continue
        record = (marker["reviewer"], marker_model_token(marker["reviewer_agent"]))
        if record not in seen:
            seen.append(record)
    return seen


ReviewEligibilityStatus = Literal[
    "eligible", "self-review", "reviewer-model-unrecorded", "no-approval"
]


@dataclass(frozen=True)
class ReviewEligibility:
    """Whether one exact head has an approval that is not known self-review.

    Attributes:
        eligible: An approval of the exact head may count toward merge.
        status: Stable machine reason.
        author_models: The current head's author models, excluded from approving it.
        qualifying: Reviewer models whose approval counts.
        self_reviews: Reviewer models that matched a known author model.
        cleared_agents: Reviewer agents whose approval counts by model, even if
            the same worker once contributed with another model.
        reason: Plain-English explanation.

    """

    eligible: bool
    status: ReviewEligibilityStatus
    author_models: tuple[str, ...]
    qualifying: tuple[str, ...]
    self_reviews: tuple[str, ...]
    cleared_agents: tuple[str, ...]
    reason: str


def evaluate_review_eligibility(
    comments: Iterable[str],
    *,
    pr: int,
    head: str,
    approvers: Iterable[str] = (),
    excluded: Sequence[str] | None = None,
) -> ReviewEligibility:
    """Apply the owner's review rule (2026-11-24) to one exact head.

    Only the current head's author models may not approve it; see
    :func:`head_author_record`. ``excluded`` replaces that lookup with an
    already resolved set (``factory_review_exclusion`` follows sync merges). The reviewer's model comes only from the
    controller's record of what the runtime executed. An approval by any other
    model counts, even in the same family. With no head author recorded, any
    trusted approval counts. A known self-review, or an approval whose model was
    not recorded while the head has an author, goes back to review.
    """
    rows = [str(body or "") for body in comments]
    approving = sorted({str(item) for item in approvers})
    found = head_author_record(rows, pr=pr, heads=(head,))[1] if excluded is None else excluded
    authors = list(found)
    records = head_reviewer_models(rows, pr=pr, head=head)
    qualifying = [
        (model, agent) for model, agent in records
        if not any(same_actual_model(model, author) for author in authors)
    ]
    self_reviews = sorted({model for model, _agent in records} - {m for m, _a in qualifying})
    authors_text = ", ".join(f"`{model}`" for model in authors)
    common = {
        "author_models": tuple(authors),
        "self_reviews": tuple(self_reviews),
    }
    if qualifying:
        models = sorted({model for model, _agent in qualifying})
        known = f"head author {authors_text}" if authors else "no recorded head author"
        return ReviewEligibility(
            eligible=True,
            status="eligible",
            qualifying=tuple(models),
            cleared_agents=tuple(sorted({agent for _model, agent in qualifying})),
            reason=f"Approved by {', '.join(f'`{m}`' for m in models)} ({known}).",
            **common,
        )
    if not authors and (records or approving):
        return ReviewEligibility(
            eligible=True,
            status="eligible",
            qualifying=tuple(sorted({model for model, _agent in records})),
            cleared_agents=(),
            reason=(
                "Approved; no author model is recorded for the current head "
                "(human, external, or unattributed), so any reviewer may approve."
            ),
            **common,
        )
    if self_reviews:
        return ReviewEligibility(
            eligible=False,
            status="self-review",
            qualifying=(),
            cleared_agents=(),
            reason=(
                f"Self-review: the approval came from {', '.join(f'`{m}`' for m in self_reviews)}, "
                f"which authored the current head ({authors_text}). "
                "A different model must review it."
            ),
            **common,
        )
    if approving:
        return ReviewEligibility(
            eligible=False,
            status="reviewer-model-unrecorded",
            qualifying=(),
            cleared_agents=(),
            reason=(
                f"The current head has author models ({authors_text}) but no record of which "
                "model executed the approval, so self-review cannot be ruled out. "
                "It needs a review whose model the runtime records."
            ),
            **common,
        )
    return ReviewEligibility(
        eligible=False,
        status="no-approval",
        qualifying=(),
        cleared_agents=(),
        reason="No approval of this exact head yet.",
        **common,
    )


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


# Review findings ------------------------------------------------------------

AUTHORITATIVE_DIFF_EVIDENCE = f"# {MARKER_PREFIX}-factory-authoritative-diff-evidence"

# Lines a review transcript carries that are not findings: the model
# narrating its next step ("Let me check the PR", "I'll start by reading"),
# empty tool results, and shell commands it ran (PR #5684 was returned to
# repair on a verdict made only of these, leaving repair nothing to fix).
NON_FINDING_LINE = re.compile(
    r"(?i)^(?:"
    r"(?:(?:now|first|next|then|ok(?:ay)?|great|good|alright)[,!.]?\s+)*"
    r"(?:i'll|i will|i'm going to|i am going to|i need to|let me|let's)\b"
    r"|\((?:no output|empty|no matches|none)\)$"
    r"|(?:gh|git|cargo|pnpm|npm|npx|cd|ls|cat|rg|grep|sed|awk|python3?|uv|make|bash|sh"
    r"|curl|jq|find|head|tail|wc|echo)\s"
    r")"
)


# A JSON array or object row, not "[P1] finding" prose.
JSON_LINE = re.compile(r'^(?:\[\s*[{\["\]]|"[^"]*"\s*:)')


def has_actionable_review_findings(excerpt: str) -> bool:
    """Require a prose finding, not diff/source material or terminal boilerplate."""
    semantic = excerpt.partition(AUTHORITATIVE_DIFF_EVIDENCE)[0]
    semantic = re.split(r"(?m)^diff --git ", semantic, maxsplit=1)[0]
    boilerplate = {
        "semantic blockers remain",
        "the pr is returning to repair",
        "semantic blockers remain. the pr is returning to repair",
        "repair required",
        "factory_gate_blocked",
        "factory_gate_not_ready",
        "factory_gate_ready",
        "factory_gate_reject",
    }
    in_code = False
    for raw in semantic.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line:
            continue
        # Indented tool output (git log bodies, commit trailers, file
        # listings) is never the model's diagnosis; nested list items are.
        if raw[:1] == "\t" or raw.startswith("    "):
            if not re.match(r"^(?:[-*]|\d+[.)])\s", line):
                continue
        # One whitespace-free token (a path, a SHA, a URL) is not a finding,
        # nor tabular CLI output, JSON, or a JSON-escaped string (several
        # literal "\n"; one may be a finding about escaping).
        if not re.search(r"\s", line) or "\t" in line or line.count("\\n") >= 2:
            continue
        if JSON_LINE.match(line):
            continue
        if NON_FINDING_LINE.match(re.sub(r"^(?:[-*]|\d+[.)])\s+", "", line)):
            continue
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@ ", "#", "//", "/*", "{", "}")):
            continue
        # OpenCode streams tool activity together with the model's final
        # explanation. A command transcript is evidence of inspection, not an
        # actionable diagnosis, even when its text contains English words.
        if line.startswith(("> ", "→ ", "$ ", "↳ ")):
            continue
        if re.match(r"^[0-9a-f]{7,40}\s", line):
            continue
        if re.match(r"^(?:commit |Author:|Date:|On branch |nothing to commit|working tree clean)", line, re.I):
            continue
        if re.match(r"^\S+\s+\|\s+\d+\s", line) or re.match(
            r"^\d+ files? changed(?:,|$)", line
        ):
            continue
        # Unified diff additions/deletions and standalone source expressions
        # are not a model diagnosis, even when they contain English words.
        if re.match(r"^[+-](?:[^ ]| {2,})", line):
            continue
        line = re.sub(r"^[-*]\s+", "", line).strip().strip("*` .")
        if line.casefold() in boilerplate or not line:
            continue
        if re.match(
            r"^(?:import|from|const|let|var|def|class|function|export|return|raise|assert|await|async|if|for|while)\b",
            line,
        ):
            continue
        if len(re.findall(r"[A-Za-z]{2,}", line)) >= 2:
            return True
    return False
