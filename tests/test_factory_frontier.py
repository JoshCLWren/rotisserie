"""Agent-facing read-only work offers never grant authority to act."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rotisserie.factory_console.github import GitHub
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


def test_ledger_rows_are_bound_to_their_keys_not_stale_payload_overlap() -> None:
    # Old claim A covered both #9 and #10. Claim B replaced only #10.
    # Do not accidentally overwrite B with the stale A row.
    rows = [
        {
            "key": f"factory-lease-v1:{REPO}:target:10",
            "targets": [10],
            "claim_id": "new-B",
            "worker": "B",
            "active": True,
        },
        {
            "key": f"factory-lease-v1:{REPO}:target:9",
            "targets": [9, 10],
            "claim_id": "expired-A",
            "worker": "A",
            "active": False,
        },
        {
            "key": f"factory-lease-v1:{REPO}:worker:A",
            "targets": [9, 10],
            "claim_id": "expired-A",
            "worker": "A",
            "active": False,
        },
    ]
    leases = frontier._target_leases(REPO, rows)
    assert leases[10]["claim_id"] == "new-B"
    assert leases[10]["active"] is True
    assert leases[9]["claim_id"] == "expired-A"
    assert leases[9]["active"] is False
    result = snapshot(
        [issue(9), issue(10)], [], [candidate("issue", 9), candidate("issue", 10)], leases=leases
    )
    assert [offer.status for offer in result.offers] == ["held", "held"]
    assert [offer.lease["claim_id"] for offer in result.offers if offer.lease] == [
        "expired-A",
        "new-B",
    ]


def test_capture_passes_repository_capacity_to_shared_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rotisserie.factory_console import runtime
    from rotisserie.factory_console.service import providers

    settings: list[tuple[int, int]] = []

    class PolicyProxy:
        def build_candidates(self, issues: object, prs: object, **kwargs: int) -> list[Any]:
            settings.append((kwargs["pr_wip_limit"], kwargs["review_backlog_limit"]))
            return [candidate("issue", 7)]

        def labels_of(self, row: Any) -> set[str]:
            return {"factory:unowned"}

        def owner_of(self, labels: set[str]) -> str:
            return "factory:unowned"

        def parse_declared_dependencies(self, body: str) -> set[int]:
            return set()

        def declared_incidents(self, variables: object) -> dict[int, int]:
            return {}

    policy = PolicyProxy()
    monkeypatch.setattr(runtime, "completion_backlog", lambda rows: [])
    monkeypatch.setattr(runtime, "pr_wip_count", lambda rows: 0)
    monkeypatch.setattr(runtime, "module", lambda name: policy)
    monkeypatch.setattr(providers, "repository_variables", lambda gh: {})
    monkeypatch.setattr(frontier, "_active_leases", lambda repo: ({}, ""))
    monkeypatch.setattr(frontier, "_retry_history", lambda gh, core, now: [])
    for wip, backlog in [(8, 6), (14, 12)]:
        board = queue.Board([issue(7)], [], wip, backlog, {})
        summary = queue.QueueSummary(
            (queue.QueueEntry(7, "Issue 7", queue.Verdict("ready", ())),), board
        )
        monkeypatch.setattr(
            queue, "summarize_queue", lambda gh, *, now, variables, summary=summary: summary
        )
        github = SimpleNamespace(repo=REPO)
        result = frontier.capture(cast(GitHub, github), now=NOW)
        assert len(result.offers) == 1
        assert result.offers[0].capacity["review_limit"] == backlog
    assert settings == [(8, 6), (14, 12)]

    # No candidate becomes claimable if the gh list cap might hide work.
    capped = queue.Board([issue(number) for number in range(1, 1001)], [], 8, 6, {})
    capped_summary = queue.QueueSummary(
        (queue.QueueEntry(7, "Issue 7", queue.Verdict("ready", ())),), capped
    )
    monkeypatch.setattr(queue, "summarize_queue", lambda gh, *, now, variables: capped_summary)
    held = frontier.capture(cast(GitHub, SimpleNamespace(repo=REPO)), now=NOW)
    assert not held.authoritative_claim_available
    assert held.offers[0].status == "held"
    assert "pagination cap" in held.authority_note


def test_shared_policy_applies_nondefault_backpressure_to_candidates() -> None:
    from rotisserie.factory_console import runtime

    policy = runtime.module("factory_work_policy")
    rows = [
        {
            "number": number,
            "title": f"Review PR {number}",
            "headRefName": f"factory/worker-{number}",
            "headRefOid": "a" * 40,
            "labels": ["factory:review", "factory:unowned"],
        }
        for number in range(100, 106)
    ]
    fresh = issue(7, "factory:unowned")
    default = policy.build_candidates([fresh], rows, pr_wip_limit=8, review_backlog_limit=6)
    configured = policy.build_candidates([fresh], rows, pr_wip_limit=8, review_backlog_limit=12)
    assert not any(item.kind == "issue" and item.number == 7 for item in default)
    assert any(item.kind == "issue" and item.number == 7 for item in configured)
    assert all(item.kind == "pr" for item in default)


def test_unreadable_retry_history_holds_issue_intake_like_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rotisserie.factory_console import runtime
    from rotisserie.factory_console.service import providers

    recorded: dict[str, object] = {}
    real = runtime.module("factory_work_policy")

    class PolicyProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        def build_candidates(self, issues: Any, prs: Any, **kwargs: Any) -> list[Any]:
            recorded.update(kwargs)
            return [candidate("issue", 7)]

    monkeypatch.setattr(runtime, "completion_backlog", lambda rows: [])
    monkeypatch.setattr(runtime, "pr_wip_count", lambda rows: 0)
    monkeypatch.setattr(runtime, "module", lambda name: PolicyProxy())
    monkeypatch.setattr(providers, "repository_variables", lambda gh: {})
    monkeypatch.setattr(frontier, "_active_leases", lambda repo: ({}, ""))
    board = queue.Board([issue(7)], [], 8, 6, {})
    summary = queue.QueueSummary(
        (queue.QueueEntry(7, "Issue 7", queue.Verdict("ready", ())),), board
    )
    monkeypatch.setattr(queue, "summarize_queue", lambda gh, *, now, variables: summary)

    class NoHistory:
        repo = REPO

        def gh(self, *args: str, **kwargs: Any) -> str:
            from rotisserie.factory_console.github import GitHubError

            raise GitHubError("rate limited")

    held = frontier.capture(cast(GitHub, NoHistory()), now=NOW)
    assert recorded["no_diff_attempt_records"] is None
    assert held.offers[0].status == "held"
    assert frontier.RETRY_HISTORY_UNAVAILABLE in held.offers[0].hold_reasons


def test_retry_history_uses_the_controllers_trusted_marker_parser() -> None:
    import json as jsonlib

    from rotisserie.factory_console import runtime

    core = runtime.module("factory_work_policy")
    epoch = int(NOW.timestamp()) - 60
    marker = (
        f"<!-- widgets-factory-claim-released-v3:issue-7:3:{epoch}:no-persisted-change-handoff -->"
    )
    pages = [
        [
            {"id": 1, "body": marker, "author_association": "OWNER"},
            {"id": 2, "body": marker, "author_association": "NONE"},
        ]
    ]
    asked: list[str] = []

    class History:
        repo = REPO

        def gh(self, *args: str, **kwargs: Any) -> str:
            asked.append(args[-1])
            return jsonlib.dumps(pages)

    records = frontier._retry_history(cast(GitHub, History()), core, NOW)
    assert records is not None
    assert [(r.kind, r.number) for r in records] == [("issue", 7)]
    assert asked[0].startswith(f"repos/{REPO}/issues/comments?since=")
