"""Agent-facing read-only work offers never grant authority to act."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from rotisserie.factory_console.service import frontier, queue

REPO = "acme/widgets"
NOW = datetime(2026, 11, 22, 21, 0, tzinfo=UTC)


def issue(number: int, *labels: str, body: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "labels": list(labels),
        "createdAt": NOW.isoformat(),
    }


def pr(number: int, *labels: str, head: str = "a" * 40) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "labels": list(labels),
        "headRefOid": head,
        "createdAt": NOW.isoformat(),
    }


def candidate(
    kind: str,
    number: int,
    *,
    lane: int = 3,
    priority: int = 1,
    linked_issue: int | None = None,
    stage: str | None = None,
    contributors: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        number=number,
        lane=lane,
        priority=priority,
        linked_issue=linked_issue,
        conflicted=False,
        stage=stage,
        producer_worker=None,
        head_contributors=frozenset(contributors),
    )


def snapshot(
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    candidates: list[SimpleNamespace],
    *,
    problem: str = "",
    leases: dict[int, dict[str, object]] | None = None,
    verdicts: dict[int, queue.Verdict] | None = None,
) -> frontier.Frontier:
    return frontier.from_snapshot(
        REPO,
        now=NOW,
        issues=issues,
        prs=prs,
        candidates=candidates,
        issue_verdicts=verdicts
        if verdicts is not None
        else {int(item["number"]): queue.Verdict("ready", ()) for item in issues},
        leases=leases or {},
        lease_problem=problem,
        review_limit=6,
        wip_limit=5,
        review_backlog=6,
        pr_wip=2,
    )


def test_rank_respects_controller_order_and_completes_pr_before_new_issue() -> None:
    result = snapshot(
        [issue(7, "ralph-task", "ralph-priority:critical")],
        [pr(8, "factory:review", "factory:unowned")],
        [candidate("pr", 8, stage="factory:review"), candidate("issue", 7)],
    )
    assert [(offer.rank, offer.target, offer.action) for offer in result.offers] == [
        (1, "pr:8", "review"),
        (2, "issue:7", "implement"),
    ]
    assert result.offers[0].head_sha == "a" * 40
    assert result.offers[0].capacity["review_backlog"] == 6
    assert all(offer.status == "candidate" for offer in result.offers)
    assert result.to_dict()["requires_claim"] is True
    assert result.schema_version == 1


def test_missing_neon_does_not_treat_labels_as_claim_permission() -> None:
    result = snapshot(
        [issue(7, "ralph-task")],
        [],
        [candidate("issue", 7)],
        problem="Neon credentials unavailable",
    )
    assert result.authoritative_claim_available is False
    assert result.offers[0].status == "held"
    assert "Neon credentials unavailable" in result.offers[0].hold_reasons
    assert result.offers[0].lease is None


def test_stale_lease_is_still_held_until_liveness_proved() -> None:
    stale = {"claim_id": "old", "active": False, "worker": "22", "expires_at": "yesterday"}
    result = snapshot(
        [issue(7)],
        [],
        [candidate("issue", 7)],
        leases={7: stale},
    )
    assert result.offers[0].status == "held"
    assert result.offers[0].lease == stale
    assert "unreconciled Neon lease" in " ".join(result.offers[0].hold_reasons)


def test_linked_issue_claim_blocks_pr_review() -> None:
    result = snapshot(
        [issue(7)],
        [pr(8, "factory:review")],
        [candidate("pr", 8, linked_issue=7, stage="factory:review")],
        leases={7: {"active": True, "worker": "1"}},
    )
    assert result.offers[0].status == "held"
    assert "#7" in " ".join(result.offers[0].hold_reasons)


def test_review_provenance_and_exact_head_are_not_assumed() -> None:
    result = snapshot(
        [],
        [pr(8, "factory:review", head="")],
        [candidate("pr", 8, stage="factory:review", contributors=("writer-1",))],
    )
    assert result.offers[0].status == "held"
    assert result.offers[0].provenance["head_contributors"] == ["writer-1"]
    assert result.offers[0].head_sha is None
    assert "Missing exact" in " ".join(result.offers[0].hold_reasons)


def test_issue_dependency_and_unknown_verdict_hold_closed() -> None:
    result = snapshot(
        [issue(7, body="Depends on #3"), issue(3)],
        [],
        [candidate("issue", 7)],
        verdicts={},
    )
    assert result.offers[0].blocked_by == ("issue:3",)
    assert result.offers[0].status == "held"
    assert "No complete issue eligibility verdict" in result.offers[0].hold_reasons
    assert result.holds[0]["target"] == "issue:3"


def test_an_issue_not_admitted_is_visible_as_a_hold() -> None:
    result = snapshot(
        [issue(7), issue(8)],
        [],
        [candidate("issue", 7)],
        verdicts={
            7: queue.Verdict("ready", ()),
            8: queue.Verdict(
                "waiting", (queue.Reason("review-limit", "Completion backlog saturated"),)
            ),
        },
    )
    assert result.holds[0]["target"] == "issue:8"
    assert result.holds[0]["reasons"] == ["Completion backlog saturated"]


def test_ambiguous_pr_stage_fails_closed() -> None:
    result = snapshot(
        [],
        [pr(8, "factory:review", "factory:ci")],
        [candidate("pr", 8, stage="factory:review")],
    )
    assert result.offers[0].status == "held"
    assert "ambiguous" in " ".join(result.offers[0].hold_reasons)
