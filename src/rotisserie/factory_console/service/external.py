"""Authorized external-agent claims over the existing Neon target-lease ledger.

A frontier observation is never authority. Recheck repository permissions, live
GitHub state and dependency edges before and after the transactional CAS. No
code-writing agent is enrolled in the provider/model roster.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import ModuleType

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import frontier, queue
from rotisserie.factory_console.service import sync as branch_sync

ACTIONS = frozenset({"implement", "review", "review-findings", "ci-repair", "merge-conflict"})
CLAIM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
CLIENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
EVIDENCE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull|actions/runs)/[1-9][0-9]*(?:#(?:issuecomment|discussion_r|pullrequestreview)-[0-9]+)?\Z"
)


def ledger() -> ModuleType:
    """Load the exact shared factory_state_ledger, never a separate claim store."""
    module, problem = branch_sync.load_ledger()
    if module is None:
        raise RuntimeError(problem)
    if not all(
        hasattr(module, attr)
        for attr in (
            "reserve_external",
            "external_lease_action",
            "external_wip_count",
        )
    ):
        raise RuntimeError("installed Rotisserie factory runtime lacks external claim authority")
    return module


def principal(github: GitHub, environment: dict[str, str]) -> str:
    """Bind identity to the authenticated GitHub account or trusted Actions actor.

    Write-capable repository permission is required even if the Neon
    connection is present. Errors and revoked permissions hold closed.
    """
    if environment.get("GITHUB_ACTIONS") == "true":
        if (
            environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environment.get("GITHUB_REPOSITORY") != github.repo
            or environment.get("GITHUB_RUN_ATTEMPT") != "1"
            or environment.get("GITHUB_TRIGGERING_ACTOR", environment.get("GITHUB_ACTOR"))
            != environment.get("GITHUB_ACTOR")
        ):
            raise ValueError("external claim requires an original authorized workflow dispatch")
        actor = environment.get("GITHUB_ACTOR", "")
    else:
        actor = github.whoami()
    if not CLIENT.fullmatch(actor):
        raise ValueError("GitHub identity could not be verified")
    permission = string(obj(github.repo_api(f"collaborators/{actor}/permission")), "permission")
    if permission not in ("admin", "maintain", "write"):
        raise ValueError("GitHub actor lacks verified write access to this repository")
    return actor


def _linked_targets(github: GitHub, offer: frontier.Offer) -> list[int]:
    kind, number_text = offer.target.split(":")
    number = int(number_text)
    if kind == "issue":
        live = obj(github.repo_api(f"issues/{number}"))
        if string(live, "state") != "open" or "pull_request" in live:
            raise ValueError("issue was closed or replaced by a pull request")
        return [number]
    live = obj(github.repo_api(f"pulls/{number}"))
    if string(live, "state") != "open":
        raise ValueError("pull request was closed")
    head = string(obj(live.get("head")), "sha")
    if head != offer.head_sha:
        raise ValueError("pull request head changed")
    core = runtime.module("factory_work_policy")
    link = core.linked_issue_from_pr(
        {
            "number": number,
            "headRefName": string(obj(live.get("head")), "ref"),
            "body": string(live, "body"),
        }
    )
    if link is None:
        return [number]
    issue = obj(github.repo_api(f"issues/{link}"))
    if string(issue, "state") != "open" or "pull_request" in issue:
        return [number]
    return [int(link), number] if link != number else [number]


def _live_offer(github: GitHub, previous: frontier.Offer) -> tuple[bool, str]:
    """Reject moved heads, priority/stage changes, new dependencies and holds."""
    latest = frontier.capture(github, now=datetime.now(UTC))
    if not latest.authoritative_claim_available:
        return False, "Neon ownership became unavailable"
    found = next((o for o in latest.offers if o.target == previous.target), None)
    if found is None:
        return False, "offer disappeared from current controller ranking"
    if (
        found.action != previous.action
        or found.head_sha != previous.head_sha
        or found.priority != previous.priority
        or found.lane != previous.lane
    ):
        return False, "head, priority, stage or action changed"
    if found.status != "candidate":
        return False, "offer no longer eligible: " + "; ".join(found.hold_reasons)
    higher = [o for o in latest.offers if o.rank < found.rank and o.status == "candidate"]
    if higher:
        return False, "a higher-ranked eligible action appeared"
    kind, number_text = previous.target.split(":")
    if kind == "issue":
        blocked, problem = queue.open_blocker(github, int(number_text))
        if blocked:
            return False, problem or "issue dependencies changed"
    return True, ""


def reserve_next(
    github: GitHub,
    *,
    client: str,
    environment: dict[str, str],
    abilities: frozenset[str] = ACTIONS,
    expected: str = "",
    advertised_at: str = "",
) -> dict[str, object]:
    if not CLIENT.fullmatch(client):
        raise ValueError("client must be a stable non-secret identifier")
    if not abilities or not abilities <= ACTIONS:
        raise ValueError("unsupported agent abilities")
    actor = principal(github, environment)
    store = ledger()
    snapshot = frontier.capture(github, now=datetime.now(UTC))
    if not snapshot.authoritative_claim_available:
        return {"status": "held", "reason": snapshot.authority_note, "retry": "refresh_frontier"}
    if advertised_at:
        try:
            offered_at = datetime.fromisoformat(advertised_at.replace("Z", "+00:00"))
            seconds_old = (datetime.now(UTC) - offered_at).total_seconds()
        except ValueError:
            seconds_old = 999999
        if not 0 <= seconds_old <= 60:
            return {
                "status": "conflict",
                "reason": "offer_snapshot_expired",
                "retry": "refresh_frontier",
            }
    candidates = [
        o
        for o in snapshot.offers
        if o.status == "candidate"
        and o.action in abilities
        and not (
            o.action == "review"
            and (
                client in o.provenance.get("head_contributors", [])
                or actor in o.provenance.get("head_contributors", [])
                or client == o.provenance.get("producer")
            )
        )
    ]
    if not candidates:
        return {"status": "held", "reason": "no_eligible_offer", "retry": "refresh_frontier"}
    chosen = candidates[0]
    if expected and chosen.target != expected:
        return {
            "status": "conflict",
            "reason": "offer_reordered_or_no_longer_highest",
            "expected": expected,
            "current": chosen.target,
            "retry": "refresh_frontier",
        }
    okay, reason = _live_offer(github, chosen)
    if not okay:
        return {"status": "conflict", "reason": reason, "retry": "refresh_frontier"}
    numbers = _linked_targets(github, chosen)
    outcome: dict[str, object] = store.reserve_external(
        github.repo,
        actor,
        client,
        numbers,
        target=chosen.target,
        action=chosen.action,
        head=chosen.head_sha,
        observed_wip=chosen.capacity["pr_wip"],
        wip_limit=chosen.capacity["wip_limit"],
        offer_at=snapshot.generated_at,
    )
    if outcome.get("status") != "reserved":
        return outcome
    # Verify after cross-system CAS. A changed head or rank invalidates the
    # newly acquired token; only our own identity-fenced release can undo it.
    claim_id = str(outcome["claim_id"])
    try:
        live = obj(
            github.repo_api(
                f"pulls/{chosen.target.split(':')[1]}"
                if chosen.target.startswith("pr:")
                else f"issues/{chosen.target.split(':')[1]}"
            )
        )
        if (
            chosen.target.startswith("pr:")
            and string(obj(live.get("head")), "sha") != chosen.head_sha
        ):
            raise ValueError("pull request head changed after reservation")
        if string(live, "state") != "open":
            raise ValueError("target closed after reservation")
    except (GitHubError, ValueError) as exc:
        store.external_lease_action(
            github.repo,
            actor,
            client,
            claim_id,
            operation="release",
            evidence="post-reservation-revalidation-failed",
        )
        return {"status": "conflict", "reason": str(exc), "retry": "refresh_frontier"}
    outcome["handoff"] = {
        "repository": github.repo,
        "url": chosen.url,
        "target": chosen.target,
        "action": chosen.action,
        "goal": chosen.instruction,
        "prerequisites": list(chosen.prerequisites),
        "blocked_by": list(chosen.blocked_by),
        "head_sha": chosen.head_sha,
        "claim_id": claim_id,
        "client": client,
        "actor": actor,
        "renew": f"rotisserie factory renew {claim_id} --client {client} --repo {github.repo}",
        "release": f"rotisserie factory release {claim_id} --client {client} --repo {github.repo}",
        "report": chosen.url,
        "review_and_merge_gates": "Existing exact-head independent review, CI and acceptance remain required",
    }
    return outcome


def claim_action(
    github: GitHub,
    *,
    client: str,
    claim_id: str,
    operation: str,
    environment: dict[str, str],
    evidence: str = "",
) -> dict[str, object]:
    if not CLIENT.fullmatch(client) or not CLAIM_ID.fullmatch(claim_id):
        raise ValueError("invalid client or claim identity")
    if operation == "handoff" and (
        not EVIDENCE.fullmatch(evidence)
        or not evidence.startswith(f"https://github.com/{github.repo}/")
    ):
        raise ValueError("handoff evidence must be a same-repository GitHub URL")
    actor = principal(github, environment)
    store = ledger()
    result: dict[str, object] = store.external_lease_action(
        github.repo, actor, client, claim_id, operation=operation, evidence=evidence
    )
    return result
