"""Runtime regression coverage for factory assignment and lease reconciliation."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = REPO_ROOT / ".github/scripts/factory-work-controller.py"


@pytest.fixture(scope="module")
def controller() -> types.ModuleType:
    """Load the factory work controller once for runtime regression tests."""
    spec = importlib.util.spec_from_file_location("factory_work_controller_runtime", CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stale_fixed_lease_releases_but_live_worker_is_not_stolen(
    controller: types.ModuleType,
) -> None:
    """Verify stale fixed lease releases but live worker is not stolen."""
    assert controller.lease_is_stale(
        "factory:13",
        active_fixed_workers=set(),
        latest_activity_epoch=None,
        now_epoch=10_000,
    )
    assert not controller.lease_is_stale(
        "factory:13",
        active_fixed_workers={13},
        latest_activity_epoch=None,
        now_epoch=10_000,
    )


def test_chatgpt_factory_lease_is_not_reaped_using_fixed_model_run_state(
    controller: types.ModuleType,
) -> None:
    """Verify chatgpt factory lease is not reaped using fixed model run state."""
    assert not controller.lease_is_stale(
        "factory:3",
        active_fixed_workers=set(),
        latest_activity_epoch=None,
        now_epoch=10_000,
    )


def test_local_lease_requires_explicit_stale_activity_evidence(
    controller: types.ModuleType,
) -> None:
    """Verify local lease requires explicit stale activity evidence."""
    assert controller.lease_is_stale(
        "factory:local",
        active_fixed_workers=set(),
        latest_activity_epoch=1_000,
        now_epoch=10_000,
        local_ttl_seconds=8_100,
    )
    assert not controller.lease_is_stale(
        "factory:local",
        active_fixed_workers=set(),
        latest_activity_epoch=None,
        now_epoch=10_000,
        local_ttl_seconds=8_100,
    )


def test_invalid_timeout_environment_falls_back_safely(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify invalid timeout environment falls back safely."""
    monkeypatch.setenv("FACTORY_TEST_TIMEOUT", "garbage")
    assert controller.env_positive_int("FACTORY_TEST_TIMEOUT", 120) == 120
    monkeypatch.setenv("FACTORY_TEST_TIMEOUT", "0")
    assert controller.env_positive_int("FACTORY_TEST_TIMEOUT", 120) == 120


def test_run_gh_converts_subprocess_timeout_to_controlled_error(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify run gh converts subprocess timeout to controlled error."""
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["gh", "api"], timeout=120)

    monkeypatch.setattr(controller.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        controller.run_gh(["api", "rate_limit"])


def test_assign_candidate_writes_nothing_when_linked_issue_is_owned(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify assign candidate writes nothing when linked issue is owned."""
    candidate = controller.Candidate(
        "pr",
        1601,
        2,
        3,
        "2026-08-16T12:00:00Z",
        linked_issue=601,
    )
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {"state": "open"} if number == 601 else {"labels": []},
    )
    monkeypatch.setattr(controller, "target_still_unowned", lambda number: number != 601)
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda *args: writes.append(args),
    )

    assert controller.assign_candidate(candidate, "13") is False
    assert writes == []


def test_assign_candidate_rolls_first_target_back_when_second_write_fails(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify assign candidate rolls first target back when second write fails."""
    candidate = controller.Candidate(
        "pr",
        1602,
        2,
        3,
        "2026-08-16T12:00:00Z",
        linked_issue=602,
    )
    monkeypatch.setattr(controller, "target_json", lambda number: {"state": "open"})
    monkeypatch.setattr(controller, "target_still_unowned", lambda number: True)
    monkeypatch.setattr(controller, "target_owned_by", lambda number, owner: True)
    monkeypatch.setattr(controller, "record_controller_lease_activity", lambda *args: None)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _worker: False)
    # Mock dispatcher identity verification for testing
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    calls: list[tuple[int, str, str | None]] = []

    def replace(number: int, owner: str, stage: str | None = None) -> None:
        calls.append((number, owner, stage))
        if number == 1602 and owner == "factory:13":
            raise RuntimeError("write failed")

    monkeypatch.setattr(controller, "replace_factory_labels", replace)

    with pytest.raises(RuntimeError, match="write failed"):
        controller.assign_candidate(candidate, "13")

    assert (602, "factory:unowned", None) in calls


def test_pr_assignment_preserves_existing_workflow_stage(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pr assignment preserves existing workflow stage."""
    candidate = controller.Candidate("pr", 1603, 2, 3, "2026-08-16T12:00:00Z")
    monkeypatch.setattr(controller, "target_still_unowned", lambda number: True)
    monkeypatch.setattr(controller, "target_owned_by", lambda number, owner: True)
    monkeypatch.setattr(controller, "record_controller_lease_activity", lambda *args: None)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _worker: False)
    # Mock dispatcher identity verification for testing
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    calls: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: calls.append((number, owner, stage)),
    )

    assert controller.assign_candidate(candidate, "13") is True
    assert calls == [(1603, "factory:13", None)]


