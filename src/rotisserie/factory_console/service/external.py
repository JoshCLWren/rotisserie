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

# Work an outside agent can finish through the existing factory gates. Review
# is deliberately absent: the merge gate only counts approvals whose reviewer
# model the trusted runtime recorded, so an outside agent's review could never
# satisfy it. Independent review stays with the factory's own reviewers.
ACTIONS = frozenset({"implement", "review-findings", "ci-repair", "merge-conflict"})
REPAIRS = frozenset({"review-findings", "ci-repair", "merge-conflict"})
COMPLETION_STAGES = frozenset({"factory:review", "factory:ci", "factory:changes-requested"})
WORKFLOW = "rotisserie-external-claim.yml"
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
    # Lease keys and the advisory lock are built from this exact name; another
    # spelling of the same repository would get a separate lock and keys.
    canonical = string(obj(json.loads(github.gh("api", f"repos/{github.repo}"))), "full_name")
    if canonical != github.repo:
        raise ValueError(f"use the repository's exact name: --repo {canonical or '?'}")
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
            "title": string(live, "title"),
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
    passed = {"SUCCESS", "NEUTRAL", "SKIPPED"}  # the dispatcher's passing states
    failed = set(runtime.module("factory_work_policy").REQUIRED_CHECK_FAILURE_STATES)
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


def _live_offer(
    github: GitHub, previous: frontier.Offer, latest: frontier.Frontier | None = None
) -> tuple[bool, str]:
    """Reject moved heads, priority/stage changes, new dependencies and holds."""
    if latest is None:
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


def _bypasses_wip(github: GitHub, offer: frontier.Offer) -> bool:
    """The work policy's own WIP bypass (urgent defects) for this issue offer."""
    if offer.action != "implement":
        return False
    core = runtime.module("factory_work_policy")
    issue = obj(github.repo_api(f"issues/{offer.target.split(':')[1]}"))
    saturated = offer.capacity.get("review_backlog", 0) >= offer.capacity.get("review_limit", 1)
    return bool(core.issue_bypasses_wip_limit(issue, review_backlog_saturated=saturated))


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
        # The same static gate the controller applies: manual-only, epics,
        # blocked/terminal labels and ownership added since the snapshot.
        if not core.issue_is_static_candidate({**live, "number": int(number_text)}, set()):
            return "issue is no longer eligible (manual-only, blocked or owned)"
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
        "renew": f"rotisserie factory renew {claim_id} {cli}",
        "release": f"rotisserie factory release {claim_id} {cli}",
        "handoff_command": f"rotisserie factory handoff {claim_id} {cli} --evidence URL",
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
    able = [o for o in snapshot.offers if o.status == "candidate" and o.action in abilities]
    if not able:
        return {
            "status": "conflict" if expected else "held",
            "reason": "expected_offer_no_longer_eligible" if expected else "no_eligible_offer",
            "retry": "refresh_frontier",
        }
    latest = frontier.capture(github, now=datetime.now(UTC))
    chosen: frontier.Offer | None = None
    passed_over: list[dict[str, str]] = []
    for offered in able:
        okay, reason = _live_offer(github, offered, latest)
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


def _project_review_stage(github: GitHub, core: ModuleType, number: int, labels: set[str]) -> None:
    """Put a PR into the factory's existing review queue, exactly as a worker would.

    A factory worker finishing an implementation or repair leaves its PR in
    ``factory:review`` with no owner. This is that same projection: labels
    are hints for the existing controllers, never ownership or approval.
    """
    wanted = (labels - set(core.STAGE_LABELS) - {"factory:unowned"}) | {
        "factory",
        "factory:review",
        "factory:unowned",
    }
    if wanted != labels:
        github.gh(
            "api",
            f"repos/{github.repo}/issues/{number}/labels",
            "--method",
            "PUT",
            "--input",
            "-",
            stdin=json.dumps({"labels": sorted(wanted)}),
        )
    actual = core.labels_of(obj(github.repo_api(f"pulls/{number}")))
    if core.stage_of(actual) != "factory:review" or core.owner_of(actual) != "factory:unowned":
        raise RuntimeError("could not verify the review handoff labels")


