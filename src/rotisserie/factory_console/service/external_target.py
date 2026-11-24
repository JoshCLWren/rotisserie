"""Claim one named issue or pull request (issue #212).

A person who names the item decides the priority. Factory intake rules
(capacity, backpressure, ranking, "offered now") never apply here. Only an
active Neon lease on the item, held by someone else, refuses the claim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import frontier
from rotisserie.factory_console.service.external_checks import (
    _linked_open_issue,
    _required_checks_outcome,
)
from rotisserie.factory_console.service.external_reserve import _rollback, handoff_packet

INSTRUCTIONS = {
    "implement": "Implement the issue and open a pull request that closes it.",
    "review": "Review this pull request's current head.",
    "review-findings": "Push commits to this pull request's branch.",
    "ci-repair": "Make this pull request's failing required checks pass.",
    "merge-conflict": "Resolve this pull request's conflict with its base branch.",
}


def action_for(github: GitHub, kind: str, number: int, item: dict[str, Any], actor: str) -> str:
    """The claimable action the item's current stage asks for."""
    if kind == "issue":
        return "implement"
    labels = runtime.module("factory_work_policy").labels_of(item)
    if "factory:merge-conflict" in labels:
        return "merge-conflict"
    if "factory:changes-requested" in labels:
        return "review-findings"
    if "factory:ci" in labels and _required_checks_outcome(github, number) == "failed":
        return "ci-repair"
    author = string(obj(item.get("user")), "login")
    if "factory:review" in labels and author != actor:
        return "review"
    return "review-findings"


def _offer(
    github: GitHub, target: str, action: str, item: dict[str, Any], head: str | None
) -> frontier.Offer:
    return frontier.Offer(
        rank=0,
        target=target,
        url=string(item, "html_url"),
        title=string(item, "title"),
        action=action,
        instruction=INSTRUCTIONS[action],
        reason="named by the claimant",
        status="candidate",
        hold_reasons=(),
        lane=0,
        priority=0,
        prerequisites=(),
        blocked_by=(),
        head_sha=head,
        owner=None,
        lease=None,
        capacity={"pr_wip": 0, "wip_limit": 1},
        parallel_group="",
        parallel_confidence="",
        provenance={},
    )


def _refused(reason: str, next_step: str) -> dict[str, object]:
    return {"status": "conflict", "reason": reason, "next": next_step}


def reserve_target(
    github: GitHub,
    *,
    store: ModuleType,
    actor: str,
    client: str,
    target: str,
    abilities: frozenset[str],
) -> dict[str, object]:
    """Lease exactly ``target`` for ``actor`` unless another active lease holds it."""
    kind, number_text = target.split(":")
    number = int(number_text)
    try:
        item = obj(github.repo_api(f"pulls/{number}" if kind == "pr" else f"issues/{number}"))
    except GitHubError as exc:
        return _refused(f"GitHub could not read {target}: {exc}", "Check the number and retry.")
    if string(item, "state") != "open":
        return _refused(f"{target} is closed", f"Reopen {target}, then claim it again.")
    if kind == "issue" and "pull_request" in item:
        return _refused(f"#{number} is a pull request", f"Claim pr:{number} instead.")
    action = action_for(github, kind, number, item, actor)
    if action not in abilities:
        return _refused(f"the next step is {action}, which this agent cannot do", "")
    head = string(obj(item.get("head")), "sha") if kind == "pr" else None
    link = _linked_open_issue(github, number, item) if kind == "pr" else None
    numbers = [link, number] if link is not None else [number]
    outcome: dict[str, object] = store.reserve_external(
        github.repo,
        actor,
        client,
        numbers,
        target=target,
        action=action,
        head=head,
        observed_wip=0,
        wip_limit=1,
        offer_at=datetime.now(UTC).isoformat(),
        wip_exempt=True,
    )
    if outcome.get("status") != "reserved":
        return {**outcome, **_holder_detail(github, store, numbers)}
    claim_id = str(outcome["claim_id"])
    try:
        live = obj(github.repo_api(f"pulls/{number}" if kind == "pr" else f"issues/{number}"))
        if string(live, "state") != "open":
            raise ValueError(f"{target} closed while it was being claimed")
        if kind == "pr" and string(obj(live.get("head")), "sha") != head:
            raise ValueError("new commits arrived while it was being claimed")
    except (GitHubError, ValueError) as exc:
        if not _rollback(store, github, actor, client, claim_id, "target changed during claim"):
            return {
                "status": "held",
                "reason": "the claim could not be undone; check the lease",
                "claim_id": claim_id,
                "next": f"baste leases {target} --via github",
            }
        return _refused(str(exc), "Claim it again.")
    issue_body = string(item, "body")
    if link is not None:
        issue_body = string(obj(github.repo_api(f"issues/{link}")), "body")
    outcome["handoff"] = handoff_packet(
        github,
        _offer(github, target, action, item, head),
        claim_id=claim_id,
        client=client,
        actor=actor,
        expires_at=str(outcome.get("expires_at") or ""),
        title=string(item, "title"),
        acceptance=issue_body,
    )
    return outcome


def _holder_detail(github: GitHub, store: ModuleType, numbers: list[int]) -> dict[str, object]:
    """Who holds the lease that refused the claim, in words an agent can act on."""
    try:
        rows: list[dict[str, Any]] = store.list_leases(github.repo)
    except Exception:  # noqa: BLE001 - the refusal stands without the detail
        return {"next": "baste leases --via github"}
    for row in rows:
        targets = [n for n in row.get("targets") or [] if isinstance(n, int)]
        if not set(targets) & set(numbers):
            continue
        operation = str(row.get("operation") or "")
        outside = operation.startswith("external:")
        who = (
            "@" + operation.removeprefix("external:").split("/")[0]
            if outside
            else f"factory worker {row.get('worker') or '?'}"
        )
        expires = row.get("expires_at", "?")
        end = (
            f"baste revoke {row.get('claim_id')}" if outside else f"baste cancel issue:{targets[0]}"
        )
        if row.get("active"):
            reason = f"#{targets[-1]} is held by {who} until {expires}"
            next_step = f"Wait for {who} to finish, or end the lease with: {end}"
        else:
            reason = f"#{targets[-1]} still has an expired lease by {who} (expired {expires})"
            next_step = f"End the stale lease with: {end}, then claim again"
        return {
            "reason": reason,
            "holder": {
                key: row.get(key) for key in ("worker", "operation", "claim_id", "expires_at")
            },
            "next": next_step,
        }
    return {"next": "baste leases --via github"}
