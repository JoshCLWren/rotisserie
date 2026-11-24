"""Authorized external-agent claims over the existing Neon target-lease ledger.

A frontier observation is never authority. Recheck repository permissions, live
GitHub state and dependency edges before and after the transactional CAS. No
code-writing agent is enrolled in the provider/model roster.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from types import ModuleType

from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import frontier
from rotisserie.factory_console.service import sync as branch_sync
from rotisserie.factory_console.service.external_checks import (
    _bypasses_wip,
    _post_claim_change,
)
from rotisserie.factory_console.service.external_checks import (
    _conflict_without_ci as _conflict_without_ci,
)
from rotisserie.factory_console.service.external_checks import (
    _linked_targets as _linked_targets,
)
from rotisserie.factory_console.service.external_checks import (
    _live_offer as _live_offer,
)
from rotisserie.factory_console.service.external_checks import (
    _required_checks_outcome as _required_checks_outcome,
)
from rotisserie.factory_console.service.external_checks import (
    claim_scope as claim_scope,
)
from rotisserie.factory_console.service.external_checks import (
    live_now as live_now,
)
from rotisserie.factory_console.service.external_handoff import EVIDENCE, _evidence_pr
from rotisserie.factory_console.service.external_handoff import (
    _closed_issues as _closed_issues,
)
from rotisserie.factory_console.service.external_handoff import (
    _verify_and_project_handoff as _verify_and_project_handoff,
)
from rotisserie.factory_console.service.external_reserve import (
    CLAIM_ID,
    _rollback,
    handoff_packet,
)
from rotisserie.factory_console.service.external_reserve import (
    EVIDENCE_REQUIRED as EVIDENCE_REQUIRED,
)
from rotisserie.factory_console.service.external_reserve import (
    reconcile as reconcile,
)

# Work an outside agent can finish through the existing factory gates. A
# review counts once the trusted bridge records the agent's declared model
# (#191); the bridge refuses a model that authored the PR.
ACTIONS = frozenset({"implement", "review", "review-findings", "ci-repair", "merge-conflict"})
# Events whose workflow file GitHub always takes from the default branch, so a
# pull request cannot change the code that receives the claim store secret.
LABEL_EVENTS = ("issues", "pull_request_target", "issue_comment")
CLIENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


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
            "list_leases",
            "revoke_external",
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
            environment.get("GITHUB_EVENT_NAME") not in ("workflow_dispatch", *LABEL_EVENTS)
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
    # Lease keys and the advisory lock are built from this exact name; another
    # spelling of the same repository would get a separate lock and keys.
    canonical = string(obj(json.loads(github.gh("api", f"repos/{github.repo}"))), "full_name")
    if canonical != github.repo:
        raise ValueError(f"use the repository's exact name: --repo {canonical or '?'}")
    permission = string(obj(github.repo_api(f"collaborators/{actor}/permission")), "permission")
    if permission not in ("admin", "maintain", "write"):
        raise ValueError("GitHub actor lacks verified write access to this repository")
    return actor


def reserve_next(
    github: GitHub,
    *,
    client: str,
    environment: dict[str, str],
    abilities: frozenset[str] = ACTIONS,
    expected: str = "",
    advertised_at: str = "",
    pick: str = "",
) -> dict[str, object]:
    """Reserve the highest live offer, or with ``pick`` exactly that target's offer.

    A label claim picks the item the agent labeled: any current candidate the
    agent can do, whatever its rank, after the same live and post-CAS checks.
    """
    if not CLIENT.fullmatch(client):
        raise ValueError("client must be a stable non-secret identifier")
    if not abilities or not abilities <= ACTIONS:
        raise ValueError("unsupported agent abilities")
    actor = principal(github, environment)
    store = ledger()
    reconcile(github, actor=actor, store=store)
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
    # Walk the ranking: the agent gets the highest offer it can do that is
    # still live now (CI settled, head and rank unchanged), not merely rank 1.
    if pick:
        offered = next((o for o in snapshot.offers if o.target == pick), None)
        if offered is None:
            return {"status": "conflict", "reason": "not_offered_now", "retry": "refresh_frontier"}
        if offered.status != "candidate":
            return {
                "status": "conflict",
                "reason": "held: " + "; ".join(offered.hold_reasons),
                "retry": "refresh_frontier",
            }
        if offered.action not in abilities:
            return {
                "status": "conflict",
                "reason": f"action {offered.action} is not claimable",
                "retry": "refresh_frontier",
            }
        expected = pick
    able = [
        o
        for o in snapshot.offers
        if o.status == "candidate" and o.action in abilities and (not pick or o.target == pick)
    ]
    if not able:
        return {
            "status": "conflict" if expected else "held",
            "reason": "expected_offer_no_longer_eligible" if expected else "no_eligible_offer",
            "retry": "refresh_frontier",
        }
    # One authoritative capture, walked in its own order: the agent gets the
    # highest offer it can do in the current ranking. A second capture would
    # only reorder unrelated work; head, priority, lane, ownership and CI are
    # revalidated live below and again after the CAS.
    chosen: frontier.Offer | None = None
    passed_over: list[dict[str, str]] = []
    for offered in able:
        okay, reason = _live_offer(github, offered, snapshot)
        if okay:
            chosen = offered
            break
        passed_over.append({"target": offered.target, "reason": reason})
        if expected and offered.target == expected:
            return {"status": "conflict", "reason": reason, "retry": "refresh_frontier"}
    if chosen is None:
        return {
            "status": "conflict" if expected else "held",
            "reason": "no offer is live now: "
            + "; ".join(f"{row['target']} ({row['reason']})" for row in passed_over[:5]),
            "retry": "refresh_frontier",
        }
    if expected and chosen.target != expected:
        return {
            "status": "conflict",
            "reason": "offer_reordered_or_no_longer_highest",
            "expected": expected,
            "current": chosen.target,
            "retry": "refresh_frontier",
        }
    try:
        numbers = _linked_targets(github, chosen)
    except ValueError as exc:
        return {"status": "conflict", "reason": str(exc), "retry": "refresh_frontier"}
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
        wip_exempt=_bypasses_wip(github, chosen),
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
        changed = _post_claim_change(github, chosen, numbers, live)
        if changed:
            raise ValueError(changed)
    except (GitHubError, ValueError) as exc:
        if not _rollback(
            store, github, actor, client, claim_id, "post-reservation-revalidation-failed"
        ):
            return {
                "status": "held",
                "reason": "claim rollback could not be verified",
                "claim_id": claim_id,
                "retry": "check_lease_before_retry",
            }
        return {"status": "conflict", "reason": str(exc), "retry": "refresh_frontier"}
    kind, number_text = chosen.target.split(":")
    try:
        target_info = obj(
            github.repo_api(f"pulls/{number_text}" if kind == "pr" else f"issues/{number_text}")
        )
        issue_body = string(target_info, "body")
        if kind == "pr" and len(numbers) == 2:
            linked_issue = next(number for number in numbers if number != int(number_text))
            issue_body = string(obj(github.repo_api(f"issues/{linked_issue}")), "body")
    except GitHubError as exc:
        if not _rollback(store, github, actor, client, claim_id, "handoff-preparation-failed"):
            return {
                "status": "held",
                "reason": "claim rollback could not be verified",
                "claim_id": claim_id,
                "retry": "check_lease_before_retry",
            }
        return {"status": "conflict", "reason": str(exc), "retry": "refresh_frontier"}
    outcome["handoff"] = handoff_packet(
        github,
        chosen,
        claim_id=claim_id,
        client=client,
        actor=actor,
        expires_at=str(outcome.get("expires_at") or ""),
        title=string(target_info, "title"),
        acceptance=issue_body,
    )
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
        github.repo,
        actor,
        client,
        claim_id,
        operation=operation,
        evidence=evidence,
        verify=(
            (lambda claim: _verify_and_project_handoff(github, claim, evidence, environment))
            if operation == "handoff"
            else None
        ),
        evidence_target=_evidence_pr(github, evidence) if operation == "handoff" else None,
    )
    return result


class ClaimStoreUnavailable(RuntimeError):
    """This machine has no connection to the shared claim store (Neon)."""


def holder(github: GitHub, number: int) -> dict[str, object] | None:
    """The external claim holding issue/PR ``number``, or None when it is free of one.

    Raises ClaimStoreUnavailable when the shared store cannot be read here.
    """
    try:
        store = ledger()
    except RuntimeError as exc:
        raise ClaimStoreUnavailable(str(exc)) from None
    if not hasattr(store, "external_holder"):
        raise RuntimeError("installed Rotisserie factory runtime cannot read external claims")
    if not store.enabled():
        raise ClaimStoreUnavailable("FACTORY_STATE_DSN_FILE is not set")
    found: dict[str, object] | None = store.external_holder(github.repo, number)
    return found


def revoke(
    github: GitHub,
    *,
    claim_id: str,
    reason: str,
    environment: dict[str, str],
) -> dict[str, object]:
    """Operator-initiated removal of an external claim (explicit, fenced, logged)."""
    if not CLAIM_ID.fullmatch(claim_id):
        raise ValueError("invalid claim identity")
    operator = principal(github, environment)
    store = ledger()
    if not hasattr(store, "revoke_external"):
        raise RuntimeError("installed Rotisserie factory runtime cannot revoke external claims")
    result: dict[str, object] = store.revoke_external(
        github.repo, operator, claim_id, reason=reason
    )
    return result
