#!/usr/bin/env python3
"""Controller-owned semantic review authorization and mechanical merge gates."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal, TypedDict, cast

sys.path.insert(0, os.path.dirname(__file__))
issue_acceptance = importlib.import_module("factory_issue_acceptance")
implementation_delivery = importlib.import_module("factory_implementation_delivery")
# Reuse the post-merge closure selector so the merge-side dependency check
# always inspects the exact issue the merge will deliver and close.
_post_merge_closure = importlib.import_module("factory_post_merge_closure")
_eligibility_dir = Path(__file__).resolve().parent
if not (_eligibility_dir / "factory_eligibility.py").is_file():
    _eligibility_dir = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_eligibility_dir))
eligibility = importlib.import_module("factory_eligibility")
_review_policy = importlib.import_module("factory_review_policy")
approval_can_promote = _review_policy.approval_can_promote
current_head_approvers = _review_policy.current_head_approvers
current_head_contributors = _review_policy.current_head_contributors
classify_ci_reconciliation = _review_policy.classify_ci_reconciliation
head_contributor_marker = _review_policy.head_contributor_marker
head_has_authorized_approval = _review_policy.head_has_authorized_approval
head_contributor_provenance = _review_policy.head_contributor_provenance
head_has_cross_model_review = _review_policy.head_has_cross_model_review
agent_model_author_marker = _review_policy.agent_model_author_marker
agent_model_review_marker = _review_policy.agent_model_review_marker
controller_comment_bodies = _review_policy.controller_comment_bodies
current_head_author_identity = _review_policy.current_head_author_identity
marker_model_token = _review_policy.marker_model_token
normalize_model_family = _review_policy.normalize_model_family
parse_review_marker = _review_policy.parse_review_marker
producer_worker_from_pr = _review_policy.producer_worker_from_pr
owner_attested_head_identity = _review_policy.owner_attested_head_identity
review_marker = _review_policy.review_marker
semantic_repair_heads = _review_policy.semantic_repair_heads
trusted_comment_bodies = _review_policy.trusted_comment_bodies

REPO = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
OWNER_RE = re.compile(r"^factory:(?:unowned|local|[1-9][0-9]*)$")
FIXED_WORKER_RE = re.compile(r"^(?:[6-9]|[1-9][0-9]+)$")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|DATABASE_URL|REDIS_URL|POSTGRES_URL)[\"']?\s*[=:]\s*[\"']?)([^\"'\s,}]+)"
)
BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+")
GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")
API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
# Accept authoritative PR-diff commands, common review inspection paths, and
# unified-diff hunks. Workers append `gh pr diff` evidence before invoking this
# controller; the excerpt is a 7000-char tail, so matching `diff --git` keeps
# honest approvals from soft-failing when only the dumped hunks remain in-window.
DIFF_INSPECTION_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"gh (?:pr|api)[^\n]{0,80}\bdiff\b"
    r"|git (?:diff|show|log -p)"
    r"|gh pr view\b"
    r"|gh api[^\n]{0,100}/pulls/\d+/(?:files|commits)\b"
    r"|comic-pile-factory-authoritative-diff-evidence"
    r"|diff --git "
    r"|\+\+\+ b/"
    r")"
)
STAGE_LABELS = {
    "factory:building",
    "factory:review",
    "factory:changes-requested",
    "factory:ci",
    "factory:ready",
    "factory:blocked",
}
GH_TIMEOUT_SECONDS = 120
# A worker records the commit its own push just created, so the authoritative PR
# head can still report the previous SHA for a moment. Retry that read a bounded
# number of times before abandoning the contributor record.
CONTRIBUTION_HEAD_SYNC_ATTEMPTS = 4
CONTRIBUTION_HEAD_SYNC_INTERVAL_SECONDS = 1.0
MERGEABLE_POLL_ATTEMPTS = 10
MERGEABLE_POLL_INTERVAL_SECONDS = 2.0
PASSING_CHECK_STATES = {"SUCCESS", "SKIPPED", "NEUTRAL"}
RETRY_CHECK_STATES = {
    "ACTION_REQUIRED",
    "EXPECTED",
    "IN_PROGRESS",
    "PENDING",
    "QUEUED",
    "REQUESTED",
    "WAITING",
}
FAILING_CHECK_STATES = {
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
NO_REQUIRED_CHECKS_RE = re.compile(r"no checks reported|no required checks", re.IGNORECASE)
NO_REQUIRED_CHECKS_REASON = (
    "no required checks reported at this head; merge is blocked until CI runs and passes"
)
FACTORY_REPAIR_CYCLE_LIMIT = 3
STRIKE_RESET_MARKER = "comic-pile-factory-strike-reset-v1"
DEPENDENCY_BLOCK_MARKER_PREFIX = "comic-pile-factory-dependency-block-v1"

GateDecision = Literal["pass", "retry", "deny"]


class GateResult(TypedDict):
    decision: GateDecision
    reason: str


def gate_result(decision: GateDecision, reason: str) -> GateResult:
    return {"decision": decision, "reason": reason}


def run_gh(
    args: list[str],
    *,
    input_json: object | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded GitHub CLI command."""
    command = ["gh", *args]
    try:
        proc = subprocess.run(
            command,
            input=None if input_json is None else json.dumps(input_json),
            text=True,
            capture_output=True,
            check=False,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out") from exc
    if check and proc.returncode:
        raise RuntimeError(f"{' '.join(command)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def gh_json(args: list[str], *, input_json: object | None = None) -> object | None:
    """Run GitHub CLI and decode JSON stdout.

    Returns untyped ``object`` because GitHub payloads are arbitrary JSON.
    Every consumer must narrow through :func:`json_object` / :func:`json_array`
    rather than casting blindly - those helpers document the one invariant
    that makes the narrowing sound (RFC 8259 objects are str-keyed) in a
    single place.
    """
    output = run_gh(args, input_json=input_json).stdout
    return json.loads(output) if output.strip() else None


def json_object(value: object) -> dict[str, Any] | None:
    """Return ``value`` as a str-keyed dict when it is one, else ``None``.

    RFC 8259 guarantees JSON object keys are strings, so once
    :func:`isinstance` confirms dict-ness the key type is known - this is the
    documented boundary that replaces scattered blind casts over ``gh_json``
    output.
    """
    if not isinstance(value, dict):
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return cast(dict[str, Any], value)


def json_array(value: object) -> list[object] | None:
    """Return ``value`` as a list when it is one, else ``None``."""
    if isinstance(value, list):
        return cast(list[object], value)
    return None


def pr_json(pr_number: int) -> dict[str, Any]:
    """Fetch authoritative current PR state."""
    payload = gh_json(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            REPO,
            "--json",
            "state,isDraft,mergeable,headRefOid,headRefName,body,labels,author",
        ]
    )
    obj = json_object(payload)
    if obj is None:
        raise RuntimeError(f"PR #{pr_number} payload was not an object")
    return obj


def target_json(number: int) -> dict[str, Any]:
    """Fetch issue-compatible target state."""
    return cast(dict[str, Any], gh_json(["api", f"repos/{REPO}/issues/{number}"]))


def labels_of(item: dict[str, Any]) -> set[str]:
    """Return normalized label names from a GitHub payload."""
    labels: set[str] = set()
    for label in item.get("labels") or []:
        if isinstance(label, dict) and label.get("name"):
            labels.add(str(label["name"]))
        elif isinstance(label, str):
            labels.add(label)
    return labels


def replace_factory_labels(number: int, owner: str, stage: str) -> None:
    """Atomically replace factory owner/stage labels on one target.

    Uses GET-then-PUT with bounded retry on transient 409/429/5xx so
    concurrent drain vs dispatcher label clobber is eventually recovered.
    """
    last_exc: RuntimeError | None = None
    for attempt in range(3):
        try:
            target = target_json(number)
            # Shared write choke point: never add or rewrite factory labels on
            # a manual-only hold. Leftover factory* labels stay for humans.
            if eligibility.is_manual_only(str(target.get("body") or "")):
                return
            current = labels_of(target)
            labels = {
                label
                for label in current
                if not OWNER_RE.fullmatch(label) and label not in STAGE_LABELS and label != "factory"
                and not (label == "factory:approved" and stage in {"factory:building", "factory:changes-requested"})
            }
            labels.update({"factory", owner, stage})
            run_gh(
                [
                    "api",
                    "--method",
                    "PUT",
                    f"repos/{REPO}/issues/{number}/labels",
                    "--input",
                    "-",
                ],
                input_json={"labels": sorted(labels)},
            )
            return
        except RuntimeError as exc:
            last_exc = exc
            message = str(exc)
            is_transient = any(code in message for code in ("409", "429", "409 Conflict", "429 Too"))
            is_transient = is_transient or "rate limit" in message.lower()
            if is_transient and attempt < 2:
                time.sleep(0.5 * (2**attempt))
                continue
            # Retry once on any other transient PUT failure (GET consistency window).
            if attempt < 1:
                time.sleep(0.25)
                continue
            raise
    if last_exc is not None:
        raise last_exc


def linked_issue_from_branch(branch: str | None) -> int | None:
    """Extract an issue number from a canonical fixed-model branch."""
    if not branch:
        return None
    match = re.match(r"^factory/\d+-(\d+)-", branch)
    return int(match.group(1)) if match else None


PART_OF_ISSUE_RE = re.compile(
    r"(?im)^[ \t]*part[ \t]+of[ \t]+#(?P<issue>\d+)"
    r"(?:[ \t]+and[ \t]+governed[ \t]+by[ \t]+#\d+)?\.[ \t]*\r?$"
)


def explicit_part_of_issue(body: str) -> int | None:
    """Resolve the issue named by the canonical non-closing ``Part of #N.`` line.

    Only the standalone canonical line counts, including the governed form
    ``Part of #N and governed by #M.``; it names #N as the issue this PR
    advances while deliberately leaving closure to later delivery work.
    Casual prose mentions such as ``Advances #N``, ``See #N``, or a bare
    parent reference never identify an executable issue.

    Args:
        body: Complete pull-request body text.

    Returns:
        The issue number named by the canonical line, or None when absent.
    """
    match = PART_OF_ISSUE_RE.search(body)
    return int(match.group("issue")) if match else None


def linked_issue_mode_for_pr(
    pr_number: int, branch: str | None, *, body: str | None = None
) -> tuple[int | None, bool]:
    """Resolve a PR's issue and whether the link is review-only.

    A canonical factory branch retains issue ownership, but a matching
    ``Part of #N.`` line makes its PR non-closing unless the body explicitly
    claims closure. Without a factory branch, explicit closing references
    take precedence over a canonical non-closing ``Part of`` line. Mismatched
    references cannot redirect issue identity or relax acceptance; merge-time
    closure still follows explicit closing references alone.

    Args:
        pr_number: Pull request number.
        branch: Pull request head branch.
        body: Optional already-fetched PR description to avoid redundant API reads.

    Returns:
        ``(issue, non_closing)``; ``non_closing`` is true only for a ``Part of``
        link.
    """
    if body is None:
        body = str(pr_json(pr_number).get("body") or "")
    linked = linked_issue_from_branch(branch)
    match = re.search(
        r"(?i)\b(?:closes?|fix(?:es|ed)?|resolves?|resolved)\s+#(\d+)",
        body,
    )
    if linked is not None:
        # The canonical branch selects the issue, not the PR's closure scope.
        # Honor a matching explicit Part of only when no closing claim exists.
        return linked, match is None and explicit_part_of_issue(body) == linked
    if match:
        return int(match.group(1)), False
    issue = explicit_part_of_issue(body)
    return issue, issue is not None


def linked_issue_for_pr(pr_number: int, branch: str | None) -> int | None:
    """Resolve a manual/local PR's executable issue for trusted semantic review.

    Args:
        pr_number: Pull request number.
        branch: Pull request head branch.

    Returns:
        Canonical linked issue number, if any.
    """
    return linked_issue_mode_for_pr(pr_number, branch)[0]


def flatten_pages(value: object | None) -> list[dict[str, Any]]:
    """Flatten gh api --paginate --slurp JSON pages into item dicts.

    ``--slurp`` normally yields a list of pages (each page a list of items),
    but a single-page response can arrive as one bare object. Both shapes
    narrow through :func:`json_object` so the str-key invariant is checked,
    not assumed.
    """
    top = json_object(value)
    if top is not None:
        return [top]
    pages = json_array(value)
    if pages is None:
        return []
    result: list[dict[str, Any]] = []
    for page in pages:
        page_obj = json_object(page)
        if page_obj is not None:
            result.append(page_obj)
            continue
        page_items = json_array(page)
        if page_items is None:
            continue
        for item in page_items:
            item_obj = json_object(item)
            if item_obj is not None:
                result.append(item_obj)
    return result


def issue_comment_records(pr_number: int) -> list[dict[str, Any]]:
    """Return raw issue-comment payloads for one pull request."""
    pages = gh_json(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{REPO}/issues/{pr_number}/comments?per_page=100",
        ]
    )
    return flatten_pages(pages)


