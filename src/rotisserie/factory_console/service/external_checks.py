"""Live GitHub rechecks for outside-agent claims, before and after the CAS."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, obj, string
from rotisserie.factory_console.service import frontier, queue


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
    link = _linked_open_issue(github, number, live)
    return [link, number] if link is not None else [number]


def _linked_open_issue(github: GitHub, number: int, pr: dict[str, Any]) -> int | None:
    """The open issue a PR is linked to, which a claim on the PR also fences."""
    link = runtime.module("factory_work_policy").linked_issue_from_pr(
        {
            "number": number,
            "headRefName": string(obj(pr.get("head")), "ref"),
            "body": string(pr, "body"),
            "title": string(pr, "title"),
        }
    )
    if link is None or int(link) == number:
        return None
    issue = obj(github.repo_api(f"issues/{link}"))
    if string(issue, "state") != "open" or "pull_request" in issue:
        return None
    return int(link)


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
    """Narrow controller exception: a conflicted head whose CI is not running."""
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
    # Same rule as the dispatcher (#182): only a still-running check waits.
    return not any(
        not isinstance(run, dict) or str(run.get("status", "")).lower() != "completed"
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
    ):
        # Rank is not compared: unrelated offers enter and leave above this
        # one all the time. The caller walks one fresh capture in its order.
        return False, "head, priority, stage or action changed"
    if found.status != "candidate":
        return False, "offer no longer eligible: " + "; ".join(found.hold_reasons)
    return live_now(github, previous.target, previous.action, previous.head_sha)


def live_now(github: GitHub, target: str, action: str, head_sha: str | None) -> tuple[bool, str]:
    """The live GitHub conditions reserve-next checks before reserving a candidate.

    Open dependencies for an issue; for a PR, required CI that is still
    running (except a conflicted PR with no CI to wait for) or, for a CI
    repair, no terminal failure to repair.
    """
    kind, number_text = target.split(":")
    number = int(number_text)
    if kind == "issue":
        blocked, problem = queue.open_blocker(github, number)
        if blocked:
            return False, problem or "issue dependencies changed"
        return True, ""
    checks = _required_checks_outcome(github, number)
    if checks == "waiting" and not (
        action == "merge-conflict" and _conflict_without_ci(github, number, head_sha)
    ):
        return False, "required CI is still running or unknown"
    if action == "ci-repair" and checks != "failed":
        return False, "current head has no terminal required CI failure"
    return True, ""


def _implemented_issues(github: GitHub, core: ModuleType) -> set[int]:
    """Issues an open PR already implements, read live (the controller's rule)."""
    rows = json.loads(
        github.gh(
            "pr",
            "list",
            "--repo",
            github.repo,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number,headRefName,body,title,state",
        )
    )
    if not isinstance(rows, list) or len(rows) >= 500:
        raise ValueError("open pull requests could not be listed completely")
    return {
        int(linked)
        for row in rows
        if isinstance(row, dict)
        and core.pr_suppresses_issue_candidate(row, {})
        and (linked := core.linked_issue_from_pr(row)) is not None
    }


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
        if not core.issue_is_static_candidate(
            {**live, "number": int(number_text)}, _implemented_issues(github, core)
        ):
            return "issue is no longer eligible (manual-only, blocked or owned)"
        blocked, why = queue.open_blocker(github, int(number_text))
        if blocked:
            return why or "dependency graph changed after reservation"
        body = string(live, "body")
        for prerequisite in sorted(core.parse_depends_on_numbers(body) - {int(number_text)}):
            row = obj(github.repo_api(f"issues/{prerequisite}"))
            if string(row, "state") == "open":
                return f"issue now depends on open #{prerequisite}"
        all_labels = labels
    else:
        all_labels = set(labels)
        live_link = core.linked_issue_from_pr(
            {
                "number": int(number_text),
                "headRefName": string(obj(live.get("head")), "ref"),
                "body": string(live, "body"),
                "title": string(live, "title"),
            }
        )
        if live_link is not None and live_link not in numbers:
            # A closed linked issue is not leased (the factory leases only the
            # PR then); any open one we did not fence means the link changed.
            relinked = obj(github.repo_api(f"issues/{live_link}"))
            if string(relinked, "state") == "open" and "pull_request" not in relinked:
                return "the pull request's linked issue changed after reservation"
        if len(numbers) == 2:
            linked = next(n for n in numbers if n != int(number_text))
            linked_row = obj(github.repo_api(f"issues/{linked}"))
            if string(linked_row, "state") != "open":
                return "linked issue changed after reservation"
            linked_labels = core.labels_of(linked_row)
            if core.owner_of(linked_labels) not in (None, "factory:unowned"):
                return "another worker became the linked issue's owner after reservation"
            all_labels.update(linked_labels)
        row = {
            "number": int(number_text),
            "state": string(live, "state"),
            "isDraft": live.get("draft") is True,
            "body": string(live, "body"),
            "title": string(live, "title"),
            "headRefName": string(obj(live.get("head")), "ref"),
            "labels": live.get("labels") or [],
        }
        if not core.pr_is_static_candidate(row, {}):
            return "pull request is no longer eligible (manual-only, draft, blocked or owned)"
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
        if offer.action == "ci-repair" and checks != "failed":
            return "required CI is no longer failing; nothing to repair"
    if (
        core.priority_rank(all_labels) != offer.priority
        or core.provenance_lane(all_labels, pull_request=(kind == "pr")) != offer.lane
    ):
        return "priority or scheduling lane changed after reservation"
    return ""


def claim_scope(github: GitHub, kind: str, number: int) -> list[int]:
    """The numbers a claim on this item could sit on: a PR and its linked issue."""
    if kind != "pr":
        return [number]
    try:
        pr = obj(github.repo_api(f"pulls/{number}"))
    except GitHubError:
        return [number]
    linked = runtime.module("factory_work_policy").linked_issue_from_pr(
        {
            "number": number,
            "headRefName": string(obj(pr.get("head")), "ref"),
            "body": string(pr, "body"),
            "title": string(pr, "title"),
        }
    )
    return [number] if linked in (None, number) else [number, int(linked)]
