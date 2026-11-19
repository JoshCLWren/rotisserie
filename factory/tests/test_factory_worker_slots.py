"""Worker identity is independent of live provider/model catalogs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
SLOTS = importlib.import_module("factory_worker_slots")


def test_provider_additions_do_not_renumber_existing_sources() -> None:
    existing = SLOTS.sources()
    source = existing[0]
    first = SLOTS.worker_id(source, 0)
    assert first == SLOTS.worker_id(source, 0)
    assert first.isdecimal()
    assert int(first) > 1_000_000_000
    assert SLOTS.resolve(first, {"FACTORY_SLOTS_PER_PROVIDER": "2"}).source == source


def test_slots_are_unique_and_model_agnostic() -> None:
    all_slots = SLOTS.slots({"FACTORY_SLOTS_PER_PROVIDER": "3"})
    assert len(all_slots) == 3 * len(SLOTS.sources())
    assert len({item.worker for item in all_slots}) == len(all_slots)
    assert {item.source for item in all_slots} == set(SLOTS.sources())
    assert all("model" not in item.as_record() for item in all_slots)


def test_source_diverse_offer_and_rotation() -> None:
    env = {"FACTORY_SLOTS_PER_PROVIDER": "2"}
    first, second = SLOTS.offer(0, env), SLOTS.offer(1, env)
    assert set(first) == set(second)
    assert first != second
    assert len({SLOTS.resolve(worker, env).source for worker in first[:len(SLOTS.sources())]}) == len(SLOTS.sources())


def test_invalid_slots_do_not_resolve() -> None:
    env = {"FACTORY_SLOTS_PER_PROVIDER": "2"}
    with pytest.raises(ValueError):
        SLOTS.resolve("42", env)
    with pytest.raises(ValueError):
        SLOTS.worker_id("not-a-real-provider", 0)
    with pytest.raises(ValueError):
        SLOTS.slot_count({"FACTORY_SLOTS_PER_PROVIDER": "0"})
    with pytest.raises(ValueError):
        SLOTS.slot_count({"FACTORY_SLOTS_PER_PROVIDER": "invalid"})