def _author_model(github: GitHub, head: str) -> str:
    """The single ``Model:`` trailer on the handed-off head commit, or refuse."""
    commit = obj(obj(github.repo_api(f"commits/{head}")).get("commit"))
    models = runtime.module("factory_review_policy").model_trailer_models(string(commit, "message"))
    if len(models) != 1:
        raise ValueError(
            "the handed-off head commit needs exactly one 'Model: <your model id>' trailer "
            "so the factory can keep its author from approving it"
        )
    return str(next(iter(models)))


def _record_author(
    github: GitHub, pr: int, head: str, model: str, client: str, environment: dict[str, str]
) -> None:
    """Write the controller-trusted author-model marker for an outside agent's head.

    Only the Actions bot's comments count as author records, so this runs in
    the GitHub bridge. Without it a repair model could later approve its own
    repair, since a repair commit cannot be attested from the PR's author.
    """
    if environment.get("GITHUB_ACTIONS") != "true":
        raise ValueError(
            "repair handoffs must run in the GitHub bridge so the factory records their "
            f"author: gh workflow run {WORKFLOW} --repo {github.repo} --ref main "
            "-f operation=handoff -f client=CLIENT -f claim_id=UUID -f evidence=URL"
        )
    policy = runtime.module("factory_review_policy")
    marker = policy.agent_model_author_marker(
        pr=pr, head=head, model=model, agent=f"external-{client}"
    )
    github.gh("issue", "comment", str(pr), "--repo", github.repo, "--body", marker)


def _verify_and_project_handoff(
    github: GitHub,
    claim: dict[str, Any],
    evidence: str,
    environment: dict[str, str] | None = None,
) -> None:
    """Validate evidence against the fenced task, then hand the PR to review.

    Runs inside the ledger's locked handoff transaction: if anything here
    fails, the claim stays held and nothing is released. Only a pull request
    proven to carry the claimed work is moved into the review queue.
    """
    prefix = f"https://github.com/{github.repo}/"
    if not evidence.startswith(prefix) or not EVIDENCE.fullmatch(evidence):
        raise ValueError("handoff evidence must be a same-repository GitHub URL")
    route = evidence[len(prefix) :].split("#", 1)[0]
    if not route.startswith("pull/"):
        raise ValueError(
            "hand off the pull request that carries the work; with no PR, release the claim"
        )
    kind, number_str = str(claim["target"]).split(":")
    number = int(number_str)
    action = str(claim["action"])
    core = runtime.module("factory_work_policy")
    pr_number = int(route.split("/")[1])
    pr = obj(github.repo_api(f"pulls/{pr_number}"))
    head = string(obj(pr.get("head")), "sha")
    if string(pr, "state") != "open" or pr.get("draft") is True or not head:
        raise ValueError("handoff PR must be open, not a draft, with an exact head")
    labels = core.labels_of(pr)
    if core.owner_of(labels) not in (None, "factory:unowned"):
        raise ValueError("handoff PR is owned by another worker")
    stage = core.stage_of(labels)
    if action == "implement":
        if kind != "issue":
            raise ValueError("implementation handoff must originate from an issue")
        row = {
            "number": pr_number,
            "headRefName": string(obj(pr.get("head")), "ref"),
            "body": string(pr, "body"),
            "title": string(pr, "title"),
        }
        # GitHub closes what the body names; the factory links by branch first.
        # Both must name the claimed issue.
        if (
            core.issue_explicitly_closed_by_pr(row) != number
            or core.linked_issue_from_pr(row) != number
        ):
            raise ValueError("PR body must say 'Closes #N' for the claimed issue and nothing else")
        if stage not in (None, "factory:review"):
            raise ValueError("handoff PR is already in a different factory stage")
    elif action in REPAIRS:
        if kind != "pr" or pr_number != number:
            raise ValueError("handoff evidence points to a different PR")
        if head == claim.get("head"):
            raise ValueError("no new commit on the claimed PR; release the claim instead")
        if stage not in COMPLETION_STAGES:
            raise ValueError("the PR left the factory's completion stages")
    else:
        raise ValueError("unsupported claimed action")
    model = _author_model(github, head)
    if action in REPAIRS:
        _record_author(github, pr_number, head, model, str(claim["client"]), environment or {})
    _project_review_stage(github, core, pr_number, labels)


def _evidence_pr(github: GitHub, evidence: str) -> int | None:
    """The PR number a handoff URL names, so the ledger can check nobody holds it."""
    route = evidence.removeprefix(f"https://github.com/{github.repo}/").split("#", 1)[0]
    return int(route.split("/")[1]) if route.startswith("pull/") else None


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
