"""Regression coverage for centralized factory assignment and leases."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = REPO_ROOT / ".github/scripts/factory-work-controller.py"


@pytest.fixture(scope="module")
def controller() -> types.ModuleType:
    """Load the factory work controller once for this regression module."""
    spec = importlib.util.spec_from_file_location("factory_work_controller", CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
            "factory:review",
            created=f"2026-08-20T12:00:{index:02d}Z",
        )
        for index in range(5)
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


def test_assign_candidate_refuses_second_active_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """assign_candidate must refuse when the worker already holds an active lease."""
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda w: w == "99")
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
    assert replaced == [(3001, "factory:13", None)]


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
