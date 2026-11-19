"""Regression coverage for centralized factory assignment and leases."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = REPO_ROOT / ".github/scripts/factory-work-controller.py"
_BLOCKED_LABELS_FOR_RECONCILE_SKIP = (
    "duplicate",
    "factory:blocked",
    "invalid",
    "ralph-status:blocked",
    "wontfix",
)


@pytest.fixture(scope="module")
def controller() -> types.ModuleType:
    """Load the factory work controller once for this regression module."""
    spec = importlib.util.spec_from_file_location("factory_work_controller", CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stale_check_uses_merged_history_for_baseline(controller, monkeypatch) -> None:
    """The staleness baseline must include merged history, not only open PRs."""
    merged = {"number": 1, "state": "MERGED"}
    opened = {"number": 2, "state": "OPEN"}
    seen: dict[str, object] = {}

    class Guard:
        def get_baseline(self, prs: list[dict[str, Any]]) -> dict[str, Any]:
            seen["baseline"] = prs
            return {"threshold_seconds": 123_000, "sample_size": 100}

        def process_stale_prs(
            self, prs: list[dict[str, Any]] | None = None, dry_run: bool = False
        ) -> list[dict[str, Any]]:
            seen["process"] = prs
            return []

    monkeypatch.setattr(controller, "StalePRGuard", lambda: Guard())
    monkeypatch.setattr(controller, "list_factory_prs", lambda: [merged, opened])

    report = controller.check_stale_prs()

    assert report["baseline"] == {"threshold_seconds": 123_000, "sample_size": 100}
    assert seen == {"baseline": [merged, opened], "process": [merged, opened]}


def test_stale_check_reports_recoverable_prs_without_expiration(controller, monkeypatch) -> None:
    """Controller visibility reports recoverable stale work, never closures."""
    class Guard:
        def get_baseline(self, prs: list[dict[str, Any]]) -> dict[str, Any]:
            return {"threshold_seconds": 90_000, "sample_size": 100}

        def process_stale_prs(
            self, prs: list[dict[str, Any]] | None = None, dry_run: bool = False
        ) -> list[dict[str, Any]]:
            return [{"pr": 4727, "status": "stale-preserved", "issue": 4719}]

    monkeypatch.setattr(controller, "StalePRGuard", lambda: Guard())
    monkeypatch.setattr(controller, "list_factory_prs", lambda: [])
    report = controller.check_stale_prs()
    assert report["stale_preserved_count"] == 1
    assert report["expired_count"] == 0
    assert report["results"][0]["issue"] == 4719


def issue(
    number: int,
    *labels: str,
    created: str = "2026-08-16T12:00:00Z",
) -> dict[str, Any]:
    """Build a minimal issue payload for controller ranking tests."""
    return {
        "number": number,
        "title": f"Issue {number}",
        "labels": [{"name": label} for label in labels],
        "createdAt": created,
    }


def test_rotisserie_intake_preserves_legacy_stage(
    controller: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy control leaves current issue selection untouched."""
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"stage": "legacy"}), encoding="utf-8")
    monkeypatch.setattr(controller, "ROTISSERIE_CONTROL_PATH", control)

    assert controller.rotisserie_issue_intake([], []) is None


