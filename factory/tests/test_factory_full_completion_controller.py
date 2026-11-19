"""Regression coverage for demand-driven completion drain selection."""
from __future__ import annotations

import importlib.util
import inspect
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

scripts_dir = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(scripts_dir))

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "factory_full_completion_controller.py"
)
SPEC = importlib.util.spec_from_file_location("factory_full_completion_controller", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
full = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = full
SPEC.loader.exec_module(full)


@contextmanager
def stubbed_work_controller(controller):
    """Replace the gh-backed work controller with an offline stub.

    signal_dispatch only needs owned-worker lookup and reconciliation
    hooks from the work controller; stubbing keeps these regression tests
    hermetic (no gh auth or network required).
    """
    work = MagicMock()
    work.list_issues.return_value = []
    work.list_prs.return_value = []
    work.reconcile_stale_leases.return_value = []
    work.reconcile_contradictory_labels.return_value = []
    original = controller.load_controller
    controller.load_controller = lambda: work
    try:
        yield work
    finally:
        controller.load_controller = original


def test_signal_dispatch_returns_no_assignments():
    controller = full.load_controller()
    demand = full.FleetDemand(
        completion=0, production=0, idle_workers=0
    )
    capacity = {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12}
    with stubbed_work_controller(controller):
        result = full.signal_dispatch(controller, demand, capacity)
    assert result["assignments"] == []
    assert result["signal"] in ("explicit-worker", "roster")
    # Capacity refill derives its executable worker set from the canonical
    # capacity report, so it must be consistent with demand measurement.
    assert result["idle_executable_workers"] >= 0


def test_signal_dispatch_includes_demand_data():
    controller = full.load_controller()
    demand = full.FleetDemand(
        completion=1, production=2, idle_workers=3
    )
    capacity = {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12}
    with stubbed_work_controller(controller):
        result = full.signal_dispatch(controller, demand, capacity)
    assert "completion_demand" in result
    assert "production_demand" in result
    assert "completion_share" in result
    assert "completion_target" in result
    assert "capacity" in result


def test_signal_dispatch_reuses_snapshot_without_any_github_reads_or_reconciliation():
    controller = full.load_controller()
    demand = full.FleetDemand(
        completion=2, production=1, idle_workers=2
    )
    capacity = {
        "enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12,
        "owned_workers": ["11"],
        "executable_candidates": [{"worker": "11"}, {"worker": "20"}],
    }
    with stubbed_work_controller(controller) as work:
        result = full.signal_dispatch(controller, demand, capacity)
    assert "signal" in result
    assert result["assignments"] == []
    assert result["selected_workers"] == ["20"]
    # All owner/lease evidence came from current_demand's same snapshot.
    # Only the next serialized dispatcher may reconcile and claim work.
    work.list_issues.assert_not_called()
    work.list_prs.assert_not_called()
    work.reconcile_stale_leases.assert_not_called()
    work.reconcile_contradictory_labels.assert_not_called()


def test_signal_dispatch_omits_direct_assignment():
    controller = full.load_controller()
    demand = full.FleetDemand(
        completion=0, production=0, idle_workers=0
    )
    capacity = {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12}
    with stubbed_work_controller(controller):
        result = full.signal_dispatch(controller, demand, capacity)
    assert result["assignments"] == []


def test_main_does_not_assign_directly():
    from unittest.mock import patch

    with patch.object(full, "current_demand") as mock_demand, \
         patch.object(full, "persist_funnel_telemetry") as mock_persist, \
         patch.object(full, "signal_dispatch") as mock_signal:
        mock_demand.return_value = (
            full.FleetDemand(completion=0, production=0, idle_workers=0),
            {"enabled": 0, "in_flight": 0, "cap": 12, "remaining": 12},
        )
        mock_signal.return_value = {"signal": "roster", "assignments": []}
        full.main()
        mock_signal.assert_called_once()
        mock_persist.assert_called_once()


def test_raw_demand_is_not_erased_by_legacy_review_backpressure_threshold():
    controller = full.load_controller()
    policy = controller.load_policy()
    prs = [
        {
            "number": 100 + index,
            "state": "OPEN",
            "isDraft": False,
            "headRefName": f"factory/6-{2000 + index}-review",
            "labels": ["factory:review", "factory:unowned"],
            "createdAt": "2026-08-24T00:00:00Z",
        }
        for index in range(policy.FACTORY_REVIEW_BACKLOG_LIMIT)
    ]
    issue = {
        "number": 9999,
        "state": "OPEN",
        "labels": ["factory:unowned"],
        "createdAt": "2026-08-24T00:00:00Z",
    }

    legacy = policy.build_candidates([issue], prs, no_diff_attempts_by_issue={})
    assert not any(candidate.kind == "issue" for candidate in legacy)
    assert full.raw_work_demand(policy, [issue], prs) == (
        policy.FACTORY_REVIEW_BACKLOG_LIMIT,
        1,
    )


def test_current_demand_caps_idle_workers_to_remaining_omniroute_slots():
    from unittest.mock import patch

    with patch.dict("os.environ", {"FACTORY_OMNIROUTE_ENABLED": "on"}):
        _assert_current_demand_caps()


def test_current_demand_uses_multi_provider_budget_when_omniroute_dark():
    from unittest.mock import patch

    env = dict(__import__("os").environ)
    env.pop("FACTORY_OMNIROUTE_ENABLED", None)
    with patch.dict("os.environ", env, clear=True):
        _assert_current_demand_multi_provider()


def _assert_current_demand_caps():
    class FakeWork:
        def list_issues(self):
            return []

        def list_prs(self):
            return [{"number": 44, "headRefName": "factory/44-44", "labels": []}]

        def in_flight_omniroute_free_entries(self):
            return 2

    class FakePolicy:
        def comment_is_trusted(self, comment):
            return True

        def pr_is_static_candidate(self, pr, issue_map):
            return True

        def linked_issue_from_branch(self, name):
            return None

        def linked_issue_from_pr(self, pr):
            return None

        def pr_suppresses_issue_candidate(self, pr, issue_map):
            return False

        def issue_is_static_candidate(self, issue, suppressing, no_diff_attempts=0):
            return False

    class FakeCompletion:
        def load_controller(self):
            return FakeWork()

        def load_policy(self):
            return FakePolicy()

        def load_manifest_candidates(self, manifest=None):
            return [{"worker": str(worker)} for worker in range(6, 16)]

        def owned_worker_ids(self, items):
            return set()

        def durable_factory_state(self, now_epoch=None):
            return {}, {}

        def capacity_report(self, candidates, health, now_epoch):
            return {"executable_slot_capacity": 10}

        def worker_is_executable(self, worker, health, now_epoch):
            return True

    measured, capacity = full.current_demand(FakeCompletion(), now_epoch=1)
    assert measured.idle_workers == 1
    assert measured.completion == 1
    assert measured.production == 0
    assert full.completion_worker_target(measured) == 1
    assert capacity["executable_slot_capacity"] == 10


def _assert_current_demand_multi_provider():
    class FakeWork:
        def list_issues(self):
            return []

        def list_prs(self):
            return [{"number": 44, "headRefName": "factory/44-44", "labels": []}]

        def in_flight_omniroute_free_entries(self):
            return 2

    class FakePolicy:
        def comment_is_trusted(self, comment):
            return True

        def pr_is_static_candidate(self, pr, issue_map):
            return True

        def linked_issue_from_branch(self, name):
            return None

        def linked_issue_from_pr(self, pr):
            return None

        def pr_suppresses_issue_candidate(self, pr, issue_map):
            return False

        def issue_is_static_candidate(self, issue, suppressing, no_diff_attempts=0):
            return False

    class FakeCompletion:
        def load_controller(self):
            return FakeWork()

        def load_policy(self):
            return FakePolicy()

        def load_manifest_candidates(self, manifest=None):
            return [{"worker": str(worker)} for worker in range(6, 16)]

        def owned_worker_ids(self, items):
            return set()

        def durable_factory_state(self, now_epoch=None):
            return {}, {}

        def capacity_report(self, candidates, health, now_epoch):
            return {"executable_slot_capacity": 10}

        def worker_is_executable(self, worker, health, now_epoch):
            return True

    measured, capacity = full.current_demand(FakeCompletion(), now_epoch=1)
    assert measured.idle_workers == 10
    assert measured.completion == 1
    assert measured.production == 0
    assert full.completion_worker_target(measured) == 1
    assert capacity["executable_slot_capacity"] == 10

def test_no_work_dispatch_signal_writes_no_summary(monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    full.persist_funnel_telemetry({"selected_workers": []})
    assert not summary.exists()


def test_scheduled_funnel_goes_to_job_summary_never_github_comments(monkeypatch, tmp_path):
    """Issue #1093 is retired: the funnel lives in the trusted Actions run."""
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    full.persist_funnel_telemetry({"selected_workers": ["11"], "completion_target": 1})
    text = summary.read_text(encoding="utf-8")
    assert full.TELEMETRY_MARKER in text
    assert "Workers selected: 1" in text
    assert "Updated: " in text
    # The signature has no controller, so it cannot reach the GitHub API.
    assert list(inspect.signature(full.persist_funnel_telemetry).parameters) == ["result"]
    assert not hasattr(full, "TELEMETRY_COMMENT_ID")


def test_missing_summary_file_does_not_block_factory(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    full.persist_funnel_telemetry({"selected_workers": ["11"]})
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing-dir" / "summary.md"))
    full.persist_funnel_telemetry({"selected_workers": ["11"]})


def test_zero_completion_demand_never_reads_registry_or_inflight_capacity():
    """An empty completion queue must not burn ~20 pages of registry quota."""
    completion = MagicMock()
    work = MagicMock()
    work.list_issues.return_value = []
    work.list_prs.return_value = []
    completion.load_controller.return_value = work
    completion.load_policy.return_value = MagicMock()
    completion.durable_factory_state.side_effect = AssertionError("unnecessary health scan")
    completion.load_manifest_candidates.side_effect = AssertionError("unnecessary manifest")
    completion.owned_worker_ids.side_effect = AssertionError("unnecessary owners")
    work.in_flight_omniroute_free_entries.side_effect = AssertionError("unnecessary inflight scan")

    demand, capacity = full.current_demand(completion, now_epoch=1)
    assert demand.completion == 0
    assert demand.production == 0
    assert demand.idle_workers == 0
    assert capacity["capacity_scan_skipped"] is True
    assert capacity["executable_candidates"] == []
    assert full.signal_dispatch(completion, demand, capacity)["selected_workers"] == []
    completion.durable_factory_state.assert_not_called()
    completion.owned_worker_ids.assert_not_called()
    work.in_flight_omniroute_free_entries.assert_not_called()
