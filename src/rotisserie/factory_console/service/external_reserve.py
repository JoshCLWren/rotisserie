"""Claim packet, finished-work recovery and rollback used by reserve-next."""

from __future__ import annotations

import re
from types import ModuleType
from typing import Any

from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import frontier
from rotisserie.factory_console.service.external_handoff import WORKFLOW

CLAIM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


EVIDENCE_REQUIRED = {
    "implement": (
        "Open a non-draft pull request in this repository whose body says 'Closes #N' "
        "for the claimed issue, then hand off that pull request's URL"
    ),
    "review-findings": (
        "Push commits resolving the review findings to this pull request's branch, "
        "then hand off the pull request's URL"
    ),
    "ci-repair": (
        "Push commits that make this pull request's own failing checks pass, "
        "then hand off the pull request's URL"
    ),
    "merge-conflict": (
        "Push a commit resolving the conflict with the base branch, "
        "then hand off the pull request's URL"
    ),
    "review": (
        "Submit a formal GitHub review (Approve or Request changes) on this exact head. "
        "Its body needs one 'Model: <your exact model id>' line; an approval also needs "
        "the issue's FACTORY_ACCEPTANCE_V1= report. Then hand off the review's URL"
    ),
}


def handoff_packet(
    github: GitHub,
    offer: frontier.Offer,
    *,
    claim_id: str,
    client: str,
    actor: str,
    expires_at: str,
    title: str,
    acceptance: str,
) -> dict[str, object]:
    """Portable, secret-free task packet for whichever agent won the claim."""
    repo = github.repo
    cli = f"--client {client} --repo {repo}"
    bridge = (
        f"gh workflow run {WORKFLOW} --repo {repo} --ref main "
        f"-f client={client} -f claim_id={claim_id}"
    )
    return {
        "schema_version": 1,
        "repository": repo,
        "target": offer.target,
        "url": offer.url,
        "task_title": title,
        "action": offer.action,
        "goal": offer.instruction,
        "rank": offer.rank,
        "priority": offer.priority,
        "lane": offer.lane,
        "acceptance_criteria": acceptance[:12000],
        "prerequisites": list(offer.prerequisites),
        "blocked_by": list(offer.blocked_by),
        "head_sha": offer.head_sha,
        "claim_id": claim_id,
        "client": client,
        "actor": actor,
        "expires_at": expires_at,
        "renew": f"baste renew {claim_id} {cli}",
        "release": f"baste release {claim_id} {cli}",
        "renew_github": f"{bridge} -f operation=renew",
        "release_github": f"{bridge} -f operation=release",
        "handoff_github": f"{bridge} -f operation=handoff -f evidence=URL",
        "renew_before": "Renew well before expires_at; an expired claim can only be released",
        "evidence_required": EVIDENCE_REQUIRED.get(offer.action, ""),
        "provenance": (
            "End every commit message with a 'Model: <your exact model id>' trailer. "
            "The factory reads it so the model that wrote this work never approves it."
        ),
        "report": offer.url,
        "review_and_merge_gates": (
            "Handoff is not approval. Existing exact-head independent review, required CI, "
            "acceptance, merge authorization and production verification still decide."
        ),
    }


def _target_closed(github: GitHub, number: int) -> bool:
    """GitHub's own answer on whether the issue or PR is finished (closed/merged)."""
    return string(obj(github.repo_api(f"issues/{number}")), "state") == "closed"


def reconcile(github: GitHub, *, actor: str, store: ModuleType) -> list[dict[str, object]]:
    """Remove outside-agent claims whose work GitHub says no longer exists.

    A lapsed timestamp never proves an agent stopped, so expiry alone frees
    nothing. A claim is recovered only on authoritative evidence that its work
    is gone: every target it holds is closed or merged on GitHub. The removal
    is UUID-fenced and logged like an operator revoke. Any read failure leaves
    the claim in place.
    """
    removed: list[dict[str, object]] = []
    marker = f"factory-lease-v1:{github.repo}:external:"
    try:
        rows: list[dict[str, Any]] = store.list_leases(github.repo)
    except Exception:  # noqa: BLE001 - unknown state keeps every claim
        return removed
    for row in rows:
        if not str(row.get("key", "")).startswith(marker):
            continue
        claim_id = str(row.get("claim_id", ""))
        targets = [n for n in row.get("targets") or [] if isinstance(n, int)]
        if not CLAIM_ID.fullmatch(claim_id) or not targets:
            continue
        try:
            finished = all(_target_closed(github, number) for number in targets)
        except GitHubError:
            continue
        if not finished:
            continue
        closed = ", ".join(f"#{n}" for n in targets)
        result: dict[str, object] = store.revoke_external(
            github.repo, actor, claim_id, reason=f"work finished on GitHub: {closed} closed"
        )
        if result.get("status") == "revoked":
            removed.append(result)
    return removed


def _rollback(
    store: ModuleType, github: GitHub, actor: str, client: str, claim_id: str, why: str
) -> bool:
    """Release our own just-acquired claim; False when that cannot be confirmed."""
    try:
        result: dict[str, object] = store.external_lease_action(
            github.repo, actor, client, claim_id, operation="release", evidence=why
        )
    except Exception:  # noqa: BLE001 - the caller reports the claim it still holds
        return False
    return result.get("status") == "released"
