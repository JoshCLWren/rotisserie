"""Provider integrations carry endpoints, not lists of permitted model IDs."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REGISTRY_PATH = Path(".github/scripts/factory_provider_registry.py")


@pytest.fixture(scope="module")
def registry():
    spec = importlib.util.spec_from_file_location("factory_provider_registry", REGISTRY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_provider_preserves_discovered_id(registry):
    for source, provider in registry.PROVIDERS.items():
        model = "synthetic-upstream/model-v1"
        env = {provider.credential: "secret", **dict.fromkeys(provider.additional_credentials, "account")}
        resolution = registry.resolve(source, model, env)
        assert resolution["runtime_model"] == f"{provider.provider}/{model}"
        assert resolution["configured"]
        assert resolution["branch_suffix"] == source
        assert resolution["configure_adapter"]
        config = registry.compatible_config(source, model, env)["providers"][provider.provider]
        assert config["models"][model]["modelID"] == model
        assert "secret" not in str(config)
        assert config["env"] == [provider.credential]


def test_vendor_qualified_model_id_is_not_renamed(registry):
    env = {"NVIDIA_API_KEY": "secret"}
    model = "synthetic-vendor/submodel"
    assert registry.resolve("nvidia", model, env)["runtime_model"] == f"nvidia/{model}"
    assert registry.compatible_config("nvidia", model, env)["providers"]["nvidia"]["models"][model]["modelID"] == model
    assert registry.resolve("nvidia", model, env)["configure_adapter"]


def test_missing_credentials_and_unknown_sources(registry):
    synthetic = "synthetic-model"
    assert registry.resolve("groq-free", synthetic, {})["reason"] == "missing-GROQ_API_KEY"
    with pytest.raises(ValueError):
        registry.compatible_config("groq-free", synthetic, {})
    assert not registry.resolve("cloudflare-workers-ai", synthetic, {"CLOUDFLARE_API_TOKEN": "secret"})["configured"]
    with pytest.raises(ValueError):
        registry.resolve("nonexistent-provider", synthetic, {})
    for bad_model in ("", "bad\nmodel", "/missing-model"):
        with pytest.raises(ValueError):
            registry.resolve("groq-free", bad_model, {})


def test_registry_is_integration_metadata_not_model_allowlist(registry):
    assert registry.PROVIDERS
    for source, provider in registry.PROVIDERS.items():
        assert source
        assert provider.endpoint.startswith("https://")
        assert provider.credential.endswith("_KEY") or provider.credential.endswith("_TOKEN")
        assert not hasattr(provider, "explicit_models")


def test_workflow_consumes_registry():
    runner = Path(".github/workflows/free-model-factory-run.yml").read_text()
    # Lane resolution checks credentials via the registry without a model ID.
    assert "factory_provider_registry.py credentials --source" in runner
    assert "factory_provider_registry.py config" in runner
    assert "base_url='https://" not in runner
