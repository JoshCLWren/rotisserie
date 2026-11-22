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
from typing import Any

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
            "external_claim_details",
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


def _required_checks_outcome(github: GitHub, number: int) -> str:
    """Use the same exact-head required-check classification as the dispatcher."""
    try:
        values = json.loads(
            github.gh(
                "pr",
                "checks",
                str(number),
                "--repo",
                github.repo,
                "--required",
                "--json",
                "state",
                allow_exit_one=True,
            )
        )
    except (GitHubError, ValueError):
        return "waiting"
    if not isinstance(values, list) or not values:
        return "waiting"
    states = [
        str(row.get("state", "")).upper()
        for row in values
        if isinstance(row, dict) and isinstance(row.get("state"), str)
    ]
    if len(states) != len(values):
        return "waiting"
    passed = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    failed = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    if any(value not in passed | failed for value in states):
        return "waiting"
    return "failed" if any(value in failed for value in states) else "passed"


def _conflict_without_ci(github: GitHub, number: int, head: str | None) -> bool:
    """Narrow existing controller exception for a conflicted head with no CI."""
    if head is None:
        return False
    try:
        pr = obj(github.repo_api(f"pulls/{number}"))
        if (
            string(obj(pr.get("head")), "sha") != head
            or string(pr, "state") != "open"
            or not (
                pr.get("mergeable") is False or string(pr, "mergeable_state").upper() == "DIRTY"
            )
        ):
            return False
        response = obj(github.repo_api(f"commits/{head}/check-runs?per_page=100"))
    except GitHubError:
        return False
    runs = response.get("check_runs")
    if (
        not isinstance(runs, list)
        or not isinstance(response.get("total_count"), int)
        or response["total_count"] != len(runs)
    ):
        return False
    return not any(
        not isinstance(run, dict)
        or run.get("name") == "CI Summary"
        or str(run.get("status", "")).lower() != "completed"
        for run in runs
    )


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
        or found.rank != previous.rank
    ):
        return False, "head, priority, stage or action changed"
    if found.status != "candidate":
        return False, "offer no longer eligible: " + "; ".join(found.hold_reasons)
    kind, number_text = previous.target.split(":")
    number = int(number_text)
    if kind == "issue":
        blocked, problem = queue.open_blocker(github, number)
        if blocked:
            return False, problem or "issue dependencies changed"
    else:
        checks = _required_checks_outcome(github, number)
        if checks == "waiting" and not (
            previous.action == "merge-conflict"
            and _conflict_without_ci(github, number, previous.head_sha)
        ):
            return False, "required CI is still running or unknown"
        if previous.action == "ci-repair" and checks != "failed":
            return False, "current head has no terminal required CI failure"
    return True, ""


def _is_known_author(offer: frontier.Offer, *, client: str) -> bool:
    if offer.action != "review":
        return False
    identities = offer.provenance.get("head_contributors")
    contributors = identities if isinstance(identities, (list, tuple, set, frozenset)) else ()
    return client in contributors or client == offer.provenance.get("producer")


