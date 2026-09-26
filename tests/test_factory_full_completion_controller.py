"""Regression coverage for demand-driven completion drain selection."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


def test_selector_uses_calculated_target_without_backlog_thresholds():
    controller = full.load_controller()
    full.configure_demand_selection(controller, target=7)

    workers = [str(worker) for worker in range(6, 26)]
    health = dict.fromkeys(workers, ("success", 0))
    selected = controller.select_completion_workers(
        workers,
        review_backlog=3,
        health=health,
        now_epoch=1,
    )

    assert selected == workers[:7]
    assert controller.completion_batch_size(3) == 7


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


def test_only_healthy_or_degraded_workers_count_as_executable_capacity():
    controller = full.load_controller()
    full.configure_demand_selection(controller, target=4)
    now = controller.parse_time("2026-08-24T17:00:00Z")
    assert now is not None

    workers = ["6", "7", "8", "9", "10", "11"]
    health = {
        "6": ("failure", now - 60),
        "8": ("RATE LIMITED", now - 60),
        "9": ("success", now - 60),
        "10": ("MODEL MISSING", now - 60),
        "11": ("provider_unavailable", now - 3600),
    }

    selected = controller.select_completion_workers(
        workers,
        review_backlog=35,
        owned_workers={"7"},
        health=health,
        now_epoch=now,
    )

    assert "7" not in selected
    assert "10" not in selected
    assert "6" not in selected
    assert "8" not in selected
    assert selected == ["9", "11"]


def test_current_demand_caps_idle_workers_to_remaining_omniroute_slots(monkeypatch):
    # OmniRoute defaults off for the incident freeze; this test exercises the
    # OmniRoute free-entry ceiling (cap 3, in-flight 2 → remaining 1).
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")

    class FakeWork:
        def list_issues(self):
            return []

        def list_prs(self):
            return []

        def in_flight_omniroute_free_entries(self):
            return 2

    class FakePolicy:
        def comment_is_trusted(self, comment):
            return True

        def pr_is_static_candidate(self, pr, issue_map):
            return False

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

        def load_manifest_candidates(self, manifest):
            return [{"worker": str(worker)} for worker in range(6, 16)]

        def owned_worker_ids(self, items):
            return set()

        def registry_comments(self):
            return []

        def latest_worker_health(self, comments, trusted):
            return {}

        def capacity_report(self, candidates, health, now_epoch):
            return {"executable_slot_capacity": 10}

        def worker_is_executable(self, worker, health, now_epoch):
            return True

    measured, capacity = full.current_demand(FakeCompletion(), now_epoch=1)
    assert measured.idle_workers == 1
    assert measured.completion == 0
    assert measured.production == 0
    assert full.completion_worker_target(measured) == 0
    assert capacity["executable_slot_capacity"] == 10


def test_current_demand_uses_multi_provider_budget_when_omniroute_dark(monkeypatch):
    monkeypatch.delenv("FACTORY_OMNIROUTE_ENABLED", raising=False)

    class FakeWork:
        def list_issues(self):
            return []

        def list_prs(self):
            return []

        def in_flight_omniroute_free_entries(self):
            return 2

    class FakePolicy:
        def comment_is_trusted(self, comment):
            return True

        def pr_is_static_candidate(self, pr, issue_map):
            return False

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

        def load_manifest_candidates(self, manifest):
            return [{"worker": str(worker)} for worker in range(6, 16)]

        def owned_worker_ids(self, items):
            return set()

        def registry_comments(self):
            return []

        def latest_worker_health(self, comments, trusted):
            return {}

        def capacity_report(self, candidates, health, now_epoch):
            return {"executable_slot_capacity": 10}

        def worker_is_executable(self, worker, health, now_epoch):
            return True

    measured, capacity = full.current_demand(FakeCompletion(), now_epoch=1)
    # 10 executable idle workers fit under multi-provider remaining (12 - 2).
    assert measured.idle_workers == 10
    assert measured.completion == 0
    assert measured.production == 0
    assert full.completion_worker_target(measured) == 0
    assert capacity["executable_slot_capacity"] == 10