def test_assign_candidate_verifies_post_write_owner(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify assign candidate verifies post write owner."""
    candidate = controller.Candidate("issue", 603, 1, 3, "2026-08-16T12:00:00Z")
    monkeypatch.setattr(controller, "target_still_unowned", lambda number: True)
    monkeypatch.setattr(controller, "target_owned_by", lambda number, owner: False)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _worker: False)
    monkeypatch.setattr(controller, "record_controller_lease_activity", lambda *args: None)
    # Mock dispatcher identity verification for testing
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda *args: writes.append(args),
    )

    assert controller.assign_candidate(candidate, "13") is False
    assert writes == [(603, "factory:13", "factory:building")]


def test_reconcile_does_not_release_active_fixed_worker(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reconcile does not release active fixed worker."""
    monkeypatch.setattr(controller, "active_fixed_workers", lambda: {13})
    monkeypatch.setattr(controller, "owned_targets", lambda: [(701, "factory:13")])
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda *args: writes.append(args),
    )

    assert controller.reconcile_stale_leases(now_epoch=10_000) == []
    assert writes == []


def test_replace_factory_labels_preserves_existing_stage(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify replace factory labels preserves existing stage."""
    # Mock dispatcher identity verification for testing
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {
            "labels": [
                {"name": "bug"},
                {"name": "factory"},
                {"name": "factory:unowned"},
                {"name": "factory:changes-requested"},
            ]
        },
    )
    payloads: list[dict[str, Any]] = []

    def fake_run(
        args: list[str], *, input_json: object | None = None, check: bool = True
    ) -> str:
        assert input_json is not None
        assert isinstance(input_json, dict)
        payloads.append(input_json)
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run)

    controller.replace_factory_labels(801, "factory:13")

    assert set(payloads[0]["labels"]) == {
        "bug",
        "factory",
        "factory:13",
        "factory:changes-requested",
    }


def test_ci_pr_with_passing_required_checks_is_not_executable(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ci pr with passing required checks is not executable."""
    candidate = controller.Candidate("pr", 901, 3, 2, "2026-08-16T12:00:00Z")
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {"labels": [{"name": "factory:ci"}]},
    )
    monkeypatch.setattr(controller, "required_checks_failed", lambda number: False)

    assert controller.candidate_is_live_executable(candidate) is False


@pytest.mark.parametrize("stage", ["factory:review", "factory:changes-requested"])
def test_actionable_pr_stage_is_live_executable(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """Review and repair stages have an executable next action."""
    candidate = controller.Candidate("pr", 902, 3, 2, "2026-08-16T12:00:00Z")
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {"labels": [{"name": stage}]},
    )

    assert controller.candidate_is_live_executable(candidate) is True