def review_comment_bodies(pr_number: int) -> list[str]:
    """Return trusted comments that may contain review attestations.

    Factory workers post through ``GITHUB_TOKEN`` as ``github-actions[bot]``.
    The repository owner may also run the review controller during incidents;
    those OWNER-authored exact-head markers must remain visible to authorize /
    drain or ready PRs get falsely demoted (incident #2309 observation).
    """
    return trusted_comment_bodies(issue_comment_records(pr_number))


def model_comment_bodies(pr_number: int) -> list[str]:
    """Return controller-bot comments trusted for author/reviewer model markers.

    OWNER and collaborator comments are ignored here even when they are trusted
    for factory worker provenance. Cursor cloud agents act as those accounts
    and must not be able to declare their own author or reviewer model.
    """
    return controller_comment_bodies(issue_comment_records(pr_number))


def exact_head_provenance(
    *,
    reviewer_comments: list[str],
    model_comments: list[str],
    pr_number: int,
    head: str,
    producer: str | None,
) -> tuple[set[str], bool]:
    """Return trusted exact-head contributors and whether authorship is recorded.

    Contributor markers and author-model markers come from different trust
    streams: contributor markers may also be written by the owner during
    incidents, while author and reviewer model markers are trusted only from the
    controller bot. An exact-head author attestation is complete provenance for
    the same reason a head-contributor marker is: it names who authored this
    exact head, so one independent approval authorizes it exactly as it does for
    a wrapper-pushed head. A head with neither record still fails closed at two
    distinct approvals.

    Args:
        reviewer_comments: Trusted comment bodies from the human/bot stream.
        model_comments: Comment bodies trusted for author and reviewer models.
        pr_number: Pull request number.
        head: Exact commit SHA under consideration.
        producer: Declared branch or body producer worker, if any.

    Returns:
        ``(contributors, provenance_complete)``.
    """
    contributors, head_recorded = head_contributor_provenance(
        reviewer_comments, pr=pr_number, head=head, producer=producer
    )
    author_recorded = (
        current_head_author_identity(model_comments, pr=pr_number, head=head)
        is not None
    )
    return contributors, head_recorded or author_recorded


def wrapper_recorded_contributors(pr_number: int, *, head: str) -> set[str]:
    """Return the wrapper's own exact-head contributor record, or an empty set.

    The contributor-repair path must not read the owner-writable reviewer stream:
    owner, member, and collaborator accounts include Cursor cloud agents that can
    post a plausible head-contributor marker, and minting a controller-bot author
    marker from one would let a spoofed comment supply exact-head author
    provenance. `record_contribution` writes through `GITHUB_TOKEN`, so the
    wrapper's own record is always on the controller-bot stream.

    Args:
        pr_number: Pull request number.
        head: Exact commit SHA being repaired.

    Returns:
        Worker ids the controller bot recorded against this exact head.
    """
    return current_head_contributors(
        model_comment_bodies(pr_number), pr=pr_number, head=head
    )


def roster_model_for_worker(worker: str) -> str | None:
    """Report the actually invoked model, never infer one from a worker ID."""
    del worker
    selected = os.environ.get("FACTORY_RUNTIME_MODEL", "").strip()
    return selected or None


def persist_author_model_marker(*, pr_number: int, head: str, worker: str) -> str | None:
    """Write the controller author-model marker for one exact head.

    Args:
        pr_number: Pull request number.
        head: Exact commit SHA just recorded.
        worker: Factory worker that pushed the head.

    Returns:
        Marker text when a roster model exists, otherwise ``None``.
    """
    model = roster_model_for_worker(worker)
    if not model:
        return None
    marker = agent_model_author_marker(
        pr=pr_number,
        head=head,
        model=model,
        agent=worker,
    )
    run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", marker])
    return marker


def head_commit_evidence(head: str) -> tuple[str | None, str | None, str] | None:
    """Return GitHub-attributed identities and message for one exact commit.

    Args:
        head: Exact commit SHA that must still match the fetched commit.

    Returns:
        ``(author_login, committer_login, message)`` when GitHub reports the
        requested SHA, otherwise ``None``. A ``None`` login means GitHub
        attributed no verified account to that half of the commit.
    """
    commit = json_object(gh_json(["api", f"repos/{REPO}/commits/{head}"]))
    if commit is None or str(commit.get("sha") or "") != head:
        return None
    author = json_object(commit.get("author"))
    committer = json_object(commit.get("committer"))
    metadata = json_object(commit.get("commit"))
    author_login = None
    if author is not None and author.get("login"):
        author_login = str(author["login"])
    committer_login = None
    if committer is not None and committer.get("login"):
        committer_login = str(committer["login"])
    message = str(metadata.get("message") or "") if metadata is not None else ""
    return author_login, committer_login, message


def attest_exact_head_author(
    *, pr_number: int, head: str, pr: dict[str, Any]
) -> tuple[str, str] | None:
    """Write the missing controller author attestation for one exact head.

    A head created by hand or by a local agent has no wrapper
    ``record-contribution`` marker, so the cross-model gate cannot establish its
    author and the pull request can never become ready. This is the single
    repair path for that case, and every branch of it is controller-authored and
    bound to the exact head:

    1. the controller bot's own exact-head head-contributor record names the
       numbered worker that pushed it, so the missing author marker is repaired
       from that worker's roster pin. Only the controller-bot stream is trusted
       here: contributor markers on the owner stream are attacker-controlled and
       must never be laundered into a bot author attestation;
    2. otherwise a human head is attested from the pull request's verified
       GitHub author and the head commit's ``Model:`` trailer, or ``human``
       when absent; contributor commits must match the verified PR author;
    3. anything else, including conflicting records or a foreign commit
       identity, fails closed with ``None``.

    The command ``reconcile-provenance`` exposes this explicitly for a head that
    will not receive another review pass; ordinary reviews call it directly, so a
    hand-made or local-agent head is reviewable exactly like a wrapper-pushed
    one.

    Args:
        pr_number: Pull request number.
        head: Exact current head being attested.
        pr: Authoritative PR payload from :func:`pr_json`.

    Returns:
        ``(model_family, agent)`` when a marker was written, else ``None``.
    """
    if "factory" not in labels_of(pr):
        return None
    if current_head_author_identity(
        model_comment_bodies(pr_number), pr=pr_number, head=head
    ) is not None:
        return None
    contributors = wrapper_recorded_contributors(pr_number, head=head)
    if len(contributors) == 1:
        worker = next(iter(contributors))
        try:
            model = roster_model_for_worker(worker)
        except (ImportError, OSError, ValueError):
            # A missing or unreadable roster is an infrastructure fault. Fail
            # closed instead of aborting the review that could still attest the
            # head through the owner path.
            return None
        if not model:
            return None
        marker = agent_model_author_marker(
            pr=pr_number, head=head, model=model, agent=worker
        )
        run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", marker])
        return normalize_model_family(model), marker_model_token(worker)
    if len(contributors) > 1:
        return None
    evidence = head_commit_evidence(head)
    if evidence is None:
        return None
    commit_author, commit_committer, message = evidence
    author = json_object(pr.get("author"))
    owner = REPO.split("/", 1)[0]
    pr_author_login = str(author.get("login") or "") if author is not None else ""
    # A contributor head has the same provenance rule as a local human head,
    # anchored to GitHub's actual PR and commit identities. Unlinked commit
    # emails use the verified submitter, as for owner-authored local heads;
    # known foreign identities cannot inherit the submitter's attribution.
    if pr_author_login != owner:
        if (
            not pr_author_login
            or pr_author_login.endswith("[bot]")
            or commit_author not in {None, pr_author_login}
            or commit_committer not in {None, pr_author_login, "web-flow"}
        ):
            return None
    identity = owner_attested_head_identity(
        pr_author_login=pr_author_login,
        owner_login=pr_author_login,
        commit_author_login=commit_author,
        commit_committer_login=commit_committer,
        commit_message=message,
        trusted_logins=("web-flow",) if pr_author_login != owner else _review_policy.TRUSTED_AUTOMATION_LOGINS,
    )
    if identity is None:
        return None
    model, agent = identity
    marker = agent_model_author_marker(
        pr=pr_number, head=head, model=model, agent=agent
    )
    run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", marker])
    return model, agent


RECONCILIATION_MARKER_PREFIX = "comic-pile-factory-provenance-reconciliation-v1"


def reconciliation_marker(*, pr: int, head: str, model: str, agent: str) -> str:
    """Build the durable audit marker for one explicit provenance reconciliation.

    The marker is informational: it records which head a maintainer reconciled
    and the identity that was attested, so a later auditor can see that the
    attribution was an explicit reconciliation rather than a wrapper push.
    """
    return (
        f"<!-- {RECONCILIATION_MARKER_PREFIX}:"
        f"pr-{pr}:head-{head}:model-{model}:agent-{agent} -->"
    )