def _post_claim_change(
    github: GitHub, offer: frontier.Offer, numbers: list[int], live: dict[str, Any]
) -> str:
    """Recheck ownership, priority, dependencies, PR stage and CI after CAS."""
    core = runtime.module("factory_work_policy")
    labels = core.labels_of(live)
    if core.owner_of(labels) not in (None, "factory:unowned"):
        return "another worker became the GitHub owner after reservation"
    kind, number_text = offer.target.split(":")
    if kind == "issue":
        blocked, why = queue.open_blocker(github, int(number_text))
        if blocked:
            return why or "dependency graph changed after reservation"
        all_labels = labels
    else:
        all_labels = set(labels)
        if len(numbers) == 2:
            linked = next(n for n in numbers if n != int(number_text))
            linked_row = obj(github.repo_api(f"issues/{linked}"))
            if string(linked_row, "state") != "open":
                return "linked issue changed after reservation"
            all_labels.update(core.labels_of(linked_row))
        conflicting = (
            live.get("mergeable") is False or string(live, "mergeable_state").upper() == "DIRTY"
        )
        if core.completion_action_for(core.stage_of(labels), conflicting) != offer.action:
            return "PR stage or merge state changed after reservation"
        checks = _required_checks_outcome(github, int(number_text))
        if checks == "waiting" and not (
            offer.action == "merge-conflict"
            and _conflict_without_ci(github, int(number_text), offer.head_sha)
        ):
            return "required CI became pending or unknown after reservation"
    if (
        core.priority_rank(all_labels) != offer.priority
        or core.provenance_lane(all_labels, pull_request=(kind == "pr")) != offer.lane
    ):
        return "priority or scheduling lane changed after reservation"
    return ""


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
    chosen: frontier.Offer | None = None
    numbers: list[int] = []
    for offered in snapshot.offers:
        if offered.status != "candidate" or offered.action not in abilities:
            continue
        if _is_known_author(offered, client=client):
            continue
        if offered.action == "review":
            try:
                linked = _linked_targets(github, offered)
                authors: set[str] = store.external_code_authors(github.repo, linked)
            except (GitHubError, RuntimeError, ValueError) as exc:
                return {
                    "status": "held",
                    "reason": f"review authorship unavailable: {exc}",
                    "retry": "refresh_frontier",
                }
            if client in authors:
                continue
            numbers = linked
        chosen = offered
        break
    if chosen is None:
        return {
            "status": "conflict" if expected else "held",
            "reason": "expected_offer_no_longer_eligible" if expected else "no_eligible_offer",
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
    okay, reason = _live_offer(github, chosen)
    if not okay:
        return {"status": "conflict", "reason": reason, "retry": "refresh_frontier"}
    if not numbers:
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
        changed = _post_claim_change(github, chosen, numbers, live)
        if changed:
            raise ValueError(changed)
    except (GitHubError, ValueError) as exc:
        rolled_back = store.external_lease_action(
            github.repo,
            actor,
            client,
            claim_id,
            operation="release",
            evidence="post-reservation-revalidation-failed",
        )
        if rolled_back.get("status") != "released":
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
        store.external_lease_action(
            github.repo,
            actor,
            client,
            claim_id,
            operation="release",
            evidence="handoff-preparation-failed",
        )
        return {"status": "conflict", "reason": str(exc), "retry": "refresh_frontier"}
    outcome["handoff"] = {
        "acceptance_criteria": issue_body[:12000],
        "task_title": string(target_info, "title"),
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
        "release": (
            f"rotisserie factory release {claim_id} --client {client} --repo {github.repo}"
        ),
        "report": chosen.url,
        "review_and_merge_gates": (
            "Existing exact-head independent review, CI and acceptance remain required"
        ),
    }
    return outcome


def _verify_and_project_handoff(github: GitHub, claim: dict[str, Any], evidence: str) -> None:
    """Validate evidence against the fenced task before factory-stage projection.

    Only this authorized controller adds review-stage labels to the PR it
    verified as implementing the issue. The labels are projections, not locks;
    the original external Neon claim remains held until the final ledger event
    and identity-fenced release succeed.
    """
    prefix = f"https://github.com/{github.repo}/"
    if not evidence.startswith(prefix) or not EVIDENCE.fullmatch(evidence):
        raise ValueError("handoff evidence must be a same-repository GitHub URL")
    route = evidence[len(prefix) :].split("#", 1)[0]
    target = str(claim["target"])
    action = str(claim["action"])
    kind, number_str = target.split(":")
    number = int(number_str)
    core = runtime.module("factory_work_policy")
    if route.startswith("pull/"):
        pr_number = int(route.split("/")[1])
        pr = obj(github.repo_api(f"pulls/{pr_number}"))
        if string(pr, "state") != "open":
            raise ValueError("handoff PR is not open")
        if action == "implement":
            if kind != "issue":
                raise ValueError("implementation handoff must originate from an issue")
            linked_issue = core.linked_issue_from_pr(
                {
                    "number": pr_number,
                    "headRefName": string(obj(pr.get("head")), "ref"),
                    "body": string(pr, "body"),
                    "title": string(pr, "title"),
                }
            )
            if linked_issue != number:
                raise ValueError("PR must explicitly close or link the claimed issue")
            if pr.get("draft") is True or not string(obj(pr.get("head")), "sha"):
                raise ValueError("implementation PR is draft or lacks an exact head")
            labels = core.labels_of(pr)
            if core.owner_of(labels) not in (None, "factory:unowned"):
                raise ValueError("handoff PR is owned by another worker")
            if core.stage_of(labels) not in (None, "factory:review"):
                raise ValueError("handoff PR is already in a different factory stage")
            # A newly opened external PR is brought into the existing factory
            # review queue, never approved or merged. This is the existing
            # stage projection, not a new independent scheduler.
            github.gh(
                "api",
                f"repos/{github.repo}/issues/{pr_number}/labels",
                "--method",
                "POST",
                "-f",
                "labels[]=factory",
                "-f",
                "labels[]=factory:review",
                "-f",
                "labels[]=factory:unowned",
            )
            updated = obj(github.repo_api(f"pulls/{pr_number}"))
            actual = core.labels_of(updated)
            if not {"factory", "factory:review", "factory:unowned"} <= actual:
                raise RuntimeError("review handoff labels could not be verified")
        elif kind != "pr" or pr_number != number:
            raise ValueError("handoff evidence points to a different PR")
        return
    if route.startswith("issues/"):
        issue_number = int(route.split("/")[1])
        if kind != "issue" or issue_number != number or action == "implement":
            raise ValueError("issue link is not sufficient implementation evidence")
        if string(obj(github.repo_api(f"issues/{issue_number}")), "state") != "open":
            raise ValueError("handoff evidence issue is not open")
        return
    if route.startswith("actions/runs/"):
        run_number = int(route.split("/")[2])
        run = obj(github.repo_api(f"actions/runs/{run_number}"))
        if string(run, "repository") and string(run, "repository") != github.repo:
            raise ValueError("workflow evidence has another repository")
        if kind == "pr" and string(run, "head_sha") != claim.get("head"):
            raise ValueError("workflow evidence does not match reserved PR head")
        if action == "implement":
            raise ValueError("an implementation must hand off a linked pull request")
        if string(run, "status") != "completed":
            raise ValueError("workflow evidence is not terminal")
        return
    raise ValueError("unsupported handoff evidence")


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
    if operation == "handoff":
        claim: dict[str, Any] = store.external_claim_details(github.repo, actor, client, claim_id)
        _verify_and_project_handoff(github, claim, evidence)
    result: dict[str, object] = store.external_lease_action(
        github.repo, actor, client, claim_id, operation=operation, evidence=evidence
    )
    return result
