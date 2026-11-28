"""Project the bounded urgent reserve through the portable intake verdict."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from factory_work_policy_candidates import reserved_urgent_issue


def intake_with_urgent_reserve(
    observations: Iterable[dict[str, Any]],
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    *,
    pr_wip_limit: int,
    review_backlog_limit: int,
    operator_force: bool,
) -> dict[int, tuple[bool, tuple[str, ...]]]:
    """Only waive the portable soft backlog gate for the shared reserved issue.

    Every other portable refusal remains authoritative, including capacity,
    ownership, dependencies and existing changes. Owner-forced runs already
    pass zero pressure to the portable decision and need no exception here.
    """
    reserved = None if operator_force else reserved_urgent_issue(
        issues, prs, pr_wip_limit=pr_wip_limit, review_backlog_limit=review_backlog_limit
    )
    verdicts: dict[int, tuple[bool, tuple[str, ...]]] = {}
    for item in observations:
        if item.get("dimension") != "eligibility" or not str(item.get("subject", "")).startswith("work:"):
            continue
        number = int(item["subject"].removeprefix("work:"))
        reasons = tuple(str(reason) for reason in item.get("reasons") or ())
        eligible = item.get("outcome") == "eligible"
        if number == reserved and item.get("outcome") == "blocked" and reasons == ("backpressure",):
            eligible, reasons = True, ()
        verdicts[number] = (eligible, reasons)
    return verdicts