def reconcile_provenance(
    pr_number: int, expected_head: str | None = None
) -> dict[str, Any]:
    """Explicitly reconcile exact-head provenance for a wrapper-less head.

    A head advanced outside the numbered-worker wrapper - an owner-pushed merge
    or rebase, or trusted repository automation - has no controller-authored
    author marker, so the exact-head cross-model gate fails closed forever and
    the PR is parked in ``factory:blocked``. This command establishes that
    provenance from GitHub-authoritative evidence without inventing a worker
    identity, then releases the head back to review so any approvals already
    recorded for it and the new author marker can complete the merge gate.

    Args:
        pr_number: Pull request number to reconcile.
        expected_head: Optional exact head the caller believes is current. A
            mismatch fails closed so a moved head is never attested blindly.

    Returns:
        JSON-serializable result describing the reconciliation status.
    """
    pr = pr_json(pr_number)
    if str(pr.get("state")) != "OPEN":
        return {"pr": pr_number, "status": "not-open"}
    if "factory" not in labels_of(pr):
        return {"pr": pr_number, "status": "not-factory"}
    head = str(pr.get("headRefOid") or "")
    if not HEAD_RE.fullmatch(head):
        return {"pr": pr_number, "status": "invalid-head"}
    if expected_head is not None and expected_head != head:
        return {
            "pr": pr_number,
            "status": "stale-head",
            "head": head,
            "expected": expected_head,
        }
    if current_head_author_identity(
        model_comment_bodies(pr_number), pr=pr_number, head=head
    ) is not None:
        replace_factory_labels(pr_number, "factory:unowned", "factory:review")
        return {"pr": pr_number, "status": "already-attested", "head": head}
    identity = attest_exact_head_author(pr_number=pr_number, head=head, pr=pr)
    if identity is None:
        return {
            "pr": pr_number,
            "status": "unattested",
            "head": head,
            "reason": (
                "no owner-attested exact-head identity: the pull-request author, "
                "the head commit identity, or the Model trailer is untrusted; "
                "repair the head through a numbered worker wrapper instead"
            ),
        }
    model, agent = identity
    run_gh(
        [
            "issue",
            "comment",
            str(pr_number),
            "--repo",
            REPO,
            "--body",
            reconciliation_marker(pr=pr_number, head=head, model=model, agent=agent),
        ]
    )
    replace_factory_labels(pr_number, "factory:unowned", "factory:review")
    return {
        "pr": pr_number,
        "status": "attested",
        "head": head,
        "model": model,
        "agent": agent,
    }


def model_for_trusted_review_session(reviewer: str) -> str | None:
    """Bind reviewer provenance to the executed route, not the coding roster.

    The runtime/source/model triplet comes from the trusted factory Entry job,
    not from the PR body or agent's prose. Ordinary sessions and local/manual
    controller invocations retain the existing roster-based behavior.
    """
    runtime = os.environ.get("FACTORY_RUNTIME_MODEL", "")
    source = os.environ.get("FACTORY_SOURCE", "")
    selected = os.environ.get("FACTORY_MODEL", "")
    if runtime == "openrouter/free" or selected == "openrouter/free":
        if (source, selected, runtime) != (
            "openrouter-free", "openrouter/free", "openrouter/free"
        ):
            raise RuntimeError("inconsistent dedicated semantic review runtime provenance")
        return "openrouter/free"
    return roster_model_for_worker(reviewer)


def persist_cross_model_review_marker(
    *,
    pr_number: int,
    head: str,
    reviewer: str,
) -> str | None:
    """Write the controller review-model marker when identities and families differ.

    The reviewer cannot declare the author's model: that identity is read from
    the controller-authored author marker recorded at push time. Same-family
    models and same-agent reviews are skipped so the merge gate stays fail
    closed.

    Args:
        pr_number: Pull request number.
        head: Exact commit SHA under review.
        reviewer: Factory worker the controller launched for this review.

    Returns:
        Marker text when a qualifying cross-model review was recorded.
    """
    recorded = current_head_author_identity(
        model_comment_bodies(pr_number),
        pr=pr_number,
        head=head,
    )
    reviewer_model = model_for_trusted_review_session(reviewer)
    if recorded is None or reviewer_model is None:
        return None
    author_model, author_agent = recorded
    if marker_model_token(author_agent) == marker_model_token(reviewer):
        return None
    if normalize_model_family(author_model) == normalize_model_family(reviewer_model):
        return None
    marker = agent_model_review_marker(
        pr=pr_number,
        head=head,
        author_model=author_model,
        reviewer_model=reviewer_model,
        author_agent=author_agent,
        reviewer_agent=reviewer,
    )
    run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", marker])
    return marker


def redact_review_text(text: str) -> str:
    """Redact common credential shapes before review output reaches GitHub."""
    text = SENSITIVE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = BEARER_RE.sub(r"\1[REDACTED]", text)
    text = GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", text)
    return API_KEY_RE.sub("[REDACTED_API_KEY]", text)


AUTHORITATIVE_DIFF_EVIDENCE = "# comic-pile-factory-authoritative-diff-evidence"


def semantic_review_excerpt(text: str) -> str:
    """Keep semantic findings ahead of appended inspection proof or raw diff.

    The inspection command is evidence for APPROVE, but its diff body is never
    itself a REPAIR diagnosis. Preserve the last 7000 chars of semantic output
    rather than letting thousands of changed lines evict the model's findings.
    """
    semantic, marker, evidence = text.partition(AUTHORITATIVE_DIFF_EVIDENCE)
    # Older worker logs sometimes contain a diff without a delimiting marker.
    semantic = re.split(r"(?m)^diff --git ", semantic, maxsplit=1)[0]
    excerpt = redact_review_text(semantic[-7000:])
    if marker:
        command = re.search(r"(?m)^gh pr diff [0-9]+\s*$", evidence)
        if command:
            excerpt += f"\n{AUTHORITATIVE_DIFF_EVIDENCE}\n{command.group(0).strip()}"
    return excerpt


def trusted_review_log(path: str | None, *, worker: str) -> str:
    """Read only a validated worker log, retaining the full review evidence."""
    if not path:
        return ""
    expected = {
        Path(f"/tmp/opencode-factory-{worker}.log"),
        Path(f"/tmp/opencode-factory-{worker}.sanitized.log"),
    }
    candidate = Path(path)
    if candidate not in expected or candidate.is_symlink():
        return ""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except OSError:
        return ""
    return text


def review_excerpt(path: str | None, *, worker: str) -> str:
    """Expose only the bounded, redacted prose excerpt in GitHub comments."""
    return semantic_review_excerpt(trusted_review_log(path, worker=worker))


def review_acceptance_report(path: str | None, *, worker: str) -> Mapping[str, Any] | None:
    """Parse complete acceptance JSON before any 7K excerpt truncation."""
    return issue_acceptance.report_from_text(trusted_review_log(path, worker=worker))


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


def post_review_comment(
    *,
    pr_number: int,
    marker: str | None,
    reviewer: str,
    verdict: str,
    excerpt: str,
    note: str,
    acceptance_marker: str | None = None,
) -> None:
    """Persist semantic findings and controller audit metadata."""
    parts: list[str] = []
    if marker:
        parts.append(marker)
    if acceptance_marker:
        parts.append(acceptance_marker)
    parts.append(f"### Factory semantic review · Factory {reviewer} · {verdict.upper()}")
    if note:
        parts.append(note)
    if excerpt:
        parts.append(
            "<details><summary>Review output</summary>\n\n```text\n" + excerpt + "\n```\n</details>"
        )
    run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", "\n\n".join(parts)])
    if acceptance_marker is not None:
        # Controller-generated metadata is a separate whole-body comment. Never
        # accept matching strings in review prose, quoted output, or PR bodies.
        run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", acceptance_marker])


def set_review_approval_visibility(pr_number: int, head: str) -> None:
    """Project a validated semantic approval without relying on bot comment events.

    GitHub does not dispatch issue_comment workflows for comments written using
    its GITHUB_TOKEN. This is called only after the controller has verified
    independent review, exact-head linked-issue acceptance, cross-model
    provenance, and every mechanical readiness gate. The label never grants
    merge authorization or replaces any of those checks.
    """
    live = pr_json(pr_number)
    if live.get("state") != "OPEN" or live.get("headRefOid") != head:
        return
    target = target_json(pr_number)
    if eligibility.is_manual_only(str(target.get("body") or "")):
        return
    labels = labels_of(target)
    if ("factory:building" in labels or
            "factory:changes-requested" in labels or
            "factory:approved" in labels):
        return
    run_gh(
        ["api", "--method", "PUT", f"repos/{REPO}/issues/{pr_number}/labels",
         "--input", "-"],
        input_json={"labels": sorted(labels | {"factory:approved"})},
    )


def interpret_required_checks(
    checks: object | None,
    *,
    command_status: int,
    stderr: str,
) -> GateResult:
    """Pure interpretation of `gh pr checks --required --json state` output.

    Fail-closed: a head with no reported required checks has not been
    validated, so it can never satisfy the merge gates.
    """
    del command_status
    if checks is None:
        if NO_REQUIRED_CHECKS_RE.search(stderr):
            return gate_result("retry", NO_REQUIRED_CHECKS_REASON)
        return gate_result("retry", "required checks could not be determined")
    if not isinstance(checks, list):
        return gate_result("retry", "required check payload was not a list")
    if not checks:
        return gate_result("retry", NO_REQUIRED_CHECKS_REASON)

    states: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            return gate_result("retry", "required check payload contained an invalid row")
        state = str(check.get("state") or "").upper()
        if not state:
            return gate_result("retry", "required check state was missing")
        states.append(state)

    failing = sorted(set(states) & FAILING_CHECK_STATES)
    if failing:
        return gate_result("deny", f"required checks failed: {', '.join(failing)}")
    waiting = sorted(set(states) & RETRY_CHECK_STATES)
    if waiting:
        return gate_result("retry", f"required checks are not terminal: {', '.join(waiting)}")
    unknown = sorted(set(states) - PASSING_CHECK_STATES)
    if unknown:
        return gate_result("retry", f"unknown required check states: {', '.join(unknown)}")
    return gate_result("pass", "all required checks are successful")


def required_checks_gate(pr_number: int) -> GateResult:
    proc = run_gh(
        ["pr", "checks", str(pr_number), "--repo", REPO, "--required", "--json", "state"],
        check=False,
    )
    checks: object | None = None
    if proc.stdout.strip():
        try:
            checks = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return gate_result("retry", "required checks returned invalid JSON")
    return interpret_required_checks(checks, command_status=proc.returncode, stderr=proc.stderr)