@pytest.mark.parametrize("stage", ["factory:review", "factory:changes-requested"])
@pytest.mark.parametrize(
    ("attempts", "expect_assigned"),
    [
        (2, True),
        (3, False),
    ],
)
def test_assign_respects_pr_no_diff_retry_budget(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    attempts: int,
    expect_assigned: bool,
) -> None:
    """PR retry exhaustion suppresses assignment without rewriting truthful stages."""
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    pr = {
        "number": 2122,
        "state": "OPEN",
        "isDraft": False,
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": stage},
        ],
        "headRefName": "factory/50-1767-opencode-free",
        "headRefOid": head,
        "body": "Worker: opencode-free-model-factory-50",
        "createdAt": "2026-09-03T00:25:29Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setattr(
        controller,
        "omniroute_free_entry_capacity",
        lambda: {"in_flight": 0, "cap": 3, "remaining": 3},
    )
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [pr])
    monkeypatch.setattr(
        controller,
        "load_no_diff_attempt_records",
        lambda: [
            controller.NoDiffAttempt(
                kind="pr",
                number=2122,
                epoch=2_000_000_000 - index,
                sha=head,
                stage=stage,
                conflicted=False,
            )
            for index in range(attempts)
        ],
    )
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {"labels": [{"name": stage}]},
    )
    monkeypatch.setattr(controller, "assign_candidate", lambda candidate, worker: True)

    assignment = controller.assign("13")

    if expect_assigned:
        assert assignment is not None
        assert (assignment.kind, assignment.number, assignment.stage) == ("pr", 2122, stage)
    else:
        assert assignment is None
    assert {str(label["name"]) for label in pr["labels"]} == {
        "factory",
        "factory:unowned",
        stage,
    }


def test_assign_wakes_exhausted_pr_after_new_head(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact-head retry accounting must not suppress a later push."""
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    old_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    new_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    pr = {
        "number": 2122,
        "state": "OPEN",
        "isDraft": False,
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": "factory:changes-requested"},
        ],
        "headRefName": "factory/50-1767-opencode-free",
        "headRefOid": new_head,
        "body": "Worker: opencode-free-model-factory-50",
        "createdAt": "2026-09-03T00:25:29Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setattr(
        controller,
        "omniroute_free_entry_capacity",
        lambda: {"in_flight": 0, "cap": 3, "remaining": 3},
    )
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [pr])
    monkeypatch.setattr(
        controller,
        "load_no_diff_attempt_records",
        lambda: [
            controller.NoDiffAttempt(
                kind="pr",
                number=2122,
                epoch=2_000_000_000 - index,
                sha=old_head,
                stage="factory:changes-requested",
                conflicted=False,
            )
            for index in range(3)
        ],
    )
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {"labels": [{"name": "factory:changes-requested"}]},
    )
    monkeypatch.setattr(controller, "assign_candidate", lambda candidate, worker: True)

    assignment = controller.assign("13")

    assert assignment is not None
    assert (assignment.kind, assignment.number) == ("pr", 2122)


def test_ci_pr_with_failed_required_checks_is_live_executable(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A required-check failure makes CI-stage work actionable for repair."""
    candidate = controller.Candidate("pr", 903, 3, 2, "2026-08-16T12:00:00Z")
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {"labels": [{"name": "factory:ci"}]},
    )
    monkeypatch.setattr(controller, "required_checks_failed", lambda number: True)

    assert controller.candidate_is_live_executable(candidate) is True


