"""Tests for ComicPile's bounded Rotisserie adoption control."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "factory_rotisserie_control.py"


def load_control() -> ModuleType:
    """Load the workflow-side control as a testable module."""
    spec = importlib.util.spec_from_file_location("factory_rotisserie_control", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state(stage: str = "legacy") -> dict[str, object]:
    """Build the one supported ComicPile lane state."""
    return {
        "schema_version": 1,
        "control_revision": "controls-1",
        "lane": {"name": "issue-intake", "subjects": ["label:factory:unowned"]},
        "stage": stage,
        "last_operation_key": None,
    }


def result(action: str, start: str, target: str) -> dict[str, Any]:
    """Build an authorized Rotisserie CLI result."""
    transition: dict[str, object] = {
        "schema_version": 1,
        "control_revision": "controls-1",
        "action": action,
        "lane": {"name": "issue-intake", "subjects": ["label:factory:unowned"]},
        "expected_stage": start,
        "target_stage": target,
    }
    encoded = json.dumps(transition, sort_keys=True, separators=(",", ":")).encode()
    transition["operation_key"] = f"adoption-transition:{hashlib.sha256(encoded).hexdigest()}"
    return {
        "status": "authorized",
        "evidence": {"remote_mutation": False, "transition": transition},
    }


def test_canary_and_rollback() -> None:
    control = load_control()

    canary = control.apply_transition(state(), result("enter_canary", "legacy", "canary"))
    restored = control.apply_transition(canary, result("rollback", "canary", "legacy"))

    assert canary["stage"] == "canary"
    assert restored["stage"] == "legacy"
    assert restored["last_operation_key"] != canary["last_operation_key"]


def test_stale_transition_fails_closed() -> None:
    control = load_control()
    canary = control.apply_transition(state(), result("enter_canary", "legacy", "canary"))

    try:
        control.apply_transition(canary, result("enter_canary", "legacy", "canary"))
    except control.ControlError as error:
        assert "stage changed" in str(error)
    else:
        raise AssertionError("stale transition was accepted")


def test_tampering_fails_closed() -> None:
    control = load_control()
    command = result("enter_canary", "legacy", "canary")
    command["evidence"]["transition"]["target_stage"] = "expanded"

    try:
        control.apply_transition(state(), command)
    except control.ControlError as error:
        assert "operation key" in str(error)
    else:
        raise AssertionError("modified transition was accepted")