def list_ci_pr_numbers() -> list[int]:
    """List open CI-stage factory PRs for deterministic reconciliation.

    Uses paginated search so enumeration beyond --limit does not starve
    oldest CI PRs. Falls back to a high-limit pr list on transient search
    failures.
    """
    try:
        pages = gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"search/issues?per_page=100&q=repo:{REPO}+type:pr+state:open+label:factory:ci",
            ]
        )
        flat = flatten_pages(pages)
        # Search API returns objects with `items`; pulls API returns flat lists.
        numbers: list[int] = []
        for page in flat:
            if isinstance(page, dict) and isinstance(page.get("items"), list):
                for item in page["items"]:
                    if isinstance(item, dict) and item.get("number") is not None:
                        try:
                            numbers.append(int(item["number"]))
                        except (TypeError, ValueError):
                            continue
            elif isinstance(page, dict) and page.get("number") is not None:
                try:
                    numbers.append(int(page["number"]))
                except (TypeError, ValueError):
                    continue
            elif isinstance(page, list):
                for item in page:
                    if isinstance(item, dict) and item.get("number") is not None:
                        try:
                            numbers.append(int(item["number"]))
                        except (TypeError, ValueError):
                            continue
        if numbers:
            return sorted(set(numbers))
        # If search returned no items but succeeded, treat as empty rather than fallback.
        if isinstance(pages, list) and flat == []:
            # Check whether pages was an empty search result (total_count 0)
            # vs actual failure; empty success should return []
            for pg in pages if isinstance(pages, list) else []:
                if isinstance(pg, dict) and "total_count" in pg:
                    return []
    except (RuntimeError, json.JSONDecodeError, ValueError):
        pass
    # Fallback: high-limit pr list (handles pagination internally) for resilience.
    rows = gh_json(
        [
            "pr",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--label",
            "factory:ci",
            "--limit",
            "1000",
            "--json",
            "number",
        ]
    )
    if not isinstance(rows, list):
        raise RuntimeError("factory:ci PR listing was unavailable")
    return [int(row["number"]) for row in rows if isinstance(row, dict) and row.get("number")]


def active_factory_owner(labels: set[str]) -> str | None:
    """Return an active owner, ignoring the explicit unowned marker."""
    owners = {label for label in labels if OWNER_RE.fullmatch(label)}
    active = owners - {"factory:unowned"}
    return sorted(active)[0] if active else None


def persist_repair_handoff(
    *,
    pr_number: int,
    findings: str,
    reviewer: str,
    note: str,
    marker: str | None = None,
    branch: str | None = None,
    worker: str | None = None,
) -> None:
    """Persist validated repair findings before releasing a PR for changes."""
    excerpt = redact_review_text(findings)[-7000:]
    if not has_actionable_review_findings(excerpt):
        raise RuntimeError("repair handoff requires durable actionable review findings")
    post_review_comment(
        pr_number=pr_number,
        marker=marker,
        reviewer=reviewer,
        verdict="repair",
        excerpt=excerpt,
        note=note,
    )
    if branch is not None and worker is not None:
        transition_pr_and_linked_issue(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            pr_stage="factory:changes-requested",
        )
    else:
        replace_factory_labels(pr_number, "factory:unowned", "factory:changes-requested")


def reconcile_ci_pr(pr_number: int) -> dict[str, Any]:
    """Reconcile one CI PR using exact-head controller authorities."""
    pr = pr_json(pr_number)
    labels = labels_of(pr)
    if str(pr.get("state")) != "OPEN" or "factory:ci" not in labels:
        return {"pr": pr_number, "status": "skipped"}

    owner = active_factory_owner(labels)
    if owner is not None:
        checks = required_checks_gate(pr_number)
        if checks["decision"] != "deny":
            return {"pr": pr_number, "status": "owned"}
        persist_repair_handoff(
            pr_number=pr_number,
            findings=str(checks["reason"]),
            reviewer="controller",
            note=(
                f"Required checks failed during CI reconciliation while {owner} still held "
                "the lease; terminal CI failure overrides that lease and requires repair."
            ),
        )
        return {"pr": pr_number, "status": "changes-requested", "reason": checks["reason"]}

    checks = required_checks_gate(pr_number)
    if checks["decision"] != "pass":
        status = classify_ci_reconciliation(checks_decision=checks["decision"], authorized=False)
        if checks["decision"] == "deny":
            persist_repair_handoff(
                pr_number=pr_number,
                findings=str(checks["reason"]),
                reviewer="controller",
                note="Required checks failed during CI reconciliation; repair is required.",
            )
            status = "changes-requested"
        return {"pr": pr_number, "status": status, "reason": checks["reason"]}

    head = str(pr.get("headRefOid") or "")
    if not HEAD_RE.fullmatch(head):
        return {"pr": pr_number, "status": "retry-ci", "reason": "current head unavailable"}

    producer = producer_worker_from_pr(
        branch=str(pr.get("headRefName") or ""), body=str(pr.get("body") or "")
    )
    try:
        comments = review_comment_bodies(pr_number)
        model_comments = model_comment_bodies(pr_number)
        approvers = current_head_approvers(comments, pr=pr_number, head=head)
        contributors, provenance_complete = exact_head_provenance(
            reviewer_comments=comments,
            model_comments=model_comments,
            pr_number=pr_number,
            head=head,
            producer=producer,
        )
    except (RuntimeError, json.JSONDecodeError) as exc:
        return {
            "pr": pr_number,
            "status": "retry-ci",
            "reason": f"semantic authorization could not be determined: {exc}",
        }
    authorized = head_has_authorized_approval(
        contributors=contributors,
        approvers=approvers,
        provenance_complete=provenance_complete,
    )
    if not authorized:
        replace_factory_labels(pr_number, "factory:unowned", "factory:review")
        return {"pr": pr_number, "status": "review", "head": head}

    # CI reconciliation is also a ready-promoter, not just the review callback.
    # Both must enforce the same trusted exact-head cross-model identity gate.
    try:
        cross_model = head_has_cross_model_review(
            model_comment_bodies(pr_number), pr=pr_number, head=head
        )
    except (RuntimeError, json.JSONDecodeError) as exc:
        return {
            "pr": pr_number,
            "status": "retry-ci",
            "reason": f"cross-model provenance could not be inspected: {exc}",
        }
    if not cross_model:
        replace_factory_labels(pr_number, "factory:unowned", "factory:blocked")
        return {
            "pr": pr_number,
            "status": "cross-model-provenance-blocked",
            "head": head,
            "reason": "trusted exact-head author/reviewer model markers are missing",
        }

    mechanical = mechanical_merge_gate(pr_number, head)
    current_head = str(pr_json(pr_number).get("headRefOid") or "")
    if current_head != head:
        replace_factory_labels(pr_number, "factory:unowned", "factory:review")
        return {"pr": pr_number, "status": "review", "head": current_head}

    status = classify_ci_reconciliation(
        checks_decision=checks["decision"],
        authorized=True,
        mechanical_decision=mechanical["decision"],
    )
    if status == "ready":
        replace_factory_labels(pr_number, "factory:unowned", "factory:ready")
    elif status == "changes-requested":
        persist_repair_handoff(
            pr_number=pr_number,
            findings=str(mechanical["reason"]),
            reviewer="controller",
            note=f"Exact-head mechanical gates failed for {head}; repair is required.",
        )
    return {
        "pr": pr_number,
        "status": status,
        "head": head,
        "reason": mechanical["reason"],
    }


def reconcile_ci() -> list[dict[str, Any]]:
    """Reconcile every currently open factory:ci PR."""
    results: list[dict[str, Any]] = []
    for pr_number in list_ci_pr_numbers():
        try:
            result = reconcile_ci_pr(pr_number)
        except (RuntimeError, json.JSONDecodeError) as exc:
            result = {"pr": pr_number, "status": "retry-ci", "reason": str(exc)}
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
            raise
        results.append(result)
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
    return results


def interpret_review_threads(
    nodes: object | None,
    *,
    head: str,
    has_next_page: bool = False,
) -> GateResult:
    """Pure exact-head unresolved-thread interpretation."""
    if has_next_page:
        return gate_result("retry", "review thread pagination was incomplete")
    if not isinstance(nodes, list):
        return gate_result("retry", "review thread payload was unavailable")
    for node in nodes:
        if not isinstance(node, dict):
            return gate_result("retry", "review thread payload contained an invalid row")
        if bool(node.get("isResolved")):
            continue
        comments = node.get("comments")
        if not isinstance(comments, dict):
            return gate_result("retry", "unresolved review thread lacked comment metadata")
        if bool((comments.get("pageInfo") or {}).get("hasNextPage")):
            return gate_result("retry", "review thread comments were only partially inspected")
        comment_nodes = comments.get("nodes")
        if not isinstance(comment_nodes, list) or not comment_nodes:
            return gate_result("retry", "unresolved review thread lacked commit metadata")
        # This reviewer was removed from the factory, so its historical
        # comments must not hold PRs. Unknown/human reviewer metadata still
        # follows the normal fail-closed exact-head gate.
        origin = comment_nodes[0].get("author") if isinstance(comment_nodes[0], dict) else None
        login = origin.get("login") if isinstance(origin, dict) else None
        if login in {"coderabbitai", "coderabbitai[bot]"}:
            continue
        saw_commit = False
        for comment in comment_nodes:
            if not isinstance(comment, dict):
                return gate_result("retry", "review thread comment metadata was invalid")
            commit = comment.get("commit")
            if not isinstance(commit, dict) or not commit.get("oid"):
                return gate_result("retry", "review thread comment lacked a commit SHA")
            saw_commit = True
            if str(commit["oid"]) == head:
                return gate_result("deny", "current head has an unresolved review thread")
        if not saw_commit:
            return gate_result("retry", "unresolved review thread could not be head-scoped")
    return gate_result("pass", "no current-head review thread blockers")


def review_thread_query_required(comments: object | None, *, head: str) -> GateResult | None:
    """Use REST review comments to decide whether GraphQL thread state is needed.

    A review thread cannot exist without at least one inline review comment. If REST
    proves every inline comment belongs to an older head, unresolved thread state
    cannot block the exact current head and the GraphQL query is unnecessary. A
    missing commit id fails closed by requiring the GraphQL query.
    """
    rows = flatten_pages(comments)
    for comment in rows:
        author = (comment.get("user") or {}).get("login")
        if author in {"coderabbitai", "coderabbitai[bot]"}:
            continue
        commit_id = comment.get("commit_id")
        if not commit_id or str(commit_id) == head:
            return None
    return gate_result("pass", "no current-head review thread blockers")


