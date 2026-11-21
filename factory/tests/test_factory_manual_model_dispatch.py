"""Exact manual model must preserve existing lease and free Go constraints."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
from factory_opencode_go import GoCatalogError, choose


def sample():
    zero = {"type": "chat", "capabilities": {"tools": {"supported": True}},
            "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}}}
    return {"opencode-go": {"models": {"hy4-flash-free": zero, "another-free": zero}}}


def test_exact_model_rejects_others_and_model_cooldown():
    picked = choose(sample(), "1327811785100",
                    required_model="opencode-go/hy4-flash-free")
    assert picked["runtime_model"] == "opencode-go/hy4-flash-free"
    with pytest.raises(GoCatalogError, match="cooling"):
        choose(sample(), "1327811785100", excluded={"hy4-flash-free"},
               required_model="opencode-go/hy4-flash-free")
    with pytest.raises(GoCatalogError, match="Unsupported"):
        choose(sample(), "1327811785100", required_model="opencode-go/another-free")


def test_target_only_claims_eligible_go_worker(monkeypatch):
    import factory_completion_controller as completion

    spec = importlib.util.spec_from_file_location(
        "factory_work_controller", ROOT / ".github/scripts/factory-work-controller.py"
    )
    assert spec and spec.loader
    controller = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "factory_work_controller", controller)
    spec.loader.exec_module(controller)
    monkeypatch.setattr(completion, "durable_factory_state", lambda: ({}, {}))
    monkeypatch.setattr(completion, "load_manifest_candidates", lambda: [])
    monkeypatch.setattr(completion, "capacity_report", lambda *_a, **_kw: {
        "executable_candidates": [
            {"worker": "1327811785100"}, {"worker": "1004230852000"},
        ]
    })
    monkeypatch.setattr(controller, "worker_sources", lambda: {
        "1327811785100": "opencode-free", "1004230852000": "nvidia",
    })
    calls = []
    monkeypatch.setattr(controller, "assign", lambda worker, **_kw: (
        calls.append(worker) or SimpleNamespace(kind="issue", number=123)
    ))
    monkeypatch.setattr(controller, "assignment_context",
                        lambda _c, worker: {"worker": worker, "claim_id": "synthetic"})
    emitted = []
    monkeypatch.setattr(controller, "run_gh", lambda args: emitted.append(args))
    result = controller.dispatch_target(
        "issue", 123, "implement", "opencode-go/hy4-flash-free"
    )
    assert result["status"] == "dispatched"
    assert result["worker"] == "1327811785100"
    assert calls == ["1327811785100"]
    assert "model=opencode-go/hy4-flash-free" in emitted[0]
    with pytest.raises(ValueError, match="Unsupported"):
        controller.dispatch_target("issue", 123, "implement", "opencode/paid")


def test_model_choice_is_end_to_end_and_never_falls_back():
    dispatch = (ROOT / ".github/workflows/fixed-model-factory-dispatch.yml").read_text()
    entry = (ROOT / ".github/workflows/free-model-factory-entry.yml").read_text()
    runner = (ROOT / ".github/workflows/free-model-factory-run.yml").read_text()
    worker = (ROOT / ".github/scripts/free-model-factory-worker.sh").read_text()
    assert "Exact runtime provider/model ID" in dispatch
    assert "dispatch-next" in dispatch
    assert '--model "$REQUESTED_MODEL"' in dispatch
    assert 'model: ${{ inputs.model }}' in entry
    assert 'REQUESTED_MODEL: ${{ inputs.model ||' in runner
    assert "--required-model" in runner
    assert "Exact requested model could not be selected; no provider fallback" in runner
    assert 'FACTORY_EXACT_MODEL: ${{ inputs.model ||' in runner
    assert "Explicit operator provider/model selection" in worker
    assert "factory-work-controller.py validate-assignment" in runner
