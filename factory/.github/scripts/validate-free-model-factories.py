#!/usr/bin/env python3
"""Validate model-agnostic factory discovery and provider-derived worker identity.

No test or contract assumes a particular published model ID or worker roster.
Execution availability is proved only by a real worker run, not this validator.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = ROOT / ".github" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_cred = importlib.import_module("factory_dispatch_credential_filter")
filter_workers = _cred.filter_workers
worker_sources = _cred.worker_sources
_cand = importlib.import_module("factory_provider_candidates")
discover = _cand.discover
PROVIDERS = importlib.import_module("factory_provider_registry").PROVIDERS
_slots = importlib.import_module("factory_worker_slots")
resolve = _slots.resolve
slot_count = _slots.slot_count
slots = _slots.slots
sources = _slots.sources

WORKFLOWS = ROOT / ".github" / "workflows"


def validate() -> None:
    source_names = sources()
    capacity = slot_count({"FACTORY_SLOTS_PER_PROVIDER": "2"})
    assert capacity == 2
    derived = slots({"FACTORY_SLOTS_PER_PROVIDER": "2"})
    assert len(derived) == capacity * len(source_names)
    assert len({item.worker for item in derived}) == len(derived)
    assert {item.source for item in derived} == set(source_names)
    assert all("model" not in item.as_record() for item in derived)
    assert all(resolve(item.worker, {"FACTORY_SLOTS_PER_PROVIDER": "2"}) == item for item in derived)
    synthetic = {"data": [
        {"id": "synthetic/paid-model", "pricing": {"prompt": "2", "completion": "4"}},
        {"id": "synthetic/unpriced-model"},
    ]}
    for source in PROVIDERS:
        result = discover(source, json.dumps(synthetic))
        assert result.status == "available", source
        assert {row.model for row in result.candidates} == {
            "synthetic/paid-model", "synthetic/unpriced-model",
        }, source
        provider = PROVIDERS[source].provider
        assert all(
            candidate.runtime_model == f"{provider}/{candidate.model}"
            for candidate in result.candidates
        )
    assert discover("opencode-free", "opencode/synthetic-model\n").candidates
    assert not discover("opencode-free", "other/synthetic-model\n").candidates
    assert worker_sources()
    some_source = next(iter(PROVIDERS))
    some_worker = next(item.worker for item in slots() if item.source == some_source)
    kept, deferred = filter_workers([some_worker], worker_sources(), {})
    assert kept == [] and len(deferred) == 1

    dispatch = (WORKFLOWS / "fixed-model-factory-dispatch.yml").read_text(encoding="utf-8")
    runner = (WORKFLOWS / "free-model-factory-run.yml").read_text(encoding="utf-8")
    entry = (WORKFLOWS / "free-model-factory-entry.yml").read_text(encoding="utf-8")
    assert "slots='.github/scripts/factory_worker_slots.py'" in dispatch
    assert 'python3 "$slots" offer' in dispatch
    assert "factory_dispatch_credential_filter.py filter" in dispatch
    assert "factory_source_budget.py filter" in dispatch
    assert 'assign --worker "$worker"' in dispatch
    assert dispatch.index("factory_dispatch_credential_filter.py filter") < dispatch.rindex(
        'dispatch_assigned_worker "$worker"'
    )
    assert 'factory_worker_slots.py resolve' in runner
    assert "factory_runtime_model_select.py" in runner
    assert "factory_published_model_catalog.py" in runner
    assert "factory_live_opencode.py" in runner
    assert "factory_kilo_catalog.py" in runner
    assert runner.count('factory_worker_slots.py resolve --worker "$FACTORY_WORKER"') == 2
    assert "[0-9]{0,2}" not in runner
    assert "Require Vercel model to remain zero-price" not in runner
    assert "selected-from-live-runtime-catalog" in runner
    assert "group: fixed-model-factory-${{ inputs.worker }}" in runner
    assert "cancel-in-progress: false" in runner
    assert "uses: ./.github/workflows/free-model-factory-run.yml" in entry
    assert "secrets: inherit" in entry

    registry = (ROOT / ".github/scripts/factory_provider_registry.py").read_text(encoding="utf-8")
    assert "explicit_models" not in registry
    print(
        json.dumps({
            "providers": len(source_names),
            "registered_catalog_providers": len(PROVIDERS),
            "synthetic_workers_validated": len(derived),
            "runtime_model_inventory_pinned": False,
        }, sort_keys=True)
    )


if __name__ == "__main__":
    validate()