def current_head_review_gate(pr_number: int, head: str) -> GateResult:
    try:
        pages = gh_json(
            ["api", "--paginate", "--slurp", f"repos/{REPO}/pulls/{pr_number}/reviews?per_page=100"]
        )
    except RuntimeError as exc:
        detail = redact_review_text(str(exc))[-500:]
        return gate_result("retry", f"review submissions could not be inspected: {detail}")
    for review in flatten_pages(pages):
        author = (review.get("user") or {}).get("login")
        if author in {"coderabbitai", "coderabbitai[bot]"}:
            continue
        if review.get("state") == "CHANGES_REQUESTED" and review.get("commit_id") == head:
            return gate_result("deny", "current head has CHANGES_REQUESTED")

    try:
        comment_pages = gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{REPO}/pulls/{pr_number}/comments?per_page=100",
            ]
        )
    except RuntimeError as exc:
        detail = redact_review_text(str(exc))[-500:]
        return gate_result("retry", f"review comments could not be inspected: {detail}")

    rest_result = review_thread_query_required(comment_pages, head=head)
    if rest_result is not None:
        return rest_result

    owner, name = REPO.split("/", 1)
    try:
        result = cast(
            dict[str, Any],
            gh_json(
                [
                    "api",
                    "graphql",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"number={pr_number}",
                    "-f",
                    (
                        "query=query($owner:String!,$name:String!,$number:Int!){"
                        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
                        "reviewThreads(first:100){nodes{isResolved comments(first:100){"
                        "nodes{commit{oid} author{login}} pageInfo{hasNextPage}}} pageInfo{hasNextPage}}}}}"
                    ),
                ]
            ),
        )
    except RuntimeError as exc:
        detail = redact_review_text(str(exc))[-500:]
        return gate_result("retry", f"review threads could not be inspected: {detail}")
    threads = (
        result.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {})
    )
    return interpret_review_threads(
        threads.get("nodes"),
        head=head,
        has_next_page=bool((threads.get("pageInfo") or {}).get("hasNextPage")),
    )


def poll_mergeable_gate(
    pr_number: int,
    expected_head: str,
    *,
    poll_attempts: int = MERGEABLE_POLL_ATTEMPTS,
    poll_interval: float = MERGEABLE_POLL_INTERVAL_SECONDS,
) -> GateResult:
    """Poll GitHub's asynchronous mergeability computation for an exact head."""
    for attempt in range(max(1, poll_attempts)):
        try:
            info = pr_json(pr_number)
        except RuntimeError:
            return gate_result("retry", "pull request state could not be read")
        if str(info.get("state")) != "OPEN":
            return gate_result("deny", "pull request is not open")
        if bool(info.get("isDraft")):
            return gate_result("deny", "pull request is a draft")
        head = str(info.get("headRefOid") or "")
        if head != expected_head:
            return gate_result("deny", "pull request head changed after semantic review")
        mergeable = str(info.get("mergeable") or "UNKNOWN").upper()
        if mergeable == "MERGEABLE":
            return gate_result("pass", "pull request is mergeable")
        if mergeable == "CONFLICTING":
            return gate_result("deny", "pull request has merge conflicts")
        if mergeable != "UNKNOWN":
            return gate_result("retry", f"unrecognized mergeable state: {mergeable}")
        if attempt + 1 < max(1, poll_attempts):
            time.sleep(poll_interval)
    return gate_result("retry", "mergeability remained UNKNOWN after bounded polling")



def implementation_delivery_gate(
    pr_number: int, expected_head: str, issue_body: str
) -> GateResult:
    """Require behavioral source changes on implementation issues.

    Args:
        pr_number: PR whose implementation is being checked.
        expected_head: Immutable reviewed commit SHA.
        issue_body: Authoritative linked issue contract.

    Returns:
        Pass for behavioral delivery, deny for non-delivery, or retry on read failure.
    """
    if not HEAD_RE.fullmatch(expected_head):
        return gate_result("deny", "reviewed commit SHA is missing or invalid")
    try:
        files, patch = implementation_delivery.complete_pr_diff(
            pr_number, expected_head, REPO, lambda args: run_gh(args).stdout
        )
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        return gate_result("retry", f"implementation diff unavailable: {exc}")
    current_head = str(pr_json(pr_number).get("headRefOid") or "")
    if current_head != expected_head:
        return gate_result("retry", "PR head changed during implementation inspection")
    try:
        delivered, reason = implementation_delivery.pr_delivers_implementation(
            issue_body, files, patch
        )
    except RuntimeError as exc:
        return gate_result("retry", f"implementation parser unavailable: {exc}")
    return gate_result("pass" if delivered else "deny", reason)


def issue_acceptance_gate(pr_number: int, expected_head: str) -> GateResult:
    """Require controller-bot attestation for the current issue body and head."""
    pr = pr_json(pr_number)
    issue_number = linked_issue_for_pr(pr_number, str(pr.get("headRefName") or ""))
    if issue_number is None:
        return gate_result("deny", "factory PR has no linked issue acceptance contract")
    issue = target_json(issue_number)
    body = issue.get("body")
    if not isinstance(body, str):
        return gate_result("retry", "linked issue body could not be read")
    executable = implementation_delivery_gate(pr_number, expected_head, body)
    if executable["decision"] != "pass":
        return executable
    if not issue_acceptance.attested(
        model_comment_bodies(pr_number), issue_number, expected_head, body
    ):
        return gate_result("deny", "no valid current-head, current-issue acceptance attestation")
    return gate_result("pass", "all issue acceptance criteria attested")


def open_native_blockers(issue_number: int) -> list[int]:
    """Return open issues GitHub records as blocking one issue natively.

    Reads the native blocked-by dependency endpoint (the sub-issue blocker
    graph), not the issue body, so blockers recorded through GitHub's own
    relationship links are honored alongside declared text. Mirror of the
    bounded ``?per_page=100`` read in ``factory-work-controller.py`` and
    ``stale_pr_decay.py`` intake.

    Args:
        issue_number: The target issue whose blocked-by links are read.

    Returns:
        Issue numbers whose native blocked-by state is not closed.

    Raises:
        RuntimeError: When the blocked-by state cannot be read or a payload
            item is malformed, so callers fail closed instead of treating
            unknown state as permission.
    """
    payload = gh_json(
        ["api", f"repos/{REPO}/issues/{issue_number}/dependencies/blocked_by?per_page=100"]
    )
    items = json_array(payload)
    if items is None:
        raise RuntimeError(f"native blocked-by payload for #{issue_number} was not an array")
    open_numbers: list[int] = []
    for item in items:
        obj = json_object(item)
        if obj is None:
            raise RuntimeError(f"native blocked-by item for #{issue_number} was not an object")
        number = obj.get("number")
        if not isinstance(number, int):
            raise RuntimeError(f"native blocked-by item for #{issue_number} had no issue number")
        if str(obj.get("state", "")).lower() != "closed":
            open_numbers.append(number)
    return open_numbers


def dependency_block_marker(pr_number: int, issue_number: int, blockers: list[int]) -> str:
    """Build the stable dedupe marker for one dependency-block reason.

    Args:
        pr_number: The pull request whose merge is blocked.
        issue_number: The issue the merge would deliver.
        blockers: The open prerequisite issue numbers.

    Returns:
        An HTML comment marker unique to this PR, issue, and blocker set.
    """
    deps = "-".join(str(number) for number in sorted(blockers))
    return (
        f"<!-- {DEPENDENCY_BLOCK_MARKER_PREFIX}:pr-{pr_number}:"
        f"issue-{issue_number}:deps-{deps} -->"
    )


def record_dependency_block(
    pr_number: int, issue_number: int, blockers: list[int]
) -> None:
    """Leave one durable, deduplicated dependency-block reason on the pull request.

    The drain and every other auto-merge route evaluate this gate repeatedly,
    so an exact marker keeps the reason visible to repair workers without
    turning a long dependency wait into comment spam. A failed write is
    swallowed: recording the reason must never decide whether a merge happens.

    Args:
        pr_number: The pull request whose merge is blocked.
        issue_number: The issue the merge would deliver.
        blockers: The open prerequisite issue numbers.
    """
    marker = dependency_block_marker(pr_number, issue_number, blockers)
    try:
        if any(marker in body for body in review_comment_bodies(pr_number)):
            return
    except (RuntimeError, ValueError):
        return
    listed = ", ".join(f"#{number}" for number in sorted(blockers))
    body = "\n\n".join(
        [
            marker,
            f"Blocked by scheduling or declared dependencies for issue #{issue_number}: "
            f"{listed}. This pull request and its linked issue stay open "
            "until every declared dependency is closed; the check runs again "
            "automatically afterwards.",
        ]
    )
    try:
        run_gh(
            ["issue", "comment", str(pr_number), "--repo", REPO, "--body", body]
        )
    except RuntimeError:
        return


def declared_dependency_gate(pr_number: int) -> GateResult:
    """Block merge while any declared issue dependency is still open.

    Resolves the delivered issue through the same post-merge closure rule
    (explicit closing reference first, then the factory branch number), so the
    check covers the issue the merge actually closes. Dependencies are read
    through the shared ``## Dependencies``/``Depends on #N`` parser plus
    GitHub's native blocked-by links, at their current open/closed states.

    Fails closed on API errors.
    """
    try:
        pr = pr_json(pr_number)
    except RuntimeError:
        return gate_result("retry", "pull request state could not be read for dependency check")
    issue_number = _post_merge_closure.linked_issue(
        str(pr.get("headRefName") or ""), str(pr.get("body") or "")
    )
    if issue_number is None:
        return gate_result("pass", "no linked issue for dependency check")
    try:
        issue = target_json(issue_number)
    except RuntimeError:
        return gate_result("retry", "linked issue could not be read for dependency check")
    body = issue.get("body")
    if not isinstance(body, str):
        return gate_result("retry", "linked issue body could not be read")
    # Recheck the live cutover gate at merge time. A new issue might not yet
    # contain the scheduling prerequisite, and a PR can predate that body edit.
    try:
        cutover = target_json(eligibility.RUST_CUTOVER_EPIC)
    except RuntimeError:
        return gate_result("retry", "Rust cutover state could not be read")
    cutover_state = str(cutover.get("state") or "").upper()
    if cutover_state not in {"OPEN", "CLOSED"}:
        return gate_result("retry", "Rust cutover state is unknown")
    if not eligibility.rust_cutover_work_allowed(
        labels_of(issue), body, cutover_open=cutover_state == "OPEN"
    ):
        record_dependency_block(
            pr_number, issue_number, [eligibility.RUST_CUTOVER_EPIC]
        )
        return gate_result(
            "retry",
            f"Rust cutover #{eligibility.RUST_CUTOVER_EPIC} is still open; "
            "unrelated work must wait",
        )
    try:
        declared = eligibility.parse_declared_dependencies(body)
    except Exception:
        return gate_result("retry", "dependency parse failed")
    open_deps: list[int] = []
    for dep in sorted(declared):
        try:
            dep_issue = target_json(dep)
        except RuntimeError:
            return gate_result("retry", f"dependency state unavailable for #{dep}")
        if str(dep_issue.get("state", "")).upper() != "CLOSED":
            open_deps.append(dep)
    try:
        open_deps.extend(open_native_blockers(issue_number))
    except RuntimeError:
        return gate_result(
            "retry", f"native blocked-by state unavailable for #{issue_number}"
        )
    blockers = sorted(set(open_deps))
    if blockers:
        record_dependency_block(pr_number, issue_number, blockers)
        return gate_result(
            "retry",
            "declared dependencies still open: " + ", ".join(f"#{n}" for n in blockers),
        )
    return gate_result("pass", "all declared dependencies closed")


