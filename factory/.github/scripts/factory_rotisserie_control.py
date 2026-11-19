#!/usr/bin/env python3
"""Apply a Rotisserie adoption transition to ComicPile's bounded lane control."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ControlError(ValueError):
    """Raised when a transition cannot safely change the adopter control."""


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlError(f"{label} must be an object")
    return value


def _operation_key(transition: dict[str, Any]) -> str:
    payload = {
        "schema_version": 1,
        "control_revision": transition.get("control_revision"),
        "action": transition.get("action"),
        "lane": transition.get("lane"),
        "expected_stage": transition.get("expected_stage"),
        "target_stage": transition.get("target_stage"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"adoption-transition:{hashlib.sha256(encoded).hexdigest()}"


def apply_transition(state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Return control state after one exact, authorized compare-and-swap."""
    if state.get("schema_version") != 1:
        raise ControlError("unsupported control schema version")
    if result.get("status") != "authorized":
        raise ControlError("Rotisserie did not authorize a transition")
    evidence = _object(result.get("evidence"), "evidence")
    if evidence.get("remote_mutation") is not False:
        raise ControlError("adoption evidence must be non-mutating")
    transition = _object(evidence.get("transition"), "transition")
    if transition.get("schema_version") != 1:
        raise ControlError("unsupported transition schema version")
    if transition.get("operation_key") != _operation_key(transition):
        raise ControlError("transition operation key does not match its content")
    if transition.get("control_revision") != state.get("control_revision"):
        raise ControlError("control revision changed after authorization")
    if transition.get("lane") != state.get("lane"):
        raise ControlError("transition is outside the configured lane")
    if transition.get("expected_stage") != state.get("stage"):
        raise ControlError("lane stage changed after authorization")

    target = transition.get("target_stage")
    if target not in {"legacy", "canary", "expanded"}:
        raise ControlError("invalid target stage")
    action = transition.get("action")
    expected = {
        "enter_canary": ("legacy", "canary"),
        "expand": ("canary", "expanded"),
        "rollback": (state.get("stage"), "legacy"),
    }.get(action)
    if expected != (state.get("stage"), target) or (
        action == "rollback" and state.get("stage") == "legacy"
    ):
        raise ControlError("transition stages do not match its action")

    return {
        **state,
        "stage": target,
        "last_operation_key": transition["operation_key"],
    }


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    """Apply one transition envelope to a local control file."""
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        state = _object(json.loads(arguments.state.read_text()), "control")
        result = _object(json.loads(arguments.result.read_text()), "result")
        updated = apply_transition(state, result)
        _write_atomic(arguments.state, updated)
    except (ControlError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(updated, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
