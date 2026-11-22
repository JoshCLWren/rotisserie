"""Read-only ranked execution frontier for agents outside the factory fleet.

All eligibility and ordering come from the installed controller's work policy.
A candidate is never a lease, and missing Neon evidence cannot be treated as
a free claim. The write boundary belongs to the trusted controller, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, string
from rotisserie.factory_console.service import providers, queue
from rotisserie.factory_console.service import sync as branch_sync

SCHEMA_VERSION = 1
FRESH_FOR = timedelta(seconds=60)


@dataclass(frozen=True)
class Offer:
    rank: int
    target: str
    url: str
    title: str
    action: str
    instruction: str
    reason: str
    status: str  # candidate | held
    hold_reasons: tuple[str, ...]
    lane: int
    priority: int
    prerequisites: tuple[str, ...]
    blocked_by: tuple[str, ...]
    head_sha: str | None
    owner: str | None
    lease: dict[str, object] | None
    capacity: dict[str, int]
    parallel_group: str
    parallel_confidence: str
    provenance: dict[str, object]


@dataclass(frozen=True)
class Frontier:
    schema_version: int
    repository: str
    generated_at: str
    expires_at: str
    authoritative_claim_available: bool
    authority_note: str
    offers: tuple[Offer, ...]
    holds: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "requires_claim": True,
            "claim_authority": "trusted Rotisserie controller / Neon fenced lease",
            "disclaimer": (
                "Candidates are read-only. A GitHub label or this response does not "
                "reserve work. Requery and acquire a trusted fenced claim before acting."
            ),
        }


def _active_leases(repository: str) -> tuple[dict[int, dict[str, object]], str]:
    """Read the shared ledger, never infer that a missing reader means no leases."""
    ledger, problem = branch_sync.load_ledger()
    if ledger is None:
        return {}, problem
    try:
        rows: list[dict[str, Any]] = ledger.list_leases(repository)
        leases = _target_leases(repository, rows)
    except Exception as exc:
        return {}, f"Neon lease state unavailable ({type(exc).__name__})"
    return leases, ""


def _target_leases(repository: str, rows: list[dict[str, Any]]) -> dict[int, dict[str, object]]:
    """Decode the target keyed row, not the other targets in a claim payload."""
    prefix = f"factory-lease-v1:{repository}:target:"
    leases: dict[int, dict[str, object]] = {}
    for row in rows:
        key = row.get("key")
        if not isinstance(key, str) or not key.startswith(prefix):
            continue  # Worker rows share target lists, but are not target leases.
        number_text = key.removeprefix(prefix)
        if not number_text.isascii() or not number_text.isdecimal():
            raise ValueError("Neon ledger returned an invalid target lease key")
        number = int(number_text)
        if number <= 0 or number in leases:
            raise ValueError("Neon ledger returned a duplicate or invalid target")
        # Expired rows remain holds until trusted liveness reconciliation.
        leases[number] = {
            "claim_id": str(row.get("claim_id", "")),
            "worker": str(row.get("worker", "")),
            "operation": str(row.get("operation", "")),
            "expires_at": str(row.get("expires_at", "")),
            "active": row.get("active") is True,
            "wip": row.get("wip") is True,
        }
    return leases


def _number(row: Obj) -> int:
    number = row.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ValueError("A factory work row has no valid issue/PR number")
    return number


def _labels(core: Any, row: Obj) -> set[str]:
    return set(core.labels_of(row))


def from_snapshot(
    repository: str,
    *,
    now: datetime,
    issues: list[Obj],
    prs: list[Obj],
    candidates: list[Any],
    issue_verdicts: dict[int, queue.Verdict],
    leases: dict[int, dict[str, object]],
    lease_problem: str,
    review_limit: int,
    wip_limit: int,
    review_backlog: int,
    pr_wip: int,
) -> Frontier:
    """Pure projection of the same already-ranked controller candidates.

    This function never upgrades a label-derived owner into a verified lease.
    Unknown lease state blocks every claim, while leaving candidates inspectable.
    """
    core = runtime.module("factory_work_policy")
    issues_by_number = {_number(row): row for row in issues}
    prs_by_number = {_number(row): row for row in prs}
    active_targets = set(leases)
    offers: list[Offer] = []
    for position, candidate in enumerate(candidates, 1):
        kind = str(candidate.kind)
        number = int(candidate.number)
        row = (issues_by_number if kind == "issue" else prs_by_number).get(number)
        if row is None:
            continue
        labels = _labels(core, row)
        owner = core.owner_of(labels)
        action = (
            "implement"
            if kind == "issue"
            else core.completion_action_for(candidate.stage, bool(candidate.conflicted))
        )
        blockers: list[str] = []
        if action is None:
            blockers.append("No executable controller action for the current PR stage")
            action = "inspect"
        if owner not in (None, "", "factory:unowned"):
            blockers.append(f"GitHub still identifies an owner: {owner}")
        if lease_problem:
            blockers.append(lease_problem)
        touched = [number]
        if kind == "pr" and candidate.linked_issue is not None:
            touched.append(int(candidate.linked_issue))
        claimed = [n for n in touched if n in active_targets]
        if claimed:
            blockers.append(
                "Existing or unreconciled Neon lease on " + ", ".join(f"#{n}" for n in claimed)
            )
        dependencies: set[int] = set()
        if kind == "issue":
            verdict = issue_verdicts.get(number)
            if verdict is None:
                blockers.append("No complete issue eligibility verdict")
            elif verdict.status != "ready":
                blockers.extend(reason.text for reason in verdict.reasons)
            dependencies = core.parse_declared_dependencies(string(row, "body"))
        if kind == "pr" and not string(row, "headRefOid"):
            blockers.append("Missing exact pull-request head SHA")
        if (
            kind == "pr"
            and len(
                [
                    label
                    for label in labels
                    if label
                    in (
                        "factory:review",
                        "factory:ci",
                        "factory:changes-requested",
                        "factory:ready",
                        "factory:blocked",
                        "factory:building",
                    )
                ]
            )
            != 1
        ):
            blockers.append("PR stage is ambiguous; reconcile lifecycle labels")
        if action == "review" and candidate.head_contributors:
            # A known contributor may still be a valid reviewer on another PR;
            # the trusted reviewer selector checks that specific identity.
            independence = "must verify selected reviewer against recorded authors"
        elif action == "review":
            independence = "unknown author provenance must use current review policy"
        else:
            independence = "not a review action"
        url = f"https://github.com/{repository}/{'issues' if kind == 'issue' else 'pull'}/{number}"
        goal = {
            "implement": "Implement the issue's acceptance criteria and produce a PR",
            "review": "Independently review this exact head and report valid findings",
            "ci-repair": "Repair this PR's own failing checks without broadening scope",
            "review-findings": "Resolve verified review findings on this PR",
            "merge-conflict": "Resolve its merge conflicts and rerun exact-head checks",
        }.get(action, "Inspect the current stage; controller authorization is required")
        reason = f"controller lane {candidate.lane}, priority {candidate.priority}"
        lease = next((leases[n] for n in touched if n in leases), None)
        offers.append(
            Offer(
                rank=position,
                target=f"{kind}:{number}",
                url=url,
                title=string(row, "title"),
                action=action,
                instruction=goal,
                reason=reason,
                status="held" if blockers else "candidate",
                hold_reasons=tuple(dict.fromkeys(blockers)),
                lane=int(candidate.lane),
                priority=int(candidate.priority),
                prerequisites=tuple(f"issue:{n}" for n in sorted(dependencies)),
                blocked_by=tuple(
                    f"issue:{n}" for n in sorted(n for n in dependencies if n in issues_by_number)
                ),
                head_sha=string(row, "headRefOid") or None if kind == "pr" else None,
                owner=owner,
                lease=lease,
                capacity={
                    "review_backlog": review_backlog,
                    "review_limit": review_limit,
                    "pr_wip": pr_wip,
                    "wip_limit": wip_limit,
                },
                parallel_group=f"isolated:{kind}:{number}",
                parallel_confidence=(
                    "unknown: independent targets do not prove disjoint files, "
                    "dependencies, or provider capacity"
                ),
                provenance={
                    "producer": candidate.producer_worker or None,
                    "head_contributors": sorted(candidate.head_contributors),
                    "review_independence": independence,
                },
            )
        )
    represented = {offer.target for offer in offers}
    holds: list[dict[str, object]] = []
    for row in issues:
        number = _number(row)
        if f"issue:{number}" in represented:
            continue
        verdict = issue_verdicts.get(number)
        holds.append(
            {
                "target": f"issue:{number}",
                "url": f"https://github.com/{repository}/issues/{number}",
                "status": verdict.status if verdict else "unknown",
                "reasons": [reason.text for reason in verdict.reasons]
                if verdict
                else ["Not admitted by the controller's current policy"],
            }
        )
    for row in prs:
        number = _number(row)
        if f"pr:{number}" not in represented:
            holds.append(
                {
                    "target": f"pr:{number}",
                    "url": f"https://github.com/{repository}/pull/{number}",
                    "status": "waiting",
                    "reasons": ["No executable action selected for this PR at this stage"],
                }
            )
    instant = now.astimezone(UTC)
    return Frontier(
        SCHEMA_VERSION,
        repository,
        instant.isoformat(),
        (instant + FRESH_FOR).isoformat(),
        not bool(lease_problem),
        lease_problem or "Neon leases inspected, but no work has been reserved",
        tuple(offers),
        tuple(holds),
    )


def capture(github: GitHub, *, now: datetime) -> Frontier:
    """Read existing GitHub/controller eligibility, ranking and Neon lease state."""
    variables = providers.repository_variables(github)
    summary = queue.summarize_queue(github, now=int(now.timestamp()), variables=variables)
    board = summary.board
    # This is precisely the installed factory policy's deterministic candidate
    # order. In-flight Jev overrides remain controller-owned, never simulated.
    # Shared Neon target keys record external ownership; count each linked
    # PR/issue claim only once when enforcing the normal WIP gate.
    leases, problem = _active_leases(github.repo)
    if len(board.issues) >= 1000 or len(board.prs) >= 500:
        # gh list --limit is a cap, not proof that the server has no more rows.
        # A partial graph may conceal a higher-priority item or active PR.
        problem = (
            "GitHub open issue/PR listing reached its pagination cap; "
            "a complete work view cannot be established" + (f"; {problem}" if problem else "")
        )
    external_wip = len(
        {
            str(value["claim_id"])
            for value in leases.values()
            if str(value.get("operation", "")).startswith("external:") and value.get("wip") is True
        }
    )
    core = runtime.module("factory_work_policy")
    candidates = core.build_candidates(
        board.issues,
        board.prs,
        pr_wip_limit=board.pr_wip_limit,
        review_backlog_limit=board.review_backlog_limit,
        external_wip=external_wip,
    )
    by_issue = {entry.issue: entry.verdict for entry in summary.entries}
    rows_for_counts: list[Mapping[str, object]] = list(board.prs)
    return from_snapshot(
        github.repo,
        now=now,
        issues=board.issues,
        prs=board.prs,
        candidates=candidates,
        issue_verdicts=by_issue,
        leases=leases,
        lease_problem=problem,
        review_limit=board.review_backlog_limit,
        wip_limit=board.pr_wip_limit,
        review_backlog=len(runtime.completion_backlog(rows_for_counts)),
        pr_wip=runtime.pr_wip_count(rows_for_counts),
    )