def mechanical_merge_gate(pr_number: int, expected_head: str) -> GateResult:
    """Single authoritative exact-head mechanical gate for controller and workflows."""
    mergeable = poll_mergeable_gate(pr_number, expected_head)
    if mergeable["decision"] != "pass":
        return mergeable
    checks = required_checks_gate(pr_number)
    if checks["decision"] != "pass":
        return checks
    reviews = current_head_review_gate(pr_number, expected_head)
    if reviews["decision"] != "pass":
        return reviews
    if "factory" in labels_of(pr_json(pr_number)):
        acceptance = issue_acceptance_gate(pr_number, expected_head)
        if acceptance["decision"] != "pass":
            return acceptance
    dependency = declared_dependency_gate(pr_number)
    if dependency["decision"] != "pass":
        return dependency
    return gate_result("pass", "all exact-head mechanical gates passed")


def target_owned_by_worker(number: int, worker: str) -> bool:
    """Return whether exactly this fixed worker currently owns a target."""
    active = {
        label
        for label in labels_of(target_json(number))
        if OWNER_RE.fullmatch(label) and label != "factory:unowned"
    }
    return active == {f"factory:{worker}"}


def transition_pr_and_linked_issue(
    *,
    pr_number: int,
    branch: str,
    worker: str,
    pr_stage: str,
    issue_stage: str | None = None,
) -> None:
    """Release a reviewed PR and its linked issue to controller-owned state."""
    replace_factory_labels(pr_number, "factory:unowned", pr_stage)
    issue = linked_issue_for_pr(pr_number, branch)
    if issue is None:
        return
    try:
        state = target_json(issue)
    except RuntimeError:
        return
    if state.get("state") != "open" or not target_owned_by_worker(issue, worker):
        return
    replace_factory_labels(issue, "factory:unowned", issue_stage or pr_stage)


def validate_review_lease(
    pr_number: int, worker: str, pr: dict[str, Any]
) -> dict[str, Any] | None:
    """Reject review state changes not backed by the current worker lease.

    Returns ``{"status": "already-ready"}`` when the PR has already been
    promoted to ``factory:ready`` by another process — a normal race between
    the work controller assignment and the review session start.  In that
    case the caller must skip the review instead of crashing.
    """
    if str(pr.get("state")) != "OPEN":
        raise RuntimeError(f"PR #{pr_number} is not open")
    labels = labels_of(pr)
    if "factory:review" in labels:
        if f"factory:{worker}" not in labels or not target_owned_by_worker(
            pr_number, worker
        ):
            raise RuntimeError(
                f"PR #{pr_number} is not exclusively leased to Factory {worker}"
            )
        return None
    if "factory:ready" in labels:
        return {"status": "already-ready"}
    raise RuntimeError(f"PR #{pr_number} is not in factory:review")


def return_to_review(
    *,
    pr_number: int,
    branch: str,
    worker: str,
    reviewer: str,
    verdict: str,
    excerpt: str,
    note: str,
    status: str,
    head: str,
    producer: str | None,
    marker: str | None = None,
    stage: str = "factory:review",
) -> dict[str, Any]:
    """Record a non-authoritative result and safely release its lease."""
    try:
        post_review_comment(
            pr_number=pr_number,
            marker=marker,
            reviewer=reviewer,
            verdict=verdict,
            excerpt=excerpt,
            note=note,
        )
    except Exception:
        # Best-effort diagnostic: a failed comment write must not prevent the
        # blocked transition below.
        pass
    transition_pr_and_linked_issue(
        pr_number=pr_number,
        branch=branch,
        worker=worker,
        pr_stage=stage,
    )
    return {"status": status, "head": head, "producer": producer}


