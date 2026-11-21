"""Credential gating uses provider-derived workers, not a static roster."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/factory_dispatch_credential_filter.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("factory_dispatch_credential_filter", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_unconfigured_provider_defers_only_its_workers():
    sources = gate.worker_sources()
    from factory_worker_slots import slots

    gateway_workers = [slot.worker for slot in slots() if slot.source == "vercel-ai-gateway"]
    native_workers = [slot.worker for slot in slots() if slot.source == "opencode-free"]
    assert gateway_workers and native_workers
    picked = [*gateway_workers[:2], native_workers[0]]
    kept, deferred = gate.filter_workers(picked, sources, {})
    assert kept == []
    assert len(deferred) == 3
    kept, deferred = gate.filter_workers(
        picked, sources, {"FACTORY_HAS_AI_GATEWAY_API_KEY": "true"})
    assert kept == gateway_workers[:2]
    assert len(deferred) == 1
    assert gate.filter_workers(
        picked, sources, {
            "FACTORY_HAS_AI_GATEWAY_API_KEY": "true",
            "FACTORY_HAS_OPENCODE_GO_CLIENT_ID": "true",
        },
    ) == (picked, [])


def test_cloudflare_requires_token_and_account():
    source_map = {"11": "cloudflare-workers-ai", "12": "unknown"}
    env = {"FACTORY_HAS_CLOUDFLARE_API_TOKEN": "true"}
    assert gate.filter_workers(["11", "12"], source_map, env)[0] == []
    env["FACTORY_HAS_CLOUDFLARE_ACCOUNT_ID"] = "true"
    assert gate.filter_workers(["11", "12"], source_map, env)[0] == ["11"]


def test_workflow_filters_before_leasing():
    source = (ROOT / ".github/workflows/fixed-model-factory-dispatch.yml").read_text()
    assert "FACTORY_HAS_AI_GATEWAY_API_KEY" in source
    assert source.index("factory_dispatch_credential_filter.py filter") < source.index(
        'dispatch_assigned_worker "$worker"'
    )


def test_openrouter_free_needs_its_api_key() -> None:
    import importlib

    module = importlib.import_module("factory_dispatch_credential_filter")
    sources = {"1": "openrouter-free", "2": "opencode-free"}
    kept, _ = module.filter_workers(["1", "2"], sources, {})
    assert kept == []
    kept, _ = module.filter_workers(["2"], sources, {"FACTORY_HAS_OPENCODE_GO_CLIENT_ID": "true"})
    assert kept == ["2"]
    kept, _ = module.filter_workers(["1"], sources, {"FACTORY_HAS_OPENROUTER_API_KEY": "true"})
    assert kept == ["1"]
