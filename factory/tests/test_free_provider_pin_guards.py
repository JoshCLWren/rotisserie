"""Provider security and execution health are not hard-coded model pin guards."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
PROVIDERS = importlib.import_module("factory_provider_registry")
CANDIDATES = importlib.import_module("factory_provider_candidates")
WORKERS = importlib.import_module("factory_worker_slots")
SELECTOR = importlib.import_module("factory_runtime_model_select")


@pytest.mark.parametrize("source", sorted(PROVIDERS.PROVIDERS))
def test_every_registered_provider_accepts_unlisted_upstream_id(source: str) -> None:
    """Registered providers must accept upstream model IDs without a committed allowlist."""
    spec = PROVIDERS.PROVIDERS[source]
    env = {spec.credential: "synthetic"}
    env.update(dict.fromkeys(spec.additional_credentials, "test-account"))
    model = "synthetic/upstream-model"
    resolved = PROVIDERS.resolve(source, model, env)
    assert resolved["configured"] is True
    assert resolved["runtime_model"] == f"{spec.provider}/{model}"
    config = PROVIDERS.compatible_config(source, model, env)
    providers = config["providers"]
    assert isinstance(providers, dict)
    provider_cfg = providers[spec.provider]
    assert isinstance(provider_cfg, dict)
    models = provider_cfg["models"]
    assert isinstance(models, dict)
    model_cfg = models[model]
    assert isinstance(model_cfg, dict)
    assert model_cfg["modelID"] == model


def test_missing_credential_never_counts_as_usable_model() -> None:
    """Missing credentials must never be treated as usable provider configuration."""
    for source, spec in PROVIDERS.PROVIDERS.items():
        assert PROVIDERS.credential_status(source, {})["configured"] is False, source
        assert PROVIDERS.missing_credentials(spec, {}) != []


def test_zero_paid_and_unpriced_catalog_rows_all_enter_candidate_pool() -> None:
    """Test zero paid and unpriced catalog rows all enter candidate pool."""
    raw = json.dumps({"data": [
        {"id": "synthetic/zero", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "synthetic/paid", "pricing": {"prompt": "5", "completion": "2"}},
        {"id": "synthetic/unpriced"},
    ]})
    for provider in ("nvidia", "groq-free", "openrouter-free"):
        result = CANDIDATES.discover(provider, raw)
        assert {item.model for item in result.candidates} == {
            "synthetic/zero", "synthetic/paid", "synthetic/unpriced",
        }


def test_known_missing_tools_is_not_misrepresented_as_executable() -> None:
    """Test known missing tools is not misrepresented as executable."""
    result = CANDIDATES.discover("nvidia", json.dumps({"data": [
        {"id": "synthetic/no-tools", "supported_parameters": []},
        {"id": "synthetic/unknown-capability"},
        {"id": "synthetic/tool-capable", "supported_parameters": ["tools"]},
    ]}))
    assert {m.model for m in result.candidates} == {
        "synthetic/unknown-capability", "synthetic/tool-capable",
    }


def test_provider_worker_ids_do_not_depend_on_catalog_membership() -> None:
    """Test provider worker ids do not depend on catalog membership."""
    provider = sorted(PROVIDERS.PROVIDERS)[0]
    worker = WORKERS.worker_id(provider, 0)
    assert worker == WORKERS.worker_id(provider, 0)
    assert WORKERS.resolve(worker).source == provider
    assert "model" not in WORKERS.resolve(worker).as_record()


def test_cooling_exclusions_are_scoped_to_the_source() -> None:
    """Test cooling exclusions are scoped to the source."""
    catalog = {"data": [{"id": "synthetic/a"}]}
    with pytest.raises(SELECTOR.CandidateUnavailableError):
        SELECTOR.select("nvidia", "123", catalog, {"nvidia": ["synthetic/a"]})
    assert SELECTOR.select("groq-free", "123", catalog, {"nvidia": ["synthetic/a"]})["model"] == "synthetic/a"


def test_unsupported_provider_cannot_invent_adapter() -> None:
    """Test unsupported provider cannot invent adapter."""
    with pytest.raises(ValueError):
        CANDIDATES.discover("no-such-provider", "{}")