def handle_review(
    *,
    worker: str,
    pr_number: int,
    verdict: str,
    reviewed_head: str,
    review_log: str | None,
) -> dict[str, Any]:
    """Interpret model review output under controller-owned repository authority."""
    if not FIXED_WORKER_RE.fullmatch(worker):
        raise RuntimeError(f"unsupported fixed-model reviewer: {worker}")
    if verdict not in {"approve", "repair", "reject", "obsolete", "duplicate", "superseded", "delivered"}:
        raise RuntimeError(f"unsupported semantic verdict: {verdict}")
    if not HEAD_RE.fullmatch(reviewed_head):
        raise RuntimeError("reviewed head must be a full lowercase Git SHA")

    pr = pr_json(pr_number)
    lease_result = validate_review_lease(pr_number, worker, pr)
    if lease_result is not None:
        return lease_result
    branch = str(pr.get("headRefName") or "")
    current_head = str(pr.get("headRefOid") or "")
    if not HEAD_RE.fullmatch(current_head):
        raise RuntimeError(f"PR #{pr_number} has an invalid current head")

    producer = producer_worker_from_pr(branch=branch, body=str(pr.get("body") or ""))
    excerpt = review_excerpt(review_log, worker=worker)

    # Check for stale head - the reviewed head must match the current PR head
    if current_head != reviewed_head:
        return return_to_review(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            reviewer=worker,
            verdict=verdict,
            excerpt=excerpt,
            note=(
                f"Verdict ignored because the reviewed checkout {reviewed_head} no longer "
                f"matches current head {current_head}. The new head requires fresh review."
            ),
            status="stale-head",
            head=current_head,
            producer=producer,
        )

    # Check for self-review first (takes precedence). The declared producer
    # authored every head of its own branch, so the producer is part of the
    # trusted contributor set for this exact head.
    head_comments = review_comment_bodies(pr_number)
    contributors, _ = head_contributor_provenance(
        head_comments, pr=pr_number, head=reviewed_head, producer=producer
    )
    if verdict in {"approve", "reject"}:
        if producer is not None and producer == worker:
            return return_to_review(
                pr_number=pr_number,
                branch=branch,
                worker=worker,
                reviewer=worker,
                verdict=verdict,
                excerpt=excerpt,
                note=(
                    "Verdict ignored because the reviewer is the producing factory. "
                    "A different factory must independently review this exact head."
                ),
                status="self-review-blocked",
                head=reviewed_head,
                producer=producer,
            )
        if worker in contributors:
            return return_to_review(
                pr_number=pr_number,
                branch=branch,
                worker=worker,
                reviewer=worker,
                verdict=verdict,
                excerpt=excerpt,
                note=(
                    "Verdict ignored because this worker contributed to the exact head under "
                    "review, so it cannot attest its own code. A worker that authored none of "
                    f"this head must review it; current contributors: {sorted(contributors)}."
                ),
                status="self-review-blocked",
                head=reviewed_head,
                producer=producer,
            )

    # For approval, require evidence of diff / PR inspection. Failed attempts must
    # not render as APPROVE: that false signal stalls promotion while looking done.
    if verdict == "approve":
        if not DIFF_INSPECTION_RE.search(excerpt):
            return return_to_review(
                pr_number=pr_number,
                branch=branch,
                worker=worker,
                reviewer=worker,
                verdict="not-ready",
                excerpt=excerpt,
                note=(
                    "Approval rejected: no evidence of diff inspection "
                    "(e.g., `gh pr diff`, `git diff`/`git show`, or `gh pr view`). "
                    "The reviewed head must be inspected before a durable approve marker is written."
                ),
                status="diff-inspection-required",
                head=reviewed_head,
                producer=producer,
            )

    # A semantic APPROVE must independently account for every issue criterion.
    # The review log is untrusted; only this controller may attest it, and
    # only for the exact issue revision + PR head it verified.
    accepted_contract_marker: str | None = None
    if verdict == "approve":
        issue_number, non_closing = linked_issue_mode_for_pr(
            pr_number, branch, body=str(pr.get("body") or "")
        )
        if issue_number is None:
            return return_to_review(
                pr_number=pr_number, branch=branch, worker=worker,
                reviewer=worker, verdict="not-ready", excerpt=excerpt,
                note="Approval rejected: no authoritative linked issue could be found.",
                status="acceptance-issue-missing", head=reviewed_head, producer=producer,
            )
        issue_object = target_json(issue_number)
        if issue_object.get("state") not in {"open", "closed"}:
            raise RuntimeError("linked issue state unavailable")
        body = issue_object.get("body")
        if not isinstance(body, str):
            raise RuntimeError("linked issue body unavailable")
        implementation = implementation_delivery_gate(
            pr_number, reviewed_head, body
        )
        if implementation["decision"] != "pass":
            return return_to_review(
                pr_number=pr_number, branch=branch, worker=worker,
                reviewer=worker, verdict="not-ready", excerpt=excerpt,
                note="Approval rejected: " + str(implementation["reason"]),
                status="implementation-evidence-required",
                stage=("factory:changes-requested"
                       if implementation["decision"] == "deny" else "factory:review"),
                head=reviewed_head, producer=producer,
            )
        changed = run_gh(
            ["pr", "diff", str(pr_number), "--repo", REPO, "--name-only"]
        ).stdout.splitlines()
        failures = issue_acceptance.validate(
            review_acceptance_report(review_log, worker=worker),
            issue_number, reviewed_head, body, changed,
            non_closing=non_closing,
        )
        if failures:
            return return_to_review(
                pr_number=pr_number, branch=branch, worker=worker,
                reviewer=worker, verdict="not-ready", excerpt=excerpt,
                note="Approval rejected: issue acceptance evidence incomplete: "
                     + "; ".join(failures[:8]),
                status="acceptance-evidence-required", head=reviewed_head,
                producer=producer,
            )
        accepted_contract_marker = issue_acceptance.marker(
            issue_number, reviewed_head, body
        )

    if verdict in {"repair", "reject"} and not has_actionable_review_findings(excerpt):
        return return_to_review(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            reviewer=worker,
            verdict="not-ready",
            excerpt="",
            note=(
                "Semantic REPAIR/REJECT did not include an actionable prose diagnosis. "
                "Raw diffs, source code, inspection proof and terminal markers are not "
                "durable repair findings; a fresh independent review is required."
            ),
            status="insufficient-review-findings",
            head=reviewed_head,
            producer=producer,
        )

    marker = review_marker(
        pr=pr_number,
        head=reviewed_head,
        reviewer=worker,
        producer=producer,
        verdict=verdict,
    )

    if verdict == "repair":
        prior_repair_heads = semantic_repair_heads(
            review_comment_bodies(pr_number),
            pr=pr_number,
        )
        if (
            producer is not None
            and reviewed_head not in prior_repair_heads
            and len(prior_repair_heads) >= FACTORY_REPAIR_CYCLE_LIMIT
        ):
            if not has_actionable_review_findings(excerpt):
                raise RuntimeError("strike-limit repair requires durable actionable review findings")
            post_review_comment(
                pr_number=pr_number,
                marker=marker,
                reviewer=worker,
                verdict="repair",
                excerpt=excerpt,
                note=(
                    "Fourth distinct semantic repair cycle reached. This implementation attempt "
                    "is cancelled; the linked issue is returned for a clean implementation."
                ),
            )
            issue = linked_issue_from_branch(branch)
            if issue is not None:
                excluded = producer or "unknown"
                strike_marker = (
                    f"<!-- {STRIKE_RESET_MARKER}:issue-{issue}:pr-{pr_number}:"
                    f"excluded-producer-{excluded} -->"
                )
                run_gh(
                    [
                        "issue",
                        "comment",
                        str(issue),
                        "--repo",
                        REPO,
                        "--body",
                        strike_marker,
                    ]
                )
            transition_pr_and_linked_issue(
                pr_number=pr_number,
                branch=branch,
                worker=worker,
                pr_stage="factory:blocked",
                issue_stage="factory:building",
            )
            run_gh(["pr", "close", str(pr_number), "--repo", REPO])
            return {
                "status": "strike-limit-cancelled",
                "head": reviewed_head,
                "producer": producer,
                "repair_cycles": len(prior_repair_heads) + 1,
            }
        persist_repair_handoff(
            pr_number=pr_number,
            findings=excerpt,
            marker=marker,
            reviewer=worker,
            note=(
                f"Semantic blockers remain. Repair cycle "
                f"{len(prior_repair_heads | {reviewed_head})}/{FACTORY_REPAIR_CYCLE_LIMIT}; "
                "the PR is returning to repair."
            ),
            branch=branch,
            worker=worker,
        )
        return {
            "status": "repair",
            "head": reviewed_head,
            "producer": producer,
            "repair_cycles": len(prior_repair_heads | {reviewed_head}),
        }

    if verdict == "reject":
        if not has_actionable_review_findings(excerpt):
            raise RuntimeError("semantic rejection requires durable actionable review findings")
        post_review_comment(
            pr_number=pr_number,
            marker=marker,
            reviewer=worker,
            verdict=verdict,
            excerpt=excerpt,
            note=(
                "The independent reviewer classified this factory PR as unsalvageable. "
                "The linked issue remains available for a clean implementation."
            ),
        )
        transition_pr_and_linked_issue(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            pr_stage="factory:blocked",
            issue_stage="factory:building",
        )
        run_gh(["pr", "close", str(pr_number), "--repo", REPO])
        return {"status": "rejected", "head": reviewed_head, "producer": producer}

    # Handle terminal verdicts that result in PR closure
    if verdict in {"obsolete", "duplicate", "superseded", "delivered"}:
        if not has_actionable_review_findings(excerpt):
            raise RuntimeError(f"semantic {verdict} requires durable actionable review findings")
        
        # Map verdicts to human-readable reasons
        reason_messages = {
            "obsolete": "This PR is obsolete and no longer needed",
            "duplicate": "This PR is a duplicate of other work",
            "superseded": "This PR has been superseded by other changes",
            "delivered": "The changes targeted by this PR are already delivered on main"
        }
        
        post_review_comment(
            pr_number=pr_number,
            marker=marker,
            reviewer=worker,
            verdict=verdict,
            excerpt=excerpt,
            note=(
                f"The independent reviewer classified this factory PR as {verdict}. "
                f"{reason_messages[verdict]}. "
                "The linked issue remains available for a clean implementation."
            ),
        )
        
        # Close the PR with terminal state reconciliation
        run_gh(["pr", "close", str(pr_number), "--repo", REPO])
        
        # Record terminal reason marker for future reconciliation
        terminal_marker = (
            f"<!-- comic-pile-factory-terminal-state-v1:"
            f"pr-{pr_number}:head-{reviewed_head}:"
            f"verdict-{verdict}:reason-{reason_messages[verdict]} -->"
        )
        run_gh([
            "issue", 
            "comment", 
            str(linked_issue_from_branch(branch) or pr_number),
            "--repo", 
            REPO,
            "--body", 
            terminal_marker,
        ])
        
        return {
            "status": f"terminal-{verdict}", 
            "head": reviewed_head, 
            "producer": producer,
            "reason": reason_messages[verdict]
        }

    post_review_comment(
        pr_number=pr_number,
        marker=marker,
        reviewer=worker,
        verdict=verdict,
        excerpt=excerpt,
        note="Semantic approval is scoped to this exact reviewed PR head and linked issue revision.",
        acceptance_marker=accepted_contract_marker,
    )
    # A head created by hand or by a local agent has no numbered wrapper
    # contribution record, so establish its exact-head author attestation here
    # from trusted evidence instead of stranding it. This is what makes such a
    # head reviewable exactly like a wrapper-pushed one.
    operation = "attest_exact_head_author"
    try:
        attest_exact_head_author(pr_number=pr_number, head=reviewed_head, pr=pr)
        operation = "persist_cross_model_review_marker"
        persist_cross_model_review_marker(
            pr_number=pr_number,
            head=reviewed_head,
            reviewer=worker,
        )
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        # A staged dependency or provenance-write failure is controller
        # infrastructure work, not a reason to run another model against the
        # same approved head. Preserve the approval, fail closed, and leave
        # enough redacted evidence to repair the trusted bundle/write path.
        exc_name = type(exc).__name__
        redacted = redact_review_text(str(exc))
        # Keep exception name and operation outside the limit; retain the
        # tail of the message which has the specific failure details.
        detail_suffix = redacted[-480:] if len(redacted) > 480 else redacted
        detail = f"{exc_name} in {operation}: {detail_suffix}"
        return return_to_review(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            reviewer=worker,
            verdict=verdict,
            excerpt="",
            note=(
                "Blocked: trusted controller provenance could not be recorded. "
                f"operation={operation}. {detail}. Repair the trusted "
                "controller/roster bundle or GitHub provenance-write access, "
                "then reconcile this exact head. "
                "Do not re-review this unchanged head while the infrastructure "
                "failure remains."
            ),
            status="provenance-infrastructure-blocked",
            head=reviewed_head,
            producer=producer,
            stage="factory:blocked",
        )

    # A semantic approval alone cannot make this head mergeable. If an agent
    # bypassed the wrapper's trusted contribution record, the cross-model
    # gate cannot establish the author and MUST fail closed instead of sending
    # the same unchanged head through another costly reviewer session.
    if not head_has_cross_model_review(
        model_comment_bodies(pr_number), pr=pr_number, head=reviewed_head
    ):
        return return_to_review(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            reviewer=worker,
            verdict=verdict,
            excerpt="",
            note=(
                "Blocked: exact-head cross-model author/reviewer provenance is "
                "missing or invalid. Exact-head authorship is attested "
                "automatically when the head commit identity is trusted, so this "
                "head has no attributable author: verify the trusted wrapper's "
                "contributor recording, or whether the head commit is attributed "
                "to a foreign account or carries conflicting `Model:` trailers. "
                "Do not re-review this unchanged head; an owner-authored head "
                "that can be reconciled explicitly may be attested from trusted "
                "main with `factory-review-controller.py reconcile-provenance "
                f"--pr {pr_number}`."
            ),
            status="cross-model-provenance-blocked",
            head=reviewed_head,
            producer=producer,
            marker=marker,
            stage="factory:blocked",
        )

    mechanical = mechanical_merge_gate(pr_number, reviewed_head)
    latest = pr_json(pr_number)
    latest_head = str(latest.get("headRefOid") or "")

    if mechanical["decision"] == "retry":
        # CI is pending or otherwise undecidable at this exact head. Park the
        # approved PR at factory:ci where the dispatcher's reconcile-ci polls
        # cheaply, instead of burning another semantic review pass on the
        # same code.
        result = return_to_review(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            reviewer=worker,
            verdict=verdict,
            excerpt="",
            note=(
                "Semantic approval is preserved for this exact head, but mechanical gates "
                f"are not yet decidable: {mechanical['reason']}. Parked at factory:ci for "
                "cheap reconciliation instead of another review pass."
            ),
            status="approved-deferred",
            head=latest_head or reviewed_head,
            producer=producer,
            marker=marker,
            stage="factory:ci",
        )
        result["mechanical"] = mechanical
        return result

    mechanical_passed = mechanical["decision"] == "pass"
    if not mechanical_passed:
        # Mechanical gates genuinely failed (mergeability conflict or failing
        # required checks). Route to the repair stage rather than re-reviewing
        # identical code with another expensive model session.
        persist_repair_handoff(
            pr_number=pr_number,
            findings=str(mechanical["reason"]),
            branch=branch,
            worker=worker,
            reviewer=worker,
            note=(
                "Semantic approval recorded, but exact-head mechanical gates failed. "
                "The PR is returning to repair."
            ),
        )
        return {
            "status": "approved-mechanical-failure",
            "head": latest_head or reviewed_head,
            "producer": producer,
            "mechanical": mechanical,
        }

    # Fetch after posting so concurrent approvals are observed atomically. The
    # contributor set is re-read from the same snapshot: a repairer's record for
    # this exact head can land while the reviewer is working, and promotion must
    # not reuse the pre-post view that could not see it.
    promoted_comments = review_comment_bodies(pr_number)
    prior_approvers = current_head_approvers(
        promoted_comments,
        pr=pr_number,
        head=reviewed_head,
    )
    contributors, provenance_complete = exact_head_provenance(
        reviewer_comments=promoted_comments,
        model_comments=model_comment_bodies(pr_number),
        pr_number=pr_number,
        head=reviewed_head,
        producer=producer,
    )
    authorized = approval_can_promote(
        contributors=contributors,
        provenance_complete=provenance_complete,
        reviewer=worker,
        reviewed_head=reviewed_head,
        current_head=latest_head,
        verdict=verdict,
        mechanical_gates_passed=mechanical_passed,
        prior_approvers=prior_approvers,
    )
    if not authorized:
        note = (
            "Contributor provenance is not recorded for this head, so two distinct "
            "independent factory approvals are required before it can be ready."
            if not provenance_complete
            else f"Approval was denied by controller-side gates: {mechanical['reason']}."
        )
        result = return_to_review(
            pr_number=pr_number,
            branch=branch,
            worker=worker,
            reviewer=worker,
            verdict=verdict,
            excerpt="",
            note=note,
            status="approved-not-ready",
            head=latest_head or reviewed_head,
            producer=producer,
        )
        result["mechanical"] = mechanical
        return result

    transition_pr_and_linked_issue(
        pr_number=pr_number,
        branch=branch,
        worker=worker,
        pr_stage="factory:ready",
    )
    # The controller has now validated independent exact-head acceptance,
    # cross-model provenance, contributor independence, and mechanical gates.
    # GitHub does not dispatch issue_comment workflows for GITHUB_TOKEN posts,
    # so project informational approval in this same trusted controller run.
    try:
        set_review_approval_visibility(pr_number, reviewed_head)
    except RuntimeError as exc:
        print(f"PR #{pr_number}: approved label sync unavailable: {exc}",
              file=sys.stderr)
    return {
        "status": "ready",
        "head": reviewed_head,
        "producer": producer,
        "mechanical": mechanical,
    }