def test_active_fixed_workers_paginates_all_active_run_states(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify active fixed workers paginates all active run states."""
    calls: list[list[str]] = []
    responses = iter(
        [
            [
                {
                    "workflow_runs": [
                        {"id": 1, "display_title": "Factory 13 · fixed-model entry"}
                    ]
                }
            ],
            [
                {
                    "workflow_runs": [
                        {"id": 2, "display_title": "Factory 29 · fixed-model entry"}
                    ]
                }
            ],
        ]
    )

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    assert controller.active_fixed_workers() == {13, 29}
    assert all("--paginate" in args and "--slurp" in args for args in calls)


def test_active_fixed_workers_fails_closed_when_live_run_identity_is_unknown(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify active fixed workers fails closed when live run identity is unknown."""
    responses = iter(
        [
            [{"workflow_runs": [{"id": 99, "display_title": "Fixed Model Factory Entry"}]}],
            [{"workflow_runs": []}],
            [[]],
        ]
    )
    monkeypatch.setattr(
        controller,
        "gh_json",
        lambda args, **kwargs: next(responses),
    )

    workers, unresolved = controller.active_fixed_workers()
    assert workers == set()
    assert unresolved == {"99"}


def test_busy_fixed_worker_is_not_given_a_second_assignment(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify busy fixed worker is not given a second assignment."""
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: True)
    monkeypatch.setattr(
        controller,
        "list_issues",
        lambda: (_ for _ in ()).throw(AssertionError("must not rank new work")),
    )

    assert controller.assign("13") is None


def test_assign_skips_when_omniroute_free_entry_cap_is_exhausted(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full OmniRoute free pool must not lease more Entry sessions."""
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setattr(
        controller,
        "omniroute_free_entry_capacity",
        lambda: {"in_flight": 3, "cap": 3, "remaining": 0},
    )
    monkeypatch.setattr(
        controller,
        "list_issues",
        lambda: (_ for _ in ()).throw(AssertionError("must not rank new work")),
    )

    assert controller.assign("13") is None


def test_in_flight_units_union_active_runs_and_leases(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leases and Entry runs occupy the same OmniRoute free-entry budget."""
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    monkeypatch.setattr(
        controller,
        "active_fixed_workers",
        lambda: controller.ActiveWorkerResult({41, 58}, set()),
    )
    monkeypatch.setattr(
        controller,
        "owned_targets",
        lambda: [(2201, "factory:58"), (2202, "factory:60")],
    )

    assert controller.in_flight_omniroute_free_entries() == 3
    assert controller.omniroute_free_entry_capacity() == {
        "enabled": 1,
        "in_flight": 3,
        "cap": 3,
        "remaining": 0,
    }


def test_unresolved_run_listing_fails_closed_at_the_free_entry_cap(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown Entry occupancy must not start additional OmniRoute smokes."""
    monkeypatch.setattr(
        controller,
        "active_fixed_workers",
        lambda: controller.ActiveWorkerResult(
            set(),
            {"queued-run-query-unavailable"},
        ),
    )
    monkeypatch.setattr(controller, "owned_targets", lambda: [])

    assert controller.in_flight_omniroute_free_entries() == (
        controller.DEFAULT_MULTI_PROVIDER_ENTRY_CAP
    )
    assert controller.omniroute_free_entry_has_capacity() is False


def test_one_dispatch_batch_plans_distinct_targets(controller: types.ModuleType) -> None:
    """Verify one dispatch batch plans distinct targets."""
    candidates = [
        controller.Candidate("issue", 601, 1, 3, "2026-08-16T12:00:00Z"),
        controller.Candidate("issue", 602, 3, 2, "2026-08-16T11:00:00Z"),
    ]

    plan = controller.plan_distinct_assignments(candidates, ["13", "29"])

    assert plan["13"].number == 601
    assert plan["29"].number == 602


def test_one_dispatch_batch_plans_distinct_actionable_prs(
    controller: types.ModuleType,
) -> None:
    """Several offered workers can drain separate review and repair PRs."""
    candidates = [
        controller.Candidate(
            "pr",
            2122,
            3,
            0,
            "2026-09-03T00:25:29Z",
            stage="factory:changes-requested",
            producer_worker="50",
        ),
        controller.Candidate(
            "pr",
            2132,
            3,
            0,
            "2026-09-03T01:59:57Z",
            stage="factory:review",
            producer_worker="42",
        ),
        controller.Candidate(
            "pr",
            2133,
            3,
            0,
            "2026-09-03T02:15:07Z",
            stage="factory:review",
            producer_worker="66",
        ),
    ]

    plan = controller.plan_distinct_assignments(candidates, ["42", "43", "44"])

    assert len(plan) == 3
    assert len({candidate.number for candidate in plan.values()}) == 3
    assert plan["42"].number != 2132


def test_active_worker_no_longer_performs_repo_wide_selection_or_merging() -> None:
    """Verify active worker no longer performs repo wide selection or merging."""
    worker = (
        REPO_ROOT / ".github/scripts/free-model-factory-worker.sh"
    ).read_text(encoding="utf-8")

    assert "choose_existing_pr" not in worker
    assert "choose_ranked_issues" not in worker
    assert "claim_from_pool" not in worker
    assert "select_controller_assignment" in worker
    assert "controller-assignment-read-failed" in worker
    assert "exiting without repo-wide selection" in worker
    assert "handing it to the merge controller" in worker
    assert 'gh pr merge "$NUMBER"' not in worker


def test_dispatcher_validates_controller_assignment_and_survives_dispatch_failure() -> None:
    """Verify dispatcher validates controller assignment and survives dispatch failure."""
    dispatcher = (
        REPO_ROOT / ".github/workflows/free-model-factory-dispatch.yml"
    ).read_text(encoding="utf-8")

    assert "group: fixed-model-factory-dispatch" in dispatcher
    assert 'python3 "$controller" reconcile' in dispatcher
    assert 'if ! assignment="$(python3 "$controller" assign --worker "$worker")"' in dispatcher
    assert "unusable controller response" in dispatcher
    assert "while (( attempt <= 3 ))" in dispatcher
    assert 'python3 "$controller" release --worker "$worker"' in dispatcher
    assert 'dispatch_assigned_worker "$worker" || dispatch_failures=' in dispatcher
    assert "later workers were still attempted" in dispatcher


def test_entry_run_name_exposes_queued_worker_identity() -> None:
    """Verify entry run name exposes queued worker identity."""
    entry = (
        REPO_ROOT / ".github/workflows/free-model-factory-entry.yml"
    ).read_text(encoding="utf-8")

    assert "run-name: Factory ${{ inputs.worker }} · fixed-model entry" in entry


def test_inspect_assignment_returns_review_pr_for_smoke_routing(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entry can see a leased review PR before OpenCode smoke."""
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(
        controller,
        "list_prs",
        lambda: [
            {
                "number": 2235,
                "labels": [
                    {"name": "factory"},
                    {"name": "factory:42"},
                    {"name": "factory:review"},
                ],
            }
        ],
    )

    assert controller.inspect_assignment("42") == {
        "kind": "pr",
        "number": 2235,
        "stage": "factory:review",
        "owner": "factory:42",
    }


def test_inspect_assignment_returns_none_when_worker_has_no_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker without a controller claim smokes the lane default."""
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [])

    assert controller.inspect_assignment("42") == {"kind": "none"}


def test_smoke_failure_releases_review_lease_immediately(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factory 10 / 42 fixture: assign then smoke-fail does not wait 900s.

    Factory 10 run 33990134163 and Factory 42 run 33991283563 claimed PR
    #2235, smoked auto/coding:free, died before free-model-factory-worker.sh,
    and left factory:<worker> + factory:review until the stale-lease TTL.
    """
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(
        controller,
        "list_prs",
        lambda: [
            {
                "number": 2235,
                "labels": [
                    {"name": "bug"},
                    {"name": "factory"},
                    {"name": "factory:42"},
                    {"name": "factory:review"},
                ],
            }
        ],
    )
    writes: list[tuple[Any, ...]] = []
    markers: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        controller,
        "replace_factory_labels",
        lambda number, owner, stage=None: writes.append((number, owner, stage)),
    )
    monkeypatch.setattr(
        controller,
        "record_claim_released",
        lambda number, worker, kind, reason: markers.append(
            (number, worker, kind, reason)
        ),
    )

    assert controller.release_worker("42", reason="smoke-failure") == [2235]
    assert writes == [(2235, "factory:unowned", None)]
    assert markers == [(2235, "42", "pr", "smoke-failure")]


def test_record_claim_released_uses_v3_marker(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-session abort posts the same claim-released marker the worker uses."""
    monkeypatch.setattr(controller.time, "time", lambda: 1_788_641_416)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(controller, "run_gh", fake_run)
    controller.record_claim_released(2235, "42", "pr", "smoke-failure")

    assert calls == [
        [
            "issue",
            "comment",
            "2235",
            "--repo",
            controller.REPO,
            "--body",
            "<!-- comic-pile-factory-claim-released-v3:"
            "pr-2235:opencode-free-model-factory-42:1788641416:smoke-failure -->",
        ]
    ]


def test_worker_accepts_in_progress_pr_handoff_from_controller() -> None:
    """A leased building PR runs as PR work instead of a control-plane failure."""
    worker = (
        REPO_ROOT / ".github/scripts/free-model-factory-worker.sh"
    ).read_text(encoding="utf-8")

    assert (
        '. == "factory:building" or . == "factory:review" '
        'or . == "factory:changes-requested"'
    ) in worker
    assert "ASSIGNED_PR_STAGE" in worker
    assert "repair-no-change-ready-handoff" in worker
