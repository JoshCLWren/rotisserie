from __future__ import annotations

import pytest

from rotisserie.domain import WorkerId, WorkerInventory, resolve_wip_limit


def worker(value: str, namespace: str = "model") -> WorkerId:
    return WorkerId(namespace, value)


def test_expected_size_is_the_config_derived_ceiling() -> None:
    inventory = WorkerInventory(frozenset({worker("a"), worker("b"), worker("c")}))
    assert inventory.size == 3
    assert worker("a") in inventory.expected
    assert worker("a") not in inventory.retired
    assert worker("a") not in inventory.scheduled


def test_inventory_size_has_no_hard_ceiling() -> None:
    # Identities far beyond any historical fixed ceiling resolve from membership.
    expected = frozenset(worker(f"worker-{index}") for index in range(1, 201))
    inventory = WorkerInventory(expected)
    assert inventory.size == 200
    assert worker("worker-200") in inventory.expected


def test_scheduled_and_retired_membership_is_tracked() -> None:
    inventory = WorkerInventory(
        expected=frozenset({worker("a"), worker("b")}),
        retired=frozenset({worker("c")}),
        scheduled=frozenset({worker("a")}),
    )
    assert inventory.scheduled == frozenset({worker("a")})
    assert worker("b") not in inventory.scheduled
    assert worker("c") in inventory.retired
    assert worker("c") not in inventory.expected


def test_empty_expected_inventory_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one expected worker"):
        WorkerInventory(frozenset())


def test_expected_and_retired_must_be_disjoint() -> None:
    with pytest.raises(ValueError, match="both expected and retired"):
        WorkerInventory(
            expected=frozenset({worker("a")}),
            retired=frozenset({worker("a")}),
        )


def test_scheduled_must_be_subset_of_expected() -> None:
    with pytest.raises(ValueError, match="subset of expected"):
        WorkerInventory(
            expected=frozenset({worker("a")}),
            scheduled=frozenset({worker("b")}),
        )


def test_resolve_wip_limit_prefers_explicit_override() -> None:
    inventory = WorkerInventory(frozenset({worker("a"), worker("b")}))
    assert resolve_wip_limit(inventory, 7) == 7


def test_resolve_wip_limit_defaults_to_inventory_size() -> None:
    inventory = WorkerInventory(frozenset({worker("a"), worker("b")}))
    assert resolve_wip_limit(inventory, None) == 2


def test_resolve_wip_limit_without_inventory_falls_back_to_one() -> None:
    assert resolve_wip_limit(None, None) == 1
    assert resolve_wip_limit(None, 4) == 4


def test_resolve_wip_limit_rejects_negative_override() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        resolve_wip_limit(None, -1)
