"""Runtime regression coverage for factory assignment and lease reconciliation."""

from __future__ import annotations

import importlib.util
import json
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
    # Existing runtime fixtures exercise historical worker IDs that are no
    # longer on the live roster. Keep that test data explicit, never relax
    # the production assignment check.
    real_sources = module.worker_sources
    module.__dict__["worker_sources"] = lambda: {
        **real_sources(),
        "13": "nvidia",
        "17": "nvidia",
        "59": "nvidia",
    }
    return module



def test_three_digit_assignment_checks_canonical_id_and_live_roster(
    controller: types.ModuleType,
) -> None:
    """Every live three-digit roster member can dispatch; unknown IDs cannot."""
    authorized = controller.is_authorized_fixed_worker
    roster = controller.worker_sources()
    three_digit_workers = sorted(int(worker) for worker in roster if worker.isdigit() and int(worker) >= 100)
    assert three_digit_workers
    for worker in three_digit_workers:
        assert authorized(str(worker))
    unknown = str(max(map(int, roster)) + 1)
    for invalid in (unknown, "999", "0", "00", "0104", "+104", "104 ", "abc", ""):
        assert not authorized(invalid)



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


@pytest.mark.parametrize("worker", ["13", "104", "105", "106", "107"])
def test_pr_assignment_preserves_existing_workflow_stage(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, worker: str
) -> None:
    """A real three-digit worker can claim a PR without losing its stage."""
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

    assert controller.assign_candidate(candidate, worker) is True
    assert calls == [(1603, f"factory:{worker}", None)]


def test_assign_candidate_verifies_post_write_owner(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify assign candidate verifies post write owner."""
    candidate = controller.Candidate("issue", 603, 1, 3, "2026-08-16T12:00:00Z")
    monkeypatch.setattr(controller, "issue_has_open_blocker", lambda number, *, live=False: False)
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
    monkeypatch.setattr(
        controller,
        "list_issues",
        lambda: [{"number": 701, "labels": [{"name": "factory:13"}]}],
    )
    monkeypatch.setattr(controller, "list_prs", lambda: [])
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
    monkeypatch.setattr(controller, "required_checks_outcome", lambda number: 'passed')

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
    monkeypatch.setattr(controller, "required_checks_outcome", lambda number: 'passed')

    assert controller.candidate_is_live_executable(candidate) is True


@pytest.mark.parametrize("stage", ["factory:review", "factory:changes-requested"])
@pytest.mark.parametrize(
    ("attempts", "expect_assigned"),
    [
        (-1, True),
        (0, False),
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
    # Exercise the selected PR/lease branch, not the Actions identity gate.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    attempts += controller.FACTORY_NO_DIFF_RETRY_LIMIT
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    # Live CI is covered separately; these are deterministic retry/selection tests.
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _candidate: True)
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


def test_assign_skips_a_head_the_requesting_worker_contributed_to(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3059: a repairer is never dispatched to review its own repair.

    The producer opened the pull request and factory 59 pushed the head that is
    now under review. The review controller would refuse factory 59's verdict,
    so selection itself has to route that head to another eligible worker
    instead of burning factory 59's lease on a guaranteed refusal.
    """
    # Exercise the selected PR/lease branch, not the Actions identity gate.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    # Live CI is covered separately; these are deterministic retry/selection tests.
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _candidate: True)
    head = "cccccccccccccccccccccccccccccccccccccccc"
    pr = {
        "number": 3073,
        "state": "OPEN",
        "isDraft": False,
        "labels": [
            {"name": "factory"},
            {"name": "factory:unowned"},
            {"name": "factory:review"},
        ],
        "headRefName": "factory/29-3059-catalog-free",
        "headRefOid": head,
        "body": "Closes #3059.\n\nWorker: opencode-free-model-factory-29\n",
        "createdAt": "2026-09-03T00:25:29Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
    }

    def comment(body: str) -> dict[str, Any]:
        return {"user": {"login": "github-actions[bot]"}, "author_association": "NONE", "body": body}

    marker = (
        "<!-- comic-pile-factory-head-contributor-v1:"
        f"pr-3073:head-{head}:worker-59:epoch-1234567890 -->"
    )
    monkeypatch.setattr(controller, "gh_json", lambda *args, **kwargs: [[comment(marker)]])
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setattr(
        controller,
        "omniroute_free_entry_capacity",
        lambda: {"in_flight": 0, "cap": 3, "remaining": 3},
    )
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: [])
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [pr])
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda: [])
    monkeypatch.setattr(
        controller,
        "target_json",
        lambda number: {"labels": [{"name": "factory:review"}]},
    )
    monkeypatch.setattr(controller, "assign_candidate", lambda candidate, worker: True)

    assert controller.assign("59") is None
    assignment = controller.assign("17")
    assert assignment is not None
    assert (assignment.kind, assignment.number) == ("pr", 3073)