def test_rotisserie_intake_uses_public_decision_and_fails_closed(
    controller: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expanded intake accepts only eligible work from the Rotisserie CLI."""
    control = tmp_path / "control.json"
    control.write_text(
        json.dumps(
            {
                "stage": "expanded",
                "lane": {
                    "name": "issue-intake",
                    "subjects": ["label:factory:unowned"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(controller, "ROTISSERIE_CONTROL_PATH", control)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)

    def decide(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "evidence": {
                        "decisions": {
                            "observations": [
                                {
                                    "dimension": "eligibility",
                                    "subject": "work:101",
                                    "outcome": "eligible",
                                },
                                {
                                    "dimension": "eligibility",
                                    "subject": "work:102",
                                    "outcome": "blocked",
                                },
                            ]
                        }
                    },
                }
            ),
        )

    monkeypatch.setattr(controller.subprocess, "run", decide)
    issues = [
        issue(101, "factory", "factory:unowned"),
        issue(102, "factory", "factory:unowned"),
    ]
    assert controller.rotisserie_issue_intake(issues, []) == {101}

    control.write_text("{}", encoding="utf-8")
    assert controller.rotisserie_issue_intake(issues, []) == set()


def pr(
    number: int,
    branch: str,
    *labels: str,
    created: str = "2026-08-16T12:00:00Z",
    draft: bool = False,
) -> dict[str, Any]:
    """Build a minimal pull-request payload for controller ranking tests."""
    return {
        "number": number,
        "title": f"PR {number}",
        "labels": [{"name": label} for label in labels],
        "headRefName": branch,
        "createdAt": created,
        "isDraft": draft,
    }


def test_user_report_beats_ordinary_product_and_e2e(controller: types.ModuleType) -> None:
    """Verify user report beats ordinary product and e2e."""
    candidates = controller.build_candidates(
        [
            issue(101, "bug", "e2e-discovered", "factory:unowned"),
            issue(102, "enhancement", "factory:unowned"),
            issue(103, "bug", "user-reported", "factory:unowned"),
        ],
        [],
    )
    assert [(candidate.number, candidate.lane) for candidate in candidates] == [
        (103, 1),
        (102, 3),
        (101, 4),
    ]


def test_ordinary_product_beats_e2e_even_when_e2e_is_newer(
    controller: types.ModuleType,
) -> None:
    """Verify ordinary product beats e2e even when e2e is newer."""
    candidates = controller.build_candidates(
        [
            issue(
                201,
                "bug",
                "e2e-discovered",
                "factory:unowned",
                created="2026-08-16T15:00:00Z",
            ),
            issue(
                202,
                "enhancement",
                "factory:unowned",
                created="2026-08-01T15:00:00Z",
            ),
        ],
        [],
    )
    assert [candidate.number for candidate in candidates] == [202, 201]


def test_e2e_is_selected_when_higher_lanes_are_empty(controller: types.ModuleType) -> None:
    """Verify e2e is selected when higher lanes are empty."""
    candidates = controller.build_candidates(
        [issue(301, "bug", "e2e-discovered", "factory:unowned")],
        [],
    )
    assert [(candidate.number, candidate.lane) for candidate in candidates] == [(301, 4)]


def test_equal_priority_items_drain_oldest_first(
    controller: types.ModuleType,
) -> None:
    """Verify equal priority items drain oldest first so backlogged work cannot starve."""
    candidates = controller.build_candidates(
        [
            issue(
                310,
                "bug",
                "user-reported",
                "factory:unowned",
                created="2026-08-15T12:00:00Z",
            ),
            issue(
                311,
                "bug",
                "user-reported",
                "factory:unowned",
                created="2026-08-16T12:00:00Z",
            ),
        ],
        [],
    )
    assert [candidate.number for candidate in candidates] == [310, 311]


def test_user_bug_pr_repair_inherits_priority_without_worker_affinity(
    controller: types.ModuleType,
) -> None:
    """Verify user bug pr repair inherits priority without worker affinity."""
    candidates = controller.build_candidates(
        [
            issue(401, "bug", "user-reported", "factory:unowned"),
            issue(402, "enhancement", "factory:unowned"),
        ],
        [
            pr(
                1401,
                "factory/27-401-fix",
                "factory",
                "factory:unowned",
                "factory:changes-requested",
            )
        ],
    )
    assert [(candidate.kind, candidate.number, candidate.lane) for candidate in candidates] == [
        ("pr", 1401, 2),
        ("issue", 402, 3),
    ]


def test_controller_uses_only_canonical_worker_issue_branch_shape(
    controller: types.ModuleType,
) -> None:
    """Verify controller uses only canonical worker issue branch shape."""
    assert controller.linked_issue_from_branch("factory/27-401-repair") == 401
    assert controller.linked_issue_from_branch("factory/27-repair") is None
    assert controller.linked_issue_from_branch("factory/401-repair") is None


def test_draft_pr_suppresses_linked_issue_until_pr_closes(
    controller: types.ModuleType,
) -> None:
    """A draft canonical PR still owns issue identity until explicitly closed."""
    candidates = controller.build_candidates(
        [issue(411, "bug", "user-reported", "factory:unowned")],
        [
            pr(
                1411,
                "factory/27-411-fix",
                "factory",
                "factory:unowned",
                draft=True,
            )
        ],
    )
    assert candidates == []


def test_blocked_pr_suppresses_linked_issue_until_pr_closes(
    controller: types.ModuleType,
) -> None:
    """A blocked canonical PR cannot silently spawn a replacement implementation."""
    candidates = controller.build_candidates(
        [issue(412, "bug", "user-reported", "factory:unowned")],
        [
            pr(
                1412,
                "factory/27-412-fix",
                "factory",
                "factory:unowned",
                "factory:blocked",
            )
        ],
    )
    assert candidates == []


def test_ready_pr_is_reserved_for_merge_controller_and_suppresses_duplicate_issue(
    controller: types.ModuleType,
) -> None:
    """Verify ready pr is reserved for merge controller and suppresses duplicate issue."""
    candidates = controller.build_candidates(
        [issue(501, "bug", "user-reported", "factory:unowned")],
        [
            pr(
                1501,
                "factory/13-501-opencode-free",
                "factory",
                "factory:unowned",
                "factory:ready",
            )
        ],
    )
    assert candidates == []


def test_urgent_defects_bypass_wip_but_not_review_backlog_saturation(
    controller: types.ModuleType,
) -> None:
    """Urgent defects bypass worker WIP only; main-breakage alone beats backlog."""
    backlog = [
        pr(
            400 + index,
            f"factory/{10 + index}-990-fix",
            "factory:unowned",
            "factory:review",
            created=f"2026-08-20T12:00:{index:02d}Z",
        )
        for index in range(20)
    ]
    ordinary = issue(501, "enhancement", "factory:unowned")
    urgent = issue(502, "bug", "user-reported", "factory:unowned")
    breakage = issue(503, "bug", "main-breakage", "factory:unowned")

    candidates = controller.build_candidates([ordinary, urgent, breakage], backlog)

    produced = {candidate.number for candidate in candidates if candidate.kind == "issue"}
    assert "pr" in {candidate.kind for candidate in candidates}
    assert 501 not in produced, "ordinary issue intake must stop under backlog"
    assert 502 not in produced, "urgent user-reported defects are stopped by backlog saturation"
    assert 503 in produced, "main-breakage remains executable under backlog saturation"

    wip = [
        pr(
            430 + index,
            f"factory/{40 + index}-991-fix",
            f"factory:{20 + index}",
            "factory:review" if index < 2 else "factory:building",
            created=f"2026-08-20T12:00:{index:02d}Z",
        )
        for index in range(controller.FACTORY_PR_WIP_LIMIT)
    ]
    wip_candidates = controller.build_candidates([ordinary, urgent], wip)

    wip_produced = {
        candidate.number for candidate in wip_candidates if candidate.kind == "issue"
    }
    assert 501 not in wip_produced, "ordinary issue intake must stop under worker WIP"
    assert 502 in wip_produced, "urgent user-reported defects still bypass worker WIP"


def test_human_local_pr_closing_issue_suppresses_controller_intake(
    controller: types.ModuleType,
) -> None:
    """The central controller suppresses intake when any open PR closes an issue."""
    candidates = controller.build_candidates(
        [
            {
                "number": 2127,
                "title": "CBL chain",
                "labels": [{"name": "factory:unowned"}],
                "createdAt": "2026-09-04T12:00:00Z",
            }
        ],
        [
            {
                "number": 2161,
                "title": "Complete CBL implementation",
                "labels": [],
                "headRefName": "local/2127-cbl-commit",
                "body": "Closes #2127.\n\nImplements the full chain.",
                "createdAt": "2026-09-04T13:00:00Z",
                "isDraft": False,
            }
        ],
    )
    assert candidates == []


def test_controller_does_not_suppress_on_casual_hash_mention(
    controller: types.ModuleType,
) -> None:
    """A PR that only references ``#N`` without closing it never blocks intake."""
    candidates = controller.build_candidates(
        [
            {
                "number": 2127,
                "title": "CBL chain",
                "labels": [{"name": "factory:unowned"}],
                "createdAt": "2026-09-04T12:00:00Z",
            }
        ],
        [
            {
                "number": 2170,
                "title": "Stacked child work",
                "labels": [],
                "headRefName": "local/stacked-child",
                "body": "Depends on #2127 for context.",
                "createdAt": "2026-09-04T13:00:00Z",
                "isDraft": False,
            }
        ],
    )
    assert any(
        candidate.kind == "issue" and candidate.number == 2127
        for candidate in candidates
    )


def test_strike_retry_excludes_only_failed_producer_until_new_implementation_claim(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = {
        "body": "<!-- comic-pile-factory-strike-reset-v1:issue-77:pr-88:excluded-producer-12 -->",
        "performed_via_github_app": {"slug": "github-actions"},
    }
    monkeypatch.setattr(controller, "gh_json", lambda *args, **kwargs: [[reset]])
    assert controller.issue_excludes_worker_on_strike_retry(77, "12") is True
    assert controller.issue_excludes_worker_on_strike_retry(77, "13") is False

    claim = {
        "body": "<!-- comic-pile-factory-implement-claim-v3:issue-77:opencode-free-model-factory-13:1780000000:attempt-1 -->",
        "performed_via_github_app": {"slug": "github-actions"},
    }
    monkeypatch.setattr(controller, "gh_json", lambda *args, **kwargs: [[reset, claim]])
    assert controller.issue_excludes_worker_on_strike_retry(77, "12") is False


def test_assign_accepts_kinds_parameter(controller: types.ModuleType) -> None:
    """assign() must accept a kinds parameter with default None."""
    import inspect
    sig = inspect.signature(controller.assign)
    assert "kinds" in sig.parameters
    assert sig.parameters["kinds"].default is None


def review_pr(number: int, head: str) -> dict[str, Any]:
    """Build an open review-stage factory pull request payload."""
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "title": f"PR {number}",
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": "factory:review"},
        ],
        "headRefName": f"factory/29-{number}-opencode-free",
        "headRefOid": head,
        "body": "Closes #1386.",
        "createdAt": "2026-08-16T00:00:00Z",
    }


def trusted_comment(body: str) -> dict[str, Any]:
    """Build a controller-authored pull request comment."""
    return {"user": {"login": "github-actions[bot]"}, "author_association": "NONE", "body": body}


def contributor_marker(pr: int, head: str, worker: str) -> str:
    """Return the exact marker the controller writes at push time."""
    return (
        "<!-- comic-pile-factory-head-contributor-v1:"
        f"pr-{pr}:head-{head}:worker-{worker}:epoch-1234567890 -->"
    )