def demote_ready(pr_number: int) -> dict[str, Any]:
    """Demote a stuck factory:ready PR so its merge slot is released."""
    pr = pr_json(pr_number)
    if str(pr.get("state")) != "OPEN":
        raise RuntimeError(f"PR #{pr_number} is not open")
    if "factory:ready" not in labels_of(pr):
        raise RuntimeError(f"PR #{pr_number} is not in factory:ready")

    branch = str(pr.get("headRefName") or "")
    replace_factory_labels(pr_number, "factory:unowned", "factory:changes-requested")
    issue = linked_issue_from_branch(branch)
    if issue is not None:
        try:
            issue_target = target_json(issue)
        except RuntimeError:
            issue_target = None
        if (
            issue_target
            and issue_target.get("state") == "open"
            and "factory:ready" in labels_of(issue_target)
        ):
            replace_factory_labels(issue, "factory:unowned", "factory:changes-requested")

    return {"status": "demoted", "pr": pr_number}


def reconcile_terminal_pr(pr_number: int) -> dict[str, Any]:
    """Reconcile one terminal PR using authoritative semantic review results."""
    pr = pr_json(pr_number)
    labels = labels_of(pr)
    
    # Skip if PR is not open or doesn't have factory labels
    if str(pr.get("state")) != "OPEN" or "factory" not in labels:
        return {"pr": pr_number, "status": "skipped"}
    
    # Check for existing terminal verdict markers
    comments = review_comment_bodies(pr_number)
    terminal_verdict = None
    terminal_head = None
    terminal_reason = None
    
    for comment in comments:
        first_line = str(comment or "").splitlines()[0] if comment else ""
        marker = parse_review_marker(first_line)
        if marker and marker["verdict"] in {"obsolete", "duplicate", "superseded", "delivered"}:
            terminal_verdict = marker["verdict"]
            terminal_head = marker["head"]
            terminal_reason = f"Terminal {terminal_verdict} verdict"
            break
    
    # No terminal verdict found, skip
    if terminal_verdict is None:
        return {"pr": pr_number, "status": "no-terminal-verdict"}
    
    # Verify the terminal verdict still applies to the current head
    current_head = str(pr.get("headRefOid") or "")
    if current_head != terminal_head:
        return {"pr": pr_number, "status": "stale-terminal-verdict", "current_head": current_head, "terminal_head": terminal_head}
    
    # Close the PR with terminal state reconciliation
    try:
        run_gh(["pr", "close", str(pr_number), "--repo", REPO])
        
        # Remove incompatible factory state
        replace_factory_labels(pr_number, "factory:unowned", "factory:unowned")
        
        # Record durable terminal reason marker
        terminal_marker = (
            f"<!-- comic-pile-factory-terminal-state-v1:"
            f"pr-{pr_number}:head-{current_head}:"
            f"verdict-{terminal_verdict}:reason-{terminal_reason} -->"
        )
        run_gh([
            "issue", 
            "comment", 
            str(linked_issue_from_branch(pr.get("headRefName")) or pr_number),
            "--repo", 
            REPO,
            "--body", 
            terminal_marker,
        ])
        
        return {
            "pr": pr_number, 
            "status": "terminal-closed", 
            "verdict": terminal_verdict,
            "reason": terminal_reason,
            "head": current_head
        }
        
    except RuntimeError as exc:
        return {"pr": pr_number, "status": "reconciliation-failed", "error": str(exc)}


def authorize_ready(pr_number: int) -> dict[str, Any]:
    """Validate that a ready PR still has authorization for its current head."""
    pr = pr_json(pr_number)
    branch = str(pr.get("headRefName") or "")
    head = str(pr.get("headRefOid") or "")
    producer = producer_worker_from_pr(branch=branch, body=str(pr.get("body") or ""))
    comments = review_comment_bodies(pr_number)
    model_comments = model_comment_bodies(pr_number)
    approvers = current_head_approvers(comments, pr=pr_number, head=head)
    contributors, provenance_complete = exact_head_provenance(
        reviewer_comments=comments,
        model_comments=model_comments,
        pr_number=pr_number,
        head=head,
        producer=producer,
    )
    cross_model = head_has_cross_model_review(
        model_comments,
        pr=pr_number,
        head=head,
    )
    authorized = (
        str(pr.get("state")) == "OPEN"
        and "factory:ready" in labels_of(pr)
        and HEAD_RE.fullmatch(head) is not None
        and head_has_authorized_approval(
            contributors=contributors,
            approvers=approvers,
            provenance_complete=provenance_complete,
        )
        and cross_model
        and issue_acceptance_gate(pr_number, head)["decision"] == "pass"
    )
    if authorized:
        return {
            "authorized": True,
            "head": head,
            "producer": producer,
            "contributors": sorted(contributors),
            "provenance_complete": provenance_complete,
            "approvers": sorted(approvers),
            "cross_model_review": True,
        }

    if str(pr.get("state")) == "OPEN" and "factory:ready" in labels_of(pr):
        replace_factory_labels(pr_number, "factory:unowned", "factory:review")
        issue = linked_issue_for_pr(pr_number, branch)
        if issue is not None:
            try:
                issue_target = target_json(issue)
            except RuntimeError:
                issue_target = None
            if (
                issue_target
                and issue_target.get("state") == "open"
                and "factory:ready" in labels_of(issue_target)
            ):
                replace_factory_labels(issue, "factory:unowned", "factory:review")

    return {
        "authorized": False,
        "head": head,
        "producer": producer,
        "contributors": sorted(contributors),
        "provenance_complete": provenance_complete,
        "approvers": sorted(approvers),
        "cross_model_review": cross_model,
    }


def record_contribution(*, worker: str, pr_number: int, head: str) -> dict[str, Any]:
    """Record one factory worker's contribution to one exact PR head.

    Worker wrappers call this immediately after every push that changes a PR
    head. The controller re-reads the authoritative PR head, refuses to record a
    stale SHA, and writes the marker as the entire comment body so worker PR
    body text, commit messages, resume packets, and review prose can never be
    read as trusted contributor provenance.

    A worker records the commit its own push just created, and GitHub publishes
    the new ref asynchronously, so the first read can still report the previous
    head. That lag is transient rather than a genuine stale push, so the head
    match is retried a bounded number of times before the record is abandoned.
    Losing the record entirely is survivable (the head then needs two distinct
    reviewers), but losing it to normal ref propagation would quietly halve
    review throughput on every repaired pull request.
    """
    if not FIXED_WORKER_RE.fullmatch(worker):
        raise RuntimeError(f"unsupported fixed-model worker: {worker}")
    if not HEAD_RE.fullmatch(head):
        raise RuntimeError("head must be a full lowercase Git SHA")

    mismatch = f"PR #{pr_number} head did not reach expected {head}"
    for attempt in range(CONTRIBUTION_HEAD_SYNC_ATTEMPTS):
        pr = pr_json(pr_number)
        if str(pr.get("state")) != "OPEN":
            raise RuntimeError(f"PR #{pr_number} is not open")
        if "factory" not in labels_of(pr):
            raise RuntimeError(f"PR #{pr_number} is not a factory pull request")
        current_head = str(pr.get("headRefOid") or "")
        if current_head == head:
            epoch = int(time.time())
            marker = head_contributor_marker(
                pr=pr_number,
                head=head,
                worker=worker,
                epoch=epoch,
            )
            run_gh(["issue", "comment", str(pr_number), "--repo", REPO, "--body", marker])
            author_marker = persist_author_model_marker(
                pr_number=pr_number,
                head=head,
                worker=worker,
            )
            return {
                "status": "recorded",
                "pr": pr_number,
                "head": head,
                "worker": worker,
                "epoch": epoch,
                "author_model_marker": author_marker,
            }
        mismatch = f"PR #{pr_number} head {current_head} does not match expected {head}"
        if attempt + 1 < CONTRIBUTION_HEAD_SYNC_ATTEMPTS:
            time.sleep(CONTRIBUTION_HEAD_SYNC_INTERVAL_SECONDS)
    raise RuntimeError(mismatch)


def main() -> int:
    """Run semantic review controller commands."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review")
    review.add_argument("--worker", required=True)
    review.add_argument("--pr", type=int, required=True)
    review.add_argument("--reviewed-head", required=True)
    review.add_argument(
        "--verdict",
        choices=("approve", "repair", "reject"),
        required=True,
    )
    review.add_argument("--review-log")

    authorized = subparsers.add_parser("authorized")
    authorized.add_argument("--pr", type=int, required=True)

    demote = subparsers.add_parser("demote-ready")
    demote.add_argument("--pr", type=int, required=True)

    gates = subparsers.add_parser("gates")
    gates.add_argument("--pr", type=int, required=True)
    gates.add_argument("--expected-head", required=True)

    record = subparsers.add_parser("record-contribution")
    record.add_argument("--worker", required=True)
    record.add_argument("--pr", type=int, required=True)
    record.add_argument("--head", required=True)

    reconcile_provenance_parser = subparsers.add_parser("reconcile-provenance")
    reconcile_provenance_parser.add_argument("--pr", type=int, required=True)
    reconcile_provenance_parser.add_argument("--head")

    subparsers.add_parser("reconcile-ci")
    subparsers.add_parser("reconcile-terminal")

    args = parser.parse_args()
    if args.command == "review":
        result = handle_review(
            worker=args.worker,
            pr_number=args.pr,
            verdict=args.verdict,
            reviewed_head=args.reviewed_head,
            review_log=args.review_log,
        )
    elif args.command == "authorized":
        result = authorize_ready(args.pr)
    elif args.command == "demote-ready":
        result = demote_ready(args.pr)
    elif args.command == "record-contribution":
        result = record_contribution(
            worker=args.worker,
            pr_number=args.pr,
            head=args.head,
        )
    elif args.command == "reconcile-provenance":
        if args.head is not None and not HEAD_RE.fullmatch(args.head):
            raise RuntimeError("head must be a full lowercase Git SHA")
        result = reconcile_provenance(args.pr, args.head)
    elif args.command == "reconcile-ci":
        result = {"results": reconcile_ci()}
    elif args.command == "reconcile-terminal":
        result = {"results": [reconcile_terminal_pr(args.pr)]}
    else:
        if not HEAD_RE.fullmatch(args.expected_head):
            raise RuntimeError("expected head must be a full lowercase Git SHA")
        result = mechanical_merge_gate(args.pr, args.expected_head)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