def test_assign_wakes_exhausted_pr_after_new_head(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact-head retry accounting must not suppress a later push."""
    # Exercise the selected PR/lease branch, not the Actions identity gate.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    # Live CI is covered separately; these are deterministic retry/selection tests.
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda _candidate: True)
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
    monkeypatch.setattr(controller, "required_checks_outcome", lambda number: 'failed')

    assert controller.candidate_is_live_executable(candidate) is True


@pytest.mark.parametrize(
    ("states", "outcome"),
    [
        (["PENDING"], "waiting"),
        (["IN_PROGRESS"], "waiting"),
        (["QUEUED"], "waiting"),
        (["ACTION_REQUIRED"], "waiting"),
        (["FAILURE", "PENDING"], "waiting"),
        (["FAILURE", "SUCCESS"], "failed"),
        (["STALE"], "failed"),
        (["SUCCESS"], "passed"),
        (["SUCCESS", "SKIPPED"], "passed"),
        ([], "waiting"),
        (["SOME_NEW_CHECK_STATE"], "waiting"),
    ],
)
def test_required_ci_outcome_waits_for_all_terminal_checks(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    states: list[str],
    outcome: str,
) -> None:
    """Only terminal exact-head CI results can unlock a model assignment."""
    monkeypatch.setattr(
        controller, "run_gh",
        lambda *args, **kwargs: json.dumps(
            [{"state": state} for state in states]
        ),
    )
    assert controller.required_checks_outcome(1401) == outcome


@pytest.mark.parametrize(
    ("stage", "conflicted"),
    [
        ("factory:review", False),
        ("factory:changes-requested", False),
        ("factory:ci", False),
        ("factory:changes-requested", True),
    ],
)
def test_running_ci_prevents_pr_assignment_in_any_stage(
    controller: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    conflicted: bool,
) -> None:
    candidate = controller.Candidate(
        "pr", 1402, 3, 2, "2026-11-15T12:00:00Z",
        stage=stage, conflicted=conflicted,
    )
    monkeypatch.setattr(
        controller, "target_json",
        lambda number: {"labels": [{"name": stage}]},
    )
    monkeypatch.setattr(controller, "required_checks_outcome", lambda number: "waiting")
    assert not controller.candidate_is_live_executable(candidate)


def test_ci_pending_never_acquires_worker_lease(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = controller.Candidate(
        "pr", 1403, 3, 2, "2026-11-15T12:00:00Z", stage="factory:review",
    )
    monkeypatch.setattr(controller, "target_still_unowned", lambda number: True)
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda item: False)
    monkeypatch.setattr(
        controller, "acquire_lease",
        lambda *args, **kwargs: pytest.fail("waiting CI cannot reserve a worker"),
    )
    assert controller.assign_candidate(candidate, "13") is False


def test_ci_starting_during_lease_releases_reservation_before_mutations(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    candidate = controller.Candidate(
        "pr", 1404, 3, 2, "2026-11-15T12:00:00Z", stage="factory:review",
    )
    statuses = iter([True, False])
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda item: next(statuses))
    monkeypatch.setattr(controller, "target_still_unowned", lambda number: True)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setattr(controller, "acquire_lease", lambda *args: "ci-race-lease")
    released: list[tuple[object, ...]] = []
    monkeypatch.setattr(controller, "release_lease", lambda *args: released.append(args))
    monkeypatch.setattr(
        controller, "replace_factory_labels",
        lambda *args, **kwargs: pytest.fail("CI started: labels must not be claimed"),
    )
    monkeypatch.setattr(
        controller, "record_controller_lease_activity",
        lambda *args, **kwargs: pytest.fail("CI started: no model claim marker"),
    )

    assert controller.assign_candidate(candidate, "13") is False
    assert released == [(controller.REPO, "13", "ci-race-lease", "claim-rollback")]


def test_active_fixed_workers_paginates_all_active_run_states(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify active fixed workers paginates all active run states."""
    # Exercise the selected PR/lease branch, not the Actions identity gate.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    calls: list[list[str]] = []
    workflows = ("free-model-factory-entry.yml", "factory-fork-execution.yml")
    responses = {
        ("free-model-factory-entry.yml", "queued"): [
            {
                "workflow_runs": [
                    {"id": 1, "display_title": "Factory 13 · fixed-model entry"}
                ]
            }
        ],
        ("free-model-factory-entry.yml", "in_progress"): [{"workflow_runs": []}],
        ("factory-fork-execution.yml", "queued"): [{"workflow_runs": []}],
        ("factory-fork-execution.yml", "in_progress"): [
            {
                "workflow_runs": [
                    {
                        "id": 2,
                        "display_title": f"Factory 29 · fork repair · PR 3897 · {'b' * 32}",
                    }
                ]
            }
        ],
    }

    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        joined = " ".join(args)
        workflow = next(name for name in workflows if name in joined)
        status = next(state for state in ("queued", "in_progress") if f"status={state}" in joined)
        return responses[(workflow, status)]

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    assert controller.active_fixed_workers() == {13, 29}
    assert all("--paginate" in args and "--slurp" in args for args in calls)
    queried = {
        next(name for name in workflows if name in " ".join(args)) for args in calls
    }
    assert queried == set(workflows)


def test_active_fixed_workers_fails_closed_when_live_run_identity_is_unknown(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify active fixed workers fails closed when live run identity is unknown."""
    def fake_gh_json(args: list[str], **kwargs: object) -> object:
        joined = " ".join(args)
        if "free-model-factory-entry.yml" in joined and "status=queued" in joined:
            return [
                {
                    "workflow_runs": [
                        {"id": 99, "display_title": "Fixed Model Factory Entry"}
                    ]
                }
            ]
        if "status=in_progress" in joined or "factory-fork-execution.yml" in joined:
            return [{"workflow_runs": []}]
        if "issues/1093/comments" in joined:
            return [[]]
        raise AssertionError(joined)

    monkeypatch.setattr(controller, "gh_json", fake_gh_json)

    workers, unresolved = controller.active_fixed_workers()
    assert workers == set()
    assert unresolved == {"99"}



def test_active_unknown_run_uses_repo_scoped_neon_run_id_lookup(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One targeted event lookup resolves a live run without registry history."""
    requests: list[list[str]] = []
    monkeypatch.setitem(
        sys.modules, "factory_runtime_view",
        types.SimpleNamespace(run_workers=lambda repo, ids: (
            requests.append(ids) or {"99": "13"}
        )),
    )
    def github(args: list[str], **kwargs: object) -> object:
        endpoint = args[-1]
        if "issues/1093/comments" in endpoint:
            pytest.fail("worker liveness must never read the full registry")
        if "free-model-factory-entry.yml" in endpoint and "status=queued" in endpoint:
            return [{"workflow_runs": [{"id": 99, "display_title": "Unknown fixed factory"}]}]
        return [{"workflow_runs": []}]
    monkeypatch.setattr(controller, "gh_json", github)
    workers, unresolved = controller.active_fixed_workers()
    assert workers == {13}
    assert unresolved == set()
    assert requests == [["99"]]


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



def test_explicit_release_cannot_delete_a_newer_neon_claim(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture the old claim before labels change, then release that token only."""
    state = {"labels_modified": False}
    released_tokens: list[str] = []

    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [{
        "number": 2235,
        "labels": [{"name": "factory"}, {"name": "factory:42"}],
    }])

    def snapshot(_repo: str, _worker: str) -> tuple[str, list[int]]:
        assert not state["labels_modified"], "claim token must precede label release"
        return "old-token", [2235]

    def replace_labels(_number: int, _owner: str) -> bool:
        state["labels_modified"] = True
        return True

    monkeypatch.setattr(controller, "worker_claim", snapshot)
    monkeypatch.setattr(controller, "replace_factory_labels", replace_labels)
    monkeypatch.setattr(controller, "record_claim_released", lambda *args: None)
    monkeypatch.setattr(controller, "target_owned_by", lambda number, owner: False)
    monkeypatch.setattr(
        controller, "release_lease",
        lambda _repo, _worker, token, _reason: released_tokens.append(token),
    )

    assert controller.release_worker("42", reason="finished") == [2235]
    assert state["labels_modified"] is True
    assert released_tokens == ["old-token"]



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

    assert "ASSIGNED_PR_STAGE=" in worker
    assert "repair-no-change-ready-handoff" in worker
    assert 'pr list' not in worker[worker.index('select_controller_assignment() {'):worker.index('ensure_owner_label\n', worker.index('select_controller_assignment() {'))]



def test_manual_advance_selects_only_linked_pr_and_preserves_review_independence(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    """A manual issue request follows the linked PR and cannot select its producer."""
    issue_number, pr_number = 812, 813
    head = "a" * 40
    candidate = controller.Candidate(
        kind="pr", number=pr_number, lane=3, priority=0, created_at="",
        stage="factory:review", linked_issue=issue_number, producer_worker="13",
        head_contributors=frozenset({"13"}),
    )
    unrelated = controller.Candidate(
        kind="issue", number=814, lane=0, priority=0, created_at="",
    )
    monkeypatch.setattr(controller, "quota_pause_for", lambda worker: None)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda worker: False)
    monkeypatch.setattr(controller, "budget_block_reason", lambda worker: None)
    monkeypatch.setattr(controller, "omniroute_free_entry_capacity", lambda: {"remaining": 3})
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [{"number": pr_number, "headRefOid": head}])
    monkeypatch.setattr(controller, "linked_issue_from_pr", lambda pr: issue_number)
    monkeypatch.setattr(controller, "pr_suppresses_issue_candidate", lambda pr, issues: True)
    retry_reads: list[int | None] = []
    monkeypatch.setattr(
        controller, "load_no_diff_attempt_records",
        lambda *, target_pr=None: retry_reads.append(target_pr) or [],
    )
    monkeypatch.setattr(controller, "build_candidates", lambda *args, **kwargs: [unrelated, candidate])
    def forbidden_issue_intake(*_args: object) -> None:
        raise AssertionError("A targeted canonical PR must skip issue intake")
    monkeypatch.setattr(controller, "rotisserie_issue_intake", forbidden_issue_intake)
    monkeypatch.setattr(controller, "candidate_is_live_executable", lambda item: True)
    claims: list[tuple[object, str, dict[str, str]]] = []

    def claim(item: object, worker: str, **kwargs: str) -> bool:
        claims.append((item, worker, kwargs))
        return True

    monkeypatch.setattr(controller, "assign_candidate", claim)
    assert controller.assign("13", target=("issue", issue_number)) is None
    with pytest.raises(RuntimeError, match="Current action: review"):
        controller.assign("17", target=("issue", issue_number), requested_action="ci-repair")
    assert controller.assign("17", target=("issue", issue_number)) == candidate
    assert claims == [(candidate, "17", {"expected_head": head, "source": "manual"})]
    feedback = controller.replace(candidate, stage="factory:changes-requested")
    monkeypatch.setattr(controller, "build_candidates", lambda *args, **kwargs: [feedback])
    monkeypatch.setattr(controller, "required_checks_failed", lambda number: number == pr_number)
    assert controller.assign("17", target=("issue", issue_number), requested_action="ci-repair") == feedback
    assert claims[-1] == (
        feedback, "17", {"action": "ci-repair", "expected_head": head, "source": "manual"},
    )
    # Each wakeup selects the exact PR's retry window. The unrelated backlog's
    # global comment history and Rotisserie intake are never fetched.
    assert retry_reads == [pr_number] * 4


def test_shared_provider_quota_uses_trusted_neon_projection(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider cooldown applies to every dynamic slot for the same source."""
    import importlib

    slots = importlib.import_module("factory_worker_slots")
    operations = importlib.import_module("factory_state_operations")
    provider_workers = [
        item.worker for item in slots.slots()
        if item.source == "z-ai"
    ]
    assert len(provider_workers) > 1
    monkeypatch.setattr(operations, "enabled", lambda: True)
    monkeypatch.setattr(
        operations, "filter_workers",
        lambda _repo, _workers: ([], ["provider z-ai cooling"]),
    )
    for worker in provider_workers[:3]:
        assert "cooling" in (controller.runtime_quota_block_reason(worker) or "")
    monkeypatch.setattr(
        operations, "filter_workers",
        lambda _repo, workers: (workers, []),
    )
    for worker in provider_workers[:3]:
        assert controller.runtime_quota_block_reason(worker) is None


def test_provider_health_database_outage_fails_closed(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    slots = importlib.import_module("factory_worker_slots")
    operations = importlib.import_module("factory_state_operations")
    z_ai = next(item.worker for item in slots.slots() if item.source == "z-ai")
    nvidia = next(item.worker for item in slots.slots() if item.source == "nvidia")
    monkeypatch.setattr(operations, "enabled", lambda: False)
    assert "deferred" in (controller.runtime_quota_block_reason(z_ai) or "")
    assert controller.runtime_quota_block_reason(nvidia) is None


def test_issue_assignment_rechecks_dependency_at_mutation_boundary(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An advisory cached issue cannot be claimed if a fresh blocker appeared."""
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    issue = controller.Candidate("issue", 4737, 0, 0, "2026-11-14T00:00:00Z")
    monkeypatch.setattr(controller, "dispatcher_identity_verified", lambda: True)
    lookups = []
    lease_writes = []
    monkeypatch.setattr(controller, "target_still_unowned", lambda number: True)
    monkeypatch.setattr(controller, "issue_has_open_blocker",
                        lambda number, *, live=False: lookups.append((number, live)) or True)
    monkeypatch.setattr(controller, "acquire_lease", lambda *args: lease_writes.append(args))
    assert controller.assign_candidate(issue, "20") is False
    assert lookups == [(4737, True)]
    assert lease_writes == []


def test_live_blocker_lookup_explicitly_bypasses_neon_cache(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(controller, "gh_json",
                        lambda args: calls.append(args) or [{"state": "open"}])
    assert controller.issue_has_open_blocker(4737) is True
    assert controller.issue_has_open_blocker(4737, live=True) is True
    assert calls[0][0] == "api" and len(calls[0]) == 2
    assert calls[1][:3] == ["api", "--header", "Cache-Control: no-cache"]


def test_batch_assignment_omits_redundant_reconciliation(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_HAS_NVIDIA_API_KEY", "true")
    """Only the trusted reconciled batch skips global sweeps; manual calls keep them."""
    sweeps: list[str] = []
    monkeypatch.setattr(controller, "quota_pause_for", lambda _worker: None)
    monkeypatch.setattr(controller, "runtime_quota_block_reason", lambda _worker: None)
    monkeypatch.setattr(controller, "worker_has_active_lease", lambda _worker: False)
    monkeypatch.setattr(controller, "budget_block_reason", lambda _worker: None)
    monkeypatch.setattr(controller, "omniroute_free_entry_capacity", lambda: {"remaining": 1})
    monkeypatch.setattr(controller, "reconcile_stale_leases", lambda: sweeps.append("stale"))
    monkeypatch.setattr(controller, "reconcile_contradictory_labels", lambda: sweeps.append("labels"))
    monkeypatch.setattr(controller, "list_issues", lambda: [])
    monkeypatch.setattr(controller, "list_prs", lambda: [])
    monkeypatch.setattr(controller, "load_no_diff_attempt_records", lambda: [])
    monkeypatch.setattr(controller, "build_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(controller, "rotisserie_issue_intake", lambda *a: None)
    monkeypatch.setattr(controller, "resolve_review_head_contributors", lambda c, p: c)
    monkeypatch.setattr(controller, "order_candidates_for_worker", lambda c, w: c)
    monkeypatch.delenv("FACTORY_JEV_RANKING_FILE", raising=False)

    assert controller.assign("13", batch_reconciled=True) is None
    assert sweeps == []
    assert controller.assign("13") is None
    assert sweeps == ["stale", "labels"]


def test_release_label_write_updates_run_local_discovery(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A label write updates only this dispatcher run, with no Neon projection."""
    events: list[str] = []
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", "/trusted/dsn")
    monkeypatch.setattr(
        controller, "target_json",
        lambda number: {
            "number": number, "state": "open",
            "pull_request": {"url": "github"},
            "labels": [
                {"name": "factory"}, {"name": "factory:54"},
                {"name": "factory:changes-requested"},
            ],
        },
    )
    monkeypatch.setattr(
        controller, "run_gh",
        lambda *_args, **_kwargs: events.append("github-write") or "[]",
    )
    monkeypatch.setattr(
        controller, "update_labels",
        lambda repo, kind, number, labels: events.append(
            f"local:{repo}:{number}:{kind}:{any(row['name'] == 'factory:unowned' for row in labels)}"
        ),
    )
    assert controller.replace_factory_labels(5060, "factory:unowned") is True
    assert events == ["github-write", "local:JoshCLWren/comic-pile:5060:pr:True"]


def test_failed_local_discovery_refresh_does_not_undo_github_release(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Projection failure must not strand a successful GitHub label release."""
    events: list[str] = []
    monkeypatch.setenv("FACTORY_STATE_DSN_FILE", "/trusted/dsn")
    monkeypatch.setattr(
        controller, "target_json",
        lambda number: {
            "number": number, "state": "open",
            "labels": [{"name": "factory"}, {"name": "factory:54"}],
        },
    )
    monkeypatch.setattr(
        controller, "run_gh",
        lambda *_args, **_kwargs: events.append("github-write") or "[]",
    )
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("local scheduling memory unavailable")

    monkeypatch.setattr(controller, "update_labels", unavailable)
    assert controller.replace_factory_labels(4843, "factory:unowned") is True
    assert events == ["github-write"]


def _cli_assign(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    monkeypatch.setattr(sys, "argv", ["factory-work-controller.py", "assign", "--worker", "13", "--batch-reconciled"])
    assert controller.main() == 0
    reply: dict[str, object] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return reply


def test_cli_assign_marks_leased_worker_unavailable_without_reading_pool(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy worker must not be reported to the dispatcher as an empty pool."""
    monkeypatch.setattr(controller, "worker_skip_reason", lambda worker: f"Factory {worker} already has an active lease")
    monkeypatch.setattr(
        controller,
        "list_issues",
        lambda: (_ for _ in ()).throw(AssertionError("must not rank new work")),
    )

    assert _cli_assign(controller, monkeypatch, capsys) == {"kind": "none", "reason": "worker_unavailable"}


def test_cli_assign_reports_no_work_when_eligible_worker_finds_empty_pool(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only an eligible worker that ranked the pool can report no_work."""
    monkeypatch.setattr(controller, "worker_skip_reason", lambda worker: None)
    monkeypatch.setattr(controller, "assign_from_pool", lambda *args, **kwargs: None)

    assert _cli_assign(controller, monkeypatch, capsys) == {"kind": "none", "reason": "no_work"}


def test_worker_skip_reason_stops_at_quota_pause_before_lease_lookup(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quota-paused worker is skipped without consulting leases or budgets."""
    monkeypatch.setattr(controller, "quota_pause_for", lambda worker: {"worker": worker})
    monkeypatch.setattr(controller, "describe_quota_pause", lambda pause: "Factory 13 quota-paused")
    monkeypatch.setattr(
        controller,
        "worker_has_active_lease",
        lambda worker: (_ for _ in ()).throw(AssertionError("lease must not be read")),
    )

    assert controller.worker_skip_reason("13") == "Factory 13 quota-paused"