def test_dispatcher_routes_a_repairers_head_to_another_reviewer(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selection reads exact-head provenance so the repairer is never re-dispatched.

    #3059 requires queue selection to choose another eligible reviewer without
    human intervention. Refusing the verdict in the review controller is not
    enough: the same worker would keep receiving the head and keep burning its
    lease. The dispatcher list payload carries no comments, so the contributors
    for review-stage candidates have to be resolved before ordering.
    """
    head = "a" * 40
    pr = review_pr(3073, head)
    monkeypatch.setattr(controller, "gh_json", lambda *args, **kwargs: [[trusted_comment(
        contributor_marker(3073, head, "29")
    ), trusted_comment(contributor_marker(3073, head, "59"))]])

    candidates = controller.resolve_review_head_contributors(
        controller.build_candidates([], [pr]), [pr]
    )

    assert [item.head_contributors for item in candidates] == [frozenset({"29", "59"})]
    assert controller.order_candidates_for_worker(candidates, "59") == []
    assert controller.order_candidates_for_worker(candidates, "29") == []
    assert controller.order_candidates_for_worker(candidates, "17") == candidates


def test_dispatcher_excludes_a_superseded_repairer_from_the_newer_head(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker recorded on an earlier head of the PR stays out of its review pool.

    Repairs stack onto the previous head, so the third repairer's head still
    carries the second repairer's commits. Selecting on the current head's record
    alone would hand that head back to the worker that authored part of it.
    """
    pr = review_pr(3073, "c" * 40)
    monkeypatch.setattr(
        controller,
        "gh_json",
        lambda *args, **kwargs: [[
            trusted_comment(contributor_marker(3073, "a" * 40, "29")),
            trusted_comment(contributor_marker(3073, "b" * 40, "59")),
            trusted_comment(contributor_marker(3073, "c" * 40, "77")),
        ]],
    )

    candidates = controller.resolve_review_head_contributors(
        controller.build_candidates([], [pr]), [pr]
    )

    assert [item.head_contributors for item in candidates] == [frozenset({"29", "59", "77"})]
    for contributor in ("29", "59", "77"):
        assert controller.order_candidates_for_worker(candidates, contributor) == []
    assert controller.order_candidates_for_worker(candidates, "17") == candidates


def test_dispatcher_keeps_captured_contributors_without_extra_reads(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Rotisserie capture already carries provenance; do not re-read comments."""
    head = "a" * 40
    pr = review_pr(3073, head)
    pr["head_contributors"] = ["29", "59"]

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("captured provenance must not trigger a comment read")

    monkeypatch.setattr(controller, "gh_json", fail)

    candidates = controller.resolve_review_head_contributors(
        controller.build_candidates([], [pr]), [pr]
    )

    assert [item.head_contributors for item in candidates] == [frozenset({"29", "59"})]


def test_unreadable_provenance_narrows_nothing_at_selection(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed provenance read must not widen who the controller will accept."""
    head = "a" * 40
    pr = review_pr(3073, head)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("gh rate limited")

    monkeypatch.setattr(controller, "gh_json", unavailable)

    candidates = controller.resolve_review_head_contributors(
        controller.build_candidates([], [pr]), [pr]
    )

    assert [item.head_contributors for item in candidates] == [frozenset()]
    # The producer is still excluded, and the apparent reviewer stays eligible,
    # but the review controller refuses that head without two distinct reviews.
    assert controller.order_candidates_for_worker(candidates, "29") == []
    assert controller.order_candidates_for_worker(candidates, "59") == candidates


def test_untrusted_authors_cannot_steer_reviewer_selection(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only trusted provenance can remove a worker from the reviewer pool."""
    head = "a" * 40
    pr = review_pr(3073, head)
    forged = {
        "user": {"login": "somebody-else"},
        "author_association": "CONTRIBUTOR",
        "body": contributor_marker(3073, head, "17"),
    }
    monkeypatch.setattr(controller, "gh_json", lambda *args, **kwargs: [[forged]])

    candidates = controller.resolve_review_head_contributors(
        controller.build_candidates([], [pr]), [pr]
    )

    assert [item.head_contributors for item in candidates] == [frozenset()]
    assert controller.order_candidates_for_worker(candidates, "17") == candidates


def test_assign_candidate_refuses_second_active_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """assign_candidate must refuse when the worker already holds an active lease."""
    monkeypatch.setattr(controller, "issue_has_open_blocker", lambda _n, *, live=False: False)
    monkeypatch.setattr(controller, "acquire_lease", lambda *_args: "synthetic-lease")
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda w: w == "99")
    monkeypatch.setattr(controller, "issue_has_open_blocker", lambda n, *, live=False: False)
    monkeypatch.setattr(controller, "acquire_lease", lambda *_args: "test-lease")
    monkeypatch.setattr(controller, "target_still_unowned", lambda n: True)
    monkeypatch.setattr(controller, "replace_factory_labels", lambda *a, **k: None)
    monkeypatch.setattr(controller, "target_owned_by", lambda num, owner: True)
    monkeypatch.setattr(controller, "record_controller_lease_activity", lambda *a, **k: None)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    class FakeCandidate:
        number = 1
        kind = "issue"
        linked_issue = None
        conflicted = False

    assert controller.assign_candidate(FakeCandidate(), "99") is False
    assert controller.assign_candidate(FakeCandidate(), "1") is True


def test_selective_conflict_recovery_canonical_pr_plus_stray_issue(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that canonical PR survives and stray issue is released."""
    # Mock GitHub API responses
    issues = [
        {
            "number": 1001,
            "title": "Stray issue owned by worker",
            "labels": [{"name": "factory:54"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    prs = [
        {
            "number": 2001,
            "title": "Canonical PR for issue 1000",
            "labels": [{"name": "factory:54"}],
            "headRefName": "factory/54-1000-fix",
            "state": "open",
            "isDraft": False,
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    # Mock target_json to verify ownership
    def mock_target_json(number):
        if number == 1001:
            return {
                "number": 1001,
                "title": "Stray issue owned by worker",
                "labels": [{"name": "factory:54"}],
                "state": "open",
            }
        elif number == 2001:
            return {
                "number": 2001,
                "title": "Canonical PR for issue 1000",
                "labels": [{"name": "factory:54"}],
                "state": "open",
            }
        return {}

    # Mock replace_factory_labels to track calls
    replaced_labels = []
    def mock_replace_factory_labels(number, owner, stage=None):
        replaced_labels.append((number, owner, stage))

    # Mock record_claim_released to track calls
    recorded_releases = []
    def mock_record_claim_released(number, worker, kind, reason):
        recorded_releases.append((number, worker, kind, reason))

    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "target_json", mock_target_json)
    monkeypatch.setattr(controller, "replace_factory_labels", mock_replace_factory_labels)
    monkeypatch.setattr(controller, "record_claim_released", mock_record_claim_released)
    monkeypatch.setattr(controller, "target_owned_by", lambda num, owner: True)

    # Run the conflict recovery
    result = controller.selectivity_conflict_recovery("54")

    # Verify results
    assert result["worker"] == "54"
    assert 1001 in result["recovered_issues"]
    assert 2001 in result["canonical_prs_preserved"]
    assert 1001 in result["orphan_issues_released"]
    assert len(result["errors"]) == 0

    # Verify that the stray issue was released
    assert (1001, "factory:unowned", None) in replaced_labels
    assert (1001, "54", "issue", "selective-conflict-recovery") in recorded_releases

    # Verify that the canonical PR was not touched
    pr_label_changes = [call for call in replaced_labels if call[0] == 2001]
    assert len(pr_label_changes) == 0


def test_selective_conflict_recovery_orphan_issue_plus_canonical_pr(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that orphan issue is released when canonical PR exists."""
    # Mock GitHub API responses
    issues = [
        {
            "number": 1002,
            "title": "Orphan issue owned by worker",
            "labels": [{"name": "factory:54"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    prs = [
        {
            "number": 2002,
            "title": "Canonical PR that closes issue 1002",
            "labels": [{"name": "factory:10"}],  # Owned by different worker
            "headRefName": "factory/10-1002-fix",
            "state": "open",
            "isDraft": False,
            "createdAt": "2026-09-01T12:00:00Z",
            "body": "Closes #1002",
        }
    ]

    # Mock target_json to verify ownership
    def mock_target_json(number):
        if number == 1002:
            return {
                "number": 1002,
                "title": "Orphan issue owned by worker",
                "labels": [{"name": "factory:54"}],
                "state": "open",
            }
        elif number == 2002:
            return {
                "number": 2002,
                "title": "Canonical PR that closes issue 1002",
                "labels": [{"name": "factory:10"}],
                "state": "open",
            }
        return {}

    # Mock replace_factory_labels to track calls
    replaced_labels = []
    def mock_replace_factory_labels(number, owner, stage=None):
        replaced_labels.append((number, owner, stage))

    # Mock record_claim_released to track calls
    recorded_releases = []
    def mock_record_claim_released(number, worker, kind, reason):
        recorded_releases.append((number, worker, kind, reason))

    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "target_json", mock_target_json)
    monkeypatch.setattr(controller, "replace_factory_labels", mock_replace_factory_labels)
    monkeypatch.setattr(controller, "record_claim_released", mock_record_claim_released)
    monkeypatch.setattr(controller, "target_owned_by", lambda num, owner: True)

    # Run the conflict recovery
    result = controller.selectivity_conflict_recovery("54")

    # Verify results
    assert result["worker"] == "54"
    assert 1002 in result["recovered_issues"]
    assert 1002 in result["orphan_issues_released"]
    assert len(result["errors"]) == 0

    # Verify that the orphan issue was released
    assert (1002, "factory:unowned", None) in replaced_labels
    assert (1002, "54", "issue", "selective-conflict-recovery") in recorded_releases


def test_selective_conflict_recovery_ambiguous_cases_fail_closed(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that ambiguous multi-PR/multi-issue corruption fails closed."""
    # Mock GitHub API responses with ambiguous case
    issues = [
        {
            "number": 1003,
            "title": "Issue owned by worker",
            "labels": [{"name": "factory:54"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        },
        {
            "number": 1004,
            "title": "Another issue owned by worker",
            "labels": [{"name": "factory:54"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    prs = [
        {
            "number": 2003,
            "title": "PR for issue 1003",
            "labels": [{"name": "factory:10"}],
            "headRefName": "factory/10-1003-fix",
            "state": "open",
            "isDraft": False,
            "createdAt": "2026-09-01T12:00:00Z",
        },
        {
            "number": 2004,
            "title": "PR for issue 1004",
            "labels": [{"name": "factory:20"}],
            "headRefName": "factory/20-1004-fix",
            "state": "open",
            "isDraft": False,
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "target_owned_by", lambda num, owner: True)

    # Run the conflict recovery
    result = controller.selectivity_conflict_recovery("54")

    # Verify that ambiguous cases are detected and not resolved
    assert len(result["ambiguous_cases"]) == 2
    assert {case["issue"] for case in result["ambiguous_cases"]} == {1003, 1004}
    assert all(
        case["reason"] == "multiple workers could claim this issue"
        for case in result["ambiguous_cases"]
    )
    assert len(result["recovered_issues"]) == 0
    assert len(result["recovered_prs"]) == 0
    assert len(result["canonical_prs_preserved"]) == 0
    assert len(result["orphan_issues_released"]) == 0


def test_selective_conflict_recovery_canonical_pair_is_not_ambiguous(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a worker's own canonical PR never flags its linked issue as ambiguous."""
    issues = [
        {
            "number": 1007,
            "title": "Canonical issue owned by worker with its PR",
            "labels": [{"name": "factory:54"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        },
        {
            "number": 1008,
            "title": "Stray issue owned by worker",
            "labels": [{"name": "factory:54"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    prs = [
        {
            "number": 2007,
            "title": "Canonical PR for issue 1007",
            "labels": [{"name": "factory:54"}],
            "headRefName": "factory/54-1007-fix",
            "state": "open",
            "isDraft": False,
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    def mock_target_json(number):
        if number == 1007:
            return {
                "number": 1007,
                "title": "Canonical issue owned by worker with its PR",
                "labels": [{"name": "factory:54"}],
                "state": "open",
            }
        elif number == 1008:
            return {
                "number": 1008,
                "title": "Stray issue owned by worker",
                "labels": [{"name": "factory:54"}],
                "state": "open",
            }
        elif number == 2007:
            return {
                "number": 2007,
                "title": "Canonical PR for issue 1007",
                "labels": [{"name": "factory:54"}],
                "state": "open",
            }
        return {}

    replaced_labels = []
    def mock_replace_factory_labels(number, owner, stage=None):
        replaced_labels.append((number, owner, stage))

    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "target_json", mock_target_json)
    monkeypatch.setattr(controller, "replace_factory_labels", mock_replace_factory_labels)
    monkeypatch.setattr(controller, "record_claim_released", lambda *a, **k: None)
    monkeypatch.setattr(controller, "target_owned_by", lambda num, owner: True)

    result = controller.selectivity_conflict_recovery("54")

    # The canonical pair is provably owned, so no spurious ambiguity is reported.
    assert len(result["ambiguous_cases"]) == 0

    # Only the stray issue is released; the canonical issue and PR survive.
    assert 1008 in result["recovered_issues"]
    assert 1008 in result["orphan_issues_released"]
    assert 2007 in result["canonical_prs_preserved"]
    assert len(result["errors"]) == 0
    assert (1008, "factory:unowned") == replaced_labels[0][:2]
    assert len(replaced_labels) == 1


def test_selective_conflict_recovery_preserves_label_state_invariants(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that label-state invariants are preserved during recovery."""
    # Mock GitHub API responses
    issues = [
        {
            "number": 1005,
            "title": "Stray issue owned by worker",
            "labels": [{"name": "factory:54"}, {"name": "factory:building"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    prs = [
        {
            "number": 2005,
            "title": "Canonical PR for issue 1000",
            "labels": [{"name": "factory:54"}, {"name": "factory:changes-requested"}],
            "headRefName": "factory/54-1000-fix",
            "state": "open",
            "isDraft": False,
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    # Mock target_json to verify ownership and labels
    def mock_target_json(number):
        if number == 1005:
            return {
                "number": 1005,
                "title": "Stray issue owned by worker",
                "labels": [{"name": "factory:54"}, {"name": "factory:building"}],
                "state": "open",
            }
        elif number == 2005:
            return {
                "number": 2005,
                "title": "Canonical PR for issue 1000",
                "labels": [{"name": "factory:54"}, {"name": "factory:changes-requested"}],
                "state": "open",
            }
        return {}

    # Mock replace_factory_labels to track calls
    replaced_labels = []
    def mock_replace_factory_labels(number, owner, stage=None):
        replaced_labels.append((number, owner, stage))

    # Mock record_claim_released to track calls
    recorded_releases = []
    def mock_record_claim_released(number, worker, kind, reason):
        recorded_releases.append((number, worker, kind, reason))

    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "target_json", mock_target_json)
    monkeypatch.setattr(controller, "replace_factory_labels", mock_replace_factory_labels)
    monkeypatch.setattr(controller, "record_claim_released", mock_record_claim_released)
    monkeypatch.setattr(controller, "target_owned_by", lambda num, owner: True)

    # Run the conflict recovery
    result = controller.selectivity_conflict_recovery("54")

    # Verify results
    assert result["worker"] == "54"
    assert 1005 in result["recovered_issues"]
    assert 2005 in result["canonical_prs_preserved"]
    assert len(result["errors"]) == 0

    # Verify that the stray issue was released with proper stage handling
    issue_releases = [call for call in replaced_labels if call[0] == 1005]
    assert len(issue_releases) == 1
    assert issue_releases[0][1] == "factory:unowned"  # Owner released
    # Stage should be None when releasing an issue that's not the PR's linked issue

    # Verify that the canonical PR's stage was preserved
    pr_changes = [call for call in replaced_labels if call[0] == 2005]
    assert len(pr_changes) == 0  # PR should not be touched


def test_cli_conflict_recovery_command(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test that the conflict recovery CLI command works correctly."""
    # Mock the selective_conflict_recovery function
    mock_recovery = Mock(return_value={
        "worker": "54",
        "recovered_issues": [1001],
        "canonical_prs_preserved": [2001],
        "orphan_issues_released": [1001],
        "errors": []
    })

    monkeypatch.setattr(controller, "selectivity_conflict_recovery", mock_recovery)

    # Simulate CLI command execution
    original_argv = sys.argv
    sys.argv = ["factory-work-controller.py", "conflict-recovery", "--worker", "54"]

    # Capture stdout
    try:
        with capsys.disabled():
            result = controller.main()
    finally:
        sys.argv = original_argv

    # Should return 0 for success
    assert result == 0

    # The function should have been called with the right worker
    assert mock_recovery.call_count == 1
    assert mock_recovery.call_args.args[0] == "54"


def test_selective_conflict_recovery_current_owner_verification(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that worker ownership is verified before releasing."""
    # Mock GitHub API responses
    issues = [
        {
            "number": 1006,
            "title": "Stray issue owned by worker",
            "labels": [{"name": "factory:54"}],
            "state": "open",
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    prs = [
        {
            "number": 2006,
            "title": "Canonical PR for issue 1000",
            "labels": [{"name": "factory:54"}],
            "headRefName": "factory/54-1000-fix",
            "state": "open",
            "isDraft": False,
            "createdAt": "2026-09-01T12:00:00Z",
        }
    ]

    # Mock target_json to return different ownership (worker no longer owns)
    def mock_target_json(number):
        if number == 1006:
            return {
                "number": 1006,
                "title": "Stray issue now owned by someone else",
                "labels": [{"name": "factory:10"}],  # Ownership changed
                "state": "open",
            }
        elif number == 2006:
            return {
                "number": 2006,
                "title": "Canonical PR for issue 1000",
                "labels": [{"name": "factory:54"}],
                "state": "open",
            }
        return {}

    # Mock replace_factory_labels to track calls
    replaced_labels = []
    def mock_replace_factory_labels(number, owner, stage=None):
        replaced_labels.append((number, owner, stage))

    # Mock record_claim_released to track calls
    recorded_releases = []
    def mock_record_claim_released(number, worker, kind, reason):
        recorded_releases.append((number, worker, kind, reason))

    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "target_json", mock_target_json)
    monkeypatch.setattr(controller, "replace_factory_labels", mock_replace_factory_labels)
    monkeypatch.setattr(controller, "record_claim_released", mock_record_claim_released)
    monkeypatch.setattr(controller, "target_owned_by", lambda num, owner: num == 2006)  # Only PR is still owned

    # Run the conflict recovery
    result = controller.selectivity_conflict_recovery("54")

    # Verify that the stray issue was NOT released because ownership changed
    assert len(result["recovered_issues"]) == 0
    assert len(result["orphan_issues_released"]) == 0
    assert len(result["errors"]) == 0

    # Verify that no labels were changed
    assert len(replaced_labels) == 0
    assert len(recorded_releases) == 0


# ... existing code ...
def test_reconcile_contradictory_labels_repairs_unowned_plus_worker(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unowned coexisting with one worker lease keeps the worker and drops unowned."""
    issues = [issue(3001, "factory", "factory:unowned", "factory:13", "factory:building")]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == [3001]
    assert replaced == [(3001, "factory:13", "factory:building")]


def test_reconcile_contradictory_labels_repairs_building_unowned(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify factory:building + factory:unowned is repaired to factory:unowned with no stage."""
    issues = [issue(3005, "factory", "factory:unowned", "factory:building")]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == [3005]
    assert replaced == [(3005, "factory:unowned", None)]


def test_reconcile_contradictory_labels_repairs_blocked_unowned(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ralph-status:blocked holds without a live factory lease are not rewritten."""
    issues = [
        issue(3006, "ralph-status:blocked"),
        issue(3011, "ralph-status:blocked", "factory:unowned"),
    ]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == []
    assert replaced == []


@pytest.mark.parametrize("blocked_label", _BLOCKED_LABELS_FOR_RECONCILE_SKIP)
def test_reconcile_contradictory_labels_skips_each_blocked_label_without_factory_labels(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, blocked_label: str
) -> None:
    """Every BLOCKED_LABELS member with no factory labels must produce no writes."""
    assert set(_BLOCKED_LABELS_FOR_RECONCILE_SKIP) == set(controller.BLOCKED_LABELS)
    issues = [issue(4000, blocked_label)]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == []
    assert replaced == []


def test_reconcile_contradictory_labels_repairs_blocked_with_live_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live factory:NN lease on a blocked item is still normalized."""
    issues = [
        issue(
            3010,
            "ralph-status:blocked",
            "factory",
            "factory:unowned",
            "factory:13",
            "factory:building",
        )
    ]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == [3010]
    assert replaced == [(3010, "factory:13", "factory:building")]


def test_reconcile_contradictory_labels_skips_manual_only_pr(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual-only PR must not be rewritten even when labels are contradictory.

    The held PR carries a live factory:NN lease plus factory:unowned, which
    reconcile_contradictory_labels would otherwise repair. A blocked-unowned
    hold is skipped by the BLOCKED_LABELS guard, so it would not uniquely
    cover is_manual_only(). The sibling uses the same labels without the
    marker and must still be repaired.
    """
    contradictory = (
        "factory",
        "factory:unowned",
        "factory:13",
        "factory:building",
    )
    hold = pr(3971, "infra/factory-local-hard-stop", *contradictory)
    hold["body"] = "<!-- factory-execution:manual-only -->\nHold this factory PR.\n"
    line_hold = pr(4512, "cursor/revert-auth", *contradictory)
    line_hold["body"] = "P0 auth outage rollback.\n\nmanual-only\n"
    sibling = pr(3972, "infra/factory-sibling", *contradictory)
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels([], [hold, line_hold, sibling]) == [3972]
    assert replaced == [(3972, "factory:13", "factory:building")]


def test_reconcile_contradictory_labels_repairs_done_state(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ralph-status:done removes all transient factory labels."""
    issues = [issue(3007, "ralph-status:done", "factory:13", "factory:building")]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == [3007]
    assert replaced == [(3007, "factory:unowned", None)]


def test_reconcile_contradictory_labels_repairs_unowned_plus_single_worker(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-persisted-change handoff can leave one active owner behind with
    factory:unowned; reconciliation must drop the redundant unowned marker
    and keep the legitimate owner rather than silently skipping the target."""
    issues = [issue(3008, "factory", "factory:unowned", "factory:13", "factory:building")]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == [3008]
    assert replaced == [(3008, "factory:13", "factory:building")]


def test_reconcile_contradictory_labels_repairs_in_progress_unowned(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executable work must never remain ralph-status:in-progress without a real
    owner; reconciliation must normalize it to unowned with no stage."""
    issues = [issue(3009, "ralph-status:in-progress", "factory:unowned")]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == [3009]
    assert replaced == [(3009, "factory:unowned", None)]


def test_reconcile_contradictory_labels_fails_closed_on_two_workers(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two distinct active leases must not be resolved by guessing."""
    issues = [issue(3002, "factory", "factory:unowned", "factory:13", "factory:14")]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == []
    assert replaced == []



def test_reconcile_contradictory_labels_leaves_clean_targets_alone(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Singly-owned and purely-unowned targets are not rewritten."""
    issues = [
        issue(3003, "factory", "factory:13", "factory:building"),
        issue(3004, "factory", "factory:unowned"),
    ]
    replaced: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: replaced.append((number, owner, stage)),
    )

    assert controller.reconcile_contradictory_labels(issues, []) == []
    assert replaced == []


@pytest.mark.parametrize("use_jev", [True, False])
def test_real_assign_uses_jev_when_confident_and_canonical_when_absent(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    use_jev: bool,
) -> None:
    """Jev must affect assign() itself, not just an advisory report."""
    # Legacy test worker 6 is intentionally simulated, not in today's roster.
    monkeypatch.setattr(controller, "worker_sources", lambda: {"6": "nvidia"})
    # The simulated worker is credentialed; assign() checks this flag.
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    numbers = [33001, 33002, 33003]
    issues = [
        {
            "number": number,
            "title": f"Product issue {number}",
            "body": "Improve reader reliability.",
            "state": "OPEN",
            "createdAt": f"2026-10-{index + 1:02d}T12:00:00Z",
            "labels": [
                {"name": "factory:unowned"},
                {"name": "ralph-priority:medium"},
            ],
        }
        for index, number in enumerate(numbers)
    ]
    report = {
        "model": "typesafe/jev-1.14",
        "mode": "active-selection",
        "surface": "issue-selection",
        "status": "scored",
        "baseline": numbers,
        "shadow": [numbers[-1], *numbers[:-1]],
        "qualified": numbers,
    }
    report_path = tmp_path / "jev.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    if use_jev:
        monkeypatch.setenv("FACTORY_JEV_RANKING_FILE", str(report_path))
    else:
        monkeypatch.delenv("FACTORY_JEV_RANKING_FILE", raising=False)

    monkeypatch.setattr(controller, "quota_pause_for", lambda _w: None)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _w: False)
    monkeypatch.setattr(controller, "budget_block_reason", lambda _w: None)
    monkeypatch.setattr(
        controller, "omniroute_free_entry_capacity",
        lambda: {"remaining": 3},
    )
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "reconcile_contradictory_labels", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: [])
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda: [])
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda _i, _p: None)
    monkeypatch.setattr(
        controller, "resolve_review_head_contributors", lambda candidates, _prs: candidates
    )
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _c: True)
    monkeypatch.setattr(
        controller, "issue_excludes_worker_on_strike_retry", lambda _n, _w: False
    )
    claimed: list[int] = []
    sources: list[str | None] = []

    def assign_candidate(
        candidate: object, _worker: str, source: str | None = None
    ) -> bool:
        claimed.append(int(vars(candidate)["number"]))
        sources.append(source)
        return True

    monkeypatch.setattr(controller, "assign_candidate", assign_candidate)
    result = controller.assign("6")
    assert result is not None
    assert claimed == [numbers[-1] if use_jev else numbers[0]]
    assert sources == ["jev" if use_jev else None]
    stderr = capsys.readouterr().err
    if use_jev:
        assert "Jev ACTIVE worker=6" in stderr
        assert (
            f"Jev RESULT worker=6 assigned=issue #{numbers[-1]} "
            f"canonical=#{numbers[0]} influenced=yes"
        ) in stderr
    else:
        assert "Jev RESULT" not in stderr


def test_real_assign_falls_back_when_jev_file_is_invalid(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Malformed model output must not stop the normal factory claim."""
    monkeypatch.setattr(controller, "worker_sources", lambda: {"6": "nvidia"})
    # The simulated worker is credentialed; assign() checks this flag.
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    bad = tmp_path / "bad-jev.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("FACTORY_JEV_RANKING_FILE", str(bad))
    monkeypatch.setattr(controller, "quota_pause_for", lambda _w: None)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _w: False)
    monkeypatch.setattr(controller, "budget_block_reason", lambda _w: None)
    monkeypatch.setattr(
        controller, "omniroute_free_entry_capacity",
        lambda: {"remaining": 3},
    )
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "reconcile_contradictory_labels", lambda: [])
    monkeypatch.setattr(
        controller, "list_issues",
        lambda: [{
            "number": 33401, "title": "Product issue", "body": "",
            "state": "OPEN", "createdAt": "2026-10-01T00:00:00Z",
            "labels": [{"name": "factory:unowned"}],
        }],
    )
    monkeypatch.setattr(controller, "list_prs", lambda: [])
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda: [])
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda _i, _p: None)
    monkeypatch.setattr(
        controller, "resolve_review_head_contributors", lambda items, _prs: items
    )
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _c: True)
    monkeypatch.setattr(
        controller, "issue_excludes_worker_on_strike_retry", lambda _n, _w: False
    )
    monkeypatch.setattr(controller, "assign_candidate", lambda _c, _w: True)
    selected = controller.assign("6")
    assert selected is not None
    assert selected.number == 33401


def _completion_pr(number: int, stage: str, *, head: str, merge_state: str = "CLEAN") -> dict:
    return {
        "number": number,
        "title": "Completion work",
        "body": "",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": f"factory/completion-{number}",
        "headRefOid": head,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": merge_state,
        "createdAt": "2026-10-30T12:00:00Z",
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": stage},
        ],
    }


def _completion_assign_env(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    prs: list[dict],
    report: dict,
    tmp_path: Path,
) -> None:
    # These Jev unit fixtures use legacy worker 6, not a live roster worker.
    monkeypatch.setattr(controller, "worker_sources", lambda: {"6": "nvidia"})
    # The simulated worker is credentialed; assign() checks this flag.
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    report_path = tmp_path / "jev.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setenv("FACTORY_JEV_RANKING_FILE", str(report_path))
    monkeypatch.setattr(controller, "quota_pause_for", lambda _w: None)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _w: False)
    monkeypatch.setattr(controller, "budget_block_reason", lambda _w: None)
    monkeypatch.setattr(
        controller, "omniroute_free_entry_capacity", lambda: {"remaining": 3}
    )
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "reconcile_contradictory_labels", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: prs)
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda: [])
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda _i, _p: None)
    monkeypatch.setattr(
        controller, "resolve_review_head_contributors", lambda items, _prs: items
    )
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _c: True)


def test_real_assign_promotes_jev_completion_action_and_hands_it_off(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Jev's chosen completion action must become the real lease and be recorded."""
    head_a, head_b = "a" * 40, "b" * 40
    prs = [
        _completion_pr(34001, "factory:review", head=head_a),
        _completion_pr(34002, "factory:ci", head=head_b, merge_state="UNSTABLE"),
    ]
    report = {
        "surface": "pr-completion",
        "status": "chosen",
        "chosen": "pr-34002",
        "baseline": ["pr-34001", "pr-34002"],
        "action_map": {
            "pr-34002": {"pr": 34002, "action": "ci-repair", "head": head_b},
        },
    }
    _completion_assign_env(controller, monkeypatch, prs, report, tmp_path)
    claimed: list[tuple[int, str | None]] = []

    def fake_assign(
        candidate: object, _worker: str, action: str | None = None, expected_head: str | None = None
    ) -> bool:
        claimed.append((int(vars(candidate)["number"]), action))
        return True

    monkeypatch.setattr(controller, "assign_candidate", fake_assign)

    assert controller.assign("6") is not None
    assert claimed == [(34002, "ci-repair")]
    stderr = capsys.readouterr().err
    assert "Jev ACTIVE surface=pr-completion worker=6" in stderr
    assert "Jev RESULT worker=6 assigned=pr #34002 action=ci-repair" in stderr
    assert "influenced=yes" in stderr


def test_real_assign_rejects_a_stale_completion_decision(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    head_a, head_b = "a" * 40, "b" * 40
    prs = [
        _completion_pr(34101, "factory:review", head=head_a),
        _completion_pr(34102, "factory:ci", head=head_b),
    ]
    report = {
        "surface": "pr-completion",
        "status": "chosen",
        "chosen": "pr-34102",
        "baseline": ["pr-34101", "pr-34102"],
        "action_map": {
            "pr-34102": {"pr": 34102, "action": "ci-repair", "head": "c" * 40},
        },
    }
    _completion_assign_env(controller, monkeypatch, prs, report, tmp_path)
    claimed: list[int] = []

    def fake_assign(
        candidate: object, _worker: str, action: str | None = None, expected_head: str | None = None
    ) -> bool:
        claimed.append(int(vars(candidate)["number"]))
        return True

    monkeypatch.setattr(controller, "assign_candidate", fake_assign)

    assert controller.assign("6") is not None
    assert claimed == [34101]


def test_lease_marker_records_decision_provenance(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        controller, "run_gh", lambda args, **_kwargs: captured.append(args)
    )
    head = "a" * 40
    controller.record_controller_lease_activity(
        35001, "6", "pr", action="ci-repair", source="jev", head=head
    )
    body = captured[0][-1]
    assert body.startswith("<!-- comic-pile-factory-controller-claim-v1:pr-35001:6:")
    assert f":source=jev:action=ci-repair:head={head}" in body
    assert body.endswith(" -->")


def test_issue_lease_marker_records_jev_selection_provenance(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Jev-ranked issue must leave durable source=jev evidence on its lease."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        controller, "run_gh", lambda args, **_kwargs: captured.append(args)
    )
    controller.record_controller_lease_activity(35002, "6", "issue", source="jev")
    body = captured[0][-1]
    assert body.startswith("<!-- comic-pile-factory-controller-claim-v1:issue-35002:6:")
    assert ":source=jev" in body
    assert body.endswith(" -->")


def test_assign_candidate_forwards_jev_source_to_the_issue_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The controller must pass issue-selection provenance into the lease write."""
    monkeypatch.setattr(controller, "issue_has_open_blocker", lambda _n, *, live=False: False)
    monkeypatch.setattr(controller, "acquire_lease", lambda *_args: "synthetic-lease")
    candidate = controller.Candidate(
        "issue", 35301, 3, 2, "2026-10-30T12:00:00Z"
    )
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    monkeypatch.setattr(controller, "target_still_unowned", lambda _n: True)
    monkeypatch.setattr(controller, "target_owned_by", lambda _n, _o: True)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _w: False)
    monkeypatch.setattr(controller, "issue_has_open_blocker", lambda _n, *, live=False: False)
    monkeypatch.setattr(controller, "acquire_lease", lambda *_args: "test-lease")
    monkeypatch.setattr(
        controller, "replace_factory_labels", lambda *_a, **_k: None
    )
    recorded: list[tuple[int, str, dict[str, str]]] = []
    monkeypatch.setattr(
        controller,
        "record_controller_lease_activity",
        lambda number, _worker, kind, **kwargs: recorded.append(
            (number, kind, kwargs)
        ),
    )

    assert controller.assign_candidate(candidate, "6", source="jev") is True
    assert recorded == [(35301, "issue", {"source": "jev"})]


def test_assign_candidate_rejects_a_stale_head_before_writing_labels(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = controller.Candidate("pr", 35201, 3, 0, "2026-10-30T12:00:00Z", stage="factory:ci")
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    monkeypatch.setattr(controller, "target_still_unowned", lambda _n: True)
    monkeypatch.setattr(controller, "target_owned_by", lambda _n, _o: True)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _w: False)
    monkeypatch.setattr(
        controller,
        "live_pr_state",
        lambda _n: {
            "head": {"sha": "b" * 40},
            "mergeable": True,
            "mergeable_state": "CLEAN",
            "labels": [{"name": "factory"}, {"name": "factory:ci"}],
        },
    )
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _candidate: True)
    written: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: written.append((number, owner, stage)),
    )

    assert controller.assign_candidate(
        candidate, "6", action="ci-repair", expected_head="a" * 40
    ) is False
    assert written == []


def test_assign_falls_back_when_selected_head_changes_before_the_lease(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale decision must be discarded and canonical order reselected."""
    head_a, head_b = "a" * 40, "b" * 40
    prs = [
        _completion_pr(35301, "factory:review", head=head_a),
        _completion_pr(35302, "factory:ci", head=head_b),
    ]
    report = {
        "surface": "pr-completion",
        "status": "chosen",
        "chosen": "pr-35302",
        "baseline": ["pr-35301", "pr-35302"],
        "action_map": {
            "pr-35302": {"pr": 35302, "action": "ci-repair", "head": head_b},
        },
    }
    _completion_assign_env(controller, monkeypatch, prs, report, tmp_path)
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    monkeypatch.setattr(controller, "target_still_unowned", lambda _n: True)
    monkeypatch.setattr(controller, "target_owned_by", lambda _n, _o: True)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _w: False)
    monkeypatch.setattr(
        controller,
        "live_pr_state",
        lambda _n: {
            "head": {"sha": "c" * 40},
            "mergeable": True,
            "mergeable_state": "CLEAN",
            "labels": [{"name": "factory"}, {"name": "factory:ci"}],
        },
    )
    written: list[int] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, _owner, stage=None: written.append(number),
    )
    monkeypatch.setattr(controller, "record_controller_lease_activity", lambda *a, **k: None)

    selected = controller.assign("6")
    assert selected is not None
    assert selected.number == 35301
    assert written == [35301]
    assert "Jev FALLBACK worker=6" in capsys.readouterr().err


def test_replace_factory_labels_skips_standalone_manual_only_line(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label-write choke point must not re-apply factory* labels on a hold."""
    writes: list[list[str]] = []
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda _number: {
            "number": 4512,
            "body": "P0 auth outage rollback.\n\nmanual-only\n",
            "labels": [{"name": "bug"}],
        },
    )

    def fake_run_gh(
        args: list[str], *, input_json: object | None = None, check: bool = True
    ) -> str:
        writes.append(list(args))
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run_gh)

    controller.replace_factory_labels(4512, "factory:unowned", "factory:review")
    assert writes == []


def test_replace_factory_labels_writes_ordinary_pr(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary PRs still receive a single factory label-set replacement."""
    written_labels: list[str] = []
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda _number: {
            "number": 12,
            "body": "Ordinary factory work.",
            "labels": [{"name": "bug"}],
        },
    )

    def fake_run_gh(
        args: list[str], *, input_json: object | None = None, check: bool = True
    ) -> str:
        assert args[:2] == ["api", "--method"]
        assert isinstance(input_json, dict)
        labels = input_json["labels"]
        assert isinstance(labels, list)
        written_labels.extend(str(label) for label in labels)
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run_gh)

    controller.replace_factory_labels(12, "factory:unowned", "factory:review")
    assert written_labels == [
        "bug",
        "factory",
        "factory:review",
        "factory:unowned",
    ]


def _lease_timestamp(epoch: int) -> str:
    """Return a deterministic GitHub update timestamp."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _local_lease_item(number: int, updated: int, *extra: str) -> dict[str, Any]:
    """Model a locally owned GitHub issue or PR."""
    return {
        "number": number,
        "state": "OPEN",
        "updatedAt": _lease_timestamp(updated),
        "labels": [
            {"name": name}
            for name in ("factory", "factory:local", *extra)
        ],
    }


def _lease_reconcile_setup(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]] | None = None,
    *,
    markers: dict[int, int] | None = None,
    live: dict[int, dict[str, Any]] | None = None,
    workers: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Capture only actual label writes made during reconciliation."""
    targets = {
        int(row["number"]): row for row in [*issues, *(prs or [])]
    }
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(controller, "list_issues", lambda: issues)
    monkeypatch.setattr(controller, "list_prs", lambda: prs or [])
    monkeypatch.setattr(
        controller, "active_fixed_workers", lambda: (workers or set(), set())
    )
    monkeypatch.setattr(
        controller,
        "latest_lease_activity_epoch",
        lambda number: (markers or {}).get(number),
    )
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {
            **(live or {}).get(number, targets[number]),
            "updated_at": (live or {}).get(number, targets[number]).get(
                "updated_at",
                (live or {}).get(number, targets[number]).get("updatedAt"),
            ),
        },
    )

    def record_write(
        args: list[str], *, input_json: object | None = None, check: bool = True
    ) -> str:
        assert input_json is not None
        assert isinstance(input_json, dict)
        writes.append({"args": args, "labels": input_json["labels"]})
        return "{}"

    monkeypatch.setattr(controller, "run_gh", record_write)
    return writes


def test_reconcile_skips_manual_only_history_reads(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual-only holds never consume comment history calls or label writes."""
    now = 2_000_000_000
    held_issue = _local_lease_item(
        3129, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "factory:building"
    )
    held_issue["body"] = "<!-- factory-execution:manual-only -->\n"
    ordinary_issue = _local_lease_item(
        4684, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "factory:building"
    )
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [held_issue, ordinary_issue]
    )
    history_reads: list[int] = []
    monkeypatch.setattr(
        controller,
        "latest_lease_activity_epoch",
        lambda number: history_reads.append(number) or None,
    )

    assert controller.reconcile_stale_leases(now_epoch=now) == [4684]
    assert history_reads == [4684]
    assert len(writes) == 1
    assert "factory:unowned" in writes[0]["labels"]


def test_reconcile_expires_old_local_claim_without_trusted_marker(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No marker must not immortalize an abandoned implementation lease."""
    now = 2_000_000_000
    issue_row = _local_lease_item(
        4684,
        now - controller.LOCAL_LEASE_TTL_SECONDS - 1,
        "factory:building",
        "ralph-status:in-progress",
    )
    writes = _lease_reconcile_setup(controller, monkeypatch, [issue_row])
    assert controller.reconcile_stale_leases(now_epoch=now) == [4684]
    assert len(writes) == 1
    assert "factory:unowned" in writes[0]["labels"]
    assert "factory:local" not in writes[0]["labels"]
    assert "factory:building" not in writes[0]["labels"]
    assert "ralph-status:pending" in writes[0]["labels"]
    assert "ralph-status:in-progress" not in writes[0]["labels"]


def test_reconcile_preserves_current_local_implementation_and_pr_repair(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protect Copilot's #4625 and the #4615 / PR #4709 local repair."""
    now = 2_000_000_000
    issues = [
        _local_lease_item(4625, now - 120, "factory:building"),
        _local_lease_item(4615, now - 240, "factory:building"),
    ]
    prs = [_local_lease_item(4709, now - 60, "factory:building")]
    writes = _lease_reconcile_setup(controller, monkeypatch, issues, prs)
    assert controller.reconcile_stale_leases(now_epoch=now) == []
    assert writes == []


def test_reconcile_uses_latest_trusted_marker_when_more_recent_than_update(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh trusted progress record protects a legitimately active lease."""
    now = 2_000_000_000
    item = _local_lease_item(
        4625, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "factory:building"
    )
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [item], markers={4625: now - 120}
    )
    assert controller.reconcile_stale_leases(now_epoch=now) == []
    assert writes == []


def test_reconcile_keeps_blocked_issue_blocked_after_releasing_old_owner(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocked work loses ownership, not its product blocker."""
    now = 2_000_000_000
    item = _local_lease_item(
        3679, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "ralph-status:blocked"
    )
    writes = _lease_reconcile_setup(controller, monkeypatch, [item])
    assert controller.reconcile_stale_leases(now_epoch=now) == [3679]
    assert "factory:unowned" in writes[0]["labels"]
    assert "factory:blocked" in writes[0]["labels"]
    assert "ralph-status:blocked" in writes[0]["labels"]


def _local_lease_ttl_seconds() -> int:
    """Read the current local lease TTL from policy without test-global coupling."""
    scripts_dir = str(REPO_ROOT / ".github" / "scripts")
    added = scripts_dir not in sys.path
    if added:
        sys.path.insert(0, scripts_dir)
    try:
        import factory_work_policy as policy

        return int(policy.LOCAL_LEASE_TTL_SECONDS)
    finally:
        if added:
            sys.path.remove(scripts_dir)


@pytest.mark.parametrize(
    ("live_labels", "live_updated"),
    [
        (("factory", "factory:local", "factory:building"), -120),
        (
            ("factory", "factory:62", "factory:building"),
            -(_local_lease_ttl_seconds() + 1),
        ),
    ],
)
def test_reconcile_does_not_steal_a_refreshed_or_reassigned_lease(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    live_labels: tuple[str, ...],
    live_updated: int,
) -> None:
    """Recheck GitHub after snapshot selection, before the label write."""
    now = 2_000_000_000
    item = _local_lease_item(
        4684, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "factory:building"
    )
    live = {
        4684: {
            "number": 4684,
            "state": "open",
            "updated_at": _lease_timestamp(now + live_updated),
            "labels": [{"name": label} for label in live_labels],
        }
    }
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [item], live=live
    )
    assert controller.reconcile_stale_leases(now_epoch=now) == []
    assert writes == []


def test_reconcile_expires_inactive_fixed_lease_without_marker_but_not_active_run(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inactive fixed owner is bounded by last update; live runs still win."""
    now = 2_000_000_000
    item = {
        **_local_lease_item(
            4450, now - controller.FIXED_LEASE_TTL_SECONDS - 1, "factory:building"
        ),
        "labels": [
            {"name": "factory"},
            {"name": "factory:62"},
            {"name": "factory:building"},
        ],
    }
    writes = _lease_reconcile_setup(controller, monkeypatch, [item])
    assert controller.reconcile_stale_leases(now_epoch=now) == [4450]
    assert len(writes) == 1
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [item], workers={62}
    )
    assert controller.reconcile_stale_leases(now_epoch=now) == []
    assert writes == []


def test_future_marker_does_not_immortalize_old_local_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed future markers must not prevent inactivity expiration."""
    now = 2_000_000_000
    item = _local_lease_item(
        4344, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "factory:building"
    )
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [item], markers={4344: now + 86400}
    )
    assert controller.reconcile_stale_leases(now_epoch=now) == [4344]
    assert len(writes) == 1


def test_reconcile_does_not_guess_when_github_update_time_is_missing(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent updatedAt must not be replaced with ancient issue creation."""
    now = 2_000_000_000
    item = _local_lease_item(
        4549, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "factory:building"
    )
    item.pop("updatedAt")
    item["createdAt"] = _lease_timestamp(now - 365 * 86400)
    writes = _lease_reconcile_setup(controller, monkeypatch, [item])
    assert controller.reconcile_stale_leases(now_epoch=now) == []
    assert writes == []


@pytest.mark.parametrize("fresh_side", ["issue", "pr"])
def test_canonical_pr_and_issue_share_local_lease_activity(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    fresh_side: str,
) -> None:
    """A local PR repair stays owned when progress appears on either side."""
    now = 2_000_000_000
    issue_updated = (
        now - 90
        if fresh_side == "issue"
        else now - controller.LOCAL_LEASE_TTL_SECONDS - 1
    )
    pr_updated = (
        now - 90
        if fresh_side == "pr"
        else now - controller.LOCAL_LEASE_TTL_SECONDS - 1
    )
    issue_row = _local_lease_item(4615, issue_updated, "factory:building")
    pr_row = _local_lease_item(4709, pr_updated, "factory:building")
    pr_row["body"] = "Closes #4615"
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [issue_row], [pr_row]
    )
    assert controller.paired_owned_targets([issue_row], [pr_row]) == {
        4615: 4709,
        4709: 4615,
    }
    assert controller.reconcile_stale_leases(now_epoch=now) == []
    assert writes == []


def test_expired_canonical_local_pair_releases_both_leases(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once both issue and PR are inactive they must not hold each other forever."""
    now = 2_000_000_000
    stale = now - controller.LOCAL_LEASE_TTL_SECONDS - 1
    issue_row = _local_lease_item(4615, stale, "factory:building")
    pr_row = _local_lease_item(4709, stale, "factory:review")
    pr_row["body"] = "Closes #4615"
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [issue_row], [pr_row]
    )
    assert controller.reconcile_stale_leases(now_epoch=now) == [4615, 4709]
    assert len(writes) == 2
    assert "factory:unowned" in writes[1]["labels"]
    assert "factory:review" in writes[1]["labels"]


def test_unrelated_pr_activity_cannot_extend_an_inactive_issue_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a canonical PR with the same owner may extend the issue lease."""
    now = 2_000_000_000
    issue_row = _local_lease_item(
        4549, now - controller.LOCAL_LEASE_TTL_SECONDS - 1, "factory:building"
    )
    unrelated = _local_lease_item(4709, now - 10, "factory:review")
    unrelated["body"] = "Closes #4615"
    writes = _lease_reconcile_setup(
        controller, monkeypatch, [issue_row], [unrelated]
    )
    assert controller.paired_owned_targets([issue_row], [unrelated]) == {}
    assert controller.reconcile_stale_leases(now_epoch=now) == [4549]
    assert len(writes) == 1


@pytest.mark.parametrize('worker', ['54', '46'])
def test_worker_ordering_is_role_neutral_with_protected_lanes(
    controller: types.ModuleType, worker: str,
) -> None:
    """No provider/model pin dictates intake preferences (#5213).

    Former starter/finisher specialists order work exactly like any worker,
    while urgent and protected-review lanes still lead.
    """
    import factory_work_policy

    assert factory_work_policy.worker_delivery_role(worker) == ''
    candidates = [
        controller.Candidate('issue', 1001, 3, 4, '2026-11-01T00:00:00Z'),
        controller.Candidate('pr', 1002, 3, 0, '2026-11-01T00:00:00Z',
                             stage='factory:changes-requested'),
        controller.Candidate('pr', 1004, 3, 0, '2026-10-01T00:00:00Z',
                             stage='factory:review'),
    ]
    ordered = controller.order_candidates_for_worker(candidates, worker)
    assert sorted(c.number for c in ordered) == [1001, 1002, 1004]
    urgent = controller.Candidate('issue', 1003, 0, 4, '2026-11-07T00:00:00Z')
    assert controller.order_candidates_for_worker([*candidates, urgent], worker)[0] == urgent
    # A protected review must not be demoted by review-capacity or role preferences.
    protected_review = controller.Candidate(
        'pr', 1005, 0, 0, '2026-11-07T00:00:00Z', stage='factory:review',
    )
    assert controller.order_candidates_for_worker(
        [*candidates, protected_review], worker,
    )[0] == protected_review
