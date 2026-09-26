"""Regression coverage for demand-driven factory capacity allocation."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "factory_capacity_policy.py"
SPEC = importlib.util.spec_from_file_location("factory_capacity_policy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


def _enable_omniroute(monkeypatch) -> None:
    """Re-enable OmniRoute for capacity-math assertions during the incident freeze."""
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")


def demand(completion: int, production: int, idle: int):
    return policy.FleetDemand(
        completion=completion,
        production=production,
        idle_workers=idle,
    )


def test_completion_uses_full_idle_fleet_when_no_production_waits():
    current = demand(completion=35, production=0, idle=30)
    assert policy.completion_worker_target(current) == 30
    assert policy.production_worker_target(current) == 0


def test_mixed_workload_allocates_capacity_by_current_demand_ratio():
    current = demand(completion=30, production=10, idle=20)
    assert current.completion_share == 0.75
    assert policy.completion_worker_target(current) == 15
    assert policy.production_worker_target(current) == 5


def test_small_completion_queue_does_not_capture_the_whole_fleet():
    current = demand(completion=2, production=18, idle=10)
    assert policy.completion_worker_target(current) == 1
    assert policy.production_worker_target(current) == 9


def test_completion_demand_never_starves_when_idle_capacity_exists():
    current = demand(completion=1, production=999, idle=8)
    assert policy.completion_worker_target(current) == 1


def test_targets_are_bounded_by_real_queue_depth_and_capacity():
    assert policy.completion_worker_target(demand(3, 0, 50)) == 3
    assert policy.completion_worker_target(demand(50, 0, 3)) == 3
    assert policy.completion_worker_target(demand(0, 50, 10)) == 0


def test_omniroute_free_entry_cap_default_is_three_concurrent_units(monkeypatch):
    _enable_omniroute(monkeypatch)
    assert policy.DEFAULT_OMNIROUTE_FREE_ENTRY_CAP == 3
    assert policy.remaining_omniroute_free_entry_slots(0) == 3
    assert policy.remaining_omniroute_free_entry_slots(2) == 1
    assert policy.remaining_omniroute_free_entry_slots(3) == 0
    assert policy.remaining_omniroute_free_entry_slots(12) == 0


def test_omniroute_free_entry_cap_bounds_idle_workers_without_erasing_demand(monkeypatch):
    _enable_omniroute(monkeypatch)
    current = demand(completion=35, production=10, idle=30)
    capped = policy.apply_omniroute_free_entry_cap(current, in_flight=1)

    assert capped.completion == 35
    assert capped.production == 10
    assert capped.idle_workers == 2
    assert policy.completion_worker_target(capped) == 2
    assert policy.production_worker_target(capped) == 0


def test_omniroute_free_entry_cap_leaves_idle_unchanged_when_already_inside_budget(monkeypatch):
    _enable_omniroute(monkeypatch)
    current = demand(completion=2, production=1, idle=2)
    assert policy.apply_omniroute_free_entry_cap(current, in_flight=0) is current


def test_exhausted_omniroute_free_entry_cap_allocates_no_workers(monkeypatch):
    _enable_omniroute(monkeypatch)
    current = policy.apply_omniroute_free_entry_cap(demand(20, 20, 20), in_flight=3)
    assert current.idle_workers == 0
    assert policy.completion_worker_target(current) == 0
    assert policy.production_worker_target(current) == 0


def test_omniroute_free_entry_slot_counts_reject_negative_inputs(monkeypatch):
    _enable_omniroute(monkeypatch)
    try:
        policy.remaining_omniroute_free_entry_slots(-1)
    except ValueError as exc:
        assert "in-flight" in str(exc)
    else:
        raise AssertionError("negative in-flight must fail closed")
    try:
        policy.remaining_omniroute_free_entry_slots(0, cap=-1)
    except ValueError as exc:
        assert "cap" in str(exc)
    else:
        raise AssertionError("negative cap must fail closed")


def test_omniroute_disabled_uses_multi_provider_entry_budget(monkeypatch):
    monkeypatch.delenv("FACTORY_OMNIROUTE_ENABLED", raising=False)
    assert policy.omniroute_enabled() is False
    assert policy.remaining_omniroute_free_entry_slots(0) == policy.DEFAULT_MULTI_PROVIDER_ENTRY_CAP
    assert policy.remaining_omniroute_free_entry_slots(3) == policy.DEFAULT_MULTI_PROVIDER_ENTRY_CAP - 3
    capped = policy.apply_omniroute_free_entry_cap(demand(20, 20, 20), in_flight=0)
    assert capped.idle_workers == policy.DEFAULT_MULTI_PROVIDER_ENTRY_CAP


def test_omniroute_reenable_restores_free_entry_capacity(monkeypatch):
    monkeypatch.setenv("FACTORY_OMNIROUTE_ENABLED", "on")
    assert policy.omniroute_enabled() is True
    assert policy.remaining_omniroute_free_entry_slots(0) == policy.DEFAULT_OMNIROUTE_FREE_ENTRY_CAP
