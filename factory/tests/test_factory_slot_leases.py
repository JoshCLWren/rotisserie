"""Slot lease names and input checks, without a database (#239)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

ledger = importlib.import_module("factory_state_ledger")


def test_slot_keys_live_outside_the_single_target_namespace() -> None:
    key = ledger.slot_key("acme/widgets", 239, "attempt-2")
    assert key == "factory-lease-v1:acme/widgets:slot:239:attempt-2"
    assert not key.startswith(ledger._target_key("acme/widgets", 239))


def test_attempt_slots_are_numbered_from_one() -> None:
    assert ledger.attempt_slots(3) == ["attempt-1", "attempt-2", "attempt-3"]


@pytest.mark.parametrize(
    "slots",
    [
        [],
        ["attempt-0"],
        ["attempt-1", "attempt-1"],
        ["piece-"],
        ["piece-Bad"],
        ["piece-a:b"],
        ["other-1"],
        [f"attempt-{n}" for n in range(1, 34)],
    ],
)
def test_bad_slot_lists_are_refused(slots: list[str]) -> None:
    with pytest.raises(ValueError):
        ledger.acquire_slot("acme/widgets", "3", 239, slots, 600)


@pytest.mark.parametrize(
    ("worker", "number", "ttl", "distinct"),
    [("x", 239, 600, "provider"), ("3", 0, 600, "provider"), ("3", 239, 5, "provider"),
     ("3", 239, 600, "model")],
)  # fmt: skip
def test_bad_claims_are_refused(worker: str, number: int, ttl: int, distinct: str) -> None:
    with pytest.raises(ValueError):
        ledger.acquire_slot("acme/widgets", worker, number, ["attempt-1"], ttl, distinct=distinct)


def test_without_neon_racing_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_STATE_DSN_FILE", raising=False)
    answer = ledger.acquire_slot("acme/widgets", "3", 239, ["attempt-1", "piece-api"], 600)
    assert answer == {"status": "conflict", "reason": "ledger_disabled"}
    assert ledger.slot_holders("acme/widgets", 239) == []
